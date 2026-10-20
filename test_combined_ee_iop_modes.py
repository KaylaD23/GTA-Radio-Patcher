"""End-to-end combined EE+IOP regression on authentic source binaries and a synthetic ISO.

Default quick tests use a short, structurally valid ADPCM replacement; add the
real 367 MB KJAH path as argv[1] to run long replacement integration as well.
"""
import sys
import hashlib
import struct
import tempfile
from pathlib import Path
from test_lcs_native_provider import make_iso
import lcs_duration_aware_iso_patcher as iso
import lcs_duration_aware_exe_patcher as exe
from lcs_iop_safe_seek import patch_cdstream

BASE = Path(__file__).parent
ORIGINAL_ELF = BASE / 'SLUS_214.23.original'
ORIGINAL_IRX = BASE / 'CDSTREAM_ORIGINAL_REFERENCE.IRX'
assert ORIGINAL_ELF.exists(), 'Test source missing authenticated ELF'
assert ORIGINAL_IRX.exists(), 'Test source missing authenticated IRX'

def run():
    elf = ORIGINAL_ELF.read_bytes(); irx = ORIGINAL_IRX.read_bytes()
    assert exe.validate_original_executable(elf)
    with tempfile.TemporaryDirectory(prefix='combined_lcs_') as tmp:
        d=Path(tmp)
        source=d/'source.iso'; make_iso(source, elf, irx)
        inputs=d/'replacements';inputs.mkdir()
        # Valid stereo frame repeated (0x4000 multiple)
        short = inputs/'KJAH.VB'; short.write_bytes((b'\x0c\x00'+bytes(14))*(0x8000//16))
        for mode in ('iop-elf-stock','iop-elf-shared','iop-elf-station-specific'):
            target=d/f'{mode}.iso'
            result=iso.build_duration_aware_iso(source,inputs,target,offset_mode=mode)
            out=iso.Iso9660Image(target)
            patched=out.read_entry_bytes('SLUS_214.23')
            meta=result['executable_patch_report']
            assert meta['length_getter_modified'] is True
            assert exe.sha256(patched)==meta['output_sha256']
            assert patched[exe.GETTER_FILE_OFFSET:exe.GETTER_FILE_OFFSET+len(exe.PATCH_BYTES)]==exe.PATCH_BYTES
            assert patched != elf
            assert result['combined_executable_and_iop_patch'] is True
            assert result['ee_offset_mode']==iso.COMBINED_EE_MODES[mode]
            assert out.read_entry_bytes('MODULES/CDSTREAM.IRX')==irx, 'Short stock streams need no extra IOP shift'
            assert result['iop_seek_patch_report']['extra_shift']==0
            assert out.get('AUDIO/MUSIC/KJAH.VB').size==0x40000, 'Tiny input must be padded for safe IOP buffer wrap'
            if mode == 'iop-elf-stock':
                assert struct.unpack_from('<H',patched,exe.ELF32_EHDR_PHNUM_OFFSET)[0]==1
            else:
                assert struct.unpack_from('<H',patched,exe.ELF32_EHDR_PHNUM_OFFSET)[0]==2
            print('PASS short combined mode',mode,exe.sha256(patched))
        # A prior patched ELF is not an acceptable original (even with external ELF input).
        already_patched=d/'repatched.iso'
        make_iso(already_patched,elf,irx,modify_exe=True)
        existing=d/'do_not_touch.iso'; existing.write_bytes(b'UNCHANGED')
        try:
            iso.build_duration_aware_iso(already_patched,inputs,existing,executable_path=ORIGINAL_ELF,offset_mode='iop-elf-stock')
        except ValueError as err: assert 'ORIGINAL unmodified' in str(err),str(err)
        else: raise AssertionError('Combined build allowed already-patched source ELF')
        assert existing.read_bytes()==b'UNCHANGED'
        print('PASS modified source rejected, original output remains intact')
        changed_irx=d/'bad_irx.iso';make_iso(changed_irx,elf,irx,modify_irx=True)
        try:
            iso.build_duration_aware_iso(changed_irx,inputs,d/'bad_irx_output.iso',offset_mode='iop-elf-stock')
        except ValueError as err: assert 'original verified CDSTREAM.IRX' in str(err),str(err)
        else: raise AssertionError('Combined build allowed unverified IOP')
        print('PASS unverified IOP rejected')
        # External ELF: correctly identifies pristine embedded copy and validates it.
        summary=iso.inspect_input_iso(source,ORIGINAL_ELF)
        assert summary['embedded_executable_matches_supported'] is True
        print('PASS standalone original ELF correctly recognized')
        if len(sys.argv)>1:
            real=Path(sys.argv[1]); assert real.exists()
            long_source=inputs/'KJAH.VB';long_source.unlink();long_source.symlink_to(real)
            target=d/'real_long.iso'
            result=iso.build_duration_aware_iso(source,inputs,target,executable_path=ORIGINAL_ELF,offset_mode='iop-elf-stock')
            assert result['iop_seek_patch_report']['extra_shift']==4
            assert {r['iso_path'] for r in result['iso_files_replaced']}=={'AUDIO/MUSIC/KJAH.VB','MODULES/CDSTREAM.IRX','SLUS_214.23'}
            out=iso.Iso9660Image(target)
            patched=out.read_entry_bytes('SLUS_214.23')
            assert patched != elf
            assert patched[exe.GETTER_FILE_OFFSET:exe.GETTER_FILE_OFFSET+len(exe.PATCH_BYTES)]==exe.PATCH_BYTES
            assert struct.unpack_from('<I',patched,exe.EXPECTED_SEGMENT_OFFSET+(exe.LENGTH_TABLE_VA-exe.EXPECTED_SEGMENT_VADDR)+2*4)[0]==10035648
            expected_irx,_=patch_cdstream(irx,[0x4000]*2+[real.stat().st_size]+[0x4000]*7)
            assert out.read_entry_bytes('MODULES/CDSTREAM.IRX') == expected_irx
            entry=out.get('AUDIO/MUSIC/KJAH.VB')
            h=hashlib.sha256()
            with target.open('rb') as f:
                f.seek(entry.data_lba*2048);rem=entry.size
                while rem:
                    block=f.read(min(1<<20,rem));assert block;h.update(block);rem-=len(block)
            hr=hashlib.file_digest(real.open('rb'),'sha256').hexdigest()
            assert h.hexdigest()==hr
            print('PASS long combined real KJAH, original ELF modified, IOP patched and bytes verified',entry.size)
    print('COMBINED EE+IOP REGRESSION ALL PASS')

if __name__=='__main__':run()
