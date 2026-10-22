"""Regression tests for configurable gain across both GTA PS2 radio ISO builders."""
import json
import math
from pathlib import Path
import subprocess
import tempfile
import unittest

from radio_vb_gain import boost_vb, compile_gain_engine, validate_gain_db
from test_vcs_vcpr_gain_iso import generate_vb, decode_rms
from test_lcs_native_provider import make_iso
from lcs_duration_aware_iso_patcher import build_duration_aware_iso, Iso9660Image
from vcs_vb_gain_install import build_radio_audio_iso

BASE=Path(__file__).parent

class GainValidationTests(unittest.TestCase):
    def test_valid_and_invalid_gain(self):
        for val in (0.1, 0.10005, 1.3, 2.25, 2.99, 3.0, '1.75'):
            self.assertEqual(validate_gain_db(val), float(val))
        for val in (0.0, -0.1, 3.00001, 5, 'nan', 'inf', '-inf', '', 'text', True, None):
            with self.subTest(val=val), self.assertRaises(ValueError):
                validate_gain_db(val)

    def test_encoder_boundary_and_fractional_gains(self):
        with tempfile.TemporaryDirectory() as td:
            d=Path(td); source=d/'HEAD.VB'; generate_vb(source,pairs=2)
            before=source.read_bytes(); initial_rms,_=decode_rms(source)
            for gain in (.1, .75, 1.4, 2.5, 3.0):
                with self.subTest(gain=gain):
                    dst=d/f'gain_{gain}.VB'
                    details=boost_vb(source,dst,gain_db=gain)
                    self.assertAlmostEqual(details['requested_gain_db'],gain,places=7)
                    self.assertEqual(dst.stat().st_size,len(before))
                    new_rms,_=decode_rms(dst)
                    # ADPCM re-encoding is lossy: small gains need not equal
                    # the measured decoded RMS gain exactly.
                    self.assertGreater(new_rms, initial_rms*0.94)
                    self.assertEqual(details['target_clipped_samples'],0)
                    # flags must be preserved frame-by-frame
                    with dst.open('rb') as stream:
                        candidate=stream.read()
                    for pos in range(0,len(before),16):
                        self.assertEqual(candidate[pos+1],before[pos+1])
            self.assertEqual(source.read_bytes(),before)

    def test_reducing_gain_can_avoid_clipping_for_hot_source(self):
        with tempfile.TemporaryDirectory() as td:
            folder=Path(td)
            source=folder/'HOT.VB'
            source.write_bytes(bytes([0x00,0x00]+[0x66]*14)*(0x4000//16))
            small=boost_vb(source,folder/'HALF.VB',gain_db=0.5)
            high=boost_vb(source,folder/'THREE.VB',gain_db=3.0)
            self.assertEqual(small['requested_gain_db'],0.5)
            self.assertEqual(high['requested_gain_db'],3.0)
            self.assertEqual(small['target_clipped_samples'],0)
            self.assertGreater(high['target_clipped_samples'],0)

    def test_gain_rejected_before_output_is_created(self):
        with tempfile.TemporaryDirectory() as td:
            source=Path(td)/'FLASH.VB'; generate_vb(source)
            for val in (0, 3.5, float('nan')):
                dst=Path(td)/'output.VB'
                with self.assertRaises(ValueError):
                    boost_vb(source,dst,gain_db=val)
                self.assertFalse(dst.exists())

    def test_engine_rejects_bad_numeric_inputs(self):
        with tempfile.TemporaryDirectory() as td:
            source=Path(td)/'SRC.VB';generate_vb(source)
            exe=compile_gain_engine()
            for gain in ('-1','0','3.1','nan','inf','1.0xyz'):
                dest=Path(td)/'DST.VB'
                r=subprocess.run([str(exe),str(source),str(dest),gain],capture_output=True,text=True)
                self.assertNotEqual(r.returncode,0, (gain,r.stdout,r.stderr))
                self.assertFalse(dest.exists())
            # Legacy three-argument execution still selects +3.0 dB.
            dest=Path(td)/'LEGACY.VB'
            r=subprocess.run([str(exe),str(source),str(dest)],capture_output=True,text=True)
            self.assertEqual(r.returncode,0,r.stderr)
            self.assertEqual(json.loads(r.stdout)['requested_gain_db'],3.0)

class IntegratedGainTests(unittest.TestCase):
    def test_lcs_multistation_fractional_gain(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); original=root/'lcs.iso'; target=root/'output.iso'; folder=root/'inputs';folder.mkdir()
            make_iso(original,(BASE/'SLUS_214.23.original').read_bytes(),(BASE/'CDSTREAM_ORIGINAL_REFERENCE.IRX').read_bytes())
            for name in ('HEAD.VB','KJAH.VB','LCFR.VB'):
                generate_vb(folder/name,pairs=3)
            report=build_duration_aware_iso(input_iso=original,replacement_dir=folder,
                output_iso=target,offset_mode='native',boost_radio_3db=True,gain_db=1.25,
                strict_original_reference=False)
            self.assertAlmostEqual(report['gain_db'],1.25)
            self.assertEqual(set(report['boosted_replacement_stations']),{'HEAD.VB','KJAH.VB','LCFR.VB'})
            self.assertTrue(all(item['requested_gain_db']==1.25 for item in report['gain_by_station'].values()))
            orig=Iso9660Image(original);built=Iso9660Image(target)
            self.assertEqual(built.read_entry_bytes('AUDIO/MUSIC/DOUBLE.VB'),orig.read_entry_bytes('AUDIO/MUSIC/DOUBLE.VB'))
            self.assertEqual(built.read_entry_bytes('MODULES/CDSTREAM.IRX'),orig.read_entry_bytes('MODULES/CDSTREAM.IRX'))

    def test_vcs_multistation_low_gain(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); original=root/'vcs.iso'; target=root/'out.iso'; folder=root/'inputs'; folder.mkdir()
            make_iso(original,(Path(__file__).with_name('SLUS_215.90.reference').read_bytes()),(Path(__file__).with_name('CDSTREAM_VCS_Experimental_Fixed.IRX').read_bytes()))
            original.write_bytes(original.read_bytes().replace(b'SLUS_214.23',b'SLUS_215.90').replace(b'KJAH.VB',b'VCPR.VB'))
            for name in ('VCPR.VB','FLASH.VB'):generate_vb(folder/name,pairs=3)
            report=build_radio_audio_iso(input_iso=original,replacement_vb=folder,
                output_iso=target,boost_radio_3db=True,gain_db=0.1)
            self.assertEqual(report['gain_db'],.1)
            self.assertEqual(set(report['boosted_replacement_stations']),{'VCPR.VB','FLASH.VB'})
            self.assertTrue(all(abs(item['requested_gain_db']-.1)<1e-10 for item in report['gain_by_station'].values()))
            orig=Iso9660Image(original);built=Iso9660Image(target)
            self.assertEqual(built.read_entry_bytes('MODULES/CDSTREAM.IRX'),orig.read_entry_bytes('MODULES/CDSTREAM.IRX'))
            self.assertEqual(built.read_entry_bytes('AUDIO/MUSIC/HEAD.VB'),orig.read_entry_bytes('AUDIO/MUSIC/HEAD.VB'))

if __name__=='__main__':unittest.main()
