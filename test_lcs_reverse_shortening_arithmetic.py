"""Targeted regression for stations much SHORTER than the stock LCS PS2 VB.

Differentiates short-stream boundary/saved-position cases from long-stream
positive 32-bit multiplication overflow. Instruction-level tests, not live gameplay.
"""
import unittest
from pathlib import Path

from lcs_patch_calculator import calculate_plan
from lcs_patch_location_registry import VERIFIED_STOCK_SIZES
from lcs_iop_safe_seek import (
    duration_ms, patch_cdstream, select_extra_shift,
    NATIVE_POSITION_SIGNED_LIMIT, NATIVE_UNPATCHED_DURATION_LIMIT,
)
from lcs_stream_bounds import installed_size, READ_CHUNK_BYTES
from test_lcs_iop_safe_seek import SeekInterpreter

IRX = (Path(__file__).parent / 'CDSTREAM_ORIGINAL_REFERENCE.IRX').read_bytes()
LONG_KJAH = 367_017_984


class ReverseShorteningArithmeticTests(unittest.TestCase):
    def test_each_of_ten_can_be_much_shorter_without_negative_boundary(self):
        for name, stock_size in VERIFIED_STOCK_SIZES.items():
            for replacement in (0x4000, 0x40000, 0x80000, 0x100000, stock_size // 0x8000 * 0x4000):
                with self.subTest(station=name, replacement=replacement):
                    plan = calculate_plan(VERIFIED_STOCK_SIZES, {name:replacement})
                    item = next(x for x in plan.stations if x.station == name)
                    self.assertEqual(item.source_size, replacement)
                    self.assertEqual(item.installed_size, installed_size(replacement))
                    self.assertLess(item.installed_size, item.original_size)
                    self.assertEqual(item.classification, 'shorter')
                    self.assertEqual(item.duration_delta_ms,
                                     duration_ms(item.installed_size)-duration_ms(stock_size))
                    self.assertLess(item.duration_delta_ms, 0)
                    self.assertGreater(item.ee_boundary_ms,0)
                    self.assertLessEqual(item.ee_boundary_ms,0xffffffff)
                    self.assertEqual(item.size_delta_bytes,item.installed_size-stock_size)
                    self.assertLess(item.size_delta_bytes,0)
                    self.assertEqual(plan.iop_extra_shift,select_extra_shift([x.installed_size for x in plan.stations]))
                    self.assertFalse(plan.iop_position_overflow_fix_required)
                    self.assertFalse(plan.iop_duration_overflow_fix_required)

    def test_all_ten_shortened_global_iop_requirement_recomputes(self):
        plan = calculate_plan(VERIFIED_STOCK_SIZES,
                              {name:0x4000 for name in VERIFIED_STOCK_SIZES})
        self.assertTrue(all(row.classification == 'shorter' for row in plan.stations))
        self.assertTrue(all(row.installed_size==READ_CHUNK_BYTES for row in plan.stations))
        self.assertEqual(plan.iop_extra_shift,0)
        self.assertFalse(plan.iop_needs_instruction_patch)
        patched,report=patch_cdstream(IRX,[row.installed_size for row in plan.stations])
        self.assertEqual(patched,IRX)
        self.assertEqual(report['extra_shift'],0)

    def test_expired_saved_times_and_negative_values_are_bounded(self):
        # The native IRX is exercised as actual MIPS words. An expired saved
        # station time should lead to 0 rather than an out-of-file byte seek.
        base = SeekInterpreter(IRX)
        mixed,plan = patch_cdstream(IRX,[LONG_KJAH,READ_CHUNK_BYTES])
        self.assertEqual(plan['extra_shift'],4)
        long_aware = SeekInterpreter(mixed)
        for name,stock_size in VERIFIED_STOCK_SIZES.items():
            old_time=duration_ms(stock_size)
            for size in (READ_CHUNK_BYTES,0x80000,0x100000,0x400000):
                new_time=duration_ms(size)
                positions=(-0x80000000,-new_time-1,-1,new_time,new_time+1,old_time,old_time*2,0x7fffffff,0xffffffff)
                for ms in positions:
                    with self.subTest(station=name, size=size, ms=ms):
                        for it in (base,long_aware):
                            byte_off=it.run(size,ms)[1]
                            self.assertGreaterEqual(byte_off,0)
                            self.assertLess(byte_off,size)
                            self.assertEqual(byte_off % 0x4000,0)
                            self.assertEqual(byte_off,0)

    def test_large_other_station_rescale_preserves_short_seek_range(self):
        # Global +4 shift for a long station must not corrupt any other station's
        # seek range. A <=1 read-window file legitimately has only start=0.
        mixed,_=patch_cdstream(IRX,[LONG_KJAH,0x40000]); fixed=SeekInterpreter(mixed)
        pristine=SeekInterpreter(IRX)
        for size in (READ_CHUNK_BYTES,0x80000,0x100000,0x400000,0x1000000):
            duration=duration_ms(size)
            with self.subTest(short_file=size):
                outputs=set()
                for j in range(101):
                    ms=duration*j//101
                    observed=fixed.run(size,ms)[1]
                    baseline=pristine.run(size,ms)[1]
                    self.assertTrue(0<=observed<size,(size,ms,observed))
                    self.assertEqual(observed%0x4000,0)
                    self.assertLessEqual(abs(observed-baseline),0x4000,(size,ms,observed,baseline))
                    outputs.add(observed)
                if size==READ_CHUNK_BYTES:
                    self.assertEqual(outputs,{0})
                else:
                    self.assertGreater(len(outputs),1)

    def test_reject_invalid_size_and_preserve_signed_deltas(self):
        for invalid in (-0x4000,0,1,0x5000):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    calculate_plan(VERIFIED_STOCK_SIZES, {'kjah':invalid})
        plan=calculate_plan(VERIFIED_STOCK_SIZES,{'kjah':0x40000})
        item=next(row for row in plan.stations if row.station=='kjah')
        # Delta may be negative; never use the negative delta as a length
        # or truncate/wrap it to unsigned 32 bits in the binary writer.
        self.assertLess(item.size_delta_bytes,0)
        self.assertLess(item.duration_delta_ms,0)
        self.assertEqual(item.ee_boundary_ms,7168)
        row = plan.as_dict()['stations']['kjah']
        self.assertEqual(row['ee_boundary_ms'],7168)
        self.assertTrue(row['shortened_from_original'])
        self.assertTrue(row['one_read_window_only'])
        self.assertIn('byte zero',row['short_seek_warning'])


if __name__=='__main__':
    unittest.main(verbosity=2)
