"""``python -m coop``: the operator's commands (S3-1; the ``coop`` entry point flips here in S3-2)."""

import sys

from .cli import main

sys.exit(main())
