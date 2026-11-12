"""PasteStack, a clipboard manager. The name people see lives only here."""

from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "PasteStack"
VERSION = "0.4.0"

# Named after the app's working title, and staying that way: changing it would leave saved
# data behind. So do the internal names in crypto.py and engine.py, which the key and the
# one-copy-at-a-time check depend on. Only APP_NAME follows a rename.
_DATA_FOLDER = "ClipApp"


def data_dir() -> Path:
    """Where the database and log live. CLIPAPP_DATA overrides it (used by the tests)."""
    override = os.environ.get("CLIPAPP_DATA")
    base = Path(override) if override else Path(os.environ["APPDATA"]) / _DATA_FOLDER
    base.mkdir(parents=True, exist_ok=True)
    return base
