#!/usr/bin/env python3
"""Read-only GTA Vice City Stories PS2 US ISO9660 filesystem validator.

No data is ever written to the input image. Generated report lives next to this
script, not next to the user's disc. This is a structural/static verification,
not a claim of authenticity or PCSX2 playback correctness.
"""
from __future__ import annotations
import argparse
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import struct
import sys
from datetime import datetime, timezone

BLOCK = 2048
US_EXEC = "SLUS_215.90"
HASHES = {
    "SLUS_215.90": "3b43be57d60a29df35e41504d67bbf728d5ce2fc5aab6bbf928cbbc5963fe547",
    "MODULES/CDSTREAM.IRX": "a72b95c6f0db77ba0363196b9d36ce1ac8585560a0730edc4125f29c12b71371",
    "MODULES/CDVDSTM.IRX": "276c175fe4a6d83d43baeb86b40cd5a7c3fc7b13b15df6da541040f926d5eb07",
}
STATIONS = ("FLASH", "VROCK", "PARADISE", "VCPR", "VCFL", "WAVE", "FRESH", "ESPANT", "EMOTION")

@dataclass
class Entry:
    path: str
    lba: int
    size: int
    flags: int
    record_offset: int
    extension_blocks: int
    @property
    def is_dir(self):
        return bool(self.flags & 2)
    @property
    def start(self):
        return (self.lba + self.extension_blocks) * BLOCK
    @property
    def end(self):
        return self.start + self.size


def both_u32(buf: bytes, p: int, name: str):
    le, be = struct.unpack_from('<I', buf, p)[0], struct.unpack_from('>I', buf, p+4)[0]
    if le != be:
        raise ValueError(f'{name}: mismatched little/big endian values: {le} versus {be}')
    return le


def record(buf: bytes, rec_off: int, parent: str):
    if len(buf) < 34 or buf[0] != len(buf):
        raise ValueError(f'Invalid ISO9660 directory record at ISO byte {rec_off}')
    namelen = buf[32]
    if namelen == 0 or 33+namelen > len(buf):
        raise ValueError(f'Bad file identifier at ISO byte {rec_off}')
    rawname = buf[33:33+namelen]
    if rawname in (b'\0', b'\1'):
        return None
    name = re.sub(r';[0-9]+$', '', rawname.decode('ascii', 'replace')).upper()
    lba = both_u32(buf, 2, f'{parent}/{name} extent')
    size = both_u32(buf, 10, f'{parent}/{name} length')
    return Entry((parent+'/'+name).lstrip('/'), lba, size, buf[25], rec_off, buf[1])


def hash_entry(f, entry: Entry) -> str:
    h = hashlib.sha256()
    f.seek(entry.start)
    remain = entry.size
    while remain:
        data = f.read(min(remain, 1024*1024))
        if not data:
            raise ValueError(f'Incomplete read while hashing {entry.path}')
        h.update(data)
        remain -= len(data)
    return h.hexdigest()


