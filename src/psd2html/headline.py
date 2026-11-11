"""Headline text helpers that must agree with Palette's canvas code.

Palette numbers the words of a headline so an author can paint each one.
`tokenize` here is a line-for-line port of `tokenizeHeadline` in
`lib/news-templates/canvas.ts`: when the two disagree, a word colour chosen
at conversion time lands on the wrong word in the studio. Keep them in step
and keep the shared test strings passing on both sides.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

# JavaScript's `\s`, spelled out, because Python's `\s` also takes the
# separator controls U+001C..U+001F.
_JS_WHITESPACE = (
    " \t\n\x0b\x0c\r         "
    "        　﻿"
)
_WS_CLASS = "[" + re.escape(_JS_WHITESPACE) + "]"
_TRAILING_WS = re.compile(_WS_CLASS + "+$")
_SPLIT_WS = re.compile("(" + _WS_CLASS + "+)")

# Same set as QUOTE_MARK in canvas.ts.
_QUOTE_MARKS = set("'\"`‘’‚‛“”„‟«»‹›")

_BANGLA = re.compile(r"[ঀ-৿]")
_BANGLA_DIGITS = re.compile(r"[০-৯]")
_LATIN_LETTERS = re.compile(r"[A-Za-zÀ-ɏ]")


@dataclass(frozen=True)
class Token:
    text: str
    word_index: int | None
    start: int  # offset of the token's first character in the source text


def tokenize(headline: str) -> list[Token]:
    """Split a headline into words, whitespace and peeled quotation marks.

    Only words get an index. Offsets are into `headline` as given, so a
    caller can map a word back to the text run it came from.
    """
    tokens: list[Token] = []
    text = _TRAILING_WS.sub("", headline)
    word_index = 0
    offset = 0
    for chunk in _SPLIT_WS.split(text):
        if not chunk:
            continue
        start = offset
        offset += len(chunk)
        if _SPLIT_WS.fullmatch(chunk):
            tokens.append(Token(chunk, None, start))
            continue
        lead = 0
        end = len(chunk)
        while lead < end and chunk[lead] in _QUOTE_MARKS:
            lead += 1
        while end > lead and chunk[end - 1] in _QUOTE_MARKS:
            end -= 1
        if lead:
            tokens.append(Token(chunk[:lead], None, start))
        if end > lead:
            tokens.append(Token(chunk[lead:end], word_index, start + lead))
            word_index += 1
        if end < len(chunk):
            tokens.append(Token(chunk[end:], None, start + end))
    return tokens


def count_words(headline: str) -> int:
    return sum(1 for token in tokenize(headline) if token.word_index is not None)


@dataclass(frozen=True)
class HeadlineColors:
    text: str
    base_color: str
    highlight_color: str
    word_colors: list[tuple[int, str]]  # (word ordinal, colour) for words off the base colour


DEFAULT_HIGHLIGHT = "#ffe107"


def headline_colors(paragraphs: list[dict], fallback_color: str = "#ffffff") -> HeadlineColors:
    """Rebuild a text layer's string from its runs and work out its word colours.

    Paragraphs are joined with newlines, exactly as the studio shows a
    multi-line headline. A word takes the colour of the run its first
    character falls in. The colour most words share is the base; the most
    common other colour is the highlight brush; every word off the base is
    listed so the studio opens with the design's own emphasis.
    """
    text_parts: list[str] = []
    spans: list[tuple[int, int, str]] = []  # (start, end, colour)
    offset = 0
    for index, paragraph in enumerate(paragraphs):
        if index:
            text_parts.append("\n")
            offset += 1
        for run in paragraph.get("runs", []):
            run_text = run.get("text", "")
            if not run_text:
                continue
            color = _normalize_color(run.get("color")) or fallback_color
            spans.append((offset, offset + len(run_text), color))
            text_parts.append(run_text)
            offset += len(run_text)
    text = "".join(text_parts)

    words = [token for token in tokenize(text) if token.word_index is not None]
    colors_by_word: list[tuple[int, str]] = []
    for token in words:
        color = fallback_color
        for start, end, span_color in spans:
            if start <= token.start < end:
                color = span_color
                break
        colors_by_word.append((token.word_index or 0, color))

    if not colors_by_word:
        return HeadlineColors(text, fallback_color, DEFAULT_HIGHLIGHT, [])

    counts = Counter(color for _, color in colors_by_word)
    # Ties go to the colour that appears first in the text.
    base = max(counts, key=lambda c: (counts[c], -next(i for i, (_, w) in enumerate(colors_by_word) if w == c)))
    others = [(color, n) for color, n in counts.most_common() if color != base]
    highlight = others[0][0] if others else DEFAULT_HIGHLIGHT
    off_base = [(index, color) for index, color in colors_by_word if color != base]
    return HeadlineColors(text, base, highlight, off_base)


def _normalize_color(value: str | None) -> str | None:
    """Palette stores word colours as #rrggbb; drop alpha from rgba() runs."""
    if not value:
        return None
    value = value.strip().lower()
    if re.fullmatch(r"#[0-9a-f]{6}", value):
        return value
    match = re.fullmatch(r"rgba?\((\d+),\s*(\d+),\s*(\d+)(?:,\s*[\d.]+)?\)", value)
    if match:
        r, g, b = (min(255, int(part)) for part in match.groups())
        return f"#{r:02x}{g:02x}{b:02x}"
    return None


def script_of(text: str) -> str:
    """'bangla', 'latin' or 'mixed', by the letters the text uses."""
    has_bangla = bool(_BANGLA.search(text))
    has_latin = bool(_LATIN_LETTERS.search(text))
    if has_bangla and has_latin:
        return "mixed"
    if has_bangla:
        return "bangla"
    return "latin"


@dataclass(frozen=True)
class DateFormat:
    language: str  # "en" | "bn"
    month_style: str  # "short" | "long"
    order: str  # "day-first" | "month-first"


_MONTH_TOKEN = re.compile(r"[A-Za-zঀ-৥ৰ-৿]+\.?")  # letters only, no Bengali digits
_DAY_TOKEN = re.compile(r"\d{1,2}|[০-৯]{1,2}")


def date_format(text: str) -> DateFormat:
    """Guess how a dateline is written from a sample like '28 Sept 2026'.

    Bengali digits mean Bengali; a month token of four letters or fewer is
    the short form; a day before the month is day-first. Anything unclear
    falls back to Palette's own default (English, short, day-first).
    """
    language = "bn" if _BANGLA_DIGITS.search(text) or _BANGLA.search(text) else "en"
    month = _MONTH_TOKEN.search(text)
    month_style = "short"
    if month:
        letters = month.group(0).rstrip(".")
        if language == "en":
            month_style = "short" if len(letters) <= 4 else "long"
        else:
            # Bengali months are always written in full; a short form is rare
            # enough that Palette's long style is the better default.
            month_style = "long"
    order = "day-first"
    if month:
        before = text[: month.start()]
        if not _DAY_TOKEN.search(before):
            order = "month-first"
    return DateFormat(language, month_style, order)
