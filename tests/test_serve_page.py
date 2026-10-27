"""The local app page (``agentbisect serve``), drawn under node with a fake ``fetch``.

The backend is not needed: ``fetch`` is replaced by a table of canned answers that each test
sets up before the app script runs.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from agentbisect.report.page import VENDOR_SCRIPTS
from agentbisect.report.serve_page import render_serve_page
from tests.test_report import ASSETS, DATA_RE, needs_node, run_page

NO_TOKEN = "Open the link printed in your terminal; it carries the access token."
EVIL = '<script>alert("x")</script>'

# Runs before the app. `routes` maps "METHOD path" to {status, body} or a function of the request.
STUB = r"""
window.AB_POLL_MS = 15;
window.calls = [];
window.replaced = [];
history.replaceState = function (a, b, u) { window.replaced.push(u); };
window.RUNS = [];
window.routes = {};
window.fetch = function (path, init) {
  init = init || {};
  var key = (init.method || "GET") + " " + path;
  window.calls.push({ key: key, headers: init.headers || {}, body: init.body || null });
  var r = key === "GET /api/runs" && !window.routes[key] ? { status: 200, body: { runs: window.RUNS } } : window.routes[key];
  if (typeof r === "function") r = r(init);
  if (!r) r = { status: 404, body: { error: "no route " + key } };
  return Promise.resolve({ ok: r.status < 400, status: r.status, json: function () { return Promise.resolve(r.body); } });
};
"""

HELPERS = """
const id = (x) => H.all((n) => n.id === x)[0];
const type = async (x, v) => { H.input(id(x), v); await H.tick(10); };
const submit = async () => {
  H.byTag("form")[0].dispatchEvent({ type: "submit", preventDefault() {} });
  await H.tick(120);
};
const keys = () => calls.map((c) => c.key);
"""


def run(
    tmp_path: Path, setup: str, scenario: str = "", hash_: str = "#token=tok"
) -> dict[str, Any]:
    page = render_serve_page()
    i = page.index("<script>\n")
    path = tmp_path / "serve.html"
    path.write_text(page[:i] + "<script>" + STUB + setup + "</script>\n" + page[i:])
    return run_page(path, HELPERS + scenario, hash=hash_)


def run_row(**kw: Any) -> dict[str, Any]:
    row = {
        "id": "r1",
        "title": "r1",
        "source": "repo",
        "repo_origin": "https://example.com/p",
        "task_id": None,
        "model": "flash",
        "steps": 12,
        "finished": True,
        "has_bisect": False,
        "has_report": False,
        "verdict": None,
    }
    row.update(kw)
    return row


def js(data: Any) -> str:
    return json.dumps(data).replace("</", "<\\/")


def push(*rows: dict[str, Any]) -> str:
    """JS that adds runs to the list the fake ``/api/runs`` answers with."""
    return f"RUNS.push(...{js(list(rows))});"


@pytest.fixture
def page() -> str:
    return render_serve_page()


# --------------------------------------------------------------------------- static checks


def test_page_works_offline(page: str) -> None:
    assert not re.search(r"""(src|href)\s*=\s*["']?\s*(https?:)?//""", page, re.I)
    assert "<link" not in page.lower() and "@import" not in page and "url(" not in page
    assert not re.search(r"<script[^>]*\ssrc=", page, re.I)
    outside = DATA_RE.sub("", page)
    assert "http://" not in outside and "https://" not in outside
    assert (
        page.count("<script") == 1 + len(VENDOR_SCRIPTS) + 1 + 2 + 1
    )  # data, libs, common, 2 helpers, app
    assert len(page.encode()) < 400_000


def test_scripts_use_text_nodes_only() -> None:
    for name in ("serve_app.js", "runform.js"):
        text = (ASSETS / name).read_text()
        assert "innerHTML" not in text and "dangerouslySetInnerHTML" not in text
        assert "http://" not in text and "https://" not in text
    css = (ASSETS / "serve.css").read_text()
    assert "gradient" not in css and "url(" not in css


# --------------------------------------------------------------------------- drawn page


@needs_node
def test_renders_and_asks_for_the_runs(tmp_path: Path) -> None:
    out = run(tmp_path, push(run_row(title="humanize run")), "return keys();")
    text = out["text"]
    assert "local app" in text
    assert "Runs on your machine. Nothing leaves it except git clone and package installs." in text
    assert (
        "Installing a project's dependencies runs its build code. "
        "Use repositories you trust, or add --no-setup."
    ) in text
    assert "Set up the run" in text and "Study page" in text
    assert "humanize run" in text and "Not analyzed" in text
    assert out["result"][0] == "GET /api/runs"
    assert out["errors"] == []


