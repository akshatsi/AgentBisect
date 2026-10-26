"""The HTML report: built from a synthetic run, no LLM, no network.

The page is a Preact app. Most checks read the JSON data it carries (``data_of``). Checks of what
the app draws run its real inline scripts under ``node`` against a tiny DOM (``run_page``) and
are skipped when node is missing.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import pytest

from agentbisect.bisect import bisect_run
from agentbisect.models import (
    Analysis,
    BisectResult,
    BisectStatus,
    CulpritReport,
    OracleRun,
    RepairEpisode,
    RunReport,
    Step,
    ToolKind,
    Trace,
    TraceMeta,
    TraceSource,
)
from agentbisect.report import page as page_mod
from agentbisect.report import render as render_mod
from agentbisect.report.render import build_data, render_report
from agentbisect.report.text import (
    Shortener,
    check_label,
    fmt_percent,
    fmt_tokens,
    repaired_sentences,
    step_label,
    verdict_sentences,
)
from agentbisect.synthetic import make_synthetic_run, pytest_failure_output

CHECK = "python -m pytest -q tests/test_calc.py::test_add"
BREAK_AT, OBSERVE_AT, N = 4, 7, 10
REPO_ROOT = Path(__file__).resolve().parent.parent
ASSETS = REPO_ROOT / "agentbisect" / "report" / "assets"

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")

# --------------------------------------------------------------------------- a tiny DOM for node

DOM_HARNESS = r"""// Runs a report page's inline scripts against a tiny DOM and prints what it drew.
// usage: node harness.js <page.html> [scenario.js]
"use strict";
const fs = require("fs");
const vm = require("vm");

class Node {
  constructor(type) {
    this.nodeType = type;
    this.childNodes = [];
    this.parentNode = null;
  }
  get firstChild() { return this.childNodes[0] || null; }
  get lastChild() { return this.childNodes[this.childNodes.length - 1] || null; }
  get nextSibling() {
    if (!this.parentNode) return null;
    const s = this.parentNode.childNodes;
    return s[s.indexOf(this) + 1] || null;
  }
  get previousSibling() {
    if (!this.parentNode) return null;
    const s = this.parentNode.childNodes;
    return s[s.indexOf(this) - 1] || null;
  }
  _detach(child) {
    if (child.parentNode) child.parentNode.removeChild(child);
  }
  appendChild(child) {
    this._detach(child);
    child.parentNode = this;
    this.childNodes.push(child);
    return child;
  }
  insertBefore(child, ref) {
    if (!ref) return this.appendChild(child);
    this._detach(child);
    child.parentNode = this;
    this.childNodes.splice(this.childNodes.indexOf(ref), 0, child);
    return child;
  }
  removeChild(child) {
    const i = this.childNodes.indexOf(child);
    if (i >= 0) this.childNodes.splice(i, 1);
    child.parentNode = null;
    return child;
  }
  remove() { if (this.parentNode) this.parentNode.removeChild(this); }
  replaceChild(n, o) { this.insertBefore(n, o); this.removeChild(o); return o; }
  contains(n) { for (let x = n; x; x = x.parentNode) if (x === this) return true; return false; }
}
class Text extends Node {
  constructor(data) { super(3); this.data = String(data); }
  get nodeValue() { return this.data; }
  set nodeValue(v) { this.data = String(v); }
  get textContent() { return this.data; }
  set textContent(v) { this.data = String(v); }
}
class Element extends Node {
  constructor(name, ns) {
    super(1);
    this.localName = name.toLowerCase();
    this.nodeName = name.toUpperCase();
    this.tagName = this.nodeName;
    this.namespaceURI = ns || "http://www.w3.org/1999/xhtml";
    this._attrs = new Map();
    this._listeners = new Map();
    for (const h of ["onclick", "onkeydown", "oninput", "onchange", "onfocus", "onblur", "onmouseenter", "onmouseleave"]) this[h] = null;
    const store = {};
    this.style = new Proxy(store, {
      get: (t, k) => (k === "setProperty" ? (n, v) => { t[n] = v; } : k === "removeProperty" ? (n) => { delete t[n]; } : t[k]),
      set: (t, k, v) => { t[k] = v; return true; },
    });
  }
  setAttribute(k, v) { this._attrs.set(k, String(v)); }
  getAttribute(k) { return this._attrs.has(k) ? this._attrs.get(k) : null; }
  removeAttribute(k) { this._attrs.delete(k); }
  hasAttribute(k) { return this._attrs.has(k); }
  get id() { return this.getAttribute("id") || ""; }
  set id(v) { this.setAttribute("id", v); }
  get className() { return this.getAttribute("class") || ""; }
  set className(v) { this.setAttribute("class", v); }
  get children() { return this.childNodes.filter((c) => c.nodeType === 1); }
  get textContent() { return this.childNodes.map((c) => c.textContent).join(""); }
  set textContent(v) {
    this.childNodes.slice().forEach((c) => this.removeChild(c));
    if (v !== "" && v != null) this.appendChild(new Text(v));
  }
  get innerText() { return this.textContent; }
  addEventListener(t, fn) {
    if (!this._listeners.has(t)) this._listeners.set(t, []);
    this._listeners.get(t).push(fn);
  }
  removeEventListener(t, fn) {
    const l = this._listeners.get(t) || [];
    const i = l.indexOf(fn);
    if (i >= 0) l.splice(i, 1);
  }
  dispatchEvent(ev) {
    ev.target = ev.target || this;
    for (let n = this; n; n = n.parentNode) {
      ev.currentTarget = n;
      (n._listeners && n._listeners.get(ev.type) || []).slice().forEach((fn) => fn.call(n, ev));
      if (ev._stop) break;
    }
    return !ev.defaultPrevented;
  }
  closest(sel) {
    const cls = sel.startsWith(".") ? sel.slice(1) : null;
    for (let n = this; n && n.nodeType === 1; n = n.parentNode)
      if (cls ? n.className.split(/\s+/).includes(cls) : n.localName === sel) return n;
    return null;
  }
  focus() { documentStub.activeElement = this; }
  scrollIntoView() {}
  getBoundingClientRect() { return { x: 0, y: 0, width: 100, height: 20, top: 0, left: 0, right: 100, bottom: 20 }; }
}

const documentStub = {
  activeElement: null,
  _ids: new Map(),
  _listeners: new Map(),
  createElement: (n) => new Element(n),
  createElementNS: (ns, n) => new Element(n, ns),
  createTextNode: (t) => new Text(t),
  getElementById(id) { return this._ids.get(id) || null; },
  addEventListener(t, fn) { if (!this._listeners.has(t)) this._listeners.set(t, []); this._listeners.get(t).push(fn); },
  removeEventListener(t, fn) { const l = this._listeners.get(t) || []; const i = l.indexOf(fn); if (i >= 0) l.splice(i, 1); },
};
documentStub.documentElement = new Element("html");
documentStub.body = new Element("body");
documentStub.documentElement.appendChild(documentStub.body);

const pagePath = process.argv[2];
const scenarioPath = process.argv[3];
const page = fs.readFileSync(pagePath, "utf8");
const opts = JSON.parse(process.env.HARNESS_OPTS || "{}");

