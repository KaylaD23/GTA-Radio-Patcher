import tkinter as tk
from unittest import TestCase
from unittest.mock import patch
import tempfile
from pathlib import Path
from gta_radio_patcher_gui import DurationPatcherGUI
from gta_radio_profiles import PROFILES

class VCSToggleTest(TestCase):
    def test_checkbox_enabled_for_both_games(self):
        try:
            root=tk.Tk();root.withdraw()
        except tk.TclError as e:
            self.skipTest(f'No graphical display: {e}')
        try:
            gui=DurationPatcherGUI(root)
            root.update_idletasks()
            self.assertFalse(gui.vcs_boost_3db_var.get())
            self.assertEqual(str(gui.vcs_gain_checkbox.cget('state')),'normal')
            self.assertEqual(str(gui.vcs_iso_install_button.cget('state')),'disabled')
            gui.profile_var.set(PROFILES['vcs_ps2'].display)
            gui._profile_changed()
            self.assertEqual(str(gui.vcs_gain_checkbox.cget('state')),'normal')
            self.assertEqual(str(gui.vcs_iso_install_button.cget('state')),'normal')
            gui.vcs_boost_3db_var.set(True)
            self.assertTrue(gui.vcs_boost_3db_var.get())
            gui.profile_var.set(PROFILES['lcs_ps2'].display)
            gui._profile_changed()
            self.assertEqual(str(gui.vcs_iso_install_button.cget('state')),'disabled')
            self.assertEqual(str(gui.radio_gain_checkbox.cget('state')),'normal')
        finally:
            root.destroy()

    def test_single_named_vb_can_be_assigned_to_vcpr(self):
        try:
            root=tk.Tk();root.withdraw()
        except tk.TclError as e:
            self.skipTest(f'No graphical display: {e}')
        try:
            gui=DurationPatcherGUI(root)
            gui.profile_var.set(PROFILES['vcs_ps2'].display)
            gui._profile_changed()
            with tempfile.TemporaryDirectory() as td:
                folder=Path(td)
                (folder/'disc.iso').write_bytes(b'placeholder')
                (folder/'my_modded_vcpr.vb').write_bytes(b'placeholder')
                gui.input_iso_var.set(str(folder/'disc.iso'))
                gui.radio_dir_var.set(str(folder/'my_modded_vcpr.vb'))
                gui.output_iso_var.set(str(folder/'new.iso'))
                gui.single_station_var.set('VCPR.VB')
                with patch('gta_radio_patcher_gui.messagebox.askyesno', return_value=False) as confirm, \
                     patch('gta_radio_patcher_gui.messagebox.showerror') as error:
                    gui._vcs_install_clicked()
                    confirm.assert_called_once()
                    error.assert_not_called()
        finally:
            root.destroy()
