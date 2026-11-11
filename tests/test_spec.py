"""The PhotocardSpec contract: schema export, fixtures, validation rules."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from psd2html.spec import PhotocardDecisions, PhotocardSpec, schema_json, slug_key

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "src" / "psd2html" / "photocard_schema.json"
FIXTURES = sorted((Path(__file__).parent / "fixtures").glob("*.json"))

REGENERATE = (
    "photocard_schema.json is stale; regenerate it with:\n"
    "  uv run python -c \"from pathlib import Path; from psd2html.spec import schema_json; "
    "Path('src/psd2html/photocard_schema.json').write_text(schema_json(), encoding='utf-8')\""
)


def test_exported_schema_matches_the_model():
    assert SCHEMA_PATH.read_text(encoding="utf-8") == schema_json(), REGENERATE


def test_schema_forbids_extra_properties_everywhere():
    schema = json.loads(schema_json())
    for name, definition in schema["$defs"].items():
        if definition.get("type") == "object":
            assert definition.get("additionalProperties") is False, name
    assert schema["additionalProperties"] is False


@pytest.mark.parametrize("path", FIXTURES, ids=[p.stem for p in FIXTURES])
def test_fixture_parses(path: Path):
    spec = PhotocardSpec.model_validate_json(path.read_text(encoding="utf-8"))
    assert spec.photo.kind == "photo"
    assert spec.headline.kind == "headline"
    # Round-trips through JSON unchanged.
    again = PhotocardSpec.model_validate_json(spec.model_dump_json())
    assert again == spec


# -- invalid specs ----------------------------------------------------------------


def minimal_spec() -> dict:
    return {
        "version": 1,
        "canvas": {"width": 1080, "height": 1350},
        "fonts": [{"key": "fbold", "postscript": "Inter-Bold", "family": "Inter", "weight": 700}],
        "layers": [
            {"kind": "photo", "key": "photo", "box": {"x": 0, "y": 0, "width": 1080, "height": 900}},
            {
                "kind": "headline",
                "key": "headline",
                "box": {"x": 40, "y": 940, "width": 1000, "height": 300},
                "font": "fbold",
                "size": 64,
                "lineHeight": 72,
                "baseColor": "#111111",
                "highlightColor": "#ffe107",
                "defaultText": "Three word headline",
            },
        ],
        "source": {"fileName": "design.psd", "model": "fixture", "generatedAt": "2026-11-11T00:00:00Z"},
    }


def test_minimal_spec_is_valid():
    spec = PhotocardSpec.model_validate(minimal_spec())
    assert spec.background == "#ffffff"
    assert spec.pixelDensity == 1


def caption(key: str, role: str, **extra) -> dict:
    return {
        "kind": "caption", "key": key, "role": role, "font": "fbold", "size": 24, "color": "#000000",
        "box": {"x": 0, "y": 0, "width": 100, "height": 30}, **extra,
    }


def test_two_photo_layers_fail():
    data = minimal_spec()
    data["layers"].append({"kind": "photo", "key": "photo2", "box": {"x": 0, "y": 0, "width": 10, "height": 10}})
    with pytest.raises(ValidationError, match="exactly one photo layer"):
        PhotocardSpec.model_validate(data)


def test_no_headline_fails():
    data = minimal_spec()
    data["layers"] = [layer for layer in data["layers"] if layer["kind"] != "headline"]
    data["layers"].append(caption("site", "site"))
    with pytest.raises(ValidationError, match="exactly one headline layer"):
        PhotocardSpec.model_validate(data)


def test_unknown_font_key_fails():
    data = minimal_spec()
    data["layers"][1]["font"] = "nosuchfont"
    with pytest.raises(ValidationError, match="unknown font 'nosuchfont'"):
        PhotocardSpec.model_validate(data)


def test_duplicate_layer_keys_fail():
    data = minimal_spec()
    data["layers"].append(caption("headline", "site"))
    with pytest.raises(ValidationError, match="duplicate layer keys"):
        PhotocardSpec.model_validate(data)


def test_static_caption_needs_text():
    data = minimal_spec()
    data["layers"].append(caption("brand", "static"))
    with pytest.raises(ValidationError, match="static caption needs text"):
        PhotocardSpec.model_validate(data)
    data["layers"][-1]["text"] = "   "
    with pytest.raises(ValidationError, match="static caption needs text"):
        PhotocardSpec.model_validate(data)
    data["layers"][-1]["text"] = "TYGER"
    assert PhotocardSpec.model_validate(data).layers[-1].text == "TYGER"


def test_non_static_caption_may_not_carry_text():
    data = minimal_spec()
    data["layers"].append(caption("date", "date", text="28 Sept 2026"))
    with pytest.raises(ValidationError, match="only static captions carry text"):
        PhotocardSpec.model_validate(data)


def test_date_caption_gets_a_default_date_format():
    data = minimal_spec()
    data["layers"].append(caption("date", "date"))
    spec = PhotocardSpec.model_validate(data)
    assert spec.layers[-1].dateFormat is not None
    assert spec.layers[-1].dateFormat.model_dump() == {"language": "en", "monthStyle": "short", "order": "day-first"}


def test_word_colour_beyond_the_word_count_fails():
    data = minimal_spec()
    data["layers"][1]["defaultWordColors"] = [{"word": 2, "color": "#ff0000"}, {"word": 3, "color": "#ff0000"}]
    with pytest.raises(ValidationError, match=r"names words \[3\] but the headline has 3"):
        PhotocardSpec.model_validate(data)


def test_word_colour_counts_words_like_palette():
    data = minimal_spec()
    data["layers"][1]["defaultText"] = "as 'Kutta Mizan'"
    data["layers"][1]["defaultWordColors"] = [{"word": 2, "color": "#ff0000"}]
    assert PhotocardSpec.model_validate(data).headline.defaultWordColors[0].word == 2
    data["layers"][1]["defaultWordColors"] = [{"word": 3, "color": "#ff0000"}]
    with pytest.raises(ValidationError):
        PhotocardSpec.model_validate(data)


def test_overlay_keys_must_name_a_layer():
    data = minimal_spec()
    data["layers"][0]["overlayKeys"] = ["frame"]
    with pytest.raises(ValidationError, match="overlayKeys names unknown layer 'frame'"):
        PhotocardSpec.model_validate(data)


def test_extra_fields_are_rejected():
    data = minimal_spec()
    data["layers"][0]["zIndex"] = 3
    with pytest.raises(ValidationError, match="zIndex"):
        PhotocardSpec.model_validate(data)


def test_colours_must_be_lower_case_hex():
    data = minimal_spec()
    data["layers"][1]["baseColor"] = "#FFFFFF"
    with pytest.raises(ValidationError):
        PhotocardSpec.model_validate(data)
    data["layers"][1]["baseColor"] = "white"
    with pytest.raises(ValidationError):
        PhotocardSpec.model_validate(data)


# -- slug_key ---------------------------------------------------------------------

KEY = re.compile(r"^[a-z][a-z0-9]{0,31}$")


def test_slug_key_shape_and_uniqueness():
    used: set[str] = set()
    keys = [slug_key(name, used) for name in ["Logo", "logo", "LOGO!!", "Layer 2", "2nd bar", "", "   ", "গ্রেডিয়েন্ট"]]
    assert keys == ["logo", "logo2", "logo3", "layer2", "l2ndbar", "l", "l2", "l3"]
    assert len(set(keys)) == len(keys)
    assert all(KEY.match(key) for key in keys)
    assert used == set(keys)


def test_slug_key_custom_prefix_and_length_cap():
    used: set[str] = set()
    assert slug_key("42", used, prefix="f") == "f42"
    long_name = "x" * 100
    first = slug_key(long_name, used)
    second = slug_key(long_name, used)
    assert first == "x" * 24
    assert second == "x" * 24 + "2"  # the suffix fits under the 32-character cap
    assert all(len(key) <= 32 and KEY.match(key) for key in (first, second))


# -- PhotocardDecisions ------------------------------------------------------------


def test_decisions_parse_a_minimal_document():
    decisions = PhotocardDecisions.model_validate(
        {"headline": {"source": "layer", "layerId": 3}, "photo": {"source": "window", "layerId": 1}}
    )
    assert decisions.headline.layerId == 3
    assert decisions.photo.source == "window"
    assert decisions.captions == [] and decisions.drop == [] and decisions.gradients == []
    assert decisions.overlayAbovePhoto == [] and decisions.notes == []


def test_decisions_accept_new_headline_and_photo():
    decisions = PhotocardDecisions.model_validate(
        {
            "headline": {"source": "new", "box": {"x": 0, "y": 0, "width": 10, "height": 10}, "reason": "no text"},
            "photo": {"source": "new", "box": {"x": 0, "y": 0, "width": 10, "height": 10}, "reason": "no slot"},
        }
    )
    assert decisions.headline.align == "center"
    assert decisions.photo.radii is None


def test_decisions_reject_an_unknown_field():
    with pytest.raises(ValidationError, match="extra"):
        PhotocardDecisions.model_validate(
            {"headline": {"source": "layer", "layerId": 3}, "photo": {"source": "window", "layerId": 1}, "colour": "#fff"}
        )
    with pytest.raises(ValidationError):
        PhotocardDecisions.model_validate({"headline": {"source": "layer", "layerId": 3, "box": {}}, "photo": {"source": "window", "layerId": 1}})