const app = new Element("div");
app.setAttribute("id", "app");
app.appendChild(new Text("Loading"));
documentStub.body.appendChild(app);
documentStub._ids.set("app", app);

const scripts = [...page.matchAll(/<script([^>]*)>([\s\S]*?)<\/script>/g)];
const created = [];
for (const m of scripts) {
  if (/application\/json/.test(m[1])) {
    const idm = /id="([^"]+)"/.exec(m[1]);
    const el = new Element("script");
    el.setAttribute("id", idm[1]);
    el.appendChild(new Text(m[2]));
    documentStub._ids.set(idm[1], el);
  }
}

const errors = [];
const sandbox = {
  document: documentStub,
  console: { log() {}, warn: (...a) => errors.push("warn: " + a.join(" ")), error: (...a) => errors.push("error: " + a.join(" ")) },
  setTimeout, clearTimeout, setInterval, clearInterval,
  Promise, JSON, Math, Object, Array, String, Number, Map, Set, Symbol, Error, TypeError, RegExp, Date, parseInt, parseFloat, isNaN,
  requestAnimationFrame: (fn) => setTimeout(fn, 0),
  cancelAnimationFrame: clearTimeout,
  localStorage: { getItem: () => null, setItem() {} },
  location: { hash: opts.hash || "" },
  history: { replaceState() {} },
  navigator: {},
  matchMedia: (q) => ({
    matches: /reduce/.test(q) ? !!opts.reduced : /dark/.test(q) ? !!opts.dark : false,
    addEventListener() {}, removeEventListener() {},
  }),
  addEventListener() {}, removeEventListener() {},
  Element, Node, Text,
};
sandbox.window = sandbox;
sandbox.self = sandbox;
sandbox.globalThis = sandbox;
vm.createContext(sandbox);

function walk(n, fn) { fn(n); n.childNodes.forEach((c) => walk(c, fn)); }
const H = {
  root: app,
  all(pred) { const out = []; walk(app, (n) => { if (n.nodeType === 1 && pred(n)) out.push(n); }); return out; },
  byClass(c) { return this.all((n) => n.className.split(/\s+/).includes(c)); },
  byTag(t) { return this.all((n) => n.localName === t); },
  byText(t, tag) { return this.all((n) => (!tag || n.localName === tag) && n.textContent.trim() === t); },
  byLabel(l) { return this.all((n) => n.getAttribute("aria-label") === l); },
  text() { return app.textContent; },
  click(el) { return el.dispatchEvent({ type: "click", preventDefault() { this.defaultPrevented = true; }, stopPropagation() { this._stop = true; } }); },
  key(el, key) { return el.dispatchEvent({ type: "keydown", key, target: el, preventDefault() { this.defaultPrevented = true; }, stopPropagation() { this._stop = true; } }); },
  docKey(key, target) {
    const ev = { type: "keydown", key, target: target || documentStub.body, preventDefault() { this.defaultPrevented = true; } };
    (documentStub._listeners.get("keydown") || []).slice().forEach((fn) => fn(ev));
    return ev;
  },
  input(el, value) { el.value = value; return el.dispatchEvent({ type: "input", target: el }); },
  tick(ms) { return new Promise((r) => setTimeout(r, ms || 30)); },
  tagsMade() { return created; },
};
sandbox.H = H;

(async () => {
  let failure = null;
  try {
    for (const m of scripts) {
      if (/application\/json/.test(m[1])) continue;
      vm.runInContext(m[2], sandbox, { filename: "page-script" });
    }
    await H.tick(40);
    let extra = null;
    if (scenarioPath) {
      const code = fs.readFileSync(scenarioPath, "utf8");
      extra = await vm.runInContext("(async () => {\n" + code + "\n})()", sandbox);
    }
    const elements = H.all(() => true);
    const out = {
      text: app.textContent,
      tags: [...new Set(elements.map((n) => n.localName))].sort(),
      attrNames: [...new Set(elements.flatMap((n) => [...n._attrs.keys()]))].sort(),
      errors,
      result: extra,
    };
    process.stdout.write(JSON.stringify(out));
  } catch (e) {
    failure = e;
    process.stdout.write(JSON.stringify({ failure: String((e && e.stack) || e), errors }));
  }
  process.exit(failure ? 1 : 0);
})();"""


def run_page(page: Path, scenario: str = "", **opts: Any) -> dict[str, Any]:
    """Run a page's inline scripts under node and return what the app drew.

    ``scenario`` is the body of an async function run after load, with the helper ``H`` in scope
    (``H.click``, ``H.byClass``, ``H.docKey``, ...); its return value comes back as ``result``.
    ``opts`` are page options: ``reduced`` (prefers-reduced-motion), ``dark``, ``hash``.
    """
    assert NODE
    with tempfile.TemporaryDirectory() as tmp:
        harness = Path(tmp) / "harness.js"
        harness.write_text(DOM_HARNESS)
        args = [NODE, str(harness), str(page)]
        if scenario:
            sc = Path(tmp) / "scenario.js"
            sc.write_text(scenario)
            args.append(str(sc))
        done = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=90,
            env={"HARNESS_OPTS": json.dumps(opts), "PATH": "/usr/bin:/bin"},
        )
    assert done.stdout, done.stderr
    out = json.loads(done.stdout)
    assert "failure" not in out, out.get("failure")
    assert out["errors"] == [], out["errors"]
    return out


# --------------------------------------------------------------------------- fixtures


@pytest.fixture(scope="module")
def bisected(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A 10-step synthetic run, bisected with a stand-in oracle (no pytest subprocesses). Tests copy it before editing."""
    run = tmp_path_factory.mktemp("rep") / "run"
    trace = make_synthetic_run(run, n_steps=N, break_at=BREAK_AT, observe_at=OBSERVE_AT)
    good = {trace.meta.base_snapshot} | {s.snapshot for s in trace.steps[: BREAK_AT - 1]}
    tail = pytest_failure_output(-1)

    def factory(_check: str):
        def execute(sha: str) -> OracleRun:  # a stand-in for running pytest on the snapshot
            return OracleRun(exit_code=0 if sha in good else 1, duration_s=0.01, output_tail=tail)

        return execute

    report = bisect_run(trace, run, CHECK, executor_factory=factory)
    (run / "bisect.json").write_text(report.model_dump_json(indent=2))
    return run


@pytest.fixture
def run_copy(bisected: Path, tmp_path: Path) -> Path:
    dest = tmp_path / "run"
    shutil.copytree(bisected, dest, ignore=shutil.ignore_patterns("scratch", "workdir"))
    return dest


def html_of(run: Path) -> str:
    return render_report(run).read_text(encoding="utf-8")


DATA_RE = re.compile(r'<script type="application/json" id="data">(.*?)</script>', re.S)


def data_of(page: str) -> dict[str, Any]:
    m = DATA_RE.search(page)
    assert m, "no data blob"
    return json.loads(m.group(1))


def load_report(run: Path) -> RunReport:
    return RunReport.model_validate_json((run / "bisect.json").read_text())


