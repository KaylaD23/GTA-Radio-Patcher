"""Stateful VCS executable recognition and GUI preflight regression tests.

No ISO writer runs, no original is overwritten, and no backup is created.
"""
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import unittest

from vcs_ee_overflow_patch import (
    ORIGINAL_SHA256, PATCHED_SHA256, PATCHES,
    AlreadyPatchedError, UnsupportedBinaryError,
    check_pristine, classify_executable, patch_file, patched_bytes, sha256,
)
from gta_radio_backends import get_backend

REFERENCE = Path(__file__).with_name('SLUS_215.90.reference')


class RecognitionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.original = REFERENCE.read_bytes()
        cls.corrected = patched_bytes(cls.original)

    def test_original_and_exact_patched_states(self):
        original = classify_executable(self.original)
        patched = classify_executable(self.corrected)
        self.assertEqual(original['status'], 'original')
        self.assertEqual(original['sha256'], ORIGINAL_SHA256)
        self.assertTrue(original['patchable'])
        self.assertFalse(original['already_patched'])
        self.assertEqual(patched['status'], 'already_patched')
        self.assertEqual(patched['sha256'], PATCHED_SHA256)
        self.assertFalse(patched['patchable'])
        self.assertTrue(patched['already_patched'])
        self.assertEqual(len(patched['patch_sites']), 2)

    def test_exact_patched_file_reports_already_patched_without_output(self):
        with TemporaryDirectory() as folder:
            folder = Path(folder)
            source = folder / 'SLUS_215.90'
            target = folder / 'patched.elf'
            source.write_bytes(self.corrected)
            with self.assertRaisesRegex(AlreadyPatchedError, 'already patched'):
                patch_file(source, target)
            self.assertFalse(target.exists())
            self.assertEqual(source.read_bytes(), self.corrected)
            self.assertFalse(list(folder.glob('*.bak')))

    def test_unknown_revision_same_original_opcodes_rejected(self):
        mutant = bytearray(self.original)
        mutant[0xA000] ^= 1  # outside supported DIV and guards
        status = classify_executable(mutant)
        self.assertEqual(status['status'], 'unsupported')
        self.assertFalse(status['recognized_reference'])
        self.assertIn('SHA-256 differs', status['reason'])
        with self.assertRaises(UnsupportedBinaryError):
            patched_bytes(mutant)

    def test_same_divu_but_extra_modification_is_not_accepted(self):
        mutant = bytearray(self.corrected)
        mutant[0xA000] ^= 1
        status = classify_executable(mutant)
        self.assertEqual(status['status'], 'unsupported')
        self.assertFalse(status['already_patched'])
        self.assertIn('additional changes', status['reason'])

    def test_partial_patch_state_rejected(self):
        mutant = bytearray(self.original)
        offset, _, new, _, _ = PATCHES[0]
        mutant[offset:offset + 4] = new.to_bytes(4, 'little')
        status = classify_executable(mutant)
        self.assertEqual(status['status'], 'unsupported')
        self.assertIn('Partial', status['reason'])
        with self.assertRaises(UnsupportedBinaryError):
            patched_bytes(mutant)

    def test_unknown_size_and_guard_rejected(self):
        self.assertIn('Unsupported executable size', classify_executable(b'bad')['reason'])
        mutant = bytearray(self.original)
        mutant[0xF8A0] ^= 1
        self.assertIn('instruction guards differ', classify_executable(mutant)['reason'])

    def test_backend_reports_supported_patched_state_but_not_patchable(self):
        with TemporaryDirectory() as folder:
            # Common real-world workflow: users rename the output executable.
            source = Path(folder) / 'Patched SLUS_215.90'
            source.write_bytes(self.corrected)
            result = get_backend('vcs_ps2').inspect_executable(source)
            self.assertEqual(result['status'], 'already_patched')
            self.assertTrue(result['recognized_reference'])
            self.assertFalse(result['patchable'])
            self.assertFalse(result['safe_to_write'])


class GUIRecognitionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import tkinter as tk
        try:
            cls.root = tk.Tk()
            cls.root.withdraw()
        except tk.TclError as exc:
            raise unittest.SkipTest('GUI display not available: ' + str(exc))

    @classmethod
    def tearDownClass(cls):
        cls.root.destroy()

    def _gui(self):
        from gta_radio_patcher_gui import DurationPatcherGUI
        from gta_radio_profiles import PROFILES
        gui = DurationPatcherGUI(self.root)
        gui.profile_var.set(PROFILES['vcs_ps2'].display)
        gui._profile_changed()
        return gui

    def test_patched_input_does_not_open_save_dialog(self):
        with TemporaryDirectory() as folder:
            source = Path(folder) / 'Patched SLUS_215.90'
            source.write_bytes(patched_bytes(REFERENCE.read_bytes()))
            gui = self._gui()
            try:
                gui.input_exe_var.set(str(source))
                with patch('tkinter.filedialog.asksaveasfilename') as ask, \
                     patch('tkinter.messagebox.showinfo') as info, \
                     patch('tkinter.messagebox.showerror') as error:
                    gui._patch_vcs_executable_clicked()
                    ask.assert_not_called()
                    info.assert_called_once()
                    error.assert_not_called()
                    self.assertIn('already patched', gui.status_var.get())
                    self.assertEqual(list(Path(folder).iterdir()), [source])
            finally:
                for child in list(self.root.winfo_children()):
                    child.destroy()

    def test_pristine_input_still_patches_standalone_output(self):
        with TemporaryDirectory() as folder:
            source = Path(folder) / 'SLUS_215.90'
            target = Path(folder) / 'SLUS_215.90.overflow_fixed'
            source.write_bytes(REFERENCE.read_bytes())
            gui = self._gui()
            try:
                gui.input_exe_var.set(str(source))
                with patch('tkinter.filedialog.asksaveasfilename', return_value=str(target)), \
                     patch('tkinter.messagebox.showinfo') as info, \
                     patch('tkinter.messagebox.showerror') as error:
                    gui._patch_vcs_executable_clicked()
                    info.assert_called_once()
                    error.assert_not_called()
                self.assertEqual(sha256(target.read_bytes()), PATCHED_SHA256)
                self.assertEqual(sha256(source.read_bytes()), ORIGINAL_SHA256)
            finally:
                for child in list(self.root.winfo_children()):
                    child.destroy()

    def test_modified_original_rejected_without_save_dialog(self):
        with TemporaryDirectory() as folder:
            source = Path(folder) / 'SLUS_215.90'
            mutant = bytearray(REFERENCE.read_bytes())
            mutant[0xA000] ^= 1
            source.write_bytes(mutant)
            gui = self._gui()
            try:
                gui.input_exe_var.set(str(source))
                with patch('tkinter.filedialog.asksaveasfilename') as ask, \
                     patch('tkinter.messagebox.showinfo') as info, \
                     patch('tkinter.messagebox.showerror') as error:
                    gui._patch_vcs_executable_clicked()
                    ask.assert_not_called()
                    error.assert_called_once()
                    info.assert_not_called()
            finally:
                for child in list(self.root.winfo_children()):
                    child.destroy()


if __name__ == '__main__':
    unittest.main()
