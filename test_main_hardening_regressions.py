"""Focused fail-closed regressions for the uploaded MAIN project baseline.

These are static checks; emulator/PS2 playback is outside this test suite.
"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lcs_patch_calculator import calculate_plan
from lcs_patch_location_registry import VERIFIED_STOCK_SIZES
from lcs_duration_aware_exe_patcher import STATIONS
from lcs_iop_safe_seek import duration_ms, select_extra_shift, patch_file, ORIGINAL_SHA256, sha256

BASE = Path(__file__).resolve().parent

class MainHardeningRegressions(unittest.TestCase):
    def test_calculator_uses_game_radio_index_order_independent_of_mapping(self):
        reversed_original = dict(reversed(list(VERIFIED_STOCK_SIZES.items())))
        original = calculate_plan(VERIFIED_STOCK_SIZES, {'kjah': 367_017_984})
        reversed_plan = calculate_plan(reversed_original, {'kjah': 367_017_984})
        expected = [k for k, _ in STATIONS]
        self.assertEqual([s.station for s in original.stations], expected)
        self.assertEqual([s.station for s in reversed_plan.stations], expected)
        self.assertEqual(original, reversed_plan)

    def test_standalone_iop_calculations_reject_coerced_lengths(self):
        for invalid in (True, False, '16384', 16384.0, 16384.5, None):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                duration_ms(invalid)
            with self.subTest(extra=invalid), self.assertRaises(ValueError):
                select_extra_shift([16_384, invalid])

    def test_standalone_irx_publish_is_atomic_on_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            src = BASE / 'CDSTREAM_ORIGINAL_REFERENCE.IRX'
            self.assertEqual(sha256(src.read_bytes()), ORIGINAL_SHA256)
            output = root / 'CDSTREAM.patched.IRX'
            sentinel = b'EXISTING OUTPUT MUST NOT BE DELETED OR TRUNCATED'
            output.write_bytes(sentinel)
            with mock.patch('lcs_iop_safe_seek.os.replace', side_effect=OSError('simulated atomic-publish error')):
                with self.assertRaisesRegex(OSError, 'simulated'):
                    patch_file(src, output, [367_017_984, 63_176_704])
            self.assertEqual(output.read_bytes(), sentinel)
            self.assertEqual(list(root.glob('*.building')), [])
            self.assertEqual(list(root.glob('.*.building')), [])
            result = patch_file(src, output, [367_017_984, 63_176_704])
            self.assertEqual(sha256(output.read_bytes()), result['output_sha256'])
            self.assertNotEqual(output.read_bytes(), src.read_bytes())
            self.assertEqual(sha256(src.read_bytes()), ORIGINAL_SHA256)

    def test_standalone_irx_never_writes_over_original(self):
        source = BASE / 'CDSTREAM_ORIGINAL_REFERENCE.IRX'
        with self.assertRaises(ValueError):
            patch_file(source, source, [367_017_984, 63_176_704])

if __name__ == '__main__':
    unittest.main()
