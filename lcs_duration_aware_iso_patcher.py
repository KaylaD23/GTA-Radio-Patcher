#!/usr/bin/env python3
"""Experimental ISO9660 integration for the LCS PS2 duration-aware patcher.

This module reads radio streams from a raw 2048-byte ISO9660 image, patches a
selected standalone SLUS_214.23 executable (or the embedded one in legacy mode),
and writes a new ISO with the patched executable at the ISO boot path. Missing
station replacements are kept from the input ISO.

Caveat: the radio length unit is still an unconfirmed milliseconds hypothesis;
output has not been validated in PCSX2 or on hardware.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import struct
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple

import lcs_duration_aware_exe_patcher as patcher

# Explicit EE + IOP modes. Keep the modes separate so the build cannot
# silently omit either binary modification.
COMBINED_EE_MODES = {
    "iop-elf-stock": "stock",
    "iop-elf-shared": "shared",
    "iop-elf-station-specific": "station-specific",
}
IOP_PATCH_MODES = ("iop-safe-seek", *COMBINED_EE_MODES)
PRISTINE_SOURCE_MODES = ("native", *IOP_PATCH_MODES)

from lcs_radio_stream_audit import audit_vb, audit_cdstream_module
from lcs_iop_safe_seek import patch_cdstream, ORIGINAL_SHA256 as SUPPORTED_CDSTREAM_SHA256
from lcs_stream_bounds import installed_size, stage_audio_with_short_stream_guard, READ_CHUNK_BYTES
from lcs_patch_calculator import calculate_plan, PatchCalculation
from lcs_patch_location_registry import (locate_original_values, make_write_manifest,
    verify_executable_slots, verify_output_locations)


def validate_mode_against_calculated_iop_requirements(calculated: PatchCalculation, mode: str) -> None:
    """Block output when the chosen mode omits required CDSTREAM.IRX changes.

    Shift interpolation, signed position conversion and unsigned length
    conversion are separate requirements. The former can be needed long
    before the unsigned duration multiply exceeds its limit.
    """
    if mode not in IOP_PATCH_MODES and calculated.iop_needs_instruction_patch:
        reasons = []
        if calculated.iop_extra_shift:
            reasons.append(f'IOP seek interpolation needs extra shift +{calculated.iop_extra_shift}')
        if calculated.iop_position_overflow_fix_required:
            reasons.append('IOP signed playback-position conversion can overflow')
        if calculated.iop_duration_overflow_fix_required:
            reasons.append('IOP unsigned playback-length conversion can overflow')
        raise ValueError(
            'Unsafe patch mode ' + repr(mode) + ' omits required CDSTREAM.IRX changes for '
            f'the installed radio sizes ({calculated.largest_installed_size:,} bytes maximum): '
            + '; '.join(reasons) + '. Select Combined EE+IOP or IOP safe seeks only.'
        )

BLOCK = 2048
PVD_START_SECTOR = 16
MAX_DESCRIPTOR_SECTORS = 64
STATION_ISO_PREFIX = "AUDIO/MUSIC"
# Companion radio-mod assets are optional. When a user selects a replacement
# *directory*, accept these files alongside the station .VB files. A single
# selected .VB never implicitly scans its parent for unrelated files.
OPTIONAL_RADIO_ASSETS = {
    "ENGLISH.GXT": "TEXT/ENGLISH.GXT",
    "GAME.DTZ": "CHK/PS2/GAME.DTZ",
}

Progress = Optional[Callable[[str], None]]


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()



def atomic_write_report(path: Path, content: str) -> None:
    """Do not truncate an existing JSON report if writing a new one fails."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".building", dir=str(path.parent))
    temp_path = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as dst:
            dst.write(content)
            dst.flush()
            os.fsync(dst.fileno())
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def norm_iso_path(path: str) -> str:
    parts = []
    for part in path.replace("\\", "/").split("/"):
        part = part.strip()
        if not part or part == ".":
            continue
        if part == "..":
            if parts:
                parts.pop()
            continue
        part = re.sub(r";\d+$", "", part)
        parts.append(part.upper())
    return "/".join(parts)


def _le_u32(buf: bytes, off: int) -> int:
    return struct.unpack_from("<I", buf, off)[0]


def _be_u32(buf: bytes, off: int) -> int:
    return struct.unpack_from(">I", buf, off)[0]


def _both_endian_u32(raw: bytes, off: int, field_name: str) -> int:
    le = _le_u32(raw, off)
    be = _be_u32(raw, off + 4)
    if le != be:
        raise ValueError(f"ISO9660 {field_name} both-endian values disagree ({le} vs {be})")
    return le


@dataclass
class IsoEntry:
    path: str
    record_offset: int
    extent_lba: int
    size: int
    flags: int
    ext_attr_blocks: int
    identifier: str

    @property
    def is_directory(self) -> bool:
        return bool(self.flags & 0x02)

    @property
    def is_multi_extent(self) -> bool:
        return bool(self.flags & 0x80)

    @property
    def data_lba(self) -> int:
        return self.extent_lba + self.ext_attr_blocks