def save_report(run: Path, report: RunReport) -> None:
    (run / "bisect.json").write_text(report.model_dump_json(indent=2))


def first_member(data: dict[str, Any]) -> dict[str, Any]:
    return data["groups"][0]["members"][0]


# Land the replay at once (prefers-reduced-motion) and read the state of the strip.
LAND = """
H.docKey(" ");
await H.tick(60);
"""
CELL_STATE = """
const out = {};
H.byClass("cell").forEach((c) => { out[c.getAttribute("id")] = c.className.split(/\\s+/); });
return out;
"""


# --------------------------------------------------------------------------- the page


def test_header_shows_source_and_model(run_copy: Path) -> None:
    meta = (run_copy / "meta.json").read_text()
    meta = meta.replace('"source": "synthetic"', '"source": "alan_code"')
    meta = meta.replace('"model": null', '"model": "flash"')
    (run_copy / "meta.json").write_text(meta)
    page = html_of(run_copy)
    run = data_of(page)["run"]
    assert run["source"] == "Alan Code via OpenCode"
    assert run["model"].startswith("flash")
    assert "recorded by Alan Code via OpenCode" in page  # the no-script text carries it too


def test_header_shows_repo_origin_instead_of_task(run_copy: Path) -> None:
    meta = (run_copy / "meta.json").read_text()
    assert '"repo_origin": null' in meta
    run = data_of(html_of(run_copy))["run"]
    assert run["task"] and run["repo"] is None
    (run_copy / "meta.json").write_text(
        meta.replace('"repo_origin": null', '"repo_origin": "https://github.com/me/proj"')
    )
    run = data_of(html_of(run_copy))["run"]
    assert run["repo"] == "https://github.com/me/proj"
    assert run["task"] is None


def test_source_labels_cover_every_source() -> None:
    from agentbisect.report.render import SOURCE_LABELS

    assert set(SOURCE_LABELS) == set(TraceSource)
    assert SOURCE_LABELS[TraceSource.CLAUDE_CODE] == "Claude Code"
    assert SOURCE_LABELS[TraceSource.ALAN] == "own harness"


def test_render_writes_one_html_file(run_copy: Path) -> None:
    before = {p.name for p in run_copy.iterdir()}
    out = render_report(run_copy)
    assert out == run_copy / "report.html"
    assert out.read_text().lstrip().lower().startswith("<!doctype html>")
    assert {p.name for p in run_copy.iterdir()} - before == {"report.html"}


def test_banner_names_step_test_lag_and_tokens(run_copy: Path) -> None:
    data = data_of(html_of(run_copy))
    g = data["groups"][0]
    assert g["sentences"][0] == f"Step {BREAK_AT} (edit calc/__init__.py) broke test_add."
    assert g["sentences"][1].startswith(f"The agent only noticed at step {OBSERVE_AT}")
    assert f"{OBSERVE_AT - BREAK_AT} steps" in g["sentences"][1]
    stats = {s["k"]: s for s in first_member(data)["stats"]}
    assert stats["Decisive step"]["v"] == str(BREAK_AT)
    assert stats["Noticed at"]["v"] == str(OBSERVE_AT)
    assert stats["Detection lag"]["v"] == str(OBSERVE_AT - BREAK_AT)
    assert "of run" in stats["Wasted tokens"]["small"]


@needs_node
def test_app_draws_the_verdict(run_copy: Path) -> None:
    out = run_page(render_report(run_copy))
    text = out["text"]
    assert f"Step {BREAK_AT}" in text
    assert "(edit calc/__init__.py) broke test_add." in text
    assert f"The agent only noticed at step {OBSERVE_AT}" in text
    assert "Detection lag" in text and "Wasted tokens" in text and "of run" in text
    assert "Bisect replay" in text and "Step explorer" in text
    assert "Culprit found" in text


def test_page_works_offline(run_copy: Path) -> None:
    page = html_of(run_copy)
    assert not re.search(r"""(src|href)\s*=\s*["']?\s*(https?:)?//""", page, re.I)
    assert "<link" not in page.lower()
    assert not re.search(r"<script[^>]*\ssrc=", page, re.I)
    assert "@import" not in page and "url(" not in page
    outside_data = DATA_RE.sub("", page)
    assert "http://" not in outside_data and "https://" not in outside_data
    assert page.count("<script") == 3 + len(page_mod.VENDOR_SCRIPTS) + 1  # boot, data, libs, 2 apps
    assert len(page) < 1_000_000


def test_vendored_assets_are_listed_and_clean() -> None:
    notes = (ASSETS / "LICENSES.md").read_text()
    for name in ("preact", "htm"):
        assert name in notes
    assert re.search(r"\|\s*preact\s*\|\s*10\.\d+\.\d+\s*\|", notes)
    assert re.search(r"\|\s*htm\s*\|\s*3\.\d+\.\d+\s*\|", notes)
    assert (ASSETS / "vendor" / "LICENSE.preact").exists() and (
        ASSETS / "vendor" / "LICENSE.htm"
    ).exists()
    for name in page_mod.VENDOR_SCRIPTS:
        text = page_mod.vendor_js(name)
        assert "http://" not in text and "https://" not in text
        assert "sourceMappingURL" not in text
    for path in ASSETS.glob("*.js"):
        text = path.read_text()
        assert "</script" not in text.lower()
        assert "innerHTML" not in text and "dangerouslySetInnerHTML" not in text
        assert "http://" not in text and "https://" not in text


@needs_node
def test_every_script_parses() -> None:
    for path in [*ASSETS.glob("*.js"), *(ASSETS / "vendor").glob("*.js")]:
        done = subprocess.run([NODE, "--check", str(path)], capture_output=True, text=True)
        assert done.returncode == 0, f"{path.name}: {done.stderr}"


def test_page_has_dark_mode_phone_layout_and_reduced_motion(run_copy: Path) -> None:
    page = html_of(run_copy)
    assert "prefers-color-scheme: dark" in page
    assert 'data-theme="dark"' in page
    assert 'name="viewport"' in page
    assert "max-width: 560px" in page
    assert "prefers-reduced-motion: reduce" in page
    assert ":focus-visible" in page


@needs_node
def test_theme_toggle_flips_the_theme(run_copy: Path) -> None:
    out = run_page(
        render_report(run_copy),
        """
        const btn = H.all((n) => (n.getAttribute("aria-label") || "").startsWith("Switch to"))[0];
        const before = document.documentElement.getAttribute("data-theme");
        H.click(btn);
        await H.tick(40);
        const mid = document.documentElement.getAttribute("data-theme");
        H.click(H.all((n) => (n.getAttribute("aria-label") || "").startsWith("Switch to"))[0]);
        await H.tick(40);
        return [before, mid, document.documentElement.getAttribute("data-theme")];
        """,
    )
    assert out["result"] == [None, "dark", "light"]


