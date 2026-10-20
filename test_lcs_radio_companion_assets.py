"""Regression tests: GXT and DTZ are automatically copied with radio .VB files.

These tests build a synthetic ISO9660 filesystem with the authentic original
ELF and IOP module. They verify destination records and actual output bytes;
they do NOT claim a successful PCSX2 playback test.
"""
from __future__ import annotations

from pathlib import Path
import hashlib
import tempfile
import unittest

import lcs_duration_aware_iso_patcher as iso_builder
from test_lcs_native_provider import make_iso

ROOT = Path(__file__).resolve().parent
ORIGINAL_ELF = (ROOT / 'SLUS_214.23.original').read_bytes()
ORIGINAL_IRX = (ROOT / 'CDSTREAM_ORIGINAL_REFERENCE.IRX').read_bytes()
VB_CONTENT = (b'\x0c\x00' + b'\0' * 14) * (0x40000 // 16)
DESTINATIONS = {'ENGLISH.GXT': 'TEXT/ENGLISH.GXT', 'GAME.DTZ': 'CHK/PS2/GAME.DTZ'}


class CompanionAssetTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix='lcs_radio_companion_test_')
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.source_iso = self.root / 'original.iso'
        make_iso(self.source_iso, ORIGINAL_ELF, ORIGINAL_IRX, include_companion_assets=True)
        self.vb_dir = self.root / 'radio'
        self.vb_dir.mkdir()
        (self.vb_dir / 'KJAH.VB').write_bytes(VB_CONTENT)
        self.original_entries = iso_builder.Iso9660Image(self.source_iso)
        self.original_payloads = {path: self.original_entries.read_entry_bytes(path)
                                  for path in DESTINATIONS.values()}

    def build(self, radio_dir=None):
        path = self.root / 'output.iso'
        result = iso_builder.build_duration_aware_iso(
            self.source_iso, radio_dir or self.vb_dir, path,
            offset_mode='iop-elf-stock', strict_original_reference=False,
        )
        return result, iso_builder.Iso9660Image(path)

    def test_both_files_auto_installed_byte_exact_without_extra_selection(self):
        gxt = b'new english gxt contents' * 160  # larger than original extent
        dtz = b'new game dtz contents' * 130  # larger than original extent
        (self.vb_dir / 'English.Gxt').write_bytes(gxt)
        (self.vb_dir / 'game.dtz').write_bytes(dtz)
        result, built = self.build()
        self.assertEqual(built.read_entry_bytes('TEXT/ENGLISH.GXT'), gxt)
        self.assertEqual(built.read_entry_bytes('CHK/PS2/GAME.DTZ'), dtz)
        self.assertEqual(built.read_entry_bytes('AUDIO/MUSIC/KJAH.VB'), VB_CONTENT)
        changed = {entry['iso_path'] for entry in result['iso_files_replaced']}
        self.assertTrue(set(DESTINATIONS.values()).issubset(changed))
        self.assertIn('AUDIO/MUSIC/KJAH.VB', changed)
        listed = result['automatically_installed_radio_companion_assets']
        self.assertEqual({a['iso_path'] for a in listed}, set(DESTINATIONS.values()))
        for item in listed:
            expected = gxt if item['iso_path'] == 'TEXT/ENGLISH.GXT' else dtz
            self.assertEqual(item['verified_output_sha256'], hashlib.sha256(expected).hexdigest())
            self.assertEqual(built.get(item['iso_path']).size, len(expected))
        original = iso_builder.Iso9660Image(self.source_iso)
        for name, data in self.original_payloads.items():
            self.assertEqual(original.read_entry_bytes(name), data)

    def test_only_gxt_present_leaves_game_dtz_untouched(self):
        new_gxt = b'updated translations'
        (self.vb_dir / 'ENGLISH.GXT').write_bytes(new_gxt)
        result, built = self.build()
        self.assertEqual(built.read_entry_bytes('TEXT/ENGLISH.GXT'), new_gxt)
        self.assertEqual(built.read_entry_bytes('CHK/PS2/GAME.DTZ'),
                         self.original_payloads['CHK/PS2/GAME.DTZ'])
        self.assertEqual(len(result['automatically_installed_radio_companion_assets']), 1)

    def test_only_dtz_present_leaves_english_gxt_untouched(self):
        new_dtz = b'updated icon resources'
        (self.vb_dir / 'GAME.DTZ').write_bytes(new_dtz)
        result, built = self.build()
        self.assertEqual(built.read_entry_bytes('CHK/PS2/GAME.DTZ'), new_dtz)
        self.assertEqual(built.read_entry_bytes('TEXT/ENGLISH.GXT'),
                         self.original_payloads['TEXT/ENGLISH.GXT'])
        self.assertEqual(len(result['automatically_installed_radio_companion_assets']), 1)

    def test_no_companions_keeps_both_existing_iso_assets(self):
        result, built = self.build()
        self.assertEqual(result['automatically_installed_radio_companion_assets'], [])
        for name, data in self.original_payloads.items():
            self.assertEqual(built.read_entry_bytes(name), data)
            self.assertNotIn(name, {e['iso_path'] for e in result['iso_files_replaced']})

    def test_single_vb_selection_does_not_pick_up_other_files_in_parent(self):
        (self.vb_dir / 'ENGLISH.GXT').write_bytes(b'should not install via single VB')
        (self.vb_dir / 'GAME.DTZ').write_bytes(b'should not install via single VB')
        result, built = self.build(self.vb_dir / 'KJAH.VB')
        self.assertEqual(result['automatically_installed_radio_companion_assets'], [])
        for name, data in self.original_payloads.items():
            self.assertEqual(built.read_entry_bytes(name), data)

    def test_rejects_ambiguous_companion_names_without_publishing(self):
        (self.vb_dir / 'GAME.DTZ').write_bytes(b'a')
        (self.vb_dir / 'game.dtz').write_bytes(b'b')
        with self.assertRaisesRegex(ValueError, 'Ambiguous case-insensitive duplicate'):
            self.build()
        self.assertFalse((self.root / 'output.iso').exists())

    def test_present_companion_requires_existing_iso_entry(self):
        make_iso(self.source_iso, ORIGINAL_ELF, ORIGINAL_IRX)
        (self.vb_dir / 'GAME.DTZ').write_bytes(b'new icon data')
        with self.assertRaisesRegex(ValueError, 'ISO entry not found.*CHK/PS2/GAME.DTZ'):
            self.build()
        self.assertFalse((self.root / 'output.iso').exists())

    def test_empty_companion_rejected_before_publication(self):
        (self.vb_dir / 'ENGLISH.GXT').write_bytes(b'')
        with self.assertRaisesRegex(ValueError, 'Optional radio asset is empty'):
            self.build()
        self.assertFalse((self.root / 'output.iso').exists())


if __name__ == '__main__':
    unittest.main(verbosity=2)
