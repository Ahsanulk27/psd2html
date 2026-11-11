"""`headline.py` must number words exactly as Palette's `tokenizeHeadline`.

The first block of strings is lifted from `lib/news-templates/canvas.test.ts`
in Palette; keep both sides passing on the same inputs.
"""

from __future__ import annotations

import pytest

from psd2html.headline import (
    DEFAULT_HIGHLIGHT,
    count_words,
    date_format,
    headline_colors,
    script_of,
    tokenize,
)


def words(text: str) -> list[str]:
    return [token.text for token in tokenize(text) if token.word_index is not None]


def ordinals(text: str) -> list[int | None]:
    return [token.word_index for token in tokenize(text)]


# -- tokenize: parity with canvas.test.ts -------------------------------------


def test_numbers_words_and_leaves_whitespace_unnumbered():
    assert [(t.text, t.word_index) for t in tokenize("one two")] == [("one", 0), (" ", None), ("two", 1)]


def test_trailing_whitespace_adds_no_word():
    assert count_words("one two   ") == 2


def test_whitespace_runs_between_words_are_one_token():
    tokens = tokenize("a \n b")
    assert [t.word_index for t in tokens] == [0, None, 1]
    assert tokens[1].text == " \n "


def test_peels_wrapping_quotes():
    tokens = tokenize("as 'Kutta Mizan'")
    assert words("as 'Kutta Mizan'") == ["as", "Kutta", "Mizan"]
    assert [t.text for t in tokens] == ["as", " ", "'", "Kutta", " ", "Mizan", "'"]


def test_internal_apostrophes_stay_on_the_word():
    assert words("Mizan's don't") == ["Mizan's", "don't"]


# -- tokenize: more shapes ----------------------------------------------------


def test_bengali_words():
    text = "ইরানি বিমান চলাচলে মার্কিন নিষেধাজ্ঞার"
    assert words(text) == text.split(" ")
    assert count_words(text) == 5


def test_double_spaces_are_a_single_whitespace_token():
    tokens = tokenize("one  two")
    assert [(t.text, t.word_index) for t in tokens] == [("one", 0), ("  ", None), ("two", 1)]


def test_curly_quotes_are_peeled():
    text = "named “Korai Kitchen” among"
    assert words(text) == ["named", "Korai", "Kitchen", "among"]
    assert [t.text for t in tokenize(text) if t.word_index is None and not t.text.isspace()] == ["“", "”"]


def test_trailing_whitespace_is_dropped_from_the_tokens():
    tokens = tokenize("word \n\t")
    assert [(t.text, t.word_index) for t in tokens] == [("word", 0)]
    assert tokenize("   ") == []
    assert tokenize("") == []


def test_quote_only_chunk_is_not_a_word():
    assert ordinals("a ' b") == [0, None, None, None, 1]
    assert words("a ' b") == ["a", "b"]
    assert count_words("''") == 0


def test_token_offsets_map_back_to_the_source():
    text = "  'Lead' ‘middle’ \n trail  "
    for token in tokenize(text):
        assert text[token.start : token.start + len(token.text)] == token.text
    assert words(text) == ["Lead", "middle", "trail"]


# -- count_words ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", 0),
        ("one", 1),
        ("one two three", 3),
        ("one\ntwo", 2),
        ("'quoted' \"words\"", 2),
        ("'", 0),
    ],
)
def test_count_words(text, expected):
    assert count_words(text) == expected


# -- headline_colors ------------------------------------------------------------


def runs(*pairs: tuple[str, str | None]) -> dict:
    return {"runs": [{"text": text, **({"color": color} if color else {})} for text, color in pairs]}


def test_majority_colour_is_the_base_and_the_other_is_the_highlight():
    colors = headline_colors([runs(("Breaking ", "#ffffff"), ("news", "#ffe107"), (" tonight", "#ffffff"))])
    assert colors.text == "Breaking news tonight"
    assert colors.base_color == "#ffffff"
    assert colors.highlight_color == "#ffe107"
    assert colors.word_colors == [(1, "#ffe107")]


