"""``agentbisect serve``: a small local web app over the same engine as the CLI.

The page lets you name a repo and a prompt, prepares a run (clone, install), and later bisects the
finished run and shows the report. It cannot start the agent; that step stays manual.

SECURITY: this server clones repos, installs their dependencies and runs their tests. So it
  * binds to 127.0.0.1 only,
  * needs a random per-process token (header ``X-AgentBisect-Token``) on every ``/api/*`` call,
  * checks ``Host`` and ``Origin`` (DNS rebinding and cross-site requests),
  * accepts JSON POST bodies up to 1 MB,
  * validates inputs the way the CLI does (https://, file:// or a local directory),
  * serves only ``/runs/<id>/report.html``, ``/runs/<id>/PROMPT.txt`` and ``/runs/study/study.html``,
  * never logs request bodies or the token.

Standard library only. Jobs run one at a time on a single worker thread.
"""

from __future__ import annotations

import contextlib
import hmac
import json
import queue
import re
import secrets
import sys
import threading
import time
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from pydantic import ValidationError

from agentbisect.models import SabotageSpec
from agentbisect.workspace import GIT_SCHEMES

TOKEN_HEADER = "X-AgentBisect-Token"
MAX_BODY_BYTES = 1_000_000
MAX_DRAIN_BYTES = 4_000_000  # read and drop at most this much of an oversized body
MAX_JOBS = 50
LOG_API_CHARS = 20_000
LOG_KEEP_CHARS = 100_000
MAX_PROMPT_CHARS = 50_000
SOCKET_TIMEOUT_S = 30
RUN_ID = re.compile(r"^[A-Za-z0-9._-]+$")
JOB_ID = re.compile(r"^[0-9a-f]{16}$")
STATIC_FILES = {"report.html": "text/html", "PROMPT.txt": "text/plain"}
STATIC_PATH = re.compile(r"^/runs/([A-Za-z0-9._-]+)/(report\.html|PROMPT\.txt)$")
CSP = (
    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
    "img-src data:; connect-src 'self'; base-uri 'none'; form-action 'none'"
)
_URL_USERINFO = re.compile(r"(://)[^/@\s]+@")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")

PLACEHOLDER_PAGE = (
    "<!doctype html><html lang=en><head><meta charset=utf-8>"
    "<meta name=viewport content='width=device-width,initial-scale=1'>"
    "<title>AgentBisect</title></head><body><h1>AgentBisect</h1>"
    "<p>The server is running. The page is not installed yet.</p></body></html>"
)


class BadRequest(ValueError):
    """The client sent something the server will not accept. The message is safe to show."""


class JobError(RuntimeError):
    """A job failed for a reason the user can read."""


def redact(text: str) -> str:
    """Hide ``user:password@`` in URLs before text reaches a job log or an API reply."""
    return _URL_USERINFO.sub(r"\1***@", text)


# --------------------------------------------------------------------------- validation


@dataclass(frozen=True)
class RecordRequest:
    repo: str
    prompt: str
    model: str | None
    ref: str | None
    setup: bool
    sabotage: SabotageSpec | None


def _text_field(body: dict[str, Any], key: str, *, required: bool, limit: int) -> str | None:
    value = body.get(key)
    if value is None or value == "":
        if required:
            raise BadRequest(f"{key} is required.")
        return None
    if not isinstance(value, str):
        raise BadRequest(f"{key} must be a string.")
    if len(value) > limit:
        raise BadRequest(f"{key} is too long (limit {limit} characters).")
    return value


def _no_dash_or_control(key: str, value: str) -> None:
    if value.startswith("-"):
        raise BadRequest(f"{key} must not start with '-'.")
    if _CONTROL.search(value):
        raise BadRequest(f"{key} must not contain control characters.")


def _validate_repo(repo: str) -> str:
    repo = repo.strip()
    _no_dash_or_control("repo", repo)
    if repo.startswith(GIT_SCHEMES):
        if len(repo.split("://", 1)[1]) == 0 or any(c.isspace() for c in repo):
            raise BadRequest("repo is not a valid URL.")
        return repo
    if not Path(repo).expanduser().is_dir():
        raise BadRequest("repo must be an https:// or file:// URL, or an existing local directory.")
    return repo


