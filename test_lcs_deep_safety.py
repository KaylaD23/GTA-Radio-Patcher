#!/usr/bin/env python3
"""New adversarial regression cases: root aliases, out-of-volume extents,
and extreme valid stream sizes and mixed station-length selections.
"""
from pathlib import Path
import tempfile
import struct
import lcs_duration_aware_iso_patcher as iso
import lcs_duration_aware_exe_patcher as patcher
from test_lcs_additional_safety import make_iso

B=2048

def find_root_entry(data:bytes,name:bytes):
    root=data[20*B:21*B]
    cursor=0
    while cursor<len(root) and root[cursor]:
        n=root[cursor]
        r=root[cursor:cursor+n]
        if r[33:33+r[32]]==name:
            return 20*B+cursor
        cursor+=n
    raise AssertionError('file entry not found')

with tempfile.TemporaryDirectory() as td:
    root=Path(td)
    source=root/'reference.iso'; make_iso(source)
    # Manufacture a malformed but still parseable ISO where FILE_A is falsely
    # given the root directory sector as its own file extent.
    corrupted=bytearray(source.read_bytes())
    file_a_offset=find_root_entry(corrupted,b'FILE_A.DAT;1')
    struct.pack_into('<I',corrupted,file_a_offset+2,20)
    struct.pack_into('>I',corrupted,file_a_offset+6,20)
    aliased=root/'root_alias.iso';aliased.write_bytes(corrupted)
    repl=root/'payload.dat';repl.write_bytes(b'X'*90)
    out=root/'root_replacement.iso'
    report=iso.apply_iso_replacements(aliased,out,{'FILE_A.DAT':repl})
    rebuilt=iso.Iso9660Image(out)
    assert rebuilt.read_entry_bytes('FILE_A.DAT')==b'X'*90
    assert rebuilt.get('FILE_B.DAT').data_lba==23
    assert rebuilt.get('FILE_A.DAT').data_lba!=20
    assert report[0]['write_method']=='appended'
    assert report[0]['relocation_reason']=='overlaps another ISO entry'
    print('PASS root-directory extent preserved despite aliased file record')
    # File claims to occupy blocks beyond the logical volume and must be
    # rejected without damaging a previously generated output ISO.
    malformed=bytearray(source.read_bytes())
    struct.pack_into('<I',malformed,file_a_offset+2,31)
    struct.pack_into('>I',malformed,file_a_offset+6,31)
    struct.pack_into('<I',malformed,file_a_offset+10,5000)
    struct.pack_into('>I',malformed,file_a_offset+14,5000)
    broken=root/'broken_extent.iso';broken.write_bytes(malformed)
    preserved=root/'preserve.iso';preserved.write_bytes(b'UNCHANGED')
    try:
        iso.apply_iso_replacements(broken,preserved,{'FILE_A.DAT':repl})
    except ValueError as err:
        assert 'outside the declared volume' in str(err),str(err)
    else:
        raise AssertionError('Out-of-volume source extent not rejected')
    assert preserved.read_bytes()==b'UNCHANGED'
    print('PASS out-of-volume file extent rejected without losing old output')
    # An ISO input being modified by an outside application during copying
    # must not produce a nominally valid replacement image.
    from unittest.mock import patch
    changing=root/'changing_source.iso'; make_iso(changing)
    previous=root/'previous_output.iso'; previous.write_bytes(b'KEEP PREVIOUS')
    original_copy=iso.shutil.copyfile
    def corrupt_iso_after_copy(src,dst):
        result=original_copy(src,dst)
        with changing.open('r+b') as f:
            f.seek(23*B)
            f.write(b'!')
        return result
    with patch.object(iso.shutil, 'copyfile', side_effect=corrupt_iso_after_copy):
        try:
            iso.apply_iso_replacements(changing,previous,{'FILE_A.DAT':repl})
        except IOError as exc:
            assert 'Input ISO changed' in str(exc), str(exc)
        else:
            raise AssertionError('Changed input ISO was accepted')
    assert previous.read_bytes()==b'KEEP PREVIOUS'
    print('PASS concurrent ISO-source mutation detected and previous output preserved')
    # Exercise small/large (including capped) timeline generation and the
    # original user's exact size, without pretending to test live gameplay.
    for size in [0x4000, 0x4000*2, 367_017_984, __import__("lcs_iop_safe_seek").MAX_SAFE_DURATION_SIZE]:
        duration=iso.duration_ms_from_size(size)
        durations=[duration]*10
        sequence=patcher.build_length_adaptive_offset_sequence(durations)
        assert sequence[0]==sequence[-1]==0
        assert len(sequence)-2 <= 4096
        assert len(sequence[1:-1])==len(set(sequence[1:-1]))
        print(f'PASS offset construction: {size:,} bytes, {duration:,} ms, {len(sequence)-2} offsets')
    # For distinct extreme station lengths, generation should either produce
    # a well-formed table or fail explicitly, never silently wrap uint32.
    combinations=[(448, 10035648), (10035648, 448), (448, 896), (10035648, 10035648), (10035648, 20071296)]
    for short,long in combinations:
        try:
            seq=patcher.build_length_adaptive_offset_sequence([short,long]*5)
        except ValueError as err:
            assert str(err), 'A failed validation must explain itself'
        else:
            assert seq[0]==seq[-1]==0 and len(set(seq[1:-1]))==len(seq)-2
        print('PASS explicit handling mixed timelines',short,long)
