"""Adversarial tests for exact GUI->calculator->EE/IOP patch destination decisions.

Static safety tests only. No PCSX2 audio playback certification.
"""
import json
import struct
import tempfile
import unittest
from pathlib import Path

import lcs_duration_aware_exe_patcher as exe
import lcs_duration_aware_iso_patcher as iso
from lcs_patch_calculator import calculate_plan
from lcs_iop_safe_seek import (
    NATIVE_UNPATCHED_DURATION_LIMIT, NATIVE_POSITION_SIGNED_LIMIT,
    MAX_SAFE_DURATION_SIZE, patch_cdstream,
)
from lcs_stream_bounds import stage_audio_with_short_stream_guard

BASE = Path(__file__).parent
KEYS = [k for k, _ in exe.STATIONS]
# Use actual ISO sizes if supplied; these are real game directory-record sizes.
REAL_GAME_ORIGINAL_SIZES = dict(zip(KEYS,[
    58392576, 58245120, 63176704, 92897280, 61390848,
    73728000, 85147648, 63537152, 104431616, 110247936,
]))
LONG_SIZE = 367_017_984


class PatchDestinationAuditTests(unittest.TestCase):
    def test_exact_source_and_all_ten_station_keys(self):
        self.assertEqual(set(REAL_GAME_ORIGINAL_SIZES), set(KEYS))
        for invalid in (float(0x4000), '16384', 0x4000 + 0.1, True, None):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                calculate_plan(REAL_GAME_ORIGINAL_SIZES, {'head':invalid})
        swapped=dict(REAL_GAME_ORIGINAL_SIZES)
        swapped['head_fake'] = swapped.pop('head')
        with self.assertRaises(ValueError):
            calculate_plan(swapped, {'kjah':LONG_SIZE})
        bad_original=dict(REAL_GAME_ORIGINAL_SIZES)
        bad_original['head']='58392576'
        with self.assertRaises(ValueError):
            calculate_plan(bad_original,{})

    def test_three_distinct_actual_limits_and_full_range(self):
        plan=calculate_plan(REAL_GAME_ORIGINAL_SIZES,{'kjah':LONG_SIZE})
        info=plan.as_dict()
        self.assertEqual(info['original_32_bit_duration_limit'],NATIVE_UNPATCHED_DURATION_LIMIT)
        self.assertEqual(info['original_signed_position_limit'],NATIVE_POSITION_SIGNED_LIMIT)
        self.assertEqual(info['supported_max_aligned_stream_bytes'],MAX_SAFE_DURATION_SIZE)
        self.assertEqual(info['stations']['kjah']['ee_boundary_ms'],10_035_648)
        self.assertTrue(plan.iop_needs_instruction_patch)
        self.assertTrue(plan.iop_position_overflow_fix_required)
        self.assertFalse(plan.iop_duration_overflow_fix_required)
        self.assertEqual(plan.iop_extra_shift,4)

    def test_every_station_long_and_mixed_lengths_enforces_iop(self):
        for key in KEYS:
            plan=calculate_plan(REAL_GAME_ORIGINAL_SIZES,{key:LONG_SIZE})
            self.assertEqual({s.station:s.ee_boundary_ms for s in plan.stations}[key],10_035_648)
            self.assertTrue(plan.iop_needs_instruction_patch)
            for mode in ('native','stock','shared','station-specific'):
                with self.subTest(station=key,mode=mode), self.assertRaisesRegex(ValueError,'CDSTREAM.IRX'):
                    iso.validate_mode_against_calculated_iop_requirements(plan,mode)
            for mode in iso.IOP_PATCH_MODES:
                iso.validate_mode_against_calculated_iop_requirements(plan,mode)
        for size in (0x4000,0x40000,0x200000,0x1800000,0x10000000,0x13000000,0x19000000,
                     ((NATIVE_UNPATCHED_DURATION_LIMIT//0x4000)+1)*0x4000,MAX_SAFE_DURATION_SIZE):
            plan=calculate_plan(REAL_GAME_ORIGINAL_SIZES,{'head':size,'flash':0x4000,'lcfr':0x80000})
            self.assertEqual(plan.stations[0].ee_boundary_ms,max(size,0x40000)*7//256)
            self.assertEqual(next(s for s in plan.stations if s.station=='flash').installed_size,0x40000)
            if plan.iop_needs_instruction_patch:
                with self.assertRaisesRegex(ValueError,'CDSTREAM.IRX'):
                    iso.validate_mode_against_calculated_iop_requirements(plan,'shared')
            else:
                iso.validate_mode_against_calculated_iop_requirements(plan,'shared')

    def test_padded_audio_does_not_delete_existing_output(self):
        with tempfile.TemporaryDirectory() as td:
            base=Path(td)
            src=base/'HEAD.VB'
            src.write_bytes(b'\x0c\x00'+bytes(14) * 1 if False else (b'\x0c\x00'+bytes(14))*(0x4000//16))
            out=base/'staged.VB'
            out.write_bytes(b'KEEP_EXISTING')
            with self.assertRaises(FileExistsError):
                stage_audio_with_short_stream_guard(src,out)
            self.assertEqual(out.read_bytes(),b'KEEP_EXISTING')

    def test_every_station_binary_boundary_written_at_correct_index(self):
        with tempfile.TemporaryDirectory() as td:
            t=Path(td)
            source=BASE/'SLUS_214.23.original'
            vb={}
            sizes=[0x40000,0x240000,0x400000,0x800000,0x1000000,
                   0x2000000,0x4000000,0x8000000,LONG_SIZE,MAX_SAFE_DURATION_SIZE]
            for (key,_filename), size in zip(exe.STATIONS,sizes):
                f=t/(key+'.VB')
                with f.open('wb') as fd: fd.truncate(size)  # stat-only binary verification, not gameplay audio
                vb[key]=f
            dest=t/'patched_exe'
            report=exe.build_patched_executable(source,vb,dest,offset_mode='stock')
            elf=dest.read_bytes()
            start=exe.EXPECTED_SEGMENT_OFFSET+(exe.LENGTH_TABLE_VA-exe.EXPECTED_SEGMENT_VADDR)
            expected=tuple(s*7//256 for s in sizes)
            observed=struct.unpack_from('<10I',elf,start)
            self.assertEqual(observed,expected)
            self.assertEqual(tuple(s['boundary_value_written'] for s in report['stations']),expected)
            self.assertEqual(len(elf)>=exe.EXPECTED_SIZE,True)
            # Compare the IOP opcodes to the same calculator used for the station values.
            plan=calculate_plan(REAL_GAME_ORIGINAL_SIZES,dict(zip(KEYS,sizes)))
            irx=(BASE/'CDSTREAM_ORIGINAL_REFERENCE.IRX').read_bytes()
            modified,rep=patch_cdstream(irx,sizes)
            self.assertEqual(rep['extra_shift'],plan.iop_extra_shift)
            if plan.iop_position_overflow_fix_required:
                self.assertTrue(rep['position_overflow_fix_required'])
            if plan.iop_duration_overflow_fix_required:
                self.assertTrue(rep['duration_overflow_fix_required'])

if __name__=='__main__':
    unittest.main()
