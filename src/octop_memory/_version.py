"""Package version — single source of truth is ``pyproject.toml`` ``[project].version``.

Resolve the version from installed distribution metadata so callers never need
to keep a second hardcoded copy in sync.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

_DISTRIBUTION_NAME = "octop-memory"

try:
    __version__ = version(_DISTRIBUTION_NAME)
except PackageNotFoundError:  # pragma: no cover - bare source tree without install
    __version__ = "0.0.0+unknown"
