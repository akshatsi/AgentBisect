/* The study page: landing header, demo run cards, headline numbers, lag chart, runs table. */
(function () {
  "use strict";
  var A = AB;
  var html = A.html;
  var useState = A.useState;
  var useMemo = A.useMemo;
  var cx = A.cx;
  var D = A.readData();

  function pctOf(v, max) {
    return (max > 0 ? A.clamp(v / max, 0, 1) * 100 : 0) + "%";
  }

  /* ---------------------------------------------------------------- landing */

  function Landing() {
    var c = D.counts;
    return html`<section class="landing">
      <div class="wrap">
        <p class="kicker">Study results${D.created ? " · " + D.created : ""}</p>
        <h1><${A.Logo} /> AgentBisect</h1>
        <p class="pitch">git bisect for coding agents. It finds the exact step where an agent broke your code, and how long the agent kept working before it noticed.</p>
        <p class="pitch2">It saves the repo after every agent action, then binary-searches those saves with your failing test. The verdict comes from running tests. No language model decides pass or fail.</p>
        <div class="chips">
          <span class="chip"><b>${c.runs}</b> ${c.runs === 1 ? "run" : "runs"} counted</span>
          <span class="chip"><b>${c.broken_runs}</b> ended broken</span>
          ${D.excluded.length ? html`<span class="chip"><b>${D.excluded.length}</b> excluded</span>` : null}
          ${c.models.length ? html`<span class="chip">model <b>${c.models.join(", ")}</b></span>` : null}
          ${D.design ? html`<span class="chip" title=${D.design}><code>${D.design.length > 70 ? D.design.slice(0, 69) + "…" : D.design}</code></span>` : null}
        </div>
        <div class="how">
          <div><b><span class="num">1</span> Record</b>Snapshot the repo after every step the agent takes.</div>
          <div><b><span class="num">2</span> Bisect</b>Binary-search the snapshots with your failing test.</div>
          <div><b><span class="num">3</span> Measure</b>Count the steps and tokens between the break and the agent noticing.</div>
        </div>
      </div>
    </section>`;
  }

  function RunCards() {
    if (!D.featured.length) return null;
    return html`<div class="section">
      <h2>Start here: real runs</h2>
      <div class="cards">
        ${D.featured.map(function (f) {
          var tone = f.kind === "bad" ? "" : f.kind === "ok" ? "ok" : "warn";
          return html`<a class="card runcard" key=${f.run_id} href=${f.href} aria-label=${"Open the report for " + f.run_id}>
            <span class=${cx("rtag", tone)}>${f.tag}</span>
            <h3>${f.run_id}</h3>
            <p class="v1" title=${f.sentences[0] || null}>${f.sentences[0] || "No verdict."}</p>
            ${f.sentences.length > 1 ? html`<p class="v2">${f.sentences.slice(1).join(" ")}</p>` : null}
            <span class="rmeta">${f.n_steps} steps · ${f.tokens} tokens${f.model ? " · " + f.model : ""}</span>
            <span class="open">Open the report <${A.Icon} name="arrow" size=${15} /></span>
          </a>`;
        })}
      </div>
    </div>`;
  }

  /* ---------------------------------------------------------------- try it on your repo */

  var C = A.cmd;

  /* Copy text with the clipboard API, or with a hidden textarea where that is missing or refused. */
  function fallbackCopy(text) {
    try {
      var ta = document.createElement("textarea");
      ta.value = text;
      ta.setAttribute("readonly", "");
      ta.setAttribute("aria-hidden", "true");
      ta.style.position = "fixed";
      ta.style.top = "0";
      ta.style.left = "-9999px";
      document.body.appendChild(ta);
      ta.select();
      var ok = document.execCommand("copy");
      document.body.removeChild(ta);
      return !!ok;
    } catch (e) {
      return false;
    }
  }

  function CopyIcon() {
    return html`<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">
      <path d="M5.5 5.5h8v8h-8z" /><path d="M3 10.5V2.5h8" />
    </svg>`;
  }

  function CopyButton(props) {
    var st = useState("");
    var timer = A.useRef(null);
    function done(ok) {
      st[1](ok ? "copied" : "failed");
      clearTimeout(timer.current);
      timer.current = setTimeout(function () { st[1](""); }, 2200);
    }
    function copy() {
      var text = props.text;
      var nav = typeof navigator !== "undefined" ? navigator : null;
      if (nav && nav.clipboard && nav.clipboard.writeText) {
        nav.clipboard.writeText(text).then(function () { done(true); }, function () { done(fallbackCopy(text)); });
      } else {
        done(fallbackCopy(text));
      }
    }
    var msg = st[0] === "copied" ? "Copied" : st[0] === "failed" ? "Copy failed. Select the text and copy it by hand." : "";
    return html`<span class="copywrap">
      <button type="button" class="btn copy" aria-label=${props.label} onClick=${copy}>
        ${st[0] === "copied" ? html`<${A.Icon} name="check" size=${14} />` : html`<${CopyIcon} />`}${st[0] === "copied" ? "Copied" : props.text_ || "Copy"}
      </button>
      <span class="sr" role="status">${msg}</span>
      ${st[0] === "failed" ? html`<span class="tiny muted" aria-hidden="true">Copy failed</span>` : null}
    </span>`;
  }

  function Field(props) {
    var id = props.id;
    var describedBy = [props.hint ? id + "-hint" : null, props.error ? id + "-err" : null].filter(Boolean).join(" ") || null;
    var control = props.control({ id: id, "aria-invalid": props.error ? "true" : null, "aria-describedby": describedBy, "aria-required": props.required ? "true" : null });
    return html`<div class=${cx("field", props.wide && "wide")}>
      <label for=${id}>${props.label}${props.required ? null : html` <span class="opt">(optional)</span>`}</label>
      ${control}
      ${props.hint ? html`<p class="hint" id=${id + "-hint"}>${props.hint}</p>` : null}
      ${props.error ? html`<p class="ferr" id=${id + "-err"} role="alert">${props.error}</p>` : null}
    </div>`;
  }

  function Step(props) {
    return html`<li class="tstep">
      <div class="thead">
        <span class="num">${props.n}</span>
        <h3>${props.title}</h3>
        ${props.copy ? html`<${CopyButton} text=${props.copy} text_=${props.copyText} label=${props.copyLabel || "Copy the commands of step " + props.n} />` : null}
      </div>
      ${props.children}
    </li>`;
  }

  /* The file name of a run report is split in the source below: a test searches the page for it to find links to run reports. */
  function TryIt() {
    var fS = useState(C.emptyForm());
    var tS = useState({});
    var xS = useState("");
    var form = fS[0];
    var touched = tS[0];
    var result = useMemo(function () { return C.validate(form); }, [form]);
    var errors = result.errors;
    var values = result.values;

    function set(key, value) {
      var next = Object.assign({}, form);
      next[key] = value;
      fS[1](next);
      xS[1]("");
    }
    function touch(key) {
      if (!touched[key]) {
        var next = Object.assign({}, touched);
        next[key] = true;
        tS[1](next);
      }
    }
    function shown(key) {
      var v = form[key];
      return errors[key] && (touched[key] || (typeof v === "string" && v.trim() !== "")) ? errors[key] : null;
    }
    function text(key) {
      return function (extra) {
        return Object.assign({}, extra, {
          value: form[key],
          onInput: function (e) { set(key, e.target.value); },
          onBlur: function () { touch(key); },
        });
      };
    }
    function pick(e) {
      var id = e.target.value;
      xS[1](id);
      var preset = C.PRESETS.filter(function (p) { return p.id === id; })[0];
      if (preset) {
        fS[1](Object.assign(C.emptyForm(), preset.form));
        tS[1]({});
      }
    }
    function input(key, extra) {
      return function (props) { return html`<input type="text" autocomplete="off" autocapitalize="off" spellcheck="false" ...${Object.assign({}, props, text(key)(), extra)} />`; };
    }
    function area(key, rows, mono) {
      return function (props) { return html`<textarea rows=${rows} spellcheck=${mono ? "false" : "true"} class=${mono ? "mono" : null} ...${Object.assign({}, props, text(key)())}></textarea>`; };
    }

    var record = values ? C.recordBlock(values) : null;
    var model = values ? values.model : form.model.trim() || "flash";

    return html`<div class="section" id="try-it"><div class="card panel">
      <h2 class="t">Try it on your own repo</h2>
      <p class="lede">AgentBisect runs on your machine. Fill in your project and copy the commands.</p>
      <div class="notice warn tryfirst" role="note">
        <b>Security.</b> Installing a project's dependencies runs its build code. Use repositories you trust, or add <code>--no-setup</code>.
      </div>
      <p class="small muted trylimits">Limits: Python and pytest only. The tests must not need a database or a network. The project must install on Python 3.11. The commands are for bash or zsh.</p>

      <form class="tryform" onSubmit=${function (e) { e.preventDefault(); }}>
        <${Field} id="try-example" label="Examples" required=${true} hint="Fills the form below." control=${function (p) {
          return html`<select ...${Object.assign({}, p, { "aria-required": null })} value=${xS[0]} onChange=${pick}>
            <option value="">Choose an example</option>
            ${C.PRESETS.map(function (x) { return html`<option value=${x.id} key=${x.id}>${x.label}</option>`; })}
          </select>`;
        }} />
        <${Field} id="try-repo" wide=${true} required=${true} label="Repository" error=${shown("repo")}
          hint=${"A git address (" + C.HTTPS + " or " + C.FILE + ") or a folder on this computer."} control=${input("repo", { placeholder: C.HTTPS + "github.com/you/project" })} />
        <${Field} id="try-prompt" wide=${true} required=${true} label="Task prompt" error=${shown("prompt")}
          hint="What to ask the agent." control=${area("prompt", 6, false)} />
        <${Field} id="try-model" required=${true} label="Model" error=${shown("model")}
          hint="The name you will pick in Alan Code. It becomes --model." control=${input("model")} />
        <${Field} id="try-ref" label="Branch or tag" error=${shown("ref")} hint="Starts from this branch or tag. It becomes --ref." control=${input("ref", { placeholder: "main" })} />
        <div class="field wide checks">
          <label class="check"><input type="checkbox" id="try-nosetup" checked=${form.noSetup} onChange=${function (e) { set("noSetup", !!e.target.checked); }} />
            <span>Skip installing dependencies (<code>--no-setup</code>)</span></label>
          <label class="check"><input type="checkbox" id="try-plant" checked=${form.plant} onChange=${function (e) { set("plant", !!e.target.checked); }} />
            <span>Plant a known break to test the bisector</span></label>
        </div>
        ${form.plant
          ? html`<div class="field wide plant" role="group" aria-label="Planted break">
              <p class="small muted">After the chosen step, AgentBisect edits one file to break it. Then it checks that the bisector names that step.</p>
              <div class="plantgrid">
                <${Field} id="try-step" required=${true} label="After which step" error=${shown("atStep")} control=${input("atStep", { inputmode: "numeric" })} />
                <${Field} id="try-file" required=${true} label="File path" error=${shown("file")} hint="Relative to the repository." control=${input("file", { placeholder: "src/package/module.py" })} />
                <${Field} id="try-find" wide=${true} required=${true} label="Text to replace" error=${shown("find")} hint="Exact text, spaces included. It must occur once in the file." control=${area("find", 2, true)} />
                <${Field} id="try-replace" wide=${true} required=${false} label="Replacement text" error=${shown("replace")} hint="Empty deletes the text." control=${area("replace", 2, true)} />
              </div>
            </div>`
          : null}
      </form>

      <ol class="tsteps">
        <${Step} n=${1} title="Install AgentBisect once" copy=${C.installCommand()}>
          <p class="small muted">You need git and uv.</p>
          <${A.CodeBlock} text=${C.installCommand()} label="Install commands" />
        <//>
        <${Step} n=${2} title="Save the prompt and record the run" copy=${record}>
          ${record
            ? html`<p class="small muted">Run this in the <code>Breaking-Point</code> folder. It copies your project into a new folder under <code>runs/</code>${values.noSetup ? "." : " and installs its dependencies. That can take a minute."} Your own repository stays as it is.</p>
                <${A.CodeBlock} text=${record} label="Record commands" />`
            : html`<p class="small muted" role="status">Fill in the repository and the prompt, and this step shows the command. ${Object.keys(errors).length ? "Fields still to fix: " + Object.keys(errors).length + "." : ""}</p>`}
        <//>
        <${Step} n=${3} title="Let the agent work in Alan Code" copy=${values ? values.prompt : null} copyLabel="Copy the prompt" copyText="Copy prompt">
          <p>Open the printed <code>workdir</code> folder in a <b>new</b> Alan Code conversation. Pick the <code>${model}</code> model, paste the prompt, and let the agent finish. Do not edit files yourself while it works.</p>
        <//>
        <${Step} n=${4} title="Find the step that broke it" copy=${C.bisectBlock()}>
          <${A.CodeBlock} text=${C.bisectBlock()} label="Bisect and report commands" />
          <p class="small muted">Replace <code>${"<run-folder>"}</code> with the folder name that <code>record</code> printed under <code>runs/</code>. <code>${"report"}.html</code> is one file. Open it or share it.</p>
        <//>
      </ol>
    </div></div>`;
  }

  /* ---------------------------------------------------------------- numbers */

  function trimNum(x) {
    return String(Math.round(x * 10) / 10);
  }

  function Metric(props) {
    var m = props.m;
    var hasCi = m.raw != null && m.low != null && m.high != null;
    var top = m.percent ? 1 : Math.max(m.high || 0, m.raw || 0) * 1.1 || 1;
    return html`<div class="card metric">
      <div class="k">${m.name}</div>
      <div class=${cx("v", !m.estimable && "na")}>${m.value}</div>
      ${m.interval ? html`<div class="ciline mono">${m.interval}</div>` : null}
      ${hasCi
        ? html`<div aria-hidden="true">
            <div class="cirail"><i style=${{ left: pctOf(m.low, top), width: "calc(" + pctOf(m.high, top) + " - " + pctOf(m.low, top) + ")" }}></i><b style=${{ left: pctOf(m.raw, top) }}></b></div>
            <div class="ciaxis"><span>0</span><span>${m.percent ? "100%" : trimNum(top)}</span></div>
          </div>`
        : null}
      <div class="m">n = ${m.n} · ${m.method}</div>
      ${m.note ? html`<div class="m">${m.note}</div>` : null}
      ${m.simulation ? html`<span class="sim">simulation, not real agent runs</span>` : null}
    </div>`;
  }

  function Numbers() {
    return html`<div class="section">
      <h2>Headline numbers</h2>
      ${D.metrics.length
        ? html`<div class="metrics">${D.metrics.map(function (m) { return html`<${Metric} m=${m} key=${m.name} />`; })}</div>`
        : html`<p class="muted">No metrics were computed.</p>`}
    </div>`;
  }

  /* ---------------------------------------------------------------- chart */

  function LagRow(props) {
    var b = props.b;
    var ch = props.chart;
    var x = function (v) { return pctOf(v, ch.axis_max); };
    var label = b.href
      ? html`<a href=${b.href}>${b.label}</a>`
      : html`<span class="nm">${b.label}</span>`;
    return html`<div class="lrow" tabindex="0" role="img" aria-label=${b.title}>
      <div class="llabel">${label}<span class="ltext">${b.text}</span></div>
      <div class="ltrack">
        ${ch.ticks.map(function (t) { return html`<i class="gl" style=${{ left: x(t) }}></i>`; })}
        <i class="runend" style=${{ left: x(b.end) }} title="end of the run"></i>
        ${b.kind === "censored"
          ? html`<i class="open" style=${{ left: x(b.t_err), width: "calc(" + x(b.end) + " - " + x(b.t_err) + ")" }}></i>
              <i class="arrowhead" style=${{ left: x(b.end) }}></i>
              <b class="dot err" style=${{ left: x(b.t_err) }}></b>`
          : b.kind === "instant"
            ? html`<b class="dot obs" style=${{ left: "calc(" + x(b.t_err) + " + 6px)" }}></b><b class="dot err" style=${{ left: x(b.t_err) }}></b>`
            : html`<i class="span" style=${{ left: x(b.t_err), width: "calc(" + x(b.t_obs) + " - " + x(b.t_err) + ")" }}></i>
                <b class="dot err" style=${{ left: x(b.t_err) }}></b><b class="dot obs" style=${{ left: x(b.t_obs) }}></b>`}
      </div>
      <div class="tip" role="tooltip">${b.title}</div>
    </div>`;
  }

  function Chart(props) {
    var ch = props.chart;
    return html`<div class="lagchart chartwrap">
      <div class="laxis"><span></span><div class="ticks">${ch.ticks.map(function (t) {
        return html`<span style=${{ left: pctOf(t, ch.axis_max) }}>${t}</span>`;
      })}</div></div>
      ${ch.bars.map(function (b, i) { return html`<${LagRow} b=${b} chart=${ch} key=${i} />`; })}
    </div>`;
  }

  function LagPanel() {
    return html`<div class="section"><div class="card panel">
      <h2 class="t">Detection lag in broken runs</h2>
      <p class="lede">One row per run that ended broken. The axis counts steps. Hover or focus a row for the details.</p>
      ${D.lag_chart
        ? html`<div class="keys">
              <span><i class="k-err"></i>step that broke the code</span>
              <span><i class="k-obs"></i>step where the agent first saw the failure</span>
              <span><i class="k-span"></i>steps spent on broken code</span>
              <span><i class="k-open"></i>open end: the agent never saw it (censored)</span>
            </div>
            <${Chart} chart=${D.lag_chart} />
            <p class="note small muted" style=${{ marginTop: "10px" }}>The grey tick marks where each run ended.</p>`
        : html`<p class="muted">No run ended broken with a decisive step, so there is nothing to draw.</p>`}
    </div></div>`;
  }

  function EpisodePanel() {
    return html`<div class="section"><div class="card panel sep">
      <h2 class="t">Repaired breaks (a separate series)</h2>
      <p class="lede">The agent broke a test that passed at the start, saw the failure, and fixed it. We find these only when the agent saw them, so none is censored. They say nothing about breaks the agent never saw. Never pool this series with the broken runs above.</p>
      ${D.episode_chart
        ? html`<div class="keys">
              <span><i class="k-err"></i>step that broke the test</span>
              <span><i class="k-obs"></i>step where the agent saw the failure</span>
              <span><i class="k-span"></i>steps before it saw it</span>
            </div>
            <${Chart} chart=${D.episode_chart} />`
        : null}
      ${D.episodes.length
        ? html`<div class="tablewrap" style=${{ marginTop: "14px" }}><table>
            <thead><tr><th>Run</th><th>Test</th><th>Seen at</th><th>t_err</th><th>Lag</th><th>Tokens wasted</th><th>Note</th></tr></thead>
            <tbody>${D.episodes.map(function (e, i) {
              return html`<tr key=${i}>
                <td class="mono">${e.href ? html`<a href=${e.href}>${e.run_id}</a>` : e.run_id}</td>
                <td class="mono">${e.check}</td><td class="num">${e.seen_at}</td><td class="num">${e.t_err}</td>
                <td class="num">${e.lag}</td><td class="num">${e.wasted}</td><td>${e.reason || ""}</td></tr>`;
            })}</tbody></table></div>`
        : html`<p class="muted">No repaired breaks found.</p>`}
    </div></div>`;
  }

  /* ---------------------------------------------------------------- runs table */

  var COLS = [
    { k: "task", label: "Task", get: function (r) { return r.task; } },
    { k: "run", label: "Run", get: function (r) { return r.run_id; } },
    { k: "check", label: "Test", get: function (r) { return r.check; } },
    { k: "steps", label: "Steps", get: function (r) { return r.steps; }, num: true },
    { k: "broken", label: "Broken?", get: function (r) { return r.broken; } },
    { k: "t_err", label: "t_err", get: function (r) { return r.t_err_n; }, num: true },
    { k: "t_obs", label: "t_obs", get: function (r) { return r.t_obs_n; }, num: true },
    { k: "lag", label: "Lag", get: function (r) { return r.lag_n; }, num: true },
    { k: "share", label: "Wasted share", get: function (r) { return r.share_n; }, num: true },
    { k: "sabotage", label: "Sabotage correct", get: function (r) { return r.sabotage; } },
  ];

  function cellOf(r, col) {
    switch (col.k) {
      case "run":
        return r.href ? html`<a href=${r.href}>${r.run_id}</a>` : r.run_id;
      case "broken":
        return html`<span class=${"pill " + r.broken_class}>${r.broken}</span>`;
      case "t_err":
        return r.t_err;
      case "t_obs":
        return r.t_obs;
      case "lag":
        return r.censored ? html`<span class="pill cens">${r.lag}</span>` : r.lag;
      case "share":
        return r.share;
      case "sabotage":
        return r.sabotage_class ? html`<span class=${"pill " + r.sabotage_class}>${r.sabotage}</span>` : r.sabotage;
      case "task":
        return r.task;
      case "check":
        return r.check;
      default:
        return r.steps;
    }
  }

  function RunsTable() {
    var sortS = useState({ k: null, dir: 1 });
    var qS = useState("");
    var sort = sortS[0];
    var q = qS[0].trim().toLowerCase();
    var rows = useMemo(
      function () {
        var out = D.runs
          .map(function (r, i) { return { r: r, i: i }; })
          .filter(function (x) {
            if (!q) return true;
            var r = x.r;
            return [r.task, r.run_id, r.check, r.steps, r.broken, r.t_err, r.t_obs, r.lag, r.share, r.sabotage]
              .join(" ")
              .toLowerCase()
              .indexOf(q) !== -1;
          });
        if (sort.k) {
          var col = COLS.filter(function (c) { return c.k === sort.k; })[0];
          out.sort(function (a, b) {
            var x = col.get(a.r);
            var y = col.get(b.r);
            if (x == null && y == null) return a.i - b.i;
            if (x == null) return 1;
            if (y == null) return -1;
            var d = typeof x === "number" && typeof y === "number" ? x - y : String(x).localeCompare(String(y), "en", { numeric: true });
            return d ? d * sort.dir : a.i - b.i;
          });
        }
        return out;
      },
      [sort.k, sort.dir, q]
    );
    function toggle(k) {
      sortS[1](sort.k === k ? { k: k, dir: -sort.dir } : { k: k, dir: 1 });
    }
    return html`<div class="section"><div class="card panel">
      <h2 class="t">Runs</h2>
      <p class="lede">Every counted run. Click a column to sort. Click a run id to open its report.</p>
      ${D.runs.length
        ? html`<div class="tools">
              <input type="search" placeholder="Filter runs" aria-label="Filter runs" value=${qS[0]} onInput=${function (e) { qS[1](e.target.value); }} />
              <span class="small muted" role="status">${rows.length} of ${D.runs.length} rows</span>
            </div>
            <div class="tablewrap"><table>
              <thead><tr>${COLS.map(function (c) {
                var on = sort.k === c.k;
                return html`<th key=${c.k} aria-sort=${on ? (sort.dir === 1 ? "ascending" : "descending") : "none"}>
                  <button type="button" onClick=${function () { toggle(c.k); }}>${c.label}<span class="arrow" aria-hidden="true">${on ? (sort.dir === 1 ? "▲" : "▼") : "⇅"}</span></button></th>`;
              })}</tr></thead>
              <tbody>${rows.map(function (x) {
                return html`<tr key=${x.i}>${COLS.map(function (c) {
                  return html`<td class=${cx(c.num && "num", c.k === "run" && "mono nw", c.k === "check" && "mono clip")} title=${c.k === "check" ? x.r.check : null}>${cellOf(x.r, c)}</td>`;
                })}</tr>`;
              })}</tbody>
            </table></div>`
        : html`<p class="muted">No runs.</p>`}
    </div></div>`;
  }

  function Excluded() {
    return html`<div class="section"><div class="card panel">
      <h2 class="t">Excluded runs</h2>
      ${D.excluded.length
        ? html`<div class="tablewrap"><table><thead><tr><th>Run</th><th>Reason</th></tr></thead>
            <tbody>${D.excluded.map(function (x, i) { return html`<tr key=${i}><td class="mono">${x[0]}</td><td>${x[1]}</td></tr>`; })}</tbody></table></div>`
        : html`<p class="muted">No run was excluded.</p>`}
    </div></div>`;
  }

  function Claims() {
    var li = function (name) {
      return function (s) { return html`<li><${A.Icon} name=${name} size=${16} /><span>${s}</span></li>`; };
    };
    return html`<div class="section"><div class="card panel claimbox">
      <h2 class="t">What this can and cannot claim</h2>
      <div class="claims" style=${{ marginTop: "12px" }}>
        <div class="yes"><h3>It can claim</h3><ul>${D.can.map(li("check"))}</ul></div>
        <div class="no"><h3>It cannot claim</h3><ul>${D.cannot.map(li("cross"))}</ul></div>
      </div>
    </div></div>`;
  }

  function App() {
    return html`<div>
      <${A.TopBar} kind="study" crumb="" />
      <${Landing} />
      <main class="wrap page">
        <${RunCards} />
        <${TryIt} />
        <${Numbers} />
        <${LagPanel} />
        <${EpisodePanel} />
        <${RunsTable} />
        <${Excluded} />
        <${Claims} />
        <p class="footer">Each verdict comes from running a test on each snapshot. No language model decides pass or fail.<br />Made by AgentBisect.</p>
      </main>
    </div>`;
  }

  var root = document.getElementById("app");
  root.textContent = "";
  A.render(html`<${App} />`, root);
})();