class Iso9660Image:
    def __init__(self, path: Path):
        self.path = Path(path)
        if not self.path.is_file():
            raise ValueError(f"Input ISO not found: {self.path}")
        self.size = self.path.stat().st_size
        if self.size < (PVD_START_SECTOR + 1) * BLOCK:
            raise ValueError("Input image is too small to contain an ISO9660 volume descriptor")
        if self.size % BLOCK:
            raise ValueError(f"Input image size ({self.size:,}) is not aligned to 2048-byte ISO sectors")
        self.pvd_offset, self.volume_sectors, self.block_size, self.root = self._read_volume_descriptor()
        self._reject_unhandled_secondary_volume_descriptors()
        if self.block_size != BLOCK:
            raise ValueError(f"Unsupported ISO logical block size {self.block_size}; expected 2048")
        if self.volume_sectors * BLOCK > self.size:
            raise ValueError("ISO volume-space size extends beyond the physical image size")
        self.entries: Dict[str, IsoEntry] = {}
        self.root_entry: Optional[IsoEntry] = None
        self._index_filesystem()

    def _read_volume_descriptor(self) -> Tuple[int, int, int, bytes]:
        with self.path.open("rb") as f:
            for sector in range(PVD_START_SECTOR, min(MAX_DESCRIPTOR_SECTORS, self.size // BLOCK)):
                f.seek(sector * BLOCK)
                desc = f.read(BLOCK)
                if len(desc) != BLOCK:
                    break
                dtype = desc[0]
                if desc[1:6] != b"CD001" or desc[6] != 1:
                    continue
                if dtype == 1:  # Primary Volume Descriptor
                    volume_sectors = _both_endian_u32(desc, 80, "volume space size")
                    block_size = struct.unpack_from("<H", desc, 128)[0]
                    block_size_be = struct.unpack_from(">H", desc, 130)[0]
                    if block_size != block_size_be:
                        raise ValueError("ISO logical block size both-endian values disagree")
                    root_len = desc[156]
                    if root_len < 34:
                        raise ValueError("Invalid ISO9660 root directory record")
                    root = desc[156:156 + root_len]
                    return sector * BLOCK, volume_sectors, block_size, root
                if dtype == 255:
                    break
        raise ValueError("No ISO9660 Primary Volume Descriptor found at standard sector 16+")

    def _reject_unhandled_secondary_volume_descriptors(self) -> None:
        """Do not claim a coherent patched image if secondary trees exist.

        The builder updates the PVD ISO9660 directory, not Joliet/UDF trees.
        Such images should be rebuilt with a multi-filesystem-aware authoring tool.
        """
        with self.path.open("rb") as f:
            for sector in range(PVD_START_SECTOR, min(MAX_DESCRIPTOR_SECTORS, self.size // BLOCK)):
                f.seek(sector * BLOCK)
                desc = f.read(BLOCK)
                if desc[1:6] != b"CD001" or desc[6] != 1:
                    continue
                if desc[0] == 2:
                    raise ValueError(
                        "ISO contains a Supplementary Volume Descriptor (e.g. Joliet). "
                        "This patcher edits only primary ISO9660 file records; refusing "
                        "to publish inconsistent alternate directory trees."
                    )
                if desc[0] == 255:
                    break

    def _parse_record(self, rec: bytes, record_offset: int, parent: str, allow_special: bool = False) -> Optional[IsoEntry]:
        if len(rec) < 34 or rec[0] != len(rec):
            return None
        id_len = rec[32]
        if 33 + id_len > len(rec):
            return None
        identifier_bytes = rec[33:33 + id_len]
        if identifier_bytes in (b"\x00", b"\x01") and not allow_special:
            return None
        identifier = identifier_bytes.decode("ascii", errors="replace")
        clean = re.sub(r";\d+$", "", identifier).upper()
        if identifier_bytes == b"\x00" and allow_special:
            clean = ""
        if (not clean and not (allow_special and identifier_bytes == b"\x00")) or clean in (".", ".."):
            return None
        path = f"{parent}/{clean}" if parent else clean
        ext_attr = rec[1]
        extent_lba = _both_endian_u32(rec, 2, "extent location")
        size = _both_endian_u32(rec, 10, "data length")
        flags = rec[25]
        return IsoEntry(
            path=path,
            record_offset=record_offset,
            extent_lba=extent_lba,
            size=size,
            flags=flags,
            ext_attr_blocks=ext_attr,
            identifier=identifier,
        )

    def _read_entry_data(self, entry: IsoEntry) -> bytes:
        with self.path.open("rb") as f:
            f.seek(entry.data_lba * BLOCK)
            data = f.read(entry.size)
        if len(data) != entry.size:
            raise ValueError(f"Could not read complete ISO entry: {entry.path}")
        return data

    def _index_filesystem(self) -> None:
        root_entry = self._parse_record(self.root, self.pvd_offset + 156, "", allow_special=True)
        if root_entry is None or not root_entry.is_directory:
            raise ValueError("The ISO primary descriptor has an invalid root directory")
        self.root_entry = root_entry
        visited = set()

        def walk(directory: IsoEntry, depth: int = 0) -> None:
            if depth > 32:
                raise ValueError("ISO directory nesting is unexpectedly deep")
            identity = (directory.data_lba, directory.size)
            if identity in visited:
                return
            visited.add(identity)
            with self.path.open("rb") as f:
                f.seek(directory.data_lba * BLOCK)
                data = f.read(directory.size)
            if len(data) != directory.size:
                raise ValueError(f"Truncated ISO directory: {directory.path or '<root>'}")
            pos = 0
            children = []
            while pos < len(data):
                rec_len = data[pos]
                if rec_len == 0:
                    pos = ((pos // BLOCK) + 1) * BLOCK
                    continue
                if pos + rec_len > len(data):
                    # A malformed/truncated final record is an error, not silently skipped.
                    raise ValueError(f"Malformed directory record in {directory.path or '<root>'} at byte {pos}")
                rec = data[pos:pos + rec_len]
                abs_off = directory.data_lba * BLOCK + pos
                parsed = self._parse_record(rec, abs_off, directory.path)
                if parsed is not None:
                    if parsed.path in self.entries:
                        # A versioned ISO can contain duplicates after version suffix removal.
                        # Prefer ;1 over later versions to match the game's normal ISO path.
                        existing = self.entries[parsed.path]
                        if parsed.identifier.endswith(";1") or not existing.identifier.endswith(";1"):
                            self.entries[parsed.path] = parsed
                    else:
                        self.entries[parsed.path] = parsed
                    if parsed.is_directory:
                        children.append(parsed)
                pos += rec_len
            for child in children:
                walk(child, depth + 1)

        walk(root_entry)

    def get(self, path: str) -> IsoEntry:
        norm = norm_iso_path(path)
        if norm not in self.entries:
            raise ValueError(f"ISO entry not found: {path}")
        return self.entries[norm]

    def read_entry_bytes(self, entry_or_path) -> bytes:
        entry = self.get(entry_or_path) if isinstance(entry_or_path, str) else entry_or_path
        if entry.is_directory:
            raise ValueError(f"Expected a file, found directory: {entry.path}")
        if entry.is_multi_extent:
            raise ValueError(f"Multi-extent ISO files are not supported for patch targets: {entry.path}")
        return self._read_entry_data(entry)

    def extract_entry(self, entry_or_path, out_path: Path) -> Path:
        entry = self.get(entry_or_path) if isinstance(entry_or_path, str) else entry_or_path
        if entry.is_directory:
            raise ValueError(f"Expected a file, found directory: {entry.path}")
        if entry.is_multi_extent:
            raise ValueError(f"Multi-extent ISO files are not supported for patch targets: {entry.path}")
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        remaining = entry.size
        with self.path.open("rb") as src, out_path.open("wb") as dst:
            src.seek(entry.data_lba * BLOCK)
            while remaining:
                chunk = src.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise ValueError(f"Unexpected end of ISO while extracting {entry.path}")
                dst.write(chunk)
                remaining -= len(chunk)
        return out_path

    def find_boot_executable(self) -> str:
        try:
            system_entry = self.get("SYSTEM.CNF")
            cnf = self.read_entry_bytes(system_entry).decode("ascii", errors="replace")
            match = re.search(r"^\s*BOOT2?\s*=\s*(.*?)\s*$", cnf, flags=re.IGNORECASE | re.MULTILINE)
            if match:
                boot_path = match.group(1).strip().strip('"')
                if ":" in boot_path:
                    boot_path = boot_path.split(":", 1)[1]
                boot_path = boot_path.strip().replace("\\", "/").lstrip("/")
                normalized = norm_iso_path(boot_path)
                if normalized in self.entries:
                    return normalized
                # Some CNFs include a trailing boot-media annotation or unusual path suffix.
                first = normalized.split(" ", 1)[0]
                if first in self.entries:
                    return first
        except ValueError:
            pass
        candidates = [p for p, e in self.entries.items() if p.split("/")[-1].upper() == "SLUS_214.23" and not e.is_directory]
        if len(candidates) == 1:
            return candidates[0]
        if not candidates:
            raise ValueError("Could not find SLUS_214.23 in the ISO (also unable to resolve it from SYSTEM.CNF)")
        raise ValueError("Multiple SLUS_214.23 candidates found; could not determine boot executable")

    def inspect_targets(self, validate_embedded_executable: bool = True) -> dict:
        """Find the boot executable path and radio entries in the ISO.

        By default the embedded executable must be the supported pristine build.
        If an explicit external executable is supplied to the builder, the ISO's
        boot path is still located from SYSTEM.CNF but the external file becomes
        the patch source; in that mode the embedded file is reported, not used as
        the source of executable bytes.
        """
        exe_path = self.find_boot_executable()
        exe = self.get(exe_path)
        station_records = {}
        for key, filename in patcher.STATIONS:
            iso_path = f"{STATION_ISO_PREFIX}/{filename}"
            station_records[key] = self.get(iso_path)
        exe_blob = self.read_entry_bytes(exe)
        exe_digest = hashlib.sha256(exe_blob).hexdigest()
        exe_meta = None
        if validate_embedded_executable:
            exe_meta = patcher.validate_original_executable(exe_blob)
        return {
            "boot_executable_path": exe_path,
            "executable_size": exe.size,
            "executable_sha256": exe_meta["sha256"] if exe_meta else exe_digest,
            "embedded_executable_sha256": exe_digest,
            "embedded_executable_matches_supported": exe_digest == patcher.SUPPORTED_SHA256,
            "station_records": station_records,
        }


def duration_ms_from_size(size: int, label: str = "audio") -> int:
    if size <= 0:
        raise ValueError(f"{label}: empty audio stream")
    if size % 0x4000 != 0:
        raise ValueError(
            f"{label}: size {size:,} is not a multiple of 0x4000; expected the raw stereo PSX ADPCM stream format"
        )
    from lcs_iop_safe_seek import MAX_SAFE_DURATION_SIZE
    if size > MAX_SAFE_DURATION_SIZE:
        raise ValueError(f"{label}: stream exceeds configured 5-hour limit")
    numerator = size * 7
    if numerator % 256:
        raise ValueError(f"{label}: size does not yield an exact millisecond duration")
    duration_ms = numerator // 256
    if not 0 < duration_ms <= 0xFFFFFFFF:
        raise ValueError(f"{label}: duration does not fit a nonzero 32-bit value")
    return duration_ms


def _write_both_endian_u32(f, record_offset: int, field_offset: int, value: int) -> None:
    if not 0 <= value <= 0xFFFFFFFF:
        raise ValueError(f"Value {value} does not fit uint32")
    f.seek(record_offset + field_offset)
    f.write(struct.pack("<I", value))
    f.write(struct.pack(">I", value))


def verify_untouched_file_contents(original: Iso9660Image, staged: Iso9660Image,
                                    changed_paths: set[str]) -> tuple[int, int]:
    """Byte-compare *every* untouched ISO file before publishing the build.

    Fails closed for unsupported multi-extent data, and checks that no original
    filesystem path vanished or changed type/size. This goes beyond verifying
    the hashes of only the files the installer intentionally replaced.
    """
    if set(original.entries) != set(staged.entries):
        missing = sorted(set(original.entries) - set(staged.entries))
        added = sorted(set(staged.entries) - set(original.entries))
        raise IOError(f"ISO structure changed unexpectedly: missing={missing[:4]} added={added[:4]}")
    num_files = total_bytes = 0
    with original.path.open("rb") as src, staged.path.open("rb") as dst:
        for path, old in original.entries.items():
            current = staged.get(path)
            if old.is_directory != current.is_directory:
                raise IOError(f"ISO entry changed file/directory type: {path}")
            if old.is_directory or path in changed_paths:
                continue
            if old.is_multi_extent or current.is_multi_extent:
                raise ValueError(f"Cannot certify preservation of multi-extent ISO file: {path}")
            if old.size != current.size:
                raise IOError(f"Untouched ISO file size changed: {path}")
            src.seek(old.data_lba * BLOCK)
            dst.seek(current.data_lba * BLOCK)
            remaining = old.size
            while remaining:
                amount=min(1<<20,remaining)
                a=src.read(amount)
                b=dst.read(amount)
                if len(a)!=amount or len(b)!=amount or a!=b:
                    raise IOError(f"Untouched file unexpectedly modified: {path}")
                remaining -= amount
            num_files += 1
            total_bytes += old.size
    return num_files,total_bytes


def apply_iso_replacements(
    input_iso: Path,
    output_iso: Path,
    replacements: Dict[str, Path],
    progress: Progress = None,
    post_verify: Optional[Callable[[Iso9660Image], None]] = None,
) -> List[dict]:
    """Create a verified ISO replacement build, publishing it only on success.

    Output is assembled in a temporary sibling and atomically moved into place
    after all directory entries and payload hashes pass verification. A failed
    write therefore does not destroy an earlier output image.
    """
    input_iso = Path(input_iso)
    output_iso = Path(output_iso)
    input_resolved = input_iso.resolve()
    output_resolved = output_iso.resolve()
    if input_resolved == output_resolved:
        raise ValueError("Output ISO must be a different file from the input ISO")
    if not input_iso.is_file():
        raise ValueError(f"Input ISO not found: {input_iso}")
    if output_iso.exists() and output_iso.is_dir():
        raise ValueError(f"Output path is a directory: {output_iso}")

    iso = Iso9660Image(input_iso)
    # Source ISOs are often several GB: record filesystem change metadata so an
    # image concurrently rewritten by another process cannot be published as a
    # valid new build after the source changed. This is not a cryptographic
    # snapshot of the whole source image.
    input_initial_stat = input_iso.stat()
    input_signature = (input_initial_stat.st_size, input_initial_stat.st_mtime_ns,
                       input_initial_stat.st_ctime_ns)
    def ensure_input_unchanged() -> None:
        current = input_iso.stat()
        if (current.st_size, current.st_mtime_ns, current.st_ctime_ns) != input_signature:
            raise IOError(f"Input ISO changed while building output: {input_iso}")

    # Never overwrite a shared/overlapping file extent in-place. Some ISOs
    # include aliased extents or malformed overlapping directory records.
    indexed_extents = []
    # The PVD root directory is not an ordinary entry in iso.entries. Reserve
    # it explicitly: a malformed source image can make a target file alias
    # that extent, which would otherwise destroy the filesystem on in-place write.
    filesystem_entries = list(iso.entries.values())
    if iso.root_entry is not None:
        filesystem_entries.append(iso.root_entry)
    for existing in filesystem_entries:
        blocks = math.ceil(existing.size / BLOCK)
        if blocks:
            start, end = existing.data_lba, existing.data_lba + blocks
            if end > iso.volume_sectors or end * BLOCK > iso.size:
                raise ValueError(
                    f"ISO source entry {existing.path or '<root>'} extends outside the declared volume"
                )
            indexed_extents.append((existing.path or '<root>', start, end))
    plan = []
    target_paths = set()
    for iso_path, source_path in replacements.items():
        source_path = Path(source_path)
        if not source_path.is_file():
            raise ValueError(f"Replacement source file not found: {source_path}")
        source_resolved = source_path.resolve()
        if source_resolved in (input_resolved, output_resolved):
            raise ValueError(
                f"Replacement source conflicts with an ISO input/output path: {source_path}"
            )
        entry = iso.get(iso_path)
        if entry.path in target_paths:
            raise ValueError(f"The same ISO entry is targeted twice: {entry.path}")
        target_paths.add(entry.path)
        if entry.is_directory or entry.is_multi_extent:
            raise ValueError(f"Cannot replace directory or multi-extent file: {entry.path}")
        new_size = source_path.stat().st_size
        if new_size <= 0:
            raise ValueError(f"Replacement file is empty: {source_path}")
        old_capacity = math.ceil(entry.size / BLOCK) * BLOCK
        # An ISO file's file-backed data begins after any extended-attribute sectors.
        data_offset = entry.data_lba * BLOCK
        interval_start = entry.data_lba
        interval_end = interval_start + math.ceil(entry.size / BLOCK)
        overlap = [path for path, start, end in indexed_extents
                   if path != entry.path and interval_start < end and start < interval_end]
        # The initial ISO metadata occupies reserved sectors (0-15) and the
        # volume descriptor chain; never write payload data into those sectors.
        unsafe_metadata = interval_start < PVD_START_SECTOR + 2
        relocate = new_size > old_capacity or bool(overlap) or unsafe_metadata
        stat = source_path.stat()
        source_signature = (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
        # Snapshot the *contents* too, rather than trusting size alone: files
        # can change while a lengthy ISO is copied, even at identical sizes.
        source_sha256 = sha256_file(source_path)
        if (source_path.stat().st_size, source_path.stat().st_mtime_ns, source_path.stat().st_ctime_ns) != source_signature:
            raise ValueError(f"Replacement changed during preflight hashing: {source_path}")
        plan.append({
            "iso_path": entry.path,
            "record_offset": entry.record_offset,
            "old_size": entry.size,
            "old_extent_lba": entry.extent_lba,
            "data_lba": entry.data_lba,
            "old_capacity_bytes": old_capacity,
            "new_size": new_size,
            "relocate": relocate,
            "source_path": source_path,
            "source_signature": source_signature,
            "source_sha256": source_sha256,
            "relocation_reason": ("exceeds allocated space" if new_size > old_capacity else
                                   "overlaps another ISO entry" if overlap else
                                   "metadata sector" if unsafe_metadata else "not needed"),
            "data_offset": data_offset,
            "ext_attr_blocks": entry.ext_attr_blocks,
        })

    if progress:
        progress(f"Validated {len(plan)} ISO file replacement(s). Creating output copy…")
    output_iso.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{output_iso.name}.", suffix=".building", dir=str(output_iso.parent)
    )
    os.close(fd)
    staged_iso = Path(temp_name)
    try:
        shutil.copyfile(input_iso, staged_iso)
        ensure_input_unchanged()
        appended_end_sector = iso.volume_sectors
        with staged_iso.open("r+b") as out:
            # Never append inside a volume extent or to a truncated file. A valid ISO is sector-aligned.
            out.seek(0, 2)
            file_size = out.tell()
            append_lba = max(math.ceil(file_size / BLOCK), iso.volume_sectors)
            if file_size < append_lba * BLOCK:
                out.seek(append_lba * BLOCK - 1)
                out.write(b"\x00")
            for n, item in enumerate(plan, 1):
                source_path: Path = item["source_path"]
                new_size = int(item["new_size"])
                if progress:
                    progress(f"Writing ISO file {n}/{len(plan)}: {item['iso_path']} ({new_size:,} bytes)…")
                if item["relocate"]:
                    new_lba = append_lba
                    out.seek(new_lba * BLOCK)
                    written = 0
                    with source_path.open("rb") as src:
                        while True:
                            chunk = src.read(1024 * 1024)
                            if not chunk:
                                break
                            out.write(chunk)
                            written += len(chunk)
                    if written != new_size:
                        raise IOError(f"Source changed while being copied: {source_path}")
                    padded_size = math.ceil(new_size / BLOCK) * BLOCK
                    if padded_size > new_size:
                        out.write(b"\x00" * (padded_size - new_size))
                    append_lba += padded_size // BLOCK
                    if append_lba > 0xFFFFFFFF:
                        raise ValueError("Resulting ISO exceeds ISO9660 32-bit sector extent limit")
                    appended_end_sector = max(appended_end_sector, append_lba)
                    # A relocated file no longer has extended attribute sectors at the new extent.
                    out.seek(int(item["record_offset"]) + 1)
                    out.write(b"\x00")
                    _write_both_endian_u32(out, int(item["record_offset"]), 2, new_lba)
                    location = "appended"
                    new_extent = new_lba
                else:
                    data_offset = int(item["data_offset"])
                    capacity = int(item["old_capacity_bytes"])
                    out.seek(data_offset)
                    written = 0
                    with source_path.open("rb") as src:
                        while True:
                            chunk = src.read(1024 * 1024)
                            if not chunk:
                                break
                            out.write(chunk)
                            written += len(chunk)
                    if written != new_size:
                        raise IOError(f"Source changed while being copied: {source_path}")
                    if capacity > new_size:
                        out.write(b"\x00" * (capacity - new_size))
                    location = "in-place"
                    new_extent = int(item["old_extent_lba"])
                _write_both_endian_u32(out, int(item["record_offset"]), 10, new_size)
                item["new_extent_lba"] = new_extent
                item["write_method"] = location

            new_volume_sectors = max(iso.volume_sectors, appended_end_sector)
            out.seek(iso.pvd_offset + 80)
            out.write(struct.pack("<I", new_volume_sectors))
            out.write(struct.pack(">I", new_volume_sectors))
            out.flush()
            os.fsync(out.fileno())

        # Verify the staged image before publishing it over a prior output.
        verified_iso = Iso9660Image(staged_iso)
        for item in plan:
            entry = verified_iso.get(item["iso_path"])
            expected_size = int(item["new_size"])
            if entry.size != expected_size:
                raise IOError(
                    f"ISO verification failed for {item['iso_path']}: directory size {entry.size} != {expected_size}"
                )
            expected_hash = item["source_sha256"]
            source_path = Path(item["source_path"])
            stat = source_path.stat()
            if (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns) != item["source_signature"]:
                raise IOError(f"Replacement source changed during ISO build: {source_path}")
            digest = hashlib.sha256()
            remaining = entry.size
            with staged_iso.open("rb") as f:
                f.seek(entry.data_lba * BLOCK)
                while remaining:
                    chunk = f.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise IOError(f"ISO verification hit unexpected end of file: {item['iso_path']}")
                    digest.update(chunk)
                    remaining -= len(chunk)
            if digest.hexdigest() != expected_hash:
                raise IOError(f"ISO verification failed: payload hash mismatch for {item['iso_path']}")

        if progress:
            progress("Verifying every untouched ISO file is byte-for-byte identical to the source…")
        untouched_count, untouched_bytes = verify_untouched_file_contents(
            iso, verified_iso, target_paths
        )
        if progress:
            progress(f"Preservation verified: {untouched_count} untouched ISO file(s), {untouched_bytes:,} bytes")
        # The caller may additionally check calculated EE/IOP patch destinations
        # using the actual staged ISO image. A failure must not publish output.
        if post_verify is not None:
            post_verify(verified_iso)
        ensure_input_unchanged()
        os.replace(staged_iso, output_iso)
    finally:
        try:
            staged_iso.unlink(missing_ok=True)
        except OSError:
            pass

    return [{
        "iso_path": item["iso_path"],
        "old_size_bytes": item["old_size"],
        "new_size_bytes": item["new_size"],
        "old_extent_lba": item["old_extent_lba"],
        "new_extent_lba": item["new_extent_lba"],
        "write_method": item["write_method"],
        "relocation_reason": item["relocation_reason"],
        "source_sha256": item["source_sha256"],
        "source_path": str(item["source_path"]),
    } for item in plan]


def inspect_input_iso(input_iso: Path, executable_path: Optional[Path] = None,
                      strict_original_reference: bool = True) -> dict:
    """Inspect ISO targets, optionally validating a separate executable source.

    When executable_path is supplied, it is the executable the patcher will edit
    and insert into the ISO. It must be the exact supported pristine SLUS_214.23.
    The ISO's boot-executable path is located from SYSTEM.CNF, but its current
    executable bytes are not used as the patch source.
    """
    iso = Iso9660Image(Path(input_iso))
    targets = iso.inspect_targets(validate_embedded_executable=(executable_path is None))
    # Reference lookup resolves ORIGINAL station values and physical locations.
    # It must not infer stock lengths from modded ISO entries.
    original_lookup = locate_original_values(
        iso, strict_stock=strict_original_reference,
        validate_executable=(executable_path is None),
    )
    input_executable_sha256 = None
    if executable_path is not None:
        executable_path = Path(executable_path)
        if not executable_path.is_file():
            raise ValueError(f"Input executable not found: {executable_path}")
        input_executable_sha256 = patcher.validate_original_executable(executable_path.read_bytes())["sha256"]
    stations = []
    for key, filename in patcher.STATIONS:
        entry: IsoEntry = targets["station_records"][key]
        duration = duration_ms_from_size(entry.size, entry.path)
        stations.append({
            "key": key,
            "filename": filename,
            "iso_path": entry.path,
            "size_bytes": entry.size,
            "duration_ms": duration,
            "duration_text": f"{duration // 60000:02d}:{(duration % 60000) // 1000:02d}.{duration % 1000:03d}",
        })
    return {
        "input_iso": str(Path(input_iso).resolve()),
        "size_bytes": iso.size,
        "pvd_volume_sectors": iso.volume_sectors,
        "boot_executable_path": targets["boot_executable_path"],
        "executable_sha256": input_executable_sha256 or targets["executable_sha256"],
        "input_executable": str(Path(executable_path).resolve()) if executable_path is not None else None,
        "embedded_executable_sha256": targets.get("embedded_executable_sha256", targets["executable_sha256"]),
        "embedded_executable_matches_supported": targets.get("embedded_executable_matches_supported", True),
        "stations": stations,
        "original_station_patch_lookup": original_lookup.as_dict(),
        "stock_station_reference_verified": strict_original_reference,
    }


def build_duration_aware_iso(
    input_iso: Path,
    replacement_dir: Optional[Path],
    output_iso: Path,
    report_path: Optional[Path] = None,
    progress: Progress = None,
    executable_path: Optional[Path] = None,
    adapt_offset_sequence: bool = True,
    offset_mode: Optional[str] = None,
    expected_preview_metrics: Optional[Dict[str, Dict[str, int]]] = None,
    strict_original_reference: bool = True,
) -> dict:
    """Build an LCS ISO with independently selectable authenticated EE and IOP fixes.

    Combined modes modify the original boot ELF plus original CDSTREAM.IRX when
    safe-seek arithmetic requires a shift change. Native mode preserves both.
    Staged file audits and exact preview metrics protect against input changes.
    """
    input_iso = Path(input_iso)
    output_iso = Path(output_iso)
    if input_iso.resolve() == output_iso.resolve():
        raise ValueError("Output ISO must differ from input ISO; the source image is never modified")
    if report_path:
        report_path = Path(report_path)
        blocked_report_paths = {input_iso.resolve(), output_iso.resolve()}
        if executable_path is not None:
            blocked_report_paths.add(Path(executable_path).resolve())
        if report_path.resolve() in blocked_report_paths:
            raise ValueError("JSON report must not overwrite the input ISO, input executable or output ISO")
    if executable_path is not None:
        executable_path = Path(executable_path)
        if not executable_path.is_file():
            raise ValueError(f"Input executable not found: {executable_path}")
        if executable_path.resolve() in (input_iso.resolve(), output_iso.resolve()):
            raise ValueError("Input executable must be a separate file from the ISO images")
    if output_iso.exists() and output_iso.is_dir():
        raise ValueError(f"Output path is a directory: {output_iso}")
    if replacement_dir is not None:
        replacement_dir = Path(replacement_dir)
        if not (replacement_dir.is_dir() or (replacement_dir.is_file() and replacement_dir.suffix.upper() == '.VB')):
            raise ValueError(f"Replacement audio folder or .VB file not found: {replacement_dir}")
        replacement_candidates = ([replacement_dir.resolve()] if replacement_dir.is_file() else
                                  [p.resolve() for p in replacement_dir.iterdir() if p.is_file()])
        if output_iso.resolve() in replacement_candidates:
            raise ValueError("Output ISO path conflicts with a source replacement file")
        if report_path and report_path.resolve() in replacement_candidates:
            raise ValueError("JSON report path conflicts with a source replacement file")

    if progress:
        progress("Reading ISO9660 filesystem and locating the boot executable…")
    iso = Iso9660Image(input_iso)
    # If a loose executable is supplied, validate and patch that file instead of
    # requiring the executable currently embedded in the ISO to be pristine.
    targets = iso.inspect_targets(validate_embedded_executable=(executable_path is None))
    boot_path = targets["boot_executable_path"]
    station_records: Dict[str, IsoEntry] = targets["station_records"]
    if executable_path is not None:
        input_exe_meta = patcher.validate_original_executable(executable_path.read_bytes())
        source_executable_sha256 = input_exe_meta["sha256"]
        if progress:
            progress(f"Validated selected executable: {executable_path} ({source_executable_sha256})")
    else:
        source_executable_sha256 = targets["executable_sha256"]

    # Distinguish validated filename/IRX-layout facts from a *proven* IOP runtime
    # seek range. No speculative IRX binary patches are performed here.
    iop_module_report = None
    build_warnings: List[str] = []
    cdstream_iso_path = "MODULES/CDSTREAM.IRX"
    try:
        iop_module_report = audit_cdstream_module(iso.read_entry_bytes(cdstream_iso_path))
    except (ValueError, OSError) as exc:
        build_warnings.append(f"IOP_MODULE_NOT_VALIDATED: {exc}")
        if progress:
            progress(f"WARNING: unable to validate CDSTREAM.IRX radio file table: {exc}")

    replacement_lookup = {}
    if replacement_dir is not None:
        recognized = {filename for _key, filename in patcher.STATIONS}
        candidates = [replacement_dir] if replacement_dir.is_file() else replacement_dir.iterdir()
        for candidate in candidates:
            if candidate.is_file():
                upper_name = candidate.name.upper()
                if upper_name.endswith(".VB") and upper_name not in recognized:
                    raise ValueError(f"Unrecognized station .VB file: {candidate.name}. Expected one of: {', '.join(sorted(recognized))}")
                if upper_name in replacement_lookup:
                    raise ValueError(f"Ambiguous case-insensitive duplicate filename: {candidate.name} and {replacement_lookup[upper_name].name}")
                replacement_lookup[upper_name] = candidate

    # One calculator derives all 10 EE boundary values and both IOP opcodes
    # directly from the incoming bytes + authentic stock ISO reference sizes.
    original_sizes = {key: station_records[key].size for key, _ in patcher.STATIONS}
    replacement_sizes = {
        key: replacement_lookup[filename.upper()].stat().st_size
        for key, filename in patcher.STATIONS if filename.upper() in replacement_lookup
    }
    # The reference-finder knows the original ISO records and writer sites.
    # The calculator knows only the math; the writer must consume its result.
    original_lookup = locate_original_values(
        iso, strict_stock=strict_original_reference,
        validate_executable=(executable_path is None),
    )
    if original_lookup.original_sizes != original_sizes:
        raise AssertionError('ISO target records differ from original reference lookup')
    calculated = original_lookup.calculate(replacement_sizes)
    selected_build_mode = offset_mode.strip().lower() if offset_mode is not None else ('shared' if adapt_offset_sequence else 'stock')
    validate_mode_against_calculated_iop_requirements(calculated, selected_build_mode)
    write_manifest = make_write_manifest(original_lookup, calculated, selected_build_mode)
    calculated_by_station = {row.station: row for row in calculated.stations}
    if expected_preview_metrics is not None:
        for row in calculated.stations:
            preview = expected_preview_metrics.get(row.station)
            if not preview or int(preview.get('size_bytes', -1)) != row.installed_size or int(preview.get('duration_ms', -1)) != row.ee_boundary_ms:
                raise ValueError(f'{row.station}: calculator values changed after GUI preview. Refresh inputs before building.')
    if progress:
        progress(f'Automatic VB calculator: {len(replacement_sizes)} replacement(s), '
                 f'IOP extra shift {calculated.iop_extra_shift}, '
                 f'largest file {calculated.largest_installed_size:,} bytes.')

    with tempfile.TemporaryDirectory(prefix="lcs_duration_iso_") as tmp_name:
        temp = Path(tmp_name)
        stage_dir = temp / "radio_stage"
        stage_dir.mkdir()
        vb_paths: Dict[str, Path] = {}
        station_sources: Dict[str, dict] = {}
        iso_replacements: Dict[str, Path] = {}
        vb_audio_structure_reports: Dict[str, dict] = {}
        optional_radio_assets: List[dict] = []
        # No new GUI steps or prompts: recognized companions in the selected
        # VB folder are installed automatically, byte-for-byte. Both files
        # must already have a real ISO9660 destination; never create a
        # mismatched directory record or silently discard a supplied asset.
        if replacement_dir is not None and replacement_dir.is_dir():
            for filename, destination in OPTIONAL_RADIO_ASSETS.items():
                companion = replacement_lookup.get(filename)
                if companion is None:
                    continue
                destination_entry = iso.get(destination)
                if destination_entry.is_directory or destination_entry.is_multi_extent:
                    raise ValueError(f"Invalid ISO destination for {filename}: {destination}")
                if companion.stat().st_size <= 0:
                    raise ValueError(f"Optional radio asset is empty: {companion}")
                iso_replacements[destination] = companion
                optional_radio_assets.append({
                    "filename": filename,
                    "iso_path": destination,
                    "source_path": str(companion.resolve()),
                    "source_size_bytes": companion.stat().st_size,
                })
        for key, filename in patcher.STATIONS:
            replacement = replacement_lookup.get(filename.upper())
            if replacement is not None:
                # Validate the *source* before making a padded working copy.
                original_size, _ = patcher.duration_ms_from_raw_stereo_psx_adpcm(replacement)
                diagnostic = audit_vb(replacement, full_scan=True)
                vb_audio_structure_reports[key] = diagnostic
                stage_path = replacement
                pad = None
                if original_size < READ_CHUNK_BYTES:
                    stage_path = stage_dir / filename
                    pad = stage_audio_with_short_stream_guard(replacement, stage_path)
                    # Also validate the exact installed padded stream.
                    padded_audit = audit_vb(stage_path, full_scan=True)
                    vb_audio_structure_reports[key]['installed_padded_audit'] = padded_audit
                    build_warnings.append(
                        f"{filename}: {original_size:,}-byte stream was extended to "
                        f"{pad['installed_size_bytes']:,} bytes with valid silent ADPCM "
                        "because the original IOP loop reads up to 0x40000 bytes at a time."
                    )
                    if progress:
                        progress(f"Short-file buffer guard: padded {filename} from "
                                 f"{original_size:,} to {pad['installed_size_bytes']:,} bytes in staging only.")
                station_sources[key] = {
                    "source": "replacement folder", "path": str(replacement),
                    "installed": True, "source_size_bytes": original_size,
                    "stream_guard": pad,
                }
                iso_replacements[f"{STATION_ISO_PREFIX}/{filename}"] = stage_path
                for warning in diagnostic["warnings"]:
                    build_warnings.append(f"{filename}: {warning}")
                    if progress:
                        progress(f"WARNING: {filename}: {warning}")
            else:
                stage_path = stage_dir / filename
                if progress:
                    progress(f"Using authenticated ISO size for unchanged {filename} (no audio extraction)…")
                # Only the BYTE LENGTH of an unmodified station is consumed by
                # the EE/IOP calculator. Its contents are never an ISO input:
                # the original ISO extent remains in place. A sparse metadata
                # proxy avoids needlessly extracting hundreds of MB of stock
                # audio merely so the executable writer can stat() a path.
                # Keep it out of iso_replacements, by construction.
                with stage_path.open('xb') as proxy:
                    proxy.truncate(station_records[key].size)
                if f"{STATION_ISO_PREFIX}/{filename}" in iso_replacements:
                    raise AssertionError(f'Unchanged {filename} was accidentally scheduled as a replacement')
                station_sources[key] = {"source": "original ISO", "path": station_records[key].path, "installed": False,
                                        "source_size_bytes":station_records[key].size, "stream_guard":None,
                                        "staged_metadata_proxy_only": True}
            actual_size, actual_duration_ms = patcher.duration_ms_from_raw_stereo_psx_adpcm(stage_path)
            station_calculation = calculated_by_station[key]
            if (actual_size, actual_duration_ms) != (station_calculation.installed_size, station_calculation.ee_boundary_ms):
                raise ValueError(f'{filename} changed during preparation: actual stream bytes disagree with automatic calculation')
            if expected_preview_metrics is not None:
                preview = expected_preview_metrics.get(key)
                if preview is None:
                    raise ValueError(
                        f"No GUI preview measurements were supplied for {filename}; rerun input validation before building."
                    )
                preview_size = int(preview.get("size_bytes", -1))
                preview_duration = int(preview.get("duration_ms", -1))
                if preview_size != actual_size or preview_duration != actual_duration_ms:
                    raise ValueError(
                        f"{filename} changed after GUI preview: preview was {preview_size:,} bytes / "
                        f"{preview_duration:,} ms, but build reads {actual_size:,} bytes / "
                        f"{actual_duration_ms:,} ms. Refresh/validate inputs before rebuilding."
                    )
                station_sources[key]["gui_preview_size_bytes"] = preview_size
                station_sources[key]["gui_preview_duration_ms"] = preview_duration
            vb_paths[key] = stage_path

        exe_temp = temp / "SLUS_214.23.original"
        patched_exe = temp / "SLUS_214.23.patched"
        if executable_path is None:
            iso.extract_entry(boot_path, exe_temp)
            executable_source_for_patch = exe_temp
            if progress:
                progress("Using the executable extracted from the input ISO…")
        else:
            executable_source_for_patch = executable_path
            if progress:
                progress("Using the selected standalone executable as the patch source…")
        if progress:
            progress("Checking native IOP duration provider and selected patch mode…")
        if offset_mode is not None and offset_mode.strip().lower() in PRISTINE_SOURCE_MODES:
            # Verified CDSTREAM.IRX native handler 0x1FDC obtains sceCdlFILE.size
            # from sceCdSearchFile and returns floor(size_bytes * 7 / 256).
            # The safest control build installs only the replacement audio:
            # preserve the source ISO boot ELF and *all* of its bytes.
            original_irx_sha256 = SUPPORTED_CDSTREAM_SHA256
            if not iop_module_report or iop_module_report.get("sha256") != original_irx_sha256:
                raise ValueError(
                    "Native-duration mode requires the original verified CDSTREAM.IRX "
                    "inside the input ISO. Missing or changed streaming modules "
                    "could use a different duration provider; refusing to assume its behavior."
                )
            source_sha = targets.get("embedded_executable_sha256", targets["executable_sha256"])
            if source_sha != patcher.SUPPORTED_SHA256:
                raise ValueError(
                    "Native-duration mode requires the ORIGINAL unmodified SLUS_214.23 "
                    "already embedded in the input ISO. This ISO contains a different "
                    "executable; native mode cannot silently preserve previously "
                    "patched radio logic. Restore a pristine source ISO first."
                )
            effective_offset_mode = offset_mode.strip().lower()
            if effective_offset_mode in COMBINED_EE_MODES:
                selected_ee_mode = COMBINED_EE_MODES[effective_offset_mode]
                exe_report = patcher.build_patched_executable(
                    executable_source_for_patch,
                    vb_paths,
                    patched_exe,
                    offset_mode=selected_ee_mode,
                )
                # Fail closed: it is not a combined build unless the EE getter
                # and calculated ten-station durations were actually patched.
                if exe_report.get("length_getter_modified") is not True:
                    raise AssertionError("Combined mode did not patch the EE duration getter")
                if progress:
                    progress(f"Combined mode: patched the EE executable ({selected_ee_mode} offsets); now patching IOP seeks.")
            else:
                exe_report = {
                    "status": "ORIGINAL_EXECUTABLE_PRESERVED_BYTES_IDENTICAL",
                "input_sha256": source_sha,
                "output_sha256": source_sha,
                "length_getter_va": "0x00115158",
                "native_iop_handler_irx_offset": "0x1FDC",
                "native_duration_formula": "(sceCdSearchFile.size * 7) >> 8",
                "length_getter_modified": False,
                "boundary_values_written": {},
                "offset_mode": effective_offset_mode,
                "offset_sequence_changed": False,
                "offset_sequence_strategy": {
                    "mode": effective_offset_mode, "enabled": False,
                    "stock_entry_count_available_to_runtime": len(patcher.STOCK_OFFSET_VALUES)-1,
                    "note": "No executable code or tables changed. Stock behavior intact.",
                },
                "stations": [
                    {
                        "station": filename, "source_file": str(vb_paths[key]),
                        "size_bytes": vb_paths[key].stat().st_size,
                        "duration_ms": patcher.duration_ms_from_raw_stereo_psx_adpcm(vb_paths[key])[1],
                        "boundary_value_written": None,
                        "boundary_unit": "milliseconds read by original IOP handler from ISO file size",
                    }
                    for key, filename in patcher.STATIONS
                ],
            }
            if progress and effective_offset_mode not in COMBINED_EE_MODES:
                progress(f"{effective_offset_mode} mode: preserving SLUS_214.23 byte-for-byte; all IRX edits are separately validated.")
        elif offset_mode is None:
            exe_report = patcher.build_patched_executable(
                executable_source_for_patch,
                vb_paths,
                patched_exe,
                adapt_offset_sequence=adapt_offset_sequence,
            )
            effective_offset_mode = "shared" if adapt_offset_sequence else "stock"
        else:
            effective_offset_mode = offset_mode.strip().lower()
            exe_report = patcher.build_patched_executable(
                executable_source_for_patch,
                vb_paths,
                patched_exe,
                offset_mode=effective_offset_mode,
            )
        # Prove the patched boundary is the exact duration shown in the GUI preview.
        for station in exe_report.get("stations", []):
            key = next((k for k, fn in patcher.STATIONS if fn == Path(station["source_file"]).name), None)
            if key is not None:
                station["source_file"] = station_sources[key]["path"]
                station["source_kind"] = station_sources[key]["source"]
                expected_ms = int(station["duration_ms"])
                if expected_ms != calculated_by_station[key].ee_boundary_ms:
                    raise AssertionError(f'ELF duration differs from calculated VB plan for {key}')
                if effective_offset_mode not in ("native", "iop-safe-seek") and int(station.get("boundary_value_written", -1)) != expected_ms:
                    raise ValueError(
                        f"Internal boundary check failed for {station['station']}: +0x18 does not match calculated milliseconds."
                    )
                if expected_preview_metrics is not None:
                    preview = expected_preview_metrics.get(key, {})
                    if int(preview.get("duration_ms", -1)) != expected_ms:
                        raise ValueError(
                            f"GUI preview mismatch for {station['station']}: preview calculated "
                            f"{preview.get('duration_ms')} ms but patcher calculated {expected_ms} ms."
                        )
                    if effective_offset_mode not in ("native", "iop-safe-seek"):
                        station_sources[key]["boundary_value_written_ms"] = expected_ms
        # Check the REAL executable bytes from the writer, not just its report.
        if write_manifest['executable_getter_replaced']:
            verify_executable_slots(patched_exe.read_bytes(), write_manifest)
        iop_seek_report = None
        if effective_offset_mode in IOP_PATCH_MODES:
            # Checked against the exact pristine IRX hash above. The native
            # time and 10-station size map remain unchanged: only seek scaling
            # instructions are modified. ELF file length is exactly preserved.
            stream_sizes = [vb_paths[key].stat().st_size for key, _ in patcher.STATIONS]
            corrected_irx, iop_seek_report = patch_cdstream(
                iso.read_entry_bytes(cdstream_iso_path), stream_sizes)
            if iop_seek_report['extra_shift'] != calculated.iop_extra_shift:
                raise AssertionError('IOP patch shift disagrees with automatic VB calculator')
            if iop_seek_report['extra_shift'] > 0:
                actual_ops = [f'0x{__import__("struct").unpack_from("<I", corrected_irx, 0xA0 + pos)[0]:08X}'
                              for pos in (0x1538, 0x172C)]
                if actual_ops != calculated.as_dict()['iop_calculated_words']:
                    raise AssertionError('Actual IOP instructions disagree with automatic calculator')
            if iop_seek_report['duration_overflow_fix_required'] != calculated.iop_duration_overflow_fix_required:
                raise AssertionError('IOP duration overflow patch differs from automatic calculator')
            if iop_seek_report['position_overflow_fix_required'] != calculated.iop_position_overflow_fix_required:
                raise AssertionError('IOP position overflow patch differs from automatic calculator')
            if corrected_irx != iso.read_entry_bytes(cdstream_iso_path):
                corrected_irx_path = temp / "CDSTREAM.IRX.calculated"
                corrected_irx_path.write_bytes(corrected_irx)
                if hashlib.sha256(corrected_irx_path.read_bytes()).hexdigest() != iop_seek_report["output_sha256"]:
                    raise IOError("IOP patch staging hash mismatch")
                iso_replacements[cdstream_iso_path] = corrected_irx_path
            if progress:
                progress(f"IOP 32-bit seek multiplication fix: extra shift {iop_seek_report['extra_shift']}, "
                         f"maximum signed product {iop_seek_report['maximum_signed_multiplication']:,}.")
        if effective_offset_mode not in ("native", "iop-safe-seek"):
            iso_replacements[boot_path] = patched_exe
        if progress:
            progress("Creating the output ISO and updating ISO9660 file records…")
        if output_iso.resolve() == input_iso.resolve():
            raise ValueError("Output ISO must differ from input ISO")
        # Re-read the already-staged image and check every destination BEFORE
        # the atomic publication of the new output ISO.
        pristine_irx = iso.read_entry_bytes(cdstream_iso_path)
        def check_all_written_locations(staged_image: Iso9660Image) -> None:
            verify_output_locations(
                staged_image, original_lookup, calculated,
                write_manifest, pristine_irx,
                expected_ee_sha256=(hashlib.sha256(patched_exe.read_bytes()).hexdigest()
                                   if write_manifest['executable_getter_replaced'] else None),
            )
        iso_write_report = apply_iso_replacements(
            input_iso, output_iso, iso_replacements, progress,
            post_verify=check_all_written_locations,
        )
        if effective_offset_mode in COMBINED_EE_MODES:
            replaced_paths = {x["iso_path"] for x in iso_write_report}
            must_replace = {boot_path}
            if iop_seek_report and (iop_seek_report["extra_shift"] or iop_seek_report['duration_overflow_fix_required'] or iop_seek_report['position_overflow_fix_required']):
                must_replace.add(cdstream_iso_path)
            if not must_replace.issubset(replaced_paths):
                raise AssertionError(
                    "Combined mode did not include its required binary changes: "
                    f"required={sorted(must_replace)}, observed={sorted(replaced_paths)}"
                )

        report = {
            "status": "ISO_CREATED_STATICALLY_NOT_EMULATOR_TESTED",
            "automatic_patch_calculator": calculated.as_dict(),
            "original_station_patch_lookup": original_lookup.as_dict(),
            "location_aware_write_manifest": write_manifest,
            "write_destination_verification": "PASSED_ON_STAGED_ISO_BEFORE_PUBLICATION",
            "stock_station_reference_verified": strict_original_reference,
            "input_iso": str(input_iso.resolve()),
            "input_iso_size_bytes": input_iso.stat().st_size,
            "output_iso": str(output_iso.resolve()),
            "output_iso_size_bytes": output_iso.stat().st_size,
            "boot_executable_path": boot_path,
            "source_executable": str(executable_path.resolve()) if executable_path is not None else f"{input_iso.resolve()}::{boot_path}",
            "source_executable_sha256": source_executable_sha256,
            "iop_cdstream_module_structure_audit": iop_module_report,
            "replacement_vb_adpcm_structure_audit": vb_audio_structure_reports,
            "automatically_installed_radio_companion_assets": [
                {**asset, "verified_output_sha256": next(
                    item["source_sha256"] for item in iso_write_report
                    if item["iso_path"] == asset["iso_path"]
                )} for asset in optional_radio_assets
            ],
            "stream_compatibility_warnings": build_warnings,
            "changes_to_iop_modules": ("Calculated and authenticated seek and/or duration instructions in CDSTREAM.IRX" if iop_seek_report and (iop_seek_report['extra_shift'] or iop_seek_report['duration_overflow_fix_required'] or iop_seek_report['position_overflow_fix_required']) else "none (no IOP change required by input sizes)"),
            "iop_seek_patch_report": iop_seek_report,
            "embedded_executable_sha256_before_build": targets.get("embedded_executable_sha256", targets["executable_sha256"]),
            "external_executable_selected": executable_path is not None,
            "duration_unit_assumption": "milliseconds (proved by CDSTREAM.IRX 0x1FDC reading sceCdlFILE.size after sceCdSearchFile)",
            "playback_boundary_unit": "milliseconds (native IOP handler confirmed by static instruction trace)",
            "boundary_values_written_ms": exe_report.get("boundary_values_written", {}),
            "gui_preview_values_verified": expected_preview_metrics is not None,
            "adaptive_offset_sequence_enabled": effective_offset_mode in ("shared", "station-specific", "iop-elf-shared", "iop-elf-station-specific"),
            "offset_mode": effective_offset_mode,
            "combined_executable_and_iop_patch": effective_offset_mode in COMBINED_EE_MODES,
            "ee_offset_mode": COMBINED_EE_MODES.get(effective_offset_mode, effective_offset_mode),
            "iso_files_replaced": iso_write_report,
            "executable_patch_report": exe_report,
            "stations": [
                {
                    "station": filename,
                    "source_kind": station_sources[key]["source"],
                    "source": station_sources[key]["path"],
                    "installed_into_iso": station_sources[key]["installed"],
                    "original_source_size_bytes": station_sources[key]["source_size_bytes"],
                    "short_stream_guard": station_sources[key]["stream_guard"],
                    "file_size_bytes": patcher.duration_ms_from_raw_stereo_psx_adpcm(vb_paths[key])[0],
                    "duration_ms": patcher.duration_ms_from_raw_stereo_psx_adpcm(vb_paths[key])[1],
                    "boundary_value_written_ms": None if effective_offset_mode in ("native", "iop-safe-seek") else patcher.duration_ms_from_raw_stereo_psx_adpcm(vb_paths[key])[1],
                    "gui_preview_verified": expected_preview_metrics is not None,
                    "gui_preview_size_bytes": (expected_preview_metrics[key]["size_bytes"] if expected_preview_metrics is not None else None),
                    "gui_preview_duration_ms": (expected_preview_metrics[key]["duration_ms"] if expected_preview_metrics is not None else None),
                }
                for key, filename in patcher.STATIONS
            ],
            "limitations": [
                "Prototype supports ISO9660 raw 2048-byte-sector images whose SYSTEM.CNF resolves a boot executable path, and a standalone input executable that matches the exact supported SLUS_214.23 build (unless using the legacy embedded-executable mode).",
                "Only named station VB files and optional ENGLISH.GXT/GAME.DTZ companions found in the replacement folder are installed; all other ISO files remain unchanged.",
                "Oversized replacements are appended at the end of the ISO; their directory record extents/sizes and PVD volume-space size are updated.",
                "IOP seek patch corrects 32-bit interpolation multiplication only when scaling is needed. Files shorter than 256 KiB are staged with silent ADPCM padding to avoid an identified wrap-buffer read-range risk; no arbitrary IOP read-loop patch is injected.",
                "Each replacement .VB is fully checked for plausible headerless PSX ADPCM frames; this does not decode/listen to the whole song or guarantee interleaving is correct.",
                "A replacement exceeding 65,535 CD sectors is flagged as a 16-bit-addressing investigation target; this is not an established 16-bit IOP limit.",
                "Native IOP length calculation is verified from CDSTREAM.IRX static disassembly. IOP-safe-seek/combined branches have offline differential tests but no confirmed uninterrupted PCSX2 playback.",
                "Shared mode builds a modulo-alias-aware table with maximin ordering and a cursor-cycle count compatible with the ten-station stride. Station-specific mode builds ten independent maximin offset sequences/cursors and injects a MIPS helper that skips the empty terminal-zero event. Both adaptive modes require target PS2 loader/PCSX2 validation; neither guarantees that final runtime positions never repeat or that playback stalls are eliminated.",
                "Static creation is not proof of boot/playback compatibility; test only on a disposable ISO and keep backups.",
            ],
        }
        if report_path:
            try:
                atomic_write_report(report_path, json.dumps(report, indent=2) + "\n")
                report["report_path"] = str(report_path.resolve())
            except OSError as exc:
                # The ISO has already been atomically published and verified.
                # Do not misreport that successful build as a total failure.
                report["report_write_error"] = f"ISO built successfully, but JSON report could not be saved: {exc}"
                if progress:
                    progress("WARNING: " + report["report_write_error"])
        return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a duration-aware LCS PS2 ISO from a supported source ISO")
    parser.add_argument("input_iso", type=Path)
    parser.add_argument("--executable", type=Path, help="Optional standalone SLUS_214.23 executable to patch and insert into the ISO")
    parser.add_argument("--radio-dir", type=Path, help="Folder with replacement .VB files and optional ENGLISH.GXT / GAME.DTZ companions")
    parser.add_argument("--out", required=True, type=Path, help="Output ISO path; must differ from source ISO")
    parser.add_argument("--report", type=Path, help="Optional JSON report path")
    parser.add_argument(
        "--offset-mode", choices=("native", *IOP_PATCH_MODES, *patcher.OFFSET_MODES), default="iop-elf-stock",
        help="Combined EE+IOP modes, IOP-only control, native control, or legacy EE-only modes",
    )
    parser.add_argument(
        "--keep-stock-offset-sequence", action="store_true",
        help="Compatibility alias for --offset-mode stock",
    )
    args = parser.parse_args()
    try:
        selected_mode = "stock" if args.keep_stock_offset_sequence else args.offset_mode
        report = build_duration_aware_iso(
            args.input_iso, args.radio_dir, args.out, args.report, print, args.executable,
            offset_mode=selected_mode,
        )
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
