"""``agentbisect serve``: security checks, validation, static files and the job lifecycle.

No network and no real clone or install: ``prepare_repo_run`` is replaced. The analyze test runs
the real bisect on a copy of a saved demo run (local pytest runs only).
"""

from __future__ import annotations

import http.client
import json
import shutil
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from agentbisect import serve as serve_mod
from agentbisect.cli import app
from agentbisect.models import Trace, TraceMeta, TraceSource
from agentbisect.serve import AgentBisectServer, JobQueue, redact

PROJECT = Path(__file__).resolve().parents[1]
DEMO = PROJECT / "runs" / "demo" / "caught-and-fixed"


class Api:
    """A tiny client for the test server (http.client, so tests can set Host and Origin)."""

    def __init__(self, server: AgentBisectServer) -> None:
        self.server = server
        self.port = server.port

    def call(
        self,
        method: str,
        path: str,
        body: Any = None,
        *,
        token: str | None = "default",
        headers: dict[str, str] | None = None,
        raw: bytes | None = None,
        content_type: str | None = "application/json",
    ) -> tuple[int, dict[str, str], bytes]:
        h: dict[str, str] = {}
        if token == "default":
            token = self.server.token
        if token is not None:
            h["X-AgentBisect-Token"] = token
        data = raw
        if data is None and body is not None:
            data = json.dumps(body).encode()
        if method == "POST":
            h["Content-Length"] = str(len(data or b""))
            if content_type:
                h["Content-Type"] = content_type
        h.update(headers or {})
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            conn.request(method, path, body=data, headers=h)
            resp = conn.getresponse()
            payload = resp.read()
            return resp.status, {k.lower(): v for k, v in resp.getheaders()}, payload
        finally:
            conn.close()

    def json(self, method: str, path: str, body: Any = None, **kw: Any) -> tuple[int, Any]:
        status, _, payload = self.call(method, path, body, **kw)
        return status, json.loads(payload) if payload else None

    def wait_job(self, job_id: str, timeout: float = 120.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status, job = self.json("GET", f"/api/jobs/{job_id}")
            assert status == 200
            if job["status"] in ("done", "error"):
                return job
            time.sleep(0.05)
        raise AssertionError(f"job {job_id} did not finish")


@pytest.fixture
def make_server(tmp_path: Path) -> Iterator[Callable[[], tuple[Api, Path]]]:
    servers: list[AgentBisectServer] = []

    def make() -> tuple[Api, Path]:
        runs = tmp_path / "runs"
        runs.mkdir(exist_ok=True)
        server = AgentBisectServer(runs, 0)
        servers.append(server)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return Api(server), runs

    yield make
    for server in servers:
        server.shutdown()
        server.server_close()


@pytest.fixture
def api(make_server: Callable[[], tuple[Api, Path]]) -> Api:
    return make_server()[0]


@pytest.fixture
def api_runs(make_server: Callable[[], tuple[Api, Path]]) -> tuple[Api, Path]:
    return make_server()


def fake_run(runs: Path, run_id: str, *, prompt: str = "do the thing") -> Path:
    run_dir = runs / run_id
    run_dir.mkdir()
    workdir = run_dir / "workdir"
    workdir.mkdir()
    meta = TraceMeta(
        run_id=run_id,
        source=TraceSource.ALAN_CODE,
        workdir=workdir,
        base_snapshot="0" * 40,
        model="flash",
        repo_origin="https://user:secret@example.com/acme/widgets.git",
    )
    Trace(meta=meta).save(run_dir)
    (run_dir / "PROMPT.txt").write_text(prompt)
    return run_dir


GOOD = {"repo": "https://example.com/acme/widgets.git", "prompt": "fix the bug", "model": "flash"}


# --------------------------------------------------------------------------- access control


def test_api_needs_the_token(api: Api) -> None:
    assert api.call("GET", "/api/runs", token=None)[0] == 403
    assert api.call("GET", "/api/runs", token="wrong")[0] == 403
    assert api.call("GET", "/api/runs", token="")[0] == 403
    assert api.call("GET", "/api/jobs/0123456789abcdef", token=None)[0] == 403
    assert api.call("POST", "/api/record", GOOD, token=None)[0] == 403
    assert api.call("POST", "/api/record", GOOD, token="wrong")[0] == 403
    assert api.call("POST", "/api/runs/x/analyze", {}, token=None)[0] == 403
    assert api.call("GET", "/api/nothing", token=None)[0] == 403  # no token, no information
    status, body = api.json("GET", "/api/runs")
    assert (status, body) == (200, {"runs": []})


def test_page_needs_no_token_and_has_security_headers(api: Api) -> None:
    status, headers, body = api.call("GET", "/", token=None)
    assert status == 200
    assert b"<html" in body.lower()
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["cache-control"] == "no-store"
    csp = headers["content-security-policy"]
    assert "default-src 'none'" in csp and "connect-src 'self'" in csp
    assert "form-action 'none'" in csp and "base-uri 'none'" in csp


def test_server_listens_on_loopback_only(api: Api) -> None:
    assert api.server.server_address[0] == "127.0.0.1"


@pytest.mark.parametrize("host", ["evil.example.com", "127.0.0.1:1", "localhost", "127.0.0.1", ""])
def test_bad_host_is_refused(api: Api, host: str) -> None:
    assert api.call("GET", "/api/runs", headers={"Host": host})[0] == 403
    assert api.call("GET", "/", headers={"Host": host})[0] == 403
    assert api.call("POST", "/api/record", GOOD, headers={"Host": host})[0] == 403


def test_localhost_host_is_accepted(api: Api) -> None:
    host = f"localhost:{api.port}"
    assert api.call("GET", "/api/runs", headers={"Host": host})[0] == 200


@pytest.mark.parametrize(
    "origin", ["http://evil.example.com", "null", "https://127.0.0.1:{port}", "http://127.0.0.1"]
)
def test_bad_origin_is_refused(api: Api, origin: str) -> None:
    origin = origin.format(port=api.port)
    assert api.call("GET", "/api/runs", headers={"Origin": origin})[0] == 403
    assert api.call("POST", "/api/record", GOOD, headers={"Origin": origin})[0] == 403


def test_local_origins_are_accepted(api: Api) -> None:
    for origin in (f"http://127.0.0.1:{api.port}", f"http://localhost:{api.port}"):
        assert api.call("GET", "/api/runs", headers={"Origin": origin})[0] == 200


def test_post_must_be_json(api: Api) -> None:
    status, _, _ = api.call("POST", "/api/record", raw=b"repo=x", content_type="text/plain")
    assert status == 415
    status, _, _ = api.call(
        "POST", "/api/record", raw=b"a=b", content_type="application/x-www-form-urlencoded"
    )
    assert status == 415
    status, _, _ = api.call("POST", "/api/record", raw=b"{}", content_type=None)
    assert status == 415
    assert api.call("POST", "/api/record", raw=b"{not json")[0] == 400


def test_body_over_1mb_is_refused(api: Api) -> None:
    big = b'{"prompt": "' + b"a" * 1_000_100 + b'"}'
    status, _, _ = api.call("POST", "/api/record", raw=big)
    assert status == 413
    # The server still works afterwards.
    assert api.call("GET", "/api/runs")[0] == 200


def test_other_methods_are_refused(api: Api) -> None:
    assert api.call("PUT", "/api/record", {})[0] == 405
    assert api.call("DELETE", "/api/runs/x")[0] == 405


# --------------------------------------------------------------------------- validation


@pytest.mark.parametrize(
    "change",
    [
        {"repo": "git@github.com:acme/widgets.git"},
        {"repo": "ext::sh -c 'touch /tmp/pwned'"},
        {"repo": "http://example.com/acme/widgets.git"},
        {"repo": "ssh://git@example.com/acme/widgets.git"},
        {"repo": "-oProxyCommand=evil"},
        {"repo": "--upload-pack=evil"},
        {"repo": ""},
        {"repo": "/no/such/directory/anywhere"},
        {"repo": 5},
        {"prompt": ""},
        {"prompt": "   \n"},
        {"prompt": 7},
        {"ref": "-evil"},
        {"ref": "has space"},
        {"ref": 3},
        {"model": "-m"},
        {"no_setup": "yes"},
        {"surprise": 1},
        {"sabotage": "nope"},
        {"sabotage": {"at_step": 0, "file": "a.py", "find": "x", "replace": "y"}},
        {"sabotage": {"at_step": "2", "file": "a.py", "find": "x", "replace": "y"}},
        {"sabotage": {"at_step": True, "file": "a.py", "find": "x", "replace": "y"}},
        {"sabotage": {"at_step": 2, "file": "../a.py", "find": "x", "replace": "y"}},
        {"sabotage": {"at_step": 2, "file": "src/../../a.py", "find": "x", "replace": "y"}},
        {"sabotage": {"at_step": 2, "file": "/etc/passwd", "find": "x", "replace": "y"}},
        {"sabotage": {"at_step": 2, "file": "a.py", "find": "", "replace": "y"}},
        {"sabotage": {"at_step": 2, "file": "a.py", "find": "x"}},
        {"sabotage": {"at_step": 2, "file": "a.py", "find": "x", "replace": "y", "more": 1}},
    ],
)
def test_record_validation(api: Api, change: dict[str, Any]) -> None:
    status, body = api.json("POST", "/api/record", {**GOOD, **change})
    assert status == 400, change
    assert body["error"]
    assert "Traceback" not in body["error"]


def test_record_body_must_be_an_object(api: Api) -> None:
    assert api.call("POST", "/api/record", raw=b"[1, 2]")[0] == 400
    assert api.call("POST", "/api/record", raw=b'"text"')[0] == 400


def test_record_accepts_good_inputs(
    api: Api, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("agentbisect.hooks.opencode.prepare_repo_run", _noop_prepare)
    local = tmp_path / "proj"
    local.mkdir()
    sabotage = {"at_step": 3, "file": "src/a.py", "find": "a + b", "replace": "a - b"}
    for body in (
        GOOD,
        {"repo": f"file://{local}", "prompt": "p"},
        {"repo": str(local), "prompt": "p", "ref": "main", "no_setup": True, "sabotage": sabotage},
    ):
        status, reply = api.json("POST", "/api/record", body)
        assert status == 202, reply
        assert api.wait_job(reply["job_id"])["status"] == "done"


def _noop_prepare(origin, prompt, runs_dir, *, run_id=None, **kw) -> Path:  # noqa: ANN001
    return fake_run(Path(runs_dir), run_id)


# --------------------------------------------------------------------------- static files


def test_path_traversal_is_not_found(api_runs: tuple[Api, Path], tmp_path: Path) -> None:
    api, runs = api_runs
    (tmp_path / "secret.html").write_text("secret")
    fake_run(runs, "ok")
    (runs / "ok" / "report.html").write_text("<html>ok</html>")
    for path in (
        "/runs/..%2F..%2Fetc/passwd",
        "/runs/x/../..",
        "/runs/../report.html",
        "/runs/%2e%2e/report.html",
        "/runs/ok/../ok/report.html",
        "/runs/ok/%2e%2e/secret.html",
        "/runs/ok/report.html/",
        "/runs//report.html",
        "/runs/.hidden/report.html",
        "/runs/ok/report.html%00.txt",
        "//etc/passwd",
        "/etc/passwd",
        "/runs/ok/..\\..\\secret.html",
    ):
        status, _, body = api.call("GET", path, token=None)
        assert status == 404, path
        assert b"secret" not in body and b"root:" not in body
    assert api.call("GET", "/runs/ok/report.html", token=None)[0] == 200


def test_symlinks_cannot_escape_the_runs_dir(api_runs: tuple[Api, Path], tmp_path: Path) -> None:
    api, runs = api_runs
    outside = fake_run(tmp_path, "outside")
    (outside / "report.html").write_text("<html>outside</html>")
    (runs / "evil").symlink_to(outside, target_is_directory=True)
    assert api.call("GET", "/runs/evil/report.html", token=None)[0] == 404
    assert api.call("GET", "/runs/evil/PROMPT.txt", token=None)[0] == 404
    assert api.json("GET", "/api/runs/evil")[0] == 404
    assert [r["id"] for r in api.json("GET", "/api/runs")[1]["runs"]] == []

    inside = fake_run(runs, "inside")
    (tmp_path / "private.txt").write_text("private")
    (inside / "report.html").symlink_to(tmp_path / "private.txt")
    assert api.call("GET", "/runs/inside/report.html", token=None)[0] == 404


def test_only_whitelisted_files_are_served(api_runs: tuple[Api, Path]) -> None:
    api, runs = api_runs
    run = fake_run(runs, "r1", prompt="hello prompt")
    (run / "report.html").write_text("<html>report</html>")
    (run / "bisect.json").write_text("{}")
    (runs / "study").mkdir()
    (runs / "study" / "study.html").write_text("<html>study</html>")
    (runs / "study" / "study.json").write_text("{}")
    ok = [
        "/runs/r1/report.html",
        "/runs/r1/PROMPT.txt",
        "/runs/study/study.html",
    ]
    for path in ok:
        status, headers, _ = api.call("GET", path, token=None)
        assert status == 200, path
        assert headers["x-content-type-options"] == "nosniff"
        assert headers["cache-control"] == "no-store"
    html = api.call("GET", "/runs/r1/report.html", token=None)
    assert "default-src 'none'" in html[1]["content-security-policy"]
    assert api.call("GET", "/runs/r1/PROMPT.txt", token=None)[2] == b"hello prompt"
    for path in (
        "/runs/r1/bisect.json",
        "/runs/r1/meta.json",
        "/runs/r1/trace.jsonl",
        "/runs/r1/workdir/",
        "/runs/r1/",
        "/runs/r1",
        "/runs/",
        "/runs",
        "/runs/study/study.json",
        "/runs/study/report.html",
        "/runs/missing/report.html",
        "/index.html",
        "/api",
    ):
        status = api.call("GET", path, token=None)[0]
        assert status in (403, 404), path
    assert api.call("GET", "/runs/r1/bisect.json")[0] == 404


# --------------------------------------------------------------------------- jobs


def test_record_job_lifecycle(api_runs: tuple[Api, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    api, runs = api_runs
    seen: dict[str, Any] = {}

    def fake_prepare(origin, prompt, runs_dir, **kw) -> Path:  # noqa: ANN001
        seen.update(origin=origin, prompt=prompt, runs_dir=Path(runs_dir), **kw)
        return fake_run(Path(runs_dir), kw["run_id"], prompt=prompt)

    monkeypatch.setattr("agentbisect.hooks.opencode.prepare_repo_run", fake_prepare)
    sabotage = {"at_step": 4, "file": "pkg/m.py", "find": "a + b", "replace": "a - b"}
    status, reply = api.json(
        "POST",
        "/api/record",
        {**GOOD, "ref": "v1", "no_setup": True, "sabotage": sabotage},
    )
    assert status == 202
    job = api.wait_job(reply["job_id"])
    assert job["status"] == "done", job
    assert job["kind"] == "record"
    assert job["error"] is None
    assert job["run_id"] and job["run_id"].startswith("widgets-flash-")
    assert "Starting run" in job["log"] and "Prepared run" in job["log"]
    assert seen["origin"] == GOOD["repo"]
    assert seen["ref"] == "v1" and seen["setup"] is False and seen["model_alias"] == "flash"
    assert seen["sabotage"].at_step == 4 and seen["sabotage"].file == "pkg/m.py"
    assert seen["runs_dir"] == runs.resolve()

    status, listing = api.json("GET", "/api/runs")
    assert status == 200
    [row] = listing["runs"]
    assert row["id"] == job["run_id"]
    assert set(row) == {
        "id",
        "title",
        "source",
        "repo_origin",
        "task_id",
        "model",
        "steps",
        "finished",
        "has_bisect",
        "has_report",
        "verdict",
    }
    assert row["title"] == "widgets" and row["model"] == "flash" and row["steps"] == 0
    assert row["has_bisect"] is False and row["has_report"] is False and row["verdict"] is None
    assert "secret" not in json.dumps(listing)  # credentials in a URL are hidden

    status, detail = api.json("GET", f"/api/runs/{job['run_id']}")
    assert status == 200
    assert set(detail) == {
        "id",
        "workdir",
        "prompt",
        "model",
        "repo_origin",
        "finished",
        "steps",
        "has_report",
        "has_bisect",
    }
    assert detail["prompt"] == "fix the bug"
    assert api.json("GET", "/api/runs/nope")[0] == 404
    assert api.json("GET", "/api/jobs/ffffffffffffffff")[0] == 404
    assert api.json("GET", "/api/jobs/not-a-job")[0] == 404


def test_failed_record_job_reports_setup_error_without_traceback(
    api_runs: tuple[Api, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    from agentbisect.workspace import SetupError

    api, runs = api_runs

    def failing(origin, prompt, runs_dir, *, run_id=None, **kw) -> Path:  # noqa: ANN001
        log = Path(runs_dir) / run_id / "setup.log"
        log.parent.mkdir()
        log.write_text("$ uv pip install x\nERROR: no matching distribution\n")
        raise SetupError("dependency install failed: uv pip install", log)

    monkeypatch.setattr("agentbisect.hooks.opencode.prepare_repo_run", failing)
    _, reply = api.json("POST", "/api/record", GOOD)
    job = api.wait_job(reply["job_id"])
    assert job["status"] == "error"
    assert job["error"] == "Setup failed: dependency install failed: uv pip install"
    assert "no matching distribution" in job["log"]  # the setup.log tail reaches the page
    assert "Traceback" not in job["log"] and "Traceback" not in job["error"]
    assert job["run_id"] is None

    def crashing(*a, **kw) -> Path:  # noqa: ANN002, ANN003
        raise RuntimeError("git clone of https://bob:hunter2@example.com/x failed: not found\nmore")

    monkeypatch.setattr("agentbisect.hooks.opencode.prepare_repo_run", crashing)
    _, reply = api.json("POST", "/api/record", GOOD)
    job = api.wait_job(reply["job_id"])
    assert job["status"] == "error"
    assert "hunter2" not in json.dumps(job) and "Traceback" not in json.dumps(job)
    assert job["error"].startswith("RuntimeError: ")


def test_jobs_run_one_at_a_time_in_order(
    api_runs: tuple[Api, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    api, _ = api_runs
    release = threading.Event()
    events: list[str] = []
    active = 0
    overlap = False

    def slow(origin, prompt, runs_dir, *, run_id=None, **kw) -> Path:  # noqa: ANN001
        nonlocal active, overlap
        active += 1
        overlap = overlap or active > 1
        events.append(f"start {prompt}")
        if prompt == "first":
            release.wait(10)
        events.append(f"end {prompt}")
        active -= 1
        return fake_run(Path(runs_dir), run_id)

    monkeypatch.setattr("agentbisect.hooks.opencode.prepare_repo_run", slow)
    first = api.json("POST", "/api/record", {**GOOD, "prompt": "first"})[1]["job_id"]
    second = api.json("POST", "/api/record", {**GOOD, "prompt": "second"})[1]["job_id"]
    deadline = time.monotonic() + 10
    while api.json("GET", f"/api/jobs/{first}")[1]["status"] != "running":
        assert time.monotonic() < deadline
        time.sleep(0.02)
    time.sleep(0.2)
    assert api.json("GET", f"/api/jobs/{second}")[1]["status"] == "queued"
    release.set()
    assert api.wait_job(first)["status"] == "done"
    assert api.wait_job(second)["status"] == "done"
    assert events == ["start first", "end first", "start second", "end second"]
    assert not overlap


def test_job_queue_keeps_at_most_50_jobs() -> None:
    jobs = JobQueue()
    try:
        made = [jobs.submit("record", lambda job: None) for _ in range(60)]
        deadline = time.monotonic() + 10
        while any(j.status != "done" for j in made):
            assert time.monotonic() < deadline
            time.sleep(0.01)
        jobs.submit("record", lambda job: None)  # triggers eviction of finished jobs
        assert len(jobs._jobs) <= serve_mod.MAX_JOBS
        assert jobs.get(made[0].id) is None
        assert jobs.get(made[-1].id) is not None
    finally:
        jobs.close()


def test_active_analyze_job_is_reused() -> None:
    jobs = JobQueue()
    release = threading.Event()
    try:
        first = jobs.submit("analyze", lambda job: release.wait(10), run_id="r1")
        assert jobs.active_for("analyze", "r1") is first
        assert jobs.active_for("analyze", "r2") is None
        assert jobs.active_for("record", "r1") is None
        release.set()
        deadline = time.monotonic() + 10
        while first.status != "done":
            assert time.monotonic() < deadline
            time.sleep(0.01)
        assert jobs.active_for("analyze", "r1") is None
    finally:
        release.set()
        jobs.close()


def test_job_log_is_capped_and_redacted() -> None:
    job = serve_mod.Job(id="a" * 16, kind="record")
    job.log("cloning https://bob:hunter2@example.com/x")
    job.log("x" * 150_000)
    view = job.view()
    assert len(view["log"]) <= serve_mod.LOG_API_CHARS
    job2 = serve_mod.Job(id="b" * 16, kind="record")
    job2.log("cloning https://bob:hunter2@example.com/x")
    assert "hunter2" not in job2.view()["log"] and "***@" in job2.view()["log"]
    assert redact("https://a:b@h/x file:///tmp/x") == "https://***@h/x file:///tmp/x"


def test_analyze_unknown_run_is_404(api: Api) -> None:
    assert api.json("POST", "/api/runs/nope/analyze", {})[0] == 404
    assert api.json("POST", "/api/runs/..%2F..%2Fetc/analyze", {})[0] == 404
    assert api.json("POST", "/api/runs/../analyze", {})[0] == 404


@pytest.mark.skipif(not (DEMO / "trace.jsonl").exists(), reason="demo run not present")
def test_analyze_bisects_and_writes_report(api_runs: tuple[Api, Path], tmp_path: Path) -> None:
    api, runs = api_runs
    run = runs / "caught-and-fixed"
    shutil.copytree(
        DEMO,
        run,
        symlinks=True,
        ignore=shutil.ignore_patterns("scratch", "report.html", "bisect.json"),
    )
    status, listing = api.json("GET", "/api/runs")
    [row] = listing["runs"]
    assert row["has_bisect"] is False and row["has_report"] is False and row["steps"] > 0

    status, reply = api.json("POST", "/api/runs/caught-and-fixed/analyze", {})
    assert status == 202
    job = api.wait_job(reply["job_id"])
    assert job["status"] == "done", job
    assert job["kind"] == "analyze" and job["run_id"] == "caught-and-fixed"
    assert "Bisecting" in job["log"] and "Done." in job["log"]
    assert (run / "bisect.json").is_file() and (run / "report.html").is_file()

    [row] = api.json("GET", "/api/runs")[1]["runs"]
    assert row["has_bisect"] and row["has_report"]
    assert row["verdict"] == "caught and fixed at step 9"
    status, headers, body = api.call("GET", "/runs/caught-and-fixed/report.html", token=None)
    assert status == 200 and b"<html" in body.lower()


# --------------------------------------------------------------------------- logs and CLI


def test_token_and_bodies_never_reach_the_log(api: Api, capsys: pytest.CaptureFixture[str]) -> None:
    api.call("GET", "/api/runs")
    api.call("GET", "/api/runs", token="wrong-token-value")
    api.call("POST", "/api/record", {**GOOD, "prompt": "SECRET-PROMPT-TEXT", "repo": "-x"})
    api.call("GET", "/runs/a/report.html?token=" + api.server.token)
    err = capsys.readouterr().err
    assert "[serve]" in err
    assert api.server.token not in err
    assert "wrong-token-value" not in err
    assert "SECRET-PROMPT-TEXT" not in err


def test_cli_lists_serve() -> None:
    result = CliRunner().invoke(app, ["--help"], env={"COLUMNS": "120"})
    assert result.exit_code == 0
    assert "serve" in result.output
    sub = CliRunner().invoke(app, ["serve", "--help"], env={"COLUMNS": "120"})
    assert sub.exit_code == 0
    for flag in ("--runs", "--port", "--no-open"):
        assert flag in sub.output
