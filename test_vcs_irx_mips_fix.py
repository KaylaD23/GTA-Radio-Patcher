"""Inspect and execute patched MIPS words with branch-delay semantics.

Does not certify IOP loader compatibility or in-game streaming behaviour.
"""
from __future__ import annotations
import hashlib
import random
import struct
import tempfile
import unittest
from pathlib import Path

from vcs_cdstream_signed_position_patch import build, branch, inspect, u32, TEXT_OFF
from vcs_iop_position_math import ideal_signed_position

REF = Path('/mnt/data/CDSTREAM_IRX_VCS_disc_copy.bin')
U32=0xffffffff

def as_i32(v):
    v &= U32
    return v-(1<<32) if v>=1<<31 else v


def execute_actual_patch(elf,raw):
    """Emulate actual words from 0x2560 through the MIPS stub and return 0x2570.

    Zero register read-only; branch delay slots executed correctly; no arithmetic
    oracle called by this interpreter. Instructions decoded dynamically from output.
    """
    reg=[(0x12345678 + (i*123)) & U32 for i in range(32)]
    reg[0]=0
    reg[6]=raw & U32
    initial={i:reg[i] for i in (4,6,7,8,9,10,11,12,13,14,15,16,17,18,19,20,21,22,23,24,25,26,27,28,29,30,31)}
    def fetch(pc): return u32(elf,TEXT_OFF+pc)
    def ins(w):
        op=w>>26;rs=(w>>21)&31;rt=(w>>16)&31;rd=(w>>11)&31;sa=(w>>6)&31;func=w&63;imm=w&65535
        val=reg[rs]; right=reg[rt]
        if op==0:
            if func==0x2b: reg[rd]=int(val < right)  # sltu
            elif func==0x24: reg[rd]=val&right      # and
            elif func==3: reg[rd]=(as_i32(right)>>sa)&U32 # sra
            elif func==0x21: reg[rd]=(val+right)&U32 # addu
            elif func==0x23: reg[rd]=(val-right)&U32 # subu
            elif func==0: reg[rd]=(right<<sa)&U32  # sll/nop
            else:raise ValueError(f'Unsupported SPECIAL 0x{w:08x}')
        elif op==0xa:reg[rt]=int(as_i32(val) < (imm if imm<0x8000 else imm-0x10000))
        elif op==0xc:reg[rt]=val&imm
        elif op==0xe:reg[rt]=val ^ imm
        elif op==0xf:reg[rt]=(imm<<16)&U32
        else:raise ValueError(f'Unsupported arithmetic 0x{w:08x}')
        reg[0]=0
    pc=0x2560
    visited=[]
    for _ in range(40):
        if pc==0x2570:break
        visited.append(pc)
        w=fetch(pc)
        op=w>>26
        if op==4: # beq
            rs=(w>>21)&31;rt=(w>>16)&31;off=w&0xffff
            off=off if off<32768 else off-65536
            cond=reg[rs]==reg[rt]
            delay=fetch(pc+4)
            ins(delay)
            pc=(pc+4+4*off) if cond else pc+8
        else:
            ins(w)
            pc+=4
    else:raise AssertionError('runaway branch')
    assert visited[0]==0x2560 and 0xb460 in visited
    for i,value in initial.items():assert reg[i]==value,(i,reg[i],value)
    assert reg[3]==0x10620000,hex(reg[3])
    quotient=as_i32(reg[5])
    # Also execute actual replacement at original post-return .text+0x2580.
    ins(fetch(0x2580))
    assert as_i32(reg[2])==quotient
    return quotient

class VCSIRXMipsPatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not REF.is_file():raise unittest.SkipTest('Original VCS IOP module unavailable')
        cls.original=REF.read_bytes()
        cls.patched,cls.meta=build(cls.original)
    def test_verified_original_and_elf_header_load_segment(self):
        inspect(self.original)
        self.assertEqual(len(self.patched),61628)
        self.assertEqual(u32(self.patched,0x20),0xeedc)
        self.assertEqual(u32(self.patched,0x34+32+16),0xb490)
        self.assertEqual(u32(self.patched,0x34+32+20),0xb490)
        self.assertEqual(struct.unpack_from('<H',self.patched,0x30)[0],12)
        self.assertEqual(self.patched[0xb500+40:0xb500+48],b'\0'*8)
    def test_original_section_content_and_relocations_unchanged(self):
        # Original load-file material except 3 patched instructions is exact.
        expected=bytearray(self.original[:0x8240])
        # Changes to ELF header and second program header are intentional.
        struct.pack_into('<I',expected,0x20,u32(self.patched,0x20))
        struct.pack_into('<H',expected,0x30,12)
        struct.pack_into('<I',expected,0x34+32+16,0xb490)
        struct.pack_into('<I',expected,0x34+32+20,0xb490)
        for pc in (0x2560,0x2564,0x2580):
            struct.pack_into('<I',expected,TEXT_OFF+pc,u32(self.patched,TEXT_OFF+pc))
        self.assertEqual(self.patched[:0x8240],bytes(expected))
        # All relocation entries stay word-for-word identical.
        for original_off,sz,patched_off in ((0x8294,0x3880,0xb58c),
                                             (0xbb14,0xc0,0xee0c),
                                             (0xbbd4,0x10,0xeecc)):
            self.assertEqual(self.original[original_off:original_off+sz],
                             self.patched[patched_off:patched_off+sz])
    def test_original_and_current_revision_rejected(self):
        broken=bytearray(self.original);broken[0x2560+TEXT_OFF]^=1
        with self.assertRaises(ValueError):build(bytes(broken))
        with self.assertRaises(ValueError):build(self.patched)
    def test_branch_target_and_stub_readable(self):
        self.assertEqual(u32(self.patched,TEXT_OFF+0x2560),branch(0x2560,0xb460))
        self.assertEqual(u32(self.patched,TEXT_OFF+0xb480),branch(0xb480,0x2570))
        self.assertEqual(u32(self.patched,TEXT_OFF+0xb484),0x3c031062)
        self.assertEqual(u32(self.patched,TEXT_OFF+0x2580),0x00a01021)
    def test_all_aligned_16kb_positions_both_signs_execute_real_words(self):
        for x in range(49152,613564416+1,16384):
            for v in (x,-x):
                self.assertEqual(execute_actual_patch(self.patched,v),ideal_signed_position(v))
    def test_original_signed32_extremes_and_boundaries_execute_real_words(self):
        for x in [0,1,2,3,7,8,9,15,16,-1,-7,-8,-9,0x7fffffff,-0x80000000,
                  306783378,306783379,-306783379,367017984,-367017984,
                  612220928,-612220928,613564416,-613564416]:
            self.assertEqual(execute_actual_patch(self.patched,x),ideal_signed_position(x))
    def test_random_signed32_execute_real_words(self):
        rng=random.Random(20261019)
        for _ in range(12000):
            x=rng.randint(-0x80000000,0x7fffffff)
            self.assertEqual(execute_actual_patch(self.patched,x),ideal_signed_position(x))
    def test_input_not_overwritten_and_repeatable(self):
        result2,meta2=build(self.original)
        self.assertEqual(hashlib.sha256(self.patched).digest(),hashlib.sha256(result2).digest())
        self.assertEqual(meta2['new_sha256'],self.meta['new_sha256'])

if __name__=='__main__':unittest.main()