@needs_node
def test_replay_marks_decisive_observed_and_broken_steps(run_copy: Path) -> None:
    out = run_page(render_report(run_copy), LAND + CELL_STATE, reduced=True)
    cells = out["result"]
    assert len(cells) == N + 1  # the base state and every step
    assert "decisive" in cells[f"cell-{BREAK_AT}"]
    assert "observed" in cells[f"cell-{OBSERVE_AT}"]
    for i in range(BREAK_AT + 1, OBSERVE_AT):
        assert "broken" in cells[f"cell-{i}"]
    for i in (1, BREAK_AT, OBSERVE_AT):
        assert "broken" not in cells[f"cell-{i}"]
    assert "good" in cells["cell-0"] and "good" in cells[f"cell-{BREAK_AT - 1}"]


def test_step_diff_is_in_the_data(run_copy: Path) -> None:
    data = data_of(html_of(run_copy))
    diff = data["steps"][BREAK_AT - 1]["diff"]
    assert "\n-    return a + b" in diff and "\n+    return a - b" in diff
    assert data["steps"][BREAK_AT - 1]["diff_truncated"] is False
    # a read-only step has no diff key; a mutating step that changed nothing has an empty one
    assert all("diff" in s for s in data["steps"] if s["mut"])


@needs_node
def test_inspector_shows_a_coloured_diff_with_line_numbers(run_copy: Path) -> None:
    out = run_page(
        render_report(run_copy),
        """
        const rows = (cls) => H.byClass("dl").filter((r) => r.className.split(/\\s+/).includes(cls));
        const del = rows("del")[0];
        const add = rows("add")[0];
        return {
          del: del.textContent, add: add.textContent,
          head: H.byClass("dhead")[0].textContent,
          hunk: rows("hunk").length,
          ribbon: H.byClass("ribbon")[0].textContent,
        };
        """,
    )
    r = out["result"]
    assert "return a + b" in r["del"] and "return a - b" in r["add"]
    assert "calc/__init__.py" in r["head"]
    assert r["hunk"] >= 1
    assert "Decisive step" in r["ribbon"]


def test_reasoning_probe_log_and_confidence_in_the_data(run_copy: Path) -> None:
    data = data_of(html_of(run_copy))
    assert "Simplify add()." in data["steps"][BREAK_AT - 1]["why"]
    m = first_member(data)
    assert m["probes"] and {p["verdict"] for p in m["probes"]} == {"pass", "fail"}
    assert m["confidence"]
    assert m["non_monotone"] is False


@needs_node
def test_details_show_probe_log_and_oracle_output(run_copy: Path) -> None:
    out = run_page(render_report(run_copy))
    text = out["text"]
    assert "Probe log" in text and "No sign of a non-monotone history" in text
    assert "Oracle output at step" in text
    assert "What the agent said before this step" in text and "Simplify add()." in text


def test_sabotage_ground_truth_in_the_data(run_copy: Path) -> None:
    data = data_of(html_of(run_copy))
    m = first_member(data)
    assert m["ground_truth"] == f"Sabotage was injected at step {BREAK_AT}."
    assert m["ground_truth_ok"] is True
    assert data["run"]["sabotage_step"] == BREAK_AT


@needs_node
def test_sabotage_is_marked_and_confirmed(run_copy: Path) -> None:
    out = run_page(render_report(run_copy), LAND + CELL_STATE, reduced=True)
    assert "sabotage" in out["result"][f"cell-{BREAK_AT}"]
    assert "Bisect found the same step." in run_page(render_report(run_copy))["text"]


@needs_node
def test_ground_truth_mismatch_is_flagged(run_copy: Path) -> None:
    meta = (run_copy / "meta.json").read_text()
    (run_copy / "meta.json").write_text(
        meta.replace(f'"sabotage_step": {BREAK_AT}', f'"sabotage_step": {BREAK_AT + 1}')
    )
    page = html_of(run_copy)
    assert first_member(data_of(page))["ground_truth_ok"] is False
    assert "Bisect found a different step." in run_page(render_report(run_copy))["text"]


@needs_node
def test_no_ai_box_without_explanation(run_copy: Path) -> None:
    page = html_of(run_copy)
    assert data_of(page)["groups"][0]["explanation"] is None
    assert "AI explanation" not in page  # the label travels with the text, not in the script
    assert "AI explanation" not in run_page(render_report(run_copy))["text"]


@needs_node
def test_ai_explanation_is_labelled(run_copy: Path) -> None:
    rep = load_report(run_copy)
    rep.culprits[0].explanation = "The edit flipped the sign."
    save_report(run_copy, rep)
    page = html_of(run_copy)
    box = data_of(page)["groups"][0]["explanation"]
    assert box["label"] == "AI explanation" and box["text"] == "The edit flipped the sign."
    text = run_page(render_report(run_copy))["text"]
    assert "AI explanation" in text
    assert "The edit flipped the sign." in text
    assert "does not affect the verdict" in text


def test_censored_lag_says_never_noticed(run_copy: Path) -> None:
    rep = load_report(run_copy)
    a = rep.culprits[0].analysis
    assert a is not None
    rep.culprits[0].analysis = a.model_copy(
        update={"t_obs": None, "lag": None, "censored": True, "observed_signal": None}
    )
    save_report(run_copy, rep)
    data = data_of(html_of(run_copy))
    assert "The agent never noticed." in data["groups"][0]["sentences"]
    stats = {s["k"]: s for s in first_member(data)["stats"]}
    assert stats["Detection lag"]["v"] == "censored" and stats["Noticed at"]["v"] == "never"
    assert first_member(data)["analysis"]["censored"] is True


@needs_node
def test_censored_run_shades_every_step_after_the_break(run_copy: Path) -> None:
    rep = load_report(run_copy)
    a = rep.culprits[0].analysis
    assert a is not None
    rep.culprits[0].analysis = a.model_copy(
        update={"t_obs": None, "lag": None, "censored": True, "observed_signal": None}
    )
    save_report(run_copy, rep)
    out = run_page(render_report(run_copy), LAND + CELL_STATE, reduced=True)
    shaded = [int(k.split("-")[1]) for k, c in out["result"].items() if "broken" in c]
    assert sorted(shaded) == list(range(BREAK_AT + 1, N + 1))
    assert "Never noticed" in run_page(render_report(run_copy))["text"]


@needs_node
def test_not_bisectable_explains_why(run_copy: Path) -> None:
    (run_copy / "bisect.json").write_text(
        RunReport(
            run_id="x",
            checks=["true"],
            culprits=[
                CulpritReport(
                    run_id="x",
                    check="true",
                    bisect=BisectResult(
                        check="true",
                        status=BisectStatus.NOT_BISECTABLE,
                        reason="oracle does not fail at the final step 10",
                    ),
                )
            ],
        ).model_dump_json()
    )
    page = html_of(run_copy)
    assert "Not bisectable" in page  # in the no-script text
    out = run_page(render_report(run_copy))
    assert "Not bisectable" in out["text"]
    assert "oracle does not fail at the final step 10" in out["text"]
    assert "Bisect replay" not in out["text"]  # no probes: nothing to replay


def _sibling(rep: RunReport, name: str, step: int | None = None) -> CulpritReport:
    """A copy of the first culprit for another test, optionally broken at a different step."""
    sib = rep.culprits[0].model_copy(deep=True)
    sib.check = f"python -m pytest -q tests/test_calc.py::{name}"
    if step is not None:
        sib.bisect.decisive_step = step
        assert sib.analysis is not None
        sib.analysis = sib.analysis.model_copy(
            update={"t_err": step, "steps_after_err": N - step, "t_obs": OBSERVE_AT, "lag": 1}
        )
        sib.step = None
        sib.diff = ""
    rep.culprits.append(sib)
    return sib


