"""Conversion jobs: PSD in, folder with index.html + assets out.

Each job lives in its own directory:
  <job>/source.psd
  <job>/work/      design.png, slices, layers.json (inputs for the model)
  <job>/output/    index.html and assets/ (the result)
  <job>/status.json
"""

from __future__ import annotations

import io
import json
import re
import shutil
import threading
import time
import traceback
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .extract import extract
from .generate import GenerationError, generate_html

JOB_ID = re.compile(r"^[0-9a-f]{12}$")


@dataclass
class Job:
    id: str
    filename: str
    status: str = "queued"  # queued | extracting | generating | done | error
    message: str = "Waiting to start"
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    canvas: list[int] | None = None
    layers: int = 0
    assets: int = 0
    chars_written: int = 0
    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    warnings: list[str] = field(default_factory=list)
    error: str | None = None


def convert(psd_path: Path, job_dir: Path, update=lambda **_: None) -> Path:
    """Run the whole pipeline. Returns the output directory."""
    work_dir, output_dir = job_dir / "work", job_dir / "output"
    update(status="extracting", message="Reading layers from the PSD")
    extraction = extract(psd_path, work_dir, output_dir)
    update(
        canvas=[extraction.width, extraction.height],
        layers=_count(extraction.layers),
        assets=len(extraction.assets),
        warnings=list(extraction.warnings),
        status="generating",
        message="Claude is writing the HTML",
    )

    result = generate_html(extraction, on_progress=lambda n: update(chars_written=n))
    (output_dir / "index.html").write_text(result.html, encoding="utf-8")

    warnings = list(extraction.warnings)
    if result.missing_assets:
        warnings.append("The HTML points to images that don't exist: " + ", ".join(result.missing_assets))
    update(
        model=result.model,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        warnings=warnings,
    )
    return output_dir


def _count(layers: list[dict]) -> int:
    return sum(1 + _count(layer.get("children", [])) for layer in layers)


class JobStore:
    def __init__(self, root: Path, workers: int = 2):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="psd2html")

    # -- public -----------------------------------------------------------

    def create(self, filename: str) -> tuple[Job, Path]:
        job = Job(id=uuid.uuid4().hex[:12], filename=filename)
        job_dir = self.root / job.id
        job_dir.mkdir()
        with self._lock:
            self._jobs[job.id] = job
        self._save(job)
        return job, job_dir / "source.psd"

    def start(self, job_id: str) -> None:
        self._pool.submit(self._run, job_id)

    def get(self, job_id: str) -> Job | None:
        if not JOB_ID.match(job_id):
            return None
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:  # created before a restart
            status = self.root / job_id / "status.json"
            if status.exists():
                job = Job(**json.loads(status.read_text()))
                if job.status not in ("done", "error"):
                    job.status, job.error = "error", "The server restarted during this job."
                with self._lock:
                    self._jobs[job_id] = job
        return job

    def job_dir(self, job_id: str) -> Path:
        return self.root / job_id

    def output_file(self, job_id: str, relative: str) -> Path | None:
        output = (self.root / job_id / "output").resolve()
        target = (output / relative).resolve()
        if not target.is_relative_to(output) or not target.is_file():
            return None
        return target

    def zip_output(self, job_id: str) -> bytes:
        output = self.root / job_id / "output"
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(output.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(output))
        return buffer.getvalue()

    # -- internals ----------------------------------------------------------

    def _update(self, job_id: str, **changes) -> None:
        with self._lock:
            job = self._jobs[job_id]
            for key, value in changes.items():
                setattr(job, key, value)
        # Progress ticks are frequent; only persist real state changes.
        if set(changes) != {"chars_written"}:
            self._save(job)

    def _run(self, job_id: str) -> None:
        job_dir = self.root / job_id
        update = lambda **changes: self._update(job_id, **changes)  # noqa: E731
        try:
            convert(job_dir / "source.psd", job_dir, update)
            update(status="done", message="Finished", finished_at=time.time())
        except GenerationError as exc:
            update(status="error", message="Failed", error=str(exc), finished_at=time.time())
        except Exception as exc:
            traceback.print_exc()
            update(
                status="error",
                message="Failed",
                error=f"Could not convert this file: {exc}",
                finished_at=time.time(),
            )

    def _save(self, job: Job) -> None:
        path = self.root / job.id / "status.json"
        path.write_text(json.dumps(asdict(job), indent=2))


def remove_tree(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)
