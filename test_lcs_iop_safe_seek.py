"""Standalone MIPS-I instruction-level differential test for CDSTREAM.IRX 0x14E8-0x1768.

Executes ORIGINAL machine words, not a hand-written paraphrase. Uses idealized
64-bit multiplication only in the oracle run to show where original signed
32-bit MFLO loses the upper bits. No PCSX2/emulator runtime is claimed.
"""
from __future__ import annotations
import hashlib
import struct
import sys
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parent))
from lcs_iop_safe_seek import (
    patch_cdstream, select_extra_shift, duration_ms, ORIGINAL_SHA256,
    TEXT_OFFSET,SHR_OFFSET,SHL_OFFSET, MAX_SAFE_DURATION_SIZE,
    expected_instruction_words, _maximum_product, POSITION_WORD_OFFSETS,
)

ORIGINAL_MODULE = Path(__file__).parent / 'CDSTREAM_ORIGINAL_REFERENCE.IRX'

def u32(n):return n & 0xffffffff

def s32(n):
    n &= 0xffffffff
    return n if n < 0x80000000 else n-(1<<32)

class SeekInterpreter:
    """Instruction stepping over LCS original basic blocks; handles branch delays."""
    def __init__(self, blob, ideal_mult=False):
        self.blob=blob;self.ideal_mult=ideal_mult
    def run(self, size, position_ms):
        R=[0]*32; mem={0x6e10:position_ms,0x6dec:size}
        R[2]=duration_ms(size);R[6]=duration_ms(size);R[16]=0;R[17]=0;R[18]=0;R[19]=0x6dec
        HI=LO=0; pc=0x14e8; nextpc=None; steps=0; last_lo_from_mult=False; wide_reg=-1
        while pc <= 0x1768:
            steps+=1
            if steps > 200:raise AssertionError(f'possible loop at {pc:04X}')
            instr=struct.unpack_from('<I',self.blob,TEXT_OFFSET+pc)[0]
            op=(instr>>26)&63; rs=(instr>>21)&31; rt=(instr>>16)&31
            rd=(instr>>11)&31; sa=(instr>>6)&31; funct=instr&63
            imm=instr&65535; signed_imm=imm if imm <32768 else imm-65536
            current=pc; jmp=None; setreg=lambda d,x:R.__setitem__(d,u32(x)) if d else None
            if op==0:
                if funct==0: setreg(rd,R[rt]<<sa)
                elif funct==2: setreg(rd,R[rt]>>sa)
                elif funct==3: setreg(rd,s32(R[rt])>>sa)
                elif funct==33: setreg(rd,R[rs]+R[rt])
                elif funct==35: setreg(rd,R[rs]-R[rt])
                elif funct==42: setreg(rd,int(s32(R[rs])<s32(R[rt])))
                elif funct==43: setreg(rd,int(R[rs]<R[rt]))
                elif funct==24:
                    product=s32(R[rs])*s32(R[rt]);HI=u32(product>>32)
                    LO=product if self.ideal_mult else u32(product); last_lo_from_mult=True
                elif funct==16:setreg(rd,HI)
                elif funct==18:
                    if self.ideal_mult and last_lo_from_mult:
                        R[rd]=LO;wide_reg=rd
                    else:setreg(rd,LO)
                elif funct==26:
                    a=R[rs] if self.ideal_mult and wide_reg==rs else s32(R[rs]);b=s32(R[rt]);
                    if b==0:raise AssertionError('Divide by zero!')
                    q=abs(a)//abs(b) * (-1 if (a<0) != (b<0) else 1)
                    LO=u32(q);HI=u32(a-q*b);last_lo_from_mult=False;wide_reg=-1
                elif funct==8: jmp=R[rs]
                elif funct==13:raise AssertionError(f'MIPS break at 0x{pc:x}')
                else:raise AssertionError(f'Unsupported R op 0x{instr:08x} at {pc:04x}')
            elif op==2:jmp=(pc & 0xf0000000)|((instr & 0x3ffffff)<<2)
            elif op==4:
                if R[rs]==R[rt]:jmp=pc+4+(signed_imm<<2)
            elif op==5:
                if R[rs]!=R[rt]:jmp=pc+4+(signed_imm<<2)
            elif op==6:
                if s32(R[rs])<=0:jmp=pc+4+(signed_imm<<2)
            elif op==1:
                if rt==0 and s32(R[rs])<0:jmp=pc+4+(signed_imm<<2)
                else:raise AssertionError('Unsupported regimm') if rt else None
            elif op==9:setreg(rt,R[rs]+signed_imm)
            elif op==13:setreg(rt,R[rs] | imm)
            elif op==15:setreg(rt,imm<<16)
            elif op==35:
                key=u32(R[rs]+signed_imm)
                if key not in mem:raise AssertionError(f'Unknown memory read {key:08x} at 0x{pc:04x}')
                setreg(rt,mem[key])
            elif op==43:mem[u32(R[rs]+signed_imm)]=R[rt]
            else:raise AssertionError(f'Unsupported op {op}, instr 0x{instr:08x}, at 0x{pc:04x}')
            if nextpc is not None:
                pc=nextpc;nextpc=None
            elif jmp is not None:
                nextpc=jmp;pc+=4
            else:pc+=4
            if current==0x1764: break
        return mem.get(0x6df0), mem.get(0x6dec+4,0), steps

class VerifiedSeekPatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.orig=ORIGINAL_MODULE.read_bytes()
        cls.patched,cls.report=patch_cdstream(cls.orig,[367017984,63176704])
        cls.stock=SeekInterpreter(cls.orig)
        cls.fixed=SeekInterpreter(cls.patched)
        cls.oracle=SeekInterpreter(cls.orig,ideal_mult=True)

    def test_module_hash(self):
        self.assertEqual(hashlib.sha256(self.orig).hexdigest(),ORIGINAL_SHA256)
        self.assertEqual(self.report['extra_shift'],4)
        self.assertEqual(len(self.orig),len(self.patched))
        diffs={i for i,(a,b) in enumerate(zip(self.orig,self.patched)) if a!=b}
        allowed={TEXT_OFFSET+addr+i for addr in (SHR_OFFSET,SHL_OFFSET,*POSITION_WORD_OFFSETS) for i in range(4)}
        self.assertTrue(diffs)
        self.assertTrue(diffs <= allowed)
        self.assertLess(self.report['maximum_signed_multiplication'],0x80000000)

    def test_original_has_concrete_overflow(self):
        # First full >32-bit signed overflow should be around 94 seconds for user's stream.
        size=367017984
        differences=[]
        for second in range(1,250):
            pos=second*1000
            expected=self.oracle.run(size,pos)[1]
            actual=self.stock.run(size,pos)[1]
            if actual!=expected:differences.append(second)
        self.assertTrue(differences, 'Need actual instruction-level reproduction of lost high bits')
        print('Earliest wrong signed-MFLO seek output (seconds):',differences[0]);

    def test_patched_matches_64bit_oracle_sector_range(self):
        # Actual original MIPS statements run in both test cases, with only
        # mult/MFLO overflow disabled in the oracle. Playback-sector rounding
        # is done in the real disassembled instructions, not reconstructed here.
        size=367017984;end=duration_ms(size)
        seen=0; worst=0; first_error=None; changed=0
        for pos in list(range(0,end,997)) + [end-1,end]:
            a=self.oracle.run(size,pos)[1]
            b=self.fixed.run(size,pos)[1]
            c=self.stock.run(size,pos)[1]
            worst=max(worst,abs(a-b))
            seen+=1
            if a!=b and first_error is None:first_error=(pos,a,b)
            changed += int(a!=c)
            self.assertTrue(0<=b<size, (pos,b))
            self.assertTrue(b%0x4000==0,(pos,b))
        # Difference should be at most 1 seek sector, and no unexpected huge jump
        # from quantization (rare off-by-one-sector boundary cases are expected).
        self.assertLessEqual(worst,0x4000, (worst,first_error))
        self.assertGreater(changed,100)
        print('Patched vs non-overflow ideal:',seen,'positions; max sector delta:',worst,
              'unpatched divergent points:',changed,'first quantization difference:',first_error)

    def test_stock_station_compatibility(self):
        from json import loads
        p=Path(__file__).parent / 'LCS_REFERENCE_AUDIO_AUDIT.json'
        for station in loads(p.read_text())['music']:
            size=station['size_bytes'];end=duration_ms(size)
            # Compare against the original for stock signals, though the changed
            # quantization may cause sector-adjacent boundary differences.
            for pos in range(0,end,3857):
                a=self.stock.run(size,pos)[1];b=self.fixed.run(size,pos)[1]
                self.assertLessEqual(abs(self.oracle.run(size,pos)[1]-b),0x4000,(station['name'],pos,a,b))

    def test_size_limits_and_rejects_unknown_module(self):
        with self.assertRaises(ValueError):patch_cdstream(b'not the original',[367017984])
        with self.assertRaises(ValueError):select_extra_shift([MAX_SAFE_DURATION_SIZE+0x4000])
        self.assertEqual(select_extra_shift([63176704]),0)
        self.assertGreaterEqual(select_extra_shift([367017984]),4)

if __name__=='__main__': unittest.main(verbosity=2)