@needs_node
def test_token_is_read_from_the_hash_removed_and_sent_on_every_call(tmp_path: Path) -> None:
    out = run(
        tmp_path,
        push(run_row()),
        """
        H.click(H.byText("Refresh", "button")[0]);
        await H.tick(30);
        return { calls: calls, replaced: replaced };
        """,
        hash_="#token=a%2Fb-c",
    )
    r = out["result"]
    assert r["replaced"] == ["/"]  # the token is gone from the address
    assert len(r["calls"]) >= 2
    for c in r["calls"]:
        assert c["headers"]["X-AgentBisect-Token"] == "a/b-c"
        assert c["key"].split(" ")[1].startswith("/api/")  # same-origin paths only


@needs_node
def test_a_403_tells_the_person_to_use_the_terminal_link(tmp_path: Path) -> None:
    out = run(
        tmp_path,
        'routes["GET /api/runs"] = { status: 403, body: { error: "bad token" } };',
        hash_="",
    )
    assert NO_TOKEN in out["text"]
    assert "bad token" not in out["text"]


@needs_node
def test_every_field_has_a_label_and_the_page_is_keyboard_reachable(tmp_path: Path) -> None:
    out = run(
        tmp_path,
        push(run_row()),
        """
        await type("new-repo", "https://example.com/p");
        H.click(id("new-plant"));
        id("new-plant").checked = true;
        id("new-plant").dispatchEvent({ type: "change", target: id("new-plant") });
        await H.tick(20);
        const labelled = new Set(H.byTag("label").map((l) => l.getAttribute("for")));
        H.byTag("label").forEach((l) => l.children.forEach((c) => c.id && labelled.add(c.id)));
        const fields = H.all((n) => ["input", "textarea", "select"].includes(n.localName) && n.id);
        return {
          missing: fields.filter((f) => !labelled.has(f.id)).map((f) => f.id),
          n: fields.length,
          divClick: H.all((n) => n.localName === "div" && n.onclick).length,
        };
        """,
    )
    r = out["result"]
    assert r["missing"] == [] and r["n"] >= 11  # includes the planted-break fields
    assert r["divClick"] == 0


@needs_node
def test_bad_repositories_block_the_submit(tmp_path: Path) -> None:
    out = run(
        tmp_path,
        "",
        """
        const seen = {};
        await type("new-prompt", "Fix it.");
        await type("new-repo", "git@github.com:you/p.git");
        await submit();
        seen.ssh = H.byClass("ferr").map((e) => e.textContent);
        await type("new-repo", "http://example.com/p");
        await submit();
        seen.http = H.byClass("ferr").map((e) => e.textContent);
        seen.posts = keys().filter((k) => k.startsWith("POST"));
        return seen;
        """,
    )
    r = out["result"]
    assert any("SSH" in e for e in r["ssh"])
    assert any("not accepted" in e for e in r["http"])
    assert r["posts"] == []


@needs_node
def test_record_flow_polls_the_job_then_shows_the_next_step(tmp_path: Path) -> None:
    setup = """
    let polls = 0;
    routes["POST /api/record"] = (init) => ({ status: 202, body: { job_id: "j1" } });
    routes["GET /api/jobs/j1"] = () => {
      polls += 1;
      return polls < 3
        ? { status: 200, body: { id: "j1", kind: "record", status: "running", log: "cloning " + polls, run_id: null, error: null } }
        : { status: 200, body: { id: "j1", kind: "record", status: "done", log: "all set", run_id: "r9", error: null } };
    };
    routes["GET /api/runs/r9"] = { status: 200, body: {
      id: "r9", workdir: "/work/r9/workdir", prompt: "Do the thing.\\nTwice.", model: "glm",
      repo_origin: "https://example.com/p", finished: false, steps: 0, has_report: false, has_bisect: false } };
    """
    out = run(
        tmp_path,
        setup,
        """
        await type("new-repo", "https://example.com/p");
        await type("new-prompt", "Do the thing.\\nTwice.");
        await type("new-model", "glm");
        await type("new-ref", "dev");
        H.byTag("form")[0].dispatchEvent({ type: "submit", preventDefault() {} });
        await H.tick(22);
        const early = H.text();
        await H.tick(250);
        const got = [];
        navigator.clipboard = { writeText: (t) => { got.push(t); return Promise.resolve(); } };
        H.byClass("copy").forEach((b) => H.click(b));
        await H.tick(20);
        const post = calls.filter((c) => c.key === "POST /api/record")[0];
        return { early, body: JSON.parse(post.body), got, k: keys(), ct: post.headers["Content-Type"] };
        """,
    )
    r = out["result"]
    assert r["body"] == {
        "repo": "https://example.com/p",
        "prompt": "Do the thing.\nTwice.",
        "model": "glm",
        "ref": "dev",
    }
    assert r["ct"] == "application/json"
    assert "Running" in r["early"] or "Queued" in r["early"]
    text = out["text"]
    assert "Done" in text and "all set" in text
    assert "/work/r9/workdir" in text and "Do the thing." in text
    assert "new Alan Code conversation" in text and "Pick the model" in text and "glm" in text
    assert "Do not edit files yourself" in text
    assert "The agent has finished: find the step" in text
    assert r["got"] == ["/work/r9/workdir", "Do the thing.\nTwice."]
    assert r["k"].count("GET /api/jobs/j1") == 3
    assert "GET /api/runs/r9" in r["k"]
    assert r["k"].count("GET /api/runs") >= 2  # the list is refreshed when the job ends


