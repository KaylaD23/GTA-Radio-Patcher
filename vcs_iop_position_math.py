"""Read-only VCS CDSTREAM.IRX signed position conversion audit.

This module implements (1) the verified old instruction-level arithmetic and
(2) a mathematical candidate for a safe signed 7*a2/8 transformation.
It NEVER writes or patches an IRX. The origin/range of a2 remains a runtime
investigation. This mathematical reference is NOT a validated MIPS patch.
"""
from __future__ import annotations
import hashlib
from pathlib import Path
import struct

KNOWN_IRX_SHA256 = 'a72b95c6f0db77ba0363196b9d36ce1ac8585560a0730edc4125f29c12b71371'
TEXT_FILE_OFFSET = 0xA0
MIPS_WORDS = {
    0x23F4: 0x000410C0, # sll v0, a0, 3    (IOP duration)
    0x23F8: 0x00441023, # subu v0, v0, a0
    0x23FC: 0x000210C2, # srl v0, v0, 3
    0x2410: 0x00021142, # srl v0, v0, 5 (delay slot)
    0x24A4: 0x8C460004, # lw a2, 4(v0)
    0x255C: 0x000610C0, # sll v0, a2, 3
    0x2560: 0x00462823, # subu a1, v0, a2
    0x2564: 0x04A10002, # bgez a1, +2
    0x256C: 0x24A50007, # addiu a1, a1, 7 (negative path)
    0x2580: 0x000510C3, # sra v0, a1, 3
}
U32_MAX = 0xFFFFFFFF
MIN_I32 = -0x80000000
MAX_I32 = 0x7FFFFFFF
SIGNED_ORIGINAL_RAW_THRESHOLD = MAX_I32 // 7


def _require_i32(raw: int) -> int:
    if type(raw) is not int or not MIN_I32 <= raw <= MAX_I32:
        raise ValueError(f'VCS raw IOP a2 operand is not int32: {raw!r}')
    return raw


def _trunc_div(a: int, b: int) -> int:
    if b <= 0: raise ValueError('divisor must be positive')
    return a // b if a >= 0 else -((-a) // b)


def original_iop_signed_position(raw_a2: int) -> int:
    """Exact wrap+bgez+addiu+sra emulation for known original IRX opcodes.

    Important: this returns its *unrepaired* integer result, not its meaning
    in actual game logic. The upstream a2 register has other adjustments.
    """
    x = _require_i32(raw_a2)
    product = (x * 7) & U32_MAX
    signed = product if product <= MAX_I32 else product - (1 << 32)
    if signed < 0:
        signed = (signed + 7) & U32_MAX
        signed = signed if signed <= MAX_I32 else signed - (1 << 32)
    return signed >> 3


def ideal_signed_position(raw_a2: int) -> int:
    """Independent arbitrary precision mathematical ground truth."""
    x = _require_i32(raw_a2)
    return _trunc_div(x * 7, 8)


def proposed_safe_split(raw_a2: int) -> int:
    """Overflow-free, 32-bit-safe algebraic decomposition, including negatives.

    q=trunc(a2/8), r=a2-8*q. Then trunc(7*a2/8) equals
    7*q + trunc(7*r/8). Every intermediate fits signed int32 for any
    signed int32 a2; *how to implement this in MIPS remains to be proven*.
    """
    x = _require_i32(raw_a2)
    q = _trunc_div(x, 8)
    remainder = x - q * 8
    result = 7 * q + _trunc_div(remainder * 7, 8)
    assert MIN_I32 <= result <= MAX_I32
    return result


def audit_original_irx(path: Path) -> dict:
    data = Path(path).read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if digest != KNOWN_IRX_SHA256:
        raise ValueError('Unrecognized CDSTREAM.IRX revision; refusing to reuse offsets')
    for text_offset, expected in MIPS_WORDS.items():
        actual = struct.unpack_from('<I', data, TEXT_FILE_OFFSET + text_offset)[0]
        if actual != expected:
            raise ValueError(f'IRX .text+0x{text_offset:X}: expected 0x{expected:08X}, got 0x{actual:08X}')
    return dict(sha256=digest, instruction_sites_verified=len(MIPS_WORDS),
                original_binary_preserved=True, binary_patch_applied=False)
