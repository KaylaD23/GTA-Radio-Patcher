#!/usr/bin/env python3
"""Experimental duration-aware patcher for North American GTA LCS PS2 SLUS_214.23.

This is a BUILD-TIME patch: the tool reads the final, converted radio .VB files,
calculates their PSX ADPCM playback durations, and embeds a ten-entry duration
array in the main executable. The game then receives the per-station duration
when its existing radio-length getter is called.

Important: the duration unit is currently based on the leading millisecond
hypothesis (supported by comparative GTA III/VC code and the LCS position math,
but not yet directly confirmed with a nonzero LCS PS2 runtime capture). Use an
untouched backup and test in a disposable ISO. This does not patch CDSTREAM.IRX.
"""
from __future__ import annotations

import argparse
from bisect import bisect_left, insort
import hashlib
import json
import math
import os
import tempfile
import struct
import sys
from pathlib import Path
from typing import Dict, List, Tuple

SUPPORTED_SHA256 = "65bc2ec3d8a1b9f8482fb7e69427f2539e7e7b49d9dd262bf31aa8aa54ec53b2"
EXPECTED_SIZE = 2_994_708

# Exact SLUS_214.23 ELF32 little-endian MIPS segment and insertion gap.
ELF32_EHDR_PHOFF_OFFSET = 0x1C
ELF32_EHDR_PHENTSIZE_OFFSET = 0x2A
ELF32_EHDR_PHNUM_OFFSET = 0x2C
ELF32_EHDR_E_MACHINE_OFFSET = 0x12
EXPECTED_PHOFF = 0x34
EXPECTED_PHENTSIZE = 32
EXPECTED_PHNUM = 1
PT_LOAD = 1
EXPECTED_SEGMENT_OFFSET = 0x1000
EXPECTED_SEGMENT_VADDR = 0x00100000
EXPECTED_SEGMENT_FILESZ = 0x002D93C4
EXPECTED_SEGMENT_MEMSZ = 0x00573168
NEW_SEGMENT_FILESZ = EXPECTED_SEGMENT_FILESZ + 40

# The file-backed load segment ends at VA 0x003D93C4; .sbss starts at
# VA 0x003D9400. Its 60-byte alignment gap can hold the 10-u32 duration table.
LENGTH_TABLE_VA = 0x003D93C4
SBSS_START_VA = 0x003D9400
LENGTH_TABLE_BYTES = 10 * 4

# The stock random-offset table is in .rodata. The code increments its cursor
# BEFORE reading an entry, so word 0 is skipped and a zero terminator ends the
# sequence. Expanding the table in place would overwrite live strings/data.
STOCK_OFFSET_TABLE_VA = 0x003AC908
STOCK_OFFSET_TABLE_FILE_OFFSET = EXPECTED_SEGMENT_OFFSET + (STOCK_OFFSET_TABLE_VA - EXPECTED_SEGMENT_VADDR)
# Keep this legacy name as an alias for the original table location for report
# consumers; adaptive builds now point at a relocated table in a second PT_LOAD.
OFFSET_TABLE_VA = 0x00680000
OFFSET_TABLE_LOAD_VA = OFFSET_TABLE_VA
OFFSET_TABLE_ALIGNMENT = 0x1000
# Station-specific mode injects a tiny MIPS helper into the same PT_LOAD as
# its descriptor/cursor data. That segment must be writable for cursors and
# executable for the helper; this remains experimental until emulator-tested.
OFFSET_HELPER_SEGMENT_FLAGS = 7  # PF_R | PF_W | PF_X
OFFSET_DATA_SEGMENT_FLAGS = 4    # PF_R only for shared table mode
OFFSET_HOOK_VA = 0x001676E0
OFFSET_HOOK_FILE_OFFSET = EXPECTED_SEGMENT_OFFSET + (OFFSET_HOOK_VA - EXPECTED_SEGMENT_VADDR)
EXPECTED_OFFSET_HOOK_WORDS = (0x00771821, 0x00438021)  # addu v1,v1,s7 ; addu s0,v0,v1
EXPECTED_OFFSET_HOOK_BYTES = struct.pack("<2I", *EXPECTED_OFFSET_HOOK_WORDS)
OFFSET_MODES = ("shared", "station-specific", "stock")
OFFSET_TABLE_TARGET_SPACING_MS = 5_000
OFFSET_TABLE_MIN_ENTRIES = 64
OFFSET_TABLE_MAX_ENTRIES = 4096
STOCK_OFFSET_VALUES = (
    1_000_000, 15_000_000, 22_000_000, 6_000_000, 19_000_000,
    33_000_000, 18_000_000, 26_000_000, 2_000_000, 14_000_000,
    29_000_000, 10_000_000, 20_000_000, 34_000_000, 4_000_000,
    17_000_000, 25_000_000, 31_000_000, 8_000_000, 16_000_000,
    5_000_000, 24_000_000, 30_000_000, 7_000_000, 11_000_000,
    21_000_000, 12_000_000, 3_000_000, 23_000_000,
)
STOCK_OFFSET_TABLE_ENTRIES = len(STOCK_OFFSET_VALUES)
STOCK_OFFSET_TABLE_BYTES = (STOCK_OFFSET_TABLE_ENTRIES + 1) * 4
EXPECTED_OFFSET_TABLE_BYTES = struct.pack(
    f"<{STOCK_OFFSET_TABLE_ENTRIES + 1}I", *STOCK_OFFSET_VALUES, 0
)
# The table pointer is built in the initializer at 0x0016755C/0x00167564:
# lui $5,0x3b; addiu $5,$5,-0x36f8 => 0x003AC908.
OFFSET_PTR_HI_VA = 0x0016755C
OFFSET_PTR_LO_VA = 0x00167564
OFFSET_PTR_HI_FILE_OFFSET = EXPECTED_SEGMENT_OFFSET + (OFFSET_PTR_HI_VA - EXPECTED_SEGMENT_VADDR)
OFFSET_PTR_LO_FILE_OFFSET = EXPECTED_SEGMENT_OFFSET + (OFFSET_PTR_LO_VA - EXPECTED_SEGMENT_VADDR)
EXPECTED_OFFSET_PTR_HI = struct.pack("<I", 0x3C05003B)  # lui $5,0x003b
EXPECTED_OFFSET_PTR_LO = struct.pack("<I", 0x24A5C908)  # addiu $5,$5,-0x36f8
SECOND_PHDR_OFFSET = EXPECTED_PHOFF + EXPECTED_PHENTSIZE
SECOND_PHDR_SIZE = 32

# VA-to-file mapping for the executable's single PT_LOAD segment.
GETTER_VA = 0x00115158
GETTER_FILE_OFFSET = EXPECTED_SEGMENT_OFFSET + (GETTER_VA - EXPECTED_SEGMENT_VADDR)

# Existing 10-word function body at VA 0x00115158. Exact-byte check protects
# against accidental application to a different executable/revision.
EXPECTED_GETTER_BYTES = bytes.fromhex(
    "f0 ff bd 27 09 00 04 3c 00 00 bf ff 0e 52 04 0c "
    "ff 00 a5 30 27 18 02 00 00 00 bf df 0a 10 03 00 "
    "08 00 e0 03 10 00 bd 27"
)

# MIPS32 words written little-endian. This short replacement:
#  - validates a1 as station index 0..9,
#  - loads duration_table[index],
#  - returns zero for invalid indices,
#  - leaves ra and the stack untouched.
# sltiu v1,a1,10; movz a1,zero,v1; sll a1,a1,2; lui v0,0x003e;
# addiu v0,v0,-0x6c3c; addu v0,v0,a1; lw v0,0(v0); movz v0,zero,v1;
# jr ra; nop
PATCH_WORDS = (
    0x2CA3000A,
    0x0003280A,
    0x00052880,
    0x3C02003E,
    0x244293C4,
    0x00451021,
    0x8C420000,
    0x0003100A,
    0x03E00008,
    0x00000000,
)
PATCH_BYTES = struct.pack("<10I", *PATCH_WORDS)

