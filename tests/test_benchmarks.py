"""The real extractor and assembler on the three benchmark designs.

Skipped when the PSDs are not on this machine (see conftest.BENCHMARK_PSDS).
Each extraction takes a while, so they run once per module and are marked
`slow`: `uv run pytest -m "not slow"` leaves them out.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from psd2html.assemble import assemble
from psd2html.extract import Extraction, extract
from psd2html.spec import PhotocardDecisions, PhotocardSpec

from conftest import FIXTURES_DIR, benchmark_psd

pytestmark = pytest.mark.slow

# The same hand-written decisions the fixtures were generated with.
DECISIONS = {
    "boishakhi": {
        "headline": {"source": "layer", "layerId": 3},
        "photo": {"source": "window", "layerId": 1},
        "captions": [{"layerId": 4, "role": "date"}],
        "drop": [],
        "gradients": [],
        "overlayAbovePhoto": [1],
        "notes": [],
    },
    "cablgram45": {
        "headline": {"source": "layer", "layerId": 4},
        "photo": {"source": "new", "box": {"x": 0, "y": 0, "width": 2678, "height": 3348}, "reason": "The photo fills the whole card under the gradient."},
        "captions": [{"layerId": 5, "role": "source"}],
        "drop": [],
        "gradients": [{"layerId": 2, "label": "Bottom gradient", "editable": True}],
        "overlayAbovePhoto": [],
        "notes": [],
    },
    "Revamped": {
        "headline": {"source": "new", "box": {"x": 300, "y": 3000, "width": 2760, "height": 900}, "align": "left", "casing": "preserve", "reason": "No text in the design; placed over the lower part of the picture."},
        "photo": {"source": "layer", "layerId": 3},
        "captions": [],
        "drop": [],
        "gradients": [],
        "overlayAbovePhoto": [],
        "notes": [],
    },
}

# benchmark name -> (fixture stem, file name recorded in the fixture)
FIXTURE_FOR = {
    "boishakhi": ("boishakhi-tv", "boishakhi.psd"),
    "cablgram45": ("cablgram", "cablgram45.psd"),
    "Revamped": ("revamped", "Revamped.psd"),
}


def nodes(layers: list[dict]):
    for node in layers:
        yield node
        yield from nodes(node.get("children", []))


def _extracted(tmp_path_factory, name: str) -> tuple[Extraction, Path]:
    psd = benchmark_psd(name)
    job = tmp_path_factory.mktemp(name)
    return extract(psd, job / "work", job / "output"), job


@pytest.fixture(scope="module")
def boishakhi(tmp_path_factory):
    return _extracted(tmp_path_factory, "boishakhi")


@pytest.fixture(scope="module")
def cablgram(tmp_path_factory):
    return _extracted(tmp_path_factory, "cablgram45")


@pytest.fixture(scope="module")
def revamped(tmp_path_factory):
    return _extracted(tmp_path_factory, "Revamped")


# -- extraction ------------------------------------------------------------------------------


def test_boishakhi_extraction(boishakhi):
    extraction, _ = boishakhi
    assert (extraction.width, extraction.height) == (1215, 1519)
    all_nodes = list(nodes(extraction.layers))
    window = next(n for n in all_nodes if n.get("transparent_window"))
    assert window["transparent_window"] == [40, 510, 1134, 750]
    assert window.get("transparent_window_radius", 0) > 0
    logo = next(n for n in all_nodes if n["name"].strip().lower() == "logo")
    assert logo["box"] == [922, 8, 139, 123]
    assert logo.get("asset")
    assert {"LiAdorNoirrit-Bold", "EkusheyBanglaKolom-Bold"} <= set(extraction.fonts)


def test_cablgram_extraction(cablgram):
    extraction, _ = cablgram
    assert (extraction.width, extraction.height) == (2678, 3348)
    gradients = [n for n in nodes(extraction.layers) if n["kind"] == "gradientfill" and n.get("gradient")]
    assert gradients, "expected a gradientfill layer with gradient data"
    gradient = gradients[0]["gradient"]
    angle = gradient["angle"] % 360
    assert min(angle, 360 - angle) <= 2
    assert len(gradient["stops"]) >= 2
    for stop in gradient["stops"]:
        assert 0 <= stop["offset"] <= 1 and 0 <= stop["alpha"] <= 1
    assert extraction.fonts


def test_revamped_extraction(revamped):
    extraction, _ = revamped
    assert (extraction.width, extraction.height) == (3375, 4219)
    all_nodes = list(nodes(extraction.layers))
    rounded = next(n for n in all_nodes if n["kind"] == "shape" and (n.get("shape") or {}).get("type") == "rounded_rect")
    clipped = [n for n in all_nodes if n.get("clip_to") == rounded["id"]]
    assert clipped, "expected a layer clipped to the rounded rectangle"
    assert any(n.get("asset") for n in clipped)


# -- assembly against the fixtures ------------------------------------------------------------


def comparable(spec: dict) -> dict:
    spec = json.loads(json.dumps(spec))
    spec["source"].pop("generatedAt", None)
    spec["source"].pop("model", None)
    return spec


def assert_matches_fixture(extraction: Extraction, job: Path, name: str) -> None:
    stem, file_name = FIXTURE_FOR[name]
    spec = assemble(
        extraction, PhotocardDecisions.model_validate(DECISIONS[name]),
        file_name=file_name, model="test", output_dir=job / "output",
    )
    expected = json.loads((FIXTURES_DIR / f"{stem}.json").read_text(encoding="utf-8"))
    assert comparable(spec.model_dump(mode="json")) == comparable(expected)
    # And what was written is what came back.
    written = PhotocardSpec.model_validate_json((job / "output" / "photocard.json").read_text(encoding="utf-8"))
    assert written == spec
    for asset in spec.asset_files():
        assert (job / "output" / "assets" / asset).is_file(), asset
    assert (job / "output" / "preview.png").is_file()


def test_boishakhi_assembles_to_fixture(boishakhi):
    assert_matches_fixture(*boishakhi, "boishakhi")


def test_cablgram_assembles_to_fixture(cablgram):
    assert_matches_fixture(*cablgram, "cablgram45")


def test_revamped_assembles_to_fixture(revamped):
    assert_matches_fixture(*revamped, "Revamped")
