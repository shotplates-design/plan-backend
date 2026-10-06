"""Alias for `python -m app.cli stripe-setup` (TEST MODE ONLY). Usage:

uv run python scripts/stripe_setup.py --dry-run
"""

import sys

from app.cli import main

if __name__ == "__main__":
    sys.argv = [sys.argv[0], "stripe-setup", *sys.argv[1:]]
    main()
