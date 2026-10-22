#!/usr/bin/env python3
"""Exact-revision VCS EE conversion patch with a negative-position guard.

The stock station-length path is converted from DIV to DIVU. The 0x90100
position path uses an R5900-supported branch trampoline: signed DIV for
negative *input positions*, unsigned DIVU for nonnegative positions. This
preserves the original low-32-bit shift/division semantics on negative input.
No DDIV instruction is used (the R5900 does not implement DDIV/DDIVU).

An extra executable PT_LOAD at 0x00780000 holds only a 32-byte helper (and,
optionally, the existing experimental adaptive offset table). Its placement
is statically checked, but heap/loader safety remains unverified in PCSX2.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import struct
from typing import Mapping

from vcs_ee_overflow_patch import PATCHES, patched_bytes, validate_radio_sizes, MAX_ALIGNED_SAFE_BYTES, MAX_ALIGNED_SAFE_MS, TARGET_MIN_SOURCE_MS, TARGET_MAX_SOURCE_MS
from vcs_adaptive_offsets import (current_ee_baseline, build_executable as adaptive_build,
    check_elf_layout, SEGMENT_ALIGNMENT, SEGMENT_VA, ELF_SECOND_PHDR,
    ELF_PHNUM_OFF, align_up, u32)

POSITION_DIV_OFF = 0xF75C
POSITION_BRANCH_OFF = 0xF760
ORIGINAL_BRANCH = 0x50600001      # beql v1,zero,+1, following fixed divisor=7
CONTINUE_VA = 0x0010E768
SEGMENT_FLAGS_RX = 5
HELPER_SIZE = 32


def j(target_va: int) -> int:
    if target_va % 4 or (target_va >> 28) != 0:
        raise ValueError('VCS EE helper outside same 256-MB J target region')
    return (2 << 26) | ((target_va >> 2) & 0x03ffffff)


def bltz(rs: int, here: int, target: int) -> int:
    delta = target - (here + 4)
    if delta % 4 or not -32768 <= delta // 4 <= 32767:
        raise ValueError('VCS EE signed branch out of range')
    return (1 << 26) | (rs << 21) | ((delta // 4) & 0xffff)


def helper_words(va: int) -> tuple[int, ...]:
    # Registers on entry: s0 = original signed position, v0 = (s0 << 8) low
    # 32 bits, v1=7. No GPR is changed; only original division HI/LO outputs.
    # Restore/return path is the ORIGINAL instruction 0x10E768, preserving the
    # original mflo and function epilogue.
    return (
        bltz(16, va, va + 20), # bltz s0, signed (check ORIGINAL position)
        0,                    # delay slot nop
        0x0043001B,           # divu v0,v1 for nonnegative position
        j(CONTINUE_VA),       # j original restore + mflo
        0,                    # delay slot nop
        0x0043001A,           # signed: div v0,v1 for negative position
        j(CONTINUE_VA),
        0,                    # delay slot nop
    )


def _write_u32(buffer: bytearray, at: int, value: int) -> None:
    struct.pack_into('<I', buffer, at, value)


def build_guarded_executable(source: bytes, replacements: Mapping[str, int] | None = None,
                             *, adaptive: bool = False) -> tuple[bytes, dict]:
    """Return a fresh standalone EE patched executable; never alters source."""
    baseline = current_ee_baseline(source) # authentic pristine or known 2-DIVU only
    check_elf_layout(source)
    station_sizes = dict(replacements or {})
    if station_sizes:
        validate_radio_sizes(list(station_sizes.values()))
    if u32(source, POSITION_BRANCH_OFF) != ORIGINAL_BRANCH:
        raise ValueError('Unexpected EE 0x90100 divisor guard; refusing to hook')
    if adaptive:
        base, offset_report = adaptive_build(source, station_sizes)
        buf = bytearray(base)
        ph = struct.unpack_from('<8I', buf, ELF_SECOND_PHDR)
        if ph[:1] != (1,) or ph[2] != SEGMENT_VA or ph[4] != ph[5] or ph[6] != 4:
            raise ValueError('Unexpected VCS adaptive-offset PT_LOAD layout')
        file_start, seg_len = ph[1], ph[4]
        table_end = file_start + seg_len
        if table_end != len(buf):
            raise ValueError('Adaptive table must end at executable EOF')
        padding = (-seg_len) % 16
        buf.extend(b'\0' * padding)
        helper_off = len(buf)
        helper_va = SEGMENT_VA + seg_len + padding
        new_len = seg_len + padding + HELPER_SIZE
        _write_u32(buf, ELF_SECOND_PHDR + 16, new_len)
        _write_u32(buf, ELF_SECOND_PHDR + 20, new_len)
        _write_u32(buf, ELF_SECOND_PHDR + 24, SEGMENT_FLAGS_RX)
    else:
        base = patched_bytes(source) if baseline == 'original' else source
        buf = bytearray(base)
        memory_end, _ = check_elf_layout(source)
        # The extra PT_LOAD starts above the original segment's full p_memsz.
        if SEGMENT_VA < align_up(memory_end, SEGMENT_ALIGNMENT):
            raise ValueError('Guard segment intersects original executable memory')
        file_start = align_up(len(buf), SEGMENT_ALIGNMENT)
        buf.extend(b'\0' * (file_start - len(buf)))
        helper_off = len(buf)
        helper_va = SEGMENT_VA
        new_len = HELPER_SIZE
        struct.pack_into('<8I', buf, ELF_SECOND_PHDR,
                         1, file_start, SEGMENT_VA, SEGMENT_VA,
                         new_len, new_len, SEGMENT_FLAGS_RX, SEGMENT_ALIGNMENT)
        struct.pack_into('<H', buf, ELF_PHNUM_OFF, 2)
        offset_report = None
    if helper_va + HELPER_SIZE > 0x02000000 or helper_va % 16:
        raise ValueError('EE negative-position helper outside aligned EE RAM')
    words = helper_words(helper_va)
    buf.extend(struct.pack('<8I', *words))
    # Replace DIVU and fixed-divisor guard with jump+nop; the signed and
    # unsigned DIVs live in the helper. The original next live instruction
    # remains at 0x10E768 and includes the two loads before mflo.
    if u32(buf, POSITION_DIV_OFF) != PATCHES[1][2]:
        raise ValueError('Position DIVU site not in expected baseline state')
    _write_u32(buf, POSITION_DIV_OFF, j(helper_va))
    _write_u32(buf, POSITION_BRANCH_OFF, 0)
    built = bytes(buf)
    if built[helper_off:helper_off + HELPER_SIZE] != struct.pack('<8I', *words):
        raise AssertionError('Missing guarded EE helper words')
    if u32(built, 0xF8A8) != PATCHES[0][2]:
        raise AssertionError('Stock station length DIVU lost')
    if u32(built, POSITION_DIV_OFF) != j(helper_va):
        raise AssertionError('Position hook not installed')
    return built, {
        'profile': 'vcs_ps2', 'revision':'SLUS_215.90 US',
        'source_baseline': baseline, 'original_sha256': hashlib.sha256(source).hexdigest(),
        'patched_sha256': hashlib.sha256(built).hexdigest(),
        'ee_position_negative_guard': 'signed DIV for negative s0; unsigned DIVU for nonnegative s0',
        'original_ee_position_epilogue': 'preserved at 0x0010E768',
        'ee_guard_va': f'0x{helper_va:08X}', 'ee_guard_file_offset':f'0x{helper_off:X}',
        'ee_guard_words': [f'0x{w:08X}' for w in words],
        'offset_strategy': 'shared-adaptive-experimental' if adaptive else 'stock',
        'station_sizes_bytes': station_sizes,
        'max_supported_aligned_bytes':MAX_ALIGNED_SAFE_BYTES,
        'max_supported_aligned_duration_ms':MAX_ALIGNED_SAFE_MS,
        'vcs_target_source_duration_ms':[TARGET_MIN_SOURCE_MS,TARGET_MAX_SOURCE_MS],
        'offsets': offset_report['offsets'] if offset_report else None,
        'offset_patch_report': offset_report,
        'experimental_runtime_memory': '0x00780000 additional ELF PT_LOAD; runtime heap/loader safety not established',
        'gameplay_verified': False,
    }


def patch_file(source: Path, output: Path, replacements: Mapping[str, int] | None = None,
               *, adaptive: bool = False) -> dict:
    source, output = Path(source), Path(output)
    if source.resolve() == output.resolve() or output.exists():
        raise ValueError('Input and output must differ; overwrite disabled')
    code, result = build_guarded_executable(source.read_bytes(), replacements, adaptive=adaptive)
    output.parent.mkdir(parents=True, exist_ok=True)
    created = False
    try:
        with output.open('xb') as dest:
            created = True
            dest.write(code)
            dest.flush()
            os.fsync(dest.fileno())
    except BaseException:
        if created: output.unlink(missing_ok=True)
        raise
    result['output_file'] = str(output)
    result['output_verified'] = (output.read_bytes() == code)
    if not result['output_verified']:
        output.unlink(missing_ok=True)
        raise IOError('VCS standalone guarded executable output verification failed')
    return result
