#!/usr/bin/env python3
"""Read-only authoritative station/original-value/destination lookup for US LCS PS2.

The pristine SLUS_214.23 DOES NOT contain ten stock duration literals: its
length getter uses CDSTREAM.IRX's disc-file metadata. The duration patcher
creates a new ten-u32 table. This registry connects original ISO9660 metadata
and its verified stock station identities to the new table and IOP instruction
sites, without confusing the two domains.

All mappings are specific to the supported pristine US executable. They are
not claims that alternate releases, loaders or replacement radio encodings
behave the same way. Gameplay testing remains necessary.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import struct
from typing import Mapping, TYPE_CHECKING

import lcs_duration_aware_exe_patcher as exe
import lcs_iop_safe_seek as iop
from lcs_patch_calculator import PatchCalculation, calculate_plan

if TYPE_CHECKING:
    from lcs_duration_aware_iso_patcher import Iso9660Image

# Observed in the original, unmodified SLUS_214.23 game's ISO9660 directory.
# NOT replacement defaults. Only applicable with the verified US executable.
VERIFIED_STOCK_SIZES: dict[str, int] = {
    'head': 58_392_576,
    'double': 58_245_120,
    'kjah': 63_176_704,
    'rise': 92_897_280,
    'lips': 61_390_848,
    'mundo': 73_728_000,
    'msx': 85_147_648,
    'flash': 63_537_152,
    'lcj': 104_431_616,
    'lcfr': 110_247_936,
}

IOP_ISO_PATH = 'MODULES/CDSTREAM.IRX'
VB_ISO_PREFIX = 'AUDIO/MUSIC'

@dataclass(frozen=True)
class StationLocation:
    key: str
    filename: str
    iso_path: str
    original_size_bytes: int
    original_duration_ms: int
    original_extent_lba: int
    original_directory_record_file_offset: int
    original_record_size_field_file_offset: int
    original_record_extent_field_file_offset: int
    ee_runtime_duration_table_index: int
    ee_runtime_duration_table_va: int
    ee_runtime_duration_table_elf_offset: int

    def as_dict(self) -> dict:
        return vars(self).copy()

@dataclass(frozen=True)
class OriginalLocationLookup:
    executable_iso_path: str
    executable_sha256: str
    cdstream_iso_path: str
    cdstream_sha256: str
    original_duration_source: str
    stations: tuple[StationLocation, ...]

    def station(self, name: str) -> StationLocation:
        requested = str(name).strip().upper()
        for item in self.stations:
            if requested in (item.key.upper(), item.filename.upper(), item.filename[:-3].upper()):
                return item
        raise KeyError(f'Unknown LCS radio station {name!r}')

    @property
    def original_sizes(self) -> dict[str, int]:
        return {item.key: item.original_size_bytes for item in self.stations}

    def calculate(self, replacement_sizes: Mapping[str,int]) -> PatchCalculation:
        return calculate_plan(self.original_sizes, replacement_sizes)

    def as_dict(self) -> dict:
        return {
            'reference': 'verified pristine US SLUS_214.23 + stock ISO radio records',
            'executable_iso_path': self.executable_iso_path,
            'executable_sha256': self.executable_sha256,
            'cdstream_iso_path': self.cdstream_iso_path,
            'cdstream_sha256': self.cdstream_sha256,
            'original_duration_source': self.original_duration_source,
            'notes': ('There is NO per-station stock length table in the original executable. '
                      'The patcher creates its own ten-entry EE duration table. '
                      'IOP seek correction instructions are shared across stations.'),
            'stations': {item.key: item.as_dict() for item in self.stations},
            'iop_instruction_sites': [
                {'role': 'shared-seek-right', 'irx_file_offset': iop.TEXT_OFFSET+iop.SHR_OFFSET},
                {'role': 'shared-seek-left', 'irx_file_offset': iop.TEXT_OFFSET+iop.SHL_OFFSET},
                *[{'role': 'duration-overflow', 'irx_file_offset': iop.TEXT_OFFSET+x} for x in iop.LENGTH_WORD_OFFSETS],
                *[{'role': 'position-overflow', 'irx_file_offset': iop.TEXT_OFFSET+x} for x in iop.POSITION_WORD_OFFSETS],
            ],
            'inspected_only_not_automatically_modified': ['IOPRP300.IMG', 'CDVDSTM.IRX'],
        }


def locate_original_values(iso: 'Iso9660Image', *, strict_stock: bool = True,
                           validate_executable: bool = True) -> OriginalLocationLookup:
    """Resolve all 10 stock VB entries and exact patch destinations from an ISO.

    A pristine ELF is not proof that its ISO music files are pristine. When
    strict_stock=True reject already-replaced station size records instead of
    calling the modified size an 'original' length. The relaxed option exists
    only for synthetic fixtures with known non-retail station lengths.
    """
    boot_path = iso.find_boot_executable()
    embedded = iso.read_entry_bytes(boot_path)
    if validate_executable:
        exe.validate_original_executable(embedded)
    else:
        # External executable can be supplied to the caller but do not pretend
        # an altered embedded ELF has the original getter. Record actual hash.
        if len(embedded) == 0:
            raise ValueError('Empty boot executable')
    cdstream = iso.read_entry_bytes(IOP_ISO_PATH)
    if hashlib.sha256(cdstream).hexdigest() != iop.ORIGINAL_SHA256:
        raise ValueError('CDSTREAM.IRX is not the authenticated pristine US module; lookup is not safe')
    from lcs_radio_stream_audit import audit_cdstream_module
    audit_cdstream_module(cdstream)  # verify all ten filename strings and order
    if tuple(VERIFIED_STOCK_SIZES) != tuple(k for k, _ in exe.STATIONS):
        raise AssertionError('Reference-size index order differs from station runtime index order')
    items = []
    base = exe.EXPECTED_SEGMENT_OFFSET + exe.LENGTH_TABLE_VA - exe.EXPECTED_SEGMENT_VADDR
    for index, (key, filename) in enumerate(exe.STATIONS):
        iso_path = f'{VB_ISO_PREFIX}/{filename}'
        entry = iso.get(iso_path)
        if entry.is_directory or entry.is_multi_extent:
            raise ValueError(f'{iso_path}: unsupported ISO entry type')
        if strict_stock and entry.size != VERIFIED_STOCK_SIZES[key]:
            raise ValueError(
                f'{iso_path}: input ISO is not pristine for this station: '
                f'observed {entry.size:,} bytes vs verified original '
                f'{VERIFIED_STOCK_SIZES[key]:,} bytes. Choose the original game ISO '
                'or restore its unmodified station records before calculating differences.'
            )
        original_ms = iop.duration_ms(entry.size)
        items.append(StationLocation(
            key=key, filename=filename, iso_path=entry.path,
            original_size_bytes=entry.size, original_duration_ms=original_ms,
            original_extent_lba=entry.extent_lba,
            original_directory_record_file_offset=entry.record_offset,
            original_record_size_field_file_offset=entry.record_offset + 10,
            original_record_extent_field_file_offset=entry.record_offset + 2,
            ee_runtime_duration_table_index=index,
            ee_runtime_duration_table_va=exe.LENGTH_TABLE_VA + 4*index,
            ee_runtime_duration_table_elf_offset=base + 4*index,
        ))
    return OriginalLocationLookup(
        executable_iso_path=boot_path,
        executable_sha256=hashlib.sha256(embedded).hexdigest(),
        cdstream_iso_path=IOP_ISO_PATH,
        cdstream_sha256=hashlib.sha256(cdstream).hexdigest(),
        original_duration_source='Stock CDSTREAM.IRX sceCdSearchFile file-size getter (bytes * 7 / 256)',
        stations=tuple(items),
    )


def make_write_manifest(lookup: OriginalLocationLookup, calculation: PatchCalculation,
                        mode: str) -> dict:
    """Create declarative per-site old/new values; MUST NOT write binary data."""
    sizes = {row.station: row for row in calculation.stations}
    if set(sizes) != set(lookup.original_sizes):
        raise ValueError('Calculator did not cover all ten station entries')
    from lcs_duration_aware_iso_patcher import IOP_PATCH_MODES, COMBINED_EE_MODES
    has_ee = mode not in ('native', 'iop-safe-seek')
    has_iop = mode in IOP_PATCH_MODES
    if calculation.iop_needs_instruction_patch and not has_iop:
        raise ValueError('Requested mode omits required CDSTREAM.IRX instruction modifications')
    if mode not in (*IOP_PATCH_MODES, 'native', *exe.OFFSET_MODES):
        raise ValueError(f'Unknown patch mode {mode!r}; refuse to produce an incomplete patch plan')
    adaptive_mode = {'iop-elf-shared':'shared', 'iop-elf-station-specific':'station-specific'}.get(mode, mode)
    dynamic_offset_mode = adaptive_mode if has_ee else 'none'
    offsets_are_adaptive = adaptive_mode in ('shared', 'station-specific') and has_ee
    durations = [item.ee_boundary_ms for item in calculation.stations]
    longest = max(durations)
    stock_longest = max(item.original_duration_ms for item in lookup.stations)
    offset_count = (exe.choose_offset_entry_count(longest) if offsets_are_adaptive else None)
    iop_sites = []
    for role, addresses, before_words, after_words, required in (
        ('seek-rescale', (iop.SHR_OFFSET, iop.SHL_OFFSET),
         calculation.iop_old_words, calculation.iop_new_words,
         calculation.iop_extra_shift > 0),
        ('duration-overflow', iop.LENGTH_WORD_OFFSETS,
         iop.ORIGINAL_LENGTH_WORDS, iop.FIXED_LENGTH_WORDS,
         calculation.iop_duration_overflow_fix_required),
        ('position-overflow', iop.POSITION_WORD_OFFSETS,
         iop.ORIGINAL_POSITION_WORDS, iop.FIXED_POSITION_WORDS,
         calculation.iop_position_overflow_fix_required),
    ):
        if len(addresses) != len(before_words) or len(addresses) != len(after_words):
            raise AssertionError(f'{role}: IOP patch location/word count differs')
        for address, before, after in zip(addresses, before_words, after_words):
            iop_sites.append({
                'role': role, 'irx_file_offset': iop.TEXT_OFFSET+address,
                'required': bool(required),
                'pristine_expected_word': f'0x{before:08X}',
                'calculated_write_word': f'0x{(after if required else before):08X}',
            })
    padding_cases = [row.station for row in calculation.stations if row.source_size != row.installed_size]
    slots = []
    for site in lookup.stations:
        calc = sizes[site.key]
        if calc.original_size != site.original_size_bytes or calc.original_duration_ms != site.original_duration_ms:
            raise ValueError(f'{site.key}: calculator baseline differs from original ISO lookup')
        slots.append({
            'station': site.key,
            'filename': site.filename,
            'reference_iso_path': site.iso_path,
            'original_size_bytes': site.original_size_bytes,
            'replacement_source_size_bytes': calc.source_size,
            'new_installed_size_bytes': calc.installed_size,
            'size_difference_bytes': calc.size_delta_bytes,
            'original_duration_ms': site.original_duration_ms,
            'new_duration_ms': calc.ee_boundary_ms,
            'duration_difference_ms': calc.duration_delta_ms,
            'iso_record_size_field_offset': site.original_record_size_field_file_offset,
            'ee_table_index': site.ee_runtime_duration_table_index,
            'ee_table_elf_offset': site.ee_runtime_duration_table_elf_offset if has_ee else None,
            'ee_table_va': site.ee_runtime_duration_table_va if has_ee else None,
            'ee_table_write_value_ms': calc.ee_boundary_ms if has_ee else None,
            'iso_size_write_required': calc.source_size != site.original_size_bytes or calc.installed_size != site.original_size_bytes,
        })
    return {
        'mode': mode,
        'executable_getter_replaced': has_ee,
        'iop_calculated_patch_mode': has_iop,
        'station_writes': slots,
        'iop_extra_shift': calculation.iop_extra_shift,
        'iop_duration_fix_required': calculation.iop_duration_overflow_fix_required,
        'iop_position_fix_required': calculation.iop_position_overflow_fix_required,
        'iop_target_instruction_words': [f'0x{x:08X}' for x in calculation.iop_new_words],
        'offset_placement': {
            'mode': dynamic_offset_mode,
            'adaptive': offsets_are_adaptive,
            'stock_longest_duration_ms': stock_longest,
            'installed_longest_duration_ms': longest,
            'replacement_exceeds_stock_longest': longest > stock_longest,
            'calculated_candidate_entries': offset_count,
            'target_spacing_ms': exe.OFFSET_TABLE_TARGET_SPACING_MS if offsets_are_adaptive else None,
            'stock_offset_table_va': exe.STOCK_OFFSET_TABLE_VA,
            'original_offset_pointer_high_elf_offset': exe.OFFSET_PTR_HI_FILE_OFFSET,
            'original_offset_pointer_low_elf_offset': exe.OFFSET_PTR_LO_FILE_OFFSET,
            'adaptive_segment_virtual_address': exe.OFFSET_TABLE_LOAD_VA if offsets_are_adaptive else None,
            'station_helper_hook_elf_offset': exe.OFFSET_HOOK_FILE_OFFSET if adaptive_mode == 'station-specific' else None,
            'runtime_uncertainty': 'Additional PT_LOAD at 0x00680000 not proven immune to PS2 heap/memory overlaps' if offsets_are_adaptive else None,
        },
        'executable_change_sites': ({
            'length_getter_elf_offset': exe.GETTER_FILE_OFFSET,
            'length_table_va': exe.LENGTH_TABLE_VA,
            'length_table_first_elf_offset': lookup.stations[0].ee_runtime_duration_table_elf_offset,
            'offset_pointer_high_elf_offset': exe.OFFSET_PTR_HI_FILE_OFFSET if offsets_are_adaptive else None,
            'offset_pointer_low_elf_offset': exe.OFFSET_PTR_LO_FILE_OFFSET if offsets_are_adaptive else None,
            'station_specific_hook_elf_offset': exe.OFFSET_HOOK_FILE_OFFSET if adaptive_mode == 'station-specific' else None,
            'extra_elf_load_segment': offsets_are_adaptive,
        } if has_ee else None),
        'iop_change_sites': iop_sites,
        'complete_length_patch_path_selected': has_ee and has_iop and offsets_are_adaptive,
        'exact_source_length_preserved': not bool(padding_cases),
        'stations_padded_to_minimum_read_window': padding_cases,
        'short_stream_note': ('Source streams shorter than 0x40000 bytes are SILENCE-PADDED to 0x40000. '
                              'This avoids the identified IOP read-window hazard but DOES NOT preserve '
                              'an exact ~1-second source duration; a true short-stream runtime correction '
                              'is not yet implemented.') if padding_cases else None,
        'unproven_streaming_modules': ['IOPRP300.IMG', 'CDVDSTM.IRX'],

        'partial_mode_warning': (
            None if has_ee and has_iop and offsets_are_adaptive else
            'This is a comparison/control mode and does not apply the combined EE+IOP+adaptive-offset strategy. '
            'A successful file build does not imply all radio-length dependencies were modified.'
        ),
    }


def verify_executable_slots(elf_data: bytes, manifest: dict) -> None:
    """Verify every affected executable component, including adaptive table and hook."""
    if not manifest['executable_getter_replaced']:
        return
    exe_size = len(elf_data)
    if elf_data[exe.GETTER_FILE_OFFSET:exe.GETTER_FILE_OFFSET+len(exe.PATCH_BYTES)] != exe.PATCH_BYTES:
        raise AssertionError('EE duration getter does not contain the intended patch instructions')
    offset_plan = manifest['offset_placement']
    adaptive = offset_plan['adaptive']
    if adaptive:
        if struct.unpack_from('<H', elf_data, exe.ELF32_EHDR_PHNUM_OFFSET)[0] != 2:
            raise AssertionError('Adaptive-offset second ELF program header is missing')
        phdr = struct.unpack_from('<8I', elf_data, exe.SECOND_PHDR_OFFSET)
        p_type, p_offset, p_va, _p_pa, p_filesz, p_memsz, flags, alignment = phdr
        if (p_type, p_va, p_filesz, p_memsz, alignment) != (exe.PT_LOAD, exe.OFFSET_TABLE_LOAD_VA, p_filesz, p_filesz, exe.OFFSET_TABLE_ALIGNMENT):
            raise AssertionError('Adaptive-offset segment header invalid')
        if p_offset < exe.EXPECTED_SIZE or p_offset+p_filesz > len(elf_data) or p_filesz < 12:
            raise AssertionError('Adaptive-offset segment lies outside patched executable')
        if struct.unpack_from('<I', elf_data, exe.OFFSET_PTR_HI_FILE_OFFSET)[0] != 0x3C050068:
            raise AssertionError('Adaptive-offset pointer high word incorrect')
        if struct.unpack_from('<I', elf_data, exe.OFFSET_PTR_LO_FILE_OFFSET)[0] != 0x24A50000:
            raise AssertionError('Adaptive-offset pointer low word incorrect')
        if offset_plan['mode'] == 'shared':
            if flags != exe.OFFSET_DATA_SEGMENT_FLAGS:
                raise AssertionError('Shared offset segment has unexpected permissions')
            n = offset_plan['calculated_candidate_entries']
            if p_filesz != 4 * (n + 2):
                raise AssertionError('Shared-offset segment length does not equal the calculated candidate count')
            words = struct.unpack_from(f'<{n+2}I', elf_data, p_offset)
            if words[0] != 0 or words[-1] != 0 or any(not(0 < x < offset_plan['installed_longest_duration_ms']) for x in words[1:-1]):
                raise AssertionError('Shared-offset payload lacks required sentinels or contains out-of-range positions')
            if len(set(words[1:-1])) != n:
                raise AssertionError('Shared-offset candidates are not unique')
        elif offset_plan['mode'] == 'station-specific':
            if flags != exe.OFFSET_HELPER_SEGMENT_FLAGS:
                raise AssertionError('Station-specific helper must be RWX in the current experimental implementation')
            hook = struct.unpack_from('<2I', elf_data, exe.OFFSET_HOOK_FILE_OFFSET)
            if hook[0] >> 26 != 3 or hook[1] != 0x00438021:
                raise AssertionError('Station-specific helper hook is missing or invalid')
        else:
            raise AssertionError('Unknown adaptive offset strategy')
    else:
        if (struct.unpack_from('<I', elf_data, exe.OFFSET_PTR_HI_FILE_OFFSET)[0] != struct.unpack('<I', exe.EXPECTED_OFFSET_PTR_HI)[0] or
            struct.unpack_from('<I', elf_data, exe.OFFSET_PTR_LO_FILE_OFFSET)[0] != struct.unpack('<I', exe.EXPECTED_OFFSET_PTR_LO)[0]):
            # Non-adaptive patched executables must preserve original stock pointer.
            raise AssertionError('Stock offset pointer was unexpectedly redirected')
    for item in manifest['station_writes']:
        at = item['ee_table_elf_offset']
        if not isinstance(at, int) or at + 4 > exe_size:
            raise AssertionError(f'{item["station"]}: duration table write target absent')
        value = struct.unpack_from('<I', elf_data, at)[0]
        if value != item['ee_table_write_value_ms']:
            raise AssertionError(
                f'{item["station"]}: wrong value at ELF +0x{at:X}: '
                f'{value} != {item["ee_table_write_value_ms"]}'
            )


def verify_iop_words(patched_irx: bytes, pristine_irx: bytes, calculation: PatchCalculation,
                     has_iop_patch_mode: bool) -> None:
    """Check actual module against the full output of the verified IRX writer."""
    if not has_iop_patch_mode:
        if patched_irx != pristine_irx:
            raise AssertionError('IOP module modified despite a non-IOP mode')
        return
    expected, _report = iop.patch_cdstream(pristine_irx, [x.installed_size for x in calculation.stations])
    if patched_irx != expected:
        raise AssertionError('IOP binary differs from calculator-derived authenticated instruction writes')


def verify_output_locations(output_iso: 'Iso9660Image', lookup: OriginalLocationLookup,
                            calculation: PatchCalculation, manifest: dict,
                            original_irx: bytes, expected_ee_sha256: str | None = None) -> None:
    """Verify all calculated destinations in staged output before ISO publication."""
    for site, row in zip(lookup.stations, calculation.stations):
        observed = output_iso.get(site.iso_path).size
        if observed != row.installed_size:
            raise AssertionError(f'{site.key}: ISO directory-record size {observed} != plan {row.installed_size}')
    ee_data = output_iso.read_entry_bytes(lookup.executable_iso_path)
    verify_executable_slots(ee_data, manifest)
    # Compare the entire executable, not just its 40-byte duration table; this
    # also confirms that relocated offsets, helper code and ELF headers reached ISO.
    if expected_ee_sha256 is not None:
        if hashlib.sha256(ee_data).hexdigest() != expected_ee_sha256:
            raise AssertionError('ISO executable differs from the full generated executable')
    if not manifest['executable_getter_replaced'] and hashlib.sha256(ee_data).hexdigest() != lookup.executable_sha256:
        raise AssertionError('Native mode unexpectedly modified the embedded executable')
    irx_data = output_iso.read_entry_bytes(lookup.cdstream_iso_path)
    verify_iop_words(irx_data, original_irx, calculation, manifest['iop_calculated_patch_mode'])