@needs_node
def test_culprits_at_different_steps_get_one_tab_each(run_copy: Path) -> None:
    rep = load_report(run_copy)
    _sibling(rep, "test_other", step=6)
    save_report(run_copy, rep)
    page = html_of(run_copy)
    data = data_of(page)
    assert [g["decisive"] for g in data["groups"]] == [BREAK_AT, 6]
    assert [g["id"] for g in data["groups"]] == ["culprit-1", "culprit-2"]
    assert data["n_found"] == 2 and data["n_checks"] == 2
    out = run_page(render_report(run_copy))
    assert f"Step {BREAK_AT} · test_add" in out["text"]
    assert "Step 6 · test_other" in out["text"]
    clicked = run_page(
        render_report(run_copy),
        """
        const tab = H.byClass("tab")[1];
        H.click(tab);
        await H.tick(40);
        return H.byClass("headline")[0].textContent;
        """,
    )
    assert "Step 6" in clicked["result"] and "test_other" in clicked["result"]


@needs_node
def test_culprits_at_the_same_step_share_one_verdict(run_copy: Path) -> None:
    rep = load_report(run_copy)
    _sibling(rep, "test_b")
    _sibling(rep, "test_c")
    save_report(run_copy, rep)
    data = data_of(html_of(run_copy))
    assert len(data["groups"]) == 1
    g = data["groups"][0]
    assert g["sentences"][0] == (
        f"Step {BREAK_AT} (edit calc/__init__.py) broke 3 tests: test_add, test_b, test_c."
    )
    assert sum(s.startswith("The agent only noticed at step") for s in g["sentences"]) == 1
    assert g["shared"] and g["same_probes"]
    assert [m["id"] for m in g["members"]] == ["culprit-1", "culprit-2", "culprit-3"]
    assert all(m["probes"] for m in g["members"])  # a probe log for each test
    out = run_page(render_report(run_copy))
    assert "Checks (3)" in out["text"]
    assert out["text"].count("Probe log") == 1  # one replay and one log, for the same probes


def test_group_lists_five_tests_then_counts_the_rest(run_copy: Path) -> None:
    rep = load_report(run_copy)
    for i in range(6):
        _sibling(rep, f"test_x{i}")
    save_report(run_copy, rep)
    first = data_of(html_of(run_copy))["groups"][0]["sentences"][0]
    assert "broke 7 tests: test_add, test_x0, test_x1, test_x2, test_x3, and 2 more." in first


@needs_node
def test_group_with_different_lags_gets_a_test_picker(run_copy: Path) -> None:
    rep = load_report(run_copy)
    sib = _sibling(rep, "test_b")
    assert sib.analysis is not None
    sib.analysis = sib.analysis.model_copy(update={"t_obs": 9, "lag": 5})
    save_report(run_copy, rep)
    g = data_of(html_of(run_copy))["groups"][0]
    assert "broke 2 tests: test_add, test_b." in g["sentences"][0]
    joined = " ".join(g["sentences"])
    assert "test_add: The agent only noticed at step 7" in joined
    assert "test_b: The agent only noticed at step 9" in joined
    assert g["shared"] is False
    out = run_page(
        render_report(run_copy),
        """
        const select = H.byTag("select")[0];
        select.value = "1";
        select.dispatchEvent({ type: "change", target: select });
        await H.tick(40);
        return H.byClass("stats")[0].textContent;
        """,
    )
    assert "9" in out["result"]  # the second test was noticed at step 9


def test_break_on_the_last_step_gets_its_own_wording(run_copy: Path) -> None:
    rep = load_report(run_copy)
    c = rep.culprits[0]
    c.bisect.decisive_step = N
    assert c.analysis is not None
    c.analysis = c.analysis.model_copy(
        update={
            "t_err": N,
            "t_obs": None,
            "lag": None,
            "censored": True,
            "steps_after_err": 0,
            "wasted_tokens": 0,
            "observed_signal": None,
        }
    )
    c.step = None
    c.diff = ""
    save_report(run_copy, rep)
    page = html_of(run_copy)
    sentences = data_of(page)["groups"][0]["sentences"]
    assert "This was the agent's last step, so it had no chance to see the failure." in sentences
    assert "The run ended with the test broken." in sentences
    text = " ".join(sentences)
    assert "never noticed" not in text and "0 more steps" not in text
    stats = {s["k"]: s for s in first_member(data_of(page))["stats"]}
    assert stats["Detection lag"]["small"] == "(last step)"


def test_censored_with_steps_left_keeps_the_old_wording() -> None:
    b = BisectResult(check="pytest a.py::test_x", status=BisectStatus.FOUND, decisive_step=5)
    a = Analysis(
        t_err=5,
        t_obs=None,
        lag=None,
        censored=True,
        steps_after_err=3,
        wasted_tokens=900,
        total_tokens=3000,
    )
    text = " ".join(
        verdict_sentences(CulpritReport(run_id="r", check=b.check, bisect=b, analysis=a), None)
    )
    assert "The agent never noticed. It kept working for 3 more steps on a broken base" in text
    assert "last step" not in text


@needs_node
def test_non_monotone_note_and_probes_without_a_shrinking_interval(run_copy: Path) -> None:
    rep = load_report(run_copy)
    rep.culprits[0].bisect.non_monotone = True
    rep.culprits[0].bisect.transitions = [2, BREAK_AT]
    save_report(run_copy, rep)
    m = first_member(data_of(html_of(run_copy)))
    assert m["non_monotone"] and m["transitions"] == [2, BREAK_AT]
    assert m["replay"]["mono"] is False
    assert all(f["lo"] is None and f["hi"] is None for f in m["replay"]["frames"])
    text = run_page(render_report(run_copy))["text"]
    assert "Non-monotone history" in text
    assert "steps 2, 4" in text


@needs_node
def test_missing_bisect_json_still_renders(run_copy: Path) -> None:
    (run_copy / "bisect.json").unlink()
    page = html_of(run_copy)
    assert data_of(page)["overview"]["title"] == "Not bisected yet"
    out = run_page(render_report(run_copy), 'return H.byClass("cell").length;')
    assert "Not bisected yet" in out["text"]
    assert "Step explorer" in out["text"] and "Bisect replay" not in out["text"]
    assert "The run, step by step" in out["text"]
    assert out["result"] == N + 1  # a plain strip of the base state and every step
    assert len(data_of(page)["steps"]) == N


@needs_node
def test_empty_culprit_list_renders(run_copy: Path) -> None:
    save_report(run_copy, RunReport(run_id="x", checks=[], culprits=[], auto_oracle=True))
    page = html_of(run_copy)
    assert data_of(page)["overview"]["title"] == "No culprit found"
    assert "No culprit found" in run_page(render_report(run_copy))["text"]


