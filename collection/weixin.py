"""Compatibility entry; implementation moved to media_platform.weixin.core."""
from importlib import import_module
import sys

if __name__ == "__main__":
    import_module("media_platform.weixin.core").main()
else:
    sys.modules[__name__] = import_module("media_platform.weixin.core")
