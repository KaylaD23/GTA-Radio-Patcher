"""Synthetic, independent regression for optional +3 dB on LCS and VCS.

No game audio redistributed; checks unchanged lengths, station routing, and
byte-preservation of unselected station and EE/IOP payloads.
"""
from __future__ import annotations
import hashlib
from pathlib import Path
import tempfile
import unittest
from lcs_duration_aware_iso_patcher import Iso9660Image, build_duration_aware_iso
from test_lcs_native_provider import make_iso
from test_vcs_vcpr_gain_iso import generate_vb, decode_rms
from vcs_vb_gain_install import build_radio_audio_iso, _get_replacements
from radio_vb_gain import boost_vb

BASE = Path(__file__).resolve().parent

class GenericRadioGainTests(unittest.TestCase):
    def test_lcs_multiple_stations_boosted_during_main_patcher(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); source=root/'lcs.iso'; result_iso=root/'out.iso'; folder=root/'replacements'; folder.mkdir()
            make_iso(source,(BASE/'SLUS_214.23.original').read_bytes(),
                     (BASE/'CDSTREAM_ORIGINAL_REFERENCE.IRX').read_bytes())
            for name in ('HEAD.VB','KJAH.VB'):
                generate_vb(folder/name, pairs=32)
            original_hash=hashlib.sha256(source.read_bytes()).hexdigest()
            original=Iso9660Image(source)
            results=build_duration_aware_iso(input_iso=source,replacement_dir=folder,
                output_iso=result_iso,offset_mode='native',boost_radio_3db=True,strict_original_reference=False)
            self.assertEqual(set(results['gain_by_station']), {'HEAD.VB','KJAH.VB'})
            patched=Iso9660Image(result_iso)
            self.assertEqual(patched.read_entry_bytes('SLUS_214.23'),original.read_entry_bytes('SLUS_214.23'))
            self.assertEqual(patched.read_entry_bytes('MODULES/CDSTREAM.IRX'),original.read_entry_bytes('MODULES/CDSTREAM.IRX'))
            self.assertEqual(patched.read_entry_bytes('AUDIO/MUSIC/RISE.VB'),original.read_entry_bytes('AUDIO/MUSIC/RISE.VB'))
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(),original_hash)
            for name in ('HEAD.VB','KJAH.VB'):
                self.assertEqual(patched.get('AUDIO/MUSIC/'+name).size,(folder/name).stat().st_size)
                with tempfile.TemporaryDirectory() as outdir:
                    path=Path(outdir)/name
                    patched.extract_entry('AUDIO/MUSIC/'+name,path)
                    self.assertNotEqual(path.read_bytes(),(folder/name).read_bytes())
                    rms0,_=decode_rms(folder/name); rms1,_=decode_rms(path)
                    import math
                    self.assertAlmostEqual(20*math.log10(rms1/rms0),3.0,delta=0.6)
            self.assertEqual(len(results['gain_by_station']), 2)

    def test_lcs_short_station_padding_still_runs_after_gain(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); iso=root/'original.iso'; out=root/'output.iso'; folder=root/'replacement'; folder.mkdir()
            make_iso(iso,(BASE/'SLUS_214.23.original').read_bytes(),
                     (BASE/'CDSTREAM_ORIGINAL_REFERENCE.IRX').read_bytes())
            original=folder/'LCFR.VB'; generate_vb(original,pairs=4)
            result=build_duration_aware_iso(input_iso=iso,replacement_dir=folder,
                output_iso=out,offset_mode='native',boost_radio_3db=True,
                strict_original_reference=False)
            installed=Iso9660Image(out).read_entry_bytes('AUDIO/MUSIC/LCFR.VB')
            self.assertEqual(len(installed),0x40000)
            self.assertNotEqual(installed[:original.stat().st_size],original.read_bytes())
            self.assertEqual(result['gain_by_station']['LCFR.VB']['bytes'],original.stat().st_size)
            self.assertEqual(result['stations'][-1]['short_stream_guard']['installed_size_bytes'],0x40000)

    def test_vcs_non_vcpr_station_and_multiple_replacements(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);iso=root/'vcs.iso';out=root/'out.iso';folder=root/'inputs';folder.mkdir()
            make_iso(iso,b'ELF__VCS_TEST',b'IRX__VCS_TEST')
            raw=iso.read_bytes().replace(b'SLUS_214.23',b'SLUS_215.90').replace(b'KJAH.VB',b'VCPR.VB')
            iso.write_bytes(raw)
            generate_vb(folder/'VCPR.VB',pairs=4)
            generate_vb(folder/'FLASH.VB',pairs=4)
            original=Iso9660Image(iso)
            result=build_radio_audio_iso(input_iso=iso,replacement_vb=folder,output_iso=out,boost_radio_3db=True)
            self.assertEqual(set(result['gain_by_station']),{'FLASH.VB','VCPR.VB'})
            self.assertEqual(set(result['iso_targets']),{'AUDIO/MUSIC/FLASH.VB','AUDIO/MUSIC/VCPR.VB'})
            image=Iso9660Image(out)
            self.assertEqual(image.read_entry_bytes('SLUS_215.90'),original.read_entry_bytes('SLUS_215.90'))
            self.assertEqual(image.read_entry_bytes('MODULES/CDSTREAM.IRX'),original.read_entry_bytes('MODULES/CDSTREAM.IRX'))
            self.assertEqual(image.read_entry_bytes('AUDIO/MUSIC/HEAD.VB'),original.read_entry_bytes('AUDIO/MUSIC/HEAD.VB'))
            for name in ('VCPR.VB','FLASH.VB'):
                self.assertEqual(image.get('AUDIO/MUSIC/'+name).size,(folder/name).stat().st_size)
                self.assertNotEqual(image.read_entry_bytes('AUDIO/MUSIC/'+name),(folder/name).read_bytes())

    def test_generic_single_vb_requires_explicit_station_if_unnamed(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'custom_radio.vb';generate_vb(path)
            with self.assertRaisesRegex(ValueError,'Specify a VCS station'):
                _get_replacements(path)
            self.assertEqual(set(_get_replacements(path,'EMOTION')),{'EMOTION.VB'})
            with self.assertRaisesRegex(ValueError,'Station override'):
                _get_replacements(Path(temp),'FLASH')

    def test_gain_refuses_overwrite_and_unaligned(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);a=root/'A.VB';b=root/'B.VB';generate_vb(a)
            boost_vb(a,b)
            with self.assertRaises(FileExistsError):boost_vb(a,b)
            # Nonempty yet unaligned input must be rejected.
            (root/'broken.VB').write_bytes(b'\x00'*2048)
            with self.assertRaisesRegex(ValueError,'0x4000'):
                boost_vb(root/'broken.VB',root/'new.VB')

if __name__=='__main__':unittest.main()

class AllStationDetectionRegression(unittest.TestCase):
    """Exercise every station, not just the earlier two-station example."""

    def test_lcs_all_ten_stations_detected_and_boosted(self):
        from gta_radio_profiles import get_profile
        stations = [filename for _, filename in get_profile('lcs_ps2').stations]
        self.assertEqual(len(stations), 10)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            iso, output, sources = root/'lcs.iso', root/'all10.iso', root/'replacements'
            sources.mkdir()
            make_iso(iso, (BASE/'SLUS_214.23.original').read_bytes(),
                     (BASE/'CDSTREAM_ORIGINAL_REFERENCE.IRX').read_bytes())
            original = Iso9660Image(iso)
            for name in stations:
                generate_vb(sources/name.lower(), pairs=2)  # case-insensitive detection
            changes = build_duration_aware_iso(input_iso=iso, replacement_dir=sources,
                output_iso=output, offset_mode='native', boost_radio_3db=True,
                strict_original_reference=False)
            self.assertEqual(set(changes['detected_replacement_stations']), set(stations))
            self.assertEqual(set(changes['boosted_replacement_stations']), set(stations))
            self.assertEqual(set(changes['gain_by_station']), set(stations))
            built = Iso9660Image(output)
            self.assertEqual(original.read_entry_bytes('SLUS_214.23'),built.read_entry_bytes('SLUS_214.23'))
            for name in stations:
                self.assertEqual(changes['gain_by_station'][name]['bytes'],2*0x4000)
                # Short-file streaming safety padding is applied after boosting.
                self.assertEqual(built.get('AUDIO/MUSIC/'+name).size,0x40000)
                self.assertNotEqual(built.read_entry_bytes('AUDIO/MUSIC/'+name)[:2*0x4000],
                                    (sources/name.lower()).read_bytes())

    def test_lcs_odd_single_station_is_not_special_cased(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            iso=root/'lcs.iso'; out=root/'one.iso'; source=root/'mundo.vb'
            make_iso(iso,(BASE/'SLUS_214.23.original').read_bytes(),
                     (BASE/'CDSTREAM_ORIGINAL_REFERENCE.IRX').read_bytes())
            generate_vb(source,pairs=2)
            result=build_duration_aware_iso(input_iso=iso,replacement_dir=source,
                output_iso=out,offset_mode='native',boost_radio_3db=True,strict_original_reference=False)
            self.assertEqual(result['detected_replacement_stations'], ['MUNDO.VB'])
            self.assertEqual(result['boosted_replacement_stations'], ['MUNDO.VB'])
            before=Iso9660Image(iso); after=Iso9660Image(out)
            for station in ('HEAD.VB','KJAH.VB','LCFR.VB'):
                self.assertEqual(after.read_entry_bytes('AUDIO/MUSIC/'+station),
                                 before.read_entry_bytes('AUDIO/MUSIC/'+station))

    def test_vcs_all_nine_stations_detected_and_boosted(self):
        from gta_radio_profiles import get_profile
        from test_lcs_native_provider import make_iso as fixture_make_iso
        from unittest.mock import patch
        import lcs_duration_aware_exe_patcher as patcher
        names = [filename for _,filename in get_profile('vcs_ps2').stations]
        self.assertEqual(len(names),9)
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            iso=root/'vcs.iso';out=root/'out.iso'; folder=root/'all9';folder.mkdir()
            # Generate a synthetic ISO with the complete VCS station directory.
            with patch.object(patcher,'STATIONS',get_profile('vcs_ps2').stations):
                fixture_make_iso(iso,b'VCS_TEST_EXECUTABLE',b'VCS_TEST_IRX')
            data=iso.read_bytes().replace(b'SLUS_214.23',b'SLUS_215.90')
            iso.write_bytes(data)
            for name in names: generate_vb(folder/name.lower(),pairs=4)
            original=Iso9660Image(iso)
            result=build_radio_audio_iso(input_iso=iso,replacement_vb=folder,
                output_iso=out,boost_radio_3db=True)
            self.assertEqual(set(result['detected_replacement_stations']),set(names))
            self.assertEqual(set(result['boosted_replacement_stations']),set(names))
            built=Iso9660Image(out)
            self.assertEqual(built.read_entry_bytes('SLUS_215.90'),original.read_entry_bytes('SLUS_215.90'))
            self.assertEqual(built.read_entry_bytes('MODULES/CDSTREAM.IRX'),original.read_entry_bytes('MODULES/CDSTREAM.IRX'))
            for name in names:
                self.assertEqual(built.get('AUDIO/MUSIC/'+name).size,4*0x4000)
                self.assertNotEqual(built.read_entry_bytes('AUDIO/MUSIC/'+name),(folder/name.lower()).read_bytes())

    def test_vcs_selected_subset_only_gets_gain(self):
        from gta_radio_profiles import get_profile
        from test_lcs_native_provider import make_iso as fixture_make_iso
        from unittest.mock import patch
        import lcs_duration_aware_exe_patcher as patcher
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);iso=root/'in.iso';out=root/'out.iso';folder=root/'subset';folder.mkdir()
            with patch.object(patcher,'STATIONS',get_profile('vcs_ps2').stations):
                fixture_make_iso(iso,b'VCS_TEST_EXECUTABLE',b'VCS_TEST_IRX')
            iso.write_bytes(iso.read_bytes().replace(b'SLUS_214.23',b'SLUS_215.90'))
            for name in ('WAVE.VB','EMOTION.VB','PARADISE.VB'):
                generate_vb(folder/name, pairs=3)
            original=Iso9660Image(iso)
            result=build_radio_audio_iso(input_iso=iso,replacement_vb=folder,
                output_iso=out,boost_radio_3db=True)
            self.assertEqual(set(result['boosted_replacement_stations']),
                             {'WAVE.VB','EMOTION.VB','PARADISE.VB'})
            built=Iso9660Image(out)
            for name in ('VROCK.VB','VCPR.VB','VCFL.VB'):
                self.assertEqual(built.read_entry_bytes('AUDIO/MUSIC/'+name),
                                 original.read_entry_bytes('AUDIO/MUSIC/'+name))
