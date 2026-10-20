"""VCS PS2 *analysis* calculator — does not authorize binary patching.

Identified VCS streaming-size conversion from SLUS_215.90/CDSTREAM.IRX:
    duration_ms = (installed_size_bytes * 7) // 256

Unlike LCS this backend does NOT claim the known LCS instruction shift patch
can be reused. Unknown original station sizes are represented as unknown.
"""
from __future__ import annotations
from dataclasses import asdict, dataclass
from typing import Mapping
from vcs_ee_overflow_patch import (MAX_ALIGNED_SAFE_BYTES, MAX_ALIGNED_SAFE_MS,
    MIN_ALIGNED_1S_BYTES, TARGET_MIN_SOURCE_MS, TARGET_MAX_SOURCE_MS)
from vcs_iop_position_math import (original_iop_signed_position, ideal_signed_position,
    proposed_safe_split)

VCS_STATIONS = (
    ('flash', 'FLASH.VB'), ('vrock', 'VROCK.VB'),
    ('paradise', 'PARADISE.VB'), ('vcpr', 'VCPR.VB'),
    ('vcfl', 'VCFL.VB'), ('wave', 'WAVE.VB'),
    ('fresh', 'FRESH.VB'), ('espant', 'ESPANT.VB'),
    ('emotion', 'EMOTION.VB'),
)

# These are *risk thresholds for specific original opcode paths*, not global
# limits on files the game can read. Signed path operand is unproven at runtime.
SIGNED_POSITION_SIZE_THRESHOLD = ((1 << 31) - 1) // 7
DURATION_MULTIPLY_THRESHOLD = ((1 << 32) - 1) // 7
EE_SIGNED_CONVERSION_LIMIT_MS = (1 << 23) - 1
EE_DIVU_CONVERSION_LIMIT_MS = (1 << 24) - 1
STATION_KEYS = frozenset(k for k, _ in VCS_STATIONS)


def _validated_size(name: str, size: int) -> int:
    if type(size) is not int or size <= 0 or size % 16 or size > 0xffffffff:
        raise ValueError(f'{name}: PS2 raw ADPCM size must be a positive, 16-byte-aligned 32-bit integer, got {size!r}')
    return size


def duration_ms(size: int) -> int:
    return _validated_size('VB size', size) * 7 // 256


@dataclass(frozen=True)
class StationCalculation:
    station: str
    filename: str
    source_size: int | None
    original_size: int | None
    installed_size: int | None
    original_duration_ms: int | None
    replacement_duration_ms: int | None
    duration_delta_ms: int | None
    size_delta_bytes: int | None
    classification: str
    ee_boundary_ms: int | None
    aligned_8kb: bool | None
    aligned_16kb: bool | None
    duration_overflow_risk: bool | None
    position_operand_risk_if_raw_bytes: bool | None
    ee_signed_duration_to_bytes_overflow: bool | None
    ee_divu_fix_range_supported: bool | None
    within_vcs_1s_to_4h39_target_allocation: bool | None
    iop_original_if_operand_equals_file_size: int | None = None
    iop_ideal_if_operand_equals_file_size: int | None = None
    iop_safe_model_if_operand_equals_file_size: int | None = None

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class VCSCalculation:
    stations: tuple[StationCalculation, ...]
    original_sizes_complete: bool
    selected_replacement_count: int
    largest_known_size: int | None
    duration_instruction_overflow_risk: bool
    signed_position_operand_risk_if_raw_bytes: bool
    ee_signed_duration_to_bytes_overflow: bool
    ee_divu_fix_range_supported_for_all_known_streams: bool
    all_selected_replacements_within_target: bool

    def as_dict(self) -> dict:
        return {
            'game_profile': 'vcs_ps2', 'calculator': 'VCS PS2 diagnostic v1',
            'read_only': True,
            'target_source_duration_ms': [TARGET_MIN_SOURCE_MS, TARGET_MAX_SOURCE_MS],
            'min_one_second_source_vb_bytes': MIN_ALIGNED_1S_BYTES,
            'max_safe_aligned_vb_bytes': MAX_ALIGNED_SAFE_BYTES,
            'max_safe_aligned_vb_duration_ms': MAX_ALIGNED_SAFE_MS,
            'all_selected_replacements_within_target': self.all_selected_replacements_within_target,
            'duration_equation': '(installed_VB_bytes * 7) // 256',
            'iop_model_equation': 'trunc(7 * signed_a2 / 8), modeled, NOT a binary patch',
            'iop_operand_provenance': 'a2 is adjusted in IRX; installed file size is only a hypothetical comparison input',
            'duration_instruction_overflow_risk': self.duration_instruction_overflow_risk,
            'signed_position_operand_risk_if_raw_bytes': self.signed_position_operand_risk_if_raw_bytes,
            'ee_signed_duration_to_bytes_overflow': self.ee_signed_duration_to_bytes_overflow,
            'ee_divu_fix_range_supported_for_all_known_streams': self.ee_divu_fix_range_supported_for_all_known_streams,
            'original_sizes_complete': self.original_sizes_complete,
            'selected_replacement_count': self.selected_replacement_count,
            'largest_known_size': self.largest_known_size,
            'stations': {item.station: item.as_dict() for item in self.stations},
            'not_verified': 'Actual VCS runtime position operand, EOF handling, ISO writer and executable patch locations remain unverified.',
        }


