"""Ask Claude which extracted layer plays which role in the photocard."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass

import anthropic
from pydantic import ValidationError

from .extract import Extraction
from .generate import (
    DEFAULT_MODEL,
    FALLBACK_BETA,
    FALLBACK_MODELS,
    GenerationError,
    _build_image_content,
)
from .spec import PhotocardDecisions

MAX_TOKENS = 16_000

# Choosing roles is a short answer over a small layer list. Measured on the
# three benchmark PSDs (2026-11-11): Opus 5.5 at medium and Sonnet 5.5 at low
# both got every role right, at about $0.05 and $0.02 a file; Haiku 5.5 put
# Cablgram's photo slot in the wrong place. Effort barely moves the cost here,
# since almost all of it is the input images.
PHOTOCARD_DEFAULT_EFFORT = "medium"

SYSTEM_PROMPT = """\
You read Photoshop designs for news photocards and say which layer plays \
which role. You do not write coordinates, colours or font names: the \
extractor already has every number. You only assign roles.

A photocard in Palette has:
- exactly one PHOTO SLOT: the area the author's photo fills. In the PSD it is \
usually a see-through hole in a frame raster ("transparentWindow"), a shape \
or raster placed as a picture box, or a sample photograph pasted in. The \
sample photograph itself is never kept: it is dropped and its area becomes \
the slot (unless a window already marks the slot, in which case the sample \
photo under the window is simply dropped).
- exactly one HEADLINE: the main text the author rewrites for every post, \
usually the largest text. Its words can be painted in two colours.
- optional CAPTIONS: small texts the author also changes per post, with a \
role: "source" (credit lines like "Via: Reuters | Picture: Collected"), \
"date" (a dateline), "site" (a web address or handle). Any other text that \
belongs to the design and never changes (a brand word, a category tag that \
is part of the chrome) is "static".
- GRADIENTS: gradient layers over the photo or page that the author may \
recolour. Give each a short label such as "Bottom gradient" and say whether \
it should be editable (a shade over the photo: yes; a tiny decorative \
sheen: no).
- everything else is chrome and stays exactly as drawn: logos, frames, \
bars, backgrounds. Nothing else needs a decision.

Decide, in `PhotocardDecisions`:
- `headline`: the text layer id, or `new` with a box when the design has no \
headline text at all (place it over the lower third of the photo slot if \
the slot is large, otherwise in the largest empty area, and say why).
- `photo`: `window` with the id of the layer that has the transparentWindow, \
or `layer` with a raster/shape whose box is the picture area, or `new` with \
a box when nothing in the PSD marks it (say why).
- `captions`: every per-post text other than the headline, with its role.
- `drop`: sample content that must not survive: the sample photo, guide \
layers, lorem text, leftovers hidden behind other layers. Not the headline, \
not the photo marker, not chrome.
- `gradients`: every gradient layer worth a label.
- `overlayAbovePhoto`: ids of frame layers that a cut-out subject should be \
drawn IN FRONT of when the author lifts it out of the photo slot: the frame \
or rim with the window, a border around the picture. The subject then \
pops out over the frame. Never list logos, mastheads, bars, badges or any \
text: those always stay on top of the subject. Gradients are handled \
automatically. Usually this is the one frame layer, or nothing.
- `notes`: anything the author should know, in one short sentence each.

You get images of the rendered design (tall designs come as slices with \
their canvas y-range) and the candidate layers as JSON, bottom-most first, \
each with its id, kind, name, box [x, y, width, height], coverage of the \
canvas, text and font where it has them, and hints from the extractor.
"""


@dataclass
class Decided:
    decisions: PhotocardDecisions
    model: str
    input_tokens: int
    output_tokens: int


def decide_photocard(
    extraction: Extraction,
    candidates: list[dict],
    *,
    on_progress: Callable[[int], None] | None = None,
    client: anthropic.Anthropic | None = None,
) -> Decided:
    client = client or anthropic.Anthropic()
    model = os.environ.get("PSD2HTML_PHOTOCARD_MODEL") or os.environ.get("PSD2HTML_MODEL") or DEFAULT_MODEL
    effort = os.environ.get("PSD2HTML_PHOTOCARD_EFFORT") or PHOTOCARD_DEFAULT_EFFORT
    extra: dict = {}
    if model in FALLBACK_MODELS:
        extra = {"betas": [FALLBACK_BETA], "fallbacks": "default"}

    content = _build_image_content(extraction)
    content.append(
        {
            "type": "text",
            "text": (
                f"Canvas: {extraction.width} x {extraction.height} px.\n\n"
                f"Candidate layers:\n{json.dumps(candidates, ensure_ascii=False, separators=(',', ':'))}\n\n"
                "Assign the roles."
            ),
        }
    )
    messages: list[dict] = [{"role": "user", "content": content}]

    total_in = total_out = 0
    last_errors = ""
    for attempt in range(2):
        message = _call(client, model, effort, messages, extra, on_progress)
        total_in += message.usage.input_tokens
        total_out += message.usage.output_tokens
        parsed = getattr(message, "parsed_output", None)
        raw_text = "".join(block.text for block in message.content if getattr(block, "type", "") == "text")
        try:
            decisions = parsed if isinstance(parsed, PhotocardDecisions) else PhotocardDecisions.model_validate_json(raw_text)
        except (ValidationError, ValueError) as exc:
            last_errors = str(exc)
            decisions = None
        if decisions is not None:
            # Semantic checks live in assemble._check; run them here so the
            # retry carries the reasons back to the model.
            from .assemble import AssemblyError, _check
            from .candidates import flatten

            try:
                _check(decisions, {flat.node["id"]: flat for flat in flatten(extraction)})
            except AssemblyError as exc:
                last_errors = str(exc)
                decisions = None
        if decisions is not None:
            return Decided(decisions, message.model, total_in, total_out)
        if attempt == 0:
            messages.append({"role": "assistant", "content": raw_text or "{}"})
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "That answer has problems: " + last_errors + "\n\n"
                        "Reply again with a corrected PhotocardDecisions. Only use layer ids from the candidate list."
                    ),
                }
            )
    raise GenerationError(f"Could not read this design as a photocard: {last_errors[:400]}")


def _call(client, model, effort, messages, extra, on_progress):
    written = 0
    try:
        with client.beta.messages.stream(
            model=model,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            messages=messages,
            output_format=PhotocardDecisions,
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
        raise GenerationError("No Claude API key found. Put ANTHROPIC_API_KEY=... in the .env file and restart.") from exc
    except anthropic.AuthenticationError as exc:
        raise GenerationError("Claude rejected the API key. Set ANTHROPIC_API_KEY in .env and restart.") from exc
    except anthropic.BadRequestError as exc:
        raise GenerationError(f"Claude rejected the request: {exc.message}") from exc
    except anthropic.RateLimitError as exc:
        raise GenerationError("Rate limited by the Claude API. Try again in a minute.") from exc
    except anthropic.APIStatusError as exc:
        raise GenerationError(f"Claude API error {exc.status_code}: {exc.message}") from exc
    except anthropic.APIConnectionError as exc:
        raise GenerationError("Could not reach the Claude API. Check the network.") from exc
    if message.stop_reason == "refusal":
        raise GenerationError("Claude declined to read this design.")
    if message.stop_reason == "max_tokens":
        raise GenerationError("Claude's answer was cut off. Try a design with fewer layers.")
    return message
