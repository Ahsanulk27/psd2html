"""Read a PSD and turn it into things an AI model can use.

Produces, inside a work directory:
  design.png          full flattened render at original size (for previews)
  slices/slice-N.png  the render cut into model-friendly pieces, top to bottom
  overview.png        whole page scaled down (only when there is more than one slice)
  layers.json         the layer tree: boxes, text + fonts, fill colors, asset links
and inside an output directory:
  assets/*.png|jpg    image layers cropped out of the PSD, ready for <img> tags
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image
from psd_tools import PSDImage
from psd_tools.constants import Tag

# Each image sent to the model stays within 2000 px per side; slices are cut
# a bit smaller so text in them stays legible.
SLICE_MAX_WIDTH = 1600
SLICE_MAX_HEIGHT = 1600
MAX_SLICES = 12
OVERVIEW_MAX_SIDE = 2000

MAX_ASSETS = 250
MIN_ASSET_SIDE = 4
JPEG_MIN_PIXELS = 200 * 200  # opaque images larger than this are saved as JPEG
# Slices full of photos compress badly as PNG; past this size they are saved as
# JPEG so a request with many slices stays far below the API's size limits.
MODEL_IMAGE_MAX_PNG_BYTES = 1_500_000

# Layer kinds we export as image files. Groups are walked instead; adjustment
# layers only change other layers, so they are already baked into the render.
IMAGE_KINDS = {"pixel", "smartobject", "shape", "gradientfill", "patternfill"}


@dataclass
class Slice:
    path: Path
    top: int  # in original canvas pixels
    bottom: int


@dataclass
class Extraction:
    width: int
    height: int
    layers: list[dict]
    fonts: list[str]
    assets: list[dict]
    design_png: Path
    slices: list[Slice]
    overview: Path | None
    slice_scale: float  # slice pixels per canvas pixel
    warnings: list[str] = field(default_factory=list)

    def layers_json(self) -> str:
        return json.dumps(
            {
                "canvas": {"width": self.width, "height": self.height},
                "fonts": self.fonts,
                "assets": self.assets,
                "layers": self.layers,
            },
            separators=(",", ":"),
            ensure_ascii=False,
        )


def extract(psd_path: Path, work_dir: Path, output_dir: Path) -> Extraction:
    work_dir.mkdir(parents=True, exist_ok=True)
    assets_dir = output_dir / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)

    psd = PSDImage.open(psd_path)
    warnings: list[str] = []

    design = _render(psd, warnings)
    design_png = work_dir / "design.png"
    design.save(design_png, optimize=True)

    slices, scale = _slice(design, work_dir / "slices")
    overview = None
    if len(slices) > 1:
        overview = _save_for_model(_fit(design, OVERVIEW_MAX_SIDE), work_dir / "overview")

    walker = _Walker(assets_dir, warnings, (psd.width, psd.height))
    layers = walker.walk(psd)

    extraction = Extraction(
        width=psd.width,
        height=psd.height,
        layers=layers,
        fonts=sorted(walker.fonts),
        assets=walker.assets,
        design_png=design_png,
        slices=slices,
        overview=overview,
        slice_scale=scale,
        warnings=warnings,
    )
    (work_dir / "layers.json").write_text(extraction.layers_json(), encoding="utf-8")
    return extraction


# ---------------------------------------------------------------------------
# Rendering and slicing


def _render(psd: PSDImage, warnings: list[str]) -> Image.Image:
    try:
        image = psd.composite()
    except Exception as exc:  # psd-tools can't render every effect
        image = None
        warnings.append(f"Could not render layers ({exc}); used the PSD's saved preview.")
    if image is None:
        image = psd.topil()
    if image is None:
        raise ValueError(
            "This PSD has no renderable layers and no saved preview. "
            "Re-save it in Photoshop with 'Maximize compatibility' turned on."
        )
    return _flatten(image)


def _flatten(image: Image.Image) -> Image.Image:
    """Put transparent areas on white so the model sees what a browser would."""
    image = image.convert("RGBA")
    background = Image.new("RGBA", image.size, (255, 255, 255, 255))
    return Image.alpha_composite(background, image).convert("RGB")


def _slice(design: Image.Image, out_dir: Path) -> tuple[list[Slice], float]:
    out_dir.mkdir(parents=True, exist_ok=True)
    width, height = design.size
    scale = min(1.0, SLICE_MAX_WIDTH / width)
    if math.ceil(height * scale / SLICE_MAX_HEIGHT) > MAX_SLICES:
        scale = MAX_SLICES * SLICE_MAX_HEIGHT / height
    scaled = design
    if scale < 1.0:
        scaled = design.resize(
            (max(1, round(width * scale)), max(1, round(height * scale))),
            Image.LANCZOS,
        )

    slices = []
    for index, top in enumerate(range(0, scaled.height, SLICE_MAX_HEIGHT)):
        bottom = min(top + SLICE_MAX_HEIGHT, scaled.height)
        path = _save_for_model(scaled.crop((0, top, scaled.width, bottom)), out_dir / f"slice-{index + 1}")
        slices.append(Slice(path, round(top / scale), round(bottom / scale)))
    return slices, scale


def _save_for_model(image: Image.Image, stem: Path) -> Path:
    png = stem.with_suffix(".png")
    image.save(png, optimize=True)
    if png.stat().st_size <= MODEL_IMAGE_MAX_PNG_BYTES:
        return png
    png.unlink()
    jpg = stem.with_suffix(".jpg")
    image.convert("RGB").save(jpg, quality=90, optimize=True)
    return jpg


def _fit(image: Image.Image, max_side: int) -> Image.Image:
    scale = min(1.0, max_side / max(image.size))
    if scale == 1.0:
        return image
    size = (max(1, round(image.width * scale)), max(1, round(image.height * scale)))
    return image.resize(size, Image.LANCZOS)


# ---------------------------------------------------------------------------
# Layer tree


class _Walker:
    def __init__(self, assets_dir: Path, warnings: list[str], canvas: tuple[int, int]):
        self.assets_dir = assets_dir
        self.canvas = canvas
        self.warnings = warnings
        self.assets: list[dict] = []
        self.fonts: set[str] = set()
        self._skipped_assets = 0
        self._next_id = 0

    def walk(self, group) -> list[dict]:
        # psd-tools lists children bottom-most first, which is also paint order.
        nodes = []
        for layer in group:
            node = self._layer(layer)
            if node is not None:
                nodes.append(node)
        if group is not None and isinstance(group, PSDImage) and self._skipped_assets:
            self.warnings.append(
                f"{self._skipped_assets} image layers were not exported "
                f"(limit is {MAX_ASSETS})."
            )
        return nodes

    def _layer(self, layer) -> dict | None:
        if not layer.is_visible():
            return None
        # Only the part of a layer that lies on the canvas matters for HTML.
        left, top, right, bottom = layer.bbox
        left, top = max(left, 0), max(top, 0)
        right, bottom = min(right, self.canvas[0]), min(bottom, self.canvas[1])
        width, height = right - left, bottom - top
        if layer.bbox != (0, 0, 0, 0) and (width <= 0 or height <= 0):
            return None  # entirely off the canvas
        kind = layer.kind

        self._next_id += 1
        node: dict = {"id": self._next_id, "name": layer.name, "kind": kind}
        if width > 0 and height > 0:
            node["box"] = [left, top, width, height]
        if layer.opacity < 255:
            node["opacity"] = round(layer.opacity / 255, 2)
        blend = _enum_name(layer.blend_mode)
        if blend and blend not in ("normal", "pass_through"):
            node["blend"] = blend
        clipped = getattr(layer, "clipping", False)
        if clipped:
            node["clipped_to_layer_below"] = True
        effects = _effects(layer)
        if effects:
            node["effects"] = effects

        if kind == "group":
            children = self.walk(layer)
            if not children:
                return None
            node["children"] = children
            return node

        if kind == "type":
            node.update(self._text(layer))
            return node

        if "box" not in node:
            return None  # empty layer

        if kind == "solidcolorfill":
            color = _solid_fill(layer)
            if color:
                node["fill"] = color
            return node

        if kind == "shape":
            fill = _solid_fill(layer)
            if fill:
                node["fill"] = fill
            simple = _simple_shape(layer)
            if simple:
                node["shape"] = simple
                return node  # plain rectangles and circles are built in CSS

        # A clipped layer is already baked into the image of the layer it clips to.
        if kind in IMAGE_KINDS and not clipped:
            asset, fill, window = self._export(layer, node["id"], (left, top, right, bottom))
            if fill:
                node["fill"] = fill
            if asset:
                node["asset"] = asset
            if window:
                node["transparent_window"] = window
        return node

    def _text(self, layer) -> dict:
        out: dict = {"text": layer.text}
        try:
            ts = layer.typesetting
        except Exception:
            return out
        scale = _text_scale(layer)
        paragraphs = []
        for paragraph in ts:
            runs: list[dict] = []
            for run in paragraph.runs:
                if not run.text.strip("\r\n"):
                    continue
                style = _run_style(run.style, scale)
                if style.get("font"):
                    self.fonts.add(style["font"])
                text = run.text.replace("\r", "\n").replace("\u0003", "\n")
                if runs and _same_style(runs[-1], style):
                    runs[-1]["text"] += text
                else:
                    runs.append({"text": text, **style})
            if not runs:
                continue
            for run in runs:
                run["text"] = run["text"].rstrip("\n")
            align = _enum_name(paragraph.style.justification)
            entry: dict = {"runs": runs}
            if align and align != "left":
                entry["align"] = align
            paragraphs.append(entry)
        if paragraphs:
            out["paragraphs"] = paragraphs
        text_type = _enum_name(getattr(layer, "text_type", None))
        if text_type:
            out["text_box"] = "area" if text_type == "paragraph" else "point"
        return out

    def _export(self, layer, node_id: int, box: tuple[int, int, int, int]):
        """Save the on-canvas part of a layer as an image file.

        Returns (file name, solid fill color, transparent window box).
        """
        width, height = box[2] - box[0], box[3] - box[1]
        if width < MIN_ASSET_SIDE or height < MIN_ASSET_SIDE:
            return None, None, None
        try:
            image = layer.composite(viewport=box)
        except Exception as exc:
            self.warnings.append(f"Could not export layer '{layer.name}': {exc}")
            return None, None, None
        if image is None:
            return None, None, None
        image = image.convert("RGBA")
        alpha_min, alpha_max = image.getchannel("A").getextrema()
        if alpha_max == 0:
            return None, None, None  # nothing visible

        fill = _single_color(image)
        if fill and alpha_min == 255:
            return None, fill, None  # a plain colored rectangle: CSS, not an image
        if len(self.assets) >= MAX_ASSETS:
            self._skipped_assets += 1
            return None, fill, None
        window = _transparent_window(image, box)

        stem = f"{_slug(layer.name)}-{node_id}"
        if alpha_min == 255 and width * height >= JPEG_MIN_PIXELS:
            name = f"{stem}.jpg"
            image.convert("RGB").save(self.assets_dir / name, quality=85, optimize=True)
        else:
            name = f"{stem}.png"
            image.save(self.assets_dir / name, optimize=True)
        self.assets.append({"file": name, "layer_id": node_id, "width": image.width, "height": image.height})
        return name, fill, window


# ---------------------------------------------------------------------------
# Helpers


def _single_color(image: Image.Image) -> str | None:
    """Hex color if every visible pixel has the same RGB (solid shapes, flat text)."""
    rgba = np.asarray(image)
    visible = rgba[..., 3] > 0
    rgb = rgba[..., :3][visible]
    if rgb.size == 0:
        return None
    first = rgb[0]
    if np.all(np.abs(rgb.astype(np.int16) - first) <= 2):
        return _hex(*first)
    return None


def _transparent_window(image: Image.Image, box: tuple[int, int, int, int]) -> list[int] | None:
    """Canvas box of a see-through hole inside an image (e.g. a photo frame).

    Reported only when the fully transparent pixels make up a large area that
    doesn't touch the image's edges, so ordinary cut-out shapes are ignored.
    """
    alpha = np.asarray(image.getchannel("A"))
    clear = alpha == 0
    if clear.sum() < 0.05 * clear.size:
        return None
    ys, xs = np.where(clear)
    x1, y1, x2, y2 = int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1
    h, w = alpha.shape
    if x1 == 0 or y1 == 0 or x2 == w or y2 == h:
        return None
    if clear[y1:y2, x1:x2].mean() < 0.8:
        return None
    return [box[0] + x1, box[1] + y1, x2 - x1, y2 - y1]


def _enum_name(value) -> str | None:
    if value is None:
        return None
    name = getattr(value, "name", None) or str(value)
    return name.split(".")[-1].lower()


def _slug(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug[:40] or "layer"


def _hex(r: float, g: float, b: float) -> str:
    return "#{:02x}{:02x}{:02x}".format(*(max(0, min(255, round(c))) for c in (r, g, b)))


def _argb_to_css(argb: tuple[float, ...] | None) -> str | None:
    if not argb or len(argb) < 4:
        return None
    a, r, g, b = argb[:4]
    color = _hex(r * 255, g * 255, b * 255)
    if a < 0.999:
        return f"rgba({round(r * 255)},{round(g * 255)},{round(b * 255)},{round(a, 2)})"
    return color


def _text_scale(layer) -> float:
    """Font sizes are stored before the text layer's transform is applied."""
    try:
        xx, xy, yx, yy, _, _ = layer.transform
        scale = math.hypot(xy, yy)
        return scale if scale > 0 else 1.0
    except Exception:
        return 1.0