STATIONS: Tuple[Tuple[str, str], ...] = (
    ("head", "HEAD.VB"),
    ("double", "DOUBLE.VB"),
    ("kjah", "KJAH.VB"),
    ("rise", "RISE.VB"),
    ("lips", "LIPS.VB"),
    ("mundo", "MUNDO.VB"),
    ("msx", "MSX.VB"),
    ("flash", "FLASH.VB"),
    ("lcj", "LCJ.VB"),
    ("lcfr", "LCFR.VB"),
)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def duration_ms_from_raw_stereo_psx_adpcm(path: Path) -> Tuple[int, int]:
    """Return (file_size_bytes, duration_ms) for raw stereo PSX ADPCM.

    Assumes LCS PS2's raw stereo PSX ADPCM stream format at 32 kHz: 16-byte
    ADPCM frames decode to 28 samples/channel, two channels yield 28 stereo
    sample frames per 32 encoded bytes. Duration_ms = bytes * 7 / 256.
    """
    if not path.is_file():
        raise ValueError(f"File not found: {path}")
    size = path.stat().st_size
    if size <= 0:
        raise ValueError(f"Empty audio file: {path}")
    if size % 0x4000 != 0:
        raise ValueError(
            f"{path.name}: size {size:,} is not a multiple of 0x4000. "
            "This patcher expects final raw stereo PSX ADPCM .VB streams with "
            "0x2000 channel interleave; convert/validate the audio first."
        )
    from lcs_iop_safe_seek import MAX_SAFE_DURATION_SIZE
    if size > MAX_SAFE_DURATION_SIZE:
        raise ValueError(f"{path.name}: raw stream exceeds the configured 5-hour limit; no safe patch plan exists")
    numerator = size * 7
    # For >4h39m37s, the original 32-bit IOP getter overflows; the combined
    # builder verifies and installs an in-place overflow-safe getter correction.
    if numerator % 256:
        raise ValueError(f"{path.name}: size does not yield an exact millisecond duration")
    duration_ms = numerator // 256
    if not 0 < duration_ms <= 0xFFFFFFFF:
        raise ValueError(f"{path.name}: duration does not fit a nonzero 32-bit value")
    return size, duration_ms


def validate_original_executable(blob: bytes) -> Dict[str, int | str]:
    if len(blob) != EXPECTED_SIZE:
        raise ValueError(
            f"Executable size mismatch: expected {EXPECTED_SIZE:,}, got {len(blob):,}. "
            "Only the exact reference SLUS_214.23 is supported."
        )
    digest = sha256(blob)
    if digest != SUPPORTED_SHA256:
        raise ValueError(
            "Executable SHA-256 mismatch. Refusing to patch a different build.\n"
            f"Expected: {SUPPORTED_SHA256}\nGot:      {digest}"
        )
    if blob[:4] != b"\x7fELF" or blob[4] != 1 or blob[5] != 1:
        raise ValueError("Expected an ELF32 little-endian executable")
    machine = struct.unpack_from("<H", blob, ELF32_EHDR_E_MACHINE_OFFSET)[0]
    if machine != 8:
        raise ValueError(f"Expected ELF machine MIPS (8), got {machine}")
    phoff = struct.unpack_from("<I", blob, ELF32_EHDR_PHOFF_OFFSET)[0]
    phentsize = struct.unpack_from("<H", blob, ELF32_EHDR_PHENTSIZE_OFFSET)[0]
    phnum = struct.unpack_from("<H", blob, ELF32_EHDR_PHNUM_OFFSET)[0]
    if (phoff, phentsize, phnum) != (EXPECTED_PHOFF, EXPECTED_PHENTSIZE, EXPECTED_PHNUM):
        raise ValueError("Unexpected ELF program-header layout; refusing to patch")
    p = struct.unpack_from("<8I", blob, phoff)
    p_type, p_offset, p_vaddr, _p_paddr, p_filesz, p_memsz, _p_flags, _p_align = p
    if (p_type, p_offset, p_vaddr, p_filesz, p_memsz) != (
        PT_LOAD,
        EXPECTED_SEGMENT_OFFSET,
        EXPECTED_SEGMENT_VADDR,
        EXPECTED_SEGMENT_FILESZ,
        EXPECTED_SEGMENT_MEMSZ,
    ):
        raise ValueError("Unexpected PT_LOAD segment layout; refusing to patch")
    if blob[GETTER_FILE_OFFSET : GETTER_FILE_OFFSET + len(EXPECTED_GETTER_BYTES)] != EXPECTED_GETTER_BYTES:
        raise ValueError("The radio-length getter bytes do not match the expected signature")
    if blob[STOCK_OFFSET_TABLE_FILE_OFFSET : STOCK_OFFSET_TABLE_FILE_OFFSET + STOCK_OFFSET_TABLE_BYTES] != EXPECTED_OFFSET_TABLE_BYTES:
        raise ValueError("The radio random-offset table does not match the expected stock signature")
    if blob[OFFSET_PTR_HI_FILE_OFFSET : OFFSET_PTR_HI_FILE_OFFSET + 4] != EXPECTED_OFFSET_PTR_HI:
        raise ValueError("The random-offset table pointer high instruction does not match the expected signature")
    if blob[OFFSET_PTR_LO_FILE_OFFSET : OFFSET_PTR_LO_FILE_OFFSET + 4] != EXPECTED_OFFSET_PTR_LO:
        raise ValueError("The random-offset table pointer low instruction does not match the expected signature")
    if blob[SECOND_PHDR_OFFSET : SECOND_PHDR_OFFSET + SECOND_PHDR_SIZE] != bytes(SECOND_PHDR_SIZE):
        raise ValueError("Reserved ELF program-header slot is not empty; refusing to add an expanded offset segment")
    table_file_offset = p_offset + p_filesz
    table_end = table_file_offset + LENGTH_TABLE_BYTES
    sbss_file_boundary = p_offset + (SBSS_START_VA - p_vaddr)
    if table_end > sbss_file_boundary:
        raise ValueError("Not enough verified segment padding for the duration table")
    # The inserted table bytes must stay before the first NOBITS section offset.
    if table_end > 0x2DA400:
        raise ValueError("Duration table would overlap the .sbss placeholder boundary")
    return {
        "sha256": digest,
        "phoff": phoff,
        "ph_filesz_offset": phoff + 16,
        "segment_offset": p_offset,
        "segment_vaddr": p_vaddr,
        "old_filesz": p_filesz,
        "new_filesz": p_filesz + LENGTH_TABLE_BYTES,
        "memsz": p_memsz,
        "table_va": p_vaddr + p_filesz,
        "table_file_offset": table_file_offset,
    }


def choose_offset_entry_count(longest_duration_ms: int) -> int:
    """Choose sequence size, targeting one offset per ~5 s.

    The original shared cursor advances *once per rebase*, before the station
    loop. No coprimality adjustment for the ten stations is required.
    """
    if not isinstance(longest_duration_ms, int) or longest_duration_ms <= 0:
        raise ValueError("The longest station duration must be a positive integer")
    desired = math.ceil(longest_duration_ms / OFFSET_TABLE_TARGET_SPACING_MS)
    return max(OFFSET_TABLE_MIN_ENTRIES, min(OFFSET_TABLE_MAX_ENTRIES, desired))


def choose_shared_offset_entry_count(longest_duration_ms: int) -> int:
    """One shared offset choice per rebase (not one read per radio station)."""
    return choose_offset_entry_count(longest_duration_ms)


def _seed_state(seed_text: str) -> int:
    state = int.from_bytes(hashlib.sha256(seed_text.encode("ascii")).digest()[:4], "little") % 2_147_483_647
    return state or 1


