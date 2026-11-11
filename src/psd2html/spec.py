"""The photocard layout spec: the contract between this service and Palette.

`PhotocardSpec` is what a conversion produces and what Palette stores and
draws. Palette mirrors it with a zod schema (`lib/news-templates/spec/schema.ts`)
and both sides validate the same fixture files, so a change here is a change
there. `photocard_schema.json` next to this file is the exported JSON Schema;
`tests/test_spec.py` fails when it is stale.

`PhotocardDecisions` is the much smaller document Claude returns: which
extracted layer plays which role. Everything numeric in the spec comes from
the PSD itself, never from the model (see assemble.py).

Rules for both models: no dict fields (Claude's structured outputs need
`additionalProperties: false` everywhere, so maps are lists with a `key`),
camelCase field names (they are the JSON), `extra="forbid"`.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .headline import count_words

SPEC_VERSION = 1
MAX_CANVAS_SIDE = 8000

Color = Annotated[str, Field(pattern=r"^#[0-9a-f]{6}$", description="#rrggbb, lower-case")]
Key = Annotated[str, Field(pattern=r"^[a-z][a-z0-9]{0,31}$")]
Unit = Annotated[float, Field(ge=0, le=1)]
Radii = Annotated[list[Annotated[float, Field(ge=0)]], Field(min_length=4, max_length=4)]

Align = Literal["left", "center", "right"]
VAlign = Literal["top", "center", "bottom"]
Blend = Literal["normal", "multiply", "screen", "overlay"]
Script = Literal["latin", "bangla", "mixed"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Box(_Model):
    """Document pixels, origin top-left."""

    x: float
    y: float
    width: Annotated[float, Field(gt=0)]
    height: Annotated[float, Field(gt=0)]


class SpecFont(_Model):
    key: Key
    postscript: str = Field(min_length=1, max_length=120)
    family: str = Field(min_length=1, max_length=120)
    weight: Annotated[int, Field(ge=100, le=900)] = 400
    style: Literal["normal", "italic"] = "normal"
    script: Script = "latin"


class Clip(_Model):
    box: Box
    radii: Radii | None = None


class ImageLayer(_Model):
    kind: Literal["image"]
    key: Key
    asset: str = Field(pattern=r"^[\w.\-]+\.(png|jpg|jpeg|webp)$")
    box: Box
    opacity: Unit = 1
    blend: Blend = "normal"
    clip: Clip | None = None


class Stroke(_Model):
    color: Color
    width: Annotated[float, Field(gt=0)]


class RectShape(_Model):
    type: Literal["rect"]


class RoundedRectShape(_Model):
    type: Literal["rounded_rect"]
    radii: Radii


class EllipseShape(_Model):
    type: Literal["ellipse"]


Shape = Annotated[RectShape | RoundedRectShape | EllipseShape, Field(discriminator="type")]


class FillLayer(_Model):
    kind: Literal["fill"]
    key: Key
    box: Box
    color: Color
    shape: Shape
    opacity: Unit = 1
    stroke: Stroke | None = None


class GradientStop(_Model):
    offset: Unit
    color: Color
    alpha: Unit


class GradientLayer(_Model):
    """A linear gradient over `box`, at a CSS angle (0 = up, 90 = right)."""

    kind: Literal["gradient"]
    key: Key
    label: str = Field(min_length=1, max_length=40)
    box: Box
    angle: float
    stops: Annotated[list[GradientStop], Field(min_length=2, max_length=16)]
    clip: Clip | None = None
    editable: bool = True
    opacity: Unit = 1
    blend: Blend = "normal"


class PhotoLayer(_Model):
    """The one slot the author's photo goes in."""

    kind: Literal["photo"]
    key: Key
    box: Box
    radii: Radii | None = None
    defaultFit: Literal["cover", "contain"] = "cover"
    emptyFill: Color = "#e5e7eb"
    placeholderInk: Literal["light", "dark"] = "dark"
    overlayKeys: list[Key] = Field(
        default_factory=list,
        description="layers above the photo that a lifted subject is drawn in front of (frames, rims, shades); text and everything above the first text layer stay on top of it",
    )


