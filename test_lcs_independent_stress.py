"""Repeatable independent regression checks for authenticated LCS PS2 IOP seek patch.

Offline only. Requires original reference IRX included in this same folder.
No PCSX2 playback claims. Uses a deterministic seed and logged sample coverage.
"""
from __future__ import annotations
import json, random, tempfile, unittest
from pathlib import Path
from lcs_iop_safe_seek import (patch_cdstream, duration_ms, select_extra_shift,
                                MAX_SAFE_DURATION_SIZE, ORIGINAL_SHA256)
from test_lcs_iop_safe_seek import SeekInterpreter
from lcs_radio_stream_audit import audit_vb

ROOT = Path(__file__).parent
RADIO = (ROOT/'CDSTREAM_ORIGINAL_REFERENCE.IRX').read_bytes()

class IndependentStressTests(unittest.TestCase):
    def test_mixed_sizes_and_interpolation_boundaries(self):
        rng=random.Random(0x1C5)
        max_size=(MAX_SAFE_DURATION_SIZE//0x4000)*0x4000
        sizes=[0x4000,0x8000,0x10000,0x40000,0x100000,0x1000000,
               0x3000000,63176704,110247936,125829120,134217728,
               134234112,(200000000//0x4000)*0x4000,
               (250000000//0x4000)*0x4000,367017984,
               (500000000//0x4000)*0x4000,max_size]
        sizes += [rng.randrange(1,MAX_SAFE_DURATION_SIZE//0x4000)*0x4000 for _ in range(40)]
        oracle=SeekInterpreter(RADIO,ideal_mult=True)
        total=0; nonidentical=0
        for size in sizes:
            patched,report=patch_cdstream(RADIO,[size])
            sim=SeekInterpreter(patched);dur=duration_ms(size)
            positions={0,1,2,997,1000,2000,3000,max(0,dur-1),dur,dur+1}
            for x in range(1,9):
                mid=dur*x//8
                positions.update(max(0,min(dur,mid+d)) for d in (-1000,-100,-16,-1,0,1,16,100,1000))
            positions.update(rng.randrange(0,dur+1) for _ in range(550))
            self.assertLess(report['maximum_signed_multiplication'],2**31)
            for pos in positions:
                got=sim.run(size,pos)[1]; reference=oracle.run(size,pos)[1]
                self.assertGreaterEqual(got,0,(size,pos))
                self.assertLess(got,size,(size,pos))
                self.assertEqual(got%0x4000,0,(size,pos))
                self.assertLessEqual(abs(got-reference),0x4000,(size,pos,got,reference))
                nonidentical += got != reference
                total+=1
        print('Boundary stress: %s sizes, %s sampled positions, %s non-identical sector offsets' % (len(sizes),total,nonidentical))
        self.assertGreater(total,30000)

    def test_reject_corrupted_frame_between_previous_sampling_windows(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'KJAH.VB'
            with p.open('wb') as out:
                out.write((b'\x0c\x00'+b'\x00'*14)*(0x800000//16))
            with p.open('r+b') as out:
                out.seek(0x58000);out.write(b'\xff')
            # Legacy distributed sampling missed this bad predictor.
            audit_vb(p,full_scan=False)
            with self.assertRaisesRegex(ValueError,'invalid PSX ADPCM frames'):
                audit_vb(p,full_scan=True)

    def test_natively_unsupported_file_size_refused_without_scanning(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'KJAH.VB'
            invalid=((MAX_SAFE_DURATION_SIZE//0x4000)+1)*0x4000
            with p.open('wb') as f:
                f.truncate(invalid)  # sparse metadata-only file; not read or bundled
            with self.assertRaisesRegex(ValueError,'5-hour input limit'):
                audit_vb(p)

if __name__=='__main__':unittest.main(verbosity=2)
