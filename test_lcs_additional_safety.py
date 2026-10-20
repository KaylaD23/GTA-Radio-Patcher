#!/usr/bin/env python3
"""ISO extent, source mutation and executable atomic-output regressions.
Run: python test_lcs_additional_safety.py /path/to/SLUS_214.23
"""
from __future__ import annotations
import hashlib
from pathlib import Path
import struct
import sys
import tempfile
from unittest.mock import patch

import lcs_duration_aware_exe_patcher as exe
import lcs_duration_aware_iso_patcher as iso
B = 2048


def record(name, extent, size, flags=0):
    identity = b'\x00' if name == '.' else b'\x01' if name == '..' else name.encode('ascii')
    length = 33 + len(identity) + (not len(identity) % 2)
    out = bytearray(length)
    out[0] = length
    struct.pack_into('<I', out, 2, extent)
    struct.pack_into('>I', out, 6, extent)
    struct.pack_into('<I', out, 10, size)
    struct.pack_into('>I', out, 14, size)
    out[25] = flags
    struct.pack_into('<H', out, 28, 1)
    struct.pack_into('>H', out, 30, 1)
    out[32] = len(identity)
    out[33:33+len(identity)] = identity
    return out


def make_iso(path):
    data = bytearray(B*32)
    p = memoryview(data)[16*B:17*B]
    p[0] = 1; p[1:6] = b'CD001'; p[6] = 1
    p[80:84] = struct.pack('<I', 32); p[84:88] = struct.pack('>I',32)
    p[128:130] = struct.pack('<H', B); p[130:132] = struct.pack('>H',B)
    root = record('.', 20, B, 2)
    p[156:156+len(root)] = root
    term = memoryview(data)[17*B:18*B]; term[0]=255; term[1:6] = b'CD001'; term[6]=1
    records=[record('.',20,B,2), record('..',20,B,2), record('FILE_A.DAT;1',23,100),
             record('FILE_B.DAT;1',23,100)]
    directory=b''.join(records)
    data[20*B:20*B+len(directory)] = directory
    data[23*B:23*B+100] = b'Q'*100
    path.write_bytes(data)


def test_extent_overlap(root):
    original = root/'alias.iso'; make_iso(original)
    repl = root/'a.dat'; repl.write_bytes(b'B'*90)
    patched = root/'alias_patched.iso'
    report = iso.apply_iso_replacements(original, patched, {'FILE_A.DAT': repl})
    changed = iso.Iso9660Image(patched)
    assert changed.read_entry_bytes('FILE_A.DAT') == b'B'*90
    assert changed.read_entry_bytes('FILE_B.DAT') == b'Q'*100, 'overlapping file must remain unmodified'
    assert report[0]['write_method'] == 'appended' and report[0]['relocation_reason'] == 'overlaps another ISO entry'
    print('PASS: overlapping ISO extents safely relocated')


def test_source_change(root):
    original = root/'source.iso'; make_iso(original)
    repl = root/'changing.dat'; repl.write_bytes(b'1'*100)
    destination=root/'older.iso'; destination.write_bytes(b'OLDER OUTPUT MUST SURVIVE')
    reference_copy=iso.shutil.copyfile
    def mutate_after_copy(src, dst):
        result=reference_copy(src,dst)
        repl.write_bytes(b'2'*100) # same size but changed content
        return result
    with patch.object(iso.shutil, 'copyfile', side_effect=mutate_after_copy):
        try:
            iso.apply_iso_replacements(original,destination,{'FILE_A.DAT':repl})
        except IOError as err:
            assert 'changed' in str(err) or 'hash mismatch' in str(err)
        else:
            raise AssertionError('Source changing during build was not rejected')
    assert destination.read_bytes() == b'OLDER OUTPUT MUST SURVIVE'
    assert not list(root.glob('.older.iso.*.building'))
    print('PASS: same-size source mutation refused, output preserved')


def test_duplicate_paths(root):
    original = root/'two.iso'; make_iso(original)
    repl = root/'two.dat'; repl.write_bytes(b'X')
    try:
        iso.apply_iso_replacements(original,root/'bad.iso',{'FILE_A.DAT':repl,'file_a.dat;1':repl})
    except ValueError as err:
        assert 'twice' in str(err)
    else:
        raise AssertionError('Duplicate normalized replacement target accepted')
    print('PASS: duplicate ISO targets refused')


def test_executable_atomic(root, pristine):
    if not pristine.is_file():
        print('SKIP: pristine SLUS_214.23 not supplied')
        return
    inp=root/'SLUS_214.23'; inp.write_bytes(pristine.read_bytes())
    station_file=root/'station.vb'; station_file.write_bytes(bytes(0x4000))
    stations={key:station_file for key,_ in exe.STATIONS}
    destination=root/'already_built.exe'; destination.write_bytes(b'OLDER EXECUTABLE')
    def block_replace(*args,**kwargs):
        raise OSError('injected rename failure')
    with patch.object(exe.os,'replace', side_effect=block_replace):
        try:
            exe.build_patched_executable(inp,stations,destination,offset_mode='stock')
        except OSError as err:
            assert 'injected rename failure' in str(err)
        else:
            raise AssertionError('Failed publish should raise')
    assert destination.read_bytes()==b'OLDER EXECUTABLE'
    assert not list(root.glob('.already_built.exe.*.building'))
    print('PASS: executable write failure leaves old output intact')


def test_atomic_report(root):
    report = root/'previous.json'; report.write_text('{"previous": true}\n')
    with patch.object(iso.os, 'replace', side_effect=OSError('rename blocked')):
        try:
            iso.atomic_write_report(report, '{"new": true}\n')
        except OSError as err:
            assert 'rename blocked' in str(err)
        else:
            raise AssertionError('Atomic report should propagate failed publish')
    assert report.read_text() == '{"previous": true}\n'
    assert not list(root.glob('.previous.json.*.building'))
    iso.atomic_write_report(report, '{"new": true}\n')
    assert report.read_text() == '{"new": true}\n'
    print('PASS: report write preserves previous output on failure')


if __name__=='__main__':
    pristine=Path(sys.argv[1]) if len(sys.argv)>1 else Path('/nonexistent')
    with tempfile.TemporaryDirectory() as temp:
        p=Path(temp)
        test_extent_overlap(p); test_source_change(p); test_duplicate_paths(p); test_executable_atomic(p,pristine); test_atomic_report(p)
    print('PASS: additional ISO/executable safety suite')
