#!/usr/bin/env python3
"""Single authoritative per-build sizing/patch plan for GTA LCS PS2 (US SLUS_214.23).

Pure functions; never use sample-specific duration or shift constants. The runtime
build must recalculate from the *staged bytes* and compare with the GUI plan.
Only sizes and calculations are established offline; gameplay is not certified.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Mapping

from lcs_iop_safe_seek import (
    duration_ms, select_extra_shift, expected_instruction_words,
    _maximum_product, MAX_SAFE_DURATION_SIZE, MAX_32_PRODUCT,
    NATIVE_UNPATCHED_DURATION_LIMIT, NATIVE_POSITION_SIGNED_LIMIT,
)
from lcs_stream_bounds import installed_size, READ_CHUNK_BYTES, INTERLEAVE_BYTES
from lcs_duration_aware_exe_patcher import STATIONS

STATION_KEYS = frozenset(key for key, _filename in STATIONS)


def _exact_byte_count(value: int, name: str) -> int:
    """Reject coerced measurements; a truncated byte count can target the wrong boundary."""
    if type(value) is not int:
        raise ValueError(f'{name} must be an exact integer number of bytes, got {value!r}')
    return value


@dataclass(frozen=True)
class StationCalculation:
    station: str
    original_size: int
    source_size: int
    installed_size: int
    original_duration_ms: int
    replacement_duration_ms: int
    duration_delta_ms: int
    size_delta_bytes: int
    classification: str
    silence_padding_bytes: int
    ee_boundary_ms: int

    def as_dict(self) -> dict:
        result = vars(self).copy()
        # Diagnostic only: never transform a negative delta into an unsigned
        # boundary, and never assume that a much shorter radio has enough
        # physical read windows for genuinely random seek starts.
        result['shortened_from_original'] = self.classification == 'shorter'
        result['one_read_window_only'] = self.installed_size <= READ_CHUNK_BYTES
        if result['one_read_window_only']:
            result['short_seek_warning'] = (
                'Only one 256 KiB read window: modeled CDSTREAM seek starts '
                'remain at byte zero. Playback-position variety cannot be '
                'guaranteed, even with many randomized EE offsets.'
            )
        elif result['shortened_from_original']:
            result['short_seek_warning'] = (
                'New duration is shorter than the original; the modeled IOP '
                'seek path bounds expired positions, but EE save/rebase state '
                'has not been independently runtime-validated.'
            )
        else:
            result['short_seek_warning'] = None
        return result


@dataclass(frozen=True)
class PatchCalculation:
    stations: tuple[StationCalculation, ...]
    iop_extra_shift: int
    iop_shift_right: int
    iop_shift_left: int
    iop_old_words: tuple[int, int]
    iop_new_words: tuple[int, int]
    maximum_signed_product: int
    maximum_signed_product_limit: int
    max_safe_source_size: int
    largest_installed_size: int
    iop_needs_instruction_patch: bool
    iop_duration_overflow_fix_required: bool
    iop_position_overflow_fix_required: bool

    def as_dict(self) -> dict:
        return {
            'calculator_version': '5',
            'duration_equation': '(installed_VB_bytes * 7) // 256',
            'installed_size_equation': 'max(source_size, 0x40000) for replacements; unchanged for stock stations',
            'original_32_bit_duration_limit': NATIVE_UNPATCHED_DURATION_LIMIT,
            'original_signed_position_limit': NATIVE_POSITION_SIGNED_LIMIT,
            'supported_max_aligned_stream_bytes': MAX_SAFE_DURATION_SIZE,
            'iop_extra_shift': self.iop_extra_shift,
            'iop_original_right_shift': 1,
            'iop_original_left_shift': 1,
            'iop_calculated_right_shift': self.iop_shift_right,
            'iop_calculated_left_shift': self.iop_shift_left,
            'iop_original_words': [f'0x{x:08X}' for x in self.iop_old_words],
            'iop_calculated_words': [f'0x{x:08X}' for x in self.iop_new_words],
            'iop_instruction_patch_required': self.iop_needs_instruction_patch,
            'iop_duration_overflow_fix_required': self.iop_duration_overflow_fix_required,
            'iop_position_overflow_fix_required': self.iop_position_overflow_fix_required,
            'iop_duration_calculation': '(size >> 8) * 7 when 32-bit original multiplication would overflow',
            'claimed_time_domain': '1 second to 5 hours of source content; installed length may include alignment/buffer padding',
            'maximum_signed_product': self.maximum_signed_product,
            'maximum_signed_product_limit': self.maximum_signed_product_limit,
            'largest_installed_size_bytes': self.largest_installed_size,
            'stations': {item.station: item.as_dict() for item in self.stations},
            'not_verified': 'Correct arithmetic and binary writes alone do not establish in-game correctness',
        }


def calculate_plan(
    original_sizes: Mapping[str, int],
    replacement_sizes: Mapping[str, int],
) -> PatchCalculation:
    """Create plan from exact per-station source byte counts.

    Station keys must match, replacements are optional, and all radio sizes must
    be in the native LCS PS2 domain. Fail explicitly for unrepresentable sizes.
    """
    if set(original_sizes) != STATION_KEYS:
        raise ValueError('Expected exactly the canonical ten GTA LCS PS2 station keys: ' + ', '.join(key for key, _ in STATIONS))
    if set(replacement_sizes) - STATION_KEYS:
        raise ValueError('Unknown replacement station(s): ' + ', '.join(sorted(set(replacement_sizes)-STATION_KEYS)))
    entries = []
    # Keep the game's internal radio-index order even if a caller supplies a
    # different mapping insertion order (e.g. JSON sorted alphabetically).
    for key, _filename in STATIONS:
        base = _exact_byte_count(original_sizes[key], f'Original {key}')
        original_ms = duration_ms(base)
        if key in replacement_sizes:
            source = _exact_byte_count(replacement_sizes[key], f'Replacement {key}')
            installed = installed_size(source)
        else:
            source = installed = base
        installed_ms = duration_ms(installed)
        if installed > base:
            classification = 'longer'
        elif installed < base:
            classification = 'shorter'
        else:
            classification = 'same-length'
        entries.append(StationCalculation(
            station=key, original_size=base, source_size=source, installed_size=installed,
            original_duration_ms=original_ms, replacement_duration_ms=installed_ms,
            duration_delta_ms=installed_ms-original_ms,
            size_delta_bytes=installed-base, classification=classification,
            silence_padding_bytes=installed-source, ee_boundary_ms=installed_ms,
        ))
    sizes = [e.installed_size for e in entries]
    extra = select_extra_shift(sizes)
    largest = max(sizes)
    product = _maximum_product(largest, extra)
    if product > MAX_32_PRODUCT:
        raise AssertionError('Internal seek overflow guard failed')
    old_words = expected_instruction_words(0)
    words = expected_instruction_words(extra)
    return PatchCalculation(
        stations=tuple(entries), iop_extra_shift=extra,
        iop_shift_right=1+extra, iop_shift_left=1+extra,
        iop_old_words=old_words, iop_new_words=words,
        maximum_signed_product=product, maximum_signed_product_limit=MAX_32_PRODUCT,
        max_safe_source_size=MAX_SAFE_DURATION_SIZE,
        largest_installed_size=largest,
        iop_needs_instruction_patch=extra>0 or largest>NATIVE_UNPATCHED_DURATION_LIMIT or largest>NATIVE_POSITION_SIGNED_LIMIT,
        iop_duration_overflow_fix_required=largest>NATIVE_UNPATCHED_DURATION_LIMIT,
        iop_position_overflow_fix_required=largest>NATIVE_POSITION_SIGNED_LIMIT,
    )
