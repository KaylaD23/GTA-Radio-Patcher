"""Cross-engine VCS EE signed/unsigned guard and CDSTREAM ISO installation.

Byte-based verification against authenticated modules and synthetic ISOs.
No gameplay/EE/IOP loader execution is claimed.
"""
from pathlib import Path
import hashlib
import random
import struct
import tempfile
import unittest
from unittest.mock import patch

from gta_radio_profiles import get_profile
from lcs_duration_aware_iso_patcher import Iso9660Image
from vcs_cdstream_signed_position_patch import EXPECTED_SHA, build as build_iop
from vcs_ee_guarded_position import (build_guarded_executable, helper_words, j,
    HELPER_SIZE, SEGMENT_VA, POSITION_DIV_OFF, POSITION_BRANCH_OFF)
from vcs_ee_overflow_patch import PATCHES
from vcs_vb_gain_install import build_radio_audio_iso
from test_lcs_native_provider import make_iso
import lcs_duration_aware_exe_patcher as exe

HERE = Path(__file__).parent
REFERENCE_EE = HERE/'SLUS_215.90.reference'
REFERENCE_PATCHED_IRX = HERE/'CDSTREAM_VCS_Experimental_Fixed.IRX'
OPTIONAL_PRISTINE_IRX = Path('/mnt/data/CDSTREAM_IRX_VCS_disc_copy.bin')


def word(data, p): return struct.unpack_from('<I',data,p)[0]


def as_i32(n):
    n &= 0xffffffff
    return n - (1<<32) if n & 0x80000000 else n


def sdiv(a, b):
    return abs(a)//abs(b) * (-1 if (a<0)!=(b<0) else 1)


def actual_ee_helper_result(binary, position):
    """Follow actual patched MIPS jump/branch/delay words, not a math-only switch."""
    v0=(position << 8) & 0xffffffff
    v1=7
    pc=0x0010e75c
    guard=word(binary,0xF75C)
    assert (guard >> 26)==2
    assert word(binary,0xF760)==0 # jump delay nop
    target=((pc+4)&0xf0000000) | ((guard&0x3ffffff)<<2)
    segment=struct.unpack_from('<8I',binary,0x54)
    seg_offset,seg_va=segment[1],segment[2]
    def read(va): return word(binary,seg_offset+(va-seg_va))
    cond=read(target)
    assert (cond>>26)==1 and ((cond>>21)&31)==16 and ((cond>>16)&31)==0
    assert read(target+4)==0
    negative=position<0
    nextpc=target+4+4*as_i32((cond&0xffff) if cond&0x8000==0 else (cond&0xffff)|0xffff0000) if negative else target+8
    # check our verified, exact branch layout with signed and unsigned DIVs
    if negative: assert nextpc == target+20
    else: assert nextpc == target+8
    div=read(nextpc)
    assert div == (0x0043001A if negative else 0x0043001B)
    return sdiv(as_i32(v0),v1) if negative else v0//v1


def fixture(iso:Path, ee:bytes, irx:bytes):
    with patch.object(exe, 'STATIONS', get_profile('vcs_ps2').stations):
        make_iso(iso,ee,irx)
    raw=iso.read_bytes().replace(b'SLUS_214.23',b'SLUS_215.90')
    iso.write_bytes(raw)


class EEGuardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pristine=REFERENCE_EE.read_bytes()

    def test_both_modes_use_original_signed_division_for_negatives(self):
        for adaptive in (False,True):
            with self.subTest(adaptive=adaptive):
                elf,report=build_guarded_executable(self.pristine,{'vcpr':367017984},adaptive=adaptive)
                self.assertEqual(word(elf,0xF8A8),PATCHES[0][2])
                self.assertEqual(word(elf,POSITION_BRANCH_OFF),0)
                self.assertEqual(report['offset_strategy'],'shared-adaptive-experimental' if adaptive else 'stock')
                self.assertIn('signed DIV for negative', report['ee_position_negative_guard'])
                self.assertEqual(report['ee_guard_words'],[f'0x{x:08X}' for x in helper_words(int(report['ee_guard_va'],16))])
                for x in [-16777215,-8388608,-8388607,-200,-7,-1,0,1,7,8388607,8388608,10035648,16777215]:
                    with self.subTest(x=x):
                        original_dividend=as_i32((x<<8)&0xffffffff)
                        expected=sdiv(original_dividend,7) if x<0 else ((x<<8)&0xffffffff)//7
                        self.assertEqual(actual_ee_helper_result(elf,x),expected)

    def test_random_negative_and_positive_branch_execution(self):
        elf,_=build_guarded_executable(self.pristine,{'vcpr':367017984})
        r=random.Random(22016)
        for _ in range(60000):
            x=r.randint(-16777215,16777215)
            expected=sdiv(as_i32(x<<8),7) if x<0 else ((x<<8)&0xffffffff)//7
            self.assertEqual(actual_ee_helper_result(elf,x),expected)

    def test_authenticated_two_divu_only_variant_remains_accepted(self):
        from vcs_ee_overflow_patch import patched_bytes
        original=patched_bytes(self.pristine)
        elf,meta=build_guarded_executable(original,{'vcpr':367017984})
        self.assertEqual(meta['source_baseline'],'ee-divu-only')
        self.assertNotEqual(elf,original)

    def test_rejects_unknown_existing_executable_changes(self):
        source=bytearray(self.pristine);source[0x12345]^=1
        with self.assertRaises(ValueError):build_guarded_executable(bytes(source),{'vcpr':367017984})


