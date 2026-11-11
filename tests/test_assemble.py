"""assemble(): a hand-written extraction in, a PhotocardSpec out."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image

from psd2html.assemble import KNOWN_FAMILIES, AssemblyError, _check, _fold_stops, assemble
from psd2html.candidates import flatten
from psd2html.extract import Extraction
from psd2html.spec import PhotocardDecisions, PhotocardSpec

WINDOW = [100, 300, 800, 500]


def make_extraction(tmp_path: Path, layers: list[dict], width: int = 1000, height: int = 1000, fonts=()) -> Extraction:
    design = tmp_path / "work" / "design.png"
    design.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (8, 8), (200, 200, 200)).save(design)
    return Extraction(
        width=width, height=height, layers=layers, fonts=sorted(fonts), assets=[],
        design_png=design, slices=[], overview=None, slice_scale=1.0, warnings=[],
    )


def text_layer(lid: int, name: str, box: list[int], runs: list[dict], *, align: str | None = None, script: str | None = None, **extra) -> dict:
    paragraph: dict = {"runs": runs}
    if align:
        paragraph["align"] = align
    node = {"id": lid, "name": name, "kind": "type", "box": box, "text": "".join(r["text"] for r in runs), "paragraphs": [paragraph], **extra}
    if script:
        node["script"] = script
    return node


def full_layers() -> list[dict]:
    """A frame-with-a-window design, bottom first."""
    return [
        {"id": 1, "name": "Background", "kind": "solidcolorfill", "box": [0, 0, 1000, 1000], "fill": "#101820"},
        {"id": 2, "name": "Sample", "kind": "pixel", "box": list(WINDOW), "asset": "sample-2.jpg"},
        {
            "id": 3, "name": "Frame", "kind": "pixel", "box": [0, 0, 1000, 1000], "asset": "frame-3.png",
            "transparent_window": list(WINDOW), "transparent_window_radius": 24,
        },
        {
            "id": 4, "name": "Overlays", "kind": "group", "opacity": 0.8,
            "children": [
                {
                    "id": 5, "name": "Shade", "kind": "gradientfill", "box": [0, 500, 1000, 500],
                    "gradient": {
                        "angle": 0,
                        "stops": [{"offset": 0, "color": "#000000", "alpha": 1}, {"offset": 1, "color": "#000000", "alpha": 0}],
                        "scale": 1.2, "shift": [0, 0.1],
                    },
                },
            ],
        },
        {"id": 6, "name": "Sticker", "kind": "pixel", "box": [120, 320, 200, 200], "asset": "sticker-6.png", "clip_to": 3, "clipped_to_layer_below": True},
        {
            "id": 7, "name": "Headline", "kind": "type", "box": [60, 560, 880, 200], "script": "latin",
            "text": "Breaking news\ntonight",
            "paragraphs": [
                {
                    "runs": [
                        {"text": "Breaking ", "font": "Montserrat-Bold", "size_px": 64, "color": "#ffffff", "line_height_px": 70, "caps": "all_caps"},
                        {"text": "news", "font": "Montserrat-Bold", "size_px": 64, "color": "#ffe107", "line_height_px": 70, "caps": "all_caps"},
                    ],
                    "align": "center",
                },
                {"runs": [{"text": "tonight", "font": "Montserrat-Bold", "size_px": 64, "color": "#ffffff", "line_height_px": 70, "caps": "all_caps"}], "align": "center"},
            ],
        },
        text_layer(8, "Date", [60, 40, 300, 40], [{"text": "২৮ সেপ্টেম্বর ২০২৬", "font": "SolaimanLipi", "size_px": 28, "color": "#111111"}], script="bangla"),
        text_layer(9, "Brand", [800, 40, 150, 40], [{"text": "TYGER", "font": "Montserrat-Bold", "size_px": 24, "color": "rgba(255, 255, 255, 1)", "letter_spacing_em": 0.1}], align="right"),
    ]


def full_decisions() -> PhotocardDecisions:
    return PhotocardDecisions.model_validate(
        {
            "headline": {"source": "layer", "layerId": 7},
            "photo": {"source": "window", "layerId": 3},
            "captions": [{"layerId": 8, "role": "date"}],
            "gradients": [{"layerId": 5, "label": "Bottom gradient", "editable": True}],
            "notes": ["A note from the model."],
        }
    )


@pytest.fixture
def full_spec(tmp_path: Path) -> tuple[PhotocardSpec, Path]:
    extraction = make_extraction(tmp_path, full_layers(), fonts=["Montserrat-Bold", "SolaimanLipi"])
    out = tmp_path / "output"
    spec = assemble(extraction, full_decisions(), file_name="design.psd", model="test-model", output_dir=out)
    return spec, out


def test_paint_order(full_spec):
    spec, _ = full_spec
    assert [(layer.kind, layer.key) for layer in spec.layers] == [
        ("photo", "photo"),
        ("image", "frame"),
        ("gradient", "shade"),
        ("image", "sticker"),
        ("headline", "headline"),
        ("caption", "date"),
        ("caption", "brand"),
    ]
    assert spec.canvas.model_dump() == {"width": 1000, "height": 1000}
    assert spec.source.fileName == "design.psd" and spec.source.model == "test-model"


def test_full_canvas_fill_becomes_the_background(full_spec):
    spec, _ = full_spec
    assert spec.background == "#101820"
    assert all(layer.kind != "fill" for layer in spec.layers)
    # A dark page gets the dark placeholder.
    assert spec.photo.emptyFill == "#2b2f36"
    assert spec.photo.placeholderInk == "light"


def test_window_becomes_the_photo_slot_with_its_radius(full_spec):
    spec, _ = full_spec
    photo = spec.photo
    assert [photo.box.x, photo.box.y, photo.box.width, photo.box.height] == WINDOW
    assert photo.radii == [24.0, 24.0, 24.0, 24.0]


def test_sample_photo_under_the_window_is_dropped(full_spec):
    spec, _ = full_spec
    assert not any(layer.kind == "image" and layer.asset == "sample-2.jpg" for layer in spec.layers)
    assert any("Dropped 'Sample'" in warning for warning in spec.warnings)
    assert "A note from the model." in spec.warnings


def test_headline_colours(full_spec):
    spec, _ = full_spec
    headline = spec.headline
    assert headline.defaultText == "Breaking news\ntonight"
    assert headline.baseColor == "#ffffff"
    assert headline.highlightColor == "#ffe107"
    assert [(w.word, w.color) for w in headline.defaultWordColors] == [(1, "#ffe107")]
    assert headline.size == 64 and headline.lineHeight == 70
    assert headline.align == "center" and headline.casing == "uppercase"
    assert headline.autoFit.minSize == 32
    assert headline.shadow is None


def test_bengali_date_caption(full_spec):
    spec, _ = full_spec
    date = next(layer for layer in spec.layers if layer.kind == "caption" and layer.role == "date")
    assert date.text is None
    assert date.dateFormat.model_dump() == {"language": "bn", "monthStyle": "long", "order": "day-first"}
    assert date.color == "#111111" and date.size == 28
    font = next(f for f in spec.fonts if f.key == date.font)
    assert font.postscript == "SolaimanLipi" and font.script == "bangla"


def test_static_caption_keeps_its_text(full_spec):
    spec, _ = full_spec
    brand = next(layer for layer in spec.layers if layer.key == "brand")
    assert brand.role == "static" and brand.text == "TYGER"
    assert brand.color == "#ffffff"  # rgba() run colour normalised
    assert brand.align == "right" and brand.letterSpacing == 0.1


def test_gradient_layer(full_spec):
    spec, _ = full_spec
    shade = next(layer for layer in spec.layers if layer.kind == "gradient")
    assert shade.label == "Bottom gradient" and shade.editable is True
    assert shade.angle == 0
    assert shade.opacity == 0.8  # the group's opacity folded in
    offsets = [stop.offset for stop in shade.stops]
    assert offsets == sorted(offsets)
    assert offsets[0] == 0 and offsets[-1] == 1
    assert all(0 <= t <= 1 for t in offsets)
    assert shade.stops[0].alpha == pytest.approx(0.833, abs=1e-3)
    assert shade.stops[-1].alpha == 0


def test_fold_stops_scale_and_shift():
    gradient = {
        "angle": 0,
        "stops": [{"offset": 0, "color": "#ff0000", "alpha": 1}, {"offset": 1, "color": "#0000ff", "alpha": 1}],
        "scale": 0.5, "shift": [0, 0],
    }
    stops = _fold_stops(gradient, [0, 0, 100, 100])
    assert [(s.offset, s.color) for s in stops] == [(0.0, "#ff0000"), (0.25, "#ff0000"), (0.75, "#0000ff"), (1.0, "#0000ff")]
    # Stops pushed outside the box are replaced by the edge value.
    gradient["scale"] = 3
    stops = _fold_stops(gradient, [0, 0, 100, 100])
    assert [s.offset for s in stops] == [0.0, 1.0]
    assert stops[0].color == "#aa0055" and stops[1].color == "#5500aa"
    # A horizontal gradient uses the width, not the height.
    gradient = {"angle": 90, "stops": gradient["stops"], "scale": 1, "shift": [0.25, 0]}
    stops = _fold_stops(gradient, [0, 0, 200, 50])
    assert [s.offset for s in stops] == [0.0, 0.25, 1.0]


def test_clipped_raster_takes_the_window_as_its_clip(full_spec):
    spec, _ = full_spec
    sticker = next(layer for layer in spec.layers if layer.key == "sticker")
    assert sticker.kind == "image" and sticker.asset == "sticker-6.png"
    assert [sticker.clip.box.x, sticker.clip.box.y, sticker.clip.box.width, sticker.clip.box.height] == WINDOW
    assert sticker.clip.radii == [24.0, 24.0, 24.0, 24.0]


def test_overlay_keys(full_spec):
    spec, _ = full_spec
    assert spec.photo.overlayKeys == ["frame", "shade"]


def test_pixel_density_and_missing_fonts(full_spec, tmp_path):
    spec, _ = full_spec
    assert spec.pixelDensity == 2  # 1000 px canvas: under the 1200 limit
    assert "montserrat" in KNOWN_FAMILIES and "solaimanlipi" not in KNOWN_FAMILIES
    assert spec.missingFonts == ["SolaimanLipi"]
    assert sorted(f.postscript for f in spec.fonts) == ["Montserrat-Bold", "SolaimanLipi"]
    big = make_extraction(tmp_path / "big", full_layers(), width=1215, height=1519, fonts=[])
    big_spec = assemble(big, full_decisions(), file_name="big.psd", model="m", output_dir=tmp_path / "big" / "output")
    assert big_spec.pixelDensity == 1


def test_output_files_are_written(full_spec):
    spec, out = full_spec
    written = json.loads((out / "photocard.json").read_text(encoding="utf-8"))
    assert PhotocardSpec.model_validate(written) == spec
    assert (out / "preview.png").is_file()


def test_unused_assets_are_removed_from_the_output(tmp_path):
    extraction = make_extraction(tmp_path, full_layers())
    out = tmp_path / "output"
    (out / "assets").mkdir(parents=True)
    for name in ("sample-2.jpg", "frame-3.png", "sticker-6.png", "stray.png"):
        (out / "assets" / name).write_bytes(b"x")
    assemble(extraction, full_decisions(), file_name="d.psd", model="m", output_dir=out)
    assert sorted(p.name for p in (out / "assets").iterdir()) == ["frame-3.png", "sticker-6.png"]


# -- _check -----------------------------------------------------------------------------


def by_id(tmp_path):
    return {flat.node["id"]: flat for flat in flatten(make_extraction(tmp_path, full_layers()))}


def decisions(**overrides) -> PhotocardDecisions:
    data = {"headline": {"source": "layer", "layerId": 7}, "photo": {"source": "window", "layerId": 3}}
    data.update(overrides)
    return PhotocardDecisions.model_validate(data)


def test_check_rejects_a_headline_that_is_not_text(tmp_path):
    with pytest.raises(AssemblyError, match="headline layer 3 is not a text layer"):
        _check(decisions(headline={"source": "layer", "layerId": 3}), by_id(tmp_path))


def test_check_rejects_dropping_the_photo_layer(tmp_path):
    with pytest.raises(AssemblyError, match="photo layer is also listed in drop"):
        _check(decisions(drop=[{"layerId": 3, "reason": "oops"}]), by_id(tmp_path))
    with pytest.raises(AssemblyError, match="headline layer is also listed in drop"):
        _check(decisions(drop=[{"layerId": 7, "reason": "oops"}]), by_id(tmp_path))


def test_check_rejects_unknown_ids(tmp_path):
    with pytest.raises(AssemblyError, match="headline names layer 99, which does not exist"):
        _check(decisions(headline={"source": "layer", "layerId": 99}), by_id(tmp_path))
    with pytest.raises(AssemblyError, match="captions names layer 42"):
        _check(decisions(captions=[{"layerId": 42, "role": "site"}]), by_id(tmp_path))
    with pytest.raises(AssemblyError, match="overlayAbovePhoto names layer 77"):
        _check(decisions(overlayAbovePhoto=[77]), by_id(tmp_path))


def test_check_rejects_a_window_on_a_layer_without_one(tmp_path):
    with pytest.raises(AssemblyError, match="photo layer 2 has no transparent window"):
        _check(decisions(photo={"source": "window", "layerId": 2}), by_id(tmp_path))


def test_check_reports_every_problem_at_once(tmp_path):
    with pytest.raises(AssemblyError) as info:
        _check(decisions(headline={"source": "layer", "layerId": 3}, captions=[{"layerId": 3, "role": "site"}]), by_id(tmp_path))
    message = str(info.value)
    assert "headline layer 3 is not a text layer" in message
    assert "caption layer 3 is not a text layer" in message
    assert "also listed as a caption" in message


def test_check_passes_good_decisions(tmp_path):
    _check(full_decisions(), by_id(tmp_path))


# -- the "new" headline path -----------------------------------------------------------


def test_new_headline_over_the_photo(tmp_path):
    layers = [
        {"id": 1, "name": "Background", "kind": "solidcolorfill", "box": [0, 0, 1000, 1000], "fill": "#ffffff"},
        {
            "id": 2, "name": "Frame", "kind": "pixel", "box": [0, 0, 1000, 1000], "asset": "frame-2.png",
            "transparent_window": [50, 50, 900, 900],
        },
    ]
    extraction = make_extraction(tmp_path, layers)
    choice = PhotocardDecisions.model_validate(
        {
            "headline": {"source": "new", "box": {"x": 80, "y": 600, "width": 840, "height": 300}, "align": "left", "reason": "no text in the design"},
            "photo": {"source": "window", "layerId": 2},
        }
    )
    spec = assemble(extraction, choice, file_name="d.psd", model="m", output_dir=tmp_path / "out")
    assert [layer.kind for layer in spec.layers] == ["photo", "image", "headline"]
    headline = spec.headline
    assert headline.defaultText == "Your headline goes here"
    assert headline.vAlign == "bottom" and headline.align == "left"
    assert headline.baseColor == "#ffffff" and headline.highlightColor == "#ffe107"
    assert headline.shadow is not None and headline.shadow.blur == 12
    assert headline.size == 60 and headline.lineHeight == 66
    assert headline.autoFit.minSize == 30
    assert spec.fonts[0].postscript == "Inter-Bold" and spec.missingFonts == []
    assert spec.photo.radii is None  # no radius on the window
    assert any(warning.startswith("Headline placed by the model: no text in the design") for warning in spec.warnings)


def test_new_headline_on_a_light_page_is_dark(tmp_path):
    layers = [
        {"id": 1, "name": "Background", "kind": "solidcolorfill", "box": [0, 0, 1000, 1000], "fill": "#ffffff"},
        {"id": 2, "name": "Picture box", "kind": "shape", "box": [0, 0, 1000, 600], "fill": "#cccccc", "shape": {"type": "rect"}},
    ]
    choice = PhotocardDecisions.model_validate(
        {
            "headline": {"source": "new", "box": {"x": 40, "y": 640, "width": 920, "height": 300}, "reason": "below the picture"},
            "photo": {"source": "layer", "layerId": 2},
        }
    )
    spec = assemble(make_extraction(tmp_path, layers), choice, file_name="d.psd", model="m", output_dir=tmp_path / "out")
    assert spec.headline.baseColor == "#111111" and spec.headline.vAlign == "top" and spec.headline.shadow is None


# -- the "layer" photo path ------------------------------------------------------------


def rounded_photo_layers(radii: list[float]) -> list[dict]:
    return [
        {"id": 1, "name": "Background", "kind": "solidcolorfill", "box": [0, 0, 1000, 1000], "fill": "#ffffff"},
        {"id": 2, "name": "Picture", "kind": "shape", "box": [40, 40, 920, 600], "fill": "#dddddd", "shape": {"type": "rounded_rect", "radii_px": radii}},
        {"id": 3, "name": "Gradient", "kind": "pixel", "box": [40, 340, 920, 300], "asset": "gradient-3.png", "clip_to": 2},
        text_layer(4, "Title", [60, 700, 880, 200], [{"text": "Hello there", "font": "Inter-Bold", "size_px": 60, "color": "#000000"}]),
    ]


def test_layer_photo_on_a_rounded_rect_carries_its_radii(tmp_path):
    choice = PhotocardDecisions.model_validate({"headline": {"source": "layer", "layerId": 4}, "photo": {"source": "layer", "layerId": 2}})
    spec = assemble(make_extraction(tmp_path, rounded_photo_layers([30, 30, 12, 12])), choice, file_name="d.psd", model="m", output_dir=tmp_path / "out")
    assert [(layer.kind, layer.key) for layer in spec.layers] == [("photo", "photo"), ("image", "gradient"), ("headline", "headline")]
    photo = spec.photo
    assert [photo.box.x, photo.box.y, photo.box.width, photo.box.height] == [40, 40, 920, 600]
    assert photo.radii == [30.0, 30.0, 12.0, 12.0]
    # The clipped raster is cut to the photo slot, radii included.
    gradient = spec.layers[1]
    assert gradient.clip.radii == [30.0, 30.0, 12.0, 12.0]
    assert gradient.clip.box == photo.box
    assert photo.overlayKeys == []
    # Light page, dark placeholder ink.
    assert photo.emptyFill == "#e5e7eb" and photo.placeholderInk == "dark"


def test_layer_photo_drops_tiny_radii(tmp_path):
    choice = PhotocardDecisions.model_validate({"headline": {"source": "layer", "layerId": 4}, "photo": {"source": "layer", "layerId": 2}})
    spec = assemble(make_extraction(tmp_path, rounded_photo_layers([0.4, 0.4, 0.4, 0.4])), choice, file_name="d.psd", model="m", output_dir=tmp_path / "out")
    assert spec.photo.radii is None
    assert spec.layers[1].clip.radii is None


def test_overlay_above_photo_decision_is_honoured(tmp_path):
    layers = rounded_photo_layers([0, 0, 0, 0])
    layers.insert(3, {"id": 5, "name": "Logo", "kind": "pixel", "box": [60, 60, 120, 120], "asset": "logo-5.png"})
    choice = PhotocardDecisions.model_validate(
        {"headline": {"source": "layer", "layerId": 4}, "photo": {"source": "layer", "layerId": 2}, "overlayAbovePhoto": [5]}
    )
    spec = assemble(make_extraction(tmp_path, layers), choice, file_name="d.psd", model="m", output_dir=tmp_path / "out")
    assert spec.photo.overlayKeys == ["logo"]
