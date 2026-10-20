"""Real retail ISO static EE/IOP/file-target integration smoke; no gameplay claims."""
from pathlib import Path
import hashlib
import json
import struct
import tempfile
import sys
from lcs_duration_aware_iso_patcher import build_duration_aware_iso, Iso9660Image, inspect_input_iso
from lcs_duration_aware_exe_patcher import EXPECTED_SEGMENT_OFFSET,LENGTH_TABLE_VA,EXPECTED_SEGMENT_VADDR,STATIONS
from lcs_patch_calculator import calculate_plan
from lcs_iop_safe_seek import patch_cdstream

SRC=Path(sys.argv[1]) if len(sys.argv)>1 else Path('/mnt/data/GTA_LCS_reconstructed/Grand Theft Auto Liberty City Stories PS2.iso')
OUT_LOG=Path.cwd() / 'LCS_RETAIL_DYNAMIC_SMOKE_RESULT.json'
assert SRC.is_file()
with tempfile.TemporaryDirectory(prefix='lcs-retail-dynamic-') as d:
    root=Path(d)
    selected=root/'replacements';selected.mkdir()
    head=selected/'HEAD.VB'
    # Valid silent PSX ADPCM, arbitrary replacement duration, no KJAH default.
    head.write_bytes((b'\x0c\x00'+bytes(14))*(0x80000//16))
    out=root/'modified.iso'
    initial=Iso9660Image(SRC)
    info=inspect_input_iso(SRC)
    original={s['key']:s['size_bytes'] for s in info['stations']}
    expected=calculate_plan(original,{'head':0x80000})
    preview={v.station:{'size_bytes':v.installed_size,'duration_ms':v.ee_boundary_ms} for v in expected.stations}
    result=build_duration_aware_iso(SRC,selected,out,offset_mode='iop-elf-stock',expected_preview_metrics=preview)
    final=Iso9660Image(out)
    exe=final.read_entry_bytes(result['boot_executable_path'])
    p=EXPECTED_SEGMENT_OFFSET + LENGTH_TABLE_VA-EXPECTED_SEGMENT_VADDR
    actual=struct.unpack_from('<10I',exe,p)
    expect=tuple(s.ee_boundary_ms for s in expected.stations)
    assert actual==expect,(actual,expect)
    assert final.get('AUDIO/MUSIC/HEAD.VB').size==0x80000
    assert final.read_entry_bytes('AUDIO/MUSIC/HEAD.VB')==head.read_bytes()
    for key,file in STATIONS:
        if key!='head':
            assert final.read_entry_bytes('AUDIO/MUSIC/'+file)==initial.read_entry_bytes('AUDIO/MUSIC/'+file)
    expected_irx,_=patch_cdstream(initial.read_entry_bytes('MODULES/CDSTREAM.IRX'),[v.installed_size for v in expected.stations])
    assert final.read_entry_bytes('MODULES/CDSTREAM.IRX')==expected_irx
    assert result['automatic_patch_calculator']['stations']['head']['source_size']==0x80000
    assert result['gui_preview_values_verified'] is True
    report={'result':'PASS','source_iso_bytes':SRC.stat().st_size,'output_iso_bytes':out.stat().st_size,
            'station':'head','source_replacement_bytes':0x80000,'head_ee_boundary_ms':actual[0],
            'other_station_boundaries':dict(zip((k for k,f in STATIONS),actual)),
            'global_iop_extra_shift':expected.iop_extra_shift,
            'ee_table_exactly_matches_preview':True,'unchanged_other_station_audio_byte_identical':True,
            'cdstream_exactly_matches_calculated_instructions':True,'runtime_playback':'NOT TESTED'}
    OUT_LOG.write_text(json.dumps(report,indent=2))
    print(json.dumps(report,indent=2))