def calculate_plan(original_sizes: Mapping[str, int | None],
                   replacements: Mapping[str, int]) -> VCSCalculation:
    """Compute new boundaries; do not invent a stock size when ISO is unavailable.

    The VCS original audio has nine named stations. Missing originals remain
    None, and untouched stations with unknown size retain unknown duration.
    LCS padding and its exact seek opcode adjustments are never used here.
    """
    extra = (set(original_sizes) | set(replacements)) - STATION_KEYS
    if extra:
        raise ValueError(f'Unsupported VCS radio keys: {sorted(extra)}')
    rows = []
    for key, filename in VCS_STATIONS:
        stock = original_sizes.get(key)
        new = replacements.get(key)
        if stock is not None:
            stock = _validated_size('Original ' + key, stock)
        if new is not None:
            new = _validated_size('Replacement ' + key, new)
        used = new if new is not None else stock
        change = (new-stock) if new is not None and stock is not None else None
        classification = ('unknown-original' if new is not None and stock is None
                          else 'unchanged' if new is None and stock is not None
                          else 'unknown' if used is None else
                          'longer' if change > 0 else 'shorter' if change < 0 else 'same-size')
        rows.append(StationCalculation(
            station=key, filename=filename, source_size=new,
            original_size=stock, installed_size=used,
            original_duration_ms=duration_ms(stock) if stock is not None else None,
            replacement_duration_ms=duration_ms(used) if used is not None else None,
            duration_delta_ms=(duration_ms(new)-duration_ms(stock)
                               if new is not None and stock is not None else None),
            size_delta_bytes=change, classification=classification,
            ee_boundary_ms=duration_ms(used) if used is not None else None,
            aligned_8kb=used % 0x2000 == 0 if used is not None else None,
            aligned_16kb=used % 0x4000 == 0 if used is not None else None,
            duration_overflow_risk=used > DURATION_MULTIPLY_THRESHOLD if used is not None else None,
            position_operand_risk_if_raw_bytes=(used > SIGNED_POSITION_SIZE_THRESHOLD
                                                if used is not None else None),
            ee_signed_duration_to_bytes_overflow=(duration_ms(used) > EE_SIGNED_CONVERSION_LIMIT_MS
                                                if used is not None else None),
            ee_divu_fix_range_supported=(duration_ms(used) <= EE_DIVU_CONVERSION_LIMIT_MS
                                        and used <= DURATION_MULTIPLY_THRESHOLD
                                        if used is not None else None),
            within_vcs_1s_to_4h39_target_allocation=(MIN_ALIGNED_1S_BYTES <= used <= MAX_ALIGNED_SAFE_BYTES
                                        and used % 0x4000 == 0 if used is not None else None),
            iop_original_if_operand_equals_file_size=(original_iop_signed_position(used)
                if used is not None and used <= 0x7fffffff else None),
            iop_ideal_if_operand_equals_file_size=(ideal_signed_position(used)
                if used is not None and used <= 0x7fffffff else None),
            iop_safe_model_if_operand_equals_file_size=(proposed_safe_split(used)
                if used is not None and used <= 0x7fffffff else None),
        ))
    sizes = [s.installed_size for s in rows if s.installed_size is not None]
    return VCSCalculation(
        stations=tuple(rows), original_sizes_complete=all(x.original_size is not None for x in rows),
        selected_replacement_count=len(replacements), largest_known_size=max(sizes) if sizes else None,
        all_selected_replacements_within_target=all(
            MIN_ALIGNED_1S_BYTES <= s <= MAX_ALIGNED_SAFE_BYTES and s % 0x4000 == 0
            for s in replacements.values()),
        duration_instruction_overflow_risk=any(s > DURATION_MULTIPLY_THRESHOLD for s in sizes),
        signed_position_operand_risk_if_raw_bytes=any(s > SIGNED_POSITION_SIZE_THRESHOLD for s in sizes),
        ee_signed_duration_to_bytes_overflow=any(duration_ms(s)>EE_SIGNED_CONVERSION_LIMIT_MS for s in sizes),
        ee_divu_fix_range_supported_for_all_known_streams=all(
            duration_ms(s)<=EE_DIVU_CONVERSION_LIMIT_MS and s<=DURATION_MULTIPLY_THRESHOLD for s in sizes),
    )
