/* Shared helpers and components for the report pages. Loaded after Preact, hooks and htm.
   Everything from a run reaches the DOM as a text node or an attribute value. */
(function () {
  "use strict";
  var P = preact;
  var H = preactHooks;
  var html = htm.bind(P.h);
  var AB = {
    h: P.h,
    html: html,
    render: P.render,
    Fragment: P.Fragment,
    useState: H.useState,
    useEffect: H.useEffect,
    useRef: H.useRef,
    useMemo: H.useMemo,
    useCallback: H.useCallback,
    useLayoutEffect: H.useLayoutEffect,
  };
  window.AB = AB;

  AB.readData = function () {
    return JSON.parse(document.getElementById("data").textContent);
  };
  AB.cx = function () {
    var out = [];
    for (var i = 0; i < arguments.length; i++) if (arguments[i]) out.push(arguments[i]);
    return out.join(" ");
  };
  AB.plural = function (n, one, many) {
    return n + " " + (n === 1 ? one : many || one + "s");
  };
  AB.reducedMotion = function () {
    try {
      return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    } catch (e) {
      return false;
    }
  };
  AB.clamp = function (x, lo, hi) {
    return Math.max(lo, Math.min(hi, x));
  };
  AB.isTyping = function (el) {
    if (!el || !el.tagName) return false;
    var t = el.tagName;
    return t === "INPUT" || t === "TEXTAREA" || t === "SELECT" || el.isContentEditable === true;
  };

  /* ------------------------------------------------------------------ icons */

  var PATHS = {
    read: ["M1.5 8s2.3-4.5 6.5-4.5S14.5 8 14.5 8s-2.3 4.5-6.5 4.5S1.5 8 1.5 8z", "M8 6.2a1.8 1.8 0 1 0 0 3.6 1.8 1.8 0 0 0 0-3.6z"],
    write: ["M4 1.8h5l3 3v9.4H4z", "M8 7.2v4M6 9.2h4"],
    edit: ["M2.8 13.2l.7-3.1L11 2.6l2.4 2.4-7.5 7.5z", "M9.6 4l2.4 2.4"],
    shell: ["M2.5 4.5L6 8l-3.5 3.5", "M8 12h5.5"],
    finish: ["M4 14.2V2.2", "M4 3h8.2l-1.7 2.9 1.7 2.9H4"],
    other: ["M8 5.2a2.8 2.8 0 1 0 0 5.6 2.8 2.8 0 0 0 0-5.6z"],
    base: ["M3.5 3v10", "M3.5 3.5h8L9.6 6.2l1.9 2.7h-8"],
    play: ["M4.5 2.8v10.4L13 8z"],
    pause: ["M5 3v10", "M11 3v10"],
    step: ["M3.5 3v10L10 8z", "M12.5 3v10"],
    back: ["M12.5 3v10L6 8z", "M3.5 3v10"],
    reset: ["M3 8a5 5 0 1 0 1.6-3.7", "M3 2.6v3h3"],
    sun: ["M8 5.3a2.7 2.7 0 1 0 0 5.4 2.7 2.7 0 0 0 0-5.4z", "M8 1.2v1.5M8 13.3v1.5M1.2 8h1.5M13.3 8h1.5M3.2 3.2l1.1 1.1M11.7 11.7l1.1 1.1M3.2 12.8l1.1-1.1M11.7 4.3l1.1-1.1"],
    moon: ["M13.2 9.6A5.6 5.6 0 0 1 6.4 2.8a5.6 5.6 0 1 0 6.8 6.8z"],
    search: ["M7 2.8a4.2 4.2 0 1 0 0 8.4 4.2 4.2 0 0 0 0-8.4z", "M10.2 10.2l3.3 3.3"],
    chev: ["M4 6l4 4 4-4"],
    check: ["M3 8.4l3.2 3.2L13 4.8"],
    cross: ["M4 4l8 8M12 4l-8 8"],
    warn: ["M8 2.4l6 10.6H2z", "M8 6.6v3M8 11.2v.1"],
    link: ["M6.6 9.4a3 3 0 0 0 4.2 0l2-2a3 3 0 0 0-4.2-4.2l-.8.8", "M9.4 6.6a3 3 0 0 0-4.2 0l-2 2a3 3 0 0 0 4.2 4.2l.8-.8"],
    arrow: ["M3 8h10M9 4l4 4-4 4"],
  };
  AB.Icon = function (props) {
    var name = props.name;
    var size = props.size || 16;
    var paths = PATHS[name] || PATHS.other;
    var fill = name === "play" || name === "pause" ? "currentColor" : "none";
    return html`<svg width=${size} height=${size} viewBox="0 0 16 16" fill=${fill} stroke="currentColor"
      stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">
      ${paths.map(function (d) { return html`<path d=${d} />`; })}
    </svg>`;
  };
  AB.Glyph = function (props) {
    var kind = PATHS[props.kind] ? props.kind : "other";
    return html`<${AB.Icon} name=${kind} size=${props.size || 15} />`;
  };

  AB.Logo = function () {
    return html`<svg width="26" height="26" viewBox="0 0 28 28" fill="none" aria-hidden="true">
      <rect x="1" y="1" width="26" height="26" rx="8" fill="currentColor" opacity=".14" />
      <path d="M7 14h5.5M12.5 14l6-5.5M12.5 14l6 5.5" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" />
      <circle cx="7" cy="14" r="2" fill="currentColor" />
      <circle cx="19.5" cy="8.5" r="2.1" fill="var(--green)" />
      <circle cx="19.5" cy="19.5" r="2.1" fill="var(--red-solid)" />
    </svg>`;
  };

  /* ------------------------------------------------------------------ theme */

  function currentTheme() {
    var t = document.documentElement.getAttribute("data-theme");
    if (t === "light" || t === "dark") return t;
    try {
      return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
    } catch (e) {
      return "light";
    }
  }
  AB.ThemeToggle = function () {
    var s = AB.useState(currentTheme());
    var theme = s[0];
    AB.useEffect(function () {
      // follow the system setting while the person has not picked a theme
      var mq;
      try {
        mq = window.matchMedia("(prefers-color-scheme: dark)");
      } catch (e) {
        return undefined;
      }
      function onChange() {
        s[1](currentTheme());
      }
      if (mq.addEventListener) mq.addEventListener("change", onChange);
      return function () {
        if (mq.removeEventListener) mq.removeEventListener("change", onChange);
      };
    }, []);
    function flip() {
      var next = theme === "dark" ? "light" : "dark";
      document.documentElement.setAttribute("data-theme", next);
      try {
        localStorage.setItem("agentbisect-theme", next);
      } catch (e) {}
      s[1](next);
    }
    var label = theme === "dark" ? "Switch to light theme" : "Switch to dark theme";
    return html`<button type="button" class="btn icon" onClick=${flip} aria-label=${label} title=${label}>
      <${AB.Icon} name=${theme === "dark" ? "sun" : "moon"} size=${17} />
      <span class="tlabel">${theme === "dark" ? "Light" : "Dark"}</span>
    </button>`;
  };

  AB.TopBar = function (props) {
    return html`<header class="topbar">
      <div class="wrap">
        <span class="brand"><${AB.Logo} /> AgentBisect <small>${props.kind}</small></span>
        <span class="grow crumb">${props.crumb}</span>
        ${props.children}
        <${AB.ThemeToggle} />
      </div>
    </header>`;
  };

  /* ------------------------------------------------------------------ blocks */

  /* A monospace block. Long text starts capped and opens with a button. */
  AB.CodeBlock = function (props) {
    var text = props.text || "";
    var lines = text.split("\n").length;
    var long = lines > 14 || text.length > 1500;
    var s = AB.useState(false);
    var open = s[0];
    var capped = long && !open;
    return html`<div class=${AB.cx("capbox", capped && "capped")}>
      <pre class=${AB.cx("code", props.nowrap && "nowrap")} tabindex="0" aria-label=${props.label}>${text}</pre>
      ${long
        ? html`<button type="button" class="btn ghost more small" aria-expanded=${open} onClick=${function () { s[1](!open); }}>
            ${open ? "Show less" : "Show all " + lines + " lines"}
          </button>`
        : null}
    </div>`;
  };

  /* ------------------------------------------------------------------ diff */

  var KEYWORDS = {
    py: "and as assert async await break class continue def del elif else except False finally for from global if import in is lambda None nonlocal not or pass raise return True try while with yield self",
    js: "async await break case catch class const continue default delete do else export extends false finally for from function if import in instanceof let new null of return static super switch this throw true try typeof undefined var void while yield",
  };
  var KWSET = {};
  Object.keys(KEYWORDS).forEach(function (k) {
    var set = {};
    KEYWORDS[k].split(" ").forEach(function (w) { set[w] = true; });
    KWSET[k] = set;
  });
  var LANG = { py: "py", pyi: "py", js: "js", mjs: "js", ts: "js", tsx: "js", jsx: "js" };
  var TOKEN_RE = /(#.*$|\/\/.*$)|("(?:[^"\\]|\\.)*"?|'(?:[^'\\]|\\.)*'?)|(\b\d[\d_]*(?:\.\d+)?\b)|([A-Za-z_][A-Za-z_0-9]*)/g;

  /* Split one line of code into [className, text] pieces. A very light tokenizer: comments,
     strings, numbers and keywords. Anything else stays plain. */
  AB.tokenize = function (line, lang) {
    if (!lang || line.length > 400) return [["", line]];
    var set = KWSET[lang];
    var out = [];
    var last = 0;
    var m;
    TOKEN_RE.lastIndex = 0;
    while ((m = TOKEN_RE.exec(line))) {
      var cls = "";
      if (m[1] !== undefined) {
        if (lang === "py" && m[1].slice(0, 2) === "//") continue;
        if (lang === "js" && m[1][0] === "#") continue;
        cls = "tk-c";
      } else if (m[2] !== undefined) cls = "tk-s";
      else if (m[3] !== undefined) cls = "tk-n";
      else if (set[m[4]]) cls = "tk-k";
      else continue;
      if (m.index > last) out.push(["", line.slice(last, m.index)]);
      out.push([cls, m[0]]);
      last = m.index + m[0].length;
    }
    if (last < line.length) out.push(["", line.slice(last)]);
    return out;
  };

  /* Parse a unified diff into files -> rows. Row: {k: add|del|ctx|hunk|note, a: old line, b: new line, t: text}. */
  AB.parseDiff = function (text) {
    var files = [];
    var f = null;
    var inHunk = false;
    var oldN = 0;
    var newN = 0;
    var lines = text.split("\n");
    if (lines.length && lines[lines.length - 1] === "") lines.pop();
    for (var i = 0; i < lines.length; i++) {
      var ln = lines[i];
      if (ln.slice(0, 11) === "diff --git ") {
        var mm = /^diff --git a\/(.*) b\/(.*)$/.exec(ln);
        f = { path: mm ? mm[2] : ln.slice(11), status: "modified", rows: [], add: 0, del: 0 };
        files.push(f);
        inHunk = false;
        continue;
      }
      if (!f) {
        f = { path: "(diff)", status: "modified", rows: [], add: 0, del: 0 };
        files.push(f);
      }
      if (!inHunk) {
        if (ln.slice(0, 8) === "new file") f.status = "added";
        else if (ln.slice(0, 12) === "deleted file") f.status = "deleted";
        else if (ln.slice(0, 4) === "--- " && f.path === "(diff)") f.path = ln.slice(4).replace(/^a\//, "");
        else if (ln.slice(0, 13) === "Binary files ") f.rows.push({ k: "note", t: ln });
        else if (ln.slice(0, 2) === "@@") {
          inHunk = true;
          i--;
        }
        continue;
      }
      if (ln.slice(0, 2) === "@@") {
        var h = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/.exec(ln);
        oldN = h ? parseInt(h[1], 10) : 0;
        newN = h ? parseInt(h[2], 10) : 0;
        f.rows.push({ k: "hunk", t: ln });
        continue;
      }
      var c = ln.charAt(0);
      if (c === "+") {
        f.rows.push({ k: "add", b: newN++, t: ln.slice(1) });
        f.add++;
      } else if (c === "-") {
        f.rows.push({ k: "del", a: oldN++, t: ln.slice(1) });
        f.del++;
      } else if (c === "\\") {
        f.rows.push({ k: "note", t: ln.slice(1).trim() });
      } else {
        f.rows.push({ k: "ctx", a: oldN++, b: newN++, t: ln.slice(1) });
      }
    }
    return files;
  };

  function langOf(path) {
    var m = /\.([A-Za-z0-9]+)$/.exec(path || "");
    return m ? LANG[m[1].toLowerCase()] : null;
  }

  AB.DiffView = function (props) {
    var files = AB.useMemo(function () { return AB.parseDiff(props.text || ""); }, [props.text]);
    if (!files.length) return null;
    return html`<div class="diff" role="group" aria-label=${props.label || "Diff"}>
      ${files.map(function (f) {
        var lang = langOf(f.path);
        return html`<section class="dfile">
          <div class="dhead">
            <span class="path mono">${f.path}</span>
            ${f.status !== "modified" ? html`<span class="tag prb">${f.status}</span>` : null}
            <span class="st mono"><span class="plus">+${f.add}</span> <span class="minus">−${f.del}</span></span>
          </div>
          <div class="dscroll">
            ${f.rows.map(function (r) {
              if (r.k === "hunk" || r.k === "note")
                return html`<div class=${"dl " + r.k}><span class="ln"></span><span class="ln"></span><span class="sg"></span><code>${r.t}</code></div>`;
              var sign = r.k === "add" ? "+" : r.k === "del" ? "−" : "";
              var toks = AB.tokenize(r.t, lang);
              return html`<div class=${"dl " + r.k}>
                <span class="ln">${r.a === undefined ? "" : r.a}</span>
                <span class="ln">${r.b === undefined ? "" : r.b}</span>
                <span class="sg">${sign}</span>
                <code>${toks.map(function (t) { return t[0] ? html`<span class=${t[0]}>${t[1]}</span>` : t[1]; })}</code>
              </div>`;
            })}
          </div>
        </section>`;
      })}
    </div>`;
  };
})();
