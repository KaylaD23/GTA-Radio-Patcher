#!/usr/bin/env python3
"""Conservative GTA LCS PS2 VB / IOP-stream diagnostics.

Checks are deliberately distinct from emulator verification: the main goal is
rejecting mislabeled PCM/WAV/AT3/other files, detecting malformed PSX ADPCM,
and flagging ranges not exercised by the ten original radio stations.

Never modifies input bytes. No external packages required.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Any

CHANNEL_INTERLEAVE = 0x2000
STEREO_CYCLE = CHANNEL_INTERLEAVE * 2
SECTOR_SIZE = 2048
SAMPLE_RATE = 32000
DEFAULT_STATIONS = ('HEAD','DOUBLE','KJAH','RISE','LIPS','MUNDO','MSX','FLASH','LCJ','LCFR')
IOP_EXPECTED_NAMES = tuple(f'\\AUDIO\\MUSIC\\{name}.VB;1'.encode('ascii') for name in DEFAULT_STATIONS)
DISALLOWED_HEADERS = (b'RIFF',b'RIFX',b'OggS',b'fLaC',b'ID3',b'EA3',b'VAGp',b'PK\x03\x04')
ORIGINAL_MAX_RADIO_SIZE = 110247936  # LCFR.VB in the supplied reference game ZIP


def _check_adpcm_segment(buf: bytes) -> dict:
    if len(buf) % 16:
        raise ValueError('Sample buffer is not aligned to PSX ADPCM 16-byte frames')
    frame_count = len(buf) // 16
    bad_filter = bad_shift = bad_flag = zero_frames = 0
    for off in range(0, len(buf), 16):
        h, flags = buf[off], buf[off+1]
        bad_filter += (h >> 4) > 4
        bad_shift += (h & 0xF) > 12
        bad_flag += flags not in (0, 1, 2, 3, 4, 5, 6, 7)
        zero_frames += (buf[off:off+16] == b'\0'*16)
    return {'frames':frame_count,'invalid_predictors':bad_filter,'invalid_shifts':bad_shift,
            'invalid_flags':bad_flag,'zero_frames':zero_frames}


def audit_vb(path: str | Path, *, full_scan: bool = False) -> dict[str,Any]:
    path = Path(path)
    if not path.is_file():
        raise ValueError(f'Not an accessible stream file: {path}')
    size = path.stat().st_size
    if size <= 0 or size % STEREO_CYCLE:
        raise ValueError(f'{path.name}: {size:,} bytes: expected non-empty 0x4000-aligned raw stereo ADPCM')
    if size > 0xFFFFFFFF:
        raise ValueError(f'{path.name}: file size exceeds the ISO9660 single-extent 32-bit byte field')
    with path.open('rb') as f:
        magic = f.read(8)
    if magic.startswith(DISALLOWED_HEADERS):
        raise ValueError(f'{path.name}: a known non-raw container header was found ({magic!r})')
    from lcs_iop_safe_seek import MAX_SAFE_DURATION_SIZE
    if size > MAX_SAFE_DURATION_SIZE:
        raise ValueError(f'{path.name}: duration exceeds the configured 5-hour input limit')
    duration_ms = size * 7 // 256
    if not 0 < duration_ms <= 0xFFFFFFFF or size * 7 % 256:
        raise ValueError(f'{path.name}: duration is outside the supported 32-bit millisecond range')
    total = dict(frames=0,invalid_predictors=0,invalid_shifts=0,invalid_flags=0,zero_frames=0)
    offsets: list[int] = []
    scan_window = 0x40000  # 256 KiB per sample; channel-aligned
    if full_scan:
        offsets = list(range(0, size, scan_window))
    else:
        last = max(0, size - scan_window)
        offsets = sorted(set(((min((size * i) // 8, last) // STEREO_CYCLE) * STEREO_CYCLE)
                             for i in range(9)))
    with path.open('rb') as f:
        for offset in offsets:
            f.seek(offset)
            data = f.read(min(scan_window, size-offset))
            data = data[:len(data) // 16 * 16]
            sample = _check_adpcm_segment(data)
            for k,v in sample.items(): total[k] += v
    if total['invalid_predictors'] or total['invalid_shifts'] or total['invalid_flags']:
        raise ValueError(f'{path.name}: invalid PSX ADPCM frames: {total}')
    sectors = (size + SECTOR_SIZE-1) // SECTOR_SIZE
    warnings=[]
    if sectors > 65535:
        warnings.append('UNVERIFIED_LARGE_SECTOR_RANGE: stream exceeds 65,535 2048-byte sectors (128 MiB). Confirm that all IOP/EE position paths preserve full 32-bit addresses; this is a risk flag, not proof of a 16-bit bug.')
    if size > ORIGINAL_MAX_RADIO_SIZE:
        warnings.append('LONGER_THAN_LARGEST_REFERENCE_RADIO: stream length exceeds every original main station; range not covered by baseline game audio.')
    if total['zero_frames'] > total['frames'] * 0.90:
        warnings.append('PREDOMINANTLY_ZERO: stream contains >90% zero frames in sampled regions; verify it is actual audio and not a placeholder.')
    return {'file':str(path),'size_bytes':size,'duration_ms_hypothesis':duration_ms,
            'estimated_duration_seconds':duration_ms/1000,'sample_rate_assumed_hz':SAMPLE_RATE,
            'stereo_channel_interleave_bytes':CHANNEL_INTERLEAVE,'stream_sectors':sectors,
            'sectors_over_16bit_boundary':sectors>65535,
            'frame_scan':'full' if full_scan else 'distributed 256-KiB windows',
            'scanned_frame_counts':total,'warnings':warnings,
            'validation':'FORMAT_STRUCTURE_PASSED_NOT_PLAYBACK_VERIFIED'}


def audit_cdstream_module(data: bytes) -> dict[str,Any]:
    if not data.startswith(b'\x7fELF'):
        raise ValueError('CDSTREAM.IRX is not an ELF/IRX module')
    names = [n.decode('ascii') for n in IOP_EXPECTED_NAMES]
    found = [n in data for n in IOP_EXPECTED_NAMES]
    if not all(found):
        missing=[n for n,ok in zip(names,found) if not ok]
        raise ValueError(f'CDSTREAM.IRX lacks required radio filename strings: {missing}')
    offsets=[data.find(n) for n in IOP_EXPECTED_NAMES]
    if offsets != sorted(offsets):
        raise ValueError('CDSTREAM.IRX radio filename ordering differs from expected station index layout')
    return {'size_bytes':len(data),'sha256':hashlib.sha256(data).hexdigest(),
            'radio_filename_table_order':list(DEFAULT_STATIONS),
            'filename_offsets_in_irx':[hex(i) for i in offsets],
            'filenames_present':True,'confidence':'filename table/ELF signature only, no IOP seek execution verified'}


def main() -> int:
    p=argparse.ArgumentParser(description='Inspect LCS PS2 radio VB ADPCM and CDSTREAM IRX inputs (read-only)')
    p.add_argument('files',nargs='*',type=Path,help='One or more *.VB streams')
    p.add_argument('--cdstream',type=Path,help='Optional CDSTREAM.IRX for radio filename table verification')
    p.add_argument('--full',action='store_true',help='Scan every ADPCM frame, not just distributed windows')
    p.add_argument('--output',type=Path,help='Optional JSON report path')
    a=p.parse_args(); data={'format_version':1,'results':[]}
    try:
        for f in a.files: data['results'].append(audit_vb(f,full_scan=a.full))
        if a.cdstream:data['cdstream']=audit_cdstream_module(a.cdstream.read_bytes())
    except (OSError,ValueError) as e:
        print('AUDIT FAILED:',e);return 1
    s=json.dumps(data,indent=2)+'\n'
    if a.output:a.output.write_text(s,encoding='utf-8')
    print(s)
    return 0

if __name__ == '__main__':raise SystemExit(main())
