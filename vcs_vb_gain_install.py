"""VCS PS2 ISO installer: signed-safe EE and signed-position IOP updates.

Exact supported US executable and IOP revisions ONLY. A new ISO is built with
verified EE and CDSTREAM corrections, optional adaptive offsets and audio gain.
Unknown existing executable/module modifications fail closed; originals are
never overwritten. New EE PT_LOAD/IOP module loading is unverified in PCSX2.
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

from gta_radio_profiles import get_profile
from radio_vb_gain import compile_gain_engine, validate_gain_source as validate_vb, boost_vb, validate_gain_db

EXPECTED_BOOT = 'SLUS_215.90'
Progress = Optional[Callable[[str], None]]


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


def _get_replacements(source: Path, station: str | None = None) -> dict[str, Path]:
    """A named .VB or a folder of named station files; no silent guessing."""
    stations = {filename.upper() for _, filename in get_profile('vcs_ps2').stations}
    source = Path(source)
    if source.is_file():
        if source.suffix.upper() != '.VB':
            raise ValueError('Single radio replacement must be a .VB file')
        target = ((station or source.name).upper().removesuffix('.VB') + '.VB')
        if target not in stations:
            raise ValueError('Specify a VCS station for this VB: ' + ', '.join(sorted(stations)))
        return {target: source}
    if not source.is_dir():
        raise ValueError('Replacement source must be a .VB file or a folder of .VB files')
    if station is not None:
        raise ValueError('Station override is supported for a single file only; folder files must have station names')
    result: dict[str, Path] = {}
    for candidate in source.iterdir():
        if not candidate.is_file() or candidate.suffix.upper() != '.VB':
            continue
        filename = candidate.name.upper()
        if filename not in stations:
            raise ValueError(f'Unknown VCS radio replacement {candidate.name}; expected one of: {sorted(stations)}')
        if filename in result:
            raise ValueError(f'Duplicate case-insensitive station filename: {filename}')
        result[filename] = candidate
    if not result:
        raise ValueError('No supported VCS radio .VB files found')
    return result


def build_radio_audio_iso(*, input_iso: Path, replacement_vb: Path,
                          output_iso: Path, boost_radio_3db: bool = False,
                          gain_db: float = 3.0,
                          station: str | None = None,
                          expand_vcs_offsets: bool = False,
                          offset_mode: str | None = None,
                          progress: Progress = None, report_path: Path | None = None) -> dict:
    """Create a NEW ISO, auto-adding signed-safe EE and known VCS IOP fixes.

    Reject unknown executable/streaming module modifications, including older
    custom seek changes that cannot be proven equivalent. The original ISO is
    never changed; the extra EE code segment still needs PCSX2 validation.
    """
    if boost_radio_3db:
        gain_db = validate_gain_db(gain_db)
    # Explicit mode comes from the GUI's shared offset-mode dropdown. Preserve
    # the old boolean for scripts/tests that use the previous experimental flag.
    if offset_mode is None:
        offset_mode = 'vcs-shared' if expand_vcs_offsets else 'vcs-stock'
    if offset_mode not in ('vcs-stock', 'vcs-shared'):
        raise ValueError(f'Unsupported VCS offset mode: {offset_mode!r}')
    if expand_vcs_offsets and offset_mode != 'vcs-shared':
        raise ValueError('Contradictory VCS offset mode arguments')
    expand_vcs_offsets = offset_mode == 'vcs-shared'
    input_iso, replacement_vb, output_iso = map(Path, (input_iso, replacement_vb, output_iso))
    files = [input_iso, replacement_vb, output_iso]
    if len({p.resolve() for p in files}) != 3:
        raise ValueError('Input ISO, VB source and output ISO must be different paths')
    if not input_iso.is_file():
        raise ValueError(f'Missing input ISO: {input_iso}')
    if output_iso.exists():
        raise FileExistsError(f'Output ISO exists (overwrite disabled): {output_iso}')
    if report_path is not None:
        report_path = Path(report_path)
        if report_path.exists() or report_path.resolve() in {p.resolve() for p in files}:
            raise ValueError('Report already exists or conflicts with source/output; overwrite disabled')
    replacements = _get_replacements(replacement_vb, station)
    if progress:
        progress('Inspecting VCS ISO9660 file records, extents and volume length…')
    original = check_vcs_iso(input_iso)
    from vcs_ee_overflow_patch import validate_radio_sizes
    if progress:
        progress(f'VCS detected {len(replacements)} replacement radio(s): '
                 + ', '.join(sorted(replacements))
                 + (f'; applying optional +{gain_db:g} dB to each.' if boost_radio_3db else '; gain disabled.'))
    sizes = {name: validate_vb(path) for name, path in replacements.items()}
    validate_radio_sizes(list(sizes.values()))
    destinations = {'AUDIO/MUSIC/' + name: path for name, path in replacements.items()}
    for iso_path in destinations:
        original.get(iso_path)  # fail before transcoding if the ISO lacks the destination
    original_exe_bytes = original.read_entry_bytes(EXPECTED_BOOT)
    original_exe_sha = hashlib.sha256(original_exe_bytes).hexdigest()
    from vcs_ee_guarded_position import build_guarded_executable
    from vcs_cdstream_signed_position_patch import build as build_iop, EXPECTED_SHA as IRX_ORIGINAL_SHA
    from gta_radio_profiles import get_profile
    original_irx_bytes = original.read_entry_bytes('MODULES/CDSTREAM.IRX')
    original_irx_sha = hashlib.sha256(original_irx_bytes).hexdigest()
    # These hashes are exact revision locks, not fuzzy opcode-pattern matches.
    KNOWN_PATCHED_IRX_SHA = 'a29d2badb270e68d9851930ceedbb2fa7624b8708d20aec88657e8d5d2f0b695'
    if original_irx_sha == IRX_ORIGINAL_SHA:
        fixed_irx, iop_meta = build_iop(original_irx_bytes)
        iop_action = 'applied'
    elif original_irx_sha == KNOWN_PATCHED_IRX_SHA:
        fixed_irx, iop_meta = original_irx_bytes, {'new_sha256': original_irx_sha}
        iop_action = 'already-patched'
    else:
        raise ValueError('Unsupported VCS CDSTREAM.IRX revision or unknown previous modification; '
                         'refusing to silently preserve unsafe position math')
    expected_irx_sha = hashlib.sha256(fixed_irx).hexdigest()
    if expected_irx_sha != KNOWN_PATCHED_IRX_SHA:
        raise ValueError('VCS IOP patch output differs from authenticated reference')
    file_keys = {name: key for key, name in get_profile('vcs_ps2').stations}
    selected_sizes = {file_keys[name]: size for name, size in sizes.items()}
    # Both stock and adaptive modes require a signed/unsigned EE position guard.
    patched_ee, ee_report = build_guarded_executable(
        original_exe_bytes, selected_sizes, adaptive=expand_vcs_offsets)
    offset_report = ee_report.get('offset_patch_report')
    expected_exe_sha = hashlib.sha256(patched_ee).hexdigest()
    gain_reports = {}
    with tempfile.TemporaryDirectory(prefix='gta_vcs_radio_') as tmp:
        staging = Path(tmp)
        elf_stage = staging / EXPECTED_BOOT
        elf_stage.write_bytes(patched_ee)
        destinations[EXPECTED_BOOT] = elf_stage
        if iop_action == 'applied':
            irx_stage = staging / 'CDSTREAM.IRX'
            irx_stage.write_bytes(fixed_irx)
            destinations['MODULES/CDSTREAM.IRX'] = irx_stage
        if progress:
            progress('VCS: including signed-safe EE position guard and '
                     + ('corrected CDSTREAM.IRX' if iop_action == 'applied' else 'verified corrected CDSTREAM.IRX')
                     + (' plus adaptive offsets' if expand_vcs_offsets else ' with stock offset distribution'))
        if boost_radio_3db:
            for name, src in replacements.items():
                dst = staging / name
                gain_reports[name] = boost_vb(src, dst, progress, gain_db=gain_db)
                destinations['AUDIO/MUSIC/' + name] = dst
        def post_verify(image):
            if hashlib.sha256(image.read_entry_bytes(EXPECTED_BOOT)).hexdigest() != expected_exe_sha:
                raise IOError('Original/patched VCS executable changed unexpectedly')
            if hashlib.sha256(image.read_entry_bytes('MODULES/CDSTREAM.IRX')).hexdigest() != expected_irx_sha:
                raise IOError('Corrected VCS streaming module not installed or corrupted')
            for name, size in sizes.items():
                if image.get('AUDIO/MUSIC/' + name).size != size:
                    raise IOError(name + ': incorrect ISO replacement length')
        if progress:
            progress(f'Installing {len(replacements)} VCS station(s); ' +
                     ('including adaptive EE offsets + EE position guard + corrected IOP…' if expand_vcs_offsets
                      else 'including EE position guard + corrected IOP; stock offsets…'))
        installed = apply_iso_replacements(input_iso, output_iso, destinations, progress, post_verify)
    result = {
        'profile':'vcs_ps2', 'offset_mode':offset_mode, 'purpose':'Any-station VCS ISO installation (EE guarded position + corrected IOP)',
        'input_iso':str(input_iso), 'output_iso':str(output_iso),
        'iso_targets':sorted(destinations), 'vb_input_bytes':sizes,
        'boost_radio_3db':bool(boost_radio_3db), # legacy enabled-flag key
        'gain_enabled':bool(boost_radio_3db), 'gain_db':gain_db if boost_radio_3db else 0.0,
        'detected_replacement_stations':sorted(replacements),
        'boosted_replacement_stations':sorted(gain_reports),
        'gain_by_station':gain_reports,
        'installed':installed, 'executable_input_sha256':original_exe_sha,
        'executable_output_sha256':expected_exe_sha,
        'executable_preserved_sha256':None, # EE guard modifies executable in BOTH modes
        'experimental_vcs_adaptive_offsets':bool(expand_vcs_offsets),
        'offset_patch_report':offset_report, 'ee_position_guard_report':ee_report,
        'cdstream_input_sha256':original_irx_sha, 'cdstream_output_sha256':expected_irx_sha,
        'cdstream_patch_action':iop_action,
        'cdstream_preserved_sha256':(original_irx_sha if iop_action == 'already-patched' else None),
        'iop_patch_report':iop_meta, 'gameplay_verified':False,
        'notice':'EE guarded conversion + IOP signed-position arithmetic verified in output; '
                 'EE added PT_LOAD and IOP module runtime loading NOT PCSX2-verified. '
                 'Gain adjustment does not fix fade-in timing.'
    }
    if len(replacements) == 1:
        name = next(iter(replacements))
        result['iso_target'] = 'AUDIO/MUSIC/' + name
        result['gain'] = gain_reports.get(name)
        result['boost_vcpr_3db'] = bool(boost_radio_3db) if name == 'VCPR.VB' else False
    if report_path is not None:
        # The verified ISO has already been published. Do not tell the GUI the
        # game build failed solely because an optional JSON report was unwritable.
        try:
            report_path.parent.mkdir(parents=True, exist_ok=True)
            with report_path.open('x', encoding='utf-8') as dest:
                json.dump(result, dest, indent=2)
            result['report_path'] = str(report_path.resolve())
        except OSError as exc:
            result['report_write_error'] = f'ISO built successfully, but the JSON report could not be saved: {exc}'
            if progress:
                progress('WARNING: ' + result['report_write_error'])
    return result


def build_vcpr_audio_iso(*, input_iso: Path, replacement_vb: Path,
                         output_iso: Path, boost_vcpr_3db: bool = False,
                         gain_db: float = 3.0,
                         progress: Progress = None, report_path: Path | None = None) -> dict:
    """Backwards-compatible API for the earlier VCPR-only installer."""
    return build_radio_audio_iso(input_iso=input_iso, replacement_vb=replacement_vb,
        output_iso=output_iso, boost_radio_3db=boost_vcpr_3db, gain_db=gain_db, station='VCPR',
        progress=progress, report_path=report_path)


def main():
    import argparse
    parser = argparse.ArgumentParser(description='Install any VCS PS2 radio .VB(s) into a NEW US ISO; optional 0.1-3.0 dB gain')
    parser.add_argument('--input-iso', required=True, type=Path)
    parser.add_argument('--radio-vb', '--vcpr-vb', dest='source', required=True, type=Path,
                        help='A named .VB file or folder of station .VB files')
    parser.add_argument('--station', help='Target station for a single .VB with any filename')
    parser.add_argument('--output-iso', required=True, type=Path)
    parser.add_argument('--boost-3db', action='store_true', help='Enable +3 dB gain (legacy option)')
    parser.add_argument('--gain-db', type=validate_gain_db, default=None, help='Enable gain and select amount, 0.1 to 3.0 dB')
    parser.add_argument('--experimental-vcs-offsets', action='store_true',
                        help='Opt-in ~5-second shared offset ELF patch (known original/DIVU only). Runtime unverified.')
    parser.add_argument('--report', type=Path)
    ns = parser.parse_args()
    print(json.dumps(build_radio_audio_iso(input_iso=ns.input_iso, replacement_vb=ns.source,
        output_iso=ns.output_iso, station=ns.station,
        boost_radio_3db=(ns.boost_3db or ns.gain_db is not None),
        gain_db=ns.gain_db if ns.gain_db is not None else 3.0,
        expand_vcs_offsets=ns.experimental_vcs_offsets,
        report_path=ns.report, progress=lambda line: print(line, file=sys.stderr, flush=True)), indent=2))

if __name__ == '__main__':
    main()
