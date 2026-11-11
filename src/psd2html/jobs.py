"""Conversion jobs: PSD in, folder out.

Two kinds of job share the store:
  html        index.html + assets/ (the original tool)
  photocard   photocard.json + assets/ + preview.png (what Palette imports)

Each job lives in its own directory:
  <job>/source.psd
  <job>/work/      design.png, slices, layers.json (inputs for the model)
  <job>/output/    the result
  <job>/status.json
"""

from __future__ import annotations

import io
import json
import mimetypes
import os
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
JOB_KINDS = ("html", "photocard")
DEFAULT_TTL_HOURS = 24


@dataclass
class Job:
    id: str
    filename: str
    kind: str = "html"
    status: str = "queued"  # queued | extracting | generating | assembling | done | error
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
    missing_fonts: list[str] = field(default_factory=list)
    error: str | None = None


def convert(psd_path: Path, job_dir: Path, update=lambda **_: None) -> Path:
    """The HTML pipeline. Returns the output directory."""
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


def convert_photocard(psd_path: Path, job_dir: Path, update=lambda **_: None, *, file_name: str | None = None) -> Path:
    """The photocard pipeline. Returns the output directory."""
    from .assemble import AssemblyError, assemble
    from .candidates import candidates, flatten
    from .decide import decide_photocard
    from .spec import validate_spec

    work_dir, output_dir = job_dir / "work", job_dir / "output"
    update(status="extracting", message="Reading layers from the PSD")
    extraction = extract(psd_path, work_dir, output_dir)
    flats = flatten(extraction)
    items = candidates(extraction, flats)
    (work_dir / "candidates.json").write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
    update(
        canvas=[extraction.width, extraction.height],
        layers=_count(extraction.layers),
        assets=len(extraction.assets),
        warnings=list(extraction.warnings),
        status="generating",
        message="Claude is reading the design",
    )

    decided = decide_photocard(extraction, items, on_progress=lambda n: update(chars_written=n))
    (work_dir / "decisions.json").write_text(decided.decisions.model_dump_json(indent=2), encoding="utf-8")
    update(
        status="assembling",
        message="Building the photocard",
        model=decided.model,
        input_tokens=decided.input_tokens,
        output_tokens=decided.output_tokens,
    )
    try:
        spec = assemble(
            extraction,
            decided.decisions,
            file_name=file_name or psd_path.name,
            model=decided.model,
            output_dir=output_dir,
        )
    except AssemblyError as exc:
        raise GenerationError(f"Could not read this design as a photocard: {exc}") from exc
    problems = validate_spec(spec, output_dir / "assets")
    if problems:
        raise GenerationError("The photocard is incomplete: " + "; ".join(problems))
    update(warnings=list(spec.warnings), missing_fonts=list(spec.missingFonts))
    return output_dir


def _count(layers: list[dict]) -> int:
    return sum(1 + _count(layer.get("children", [])) for layer in layers)


class JobStore:
    def __init__(self, root: Path, workers: int = 2, ttl_hours: float | None = None):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="psd2html")
        self.ttl_seconds = (DEFAULT_TTL_HOURS if ttl_hours is None else ttl_hours) * 3600
        self._sweeper: threading.Thread | None = None

    # -- public -----------------------------------------------------------

    def create(self, filename: str, kind: str = "html") -> tuple[Job, Path]:
        if kind not in JOB_KINDS:
            raise ValueError(f"unknown job kind {kind!r}")
        job = Job(id=uuid.uuid4().hex[:12], filename=filename, kind=kind)
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
                data = json.loads(status.read_text(encoding="utf-8"))
                job = Job(**{k: v for k, v in data.items() if k in Job.__dataclass_fields__})
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

    def result_manifest(self, job_id: str) -> dict:
        """The photocard spec plus every file Palette should copy."""
        output = self.root / job_id / "output"
        spec = json.loads((output / "photocard.json").read_text(encoding="utf-8"))
        files = []
        for path in sorted(output.rglob("*")):
            if not path.is_file() or path.name == "photocard.json":
                continue
            relative = path.relative_to(output).as_posix()
            content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            files.append({"path": relative, "bytes": path.stat().st_size, "contentType": content_type})
        return {"spec": spec, "files": files}

    def zip_output(self, job_id: str) -> bytes:
        output = self.root / job_id / "output"
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(output.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(output))
        return buffer.getvalue()

    def delete(self, job_id: str) -> bool:
        if not JOB_ID.match(job_id):
            return False
        with self._lock:
            job = self._jobs.pop(job_id, None)
        path = self.root / job_id
        existed = path.is_dir() or job is not None
        remove_tree(path)
        return existed

    def start_sweeper(self) -> None:
        """Remove job directories older than the TTL, once an hour."""
        if self._sweeper is not None or self.ttl_seconds <= 0:
            return
        self._sweeper = threading.Thread(target=self._sweep_forever, name="psd2html-sweeper", daemon=True)
        self._sweeper.start()

    def sweep(self, now: float | None = None) -> int:
        now = time.time() if now is None else now
        removed = 0
        for path in self.root.iterdir():
            if not path.is_dir() or not JOB_ID.match(path.name):
                continue
            try:
                age = now - path.stat().st_mtime
            except OSError:
                continue
            with self._lock:
                job = self._jobs.get(path.name)
            if job is not None and job.status not in ("done", "error"):
                continue  # still running
            if age > self.ttl_seconds:
                self.delete(path.name)
                removed += 1
        return removed

    # -- internals ----------------------------------------------------------

    def _sweep_forever(self) -> None:
        while True:
            time.sleep(3600)
            try:
                self.sweep()
            except Exception:
                traceback.print_exc()

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
        with self._lock:
            job = self._jobs[job_id]
        update = lambda **changes: self._update(job_id, **changes)  # noqa: E731
        try:
            if job.kind == "photocard":
                convert_photocard(job_dir / "source.psd", job_dir, update, file_name=job.filename)
            else:
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
        if not path.parent.is_dir():
            return  # deleted underneath a late progress tick
        path.write_text(json.dumps(asdict(job), indent=2), encoding="utf-8")


def remove_tree(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


def ttl_hours_from_env() -> float:
    try:
        return float(os.environ.get("PSD2HTML_JOB_TTL_HOURS", DEFAULT_TTL_HOURS))
    except ValueError:
        return DEFAULT_TTL_HOURS