def _validate_sabotage(raw: Any) -> SabotageSpec:
    if not isinstance(raw, dict):
        raise BadRequest("sabotage must be an object.")
    unknown = set(raw) - {"at_step", "file", "find", "replace"}
    if unknown:
        raise BadRequest(f"sabotage has unknown fields: {', '.join(sorted(unknown))}.")
    at_step = raw.get("at_step")
    if isinstance(at_step, bool) or not isinstance(at_step, int) or at_step < 1:
        raise BadRequest("sabotage.at_step must be an integer of 1 or more.")
    for key in ("file", "find", "replace"):
        if not isinstance(raw.get(key), str):
            raise BadRequest(f"sabotage.{key} must be a string.")
    file = raw["file"]
    if not file.strip() or not raw["find"]:
        raise BadRequest("sabotage.file and sabotage.find must not be empty.")
    parts = file.replace("\\", "/").split("/")
    if file.startswith(("/", "~", "-")) or ".." in parts or _CONTROL.search(file):
        raise BadRequest("sabotage.file must be a relative path inside the repo, without '..'.")
    if len(raw["find"]) > 20_000 or len(raw["replace"]) > 20_000:
        raise BadRequest("sabotage.find and sabotage.replace are too long.")
    try:
        return SabotageSpec(at_step=at_step, file=file, find=raw["find"], replace=raw["replace"])
    except ValidationError as e:
        raise BadRequest(f"sabotage is invalid: {e.errors()[0]['msg']}") from e


def parse_record_request(body: Any) -> RecordRequest:
    """Check a ``POST /api/record`` body the way the CLI checks its options."""
    if not isinstance(body, dict):
        raise BadRequest("The body must be a JSON object.")
    unknown = set(body) - {"repo", "prompt", "model", "ref", "no_setup", "sabotage"}
    if unknown:
        raise BadRequest(f"Unknown fields: {', '.join(sorted(unknown))}.")
    repo = _validate_repo(_text_field(body, "repo", required=True, limit=2048) or "")
    prompt = _text_field(body, "prompt", required=True, limit=MAX_PROMPT_CHARS) or ""
    if not prompt.strip():
        raise BadRequest("The prompt is empty.")
    model = _text_field(body, "model", required=False, limit=100)
    if model is not None:
        _no_dash_or_control("model", model)
    ref = _text_field(body, "ref", required=False, limit=200)
    if ref is not None:
        _no_dash_or_control("ref", ref)
        if any(c.isspace() for c in ref):
            raise BadRequest("ref must not contain spaces.")
    no_setup = body.get("no_setup", False)
    if no_setup is None:
        no_setup = False
    if not isinstance(no_setup, bool):
        raise BadRequest("no_setup must be true or false.")
    sabotage = _validate_sabotage(body["sabotage"]) if body.get("sabotage") is not None else None
    return RecordRequest(repo, prompt, model, ref, not no_setup, sabotage)


# --------------------------------------------------------------------------- runs on disk


def run_dir_for(runs_dir: Path, run_id: str) -> Path | None:
    """The run directory for ``run_id``, or None. A run is a direct child of ``runs_dir``, not
    a symlink, with a trace. Never builds a path from anything but the checked id."""
    if not RUN_ID.fullmatch(run_id) or run_id.startswith("."):
        return None
    candidate = runs_dir / run_id
    try:
        resolved = candidate.resolve(strict=True)
    except OSError:
        return None
    if candidate.is_symlink() or resolved.parent != runs_dir or not resolved.is_dir():
        return None
    if not (resolved / "meta.json").is_file() or not (resolved / "trace.jsonl").is_file():
        return None
    return resolved


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _count_steps(run_dir: Path) -> int:
    try:
        with (run_dir / "trace.jsonl").open("rb") as f:
            return sum(1 for line in f if line.strip())
    except OSError:
        return 0


def verdict_text(bisect: Any) -> str | None:
    """A short verdict from ``bisect.json``: the same facts the CLI and report state."""
    if not isinstance(bisect, dict):
        return None
    found = sorted(
        c["bisect"]["decisive_step"]
        for c in bisect.get("culprits") or []
        if isinstance(c, dict)
        and c.get("bisect", {}).get("status") == "found"
        and isinstance(c["bisect"].get("decisive_step"), int)
    )
    if found:
        return f"culprit at step {found[0]}"
    repaired = [e for e in bisect.get("repaired") or [] if isinstance(e, dict)]
    if repaired:
        steps = sorted(e["t_err"] for e in repaired if isinstance(e.get("t_err"), int))
        return f"caught and fixed at step {steps[0]}" if steps else "caught and fixed"
    if bisect.get("culprits"):
        return "not bisectable"
    return "no failing check"


