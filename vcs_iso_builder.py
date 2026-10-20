#!/usr/bin/env python3
"""VCS USA ISO9660 *experimental* full installation of the EE and IRX fixes.

File-system-safe is not runtime-safe. The injected VCS IRX remains unproven on
PCSX2/IOP. Strictly preserves source; rejects unrecognized revisions, corrupted
extents, boot mismatch and invalid streams. Does not borrow LCS binary patches.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Callable

from gta_radio_profiles import get_profile, vcs_preview
from lcs_duration_aware_iso_patcher import (BLOCK, Iso9660Image,
                                            apply_iso_replacements,
                                            atomic_write_report)
from vcs_ee_overflow_patch import (classify_executable, patched_bytes,
                                   PATCHED_SHA256, validate_radio_sizes)
from vcs_cdstream_signed_position_patch import (build as build_irx,
                                                 classify_irx, VCS_PATCHED_SHA)

STATIONS = dict(get_profile('vcs_ps2').stations)
# VCS's companion names are recognized only when they actually exist in the
# specific ISO. Never create a path or apply LCS paths to a different game.
COMPANION_NAMES = {'ENGLISH.GXT': ('TEXT/ENGLISH.GXT', 'ENGLISH.GXT'),
                   'GAME.DTZ': ('PS2/GAME.DTZ', 'GAME.DTZ')}


def sha256_entry(image: Iso9660Image, path: str) -> str:
    e = image.get(path)
    digest = hashlib.sha256()
    with image.path.open('rb') as src:
        src.seek(e.data_lba * BLOCK)
        n = e.size
        while n:
            chunk = src.read(min(1 << 20, n))
            if not chunk:
                raise ValueError('Truncated source ISO extent: ' + path)
            digest.update(chunk)
            n -= len(chunk)
    return digest.hexdigest()


def audit_vcs_iso(path: Path, *, require_pristine: bool = True) -> dict:
    """Inspect entire indexed ISO filesystem before ANY write.

    This explicitly rejects the older, overlapping 1.285GB VCPR directory
    record. It also refuses trailing non-volume data, aliases, alternate VDs,
    and mismatched boot identities. A clean ISO9660 scan is NOT a game check.
    """
    image = Iso9660Image(Path(path))
    if image.volume_sectors * BLOCK != image.size:
        raise ValueError('VCS ISO volume-space size does not match physical image length; '
                         'refusing malformed/trailing extent image')
    if image.find_boot_executable() != 'SLUS_215.90':
        raise ValueError('This ISO does not boot the supported USA SLUS_215.90 revision')
    with image.path.open('rb') as f:
        f.seek(16 * BLOCK)
        termination_found = False
        reserved_end = 18
        for sector in range(16, 64):
            f.seek(sector * BLOCK)
            d = f.read(BLOCK)
            if d[1:6] != b'CD001' or d[6] != 1:
                raise ValueError('Malformed ISO9660 descriptor chain')
            if d[0] not in (0, 1, 255):
                raise ValueError(f'Unsupported ISO descriptor type {d[0]}')
            if d[0] == 255:
                termination_found = True
                reserved_end = sector + 1
                break
        if not termination_found:
            raise ValueError('ISO descriptor terminator not found')
    indexed = [image.root_entry, *image.entries.values()]
    occupied = []
    for e in indexed:
        if e is None or e.is_multi_extent:
            raise ValueError('Unindexed/multi-extent filesystem entry unsupported')
        if e.ext_attr_blocks:
            raise ValueError('Extended-attribute sector allocation unsupported in VCS ISO builder')
        start = e.data_lba
        blocks = (e.size + BLOCK - 1) // BLOCK
        end = start + blocks
        if e.size <= 0 or start < reserved_end or end > image.volume_sectors:
            raise ValueError(f'Out-of-range or metadata-overlapping ISO extent: {e.path}')
        occupied.append((start, end, e.path or '<root>'))
    occupied.sort()
    for (a, b, name), (c, d, next_name) in zip(occupied, occupied[1:]):
        if b > c:
            raise ValueError(f'Overlapping VCS ISO extents: {name} and {next_name}')
    # Ensure directory entry offsets refer to a directory extent, not arbitrary
    # bytes in unrelated files. The root record is an exception in the PVD.
    directories = [(e.data_lba * BLOCK, e.data_lba * BLOCK + e.size)
                   for e in indexed if e.is_directory]
    for e in image.entries.values():
        if not any(start <= e.record_offset < stop for start, stop in directories):
            raise ValueError(f'ISO directory record outside directory extent: {e.path}')
    from vcs_ee_overflow_patch import ORIGINAL_SHA256
    from vcs_cdstream_signed_position_patch import EXPECTED_SHA
    exe = image.get('SLUS_215.90')
    ee_hash = sha256_entry(image, exe.path)
    if (require_pristine and ee_hash != ORIGINAL_SHA256) or (
        not require_pristine and ee_hash not in (ORIGINAL_SHA256, PATCHED_SHA256)):
        raise ValueError('Embedded SLUS_215.90 is not the verified pristine USA executable')
    irx = image.get('MODULES/CDSTREAM.IRX')
    irx_hash = sha256_entry(image, irx.path)
    if (require_pristine and irx_hash != EXPECTED_SHA) or (
        not require_pristine and irx_hash not in (EXPECTED_SHA, VCS_PATCHED_SHA)):
        raise ValueError('Embedded CDSTREAM.IRX does not match the original supported VCS module')
    sizes = {key: image.get('AUDIO/MUSIC/' + filename).size
             for key, filename in STATIONS.items()}
    # Original station recordings are expected to be full 16-KiB stereo pairs.
    validate_radio_sizes(list(sizes.values()))
    return {'image': image, 'original_sizes': sizes,
            'executable_sha256': ee_hash, 'irx_sha256': irx_hash,
            'checked_file_count': sum(not e.is_directory for e in image.entries.values()),
            'checked_extent_count': len(occupied), 'volume_sectors': image.volume_sectors}


def select_replacements(source: Path | None, image: Iso9660Image) -> tuple[dict, dict, dict]:
    """Find only selected VCS stations and optional *existing* companion paths."""
    paths: dict[str, Path] = {}
    checked: dict[str, int] = {}
    companions: dict[str, str] = {}
    if not source:
        return paths, checked, companions
    source = Path(source)
    if source.is_file() and source.suffix.upper() == '.VB':
        candidates = [source]
    elif source.is_dir():
        candidates = [p for p in source.iterdir() if p.is_file()]
        # Allow explicit nested companion locations, no recursive wildcard.
        for name in ('TEXT/ENGLISH.GXT', 'CHK/PS2/GAME.DTZ'):
            file = source / name
            if file.is_file():
                candidates.append(file)
    else:
        raise ValueError('VCS radio input must be an existing folder or a .VB file')
    station_names = {filename.upper(): key for key, filename in STATIONS.items()}
    for file in candidates:
        upper = file.name.upper()
        if upper.endswith('.VB'):
            if upper not in station_names:
                raise ValueError(f'Unknown VCS radio file: {file.name}')
            station = station_names[upper]
            if station in checked:
                raise ValueError(f'Duplicate station replacement: {upper}')
            size = vcs_preview(file)['size_bytes']
            validate_radio_sizes([size])
            checked[station] = size
            paths['AUDIO/MUSIC/' + upper] = file
        elif upper in COMPANION_NAMES:
            matches = [entry for entry in COMPANION_NAMES[upper] if entry in image.entries]
            if len(matches) != 1:
                raise ValueError(f'{upper} companion has {len(matches)} matching VCS ISO paths; cannot install safely')
            if matches[0] in paths:
                raise ValueError(f'Duplicate companion: {upper}')
            if not file.stat().st_size:
                raise ValueError(f'Empty companion file: {file}')
            paths[matches[0]] = file
            companions[upper] = matches[0]
    return paths, checked, companions


def build_vcs_iso(*, input_iso: Path, output_iso: Path,
                  replacement_dir: Path | None = None,
                  executable_path: Path | None = None,
                  report_path: Path | None = None,
                  progress: Callable[[str], None] | None = None) -> dict:
    """Build copy of exact USA disc, patch EE/IRX, install selected radio files.

    Atomic publication and no-overwrite. All original extents kept byte intact;
    file payloads are relocated and directory records are independently checked.
    """
    input_iso, output_iso = Path(input_iso), Path(output_iso)
    if input_iso.suffix.lower() != '.iso' or output_iso.suffix.lower() != '.iso':
        raise ValueError('VCS builder accepts a raw .iso input and .iso output only')
    if input_iso.resolve() == output_iso.resolve() or output_iso.exists():
        raise ValueError('Output must be a NEW filename distinct from the input ISO; no overwriting')
    report_path = Path(report_path) if report_path else Path(str(output_iso) + '.vcs_report.json')
    if report_path.resolve() in (input_iso.resolve(), output_iso.resolve()) or report_path.exists():
        raise ValueError('Report must be NEW and distinct from ISO source/output; no overwriting')
    if progress: progress('Scanning VCS ISO9660 directory, extents and original binary revisions…')
    audit = audit_vcs_iso(input_iso)
    image = audit['image']
    extra, changes, companion_dest = select_replacements(replacement_dir, image)
    if not changes:
        raise ValueError('Select at least one VCS .VB replacement before building an output ISO')
    installed = dict(audit['original_sizes'])
    installed.update(changes)
    validate_radio_sizes(list(installed.values()))
    from vcs_radio_calculator import calculate_plan
    plan = calculate_plan(audit['original_sizes'], changes)
    if not plan.all_selected_replacements_within_target:
        raise ValueError('Replacement size exceeds supported VCS aligned duration range')
    original_ee = image.read_entry_bytes('SLUS_215.90')
    if executable_path:
        external = Path(executable_path)
        identity = classify_executable(external.read_bytes())
        if not identity['recognized_reference']:
            raise ValueError('Selected external executable is incompatible: ' + identity['reason'])
        if external.resolve() in (input_iso.resolve(), output_iso.resolve()):
            raise ValueError('Executable must not be the input/output ISO')
    ee_patched = patched_bytes(original_ee)
    old_irx = image.read_entry_bytes('MODULES/CDSTREAM.IRX')
    irx_patched, irx_info = build_irx(old_irx)
    if hashlib.sha256(ee_patched).hexdigest() != PATCHED_SHA256:
        raise ValueError('Internal EE verification failed')
    if hashlib.sha256(irx_patched).hexdigest() != VCS_PATCHED_SHA:
        raise ValueError('Internal IRX verification failed')
    if progress: progress('Verified exact VCS USA EE and experimental IRX outputs; preparing relocation…')
    output_iso.parent.mkdir(parents=True, exist_ok=True)
    replacements = {**extra}
    with tempfile.TemporaryDirectory(prefix='.vcs_build_', dir=output_iso.parent) as work:
        ee_tmp, irx_tmp = Path(work) / 'SLUS_215.90', Path(work) / 'CDSTREAM.IRX'
        ee_tmp.write_bytes(ee_patched)
        irx_tmp.write_bytes(irx_patched)
        replacements['SLUS_215.90'] = ee_tmp
        replacements['MODULES/CDSTREAM.IRX'] = irx_tmp
        def post_verify(out_image: Iso9660Image) -> None:
            result = audit_vcs_iso(out_image.path, require_pristine=False)
            if result['executable_sha256'] != PATCHED_SHA256 or result['irx_sha256'] != VCS_PATCHED_SHA:
                raise ValueError('Wrong EE or IOP patch in rebuilt VCS ISO')
            for key, size in installed.items():
                if out_image.get('AUDIO/MUSIC/' + STATIONS[key]).size != size:
                    raise ValueError(f'Radio duration/boundary verification failed: {key}')
            if out_image.find_boot_executable() != 'SLUS_215.90':
                raise ValueError('Boot executable changed unexpectedly')
        report = apply_iso_replacements(input_iso, output_iso, replacements,
                                        progress=progress, post_verify=post_verify,
                                        always_relocate=True)
    # Verify published output actually exists; do not include private temporary filenames.
    final = {
        'game_profile': 'vcs_ps2', 'input_iso': str(input_iso),
        'output_iso': str(output_iso), 'report_path': str(report_path),
        'iso_files_replaced': [{k: v for k, v in row.items() if k != 'source_path'} for row in report],
        'original_sizes': audit['original_sizes'], 'installed_sizes': installed,
        'station_calculation': plan.as_dict(),
        'optional_companions': companion_dest,
        'ee_patched_sha256': PATCHED_SHA256, 'irx_patched_sha256': VCS_PATCHED_SHA,
        'input_iso_metadata_checked': audit['checked_file_count'],
        'structural_verification': 'Passed, with relocated files and byte-comparison of all untouched entries',
        'runtime_verified': False,
        'experimental_irx_loader_verified': False,
        'warning': 'EXPERIMENTAL. PCSX2 boot, MIPS loader, radio EOF/seek/switch/wrap still unverified.',
    }
    # Report failure must not leave an apparently completed but unreported image.
    try:
        atomic_write_report(report_path, json.dumps(final, indent=2))
    except BaseException:
        output_iso.unlink(missing_ok=True)
        raise
    if progress: progress('VCS ISO published, all ISO9660 extents and file bytes verified. Gameplay NOT verified.')
    return final
