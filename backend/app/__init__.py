"""Sinas backend application package.

The platform version literal lives in `app/_version.py` so that
`pyproject.toml` (via setuptools dynamic `attr`) and the staged Docker
build can resolve it without loading the rest of the package. We
re-export it here so existing `from app import __version__` imports
keep working.

Keep this file minimal — anything that grows here invalidates the
backend image's dep-install layer cache on every change. Add new
package-init logic in a dedicated submodule instead.
"""

from app._version import __version__

__all__ = ["__version__"]
