"""GUI regression: no assumed KJAH size; any selected station is measured live.

The headless Tk test exercises the real GUI callbacks (no stubbed calculator).
"""
import tempfile
import tkinter as tk
import unittest
from pathlib import Path

from lcs_duration_aware_patcher_gui import DurationPatcherGUI, OFFSET_MODE_LABELS

BASE = Path(__file__).parent

class GuiDynamicSizes(unittest.TestCase):
    def test_preview_before_iso_and_after_size_change_without_reselect(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            audio = work / 'HEAD.VB'
            audio.write_bytes(b'\x0c\x00' + bytes(14))
            # Invalid 16B file demonstrates immediate size/alignment validation.
            root = tk.Tk(); root.withdraw()
            try:
                gui = DurationPatcherGUI(root)
                gui.radio_dir_var.set(str(audio))
                gui._refresh_audio_preview()
                self.assertEqual(gui.preview_metrics, {})
                self.assertIn('multiple of 0x4000',gui.tree.item('head','values')[-1])
                # A truly short source is reported as a source->installed pair,
                # not disguised as a 256 KiB file the user never selected.
                with audio.open('wb') as f: f.truncate(0x4000)
                gui._poll_replacement_file_changes()
                self.assertEqual(gui.tree.item('head','values')[3], '16,384 → 262,144')
                self.assertIn('00:00:07.168',gui.tree.item('head','values')[4])
                # Selecting a valid arbitrary station file triggers standalone
                # duration preview even before a complete ISO exists.
                with audio.open('wb') as f: f.truncate(0x80000)
                gui._poll_replacement_file_changes()
                row = gui.tree.item('head','values')
                self.assertEqual(row[3],f'{0x80000:,}')
                self.assertIn('00:00:14.336',row[4])
                self.assertIsNone(gui.current_calculation)
                self.assertEqual(gui.preview_metrics,{})
                # The input ISO supplies the canonical sizes of the other nine
                # stations. No hardcoded original station lengths permitted.
                iso = Path('/mnt/data/GTA_LCS_reconstructed/Grand Theft Auto Liberty City Stories PS2.iso')
                if not iso.is_file():
                    return  # No retail ISO available on this portable test machine.
                gui.input_iso_var.set(str(iso))
                gui._refresh_audio_preview()
                self.assertEqual(gui.current_calculation.stations[0].source_size,0x80000)
                self.assertEqual(gui.current_calculation.stations[0].ee_boundary_ms,0x80000*7//256)
                self.assertEqual(gui.preview_metrics['head']['size_bytes'],0x80000)
                self.assertEqual(gui.preview_metrics['kjah']['size_bytes'],63_176_704)
                # User changes the exact same HEAD.VB on disk: metadata polling
                # silently refreshes it without asking them to reselect it.
                with audio.open('wb') as f: f.truncate(0x200000)
                gui._poll_replacement_file_changes()
                self.assertEqual(gui.preview_metrics['head']['size_bytes'],0x200000)
                self.assertEqual(gui.preview_metrics['head']['duration_ms'],0x200000*7//256)
                # Mode change must update the boundary descriptor rather than
                # leaving a stale "patched" indicator from an earlier mode.
                gui.offset_mode_var.set(next(k for k,v in OFFSET_MODE_LABELS.items() if v=='iop-safe-seek'))
                gui._offset_mode_changed()
                self.assertIn('native:',gui.tree.item('head','values')[5])
                gui.offset_mode_var.set(next(k for k,v in OFFSET_MODE_LABELS.items() if v=='iop-elf-stock'))
                gui._offset_mode_changed()
                self.assertIn('patched:',gui.tree.item('head','values')[5])
                # The calculator distinguishes changed durations across stations.
                self.assertEqual(gui.current_calculation.stations[2].source_size,63_176_704)
                self.assertEqual(gui.current_calculation.stations[2].ee_boundary_ms,63_176_704*7//256)
            finally:
                root.destroy()

if __name__=='__main__':unittest.main()
