FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PSD2HTML_ENV=production \
    PSD2HTML_DATA=/data

RUN pip install --no-cache-dir uv

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev

# Jobs are temporary (deleted once Palette has copied them, or after
# PSD2HTML_JOB_TTL_HOURS), so /data needs no persistent disk.
ENV PORT=8000
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import os,urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('PORT', '8000'), timeout=4).status == 200 else 1)"

# One process: the job store is in memory, so run a single instance. The port
# comes from PORT, which hosts such as Render set.
CMD ["uv", "run", "--no-sync", "psd2html", "serve", "--host", "0.0.0.0"]