@needs_node
def test_missing_shadow_repo_still_renders_without_diff(run_copy: Path) -> None:
    rep = load_report(run_copy)
    rep.culprits[0].diff = ""
    save_report(run_copy, rep)
    shutil.rmtree(run_copy / "shadow.git")
    data = data_of(html_of(run_copy))
    assert data["steps"][BREAK_AT - 1]["diff"] == ""
    assert "No file changes in this step" in run_page(render_report(run_copy))["text"]


# --------------------------------------------------------------------------- replay data


def test_replay_has_one_frame_per_probe_and_narrows_to_the_culprit(run_copy: Path) -> None:
    rep = load_report(run_copy)
    data = data_of(html_of(run_copy))
    replay = first_member(data)["replay"]
    probes = rep.culprits[0].bisect.probes
    frames = replay["frames"]
    assert [f["step"] for f in frames] == [p.step_index for p in probes]
    assert [f["verdict"] for f in frames] == [p.verdict.value for p in probes]
    assert [f["runs"] for f in frames] == [p.n_runs for p in probes]
    assert [f["fails"] for f in frames] == [p.n_fail for p in probes]
    assert replay["mono"] and not replay["reconstructed"] and replay["window_end"] == N
    # the unknown interval only shrinks, and ends one step wide at the culprit
    spans = [f["hi"] - f["lo"] for f in frames]
    assert spans == sorted(spans, reverse=True)
    assert frames[-1]["lo"] == BREAK_AT - 1 and frames[-1]["hi"] == BREAK_AT


@needs_node
def test_replay_plays_probe_by_probe_with_the_keyboard(run_copy: Path) -> None:
    n_probes = len(first_member(data_of(html_of(run_copy)))["replay"]["frames"])
    out = run_page(
        render_report(run_copy),
        """
        const status = () => H.byClass("rp-status")[0].textContent;
        const seen = [status()];
        H.docKey("ArrowRight"); await H.tick(30); seen.push(status());
        H.docKey("ArrowRight"); await H.tick(30); seen.push(status());
        H.docKey("ArrowLeft"); await H.tick(30); seen.push(status());
        H.docKey("Home"); await H.tick(30); seen.push(status());
        for (let i = 0; i < 20; i++) { H.docKey("ArrowRight"); await H.tick(10); }
        seen.push(status());
        return seen;
        """,
    )
    ready, p1, p2, back, reset, last = out["result"]
    assert "READY" in ready and "Replay the search" in ready
    assert "Probe 1 of" in p1 and "Probe 2 of" in p2 and "Probe 1 of" in back
    assert "READY" in reset
    assert "FOUND" in last and f"Found step {BREAK_AT} in {n_probes} probes" in last


@needs_node
def test_replay_plays_by_itself_and_can_pause(run_copy: Path) -> None:
    out = run_page(
        render_report(run_copy),
        """
        const status = () => H.byClass("rp-status")[0].textContent;
        const play = H.byLabel("Play the replay")[0];
        H.click(play);
        await H.tick(700);
        const early = status();
        H.click(H.byLabel("Pause the replay")[0]);
        const paused = status();
        await H.tick(1500);
        return [early, paused, status()];
        """,
    )
    early, paused, later = out["result"]
    assert "Probe 1 of" in early
    assert later == paused  # paused: no further probes


@needs_node
def test_reduced_motion_jumps_to_the_end(run_copy: Path) -> None:
    out = run_page(
        render_report(run_copy),
        """
        H.click(H.byLabel("Play the replay")[0]);
        await H.tick(40);
        return H.byClass("rp-status")[0].textContent;
        """,
        reduced=True,
    )
    assert "FOUND" in out["result"]


@needs_node
def test_clicking_a_step_inspects_it_and_arrow_keys_move_on(run_copy: Path) -> None:
    out = run_page(
        render_report(run_copy),
        """
        const head = () => H.byClass("ins-title")[0].textContent;
        const seen = [head()];
        const cell = H.byClass("cell")[2];
        H.click(cell);
        await H.tick(30);
        seen.push(head());
        H.key(H.byClass("cell")[2], "ArrowRight");
        await H.tick(30);
        seen.push(head());
        return seen;
        """,
    )
    start, clicked, moved = out["result"]
    assert start.startswith(f"Step {BREAK_AT}")  # the verdict's step is selected first
    assert clicked.startswith("Step 2") and moved.startswith("Step 3")


@needs_node
def test_search_and_changed_only_filter_the_step_list(run_copy: Path) -> None:
    mutating = sum(1 for s in data_of(html_of(run_copy))["steps"] if s["mut"])
    out = run_page(
        render_report(run_copy),
        """
        const count = () => H.byClass("count")[0].textContent;
        const seen = [count()];
        H.input(H.byTag("input").filter((i) => i.getAttribute("type") === "search")[0], "Simplify add");
        await H.tick(30);
        seen.push(count());
        H.input(H.byTag("input").filter((i) => i.getAttribute("type") === "search")[0], "");
        const box = H.byTag("input").filter((i) => i.getAttribute("type") === "checkbox")[0];
        box.checked = true;
        box.dispatchEvent({ type: "change", target: box });
        await H.tick(30);
        seen.push(count());
        return seen;
        """,
    )
    everything, searched, only = out["result"]
    assert everything == f"Showing {N} of {N} steps"
    assert searched == f"Showing 1 of {N} steps"
    assert only == f"Showing {mutating} of {N} steps"


@needs_node
def test_diff_parser_numbers_lines_and_files() -> None:
    sample = (
        "diff --git a/x.py b/x.py\\nindex 1..2 100644\\n--- a/x.py\\n+++ b/x.py\\n"
        "@@ -3,3 +3,3 @@ def f():\\n ctx\\n-old\\n+new\\n tail\\n"
        "diff --git a/n.txt b/n.txt\\nnew file mode 100644\\n--- /dev/null\\n+++ b/n.txt\\n"
        "@@ -0,0 +1,2 @@\\n+a\\n+b\\n\\\\ No newline at end of file\\n"
    )
    with tempfile.TemporaryDirectory() as tmp:
        stub = Path(tmp) / "stub.html"
        stub.write_text(
            '<script type="application/json" id="data">{"steps":[],"groups":[],"episodes":[],'
            '"run":{"id":"r","source":"s","n_steps":0,"n_mutating":0,"tokens":"0","tokens_exact":"0",'
            '"created":"x","sabotage_step":null}}</script>'
            f"<script>{page_mod.vendor_js('preact.min.js')}</script>"
            f"<script>{page_mod.vendor_js('hooks.umd.js')}</script>"
            f"<script>{page_mod.vendor_js('htm.js')}</script>"
            f"<script>{(ASSETS / 'common.js').read_text()}</script>"
        )
        out = run_page(stub, f'return AB.parseDiff("{sample}");')
    files = out["result"]
    assert [f["path"] for f in files] == ["x.py", "n.txt"]
    assert files[0]["add"] == 1 and files[0]["del"] == 1 and files[1]["status"] == "added"
    rows = files[0]["rows"]
    assert [r["k"] for r in rows] == ["hunk", "ctx", "del", "add", "ctx"]
    assert (rows[1]["a"], rows[1]["b"]) == (3, 3) and rows[2]["a"] == 4 and rows[3]["b"] == 4
    assert rows[4]["a"] == 5 and rows[4]["b"] == 5
    assert [r["k"] for r in files[1]["rows"]] == ["hunk", "add", "add", "note"]


