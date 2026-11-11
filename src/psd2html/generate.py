"""Ask Claude to write the HTML for an extracted PSD."""

from __future__ import annotations

import base64
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import anthropic

from .extract import Extraction

DEFAULT_MODEL = "claude-opus-5-5"
# Claude Opus 5.5 defaults to "medium"; matching a design closely is worth "high".
DEFAULT_EFFORT = "high"
MAX_TOKENS = 128_000  # thinking + a long page of HTML; needs streaming

# Models that accept server-side refusal fallbacks with fallbacks="default".
FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5"}
FALLBACK_BETA = "server-side-fallback-2026-07-01"

SYSTEM_PROMPT = """\
You turn Photoshop designs into hand-quality HTML and CSS.

You get two views of one PSD:
1. Images of the rendered design. Tall designs come as slices, top to bottom, \
each labeled with the vertical range of the original canvas it covers. An \
overview of the whole page may come first.
2. The layer data extracted from the PSD as JSON. Coordinates are original \
canvas pixels: "box" is [x, y, width, height]. Layers are listed bottom-most \
first (paint order). Text layers carry the exact strings and, per run, the \
PostScript font name, size in px, color, line height and letter spacing. \
Shape layers carry fill colors and corner radii. Layers with an "asset" have \
been exported as image files you can use. A "transparent_window" box marks a \
see-through hole in that image: anything placed behind the image shows \
through it (for example a photo inside a frame). The rendered images show \
such holes as white.

Treat the layer data as the source of truth for text, colors, sizes and \
positions. Use the images for layout intent and for anything the data does \
not cover (shadows, gradients, how things group visually).

First decide which kind of design this is:
- A web page (navigation, sections, content that flows): build a responsive \
page. Use semantic elements (header, nav, main, section, footer, h1-h6, p, a, \
button, ul/li). Lay out with normal flow, flexbox and grid inside a centered \
container; use absolute positioning only for decorative overlaps. Below \
768px, stack columns and let content wrap so the page still works on a phone.
- A fixed-format graphic (social media post, news card, banner, poster, \
thumbnail): keep the composition exactly. Build one element with the canvas \
aspect ratio, sized to the canvas width but never wider than the viewport, and \
make everything inside scale with it (position and size children in units \
relative to that element, such as percentages or container query units). It \
must look identical at any size, never reflow.
If the design is a template with an empty area meant to be filled (a photo \
frame, a placeholder box), make that area an element that is easy to fill \
later, with a clear class name and a comment.

Write one complete HTML document:
- Start with <!doctype html>. Put all CSS in one <style> element in <head>. \
No CSS frameworks. No JavaScript unless the design cannot work without it. \
Set the lang attribute to the language of the text.
- At a viewport as wide as the canvas, the result should look like the design: \
same spacing, sizes, colors, fonts and alignment.
- All text is real HTML text, copied exactly from the layer data. Never use an \
image of text when the text is in the layer data.
- Fonts: map each PostScript name to its family and weight. If the family is \
on Google Fonts, load all needed families with one <link>. Otherwise use the \
closest Google Fonts family that supports the text's script and has a similar \
style and weight, list the original name first in font-family, and say which \
substitute you chose in an HTML comment at the top.
- Images: reference exported files only as assets/<file>, using the exact \
file names listed. Use them for photos, illustrations, logos and complex \
icons. Draw plain rectangles, rounded rectangles, circles, lines and solid \
backgrounds with CSS instead. If something visible has no asset and CSS \
cannot reasonably draw it, leave an empty element of the right size and color \
with a comment saying what belongs there.
- Do not add content, sections or text that are not in the design.
- Use CSS custom properties for the main colors and fonts, and readable class \
names that describe the content (hero, feature-card, site-footer).

Reply with the HTML document only: no explanation and no Markdown fences.\
"""


class GenerationError(Exception):
    pass


@dataclass
class Generated:
    html: str
    model: str
    input_tokens: int
    output_tokens: int
    missing_assets: list[str]


