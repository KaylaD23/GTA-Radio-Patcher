#!/usr/bin/env python3
"""EXPERIMENTAL VCS US shared-radio-offset expansion (NOT gameplay certified).

Exact SLUS_215.90 original or its known TWO-DIVU-only output. No input is
modified. Build + verify a standalone ELF with a separate read-only PT_LOAD
holding ~5-second byte-position contributions. This changes the reset offset
pool, not the stream seek algorithm or station-switch behaviour. It does NOT
claim a 5-second actual starting-position interval: existing game PRNG and
saved/base-position contributions remain active.

Warning: PT_LOAD loader behaviour and game heap/stack overlap at 0x00780000
have not been tested in PCSX2. Use a disposable ISO; no ISO integration here.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import struct
from pathlib import Path
from typing import Mapping

from gta_radio_profiles import get_profile
from vcs_ee_overflow_patch import (
    ORIGINAL_SHA256, ELF_FILE_SIZE, PATCHES, check_pristine, patched_bytes,
    validate_radio_sizes,
)

STOCK_TABLE_FILE = 0x3A7758
STOCK_WORDS = (1_000_000, 15_000_000, 22_000_000, 6_000_000, 19_000_000,
               33_000_000, 18_000_000, 26_000_000, 2_000_000, 14_000_000,
               29_000_000, 10_000_000, 20_000_000, 34_000_000, 4_000_000,
               17_000_000, 25_000_000, 31_000_000, 8_000_000, 16_000_000,
               5_000_000, 24_000_000, 30_000_000, 7_000_000, 11_000_000,
               21_000_000, 12_000_000, 3_000_000, 23_000_000, 0)
PTR_HI_OFF, PTR_LO_OFF = 0x89A4C, 0x89A54
PTR_HI_EXPECTED, PTR_LO_EXPECTED = 0x3C04004A, 0x24846758
SEGMENT_VA = 0x00780000
SEGMENT_ALIGNMENT = 0x1000
BLOCK_PAIR = 0x4000
SPACING_MS = 5000
MAX_ENTRIES = 4096
MIN_ENTRIES = 64
ELF_PHOFF = 0x34
ELF_SECOND_PHDR = ELF_PHOFF + 32
ELF_PHNUM_OFF = 0x2C
PTR_HI_NEW = 0x3C040078  # lui a0,0x0078
PTR_LO_NEW = 0x24840000  # addiu a0,a0,0
STOCK_SHA256 = hashlib.sha256(struct.pack('<30I', *STOCK_WORDS)).hexdigest()
# Authenticated reset/cursor instructions; cursor is 32-bit LW/SW (not u8).
CURSOR_GUARDS = {
    0x188A9C: 0x8E2505E4,  # lw a1,+0x5e4(s1)
    0x188AA4: 0x24A50001,  # addiu a1,a1,1
    0x188AAC: 0x00051080,  # sll v0,a1,2
    0x188ABC: 0x8C570000,  # lw s7,0(v0)
    0x188AEC: 0x16E00002,  # bnez s7, continued
    0x188AF0: 0xAE2505E4,  # sw a1,+0x5e4(s1)
    0x188AF4: 0xAE2005E4,  # sw zero,+0x5e4(s1)
    0x188BD0: 0x0046001B,  # divu v0,a2: wrap to station length
}


def u32(data: bytes, off: int) -> int:
    return struct.unpack_from('<I', data, off)[0]


def align_up(value: int, alignment: int) -> int:
    return (value + alignment - 1) // alignment * alignment


def current_ee_baseline(data: bytes) -> str:
    """Reject unrelated EE changes instead of silently deleting them."""
    if len(data) != ELF_FILE_SIZE:
        raise ValueError('VCS ELF length does not match authenticated SLUS_215.90')
    if hashlib.sha256(data).hexdigest() == ORIGINAL_SHA256:
        check_pristine(data)
        return 'original'
    # ONLY the two expected EE DIVU changes are accepted as an input variant.
    # Do not accept a different pre-patched executable just because opcodes match.
    candidate = bytearray(data)
    for off, original, new, _name, _va in PATCHES:
        if u32(data, off) != new:
            raise ValueError('Existing VCS ELF patch is not the recognized EE DIVU-only variant')
        struct.pack_into('<I', candidate, off, original)
    if hashlib.sha256(candidate).hexdigest() != ORIGINAL_SHA256:
        raise ValueError('VCS ELF has additional/unknown modifications; refusing to replace existing patches')
    return 'ee-divu-only'


def check_elf_layout(data: bytes) -> tuple[int, int]:
    if data[:4] != b'\x7fELF' or data[4:7] != b'\x01\x01\x01':
        raise ValueError('Expected 32-bit little-endian MIPS ELF')
    if struct.unpack_from('<H', data, 0x12)[0] != 8:
        raise ValueError('Expected MIPS machine ELF')
    phoff = u32(data, 0x1C)
    entsize, phnum = struct.unpack_from('<HH', data, 0x2A)
    if (phoff, entsize, phnum) != (0x34, 32, 1):
        raise ValueError('Unexpected VCS program-header layout')
    p = struct.unpack_from('<8I', data, phoff)
    if p != (1, 0x1000, 0x100000, 0x100000, 0x3CD0A8, 0x64DF54, 7, 0x1000):
        raise ValueError('Unexpected VCS main PT_LOAD layout')
    if data[ELF_SECOND_PHDR:ELF_SECOND_PHDR + 32] != b'\0' * 32:
        raise ValueError('Second ELF program-header slot is not empty')
    if u32(data, PTR_HI_OFF) != PTR_HI_EXPECTED or u32(data, PTR_LO_OFF) != PTR_LO_EXPECTED:
        raise ValueError('VCS stock offset-table pointer instructions differ')
    for address, expected in CURSOR_GUARDS.items():
        executable_offset = 0x1000 + (address - 0x100000)
        if u32(data, executable_offset) != expected:
            raise ValueError(f'VCS reset/cursor opcode mismatch at 0x{address:08X}')
    stock = struct.pack('<30I', *STOCK_WORDS)
    if data[STOCK_TABLE_FILE:STOCK_TABLE_FILE + len(stock)] != stock:
        raise ValueError('VCS original 29-offset table signature differs')
    memory_end = p[2] + p[5]
    if SEGMENT_VA < align_up(memory_end, SEGMENT_ALIGNMENT):
        raise ValueError('Expanded offset segment would overlap ELF load memory')
    if SEGMENT_VA >= 0x02000000:
        raise ValueError('Expanded offset segment is outside PS2 EE main RAM')
    return memory_end, phoff


def select_stride(count: int) -> int:
    if count < 2:
        return 1
    # Rough golden-angle stride: next consecutive choices are far apart in time.
    stride = max(1, round(count * 0.6180339887498948))
    while math.gcd(stride, count) != 1:
        stride = (stride + 1) % count or 1
    return stride


def make_sequence(max_size: int) -> tuple[tuple[int, ...], dict]:
    """Distinct 16-KiB-aligned offsets throughout the longest selected stream."""
    if not isinstance(max_size, int) or max_size < BLOCK_PAIR * 3 or max_size % BLOCK_PAIR:
        raise ValueError('VCS VB must contain at least 3 whole 16-KiB channel pairs')
    block_count = max_size // BLOCK_PAIR
    duration_ms = max_size * 7 // 256
    desired = max(MIN_ENTRIES, min(MAX_ENTRIES, math.ceil(duration_ms / SPACING_MS)))
    count = min(desired, block_count - 1)
    if count <= 0:
        raise ValueError('No distinct aligned nonzero positions are possible')
    if count == 1:
        increasing = (BLOCK_PAIR,)
    else:
        increasing = tuple((1 + (rank * (block_count - 2)) // (count - 1)) * BLOCK_PAIR
                           for rank in range(count))
    if len(set(increasing)) != count or not all(0 < x < max_size for x in increasing):
        raise AssertionError('Invalid/duplicate aligned offsets')
    stride = select_stride(count)
    order = tuple(increasing[(i * stride) % count] for i in range(count))
    # Index 0 is skipped on the first reset. The final 0 terminates the loop.
    # Keep its semantics unchanged: index1..N select offsets, indexN+1=0 resets.
    words = (0, *order, 0)
    if len(words) != count + 2:
        raise AssertionError('Bad expanded table size')
    cyclic_gaps = [min(abs(order[i] - order[(i + 1) % count]),
                       max_size - abs(order[i] - order[(i + 1) % count]))
                   for i in range(count)] if count > 1 else [0]
    return words, {
        'selected_station_max_size_bytes': max_size,
        'selected_station_duration_ms': duration_ms,
        'table_count': count,
        'stock_nonzero_entries': 29,
        'stock_runtime_entries_per_cycle': 28,
        'selected_cursor_entries_per_cycle': count,
        'target_spacing_ms': SPACING_MS,
        'average_distribution_spacing_ms': round(duration_ms / count, 3),
        'stride': stride,
        'smallest_sequential_circular_gap_bytes': min(cyclic_gaps),
        'table_sha256': hashlib.sha256(struct.pack('<' + 'I' * len(words), *words)).hexdigest(),
        'note': 'One shared choice per reset across nine stations. Additional PRNG/saved-position terms remain unchanged; positions and acoustic repetition are not guaranteed.',
    }


def build_executable(source: bytes, replacements: Mapping[str, int]) -> tuple[bytes, dict]:
    baseline = current_ee_baseline(source)
    memory_end, _ = check_elf_layout(source)
    station_keys = {name for name, _ in get_profile('vcs_ps2').stations}
    if not replacements:
        raise ValueError('Select at least one VCS radio replacement so the offset table can match its real length')
    if set(replacements) - station_keys:
        raise ValueError(f'Unknown VCS station(s): {sorted(set(replacements) - station_keys)}')
    validate_radio_sizes(list(replacements.values()))
    max_size = max(replacements.values())
    words, info = make_sequence(max_size)
    table = struct.pack('<' + 'I' * len(words), *words)
    if SEGMENT_VA + len(table) > 0x02000000:
        raise ValueError('Offset table would exceed PS2 EE main RAM')
    # Preserve the known EE duration/position conversion fix in the new output.
    base = patched_bytes(source) if baseline == 'original' else source
    out = bytearray(base)
    table_file = align_up(len(out), SEGMENT_ALIGNMENT)
    if table_file % SEGMENT_ALIGNMENT != SEGMENT_VA % SEGMENT_ALIGNMENT:
        raise AssertionError('PT_LOAD p_offset/p_vaddr alignment mismatch')
    out.extend(b'\0' * (table_file - len(out)))
    out.extend(table)
    struct.pack_into('<8I', out, ELF_SECOND_PHDR,
                     1, table_file, SEGMENT_VA, SEGMENT_VA, len(table), len(table), 4, SEGMENT_ALIGNMENT)
    struct.pack_into('<H', out, ELF_PHNUM_OFF, 2)
    struct.pack_into('<I', out, PTR_HI_OFF, PTR_HI_NEW)
    struct.pack_into('<I', out, PTR_LO_OFF, PTR_LO_NEW)
    generated = bytes(out)
    assert generated[table_file:table_file + len(table)] == table
    assert struct.unpack_from('<8I', generated, ELF_SECOND_PHDR) == (
        1, table_file, SEGMENT_VA, SEGMENT_VA, len(table), len(table), 4, SEGMENT_ALIGNMENT)
    assert struct.unpack_from('<H', generated, ELF_PHNUM_OFF)[0] == 2
    assert u32(generated, PTR_HI_OFF) == PTR_HI_NEW
    assert u32(generated, PTR_LO_OFF) == PTR_LO_NEW
    assert generated[STOCK_TABLE_FILE:STOCK_TABLE_FILE + 120] == source[STOCK_TABLE_FILE:STOCK_TABLE_FILE + 120]
    for off, _old, new, _name, _va in PATCHES:
        assert u32(generated, off) == new
    expected = bytearray(base)
    expected.extend(b'\0' * (table_file - len(expected)))
    expected.extend(table)
    struct.pack_into('<8I', expected, ELF_SECOND_PHDR,
                     1, table_file, SEGMENT_VA, SEGMENT_VA, len(table), len(table), 4, SEGMENT_ALIGNMENT)
    struct.pack_into('<H', expected, ELF_PHNUM_OFF, 2)
    struct.pack_into('<I', expected, PTR_HI_OFF, PTR_HI_NEW)
    struct.pack_into('<I', expected, PTR_LO_OFF, PTR_LO_NEW)
    if bytes(expected) != generated:
        raise AssertionError('Unexpected binary differences')
    return generated, {
        'profile': 'vcs_ps2',
        'revision': 'SLUS_215.90 US',
        'source_baseline': baseline,
        'original_sha256': hashlib.sha256(source).hexdigest(),
        'patched_sha256': hashlib.sha256(generated).hexdigest(),
        'ee_divu_fixes': 'preserved' if baseline == 'ee-divu-only' else 'applied',
        'offset_strategy': 'shared-adaptive-experimental',
        'station_sizes_bytes': dict(replacements),
        'table_va': f'0x{SEGMENT_VA:08X}',
        'table_file_offset': f'0x{table_file:X}',
        'original_ee_pt_load_memory_end': f'0x{memory_end:08X}',
        'gap_after_original_ee_load_bytes': SEGMENT_VA - memory_end,
        'new_segment_end_va': f'0x{SEGMENT_VA + len(table):08X}',
        'program_header_count': 2,
        'status': 'EXPERIMENTAL: STATIC BINARY VERIFIED; NOT PCSX2/PS2 VERIFIED',
        'risk': 'Additional PT_LOAD at 0x00780000 is outside original load span but not confirmed free of heap/stack allocations at runtime.',
        'limitations': ['Changes original random offset table pointer and pool, not the game PRNG, station rebase frequency, saved position or streaming seek.',
                        'Offset candidates may still produce repeated audible sections because other position contributions are added.',
                        'Standalone ELF only; new executable must be installed separately by a verified ISO editing workflow.',
                        'One shared table for nine stations; station-specific mode remains unimplemented for VCS.'],
        'offsets': info,
    }


def patch_file(source: Path, output: Path, replacements: Mapping[str, int]) -> dict:
    source, output = Path(source), Path(output)
    if source.resolve() == output.resolve() or output.exists():
        raise ValueError('Refusing to overwrite source or existing output executable')
    produced, report = build_executable(source.read_bytes(), replacements)
    output.parent.mkdir(parents=True, exist_ok=True)
    created = False
    try:
        with output.open('xb') as dst:
            created = True
            dst.write(produced)
            dst.flush()
            os.fsync(dst.fileno())
    except BaseException:
        if created:
            output.unlink(missing_ok=True)
        raise
    report['output_file'] = str(output)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description='EXPERIMENTAL VCS US PS2 shared ~5s offset expansion; standalone new ELF ONLY')
    parser.add_argument('--input', required=True, type=Path, help='original or two-DIVU-only SLUS_215.90')
    parser.add_argument('--output', required=True, type=Path, help='new file; will not overwrite')
    parser.add_argument('--radio', action='append', default=[], help='VCPR=path/to/VCPR.VB (repeat for multiple radios)')
    parser.add_argument('--report', type=Path, help='optional new JSON file')
    args = parser.parse_args()
    sizes = {}
    for item in args.radio:
        key, sep, path = item.partition('=')
        if not sep:
            parser.error('--radio values must be STATION=path')
        key = key.lower()
        if key in sizes:
            parser.error(f'duplicate station: {key}')
        sizes[key] = Path(path).stat().st_size
    if args.report and args.report.exists():
        parser.error('Report destination already exists; refusing overwrite')
    if args.report and args.report.resolve() in {args.input.resolve(), args.output.resolve()}:
        parser.error('Report cannot collide with input/output')
    report = patch_file(args.input, args.output, sizes)
    if args.report:
        args.report.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report, indent=2))

if __name__ == '__main__':
    main()