# --------------------------------------------------------------------------- determinism, size


def test_two_renders_are_byte_identical(run_copy: Path) -> None:
    first = render_report(run_copy).read_bytes()
    stamp = (run_copy / "report.html").stat().st_mtime_ns
    second = render_report(run_copy).read_bytes()
    assert first == second
    assert (run_copy / "report.html").stat().st_mtime_ns == stamp  # not even rewritten


def test_data_blob_cannot_close_the_script_element(run_copy: Path) -> None:
    evil = "</script><script>alert(1)</script><!-- & \u00e9\U0001f600"
    trace = Trace.load(run_copy)
    trace.steps[0].output = evil
    trace.save(run_copy)
    page = html_of(run_copy)
    blob = DATA_RE.search(page)
    assert blob is not None
    assert "<" not in blob.group(1) and ">" not in blob.group(1) and "&" not in blob.group(1)
    assert data_of(page)["steps"][0]["out"] == evil  # and it still round-trips


def test_ordinal_demo_page_stays_small(tmp_path: Path) -> None:
    demo = REPO_ROOT / "runs" / "demo" / "humanize-ordinal-fix"
    if not (demo / "trace.jsonl").exists():
        pytest.skip("the demo runs are not in this checkout")
    dest = tmp_path / "ordinal"
    shutil.copytree(demo, dest, ignore=shutil.ignore_patterns("scratch", "report.html"))
    page = render_report(dest).read_text(encoding="utf-8")
    assert len(page.encode()) < 3_000_000
    data = data_of(page)
    assert len(data["steps"]) == data["run"]["n_steps"] > 50
    assert sum(len(s.get("diff") or "") for s in data["steps"]) <= render_mod.DIFF_BUDGET


