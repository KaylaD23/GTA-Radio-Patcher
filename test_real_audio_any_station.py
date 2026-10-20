"""Long replacement source relabelled to a different station: no KJAH assumption.

Test runs on authentic user-provided audio bytes but a synthetic ISO filesystem
with authentic original ELF and CDSTREAM module. Not PCSX2 playback proof.
"""
import hashlib
import struct
import sys
import tempfile
from pathlib import Path
from test_lcs_native_provider import make_iso
import lcs_duration_aware_iso_patcher as iso
import lcs_duration_aware_exe_patcher as exe
from lcs_patch_calculator import calculate_plan

ROOT = Path(__file__).parent
REAL_FILE = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT/'KJAH.VB'

def run():
    assert REAL_FILE.is_file(),REAL_FILE
    size = REAL_FILE.stat().st_size
    with tempfile.TemporaryDirectory(prefix='lcs-any-station-') as temporary:
        t=Path(temporary)
        pristine = ROOT/'SLUS_214.23.original'
        module = ROOT/'CDSTREAM_ORIGINAL_REFERENCE.IRX'
        orig = t/'source.iso'
        make_iso(orig,pristine.read_bytes(),module.read_bytes())
        selected = t/'replacements';selected.mkdir()
        (selected/'HEAD.VB').symlink_to(REAL_FILE.resolve())
        out = t/'patched.iso'
        result = iso.build_duration_aware_iso(orig,selected,out,offset_mode='iop-elf-stock')
        observed = iso.Iso9660Image(out)
        head=observed.get('AUDIO/MUSIC/HEAD.VB')
        kjah=observed.get('AUDIO/MUSIC/KJAH.VB')
        assert head.size==size, (head.size,size)
        assert kjah.size==0x4000,kjah.size
        patched_elf = observed.read_entry_bytes('SLUS_214.23')
        off=exe.EXPECTED_SEGMENT_OFFSET + exe.LENGTH_TABLE_VA - exe.EXPECTED_SEGMENT_VADDR
        values=struct.unpack_from('<10I',patched_elf,off)
        assert values[0]==size*7//256,(values[0],size*7//256)
        assert values[2]==0x4000*7//256,values[2]
        assert result['iop_seek_patch_report']['extra_shift']==4
        assert [e['station'] for e in result['executable_patch_report']['stations']][:3]==['HEAD.VB','DOUBLE.VB','KJAH.VB']
        assert result['automatic_patch_calculator']['stations']['head']['source_size']==size
        h1=hashlib.file_digest(REAL_FILE.open('rb'),'sha256').hexdigest()
        h=hashlib.sha256()
        with out.open('rb') as stream:
            stream.seek(head.data_lba*2048)
            remaining=head.size
            while remaining:
                buf=stream.read(min(1024*1024,remaining))
                assert buf
                h.update(buf)
                remaining-=len(buf)
        assert h.hexdigest()==h1
        print('PASS actual user audio source was loaded as HEAD.VB, not KJAH.VB')
        print('PASS HEAD boundary:',values[0],'ms; KJAH untouched:',values[2],'ms')
        print('PASS global IOP extra shift:',result['iop_seek_patch_report']['extra_shift'])
        print('PASS copied audio hash:',h.hexdigest())
        print('PASS ISO output',out.stat().st_size,'bytes')

if __name__=='__main__':run()
