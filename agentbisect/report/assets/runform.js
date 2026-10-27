/* Form parts shared by the study page's command builder and the local app: a labelled Field, a
   Copy button, and the run form (repository, prompt, model, branch, two checkboxes, planted
   break). Loaded after common.js and builder.js; adds AB.Field, AB.CopyButton, AB.useRunForm and
   AB.RunFields. Run text reaches the DOM through text nodes and attribute values only. */
(function () {
  "use strict";
  var A = AB;
  var html = A.html;
  var useState = A.useState;
  var useMemo = A.useMemo;
  var cx = A.cx;
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

  A.CopyButton = function (props) {
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
  };

  A.Field = function (props) {
    var id = props.id;
    var describedBy = [props.hint ? id + "-hint" : null, props.error ? id + "-err" : null].filter(Boolean).join(" ") || null;
    var control = props.control({ id: id, "aria-invalid": props.error ? "true" : null, "aria-describedby": describedBy, "aria-required": props.required ? "true" : null });
    return html`<div class=${cx("field", props.wide && "wide")}>
      <label for=${id}>${props.label}${props.required ? null : html` <span class="opt">(optional)</span>`}</label>
      ${control}
      ${props.hint ? html`<p class="hint" id=${id + "-hint"}>${props.hint}</p>` : null}
      ${props.error ? html`<p class="ferr" id=${id + "-err"} role="alert">${props.error}</p>` : null}
    </div>`;
  };

  /* The state of the run form. `shown(key)` is the error to draw for a field: only once the
     person has typed in it or left it. `touchAll()` marks every field, for a submit that fails. */
  A.useRunForm = function () {
    var fS = useState(C.emptyForm());
    var tS = useState({});
    var form = fS[0];
    var touched = tS[0];
    var result = useMemo(function () { return C.validate(form); }, [form]);
    var errors = result.errors;
    function set(key, value) {
      var next = Object.assign({}, form);
      next[key] = value;
      fS[1](next);
    }
    function touch(key) {
      if (!touched[key]) {
        var next = Object.assign({}, touched);
        next[key] = true;
        tS[1](next);
      }
    }
    function touchAll() {
      var next = {};
      Object.keys(errors).forEach(function (k) { next[k] = true; });
      tS[1](Object.assign({}, touched, next));
    }
    function shown(key) {
      var v = form[key];
      return errors[key] && (touched[key] || (typeof v === "string" && v.trim() !== "")) ? errors[key] : null;
    }
    function load(values) {
      fS[1](Object.assign(C.emptyForm(), values));
      tS[1]({});
    }
    return { form: form, errors: errors, values: result.values, set: set, touch: touch, touchAll: touchAll, shown: shown, load: load };
  };

  /* The fields of the run form, in the cells of a `.tryform` grid. `prefix` makes the ids
     (`try-repo`, ...); `lead` is any extra cell drawn first. `onEdit` runs on every change. */
  A.RunFields = function (props) {
    var rf = props.rf;
    var p = props.prefix;
    var form = rf.form;
    function change(key, value) {
      rf.set(key, value);
      if (props.onEdit) props.onEdit();
    }
    function text(key) {
      return function (extra) {
        return Object.assign({}, extra, {
          value: form[key],
          onInput: function (e) { change(key, e.target.value); },
          onBlur: function () { rf.touch(key); },
        });
      };
    }
    function input(key, extra) {
      return function (q) { return html`<input type="text" autocomplete="off" autocapitalize="off" spellcheck="false" ...${Object.assign({}, q, text(key)(), extra)} />`; };
    }
    function area(key, rows, mono) {
      return function (q) { return html`<textarea rows=${rows} spellcheck=${mono ? "false" : "true"} class=${mono ? "mono" : null} ...${Object.assign({}, q, text(key)())}></textarea>`; };
    }
    return html`<${A.Fragment}>
      ${props.lead}
      <${A.Field} id=${p + "-repo"} wide=${true} required=${true} label="Repository" error=${rf.shown("repo")}
        hint=${"A git address (" + C.HTTPS + " or " + C.FILE + ") or a folder on this computer."} control=${input("repo", { placeholder: C.HTTPS + "github.com/you/project" })} />
      <${A.Field} id=${p + "-prompt"} wide=${true} required=${true} label="Task prompt" error=${rf.shown("prompt")}
        hint="What to ask the agent." control=${area("prompt", 6, false)} />
      <${A.Field} id=${p + "-model"} required=${true} label="Model" error=${rf.shown("model")}
        hint="The name you will pick in Alan Code. It becomes --model." control=${input("model")} />
      <${A.Field} id=${p + "-ref"} label="Branch or tag" error=${rf.shown("ref")} hint="Starts from this branch or tag. It becomes --ref." control=${input("ref", { placeholder: "main" })} />
      <div class="field wide checks">
        <label class="check"><input type="checkbox" id=${p + "-nosetup"} checked=${form.noSetup} onChange=${function (e) { change("noSetup", !!e.target.checked); }} />
          <span>${props.noSetupLabel}</span></label>
        <label class="check"><input type="checkbox" id=${p + "-plant"} checked=${form.plant} onChange=${function (e) { change("plant", !!e.target.checked); }} />
          <span>Plant a known break to test the bisector</span></label>
      </div>
      ${form.plant
        ? html`<div class="field wide plant" role="group" aria-label="Planted break">
            <p class="small muted">After the chosen step, AgentBisect edits one file to break it. Then it checks that the bisector names that step.</p>
            <div class="plantgrid">
              <${A.Field} id=${p + "-step"} required=${true} label="After which step" error=${rf.shown("atStep")} control=${input("atStep", { inputmode: "numeric" })} />
              <${A.Field} id=${p + "-file"} required=${true} label="File path" error=${rf.shown("file")} hint="Relative to the repository." control=${input("file", { placeholder: "src/package/module.py" })} />
              <${A.Field} id=${p + "-find"} wide=${true} required=${true} label="Text to replace" error=${rf.shown("find")} hint="Exact text, spaces included. It must occur once in the file." control=${area("find", 2, true)} />
              <${A.Field} id=${p + "-replace"} wide=${true} required=${false} label="Replacement text" error=${rf.shown("replace")} hint="Empty deletes the text." control=${area("replace", 2, true)} />
            </div>
          </div>`
        : null}
    <//>`;
  };
})();
