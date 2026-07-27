"""Build the sealed ingest into a binary extension with Nuitka.

Usage:
    python tools/build_protected_ingest.py
"""
from __future__ import annotations

from nuitka_build_support import build_nuitka_module


def main():
    build_nuitka_module("sealed_core/ingest.py", "ingest")


if __name__ == "__main__":
    main()
