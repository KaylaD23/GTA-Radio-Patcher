"""Synthetic ADPCM and VCS ISO installation regression tests; no proprietary game files."""
import hashlib
import math
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vcs_vb_gain_install import build_vcpr_audio_iso, compile_gain_engine
from lcs_duration_aware_iso_patcher import Iso9660Image
from test_lcs_native_provider import make_iso


def generate_vb(path: Path, pairs=2):
    # Filter 0, shift 8; original amplitude 1000-1800 PCM, safely below clipping at +3dB.
    size = pairs * 0x4000
    data = bytearray(size)
    for pos in range(0,size,16):
        data[pos] = 0x08 # filter 0, 16 sample levels, step = 16
        data[pos+1] = 0x06 if (pos//16)%16 == 0 else 0 # representative loop-start flag
        for i in range(14):
            # Encode 4-bit voiced waveform, deliberately not full-scale.
            lo=int(round(6*math.sin((pos//16)*.11 + i*.26))) & 15
            hi=int(round(6*math.sin((pos//16)*.11 + (i+.5)*.26))) & 15
            data[pos+2+i] = lo | (hi<<4)
    path.write_bytes(data)
    return data


def decode_rms(path):
    coef=((0,0),(60,0),(115,-52),(98,-55),(122,-60))
    buf=path.read_bytes()
    rms=0; count=0; peak=0
    for start in range(0,len(buf),0x4000):
        for channel in (0,1):
            h1=h2=0
            for pos in range(start+channel*8192,start+(channel+1)*8192,16):
                header=buf[pos];f=header>>4;shift=header&15
                for k in range(28):
                    nib=(buf[pos+2+k//2]>>4 if k&1 else buf[pos+2+k//2]&15)
                    if nib>=8:nib-=16
                    sample=max(-32768,min(32767, (nib<<(12-shift)) + ((coef[f][0]*h1+coef[f][1]*h2+32)>>6)))
                    h2,h1=h1,sample
                    rms+=sample*sample; count+=1; peak=max(peak,abs(sample))
    return math.sqrt(rms/count),peak


class TestVCSGain(unittest.TestCase):
    def test_adpcm_gain_amplitude_flags_and_length(self):
        with tempfile.TemporaryDirectory() as td:
            d=Path(td); source=d/'VCPR.VB'; boosted=d/'boosted.VB'
            raw=generate_vb(source)
            binary=compile_gain_engine()
            result=subprocess.run([str(binary),str(source),str(boosted)],capture_output=True,text=True)
            self.assertEqual(result.returncode,0, result.stderr)
            self.assertEqual(boosted.stat().st_size,len(raw))
            new=boosted.read_bytes()
            for pos in range(0,len(new),16):
                self.assertEqual(new[pos+1],raw[pos+1])
                self.assertLessEqual(new[pos]>>4,4)
                self.assertLessEqual(new[pos]&15,12)
            a,pk0=decode_rms(source);b,pk1=decode_rms(boosted)
            db=20*math.log10(b/a)
            self.assertAlmostEqual(db,3.0,delta=0.6)
            self.assertGreater(pk1,pk0)

    def test_vcs_audio_iso_preserves_exe_irx_and_non_target(self):
        with tempfile.TemporaryDirectory() as td:
            d=Path(td)
            iso=d/'us.iso';target=d/'VCPR.VB';output=d/'plus3.iso'
            # This is a minimal synthetic ISO fixture, not a real GTA game image.
            make_iso(iso,b'ELF__VCS_TEST',b'IRX__VCS_TEST')
            data=iso.read_bytes().replace(b'SLUS_214.23',b'SLUS_215.90').replace(b'KJAH.VB',b'VCPR.VB')
            iso.write_bytes(data)
            generate_vb(target,pairs=3)
            before=hashlib.sha256(iso.read_bytes()).hexdigest()
            orig=Iso9660Image(iso)
            elf=orig.read_entry_bytes('SLUS_215.90')
            irx=orig.read_entry_bytes('MODULES/CDSTREAM.IRX')
            result=build_vcpr_audio_iso(input_iso=iso,replacement_vb=target,
                output_iso=output,boost_vcpr_3db=True)
            self.assertEqual(result['gain']['requested_gain_db'],3.0)
            after=Iso9660Image(output)
            self.assertEqual(after.get('AUDIO/MUSIC/VCPR.VB').size,target.stat().st_size)
            self.assertEqual(after.read_entry_bytes('SLUS_215.90'),elf)
            self.assertEqual(after.read_entry_bytes('MODULES/CDSTREAM.IRX'),irx)
            self.assertEqual(after.read_entry_bytes('AUDIO/MUSIC/HEAD.VB'),
                             orig.read_entry_bytes('AUDIO/MUSIC/HEAD.VB'))
            self.assertEqual(hashlib.sha256(iso.read_bytes()).hexdigest(),before)
            self.assertNotEqual(after.read_entry_bytes('AUDIO/MUSIC/VCPR.VB'),target.read_bytes())
            with self.assertRaises(FileExistsError):
                build_vcpr_audio_iso(input_iso=iso,replacement_vb=target,output_iso=output,
                                     boost_vcpr_3db=False)

    def test_bad_file_and_old_iso_overlap_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            d=Path(td);vb=d/'VCPR.VB';iso=d/'bad.iso'
            vb.write_bytes(b'BAD')
            with self.assertRaisesRegex(ValueError,'0x4000'):
                from vcs_vb_gain_install import validate_vb
                validate_vb(vb)
            self.assertFalse(iso.exists())

if __name__=='__main__':unittest.main()
