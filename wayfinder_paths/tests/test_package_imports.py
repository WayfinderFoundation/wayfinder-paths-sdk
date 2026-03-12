from __future__ import annotations

import importlib


def test_wayfinder_paths_import_is_lightweight():
    module = importlib.import_module("wayfinder_paths")
    assert module.__version__ == "0.1.0"
    assert "Strategy" not in module.__dict__
