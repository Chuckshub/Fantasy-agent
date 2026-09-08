"""Fantasy platform adapters.

The engine above this package never learns which site the data came from.
See `base.py` for why the read/write split is shaped the way it is.
"""
from .base import Platform, PlatformError, available, get, load_profile, save_profile

__all__ = ["Platform", "PlatformError", "available", "get",
           "load_profile", "save_profile"]