def _run_style(style, scale: float) -> dict:
    out: dict = {}
    font = style.font
    if font is not None:
        out["font"] = font.postscript_name
    out["size_px"] = round(style.font_size * scale, 1)
    color = _argb_to_css(style.fill_color)
    if color:
        out["color"] = color
    if not style.auto_leading and style.leading:
        out["line_height_px"] = round(style.leading * scale, 1)
    if style.tracking:
        out["letter_spacing_em"] = round(style.tracking / 1000, 3)
    caps = _enum_name(style.font_caps)
    if caps and caps != "normal":
        out["caps"] = caps
    if style.underline:
        out["underline"] = True
    if style.strikethrough:
        out["strikethrough"] = True
    if style.faux_bold:
        out["faux_bold"] = True
    if style.faux_italic:
        out["faux_italic"] = True
    return out


def _same_style(a: dict, b: dict) -> bool:
    return {k: v for k, v in a.items() if k != "text"} == b


def _solid_fill(layer) -> str | None:
    try:
        data = layer.tagged_blocks.get_data(Tag.SOLID_COLOR_SHEET_SETTING)
        if data is None:
            return None
        color = data.get(b"Clr ")
        r, g, b = (float(getattr(color.get(k), "value", color.get(k))) for k in (b"Rd  ", b"Grn ", b"Bl  "))
        return _hex(r, g, b)
    except Exception:
        return None


