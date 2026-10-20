import unittest
from vcs_radio_calculator import calculate_plan

class TestVCSOverflowCalculator(unittest.TestCase):
    def test_original_and_new_profiles(self):
        p=calculate_plan({'vcpr':142606336},{'vcpr':367017984})
        self.assertTrue(p.ee_signed_duration_to_bytes_overflow)
        self.assertTrue(p.ee_divu_fix_range_supported_for_all_known_streams)
        r={r.station:r for r in p.stations}['vcpr']
        self.assertTrue(r.ee_signed_duration_to_bytes_overflow)
        self.assertTrue(r.ee_divu_fix_range_supported)
        self.assertEqual(r.replacement_duration_ms,10035648)
    def test_five_hour_cannot_use_divu_only(self):
        p=calculate_plan({'vcpr':142606336},{'vcpr':658292736})
        self.assertFalse(p.ee_divu_fix_range_supported_for_all_known_streams)
        self.assertTrue(p.duration_instruction_overflow_risk)
    def test_stock_unchanged(self):
        p=calculate_plan({'vcpr':142606336},{})
        self.assertFalse(p.ee_signed_duration_to_bytes_overflow)

if __name__=='__main__':unittest.main()
