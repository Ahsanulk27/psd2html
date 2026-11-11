"""Pure helpers in extract.py, exercised without a PSD."""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image, ImageDraw

from psd2html.extract import _enum_code, _merge_gradient_stops, _script_of, _transparent_window

# -- _merge_gradient_stops ---------------------------------------------------------

OPAQUE = [(0.0, 1.0), (1.0, 1.0)]  # what _gradient_fill substitutes when a gradient has no alpha stops


def test_colour_only_stops_gain_alpha_one():
    stops = _merge_gradient_stops([(0.0, "#000000"), (0.5, "#ff0000"), (1.0, "#ffffff")], list(OPAQUE))
    assert stops == [(0.0, "#000000", 1.0), (0.5, "#ff0000", 1.0), (1.0, "#ffffff", 1.0)]


def test_interleaved_colour_and_alpha_locations_interpolate():
    stops = _merge_gradient_stops([(0.0, "#000000"), (1.0, "#ffffff")], [(0.5, 1.0), (1.0, 0.0)])
    assert [t for t, _, _ in stops] == [0.0, 0.5, 1.0]
    assert stops[0] == (0.0, "#000000", 1.0)  # alpha before the first alpha stop is held
    assert stops[1] == (0.5, "#808080", 1.0)  # colour interpolated half way
    assert stops[2] == (1.0, "#ffffff", 0.0)


def test_alpha_only_location_gets_an_interpolated_colour():
    stops = _merge_gradient_stops([(0.0, "#000000"), (1.0, "#ffffff")], [(0.0, 1.0), (0.25, 0.0)])
    assert stops[1][0] == 0.25
    assert stops[1][1] == "#404040"
    assert stops[1][2] == 0.0
    assert stops[2] == (1.0, "#ffffff", 0.0)  # alpha after the last alpha stop is held


def test_unsorted_input_is_sorted_and_clamped():
    stops = _merge_gradient_stops([(1.2, "#ffffff"), (-0.2, "#000000")], [(0.3, 0.5), (0.0, 1.0)])
    assert [t for t, _, _ in stops] == [0.0, 0.0, 0.3, 1.0]
    assert stops[-1][1] == "#ffffff"
    alpha_at_03 = next(a for t, _, a in stops if t == 0.3)
    assert alpha_at_03 == 0.5


# -- _enum_code ---------------------------------------------------------------------


def test_enum_code_decodes_bytes_and_lower_cases():
    assert _enum_code(SimpleNamespace(enum=b"Lnr ")) == "lnr"
    assert _enum_code(SimpleNamespace(enum="Rdl ")) == "rdl"
    assert _enum_code(SimpleNamespace(enum=None)) is None
    assert _enum_code(SimpleNamespace(value=3)) is None
    assert _enum_code(None) is None


# -- _transparent_window ------------------------------------------------------------


def frame(size: tuple[int, int], hole: tuple[int, int, int, int] | None, radius: int = 0) -> Image.Image:
    """An opaque image with a see-through hole (x1, y1, x2, y2), optionally rounded."""
    image = Image.new("RGBA", size, (20, 30, 40, 255))
    if hole:
        mask = Image.new("L", size, 255)
        draw = ImageDraw.Draw(mask)
        if radius:
            draw.rounded_rectangle((hole[0], hole[1], hole[2] - 1, hole[3] - 1), radius=radius, fill=0)
        else:
            draw.rectangle((hole[0], hole[1], hole[2] - 1, hole[3] - 1), fill=0)
        image.putalpha(mask)
    return image


def test_square_hole_is_reported_in_canvas_coordinates():
    image = frame((200, 200), (50, 50, 150, 150))
    window = _transparent_window(image, (10, 20, 210, 220))
    assert window == {"box": [60, 70, 100, 100], "radius": 0}


def test_rounded_hole_estimates_its_radius():
    image = frame((400, 300), (100, 60, 300, 260), radius=24)
    window = _transparent_window(image, (0, 0, 400, 300))
    assert window is not None
    x, y, w, h = window["box"]
    assert (w, h) == (200, 200)
    # The rounded corner hides the first few pixels of the top row: the bbox of
    # the clear pixels starts at the hole's edge once the straight sides begin.
    assert abs(x - 100) <= 1 and abs(y - 60) <= 1
    assert 0 < window["radius"] < w // 4
    assert 18 <= window["radius"] <= 26


def test_hole_touching_the_edge_is_not_a_window():
    image = frame((200, 200), (0, 50, 100, 150))
    assert _transparent_window(image, (0, 0, 200, 200)) is None


def test_tiny_hole_is_not_a_window():
    image = frame((200, 200), (90, 90, 100, 100))  # 100 px of 40,000: under 5 %
    assert _transparent_window(image, (0, 0, 200, 200)) is None


def test_sparse_transparency_is_not_a_window():
    # A diagonal of clear pixels covers a large bbox but little of it.
    alpha = np.full((200, 200), 255, dtype=np.uint8)
    for i in range(20, 180):
        alpha[i, max(0, i - 6) : i + 6] = 0
    image = Image.new("RGBA", (200, 200), (0, 0, 0, 255))
    image.putalpha(Image.fromarray(alpha))
    assert _transparent_window(image, (0, 0, 200, 200)) is None


def test_fully_opaque_image_has_no_window():
    assert _transparent_window(frame((100, 100), None), (0, 0, 100, 100)) is None


# -- _script_of -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [("Hello", "latin"), ("বাংলা", "bangla"), ("Hello বাংলা", "mixed"), ("", "latin"), ("2026", "latin")],
)
def test_script_of(text, expected):
    assert _script_of(text) == expected