def _simple_shape(layer) -> dict | None:
    """Describe live rectangles/ellipses so they can be drawn with CSS."""
    try:
        if not layer.has_origination() or layer.has_effects() and _has_complex_effects(layer):
            return None
        shapes = layer.origination
        if len(shapes) != 1:
            return None
        shape = shapes[0]
        kind = type(shape).__name__
        if kind == "Rectangle":
            return {"type": "rect"}
        if kind == "RoundedRectangle":
            radii = _corner_radii(shape.radii)
            return {"type": "rounded_rect", "radii_px": radii} if radii else {"type": "rounded_rect"}
        if kind == "Ellipse":
            return {"type": "ellipse"}
        if kind == "Line":
            return {"type": "line"}
    except Exception:
        return None
    return None


def _corner_radii(descriptor) -> list[float] | None:
    """Radii in CSS order: top-left, top-right, bottom-right, bottom-left."""
    if descriptor is None:
        return None
    try:
        keys = (b"topLeft", b"topRight", b"bottomRight", b"bottomLeft")
        return [round(float(getattr(descriptor.get(k), "value", descriptor.get(k))), 1) for k in keys]
    except Exception:
        return None


def _has_complex_effects(layer) -> bool:
    return any(name not in ("drop_shadow", "stroke") for name in _effects(layer))


def _effects(layer) -> list[str]:
    try:
        if not layer.has_effects():
            return []
        names = []
        for effect in layer.effects:
            if getattr(effect, "enabled", True):
                names.append(re.sub(r"(?<!^)(?=[A-Z])", "_", type(effect).__name__).lower())
        return names
    except Exception:
        return []
