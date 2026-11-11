# psd2html

Upload a Photoshop design (`.psd` / `.psb`) and get it back as an HTML page:
one `index.html` with its own CSS, plus an `assets/` folder of images cut from
the PSD. Claude (`claude-opus-5-5`) writes the HTML.

## How it works

1. **Read the PSD** with [psd-tools](https://github.com/psd-tools/psd-tools):
   - render the full design and cut it into slices Claude can read (tall pages
     come as several slices, top to bottom)
   - walk the layer tree, skipping hidden layers. Text layers give their exact
     strings, fonts, sizes, colors, line height and letter spacing. Shape
     layers give fill colors and corner radii. Solid-color layers become CSS
     colors.
   - export photos, logos and complex icons as image files into `assets/`
2. **Ask Claude** for the HTML. It gets the design images plus the layer data as
   JSON. The images show the layout and the JSON gives exact values, so text
   comes out as real text and colors and sizes come out exact.
3. **Check the result**: the job lists any image files the HTML points to that
   don't exist. In the web app you compare the HTML with the design side by
   side, or lay the design over the HTML at adjustable opacity.

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

## API

| Method | Path | What it does |
|---|---|---|
| `POST` | `/api/convert` | Upload a file (multipart field `file`). Returns `job_id` and URLs. Status 202. |
| `GET` | `/api/jobs/{id}` | Job status: `queued`, `extracting`, `generating`, `done` or `error`, plus progress, token usage and warnings |
| `GET` | `/api/jobs/{id}/download` | The finished `index.html` + `assets/` as a zip |
| `GET` | `/api/jobs/{id}/design.png` | The rendered design |
| `GET` | `/jobs/{id}/index.html` | The generated page (assets load next to it) |

```bash
curl -F "file=@design.psd" http://127.0.0.1:8000/api/convert
curl http://127.0.0.1:8000/api/jobs/<job_id>
curl -o design-html.zip http://127.0.0.1:8000/api/jobs/<job_id>/download
```

Interactive API docs: http://127.0.0.1:8000/docs

## Settings (`.env`)

| Variable | Default | |
|---|---|---|
| `ANTHROPIC_API_KEY` | (required) | `CLAUDE_API_KEY` also works |
| `PSD2HTML_MODEL` | `claude-opus-5-5` | |
| `PSD2HTML_EFFORT` | `high` | `low` to `max`. Higher matches the design more closely but is slower and costs more. |
| `PSD2HTML_DATA` | `data` | Where jobs are stored |
| `PSD2HTML_MAX_UPLOAD_MB` | `500` | |
| `PSD2HTML_WORKERS` | `2` | Conversions that run at the same time |

## Good to know

- **Fonts**: Claude maps the PSD's fonts to Google Fonts where it can. Other
  fonts get a close fallback, named in a comment at the top of the HTML.
- **Cost and time**: one conversion is one Claude request with a few images and
  the layer data. Expect a minute or more for a full landing page at `high`
  effort. The job status shows token usage.
- **Refusal fallback**: requests use the API's server-side `fallbacks: "default"`,
  so if a safety check declines a request, another model finishes it.
- **Safety**: the generated page can't run scripts. The preview iframe is
  sandboxed and the server sends a CSP header.
- **PSDs that won't render**: if psd-tools can't draw some effect, the PSD's
  saved preview is used instead. Save with "Maximize compatibility" on.
- **Jobs are kept** in `data/jobs/` and are never cleaned up automatically.
