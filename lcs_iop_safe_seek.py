#!/usr/bin/env python3
"""Signature-checked GTA LCS PS2 CDSTREAM.IRX seek/time overflow corrections.

All patch parameters derive from the input audio size. For long files, seek
interpolation is rescaled to avoid signed 32-bit MFLO overflow, the position
getter is rearranged to shift bytes before multiplication by seven, and the
length getter is similarly corrected if the input exceeds its unsigned limit.
These edits preserve module size. Position conversion relies on the game's
sector-aligned stream positions, which are divisible by 256; this constraint
is verified in modeled input paths but not exhaustively in actual gameplay.

Only the original North American module hash is supported. Tests are static,
not a substitute for PCSX2 or hardware playback verification.
"""
from __future__ import annotations

import hashlib
import math
import os
import struct
import tempfile
from pathlib import Path

ORIGINAL_SHA256 = 'fcb4fff15053652ab162e046d491697fdbf08eeb0954d3d870d6a1b07a2fed43'
# Original instructions at .text + offsets. Section file offset is 0xA0.
TEXT_OFFSET = 0xA0
SHR_OFFSET = 0x1538
SHL_OFFSET = 0x172C
BASE_SHR = 1
BASE_SHL = 1
MAX_32_PRODUCT = (1<<31)-1
# Original IOP 7*size multiplication wraps at this old ceiling.
NATIVE_UNPATCHED_DURATION_LIMIT = ((1<<32)-1)//7
# Five hours of 32kHz stereo PSX ADPCM is ~658,285,715 bytes; the real
# interleaved file must be aligned to 0x4000, so allow the immediately
# following block (5h + at most 0.447s encoding granularity).
MAX_TARGET_DURATION_MS = 5 * 60 * 60 * 1000
MAX_SAFE_DURATION_SIZE = (((MAX_TARGET_DURATION_MS * 256 + 6)//7 + 0x3fff)//0x4000)*0x4000
# Original CDSTREAM.IRX length getter can be corrected in-place by dividing
# the byte count by 256 *before* multiplying by 7. This is exact for every
# supported 0x4000-byte-aligned stream and cannot overflow within 5h.
LENGTH_WORD_OFFSETS = (0x2018, 0x201C, 0x2020, 0x2034)
ORIGINAL_LENGTH_WORDS = (
    0x000410C0, # sll v0,a0,3
    0x00441023, # subu v0,v0,a0
    0x000210C2, # srl v0,v0,3
    0x00021142, # srl v0,v0,5 (branch delay slot)
)
FIXED_LENGTH_WORDS = (
    0x00041202, # srl v0,a0,8 : divide file bytes first
    0x000220C0, # sll a0,v0,3
    0x00821023, # subu v0,a0,v0 : (size>>8)*7
    0x00021002, # srl v0,v0,0 (identity, delay slot)
)
# Playback-position getter 0x20ac also multiplies byte offset by seven
# before dividing by 256. This one is SIGNED and overflows at about 293 MiB.
# Inputs come from 0x4000-aligned seek positions and sector-sized IOP buffer
# advances. The replacement performs exact integer conversion for offsets
# divisible by 256, including negative read-buffer-relative offsets.
NATIVE_POSITION_SIGNED_LIMIT = ((1<<31)-1)//7
POSITION_WORD_OFFSETS = (0x20FC,0x2180,0x2184,0x2190,0x2194,0x21A0,0x21A8)
ORIGINAL_POSITION_WORDS = (
    0x000610C0, 0x000610C0, 0x00461023, 0x24420007,
    0x000210C3, 0x2442001F, 0x00021143,
)
FIXED_POSITION_WORDS = (
    0x00061203, # sra v0,a2,8 at branch 0x20FC
    0x00061203, # sra v0,a2,8 at main 0x2180
    0x000230C0, # sll a2,v0,3
    0x00000000, # no sign-correction when input divisible by 256
    0x00C21023, # subu v0,a2,v0: (byte_offset>>8)*7
    0x00000000, # no sign-correction
    0x00021003, # sra v0,v0,0: return computed result
)



def sha256(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()


def duration_ms(size: int) -> int:
    if type(size) is not int:
        raise ValueError(f'Radio size must be an exact integer byte count, got {size!r}')
    if size <= 0 or size % 0x4000:
        raise ValueError(f'Radio size must be a positive multiple of 0x4000 bytes: {size}')
    if size > MAX_SAFE_DURATION_SIZE:
        raise ValueError(f'Radio size {size} exceeds configured 5-hour input limit ({MAX_SAFE_DURATION_SIZE} aligned bytes)')
    return size*7//256


def _mips_srl(reg: int, source: int, amount: int) -> int:
    return (0 << 26) | (source << 16) | (reg << 11) | (amount << 6) | 2


def _mips_sll(reg: int, source: int, amount: int) -> int:
    return (0 << 26) | (source << 16) | (reg << 11) | (amount << 6) | 0


def _maximum_product(size: int, extra_shift: int) -> int:
    """Upper bound for any size-dependent MUL of all 8 interpolation segments.

    $8 = (file size >> (1+extra_shift)) >> 3
    $5 = (duration_ms // 1000) >> 3
    so any within-segment multiplier is at most this product.
    """
    dur_sec = duration_ms(size) // 1000
    return (size >> (4 + extra_shift)) * (dur_sec >> 3)


def select_extra_shift(sizes: list[int]) -> int:
    if not sizes:
        raise ValueError('Provide original or replacement .VB sizes for all radio stations')
    for size in sizes:
        duration_ms(size)
    largest = max(sizes)
    for extra in range(0, 25):
        if _maximum_product(largest, extra) <= MAX_32_PRODUCT:
            # Even a two-instruction patch does not solve the far larger limit
            # of the 32-bit native length multiplication. Reject if impractical.
            return extra
    raise ValueError('Cannot safely scale seek calculations for the supplied streams')


def expected_instruction_words(extra_shift: int) -> tuple[int,int]:
    if not 0 <= extra_shift <= 24:
        raise ValueError('Out-of-range extra shift')
    return _mips_srl(2, 2, BASE_SHR + extra_shift), _mips_sll(2, 2, BASE_SHL + extra_shift)


def patch_cdstream(original: bytes, sizes: list[int]) -> tuple[bytes, dict]:
    if sha256(original) != ORIGINAL_SHA256:
        raise ValueError('CDSTREAM.IRX is not the exact supported original module; refusing binary patch')
    if original[:4] != b'\x7fELF' or original[5] != 1:
        raise ValueError('Invalid little-endian IRX/ELF')
    extra = select_extra_shift(sizes)
    old_words = expected_instruction_words(0)
    for address, old in zip((SHR_OFFSET, SHL_OFFSET), old_words):
        actual = struct.unpack_from('<I', original, TEXT_OFFSET + address)[0]
        if actual != old:
            raise ValueError(f'Instruction signature mismatch at .text+0x{address:X}: {actual:08X}')
    for address, old in zip(LENGTH_WORD_OFFSETS, ORIGINAL_LENGTH_WORDS):
        actual = struct.unpack_from('<I', original, TEXT_OFFSET + address)[0]
        if actual != old:
            raise ValueError(f'Length instruction signature mismatch at .text+0x{address:X}: {actual:08X}, expected {old:08X}')
    for address, old in zip(POSITION_WORD_OFFSETS, ORIGINAL_POSITION_WORDS):
        actual = struct.unpack_from('<I', original, TEXT_OFFSET + address)[0]
        if actual != old:
            raise ValueError(f'Position instruction signature mismatch at .text+0x{address:X}: {actual:08X}, expected {old:08X}')
    needs_length_fix = max(sizes) > NATIVE_UNPATCHED_DURATION_LIMIT
    needs_position_fix = max(sizes) > NATIVE_POSITION_SIGNED_LIMIT
    if extra == 0 and not needs_length_fix and not needs_position_fix:
        return original, {'strategy':'original-math-safe-for-inputs', 'extra_shift':0,
                          'duration_overflow_fix_required':False,
                          'position_overflow_fix_required':False,
                          'source_sha256':sha256(original),'output_sha256':sha256(original),
                          'maximum_signed_multiplication':_maximum_product(max(sizes),0),
                          'changed_instruction_offsets':[]}
    new_words = expected_instruction_words(extra)
    result = bytearray(original)
    if extra:
        for address, new in zip((SHR_OFFSET, SHL_OFFSET), new_words):
            struct.pack_into('<I', result, TEXT_OFFSET + address, new)
    if needs_length_fix:
        for address, new in zip(LENGTH_WORD_OFFSETS, FIXED_LENGTH_WORDS):
            struct.pack_into('<I', result, TEXT_OFFSET + address, new)
    if needs_position_fix:
        for address, new in zip(POSITION_WORD_OFFSETS, FIXED_POSITION_WORDS):
            struct.pack_into('<I', result, TEXT_OFFSET + address, new)
    changed_offsets = ((SHR_OFFSET,SHL_OFFSET) if extra else ()) + (LENGTH_WORD_OFFSETS if needs_length_fix else ()) + (POSITION_WORD_OFFSETS if needs_position_fix else ())
    expected_changes = {TEXT_OFFSET + a + k for a in changed_offsets for k in range(4)}
    diff_positions = {i for i,(a,b) in enumerate(zip(original,result)) if a != b}
    if not diff_positions or not diff_positions <= expected_changes or len(result)!=len(original):
        raise AssertionError('Unexpected IRX patch output differences')
    report = {
        'strategy': 'calculated-IOP-seek-and-duration',
        'duration_overflow_fix_required': needs_length_fix,
        'position_overflow_fix_required': needs_position_fix,
        'position_conversion_formula': '(signed_byte_position >> 8) * 7 (256-byte-aligned positions)',
        'duration_conversion_formula': '(size_bytes >> 8) * 7 (for aligned VB)',
        'extra_shift':extra,
        'max_stream_size':max(sizes),
        'max_stream_duration_ms':duration_ms(max(sizes)),
        'maximum_signed_multiplication':_maximum_product(max(sizes),extra),
        'max_signed_multiplication_limit':MAX_32_PRODUCT,
        'quantization_bytes':1<<(BASE_SHL+extra),
        'source_sha256':sha256(original),'output_sha256':sha256(result),
        'changed_instruction_offsets':[
            {'irx_file_offset':f'0x{TEXT_OFFSET + off:04X}', 'text_offset':f'0x{off:04X}',
             'old_word':f'0x{old:08X}', 'new_word':f'0x{new:08X}'}
            for off,old,new in zip((SHR_OFFSET,SHL_OFFSET),old_words,new_words) if extra] + [
            {'irx_file_offset':f'0x{TEXT_OFFSET + off:04X}', 'text_offset':f'0x{off:04X}',
             'old_word':f'0x{old:08X}', 'new_word':f'0x{new:08X}'}
            for off,old,new in zip(LENGTH_WORD_OFFSETS, ORIGINAL_LENGTH_WORDS, FIXED_LENGTH_WORDS) if needs_length_fix] + [
            {'irx_file_offset':f'0x{TEXT_OFFSET + off:04X}', 'text_offset':f'0x{off:04X}',
             'old_word':f'0x{old:08X}', 'new_word':f'0x{new:08X}'}
            for off,old,new in zip(POSITION_WORD_OFFSETS, ORIGINAL_POSITION_WORDS, FIXED_POSITION_WORDS) if needs_position_fix],
        'runtime_tested':False,
        'note':'This prevents signed 32-bit interpolation-product overflow in the identified routine. '
               'It does not establish complete runtime playback compatibility or resolve other untraced code.'
    }
    return bytes(result),report


def patch_file(source: Path, target: Path, sizes: list[int]) -> dict:
    """Stage, authenticate and atomically publish a standalone patched IRX.

    Never truncate/delete a pre-existing output if writing or verification fails.
    The input is never overwritten; no backup copies are created.
    """
    source = Path(source)
    target = Path(target)
    if target.resolve() == source.resolve():
        raise ValueError('Output IRX must differ from input; original is not modified')
    data, report = patch_cdstream(source.read_bytes(), sizes)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f'.{target.name}.', suffix='.building', dir=str(target.parent))
    staged = Path(temp_name)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if sha256(staged.read_bytes()) != report['output_sha256']:
            raise IOError('IRX staged output hash verification failed')
        os.replace(staged, target)
    finally:
        staged.unlink(missing_ok=True)
    return report
