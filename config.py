"""
agenc — configuration.

This module defines environment variables and constants.
"""

import os
from pathlib import Path

# ---------------------------------------------------------------------------
# Environment variables
# ---------------------------------------------------------------------------

DEFAULT_MODEL = os.environ.get("AGENC_MODEL", "gpt-4o")
DEFAULT_BASE_URL = os.environ.get("AGENC_BASE_URL", "https://api.openai.com/v1")
API_KEY = os.environ.get("AGENC_API_KEY", os.environ.get("OPENAI_API_KEY", ""))
AUTO_WRITE = os.environ.get("AGENC_AUTO_WRITE", "").lower() in ("1", "true", "yes")
CWD = Path.cwd().resolve()

# ---------------------------------------------------------------------------
# File reading limits
# ---------------------------------------------------------------------------

MAX_READ_LINES = 2000
MAX_FILE_BYTES = 1_000_000  # refuse to read files larger than this