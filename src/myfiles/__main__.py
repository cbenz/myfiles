"""Allow running the CLI with ``python -m myfiles``."""

import sys

from myfiles.cli import main

if __name__ == "__main__":
    sys.exit(main())
