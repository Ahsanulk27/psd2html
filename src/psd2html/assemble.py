"""Build the PhotocardSpec from the extractor's data and Claude's decisions.

Everything numeric comes from the PSD: boxes, colours, sizes, gradient
stops, fonts. Claude only said which layer plays which role (decide.py).
This module turns that into the layer list Palette draws, in paint order.
"""

from __future__ import annotations

import math
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from .candidates import Flat, flatten
from .extract import Extraction
from .headline import date_format, headline_colors, script_of
from .spec import (
    AutoFit,
    Box,
    CaptionLayer,
    Canvas,
    Clip,
    DateFormat,
    EllipseShape,
    FillLayer,
    GradientLayer,
    GradientStop,
    HeadlineLayer,
    ImageLayer,
    PhotoLayer,
    PhotocardDecisions,
    PhotocardSpec,
    RectShape,
    RoundedRectShape,
    Shadow,
    Source,
    SpecFont,
    WordColor,
    slug_key,
)

# Families Palette ships or knows how to substitute. A PSD font outside this
# list is reported as missing so the organization can upload it. Palette does
# the real matching at render time against the organization's own fonts.
KNOWN_FAMILIES = {
    "montserrat", "poppins", "arimo", "tinos", "librecaslontext", "helveticanowdisplay",
    "helveticanowtext", "helveticaneue", "coplette", "provicali", "satoshi", "inter",
    "liadornoirrit", "lialinurtatsama", "ekusheybanglakolom", "bornomala", "notosansbengali",
}

WEIGHTS = [
    ("thin", 100), ("hairline", 100), ("extralight", 200), ("ultralight", 200), ("light", 300),
    ("regular", 400), ("book", 400), ("roman", 400), ("normal", 400), ("medium", 500),
    ("semibold", 600), ("demibold", 600), ("demi", 600), ("bold", 700), ("extrabold", 800),
    ("ultrabold", 800), ("heavy", 800), ("black", 900), ("ultra", 900),
]

PLACEHOLDER_HEADLINE = {"latin": "Your headline goes here", "bangla": "আপনার শিরোনাম এখানে লিখুন", "mixed": "Your headline goes here"}
DEFAULT_HIGHLIGHT = "#ffe107"
PIXEL_DENSITY_MAX_SIDE = 1200


class AssemblyError(Exception):
    pass


