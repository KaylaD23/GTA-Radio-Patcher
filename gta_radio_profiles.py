"""Explicit game profiles for GTA Radio Patcher.

LCS is the existing experimental ISO patch backend. VCS has a narrow,
exact-revision standalone EE overflow fix but no verified ISO/IOP writer.
Never dispatch another game's assets through the LCS writer.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import hashlib


@dataclass(frozen=True)
class GameProfile:
    key: str
    display: str
    game: str
    executable: str
    stations: tuple[tuple[str, str], ...]
    can_build: bool
    description: str


PROFILES = {
    'lcs_ps2': GameProfile(
        'lcs_ps2', 'GTA LCS — PS2 (patching)', 'GTA Liberty City Stories',
        'SLUS_214.23',
        (('head', 'HEAD.VB'), ('double', 'DOUBLE.VB'), ('kjah', 'KJAH.VB'),
         ('rise', 'RISE.VB'), ('lips', 'LIPS.VB'), ('mundo', 'MUNDO.VB'),
         ('msx', 'MSX.VB'), ('flash', 'FLASH.VB'), ('lcj', 'LCJ.VB'),
         ('lcfr', 'LCFR.VB')),
        True, 'Existing experimental LCS PS2 EE + IOP patch backend.'
    ),
    'vcs_ps2': GameProfile(
        'vcs_ps2', 'GTA VCS — PS2 (EE overflow fix)', 'GTA Vice City Stories',
        'SLUS_215.90',
        tuple((s.lower(), s + '.VB') for s in
              ('FLASH', 'VROCK', 'PARADISE', 'VCPR', 'VCFL', 'WAVE', 'FRESH', 'ESPANT', 'EMOTION')),
        False, 'VCS 1s–4h39m calculator, guarded EE position and authenticated CDSTREAM.IRX auto-install in GUI ISO builder. New ELF/IRX load paths are PCSX2-unverified.'
    ),
}

PROFILE_BY_DISPLAY = {profile.display: profile for profile in PROFILES.values()}

# From the user's original VCS binaries. These signatures are diagnostic only;
# they do not license applying LCS opcode changes to VCS.
KNOWN_VCS_ELF_SHA256 = '3b43be57d60a29df35e41504d67bbf728d5ce2fc5aab6bbf928cbbc5963fe547'
KNOWN_VCS_IRX_SHA256 = 'a72b95c6f0db77ba0363196b9d36ce1ac8585560a0730edc4125f29c12b71371'
VCS_RAW_POSITION_THRESHOLD = ((1 << 31) - 1) // 7
VCS_DURATION_FILE_THRESHOLD = ((1 << 32) - 1) // 7


def get_profile(key: str) -> GameProfile:
    try:
        return PROFILES[key]
    except KeyError as exc:
        raise ValueError(f'Unsupported GTA patch profile {key!r}') from exc


def vcs_preview(path: Path) -> dict:
    """Inspect standalone raw PS2 VB without altering its payload.

    VCS duration is based on the verified CDSTREAM size transformation.
    Header plausibility and alignment are not proof of playable audio.
    """
    path = Path(path)
    size = path.stat().st_size
    if not size or size % 16:
        raise ValueError('VCS raw PS2 VB must have a nonzero 16-byte-aligned file size')
    if size > 0xFFFFFFFF:
        raise ValueError('VCS raw VB file size exceeds 32-bit ISO9660 capacity')
    with path.open('rb') as stream:
        start = stream.read(32)
    if start.startswith((b'RIFF', b'OggS', b'VAGp', b'ID3', b'fLaC')):
        raise ValueError('VCS raw VB cannot contain an audio container/header')
    # Duration formula can overflow in VCS's original 32-bit MIPS instructions
    # for large files. Report this risk, do not pretend to compensate for it.
    from vcs_ee_overflow_patch import MIN_ALIGNED_1S_BYTES, MAX_ALIGNED_SAFE_BYTES, MAX_ALIGNED_SAFE_MS
    return {
        'file': str(path),
        'size_bytes': size,
        'expected_duration_ms': size * 7 // 256,
        'duration_arithmetic_overflow_risk': size > VCS_DURATION_FILE_THRESHOLD,
        'signed_position_operand_risk_if_full_byte_position': size > VCS_RAW_POSITION_THRESHOLD,
        'aligned_16kb': size % 0x4000 == 0,
        'within_vcs_1s_to_4h39_target_allocation': (MIN_ALIGNED_1S_BYTES <= size <= MAX_ALIGNED_SAFE_BYTES
                                                   and size % 0x4000 == 0),
        'max_safe_aligned_bytes': MAX_ALIGNED_SAFE_BYTES,
        'max_safe_aligned_duration_ms': MAX_ALIGNED_SAFE_MS,
        'aligned_8kb': size % 0x2000 == 0,
        'read_only': True,
    }


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
