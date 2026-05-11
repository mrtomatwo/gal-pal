"""Single source of truth for the package version.

Lives in its own module (rather than `_galpal/__init__.py`) so the wrapper
script's `--version` path never has to execute the package's `__init__.py`.
If anyone later adds a third-party import to `__init__.py`, `--version` would
otherwise break on a fresh clone (before `init` has run). Importing this
sub-module instead keeps the version readout stdlib-only.

`pyproject.toml` carries the same version string — keep them in sync.
"""

__version__ = "1.0.0"