def generate_html(
    extraction: Extraction,
    *,
    on_progress: Callable[[int], None] | None = None,
    client: anthropic.Anthropic | None = None,
) -> Generated:
    client = client or anthropic.Anthropic()
    model = os.environ.get("PSD2HTML_MODEL", DEFAULT_MODEL)
    effort = os.environ.get("PSD2HTML_EFFORT", DEFAULT_EFFORT)
    content = _build_content(extraction)

    extra: dict = {}
    if model in FALLBACK_MODELS:
        extra = {"betas": [FALLBACK_BETA], "fallbacks": "default"}

    written = 0
    try:
        with client.beta.messages.stream(
            model=model,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": content}],
            output_config={"effort": effort},
            **extra,
        ) as stream:
            for text in stream.text_stream:
                written += len(text)
                if on_progress:
                    on_progress(written)
            message = stream.get_final_message()
    except TypeError as exc:
        if "authentication" not in str(exc):
            raise
        raise GenerationError(
            "No Claude API key found. Put ANTHROPIC_API_KEY=... in the .env file and restart."
        ) from exc
    except anthropic.AuthenticationError as exc:
        raise GenerationError(
            "Claude rejected the API key. Set ANTHROPIC_API_KEY in .env and restart."
        ) from exc
    except anthropic.BadRequestError as exc:
        raise GenerationError(f"Claude rejected the request: {exc.message}") from exc
    except anthropic.RateLimitError as exc:
        raise GenerationError("Rate limited by the Claude API. Try again in a minute.") from exc
    except anthropic.APIStatusError as exc:
        raise GenerationError(f"Claude API error {exc.status_code}: {exc.message}") from exc
    except anthropic.APIConnectionError as exc:
        raise GenerationError("Could not reach the Claude API. Check the network.") from exc

    if message.stop_reason == "refusal":
        raise GenerationError("Claude declined to convert this design.")
    if message.stop_reason == "max_tokens":
        raise GenerationError(
            "The HTML was cut off because the design is too large for one pass. "
            "Try splitting the PSD into sections."
        )

    raw = "".join(block.text for block in message.content if block.type == "text")
    html = _clean_html(raw)
    if not html:
        raise GenerationError("Claude did not return an HTML document.")

    known = {asset["file"] for asset in extraction.assets}
    referenced = set(re.findall(r"assets/([\w.\-]+)", html))
    return Generated(
        html=html,
        model=message.model,
        input_tokens=message.usage.input_tokens,
        output_tokens=message.usage.output_tokens,
        missing_assets=sorted(referenced - known),
    )


def _build_content(extraction: Extraction) -> list[dict]:
    content = _build_image_content(extraction)
    content.append(
        {
            "type": "text",
            "text": (
                f"Canvas: {extraction.width} x {extraction.height} px.\n\n"
                f"Layer data:\n{extraction.layers_json()}\n\n"
                "Write the HTML document for this design."
            ),
        }
    )
    return content


def _build_image_content(extraction: Extraction) -> list[dict]:
    """The rendered design as image blocks: overview first, then the slices."""
    content: list[dict] = []
    total = len(extraction.slices)
    if extraction.overview is not None:
        content.append({"type": "text", "text": "Overview of the whole design (scaled down):"})
        content.append(_image_block(extraction.overview))
    for index, piece in enumerate(extraction.slices, start=1):
        label = (
            f"Design slice {index} of {total}: canvas y = {piece.top} to {piece.bottom} px."
            if total > 1
            else f"The design ({extraction.width} x {extraction.height} px)."
        )
        if extraction.slice_scale < 1:
            label += f" Shown at {extraction.slice_scale:.0%} of original size."
        content.append({"type": "text", "text": label})
        content.append(_image_block(piece.path))
    return content


def _image_block(path: Path) -> dict:
    data = base64.standard_b64encode(path.read_bytes()).decode("ascii")
    media_type = "image/jpeg" if path.suffix == ".jpg" else "image/png"
    return {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}}


def _clean_html(raw: str) -> str:
    text = raw.strip()
    fenced = re.search(r"```(?:html)?\s*\n(.*?)```", text, re.DOTALL | re.IGNORECASE)
    if fenced:
        text = fenced.group(1).strip()
    start = re.search(r"<!doctype html|<html", text, re.IGNORECASE)
    if not start:
        return ""
    text = text[start.start():]
    end = text.lower().rfind("</html>")
    if end != -1:
        text = text[: end + len("</html>")]
    return text + "\n"
