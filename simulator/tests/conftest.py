"""
tests/conftest.py
------------------
Shared pytest configuration for the simulator test suite.
Adds the repo root to sys.path so that `shared/` and `simulator/` packages
are importable without installation.
"""

import os
import sys

# Ensure repo root is on the path before any test module is imported
_repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)
