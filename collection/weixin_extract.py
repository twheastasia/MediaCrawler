"""Compatibility entry; implementation moved to media_platform.weixin.extractor."""
from importlib import import_module
import sys

sys.modules[__name__] = import_module("media_platform.weixin.extractor")
