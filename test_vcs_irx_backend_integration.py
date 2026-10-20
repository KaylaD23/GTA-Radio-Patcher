"""Fail-closed VCS GUI backend IO and profile isolation regressions."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from gta_radio_backends import get_backend
from vcs_cdstream_signed_position_patch import build
from vcs_ee_overflow_patch import MAX_ALIGNED_SAFE_BYTES

ORIGINAL = Path('/mnt/data/CDSTREAM_IRX_VCS_disc_copy.bin')

class BackendTests(unittest.TestCase):
    def test_vcs_only_patch_dispatch_outputs_module(self):
        if not ORIGINAL.exists(): self.skipTest('Reference IRX not mounted')
        with TemporaryDirectory() as td:
            output=Path(td)/'CDSTREAM.VCS_PATCHED.IRX'
            result=get_backend('vcs_ps2').patch_cdstream_position_overflow_only(
                ORIGINAL,output,{'vcpr':367017984})
            self.assertTrue(output.exists())
            self.assertEqual(output.read_bytes(),build(ORIGINAL.read_bytes())[0])
            self.assertEqual(result['stub_text_va'],'0xb460')
            self.assertFalse(get_backend('vcs_ps2').profile.can_build)
    def test_refuses_same_file_or_existing_output_and_does_not_mutate(self):
        if not ORIGINAL.exists():self.skipTest('Reference IRX not mounted')
        with TemporaryDirectory() as td:
            inp=Path(td)/'CDSTREAM.IRX'
            inp.write_bytes(ORIGINAL.read_bytes())
            digest=inp.read_bytes()
            with self.assertRaises(ValueError):
                get_backend('vcs_ps2').patch_cdstream_position_overflow_only(inp,inp,{'vcpr':367017984})
            self.assertEqual(inp.read_bytes(),digest)
            dest=Path(td)/'already.IRX';dest.write_bytes(b'KEEP')
            with self.assertRaises(ValueError):
                get_backend('vcs_ps2').patch_cdstream_position_overflow_only(inp,dest,{})
            self.assertEqual(dest.read_bytes(),b'KEEP')
    def test_rejects_invalid_station_and_outside_target(self):
        if not ORIGINAL.exists():self.skipTest('Reference IRX not mounted')
        with TemporaryDirectory() as td:
            target=Path(td)/'not-made.IRX'
            with self.assertRaises(ValueError):
                get_backend('vcs_ps2').patch_cdstream_position_overflow_only(ORIGINAL,target,{'kjah':0x4000})
            with self.assertRaises(ValueError):
                get_backend('vcs_ps2').patch_cdstream_position_overflow_only(ORIGINAL,target,{'vcpr':MAX_ALIGNED_SAFE_BYTES+16384})
            self.assertFalse(target.exists())
    def test_lcs_backend_has_no_vcs_iop_patcher(self):
        self.assertFalse(hasattr(get_backend('lcs_ps2'),'patch_cdstream_position_overflow_only'))
    def test_isolated_from_lcs_iso_builder(self):
        if not ORIGINAL.exists():self.skipTest('Reference IRX not mounted')
        with TemporaryDirectory() as td:
            with patch('lcs_duration_aware_iso_patcher.build_duration_aware_iso',side_effect=AssertionError('cross-profile call')):
                get_backend('vcs_ps2').patch_cdstream_position_overflow_only(ORIGINAL,Path(td)/'new.IRX',{})
if __name__=='__main__':unittest.main()
