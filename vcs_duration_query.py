#!/usr/bin/env python3
"""Read-only VCS profile CLI for checking *any* source duration in target range.

Examples:
  python vcs_duration_query.py --station vcpr --duration 02:47:15.357
  python vcs_duration_query.py --station flash --duration 04:39:00
  python vcs_duration_query.py --station vcpr --vb-size 367017984

No IRX/ISO editing. It returns mathematical models for the signed raw-a2
position path; the true runtime a2 is not proved to equal VB size.
"""
from __future__ import annotations
import argparse,json,re
from vcs_ee_overflow_patch import aligned_bytes_for_source_duration, validate_radio_sizes
from vcs_iop_position_math import original_iop_signed_position, proposed_safe_split
from gta_radio_profiles import PROFILES

VCS_KEYS=frozenset(k for k,_ in PROFILES['vcs_ps2'].stations)

def parse_hms_ms(s: str) -> int:
    m=re.fullmatch(r'(\d{1,3}):(\d{2}):(\d{2})(?:\.(\d{1,3}))?',s)
    if not m: raise ValueError('Use HH:MM:SS or HH:MM:SS.mmm')
    h,mi,se=map(int,m.group(1,2,3))
    if mi>59 or se>59:raise ValueError('Minute/second fields must be 00..59')
    fractional=(m.group(4) or '0').ljust(3,'0')
    return ((h*60+mi)*60+se)*1000+int(fractional)

def calculate(station: str, *, duration: str | None=None, vb_size: int | None=None):
    if station not in VCS_KEYS: raise ValueError('Unknown VCS station (LCS station names not accepted)')
    if (duration is None)==(vb_size is None):raise ValueError('Supply exactly one of duration or vb_size')
    src_ms=parse_hms_ms(duration) if duration is not None else None
    size=aligned_bytes_for_source_duration(src_ms) if src_ms is not None else vb_size
    item=validate_radio_sizes([size])[0]
    expected=proposed_safe_split(size)
    old=original_iop_signed_position(size)
    return {
       'game_profile':'vcs_ps2','station':station,'source_duration_ms':src_ms,
       'requested_file_size_bytes':vb_size,'aligned_file_size_bytes':size,
       'game_duration_ms':item['duration_ms'],
       'allocation_padding_ms':item['duration_ms']-src_ms if src_ms is not None else None,
       'patched_ee_byte_boundary':item['patched_ee_position'],
       'original_ee_byte_boundary':item['original_ee_position'],
       'iop_signed_position_if_raw_a2_equals_vb_size':old,
       'iop_safe_expected_if_raw_a2_equals_vb_size':expected,
       'iop_model_mismatch_if_raw_a2_equals_vb_size':old!=expected,
       'iop_position_repair_status':'arithmetic reference only; NO binary patch applied',
       'actual_raw_a2_at_runtime':'not yet established',
       'ISO_build_status':'VCS ISO writer disabled pending verification'}

def main():
    p=argparse.ArgumentParser(description='GTA VCS PS2 per-station duration, EE and IOP arithmetic calculator')
    p.add_argument('--station',default='vcpr',choices=sorted(VCS_KEYS))
    q=p.add_mutually_exclusive_group(required=True)
    q.add_argument('--duration',help='HH:MM:SS[.mmm] source length, 00:00:01..04:39:00')
    q.add_argument('--vb-size',type=int,help='Existing 16KiB-pair aligned VB bytes')
    args=p.parse_args()
    print(json.dumps(calculate(args.station,duration=args.duration,vb_size=args.vb_size),indent=2))
if __name__=='__main__':main()