def test_diff_caps_mark_truncated_and_skipped_steps(
    run_copy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    culprit_len = len(data_of(html_of(run_copy))["steps"][BREAK_AT - 1]["diff"])
    monkeypatch.setattr(render_mod, "MAX_STEP_DIFF", 20)
    monkeypatch.setattr(render_mod, "MAX_CULPRIT_DIFF", 10_000)
    monkeypatch.setattr(render_mod, "DIFF_BUDGET", culprit_len + 30)
    steps = data_of(html_of(run_copy))["steps"]
    assert any(s.get("diff_truncated") for s in steps)
    assert any(s.get("diff_skipped") and s["diff"] is None for s in steps)
    assert steps[BREAK_AT - 1]["diff"]  # the culprit step keeps its diff whatever the budget
    assert all(len(s["diff"]) <= 20 for s in steps if s.get("diff") and s["i"] != BREAK_AT)


# --------------------------------------------------------------------------- untrusted text


@needs_node
def test_trace_content_is_escaped(run_copy: Path) -> None:
    evil = "<script>alert('x')</script>"
    trace = Trace.load(run_copy)
    trace.steps[0].output = f"{evil} </pre><img src=x onerror=alert(1)>"
    trace.steps[0].reasoning = evil
    trace.steps[0].args = {"path": evil}
    trace.steps[BREAK_AT - 1].reasoning = f"{evil} </pre>"
    trace.save(run_copy)
    rep = load_report(run_copy)
    rep.culprits[0].diff = f"--- a/f\n+++ b/f\n@@ -1 +1 @@\n-{evil}\n+</pre><b>x</b>\n"
    rep.culprits[0].explanation = evil
    rep.culprits[0].check = f"pytest {evil}"
    rep.culprits[0].step = None
    save_report(run_copy, rep)
    page = html_of(run_copy)
    assert evil not in page
    assert "<img src=x" not in page and "<b>x</b>" not in page
    blob = DATA_RE.search(page)
    assert blob is not None and "<" not in blob.group(1)
    # the no-script text is escaped by the template
    assert "&lt;script&gt;alert(&#39;x&#39;)&lt;/script&gt;" in page
    # the app draws it as text: no script or img element appears, and no handler attribute
    out = run_page(
        render_report(run_copy),
        """
        H.click(H.byClass("cell")[1]);
        await H.tick(30);
        const first = H.byClass("inspector")[0].textContent;
        H.click(H.byClass("cell")[BREAK]);
        await H.tick(30);
        return [first, H.byClass("inspector")[0].textContent];
        """.replace("BREAK", str(BREAK_AT)),
    )
    assert "script" not in out["tags"] and "img" not in out["tags"]
    assert "onerror" not in out["attrNames"]
    first, second = out["result"]
    assert evil in first  # shown, as text
    assert "</pre><b>x</b>" in second
    assert evil in out["text"]


# --------------------------------------------------------------------------- helpers


def test_fmt_helpers() -> None:
    assert fmt_tokens(830) == "830"
    assert fmt_tokens(41_200) == "41k"
    assert fmt_tokens(1_500) == "1.5k"
    assert fmt_tokens(2_000) == "2k"
    assert fmt_tokens(1_250_000) == "1.2M"
    assert fmt_percent(0.5) == "50%"
    assert fmt_percent(0.034) == "3.4%"


def test_check_label_picks_the_test_name() -> None:
    assert check_label("pytest tests/test_auth.py::test_login -q") == "test_login"
    assert check_label("python -m pytest tests/test_auth.py") == "tests/test_auth.py"
    assert check_label("make check") == "make check"
    assert check_label("pytest 'unclosed") == "pytest 'unclosed"


def test_verdict_sentences_cover_lag_zero_and_censored() -> None:
    b = BisectResult(check="pytest a.py::test_x", status=BisectStatus.FOUND, decisive_step=9)
    base = {"t_err": 9, "steps_after_err": 3, "wasted_tokens": 41_000, "total_tokens": 100_000}
    now = Analysis(t_obs=9, lag=0, censored=False, **base)
    never = Analysis(t_obs=None, lag=None, censored=True, **base)
    c = CulpritReport(run_id="r", check=b.check, bisect=b, analysis=now)
    assert "saw the failure at once" in " ".join(verdict_sentences(c, None))
    c.analysis = never
    text = " ".join(verdict_sentences(c, None))
    assert "The agent never noticed." in text
    assert "41k tokens" in text and "41%" in text
    c.analysis = None
    assert verdict_sentences(c, None) == ["Step 9 broke test_x."]


# --------------------------------------------------------------------------- repaired breaks


@pytest.fixture
def repaired_run(tmp_path: Path) -> Path:
    """Break at step 4, seen at step 7, fixed at step 9; no culprit, one repaired episode."""
    run = tmp_path / "run"
    make_synthetic_run(run, n_steps=10, break_at=4, fix_at=9, observe_at=7)
    episode = RepairEpisode(
        run_id="r", check=CHECK, seen_at=7, t_err=4, lag=3, wasted_tokens=112_000
    )
    save_report(run, RunReport(run_id="r", checks=[CHECK], repaired=[episode]))
    return run


def test_repaired_sentences() -> None:
    ep = RepairEpisode(run_id="r", check="pytest a.py::test_x", seen_at=13, t_err=9, lag=4)
    ep.wasted_tokens = 112_000
    assert repaired_sentences(ep, None) == [
        "Step 9 broke test_x.",
        "The agent saw it at step 13 and fixed it before the end.",
        "4 steps and 112k tokens went into broken code first.",
    ]
    ep.lag, ep.seen_at = 0, 9
    assert "saw it at once" in " ".join(repaired_sentences(ep, None))
    lost = RepairEpisode(run_id="r", check=ep.check, seen_at=13, reason="no clean base.")
    assert repaired_sentences(lost, None)[1] == (
        "The step that broke it was not found: no clean base."
    )


def test_report_shows_repaired_episode_in_the_data(repaired_run: Path) -> None:
    data = data_of(html_of(repaired_run))
    assert data["overview"]["title"] == "The agent broke a test and fixed it"
    assert data["groups"] == [] and len(data["episodes"]) == 1
    e = data["episodes"][0]
    assert e["sentences"] == [
        "Step 4 (edit calc/__init__.py) broke test_add.",
        "The agent saw it at step 7 and fixed it before the end.",
        "3 steps and 112k tokens went into broken code first.",
    ]
    assert "E       assert -1 == 5" in e["seen_output"]
    assert "\n+    return a - b" in data["steps"][3]["diff"]
    stats = {s["k"]: s["v"] for s in e["stats"]}
    assert stats == {
        "Broke at step": "4",
        "Seen at step": "7",
        "Detection lag": "3",
        "Tokens in broken code": "112k",
    }


def test_repaired_replay_is_reconstructed_and_ends_on_the_break(repaired_run: Path) -> None:
    replay = data_of(html_of(repaired_run))["episodes"][0]["replay"]
    assert replay["reconstructed"] is True and replay["window_end"] == 7
    frames = replay["frames"]
    assert [f["role"] for f in frames[:2]] == ["endpoint", "endpoint"]
    assert (frames[0]["step"], frames[0]["verdict"]) == (0, "pass")
    assert frames[1]["verdict"] == "fail" and frames[1]["runs"] is None
    # every verdict follows from the known answer: steps before it pass, from it on fail
    assert all((f["verdict"] == "fail") == (f["step"] >= 4) for f in frames)
    assert frames[-1]["lo"] == 3 and frames[-1]["hi"] == 4


@needs_node
def test_report_draws_repaired_section(repaired_run: Path) -> None:
    out = run_page(render_report(repaired_run), LAND + CELL_STATE, reduced=True)
    text = out["text"]
    assert "The agent broke a test and fixed it" in text
    assert "No culprit found" not in text
    assert "Step 4 (edit calc/__init__.py) broke test_add." in text
    assert "The agent saw it at step 7 and fixed it before the end." in text
    assert "3 steps and 112k tokens went into broken code first." in text
    assert "What the agent saw at step 7" in text and "E       assert -1 == 5" in text
    assert "reconstructed" in text and "Nothing was re-run" in text
    cells = out["result"]
    assert "decisive" in cells["cell-4"] and "observed" in cells["cell-7"]
    assert "outside" in cells["cell-9"]  # after the window of the search
    assert "Decisive step: the first step" not in text  # the wording for a fixed break differs


@needs_node
def test_repaired_section_escapes_untrusted_text(repaired_run: Path) -> None:
    trace_file = repaired_run / "trace.jsonl"
    text = trace_file.read_text().replace("E       assert -1 == 5", "<script>alert(1)</script>")
    trace_file.write_text(text)
    page = html_of(repaired_run)
    assert "<script>alert(1)</script>" not in page
    out = run_page(render_report(repaired_run))
    assert "script" not in out["tags"]
    assert "<script>alert(1)</script>" in out["text"]


@needs_node
def test_repaired_episode_without_t_err_shows_reason(repaired_run: Path) -> None:
    lost = RepairEpisode(run_id="r", check=CHECK, seen_at=7, reason="The base already failed <b>")
    save_report(repaired_run, RunReport(run_id="r", checks=[CHECK], repaired=[lost]))
    page = html_of(repaired_run)
    e = data_of(page)["episodes"][0]
    assert e["found"] is False and e["replay"] is None
    out = run_page(render_report(repaired_run))
    assert "Step that broke it not found" in out["text"]
    assert "The base already failed <b>" in out["text"]


# --------------------------------------------------------------------------- short step labels

RUN = Path("/Users/me/runs/r1")


def _meta() -> TraceMeta:
    return TraceMeta(
        run_id="r1",
        source=TraceSource.ALAN_CODE,
        workdir=RUN / "workdir",
        base_snapshot="0" * 40,
        python=RUN / "venv" / "bin" / "python",
    )


def _shell(command: str, **args: str) -> Step:
    return Step(index=3, tool="bash", kind=ToolKind.SHELL, args={"command": command, **args})


def test_shortener_drops_workdir_python_and_run_dir() -> None:
    short = Shortener.for_run(_meta(), RUN)
    cmd = f"'{RUN}/venv/bin/python' -m pytest {RUN}/workdir/tests/test_a.py -q"
    assert short(cmd) == "python -m pytest tests/test_a.py -q"
    assert short(f"cd {RUN}/workdir && ls {RUN}/other") == "cd . && ls <run>/other"
    assert short(f"{RUN}/venv/bin/python -V") == "python -V"
    spaced = Shortener.for_run(_meta().model_copy(update={"workdir": Path("/a b/run/workdir")}))
    assert spaced("ls -R /a\\ b/run/workdir/src") == "ls -R src"
    assert short("/usr/bin/python3 -V") == "/usr/bin/python3 -V"
    assert short(f"{RUN}/workdir-old/x") == "<run>/workdir-old/x"


def test_step_label_shortens_before_cutting_to_length() -> None:
    short = Shortener.for_run(_meta(), RUN)
    step = _shell(f"'{RUN}/venv/bin/python' -m pytest tests/test_filesize.py -q -x")
    assert step_label(step, short) == "shell: python -m pytest tests/test_filesize.py -q -x"
    assert RUN.as_posix() in step_label(step)  # without a shortener the label is unchanged
    edit = Step(
        index=4,
        tool="edit",
        kind=ToolKind.EDIT,
        args={"path": f"{RUN}/workdir/src/humanize/filesize.py"},
    )
    assert step_label(edit, short) == "edit src/humanize/filesize.py"


def test_report_shortens_labels_but_not_raw_output(run_copy: Path) -> None:
    trace = Trace.load(run_copy)
    workdir = trace.meta.workdir
    py = run_copy / "venv" / "bin" / "python"
    trace.meta = trace.meta.model_copy(update={"python": py})
    step = trace.steps[1]
    step.kind = ToolKind.SHELL
    step.args = {"command": f"'{py}' -m pytest {workdir}/tests -q"}
    step.output = f"raw output from {workdir}/tests/test_calc.py"
    trace.save(run_copy)
    steps = build_data(run_copy)["steps"]
    assert steps[1]["label"] == "shell: python -m pytest tests -q"
    assert steps[1]["out"] == f"raw output from {workdir}/tests/test_calc.py"
