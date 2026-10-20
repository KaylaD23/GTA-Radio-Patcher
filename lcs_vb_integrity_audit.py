#!/usr/bin/env python3
"""Read-only LCS PS2 .VB integrity audit. Does not repair or overwrite audio."""
from __future__ import annotations
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path

CHUNK = 0x4000

def analyze(path: Path, expected_size: int | None = None) -> dict:
    if not path.is_file():
        raise ValueError(f"Audio file does not exist: {path}")
    size = path.stat().st_size
    if not size:
        raise ValueError("The selected .VB is empty")
    sha = hashlib.sha256()
    block_first_occurrence = {}
    duplicate_blocks = 0
    zero_blocks = 0
    adjacent_duplicate_blocks = 0
    previous_hash = None
    first_block = None
    last_block = None
    i = 0
    with path.open('rb') as f:
        while (data := f.read(CHUNK)):
            sha.update(data)
            digest = hashlib.blake2b(data, digest_size=16).digest()
            if first_block is None:
                first_block = digest
            last_block = digest
            if digest in block_first_occurrence:
                duplicate_blocks += 1
            else:
                block_first_occurrence[digest] = i
            if previous_hash == digest:
                adjacent_duplicate_blocks += 1
            if not data.strip(b'\x00'):
                zero_blocks += 1
            previous_hash = digest
            i += 1
    durations = size * 7
    derived_ms = durations // 256 if size % CHUNK == 0 and durations % 256 == 0 else None
    warnings = []
    if size % CHUNK:
        warnings.append("Size is not a multiple of the expected 0x4000 interleave block. Check conversion/alignment.")
    if expected_size is not None and expected_size != size:
        warnings.append(f"Size differs from requested {expected_size:,} bytes; verify the exact GUI-selected source.")
    if duplicate_blocks:
        warnings.append("Byte-identical 16KiB blocks exist. They can be intentional silence; inspect if repeats match the in-game problem.")
    if adjacent_duplicate_blocks:
        warnings.append("Adjacent 16KiB blocks repeat byte-for-byte. Check conversion and padding if unexpected.")
    # This is a diagnostic threshold, NOT a proven LCS streaming limit.
    if size > 65536 * 2048:
        warnings.append("File exceeds 65,536 ISO sectors (128 MiB). A 16-bit sector-counter limit has NOT been demonstrated; diagnose large-seek behaviour in PCSX2.")
    return {
        'path':str(path.resolve()), 'size_bytes':size,'sha256':sha.hexdigest(),
        'interleave_aligned':size%CHUNK==0, 'derived_duration_ms_if_raw_32khz_stereo_adpcm':derived_ms,
        'derived_duration_hms': None if derived_ms is None else f'{derived_ms//3600000:02d}:{(derived_ms%3600000)//60000:02d}:{(derived_ms%60000)//1000:02d}.{derived_ms%1000:03d}',
        'blocks_16k':i,'duplicate_16k_blocks':duplicate_blocks,
        'adjacent_duplicate_16k_blocks':adjacent_duplicate_blocks, 'all_zero_16k_blocks':zero_blocks,
        'first_and_last_block_identical':first_block==last_block,
        'warnings':warnings,
        'note':'This is a read-only structural/duplicate-block check. A pass does not prove playable PSX ADPCM or correct game runtime length.'
    }

def main() -> int:
    ap = argparse.ArgumentParser(description='Inspect final LCS PS2 .VB audio, without editing it')
    ap.add_argument('vb_file',type=Path,nargs='?',help='Without this, open a file picker')
    ap.add_argument('--expected-size',type=int)
    ap.add_argument('--report',type=Path,help='Optional JSON output path')
    args=ap.parse_args()
    if args.vb_file is None:
        from tkinter import Tk, filedialog
        root=Tk();root.withdraw()
        name=filedialog.askopenfilename(title='Select final GTA LCS PS2 radio VB', filetypes=[('Radio audio','*.vb'),('All files','*.*')])
        root.destroy()
        if not name:
            print('Cancelled')
            return 1
        args.vb_file=Path(name)
    try:
        report=analyze(args.vb_file,args.expected_size)
        encoded=json.dumps(report,indent=2)
        print(encoded)
        if args.report:
            if args.report.resolve()==args.vb_file.resolve():
                raise ValueError('Report path must not overwrite the input VB file')
            args.report.write_text(encoded+'\n',encoding='utf-8')
        return 0
    except Exception as exc:
        print(f'Audit error: {exc}')
        return 2

if __name__=='__main__':
    raise SystemExit(main())
