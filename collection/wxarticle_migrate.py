"""Compatibility entry; implementation moved to store.weixin.migrate."""
from importlib import import_module
import sys

if __name__ == "__main__":
    import_module("store.weixin.migrate").main()
else:
    sys.modules[__name__] = import_module("store.weixin.migrate")
