"""Game-specific function dispatch for GTA Radio Patcher.

No executable, stream-code or ISO writer from one game is allowed to service
another game's profile. Incomplete backends deliberately fail closed.
"""
from __future__ import annotations
from pathlib import Path
from zipfile import ZipFile, BadZipFile
from typing import Mapping
from gta_radio_profiles import get_profile, KNOWN_VCS_ELF_SHA256, sha256_bytes


class UnverifiedGameWriterError(RuntimeError):
    pass


class LCSPS2Backend:
    key = 'lcs_ps2'
    @property
    def profile(self):
        return get_profile(self.key)

    def calculate(self, original_sizes: Mapping[str, int], replacement_sizes: Mapping[str, int]):
        from lcs_patch_calculator import calculate_plan
        return calculate_plan(original_sizes, replacement_sizes)

    def inspect_iso(self, iso: Path, executable: Path | None = None):
        from lcs_duration_aware_iso_patcher import inspect_input_iso
        return inspect_input_iso(Path(iso), Path(executable) if executable else None)

    def build(self, **kwargs):
        from lcs_duration_aware_iso_patcher import build_duration_aware_iso
        return build_duration_aware_iso(**kwargs)


class VCSPS2Backend:
    key = 'vcs_ps2'
    @property
    def profile(self):
        return get_profile(self.key)

    def calculate(self, original_sizes: Mapping[str, int | None], replacement_sizes: Mapping[str, int]):
        from vcs_radio_calculator import calculate_plan
        return calculate_plan(original_sizes, replacement_sizes)

    def inspect_iso(self, iso: Path, executable: Path | None = None):
        """Read VCS ISO station lengths without triggering any LCS authentication.

        Shared ISO9660 *filesystem reader* is format-level utility only.
        No LCS game-specific opcodes or offset patches are ever used.
        """
        from lcs_duration_aware_iso_patcher import Iso9660Image
        image = Iso9660Image(Path(iso))
        image.get(self.profile.executable)
        # The supplied VCS disc has a malformed, overlapping ISO9660 VCPR entry:
        # its declared length extends into later tracks. Fail closed rather than
        # feeding fictitious metadata to the VCS calculator or a future writer.
        all_files = sorted((e for e in image.entries.values() if not e.is_directory),
                           key=lambda e: e.data_lba)
        for i, entry in enumerate(all_files):
            start = entry.data_lba * 2048
            end = start + entry.size
            if end > image.size:
                raise ValueError(f'VCS ISO file {entry.path} extends past end of image: corrupt file-size metadata')
            if i + 1 < len(all_files) and end > all_files[i + 1].data_lba * 2048:
                raise ValueError(f'VCS ISO file {entry.path} overlaps {all_files[i+1].path}: '
                                 'invalid ISO9660 file-size metadata; rebuild the disc before patching')
        result = {key: image.get('AUDIO/MUSIC/' + filename).size for key, filename in self.profile.stations}
        if executable is not None:
            candidate = Path(executable)
            if candidate.name.upper() != self.profile.executable:
                raise ValueError(f'VCS selected: expected {self.profile.executable}, not {candidate.name}')
            digest = sha256_bytes(candidate.read_bytes())
            if digest != KNOWN_VCS_ELF_SHA256:
                raise ValueError('VCS executable differs from known SLUS_215.90 reference: refusing to assume opcode compatibility')
        return result

    def inspect_original_source(self, source: Path, executable: Path | None = None) -> dict:
        """Original VCS ISO OR loose-reference ZIP (partial size lookup).

        The supplied reference archive has `AUDIO/MUSIC/OG VCPR.VB` rather than
        the retail ISO path. This is explicitly recognized as a partial
        source: we cannot deduce the other eight stock radio sizes from it.
        """
        source = Path(source)
        if source.suffix.lower() != '.zip':
            return self.inspect_iso(source, executable)
        with ZipFile(source) as z:
            entries = {n.upper(): info for n in z.namelist()
                       if not (info := z.getinfo(n)).is_dir()}
            if 'SLUS_215.90' not in entries:
                raise ValueError('VCS reference ZIP does not contain SLUS_215.90')
            if sha256_bytes(z.read(entries['SLUS_215.90'].filename)) != KNOWN_VCS_ELF_SHA256:
                raise ValueError('Unrecognized VCS reference executable inside ZIP')
            if executable is not None:
                self.inspect_executable(executable)
            originals = {}
            for key, filename in self.profile.stations:
                name = 'AUDIO/MUSIC/' + filename
                info = entries.get(name)
                if info is not None:
                    originals[key] = info.file_size
            # Labelled user reference: OG VCPR rather than an ISO entry name.
            reference = entries.get('AUDIO/MUSIC/OG VCPR.VB')
            if reference is not None and 'vcpr' not in originals:
                originals['vcpr'] = reference.file_size
            if not originals:
                raise ValueError('VCS reference ZIP contains no identifiable original radio files')
            return originals

    def inspect_executable(self, executable: Path) -> dict:
        executable = Path(executable)
        if executable.name.upper() != self.profile.executable:
            raise ValueError(f'Wrong game executable: VCS requires {self.profile.executable}')
        data = executable.read_bytes()
        digest = sha256_bytes(data)
        return {'file': str(executable), 'sha256': digest,
                'recognized_reference': digest == KNOWN_VCS_ELF_SHA256,
                'safe_to_write': False}

    def patch_executable_overflow_only(self, pristine_executable: Path, new_executable: Path,
                                      replacement_sizes: Mapping[str, int] | None = None) -> dict:
        """Write a separate, exact-revision SLUS_215.90 with the two proven
        unsigned EE conversion changes. Does NOT touch any disc/ISO/IRX.
        """
        from vcs_ee_overflow_patch import patch_file
        replacements = replacement_sizes or {}
        unknown = set(replacements) - {name for name, _ in self.profile.stations}
        if unknown:
            raise ValueError(f'Unknown VCS station keys: {sorted(unknown)}')
        return patch_file(Path(pristine_executable), Path(new_executable), list(replacements.values()))

    def patch_executable_with_adaptive_offsets(self, executable: Path, new_executable: Path,
                                               replacement_sizes: Mapping[str, int]) -> dict:
        """VCS-only opt-in shared ~5-second offsets plus EE DIVU fixes.

        Accepts exactly authenticated original or two-DIVU-only ELF; refuses
        every other patched variant, preserving unrecognized user changes.
        """
        from vcs_adaptive_offsets import patch_file
        unknown = set(replacement_sizes) - {name for name, _ in self.profile.stations}
        if unknown:
            raise ValueError(f'Unknown VCS station keys: {sorted(unknown)}')
        return patch_file(Path(executable), Path(new_executable), replacement_sizes)

    def patch_cdstream_position_overflow_only(self, pristine_irx: Path, new_irx: Path,
                                              replacement_sizes: Mapping[str, int] | None = None) -> dict:
        """VCS-only experimental signed IOP position arithmetic correction.

        Uses an exact-revision MIPS patch; never touches LCS or a game ISO.
        No in-game guarantee or automatic ISO installation is claimed.
        """
        from vcs_cdstream_signed_position_patch import build
        from vcs_ee_overflow_patch import validate_radio_sizes
        import os
        import json
        original = Path(pristine_irx)
        output = Path(new_irx)
        if original.resolve() == output.resolve() or output.exists():
            raise ValueError('The pristine IRX must be preserved; output must be a new file')
        sizes = replacement_sizes or {}
        unknown = set(sizes) - {name for name, _ in self.profile.stations}
        if unknown:
            raise ValueError(f'Unknown VCS station keys: {sorted(unknown)}')
        if sizes:
            validate_radio_sizes(list(sizes.values()))
        result, metadata = build(original.read_bytes())
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open('xb') as f:
            f.write(result)
        return metadata

    def build(self, **kwargs):
        raise UnverifiedGameWriterError(
            'GTA VCS PS2 is analysis-only: its SLUS_215.90 and CDSTREAM.IRX binary '
            'patcher, runtime seek correctness and write verification are not yet established. '
            'Refusing to apply GTA LCS patches to a VCS image.')


BACKENDS = {'lcs_ps2': LCSPS2Backend(), 'vcs_ps2': VCSPS2Backend()}


def get_backend(key: str):
    get_profile(key)
    return BACKENDS[key]