def run_summary(run_dir: Path) -> dict[str, Any] | None:
    meta = _read_json(run_dir / "meta.json")
    if not isinstance(meta, dict):
        return None
    task = _read_json(run_dir / "task.json")
    origin = meta.get("repo_origin")
    title = task.get("title") if isinstance(task, dict) else None
    if not isinstance(title, str) or not title or title == "own repo":
        name = origin.rstrip("/").rsplit("/", 1)[-1] if isinstance(origin, str) else ""
        title = name.removesuffix(".git") or run_dir.name
    bisect = _read_json(run_dir / "bisect.json")
    return {
        "id": run_dir.name,
        "title": redact(title),
        "source": meta.get("source"),
        "repo_origin": redact(origin) if isinstance(origin, str) else None,
        "task_id": meta.get("task_id"),
        "model": meta.get("model"),
        "steps": _count_steps(run_dir),
        "finished": bool(meta.get("finished")),
        "has_bisect": bisect is not None,
        "has_report": (run_dir / "report.html").is_file(),
        "verdict": verdict_text(bisect),
    }


def list_runs(runs_dir: Path) -> list[dict[str, Any]]:
    found: list[tuple[str, dict[str, Any]]] = []
    try:
        children = list(runs_dir.iterdir())
    except OSError:
        return []
    for child in children:
        run_dir = run_dir_for(runs_dir, child.name)
        summary = run_summary(run_dir) if run_dir else None
        if summary is None:
            continue
        meta = _read_json(run_dir / "meta.json") or {}  # type: ignore[union-attr]
        stamp = str(meta.get("created_at") or "")
        found.append((stamp, summary))
    found.sort(key=lambda pair: (pair[0], pair[1]["id"]), reverse=True)
    return [summary for _, summary in found]


def run_details(run_dir: Path) -> dict[str, Any]:
    meta = _read_json(run_dir / "meta.json") or {}
    try:
        prompt = (run_dir / "PROMPT.txt").read_text(encoding="utf-8", errors="replace")
    except OSError:
        prompt = ""
    origin = meta.get("repo_origin")
    return {
        "id": run_dir.name,
        "workdir": str(meta.get("workdir") or ""),
        "prompt": prompt,
        "model": meta.get("model"),
        "repo_origin": redact(origin) if isinstance(origin, str) else None,
        "finished": bool(meta.get("finished")),
        "steps": _count_steps(run_dir),
        "has_report": (run_dir / "report.html").is_file(),
        "has_bisect": (run_dir / "bisect.json").is_file(),
    }


# --------------------------------------------------------------------------- jobs


@dataclass
class Job:
    id: str
    kind: str
    status: str = "queued"
    run_id: str | None = None
    error: str | None = None
    _log: str = ""
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def log(self, text: str) -> None:
        text = redact(text)
        if not text.endswith("\n"):
            text += "\n"
        with self._lock:
            self._log = (self._log + text)[-LOG_KEEP_CHARS:]

    def view(self) -> dict[str, Any]:
        with self._lock:
            log = self._log[-LOG_API_CHARS:]
        return {
            "id": self.id,
            "kind": self.kind,
            "status": self.status,
            "log": log,
            "run_id": self.run_id,
            "error": self.error,
        }


