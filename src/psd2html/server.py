"""HTTP service: upload page + JSON API."""

from __future__ import annotations

import os
from dataclasses import asdict
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response

from .auth import check_startup, require_key
from .config import load_env
from .jobs import JobStore, ttl_hours_from_env

load_env()
check_startup()

DATA_DIR = Path(os.environ.get("PSD2HTML_DATA", "data")).resolve()
MAX_UPLOAD_MB = int(os.environ.get("PSD2HTML_MAX_UPLOAD_MB", "500"))
STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="PSD to HTML", version="0.2.0")
store = JobStore(
    DATA_DIR / "jobs",
    workers=int(os.environ.get("PSD2HTML_WORKERS", "2")),
    ttl_hours=ttl_hours_from_env(),
)
store.start_sweeper()

api = Depends(require_key)


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/healthz", include_in_schema=False)
def healthz() -> dict:
    return {"ok": True}


@app.post("/api/convert", status_code=202, dependencies=[api])
def create_conversion(file: UploadFile = File(...)) -> dict:
    """Upload a .psd/.psb file for HTML. Returns a job id to poll."""
    job = _receive(file, kind="html")
    return {
        "job_id": job.id,
        "status_url": f"/api/jobs/{job.id}",
        "preview_url": f"/jobs/{job.id}/index.html",
        "download_url": f"/api/jobs/{job.id}/download",
    }


@app.post("/api/photocards", status_code=202, dependencies=[api])
def create_photocard(file: UploadFile = File(...), name: str | None = Form(default=None)) -> dict:
    """Upload a .psd/.psb file to read as a photocard layout. Returns a job id to poll."""
    job = _receive(file, kind="photocard", display_name=name)
    return {
        "job_id": job.id,
        "status_url": f"/api/jobs/{job.id}",
        "result_url": f"/api/jobs/{job.id}/result",
    }


@app.get("/api/jobs/{job_id}", dependencies=[api])
def job_status(job_id: str) -> dict:
    return asdict(_job(job_id))


@app.get("/api/jobs/{job_id}/result", dependencies=[api])
def job_result(job_id: str) -> dict:
    job = _job(job_id)
    if job.kind != "photocard":
        raise HTTPException(409, "This job did not produce a photocard.")
    if job.status != "done":
        raise HTTPException(409, "This job has not finished.")
    return store.result_manifest(job_id)


@app.get("/api/jobs/{job_id}/files/{path:path}", dependencies=[api])
def job_file(job_id: str, path: str) -> FileResponse:
    job = _job(job_id)
    if job.status != "done":
        raise HTTPException(409, "This job has not finished.")
    target = store.output_file(job_id, path)
    if target is None:
        raise HTTPException(404, "Not found.")
    return FileResponse(target)


@app.delete("/api/jobs/{job_id}", status_code=204, dependencies=[api])
def delete_job(job_id: str) -> Response:
    store.delete(job_id)
    return Response(status_code=204)


@app.get("/api/jobs/{job_id}/download", dependencies=[api])
def download(job_id: str) -> Response:
    job = _job(job_id)
    if job.status != "done":
        raise HTTPException(409, "This job has not finished.")
    stem = Path(job.filename).stem or "design"
    suffix = "photocard" if job.kind == "photocard" else "html"
    return Response(
        store.zip_output(job_id),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{stem}-{suffix}.zip"'},
    )


@app.get("/api/jobs/{job_id}/design.png", dependencies=[api])
def design_image(job_id: str) -> FileResponse:
    _job(job_id)
    path = store.job_dir(job_id) / "work" / "design.png"
    if not path.is_file():
        raise HTTPException(404, "The design has not been rendered yet.")
    return FileResponse(path)


@app.get("/jobs/{job_id}/{path:path}", include_in_schema=False, dependencies=[api])
def output_file(job_id: str, path: str) -> FileResponse:
    _job(job_id)
    target = store.output_file(job_id, path or "index.html")
    if target is None:
        raise HTTPException(404, "Not found.")
    # The generated page is shown in a sandboxed iframe; this keeps it from
    # running scripts even when opened directly.
    headers = {"Content-Security-Policy": "script-src 'none'"} if target.suffix == ".html" else {}
    return FileResponse(target, headers=headers)


def _receive(file: UploadFile, *, kind: str, display_name: str | None = None):
    name = file.filename or "design.psd"
    if not name.lower().endswith((".psd", ".psb")):
        raise HTTPException(400, "Upload a Photoshop file (.psd or .psb).")
    display = (display_name or "").strip()[:100] or name
    job, destination = store.create(display if kind == "photocard" else name, kind=kind)
    limit = MAX_UPLOAD_MB * 1024 * 1024
    size = 0
    with destination.open("wb") as out:
        while chunk := file.file.read(1024 * 1024):
            size += len(chunk)
            if size > limit:
                out.close()
                store.delete(job.id)
                raise HTTPException(413, f"File is larger than {MAX_UPLOAD_MB} MB.")
            out.write(chunk)
    with destination.open("rb") as check:
        magic = check.read(4)
    # Outside the `with`: Windows will not delete a file that is still open.
    if magic != b"8BPS":
        store.delete(job.id)
        raise HTTPException(400, "This file is not a valid Photoshop document.")
    store.start(job.id)
    return job


def _job(job_id: str):
    job = store.get(job_id)
    if job is None:
        raise HTTPException(404, "No such job.")
    return job