class Shadow(_Model):
    color: Color = "#000000"
    alpha: Unit = 0.5
    offsetX: float = 0
    offsetY: float = 0
    blur: Annotated[float, Field(ge=0)] = 0


class WordColor(_Model):
    word: Annotated[int, Field(ge=0)]
    color: Color


class AutoFit(_Model):
    enabled: bool = True
    minSize: Annotated[float, Field(gt=0)] = 24


class HeadlineLayer(_Model):
    kind: Literal["headline"]
    key: Key
    box: Box
    font: Key
    size: Annotated[float, Field(gt=0)]
    lineHeight: Annotated[float, Field(gt=0)]
    letterSpacing: float = 0
    align: Align = "left"
    casing: Literal["uppercase", "preserve"] = "preserve"
    vAlign: VAlign = "top"
    baseColor: Color
    highlightColor: Color
    defaultText: str = Field(min_length=1, max_length=600)
    defaultWordColors: list[WordColor] = Field(default_factory=list)
    shadow: Shadow | None = None
    autoFit: AutoFit = Field(default_factory=AutoFit)

    @model_validator(mode="after")
    def _words_exist(self) -> "HeadlineLayer":
        total = count_words(self.defaultText)
        bad = [entry.word for entry in self.defaultWordColors if entry.word >= total]
        if bad:
            raise ValueError(f"defaultWordColors names words {bad} but the headline has {total}")
        return self


class DateFormat(_Model):
    language: Literal["en", "bn"] = "en"
    monthStyle: Literal["short", "long"] = "short"
    order: Literal["day-first", "month-first"] = "day-first"


class CaptionLayer(_Model):
    """Small text: the source line, the dateline, the site, or fixed copy."""

    kind: Literal["caption"]
    key: Key
    role: Literal["source", "date", "site", "static"]
    text: str | None = Field(default=None, max_length=300, description="static captions only")
    box: Box
    font: Key
    size: Annotated[float, Field(gt=0)]
    color: Color
    align: Align = "left"
    letterSpacing: float = 0
    vAlign: VAlign = "top"
    dateFormat: DateFormat | None = None

    @model_validator(mode="after")
    def _static_has_text(self) -> "CaptionLayer":
        if self.role == "static" and not (self.text or "").strip():
            raise ValueError("a static caption needs text")
        if self.role != "static" and self.text is not None:
            raise ValueError("only static captions carry text; the author fills the rest")
        return self


Layer = Annotated[
    ImageLayer | FillLayer | GradientLayer | PhotoLayer | HeadlineLayer | CaptionLayer,
    Field(discriminator="kind"),
]


class Canvas(_Model):
    width: Annotated[int, Field(ge=1, le=MAX_CANVAS_SIDE)]
    height: Annotated[int, Field(ge=1, le=MAX_CANVAS_SIDE)]


class Source(_Model):
    fileName: str = Field(max_length=255)
    model: str = Field(max_length=80)
    generatedAt: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")


