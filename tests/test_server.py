"""The HTTP API, without starting a real conversion."""

from __future__ import annotations

import importlib
import json
import os
import shutil
import sys
import time
from dataclasses import asdict
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from psd2html.jobs import Job

FIXTURE = Path(__file__).parent / "fixtures" / "boishakhi-tv.json"
KEY = {"X-Psd2html-Key": "test"}


@pytest.fixture(scope="module")
def data_dir(tmp_path_factory) -> Path:
    return tmp_path_factory.mktemp("psd2html-data")


@pytest.fixture(scope="module")
def server(data_dir: Path):
    os.environ["PSD2HTML_SHARED_SECRET"] = "test"
    os.environ["PSD2HTML_ENV"] = "development"
    os.environ["PSD2HTML_DATA"] = str(data_dir)
    import psd2html.server as module

    module = importlib.reload(module)  # pick up this module's data dir
    assert module.DATA_DIR == data_dir.resolve()
    return module


@pytest.fixture(scope="module")
def client(server) -> TestClient:
    with TestClient(server.app) as test_client:
        yield test_client


def write_done_job(data_dir: Path, job_id: str, *, status: str = "done", kind: str = "photocard") -> Path:
    job_dir = data_dir / "jobs" / job_id
    output = job_dir / "output"
    (output / "assets").mkdir(parents=True, exist_ok=True)
    job = Job(id=job_id, filename="boishakhi.psd", kind=kind, status=status, message="Finished", finished_at=time.time())
    (job_dir / "status.json").write_text(json.dumps(asdict(job)), encoding="utf-8")
    shutil.copyfile(FIXTURE, output / "photocard.json")
    (output / "preview.png").write_bytes(b"\x89PNG\r\n\x1a\nfake preview")
    (output / "assets" / "logo-5.png").write_bytes(b"\x89PNG\r\n\x1a\nfake logo")
    return job_dir


# -- auth ----------------------------------------------------------------------------