def assemble(
    extraction: Extraction,
    decisions: PhotocardDecisions,
    *,
    file_name: str,
    model: str,
    output_dir: Path,
) -> PhotocardSpec:
    flats = flatten(extraction)
    by_id = {flat.node["id"]: flat for flat in flats}
    width, height = extraction.width, extraction.height
    warnings = list(extraction.warnings)
    notes = list(decisions.notes)

    _check(decisions, by_id)

    dropped = {d.layerId for d in decisions.drop}
    caption_roles = {c.layerId: c.role for c in decisions.captions}
    gradient_choices = {g.layerId: g for g in decisions.gradients}
    overlay_ids = set(decisions.overlayAbovePhoto)

    headline_id = decisions.headline.layerId if decisions.headline.source == "layer" else None
    photo_source = decisions.photo
    photo_layer_id = photo_source.layerId if photo_source.source in ("layer", "window") else None

    # -- background ---------------------------------------------------------
    background = "#ffffff"
    background_id = None
    for flat in flats:
        node = flat.node
        if node.get("fill") and _covers_canvas(node.get("box"), width, height) and node["kind"] in ("solidcolorfill", "pixel", "shape"):
            if "asset" not in node:
                background, background_id = node["fill"], node["id"]
        break  # only the bottom-most layer can be the page colour

    # -- fonts --------------------------------------------------------------
    fonts: dict[str, SpecFont] = {}
    used_keys: set[str] = set()

    def font_key(postscript: str | None, script: str) -> str:
        name = postscript or ("LiAdorNoirrit-Bold" if script == "bangla" else "Inter-Bold")
        if name not in fonts:
            family, weight, style = _parse_postscript(name)
            fonts[name] = SpecFont(
                key=slug_key("f" + re.sub(r"[^a-z0-9]", "", name.lower())[:10], used_keys, "f"),
                postscript=name, family=family, weight=weight, style=style, script=script,  # type: ignore[arg-type]
            )
        return fonts[name].key

    # -- photo slot geometry --------------------------------------------------
    if photo_source.source == "window":
        window_node = by_id[photo_source.layerId].node
        window = window_node.get("transparent_window")
        if not window:
            raise AssemblyError(f"layer {photo_source.layerId} has no transparent window")
        photo_box = Box(x=window[0], y=window[1], width=window[2], height=window[3])
        radius = window_node.get("transparent_window_radius") or 0
        photo_radii = [float(radius)] * 4 if radius else None
    elif photo_source.source == "layer":
        node = by_id[photo_source.layerId].node
        box = node.get("box")
        if not box:
            raise AssemblyError(f"layer {photo_source.layerId} has no box to use as the photo slot")
        photo_box = Box(x=box[0], y=box[1], width=box[2], height=box[3])
        shape = node.get("shape") or {}
        photo_radii = [float(r) for r in shape["radii_px"]] if shape.get("type") == "rounded_rect" and shape.get("radii_px") else None
        if photo_radii and max(photo_radii) < 1:
            photo_radii = None
    else:
        photo_box = photo_source.box
        photo_radii = photo_source.radii
        notes.append(f"Photo slot placed by the model: {photo_source.reason}")

    photo_key = slug_key("photo", used_keys)
    light_background = _luminance(background) > 0.6
    photo = PhotoLayer(
        kind="photo", key=photo_key, box=photo_box, radii=photo_radii, defaultFit="cover",
        emptyFill="#e5e7eb" if light_background else "#2b2f36",
        placeholderInk="dark" if light_background else "light",
        overlayKeys=[],
    )

    # -- walk the layers ----------------------------------------------------
    layers: list = []
    photo_placed = False
    key_by_id: dict[int, str] = {}
    slot_area = photo_box.width * photo_box.height

    def place_photo() -> None:
        nonlocal photo_placed
        if not photo_placed:
            layers.append(photo)
            photo_placed = True

    for flat in flats:
        node = flat.node
        lid = node["id"]
        kind = node["kind"]
        box = node.get("box")

        if lid == background_id:
            if photo_source.source == "new":
                place_photo()
            continue
        if photo_source.source == "new" and not photo_placed:
            place_photo()

        if lid in dropped:
            continue

        if photo_source.source == "window" and lid == photo_layer_id:
            place_photo()  # the photo sits behind the layer with the hole
        if photo_source.source == "layer" and lid == photo_layer_id:
            place_photo()
            continue

        # Sample content under the slot: a raster that fills the hole.
        # Sample content under the slot: a raster that sits inside the hole and
        # fills most of it. A frame around the slot is larger than the slot and
        # is kept.
        if kind in ("pixel", "smartobject") and node.get("asset") and box and not photo_placed:
            area = box[2] * box[3]
            if _inside(box, photo_box) >= 0.95 and 0.5 * slot_area <= area <= 1.02 * slot_area:
                notes.append(f"Dropped '{node['name']}': it fills the photo slot, so it was the sample photo.")
                continue

        if lid == headline_id:
            layers.append(_headline_from_layer(node, font_key, used_keys, warnings))
            key_by_id[lid] = layers[-1].key
            continue

        if kind == "type":
            role = caption_roles.get(lid, "static")
            caption = _caption_from_layer(node, role, font_key, used_keys)
            if caption is not None:
                layers.append(caption)
                key_by_id[lid] = caption.key
            continue

        if not box:
            continue
        key = slug_key(node["name"], used_keys)
        key_by_id[lid] = key
        opacity = min(1.0, max(0.0, flat.opacity))
        blend = node.get("blend") if node.get("blend") in ("multiply", "screen", "overlay") else "normal"
        if node.get("blend") and blend == "normal" and node["blend"] != "normal":
            warnings.append(f"Layer '{node['name']}' uses the {node['blend']} blend mode, which is drawn as normal.")
        clip = _clip_for(node, by_id, photo_box, photo_radii, photo_layer_id)

        if node.get("gradient"):
            choice = gradient_choices.get(lid)
            label = choice.label if choice else _gradient_label(node, layers)
            layers.append(
                GradientLayer(
                    kind="gradient", key=key, label=label, box=_box(box),
                    angle=node["gradient"]["angle"],
                    stops=_fold_stops(node["gradient"], box),
                    clip=clip, editable=choice.editable if choice else True,
                    opacity=opacity, blend=blend,
                )
            )
            continue

        if node.get("asset"):
            layers.append(
                ImageLayer(kind="image", key=key, asset=node["asset"], box=_box(box), opacity=opacity, blend=blend, clip=clip)
            )
            continue

        if node.get("fill"):
            shape = node.get("shape") or {"type": "rect"}
            if shape.get("type") == "rounded_rect" and shape.get("radii_px"):
                spec_shape = RoundedRectShape(type="rounded_rect", radii=[float(r) for r in shape["radii_px"]])
            elif shape.get("type") == "ellipse":
                spec_shape = EllipseShape(type="ellipse")
            elif shape.get("type") == "line":
                continue  # thin lines come through as fills of their bbox
            else:
                spec_shape = RectShape(type="rect")
            layers.append(FillLayer(kind="fill", key=key, box=_box(box), color=node["fill"], shape=spec_shape, opacity=opacity))
            continue
        # A shape with no fill and no asset (an unfilled path): nothing to draw.

    if not photo_placed:
        layers.insert(1 if background_id is not None else 0, photo)

    if headline_id is None:
        layers.append(_new_headline(decisions, extraction, photo, background, font_key, used_keys))
        notes.append(f"Headline placed by the model: {decisions.headline.reason}")  # type: ignore[union-attr]

    # Layers a lifted subject is drawn in front of: the shades and frames
    # between the photo and the first text. Text, and anything stacked above
    # it (logos over the headline), always stays on top of the subject.
    photo_index = layers.index(photo)
    above = layers[photo_index + 1 :]
    first_text = next((i for i, layer in enumerate(above) if layer.kind in ("headline", "caption")), len(above))
    above = above[:first_text]
    overlay_keys = []
    for layer in above:
        lid = next((i for i, k in key_by_id.items() if k == layer.key), None)
        if layer.kind == "gradient" or (lid is not None and (lid in overlay_ids or lid == photo_layer_id)):
            overlay_keys.append(layer.key)
    photo.overlayKeys = overlay_keys

    pixel_density = 2 if max(width, height) <= PIXEL_DENSITY_MAX_SIDE else 1
    missing = sorted(
        font.postscript for font in fonts.values() if _normalize_family(font.family) not in KNOWN_FAMILIES
    )
    spec = PhotocardSpec(
        version=1,
        canvas=Canvas(width=width, height=height),
        pixelDensity=pixel_density,
        background=background,
        fonts=list(fonts.values()),
        layers=layers,
        warnings=[*warnings, *notes],
        missingFonts=missing,
        source=Source(fileName=file_name[:255], model=model[:80], generatedAt=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")),
    )
    _write(spec, output_dir, extraction)
    return spec


# ---------------------------------------------------------------------------
# Pieces


def _check(decisions: PhotocardDecisions, by_id: dict[int, Flat]) -> None:
    """Semantic checks on Claude's answer; raises AssemblyError with every problem."""
    problems = []
    ids = set(by_id)

    def must_exist(lid: int, what: str) -> None:
        if lid not in ids:
            problems.append(f"{what} names layer {lid}, which does not exist")

    if decisions.headline.source == "layer":
        must_exist(decisions.headline.layerId, "headline")
        if decisions.headline.layerId in ids and by_id[decisions.headline.layerId].node["kind"] != "type":
            problems.append(f"headline layer {decisions.headline.layerId} is not a text layer")
    if decisions.photo.source in ("layer", "window"):
        must_exist(decisions.photo.layerId, "photo")
        if decisions.photo.layerId in ids:
            node = by_id[decisions.photo.layerId].node
            if decisions.photo.source == "window" and not node.get("transparent_window"):
                problems.append(f"photo layer {decisions.photo.layerId} has no transparent window")
            if decisions.photo.source == "layer" and node["kind"] == "type":
                problems.append(f"photo layer {decisions.photo.layerId} is a text layer")
    for caption in decisions.captions:
        must_exist(caption.layerId, "captions")
        if caption.layerId in ids and by_id[caption.layerId].node["kind"] != "type":
            problems.append(f"caption layer {caption.layerId} is not a text layer")
        if decisions.headline.source == "layer" and caption.layerId == decisions.headline.layerId:
            problems.append("the headline layer is also listed as a caption")
    for drop in decisions.drop:
        must_exist(drop.layerId, "drop")
        if decisions.headline.source == "layer" and drop.layerId == decisions.headline.layerId:
            problems.append("the headline layer is also listed in drop")
        if decisions.photo.source in ("layer", "window") and drop.layerId == decisions.photo.layerId:
            problems.append("the photo layer is also listed in drop")
    for gradient in decisions.gradients:
        must_exist(gradient.layerId, "gradients")
    for lid in decisions.overlayAbovePhoto:
        must_exist(lid, "overlayAbovePhoto")
    if problems:
        raise AssemblyError("; ".join(problems))


def _headline_from_layer(node: dict, font_key, used_keys: set[str], warnings: list[str]) -> HeadlineLayer:
    paragraphs = node.get("paragraphs", [])
    runs = [run for p in paragraphs for run in p.get("runs", [])]
    first = runs[0] if runs else {}
    colors = headline_colors(paragraphs, first.get("color") or "#ffffff")
    text = colors.text.strip() or (node.get("text") or "Headline").strip()
    size = float(first.get("size_px") or 48)
    box = node["box"]
    script = node.get("script") or script_of(text)
    shadow = _shadow(node.get("shadow"))
    return HeadlineLayer(
        kind="headline", key=slug_key("headline", used_keys), box=_box(box),
        font=font_key(first.get("font"), script), size=size,
        lineHeight=float(first.get("line_height_px") or size * 1.15),
        letterSpacing=float(first.get("letter_spacing_em") or 0),
        align=_align(paragraphs), casing="uppercase" if first.get("caps") == "all_caps" else "preserve",
        vAlign="top", baseColor=colors.base_color, highlightColor=colors.highlight_color,
        defaultText=text[:600],
        defaultWordColors=[WordColor(word=i, color=c) for i, c in colors.word_colors],
        shadow=shadow, autoFit=AutoFit(enabled=True, minSize=max(24.0, round(size * 0.5, 1))),
    )


def _caption_from_layer(node: dict, role: str, font_key, used_keys: set[str]) -> CaptionLayer | None:
    paragraphs = node.get("paragraphs", [])
    runs = [run for p in paragraphs for run in p.get("runs", [])]
    first = runs[0] if runs else {}
    text = headline_colors(paragraphs).text.strip() or (node.get("text") or "").strip()
    if not text and role == "static":
        return None
    box = node.get("box")
    if not box:
        return None
    script = node.get("script") or script_of(text)
    size = float(first.get("size_px") or 24)
    return CaptionLayer(
        kind="caption", key=slug_key(role if role != "static" else node["name"], used_keys),
        role=role, text=text[:300] if role == "static" else None, box=_box(box),  # type: ignore[arg-type]
        font=font_key(first.get("font"), script), size=size,
        color=_hex_only(first.get("color")) or "#000000",
        align=_align(paragraphs), letterSpacing=float(first.get("letter_spacing_em") or 0), vAlign="top",
        dateFormat=_date_format(text) if role == "date" else None,
    )


def _new_headline(decisions, extraction: Extraction, photo: PhotoLayer, background: str, font_key, used_keys: set[str]) -> HeadlineLayer:
    choice = decisions.headline
    over_photo = _inside([choice.box.x, choice.box.y, choice.box.width, choice.box.height], photo.box) > 0.5
    light = _luminance(background) > 0.6 and not over_photo
    size = round(extraction.width * 0.06)
    return HeadlineLayer(
        kind="headline", key=slug_key("headline", used_keys), box=choice.box,
        font=font_key(None, "latin"), size=size, lineHeight=round(size * 1.1),
        align=choice.align, casing=choice.casing, vAlign="bottom" if over_photo else "top",
        baseColor="#111111" if light else "#ffffff", highlightColor=DEFAULT_HIGHLIGHT,
        defaultText=PLACEHOLDER_HEADLINE["latin"],
        shadow=Shadow(color="#000000", alpha=0.45, offsetX=0, offsetY=2, blur=12) if over_photo else None,
        autoFit=AutoFit(enabled=True, minSize=max(24.0, size * 0.5)),
    )


def _clip_for(node: dict, by_id: dict[int, Flat], photo_box: Box, photo_radii, photo_layer_id) -> Clip | None:
    base_id = node.get("clip_to")
    if not base_id:
        return None
    if base_id == photo_layer_id:
        return Clip(box=photo_box, radii=photo_radii)
    base = by_id.get(base_id)
    if base is None or not base.node.get("box"):
        return None
    shape = base.node.get("shape") or {}
    radii = [float(r) for r in shape["radii_px"]] if shape.get("type") == "rounded_rect" and shape.get("radii_px") else None
    window = base.node.get("transparent_window")
    if window:
        return Clip(box=Box(x=window[0], y=window[1], width=window[2], height=window[3]))
    return Clip(box=_box(base.node["box"]), radii=radii)


def _fold_stops(gradient: dict, box: list[int]) -> list[GradientStop]:
    """Fold Photoshop's scale and offset into the stop positions.

    The gradient line runs through the box centre at the CSS angle with
    length L = |w sin a| + |h cos a|. Photoshop draws the stops over `scale`
    of that line, centred, then moves the centre by the offset.
    """
    angle = math.radians(gradient["angle"])
    w, h = box[2], box[3]
    length = abs(w * math.sin(angle)) + abs(h * math.cos(angle)) or 1
    direction = (math.sin(angle), -math.cos(angle))  # screen coordinates, y down
    shift = gradient.get("shift", [0, 0])
    along = (shift[0] * w * direction[0] + shift[1] * h * direction[1]) / length
    scale = gradient.get("scale", 1) or 1
    raw = [(0.5 + (s["offset"] - 0.5) * scale + along, s["color"], s["alpha"]) for s in gradient["stops"]]
    raw.sort(key=lambda s: s[0])
    return [GradientStop(offset=t, color=c, alpha=a) for t, c, a in _clamp_stops(raw)]


def _clamp_stops(stops: list[tuple[float, str, float]]) -> list[tuple[float, str, float]]:
    """Stops outside 0..1 are replaced by the gradient's value at the edge."""

    def at(t: float) -> tuple[str, float]:
        if t <= stops[0][0]:
            return stops[0][1], stops[0][2]
        if t >= stops[-1][0]:
            return stops[-1][1], stops[-1][2]
        for (t0, c0, a0), (t1, c1, a1) in zip(stops, stops[1:]):
            if t0 <= t <= t1:
                f = 0 if t1 == t0 else (t - t0) / (t1 - t0)
                return _mix(c0, c1, f), a0 + (a1 - a0) * f
        return stops[-1][1], stops[-1][2]

    inside = [(round(t, 4), c, round(a, 3)) for t, c, a in stops if 0 < t < 1]
    start, end = at(0.0), at(1.0)
    out = [(0.0, start[0], round(start[1], 3)), *inside, (1.0, end[0], round(end[1], 3))]
    # Drop duplicates at the same offset, keeping the first.
    seen: set[float] = set()
    result = []
    for stop in out:
        if stop[0] in seen:
            continue
        seen.add(stop[0])
        result.append(stop)
    return result


def _mix(a: str, b: str, f: float) -> str:
    ra, ga, ba = (int(a[i : i + 2], 16) for i in (1, 3, 5))
    rb, gb, bb = (int(b[i : i + 2], 16) for i in (1, 3, 5))
    return "#{:02x}{:02x}{:02x}".format(*(round(x + (y - x) * f) for x, y in ((ra, rb), (ga, gb), (ba, bb))))


def _shadow(data: dict | None) -> Shadow | None:
    if not data:
        return None
    angle = math.radians(float(data.get("angle", 120)))
    distance = float(data.get("distance", 0))
    return Shadow(
        color=data.get("color", "#000000"), alpha=float(data.get("alpha", 0.75)),
        offsetX=round(-math.cos(angle) * distance, 2), offsetY=round(math.sin(angle) * distance, 2),
        blur=float(data.get("blur", 0)),
    )


def _date_format(text: str) -> DateFormat:
    guess = date_format(text)
    return DateFormat(language=guess.language, monthStyle=guess.month_style, order=guess.order)  # type: ignore[arg-type]


def _gradient_label(node: dict, layers: list) -> str:
    count = sum(1 for layer in layers if layer.kind == "gradient") + 1
    box = node.get("box") or [0, 0, 0, 0]
    stops = node["gradient"]["stops"]
    if stops[0]["alpha"] > stops[-1]["alpha"]:
        where = "Bottom" if 315 <= node["gradient"]["angle"] or node["gradient"]["angle"] <= 45 else "Top"
    else:
        where = "Top" if 315 <= node["gradient"]["angle"] or node["gradient"]["angle"] <= 45 else "Bottom"
    label = f"{where} gradient"
    return label if count == 1 else f"{label} {count}"


def _align(paragraphs: list[dict]) -> str:
    align = (paragraphs[0].get("align") if paragraphs else None) or "left"
    if align.startswith("center"):
        return "center"
    if align.startswith("right"):
        return "right"
    return "left"


def _parse_postscript(name: str) -> tuple[str, int, str]:
    family_part, _, style_part = name.partition("-")
    style_lower = style_part.lower()
    weight = 400
    for token, value in sorted(WEIGHTS, key=lambda item: -len(item[0])):
        if token in style_lower:
            weight = value
            break
    if not style_part:
        # Weight sometimes rides on the family: "LiAdorNoirritSemiBold".
        lowered = family_part.lower()
        for token, value in sorted(WEIGHTS, key=lambda item: -len(item[0])):
            if lowered.endswith(token):
                weight = value
                family_part = family_part[: -len(token)]
                break
    style = "italic" if "italic" in style_lower or "oblique" in style_lower else "normal"
    family = re.sub(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Za-z])(?=\d)", " ", family_part).strip() or name
    return family, weight, style


