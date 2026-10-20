"""VCS-only 1 s–4 h 39 m arithmetic, profile separation and output-safety tests.

Static checks prove that both identified EE and IOP expressions can represent
all allocated 16 KiB channel pairs in the target range. They DO NOT prove
unverified IOP position operands, EOF handling or PCSX2 gameplay.
"""
import math
import random
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vcs_ee_overflow_patch import (
    MAX_ALIGNED_SAFE_BYTES, MAX_ALIGNED_SAFE_MS, MIN_ALIGNED_1S_BYTES,
    TARGET_MIN_SOURCE_MS, TARGET_MAX_SOURCE_MS, MAX_DURATION_BYTES, MAX_TIME_MS,
    aligned_bytes_for_source_duration, validate_radio_sizes, repaired_ee_conversion,
    original_ee_conversion, patched_bytes,
)
from gta_radio_backends import get_backend, UnverifiedGameWriterError
from gta_radio_profiles import PROFILES
from vcs_radio_calculator import calculate_plan


class TargetRangeTests(unittest.TestCase):
    def test_one_second_source_is_1344ms_allocation(self):
        size = aligned_bytes_for_source_duration(1_000)
        self.assertEqual(size, 49_152)
        self.assertEqual(size, MIN_ALIGNED_1S_BYTES)
        result = validate_radio_sizes([size])[0]
        self.assertEqual(result['duration_ms'], 1_344)
        self.assertEqual(result['patched_ee_position'], size)

    def test_upper_user_target_4h39min(self):
        size = aligned_bytes_for_source_duration(4*3_600_000 + 39*60_000)
        self.assertEqual(size, 612_220_928)
        item = validate_radio_sizes([size])[0]
        self.assertEqual(item['duration_ms'], 16_740_416)
        self.assertEqual(item['patched_ee_position'], size)
        self.assertLessEqual(size * 7, 0xFFFFFFFF)
        self.assertLessEqual(item['duration_ms'], 0xFFFFFF)
        self.assertLess(item['original_ee_position'], 0)

    def test_absolute_safe_16k_pair_ceiling(self):
        self.assertEqual(MAX_ALIGNED_SAFE_BYTES, 613_564_416)
        self.assertEqual(MAX_ALIGNED_SAFE_MS, 16_777_152)
        self.assertLessEqual(MAX_ALIGNED_SAFE_BYTES * 7, 0xFFFFFFFF)
        self.assertEqual(repaired_ee_conversion(MAX_ALIGNED_SAFE_MS),MAX_ALIGNED_SAFE_BYTES)
        self.assertLessEqual(MAX_ALIGNED_SAFE_MS, MAX_TIME_MS)
        with self.assertRaises(ValueError):
            validate_radio_sizes([MAX_ALIGNED_SAFE_BYTES + 16_384])

    def test_source_one_ms_under_and_one_ms_above_rejected(self):
        for ms in (999, 16_740_001, -1, 0, 1.0, True, '1000'):
            with self.subTest(ms=ms), self.assertRaises(ValueError):
                aligned_bytes_for_source_duration(ms)

    def test_not_one_second_audio_or_non_aligned_rejected_for_patch(self):
        for size in (0, 16_384, 32_768, 49_153, 275_257_344, -16_384, 658_292_736):
            with self.subTest(size=size), self.assertRaises(ValueError):
                validate_radio_sizes([size])

    def test_every_aligned_allocated_stream_stays_within_32_bit_mechanics(self):
        # 37,447 independent block counts; complete sweep of the target's
        # physical allocation domain, not just random endpoint spot checks.
        first=MIN_ALIGNED_1S_BYTES//16_384
        last=MAX_ALIGNED_SAFE_BYTES//16_384
        for count in range(first,last+1):
            size=count*16_384
            duration=size*7//256
            self.assertEqual(duration,count*448)
            self.assertLessEqual(size*7,0xFFFFFFFF)
            self.assertLessEqual(duration,MAX_TIME_MS)
            self.assertEqual((duration*256)//7,size)
            self.assertEqual(repaired_ee_conversion(duration),size)

    def test_100000_requested_source_durations_are_coverable(self):
        rng=random.Random(20261019)
        values=[1_000,1_001,1_343,1_344,1_345,8_388_607,8_388_608,
                10_035_648,16_740_000]
        values.extend(rng.randint(1_000,16_740_000) for _ in range(100_000))
        for source_ms in values:
            size=aligned_bytes_for_source_duration(source_ms)
            allocated_ms=size*7//256
            self.assertGreaterEqual(allocated_ms,source_ms)
            self.assertLess(allocated_ms-source_ms,448)
            self.assertLessEqual(size,MAX_ALIGNED_SAFE_BYTES)
            self.assertEqual(repaired_ee_conversion(allocated_ms),size)

    def test_vcs_plan_identifies_range_but_lcs_does_not_change(self):
        original=142_606_336
        for key,_ in PROFILES['vcs_ps2'].stations:
            plan=get_backend('vcs_ps2').calculate({key:original},{key:aligned_bytes_for_source_duration(1_000)})
            row=next(x for x in plan.stations if x.station==key)
            self.assertEqual(row.classification,'shorter')
            self.assertTrue(plan.all_selected_replacements_within_target)
            self.assertTrue(row.within_vcs_1s_to_4h39_target_allocation)
            high=aligned_bytes_for_source_duration(TARGET_MAX_SOURCE_MS)
            plan=get_backend('vcs_ps2').calculate({key:original},{key:high})
            self.assertEqual(next(x for x in plan.stations if x.station==key).classification,'longer')
            self.assertTrue(plan.all_selected_replacements_within_target)
        from lcs_patch_location_registry import VERIFIED_STOCK_SIZES
        p=get_backend('lcs_ps2').calculate(VERIFIED_STOCK_SIZES,{'kjah':367_017_984})
        self.assertEqual(len(p.stations),10)

    def test_standalone_patch_refuses_bad_lengths_before_output_publication(self):
        original=Path(__file__).with_name('SLUS_215.90.reference')
        with tempfile.TemporaryDirectory() as tmp:
            output=Path(tmp)/'patched.elf'
            with self.assertRaises(ValueError):
                get_backend('vcs_ps2').patch_executable_overflow_only(original,output,{'vcpr':16_384})
            self.assertFalse(output.exists())
            with self.assertRaises(ValueError):
                get_backend('vcs_ps2').patch_executable_overflow_only(original,output,
                    {'vcpr':MAX_ALIGNED_SAFE_BYTES+16_384})
            self.assertFalse(output.exists())
            report=get_backend('vcs_ps2').patch_executable_overflow_only(
                original,output,{'vcpr':aligned_bytes_for_source_duration(16_740_000)})
            self.assertTrue(report['output_verified'])
            self.assertEqual(report['max_supported_aligned_bytes'],MAX_ALIGNED_SAFE_BYTES)
            self.assertEqual(report['vcs_target_source_duration_ms'],[1_000,16_740_000])
            self.assertEqual(output.stat().st_size,original.stat().st_size)
            with self.assertRaises(UnverifiedGameWriterError):
                get_backend('vcs_ps2').build(output_iso=Path(tmp)/'out.iso')
            self.assertFalse((Path(tmp)/'out.iso').exists())

    def test_iso_overlap_refused_before_any_misleading_length_results(self):
        from types import SimpleNamespace
        entries={
          'AUDIO/MUSIC/VCPR.VB':SimpleNamespace(data_lba=100,size=1_285_629_181,is_directory=False,path='AUDIO/MUSIC/VCPR.VB'),
          'AUDIO/MUSIC/FRESH.VB':SimpleNamespace(data_lba=70_000,size=142_606_336,is_directory=False,path='AUDIO/MUSIC/FRESH.VB'),
        }
        class FakeImage:
            def __init__(self,*args):self.entries=entries;self.size=4_725_559_296
            def get(self,path):return entries[path] if path in entries else SimpleNamespace(size=0)
        with patch('lcs_duration_aware_iso_patcher.Iso9660Image',FakeImage):
            with self.assertRaisesRegex(ValueError,'overlaps'):
                get_backend('vcs_ps2').inspect_iso('dummy.iso')

if __name__ == '__main__':unittest.main()
