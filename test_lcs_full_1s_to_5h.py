"""All 18,000 integer seconds 1..5h, exact instruction word checks and transition tests.

No emulator execution or memory-allocation of five-hour radio is represented here.
"""
import math
import struct
import unittest
from pathlib import Path

from lcs_iop_safe_seek import (
    ORIGINAL_SHA256, TEXT_OFFSET, MAX_SAFE_DURATION_SIZE,
    NATIVE_UNPATCHED_DURATION_LIMIT, NATIVE_POSITION_SIGNED_LIMIT, POSITION_WORD_OFFSETS, ORIGINAL_POSITION_WORDS, FIXED_POSITION_WORDS, LENGTH_WORD_OFFSETS,
    ORIGINAL_LENGTH_WORDS, FIXED_LENGTH_WORDS,
    duration_ms, patch_cdstream, sha256, select_extra_shift,
)
from lcs_patch_calculator import calculate_plan
from test_lcs_dynamic_calculator import BASE

IRX = Path(__file__).with_name('CDSTREAM_ORIGINAL_REFERENCE.IRX').read_bytes()
CHUNK = 0x4000

def closest_aligned_bytes_for_seconds(seconds):
    # integer nearest aligned length (no floating point)
    # number blocks = round((seconds*1000*256/7) / CHUNK)
    numerator = seconds*1000*256
    denominator = 7*CHUNK
    return max(CHUNK, ((numerator + denominator//2)//denominator)*CHUNK)

def run_length_instructions(words, size):
    # Execute the four original/changed MIPS bit-level instructions, preserving
    # a0/v0 semantics; bgez condition is true for the supported 5h domain.
    R=[0]*32; R[4]=size
    for instruction in words:
        op=instruction>>26; funct=instruction&63
        rs=(instruction>>21)&31;rt=(instruction>>16)&31
        rd=(instruction>>11)&31;sa=(instruction>>6)&31
        if op!=0: raise AssertionError('Unexpected instruction encoding')
        if funct==0:res=R[rt]<<sa
        elif funct==2:res=R[rt]>>sa
        elif funct==35:res=R[rs]-R[rt]
        else: raise AssertionError('Unexpected MIPS function')
        if rd:R[rd]=res&0xffffffff
    return R[2]

class EverySecond1To5h(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        assert sha256(IRX)==ORIGINAL_SHA256

    def test_length_words_are_original_binary_then_match_exact_formula(self):
        for addr, expected in zip(LENGTH_WORD_OFFSETS, ORIGINAL_LENGTH_WORDS):
            self.assertEqual(struct.unpack_from('<I',IRX,TEXT_OFFSET+addr)[0], expected)
        for sec in range(1, 18001):
            size=closest_aligned_bytes_for_seconds(sec)
            self.assertLessEqual(size,MAX_SAFE_DURATION_SIZE)
            true=(size*7)//256
            got=run_length_instructions(FIXED_LENGTH_WORDS,size)
            self.assertEqual(got,true,(sec,size,got,true))
            self.assertLessEqual(abs(true-sec*1000),224,(sec,size,true))
            if size>NATIVE_UNPATCHED_DURATION_LIMIT:
                original=run_length_instructions(ORIGINAL_LENGTH_WORDS,size)
                self.assertNotEqual(original,true,(sec,size,original,true))

    def test_every_second_calculator_produces_safe_per_station_plan(self):
        seen_long=seen_short=seen_duration_patch=0
        prevshift=0
        for sec in range(1,18001):
            size=closest_aligned_bytes_for_seconds(sec)
            p=calculate_plan(BASE, {'kjah':size})
            row=p.stations[2]
            effective=max(size,0x40000)
            self.assertEqual(row.installed_size,effective)
            self.assertEqual(row.ee_boundary_ms,effective*7//256)
            self.assertEqual(row.silence_padding_bytes,effective-size)
            self.assertLessEqual(p.maximum_signed_product,p.maximum_signed_product_limit)
            self.assertEqual(p.iop_duration_overflow_fix_required,p.largest_installed_size>NATIVE_UNPATCHED_DURATION_LIMIT)
            self.assertEqual(p.iop_position_overflow_fix_required,p.largest_installed_size>NATIVE_POSITION_SIGNED_LIMIT)
            self.assertEqual(p.iop_needs_instruction_patch,
                             p.iop_extra_shift>0 or p.iop_duration_overflow_fix_required or p.iop_position_overflow_fix_required)
            if row.classification=='longer':seen_long+=1
            elif row.classification=='shorter':seen_short+=1
            seen_duration_patch+=p.iop_duration_overflow_fix_required
            self.assertGreaterEqual(p.iop_extra_shift,prevshift)
            prevshift=p.iop_extra_shift
        self.assertGreater(seen_long,10000)
        self.assertGreater(seen_short,100)
        self.assertGreater(seen_duration_patch,100)

    def test_every_possible_interleave_aligned_source_size_in_the_range(self):
        # There are ~40k distinct legal VB lengths in the requested 5h range,
        # not 18k distinct lengths: 0x4000 byte stereo interleave is 448 ms.
        # This checks *every* representable input length, not random samples.
        previous_duration=0
        for size in range(CHUNK,MAX_SAFE_DURATION_SIZE+1,CHUNK):
            actual=duration_ms(size)
            self.assertGreater(actual,previous_duration)
            previous_duration=actual
            p=calculate_plan(BASE,{'kjah':size})
            row=p.stations[2]
            self.assertEqual(row.source_size,size)
            self.assertEqual(row.installed_size,max(size,0x40000))
            self.assertEqual(row.ee_boundary_ms,max(size,0x40000)*7//256)
            self.assertLessEqual(p.maximum_signed_product,p.maximum_signed_product_limit)
            self.assertEqual(p.iop_duration_overflow_fix_required, p.largest_installed_size>NATIVE_UNPATCHED_DURATION_LIMIT)
            self.assertEqual(p.iop_position_overflow_fix_required, p.largest_installed_size>NATIVE_POSITION_SIGNED_LIMIT)

    def test_irx_patching_below_above_native_limit_and_at_five_hours(self):
        below=(NATIVE_UNPATCHED_DURATION_LIMIT//CHUNK)*CHUNK
        above=below+CHUNK
        extreme=closest_aligned_bytes_for_seconds(18000)
        self.assertFalse(below>NATIVE_UNPATCHED_DURATION_LIMIT)
        self.assertTrue(above>NATIVE_UNPATCHED_DURATION_LIMIT)
        for size in (CHUNK,0x40000,NATIVE_POSITION_SIGNED_LIMIT//CHUNK*CHUNK,(NATIVE_POSITION_SIGNED_LIMIT//CHUNK+1)*CHUNK,below,above,extreme):
            patched,report=patch_cdstream(IRX,[size])
            self.assertEqual(len(patched),len(IRX))
            needs= size>NATIVE_UNPATCHED_DURATION_LIMIT
            self.assertEqual(report['duration_overflow_fix_required'],needs)
            self.assertEqual(report['position_overflow_fix_required'],size>NATIVE_POSITION_SIGNED_LIMIT)
            length_words=tuple(struct.unpack_from('<I',patched,TEXT_OFFSET+addr)[0]
                               for addr in LENGTH_WORD_OFFSETS)
            self.assertEqual(length_words,FIXED_LENGTH_WORDS if needs else ORIGINAL_LENGTH_WORDS)
            self.assertEqual(run_length_instructions(length_words,size),
                             (size*7)//256 if needs else ((size*7)&0xffffffff)//256)
            allowed={TEXT_OFFSET+x+i for x in (0x1538,0x172C,*LENGTH_WORD_OFFSETS,*POSITION_WORD_OFFSETS) for i in range(4)}
            changed={i for i,(old,new) in enumerate(zip(IRX,patched)) if old!=new}
            self.assertLessEqual(changed,allowed)

    def test_position_conversion_mips_instruction_words_and_signed_ranges(self):
        # Simulate the live byte-position converter's instruction path that
        # uses either 0x20FC (branch) or 0x2180 (main path).
        for addr, expected in zip(POSITION_WORD_OFFSETS,ORIGINAL_POSITION_WORDS):
            self.assertEqual(struct.unpack_from('<I',IRX,TEXT_OFFSET+addr)[0],expected)
        from random import Random
        rng=Random(22553)
        def signed32(v):return ((v+(1<<31)) & 0xffffffff) - (1<<31)
        def eval_position(words, numbytes, branch):
            R=[0]*32; R[6]=numbytes & 0xffffffff
            instructions=(words[0 if branch else 1],words[2],words[3],words[4],words[5],words[6])
            # bgez decisions do not alter the final corrected calculation;
            # overwritten sign-correction instructions are NOP.
            for word in instructions:
                func=word&63;rs=(word>>21)&31;rt=(word>>16)&31;rd=(word>>11)&31;sh=(word>>6)&31
                if word==0:continue
                if (word>>26)==9: R[rt]=(R[rs]+signed32(word&0xffff))&0xffffffff;continue
                if func==3:res=signed32(R[rt])>>sh
                elif func==0:res=R[rt]<<sh
                elif func==35:res=R[rs]-R[rt]
                else:raise AssertionError(hex(word))
                R[rd]=res&0xffffffff
            return signed32(R[2])
        points=[0,256,1024,0x4000,100*1024*1024,310*1024*1024,367017984,658292736,-256,-0x40000]
        points += [rng.randint(-100000,658292736//256)*256 for _ in range(30000)]
        for n in points:
            for branch in (True,False):
                self.assertEqual(eval_position(FIXED_POSITION_WORDS,n,branch),
                                 (n//256)*7,(n,branch))

    def test_five_hour_seek_interpreter(self):
        from test_lcs_iop_safe_seek import SeekInterpreter
        from random import Random
        rng=Random(1201)
        sizes=(658292736,367017984,(NATIVE_UNPATCHED_DURATION_LIMIT//CHUNK+1)*CHUNK)
        oracle=SeekInterpreter(IRX,ideal_mult=True)
        for size in sizes:
            irx,report=patch_cdstream(IRX,[size]); sim=SeekInterpreter(irx)
            self.assertTrue(report['extra_shift']>0)
            dur=duration_ms(size)
            positions={0,1,dur-1,dur,dur+1}
            for j in range(1,9):
                mid=dur*j//8
                positions.update(max(0,min(dur,mid+d)) for d in (-10_000,-997,-1,0,1,997,10_000))
            positions.update(rng.randrange(dur+1) for _ in range(2000))
            for position in positions:
                got=sim.run(size,position)[1]
                ref=oracle.run(size,position)[1]
                self.assertTrue(0<=got<size,(size,position,got))
                self.assertEqual(got%0x4000,0,(size,position,got))
                self.assertLessEqual(abs(got-ref),0x4000,(size,position,got,ref))

    def test_reject_above_five_hour_aligned_limit(self):
        with self.assertRaises(ValueError):
            calculate_plan(BASE, {'kjah':MAX_SAFE_DURATION_SIZE+CHUNK})

if __name__=='__main__':unittest.main(verbosity=2)