def test_highlight_is_the_most_common_other_colour():
    paragraph = runs(
        ("a b c ", "#ffffff"),
        ("d e ", "#ff0000"),
        ("f", "#0000ff"),
    )
    colors = headline_colors([paragraph])
    assert colors.base_color == "#ffffff"
    assert colors.highlight_color == "#ff0000"
    assert colors.word_colors == [(3, "#ff0000"), (4, "#ff0000"), (5, "#0000ff")]


def test_tie_goes_to_the_colour_that_appears_first():
    colors = headline_colors([runs(("A ", "#ff0000"), ("B", "#0000ff"))])
    assert colors.base_color == "#ff0000"
    assert colors.highlight_color == "#0000ff"
    assert colors.word_colors == [(1, "#0000ff")]


def test_rgba_run_colours_are_normalised_to_hex():
    colors = headline_colors([runs(("one two ", "rgba(255, 0, 0, 0.5)"), ("three", "RGB(0, 0, 255)"))])
    assert colors.base_color == "#ff0000"
    assert colors.highlight_color == "#0000ff"
    assert colors.word_colors == [(2, "#0000ff")]


def test_paragraphs_join_with_newlines_and_keep_word_ordinals():
    colors = headline_colors([runs(("one two", "#ffffff")), runs(("three", "#ffe107"), (" four", "#ffffff"))])
    assert colors.text == "one two\nthree four"
    assert count_words(colors.text) == 4
    assert colors.word_colors == [(2, "#ffe107")]


def test_word_takes_the_colour_of_the_run_its_first_character_is_in():
    # "ne" is red but "o" is white: the word is white.
    colors = headline_colors([runs(("o", "#ffffff"), ("ne ", "#ff0000"), ("two", "#ffffff"))])
    assert colors.word_colors == []
    assert colors.base_color == "#ffffff"


def test_empty_runs_fall_back():
    empty = headline_colors([])
    assert (empty.text, empty.base_color, empty.highlight_color, empty.word_colors) == ("", "#ffffff", DEFAULT_HIGHLIGHT, [])
    blank = headline_colors([{"runs": [{"text": "", "color": "#000000"}]}], fallback_color="#123456")
    assert blank.text == ""
    assert blank.base_color == "#123456"
    assert blank.highlight_color == DEFAULT_HIGHLIGHT


def test_runs_without_a_colour_use_the_fallback():
    colors = headline_colors([runs(("one ", None), ("two", "#ffe107"), (" three", None))], fallback_color="#222222")
    assert colors.base_color == "#222222"
    assert colors.highlight_color == "#ffe107"
    assert colors.word_colors == [(1, "#ffe107")]


def test_single_colour_headline_gets_the_default_highlight():
    colors = headline_colors([runs(("all one colour", "#111111"))])
    assert colors.base_color == "#111111"
    assert colors.highlight_color == DEFAULT_HIGHLIGHT
    assert colors.word_colors == []


# -- date_format ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "language", "month_style", "order"),
    [
        ("28 Sept 2026", "en", "short", "day-first"),
        ("28 Sep 2026", "en", "short", "day-first"),
        ("September 28, 2026", "en", "long", "month-first"),
        ("Sep 28, 2026", "en", "short", "month-first"),
        ("২৮ সেপ্টেম্বর ২০২৬", "bn", "long", "day-first"),
        ("সেপ্টেম্বর ২৮, ২০২৬", "bn", "long", "month-first"),
        ("", "en", "short", "day-first"),
        ("12/08/2026", "en", "short", "day-first"),
        ("???", "en", "short", "day-first"),
    ],
)
def test_date_format(text, language, month_style, order):
    guess = date_format(text)
    assert (guess.language, guess.month_style, guess.order) == (language, month_style, order)


# -- script_of -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Hello world", "latin"),
        ("বাংলা শিরোনাম", "bangla"),
        ("Hello বাংলা", "mixed"),
        ("১২৩", "bangla"),
        ("123 !?", "latin"),
        ("", "latin"),
        ("Çà et là", "latin"),
    ],
)
def test_script_of(text, expected):
    assert script_of(text) == expected
