"""Actual Tk app switch/button action smoke test; run under Xvfb."""
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import tkinter as tk
import unittest
from gta_radio_patcher_gui import DurationPatcherGUI
from gta_radio_profiles import PROFILES

SOURCE=Path('/mnt/data/CDSTREAM_IRX_VCS_disc_copy.bin')

class GUIRegression(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try: cls.root=tk.Tk();cls.root.withdraw()
        except tk.TclError as e:raise unittest.SkipTest('Display unavailable: '+str(e))
    @classmethod
    def tearDownClass(cls):cls.root.destroy()
    def test_action_follows_game_profile_and_writes_a_new_module(self):
        if not SOURCE.is_file():self.skipTest('Pristine IRX not mounted')
        gui=DurationPatcherGUI(self.root)
        try:
            self.root.update_idletasks()
            self.assertEqual(str(gui.vcs_irx_fix_button.cget('state')),'disabled')
            gui.profile_var.set(PROFILES['vcs_ps2'].display)
            gui._profile_changed()
            self.assertEqual(str(gui.vcs_irx_fix_button.cget('state')),'normal')
            self.assertEqual(str(gui.run_button.cget('state')),'disabled')
            gui.input_vcs_irx_var.set(str(SOURCE))
            with TemporaryDirectory() as td:
                output=Path(td)/'CDSTREAM.NEW.IRX'
                with patch('tkinter.filedialog.asksaveasfilename',return_value=str(output)),\
                     patch('tkinter.messagebox.showinfo') as info,\
                     patch('tkinter.messagebox.showerror') as error:
                    gui._patch_vcs_irx_clicked()
                    self.assertTrue(output.exists())
                    info.assert_called_once()
                    error.assert_not_called()
            gui.profile_var.set(PROFILES['lcs_ps2'].display)
            gui._profile_changed()
            self.assertEqual(str(gui.vcs_irx_fix_button.cget('state')),'disabled')
            self.assertEqual(str(gui.run_button.cget('state')),'normal')
            gui.profile_var.set(PROFILES['vcs_ps2'].display)
            gui._profile_changed()
            self.assertEqual(gui.input_vcs_irx_var.get(),str(SOURCE))
        finally:
            for child in list(self.root.winfo_children()):child.destroy()

if __name__=='__main__':unittest.main()
