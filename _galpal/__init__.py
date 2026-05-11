"""galpal internals.

This package is intentionally underscore-prefixed. End users get the public
surface via the `galpal` console script registered in pyproject.toml's
`[project.scripts]` (after `pipx install galpal`); developers in a clone use
`python dev_galpal.py …`, which delegates here. Tests reach into `_galpal.cli`
and below directly.
"""

# Re-export from `_version` so `from _galpal import __version__` keeps working,
# but the wrapper's `--version` path now imports `_galpal._version` directly to
# avoid executing this `__init__.py` on a fresh clone where third-party deps
# might not be installed yet.
from _galpal._version import __version__

__all__ = ["__version__"]