def _deterministic_shuffle_indices(count: int, seed_text: str) -> List[int]:
    state = _seed_state(seed_text)
    indices = list(range(count))
    for i in range(count - 1, 0, -1):
        state = (16_807 * state) % 2_147_483_647
        j = state % (i + 1)
        indices[i], indices[j] = indices[j], indices[i]
    return indices


def _circular_distance(a: int, b: int, duration: int) -> int:
    delta = abs((a % duration) - (b % duration))
    return min(delta, duration - delta)


def _multi_duration_distance(a: int, b: int, durations: List[int]) -> int:
    """Worst-case circular distance between two offsets over all station tracks."""
    return min(_circular_distance(a, b, d) for d in durations)


def build_farthest_point_order(
    values: List[int] | Tuple[int, ...],
    durations_ms: List[int] | Tuple[int, ...],
    seed_text: str,
) -> Tuple[int, ...]:
    """Order fixed offset values to maximize distance from all earlier picks.

    This is a deterministic farthest-point traversal. At each step, it selects
    the remaining offset whose nearest circular distance to any already-picked
    offset is greatest; shared mode measures that distance on every station
    timeline and uses the worst station as the score. Ties are deterministic.
    """
    vals = list(values)
    durations = sorted(set(int(d) for d in durations_ms))
    if not vals:
        return tuple()
    if not durations or any(d <= 0 for d in durations):
        raise ValueError("Farthest-point ordering requires positive station durations")
    if len(set(vals)) != len(vals):
        raise ValueError("Farthest-point ordering requires unique offset values")
    priorities = _deterministic_shuffle_indices(len(vals), seed_text + ":ties")
    tie_rank = [0] * len(vals)
    for rank, index in enumerate(priorities):
        tie_rank[index] = rank
    first = priorities[0]
    selected = [first]
    used = {first}
    nearest = [
        _multi_duration_distance(value, vals[first], durations) if i != first else -1
        for i, value in enumerate(vals)
    ]
    while len(selected) < len(vals):
        best_score = max(nearest[i] for i in range(len(vals)) if i not in used)
        next_index = min(
            (i for i in range(len(vals)) if i not in used and nearest[i] == best_score),
            key=lambda i: tie_rank[i],
        )
        selected.append(next_index)
        used.add(next_index)
        chosen_value = vals[next_index]
        for i, value in enumerate(vals):
            if i not in used:
                nearest[i] = min(nearest[i], _multi_duration_distance(value, chosen_value, durations))
    return tuple(vals[i] for i in selected)


def _nearest_circular_gap(value: int, duration: int, sorted_used_residues: List[int]) -> int:
    """Nearest circular gap from value to a sorted set of occupied residues."""
    residue = value % duration
    if residue == 0:
        return 0
    if not sorted_used_residues:
        return duration // 2
    pos = bisect_left(sorted_used_residues, residue)
    if pos < len(sorted_used_residues) and sorted_used_residues[pos] == residue:
        return 0
    prev = sorted_used_residues[pos - 1] if pos > 0 else sorted_used_residues[-1] - duration
    nxt = sorted_used_residues[pos] if pos < len(sorted_used_residues) else sorted_used_residues[0] + duration
    return min(residue - prev, nxt - residue)