def test_healthz_is_open(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_job_status_needs_the_key(client):
    assert client.get("/api/jobs/x").status_code == 401
    assert client.get("/api/jobs/x", headers={"X-Psd2html-Key": "wrong"}).status_code == 401
    assert client.get("/api/jobs/x", headers=KEY).status_code == 404
    assert client.get("/api/jobs/0123456789ab", headers=KEY).status_code == 404


def test_every_api_route_needs_the_key(client):
    assert client.post("/api/photocards", files={"file": ("a.psd", b"8BPS")}).status_code == 401
    assert client.get("/api/jobs/0123456789ab/result").status_code == 401
    assert client.get("/api/jobs/0123456789ab/files/preview.png").status_code == 401
    assert client.delete("/api/jobs/0123456789ab").status_code == 401


# -- uploads ---------------------------------------------------------------------------


def test_upload_rejects_a_non_psd_name(client, data_dir):
    response = client.post("/api/photocards", headers=KEY, files={"file": ("design.txt", b"8BPS" + b"\0" * 64)})
    assert response.status_code == 400
    assert "Photoshop" in response.json()["detail"]
    assert list((data_dir / "jobs").iterdir()) == []


def test_upload_rejects_bad_magic(client):
    response = client.post("/api/photocards", headers=KEY, files={"file": ("design.psd", b"not a psd at all")})
    assert response.status_code == 400
    assert "not a valid Photoshop" in response.json()["detail"]


def test_bad_magic_upload_leaves_no_job_behind(client, data_dir):
    before = {p.name for p in (data_dir / "jobs").iterdir()}
    response = client.post("/api/photocards", headers=KEY, files={"file": ("design.psd", b"not a psd at all")})
    assert response.status_code == 400
    after = {p.name for p in (data_dir / "jobs").iterdir()}
    assert after == before, f"left behind: {sorted(after - before)}"


def test_upload_with_psd_magic_creates_a_photocard_job(client, data_dir):
    # Enough of a header to pass the magic check; the extractor fails on it
    # straight away, so Claude is never called.
    response = client.post(
        "/api/photocards", headers=KEY,
        files={"file": ("design.psd", b"8BPS" + b"\0" * 64)}, data={"name": "Weekend card"},
    )
    assert response.status_code == 202
    body = response.json()
    job_id = body["job_id"]
    assert body["status_url"] == f"/api/jobs/{job_id}"
    assert body["result_url"] == f"/api/jobs/{job_id}/result"

    status = client.get(f"/api/jobs/{job_id}", headers=KEY).json()
    assert status["kind"] == "photocard"
    assert status["filename"] == "Weekend card"

    deadline = time.time() + 20
    while status["status"] not in ("done", "error") and time.time() < deadline:
        time.sleep(0.1)
        status = client.get(f"/api/jobs/{job_id}", headers=KEY).json()
    assert status["status"] == "error"
    assert client.get(f"/api/jobs/{job_id}/result", headers=KEY).status_code == 409

    assert client.delete(f"/api/jobs/{job_id}", headers=KEY).status_code == 204
    assert client.get(f"/api/jobs/{job_id}", headers=KEY).status_code == 404
    assert not (data_dir / "jobs" / job_id).exists()


def test_delete_unknown_job_is_a_no_op(client):
    assert client.delete("/api/jobs/0123456789ab", headers=KEY).status_code == 204
    assert client.delete("/api/jobs/not-a-job", headers=KEY).status_code == 204


# -- results -----------------------------------------------------------------------------


def test_result_manifest_and_files(client, data_dir):
    job_id = "abcdef012345"
    write_done_job(data_dir, job_id)

    status = client.get(f"/api/jobs/{job_id}", headers=KEY)
    assert status.status_code == 200
    assert status.json()["status"] == "done"

    result = client.get(f"/api/jobs/{job_id}/result", headers=KEY)
    assert result.status_code == 200
    manifest = result.json()
    assert manifest["spec"] == json.loads(FIXTURE.read_text(encoding="utf-8"))
    files = {entry["path"]: entry for entry in manifest["files"]}
    assert set(files) == {"preview.png", "assets/logo-5.png"}
    assert files["preview.png"]["contentType"] == "image/png"
    assert files["preview.png"]["bytes"] == len(b"\x89PNG\r\n\x1a\nfake preview")

    preview = client.get(f"/api/jobs/{job_id}/files/preview.png", headers=KEY)
    assert preview.status_code == 200
    assert preview.content == b"\x89PNG\r\n\x1a\nfake preview"
    logo = client.get(f"/api/jobs/{job_id}/files/assets/logo-5.png", headers=KEY)
    assert logo.status_code == 200
    assert logo.content.endswith(b"fake logo")

    download = client.get(f"/api/jobs/{job_id}/download", headers=KEY)
    assert download.status_code == 200
    assert download.headers["content-type"] == "application/zip"
    assert 'filename="boishakhi-photocard.zip"' in download.headers["content-disposition"]


def test_result_files_do_not_escape_the_output_directory(client, data_dir):
    job_id = "abcdef012345"
    write_done_job(data_dir, job_id)
    assert (data_dir / "jobs" / job_id / "status.json").is_file()
    for path in ("../status.json", "..%2Fstatus.json", "%2e%2e/status.json", "assets/../../status.json", "missing.png"):
        response = client.get(f"/api/jobs/{job_id}/files/{path}", headers=KEY)
        assert response.status_code == 404, path
    # The absolute path of a real file outside the job is not served either.
    outside = (data_dir / "jobs" / job_id / "status.json").resolve().as_posix()
    assert client.get(f"/api/jobs/{job_id}/files/{outside}", headers=KEY).status_code == 404


def test_result_of_an_unfinished_job_is_a_conflict(client, data_dir):
    job_id = "0000000000aa"
    write_done_job(data_dir, job_id, status="extracting")
    # Found on disk after a "restart" with no worker: reported as an error.
    status = client.get(f"/api/jobs/{job_id}", headers=KEY).json()
    assert status["status"] == "error"
    assert client.get(f"/api/jobs/{job_id}/result", headers=KEY).status_code == 409
    assert client.get(f"/api/jobs/{job_id}/files/preview.png", headers=KEY).status_code == 409


def test_result_of_an_html_job_is_a_conflict(client, data_dir):
    job_id = "0000000000bb"
    write_done_job(data_dir, job_id, kind="html")
    assert client.get(f"/api/jobs/{job_id}/result", headers=KEY).status_code == 409
