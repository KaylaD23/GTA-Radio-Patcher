"""VCS-only numerical tests. These do not validate a patched IRX in PCSX2."""
import random
import unittest
from pathlib import Path

from vcs_iop_position_math import (
    MIN_I32, MAX_I32, SIGNED_ORIGINAL_RAW_THRESHOLD, ideal_signed_position,
    original_iop_signed_position, proposed_safe_split, audit_original_irx)
from vcs_ee_overflow_patch import (
    MIN_ALIGNED_1S_BYTES, MAX_ALIGNED_SAFE_BYTES, aligned_bytes_for_source_duration)
from gta_radio_backends import get_backend

class SignedPositionMathTests(unittest.TestCase):
    def test_original_irx_exact_opcode_checks_when_available(self):
        p = Path(__file__).parent.parent / 'CDSTREAM_IRX_VCS_disc_copy.bin'
        if not p.exists():
            self.skipTest('Original VCS IRX reference not distributed; use --verify-irx locally')
        r=audit_original_irx(p)
        self.assertEqual(r['instruction_sites_verified'],10)
        self.assertFalse(r['binary_patch_applied'])

    def test_full_16k_supported_file_size_domain_and_both_signs(self):
        checked=0;broken=0
        for b in range(MIN_ALIGNED_1S_BYTES,MAX_ALIGNED_SAFE_BYTES+1,0x4000):
            for x in (b,-b):
                self.assertEqual(proposed_safe_split(x),ideal_signed_position(x))
            broken += original_iop_signed_position(b) != ideal_signed_position(b)
            checked+=1
        self.assertEqual(checked,37447)
        self.assertEqual(broken,18725)

    def test_16_bit_64_bit_and_wrap_boundaries(self):
        edges=[0,1,-1,7,-7,8,-8,9,-9,306783378,306783379,
               306783380,-306783378,-306783379,536870911,
               -536870911,613564416,-613564416,MIN_I32,MAX_I32]
        for x in edges:
            self.assertEqual(proposed_safe_split(x),ideal_signed_position(x))
        self.assertEqual(original_iop_signed_position(306783378),ideal_signed_position(306783378))
        self.assertNotEqual(original_iop_signed_position(306783379),ideal_signed_position(306783379))
        self.assertEqual(SIGNED_ORIGINAL_RAW_THRESHOLD,306783378)

    def test_all_source_durations_at_single_second_boundaries(self):
        # Full millisecond sweep is in the independent C++ oracle; this is
        # an additional lightweight implementation-level regression.
        for src in range(1000,16740001,1000):
            b=aligned_bytes_for_source_duration(src)
            self.assertEqual(proposed_safe_split(b),ideal_signed_position(b))
            self.assertEqual(proposed_safe_split(-b),ideal_signed_position(-b))

    def test_all_sectors_for_both_signs(self):
        for x in range(0, MAX_ALIGNED_SAFE_BYTES+1,2048):
            self.assertEqual(proposed_safe_split(x),ideal_signed_position(x))
            self.assertEqual(proposed_safe_split(-x),ideal_signed_position(-x))

    def test_one_hundred_thousand_random_signed_inputs(self):
        rng=random.Random(20261019)
        for _ in range(100000):
            x=rng.randint(MIN_I32,MAX_I32)
            self.assertEqual(proposed_safe_split(x),ideal_signed_position(x))

    def test_vcs_backend_retains_nine_stations_and_lcs_separation(self):
        plan=get_backend('vcs_ps2').calculate({'vcpr':142606336},{'vcpr':367017984})
        self.assertEqual(len(plan.stations),9)
        row=next(s for s in plan.stations if s.station=='vcpr')
        self.assertEqual(row.iop_original_if_operand_equals_file_size,-215730176)
        self.assertEqual(row.iop_ideal_if_operand_equals_file_size,321140736)
        self.assertEqual(row.iop_safe_model_if_operand_equals_file_size,321140736)
        self.assertTrue(plan.signed_position_operand_risk_if_raw_bytes)
        self.assertFalse(get_backend('vcs_ps2').profile.can_build)
        self.assertEqual(len(get_backend('lcs_ps2').profile.stations),10)

    def test_reject_invalid_operands(self):
        for x in [0.0,1.5,True,MAX_I32+1,MIN_I32-1,'100']:
            with self.assertRaises(ValueError): proposed_safe_split(x)

if __name__=='__main__':unittest.main()
