"""
Pipeline job orchestration for NewsIntel.

Runs fetch.py then classifier.py as subprocesses in a background thread,
tracks progress/logs/errors in memory, and persists completed runs to disk
so they can be revisited without re-running the pipeline.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


class JobError(Exception):
    """Raised when a pipeline step fails; carries the step name + stderr tail."""

    def __init__(self, step: str, message: str, returncode: int | None = None):
        self.step = step
        self.message = message
        self.returncode = returncode
        super().__init__(f"[{step}] {message}")


@dataclass
class Job:
    id: str
    query: str
    params: dict
    status: str = "queued"          # queued -> fetching -> classifying -> done | error
    step_label: str = "Queued"
    log: list[str] = field(default_factory=list)
    error: Optional[dict] = None    # {"step": ..., "message": ...}
    result_path: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "query": self.query,
            "status": self.status,
            "step_label": self.step_label,
            "log": self.log[-200:],  # cap for safety
            "error": self.error,
            "result_path": self.result_path,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
        }

    def append_log(self, line: str):
        line = line.rstrip("\n")
        if line:
            self.log.append(line)


class PipelineRunner:
    """
    Manages background pipeline jobs. Thread-safe enough for a single-user
    local dashboard (one dict + a lock) — not built for concurrent multi-user
    production load.
    """

    def __init__(self, app_root: Path, python_exe: str = sys.executable):
        self.app_root = app_root
        self.python_exe = python_exe
        self.fetch_script = app_root / "fetch.py"
        self.classifier_script = app_root / "classifier.py"
        self.runs_dir = app_root / "data" / "runs"
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    # -- public API ---------------------------------------------------

    def start_job(self, query: str, params: dict) -> Job:
        job = Job(id=uuid.uuid4().hex[:10], query=query, params=params)
        with self._lock:
            self._jobs[job.id] = job
        thread = threading.Thread(target=self._run_job, args=(job,), daemon=True)
        thread.start()
        return job

    def get_job(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(job_id)

    def list_runs(self) -> list[dict]:
        """List completed runs saved to disk, most recent first."""
        runs = []
        for p in sorted(self.runs_dir.glob("*.json"), reverse=True):
            try:
                with p.open("r", encoding="utf-8") as f:
                    data = json.load(f)
                runs.append({
                    "filename": p.name,
                    "path": str(p),
                    "query": data.get("query", "?"),
                    "generated_at": data.get("generated_at", ""),
                    "article_count": len(data.get("articles", [])),
                })
            except (json.JSONDecodeError, OSError):
                continue
        return runs

    # -- internals ------------------------------------------------------

    def _run_job(self, job: Job):
        if not self.fetch_script.exists():
            self._fail(job, "fetch", f"fetch.py not found at {self.fetch_script}")
            return
        if not self.classifier_script.exists():
            self._fail(job, "classifier", f"classifier.py not found at {self.classifier_script}")
            return

        fetch_output = self.runs_dir / f"_tmp_fetch_{job.id}.json"
        final_output = self.runs_dir / f"{int(time.time())}_{job.id}.json"

        # ---- Step 1: fetch.py ----
        job.status = "fetching"
        job.step_label = "Fetching articles from sources…"
        fetch_cmd = self._build_fetch_cmd(job.params, fetch_output)
        job.append_log("$ " + " ".join(fetch_cmd))
        try:
            self._run_subprocess(job, fetch_cmd, step="fetch")
        except JobError as e:
            self._fail(job, e.step, e.message, e.returncode)
            return

        if not fetch_output.exists():
            self._fail(job, "fetch", "fetch.py finished but produced no output file.")
            return

        # ---- Step 2: classifier.py ----
        job.status = "classifying"
        job.step_label = "Classifying articles…"
        classify_cmd = self._build_classify_cmd(job.params, fetch_output, final_output)
        job.append_log("$ " + " ".join(classify_cmd))
        try:
            self._run_subprocess(job, classify_cmd, step="classifier")
        except JobError as e:
            self._fail(job, e.step, e.message, e.returncode)
            return
        finally:
            fetch_output.unlink(missing_ok=True)

        if not final_output.exists():
            self._fail(job, "classifier", "classifier.py finished but produced no output file.")
            return

        # sanity check the output is valid JSON with articles
        try:
            with final_output.open("r", encoding="utf-8") as f:
                json.load(f)
        except json.JSONDecodeError as e:
            self._fail(job, "classifier", f"Output file is not valid JSON: {e}")
            return

        job.status = "done"
        job.step_label = "Done"
        job.result_path = str(final_output)
        job.finished_at = time.time()

    def _build_fetch_cmd(self, params: dict, output_path: Path) -> list[str]:
        cmd = [self.python_exe, str(self.fetch_script), "-q", params["query"]]
        if params.get("sources"):
            cmd += ["-s", params["sources"]]
        cmd += ["-o", str(output_path)]

        date_mode = params.get("date_mode", "since")
        if date_mode == "since" and params.get("since"):
            cmd += ["--since", params["since"]]
        elif date_mode == "range":
            if params.get("from_date"):
                cmd += ["--from", params["from_date"]]
            if params.get("to_date"):
                cmd += ["--to", params["to_date"]]

        if params.get("no_scrape_fallback"):
            cmd += ["--no-scrape-fallback"]
        if params.get("no_dedup"):
            cmd += ["--no-dedup"]
        else:
            if params.get("dedup_threshold") is not None:
                cmd += ["--dedup-threshold", str(params["dedup_threshold"])]
            if params.get("dedup_title_weight") is not None:
                cmd += ["--dedup-title-weight", str(params["dedup_title_weight"])]
            if params.get("dedup_content_weight") is not None:
                cmd += ["--dedup-content-weight", str(params["dedup_content_weight"])]
        cmd += ["--verbose"]
        return cmd

    def _build_classify_cmd(self, params: dict, input_path: Path, output_path: Path) -> list[str]:
        cmd = [
            self.python_exe, str(self.classifier_script),
            "--input", str(input_path),
            "--output", str(output_path),
        ]
        if params.get("categories"):
            cmd += ["--categories", *params["categories"]]
        if params.get("batch_size"):
            cmd += ["--batch-size", str(params["batch_size"])]
        if params.get("description_limit") is not None:
            cmd += ["--description-limit", str(params["description_limit"])]
        if params.get("provider"):
            cmd += ["--provider", params["provider"]]
        return cmd

    def _run_subprocess(self, job: Job, cmd: list[str], step: str, timeout: int = 600):
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=str(self.app_root),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except OSError as e:
            raise JobError(step, f"Failed to launch {step}: {e}")

        start = time.time()
        stderr_tail = []
        assert proc.stdout is not None
        for line in proc.stdout:
            job.append_log(line)
            stderr_tail.append(line)
            stderr_tail[:] = stderr_tail[-40:]
            if time.time() - start > timeout:
                proc.kill()
                raise JobError(step, f"{step} timed out after {timeout}s.")

        returncode = proc.wait()
        if returncode != 0:
            tail = "".join(stderr_tail).strip() or "(no output captured)"
            raise JobError(step, f"Exited with code {returncode}.\n{tail}", returncode)

    def _fail(self, job: Job, step: str, message: str, returncode: int | None = None):
        job.status = "error"
        job.step_label = f"Failed during {step}"
        job.error = {"step": step, "message": message, "returncode": returncode}
        job.finished_at = time.time()
        job.append_log(f"ERROR [{step}]: {message}")
