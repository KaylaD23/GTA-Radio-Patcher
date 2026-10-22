#!/usr/bin/env python3
"""VCS PS2 legacy math-only two-DIVU EE conversion functions.

WARNING: patched_bytes/patch_file retain the older unsafe negative-position
behavior for reproducible regression/compatibility. The GUI and command-line
entry point now use vcs_ee_guarded_position instead.


Exact-release-only patch. The source is never overwritten. The two 32-bit
signed DIV operations at the verified 0x90000 and 0x90100 conversion sites
become unsigned DIVU; the 32-bit shift remains. Consequently this fixes
positive time operands in [0, 0xFFFFFF] milliseconds ONLY. It does NOT
extend the 32-bit intermediate beyond 0xFFFFFFFF, repair CDSTREAM.IRX or
rebuild a playable ISO. Unsupported sizes and binaries fail closed.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import struct

ORIGINAL_SHA256 = '3b43be57d60a29df35e41504d67bbf728d5ce2fc5aab6bbf928cbbc5963fe547'
ELF_FILE_SIZE = 3993368
# Original MIPS III DIV (32-bit signed) -> DIVU (32-bit unsigned).
# Both use same source and destination registers; no branch addresses change.
PATCHES = ((0xF8A8, 0x0043001A, 0x0043001B, 'station_length_divu', 0x0010E8A8),
           (0xF75C, 0x0043001A, 0x0043001B, 'playback_position_divu', 0x0010E75C))
# These instructions provide the 8-bit multiply and nonzero divisor.
GUARDS = ((0xF8A0, 0x00031200), (0xF8A4, 0x24030007),
          (0xF754, 0x00101200), (0xF758, 0x24030007))
MAX_TIME_MS = (1 << 24) - 1
MAX_CONVERTED_POSITION = (MAX_TIME_MS << 8) // 7
MAX_DURATION_BYTES = ((1 << 32) - 1) // 7
# Audio is 32 kHz stereo in 0x2000-byte channel blocks (0x4000 bytes per pair).
# Each pair represents 448 ms of *allocated* PS2 ADPCM at this rate.
VCS_CHANNEL_PAIR_BYTES = 0x4000
TARGET_MIN_SOURCE_MS = 1_000
TARGET_MAX_SOURCE_MS = 4 * 3_600_000 + 39 * 60_000
# The largest available 16-KiB block pair meeting BOTH signed-to-unsigned EE
# and unchanged IOP size*7 intermediate constraints, approximately 4:39:37.
MAX_ALIGNED_SAFE_BYTES = min(MAX_DURATION_BYTES, MAX_CONVERTED_POSITION) // VCS_CHANNEL_PAIR_BYTES * VCS_CHANNEL_PAIR_BYTES
MAX_ALIGNED_SAFE_MS = (MAX_ALIGNED_SAFE_BYTES * 7) // 256
# At least this much container audio is needed to represent a one-second source.
MIN_ALIGNED_1S_BYTES = ((TARGET_MIN_SOURCE_MS * 256 + 6) // 7 + VCS_CHANNEL_PAIR_BYTES - 1) // VCS_CHANNEL_PAIR_BYTES * VCS_CHANNEL_PAIR_BYTES


def aligned_bytes_for_source_duration(duration_ms: int) -> int:
    """Find smallest full stereo block-pair allocation for a source duration.

    The raw .VB has no explicit sample count, so a one-second source occupies
    1.344 seconds of allocation; the encoder must pad channel block tails.
    This does NOT encode audio or prove that its EOF playback is gapless.
    """
    if type(duration_ms) is not int or not TARGET_MIN_SOURCE_MS <= duration_ms <= TARGET_MAX_SOURCE_MS:
        raise ValueError('VCS target source duration must be between 1,000 and 16,740,000 ms (1 sec to 4h39m)')
    theoretical_bytes = (duration_ms * 256 + 6) // 7
    result = (theoretical_bytes + VCS_CHANNEL_PAIR_BYTES - 1) // VCS_CHANNEL_PAIR_BYTES * VCS_CHANNEL_PAIR_BYTES
    if result > MAX_ALIGNED_SAFE_BYTES:
        raise ValueError('VCS source duration exceeds safe 32-bit aligned allocation')
    return result


class UnsupportedBinaryError(ValueError):
    pass


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _word(data: bytes, offset: int) -> int:
    return struct.unpack_from('<I', data, offset)[0]


def _validate_layout(data: bytes) -> None:
    for off, expect in GUARDS:
        if _word(data, off) != expect:
            raise UnsupportedBinaryError(f'VCS instruction guard mismatch at file +0x{off:X}')


def check_pristine(data: bytes) -> None:
    if len(data) != ELF_FILE_SIZE or sha256(data) != ORIGINAL_SHA256:
        raise UnsupportedBinaryError('Executable is not the original exact supported VCS SLUS_215.90 revision')
    _validate_layout(data)
    for off, orig, new, name, _ in PATCHES:
        if _word(data, off) != orig:
            raise UnsupportedBinaryError(f'{name}: unexpected original opcode at +0x{off:X}')


def patched_bytes(data: bytes) -> bytes:
    check_pristine(data)
    out = bytearray(data)
    for off, orig, new, name, _ in PATCHES:
        struct.pack_into('<I', out, off, new)
    verify_patch(data, bytes(out))
    return bytes(out)


def verify_patch(original: bytes, patched: bytes) -> None:
    check_pristine(original)
    if len(patched) != len(original):
        raise UnsupportedBinaryError('Output ELF changed size')
    _validate_layout(patched)
    desired = bytearray(original)
    for off, orig, new, _, _ in PATCHES:
        struct.pack_into('<I', desired, off, new)
        if _word(patched, off) != new:
            raise UnsupportedBinaryError('DIVU replacement opcode missing')
    if patched != bytes(desired):
        raise UnsupportedBinaryError('Output differs at unexpected offsets')


def original_ee_conversion(time_ms: int) -> int:
    """32-bit SLL and signed DIV by 7; positive time input only."""
    if not 0 <= time_ms <= 0x7FFFFFFF:
        raise ValueError('Modeled time must be a nonnegative signed 32-bit integer')
    raw = (time_ms << 8) & 0xFFFFFFFF
    signed = raw if raw < 0x80000000 else raw - 0x100000000
    return signed // 7 if signed >= 0 else -((-signed) // 7)


def repaired_ee_conversion(time_ms: int) -> int:
    """DIVU of low 32-bit SLL result; only valid for 24-bit times."""
    if not 0 <= time_ms <= MAX_TIME_MS:
        raise ValueError('This fix only supports nonnegative time operands up to 0xFFFFFF ms')
    return ((time_ms << 8) & 0xFFFFFFFF) // 7


def validate_radio_sizes(sizes: list[int]) -> list[dict]:
    """Enforce the *whole* positive length conversion range.

    Independent streaming-module size*7 overflow occurs beyond 613,566,756
    bytes; the EE shift wraps at a slightly smaller time-model threshold.
    """
    result = []
    for size in sizes:
        if type(size) is not int or size < MIN_ALIGNED_1S_BYTES or size % VCS_CHANNEL_PAIR_BYTES:
            raise ValueError(f'VCS replacement VB must be >= {MIN_ALIGNED_1S_BYTES:,} bytes '
                             f'(1-second source), 16 KiB block-pair aligned: {size!r}')
        if size > MAX_ALIGNED_SAFE_BYTES:
            raise ValueError(f'VB {size:,} exceeds the verified VCS 32-bit arithmetic + 16 KiB allocation ceiling '
                             f'{MAX_ALIGNED_SAFE_BYTES:,} bytes ({MAX_ALIGNED_SAFE_MS:,} ms)')
        time_ms = size * 7 // 256
        if time_ms > MAX_TIME_MS:
            raise ValueError(f'VB {size:,} has time {time_ms:,} ms beyond this conservative EE DIVU fix')
        converted = repaired_ee_conversion(time_ms)
        # For 16 KiB aligned stock streams the math is exact.
        if converted != size:
            raise ValueError(f'VB duration roundtrip mismatch: {size:,} vs {converted:,}')
        result.append(dict(size_bytes=size, duration_ms=time_ms,
                           original_ee_position=original_ee_conversion(time_ms),
                           patched_ee_position=converted, prior_signed_overflow=time_ms >= 0x800000))
    return result


def patch_file(original_path: Path, output_path: Path, checked_vb_sizes: list[int] | None = None) -> dict:
    """Atomic no-overwrite output; do not create backup or modify original."""
    source = Path(original_path)
    output = Path(output_path)
    if not source.is_file():
        raise FileNotFoundError(source)
    if source.resolve() == output.resolve():
        raise ValueError('Input and output must be different paths')
    if output.exists():
        raise FileExistsError(f'Output exists, refusing to overwrite: {output}')
    sizes = checked_vb_sizes if checked_vb_sizes is not None else []
    analyses = validate_radio_sizes(sizes)
    pristine = source.read_bytes()
    patched = patched_bytes(pristine)
    output.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive create guarantees no accidental output replacement; no .bak.
    created = False
    try:
        with output.open('xb') as dst:
            created = True
            dst.write(patched)
            dst.flush()
            os.fsync(dst.fileno())
    except BaseException:
        # A pre-existing destination never belongs to this operation.
        if created:
            output.unlink(missing_ok=True)
        raise
    result = {'game':'GTA Vice City Stories PS2', 'revision':'SLUS_215.90',
              'original_sha256':ORIGINAL_SHA256,'patched_sha256':sha256(patched),
              'output_file':str(output), 'output_verified':True,
              'patches':[{'name':name,'executable_file_offset':f'0x{off:X}',
                          'runtime_address':f'0x{va:08X}',
                          'original_opcode':f'0x{orig:08X}', 'patched_opcode':f'0x{new:08X}'}
                         for off,orig,new,name,va in PATCHES],
              'checked_replacements':analyses,
              'vcs_target_source_duration_ms':[TARGET_MIN_SOURCE_MS,TARGET_MAX_SOURCE_MS],
              'max_supported_aligned_bytes':MAX_ALIGNED_SAFE_BYTES,
              'max_supported_aligned_duration_ms':MAX_ALIGNED_SAFE_MS,
              'minimum_one_second_source_allocation_bytes':MIN_ALIGNED_1S_BYTES,
              'limitations':['VCS 1s–4h39m source target; packed VB allocated duration is rounded to 448-ms 16-KiB pairs',
                             'Positive time operands only; EE 24-bit intermediate and IOP 32-bit multiply ceilings',
                             'VCS IOP signed-position arithmetic not patched',
                             'VCS seek interpolation not validated',
                             'VCPR ISO directory anomaly not fixed; full ISO not rebuilt',
                             'No gameplay or PCSX2 verification']}
    return result


def main() -> int:
    p = argparse.ArgumentParser(description='Exact-revision VCS PS2 EE overflow correction (not an ISO patcher)')
    p.add_argument('--input',type=Path,required=True,help='Original SLUS_215.90 executable')
    p.add_argument('--output',type=Path,required=True,help='New destination, must not exist')
    p.add_argument('--vb',type=Path,action='append',default=[],help='Replacement .VB to validate (can repeat)')
    p.add_argument('--report',type=Path,help='Optional JSON diagnostics (must not exist)')
    args=p.parse_args()
    # stat only; avoid loading whole radio into memory
    if args.report:
        if args.report.resolve() in (args.input.resolve(), args.output.resolve()):
            p.error('JSON report must differ from source and output executable')
        if args.report.exists():
            p.error('JSON report exists; refusing to overwrite')
    sizes=[f.stat().st_size for f in args.vb]
    from vcs_ee_guarded_position import patch_file as guarded_patch_file
    result=guarded_patch_file(args.input,args.output,
                              {f'vb_{i}':size for i,size in enumerate(sizes)})
    encoded=json.dumps(result,indent=2)
    if args.report:
        with args.report.open('x',encoding='utf-8') as f:
            f.write(encoded+'\n')
    print(encoded)
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