def build_shared_offset_values(
    durations_ms: List[int] | Tuple[int, ...],
    count: int,
) -> Tuple[int, ...]:
    """Select one value per longest-track bin and optimize feasible residues.

    Each value stays inside its own evenly-spaced bin on the longest track, but
    can move within that bin to maximize circular separation on each station
    whose duration has enough non-zero residues for all ``count`` values.
    When a station duration is <= count milliseconds, unique residues for every
    table entry are mathematically impossible; that station is excluded from
    the magnitude uniqueness constraint instead of aborting the whole build.
    """
    durations = sorted(set(int(d) for d in durations_ms))
    if not durations or any(d <= 0 for d in durations):
        raise ValueError("Shared offset optimization requires positive durations")
    longest = max(durations)
    if longest <= count:
        raise ValueError("The longest station duration is too short to create unique offset bins")
    # A duration shorter than (or equal to) the requested offset count cannot
    # have a unique non-zero modulo residue for every offset: it has at most
    # duration-1 such residues. Keep those short tracks in the later ordering
    # analysis, but only impose uniqueness while choosing magnitudes for tracks
    # with enough residue capacity. This matters for synthetic inputs and for
    # deliberately short station replacements; it must not abort long-track
    # builds merely because another station is short.
    residue_durations = [d for d in durations if d > count]
    if longest not in residue_durations:
        raise ValueError("The longest station duration is too short for the requested unique offset bins")
    used = {d: [0] for d in residue_durations}  # avoid zero-offset residue where uniqueness is feasible
    chosen: List[int] = []
    for i in range(count):
        bin_start = (i * longest) // count
        bin_end = ((i + 1) * longest) // count - 1
        if i == 0:
            bin_start = max(1, bin_start)
        target = ((2 * i + 1) * longest) // (2 * count)
        width = bin_end - bin_start
        if width < 1:
            raise ValueError("Track duration is too short for the requested offset resolution")
        jitter = max(1, width // 4)
        low = max(bin_start, target - jitter)
        high = min(bin_end, target + jitter)
        # Sample the local bin, including its centre. A 101-point search keeps
        # build time small while letting the calculator avoid bad residue gaps.
        sample_count = min(101, high - low + 1)
        candidates = {target, low, high}
        if sample_count > 1:
            for j in range(sample_count):
                candidates.add(low + ((high - low) * j) // (sample_count - 1))
        candidates = {v for v in candidates if low <= v <= high and v > 0}
        best_value = None
        best_score = -1
        best_centre_distance = None
        for candidate in candidates:
            # Exact residue collisions and zero residues are disallowed; use
            # binary search because the occupied residues remain sorted.
            collision = False
            for d in residue_durations:
                residue = candidate % d
                pos = bisect_left(used[d], residue)
                if residue == 0 or (pos < len(used[d]) and used[d][pos] == residue):
                    collision = True
                    break
            if collision:
                continue
            score = min(_nearest_circular_gap(candidate, d, used[d]) for d in residue_durations)
            centre_distance = abs(candidate - target)
            if (score > best_score or
                (score == best_score and (best_centre_distance is None or centre_distance < best_centre_distance)) or
                (score == best_score and centre_distance == best_centre_distance and candidate < best_value)):
                best_value = candidate
                best_score = score
                best_centre_distance = centre_distance
        if best_value is None:
            # Fall back to a complete integer scan of this bin before failing.
            for candidate in range(low, high + 1):
                collision = False
                for d in residue_durations:
                    residue = candidate % d
                    pos = bisect_left(used[d], residue)
                    if residue == 0 or (pos < len(used[d]) and used[d][pos] == residue):
                        collision = True
                        break
                if collision:
                    continue
                score = min(_nearest_circular_gap(candidate, d, used[d]) for d in residue_durations)
                centre_distance = abs(candidate - target)
                if (score > best_score or
                    (score == best_score and (best_centre_distance is None or centre_distance < best_centre_distance))):
                    best_value, best_score, best_centre_distance = candidate, score, centre_distance
        if best_value is None:
            raise ValueError(
                "Could not find a collision-free shared offset in a timeline bin. "
                "Use Station-specific adaptive mode, which optimizes each radio independently."
            )
        chosen.append(best_value)
        for d in residue_durations:
            insort(used[d], best_value % d)
    if len(set(chosen)) != count:
        raise AssertionError("Shared offset optimization produced duplicate magnitudes")
    return tuple(chosen)


def build_uniform_stride_order(
    values: List[int] | Tuple[int, ...],
    durations_ms: List[int] | Tuple[int, ...],
    seed_text: str,
    *,
    zero_terminator_after: bool = True,
) -> Tuple[int, ...]:
    """Choose a deterministic permutation with a large gap between EACH read.

    Farthest-point order expands early coverage but can bunch late selections
    together. A co-prime modular traversal of time-sorted positions with a
    stride near the golden angle makes every next read jump across the
    timeline, while the first 40 selections cover the whole station.

    For the original shared path there is one read per rebase, not one per
    radio station. Its terminal-zero read is tested separately below.
    """
    import math
    ordered = sorted(int(v) for v in values)
    durations = sorted(set(int(d) for d in durations_ms))
    if not ordered or len(set(ordered)) != len(ordered):
        raise ValueError("Stride traversal requires distinct, nonempty offsets")
    if not durations or any(d <= 0 for d in durations):
        raise ValueError("Stride traversal requires positive durations")
    n = len(ordered)
    if n < 3:
        return tuple(ordered)
    longest = max(durations)

    def circular_gap(a: int, b: int, duration: int) -> int:
        delta = abs((a % duration) - (b % duration))
        return min(delta, duration - delta)

    golden = n * ((3 - math.sqrt(5)) / 2)
    radius = max(8, n // 250)
    strides = [q for q in range(max(1, round(golden) - radius),
                                min(n, round(golden) + radius + 1))
               if math.gcd(q, n) == 1]
    if not strides:
        strides = [min((q for q in range(1, n) if math.gcd(q, n) == 1),
                       key=lambda q: abs(q - golden))]

    # Max gap among the first few rebase selections gauges how much of the
    # longest timeline remains unvisited early. It prevents a high count of
    # offset points from being consumed in narrow clusters.
    prefix_len = min(40, n)
    best_stride = None
    best_score = None
    for q in strides:
        indices = [(i * q) % n for i in range(n)]
        samples = sorted(ordered[i] for i in indices[:prefix_len])
        first_gaps = [samples[j + 1] - samples[j] for j in range(len(samples) - 1)]
        first_gaps.append(longest - samples[-1] + samples[0])
        worst_early_gap = max(first_gaps)
        # This value is independent of rotation, so test the real cyclic
        # spacing for all stations, not the previous false +10 cursor stride.
        min_other_gap = min(
            circular_gap(ordered[indices[i]], ordered[indices[(i + 1) % n]], d)
            for d in durations
            for i in range(n)
        )
        key = (worst_early_gap, -min_other_gap, abs(q - golden), q)
        if best_score is None or key < best_score:
            best_stride, best_score = q, key
    assert best_stride is not None
    chosen = [ordered[(i * best_stride) % n] for i in range(n)]

    if zero_terminator_after:
        # The game's shared cursor reads a literal zero once per cycle.
        # Rotate to keep its two neighbours well away from zero on *every*
        # station, avoiding an immediate near-zero rebase at the reset.
        def reset_boundary_score(start: int) -> tuple[float, int, int]:
            first = chosen[start]
            last = chosen[(start - 1) % n]
            normalized = min(
                min(circular_gap(0, first, d), circular_gap(0, last, d)) / d
                for d in durations
            )
            longest_gap = min(circular_gap(0, first, longest), circular_gap(0, last, longest))
            # Seed-based final tie breaker avoids accidental fixed indexing.
            tie = int.from_bytes(hashlib.sha256(f"{seed_text}:{start}".encode()).digest()[:4], "little")
            return (normalized, longest_gap, tie)
        first_index = max(range(n), key=reset_boundary_score)
        chosen = chosen[first_index:] + chosen[:first_index]
    return tuple(chosen)


def build_length_adaptive_offset_sequence(
    durations_ms: Dict[str, int] | List[int] | Tuple[int, ...],
) -> Tuple[int, ...]:
    """Order shared displacements in the exact order the game will read them.

    The original MIPS code at 0x001675B8..0x00167614 increments its SINGLE
    cursor and fetches ONE displacement before entering the station loop at
    0x0016761C. That displacement is reused for the ten stations. Previously
    the optimizer incorrectly interleaved for ten cursor increments/rebase,
    potentially clustering successive real selections. The read order is
    now simply words 1..N followed by the terminal zero.
    """
    values = list(durations_ms.values()) if isinstance(durations_ms, dict) else list(durations_ms)
    if not values or any(not isinstance(v, int) or v <= 0 for v in values):
        raise ValueError("Offset adaptation requires positive integer duration values")
    count = choose_shared_offset_entry_count(max(values))
    candidates = build_shared_offset_values(values, count)
    seed_text = SUPPORTED_SHA256 + ":" + ",".join(str(v) for v in values) + ":shared-farthest-point"
    ordered = build_uniform_stride_order(candidates, values, seed_text)
    if len(ordered) != count or len(set(ordered)) != count:
        raise AssertionError("Shared stride traversal did not cover the full candidate table")
    return (0, *ordered, 0)


def build_station_specific_offset_sequence(duration_ms: int, station_key: str) -> Tuple[int, ...]:
    """Create a per-station sequence ordered to maximize distance from prior picks."""
    if not isinstance(duration_ms, int) or duration_ms <= 0:
        raise ValueError(f"{station_key}: duration must be a positive integer")
    count = choose_offset_entry_count(duration_ms)
    if duration_ms <= count:
        raise ValueError(f"{station_key}: duration is too short to create unique offset bins")
    midpoint_offsets = tuple(((2 * rank - 1) * duration_ms) // (2 * count) for rank in range(1, count + 1))
    seed_text = f"{SUPPORTED_SHA256}:{station_key}:{duration_ms}:station-specific-farthest-point"
    offsets = build_uniform_stride_order(midpoint_offsets, [duration_ms], seed_text, zero_terminator_after=False)
    if any(v <= 0 or v >= duration_ms for v in offsets) or len(set(offsets)) != count:
        raise AssertionError(f"{station_key}: generated offsets are not distinct, valid positions")
    # The station-specific helper skips the terminal zero by selecting the first
    # nonzero offset during reset, so its cursor cycles only through valid offsets.
    return (0, *offsets, 0)


def build_station_specific_segment(
    durations_by_station: Dict[str, int],
) -> Tuple[bytes, Dict[str, object], int]:
    """Build RWX segment data with one descriptor/cursor and sequence per station.

    Layout at OFFSET_TABLE_LOAD_VA:
      word 0: unused zero
      word 1: zero, forcing the old shared cursor path to return zero/reset
      10 descriptors of (sequence VA, mutable cursor)
      10 independent sentinel/offset/terminator sequences
      aligned leaf helper which selects & advances the current station sequence

    Returns (segment_payload, report, helper_va). The per-station helper is
    entered from the initializer loop with $s20 as station index, adds the
    selected station-specific offset to $v1 ($3), then recomputes $s0 ($16) as
    $v0 ($2) + $v1. It preserves $v0/$a3 and saved registers, using caller-saved
    $t0-$t5 plus the intended $v1/$s0 outputs.
    """
    header = bytearray(struct.pack("<2I", 0, 0))
    descriptor_start = len(header)
    descriptor_bytes = 8 * len(STATIONS)  # sequence pointer + cursor
    payload = header + bytes(descriptor_bytes)
    per_station: Dict[str, object] = {}

    # Fill each independent optimized sequence and its descriptor pointer.
    for index, (key, filename) in enumerate(STATIONS):
        duration = durations_by_station[key]
        sequence = build_station_specific_offset_sequence(duration, key)
        seq_va = OFFSET_TABLE_LOAD_VA + len(payload)
        struct.pack_into("<2I", payload, descriptor_start + index * 8, seq_va, 0)
        seq_bytes = struct.pack(f"<{len(sequence)}I", *sequence)
        payload.extend(seq_bytes)
        offsets = sequence[1:-1]
        per_station[filename] = {
            "station_key": key,
            "sequence_va": f"0x{seq_va:08X}",
            "entry_count": len(offsets),
            "duration_ms": duration,
            "average_target_spacing_ms": duration / len(offsets),
            "cursor_initial_value": 0,
            "sentinel_first_word": sequence[0],
            "terminator": sequence[-1],
            "selected_offsets": list(offsets),
        }

    while len(payload) % 16:
        payload.append(0)
    helper_offset = len(payload)
    helper_va = OFFSET_TABLE_LOAD_VA + helper_offset
    if helper_va >> 28 != (OFFSET_HOOK_VA + 4) >> 28:
        raise ValueError("Station-specific helper is outside the MIPS JAL 256 MB region")

    # MIPS32 little-endian leaf helper. On a terminal zero, reset the cursor,
    # immediately select sequence[1], and set cursor=1. This avoids an empty
    # zero-displacement selection at every per-station cycle boundary.
    helper_words = (
        0x3C080068,  # lui   t0,0x0068
        0x25080008,  # addiu t0,t0,8       ; descriptor base 0x00680008
        0x001448C0,  # sll   t1,s4,3       ; $s4 is station loop index $s20
        0x01094021,  # addu  t0,t0,t1
        0x8D0A0000,  # lw    t2,0(t0)      ; sequence VA
        0x8D0B0004,  # lw    t3,4(t0)      ; cursor
        0x256B0001,  # addiu t3,t3,1
        0x000B6080,  # sll   t4,t3,2
        0x014C6021,  # addu  t4,t2,t4
        0x8D8D0000,  # lw    t5,0(t4)      ; selected offset or zero terminator
        0x15A00005,  # bnez  t5,nonzero    ; nonzero path skips the reset code
        0xAD0B0004,  # sw    t3,4(t0)      ; branch delay slot
        0xAD000004,  # reset: sw zero,4(t0)
        0x8D8D0004,  # lw    t5,4(t2)      ; select first nonzero offset instead
        0x240B0001,  # addiu t3,zero,1
        0xAD0B0004,  # sw    t3,4(t0)      ; cursor now points at first offset
        0x006D1821,  # nonzero: addu v1,v1,t5
        0x00438021,  # addu  s0,v0,v1     ; recompute candidate after offset
        0x03E00008,  # jr    ra
        0x00000000,  # nop
    )
    helper_bytes = struct.pack(f"<{len(helper_words)}I", *helper_words)
    payload.extend(helper_bytes)

    report = {
        "descriptor_base_va": f"0x{OFFSET_TABLE_LOAD_VA + descriptor_start:08X}",
        "descriptor_stride_bytes": 8,
        "station_count": len(STATIONS),
        "per_station_sequences": per_station,
        "helper_va": f"0x{helper_va:08X}",
        "helper_file_relative_offset": helper_offset,
        "helper_size_bytes": len(helper_bytes),
        "cursor_storage": "writable cursor in each station descriptor; one independent cursor per station",
        "global_shared_cursor_behavior": "header word 1 is zero, so the pre-loop shared cursor resets and contributes zero in station-specific mode",
        "segment_flags": OFFSET_HELPER_SEGMENT_FLAGS,
        "call_site_va": f"0x{OFFSET_HOOK_VA:08X}",
    }
    return bytes(payload), report, helper_va


def build_patched_executable(
    exe_path: Path,
    vb_paths: Dict[str, Path],
    output_path: Path,
    adapt_offset_sequence: bool | None = None,
    offset_mode: str | None = None,
) -> Dict[str, object]:
    exe_path = Path(exe_path)
    output_path = Path(output_path)
    vb_paths = {key: Path(path) for key, path in vb_paths.items()}
    exe_resolved = exe_path.resolve()
    output_resolved = output_path.resolve()
    if output_resolved == exe_resolved:
        raise ValueError("Output path must differ from input executable; original is never overwritten")
    if output_path.exists() and output_path.is_dir():
        raise ValueError(f"Output path is a directory: {output_path}")
    for station_key, audio_path in vb_paths.items():
        audio_resolved = audio_path.resolve()
        if output_resolved == audio_resolved:
            raise ValueError(
                f"Output executable path conflicts with {station_key} audio input; refusing to overwrite it"
            )
        if exe_resolved == audio_resolved:
            raise ValueError(
                f"Executable input path conflicts with {station_key} audio input; select distinct files"
            )
    original = exe_path.read_bytes()
    metadata = validate_original_executable(original)

    if offset_mode is None:
        resolved_offset_mode = "stock" if adapt_offset_sequence is False else "shared"
    else:
        resolved_offset_mode = offset_mode.strip().lower()
        if resolved_offset_mode not in OFFSET_MODES:
            raise ValueError(f"Unsupported offset mode {offset_mode!r}; choose one of {', '.join(OFFSET_MODES)}")
        if adapt_offset_sequence is not None:
            requested_legacy_mode = "shared" if adapt_offset_sequence else "stock"
            if requested_legacy_mode != resolved_offset_mode:
                raise ValueError("Specify either offset_mode or adapt_offset_sequence; the options conflict")
    adaptive_shared = resolved_offset_mode == "shared"
    adaptive_station = resolved_offset_mode == "station-specific"
    adaptive_enabled = adaptive_shared or adaptive_station

    durations: List[int] = []
    stations_report: List[Dict[str, object]] = []
    for key, label in STATIONS:
        vb_path = vb_paths[key]
        size, duration_ms = duration_ms_from_raw_stereo_psx_adpcm(vb_path)
        durations.append(duration_ms)
        stations_report.append({
            "station": label,
            "source_file": str(vb_path),
            "size_bytes": size,
            "duration_ms": duration_ms,
            # This exact value is written into the game's +0x18 table. The
            # milliseconds unit is the leading hypothesis, not yet runtime-captured.
            "boundary_value_written": duration_ms,
            "boundary_unit": "milliseconds (leading hypothesis; verify in PCSX2)",
            "duration_seconds": round(duration_ms / 1000.0, 3),
            "duration_mm_ss_ms": f"{duration_ms // 60000:02d}:{(duration_ms % 60000) // 1000:02d}.{duration_ms % 1000:03d}",
            "duration_hh_mm_ss_ms": f"{duration_ms // 3600000:02d}:{(duration_ms % 3600000) // 60000:02d}:{(duration_ms % 60000) // 1000:02d}.{duration_ms % 1000:03d}",
        })

    longest_duration_ms = max(durations)
    durations_by_station = {key: duration for (key, _filename), duration in zip(STATIONS, durations)}
    station_specific_report: Dict[str, object] | None = None
    if adaptive_shared:
        offset_table_words = build_length_adaptive_offset_sequence(durations)
        selected_offsets = tuple(offset_table_words[1:-1])
        offset_entry_count = len(selected_offsets)
    elif adaptive_station:
        # A separate optimized table and mutable cursor for every station.
        # The helper hook selects by the current loop index at runtime.
        offset_table_words = (0, 0)
        selected_offsets = tuple()
        offset_entry_count = 0
    else:
        # Exact stock behavior: preserve the original array and pointer. The
        # game increments the cursor before reading, so stock index 0 is skipped.
        offset_table_words = STOCK_OFFSET_VALUES + (0,)
        selected_offsets = STOCK_OFFSET_VALUES[1:]
        offset_entry_count = len(selected_offsets)

    def analyze_offset_residues(offset_values: Tuple[int, ...], duration: int) -> Dict[str, int]:
        residues = sorted({value % duration for value in offset_values})
        cyclic_gaps = [residues[i + 1] - residues[i] for i in range(len(residues) - 1)]
        if residues:
            cyclic_gaps.append(duration - residues[-1] + residues[0])
        return {
            "station_duration_ms": duration,
            "input_offset_count": len(offset_values),
            "distinct_offset_residues": len(residues),
            "duplicate_residues": len(offset_values) - len(residues),
            "minimum_cyclic_gap_ms": min(cyclic_gaps) if cyclic_gaps else 0,
            "maximum_cyclic_gap_ms": max(cyclic_gaps) if cyclic_gaps else 0,
        }

    def analyze_sequence_transitions(offset_values: List[int] | Tuple[int, ...], duration: int) -> Dict[str, float | int]:
        residues = [value % duration for value in offset_values]
        if len(residues) < 2:
            return {"ordered_events": len(residues), "minimum_next_offset_gap_ms": 0, "maximum_next_offset_gap_ms": 0, "mean_next_offset_gap_ms": 0.0, "exact_adjacent_repeats": 0}
        gaps = []
        for i, value in enumerate(residues):
            next_value = residues[(i + 1) % len(residues)]
            delta = abs(value - next_value)
            gaps.append(min(delta, duration - delta))
        return {
            "ordered_events": len(residues),
            "minimum_next_offset_gap_ms": min(gaps),
            "maximum_next_offset_gap_ms": max(gaps),
            "mean_next_offset_gap_ms": round(sum(gaps) / len(gaps), 2),
            "exact_adjacent_repeats": sum(1 for i, value in enumerate(residues) if value == residues[(i + 1) % len(residues)]),
        }

    modulo_residue_analysis: Dict[str, Dict[str, int]] = {}
    stock_modulo_residue_analysis: Dict[str, Dict[str, int]] = {}
    sequence_transition_analysis: Dict[str, Dict[str, float | int]] = {}
    station_sequences_for_analysis: Dict[str, Tuple[int, ...]] = {}
    for (key, filename), duration in zip(STATIONS, durations):
        if adaptive_station:
            table_for_station = build_station_specific_offset_sequence(duration, key)
            station_offsets = tuple(table_for_station[1:-1])
            station_sequences_for_analysis[filename] = station_offsets
            modulo_residue_analysis[filename] = analyze_offset_residues(station_offsets, duration)
            sequence_transition_analysis[filename] = analyze_sequence_transitions(station_offsets, duration)
        elif adaptive_shared:
            modulo_residue_analysis[filename] = analyze_offset_residues(selected_offsets, duration)
            # Exactly one event is read on each call to 0x00167540, and the
            # same displacement is applied to all ten radio stations.
            sequence_transition_analysis[filename] = analyze_sequence_transitions((*selected_offsets, 0), duration)
        else:
            modulo_residue_analysis[filename] = analyze_offset_residues(selected_offsets, duration)
            sequence_transition_analysis[filename] = analyze_sequence_transitions(selected_offsets, duration)
        # Stock table element 0 is not selected by this routine because it
        # increments the cursor before calculating table[index].
        stock_modulo_residue_analysis[filename] = analyze_offset_residues(STOCK_OFFSET_VALUES[1:], duration)

    out = bytearray(original)
    duration_table_file_offset = int(metadata["table_file_offset"])
    duration_table_bytes = struct.pack("<10I", *durations)
    if len(duration_table_bytes) != LENGTH_TABLE_BYTES:
        raise AssertionError("Unexpected duration table size")

    # Correct the per-station length provider's result table.
    out[duration_table_file_offset : duration_table_file_offset + LENGTH_TABLE_BYTES] = duration_table_bytes
    struct.pack_into("<I", out, int(metadata["ph_filesz_offset"]), int(metadata["new_filesz"]))
    # Redirect the selected-offset base pointer in the initializer to the new
    # table in an independent read-only PT_LOAD; do not overwrite .rodata strings.
    offset_table_file_offset: int | None = None
    offset_table_data: bytes | None = None
    load_segment_report: Dict[str, object] | None = None
    helper_report: Dict[str, object] | None = None
    helper_va: int | None = None
    station_mode_hooked = False
    if adaptive_enabled:
        if adaptive_shared:
            offset_table_data = struct.pack(f"<{len(offset_table_words)}I", *offset_table_words)
            segment_flags = OFFSET_DATA_SEGMENT_FLAGS
        else:
            offset_table_data, helper_report, helper_va = build_station_specific_segment(durations_by_station)
            segment_flags = OFFSET_HELPER_SEGMENT_FLAGS
        offset_table_file_offset = (len(out) + OFFSET_TABLE_ALIGNMENT - 1) & ~(OFFSET_TABLE_ALIGNMENT - 1)
        if offset_table_file_offset % OFFSET_TABLE_ALIGNMENT != OFFSET_TABLE_LOAD_VA % OFFSET_TABLE_ALIGNMENT:
            raise AssertionError("Extended table file and virtual addresses must have matching alignment")
        out.extend(bytes(offset_table_file_offset - len(out)))
        out.extend(offset_table_data)

        # Add a standard ELF PT_LOAD header in the unused header slot at 0x54.
        # Virtual address 0x00680000 is page-aligned and beyond the original
        # segment's memory end (0x00673168). Loader support for multiple PT_LOAD
        # segments still needs real PCSX2/console validation.
        ph_offset = int(metadata["phoff"])
        second_phdr = struct.pack(
            "<8I",
            PT_LOAD,
            offset_table_file_offset,
            OFFSET_TABLE_LOAD_VA,
            OFFSET_TABLE_LOAD_VA,
            len(offset_table_data),
            len(offset_table_data),
            segment_flags,
            OFFSET_TABLE_ALIGNMENT,
        )
        out[SECOND_PHDR_OFFSET : SECOND_PHDR_OFFSET + SECOND_PHDR_SIZE] = second_phdr
        struct.pack_into("<H", out, ELF32_EHDR_PHNUM_OFFSET, 2)

        # Original: lui $5,0x003b; addiu $5,$5,-0x36f8 -> 0x003AC908.
        # New table VA is 0x00680000, so this becomes lui $5,0x0068; addiu $5,$5,0.
        out[OFFSET_PTR_HI_FILE_OFFSET : OFFSET_PTR_HI_FILE_OFFSET + 4] = struct.pack("<I", 0x3C050068)
        out[OFFSET_PTR_LO_FILE_OFFSET : OFFSET_PTR_LO_FILE_OFFSET + 4] = struct.pack("<I", 0x24A50000)
        if adaptive_station:
            if helper_va is None:
                raise AssertionError("Station-specific helper VA was not created")
            if original[OFFSET_HOOK_FILE_OFFSET : OFFSET_HOOK_FILE_OFFSET + len(EXPECTED_OFFSET_HOOK_BYTES)] != EXPECTED_OFFSET_HOOK_BYTES:
                raise ValueError("Station-specific offset hook site does not match the expected original instructions")
            if helper_va >> 2 > 0x03FFFFFF:
                raise ValueError("Station-specific helper VA cannot be encoded in a MIPS JAL")
            jal_word = 0x0C000000 | ((helper_va >> 2) & 0x03FFFFFF)
            # The JAL delay slot preserves the existing $s0 = $v0 + $v1 add.
            # The helper adds this station's offset into $v1, recomputes $s0,
            # and returns directly to the following modulo operation.
            out[OFFSET_HOOK_FILE_OFFSET : OFFSET_HOOK_FILE_OFFSET + 8] = struct.pack("<2I", jal_word, 0x00438021)
            station_mode_hooked = True
        load_segment_report = {
            "program_header_index": 1,
            "program_header_offset": f"0x{SECOND_PHDR_OFFSET:X}",
            "file_offset": f"0x{offset_table_file_offset:X}",
            "virtual_address": f"0x{OFFSET_TABLE_LOAD_VA:08X}",
            "file_size_bytes": len(offset_table_data),
            "memory_size_bytes": len(offset_table_data),
            "flags": {4: "PF_R", 7: "PF_R|PF_W|PF_X"}.get(segment_flags, str(segment_flags)),
            "alignment": f"0x{OFFSET_TABLE_ALIGNMENT:X}",
            "virtual_range_end": f"0x{OFFSET_TABLE_LOAD_VA + len(offset_table_data):08X}",
            "gap_after_original_load_bytes": OFFSET_TABLE_LOAD_VA - (EXPECTED_SEGMENT_VADDR + EXPECTED_SEGMENT_MEMSZ),
            "unresolved_heap_overlap_risk": "Second PT_LOAD is just 52,888 bytes above the original ELF memory end; game's runtime allocator/memory pools may overwrite the segment despite no initial PT_LOAD overlap. This is not proof of a collision.",
            "compatibility_note": "Additional ELF PT_LOAD layout; confirm target PS2 ELF loader/PCSX2 compatibility.",
            "station_specific_helper": helper_report,
        }

    # Replace the radio-length getter with a lookup into the duration table.
    out[GETTER_FILE_OFFSET : GETTER_FILE_OFFSET + len(PATCH_BYTES)] = PATCH_BYTES

    # Verify output bytes and table mappings before writing.
    if out[GETTER_FILE_OFFSET : GETTER_FILE_OFFSET + len(PATCH_BYTES)] != PATCH_BYTES:
        raise AssertionError("Length getter patch verification failed")
    if out[duration_table_file_offset : duration_table_file_offset + LENGTH_TABLE_BYTES] != duration_table_bytes:
        raise AssertionError("Duration table verification failed")
    if struct.unpack_from("<I", out, int(metadata["ph_filesz_offset"]))[0] != int(metadata["new_filesz"]):
        raise AssertionError("Primary PT_LOAD file-size verification failed")

    if adaptive_enabled:
        assert offset_table_file_offset is not None and offset_table_data is not None and load_segment_report is not None
        if out[offset_table_file_offset : offset_table_file_offset + len(offset_table_data)] != offset_table_data:
            raise AssertionError("Extended offset table payload verification failed")
        if struct.unpack_from("<H", out, ELF32_EHDR_PHNUM_OFFSET)[0] != 2:
            raise AssertionError("ELF program-header count was not updated")
        new_phdr = struct.unpack_from("<8I", out, SECOND_PHDR_OFFSET)
        if new_phdr[0] != PT_LOAD or new_phdr[1] != offset_table_file_offset or new_phdr[2] != OFFSET_TABLE_LOAD_VA:
            raise AssertionError("Extended offset table PT_LOAD verification failed")
        if struct.unpack_from("<I", out, OFFSET_PTR_HI_FILE_OFFSET)[0] != 0x3C050068:
            raise AssertionError("Offset-table pointer high instruction verification failed")
        if struct.unpack_from("<I", out, OFFSET_PTR_LO_FILE_OFFSET)[0] != 0x24A50000:
            raise AssertionError("Offset-table pointer low instruction verification failed")
        if adaptive_station:
            if helper_va is None or offset_table_file_offset is None or offset_table_data is None:
                raise AssertionError("Station-specific helper metadata missing")
            expected_jal = 0x0C000000 | ((helper_va >> 2) & 0x03FFFFFF)
            if out[OFFSET_HOOK_FILE_OFFSET:OFFSET_HOOK_FILE_OFFSET+8] != struct.pack("<2I", expected_jal, 0x00438021):
                raise AssertionError("Station-specific helper call-site patch verification failed")
            helper_rel = helper_va - OFFSET_TABLE_LOAD_VA
            helper_bytes = struct.pack("<20I", *(
                0x3C080068, 0x25080008, 0x001448C0, 0x01094021, 0x8D0A0000,
                0x8D0B0004, 0x256B0001, 0x000B6080, 0x014C6021, 0x8D8D0000,
                0x15A00005, 0xAD0B0004, 0xAD000004, 0x8D8D0004, 0x240B0001,
                0xAD0B0004, 0x006D1821, 0x00438021, 0x03E00008, 0x00000000,
            ))
            if offset_table_data[helper_rel:helper_rel+len(helper_bytes)] != helper_bytes:
                raise AssertionError("Station-specific helper code verification failed")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    # Publish only a completely written executable. If a write, flush, or
    # rename fails, retain any older patched output unchanged. Never create
    # overwrite backup files.
    fd, staged_name = tempfile.mkstemp(prefix=f".{output_path.name}.", suffix=".building", dir=str(output_path.parent))
    staged_path = Path(staged_name)
    try:
        with os.fdopen(fd, "wb") as staged:
            staged.write(out)
            staged.flush()
            os.fsync(staged.fileno())
        if staged_path.stat().st_size != len(out) or hashlib.sha256(staged_path.read_bytes()).digest() != hashlib.sha256(out).digest():
            raise IOError("Patched executable verification failed before publishing")
        os.replace(staged_path, output_path)
    finally:
        staged_path.unlink(missing_ok=True)
    table_report_offset = offset_table_file_offset if adaptive_enabled else STOCK_OFFSET_TABLE_FILE_OFFSET
    return {
        "status": "PATCHED_STATICALLY_NOT_EMULATOR_TESTED",
        "input_executable": str(exe_path),
        "input_sha256": metadata["sha256"],
        "output_executable": str(output_path),
        "output_sha256": sha256(bytes(out)),
        "length_getter_modified": True,
        "length_getter_va": f"0x{GETTER_VA:08X}",
        "length_getter_file_offset": f"0x{GETTER_FILE_OFFSET:X}",
        "duration_table_va": f"0x{int(metadata['table_va']):08X}",
        "duration_table_file_offset": f"0x{duration_table_file_offset:X}",
        "old_pt_load_filesz": f"0x{int(metadata['old_filesz']):X}",
        "new_pt_load_filesz": f"0x{int(metadata['new_filesz']):X}",
        "duration_unit_assumption": "milliseconds (native IOP file-size conversion independently established by disassembly)",
        "playback_boundary_unit": "milliseconds (native IOP file-size conversion established by disassembly)",
        "boundary_values_written": {station["station"]: station["boundary_value_written"] for station in stations_report},
        "streaming_module_patched": False,
        "offset_mode": resolved_offset_mode,
        "offset_sequence_changed": bool(adaptive_enabled),
        "offset_sequence_strategy": {
            "mode": resolved_offset_mode,
            "enabled": bool(adaptive_enabled),
            "station_specific": bool(adaptive_station),
            "stock_table_va": f"0x{STOCK_OFFSET_TABLE_VA:08X}",
            "table_va": f"0x{OFFSET_TABLE_LOAD_VA if adaptive_enabled else STOCK_OFFSET_TABLE_VA:08X}",
            "table_file_offset": f"0x{table_report_offset:X}",
            "entry_count": len(selected_offsets) if not adaptive_station else sum(len(v) for v in station_sequences_for_analysis.values()),
            "shared_entry_count": len(selected_offsets) if adaptive_shared or resolved_offset_mode == "stock" else None,
            "per_station_entry_counts": {name: len(seq) for name, seq in station_sequences_for_analysis.items()} if adaptive_station else None,
            "per_station_sequences": helper_report.get("per_station_sequences") if adaptive_station and helper_report else None,
            "helper_va": f"0x{helper_va:08X}" if helper_va is not None else None,
            "station_specific_hook_applied": bool(station_mode_hooked),
            "stock_entry_count_available_to_runtime": len(STOCK_OFFSET_VALUES) - 1,
            "target_spacing_goal_ms": OFFSET_TABLE_TARGET_SPACING_MS if adaptive_enabled else None,
            "shared_event_cycle_length": (len(selected_offsets) + 1) if adaptive_shared else None,
            "shared_event_cycle_coprime_to_station_count": None,  # obsolete prior assumption
            "shared_cursor_reads_per_rebase": 1 if adaptive_shared else None,
            "shared_displacement_applies_to_all_stations": True if adaptive_shared else None,
            "shared_modulo_residues_unique_for_all_stations": (all(v["duplicate_residues"] == 0 for v in modulo_residue_analysis.values()) if adaptive_shared else None),
            "shared_residue_uniqueness_feasible_for_all_stations": (all(d > len(selected_offsets) for d in durations) if adaptive_shared else None),
            "stations_where_residue_aliases_are_unavoidable": ([filename for (_key, filename), d in zip(STATIONS, durations) if adaptive_shared and d <= len(selected_offsets)]),
            "position_order_strategy": ("golden-stride traversal with distributed first-40 coverage and large successive jumps; shared mode follows actual one-read-per-rebase order for all ten stations; station-specific mode keeps an independent ordered sequence" if adaptive_enabled else "stock order"),
            "repeat_avoidance_scope": "Offsets are distinct/alias-aware and ordered to maximize separation from prior offset choices. This does not guarantee the final runtime position never repeats because the game adds saved-position and random-derived terms.",
            "target_spacing_ms": (longest_duration_ms / max(1, len(selected_offsets))) if selected_offsets else None,
            "min_entry_count": OFFSET_TABLE_MIN_ENTRIES if adaptive_enabled else len(selected_offsets),
            "max_entry_count": OFFSET_TABLE_MAX_ENTRIES if adaptive_enabled else len(selected_offsets),
            "table_words_including_sentinels": len(offset_table_words) if not adaptive_station else None,
            "index_zero_word": offset_table_words[0] if not adaptive_station else None,
            "terminator": offset_table_words[-1] if not adaptive_station else None,
            "selected_offsets": list(selected_offsets) if selected_offsets else None,
            "sequence_permutation_seed_sha256": hashlib.sha256((SUPPORTED_SHA256 + ":" + ",".join(str(v) for v in durations)).encode("ascii")).hexdigest() if adaptive_shared else None,
            "load_segment": load_segment_report,
            "original_table_pointer_patched": bool(adaptive_enabled),
            "pointer_high_file_offset": f"0x{OFFSET_PTR_HI_FILE_OFFSET:X}" if adaptive_enabled else None,
            "pointer_low_file_offset": f"0x{OFFSET_PTR_LO_FILE_OFFSET:X}" if adaptive_enabled else None,
            "longest_station": next(st[1] for st, d in zip(STATIONS, durations) if d == longest_duration_ms),
            "longest_duration_ms": longest_duration_ms,
            "algorithm": (
                "deterministic per-station midpoint bins ordered by a co-prime golden-stride traversal; one candidate entry per ~5 seconds, bounded 64–4096 per station; per-station helper skips the terminal zero selection and cycles only through nonzero offsets"
                if adaptive_station else
                ("deterministic alias-aware midpoint bins covering the longest track, ordered by a co-prime golden-stride traversal in the actual single shared cursor order (one displacement per rebase, reused for all ten stations); 64–4096 entries in a second read-only PT_LOAD" if adaptive_shared else "stock sequence unchanged")
            ),
            "cursor_semantics": (
                "Station-specific mode increments each station descriptor's own cursor, skips a terminal zero by resetting and selecting the first nonzero offset, and adds only that station's selected offset. The legacy shared cursor is forced to zero by the zero word at entry 1."
                if adaptive_station else
                "The code increments the +0x38C cursor before reading; table word 0 is skipped. Selected words are 1..N; trailing zero resets cursor to zero."
            ),
            "scope_note": (
                "Each radio station has a separately generated offset sequence and writable cursor. Sequence length and values are calculated from that station's duration."
                if adaptive_station else
                "One shared offset sequence initializes all ten station base positions. Exact modulo-residue collisions are avoided where each station duration has enough distinct non-zero residues for the full table; for durations at or below the entry count, aliases are mathematically unavoidable and reported. The sequence length controls offset-choice coverage, not a playback polling rate."
            ),
            "modulo_residue_analysis": modulo_residue_analysis,
            "sequence_transition_analysis": sequence_transition_analysis,
            "stock_modulo_residue_analysis": stock_modulo_residue_analysis,
            "limitation": (
                "Station-specific mode changes the initializer arithmetic path and adds an RWX helper/data PT_LOAD. It must be tested with the target PS2 ELF loader and PCSX2; stall reduction is not guaranteed."
                if adaptive_station else
                "Longer sequence and improved bin spacing may broaden starting-position choices but do not prove or guarantee a fix for stalls; the added PT_LOAD must be tested with the target PS2 ELF loader."
            ),
        },
        "stations": stations_report,
        "limitations": [
            "This is a prototype for the exact North American SLUS_214.23 hash only.",
            "The output is statically verified but has not been tested in PCSX2 or on a console.",
            "The raw stereo PSX ADPCM duration calculation assumes 32 kHz, 16-byte ADPCM frames, 28 samples/frame/channel and the LCS PS2 stream layout.",
            "It supplies all ten station lengths; pass the final staged .VB file for every station, including unchanged original stations.",
            "Shared mode generates between 64 and 4096 offsets based on the longest staged station duration, targeting one table bin per 5 seconds of timeline.",
            "Station-specific mode generates a separate 64–4096-entry offset sequence and cursor per station, injects a MIPS helper, and uses an RWX PT_LOAD segment.",
            "Both adaptive modes add a second PT_LOAD segment; target PS2 loader compatibility and stall reduction still require emulator testing.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Experimental build-time duration patcher for the exact LCS PS2 SLUS_214.23 executable."
    )
    parser.add_argument("executable", type=Path, help="Unmodified reference SLUS_214.23")
    parser.add_argument("--out", required=True, type=Path, help="Output executable path (must not overwrite input)")
    parser.add_argument("--report", type=Path, help="Optional JSON report path")
    parser.add_argument(
        "--offset-mode", choices=OFFSET_MODES, default="shared",
        help="Offset strategy: shared adaptive list, station-specific lists, or unchanged stock list",
    )
    parser.add_argument(
        "--keep-stock-offset-sequence", action="store_true",
        help="Compatibility alias for --offset-mode stock",
    )
    parser.add_argument(
        "--radio-dir", type=Path,
        help="Folder containing all ten final staged .VB files by stock filename"
    )
    parser.add_argument("--head", type=Path, help="Final staged HEAD.VB")
    parser.add_argument("--double", type=Path, help="Final staged DOUBLE.VB")
    parser.add_argument("--kjah", type=Path, help="Final staged KJAH.VB")
    parser.add_argument("--rise", type=Path, help="Final staged RISE.VB")
    parser.add_argument("--lips", type=Path, help="Final staged LIPS.VB")
    parser.add_argument("--mundo", type=Path, help="Final staged MUNDO.VB")
    parser.add_argument("--msx", type=Path, help="Final staged MSX.VB")
    parser.add_argument("--flash", type=Path, help="Final staged FLASH.VB")
    parser.add_argument("--lcj", type=Path, help="Final staged LCJ.VB")
    parser.add_argument("--lcfr", type=Path, help="Final staged LCFR.VB")
    args = parser.parse_args()

    individual = {key: getattr(args, key) for key, _label in STATIONS}
    supplied_individual = {key: path for key, path in individual.items() if path is not None}
    if args.radio_dir is not None and supplied_individual:
        parser.error("Use either --radio-dir or individual --head/--double/... file arguments, not both")
    if args.radio_dir is not None:
        vb_paths = {key: args.radio_dir / label for key, label in STATIONS}
    elif len(supplied_individual) == len(STATIONS):
        vb_paths = individual
    else:
        missing = [key for key, _label in STATIONS if individual[key] is None]
        parser.error("Provide --radio-dir or all ten station files; missing: " + ", ".join(missing))
    try:
        selected_mode = "stock" if args.keep_stock_offset_sequence else args.offset_mode
        if args.report is not None:
            report_resolved = args.report.resolve()
            protected_paths = [args.executable, args.out, *vb_paths.values()]
            for protected_path in protected_paths:
                if report_resolved == Path(protected_path).resolve():
                    raise ValueError(
                        f"Report path conflicts with an input/output file ({protected_path}); choose a separate report path"
                    )
        report = build_patched_executable(
            args.executable, vb_paths, args.out,
            offset_mode=selected_mode,
        )
    except (OSError, ValueError, struct.error) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    report_text = json.dumps(report, indent=2)
    print(report_text)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(report_text + "\n", encoding="utf-8")
        print(f"\nReport saved to: {args.report}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
