# -*- coding: utf-8 -*-
"""Smoke test for the kylin_v11_agent CLI entry point (R1-6)."""

from __future__ import annotations
import os, sys

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
sys.path = [p for p in sys.path if "银河os" not in p]

from src.kylin_v11_agent import cli as cli_mod


def test_module_has_main():
    """The CLI module must expose a callable `main`."""
    assert hasattr(cli_mod, "main")
    assert callable(cli_mod.main)


def test_main_help_does_not_raise():
    """`main(["--help"])` must exit cleanly without raising.

    Some argparse implementations call `sys.exit` on `--help`; we accept
    SystemExit (which pytest captures) but no other exception.
    """
    try:
        cli_mod.main(["--help"])
    except SystemExit:
        pass  # argparse --help exits via SystemExit, which is fine.
    except Exception as e:
        pytest.fail("main([--help]) raised: " + repr(e))