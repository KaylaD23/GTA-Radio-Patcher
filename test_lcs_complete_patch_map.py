"""Strict per-station original lookup and complete-chain write verification.

Binary verification only; playback requires independent PCSX2 observation.
"""
import hashlib
import struct
import tempfile
import unittest
from pathlib import Path

import lcs_duration_aware_exe_patcher as exe
import lcs_duration_aware_iso_patcher as iso
from lcs_patch_location_registry import (
    VERIFIED_STOCK_SIZES, locate_original_values, make_write_manifest,
    verify_executable_slots, verify_iop_words,
)
from lcs_iop_safe_seek import patch_cdstream

BASE = Path(__file__).resolve().parent
GAME_ISO = Path('/mnt/data/GTA_LCS_reconstructed/Grand Theft Auto Liberty City Stories PS2.iso')

class CompletePatchMapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not GAME_ISO.is_file():
            raise unittest.SkipTest('Reference retail ISO not mounted')
        cls.input_iso = iso.Iso9660Image(GAME_ISO)
        cls.lookup = locate_original_values(cls.input_iso)

    def test_original_lookup_has_ten_authenticated_destinations(self):
        self.assertEqual(self.lookup.original_sizes, VERIFIED_STOCK_SIZES)
        self.assertEqual(list(self.lookup.original_sizes), [key for key, _ in exe.STATIONS])
        for index, (key, filename) in enumerate(exe.STATIONS):
            site = self.lookup.station(filename)
            self.assertEqual(site.ee_runtime_duration_table_index, index)
            self.assertEqual(site.ee_runtime_duration_table_elf_offset,
                             exe.EXPECTED_SEGMENT_OFFSET + exe.LENGTH_TABLE_VA - exe.EXPECTED_SEGMENT_VADDR + 4 * index)
            self.assertEqual(self.input_iso.get(site.iso_path).size, site.original_size_bytes)

    def test_any_station_and_mixed_duration_map_all_needed_steps(self):
        for key, _ in exe.STATIONS:
            sizes = {key: 367_017_984}
            calc = self.lookup.calculate(sizes)
            man = make_write_manifest(self.lookup, calc, 'iop-elf-shared')
            self.assertTrue(man['complete_length_patch_path_selected'])
            self.assertTrue(man['iop_position_fix_required'])
            self.assertEqual(man['offset_placement']['calculated_candidate_entries'], 2008)
            self.assertEqual(man['offset_placement']['mode'], 'shared')
            self.assertEqual(len(man['station_writes']), 10)
            row = next(s for s in man['station_writes'] if s['station']==key)
            self.assertEqual(row['new_duration_ms'], 10_035_648)
            self.assertEqual(row['size_difference_bytes'],367_017_984 - VERIFIED_STOCK_SIZES[key])
            self.assertTrue(any(x['role']=='position-overflow' and x['required'] for x in man['iop_change_sites']))
        mixed = self.lookup.calculate({'head':0x4000,'kjah':367_017_984,'flash':0x80000})
        self.assertEqual(next(r.installed_size for r in mixed.stations if r.station=='head'),0x40000)
        self.assertEqual(make_write_manifest(self.lookup,mixed,'iop-elf-shared')['offset_placement']['calculated_candidate_entries'],2008)

    def test_partial_modes_marked_not_complete(self):
        calc = self.lookup.calculate({'lcfr':367_017_984})
        for mode in ('iop-elf-stock', 'iop-safe-seek'):
            manifest = make_write_manifest(self.lookup, calc, mode)
            self.assertFalse(manifest['complete_length_patch_path_selected'])
            self.assertTrue(manifest['partial_mode_warning'])
        for mode in ('native','stock','shared','station-specific'):
            with self.assertRaisesRegex(ValueError, 'CDSTREAM.IRX'):
                make_write_manifest(self.lookup, calc, mode)

    def test_executable_full_chain_and_intentional_corruption(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = {}
            for key, _filename in exe.STATIONS:
                p=root/f'{key}.VB'
                p.write_bytes(b'\x0c\x00'+bytes(14))
                # Stateless tests only: sizing is based on the actual bytes on disk.
                with p.open('r+b') as fh:
                    fh.truncate(367_017_984 if key=='head' else VERIFIED_STOCK_SIZES[key])
                paths[key]=p
            src=BASE/'SLUS_214.23.original'
            calc=self.lookup.calculate({'head':367_017_984})
            for mode in ('shared','station-specific','stock'):
                dest=root/f'{mode}.ELF'
                exe.build_patched_executable(src, paths, dest, offset_mode=mode)
                man=make_write_manifest(self.lookup, calc, 'iop-elf-'+mode)
                data=dest.read_bytes()
                verify_executable_slots(data,man)
                tampered=bytearray(data)
                tampered[exe.GETTER_FILE_OFFSET]^=1
                with self.assertRaises(AssertionError):
                    verify_executable_slots(bytes(tampered),man)
                if mode=='shared':
                    tampered=bytearray(data)
                    tampered[exe.OFFSET_PTR_HI_FILE_OFFSET]^=1
                    with self.assertRaises(AssertionError):
                        verify_executable_slots(bytes(tampered),man)
                    ph=struct.unpack_from('<8I',data,exe.SECOND_PHDR_OFFSET)
                    tampered=bytearray(data)
                    struct.pack_into('<I',tampered,ph[1]+4,0)
                    with self.assertRaises(AssertionError):
                        verify_executable_slots(bytes(tampered),man)

    def test_iop_modification_exact_and_detects_tampering(self):
        pristine=(BASE/'CDSTREAM_ORIGINAL_REFERENCE.IRX').read_bytes()
        calc=self.lookup.calculate({'flash':367_017_984})
        patched,_=patch_cdstream(pristine,[s.installed_size for s in calc.stations])
        verify_iop_words(patched,pristine,calc,True)
        corrupt=bytearray(patched)
        corrupt[0xA0+0x1538]^=1
        with self.assertRaises(AssertionError):
            verify_iop_words(bytes(corrupt),pristine,calc,True)

if __name__=='__main__': unittest.main()
