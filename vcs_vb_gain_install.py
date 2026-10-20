"""VCS PS2 optional +3 dB VCPR.VB re-encode and audio-only ISO installation.

No EE, IOP or adaptive-offset opcode editing. It explicitly preserves all
existing executable and streaming module bytes, so it requires that the input
ISO already contains whichever verified radio playback fixes the user needs.
Uses only the ISO9660 filesystem *format* writer shared with LCS.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import Callable, Optional

from lcs_duration_aware_iso_patcher import Iso9660Image, apply_iso_replacements, BLOCK, sha256_file

GAIN_DB = 3.0
EXPECTED_BOOT = 'SLUS_215.90'
ISO_ENTRY = 'AUDIO/MUSIC/VCPR.VB'
PAIRS = 0x4000
SOURCE = Path(__file__).with_name('vcs_adpcm_gain.cpp')
Progress = Optional[Callable[[str], None]]


def compile_gain_engine(progress: Progress = None) -> Path:
    """Build portable C++17 gain encoder on the *user's* platform; never ship Linux binary on Windows."""
    digest = hashlib.sha256(SOURCE.read_bytes()).hexdigest()[:20]
    cache = Path(tempfile.gettempdir()) / f'gta_vcs_adpcm_gain_{digest}'
    binary = cache / ('vcs_adpcm_gain.exe' if os.name == 'nt' else 'vcs_adpcm_gain')
    if binary.is_file():
        return binary
    cache.mkdir(parents=True, exist_ok=True)
    compilers = ('g++', 'clang++', 'cl') if os.name == 'nt' else ('g++', 'clang++')
    results = []
    for name in compilers:
        compiler = shutil.which(name)
        if not compiler:
            continue
        if progress:
            progress(f'Compiling VCS PSX ADPCM +3 dB encoder with {name}…')
        if name == 'cl':
            args = [compiler, '/nologo', '/O2', '/EHsc', '/std:c++17', str(SOURCE), '/Fe:' + str(binary)]
        else:
            args = [compiler, '-O3', '-std=c++17', '-Wall', '-Wextra', str(SOURCE), '-o', str(binary)]
        result = subprocess.run(args, cwd=cache, capture_output=True, text=True)
        if result.returncode == 0 and binary.is_file():
            return binary
        results.append(f'{name}: {(result.stderr or result.stdout)[-700:]}')
    raise RuntimeError('VCS +3 dB requires a C++17 compiler (MSVC cl, MinGW g++ or clang++). '
                       'Run from a Visual Studio Developer Command Prompt or add a compiler to PATH. '
                       + '\n'.join(results))


