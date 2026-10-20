"""VCS experimental builder regression with real signed original EE/IRX bytes
and a synthetic ISO9660 filesystem. Not a PCSX2 boot test."""
from __future__ import annotations
import hashlib
import math
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from vcs_iso_builder import audit_vcs_iso, build_vcs_iso, STATIONS
from vcs_ee_overflow_patch import ORIGINAL_SHA256, PATCHED_SHA256
from vcs_cdstream_signed_position_patch import EXPECTED_SHA, VCS_PATCHED_SHA
from test_vcs_irx_source_identification import reconstruct_original
from test_lcs_native_provider import rec, dir as iso_dir, put
from lcs_duration_aware_iso_patcher import Iso9660Image, BLOCK
from gta_radio_profiles import get_profile

BASE=Path(__file__).resolve().parent

def fixture(path, *, invalid=None, companions=False):
    ee=(BASE/'SLUS_215.90.reference').read_bytes()
    irx=reconstruct_original()
    assert hashlib.sha256(ee).hexdigest() == ORIGINAL_SHA256
    assert hashlib.sha256(irx).hexdigest() == EXPECTED_SHA
    # Dynamic non-overlapping file extents; root dirs at 20-23.
    sources={
        'SYSTEM.CNF': b'BOOT2 = cdrom0:\\SLUS_215.90;1\n'.ljust(64,b' '),
        'SLUS_215.90': ee if invalid!='exe' else ee[:0x400]+bytes([ee[0x400]^1])+ee[0x401:],
        'MODULES/CDSTREAM.IRX': irx if invalid!='irx' else irx[:0x100]+bytes([irx[0x100]^1])+irx[0x101:],
    }
    for station, fn in STATIONS.items():
        sources['AUDIO/MUSIC/'+fn] = (station.encode()+b'\0'*16)[:16] * (0xc000//16)
    if companions:
        sources['TEXT/ENGLISH.GXT']=b'original english'.ljust(64,b'\0')
        sources['CHK/PS2/GAME.DTZ']=b'original dtz'.ljust(64,b'\0')
    lbas={};cursor=30
    for name,data in sources.items():
        lbas[name]=cursor;cursor+=math.ceil(len(data)/BLOCK)
    count=cursor+10
    disk=bytearray(count*BLOCK)
    def rpath(path):return rec(path.split('/')[-1]+';1',lbas[path],len(sources[path]))
    hdr=memoryview(disk)[16*BLOCK:17*BLOCK]
    hdr[0]=1;hdr[1:6]=b'CD001';hdr[6]=1
    hdr[80:84]=struct.pack('<I',count);hdr[84:88]=struct.pack('>I',count)
    hdr[128:130]=struct.pack('<H',BLOCK);hdr[130:132]=struct.pack('>H',BLOCK)
    hdr[156:156+len(rec('.',20,BLOCK,2))]=rec('.',20,BLOCK,2)
    disk[17*BLOCK:17*BLOCK+7]=b'\xffCD001\x01'
    root=[rec('.',20,BLOCK,2),rec('..',20,BLOCK,2),rec('AUDIO',21,BLOCK,2),
          rec('MODULES',23,BLOCK,2),rpath('SLUS_215.90'),rpath('SYSTEM.CNF')]
    if companions:
        root += [rec('TEXT',24,BLOCK,2),rec('CHK',25,BLOCK,2)]
    put(disk,20,iso_dir(root))
    put(disk,21,iso_dir([rec('.',21,BLOCK,2),rec('..',20,BLOCK,2),rec('MUSIC',22,BLOCK,2)]))
    put(disk,22,iso_dir([rec('.',22,BLOCK,2),rec('..',21,BLOCK,2),
                         *[rpath('AUDIO/MUSIC/'+fn) for fn in STATIONS.values()]]))
    put(disk,23,iso_dir([rec('.',23,BLOCK,2),rec('..',20,BLOCK,2),rpath('MODULES/CDSTREAM.IRX')]))
    if companions:
        put(disk,24,iso_dir([rec('.',24,BLOCK,2),rec('..',20,BLOCK,2),rpath('TEXT/ENGLISH.GXT')]))
        put(disk,25,iso_dir([rec('.',25,BLOCK,2),rec('..',20,BLOCK,2),rec('PS2',26,BLOCK,2)]))
        put(disk,26,iso_dir([rec('.',26,BLOCK,2),rec('..',25,BLOCK,2),rpath('PS2/GAME.DTZ')]))
    for name,data in sources.items():put(disk,lbas[name],data)
    if invalid=='overlap':
        # Declare VCPR to overlap the next station (recompute both endians).
        at=22*BLOCK
        while disk[at]:
            n=disk[at]
            if b'VCPR.VB;1' in disk[at:at+n]:
                struct.pack_into('<I',disk,at+10,0x100000)
                struct.pack_into('>I',disk,at+14,0x100000)
                break
            at+=n
    if invalid=='volume':
        struct.pack_into('<I',disk,16*BLOCK+80,count-1)
        struct.pack_into('>I',disk,16*BLOCK+84,count-1)
    path.write_bytes(disk)

class VCSISOBuilderTests(unittest.TestCase):
    def test_clean_full_build(self):
        self.assertTrue(get_profile('vcs_ps2').can_build)
        with tempfile.TemporaryDirectory() as td:
            d=Path(td);src=d/'src.iso';out=d/'out.iso';rd=d/'mods';rd.mkdir()
            fixture(src,companions=True)
            original_hash=hashlib.sha256(src.read_bytes()).hexdigest()
            radio=b'R'*0x10000
            (rd/'VCPR.VB').write_bytes(radio)
            (rd/'ENGLISH.GXT').write_bytes(b'new english')
            (rd/'GAME.DTZ').write_bytes(b'new game')
            result=build_vcs_iso(input_iso=src,output_iso=out,replacement_dir=rd)
            self.assertEqual(original_hash,hashlib.sha256(src.read_bytes()).hexdigest())
            verified=Iso9660Image(out)
            self.assertEqual(sha256_bytes(verified.read_entry_bytes('SLUS_215.90')),PATCHED_SHA256)
            self.assertEqual(sha256_bytes(verified.read_entry_bytes('MODULES/CDSTREAM.IRX')),VCS_PATCHED_SHA)
            self.assertEqual(verified.read_entry_bytes('AUDIO/MUSIC/VCPR.VB'),radio)
            self.assertEqual(verified.read_entry_bytes('TEXT/ENGLISH.GXT'),b'new english')
            self.assertEqual(verified.read_entry_bytes('PS2/GAME.DTZ'),b'new game')
            self.assertEqual(result['installed_sizes']['vcpr'],len(radio))
            self.assertEqual(len(result['iso_files_replaced']),5)
            self.assertTrue(all(row['write_method']=='appended' for row in result['iso_files_replaced']))
            self.assertFalse(result['runtime_verified'])
            self.assertTrue(Path(result['report_path']).is_file())
            with self.assertRaises(ValueError):
                build_vcs_iso(input_iso=src,output_iso=out,replacement_dir=rd)
            with self.assertRaises(ValueError):
                build_vcs_iso(input_iso=out,output_iso=d/'second.iso',replacement_dir=rd)
    def test_reject_corrupt_or_unsupported_source(self):
        with tempfile.TemporaryDirectory() as td:
            d=Path(td);rd=d/'replacements';rd.mkdir()
            (rd/'VCPR.VB').write_bytes(b'V'*0xc000)
            for corruption in ('exe','irx','overlap','volume'):
                src=d/(corruption+'.iso');out=d/(corruption+'_out.iso')
                fixture(src,invalid=corruption)
                with self.subTest(corruption=corruption):
                    with self.assertRaises(ValueError):
                        build_vcs_iso(input_iso=src,output_iso=out,replacement_dir=rd)
                    self.assertFalse(out.exists())
    def test_reject_unknown_station_and_unaligned_audio(self):
        with tempfile.TemporaryDirectory() as td:
            d=Path(td);src=d/'src.iso';out=d/'out.iso';rd=d/'mods';rd.mkdir()
            fixture(src)
            (rd/'KJAH.VB').write_bytes(b'K'*0xc000)
            with self.assertRaises(ValueError):build_vcs_iso(input_iso=src,output_iso=out,replacement_dir=rd)
            (rd/'KJAH.VB').unlink()
            (rd/'VCPR.VB').write_bytes(b'V'*0xc010)
            with self.assertRaises(ValueError):build_vcs_iso(input_iso=src,output_iso=out,replacement_dir=rd)
            self.assertFalse(out.exists())
    def test_external_patched_exe_accepted_and_unsupported_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            d=Path(td);src=d/'src.iso';out=d/'out.iso';audio=d/'VCPR.VB';fixture(src)
            audio.write_bytes(b'V'*0xc000)
            from vcs_ee_overflow_patch import patched_bytes
            patched=d/'custom.elf';patched.write_bytes(patched_bytes((BASE/'SLUS_215.90.reference').read_bytes()))
            build_vcs_iso(input_iso=src,output_iso=out,replacement_dir=audio,executable_path=patched)
            other=d/'other.elf';other.write_bytes(b'other')
            with self.assertRaises(ValueError):
                build_vcs_iso(input_iso=src,output_iso=d/'out2.iso',replacement_dir=audio,executable_path=other)
    def test_nonexistent_companion_target_is_failure_not_silent_skip(self):
        with tempfile.TemporaryDirectory() as td:
            d=Path(td);src=d/'src.iso';out=d/'out.iso';rd=d/'mods';rd.mkdir();fixture(src)
            (rd/'VCPR.VB').write_bytes(b'V'*0xc000)
            (rd/'GAME.DTZ').write_bytes(b'game')
            with self.assertRaisesRegex(ValueError,'companion'):
                build_vcs_iso(input_iso=src,output_iso=out,replacement_dir=rd)
            self.assertFalse(out.exists())

def sha256_bytes(data):return hashlib.sha256(data).hexdigest()

if __name__=='__main__':unittest.main()
