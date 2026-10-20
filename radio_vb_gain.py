"""Optional +3 dB for a single LCS/VCS PS2 raw stereo PSX ADPCM VB.

No game EXE/IRX modifications; outputs a new same-size VB in caller's
staging directory. Encoder handles 8 KiB/channel interleave and copies frame
flags verbatim. Gain is nominal; an already loud source can clip.
"""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Callable

SOURCE = Path(__file__).with_name('radio_adpcm_gain.cpp')
PAIR = 0x4000


def validate_gain_source(path: Path) -> int:
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f'VB input not found: {path}')
    size = path.stat().st_size
    if size == 0 or size % PAIR:
        raise ValueError(f'PS2 stereo .VB requires nonempty 0x4000-aligned input: {path.name} ({size:,} bytes)')
    # A sampled plausibility check does not replace full decode validation.
    with path.open('rb') as src:
        for pos in {0, 0x2000, size - 16, size // 2 - size // 2 % 16, max(0, size - 0x2000)}:
            src.seek(pos)
            frame = src.read(16)
            if len(frame) != 16 or frame[0] >> 4 > 4 or (frame[0] & 15) > 12:
                raise ValueError(f'{path.name}: invalid PSX ADPCM header at 0x{pos:x}')
    return size


def compile_gain_engine(progress: Callable[[str], None] | None = None) -> Path:
    digest = hashlib.sha256(SOURCE.read_bytes()).hexdigest()[:20]
    directory = Path(tempfile.gettempdir()) / f'gta_radio_adpcm_gain_{digest}'
    binary = directory / ('radio_adpcm_gain.exe' if os.name == 'nt' else 'radio_adpcm_gain')
    if binary.is_file():
        return binary
    directory.mkdir(parents=True, exist_ok=True)
    compilers = ('g++', 'clang++', 'cl') if os.name == 'nt' else ('g++', 'clang++')
    problems = []
    for name in compilers:
        executable = shutil.which(name)
        if not executable:
            continue
        if progress:
            progress(f'Compiling PS2 ADPCM gain engine using {name}…')
        args = ([executable, '/nologo', '/O2', '/EHsc', '/std:c++17', str(SOURCE), '/Fe:' + str(binary)]
                if name == 'cl' else
                [executable, '-O3', '-std=c++17', '-Wall', '-Wextra', str(SOURCE), '-o', str(binary)])
        run = subprocess.run(args, cwd=directory, capture_output=True, text=True)
        if run.returncode == 0 and binary.is_file():
            return binary
        problems.append(f'{name}: {(run.stderr or run.stdout)[-700:]}')
    raise RuntimeError('Optional +3 dB gain needs a working C++17 compiler (MinGW g++, clang++ or MSVC cl). '
                       'Use a Windows compiler or Visual Studio Developer Command Prompt. '\
                       + '\n'.join(problems))


def boost_vb(input_vb: Path, output_vb: Path, progress: Callable[[str], None] | None = None) -> dict:
    """Boost exactly one VB. No input overwrite, and reject changed file size."""
    src, dst = Path(input_vb), Path(output_vb)
    size = validate_gain_source(src)
    if src.resolve() == dst.resolve() or dst.exists():
        raise FileExistsError('Boosted output must be new and distinct from source')
    engine = compile_gain_engine(progress)
    if progress:
        progress(f'Applying +3 dB to {src.name}, preserving flags and stream byte length…')
    run = subprocess.run([str(engine), str(src), str(dst)], capture_output=True, text=True)
    if run.returncode:
        dst.unlink(missing_ok=True)
        raise RuntimeError(f'PS2 ADPCM gain failed for {src.name}: {run.stderr[-1500:]}')
    try:
        result = json.loads(run.stdout)
        if dst.stat().st_size != size or result.get('bytes') != size:
            raise IOError(f'{src.name}: boosted stream length differs from original')
        validate_gain_source(dst)
        result['source'] = str(src)
        result['output'] = str(dst)
        return result
    except Exception:
        dst.unlink(missing_ok=True)
        raise