def validate_vb(path: Path):
    if not path.is_file():
        raise FileNotFoundError(f'VCPR input not found: {path}')
    size = path.stat().st_size
    if size == 0 or size % PAIRS:
        raise ValueError(f'VCS stereo VCPR.VB requires a nonempty file aligned to 0x4000 bytes; got {size:,}')
    # Spot-check both channel blocks at multiple locations for PSX ADPCM framing.
    samples = [0, 0x2000, size//2 - (size//2)%16, size-16, max(0,size-0x2000)]
    with path.open('rb') as f:
        for pos in samples:
            f.seek(pos)
            frame = f.read(16)
            if len(frame) != 16 or frame[0]>>4 > 4 or (frame[0]&15)>12:
                raise ValueError(f'VCPR.VB has invalid PSX ADPCM frame header at byte offset 0x{pos:x}')
    return size


def check_vcs_iso(input_iso: Path):
    """Reject earlier malformed US VCPR record and any file extent collisions."""
    original = Iso9660Image(input_iso)
    if original.volume_sectors * BLOCK != original.size:
        raise ValueError('VCS input ISO size differs from declared ISO9660 volume length; '
                         'supply a structurally consistent original/previously patched ISO')
    boot = original.find_boot_executable()
    if boot.upper() != EXPECTED_BOOT:
        raise ValueError(f'Expected US VCS {EXPECTED_BOOT}, found boot executable {boot}')
    original.get('MODULES/CDSTREAM.IRX')
    original.get(ISO_ENTRY)
    used=[]
    if original.root_entry is not None:
        used.append(original.root_entry)
    used.extend(original.entries.values())
    extents = []
    for entry in used:
        start = entry.data_lba * BLOCK
        end = start + entry.size
        if end>original.volume_sectors*BLOCK:
            raise ValueError(f'ISO record {entry.path} extends outside declared volume')
        if entry.size and not entry.is_directory:
            extents.append((start,end,entry.path))
    extents.sort()
    for (_,last_end,last_path),(next_start,_,next_path) in zip(extents, extents[1:]):
        if last_end > next_start:
            raise ValueError(f'Overlapping ISO file records: {last_path} / {next_path}. '
                             'The earlier NTSC VCPR anomaly must not be silently normalized.')
    return original


def build_vcpr_audio_iso(*, input_iso: Path, replacement_vb: Path,
                         output_iso: Path, boost_vcpr_3db: bool = False,
                         progress: Progress = None, report_path: Path | None = None) -> dict:
    """Copy input US ISO; replace VCPR only, optionally re-encode +3 dB first.

    Prevents overwriting either original image or an existing output; does not
    create backups or claim to patch long-radio executable/IRX boundaries.
    """
    input_iso, replacement_vb, output_iso = map(Path,(input_iso,replacement_vb,output_iso))
    for source in (input_iso,replacement_vb):
        if not source.is_file():
            raise ValueError(f'Missing input: {source}')
    files = [input_iso,replacement_vb,output_iso]
    if len({p.resolve() for p in files})!=3:
        raise ValueError('Input ISO, input VB and output ISO must be different files')
    if output_iso.exists():
        raise FileExistsError(f'Output ISO exists (overwrite disabled): {output_iso}')
    if report_path is not None:
        report_path = Path(report_path)
        if report_path.exists() or report_path.resolve() in {p.resolve() for p in files}:
            raise ValueError('Report exists or conflicts with another file; overwrite disabled')
    if progress: progress('Inspecting VCS ISO for invalid/overlapping directory records…')
    original = check_vcs_iso(input_iso)
    size = validate_vb(replacement_vb)
    from vcs_ee_overflow_patch import validate_radio_sizes
    validate_radio_sizes([size])
    gain_report = None
    with tempfile.TemporaryDirectory(prefix='gta_vcs_vcpr_') as temp:
        source = replacement_vb
        if boost_vcpr_3db:
            engine = compile_gain_engine(progress)
            source = Path(temp) / 'VCPR.plus3dB.VB'
            if progress: progress('Decoding, amplifying and re-encoding VCPR (+3.0 dB) — stereo PSX ADPCM…')
            completed = subprocess.run([str(engine),str(replacement_vb),str(source)],
                                       capture_output=True,text=True)
            if completed.returncode:
                raise RuntimeError('VCS gain encoding failed: '+completed.stderr[-2500:])
            gain_report = json.loads(completed.stdout)
            if source.stat().st_size != size:
                raise IOError('Boosted VCPR changed size; refusing to build ISO')
            if progress:
                progress(f'Gain encoder completed: measured {gain_report["measured_decoded_rms_gain_db"]:.3f} dB; '
                         f'{gain_report["target_clipped_samples"]:,} clipped target samples')
        unchanged_executable_sha = hashlib.sha256(original.read_entry_bytes(EXPECTED_BOOT)).hexdigest()
        unchanged_irx_sha = hashlib.sha256(original.read_entry_bytes('MODULES/CDSTREAM.IRX')).hexdigest()
        if progress: progress(f'Installing {ISO_ENTRY} in new ISO, preserving existing EE/IOP/offset patches…')
        def final_checks(image):
            if hashlib.sha256(image.read_entry_bytes(EXPECTED_BOOT)).hexdigest()!=unchanged_executable_sha:
                raise IOError('VCS executable unexpectedly changed')
            if hashlib.sha256(image.read_entry_bytes('MODULES/CDSTREAM.IRX')).hexdigest()!=unchanged_irx_sha:
                raise IOError('VCS CDSTREAM.IRX unexpectedly changed')
            if image.get(ISO_ENTRY).size!=size:
                raise IOError('VCPR length not correctly installed')
        installed = apply_iso_replacements(input_iso,output_iso,{ISO_ENTRY:source},progress,final_checks)
    result = {'profile':'vcs_ps2','purpose':'VCPR audio-only ISO installation',
              'input_iso':str(input_iso),'output_iso':str(output_iso),
              'iso_target':ISO_ENTRY,'vb_input_bytes':size,
              'boost_vcpr_3db':bool(boost_vcpr_3db),'gain':gain_report,
              'installed':installed,'executable_preserved_sha256':unchanged_executable_sha,
              'cdstream_preserved_sha256':unchanged_irx_sha,
              'gameplay_verified':False,
              'notice':'Does not create or install any new EE/IOP/random offset corrections. Use an ISO with existing working patches.'}
    if report_path is not None:
        with report_path.open('x',encoding='utf-8') as f:
            json.dump(result,f,indent=2)
    return result


def main():
    import argparse
    parser=argparse.ArgumentParser(description='Install one VCPR.VB in a NEW VCS US PS2 ISO; optional +3dB re-encode')
    parser.add_argument('--input-iso',required=True,type=Path)
    parser.add_argument('--vcpr-vb',required=True,type=Path)
    parser.add_argument('--output-iso',required=True,type=Path)
    parser.add_argument('--boost-3db',action='store_true')
    parser.add_argument('--report',type=Path)
    ns=parser.parse_args()
    result=build_vcpr_audio_iso(input_iso=ns.input_iso,replacement_vb=ns.vcpr_vb,
        output_iso=ns.output_iso,boost_vcpr_3db=ns.boost_3db,report_path=ns.report,
        progress=lambda msg:print(msg,file=sys.stderr,flush=True))
    print(json.dumps(result,indent=2))

if __name__=='__main__':
    main()
