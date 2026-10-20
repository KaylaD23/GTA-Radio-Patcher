// Independent, read-only numeric oracle for GTA VCS PS2 radio duration and IOP position math.
// Compile: g++ -O3 -std=c++17 vcs_exhaustive_calculator_audit.cpp -o vcs_exhaustive_calculator_audit
// Run: ./vcs_exhaustive_calculator_audit > vcs_exhaustive_calculator_results.json
#include <cstdint>
#include <iostream>
#include <stdexcept>
#include <limits>

using i64=std::int64_t;
using u64=std::uint64_t;
static constexpr i64 MIN_MS = 1000;
static constexpr i64 MAX_MS = 16740000;
static constexpr i64 PAIR = 16384;
static constexpr i64 MIN_BYTE = 49152;
static constexpr i64 MAX_SAFE_BYTES=613564416;
static constexpr i64 MAX_ALLOC_MS=16777152;
static constexpr u64 U32MAX = 0xffffffffULL;
struct Results {
    u64 milliseconds=0, pair_sizes=0, sector_positions=0, duration_position_pairs=0,
        edge_positions=0, sampled_positions=0, original_wrong_size_count=0,
        original_wrong_position_count=0, original_wrong_ms_count=0;
    i64 largest_padding_ms=0, first_bad_operand=-1, first_bad_alloc=-1;
} r;
void require(bool test,const char* msg, i64 x=0){ if(!test){std::cerr <<"FAILED "<<msg<<" value="<<x<<"\n";throw std::runtime_error(msg);} }
// Proposed overflow-free replacement for signed trunc(7*x / 8).
// All intermediate values and result fit signed int32 for any int32 input.
std::int32_t split_7_over_8(std::int32_t x) {
    std::int32_t q=x/8;        // C++ trunc toward zero
    std::int32_t rem=x%8;      // -7 <= rem <= 7
    return q*7 + (rem*7)/8;
}
i64 oracle_7_over_8(i64 x){return x*7/8;} // C++ trunc toward zero
std::int32_t original_iop(std::int32_t x) {
    u64 raw=(static_cast<u64>(static_cast<std::uint32_t>(x))*7)&U32MAX;
    i64 signed_mid=(raw<=0x7fffffffULL)?static_cast<i64>(raw):static_cast<i64>(raw)-0x100000000LL;
    if(signed_mid<0) signed_mid+=7;
    i64 result=(signed_mid>=0)?signed_mid/8:-((-signed_mid+7)/8);
    return static_cast<std::int32_t>(result);
}
void check_position(std::int32_t x, u64 &counter){
    i64 expected=oracle_7_over_8(x);
    require(split_7_over_8(x)==expected,"IOP safe split mismatch",x);
    ++counter;
    if(original_iop(x)!=expected){++r.original_wrong_position_count;if(r.first_bad_operand<0 && x>0)r.first_bad_operand=x;}
}
int main(){
try {
    // All 16,739,001 integer millisecond requests, not random samples.
    i64 prev_bytes=0;
    for(i64 source_ms=MIN_MS;source_ms<=MAX_MS;++source_ms){
        i64 pairs=(source_ms+447)/448;  // independent physical oracle: 448 ms / 16 KiB stereo pair
        i64 size_oracle=pairs*PAIR;
        i64 needed=(source_ms*256+6)/7; // separate duration-to-bytes implementation
        i64 size_calc=((needed+PAIR-1)/PAIR)*PAIR;
        require(size_calc==size_oracle,"duration-size formula disagreement",source_ms);
        i64 alloc_ms=pairs*448;
        require(alloc_ms>=source_ms && alloc_ms-source_ms<448,"padding out of range",source_ms);
        require(size_calc>=MIN_BYTE && size_calc<=MAX_SAFE_BYTES,"size bounds",source_ms);
        require(size_calc>=prev_bytes,"nonmonotonic mapped size",source_ms);
        prev_bytes=size_calc;
        // Simulate VCS's unsigned 32-bit intermediate (8*bytes - bytes), then >>8.
        u64 native_mid=((static_cast<u64>(size_calc)<<3)-static_cast<u64>(size_calc)) & U32MAX;
        i64 native_ms=static_cast<i64>(native_mid>>8);
        require(native_ms==alloc_ms,"original IOP duration not exact",source_ms);
        u64 ee_shift=(static_cast<u64>(native_ms)<<8)&U32MAX;
        i64 ee_rebuild=static_cast<i64>(ee_shift/7);
        require(ee_rebuild==size_calc,"patched EE bytes incorrect",source_ms);
        ++r.milliseconds;
        if(alloc_ms-source_ms>r.largest_padding_ms)r.largest_padding_ms=alloc_ms-source_ms;
        check_position(static_cast<std::int32_t>(size_calc),r.duration_position_pairs);
        check_position(static_cast<std::int32_t>(-size_calc),r.duration_position_pairs);
        if(original_iop(static_cast<std::int32_t>(size_calc))!=oracle_7_over_8(size_calc))++r.original_wrong_ms_count;
    }
    // All aligned stereo radio sizes across *full verified arithmetic ceiling*.
    for(i64 size=MIN_BYTE;size<=MAX_SAFE_BYTES;size+=PAIR){
        i64 ms=(size*7)/256;
        require(ms<=0xffffff && ms<=MAX_ALLOC_MS,"EE intermediate exceeded",size);
        require((size*7)<=static_cast<i64>(U32MAX),"IOP intermediate exceeded",size);
        require((ms*256)/7==size,"not reversibly encoded",size);
        u64 intermediate=((static_cast<u64>(size)<<3)-static_cast<u64>(size))&U32MAX;
        require((intermediate>>8)==static_cast<u64>(ms),"raw IOP duration miscalculated",size);
        if(original_iop(static_cast<std::int32_t>(size))!=oracle_7_over_8(size)){
            ++r.original_wrong_size_count;
            if(r.first_bad_alloc<0)r.first_bad_alloc=size;
        }
        ++r.pair_sizes;
    }
    // Every 2048-byte sector boundary, both positive and negative, up to the allowed maximum.
    for(i64 pos=0;pos<=MAX_SAFE_BYTES;pos+=2048){
        check_position(static_cast<std::int32_t>(pos),r.sector_positions);
        check_position(static_cast<std::int32_t>(-pos),r.sector_positions);
    }
    // Every byte around both wrap thresholds and endpoints (incl INT32 extremes).
    constexpr i64 edges[]={0,1,7,8,9,306783375,306783376,306783377,306783378,306783379,
                           306783380,306783381,536870910,536870911,536870912,613566754,
                           613566755,613566756,613566757,MAX_SAFE_BYTES,2147483647};
    for(i64 center:edges){
        for(i64 delta=-64;delta<=64;++delta){
            i64 x=center+delta;
            if(x>=0 && x<=INT32_MAX){
                check_position(static_cast<std::int32_t>(x),r.edge_positions);
                check_position(static_cast<std::int32_t>(-x),r.edge_positions);
            }
        }
    }
    check_position(INT32_MIN,r.edge_positions);
    check_position(INT32_MAX,r.edge_positions);
    // Reproducible randomized 1.5 million signed 32-bit operands per sign.
    std::uint32_t rng=0x5c52026U;
    for(u64 i=0;i<1500000;++i){
        rng^=rng<<13;rng^=rng>>17;rng^=rng<<5;
        std::int32_t x=static_cast<std::int32_t>(rng & 0x7fffffffU);
        check_position(x,r.sampled_positions);
        check_position(-x,r.sampled_positions);
    }
    // Positive/negative positions are checked, but the actual operands on each
    // runtime path are NOT proven to be identical to the byte position.
    std::cout << "{\n"
      "  \"source_duration_min_ms\": "<<MIN_MS<<",\n"
      "  \"source_duration_max_ms\": "<<MAX_MS<<",\n"
      "  \"source_millisecond_requests_checked\": "<<r.milliseconds<<",\n"
      "  \"distinct_16kib_file_sizes_checked\": "<<r.pair_sizes<<",\n"
      "  \"positive_and_negative_duration_position_checks\": "<<r.duration_position_pairs<<",\n"
      "  \"positive_and_negative_sector_position_checks\": "<<r.sector_positions<<",\n"
      "  \"positive_and_negative_edge_position_checks\": "<<r.edge_positions<<",\n"
      "  \"randomized_signed_position_checks\": "<<r.sampled_positions<<",\n"
      "  \"max_allocated_padding_ms\": "<<r.largest_padding_ms<<",\n"
      "  \"original_iop_wrong_at_distinct_file_size_count\": "<<r.original_wrong_size_count<<",\n"
      "  \"original_iop_wrong_at_requested_ms_count\": "<<r.original_wrong_ms_count<<",\n"
      "  \"first_bad_iop_size_boundary_bytes\": "<<r.first_bad_alloc<<",\n"
      "  \"first_bad_positive_raw_operand_tested\": "<<r.first_bad_operand<<",\n"
      "  \"all_ee_and_iop_duration_checks_passed\": true,\n"
      "  \"all_overflow_free_iop_position_oracle_checks_passed\": true,\n"
      "  \"binary_iop_patch_written\": false,\n"
      "  \"pcsx2_playback_tested\": false\n"
      "}\n";
    return 0;
} catch(std::exception &){return 1;}
}
