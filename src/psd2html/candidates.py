"""Flatten an extraction into the list of layers Claude chooses roles for.

Claude never writes coordinates, colours or font names. It sees each layer
with what the extractor knows about it and says which one is the headline,
where the photo goes, which small texts are captions, and what is sample
content to drop. `candidates()` is that list, in paint order, bottom first.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .extract import Extraction
from .headline import count_words, headline_colors


@dataclass
class Flat:
    """One leaf layer with its group ancestry folded in."""

    node: dict
    opacity: float = 1.0  # own opacity times every ancestor's
    group_path: list[str] = field(default_factory=list)


def flatten(extraction: Extraction) -> list[Flat]:
    """Leaves in paint order, groups dissolved, opacities multiplied."""
    out: list[Flat] = []

    def visit(nodes: list[dict], opacity: float, path: list[str]) -> None:
        for node in nodes:
            own = float(node.get("opacity", 1.0))
            if node.get("kind") == "group":
                visit(node.get("children", []), opacity * own, [*path, node["name"]])
            else:
                out.append(Flat(node, opacity * own, path))

    visit(extraction.layers, 1.0, [])
    return out


def candidates(extraction: Extraction, flats: list[Flat]) -> list[dict]:
    """What Claude is shown about each layer."""
    width, height = extraction.width, extraction.height
    windows = [f for f in flats if f.node.get("transparent_window")]
    items = []
    for flat in flats:
        node = flat.node
        item: dict = {
            "id": node["id"],
            "kind": node["kind"],
            "name": node["name"],
        }
        if flat.group_path:
            item["group"] = " / ".join(flat.group_path)
        box = node.get("box")
        if box:
            item["box"] = box
            item["coverage"] = round(box[2] * box[3] / (width * height), 3)
        if flat.opacity < 0.999:
            item["opacity"] = round(flat.opacity, 2)
        if node.get("blend"):
            item["blend"] = node["blend"]
        if node.get("clip_to"):
            item["clippedTo"] = node["clip_to"]
        if node.get("asset"):
            item["asset"] = node["asset"]
        if node.get("fill"):
            item["fill"] = node["fill"]
        if node.get("shape"):
            item["shape"] = node["shape"]
        if node.get("gradient"):
            item["gradient"] = {
                "angleCss": node["gradient"]["angle"],
                "stops": node["gradient"]["stops"],
            }
        if node.get("transparent_window"):
            item["transparentWindow"] = node["transparent_window"]
            item["hint"] = "a see-through hole: the photo most likely goes behind it"
        if node["kind"] == "type":
            paragraphs = node.get("paragraphs", [])
            colors = headline_colors(paragraphs)
            runs = [run for p in paragraphs for run in p.get("runs", [])]
            item["text"] = colors.text or node.get("text", "")
            item["script"] = node.get("script", "latin")
            if runs:
                item["font"] = runs[0].get("font")
                item["sizePx"] = runs[0].get("size_px")
            item["colors"] = sorted({run.get("color", "") for run in runs if run.get("color")})
            item["words"] = count_words(colors.text)
            if paragraphs and paragraphs[0].get("align"):
                item["align"] = paragraphs[0]["align"]
        hints = []
        if node["kind"] in ("pixel", "smartobject") and node.get("asset") and box:
            for window in windows:
                if window.node["id"] > node["id"] and _inside(box, window.node["transparent_window"]) >= 0.8:
                    hints.append("sits under a window and fills it: probably the sample photo")
                    item["looksLikeSamplePhoto"] = True
                    break
            if box[2] * box[3] >= 0.25 * width * height and "looksLikeSamplePhoto" not in item:
                hints.append("a large raster: a photo, a background, or a frame")
        if hints:
            item["hint"] = "; ".join([item["hint"], *hints]) if "hint" in item else "; ".join(hints)
        items.append(item)
    return items


def _inside(box: list[int], frame: list[int]) -> float:
    """Share of `box` that lies inside `frame`."""
    x1, y1 = max(box[0], frame[0]), max(box[1], frame[1])
    x2 = min(box[0] + box[2], frame[0] + frame[2])
    y2 = min(box[1] + box[3], frame[1] + frame[3])
    if x2 <= x1 or y2 <= y1:
        return 0.0
    return (x2 - x1) * (y2 - y1) / (box[2] * box[3])
