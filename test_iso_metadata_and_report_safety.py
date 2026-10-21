"""Independent regression guards added by the application-wide 2026-10-21 audit."""
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

from gta_radio_profiles import get_profile
from lcs_duration_aware_iso_patcher import apply_iso_replacements, Iso9660Image
from test_lcs_native_provider import make_iso as make_game_fixture
from test_lcs_additional_safety import make_iso as make_minimal_fixture
from vcs_vb_gain_install import build_radio_audio_iso
import lcs_duration_aware_exe_patcher as exe

B = 2048


class MetadataAndReportSafetyTests(unittest.TestCase):
    def test_rejects_file_aliasing_iso_path_table_and_does_not_publish(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            src, audio, dst = root / 'source.iso', root / 'replacement.dat', root / 'output.iso'
            make_minimal_fixture(src)
            content = bytearray(src.read_bytes())
            path_table_lba = 24
            struct.pack_into('<I', content, 16*B + 132, 100)
            struct.pack_into('>I', content, 16*B + 136, 100)
            struct.pack_into('<I', content, 16*B + 140, path_table_lba)
            content[path_table_lba * B:path_table_lba * B + 100] = b'P' * 100
            record = content.index(b'FILE_A.DAT;1') - 33
            struct.pack_into('<I', content, record + 2, path_table_lba)
            struct.pack_into('>I', content, record + 6, path_table_lba)
            src.write_bytes(content)
            audio.write_bytes(b'X' * 90)
            with self.assertRaisesRegex(ValueError, 'overlaps reserved filesystem metadata'):
                apply_iso_replacements(src, dst, {'FILE_A.DAT':audio})
            self.assertFalse(dst.exists())
            self.assertEqual(src.read_bytes(),content)

    def test_normal_primary_iso_replacement_still_succeeds(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); src=root/'source.iso'; audio=root/'replacement.dat'; dst=root/'output.iso'
            make_minimal_fixture(src)
            audio.write_bytes(b'B' * 90)
            report=apply_iso_replacements(src, dst, {'FILE_A.DAT':audio})
            self.assertTrue(report)
            self.assertEqual(Iso9660Image(dst).read_entry_bytes('FILE_A.DAT'),b'B'*90)

    def test_vcs_report_directory_created_and_valid_iso_published(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); iso=root/'original.iso'; radio=root/'VCPR.VB'; out=root/'result.iso'; report=root/'reports'/'result.json'
            with patch.object(exe, 'STATIONS', get_profile('vcs_ps2').stations):
                make_game_fixture(iso,b'EE',b'IRX')
            iso.write_bytes(iso.read_bytes().replace(b'SLUS_214.23',b'SLUS_215.90'))
            radio.write_bytes(b'\x0c\x00'+bytes(14)+bytes(3*0x4000-16))
            result=build_radio_audio_iso(input_iso=iso,replacement_vb=radio,output_iso=out,report_path=report)
            self.assertTrue(out.exists())
            self.assertTrue(report.exists())
            self.assertNotIn('report_write_error',result)
            self.assertEqual(Iso9660Image(out).get('AUDIO/MUSIC/VCPR.VB').size,3*0x4000)

    def test_vcs_report_write_failure_returns_success_with_warning(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); iso=root/'original.iso'; radio=root/'VCPR.VB'; out=root/'result.iso'; blocker=root/'blocked_parent';blocker.write_text('file')
            with patch.object(exe, 'STATIONS', get_profile('vcs_ps2').stations):
                make_game_fixture(iso,b'EE',b'IRX')
            iso.write_bytes(iso.read_bytes().replace(b'SLUS_214.23',b'SLUS_215.90'))
            radio.write_bytes(b'\x0c\x00'+bytes(14)+bytes(3*0x4000-16))
            warnings=[]
            result=build_radio_audio_iso(input_iso=iso,replacement_vb=radio,output_iso=out,
                                         report_path=blocker/'result.json',progress=warnings.append)
            self.assertTrue(out.exists())
            self.assertIn('report_write_error',result)
            self.assertTrue(any('WARNING' in item for item in warnings))
            self.assertEqual(Iso9660Image(out).get('AUDIO/MUSIC/VCPR.VB').size,3*0x4000)

if __name__=='__main__':
    unittest.main()

class ADPMCGainHeadroomTests(unittest.TestCase):
    def test_full_scale_input_reports_clipping_not_false_exact_three_db(self):
        from radio_vb_gain import boost_vb
        with tempfile.TemporaryDirectory() as d:
            folder=Path(d);source=folder/'LOUD.VB';output=folder/'BOOST.VB'
            source.write_bytes(bytes([0x00,0x00]+[0x77]*14) * (0x4000//16))
            result=boost_vb(source,output)
            self.assertEqual(result['requested_gain_db'], 3.0)
            self.assertGreater(result['target_clipped_samples'],0)
            self.assertLess(result['measured_decoded_rms_gain_db'],3.0)
            self.assertEqual(source.stat().st_size,output.stat().st_size)
