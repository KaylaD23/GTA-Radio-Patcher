"""Full ISO writer regression: 40-minute original replaced by 30-minute VB.

Checks that both physical ISO size and EE runtime length resolve to NEW duration,
without leaving a logically accessible 10-minute tail. Authenticated real EE/IOP
modules run against a synthetic ISO filesystem; not a PCSX2 gameplay trace.
"""
from __future__ import annotations
import struct
import tempfile
import unittest
from pathlib import Path

import lcs_duration_aware_exe_patcher as exe
import lcs_duration_aware_iso_patcher as iso
from lcs_iop_safe_seek import duration_ms, patch_cdstream
from test_lcs_native_provider import make_iso
from test_lcs_iop_safe_seek import SeekInterpreter

ROOT = Path(__file__).resolve().parent
FRAME = b'\x0c\x00' + bytes(14)
ALIGN = 0x4000
OLD_SIZE = round((40 * 60_000 * 256 / 7) / ALIGN) * ALIGN
NEW_SIZE = round((30 * 60_000 * 256 / 7) / ALIGN) * ALIGN


def write_vb(path: Path, byte_count: int) -> None:
    assert byte_count % ALIGN == 0
    chunk = FRAME * ((1024 * 1024) // len(FRAME))
    with path.open('xb') as f:
        for _ in range(byte_count // len(chunk)):
            f.write(chunk)
        f.write(FRAME * ((byte_count % len(chunk)) // len(FRAME)))


class ReverseDirectionFullChainTest(unittest.TestCase):
    def test_40_minute_to_30_minute_iso_ee_iop_integration(self):
        elf = (ROOT / 'SLUS_214.23.original').read_bytes()
        irx = (ROOT / 'CDSTREAM_ORIGINAL_REFERENCE.IRX').read_bytes()
        exe.validate_original_executable(elf)
        with tempfile.TemporaryDirectory(prefix='reverse_lcs_full_chain_') as temp:
            root = Path(temp)
            fixture = root / 'fixture.iso'
            stock = root / 'stock_40min.iso'
            output = root / 'shortened_30min.iso'
            original_vb = root / '40min.vb'
            replacement_dir = root / 'replacement'
            replacement_dir.mkdir()
            new_vb = replacement_dir / 'KJAH.VB'
            make_iso(fixture, elf, irx)
            write_vb(original_vb, OLD_SIZE)
            iso.apply_iso_replacements(fixture, stock, {'AUDIO/MUSIC/KJAH.VB': original_vb})
            old_entry = iso.Iso9660Image(stock).get('AUDIO/MUSIC/KJAH.VB')
            self.assertEqual(old_entry.size, OLD_SIZE)
            write_vb(new_vb, NEW_SIZE)
            result = iso.build_duration_aware_iso(
                stock, replacement_dir, output,
                offset_mode='iop-elf-stock', strict_original_reference=False,
            )
            out = iso.Iso9660Image(output)
            row = next(x for x in result['automatic_patch_calculator']['stations'].values()
                       if x['station'] == 'kjah')
            self.assertEqual(row['original_size'], OLD_SIZE)
            self.assertEqual(row['source_size'], NEW_SIZE)
            self.assertEqual(row['installed_size'], NEW_SIZE)
            self.assertEqual(row['classification'], 'shorter')
            self.assertEqual(row['duration_delta_ms'], duration_ms(NEW_SIZE)-duration_ms(OLD_SIZE))
            self.assertLess(row['duration_delta_ms'], 0)
            self.assertEqual(row['ee_boundary_ms'], duration_ms(NEW_SIZE))

            new_entry = out.get('AUDIO/MUSIC/KJAH.VB')
            self.assertEqual(new_entry.size, NEW_SIZE)
            self.assertEqual(new_entry.data_lba, old_entry.data_lba)  # in-place shrink
            output_elf = out.read_entry_bytes('SLUS_214.23')
            elf_table_off = exe.EXPECTED_SEGMENT_OFFSET + exe.LENGTH_TABLE_VA - exe.EXPECTED_SEGMENT_VADDR + 2*4
            self.assertEqual(struct.unpack_from('<I', output_elf, elf_table_off)[0], duration_ms(NEW_SIZE))
            self.assertEqual(output_elf[exe.GETTER_FILE_OFFSET:exe.GETTER_FILE_OFFSET+len(exe.PATCH_BYTES)], exe.PATCH_BYTES)
            station_result = next(x for x in result['stations'] if x['station']=='KJAH.VB')
            self.assertEqual(station_result['duration_ms'], duration_ms(NEW_SIZE))
            self.assertEqual(station_result['boundary_value_written_ms'], duration_ms(NEW_SIZE))

            sizes = [x['installed_size'] for x in result['automatic_patch_calculator']['stations'].values()]
            expected_irx, irx_info = patch_cdstream(irx, sizes)
            self.assertEqual(out.read_entry_bytes('MODULES/CDSTREAM.IRX'), expected_irx)
            seek = SeekInterpreter(expected_irx)
            for millis in (duration_ms(NEW_SIZE), duration_ms(NEW_SIZE)+1,
                           35*60_000, 39*60_000, duration_ms(OLD_SIZE), 0x7fffffff):
                with self.subTest(saved_ms=millis):
                    self.assertEqual(seek.run(NEW_SIZE, millis)[1], 0)
            # A shorter ISO file must not leave the old audio readable past the new EOF.
            # The in-place ISO writer clears the now-unused old extent.
            with stock.open('rb') as fin, output.open('rb') as fout:
                fin.seek(old_entry.data_lba*2048 + NEW_SIZE)
                fout.seek(new_entry.data_lba*2048 + NEW_SIZE)
                self.assertEqual(fin.read(2), b'\x0c\x00')
                self.assertEqual(fout.read(ALIGN), bytes(ALIGN))
            self.assertEqual(iso.Iso9660Image(stock).get('AUDIO/MUSIC/KJAH.VB').size, OLD_SIZE)
            print(f'CONFIRMED offline 40->30: old={duration_ms(OLD_SIZE)} ms; new={duration_ms(NEW_SIZE)} ms; '
                  f'new ISO size={new_entry.size}; EE table={duration_ms(NEW_SIZE)}; '
                  f'IOP shift={irx_info["extra_shift"]}; old extent tail cleared')


if __name__ == '__main__':
    unittest.main(verbosity=2)
