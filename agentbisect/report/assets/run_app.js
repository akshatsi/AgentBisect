/* The run report. Reads the JSON blob and draws: verdict, bisect replay, step explorer, details. */
(function () {
  "use strict";
  var A = AB;
  var html = A.html;
  var useState = A.useState;
  var useEffect = A.useEffect;
  var useRef = A.useRef;
  var useMemo = A.useMemo;
  var cx = A.cx;
  var D = A.readData();
  var STEPS = D.steps;
  var N = STEPS.length;
  var VIEWS = D.groups.concat(D.episodes);

  function memberOf(view, mi) {
    if (!view) return null;
    return view.type === "group" ? view.members[Math.min(mi, view.members.length - 1)] : view;
  }

  /* Where step i sits against a finding: decisive, first seen, or spent on broken code. */
  function phaseOf(m, i) {
    var an = m && m.analysis;
    if (!an) return {};
    return {
      dec: i === an.t_err,
      obs: an.t_obs != null && i === an.t_obs,
      brk: i > an.t_err && (an.censored || (an.t_obs != null && i < an.t_obs)),
    };
  }

  function tokensText(n) {
    if (n < 1000) return String(n);
    if (n < 1e6) return (n / 1000).toFixed(n < 10000 ? 1 : 0).replace(/\.0$/, "") + "k";
    return (n / 1e6).toFixed(1).replace(/\.0$/, "") + "M";
  }

  function tabLabel(v) {
    if (v.type === "episode") return "Fixed · " + v.label;
    return v.found ? "Step " + v.decisive + " · " + v.label : "Not bisectable · " + v.label;
  }
  function tabTone(v) {
    if (v.type === "episode") return "ok";
    return v.found ? "" : "warn";
  }

  function initialSel() {
    var m = /^#step-(\d+)$/.exec(location.hash || "");
    if (m && +m[1] >= 0 && +m[1] <= N) return +m[1];
    var v = VIEWS[0];
    if (v && v.decisive) return v.decisive;
    for (var i = 0; i < N; i++) if (STEPS[i].diff) return STEPS[i].i;
    for (var j = 0; j < N; j++) if (STEPS[j].mut) return STEPS[j].i;
    return N ? 1 : 0;
  }

  /* ---------------------------------------------------------------- header */

  function RunMeta() {
    var r = D.run;
    return html`<div class="runmeta">
      <h1>Run report</h1>
      <div class="chips">
        <span class="chip">run <code>${r.id}</code></span>
        ${r.repo ? html`<span class="chip">repo <code>${r.repo}</code></span>` : null}
        ${r.task ? html`<span class="chip">task <code>${r.task}</code></span>` : null}
        ${r.model ? html`<span class="chip">model <code>${r.model}</code></span>` : null}
        <span class="chip">recorded by <b>${r.source}</b></span>
        <span class="chip"><b>${r.n_steps}</b> steps (${r.n_mutating} change the repo)</span>
        <span class="chip" title=${r.tokens_exact}><b>${r.tokens}</b> tokens</span>
        ${r.cost ? html`<span class="chip">cost <b>${r.cost}</b></span>` : null}
        <span class="chip">${r.created}</span>
      </div>
    </div>`;
  }

  function Tabs(props) {
    var views = props.views;
    function onKey(e) {
      var k = e.key;
      if (k !== "ArrowRight" && k !== "ArrowLeft" && k !== "Home" && k !== "End") return;
      var i = props.tab;
      if (k === "ArrowRight") i = (i + 1) % views.length;
      else if (k === "ArrowLeft") i = (i - 1 + views.length) % views.length;
      else if (k === "Home") i = 0;
      else i = views.length - 1;
      e.preventDefault();
      props.onTab(i);
      setTimeout(function () {
        var el = document.getElementById("tab-" + i);
        if (el) el.focus();
      }, 0);
    }
    return html`<div class="tabs" role="tablist" aria-label="Findings in this run" onKeyDown=${onKey}>
      ${views.map(function (v, i) {
        return html`<button type="button" role="tab" id=${"tab-" + i} key=${v.id} class="tab"
          aria-selected=${i === props.tab} aria-controls="panel" tabindex=${i === props.tab ? 0 : -1}
          onClick=${function () { props.onTab(i); }}>
          <span class=${cx("dot", tabTone(v))}></span><span class="t">${tabLabel(v)}</span>
        </button>`;
      })}
    </div>`;
  }

  /* ---------------------------------------------------------------- hero */

  function Headline(props) {
    var text = props.text || "";
    var m = /^(Step \d+)([\s\S]*)$/.exec(text);
    if (!m) return html`<h2 class="headline">${text}</h2>`;
    return html`<h2 class="headline"><span class="stepno">${m[1]}</span>${m[2]}</h2>`;
  }

  function Stat(props) {
    var s = props.s;
    return html`<div class="stat">
      <div class="k">${s.k}</div>
      <div class="v" title=${s.title || null}>${s.v}${s.small ? html`<small>${s.small}</small>` : null}</div>
      ${s.bar != null ? html`<div class="bar" aria-hidden="true"><i style=${{ width: Math.min(100, s.bar) + "%" }}></i></div>` : null}
    </div>`;
  }

  /* Bar from step 0 to the last step: where the break was, where the agent saw it, what it cost. */
  function LagPanel(props) {
    var an = props.m.analysis;
    if (!an) return null;
    var pct = function (j) { return (N ? (j / N) * 100 : 0) + "%"; };
    var censored = an.censored;
    var endAt = censored ? N : an.t_obs;
    var last = censored && an.after === 0;
    var maxTok = 1;
    STEPS.forEach(function (s) { if (s.tokens > maxTok) maxTok = s.tokens; });

    var headline;
    if (last) headline = "Last step: the agent had no chance to see the failure";
    else if (censored) headline = "Never noticed: " + A.plural(an.after, "step") + " on broken code and counting";
    else if (an.lag === 0) headline = "Noticed at once, at step " + an.t_obs;
    else headline = A.plural(an.lag, "step") + " on broken code before the agent noticed";

    return html`<div class="lag" role="group" aria-label="Detection lag">
      <h3>Detection lag</h3>
      <div class="lagtrack" role="img" aria-label=${headline}>
        <div class="seg broken" style=${{ left: pct(an.t_err), width: "calc(" + pct(endAt) + " - " + pct(an.t_err) + ")" }}></div>
        ${!censored ? html`<div class="seg after" style=${{ left: pct(endAt), right: 0 }}></div>` : null}
        <div class="mk err" style=${{ left: pct(an.t_err) }}></div>
        ${!censored ? html`<div class="mk obs" style=${{ left: pct(an.t_obs) }}></div>` : null}
        ${censored && !last ? html`<span class="arrow"><${A.Icon} name="arrow" size=${22} /></span>` : null}
      </div>
      <div class="lagkey">
        <span><i style=${{ background: "var(--red-solid)" }}></i>Step ${an.t_err} broke it</span>
        ${!censored ? html`<span><i style=${{ background: "var(--blue)" }}></i>Step ${an.t_obs} the agent saw the failure</span>` : null}
        ${!last ? html`<span><i style=${{ background: "var(--amber)" }}></i>${headline}</span>` : html`<span>${headline}</span>`}
        ${an.wasted != null && an.wasted_fmt && !last
          ? html`<span class="muted">${an.wasted_fmt} tokens${an.share_fmt ? " (" + an.share_fmt + " of the run)" : ""} spent after the break</span>`
          : null}
      </div>
      <div class="lagtok" role="group" aria-label="Tokens per step">
        ${STEPS.map(function (s) {
          var ph = phaseOf(props.m, s.i);
          var cls = ph.dec ? "err" : ph.obs ? "obs" : ph.brk ? "broken" : s.i < an.t_err ? "pre" : "post";
          var h = Math.max(4, Math.round((s.tokens / maxTok) * 100));
          return html`<button type="button" key=${s.i} class=${cls} style=${{ height: h + "%" }}
            title=${"Step " + s.i + ": " + tokensText(s.tokens) + " tokens"}
            aria-label=${"Step " + s.i + ", " + tokensText(s.tokens) + " tokens"}
            onClick=${function () { props.onSelect(s.i, true); }}></button>`;
        })}
      </div>
      <div class="lagcap"><span>Tokens per step. Amber bars are steps spent on broken code.</span><span>step 1 to ${N}</span></div>
    </div>`;
  }

  function Hero(props) {
    var v = props.view;
    var m = props.m;
    var tone = v.type === "episode" ? "ok" : v.found ? "" : "warn";
    var eyebrow = v.type === "episode" ? "Caught and fixed" : v.found ? "Culprit found" : "Not bisectable";
    var sentences = v.sentences || [];
    var multi = v.type === "group" && v.members.length > 1;
    var picker = multi && !(v.shared && v.same_probes);
    return html`<section class=${cx("card", "hero", tone)} aria-label="Verdict">
      <div class="eyebrow"><${A.Icon} name=${v.found ? "check" : "warn"} size=${15} /> ${eyebrow}</div>
      ${v.found && sentences.length
        ? html`<${Headline} text=${sentences[0]} />
            <div class="subs">${sentences.slice(1).map(function (s) { return html`<p>${s}</p>`; })}</div>`
        : html`<h2 class="headline">${v.type === "episode" ? sentences[0] : "Not bisectable: " + v.label}</h2>
            <div class="subs">
              ${v.type === "episode"
                ? sentences.slice(1).map(function (s) { return html`<p>${s}</p>`; })
                : html`<p>${m.reason || "No reason recorded."}</p>`}
            </div>`}
      ${m.stats && m.stats.length && (!multi || v.shared || picker)
        ? html`<div class="stats">${m.stats.map(function (s) { return html`<${Stat} s=${s} key=${s.k} />`; })}</div>`
        : v.found && multi
          ? html`<div class="stats"><${Stat} s=${{ k: "Decisive step", v: String(v.decisive) }} /></div>`
          : null}
      ${m.analysis && m.analysis.signal
        ? html`<p class="note">Failure signal the agent saw: <code>${m.analysis.signal}</code></p>`
        : null}
      ${m.ground_truth
        ? html`<p class=${cx("note", "truth", m.ground_truth_ok ? "ok" : "bad")}>Ground truth: ${m.ground_truth}
            ${m.ground_truth_ok ? " Bisect found the same step." : " Bisect found a different step."}</p>`
        : null}
      ${(v.type === "group" ? v.members : []).filter(function (x) { return x.non_monotone; }).map(function (x) {
        return html`<p class="note alert">${multi ? x.label + ": " : ""}Non-monotone history: the check flipped between pass and fail.
          Pass to fail at steps ${x.transitions.join(", ")}. The decisive step is the last one, because the final state still carries that break.</p>`;
      })}
      ${v.type === "episode" && !v.found
        ? html`<p class="note">Step that broke it not found: ${v.reason || "No reason recorded."}</p>`
        : null}
      ${multi
        ? html`<div class="members">
            ${picker
              ? html`<label class="small muted">Show the replay and lag for
                  <select aria-label="Test to show" value=${props.mi} onChange=${function (e) { props.onMember(+e.target.value); }}>
                    ${v.members.map(function (x, k) { return html`<option value=${k}>${x.label}</option>`; })}
                  </select></label>`
              : null}
            <details>
              <summary class="small muted">Checks (${v.members.length})</summary>
              <ul>${v.members.map(function (x) { return html`<li><code>${x.check}</code></li>`; })}</ul>
            </details>
          </div>`
        : html`<p class="note">Check: <code>${v.check || m.check}</code></p>`}
      ${v.decisive
        ? html`<div class="heroactions">
            <button type="button" class="btn primary" onClick=${function () { props.onSelect(v.decisive, true); }}>
              Inspect step ${v.decisive} <${A.Icon} name="arrow" size=${15} />
            </button>
          </div>`
        : null}
      ${v.found ? html`<${LagPanel} m=${m} onSelect=${props.onSelect} />` : null}
    </section>`;
  }

  /* ---------------------------------------------------------------- replay */

  var VERDICT_ICON = { pass: "check", fail: "cross", error: "warn" };

  /* Arrow keys, Home and End move the selected step along the strip. */
  function stripKey(e, j, onSelect) {
    var to = null;
    if (e.key === "ArrowRight") to = Math.min(j + 1, N);
    else if (e.key === "ArrowLeft") to = Math.max(j - 1, 0);
    else if (e.key === "Home") to = 0;
    else if (e.key === "End") to = N;
    if (to === null) return;
    e.preventDefault();
    onSelect(to, false);
    setTimeout(function () {
      var el = document.getElementById("cell-" + to);
      if (el) el.focus();
    }, 0);
  }

  /* The step strip alone, for a run with nothing to replay. */
  function PlainStrip(props) {
    var cells = [];
    for (var j = 0; j <= N; j++) cells.push(j);
    return html`<section class="card replay" aria-label="Steps">
      <div class="rp-head"><div>
        <h2>The run, step by step</h2>
        <p class="rp-sub">Every step the agent took. Click one to see what it did, said and changed.</p>
      </div></div>
      <div class="legend" aria-hidden="true">
        <span><i></i>changes the repo</span><span><i class="l-ro"></i>read only</span>
        ${D.run.sabotage_step != null ? html`<span><i class="l-sab"></i>planted break</span>` : null}
      </div>
      <div class="strip" role="group" aria-label="Steps of the run. Use the left and right arrow keys to move between steps.">
        ${cells.map(function (j) {
          var s = j === 0 ? null : STEPS[j - 1];
          var cls = cx("cell", j === 0 || s.mut ? "mut" : "ro", props.sel === j && "sel", D.run.sabotage_step === j && "sabotage",
            props.matches && j > 0 && !props.matches[j] && "dim");
          return html`<button type="button" key=${j} id=${"cell-" + j} class=${cls} tabindex=${props.sel === j ? 0 : -1}
            aria-pressed=${props.sel === j} aria-label=${j === 0 ? "Step 0: the repo before the agent started" : "Step " + j + ": " + s.label}
            title=${j === 0 ? "Step 0: the repo before the agent started" : "Step " + j + ": " + s.label}
            onClick=${function () { props.onSelect(j, true); }} onKeyDown=${function (e) { stripKey(e, j, props.onSelect); }}>
            <${A.Glyph} kind=${j === 0 ? "base" : s.kind} size=${15} />
            <span class="ix">${j}</span>
          </button>`;
        })}
      </div>
    </section>`;
  }

  function Replay(props) {
    var m = props.m;
    var rp = m.replay;
    var frames = rp.frames;
    var F = frames.length;
    var E = rp.window_end;
    var posS = useState(0);
    var pos = posS[0];
    var setPos = posS[1];
    var playS = useState(false);
    var playing = playS[0];
    var setPlaying = playS[1];
    var spS = useState(1);
    var speed = spS[0];
    var setSpeed = spS[1];
    var landed = F > 0 && pos >= F;
    var cur = pos > 0 && pos <= F ? frames[pos - 1] : null;

    useEffect(
      function () {
        if (!playing) return undefined;
        if (pos >= F) {
          setPlaying(false);
          return undefined;
        }
        var t = setTimeout(function () {
          setPos(function (p) { return Math.min(p + 1, F); });
        }, (pos === 0 ? 450 : 1150) / speed);
        return function () { clearTimeout(t); };
      },
      [playing, pos, speed]
    );

    useEffect(
      function () {
        if (landed && m.found) props.onSelect(m.decisive, false);
      },
      [landed]
    );

    function play() {
      if (F === 0) return;
      if (A.reducedMotion()) {
        setPlaying(false);
        setPos(F);
        return;
      }
      if (playing) {
        setPlaying(false);
        return;
      }
      if (pos >= F) setPos(0);
      setPlaying(true);
    }
    function next() {
      setPlaying(false);
      setPos(function (p) { return Math.min(p + 1, F); });
    }
    function prev() {
      setPlaying(false);
      setPos(function (p) { return Math.max(p - 1, 0); });
    }
    function reset() {
      setPlaying(false);
      setPos(0);
    }
    var api = useRef();
    api.current = { play: play, next: next, prev: prev, reset: reset };
    useEffect(function () {
      function onKey(e) {
        if (e.defaultPrevented || e.ctrlKey || e.metaKey || e.altKey) return;
        var t = e.target;
        if (A.isTyping(t)) return;
        var inCtl = t && t.closest && t.closest(".rp-keys");
        var onBody = !t || t === document.body || t === document.documentElement || !t.closest;
        if (!(onBody || inCtl)) return;
        var k = e.key;
        if (k === " " || k === "Spacebar") {
          if (t && t.tagName === "BUTTON") return;
          e.preventDefault();
          api.current.play();
        } else if (k === "ArrowRight") {
          e.preventDefault();
          api.current.next();
        } else if (k === "ArrowLeft") {
          e.preventDefault();
          api.current.prev();
        } else if (k === "Home") {
          e.preventDefault();
          api.current.reset();
        }
      }
      document.addEventListener("keydown", onKey);
      return function () { document.removeEventListener("keydown", onKey); };
    }, []);

    /* the interval that is still unknown */
    var shade = rp.mono && F > 0;
    var lo = -1;
    var hi = E + 1;
    if (pos > 0 && shade) {
      if (landed && m.found) {
        lo = m.decisive - 1;
        hi = m.decisive;
      } else if (cur) {
        lo = cur.lo;
        hi = cur.hi;
      }
    }
    var probed = {};
    for (var k = 0; k < pos && k < F; k++) probed[frames[k].step] = frames[k];

    function cellClass(j) {
      var ph = landed ? phaseOf(m, j) : {};
      var step = j === 0 ? null : STEPS[j - 1];
      var c = ["cell", j === 0 || step.mut ? "mut" : "ro"];
      if (j > E) c.push("outside");
      else if (shade && pos > 0) {
        if (j <= lo) c.push("good");
        else if (j >= hi) c.push("bad");
      }
      if (cur && cur.step === j && !landed) c.push("cur");
      if (ph.dec) c.push("decisive");
      if (ph.obs) c.push("observed");
      if (ph.brk) c.push("broken");
      if (D.run.sabotage_step === j) c.push("sabotage");
      if (props.sel === j) c.push("sel");
      if (props.matches && j > 0 && !props.matches[j]) c.push("dim");
      return c.join(" ");
    }
    function cellLabel(j) {
      var s = j === 0 ? "Step 0: the repo before the agent started" : "Step " + j + ": " + STEPS[j - 1].label;
      var pr = probed[j];
      if (pr) s += ". Probe " + pr.n + " " + pr.verdict;
      var ph = landed ? phaseOf(m, j) : {};
      if (ph.dec) s += ". Decisive step";
      if (ph.obs) s += ". The agent first sees the failure";
      return s;
    }
    function cellKey(e, j) {
      stripKey(e, j, props.onSelect);
    }

    var cells = [];
    for (var j = 0; j <= N; j++) cells.push(j);

    /* status text */
    var badge = "READY";
    var bcls = "";
    var msg;
    var sub = "";
    if (F === 0) {
      badge = "NONE";
      msg = "No probes were run.";
      sub = m.reason || "";
    } else if (pos === 0) {
      msg = "Replay the search";
      sub = A.plural(E, "step") + " to search. Press Play, or Step to go one probe at a time.";
    } else if (!landed) {
      badge = cur.verdict.toUpperCase();
      bcls = cur.verdict;
      msg = "Probe " + cur.n + " of " + F + ": restore step " + cur.step + " and run the check";
      sub = (cur.runs != null ? A.plural(cur.runs, "run") + ", " + cur.fails + " failed. " : "");
      if (shade) {
        if (lo >= 0 && hi <= E) sub += "The first failing step is among steps " + (lo + 1) + " to " + hi + ".";
        else if (lo >= 0) sub += "The first failing step comes after step " + lo + ".";
        else if (hi <= E) sub += "The first failing step is at step " + hi + " or before.";
      } else {
        sub += "The history is not monotone, so every step is tested in order.";
      }
    } else if (m.found) {
      badge = "FOUND";
      bcls = "found";
      msg = "Found step " + m.decisive + " in " + A.plural(F, "probe") + (m.confidence ? ", confidence " + m.confidence : "");
      sub = m.decisive >= 1 && STEPS[m.decisive - 1] ? STEPS[m.decisive - 1].label : "";
      if (rp.reconstructed) sub += " (reconstructed, not re-run)";
    } else {
      badge = "STOPPED";
      bcls = "error";
      msg = "The bisect could not name a step";
      sub = m.reason || "";
    }

    var total = E + 1;
    var g = shade && pos > 0 ? A.clamp(lo + 1, 0, total) : 0;
    var b = shade && pos > 0 ? A.clamp(E + 1 - hi, 0, total) : 0;
    var u = total - g - b;
    var seg = function (cls, n) { return html`<div class=${"seg " + cls} style=${{ width: (total ? (n / total) * 100 : 0) + "%" }}></div>`; };

    var goodTxt = lo >= 0 ? "step " + lo : "not known yet";
    var badTxt = hi <= E ? "step " + hi : "not known yet";

    return html`<section class="card replay" aria-label="Bisect replay">
      <div class="rp-head">
        <div>
          <h2>Bisect replay ${rp.reconstructed ? html`<span class="badge-recon">reconstructed</span>` : null}</h2>
          <p class="rp-sub">${rp.reconstructed
            ? "The probe log of a repaired break is not stored. These are the probes a binary search over steps 0 to " + E + " makes when the answer is step " + m.decisive + ". Nothing was re-run."
            : "Binary search over the agent's steps. Each probe restores one snapshot and runs the check."}</p>
        </div>
      </div>
      <div class="legend" aria-hidden="true">
        <span><i class="l-good"></i>known good</span><span><i class="l-bad"></i>known bad</span>
        <span><i class="l-unk"></i>still unknown</span><span><i></i>changes the repo</span><span><i class="l-ro"></i>read only</span>
        <span><i class="l-dec"></i>decisive step</span><span><i class="l-brk"></i>agent working on broken code</span>
        <span><i class="l-obs"></i>agent sees the failure</span>
        ${D.run.sabotage_step != null ? html`<span><i class="l-sab"></i>planted break</span>` : null}
      </div>
      <div class="strip" role="group" aria-label="Steps of the run. Use the left and right arrow keys to move between steps.">
        ${cells.map(function (j) {
          var pr = probed[j];
          var s = j === 0 ? null : STEPS[j - 1];
          return html`<button type="button" key=${j} id=${"cell-" + j} class=${cellClass(j)} tabindex=${props.sel === j ? 0 : -1}
            aria-label=${cellLabel(j)} aria-pressed=${props.sel === j} title=${j === 0 ? "Step 0: the repo before the agent started" : "Step " + j + ": " + s.label}
            onClick=${function () { props.onSelect(j, true); }} onKeyDown=${function (e) { cellKey(e, j); }}>
            ${pr ? html`<span class=${"mark " + pr.verdict}><${A.Icon} name=${VERDICT_ICON[pr.verdict] || "other"} size=${11} /></span>` : null}
            <${A.Glyph} kind=${j === 0 ? "base" : s.kind} size=${15} />
            <span class="ix">${j}</span>
          </button>`;
        })}
      </div>
      <div class="rail">
        <div class="track" role="img" aria-label=${"Interval: last good " + goodTxt + ", first bad " + badTxt}>
          ${seg("good", g)}${seg(shade && pos > 0 ? "unk" : "none", shade && pos > 0 ? u : total)}${seg("bad", b)}
        </div>
        <div class="railcap">
          <span>Last good: <b>${goodTxt}</b></span>
          ${landed && m.found ? html`<span>Culprit: <b>step ${m.decisive}</b></span>` : html`<span>${F ? "Probe " + pos + " of " + F : ""}</span>`}
          <span>First bad: <b>${badTxt}</b></span>
        </div>
      </div>
      <div class="rp-keys">
        <div class="rp-status" role="status" aria-live="polite">
          <div class=${"vbadge " + bcls}>${badge}</div>
          <div><div class="msg">${msg}</div><div class="msg2">${sub}</div></div>
        </div>
        <div class="controls">
          <button type="button" class="btn primary" onClick=${play} disabled=${F === 0} aria-label=${playing ? "Pause the replay" : "Play the replay"}>
            <${A.Icon} name=${playing ? "pause" : "play"} size=${14} /> ${playing ? "Pause" : landed ? "Replay" : "Play"}
          </button>
          <button type="button" class="btn" onClick=${prev} disabled=${pos === 0} aria-label="Previous probe"><${A.Icon} name="back" size=${14} /> Back</button>
          <button type="button" class="btn" onClick=${next} disabled=${landed || F === 0} aria-label="Next probe">Step <${A.Icon} name="step" size=${14} /></button>
          <button type="button" class="btn ghost" onClick=${reset} disabled=${pos === 0} aria-label="Reset the replay"><${A.Icon} name="reset" size=${14} /> Reset</button>
          <div class="grow">
            <input type="range" min="0" max=${F} value=${pos} aria-label="Probe position" disabled=${F === 0}
              onInput=${function (e) { setPlaying(false); setPos(+e.target.value); }} />
          </div>
          <div class="seg-btns" role="group" aria-label="Replay speed">
            ${[0.5, 1, 2, 4].map(function (s) {
              return html`<button type="button" key=${s} aria-pressed=${speed === s} onClick=${function () { setSpeed(s); }}>${s}×</button>`;
            })}
          </div>
        </div>
        <p class="hint"><kbd>Space</kbd> play or pause · <kbd>←</kbd> <kbd>→</kbd> step through the probes · <kbd>Home</kbd> reset.
          Click a step above (then use the arrow keys) to inspect it.</p>
      </div>
    </section>`;
  }

  /* ---------------------------------------------------------------- explorer */

  function Tags(props) {
    var ph = props.ph;
    var s = props.s;
    var v = props.view;
    return html`<span class="tags">
      ${ph.dec ? html`<span class="tag dec">${v && v.type === "episode" ? "BROKE IT" : "DECISIVE"}</span>` : null}
      ${ph.obs ? html`<span class="tag obs">AGENT SEES FAILURE</span>` : null}
      ${ph.brk && !ph.obs ? html`<span class="tag brk">ON BROKEN CODE</span>` : null}
      ${D.run.sabotage_step === s.i ? html`<span class="tag sab">PLANTED BREAK</span>` : null}
      ${props.probe ? html`<span class="tag prb">probe ${props.probe}</span>` : null}
      <span class="tiny muted">${tokensText(s.tokens)} tok${s.exit != null && s.exit !== 0 ? " · exit " + s.exit : ""}</span>
    </span>`;
  }

  function StepList(props) {
    var rows = props.rows;
    function onKey(e, idx) {
      var to = -1;
      if (e.key === "ArrowDown") to = Math.min(idx + 1, rows.length - 1);
      else if (e.key === "ArrowUp") to = Math.max(idx - 1, 0);
      else if (e.key === "Home") to = 0;
      else if (e.key === "End") to = rows.length - 1;
      else if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        props.onSelect(rows[idx].i, true);
        return;
      }
      if (to < 0) return;
      e.preventDefault();
      props.onSelect(rows[to].i, false);
      setTimeout(function () {
        var el = document.getElementById("row-" + rows[to].i);
        if (el) el.focus();
      }, 0);
    }
    if (!rows.length) return html`<div class="empty">No step matches.</div>`;
    var selInList = rows.some(function (r) { return r.i === props.sel; });
    return html`<ul class="steplist" role="listbox" aria-label="Steps" data-keys="steps">
      ${rows.map(function (s, idx) {
        var ph = phaseOf(props.m, s.i);
        return html`<li key=${s.i} id=${"row-" + s.i} role="option" aria-selected=${props.sel === s.i}
          tabindex=${props.sel === s.i || (!selInList && idx === 0) ? 0 : -1}
          class=${cx("srow", s.mut ? "mut" : "ro", ph.dec && "is-dec", ph.obs && "is-obs", ph.brk && !ph.obs && "is-brk")}
          onClick=${function () { props.onSelect(s.i, true); }} onKeyDown=${function (e) { onKey(e, idx); }}>
          <span class="n">${s.i}</span>
          <span class="g"><${A.Glyph} kind=${s.kind} size=${15} /></span>
          <span>
            <span class="lbl">${s.label}</span>
            <${Tags} s=${s} ph=${ph} probe=${props.probeAt[s.i]} view=${props.view} />
          </span>
        </li>`;
      })}
    </ul>`;
  }

  function Ribbon(props) {
    var ph = props.ph;
    var ep = props.view && props.view.type === "episode";
    if (ph.dec)
      return html`<div class=${"ribbon " + (ep ? "fix" : "dec")}>${ep
        ? "This step broke the test. The agent fixed it later."
        : "Decisive step: the first step after which the check fails."}</div>`;
    if (ph.obs)
      return html`<div class="ribbon obs">The agent first sees the failure here.</div>`;
    if (ph.brk)
      return html`<div class="ribbon brk">The check already fails. The agent is working on broken code.</div>`;
    return null;
  }

  function Inspector(props) {
    var i = props.sel;
    var s = i >= 1 ? STEPS[i - 1] : null;
    var ph = phaseOf(props.m, i);
    var head = useRef(null);
    var argsOpen = useState(false);
    useEffect(
      function () {
        if (props.revealTick && head.current && head.current.scrollIntoView)
          head.current.scrollIntoView({ block: "nearest", behavior: A.reducedMotion() ? "auto" : "smooth" });
      },
      [props.revealTick]
    );
    if (i < 0 || (i > N)) return html`<div class="card inspector"><div class="empty">Select a step.</div></div>`;
    var go = function (d) {
      return function () { props.onSelect(A.clamp(i + d, 0, N), false); };
    };
    if (!s)
      return html`<div class="card inspector" aria-label="Step inspector">
        <div class="ins-head" ref=${head} tabindex="-1">
          <div class="ins-title"><span class="num">Step 0</span><h3>The repo before the agent did anything</h3></div>
          <div class="ins-nav"><button type="button" class="btn" disabled=${N === 0} onClick=${go(1)}>Next step</button></div>
        </div>
        <div class="ins-body"><p class="dmsg">This is the base snapshot. Every later snapshot is compared with the one before it.</p></div>
      </div>`;
    var diffBlock = null;
    if (s.mut) {
      var inner;
      if (s.diff_skipped) inner = html`<p class="dmsg">Diff left out to keep this page small.</p>`;
      else if (!s.diff) inner = html`<p class="dmsg">No file changes in this step's snapshot, or the shadow repo is not available.</p>`;
      else inner = html`<${A.DiffView} text=${s.diff} label=${"Diff of step " + s.i} />
          ${s.diff_truncated ? html`<p class="note">Diff clipped to the first lines.</p>` : null}`;
      diffBlock = html`<div class="cap"><span>Changes in this step</span></div>${inner}`;
    }
    return html`<div class="card inspector" aria-label="Step inspector">
      <div class="ins-head" ref=${head} tabindex="-1">
        <div class="ins-title">
          <span class="num">Step ${s.i}</span>
          <h3>${s.label}</h3>
        </div>
        <div class="ins-meta">
          <span>tool <b>${s.tool}</b></span>
          <span>${s.mut ? "changes the repo" : "read only"}</span>
          <span><b>${s.tokens.toLocaleString("en-US")}</b> tokens</span>
          ${s.exit != null ? html`<span>exit <b>${s.exit}</b></span>` : null}
          ${s.snap ? html`<span>snapshot <code>${s.snap}</code></span>` : null}
        </div>
        <div class="ins-nav">
          <button type="button" class="btn" disabled=${i <= 0} onClick=${go(-1)} aria-label="Previous step"><${A.Icon} name="back" size=${14} /> Previous</button>
          <button type="button" class="btn" disabled=${i >= N} onClick=${go(1)} aria-label="Next step">Next <${A.Icon} name="step" size=${14} /></button>
        </div>
      </div>
      <div class="ins-body">
        <${Ribbon} ph=${ph} view=${props.view} />
        ${s.why
          ? html`<div class="cap"><span>What the agent said before this step</span></div><div class="reasoning">${s.why}</div>`
          : null}
        ${diffBlock}
        ${s.out
          ? html`<div class="cap"><span>Output the agent saw</span></div><${A.CodeBlock} text=${s.out} label="Tool output" />`
          : null}
        ${s.args.length
          ? html`<div class="cap"><span>Arguments</span>
              <button type="button" class="btn ghost small" aria-expanded=${argsOpen[0]} onClick=${function () { argsOpen[1](!argsOpen[0]); }}>
                ${argsOpen[0] ? "Hide" : "Show"}</button></div>
            ${argsOpen[0]
              ? s.args.map(function (p) {
                  return html`<div class="argrow"><div class="argname mono">${p[0]}</div><${A.CodeBlock} text=${p[1]} label=${"Argument " + p[0]} /></div>`;
                })
              : html`<p class="small muted mono" style=${{ overflowWrap: "anywhere" }}>${s.args.map(function (p) { return p[0]; }).join(", ")}</p>`}`
          : null}
        ${s.clipped ? html`<p class="note">Long fields were clipped.</p>` : null}
      </div>
    </div>`;
  }

  function Explorer(props) {
    var qS = useState("");
    var q = qS[0];
    var oS = useState(false);
    var onlyMut = oS[0];
    var hay = useMemo(function () {
      return STEPS.map(function (s) {
        return (s.label + "\n" + (s.why || "") + "\n" + s.args.map(function (p) { return p[0] + " " + p[1]; }).join("\n") + "\n" + (s.out || "")).toLowerCase();
      });
    }, []);
    var ql = q.trim().toLowerCase();
    var rows = STEPS.filter(function (s, k) {
      if (onlyMut && !s.mut) return false;
      return !ql || hay[k].indexOf(ql) !== -1;
    });
    useEffect(function () { props.onMatches(ql ? rows : null); }, [ql, onlyMut]);
    var probeAt = {};
    (props.m && props.m.probes ? props.m.probes : []).forEach(function (p) {
      if (probeAt[p.step] === undefined) probeAt[p.step] = "#" + p.n + " " + p.verdict;
    });
    return html`<div class="explorer">
      <div class="card listcard">
        <div class="listtools">
          <label class="searchbox"><${A.Icon} name="search" size=${16} />
            <input type="search" placeholder="Search steps, output, reasoning" aria-label="Search steps" value=${q}
              onInput=${function (e) { qS[1](e.target.value); }} />
          </label>
          <label class="check"><input type="checkbox" checked=${onlyMut} onChange=${function (e) { oS[1](e.target.checked); }} />
            Only steps that changed the repo</label>
          <span class="count" role="status">Showing ${rows.length} of ${N} steps</span>
        </div>
        <${StepList} rows=${rows} sel=${props.sel} onSelect=${props.onSelect} m=${props.m} view=${props.view} probeAt=${probeAt} />
      </div>
      <${Inspector} sel=${props.sel} m=${props.m} view=${props.view} onSelect=${props.onSelect} revealTick=${props.revealTick} />
    </div>`;
  }

  /* ---------------------------------------------------------------- details */

  function ProbeTable(props) {
    var probes = props.probes;
    if (!probes.length) return html`<p class="note">No probes were run.</p>`;
    return html`<div class="tablewrap"><table>
      <thead><tr><th>#</th><th>step</th><th>role</th><th>verdict</th><th>runs</th><th>failed runs</th><th>seconds</th></tr></thead>
      <tbody>${probes.map(function (p) {
        return html`<tr key=${p.n}><td>${p.n}</td><td>${p.step}</td><td>${p.role}</td>
          <td><span class=${"pill " + p.verdict}>${p.verdict}</span></td><td>${p.runs}</td><td>${p.fails}</td><td>${p.seconds.toFixed(2)}</td></tr>`;
      })}</tbody>
    </table></div>`;
  }

  function Details(props) {
    var v = props.view;
    var m = props.m;
    var parts = [];
    if (v.type === "episode" && v.seen_output) {
      parts.push(html`<div class="card dcard" key="seen"><details open>
        <summary>What the agent saw at step ${v.seen_at} (last lines of output)</summary>
        <div class="dbody"><${A.CodeBlock} text=${v.seen_output} label="Output the agent saw" /></div></details></div>`);
    }
    if (m.oracle_tail) {
      parts.push(html`<div class="card dcard" key="tail"><details>
        <summary>Oracle output at step ${m.oracle_tail_step} (last lines)</summary>
        <div class="dbody"><${A.CodeBlock} text=${m.oracle_tail} label="Oracle output" /></div></details></div>`);
    }
    if (v.type === "group" && m.probes) {
      parts.push(html`<div class="card dcard" key="probes"><details>
        <summary>Probe log${v.members.length > 1 ? ": " + m.label : ""}</summary>
        <div class="dbody"><${ProbeTable} probes=${m.probes} />
          <p class="note">${v.found ? "Confidence " + (m.confidence || "not recorded") + " that step " + v.decisive + " is the decisive step. " : ""}${m.policy_rule ? "Oracle rule: " + m.policy_rule + " (assumed flip rate " + m.flake_rate + "). " : ""}${m.non_monotone ? "The history was non-monotone: the check flipped back and forth." : "No sign of a non-monotone history."}</p>
        </div></details></div>`);
    }
    if (v.explanation) {
      parts.push(html`<div class="card dcard aibox" key="ai"><div class="dbody" style=${{ paddingTop: "14px" }}>
        <div class="label">${v.explanation.label}</div>
        <p class="note">${v.explanation.note}</p>
        <p class="aitext">${v.explanation.text}</p></div></div>`);
    }
    if (!parts.length) return null;
    return html`<div class="dgrid">${parts}</div>`;
  }

  /* ---------------------------------------------------------------- app */

  function App() {
    var tabS = useState(0);
    var memS = useState(0);
    var selS = useState(initialSel);
    var tickS = useState(0);
    var matchS = useState(null);
    var tab = Math.min(tabS[0], Math.max(VIEWS.length - 1, 0));
    var view = VIEWS[tab] || null;
    var m = view ? memberOf(view, memS[0]) : null;
    var sel = selS[0];

    function select(i, reveal) {
      selS[1](i);
      try {
        history.replaceState(null, "", "#step-" + i);
      } catch (e) {}
      if (reveal) tickS[1](function (t) { return t + 1; });
    }
    useEffect(function () {
      function onHash() {
        var h = /^#step-(\d+)$/.exec(location.hash || "");
        if (h && +h[1] <= N) selS[1](+h[1]);
      }
      window.addEventListener("hashchange", onHash);
      return function () { window.removeEventListener("hashchange", onHash); };
    }, []);
    function onTab(i) {
      tabS[1](i);
      memS[1](0);
      var v = VIEWS[i];
      if (v && v.decisive) select(v.decisive, false);
    }
    function onMatches(rows) {
      if (!rows) {
        matchS[1](null);
        return;
      }
      var set = {};
      rows.forEach(function (s) { set[s.i] = true; });
      matchS[1](set);
    }

    var crumb = D.run.id;
    return html`<div>
      <${A.TopBar} kind="report" crumb=${crumb} />
      <main class="wrap page">
        <${RunMeta} />
        ${D.overview
          ? html`<div class=${"notice " + D.overview.kind} role="note" style=${{ marginTop: "14px" }}>
              <strong>${D.overview.title}</strong><div>${D.overview.text}</div></div>`
          : null}
        ${VIEWS.length > 1 ? html`<${Tabs} views=${VIEWS} tab=${tab} onTab=${onTab} />` : null}
        ${view
          ? html`<div id="panel" role=${VIEWS.length > 1 ? "tabpanel" : null} aria-labelledby=${VIEWS.length > 1 ? "tab-" + tab : null}>
              <div class="section"><${Hero} view=${view} m=${m} mi=${memS[0]} onMember=${function (k) { memS[1](k); }} onSelect=${select} /></div>
              ${m.replay && m.replay.frames && (view.found || m.replay.frames.length)
                ? html`<div class="section"><${Replay} key=${view.id + ":" + memS[0]} m=${m} sel=${sel} onSelect=${select} matches=${matchS[0]} /></div>`
                : null}
            </div>`
          : null}
        ${!view && N ? html`<div class="section"><${PlainStrip} sel=${sel} onSelect=${select} matches=${matchS[0]} /></div>` : null}
        <div class="section">
          <h2>Step explorer</h2>
          <${Explorer} sel=${sel} onSelect=${select} m=${m} view=${view} revealTick=${tickS[0]} onMatches=${onMatches} />
        </div>
        ${view ? html`<div class="section"><${Details} view=${view} m=${m} /></div>` : null}
        <p class="footer">The verdict comes from running your check on each snapshot. No language model decides pass or fail.<br />Made by AgentBisect.</p>
      </main>
    </div>`;
  }

  var root = document.getElementById("app");
  root.textContent = "";
  A.render(html`<${App} />`, root);
})();