def inspect_iso(path: Path):
    findings, alerts = [], []
    filesize = path.stat().st_size
    result = {'input_file': str(path), 'physical_size_bytes': filesize, 'sector_aligned': filesize % BLOCK == 0,
              'notes': findings, 'anomalies': alerts, 'stations': {}, 'binary_revisions': {}, 'vcp_r_metadata': {},
              'checked_at_utc': datetime.now(timezone.utc).isoformat()}
    if filesize < 17*BLOCK:
        raise ValueError('File is too small for an ISO9660 primary descriptor')
    if filesize % BLOCK:
        alerts.append(f'ISO length {filesize:,} is not a multiple of {BLOCK} bytes')
    with path.open('rb') as f:
        pvd = None
        for i in range(16, min(64, filesize//BLOCK)):
            f.seek(i*BLOCK)
            desc = f.read(BLOCK)
            if desc[1:6] != b'CD001' or desc[6] != 1:
                continue
            if desc[0] == 1:
                pvd = desc
                result['primary_descriptor_lba'] = i
            elif desc[0] == 2:
                findings.append('Supplementary ISO9660 volume descriptor present; this scanner only traverses primary tree')
            elif desc[0] == 255:
                break
        if pvd is None:
            raise ValueError('No ISO9660 Primary Volume Descriptor found')
        nominal_sectors = both_u32(pvd, 80, 'Primary volume descriptor size')
        sector_size = struct.unpack_from('<H', pvd, 128)[0]
        sector_size_be = struct.unpack_from('>H', pvd, 130)[0]
        if sector_size != BLOCK or sector_size_be != BLOCK:
            raise ValueError(f'Unexpected ISO logical block size {sector_size}/{sector_size_be}')
        result['volume_id'] = pvd[40:72].decode('ascii', 'replace').strip('\x00 ')
        result['declared_volume_bytes'] = nominal_sectors * BLOCK
        if result['declared_volume_bytes'] != filesize:
            alerts.append('Declared volume size differs from physical ISO length: '
                          f'{result["declared_volume_bytes"]:,} vs {filesize:,} bytes')
        rootlen = pvd[156]
        if rootlen < 34:
            raise ValueError('Invalid root directory entry')
        root = pvd[156:156+rootlen]
        root_lba = both_u32(root, 2, 'Root LBA')
        root_size = both_u32(root, 10, 'Root directory size')
        root_entry = Entry('', root_lba, root_size, 2, result['primary_descriptor_lba']*BLOCK+156, root[1])
        seen = set()
        files, dirs = {}, {}
        def walk(ent, depth=0):
            if depth > 32:
                raise ValueError('Excessive ISO directory nesting')
            loc = (ent.start, ent.size)
            if loc in seen:
                return
            seen.add(loc)
            if ent.size > 64*1024*1024 or ent.end > filesize:
                raise ValueError(f'Directory outside image or unexpectedly large: {ent.path or "root"}')
            f.seek(ent.start)
            content = f.read(ent.size)
            if len(content) != ent.size:
                raise ValueError(f'Incomplete directory read: {ent.path}')
            cursor = 0
            children = []
            while cursor < len(content):
                reclen = content[cursor]
                if not reclen:
                    cursor = ((cursor//BLOCK)+1)*BLOCK
                    continue
                if reclen < 34 or cursor+reclen > len(content):
                    raise ValueError(f'Invalid directory record at {ent.path} offset {cursor}')
                child = record(content[cursor:cursor+reclen], ent.start+cursor, ent.path)
                cursor += reclen
                if child is None:
                    continue
                if child.is_dir:
                    dirs[child.path] = child
                    children.append(child)
                else:
                    if child.path in files:
                        findings.append(f'Duplicate ISO entry {child.path}; first record retained')
                    else:
                        files[child.path] = child
            for child in children:
                walk(child, depth+1)
        walk(root_entry)
        result['file_count'] = len(files)
        result['directory_count_excluding_root'] = len(dirs)
        spans = sorted([(e.start, e.end, e.path) for e in files.values() if e.size], key=lambda x:(x[0],x[1]))
        previous = None
        overlapping = []
        for start, end, name in spans:
            if end > filesize:
                alerts.append(f'{name} directory extent extends {end-filesize:,} bytes past physical ISO end')
            if previous is not None and start < previous[1]:
                overlap = min(end, previous[1]) - start
                overlapping.append({'earlier':previous[2], 'later':name, 'overlap_bytes':max(0, overlap)})
            if previous is None or end>previous[1]:
                previous = (start,end,name)
        result['overlaps'] = overlapping[:30]
        if overlapping:
            alerts.append(f'{len(overlapping)} overlapping ISO file extents detected (not automatically repaired)')
        for name in STATIONS:
            key = f'AUDIO/MUSIC/{name}.VB'
            if name=='WAVE' and key not in files and 'AUDIO/MUSIC/WAVE_EU.VB' in files:
                key='AUDIO/MUSIC/WAVE_EU.VB'
            if key in files:
                e=files[key]
                result['stations'][name]={'path':key,'declared_size':e.size,'lba':e.lba,'byte_offset':e.start,
                                          'end_byte':e.end,'aligned_16k':e.size%16384==0}
            else:
                alerts.append(f'Missing expected radio station: {name}.VB')
        vcpr=files.get('AUDIO/MUSIC/VCPR.VB')
        if vcpr:
            following = [(entry.start,entry.path) for entry in files.values() if entry.start>vcpr.start]
            next_start, next_name = min(following) if following else (None,None)
            result['vcp_r_metadata']={
                'declared_bytes':vcpr.size,'iso_directory_record_byte_offset':vcpr.record_offset,
                'length_little_endian_field_byte_offset':vcpr.record_offset+10,
                'length_big_endian_field_byte_offset':vcpr.record_offset+14,
                'file_start_byte':vcpr.start,'declared_end_byte':vcpr.end,
                'next_file_path':next_name,'next_file_start_byte':next_start,
                'space_before_next_file_bytes':(next_start-vcpr.start if next_start is not None else None),
                'overlap_after_next_file_start_bytes':(max(0,vcpr.end-next_start) if next_start is not None else None),
            }
            if next_start is not None and vcpr.end > next_start:
                alerts.append(f'VCPR.VB declares {vcpr.size:,} bytes, but next file {next_name} begins '
                              f'{next_start-vcpr.start:,} bytes after VCPR start')
            if vcpr.end == filesize:
                findings.append('VCPR declared extent ends at physical ISO EOF (investigate if other extents overlap)')
        else:
            alerts.append('VCPR.VB not found')
        system = files.get('SYSTEM.CNF')
        if system and system.size <= 1_048_576 and system.end <= filesize:
            f.seek(system.start)
            cfg = f.read(system.size).decode('ascii','replace')
            result['system_cnf_boot'] = re.search(r'BOOT2?\s*=\s*([^\r\n]+)', cfg, re.I).group(1).strip() if re.search(r'BOOT2?\s*=\s*([^\r\n]+)', cfg, re.I) else None
        for filename, expected in HASHES.items():
            ent = files.get(filename)
            if ent is None:
                alerts.append(f'Missing expected US binary: {filename}')
                continue
            if ent.end > filesize:
                alerts.append(f'Cannot hash {filename} because its extent extends beyond physical ISO')
                continue
            actual = hash_entry(f,ent)
            result['binary_revisions'][filename]={'sha256':actual, 'expected_sha256':expected,
                                                  'matches_original':actual==expected, 'bytes':ent.size}
            if actual != expected:
                findings.append(f'{filename} is NOT byte-identical to the authenticated original; '
                                'it may be another revision or an intentionally patched module')
        if any(p.startswith('SLES_') for p in files):
            alerts.append('PAL SLES executable found; this helper specifically targets the original US SLUS_215.90 revision')
        if not files.get(US_EXEC):
            alerts.append('US executable SLUS_215.90 missing')
    result['verdict']='ANOMALIES_REVIEW_REQUIRED' if alerts else 'STRUCTURAL_CHECKS_PASS'
    return result


def pretty_report(result):
    lines=['GTA Vice City Stories PS2 — US ISO read-only verification',
           f'ISO: {result["input_file"]}',
           f'Verdict: {result["verdict"]}',
           f'Volume: {result["volume_id"]} | files {result["file_count"]} | directories {result["directory_count_excluding_root"]}',
           f'Physical bytes: {result["physical_size_bytes"]:,} | declared volume bytes: {result["declared_volume_bytes"]:,}',
           '', 'BINARIES:']
    for name,data in result['binary_revisions'].items():
        status='ORIGINAL MATCH' if data['matches_original'] else 'DIFFERENT / POSSIBLY PATCHED'
        lines.append(f'  {name}: {status}; bytes={data["bytes"]:,}; SHA256={data["sha256"]}')
    lines.append('')
    lines.append('RADIO ENTRIES:')
    for name,data in result['stations'].items():
        lines.append(f'  {name:9s} {data["declared_size"]:>13,} bytes, LBA {data["lba"]}')
    v = result['vcp_r_metadata']
    if v:
        lines.extend(['', 'VCPR ISO9660 RECORD:',
          f'  Directory size (LE and BE): {v["declared_bytes"]:,} bytes',
          f'  Record byte offset: {v["iso_directory_record_byte_offset"]:,}',
          f'  LE size field: {v["length_little_endian_field_byte_offset"]:,}; BE size field: {v["length_big_endian_field_byte_offset"]:,}',
          f'  Gap to next file ({v["next_file_path"]}): {v["space_before_next_file_bytes"]:,} bytes' if v['next_file_path'] else '  No following file',
          f'  Extent beyond next file start: {v["overlap_after_next_file_start_bytes"]:,} bytes' if v['next_file_path'] else '',
        ])
    lines.append('')
    lines.append('ANOMALIES:')
    lines.extend('  - '+a for a in result['anomalies']) if result['anomalies'] else lines.append('  None detected')
    lines.append('NOTES:')
    lines.extend('  - '+a for a in result['notes']) if result['notes'] else lines.append('  None')
    lines.extend(['', 'This is static ISO9660 verification only. It does not prove retail authenticity, correct radio',
                  'decoder/streaming behaviour, or PCSX2 compatibility. Input ISO was not modified.'])
    return '\n'.join(lines)


def main(argv=None):
    parser=argparse.ArgumentParser(description='Verify VCS US PS2 ISO9660 and VCPR metadata without editing image')
    parser.add_argument('iso',type=Path)
    args=parser.parse_args(argv)
    iso=args.iso.expanduser().resolve()
    if not iso.is_file():
        print(f'ERROR: ISO does not exist: {iso}', file=sys.stderr)
        return 2
    try:
        data=inspect_iso(iso)
        report=pretty_report(data)
        code=0 if data['verdict']=='STRUCTURAL_CHECKS_PASS' else 1
    except (OSError,ValueError,struct.error) as exc:
        report=f'VCS ISO inspection FAILED: {type(exc).__name__}: {exc}\nInput image unmodified.'
        code=2
    print(report, flush=True)
    base=Path(__file__).resolve().parent
    for extension,content in (('.txt',report),('.json',json.dumps(data,indent=2) if code!=2 else json.dumps({'error':report},indent=2))):
        try:
            target=base/('VCS_ISO_Verification_Report'+extension)
            target.write_text(content+'\n',encoding='utf-8')
            print(f'Report saved: {target}')
        except OSError as exc:
            print(f'WARNING: Could not save {extension} report in helper folder: {exc}', file=sys.stderr)
    return code

if __name__=='__main__':
    sys.exit(main())
