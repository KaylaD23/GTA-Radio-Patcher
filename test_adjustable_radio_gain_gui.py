"""Tk GUI validation and VCS confirmation for adjustable stereo VB gain."""
import tkinter as tk
from pathlib import Path
import tempfile
from unittest import TestCase
from unittest.mock import patch
from gta_radio_patcher_gui import DurationPatcherGUI
from gta_radio_profiles import PROFILES

class GainControlTests(TestCase):
    def setUp(self):
        self.root=tk.Tk()
        self.root.withdraw()
        self.gui=DurationPatcherGUI(self.root)
    def tearDown(self):
        self.root.destroy()

    def test_checkbox_enables_numeric_selector_for_both_games(self):
        gui=self.gui
        self.assertFalse(gui.boost_radio_3db_var.get())
        self.assertEqual(str(gui.radio_gain_spinbox.cget('state')), 'disabled')
        gui.boost_radio_3db_var.set(True)
        self.assertEqual(str(gui.radio_gain_spinbox.cget('state')), 'normal')
        for key in ('lcs_ps2','vcs_ps2'):
            gui.profile_var.set(PROFILES[key].display)
            gui._profile_changed()
            gui.radio_gain_db_var.set('0.75')
            self.assertAlmostEqual(gui._selected_gain_db(), .75)
            self.assertEqual(str(gui.radio_gain_spinbox.cget('state')), 'normal')
        gui.boost_radio_3db_var.set(False)
        self.assertEqual(str(gui.radio_gain_spinbox.cget('state')), 'disabled')
        self.assertEqual(gui._selected_gain_db(),0)

    def test_out_of_range_gain_rejected_in_gui(self):
        gui=self.gui
        gui.boost_radio_3db_var.set(True)
        for gain in ('0','4','nan','invalid',''):
            with self.subTest(gain=gain), self.assertRaises(ValueError):
                gui.radio_gain_db_var.set(gain)
                gui._selected_gain_db()

    def test_vcs_install_confirmation_uses_selected_numeric_gain(self):
        gui=self.gui
        gui.profile_var.set(PROFILES['vcs_ps2'].display)
        gui._profile_changed()
        gui.boost_radio_3db_var.set(True)
        gui.radio_gain_db_var.set('1.25')
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            (root/'disc.iso').write_bytes(b'placeholder')
            (root/'VCPR.VB').write_bytes(b'placeholder')
            gui.input_iso_var.set(str(root/'disc.iso'))
            gui.radio_dir_var.set(str(root/'VCPR.VB'))
            gui.output_iso_var.set(str(root/'new.iso'))
            with patch('gta_radio_patcher_gui.messagebox.askyesno',return_value=False) as confirmation, \
                 patch('gta_radio_patcher_gui.messagebox.showerror') as error:
                gui._vcs_install_clicked()
                confirmation.assert_called_once()
                self.assertIn('+1.25 dB ENABLED',confirmation.call_args.args[1])
                error.assert_not_called()
            gui.radio_gain_db_var.set('3.1')
            with patch('gta_radio_patcher_gui.messagebox.askyesno') as confirmation, \
                 patch('gta_radio_patcher_gui.messagebox.showerror') as error:
                gui._vcs_install_clicked()
                confirmation.assert_not_called()
                error.assert_called_once()