class PhotocardSpec(_Model):
    version: Literal[1]
    canvas: Canvas
    pixelDensity: Literal[1, 2] = 1
    background: Color = "#ffffff"
    fonts: list[SpecFont] = Field(default_factory=list)
    layers: Annotated[list[Layer], Field(min_length=2, max_length=200)]
    warnings: list[str] = Field(default_factory=list)
    missingFonts: list[str] = Field(default_factory=list)
    source: Source

    @model_validator(mode="after")
    def _well_formed(self) -> "PhotocardSpec":
        keys = [layer.key for layer in self.layers]
        duplicates = sorted({k for k in keys if keys.count(k) > 1})
        if duplicates:
            raise ValueError(f"duplicate layer keys: {duplicates}")
        photos = [layer for layer in self.layers if layer.kind == "photo"]
        headlines = [layer for layer in self.layers if layer.kind == "headline"]
        if len(photos) != 1:
            raise ValueError(f"a photocard needs exactly one photo layer, found {len(photos)}")
        if len(headlines) != 1:
            raise ValueError(f"a photocard needs exactly one headline layer, found {len(headlines)}")
        font_keys = {font.key for font in self.fonts}
        font_dupes = sorted({k for k in font_keys if [f.key for f in self.fonts].count(k) > 1})
        if font_dupes:
            raise ValueError(f"duplicate font keys: {font_dupes}")
        for layer in self.layers:
            if layer.kind in ("headline", "caption") and layer.font not in font_keys:
                raise ValueError(f"layer '{layer.key}' uses unknown font '{layer.font}'")
        key_set = set(keys)
        for key in photos[0].overlayKeys:
            if key not in key_set:
                raise ValueError(f"overlayKeys names unknown layer '{key}'")
        for layer in self.layers:
            if layer.kind == "caption" and layer.role == "date" and layer.dateFormat is None:
                layer.dateFormat = DateFormat()
        return self

    # -- convenience -------------------------------------------------------

    @property
    def photo(self) -> PhotoLayer:
        return next(layer for layer in self.layers if layer.kind == "photo")

    @property
    def headline(self) -> HeadlineLayer:
        return next(layer for layer in self.layers if layer.kind == "headline")

    def asset_files(self) -> list[str]:
        return [layer.asset for layer in self.layers if layer.kind == "image"]


def validate_spec(spec: PhotocardSpec, assets_dir: Path) -> list[str]:
    """Checks that need the file system. Returns problems; empty means fine."""
    problems = []
    for name in spec.asset_files():
        if not (assets_dir / name).is_file():
            problems.append(f"asset '{name}' is referenced but was not exported")
    return problems


# ---------------------------------------------------------------------------
# What Claude decides


class LayerRef(_Model):
    source: Literal["layer"]
    layerId: Annotated[int, Field(ge=1)]


class WindowRef(_Model):
    source: Literal["window"]
    layerId: Annotated[int, Field(ge=1)] = Field(description="the layer whose transparent window is the slot")


class NewHeadline(_Model):
    source: Literal["new"]
    box: Box
    align: Align = "center"
    casing: Literal["uppercase", "preserve"] = "preserve"
    reason: str = Field(max_length=300)


class NewPhoto(_Model):
    source: Literal["new"]
    box: Box
    radii: Radii | None = None
    reason: str = Field(max_length=300)


class CaptionDecision(_Model):
    layerId: Annotated[int, Field(ge=1)]
    role: Literal["source", "date", "site", "static"]


class DropDecision(_Model):
    layerId: Annotated[int, Field(ge=1)]
    reason: str = Field(max_length=200)


class GradientDecision(_Model):
    layerId: Annotated[int, Field(ge=1)]
    label: str = Field(min_length=1, max_length=40, description="panel section name, e.g. 'Bottom gradient'")
    editable: bool = True


class PhotocardDecisions(_Model):
    """Claude's reading of the design. Ids are the extractor's layer ids."""

    headline: Annotated[LayerRef | NewHeadline, Field(discriminator="source")]
    photo: Annotated[WindowRef | LayerRef | NewPhoto, Field(discriminator="source")]
    captions: list[CaptionDecision] = Field(default_factory=list)
    drop: list[DropDecision] = Field(default_factory=list)
    gradients: list[GradientDecision] = Field(default_factory=list)
    overlayAbovePhoto: list[Annotated[int, Field(ge=1)]] = Field(
        default_factory=list,
        description="frame layers a lifted subject is drawn in front of; never logos or text",
    )
    notes: list[str] = Field(default_factory=list)


def schema_json() -> str:
    import json

    return json.dumps(PhotocardSpec.model_json_schema(), indent=2, ensure_ascii=False) + "\n"


def slug_key(name: str, used: set[str], prefix: str = "l") -> str:
    """A spec key from a layer name: lower-case alphanumerics, unique."""
    base = re.sub(r"[^a-z0-9]", "", name.lower())[:24]
    if not base or not base[0].isalpha():
        base = prefix + base
    key = base[:32]
    n = 2
    while key in used:
        suffix = str(n)
        key = base[: 32 - len(suffix)] + suffix
        n += 1
    used.add(key)
    return key
