"""Regression for shortened radio replacements and CDSTREAM fixed read-window bounds."""
import hashlib
import struct
import tempfile
import unittest
from pathlib import Path
from test_lcs_native_provider import make_iso
from test_lcs_iop_safe_seek import SeekInterpreter
import lcs_duration_aware_iso_patcher as iso
import lcs_duration_aware_exe_patcher as exe
from lcs_iop_safe_seek import patch_cdstream, duration_ms
from lcs_stream_bounds import installed_size, READ_CHUNK_BYTES, SILENT_STEREO_BLOCK

BASE = Path(__file__).parent
ELF=BASE / 'SLUS_214.23.original'
IRX=BASE/'CDSTREAM_ORIGINAL_REFERENCE.IRX'

class ShortReadSafety(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.elf=ELF.read_bytes()
        cls.irx=IRX.read_bytes()
        exe.validate_original_executable(cls.elf)
        # Static signatures: exact source module and relevant fixed-size read control.
        assert cls.irx[0xA0+0x1864:0xA0+0x1868]==struct.pack('<I',0x3C020004), 'Expected original 0x40000 max read at .text+0x1864'
        assert cls.irx[0xA0+0x1a04:0xA0+0x1a08]==struct.pack('<I',0x3C030004), 'Expected original second-read window at .text+0x1A04'

    def test_buffer_minimum_enforced_by_staging(self):
        self.assertEqual(installed_size(0x4000),READ_CHUNK_BYTES)
        self.assertEqual(installed_size(0x3c000),READ_CHUNK_BYTES)
        self.assertEqual(installed_size(0x40000),READ_CHUNK_BYTES)
        self.assertEqual(installed_size(0x44000),0x44000)
        for bad in [0,1,0x3800]:
            with self.assertRaises(ValueError):installed_size(bad)

    def test_extremely_short_and_normal_short_full_install(self):
        with tempfile.TemporaryDirectory() as td:
            d=Path(td);source=d/'source.iso';make_iso(source,self.elf,self.irx)
            inp=d/'replacements';inp.mkdir();src=inp/'KJAH.VB'
            for size in (0x4000,0x8000,0x3c000,0x40000,0x44000,0x100000):
                src.write_bytes((b'\x0c\x00'+bytes(14))*(size//16))
                before_hash=hashlib.sha256(src.read_bytes()).hexdigest()
                effective=installed_size(size)
                mode='iop-elf-stock'
                output=d/f'short_{size}.iso'
                res=iso.build_duration_aware_iso(source,inp,output,offset_mode=mode,strict_original_reference=False)
                out=iso.Iso9660Image(output)
                vb=out.read_entry_bytes('AUDIO/MUSIC/KJAH.VB')
                self.assertEqual(len(vb),effective)
                self.assertEqual(vb[:size],src.read_bytes())
                self.assertEqual(vb[size:],SILENT_STEREO_BLOCK*((effective-size)//0x4000))
                self.assertEqual(hashlib.sha256(src.read_bytes()).hexdigest(),before_hash)
                patched=out.read_entry_bytes('SLUS_214.23')
                self.assertNotEqual(patched,self.elf)
                self.assertEqual(out.read_entry_bytes('MODULES/CDSTREAM.IRX'),self.irx)
                self.assertEqual(res['iop_seek_patch_report']['extra_shift'],0)
                off=exe.EXPECTED_SEGMENT_OFFSET+exe.LENGTH_TABLE_VA-exe.EXPECTED_SEGMENT_VADDR+8
                self.assertEqual(struct.unpack_from('<I',patched,off)[0],duration_ms(effective))
                st=next(x for x in res['stations'] if x['station']=='KJAH.VB')
                self.assertEqual(st['original_source_size_bytes'],size)
                self.assertEqual(st['file_size_bytes'],effective)
                if size<READ_CHUNK_BYTES:
                    self.assertEqual(st['short_stream_guard']['silent_padding_bytes'],effective-size)
                else:
                    self.assertIsNone(st['short_stream_guard'])

    def test_gui_preview_and_installer_use_padded_length(self):
        with tempfile.TemporaryDirectory() as td:
            d=Path(td);source=d/'source.iso';make_iso(source,self.elf,self.irx)
            inputs=d/'replacements';inputs.mkdir()
            (inputs/'KJAH.VB').write_bytes((b'\x0c\x00'+bytes(14))*(0x4000//16))
            # Same values presented in GUI when tiny audio has safety padding.
            metrics={key:{'size_bytes':(READ_CHUNK_BYTES if key=='kjah' else 0x4000),
                          'duration_ms':duration_ms(READ_CHUNK_BYTES if key=='kjah' else 0x4000)}
                     for key,_ in exe.STATIONS}
            result=iso.build_duration_aware_iso(source,inputs,d/'out.iso',
                             offset_mode='iop-elf-stock',expected_preview_metrics=metrics,strict_original_reference=False)
            self.assertTrue(result['gui_preview_values_verified'])
            self.assertEqual(next(x for x in result['stations'] if x['station']=='KJAH.VB')['gui_preview_size_bytes'],READ_CHUNK_BYTES)
            metrics['kjah']['size_bytes']=0x4000
            with self.assertRaisesRegex(ValueError,'changed after GUI preview'):
                iso.build_duration_aware_iso(source,inputs,d/'mismatch.iso',
                                 offset_mode='iop-elf-stock',expected_preview_metrics=metrics,strict_original_reference=False)
            self.assertFalse((d/'mismatch.iso').exists())

    def test_overduration_saved_positions_safely_reset_in_iop_seek(self):
        # Execute native original instructions for a shortened radio with
        # old save-state positions far exceeding current duration.
        seek=SeekInterpreter(self.irx)
        for size in [0x40000,0x44000,0x100000,0x400000,0x1000000,0x2000000]:
            d=duration_ms(size)
            for ms in (0,d//2,d-1,d,d+1,2*d,0x7fffffff,0xffffffff):
                ret,byte_offset,_=seek.run(size,ms)
                self.assertGreaterEqual(byte_offset,0,(size,ms,byte_offset))
                self.assertLess(byte_offset,size,(size,ms,byte_offset))
                self.assertEqual(byte_offset%0x4000,0)
                if ms>=d and ms<0x80000000:
                    self.assertEqual(byte_offset,0)

if __name__=='__main__':unittest.main(verbosity=2)
