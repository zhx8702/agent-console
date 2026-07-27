"""Build the sealed sender into a binary extension with Nuitka.

Usage:
    python tools/build_protected_sender.py
"""
from __future__ import annotations

from nuitka_build_support import build_nuitka_module


def main():
    build_nuitka_module("sealed_core/wechat_sender.py", "sender")


if __name__ == "__main__":
    main()
