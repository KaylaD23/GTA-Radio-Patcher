#!/usr/bin/env python3
"""LCS PS2 radio streaming minimum length guard.

CDSTREAM.IRX 0x17DC..0x1AE8 streams 0x40000-byte chunks.  At EOF its
wraparound path requests the missing tail of that chunk from the file start.
A file shorter than a chunk can result in a second read longer than the
entire file. Extend only tiny replacements with valid silent stereo ADPCM
interleave blocks; never alter the user's original audio.
"""
from __future__ import annotations
import shutil
from pathlib import Path

READ_CHUNK_BYTES = 0x40000
INTERLEAVE_BYTES = 0x4000
SILENT_ADPCM_FRAME = bytes.fromhex('0c00') + bytes(14)
SILENT_STEREO_BLOCK = SILENT_ADPCM_FRAME * (INTERLEAVE_BYTES // 16)

def installed_size(source_size: int) -> int:
    if source_size <= 0 or source_size % INTERLEAVE_BYTES:
        raise ValueError('Radio audio must be a nonempty multiple of 0x4000 bytes')
    return max(source_size, READ_CHUNK_BYTES)

def stage_audio_with_short_stream_guard(src: Path, dst: Path) -> dict:
    src = Path(src)
    dst = Path(dst)
    size = src.stat().st_size
    padded = installed_size(size)
    if src.resolve() == dst.resolve():
        raise ValueError('Staged file cannot overwrite source audio')
    if padded == size:
        raise ValueError('Use original file directly when padding is unnecessary')
    dst.parent.mkdir(parents=True, exist_ok=True)
    created_here = False
    try:
        # Never remove an existing file when exclusive destination creation fails.
        # Opening the source first also avoids leaving a destination behind if
        # source access fails.
        with src.open('rb') as fin:
            with dst.open('xb') as fout:
                created_here = True
                shutil.copyfileobj(fin, fout, 1 << 20)
                for _ in range((padded - size) // INTERLEAVE_BYTES):
                    fout.write(SILENT_STEREO_BLOCK)
        if dst.stat().st_size != padded:
            raise IOError('Staged audio padding size mismatch')
    except BaseException:
        if created_here:
            dst.unlink(missing_ok=True)
        raise
    return {
        'source_size_bytes': size,
        'installed_size_bytes': padded,
        'silent_padding_bytes': padded-size,
        'read_chunk_minimum_bytes': READ_CHUNK_BYTES,
    }
