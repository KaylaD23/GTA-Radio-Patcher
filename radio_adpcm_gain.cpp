// GTA LCS/VCS PS2 raw stereo PSX ADPCM loudness processing (0.1..3.0 dB).
// Build: g++ -O3 -std=c++17 -Wall -Wextra radio_adpcm_gain.cpp -o vcs_adpcm_gain
// Requires fixed 0x2000-byte per-channel interleaving (0x4000-byte pairs).
// Preserves every frame's byte-1 flags and the complete file length/interleave.
#include <algorithm>
#include <array>
#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <limits>
#include <stdexcept>
#include <string>

namespace fs=std::filesystem;
constexpr std::size_t INTERLEAVE=8192, PAIR=16384, FRAME=16;
constexpr int C[5][2]={{0,0},{60,0},{115,-52},{98,-55},{122,-60}};
struct State { int32_t s1=0,s2=0; };
struct Stats { uint64_t frames=0,samples=0,clipped_target=0; int orig_peak=0,new_peak=0; long double orig_sq=0,new_sq=0; };
static int clamp16(int64_t v) { return int(v < -32768 ? -32768 : (v>32767 ? 32767 : v)); }
static int predict(int filter, State st) {
    int32_t n=C[filter][0]*st.s1+C[filter][1]*st.s2;
    // Arithmetic right shift with rounding (matches signed PSX filter convention).
    return (n+32)>>6;
}
static void advance(State &s, int sample) { s.s2=s.s1; s.s1=sample; }
static void decode_frame(const uint8_t *in, State &s, std::array<int,28> &pcm) {
    const int shift=in[0]&15, filter=in[0]>>4;
    if (filter > 4 || shift > 12)
        throw std::runtime_error("Unexpected PSX ADPCM filter/shift; stream may have a different format");
    for (int k=0;k<28;++k) {
        int nib=(k&1) ? (in[2+k/2]>>4) : (in[2+k/2]&15);
        if (nib >= 8) nib-=16;
        int sample=clamp16((nib*(1<<(12-shift))) + predict(filter,s));
        pcm[k]=sample;
        advance(s,sample);
    }
}
static void encode_frame(const std::array<int,28> &target, uint8_t flags,
                         State &state, uint8_t *out, std::array<int,28> &out_pcm) {
    uint64_t best_score=std::numeric_limits<uint64_t>::max();
    uint8_t best[16]{};
    State best_state{};
    std::array<int,28> best_samples{};
    for (int filt=0;filt<5;++filt) {
        // Estimate residual range using target history; test neighboring shifts.
        State ideal=state;
        int max_res=0;
        for (int k=0;k<28;++k) {
            int res=std::abs(target[k]-predict(filt,ideal));
            if (res>max_res) max_res=res;
            advance(ideal,target[k]);
        }
        int step=1;
        while (step<4096 && static_cast<int64_t>(step)*7<max_res)step*=2;
        int guess=12;
        for(int t=step;t>1;t>>=1)--guess;
        for(int shift=std::max(0,guess-1);shift<=std::min(12,guess+1);++shift) {
            const int quant=1<<(12-shift);
            State s=state;
            uint8_t candidate[16]{};
            candidate[0]=uint8_t(filt*16+shift);candidate[1]=flags;
            std::array<int,28> reconstructed{};
            uint64_t score=0;
            for(int k=0;k<28;++k) {
                int p=predict(filt,s);
                int n=int(std::lround(double(target[k]-p)/quant));
                n=std::max(-8,std::min(7,n));
                int decoded=clamp16(p+n*quant);
                reconstructed[k]=decoded;
                int64_t err=int64_t(target[k])-decoded;
                score+=uint64_t(err*err);
                if(k&1)candidate[2+k/2]|=uint8_t((n&15)<<4);
                else candidate[2+k/2]|=uint8_t(n&15);
                advance(s,decoded);
            }
            if (score<best_score) {
                best_score=score;best_state=s;best_samples=reconstructed;
                std::copy(candidate,candidate+16,best);
            }
        }
    }
    std::copy(best,best+16,out);
    state=best_state;
    out_pcm=best_samples;
}
int main(int argc, char **argv) {
    try {
        if (argc!=3 && argc!=4) throw std::runtime_error("Usage: radio_adpcm_gain INPUT.VB NEW_OUTPUT.VB GAIN_DB (0.1 to 3.0)");
        const std::string gain_arg=(argc==3 ? "3.0" : argv[3]);
        std::size_t consumed=0;
        const double gain_db=std::stod(gain_arg,&consumed);
        if (consumed!=gain_arg.size() || !std::isfinite(gain_db) || gain_db<0.1 || gain_db>3.0)
            throw std::runtime_error("Gain must be a finite number between 0.1 and 3.0 dB");
        const double gain=std::pow(10.0,gain_db/20.0);
        fs::path input=argv[1],output=argv[2];
        if (!fs::is_regular_file(input)) throw std::runtime_error("Input file missing");
        if(fs::exists(output)||fs::absolute(input).lexically_normal()==fs::absolute(output).lexically_normal()) throw std::runtime_error("Output exists or matches input: refusing overwrite");
        uintmax_t length=fs::file_size(input);
        if(length==0 || length%PAIR) throw std::runtime_error("PS2 stereo VB must be nonempty and 0x4000-byte aligned");
        std::ifstream src(input,std::ios::binary);
        std::ofstream dst(output,std::ios::binary|std::ios::out|std::ios::trunc);
        if(!src||!dst) throw std::runtime_error("Cannot open source or new destination");
        std::array<uint8_t,PAIR> buffer{},transformed{};
        State original[2],reencoded[2];Stats st;
        try {
            for (uintmax_t off=0;off<length;off+=PAIR) {
                src.read(reinterpret_cast<char*>(buffer.data()),PAIR);
                if(src.gcount()!=static_cast<std::streamsize>(PAIR))throw std::runtime_error("Unexpected input EOF");
                for (int ch=0;ch<2;++ch) {
                    for (std::size_t pos=ch*INTERLEAVE;pos<(ch+1)*INTERLEAVE;pos+=FRAME) {
                        std::array<int,28> decoded{},boosted{},rebuilt{};
                        decode_frame(buffer.data()+pos, original[ch],decoded);
                        for (int k=0;k<28;++k) {
                            st.samples++;
                            st.orig_peak=std::max(st.orig_peak,std::abs(decoded[k]));
                            st.orig_sq+=static_cast<long double>(decoded[k])*decoded[k];
                            double target=std::round(decoded[k]*gain);
                            if(target>32767 || target< -32768)++st.clipped_target;
                            boosted[k]=clamp16(static_cast<int64_t>(target));
                        }
                        encode_frame(boosted,buffer[pos+1],reencoded[ch],transformed.data()+pos,rebuilt);
                        for (int k=0;k<28;++k) {
                            st.new_peak=std::max(st.new_peak,std::abs(rebuilt[k]));
                            st.new_sq+=static_cast<long double>(rebuilt[k])*rebuilt[k];
                        }
                        ++st.frames;
                    }
                }
                dst.write(reinterpret_cast<const char*>(transformed.data()),PAIR);
                if (!dst)throw std::runtime_error("Output write failed");
                if ((off % (64u*1024u*1024u))==0)
                    std::cerr << "Encoded " << (off+PAIR) << "/" << length << " bytes\n";
            }
            dst.flush();
            if(!dst || fs::file_size(output)!=length)throw std::runtime_error("Output size mismatch");
            dst.close();
        } catch (...) {
            dst.close();std::error_code ec;fs::remove(output,ec);throw;
        }
        double orig_rms=std::sqrt(double(st.orig_sq/st.samples));
        double new_rms=std::sqrt(double(st.new_sq/st.samples));
        double actual_db = (orig_rms>0 && new_rms>0) ? 20*std::log10(new_rms/orig_rms) : 0;
        std::cout<<std::setprecision(10)<<"{\"requested_gain_db\":"<<gain_db<<",\"measured_decoded_rms_gain_db\":"<<actual_db
                 <<",\"original_peak\":"<<st.orig_peak<<",\"boosted_peak\":"<<st.new_peak
                 <<",\"target_clipped_samples\":"<<st.clipped_target<<",\"samples\":"<<st.samples
                 <<",\"frames\":"<<st.frames<<",\"bytes\":"<<length
                 <<",\"channel_interleave_bytes\":8192,\"channels\":2}\n";
        return 0;
    } catch(const std::exception &e) {
        std::cerr<<"PS2 ADPCM gain error: "<<e.what()<<"\n";return 2;
    }
}
