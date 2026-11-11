"""Shared test setup.

The environment defaults are set before anything imports `psd2html.server`,
which reads them at import time: a shared secret so the auth path is the one
production uses, a development env so `check_startup` never raises, and a
throw-away data directory so a stray import never writes into `data/`.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("PSD2HTML_SHARED_SECRET", "test")
os.environ.setdefault("PSD2HTML_ENV", "development")
os.environ.setdefault("PSD2HTML_DATA", tempfile.mkdtemp(prefix="psd2html-tests-"))

TESTS_DIR = Path(__file__).parent
FIXTURES_DIR = TESTS_DIR / "fixtures"

# The benchmark designs are clients' artwork, so they are not in the
# repository. Put them in tests/psd/ (ignored by git), or point
# PSD2HTML_BENCHMARK_DIR at a folder holding them.
BENCHMARK_FILES: dict[str, list[str]] = {
    "boishakhi": ["boishakhi.psd", "boishak TV.psd"],
    "cablgram45": ["cablgram45.psd"],
    "Revamped": ["Revamped.psd"],
}


def _benchmark_dirs() -> list[Path]:
    dirs = [TESTS_DIR / "psd"]
    extra = os.environ.get("PSD2HTML_BENCHMARK_DIR")
    if extra:
        dirs.append(Path(extra))
    return dirs


def benchmark_psd(name: str) -> Path:
    """The path of a benchmark PSD, or skip the test when it is not here."""
    names = BENCHMARK_FILES.get(name) or [f"{name}.psd"]
    candidates = [folder / file for folder in _benchmark_dirs() for file in names]
    for path in candidates:
        if path.is_file():
            return path
    pytest.skip(f"benchmark PSD {name!r} not found (looked in {', '.join(str(p) for p in candidates)})")


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES_DIR
