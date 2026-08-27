"""Where cached downloads live.

The providers keep their parquet caches beside the terminal's own files so
a Brazilian user's data sits where they would look for it. That location
comes from ``lse_terminal.engine.config``, which is the host's internal
module rather than part of the Provider contract -- so it is imported
defensively and the same path is computed directly if that module ever
moves. A plugin has no business breaking because a host internal was
renamed.
"""

from __future__ import annotations

import os
from pathlib import Path


def config_dir() -> Path:
    try:
        from lse_terminal.engine import config as _cfg  # noqa: PLC0415
        return _cfg.config_dir()
    except Exception:
        override = os.environ.get("LSE_TERMINAL_CONFIG_DIR")
        if override:
            return Path(override).expanduser()
        return Path("~/.config/lse-terminal").expanduser()