@needs_node
def test_plant_a_break_goes_into_the_request(tmp_path: Path) -> None:
    out = run(
        tmp_path,
        'routes["POST /api/record"] = { status: 202, body: { job_id: "j" } };'
        'routes["GET /api/jobs/j"] = { status: 200, body: { id: "j", status: "running", log: "", run_id: null, error: null } };',
        """
        await type("new-repo", "/some/folder");
        await type("new-prompt", "Go.");
        const box = id("new-plant");
        box.checked = true;
        box.dispatchEvent({ type: "change", target: box });
        await H.tick(20);
        await type("new-step", "5");
        await type("new-file", "a/b.py");
        await type("new-find", "x = 1");
        await type("new-replace", "x = 2");
        const nosetup = id("new-nosetup");
        nosetup.checked = true;
        nosetup.dispatchEvent({ type: "change", target: nosetup });
        await H.tick(20);
        await submit();
        return JSON.parse(calls.filter((c) => c.key === "POST /api/record")[0].body);
        """,
    )
    assert out["result"] == {
        "repo": "/some/folder",
        "prompt": "Go.",
        "model": "flash",
        "no_setup": True,
        "sabotage": {"at_step": 5, "file": "a/b.py", "find": "x = 1", "replace": "x = 2"},
    }


@needs_node
def test_a_400_from_record_is_shown(tmp_path: Path) -> None:
    out = run(
        tmp_path,
        'routes["POST /api/record"] = { status: 400, body: { error: "the folder does not exist" } };',
        """
        await type("new-repo", "/nope");
        await type("new-prompt", "Go.");
        await submit();
        return H.byClass("ferr").map((e) => e.textContent);
        """,
    )
    assert "the folder does not exist" in out["result"]


@needs_node
def test_analyze_flow_ends_with_the_verdict_and_a_report_link(tmp_path: Path) -> None:
    setup = """
    let polls = 0;
    routes["POST /api/runs/r1/analyze"] = { status: 202, body: { job_id: "a1" } };
    routes["GET /api/runs/r1"] = { status: 200, body: {
      id: "r1", workdir: "/w/r1", prompt: "P", model: "flash", repo_origin: null, finished: true, steps: 12,
      has_report: false, has_bisect: false } };
    routes["GET /api/jobs/a1"] = () => {
      polls += 1;
      if (polls < 2) return { status: 200, body: { id: "a1", kind: "analyze", status: "running", log: "probing", run_id: "r1", error: null } };
      RUNS[0].has_bisect = true; RUNS[0].has_report = true;
      RUNS[0].verdict = "Step 8 (edit a.py) broke test_x.";
      return { status: 200, body: { id: "a1", kind: "analyze", status: "done", log: "report written", run_id: "r1", error: null } };
    };
    """
    out = run(
        tmp_path,
        push(run_row()) + setup,
        """
        const before = H.byClass("vchip").map((c) => c.className);
        H.click(H.byText("Find the step", "button")[0]);
        await H.tick(250);
        const links = H.byTag("a").filter((a) => a.textContent.trim() === "Open the report");
        return {
          before,
          after: H.byClass("vchip").map((c) => c.className),
          hrefs: H.byTag("a").filter((a) => /report/i.test(a.textContent)).map((a) => a.getAttribute("href")),
          next: links.length,
          k: keys(),
        };
        """,
    )
    r = out["result"]
    assert "idle" in r["before"][0]
    assert all("fail" in c for c in r["after"])
    assert "/runs/r1/report.html" in r["hrefs"] and r["next"] == 1
    assert "POST /api/runs/r1/analyze" in r["k"] and r["k"].count("GET /api/jobs/a1") == 2
    text = out["text"]
    assert "Step 8 (edit a.py) broke test_x." in text and "report written" in text
    assert "/w/r1" in text  # the next-step card for the selected run


