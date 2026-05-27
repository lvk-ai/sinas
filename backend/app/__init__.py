"""Sinas backend application package.

`__version__` is the single source of truth for the platform version.
`pyproject.toml` reads it via setuptools dynamic metadata
(`[tool.setuptools.dynamic] version = {attr = "app.__version__"}`), and the
runtime (`main.py`, `/info` endpoint) imports it directly. Bump it here only.
"""

__version__ = "0.1.0"
