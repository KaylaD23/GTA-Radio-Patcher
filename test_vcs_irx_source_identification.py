"""Regression for LCS-vs-VCS IRX selection and read-only ISO extraction.

Uses the user-supplied experimental VCS output to regenerate its exact
original input for offline tests; distributes no additional game asset.
Tests do NOT certify gameplay or IOP loader compatibility.
"""
from __future__ import annotations
import hashlib
import math
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gta_radio_backends import get_backend
from vcs_cdstream_signed_position_patch import (
    EXPECTED_SHA, TEXT_OFF, SITES, build, classify_irx, explain_irx_rejection,
)

BASE = Path(__file__).resolve().parent
B = 2048


def reconstruct_original() -> bytes:
    """Reverse the exact known experimental IRX section-insertion transform."""
    patched = (BASE / 'CDSTREAM_VCS_Experimental_Fixed.IRX').read_bytes()
    shift = 0xb460 - 0x81a0 + 48
    tail = patched[0x8240 + shift:-40]
    tail = tail[:0x54] + tail[0x54 + 8:]
    raw = bytearray(patched[:0x8240] + tail)
    assert len(raw) == 48540
    struct.pack_into('<I', raw, 0x20, 0xbbe4)
    struct.pack_into('<H', raw, 0x30, 11)
    struct.pack_into('<II', raw, 0x34 + 32 + 16, 0x81a0, 0xb460)
    for offset in (0x2560, 0x2564, 0x2580):
        struct.pack_into('<I', raw, TEXT_OFF + offset, SITES[offset])
    for i in range(11):
        pos = 0xbbe4 + i * 40
        off = struct.unpack_from('<I', raw, pos + 16)[0]
        if off >= 0xb584 + 8:
            off -= 8
        if off >= 0x8240 + shift:
            off -= shift
        struct.pack_into('<I', raw, pos + 16, off)
        if i == 10:
            struct.pack_into('<I', raw, pos + 20, 0x54)
    assert hashlib.sha256(raw).hexdigest() == EXPECTED_SHA
    assert build(raw)[0] == patched
    return bytes(raw)


def entry(name: bytes, lba: int, size: int, directory=False) -> bytes:
    length = 33 + len(name) + (len(name) % 2 == 0)
    r = bytearray(length)
    r[0] = length
    struct.pack_into('<I', r, 2, lba)
    struct.pack_into('>I', r, 6, lba)
    struct.pack_into('<I', r, 10, size)
    struct.pack_into('>I', r, 14, size)
    r[25] = 2 if directory else 0
    struct.pack_into('<H', r, 28, 1)
    struct.pack_into('>H', r, 30, 1)
    r[32] = len(name)
    r[33:33 + len(name)] = name
    return bytes(r)


def create_tiny_vcs_iso(path: Path, irx: bytes, *, boot: str = 'SLUS_215.90') -> None:
    irx_sector = 23
    sectors = max(64, irx_sector + math.ceil(len(irx) / B) + 1)
    raw = bytearray(sectors * B)
    pvd = memoryview(raw)[16 * B:17 * B]
    pvd[0] = 1
    pvd[1:6] = b'CD001'
    pvd[6] = 1
    struct.pack_into('<I', pvd, 80, sectors)
    struct.pack_into('>I', pvd, 84, sectors)
    struct.pack_into('<H', pvd, 128, B)
    struct.pack_into('>H', pvd, 130, B)
    pvd[156:156 + len(entry(b'\x00', 20, B, True))] = entry(b'\x00', 20, B, True)
    raw[17 * B:17 * B + 7] = b'\xffCD001\x01'
    root_entries = b''.join([
        entry(b'\x00', 20, B, True),
        entry(b'\x01', 20, B, True),
        entry((boot + ';1').encode(), 21, 32),
        entry(b'MODULES', 22, B, True),
    ])
    raw[20 * B:20 * B + len(root_entries)] = root_entries
    raw[21 * B:21 * B + 32] = b'X' * 32
    mods = b''.join([entry(b'\x00', 22, B, True), entry(b'\x01', 20, B, True),
                     entry(b'CDSTREAM.IRX;1', irx_sector, len(irx))])
    raw[22 * B:22 * B + len(mods)] = mods
    raw[irx_sector * B:irx_sector * B + len(irx)] = irx
    path.write_bytes(raw)


class VCSModuleChoiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pristine = reconstruct_original()
        cls.patched = (BASE / 'CDSTREAM_VCS_Experimental_Fixed.IRX').read_bytes()
        cls.lcs = (BASE / 'CDSTREAM_ORIGINAL_REFERENCE.IRX').read_bytes()

    def test_classification_no_hash_whitelist_expansion(self):
        self.assertEqual(classify_irx(self.pristine)['status'], 'original_vcs')
        self.assertTrue(classify_irx(self.pristine)['patchable'])
        self.assertEqual(classify_irx(self.patched)['status'], 'already_patched_vcs')
        self.assertFalse(classify_irx(self.patched)['patchable'])
        self.assertEqual(classify_irx(self.lcs)['status'], 'lcs_reference')
        self.assertIn('Liberty City Stories', explain_irx_rejection(classify_irx(self.lcs)))
        other = bytearray(self.pristine)
        other[200] ^= 1
        self.assertEqual(classify_irx(other)['status'], 'unsupported')
        self.assertIn('SHA-256', explain_irx_rejection(classify_irx(other)))

    def test_read_vcs_module_from_iso_and_patch_no_iso_mutation(self):
        with tempfile.TemporaryDirectory() as d:
            inp, out = Path(d)/'vcs.iso', Path(d)/'position.fixed.IRX'
            create_tiny_vcs_iso(inp, self.pristine)
            orig_digest = hashlib.sha256(inp.read_bytes()).hexdigest()
            backend = get_backend('vcs_ps2')
            self.assertEqual(backend.read_pristine_cdstream_from_iso(inp), self.pristine)
            result = backend.patch_cdstream_position_overflow_from_iso(inp, out, {'vcpr': 367017984})
            self.assertEqual(out.read_bytes(), self.patched)
            self.assertEqual(result['original_sha256'], EXPECTED_SHA)
            self.assertEqual(hashlib.sha256(inp.read_bytes()).hexdigest(), orig_digest)
            self.assertFalse(backend.profile.can_build)
            with self.assertRaises(ValueError):
                backend.patch_cdstream_position_overflow_from_iso(inp, out, {})
            self.assertEqual(out.read_bytes(), self.patched)

    def test_iso_rejects_wrong_region_and_known_lcs_and_patched(self):
        backend = get_backend('vcs_ps2')
        for boot, data in [('SLES_546.22', self.pristine), ('SLUS_215.90', self.lcs),
                           ('SLUS_215.90', self.patched), ('SLUS_215.90', b'garbage')]:
            with self.subTest(boot=boot, status=classify_irx(data)['status']):
                with tempfile.TemporaryDirectory() as d:
                    inp, out = Path(d)/'vcs.iso', Path(d)/'out.IRX'
                    create_tiny_vcs_iso(inp, data, boot=boot)
                    with self.assertRaises(ValueError):
                        backend.patch_cdstream_position_overflow_from_iso(inp, out)
                    self.assertFalse(out.exists())

    def test_gui_click_from_iso_overrides_unrelated_lcs_browse_field(self):
        import tkinter as tk
        from gta_radio_patcher_gui import DurationPatcherGUI
        from gta_radio_profiles import PROFILES
        try:
            root = tk.Tk()
            root.withdraw()
        except tk.TclError as exc:
            self.skipTest('No active X11/Wayland display: ' + str(exc))
        try:
            with tempfile.TemporaryDirectory() as d:
                inp, out = Path(d)/'vcs.iso', Path(d)/'patched.IRX'
                create_tiny_vcs_iso(inp, self.pristine)
                gui = DurationPatcherGUI(root)
                gui.profile_var.set(PROFILES['vcs_ps2'].display)
                gui._profile_changed()
                self.assertEqual(str(gui.vcs_iso_irx_fix_button.cget('state')), 'normal')
                gui.input_iso_var.set(str(inp))
                gui.input_vcs_irx_var.set(str(BASE/'CDSTREAM_ORIGINAL_REFERENCE.IRX'))
                with patch('tkinter.filedialog.asksaveasfilename', return_value=str(out)), \
                     patch('tkinter.messagebox.showinfo') as good, \
                     patch('tkinter.messagebox.showerror') as bad:
                    gui._patch_vcs_irx_from_iso_clicked()
                    self.assertEqual(out.read_bytes(), self.patched)
                    good.assert_called_once()
                    bad.assert_not_called()
                with patch('tkinter.filedialog.asksaveasfilename') as save, \
                     patch('tkinter.messagebox.showerror') as bad:
                    gui._patch_vcs_irx_clicked()
                    bad.assert_called_once()
                    save.assert_not_called()
                    self.assertIn('Liberty City Stories', bad.call_args.args[1])
                gui.profile_var.set(PROFILES['lcs_ps2'].display)
                gui._profile_changed()
                self.assertEqual(str(gui.vcs_iso_irx_fix_button.cget('state')), 'disabled')
        finally:
            root.destroy()


if __name__ == '__main__':
    unittest.main()