@needs_node
def test_verdict_chips_are_red_green_or_grey(tmp_path: Path) -> None:
    rows = [
        run_row(id="a", title="a", has_bisect=True, verdict="Step 8 broke test_x."),
        run_row(
            id="b",
            title="b",
            has_bisect=True,
            verdict="Broke test_y at step 9 and fixed it at step 18.",
        ),
        run_row(id="c", title="c", has_bisect=False),
        run_row(
            id="d",
            title="d",
            has_bisect=True,
            verdict="No failing check, nothing to bisect.",
            has_report=True,
        ),
    ]
    out = run(
        tmp_path,
        push(*rows),
        'return H.byClass("vchip").map((c) => c.className.split(/\\s+/).filter((x) => x !== "pill" && x !== "vchip")[0]);',
    )
    assert out["result"] == ["fail", "pass", "idle", "pass"]


@needs_node
def test_selecting_a_row_shows_its_next_step_again(tmp_path: Path) -> None:
    setup = """
    routes["GET /api/runs/b"] = { status: 200, body: { id: "b", workdir: "/w/b", prompt: "Prompt B", model: "kimi",
      repo_origin: null, finished: true, steps: 3, has_report: false, has_bisect: false } };
    """
    out = run(
        tmp_path,
        push(run_row(id="a", title="A"), run_row(id="b", title="B")) + setup,
        """
        const none = !H.text().includes("Next step");
        H.click(H.byText("B", "button")[0]);
        await H.tick(40);
        return { none };
        """,
    )
    assert out["result"]["none"] is True
    assert "/w/b" in out["text"] and "Prompt B" in out["text"] and "kimi" in out["text"]


@needs_node
def test_server_text_stays_text(tmp_path: Path) -> None:
    evil = js(EVIL)
    setup = push(
        run_row(
            title=EVIL, repo_origin=EVIL, model=EVIL, has_bisect=True, verdict=EVIL, has_report=True
        )
    )
    setup += (
        'routes["POST /api/runs/r1/analyze"] = { status: 202, body: { job_id: "a1" } };'
        f'routes["GET /api/runs/r1"] = {{ status: 200, body: {{ id: "r1", workdir: {evil}, prompt: {evil}, '
        f"model: {evil}, repo_origin: null, finished: true, steps: 1, has_report: false, has_bisect: false }} }};"
        f'routes["GET /api/jobs/a1"] = {{ status: 200, body: {{ id: "a1", status: "error", log: {evil}, '
        f'run_id: "r1", error: {evil} }} }};'
    )
    out = run(
        tmp_path,
        setup,
        """
        H.click(H.byTag("button").filter((b) => b.textContent.includes("script"))[0]);
        await H.tick(40);
        H.click(H.byText("The agent has finished: find the step", "button")[0]);
        await H.tick(150);
        return null;
        """,
    )
    assert out["text"].count(EVIL) >= 6
    assert "script" not in out["tags"]
    assert "onerror" not in out["attrNames"]


@needs_node
def test_a_long_log_is_capped_and_a_dead_poll_ends_the_job(tmp_path: Path) -> None:
    setup = """
    routes["POST /api/record"] = { status: 202, body: { job_id: "j" } };
    routes["GET /api/jobs/j"] = { status: 200, body: { id: "j", status: "running", log: "HEAD" + "x".repeat(60000) + "TAIL", run_id: null, error: null } };
    """
    out = run(
        tmp_path,
        setup,
        """
        await type("new-repo", "/p");
        await type("new-prompt", "Go.");
        await submit();
        const log = H.byTag("pre").filter((p) => p.getAttribute("role") === "log")[0].textContent;
        routes["GET /api/jobs/j"] = { status: 500, body: { error: "boom" } };
        await H.tick(200);
        return { len: log.length, head: log.includes("HEAD"), tail: log.endsWith("TAIL"), failed: H.text().includes("boom") };
        """,
    )
    r = out["result"]
    assert r["len"] < 21_000 and r["tail"] and not r["head"]
    assert r["failed"]
