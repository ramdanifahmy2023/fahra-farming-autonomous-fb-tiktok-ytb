"""Run unit tests with Python network connections disabled."""
from contextlib import ExitStack
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


def main():
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    with ExitStack() as stack:
        for target in ('socket.socket.connect', 'socket.socket.connect_ex',
                       'socket.create_connection', 'socket.getaddrinfo'):
            stack.enter_context(patch(target, side_effect=OSError('Jaringan dinonaktifkan selama unit test.')))
        suite = unittest.defaultTestLoader.discover(str(root / 'tests'))
        if not suite.countTestCases():
            print('FAIL: tidak ada tes yang ditemukan.', file=sys.stderr)
            return 1
        return 0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