class JobQueue:
    """One worker thread runs jobs in order, so two installs never overlap."""

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._queue: queue.Queue[tuple[Job, Callable[[Job], None]] | None] = queue.Queue()
        self._thread = threading.Thread(target=self._work, name="agentbisect-jobs", daemon=True)
        self._thread.start()

    def submit(self, kind: str, fn: Callable[[Job], None], run_id: str | None = None) -> Job:
        job = Job(id=secrets.token_hex(8), kind=kind, run_id=run_id)
        with self._lock:
            self._jobs[job.id] = job
            self._evict()
        self._queue.put((job, fn))
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def active_for(self, kind: str, run_id: str) -> Job | None:
        with self._lock:
            for job in self._jobs.values():
                if (
                    job.kind == kind
                    and job.run_id == run_id
                    and job.status in ("queued", "running")
                ):
                    return job
        return None

    def _evict(self) -> None:
        # Dicts keep insertion order: drop the oldest finished jobs first.
        while len(self._jobs) > MAX_JOBS:
            victim = next((j for j in self._jobs.values() if j.status in ("done", "error")), None)
            if victim is None:
                break
            del self._jobs[victim.id]

    def _work(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            job, fn = item
            job.status = "running"
            try:
                fn(job)
                job.status = "done"
            except JobError as e:
                job.error = redact(str(e))[:500]
                job.log(f"Failed: {job.error}")
                job.status = "error"
            except Exception as e:  # last line only: no traceback reaches the client
                job.error = redact(f"{type(e).__name__}: {e}".splitlines()[0])[:500]
                job.log(f"Failed: {job.error}")
                job.status = "error"

    def close(self) -> None:
        self._queue.put(None)


def _tail_file(path: Path, job: Job, stop: threading.Event) -> None:
    """Copy new text from ``setup.log`` into the job log while the install runs."""
    offset = 0
    while True:
        done = stop.wait(1.0)
        try:
            with path.open("rb") as f:
                f.seek(offset)
                chunk = f.read(200_000)
            offset += len(chunk)
            if chunk:
                job.log(chunk.decode("utf-8", errors="replace"))
        except OSError:
            pass
        if done:
            return


def _log_tail(path: Path, chars: int = 3000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[-chars:]
    except OSError:
        return ""


def _fresh_run_id(runs_dir: Path, req: RecordRequest) -> str:
    from agentbisect.hooks.opencode import _default_run_id, _repo_name

    base = _default_run_id(_repo_name(req.repo), req.model)
    run_id, n = base, 1
    while (runs_dir / run_id).exists():
        n += 1
        run_id = f"{base}-{n}"
    return run_id


def run_record_job(runs_dir: Path, req: RecordRequest) -> Callable[[Job], None]:
    def work(job: Job) -> None:
        from agentbisect.hooks import opencode
        from agentbisect.workspace import SetupError

        run_id = _fresh_run_id(runs_dir, req)
        what = (
            "Cloning or copying the repo and installing dependencies"
            if req.setup
            else ("Cloning or copying the repo")
        )
        job.log(f"Starting run {run_id}.")
        job.log(f"{what}. This can take a few minutes.")
        stop = threading.Event()
        tailer = threading.Thread(
            target=_tail_file, args=(runs_dir / run_id / "setup.log", job, stop), daemon=True
        )
        tailer.start()
        try:
            run_dir = Path(
                opencode.prepare_repo_run(
                    req.repo,
                    req.prompt,
                    runs_dir,
                    model_alias=req.model,
                    run_id=run_id,
                    ref=req.ref,
                    setup=req.setup,
                    sabotage=req.sabotage,
                )
            )
        except SetupError as e:
            stop.set()
            tailer.join(timeout=5)
            raise JobError(f"Setup failed: {e.message}") from e
        except BaseException:
            stop.set()
            tailer.join(timeout=5)
            raise
        stop.set()
        tailer.join(timeout=5)
        job.run_id = run_dir.name
        job.log(f"Prepared run {run_dir.name}.")
        job.log("Next: start the agent in the Alan app, using the prompt shown on the page.")

    return work


def run_analyze_job(runs_dir: Path, run_dir: Path) -> Callable[[Job], None]:
    def work(job: Job) -> None:
        from agentbisect.bisect import bisect_run
        from agentbisect.models import Trace
        from agentbisect.report.render import render_report
        from agentbisect.report.stable import write_json_stable

        if (run_dir / "events.jsonl").exists():
            from agentbisect.hooks.opencode import finalize_run

            job.log("Merging the agent's events into the trace.")
            if finalize_run(run_dir).get("resumed_conversation"):
                job.log(
                    "Warning: Alan resumed an earlier conversation into this run, so the agent "
                    "also saw an old task. Do not use it as study data."
                )
        trace = Trace.load(run_dir)
        if trace.n == 0:
            raise JobError("The trace has no steps.")
        job.log(f"Bisecting {trace.n} steps. This runs the tests on saved snapshots.")
        started = time.monotonic()
        report = bisect_run(trace, run_dir, None)
        write_json_stable(run_dir / "bisect.json", report.model_dump_json(indent=2))
        job.log(
            f"Bisect finished in {time.monotonic() - started:.0f}s: {verdict_text(report.model_dump(mode='json'))}."
        )
        job.log("Writing the report.")
        render_report(run_dir)
        job.log("Done.")

    return work


# --------------------------------------------------------------------------- server


class AgentBisectServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, runs_dir: Path, port: int = 0, token: str | None = None) -> None:
        self.runs_dir = Path(runs_dir).resolve()
        self.token = token or secrets.token_urlsafe(24)
        self.jobs = JobQueue()
        super().__init__(("127.0.0.1", port), Handler)

    @property
    def port(self) -> int:
        return self.server_address[1]

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/#token={self.token}"

    def server_close(self) -> None:
        super().server_close()
        self.jobs.close()


class Handler(BaseHTTPRequestHandler):
    server: AgentBisectServer
    timeout = SOCKET_TIMEOUT_S
    server_version = "AgentBisect"
    sys_version = ""

    # -- logging: method, path without query, status. Never bodies or the token. --

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        sys.stderr.write(f"[serve] {format % args}\n")

    def log_request(self, code: Any = "-", size: Any = "-") -> None:
        path = (self.path or "").split("?", 1)[0].split("#", 1)[0][:200]
        code = code.value if isinstance(code, HTTPStatus) else code
        self.log_message("%s %s %s", self.command, _CONTROL.sub("?", path), code)

    # -- sending --

    def _send(
        self,
        status: int,
        body: bytes,
        content_type: str,
        *,
        csp: bool = False,
        close: bool = False,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        if csp:
            self.send_header("Content-Security-Policy", CSP)
        if close:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, payload: Any, **kw: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8", **kw)

    def _error(self, status: int, message: str, **kw: Any) -> None:
        self._json(status, {"error": message}, **kw)

    # -- request checks --

    def _allowed_hosts(self) -> set[str]:
        port = self.server.port
        return {f"127.0.0.1:{port}", f"localhost:{port}"}

    def _check_origin(self) -> bool:
        host = self.headers.get("Host")
        if host not in self._allowed_hosts():
            self._error(403, "Bad Host header.")
            return False
        origin = self.headers.get("Origin")
        if origin is not None and origin not in {f"http://{h}" for h in self._allowed_hosts()}:
            self._error(403, "Bad Origin header.")
            return False
        return True

    def _check_token(self) -> bool:
        given = self.headers.get(TOKEN_HEADER, "")
        ok = hmac.compare_digest(
            given.encode("latin-1", "replace"), self.server.token.encode("latin-1", "replace")
        )
        if not ok:
            self._error(403, "Missing or wrong token.")
        return ok

    def _read_json_body(self) -> Any | None:
        """The parsed body, or None after sending an error."""
        raw_len = self.headers.get("Content-Length")
        if raw_len is None or self.headers.get("Transfer-Encoding"):
            self._error(411, "Send a Content-Length header.", close=True)
            return None
        try:
            length = int(raw_len)
            if length < 0:
                raise ValueError
        except ValueError:
            self._error(400, "Bad Content-Length.", close=True)
            return None
        ctype = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if ctype != "application/json":
            self._drain(length)
            self._error(415, "Send Content-Type: application/json.", close=True)
            return None
        if length > MAX_BODY_BYTES:
            self._drain(length)
            self._error(413, "The body is larger than 1 MB.", close=True)
            return None
        data = self.rfile.read(length) if length else b""
        if not data.strip():
            return {}
        try:
            return json.loads(data)
        except ValueError:
            self._error(400, "The body is not valid JSON.")
            return None

    def _drain(self, length: int) -> None:
        """Read and drop an oversized body so the client can read our answer."""
        left = min(length, MAX_DRAIN_BYTES)
        try:
            while left > 0:
                chunk = self.rfile.read(min(65536, left))
                if not chunk:
                    return
                left -= len(chunk)
        except OSError:
            return

    # -- verbs --

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def _other(self) -> None:
        if self._check_origin():
            self._error(405, "Method not allowed.")

    do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = _other  # noqa: N815

    def _dispatch(self, method: str) -> None:
        if not self._check_origin():
            return
        raw = self.path.split("?", 1)[0].split("#", 1)[0]
        if not raw.startswith("/"):
            self._error(400, "Bad request path.")
            return
        path = unquote(raw)
        try:
            if path.startswith("/api/") or path == "/api":
                if not self._check_token():
                    return
                self._api(method, path)
            elif method == "GET":
                self._static(path)
            else:
                self._error(405, "Method not allowed.")
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as e:  # never send a traceback; log the type only
            sys.stderr.write(f"[serve] internal error: {type(e).__name__}\n")
            with contextlib.suppress(OSError):
                self._error(500, "Internal error.")

    # -- static --

    def _static(self, path: str) -> None:
        if path == "/":
            self._send(200, self._page().encode("utf-8"), "text/html; charset=utf-8", csp=True)
            return
        runs_dir = self.server.runs_dir
        if path == "/runs/study/study.html":
            run_id, name, ctype = "study", "study.html", "text/html"
        else:
            m = STATIC_PATH.fullmatch(path)
            if not m:
                self._error(404, "Not found.")
                return
            run_id, name = m.group(1), m.group(2)
            ctype = STATIC_FILES[name]
        if run_id.startswith("."):
            self._error(404, "Not found.")
            return
        try:
            base = (runs_dir / run_id).resolve(strict=True)
            file = (base / name).resolve(strict=True)
        except OSError:
            self._error(404, "Not found.")
            return
        if (
            (runs_dir / run_id).is_symlink()
            or base.parent != runs_dir
            or file.parent != base
            or not file.is_file()
        ):
            self._error(404, "Not found.")
            return
        try:
            data = file.read_bytes()
        except OSError:
            self._error(404, "Not found.")
            return
        self._send(200, data, f"{ctype}; charset=utf-8", csp=ctype == "text/html")

    @staticmethod
    def _page() -> str:
        try:
            from agentbisect.report.serve_page import render_serve_page
        except ImportError:
            return PLACEHOLDER_PAGE
        return render_serve_page()

    # -- api --

    def _api(self, method: str, path: str) -> None:
        runs_dir = self.server.runs_dir
        parts = path.strip("/").split("/")  # ["api", ...]
        if method == "GET":
            if parts == ["api", "runs"]:
                self._json(200, {"runs": list_runs(runs_dir)})
                return
            if len(parts) == 3 and parts[:2] == ["api", "runs"]:
                run_dir = run_dir_for(runs_dir, parts[2])
                if run_dir is None:
                    self._error(404, "No such run.")
                else:
                    self._json(200, run_details(run_dir))
                return
            if len(parts) == 3 and parts[:2] == ["api", "jobs"]:
                job = self.server.jobs.get(parts[2]) if JOB_ID.fullmatch(parts[2]) else None
                if job is None:
                    self._error(404, "No such job.")
                else:
                    self._json(200, job.view())
                return
        elif method == "POST":
            if parts == ["api", "record"]:
                self._record()
                return
            if len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "analyze":
                self._analyze(parts[2])
                return
        self._error(404, "Not found.")

    def _record(self) -> None:
        body = self._read_json_body()
        if body is None:
            return
        try:
            req = parse_record_request(body)
        except BadRequest as e:
            self._error(400, str(e))
            return
        job = self.server.jobs.submit("record", run_record_job(self.server.runs_dir, req))
        self._json(202, {"job_id": job.id})

    def _analyze(self, run_id: str) -> None:
        body = self._read_json_body()
        if body is None:
            return
        run_dir = run_dir_for(self.server.runs_dir, run_id)
        if run_dir is None:
            self._error(404, "No such run.")
            return
        jobs = self.server.jobs
        job = jobs.active_for("analyze", run_dir.name)
        if job is None:
            job = jobs.submit(
                "analyze", run_analyze_job(self.server.runs_dir, run_dir), run_id=run_dir.name
            )
        self._json(202, {"job_id": job.id})


# --------------------------------------------------------------------------- entry point


def serve(runs_dir: Path, port: int = 8765, *, open_browser: bool = True) -> None:
    """Run the server until Ctrl-C."""
    runs_dir = Path(runs_dir)
    runs_dir.mkdir(parents=True, exist_ok=True)
    server = AgentBisectServer(runs_dir, port)
    print(f"AgentBisect is running at {server.url}", flush=True)
    print("Press Ctrl-C to stop.", flush=True)
    if open_browser:
        threading.Thread(target=webbrowser.open, args=(server.url,), daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.", flush=True)
    finally:
        server.server_close()
