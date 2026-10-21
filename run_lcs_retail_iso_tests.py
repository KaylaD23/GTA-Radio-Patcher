"""Run the LCS retail ISO-dependent test class from a Windows-friendly ISO path.

Usage: python run_lcs_retail_iso_tests.py "C:\\path\\to\\your original LCS ISO.iso"
Reads the ISO but does not modify it. Uses the existing patcher's test suite.
"""
import sys
import unittest
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 2:
        print('Usage: python run_lcs_retail_iso_tests.py "C:\\path\\to\\LCS.iso"')
        return 2

    iso_path = Path(sys.argv[1].strip('"')).expanduser().resolve()
    if not iso_path.is_file():
        print(f'ERROR: ISO does not exist: {iso_path}')
        return 2
    if not all((Path(__file__).parent / x).is_file() for x in (
            'test_lcs_complete_patch_map.py',
            'SLUS_214.23.original',
            'CDSTREAM_ORIGINAL_REFERENCE.IRX')):
        print('ERROR: Place this helper in the GTA_Radio_Patcher project folder '
              'alongside test_lcs_complete_patch_map.py and the original ELF/IRX references.')
        return 2

    import test_lcs_complete_patch_map as target
    target.GAME_ISO = iso_path  # Override the Linux-only hard-coded reference path.
    print(f'Original ISO (read-only): {iso_path}', flush=True)
    print('Running retail LCS patch-map verification (no output ISO will be created).', flush=True)
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(target.CompletePatchMapTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if result.skipped:
        print(f'INCOMPLETE: {len(result.skipped)} skipped tests.', flush=True)
        return 3
    if not result.wasSuccessful():
        print('FAILED: see test output for the precise test and error.', flush=True)
        return 1
    print('PASS: LCS retail ISO patch-map tests completed successfully.', flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
