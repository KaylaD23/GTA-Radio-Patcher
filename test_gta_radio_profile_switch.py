"""Game dispatch smoke tests; ensure VCS can never invoke the LCS writer."""
from __future__ import annotations
import tempfile
import unittest
import tkinter as tk
from pathlib import Path
from unittest.mock import patch

from gta_radio_profiles import PROFILES, get_profile, KNOWN_VCS_ELF_SHA256
from gta_radio_backends import get_backend, UnverifiedGameWriterError
from vcs_radio_calculator import calculate_plan as vcs_calculate, duration_ms as vcs_duration


class ProfileDispatchTests(unittest.TestCase):
    def test_profile_station_maps_are_isolated(self):
        lcs = get_profile('lcs_ps2')
        vcs = get_profile('vcs_ps2')
        self.assertEqual(len(lcs.stations), 10)
        self.assertEqual(len(vcs.stations), 9)
        self.assertIn(('vcpr', 'VCPR.VB'), vcs.stations)
        self.assertIn(('kjah', 'KJAH.VB'), lcs.stations)
        self.assertNotIn('kjah', dict(vcs.stations))
        self.assertIsNot(get_backend('lcs_ps2'), get_backend('vcs_ps2'))
        self.assertEqual(vcs.executable, 'SLUS_215.90')
        self.assertEqual(lcs.executable, 'SLUS_214.23')

    def test_vcs_size_calculator_is_independent_of_lcs(self):
        plan = get_backend('vcs_ps2').calculate({'vcpr': 142_606_336}, {'vcpr': 367_017_984})
        row = next(s for s in plan.stations if s.station == 'vcpr')
        self.assertEqual(row.original_duration_ms, 3_899_392)
        self.assertEqual(row.replacement_duration_ms, 10_035_648)
        self.assertEqual(row.duration_delta_ms, 6_136_256)
        self.assertEqual(row.classification, 'longer')
        self.assertTrue(row.position_operand_risk_if_raw_bytes)
        self.assertFalse(row.duration_overflow_risk)
        self.assertFalse(plan.original_sizes_complete)
        self.assertEqual(len(plan.stations),9)
        self.assertIsNone(next(s for s in plan.stations if s.station == 'wave').ee_boundary_ms)
        with self.assertRaises(ValueError):
            get_backend('vcs_ps2').calculate({}, {'kjah': 0x4000})
        with self.assertRaises(ValueError):
            get_backend('lcs_ps2').calculate({}, {'vcpr': 0x4000})

    def test_vcs_reverse_shorten_is_signed_not_unsigned(self):
        plan=vcs_calculate({'vcpr':142_606_336}, {'vcpr':134_004_736})
        row=next(s for s in plan.stations if s.station=='vcpr')
        self.assertEqual(row.classification,'shorter')
        self.assertEqual(row.size_delta_bytes,-8_601_600)
        self.assertEqual(row.duration_delta_ms,-235_200)
        self.assertEqual(row.ee_boundary_ms,3_664_192)
        self.assertGreaterEqual(row.ee_boundary_ms,0)

    def test_vcs_size_rejects_bad_count_and_lcs_padding_not_applied(self):
        for invalid in (0, -0x4000, 19, 10.5, True, '16384'):
            with self.subTest(size=invalid),self.assertRaises(ValueError):
                vcs_calculate({}, {'vcpr':invalid})
        row = next(s for s in vcs_calculate({}, {'vcpr': 0x4000}).stations if s.station=='vcpr')
        self.assertEqual(row.installed_size,0x4000)
        self.assertEqual(row.ee_boundary_ms, 448)

    def test_vcs_writer_always_fails_closed_even_with_output(self):
        with tempfile.TemporaryDirectory() as d:
            target=Path(d)/'out.iso'
            with patch('lcs_duration_aware_iso_patcher.build_duration_aware_iso',
                       side_effect=AssertionError('Wrong game writer invoked')):
                with self.assertRaises(UnverifiedGameWriterError):
                    get_backend('vcs_ps2').build(output_iso=target)
            self.assertFalse(target.exists())

    def test_lcs_remains_connected_to_authoritative_plan_and_writer(self):
        from lcs_patch_location_registry import VERIFIED_STOCK_SIZES
        plan=get_backend('lcs_ps2').calculate(VERIFIED_STOCK_SIZES,{'kjah':367_017_984})
        self.assertEqual(len(plan.stations),10)
        self.assertEqual(next(x for x in plan.stations if x.station == 'kjah').ee_boundary_ms,10_035_648)
        sentinel={'correct_lcs_writer': True}
        with patch('lcs_duration_aware_iso_patcher.build_duration_aware_iso',return_value=sentinel) as fake:
            self.assertEqual(get_backend('lcs_ps2').build(input_iso='x',output_iso='y'),sentinel)
            fake.assert_called_once()

    def test_vcs_partial_reference_zip_reads_only_known_original_sizes(self):
        sample = Path('/mnt/data/GTA VCS PS2.zip')
        if not sample.is_file():
            self.skipTest('Supplied VCS reference archive not mounted')
        original = get_backend('vcs_ps2').inspect_original_source(sample)
        self.assertEqual(original, {'vcpr': 142_606_336})
        plan = get_backend('vcs_ps2').calculate(original, {'vcpr': 367_017_984})
        row = next(x for x in plan.stations if x.station == 'vcpr')
        self.assertEqual(row.duration_delta_ms,6_136_256)
        self.assertEqual(row.classification,'longer')
        self.assertFalse(plan.original_sizes_complete)

    def test_vcs_reference_executable_matches_hash(self):
        import zipfile
        sample=Path('/mnt/data/GTA VCS PS2.zip')
        if not sample.is_file():
            self.skipTest('VCS supplied zip not available')
        with zipfile.ZipFile(sample) as z, tempfile.TemporaryDirectory() as d:
            target=Path(d)/'SLUS_215.90'
            target.write_bytes(z.read('SLUS_215.90'))
            result=get_backend('vcs_ps2').inspect_executable(target)
            self.assertEqual(result['sha256'],KNOWN_VCS_ELF_SHA256)
            self.assertTrue(result['recognized_reference'])
            self.assertFalse(result['safe_to_write'])


class ProfileGUIIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.root=tk.Tk()
            cls.root.withdraw()
        except tk.TclError as e:
            raise unittest.SkipTest('Tk display unavailable: '+str(e))

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls,'root'):
            cls.root.destroy()

    def test_profile_switch_changes_station_table_backend_and_actions(self):
        from gta_radio_patcher_gui import DurationPatcherGUI
        gui=DurationPatcherGUI(self.root)
        self.root.update_idletasks()
        self.assertEqual(gui.profile_key,'lcs_ps2')
        self.assertEqual(len(gui.tree.get_children()),10)
        self.assertEqual(str(gui.run_button.cget('state')),'normal')
        gui.input_iso_var.set('/virtual/lcs_disc.iso')
        gui.radio_dir_var.set('/virtual/kjah')
        gui.output_iso_var.set('/virtual/out_lcs.iso')
        gui.profile_var.set(PROFILES['vcs_ps2'].display)
        gui._profile_changed()
        self.assertEqual(gui.profile_key,'vcs_ps2')
        self.assertEqual(len(gui.tree.get_children()),9)
        self.assertIn('vcpr',gui.tree.get_children())
        self.assertNotIn('kjah',gui.tree.get_children())
        self.assertEqual(str(gui.run_button.cget('state')),'disabled')
        self.assertEqual(str(gui.offset_mode_combo.cget('state')),'disabled')
        self.assertEqual(gui.input_iso_var.get(),'')
        self.assertEqual(gui.radio_dir_var.get(),'')
        gui.profile_var.set(PROFILES['lcs_ps2'].display)
        gui._profile_changed()
        self.assertEqual(gui.profile_key,'lcs_ps2')
        self.assertEqual(len(gui.tree.get_children()),10)
        self.assertEqual(gui.input_iso_var.get(),'/virtual/lcs_disc.iso')
        self.assertEqual(gui.radio_dir_var.get(),'/virtual/kjah')
        self.assertEqual(gui.output_iso_var.get(),'/virtual/out_lcs.iso')
        self.assertEqual(str(gui.run_button.cget('state')),'normal')

    def test_vcs_gui_with_reference_zip_uses_partial_stock_values(self):
        sample = Path('/mnt/data/GTA VCS PS2.zip')
        if not sample.is_file():
            self.skipTest('Supplied VCS reference archive not mounted')
        from gta_radio_patcher_gui import DurationPatcherGUI
        gui=DurationPatcherGUI(self.root)
        gui.profile_var.set(PROFILES['vcs_ps2'].display)
        gui._profile_changed()
        gui.input_iso_var.set(str(sample))
        with tempfile.TemporaryDirectory() as d:
            f=Path(d)/'VCPR.VB'; f.write_bytes(bytes(0x4000))
            gui.radio_dir_var.set(str(f))
            gui._refresh_audio_preview()
            self.assertEqual(gui.current_calculation.stations[3].classification,'shorter')
            self.assertEqual(gui.current_calculation.stations[3].original_size,142_606_336)
            self.assertFalse(gui.current_calculation.original_sizes_complete)
        self.assertEqual(str(gui.run_button.cget('state')),'disabled')

    def test_vcs_gui_preview_runs_vcs_only(self):
        from gta_radio_patcher_gui import DurationPatcherGUI
        gui=DurationPatcherGUI(self.root)
        gui.profile_var.set(PROFILES['vcs_ps2'].display)
        gui._profile_changed()
        with tempfile.TemporaryDirectory() as d:
            f=Path(d)/'VCPR.VB';f.write_bytes(bytes(0x4000))
            gui.radio_dir_var.set(str(f))
            with patch('lcs_patch_calculator.calculate_plan',
                       side_effect=AssertionError('LCS calculator invoked for VCS')):
                gui._refresh_audio_preview()
            self.assertEqual(gui.current_calculation.selected_replacement_count,1)
            self.assertIn('448', gui.tree.item('vcpr','values')[4])  # HH:MM:SS.448
            self.assertFalse(gui.current_calculation.original_sizes_complete)
            self.assertEqual(str(gui.run_button.cget('state')),'disabled')

if __name__=='__main__': unittest.main()