def _normalize_family(family: str) -> str:
    return re.sub(r"[^a-z0-9]", "", family.lower())


def _hex_only(color: str | None) -> str | None:
    if color and re.fullmatch(r"#[0-9a-f]{6}", color):
        return color
    match = color and re.fullmatch(r"rgba?\((\d+),\s*(\d+),\s*(\d+)(?:,\s*[\d.]+)?\)", color)
    if match:
        return "#{:02x}{:02x}{:02x}".format(*(min(255, int(v)) for v in match.groups()))
    return None


def _box(box: list) -> Box:
    return Box(x=box[0], y=box[1], width=max(1, box[2]), height=max(1, box[3]))


def _covers_canvas(box, width: int, height: int) -> bool:
    return bool(box) and box[0] <= 0 and box[1] <= 0 and box[0] + box[2] >= width and box[1] + box[3] >= height


def _inside(box: list, frame: Box) -> float:
    x1, y1 = max(box[0], frame.x), max(box[1], frame.y)
    x2 = min(box[0] + box[2], frame.x + frame.width)
    y2 = min(box[1] + box[3], frame.y + frame.height)
    if x2 <= x1 or y2 <= y1:
        return 0.0
    return (x2 - x1) * (y2 - y1) / (box[2] * box[3])


def _luminance(color: str) -> float:
    r, g, b = (int(color[i : i + 2], 16) / 255 for i in (1, 3, 5))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _write(spec: PhotocardSpec, output_dir: Path, extraction: Extraction) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "photocard.json").write_text(spec.model_dump_json(indent=2), encoding="utf-8")
    shutil.copyfile(extraction.design_png, output_dir / "preview.png")
    keep = set(spec.asset_files())
    assets_dir = output_dir / "assets"
    if assets_dir.is_dir():
        for path in assets_dir.iterdir():
            if path.name not in keep:
                path.unlink()
