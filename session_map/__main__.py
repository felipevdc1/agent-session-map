"""`python3 -m session_map ...` runs the `session-map` command."""

from __future__ import annotations

import sys

from .cli import main

sys.exit(main())
