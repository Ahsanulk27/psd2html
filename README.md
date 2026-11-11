# psd2html

Upload a Photoshop design (`.psd` / `.psb`) and get it back in one of two
forms:

- **HTML**: one `index.html` with its own CSS, plus an `assets/` folder of
  images cut from the PSD. Claude (`claude-opus-5-5`) writes the HTML.
- **Photocard**: a `photocard.json` layout spec that Palette imports as a news
  photocard template, plus the design's `assets/` and a `preview.png`. Claude
  only says which layer plays which role; every number comes from the PSD.

## How it works

1. **Read the PSD** with [psd-tools](https://github.com/psd-tools/psd-tools):
   - render the full design and cut it into slices Claude can read (tall pages
     come as several slices, top to bottom)
   - walk the layer tree, skipping hidden layers. Text layers give their exact
     strings, fonts, sizes, colors, line height and letter spacing. Shape
     layers give fill colors and corner radii. Solid-color layers become CSS
     colors. Gradient fill layers give their stops and angle. A raster with a
     see-through hole reports the hole as a "transparent window".
   - export photos, logos and complex icons as image files into `assets/`
2. **Ask Claude**. For HTML it writes the page: it gets the design images plus
   the layer data as JSON, so text comes out as real text and colors and sizes
   come out exact. For a photocard it only assigns roles (see below).
3. **Check the result**: an HTML job lists any image files the HTML points to
   that don't exist; a photocard job validates the spec and lists fonts
   Palette doesn't know. In the web app you compare the HTML with the design
   side by side, or lay the design over the HTML at adjustable opacity.

## Photocard mode

A photocard is a news post image: one photo, one headline whose words can be
painted in two colours, a few small captions (source line, dateline, site),
and the chrome around them. `PhotocardSpec` (`src/psd2html/spec.py`) is the
contract with Palette: a canvas size, a font list, and a layer list in paint
order, bottom first. Layer kinds are `image`, `fill`, `gradient`, `photo`
(exactly one), `headline` (exactly one) and `caption`. Palette mirrors the
same schema in `lib/news-templates/spec/schema.ts`; the exported JSON Schema
lives in `src/psd2html/photocard_schema.json` and the test suite fails when it
is stale.

**What Claude decides** (`PhotocardDecisions`, see `decide.py`): which text
layer is the headline (or a box for a new one when the design has none),
where the photo goes (the layer with the transparent window, a shape or raster
used as a picture box, or a new box), which small texts are captions and with
what role, which layers are sample content to drop, which gradients deserve a
label and an editable toggle, and which layers above the photo a lifted
subject must still clear.

**What is deterministic** (`assemble.py`): everything else. Boxes, colours,
font names, sizes, line heights, letter spacing, gradient stops (with
Photoshop's scale and offset folded in), corner radii, clipping, opacity and
blend modes all come straight from the PSD. The headline's base colour is the
colour most of its words share and the highlight is the most common other
colour, numbered by the same word tokenizer Palette's studio uses. A
full-canvas solid fill at the bottom becomes the page background; a raster
that fills the photo slot is dropped as the sample photo; a dateline's
language, month style and order are read from the sample date.

From the command line (writes `design-photocard/output/`):

```bash
uv run psd2html photocard design.psd
```

Output files:

| File | |
|---|---|
| `photocard.json` | The spec. `warnings` carries the extractor's and the model's notes; `missingFonts` lists PostScript names whose family Palette doesn't ship. |
| `assets/*.png\|jpg` | Image layers the spec references (unused exports are removed) |
| `preview.png` | The rendered design, for the template picker |

The `work/` directory next to it keeps `layers.json`, `candidates.json` (what
Claude saw) and `decisions.json` (what it answered), for debugging.

## Setup

Needs [uv](https://docs.astral.sh/uv/) and a Claude API key.

```bash
uv sync
cp .env.example .env   # then put your key in ANTHROPIC_API_KEY
```

## Run

Web app and API on http://127.0.0.1:8000:

```bash
uv run psd2html serve
```

One file from the command line (writes `design-html/output/index.html`):

```bash
uv run psd2html convert design.psd
```

Only read the PSD, without calling Claude. Use this to see what the model
will get (`layers.json`, slices, assets):

```bash
uv run psd2html extract design.psd
```

### Docker

```bash
docker build -t psd2html .
docker run -p 8000:8000 \
  -e ANTHROPIC_API_KEY=... -e PSD2HTML_SHARED_SECRET=... psd2html
```

The image sets `PSD2HTML_ENV=production`, so a shared secret is required. It
listens on `PORT` (8000 unless the host sets it). The job store is in memory,
so run exactly one instance. Jobs are deleted once Palette has copied them, so
no persistent disk is needed.

### Deploying on Render

Create a **Web Service** from this repository with the **Docker** runtime.
Render sets `PORT` itself and the image listens on it.

- **Instance:** at least 2 GB of memory. Rendering a large PSD is memory
  hungry, and 512 MB instances run out on big files.
- **Instances:** exactly one (the job store is in memory).
- **Health check path:** `/healthz`.
- **Environment:** `ANTHROPIC_API_KEY`, and `PSD2HTML_SHARED_SECRET` set to a
  long random string (32 or more characters). palette-server gets the same
  secret, plus this service's URL as `PSD2HTML_URL`.

Without Docker, use the Python runtime with `PYTHON_VERSION=3.12`, build
command `pip install uv && uv sync --frozen --no-dev`, and start command
`uv run psd2html serve --host 0.0.0.0`.

## API

Every `/api/` route takes the shared secret in the `X-Psd2html-Key` header
when `PSD2HTML_SHARED_SECRET` is set (it always is in production). Without it
the request gets a 401. `/healthz` is open.

| Method | Path | What it does |
|---|---|---|
| `POST` | `/api/convert` | Upload a file (multipart field `file`) for HTML. Returns `job_id` and URLs. Status 202. |
| `POST` | `/api/photocards` | Upload a file (multipart field `file`, optional `name` to show in Palette) to read as a photocard. Returns `job_id`, `status_url` and `result_url`. Status 202. |
| `GET` | `/api/jobs/{id}` | Job status: `queued`, `extracting`, `generating`, `assembling`, `done` or `error`, plus progress, token usage, warnings and missing fonts |
| `GET` | `/api/jobs/{id}/result` | A finished photocard job: `{ spec, files }`, where `files` lists every file to copy with its path, size and content type. 409 until the job is done. |
| `GET` | `/api/jobs/{id}/files/{path}` | One output file (`preview.png`, `assets/logo-5.png`...). Paths outside the job's output are 404. |
| `GET` | `/api/jobs/{id}/download` | The finished output as a zip (`index.html` + `assets/`, or `photocard.json` + `assets/` + `preview.png`) |
| `GET` | `/api/jobs/{id}/design.png` | The rendered design |
| `DELETE` | `/api/jobs/{id}` | Remove the job and its files. 204, also for a job that doesn't exist. |
| `GET` | `/jobs/{id}/index.html` | The generated page (assets load next to it) |
| `GET` | `/healthz` | `{"ok": true}`, no key needed (the Docker health check uses it) |

```bash
curl -F "file=@design.psd" http://127.0.0.1:8000/api/convert
curl -H "X-Psd2html-Key: $PSD2HTML_SHARED_SECRET" -F "file=@design.psd" -F "name=Weekend card" \
  http://127.0.0.1:8000/api/photocards
curl -H "X-Psd2html-Key: $PSD2HTML_SHARED_SECRET" http://127.0.0.1:8000/api/jobs/<job_id>
curl -H "X-Psd2html-Key: $PSD2HTML_SHARED_SECRET" http://127.0.0.1:8000/api/jobs/<job_id>/result
curl -o design-html.zip http://127.0.0.1:8000/api/jobs/<job_id>/download
```

Interactive API docs: http://127.0.0.1:8000/docs

## Settings (`.env`)

| Variable | Default | |
|---|---|---|
| `ANTHROPIC_API_KEY` | (required) | `CLAUDE_API_KEY` also works |
| `PSD2HTML_SHARED_SECRET` | (empty) | The value Palette's server sends in `X-Psd2html-Key`. Empty keeps the API open, which is fine on a developer machine. |
| `PSD2HTML_ENV` | `development` | `production` refuses to start without a shared secret (the Docker image sets it) |
| `PSD2HTML_MODEL` | `claude-opus-5-5` | |
| `PSD2HTML_EFFORT` | `high` | HTML mode. `low` to `max`. Higher matches the design more closely but is slower and costs more. |
| `PSD2HTML_PHOTOCARD_MODEL` | `PSD2HTML_MODEL` | Photocard mode. Opus 5.5 and Sonnet 5.5 both read the benchmark PSDs correctly; Haiku 5.5 did not. |
| `PSD2HTML_PHOTOCARD_EFFORT` | `medium` | Photocard mode. The answer is short, so effort barely changes the cost (about $0.05 a file on Opus 5.5). |
| `PSD2HTML_DATA` | `data` | Where jobs are stored |
| `PSD2HTML_MAX_UPLOAD_MB` | `500` | |
| `PSD2HTML_WORKERS` | `2` | Conversions that run at the same time |
| `PSD2HTML_JOB_TTL_HOURS` | `24` | Finished jobs are deleted after this; `0` keeps them forever |

## Tests

```bash
uv run pytest
```

The fast tests need no PSD and no API key. `tests/test_benchmarks.py` runs
the real extractor on three benchmark designs and compares the assembled spec
with the fixtures in `tests/fixtures/`. The designs are clients' artwork and
are not in the repository: put them in `tests/psd/` (ignored by git) or point
`PSD2HTML_BENCHMARK_DIR` at a folder holding them. The test skips when the
files are absent and takes a few minutes when they are present. Leave it out with
`uv run pytest -m "not slow"`. A change to the spec models must regenerate
`photocard_schema.json`; the failing test prints the one-liner.

## Good to know

- **Fonts**: in HTML mode Claude maps the PSD's fonts to Google Fonts where it
  can. Other fonts get a close fallback, named in a comment at the top of the
  HTML. A photocard keeps the PSD's PostScript names and reports the ones
  Palette doesn't know in `missingFonts`, so the organization can upload them.
- **Cost and time**: one conversion is one Claude request with a few images and
  the layer data. Expect a minute or more for a full landing page at `high`
  effort; a photocard is quicker because the model only answers with roles.
  The job status shows token usage.
- **Refusal fallback**: requests use the API's server-side `fallbacks: "default"`,
  so if a safety check declines a request, another model finishes it.
- **Safety**: the generated page can't run scripts. The preview iframe is
  sandboxed and the server sends a CSP header.
- **PSDs that won't render**: if psd-tools can't draw some effect, the PSD's
  saved preview is used instead. Save with "Maximize compatibility" on.
- **Jobs are kept** in `data/jobs/` until the TTL sweeper removes them, once
  an hour.
