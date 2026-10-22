"""Real supported VCS ELF byte-level tests; synthetic arithmetic and rejection cases."""
import hashlib
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vcs_ee_overflow_patch import (PATCHES, MAX_TIME_MS, UnsupportedBinaryError,
    check_pristine, patched_bytes, verify_patch, validate_radio_sizes,
    original_ee_conversion, repaired_ee_conversion, patch_file)
from gta_radio_backends import get_backend

ORIGINAL = Path(__file__).with_name('SLUS_215.90.reference')


class TestVCSArithmetic(unittest.TestCase):
    def test_reference_file_exists(self):
        self.assertTrue(ORIGINAL.is_file(), 'Extract original supported VCS executable for test')

    @classmethod
    def setUpClass(cls):
        cls.raw=ORIGINAL.read_bytes()

    def test_original_identity_verified(self):
        check_pristine(self.raw)

    def test_new_long_vcpr_exhibits_original_bug(self):
        size=367_017_984
        t=size*7//256
        self.assertEqual(t, 10_035_648)
        self.assertEqual(original_ee_conversion(t), -246_548_772)
        self.assertEqual(repaired_ee_conversion(t), size)

    def test_stock_and_short_replacements_unchanged(self):
        for size in [142_606_336,134_004_736,275_251_200, 367_017_984]:
            if size%0x4000:continue
            t=size*7//256
            self.assertEqual(repaired_ee_conversion(t),size)
            if t<0x800000:
                self.assertEqual(original_ee_conversion(t),size)

    def test_extreme_operands_and_limit(self):
        for t in [0,1,2,3,7,255,256,3899392, 8_388_607,
                  8_388_608, 10_035_648, 16_777_214,MAX_TIME_MS]:
            self.assertEqual(repaired_ee_conversion(t),(t*256)//7)
        self.assertLess(original_ee_conversion(8_388_608),0)
        with self.assertRaises(ValueError):repaired_ee_conversion(MAX_TIME_MS+1)
        with self.assertRaises(ValueError):repaired_ee_conversion(-1)

    def test_outside_limit_refused(self):
        with self.assertRaises(ValueError):validate_radio_sizes([658_292_736])
        with self.assertRaises(ValueError):validate_radio_sizes([0])
        with self.assertRaises(ValueError):validate_radio_sizes([123])

    def test_real_mips_opcode_change_exact(self):
        patched=patched_bytes(self.raw)
        verify_patch(self.raw,patched)
        for off,old,new,_,_ in PATCHES:
            self.assertEqual(struct.unpack_from('<I',self.raw,off)[0],old)
            self.assertEqual(struct.unpack_from('<I',patched,off)[0],new)
        self.assertEqual(sum(a!=b for a,b in zip(self.raw,patched)),2)

    def test_source_mutation_refused(self):
        fake=bytearray(self.raw)
        fake[0xFA5]=0xFF
        with self.assertRaises(UnsupportedBinaryError):patched_bytes(bytes(fake))
        with self.assertRaises(UnsupportedBinaryError):patched_bytes(b'other game')
        with self.assertRaises(UnsupportedBinaryError):patched_bytes(patched_bytes(self.raw))

    def test_wrong_output_disallowed(self):
        patched=bytearray(patched_bytes(self.raw))
        patched[0x10ff0]^=1
        with self.assertRaises(UnsupportedBinaryError):verify_patch(self.raw,bytes(patched))

    def test_standalone_output_no_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            inp=Path(tmp)/'SLUS_215.90'; inp.write_bytes(self.raw)
            dst=Path(tmp)/'SLUS_215.90.overflow_fixed'
            plan=patch_file(inp,dst,[367_017_984])
            self.assertTrue(plan['output_verified'])
            self.assertEqual(inp.read_bytes(),self.raw)
            self.assertEqual(dst.read_bytes(),patched_bytes(self.raw))
            self.assertEqual(len(plan['patches']),2)
            with self.assertRaises(FileExistsError):patch_file(inp,dst)
            self.assertEqual(dst.read_bytes(),patched_bytes(self.raw))
            with self.assertRaises(ValueError):patch_file(inp,inp)
            self.assertFalse(list(Path(tmp).glob('*.bak')))

    def test_large_invalid_vb_fails_before_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            inp=Path(tmp)/'SLUS_215.90'; inp.write_bytes(self.raw)
            dst=Path(tmp)/'patched'
            with self.assertRaises(ValueError):patch_file(inp,dst,[658_292_736])
            self.assertFalse(dst.exists())

    def test_cross_game_dispatch_only_vcs(self):
        with tempfile.TemporaryDirectory() as tmp:
            inp=Path(tmp)/'SLUS_215.90';inp.write_bytes(self.raw)
            dst=Path(tmp)/'patched'
            result=get_backend('vcs_ps2').patch_executable_overflow_only(
                inp,dst,{'vcpr':367_017_984})
            self.assertIn('signed DIV for negative',result['ee_position_negative_guard'])
            self.assertTrue(dst.is_file())
            with self.assertRaises(ValueError):get_backend('vcs_ps2').patch_executable_overflow_only(
                inp,Path(tmp)/'other',{'kjah':367_017_984})

    def test_negative_position_diagnostic(self):
        # Negative error sentinels do not retain DIV semantics after DIVU.
        # This must remain an explicit runtime caveat until RPC results traced.
        raw=(-1<<8)&0xFFFFFFFF
        prior=(-256)//7 if -256>=0 else -(256//7)
        after=raw//7
        self.assertNotEqual(prior,after)


if __name__ == '__main__':unittest.main()
