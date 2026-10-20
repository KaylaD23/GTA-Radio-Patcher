"""Experimental *VCS-only* CDSTREAM.IRX position patch, exact-revision locked.

The original IRX is never modified. No ISO installation is performed.
Creates an extended ELF PT_LOAD with a small PC-relative MIPS helper at the
end of the old BSS, keeping the pre-existing section virtual addresses and
text relocations unchanged. The helper uses only caller-clobbered registers.
"""
from __future__ import annotations
import hashlib
import struct
from pathlib import Path

EXPECTED_SHA = 'a72b95c6f0db77ba0363196b9d36ce1ac8585560a0730edc4125f29c12b71371'
TEXT_OFF = 0xA0
SITES={0x255c:0x000610C0,0x2560:0x00462823,0x2564:0x04A10002,0x2568:0x3C031062,0x256c:0x24A50007,0x2570:0x3C020000,0x2578:0x34634DD3,0x2580:0x000510C3}

def r(rs,rt,rd,fn,sh=0): return (rs<<21)|(rt<<16)|(rd<<11)|(sh<<6)|fn
def it(op,rs,rt,imm): return (op<<26)|(rs<<21)|(rt<<16)|(imm & 65535)
def branch(from_va,to_va):
    dist=to_va-(from_va+4)
    if dist%4 or not (-32768 <= dist//4 <= 32767): raise ValueError('Branch target out of range')
    return it(4,0,0,dist//4)
def h(b): return hashlib.sha256(b).hexdigest()
def u32(b,p): return struct.unpack_from('<I',b,p)[0]
def put(b,p,n): struct.pack_into('<I',b,p,n & 0xffffffff)

def inspect(data:bytes):
    if h(data)!=EXPECTED_SHA: raise ValueError('Unknown IRX SHA256: refusing to patch this revision')
    if data[:4]!=b'\x7fELF' or data[4]!=1 or data[5]!=1 or u32(data,0x20)!=0xbbe4:
        raise ValueError('Unexpected ELF layout')
    if struct.unpack_from('<H',data,0x28)[0] != 0x34: # e_ehsize
        raise ValueError('Unexpected ELF header size')
    p=0x34+32 # second program header
    if (u32(data,p),u32(data,p+4),u32(data,p+16),u32(data,p+20))!=(1,0xa0,0x81a0,0xb460):
        raise ValueError('Unexpected original LOAD layout')
    for v,w in SITES.items():
        if u32(data,TEXT_OFF+v)!=w: raise ValueError(f'Original IRX opcode at .text+0x{v:x} mismatch')
    if len(data)!=48540: raise ValueError('Unexpected original length')
    return p

def helper_words(stub_addr):
    # Signed exact: q=arithmetic_floor(x/8); corr=(x >= 0 and x%8 !=0)
    # result=x-q-corr; all intermediate MIPS integers valid for EVERY int32 x.
    words=[
        it(0x0a,6,5,0),        # slti  a1,a2,0       (negative flag)
        it(0x0c,6,2,7),        # andi  v0,a2,7       (remainder)
        r(0,2,2,0x2b),        # sltu  v0,zero,v0    (nonzero remainder)
        it(0x0e,5,5,1),        # xori  a1,a1,1       (nonnegative flag)
        r(5,2,5,0x24),        # and   a1,a1,v0      (correction)
        r(0,6,2,3,3),         # sra   v0,a2,3       (floor quotient)
        r(5,2,5,0x21),        # addu  a1,a1,v0
        r(6,5,5,0x23),        # subu  a1,a2,a1      (trunc 7*a2/8)
        branch(stub_addr+32,0x2570),
        it(0x0f,0,3,0x1062),   # lui   v1,0x1062    (return branch delay slot)
    ]
    return words

def build(original:bytes):
    hdr=inspect(original)
    # Input to this block from fallthrough at .text+0x255c and a separate
    # bne at .text+0x24d4 targeting .text+0x2560. Hook common 0x2560.
    stub_addr=0xb460 # after old bss (old PT_LOAD memsz)
    words=helper_words(stub_addr)
    helper=struct.pack('<'+len(words)*'I',*words)
    helper+=b'\0'*((-len(helper))%16)
    assert len(helper)==48
    old_fsz=0x81a0
    old_msz=0xb460
    gap=old_msz-old_fsz
    insertion= b'\x00'*gap + helper
    at=TEXT_OFF+old_fsz # 0x8240: old section metadata starts
    out=bytearray(original[:at] + insertion + original[at:])
    shift=len(insertion)
    # PT_LOAD carries the previous bss as explicit zeros and the new helper.
    new_sz=old_msz+len(helper)
    put(out,hdr+16,new_sz) # p_filesz
    put(out,hdr+20,new_sz) # p_memsz
    old_shoff=0xbbe4
    shoff=old_shoff+shift
    put(out,0x20,shoff)
    e_shentsize,e_shnum=struct.unpack_from('<HH',out,0x2e)
    if e_shentsize!=40 or e_shnum!=11: raise ValueError('Unexpected section table')
    # Add a named executable section for the branch helper, so ELF section-
    # aware tools/loaders also recognize it. Keep ALL old section addresses.
    # The original shstrtab has no eight-byte spare, so extend it before the
    # relocated relocation tables and advance their file offsets by 8.
    suffix=b'.vcsfix\0'
    assert len(suffix)==8
    old_str_end=0x8240+0x54
    new_str_end=old_str_end+shift
    out[new_str_end:new_str_end]=suffix
    delta=len(suffix)
    shoff += delta
    put(out,0x20,shoff)
    for i in range(e_shnum):
        s=shoff+i*40
        offset=u32(out,s+16)
        if offset>=at:offset+=shift
        if offset>=new_str_end:offset+=delta
        put(out,s+16,offset)
        if i==10: put(out,s+20,0x54+delta) # shstrtab size
    # Append section-header entry; no new MIPS relocations are required:
    # every new branch is PC-relative and in signed-16-bit reach.
    out += struct.pack('<IIIIIIIIII',0x54,1,6,stub_addr,
                       TEXT_OFF+stub_addr,len(helper),0,0,16,0)
    struct.pack_into('<H',out,0x30,e_shnum+1)
    # Hook all paths through .text+0x2560 and return before old magic division.
    for v,w in {
        0x2560:branch(0x2560,stub_addr),
        0x2564:0,
        0x2580:r(5,0,2,0x21), # move v0,a1: do NOT shift an overflowed product
    }.items(): put(out,TEXT_OFF+v,w)
    return bytes(out), {'original_sha256':h(original),'new_sha256':h(out),
        'original_bytes':len(original),'patched_bytes':len(out),
        'stub_text_va':hex(stub_addr),'stub_file_offset':hex(TEXT_OFF+stub_addr),
        'patched_text_offsets':['0x2560','0x2564','0x2580'],
        'patch_opcodes':{hex(k):hex(u32(out,TEXT_OFF+k)) for k in (0x2560,0x2564,0x2580)},
        'stub_words':[f'{v:08x}' for v in words],
        'original_irx_preserved': True, 'iso_writing_enabled':False}

def main():
    import argparse,json
    ap=argparse.ArgumentParser()
    ap.add_argument('--input',type=Path,required=True)
    ap.add_argument('--output',type=Path,required=True)
    ap.add_argument('--report',type=Path)
    ns=ap.parse_args()
    if ns.input.resolve()==ns.output.resolve():raise SystemExit('Input/output same path forbidden')
    result,meta=build(ns.input.read_bytes())
    ns.output.parent.mkdir(parents=True,exist_ok=True)
    if ns.output.exists():raise SystemExit('Refusing to overwrite existing output')
    ns.output.write_bytes(result)
    if ns.report:ns.report.write_text(json.dumps(meta,indent=2)+'\n')
    print(json.dumps(meta,indent=2))
if __name__=='__main__':main()
