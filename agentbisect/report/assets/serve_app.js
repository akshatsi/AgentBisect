/* The local app page (`agentbisect serve`): set up a run, copy the folder and prompt into Alan
   Code, find the step, list the runs. It talks to the same-origin /api/ routes with the access
   token from the address (#token=...), which it removes from the address bar at once.
   Everything from the server (run titles, logs, verdicts) is drawn as text nodes. */
(function () {
  "use strict";
  var A = AB;
  var html = A.html;
  var useState = A.useState;
  var useEffect = A.useEffect;
  var useRef = A.useRef;
  var useCallback = A.useCallback;
  var cx = A.cx;
  var C = A.cmd;

  var TOKEN_HEADER = "X-AgentBisect-Token";
  var NO_TOKEN = "Open the link printed in your terminal; it carries the access token.";
  var LOG_CAP = 20000;
  /* One second between polls. A test sets window.AB_POLL_MS before this script to go faster. */
  var POLL_MS = typeof window.AB_POLL_MS === "number" ? window.AB_POLL_MS : 1000;

  /* ---------------------------------------------------------------- api */

  function readToken() {
    var token = "";
    var m = /(?:^#|&)token=([^&]*)/.exec(String(location.hash || ""));
    if (m) {
      try { token = decodeURIComponent(m[1]); } catch (e) { token = m[1]; }
      try { history.replaceState(null, "", (location.pathname || "/") + (location.search || "")); } catch (e) { /* keep going */ }
      try { sessionStorage.setItem("ab-token", token); } catch (e) { /* storage may be blocked */ }
    } else {
      try { token = sessionStorage.getItem("ab-token") || ""; } catch (e) { /* storage may be blocked */ }
    }
    return token;
  }
  var TOKEN = readToken();

  /* fetch with the token header. Resolves to the JSON body; rejects with an Error whose message
     is fit to show, and whose `status` is the HTTP status (0 when the app is unreachable). */
  function api(path, opts) {
    opts = opts || {};
    var headers = { Accept: "application/json" };
    headers[TOKEN_HEADER] = TOKEN;
    var init = { method: opts.method || "GET", headers: headers };
    if (opts.body !== undefined) {
      headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(opts.body);
    }
    return fetch(path, init).then(
      function (res) {
        return res.json().then(
          function (d) { return d; },
          function () { return null; }
        ).then(function (data) {
          if (res.ok) return data || {};
          var msg = res.status === 403 ? NO_TOKEN : (data && typeof data.error === "string" && data.error) || "The app answered with an error (" + res.status + ").";
          var err = new Error(msg);
          err.status = res.status;
          throw err;
        });
      },
      function () {
        var err = new Error("Cannot reach the local app. Is agentbisect serve still running?");
        err.status = 0;
        throw err;
      }
    );
  }

  function runPath(id, tail) {
    return "/api/runs/" + encodeURIComponent(id) + (tail || "");
  }
  function reportHref(id) {
    return "/runs/" + encodeURIComponent(id) + "/report.html";
  }
  var STUDY_HREF = "/runs/study/study.html";

  /* ---------------------------------------------------------------- jobs */

  /* One background job: start(jobId) polls /api/jobs/<id> until it is done or failed, then calls
     onSettled(job). A poll that fails three times in a row (or is refused with 403) ends the
     job as failed. */
  function useJob(onSettled) {
    var st = useState(null);
    var alive = useRef(true);
    var gen = useRef(0);
    var cb = useRef(onSettled);
    cb.current = onSettled;
    useEffect(function () {
      alive.current = true;
      return function () { alive.current = false; };
    }, []);
    var start = useCallback(function (jobId) {
      var mine = ++gen.current;
      var fails = 0;
      function current() { return alive.current && mine === gen.current; }
      st[1]({ id: jobId, status: "queued", log: "", run_id: null, error: null });
      function poll() {
        api("/api/jobs/" + encodeURIComponent(jobId)).then(
          function (j) {
            if (!current()) return;
            fails = 0;
            st[1](j);
            if (j.status === "done" || j.status === "error") cb.current(j);
            else setTimeout(poll, POLL_MS);
          },
          function (e) {
            if (!current()) return;
            if (e.status === 403 || ++fails >= 3) {
              var failed = { id: jobId, status: "error", log: "", run_id: null, error: e.message };
              st[1](function (prev) { return Object.assign({}, prev || {}, failed, { log: (prev && prev.log) || "" }); });
              cb.current(failed);
            } else {
              setTimeout(poll, POLL_MS);
            }
          }
        );
      }
      poll();
    }, []);
    var clear = useCallback(function () {
      gen.current++;
      st[1](null);
    }, []);
    return { job: st[0], start: start, clear: clear };
  }

  var STATUS = { queued: "Queued", running: "Running", done: "Done", error: "Failed" };
  var STATUS_TONE = { queued: "idle", running: "error", done: "pass", error: "fail" };

  function JobBox(props) {
    var j = props.job;
    var box = useRef(null);
    var stick = useRef(true);
    var log = j.log || "";
    var shown = log.length > LOG_CAP ? "... earlier output left out ...\n" + log.slice(-LOG_CAP) : log;
    useEffect(function () {
      var el = box.current;
      if (el && stick.current) el.scrollTop = el.scrollHeight;
    }, [shown]);
    function onScroll(e) {
      var el = e.target;
      stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 24;
    }
    var running = j.status === "queued" || j.status === "running";
    return html`<div class="jobbox">
      <div class="jobhead">
        <span class=${cx("pill", STATUS_TONE[j.status] || "idle")}>${STATUS[j.status] || j.status}</span>
        <span class="small muted" role="status">${props.what}${running ? " ..." : ""}</span>
      </div>
      ${shown ? html`<pre class="code joblog" ref=${box} onScroll=${onScroll} tabindex="0" role="log" aria-label=${props.what + " log"}>${shown}</pre>` : null}
      ${j.status === "error" ? html`<p class="notice warn" role="alert">${j.error || "The job failed."}</p>` : null}
    </div>`;
  }

  /* ---------------------------------------------------------------- verdicts */

  /* The chip of a run: red for a culprit, green when the break was caught and fixed or nothing
     failed, amber when it could not be bisected, grey when it was not analysed. */
  function verdictOf(r) {
    if (!r.has_bisect) return { tone: "idle", text: "Not analyzed" };
    var v = String(r.verdict || "").trim() || "Analyzed";
    var low = v.toLowerCase();
    var tone = "fail";
    if (/not bisectable|could not|cannot|error|unable/.test(low)) tone = "error";
    else if (/fixed|repaired|caught|no failing|no newly|all tests pass|passes|nothing broke|no break/.test(low)) tone = "pass";
    return { tone: tone, text: v };
  }

  function VerdictChip(props) {
    var v = verdictOf(props.run);
    return html`<span class=${cx("pill", "vchip", v.tone)} title=${v.text}>${v.text}</span>`;
  }

  /* ---------------------------------------------------------------- new run */

  function NewRun(props) {
    var rf = A.useRunForm();
    var ex = useState("");
    var err = useState("");
    var sending = useState(false);
    var busy = props.busy || sending[0];

    function pick(e) {
      var id = e.target.value;
      ex[1](id);
      var preset = C.PRESETS.filter(function (p) { return p.id === id; })[0];
      if (preset) rf.load(preset.form);
    }
    function submit(e) {
      e.preventDefault();
      err[1]("");
      var v = rf.values;
      if (!v) {
        rf.touchAll();
        err[1]("Fix the fields marked in red, then try again.");
        return;
      }
      var body = { repo: v.repo, prompt: v.prompt, model: v.model };
      if (v.ref) body.ref = v.ref;
      if (v.noSetup) body.no_setup = true;
      if (v.plant) body.sabotage = { at_step: v.atStep, file: v.file, find: v.find, replace: v.replace };
      sending[1](true);
      api("/api/record", { method: "POST", body: body }).then(
        function (d) {
          sending[1](false);
          props.onStarted(d.job_id);
        },
        function (x) {
          sending[1](false);
          err[1](x.message);
        }
      );
    }
    var job = props.job;
    return html`<section class="section" id="new-run" aria-labelledby="new-run-h"><div class="card panel">
      <h2 class="t" id="new-run-h">1. New run</h2>
      <p class="lede">Copy a project, install it, and get it ready for the agent.</p>
      <div class="notice warn tryfirst" role="note">
        <b>Security.</b> Installing a project's dependencies runs its build code. Use repositories you trust, or add <code>--no-setup</code>.
      </div>
      <p class="small muted trylimits">Limits: Python and pytest only. The tests must not need a database or a network. The project must install on Python 3.11.</p>
      <form class="tryform" onSubmit=${submit} aria-label="New run" noValidate>
        <${A.RunFields} rf=${rf} prefix="new" onEdit=${function () { ex[1](""); }} noSetupLabel="Skip installing dependencies (--no-setup)"
          lead=${html`<${A.Field} id="new-example" label="Examples" required=${true} hint="Fills the form below." control=${function (p) {
            return html`<select ...${Object.assign({}, p, { "aria-required": null })} value=${ex[0]} onChange=${pick}>
              <option value="">Choose an example</option>
              ${C.PRESETS.map(function (x) { return html`<option value=${x.id} key=${x.id}>${x.label}</option>`; })}
            </select>`;
          }} />`} />
        <div class="field wide submitrow">
          <button type="submit" class="btn primary" disabled=${busy}>${busy ? "Setting up..." : "Set up the run"}</button>
          ${err[0] ? html`<p class="ferr" role="alert">${err[0]}</p>` : null}
        </div>
      </form>
      ${job ? html`<${JobBox} job=${job} what="Setting up the run" />` : null}
    </div></section>`;
  }

  /* ---------------------------------------------------------------- next step */

  function NextStep(props) {
    var run = props.run;
    var d = props.detail;
    var el = useRef(null);
    var id = run ? run.id : "";
    useEffect(function () {
      var node = el.current;
      if (id && node && node.scrollIntoView) {
        try { node.scrollIntoView({ block: "start", behavior: A.reducedMotion() ? "auto" : "smooth" }); } catch (e) { /* not fatal */ }
      }
    }, [id]);
    if (!run) return null;
    var aj = props.analyzeJob;
    var analyzing = !!aj && (aj.status === "queued" || aj.status === "running");
    var name = run.title || run.id;
    return html`<section class="section" id="next-step" aria-labelledby="next-h" ref=${el}><div class="card panel nextcard">
      <h2 class="t" id="next-h">2. Next step: run the agent</h2>
      <p class="lede">Run <b>${name}</b>${run.title && run.title !== run.id ? html` <span class="mono muted">(${run.id})</span>` : null}</p>
      ${props.error ? html`<p class="notice warn" role="alert">${props.error}</p>` : null}
      ${!d && !props.error ? html`<p class="muted" role="status">Loading the run ...</p>` : null}
      ${d
        ? html`<ol class="tsteps">
            <li class="tstep">
              <div class="thead"><span class="num">1</span><h3>Open this folder in a new Alan Code conversation</h3>
                <${A.CopyButton} text=${d.workdir} label="Copy the run folder" text_="Copy folder" /></div>
              <pre class="code pathbox" tabindex="0" aria-label="Run folder">${d.workdir}</pre>
            </li>
            <li class="tstep">
              <div class="thead"><span class="num">2</span><h3>Pick the model <code>${d.model || run.model || "flash"}</code></h3></div>
            </li>
            <li class="tstep">
              <div class="thead"><span class="num">3</span><h3>Paste this prompt</h3>
                <${A.CopyButton} text=${d.prompt} label="Copy the prompt" text_="Copy prompt" /></div>
              <${A.CodeBlock} text=${d.prompt} label="Prompt" />
            </li>
            <li class="tstep">
              <div class="thead"><span class="num">4</span><h3>Let the agent finish</h3></div>
              <p class="small muted">Do not edit files yourself while it works. This app cannot start the agent: that step stays in the Alan app.</p>
            </li>
          </ol>
          <div class="analyzerow">
            <button type="button" class="btn primary big" disabled=${analyzing} onClick=${props.onAnalyze}>
              ${analyzing ? "Finding the step ..." : "The agent has finished: find the step"}
            </button>
            <p class="small muted">AgentBisect replays each step, runs the tests on it, and names the step that broke the code.</p>
          </div>`
        : null}
      ${props.analyzeError ? html`<p class="notice warn" role="alert">${props.analyzeError}</p>` : null}
      ${aj ? html`<${JobBox} job=${aj} what="Finding the step" />` : null}
      ${run.has_bisect && !analyzing
        ? html`<div class="resultrow" role="status">
            <${VerdictChip} run=${run} />
            ${run.has_report ? html`<a class="btn primary" href=${reportHref(run.id)} target="_blank" rel="noopener">Open the report</a>` : null}
          </div>`
        : null}
    </div></section>`;
  }

  /* ---------------------------------------------------------------- runs list */

  function RunsList(props) {
    var runs = props.runs;
    return html`<section class="section" id="runs" aria-labelledby="runs-h"><div class="card panel">
      <div class="runshead">
        <h2 class="t" id="runs-h">3. Runs</h2>
        <span class="grow"></span>
        <a class="btn" href=${STUDY_HREF} target="_blank" rel="noopener">Study page</a>
        <button type="button" class="btn" onClick=${props.onRefresh} disabled=${props.loading}>${props.loading ? "Refreshing ..." : "Refresh"}</button>
      </div>
      ${props.error ? html`<p class="notice warn" role="alert">${props.error}</p>` : null}
      ${runs && runs.length
        ? html`<div class="tablewrap"><table class="runtable">
            <thead><tr><th>Run</th><th>Repository or task</th><th>Model</th><th>Steps</th><th>Verdict</th><th>Actions</th></tr></thead>
            <tbody>${runs.map(function (r) {
              var sel = props.selected === r.id;
              var title = r.title || r.id;
              return html`<tr key=${r.id} class=${cx(sel && "sel")} onClick=${function () { props.onSelect(r.id); }}>
                <td data-label="Run" class="runcell">
                  <button type="button" class="linkbtn" aria-current=${sel ? "true" : null} onClick=${function (e) { e.stopPropagation(); props.onSelect(r.id); }}>${title}</button>
                  ${title !== r.id ? html`<div class="mono tiny muted">${r.id}</div>` : null}
                  ${!r.finished ? html`<span class="tag prb">not finished</span>` : null}
                </td>
                <td data-label="Repository or task" class="srccell">${r.repo_origin || r.task_id || ""}</td>
                <td data-label="Model">${r.model || ""}</td>
                <td data-label="Steps" class="num">${r.steps}</td>
                <td data-label="Verdict"><${VerdictChip} run=${r} /></td>
                <td data-label="Actions" class="acts">
                  ${r.has_report ? html`<a class="btn small" href=${reportHref(r.id)} target="_blank" rel="noopener" onClick=${function (e) { e.stopPropagation(); }}>Open report</a>` : null}
                  ${r.finished && !r.has_bisect
                    ? html`<button type="button" class="btn small" onClick=${function (e) { e.stopPropagation(); props.onFind(r.id); }}>Find the step</button>`
                    : null}
                </td>
              </tr>`;
            })}</tbody>
          </table></div>`
        : html`<p class="muted" role="status">${runs ? "No runs yet. Set one up above." : "Loading the runs ..."}</p>`}
    </div></section>`;
  }

  /* ---------------------------------------------------------------- app */

  function App() {
    var runsS = useState(null);
    var runsErr = useState("");
    var loadingS = useState(false);
    var selS = useState("");
    var detS = useState(null);
    var detErr = useState("");
    var anaRun = useState("");
    var anaErr = useState("");
    var latest = useRef({ sel: "" });
    latest.current.sel = selS[0];

    function loadRuns() {
      loadingS[1](true);
      api("/api/runs").then(
        function (d) {
          loadingS[1](false);
          runsErr[1]("");
          runsS[1](d.runs || []);
        },
        function (e) {
          loadingS[1](false);
          runsErr[1](e.message);
          if (!runsS[0]) runsS[1]([]);
        }
      );
    }
    function loadDetail(id) {
      api(runPath(id)).then(
        function (d) {
          if (latest.current.sel !== id) return;
          detErr[1]("");
          detS[1](d);
        },
        function (e) {
          if (latest.current.sel !== id) return;
          detErr[1](e.message);
        }
      );
    }
    function select(id) {
      latest.current.sel = id;
      selS[1](id);
      detS[1](null);
      detErr[1]("");
      anaErr[1]("");
      if (anaRun[0] !== id) analyze.clear();
      loadDetail(id);
    }

    var record = useJob(function (j) {
      loadRuns();
      if (j.status === "done" && j.run_id) select(j.run_id);
    });
    var analyze = useJob(function (j) {
      loadRuns();
      if (j.status === "done" && latest.current.sel) loadDetail(latest.current.sel);
    });

    useEffect(function () { loadRuns(); }, []);

    function find(id) {
      if (latest.current.sel !== id) select(id);
      anaErr[1]("");
      anaRun[1](id);
      api(runPath(id, "/analyze"), { method: "POST" }).then(
        function (d) { analyze.start(d.job_id); },
        function (e) { anaErr[1](e.message); }
      );
    }

    var runs = runsS[0];
    var run = null;
    if (selS[0]) {
      run = ((runs || []).filter(function (r) { return r.id === selS[0]; })[0]) || { id: selS[0] };
      if (detS[0] && !run.title) run = Object.assign({}, detS[0], run);
    }
    var recBusy = !!record.job && (record.job.status === "queued" || record.job.status === "running");
    var forbidden = runsErr[0] === NO_TOKEN;

    return html`<div>
      <${A.TopBar} kind="" crumb="">
        <span class="chip"><b>local app</b></span>
        <a class="btn ghost small" href=${STUDY_HREF} target="_blank" rel="noopener">Study page</a>
      <//>
      <main class="wrap page">
        <section class="intro">
          <h1>AgentBisect</h1>
          <p class="pitch">Find the exact step where a coding agent broke your code.</p>
          <p class="pitch2">Runs on your machine. Nothing leaves it except git clone and package installs.</p>
        </section>
        ${forbidden ? html`<p class="notice warn" role="alert"><b>No access.</b> ${NO_TOKEN}</p>` : null}
        <${NewRun} job=${record.job} busy=${recBusy} onStarted=${record.start} />
        <${NextStep} run=${run} detail=${detS[0]} error=${detErr[0]} analyzeJob=${anaRun[0] === selS[0] ? analyze.job : null}
          analyzeError=${anaErr[0]} onAnalyze=${function () { find(selS[0]); }} />
        <${RunsList} runs=${runs} selected=${selS[0]} loading=${loadingS[0]} error=${forbidden ? "" : runsErr[0]}
          onRefresh=${loadRuns} onSelect=${select} onFind=${find} />
        <p class="footer">Each verdict comes from running a test on each snapshot. No language model decides pass or fail.<br />Stop the app with Ctrl+C in the terminal.</p>
      </main>
    </div>`;
  }

  var root = document.getElementById("app");
  root.textContent = "";
  A.render(html`<${App} />`, root);
})();
