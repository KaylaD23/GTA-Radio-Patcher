"""Input-dependent patch math regression: *no KJAH-specific constants*."""
import unittest
import struct
from pathlib import Path
from lcs_patch_calculator import calculate_plan
from lcs_iop_safe_seek import (
    patch_cdstream, ORIGINAL_SHA256, MAX_SAFE_DURATION_SIZE,
    expected_instruction_words, duration_ms, _maximum_product, MAX_32_PRODUCT,
)
from lcs_stream_bounds import READ_CHUNK_BYTES

IRX = Path(__file__).with_name('CDSTREAM_ORIGINAL_REFERENCE.IRX')
ORIGINAL_SIZES=[58392576,58245120,63176704, 57016320, 44040192, 60293120, 64225280, 61079552, 50462720, 46825472]
KEYS=('head','double','kjah','rise','lips','mundo','msx','flash','lcj','lcfr')
BASE=dict(zip(KEYS,ORIGINAL_SIZES))

def station(report,key='kjah'):
    return next(row for row in report.stations if row.station==key)

class DynamicCalculatorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.irx=IRX.read_bytes()

    def test_distinct_lengths_produce_distinct_boundaries_and_shifts(self):
        cases=[0x4000, 0x40000, 0x80000, 0x100000, 0x1000000, 63176704,
               0x6000000, 0x7800000, 0x9000000, 0x10000000, 0x14000000,
               367017984,0x1a000000,0x20000000,0x24000000]
        plans=[]
        for size in cases:
            p=calculate_plan(BASE,{'kjah':size})
            row=station(p)
            self.assertEqual(row.source_size,size)
            self.assertEqual(row.installed_size,max(size,READ_CHUNK_BYTES))
            self.assertEqual(row.ee_boundary_ms, max(size,READ_CHUNK_BYTES)*7//256)
            self.assertEqual(row.duration_delta_ms,row.ee_boundary_ms - 63176704*7//256)
            self.assertLessEqual(p.maximum_signed_product,MAX_32_PRODUCT)
            self.assertEqual(p.iop_new_words,expected_instruction_words(p.iop_extra_shift))
            new,irx_report=patch_cdstream(self.irx,[r.installed_size for r in p.stations])
            self.assertEqual(irx_report['extra_shift'],p.iop_extra_shift)
            for off,word in zip([0x1538,0x172C],p.iop_new_words):
                self.assertEqual(struct.unpack_from('<I',new,0xA0+off)[0],word)
            plans.append(p)
        self.assertEqual(station(plans[0]).silence_padding_bytes, READ_CHUNK_BYTES-0x4000)
        self.assertGreater(len({p.iop_extra_shift for p in plans}),3)
        self.assertEqual(station(next(p for p in plans if station(p).source_size==367017984)).ee_boundary_ms,10035648)
        self.assertEqual(next(p for p in plans if station(p).source_size==367017984).iop_extra_shift,4)

    def test_other_station_controls_global_iop_scaling(self):
        a=calculate_plan(BASE,{'kjah':0x40000})
        b=calculate_plan(BASE,{'kjah':0x40000, 'flash':367017984})
        self.assertEqual(station(a).ee_boundary_ms,station(b).ee_boundary_ms)
        self.assertEqual(station(b,'flash').ee_boundary_ms,10035648)
        self.assertGreater(b.iop_extra_shift,a.iop_extra_shift)
        self.assertNotEqual(a.iop_new_words,b.iop_new_words)

    def test_shorter_but_longer_than_read_chunk_no_padding(self):
        result=calculate_plan(BASE,{'kjah':0x180000})
        item=station(result)
        self.assertEqual(item.classification,'shorter')
        self.assertEqual(item.silence_padding_bytes,0)
        self.assertEqual(item.installed_size,0x180000)

    def test_no_replacements_original_matches(self):
        result=calculate_plan(BASE,{})
        self.assertTrue(all(r.classification=='same-length' for r in result.stations))
        self.assertEqual(result.iop_extra_shift,0)

    def test_invalid_lengths_never_wrapped_to_an_unsafe_value(self):
        invalid=[0, -0x4000, 3, 0x4000+16, 0x40000000, (MAX_SAFE_DURATION_SIZE//0x4000+1)*0x4000]
        for size in invalid:
            with self.subTest(size=size),self.assertRaises(ValueError):
                calculate_plan(BASE,{'kjah':size})
        with self.assertRaises(ValueError):
            calculate_plan(BASE,{'NOT_A_STATION':0x4000})
        with self.assertRaises(ValueError):
            calculate_plan({'kjah':0x40000},{'kjah':0x40000})

    def test_automatic_shift_never_overflows_for_supported_domain(self):
        # 408 distinct sizes spanning entire representable audio domain
        span=(MAX_SAFE_DURATION_SIZE//0x4000)*0x4000
        sizes=sorted({max(0x4000,(span*k//407)//0x4000*0x4000) for k in range(408)})
        prev=0
        for size in sizes:
            p=calculate_plan(BASE,{'kjah':size})
            self.assertGreaterEqual(p.iop_extra_shift,prev)
            prev=p.iop_extra_shift
            self.assertLessEqual(p.maximum_signed_product,MAX_32_PRODUCT)
            self.assertEqual(p.maximum_signed_product,_maximum_product(p.largest_installed_size,p.iop_extra_shift))
            if p.iop_extra_shift:
                self.assertGreater(_maximum_product(p.largest_installed_size,p.iop_extra_shift-1),MAX_32_PRODUCT)

if __name__=='__main__':unittest.main()
