"""VCS adaptive offsets: real-binary safety, arithmetic and integration checks."""
import hashlib
from pathlib import Path
import math
import struct
import tempfile
import unittest

from gta_radio_backends import get_backend
from vcs_adaptive_offsets import (
    BLOCK_PAIR, PTR_HI_OFF, PTR_LO_OFF, SEGMENT_VA, STOCK_TABLE_FILE,
    STOCK_WORDS, build_executable, current_ee_baseline, make_sequence,
    patch_file, u32, select_stride,
)
from vcs_ee_overflow_patch import patched_bytes, MAX_ALIGNED_SAFE_BYTES

ROOT = Path(__file__).resolve().parent
BINARY = ROOT / 'SLUS_215.90.reference'

class TestVCSAdaptiveOffsets(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pristine = BINARY.read_bytes()

    def test_table_reproduces_real_original_signature(self):
        self.assertEqual(struct.unpack_from('<30I', self.pristine, STOCK_TABLE_FILE), STOCK_WORDS)
        self.assertEqual(current_ee_baseline(self.pristine), 'original')
        self.assertEqual(current_ee_baseline(patched_bytes(self.pristine)), 'ee-divu-only')

    def test_measured_367mb_radio_2008_points(self):
        words, report = make_sequence(367017984)
        self.assertEqual(report['table_count'], 2008)
        self.assertEqual(len(words), 2010)
        self.assertEqual(words[0], 0)
        self.assertEqual(words[-1], 0)
        self.assertEqual(len(set(words[1:-1])), 2008)
        self.assertEqual(report['selected_cursor_entries_per_cycle'], 2008)
        self.assertGreater(report['smallest_sequential_circular_gap_bytes'], 1_000_000)
        self.assertTrue(all(0 < value < 367017984 and value % BLOCK_PAIR == 0
                            for value in words[1:-1]))
        self.assertEqual(math.gcd(report['stride'], 2008), 1)

    def test_sweeps_1sec_4h39_and_every_station(self):
        for size in [BLOCK_PAIR * 3, BLOCK_PAIR * 4, BLOCK_PAIR * 65,
                     142606336, 134004736, 367017984, 612220928, MAX_ALIGNED_SAFE_BYTES]:
            with self.subTest(bytes=size):
                words, info = make_sequence(size)
                self.assertEqual(words[0], 0)
                self.assertEqual(words[-1], 0)
                self.assertEqual(len(words[1:-1]), len(set(words[1:-1])))
                self.assertTrue(all(x % BLOCK_PAIR == 0 and 0 < x < size for x in words[1:-1]))
                self.assertEqual(info['table_count'], len(words) - 2)
                self.assertEqual(math.gcd(select_stride(info['table_count']), info['table_count']), 1)

    def test_real_elf_segment_and_overflow_corrections(self):
        generated, info = build_executable(self.pristine, {'vcpr':367017984})
        phoff = struct.unpack_from('<I', generated, 0x1C)[0]
        segment = struct.unpack_from('<8I', generated, phoff + 32)
        self.assertEqual(segment[0], 1)
        self.assertEqual(segment[2], SEGMENT_VA)
        self.assertEqual(segment[5], 4 * (2008 + 2))
        self.assertEqual(segment[6], 4)  # read-only: no helper code
        self.assertEqual(segment[7], 0x1000)
        self.assertEqual(segment[1] % 0x1000, 0)
        self.assertGreater(segment[2], 0x74DF54)
        self.assertEqual(struct.unpack_from('<H', generated, 0x2C)[0], 2)
        self.assertEqual(u32(generated, PTR_HI_OFF), 0x3C040078)
        self.assertEqual(u32(generated, PTR_LO_OFF), 0x24840000)
        self.assertEqual(struct.unpack_from('<2010I', generated, segment[1]), make_sequence(367017984)[0])
        self.assertEqual(generated[STOCK_TABLE_FILE:STOCK_TABLE_FILE+120], self.pristine[STOCK_TABLE_FILE:STOCK_TABLE_FILE+120])
        self.assertEqual(u32(generated, 0xF8A8), 0x0043001B)
        self.assertEqual(u32(generated, 0xF75C), 0x0043001B)
        self.assertEqual(info['program_header_count'], 2)

    def test_ee_fixed_input_preserved_and_idempotent_output(self):
        ee_fixed = patched_bytes(self.pristine)
        from_pristine, a = build_executable(self.pristine, {'vcpr':367017984})
        from_ee_fixed, b = build_executable(ee_fixed, {'vcpr':367017984})
        self.assertEqual(from_pristine, from_ee_fixed)
        self.assertEqual(a['ee_divu_fixes'], 'applied')
        self.assertEqual(b['ee_divu_fixes'], 'preserved')

    def test_unknown_modification_rejected(self):
        data = bytearray(patched_bytes(self.pristine))
        data[0x90] ^= 1
        with self.assertRaisesRegex(ValueError, 'additional/unknown'):
            build_executable(bytes(data), {'vcpr':367017984})
        with self.assertRaises(ValueError):
            build_executable(self.pristine, {'head':367017984})
        with self.assertRaises(ValueError):
            build_executable(self.pristine, {})
        with self.assertRaises(ValueError):
            build_executable(self.pristine, {'vcpr':367017985})
        with self.assertRaises(ValueError):
            build_executable(self.pristine, {'vcpr':MAX_ALIGNED_SAFE_BYTES+BLOCK_PAIR})

    def test_no_overwrite_and_original_integrity(self):
        with tempfile.TemporaryDirectory() as root:
            source = Path(root)/'SLUS_215.90'
            target = Path(root)/'SLUS_215.90.offsets'
            source.write_bytes(self.pristine)
            result = patch_file(source,target,{'vcpr':367017984})
            self.assertEqual(result['offset_strategy'],'shared-adaptive-experimental')
            self.assertEqual(source.read_bytes(),self.pristine)
            self.assertNotEqual(target.read_bytes(),self.pristine)
            with self.assertRaises(ValueError):
                patch_file(source,target,{'vcpr':367017984})
            with self.assertRaises(ValueError):
                patch_file(source,source,{'vcpr':367017984})

    def test_profile_isolation_and_any_vcs_station(self):
        vcs = get_backend('vcs_ps2')
        self.assertFalse(hasattr(get_backend('lcs_ps2'),'patch_executable_with_adaptive_offsets'))
        with tempfile.TemporaryDirectory() as root:
            source = Path(root)/'SLUS_215.90'
            target = Path(root)/'adapted'
            source.write_bytes(self.pristine)
            result = vcs.patch_executable_with_adaptive_offsets(source,target,{'flash':142606336,'vcpr':367017984})
            self.assertEqual(result['offsets']['selected_station_max_size_bytes'],367017984)
            self.assertEqual(set(result['station_sizes_bytes']), {'flash','vcpr'})

if __name__=='__main__':
    unittest.main()

class TestVCSISOOffsetIntegration(unittest.TestCase):
    def test_build_verified_synthetic_iso_with_ee_offsets_and_original_irx(self):
        from vcs_vb_gain_install import build_radio_audio_iso
        from lcs_duration_aware_iso_patcher import Iso9660Image
        from test_lcs_native_provider import make_iso
        from test_vcs_vcpr_gain_iso import generate_vb
        from unittest.mock import patch
        import lcs_duration_aware_exe_patcher as lcs_patcher
        from gta_radio_profiles import get_profile
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            iso, output, radio = root/'input.iso', root/'with_offsets.iso', root/'VCPR.VB'
            original = BINARY.read_bytes()
            with patch.object(lcs_patcher, 'STATIONS', get_profile('vcs_ps2').stations):
                make_iso(iso, original, b'IRX_USER_POSITION_FIX_UNCHANGED')
            iso.write_bytes(iso.read_bytes().replace(b'SLUS_214.23', b'SLUS_215.90'))
            generate_vb(radio, pairs=4)
            source_hash = hashlib.sha256(iso.read_bytes()).hexdigest()
            result = build_radio_audio_iso(input_iso=iso, replacement_vb=radio,
                                           output_iso=output, expand_vcs_offsets=True)
            self.assertTrue(result['experimental_vcs_adaptive_offsets'])
            self.assertEqual(result['offset_patch_report']['offset_strategy'],'shared-adaptive-experimental')
            self.assertEqual(hashlib.sha256(iso.read_bytes()).hexdigest(), source_hash)
            built = Iso9660Image(output)
            self.assertNotEqual(built.read_entry_bytes('SLUS_215.90'), original)
            self.assertEqual(built.read_entry_bytes('MODULES/CDSTREAM.IRX'), b'IRX_USER_POSITION_FIX_UNCHANGED')
            self.assertEqual(built.read_entry_bytes('AUDIO/MUSIC/VCPR.VB'), radio.read_bytes())
            self.assertEqual(hashlib.sha256(built.read_entry_bytes('SLUS_215.90')).hexdigest(),
                             result['offset_patch_report']['patched_sha256'])
            self.assertEqual(built.volume_sectors * 2048, built.size)

    def test_unknown_prepatched_iso_does_not_publish_output(self):
        from vcs_vb_gain_install import build_radio_audio_iso
        from test_lcs_native_provider import make_iso
        from test_vcs_vcpr_gain_iso import generate_vb
        from unittest.mock import patch
        import lcs_duration_aware_exe_patcher as lcs_patcher
        from gta_radio_profiles import get_profile
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);iso=root/'input.iso';out=root/'blocked.iso';radio=root/'VCPR.VB'
            modified=bytearray(BINARY.read_bytes());modified[0x90]^=1
            with patch.object(lcs_patcher,'STATIONS',get_profile('vcs_ps2').stations):
                make_iso(iso,bytes(modified),b'IRX')
            iso.write_bytes(iso.read_bytes().replace(b'SLUS_214.23',b'SLUS_215.90'))
            generate_vb(radio,pairs=4)
            with self.assertRaises(ValueError):
                build_radio_audio_iso(input_iso=iso,replacement_vb=radio,
                                      output_iso=out,expand_vcs_offsets=True)
            self.assertFalse(out.exists())

class TestVCSOffsetGUI(unittest.TestCase):
    @unittest.skipUnless(__import__('os').environ.get('DISPLAY'), 'Tk GUI needs X11/Xvfb')
    def test_option_disabled_by_default_and_only_on_vcs(self):
        import tkinter as tk
        from gta_radio_patcher_gui import DurationPatcherGUI
        root = tk.Tk()
        try:
            app = DurationPatcherGUI(root)
            self.assertFalse(app.vcs_adaptive_offsets_var.get())
            self.assertEqual(str(app.vcs_offset_checkbox.cget('state')), 'disabled')
            app.profile_var.set('GTA VCS — PS2 (EE overflow fix)')
            app._profile_changed()
            self.assertEqual(str(app.vcs_offset_checkbox.cget('state')), 'normal')
            app.vcs_adaptive_offsets_var.set(True)
            app._vcs_offset_checkbox_changed()
            self.assertIn('OFFSET ELF',app.vcs_iso_install_button.cget('text'))
            app.profile_var.set('GTA LCS — PS2 (patching)')
            app._profile_changed()
            self.assertEqual(str(app.vcs_offset_checkbox.cget('state')), 'disabled')
        finally:
            root.destroy()
