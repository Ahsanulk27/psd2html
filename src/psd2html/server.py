"""HTTP service: upload page + JSON API."""

from __future__ import annotations

import os
from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response

from .config import load_env
from .jobs import JobStore, remove_tree

load_env()

DATA_DIR = Path(os.environ.get("PSD2HTML_DATA", "data")).resolve()
MAX_UPLOAD_MB = int(os.environ.get("PSD2HTML_MAX_UPLOAD_MB", "500"))
STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="PSD to HTML", version="0.1.0")
store = JobStore(DATA_DIR / "jobs", workers=int(os.environ.get("PSD2HTML_WORKERS", "2")))


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.post("/api/convert", status_code=202)
def create_conversion(file: UploadFile = File(...)) -> dict:
    """Upload a .psd/.psb file. Returns a job id to poll."""
    name = file.filename or "design.psd"
    if not name.lower().endswith((".psd", ".psb")):
        raise HTTPException(400, "Upload a Photoshop file (.psd or .psb).")

    job, destination = store.create(name)
    limit = MAX_UPLOAD_MB * 1024 * 1024
    size = 0
    with destination.open("wb") as out:
        while chunk := file.file.read(1024 * 1024):
            size += len(chunk)
            if size > limit:
                out.close()
                remove_tree(store.job_dir(job.id))
                raise HTTPException(413, f"File is larger than {MAX_UPLOAD_MB} MB.")
            out.write(chunk)
    with destination.open("rb") as check:
        if check.read(4) != b"8BPS":
            remove_tree(store.job_dir(job.id))
            raise HTTPException(400, "This file is not a valid Photoshop document.")

    store.start(job.id)
    return {
        "job_id": job.id,
        "status_url": f"/api/jobs/{job.id}",
        "preview_url": f"/jobs/{job.id}/index.html",
        "download_url": f"/api/jobs/{job.id}/download",
    }


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str) -> dict:
    return asdict(_job(job_id))


@app.get("/api/jobs/{job_id}/download")
def download(job_id: str) -> Response:
    job = _job(job_id)
    if job.status != "done":
        raise HTTPException(409, "This job has not finished.")
    stem = Path(job.filename).stem or "design"
    return Response(
        store.zip_output(job_id),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{stem}-html.zip"'},
    )


@app.get("/api/jobs/{job_id}/design.png")
def design_image(job_id: str) -> FileResponse:
    _job(job_id)
    path = store.job_dir(job_id) / "work" / "design.png"
    if not path.is_file():
        raise HTTPException(404, "The design has not been rendered yet.")
    return FileResponse(path)


@app.get("/jobs/{job_id}/{path:path}", include_in_schema=False)
def output_file(job_id: str, path: str) -> FileResponse:
    _job(job_id)
    target = store.output_file(job_id, path or "index.html")
    if target is None:
        raise HTTPException(404, "Not found.")
    # The generated page is shown in a sandboxed iframe; this keeps it from
    # running scripts even when opened directly.
    headers = {"Content-Security-Policy": "script-src 'none'"} if target.suffix == ".html" else {}
    return FileResponse(target, headers=headers)


def _job(job_id: str):
    job = store.get(job_id)
    if job is None:
        raise HTTPException(404, "No such job.")
    return job