class VCSISOTwoFixTests(unittest.TestCase):
    def test_from_pristine_irx_auto_installs_both_fixes_in_iso(self):
        if not OPTIONAL_PRISTINE_IRX.is_file(): self.skipTest('Original VCS IRX not mounted')
        irx=OPTIONAL_PRISTINE_IRX.read_bytes()
        self.assertEqual(hashlib.sha256(irx).hexdigest(),EXPECTED_SHA)
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);iso=root/'source.iso';output=root/'output.iso';radio=root/'VCPR.VB'
            fixture(iso,REFERENCE_EE.read_bytes(),irx)
            radio.write_bytes(bytes(0x4000*4))
            sourcehash=hashlib.sha256(iso.read_bytes()).hexdigest()
            report=build_radio_audio_iso(input_iso=iso,replacement_vb=radio,output_iso=output)
            self.assertEqual(report['cdstream_patch_action'],'applied')
            self.assertIn('MODULES/CDSTREAM.IRX',report['iso_targets'])
            self.assertIn('SLUS_215.90',report['iso_targets'])
            self.assertEqual(report['offset_mode'],'vcs-stock')
            output_img=Iso9660Image(output)
            self.assertEqual(output_img.read_entry_bytes('MODULES/CDSTREAM.IRX'),build_iop(irx)[0])
            ee=output_img.read_entry_bytes('SLUS_215.90')
            self.assertEqual(hashlib.sha256(ee).hexdigest(),report['executable_output_sha256'])
            self.assertEqual(actual_ee_helper_result(ee,-1),-36)
            self.assertEqual(actual_ee_helper_result(ee,10_035_648),367_017_984)
            self.assertEqual(hashlib.sha256(iso.read_bytes()).hexdigest(),sourcehash)

    def test_already_patched_irx_kept_with_guarded_ee(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);iso=root/'source.iso';output=root/'output.iso';radio=root/'VCPR.VB'
            irx=REFERENCE_PATCHED_IRX.read_bytes()
            fixture(iso,REFERENCE_EE.read_bytes(),irx)
            radio.write_bytes(bytes(0x4000*4))
            report=build_radio_audio_iso(input_iso=iso,replacement_vb=radio,output_iso=output,offset_mode='vcs-shared')
            self.assertEqual(report['cdstream_patch_action'],'already-patched')
            self.assertNotIn('MODULES/CDSTREAM.IRX',report['iso_targets'])
            self.assertEqual(Iso9660Image(output).read_entry_bytes('MODULES/CDSTREAM.IRX'),irx)
            self.assertIsNotNone(report['offset_patch_report'])

    def test_unknown_irx_rejected_without_writing_output(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);iso=root/'source.iso';output=root/'output.iso';radio=root/'VCPR.VB'
            irx=bytearray(REFERENCE_PATCHED_IRX.read_bytes());irx[0x900]^=1
            fixture(iso,REFERENCE_EE.read_bytes(),bytes(irx))
            radio.write_bytes(bytes(0x4000*4))
            with self.assertRaisesRegex(ValueError,'Unsupported VCS CDSTREAM.IRX'):
                build_radio_audio_iso(input_iso=iso,replacement_vb=radio,output_iso=output)
            self.assertFalse(output.exists())

if __name__ == '__main__': unittest.main()
