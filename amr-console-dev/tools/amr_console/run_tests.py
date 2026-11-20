#!/usr/bin/env python3
"""Run console offline tests only while the simulation guard is held."""
import argparse
from pathlib import Path
import sys
import unittest

from test_support import guarded_test_environment, TestEnvironmentBusy


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pattern', default='test_*.py',
                        help='Test filename pattern; default: test_*.py')
    parser.add_argument('--verbose', action='store_true')
    args = parser.parse_args(argv)
    try:
        with guarded_test_environment():
            print('PREFLIGHT PASS: simulation lock acquired; '
                  'full simulation process inventory is empty.', flush=True)
            suite = unittest.defaultTestLoader.discover(
                str(Path(__file__).resolve().parent / 'tests'), pattern=args.pattern)
            result = unittest.TextTestRunner(verbosity=2 if args.verbose else 1).run(suite)
    except TestEnvironmentBusy as exc:
        print('TESTS REFUSED: ' + str(exc), file=sys.stderr)
        return 2
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
