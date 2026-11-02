#!/usr/bin/env python3
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def main() -> int:
    bootstrap = Path(__file__).with_name('wf_bootstrap.py')
    return subprocess.call([sys.executable, str(bootstrap), 'run', *sys.argv[1:]])


if __name__ == '__main__':
    raise SystemExit(main())
