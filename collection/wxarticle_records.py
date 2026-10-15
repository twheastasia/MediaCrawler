"""Compatibility entry; implementation moved to store.weixin.records."""
from importlib import import_module
import sys

sys.modules[__name__] = import_module("store.weixin.records")
