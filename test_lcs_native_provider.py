"""Offline regression of byte-identical native IOP radio installation.

Uses a synthetic ISO9660 disc containing authentic original EE+IOP modules.
A real full PS2 ISO is not needed for this filesystem/length mechanics test.
"""
from pathlib import Path
import sys, math, struct, tempfile, hashlib
import lcs_duration_aware_iso_patcher as mod
import lcs_duration_aware_exe_patcher as patcher
B=2048

def rec(name, extent, size, flags=0):
    if name=='.': idb=b'\0'
    elif name=='..': idb=b'\1'
    else: idb=name.encode('ascii')
    n=33+len(idb)+(1 if len(idb)%2==0 else 0)
    b=bytearray(n);b[0]=n;b[2:6]=struct.pack('<I',extent);b[6:10]=struct.pack('>I',extent)
    b[10:14]=struct.pack('<I',size);b[14:18]=struct.pack('>I',size)
    b[18:25]=bytes([126,1,1,0,0,0,0]);b[25]=flags;b[28:30]=struct.pack('<H',1);b[30:32]=struct.pack('>H',1)
    b[32]=len(idb);b[33:33+len(idb)]=idb
    return bytes(b)

def dir(records):
    out=bytearray(B);pos=0
    for r in records:
        if pos%B+len(r)>B:pos=(pos//B+1)*B
        out[pos:pos+len(r)]=r;pos+=len(r)
    return bytes(out)

def put(data,lba,payload): data[lba*B:lba*B+len(payload)]=payload

def make_iso(path,exe,irx,modify_exe=False,modify_irx=False,include_companion_assets=False):
    e=bytearray(exe);i=bytearray(irx)
    if modify_exe: e[0x100]^=0x1
    if modify_irx: i[0x100]^=0x1
    elba=26;ilba=elba+math.ceil(len(e)/B);cursor=ilba+math.ceil(len(i)/B)
    station=[];positions={}
    for key,fn in patcher.STATIONS:
        station.append(rec(fn+';1',cursor,0x4000));positions[fn]=cursor;cursor+=8
    if include_companion_assets:
        text_lba, chk_lba, ps2_lba, gxt_lba, dtz_lba = range(cursor, cursor+5)
        cursor += 5
    data=bytearray(B*(cursor+12));hdr=memoryview(data)[16*B:17*B]
    hdr[0]=1;hdr[1:6]=b'CD001';hdr[6]=1;hdr[80:84]=struct.pack('<I',len(data)//B);hdr[84:88]=struct.pack('>I',len(data)//B)
    hdr[128:130]=struct.pack('<H',B);hdr[130:132]=struct.pack('>H',B)
    hdr[156:156+len(rec('.',20,B,2))]=rec('.',20,B,2)
    term=memoryview(data)[17*B:18*B];term[0]=255;term[1:6]=b'CD001';term[6]=1
    root_records=[rec('.',20,B,2),rec('..',20,B,2),rec('AUDIO',21,B,2),
                  rec('MODULES',23,B,2),rec('SYSTEM.CNF;1',25,32),rec('SLUS_214.23;1',elba,len(e))]
    if include_companion_assets:
        root_records += [rec('TEXT',text_lba,B,2),rec('CHK',chk_lba,B,2)]
    put(data,20,dir(root_records))
    put(data,21,dir([rec('.',21,B,2),rec('..',20,B,2),rec('MUSIC',22,B,2)]))
    put(data,22,dir([rec('.',22,B,2),rec('..',21,B,2),*station]))
    put(data,23,dir([rec('.',23,B,2),rec('..',20,B,2),rec('CDSTREAM.IRX;1',ilba,len(i))]))
    put(data,25,b'BOOT2 = cdrom0:\\SLUS_214.23;1\n'.ljust(32,b' '))
    put(data,elba,e);put(data,ilba,i)
    for fn,lba in positions.items(): put(data,lba,(b'\x00'*16)*(0x4000//16))
    if include_companion_assets:
        put(data,text_lba,dir([rec('.',text_lba,B,2),rec('..',20,B,2),
                               rec('ENGLISH.GXT;1',gxt_lba,64)]))
        put(data,chk_lba,dir([rec('.',chk_lba,B,2),rec('..',20,B,2),
                              rec('PS2',ps2_lba,B,2)]))
        put(data,ps2_lba,dir([rec('.',ps2_lba,B,2),rec('..',chk_lba,B,2),
                              rec('GAME.DTZ;1',dtz_lba,64)]))
        put(data,gxt_lba,b'original english gxt'.ljust(64,b'\0'))
        put(data,dtz_lba,b'original game dtz'.ljust(64,b'\0'))
    path.write_bytes(data)

def main():
    elf=Path(sys.argv[1]).read_bytes();irx=Path(sys.argv[2]).read_bytes()
    real=Path(sys.argv[3]) if len(sys.argv)>3 else None
    assert patcher.sha256(elf)==patcher.SUPPORTED_SHA256
    with tempfile.TemporaryDirectory() as td:
        d=Path(td);src=d/'src.iso';rp=d/'audio';rp.mkdir();out=d/'native.iso';report=d/'report.json'
        make_iso(src,elf,irx)
        replacement=rp/'KJAH.VB'
        if real:
            import shutil
            shutil.copyfile(real,replacement)
        else:
            replacement.write_bytes((b'\x4b\x00'+b'K'*14)*(0x8000//16))
        result=mod.build_duration_aware_iso(src,rp,out,report,offset_mode='native')
        iso=mod.Iso9660Image(out)
        assert result['offset_mode']=='native'
        assert iso.read_entry_bytes('SLUS_214.23') == elf
        assert iso.read_entry_bytes('MODULES/CDSTREAM.IRX')==irx
        item=iso.get('AUDIO/MUSIC/KJAH.VB')
        assert item.size == mod.duration_ms_from_size(item.size) * 256//7 and item.size==max(replacement.stat().st_size,0x40000)
        assert iso.read_entry_bytes(item)[:replacement.stat().st_size]==replacement.read_bytes()
        assert result['executable_patch_report']['length_getter_modified'] is False
        assert result['boundary_values_written_ms']=={}
        assert result['stations'][2]['duration_ms']==(item.size*7)//256 # installed padded size, not short source size
        assert [r['iso_path'] for r in result['iso_files_replaced']] == ['AUDIO/MUSIC/KJAH.VB']
        print('PASS native-mode original boot executable and CDSTREAM.IRX byte-identical; installed KJAH',item.size,'bytes; duration',result['stations'][2]['duration_ms'],'ms')
        bad_src=d/'bad_elf.iso';make_iso(bad_src,elf,irx,modify_exe=True)
        try: mod.build_duration_aware_iso(bad_src,rp,d/'bad_elf_output.iso',executable_path=Path(sys.argv[1]),offset_mode='native')
        except ValueError as exc:assert 'ORIGINAL unmodified' in str(exc)
        else:raise AssertionError('native accepts modified embedded ELF')
        print('PASS rejects already-modified ELF')
        bad_src=d/'bad_irx.iso';make_iso(bad_src,elf,irx,modify_irx=True)
        try: mod.build_duration_aware_iso(bad_src,rp,d/'bad_irx_output.iso',executable_path=Path(sys.argv[1]),offset_mode='native')
        except ValueError as exc:assert 'original verified CDSTREAM.IRX' in str(exc)
        else:raise AssertionError('native accepts unverified IOP module')
        print('PASS rejects modified IOP module')
    print('PASS native IOP ISO integration regression')

if __name__=='__main__':main()
