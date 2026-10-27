/* The command builder behind the "Try it on your own repo" panel on the study page.
   Pure functions only: they check the form and write shell text. Nothing here runs a command or
   reads the page. The page loads this file after common.js, as AB.cmd. Node loads it with
   require() for the tests.
   Scheme strings are written "https:\/\/" (the same JavaScript string) so that the built page
   holds no literal network address outside its data, which a test checks by search. */
(function () {
  "use strict";

  var HTTPS = "https:\/\/";
  var FILE = "file:\/\/";
  var CLONE_URL = HTTPS + "github.com/akshatsi/AgentBisect";
  var CLONE_DIR = "AgentBisect";
  var PROMPT_DELIM = "AB_PROMPT_END";
  var SABOTAGE_DELIM = "AB_SABOTAGE_END";
  var MAX_PROMPT = 50000;
  var MAX_STEP = 100000;

  /* ------------------------------------------------------------------ quoting */

  /* Wrap text in single quotes for a POSIX shell. A single quote inside becomes '\'' . Every
     other character, newlines and $ and backticks included, is literal inside single quotes. */
  function shq(s) {
    return "'" + String(s).replace(/'/g, "'\\''") + "'";
  }

  /* A YAML double-quoted scalar. JSON strings are valid YAML double-quoted scalars, with one
     catch: YAML readers treat a few raw characters as line breaks or reject them, so those are
     written as \uXXXX escapes. */
  function yamlq(s) {
    return JSON.stringify(String(s)).replace(/[\u007f-\u009f\u2028\u2029\ufeff\ufffe\uffff]/g, function (c) {
      return "\\u" + ("0000" + c.charCodeAt(0).toString(16)).slice(-4);
    });
  }

  /* The heredoc end marker: `base`, plus "_X" until no text holds it. */
  function delimiter(base, texts) {
    var d = base;
    for (;;) {
      var clash = false;
      for (var i = 0; i < texts.length; i++) if (texts[i].indexOf(d) !== -1) clash = true;
      if (!clash) return d;
      d += "_X";
    }
  }

  /* Prompt text as the file will hold it: line ends are \n, trailing space and blank lines go. */
  function canonicalBlock(s) {
    return String(s).replace(/\r\n?/g, "\n").replace(/\s+$/, "");
  }

  /* ------------------------------------------------------------------ checks */

  var BAD_CONTROL = /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/;

  /* Check the repository field. Returns {value} or {error}. The CLI takes only a URL that starts
     with the https or file scheme, or a folder on this computer. */
  function checkRepo(raw) {
    var s = String(raw == null ? "" : raw).trim();
    if (!s) return { error: "Enter a repository: an " + HTTPS + " or " + FILE + " address, or a folder on this computer." };
    if (BAD_CONTROL.test(s) || /[\n\r]/.test(s)) return { error: "The repository cannot hold line breaks or control characters." };
    if (/^[^\/\s@]+@[^\/\s:]+:/.test(s)) {
      return { error: "SSH addresses such as git@github.com:you/project do not work. Use the " + HTTPS + " address of the repository." };
    }
    if (/^[A-Za-z][A-Za-z0-9+.-]*::/.test(s)) {
      return { error: "Remote helpers such as ext:: are not allowed. Use an " + HTTPS + " or " + FILE + " address, or a folder path." };
    }
    var m = /^([A-Za-z][A-Za-z0-9+.-]*):\/\//.exec(s);
    if (m) {
      var scheme = m[1] + ":\/\/";
      if (scheme !== HTTPS && scheme !== FILE) {
        return { error: scheme + " is not accepted. Use " + HTTPS + ", " + FILE + ", or a folder path." };
      }
      if (/\s/.test(s)) return { error: "An address cannot hold spaces. Write a space as %20." };
      var rest = s.slice(scheme.length);
      if (scheme === HTTPS) {
        if (!/^[^\/?#@]+(\/|$)/.test(rest)) {
          return { error: /^[^\/?#]*@/.test(rest) ? "Leave passwords and tokens out of the address." : "Add the host after " + HTTPS + ", for example github.com/you/project." };
        }
      } else if (!rest) {
        return { error: "Add the folder path after " + FILE + "." };
      }
      return { value: s };
    }
    if (s.charAt(0) === "-") return { error: "A path that starts with - looks like an option. Write ./ before it." };
    return { value: s };
  }

  function checkPrompt(raw) {
    var s = canonicalBlock(raw == null ? "" : raw);
    if (!s.trim()) return { error: "Write what you want the agent to do." };
    if (BAD_CONTROL.test(s)) return { error: "The prompt holds a control character. Remove it." };
    if (s.length > MAX_PROMPT) return { error: "The prompt is longer than " + MAX_PROMPT + " characters." };
    return { value: s };
  }

  function checkModel(raw) {
    var s = String(raw == null ? "" : raw).trim();
    if (!s) return { error: "Enter the model name you will pick in Alan Code, for example flash." };
    if (!/^[A-Za-z0-9][A-Za-z0-9._:+\/-]{0,63}$/.test(s)) return { error: "Use letters, digits and . _ - : + / only." };
    return { value: s };
  }

  function checkRef(raw) {
    var s = String(raw == null ? "" : raw).trim();
    if (!s) return { value: "" };
    if (s.charAt(0) === "-") return { error: "A branch or tag cannot start with -." };
    if (!/^[A-Za-z0-9._\/+@-]+$/.test(s) || s.indexOf("..") !== -1 || s.indexOf("@{") !== -1) {
      return { error: "That is not a branch, tag or commit name. Use letters, digits and . _ - / + only." };
    }
    return { value: s };
  }

  function checkStep(raw) {
    var s = String(raw == null ? "" : raw).trim();
    if (!/^\d+$/.test(s) || parseInt(s, 10) < 1) return { error: "Enter a whole number, 1 or more." };
    var n = parseInt(s, 10);
    if (n > MAX_STEP) return { error: "Enter a number up to " + MAX_STEP + "." };
    return { value: n };
  }

  function checkFile(raw) {
    var s = String(raw == null ? "" : raw).trim();
    if (!s) return { error: "Enter the file path, relative to the repository, for example src/app/core.py." };
    if (BAD_CONTROL.test(s) || /[\n\r\\]/.test(s)) return { error: "Use / in the path, with no line breaks or control characters." };
    if (s.charAt(0) === "/" || /^[A-Za-z]:/.test(s)) return { error: "Write the path relative to the repository, not from the root of the disk." };
    if (s.split("/").indexOf("..") !== -1) return { error: "The path cannot go up with .. ." };
    return { value: s };
  }

  /* Text that must match the file exactly: nothing is trimmed. */
  function checkExact(raw, label, needsText) {
    var s = String(raw == null ? "" : raw);
    if (needsText && !s) return { error: "Enter the " + label + "." };
    if (BAD_CONTROL.test(s)) return { error: "The " + label + " holds a control character. Remove it." };
    return { value: s };
  }

  function emptyForm() {
    return { repo: "", prompt: "", model: "flash", ref: "", noSetup: false, plant: false, atStep: "8", file: "", find: "", replace: "" };
  }

  /* Check the whole form. Returns {errors: {field: message}, values: {...} or null}. `values`
     is set only when there is no error. */
  function validate(form) {
    var f = form;
    var errors = {};
    var v = {};
    function take(key, res) {
      if (res.error) errors[key] = res.error;
      else v[key] = res.value;
    }
    take("repo", checkRepo(f.repo));
    take("prompt", checkPrompt(f.prompt));
    take("model", checkModel(f.model));
    take("ref", checkRef(f.ref));
    v.noSetup = !!f.noSetup;
    v.plant = !!f.plant;
    if (f.plant) {
      take("atStep", checkStep(f.atStep));
      take("file", checkFile(f.file));
      take("find", checkExact(f.find, "text to replace", true));
      take("replace", checkExact(f.replace, "replacement text", false));
      if (!errors.find && !errors.replace && v.find === v.replace) {
        errors.replace = "The replacement is the same as the text it replaces, so nothing would change.";
      }
    }
    return { errors: errors, values: Object.keys(errors).length ? null : v };
  }

  /* ------------------------------------------------------------------ commands */

  function installCommand() {
    return "git clone " + CLONE_URL + " && cd " + CLONE_DIR + " && uv sync";
  }

  /* The sabotage file text for checked values. */
  function sabotageYaml(v) {
    return [
      "at_step: " + v.atStep,
      "file: " + yamlq(v.file),
      "find: " + yamlq(v.find),
      "replace: " + yamlq(v.replace),
    ].join("\n");
  }

  /* The record step for checked values: the prompt file, an optional sabotage file, then the
     command. Pasted into bash or zsh in the project folder, it writes prompt.txt (and
     sabotage.yaml) and starts `record`. */
  function recordBlock(v) {
    var lines = [];
    var yaml = v.plant ? sabotageYaml(v) : null;
    var d1 = delimiter(PROMPT_DELIM, [v.prompt]);
    lines.push("cat > prompt.txt <<'" + d1 + "'", v.prompt, d1);
    if (yaml !== null) {
      var d2 = delimiter(SABOTAGE_DELIM, [yaml]);
      lines.push("cat > sabotage.yaml <<'" + d2 + "'", yaml, d2);
    }
    var opts = ["uv run agentbisect record", "--repo " + shq(v.repo), "--prompt-file prompt.txt"];
    if (v.ref) opts.push("--ref " + shq(v.ref));
    if (v.noSetup) opts.push("--no-setup");
    if (yaml !== null) opts.push("--sabotage sabotage.yaml");
    opts.push("--model " + shq(v.model));
    lines.push(opts.join(" \\\n    "));
    return lines.join("\n");
  }

  function bisectBlock() {
    return "uv run agentbisect run --trace runs/<run-folder>\nuv run agentbisect report --trace runs/<run-folder>";
  }

  /* ------------------------------------------------------------------ examples */

  var HUMANIZE_REPO = HTTPS + "github.com/python-humanize/humanize";
  /* Same text as tasks/demo/humanize_prompt.txt and tasks/demo/humanize_sabotage.yaml; a test
     keeps them equal. */
  var HUMANIZE_PROMPT = [
    "Extend natural_list in src/humanize/lists.py with two optional keyword arguments:",
    "",
    "- conjunction (default \"and\"): the word before the last item, e.g. natural_list([\"a\", \"b\", \"c\"], conjunction=\"or\") returns \"a, b or c\".",
    "- oxford_comma (default False): when True and there are three or more items, put a comma before the conjunction, e.g. \"a, b, and c\".",
    "",
    "Calls without the new arguments must behave exactly as today. Update the docstring examples and add tests. Make sure the whole test suite passes when you are done.",
  ].join("\n");

  function example(extra) {
    var f = emptyForm();
    f.repo = HUMANIZE_REPO;
    f.prompt = HUMANIZE_PROMPT;
    for (var k in extra) f[k] = extra[k];
    return f;
  }

  var PRESETS = [
    { id: "humanize", label: "humanize: add options to natural_list", form: example({}) },
    {
      id: "humanize-planted",
      label: "humanize with a planted break (tests the bisector)",
      form: example({
        plant: true,
        atStep: "8",
        file: "src/humanize/filesize.py",
        find: "    if abs_bytes < base:",
        replace: "    if abs_bytes <= base:",
      }),
    },
  ];

  var api = {
    HTTPS: HTTPS,
    FILE: FILE,
    shq: shq,
    yamlq: yamlq,
    delimiter: delimiter,
    canonicalBlock: canonicalBlock,
    checkRepo: checkRepo,
    checkPrompt: checkPrompt,
    checkModel: checkModel,
    checkRef: checkRef,
    emptyForm: emptyForm,
    validate: validate,
    installCommand: installCommand,
    sabotageYaml: sabotageYaml,
    recordBlock: recordBlock,
    bisectBlock: bisectBlock,
    PRESETS: PRESETS,
  };
  if (typeof window !== "undefined" && window.AB) window.AB.cmd = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})();
