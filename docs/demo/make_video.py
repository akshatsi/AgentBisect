#!/usr/bin/env python3
"""Build the AgentBisect demo video: a real browser is driven through the real pages.

Playwright opens the pages that `agentbisect report`, `study` and `serve` write, moves a visible
cursor, clicks, types and scrolls, and records the screen. The terminal text is the real output of
`agentbisect run`. The voice is Kokoro, a small neural text-to-speech model that runs on this
machine. ffmpeg joins the scenes.

Needs: Google Chrome, ffmpeg, and `uv sync` done. Run it with its own throwaway environment, so the
project's dependencies stay as they are:

    uv run --no-project --python 3.12 --with playwright --with kokoro-onnx --with soundfile --with numpy \\
        python docs/demo/make_video.py

    --voice am_michael   other voices: af_heart, am_adam, bf_emma, af_nicole, ...
    --speed 1.12       speaking speed (1.0 is the model's default)
    --keep             keep the work folder (frames, audio, raw recordings)

First run downloads the Kokoro model (about 350 MB) into ~/.cache/agentbisect/kokoro.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CACHE = Path.home() / ".cache/agentbisect/kokoro"
MODEL_URL = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/"
W, H = 1920, 1080
ZOOM = 1.3  # the pages are laid out for ~1280 px; zooming makes them fill a 1080p screen
PORT = 8795

# --------------------------------------------------------------------------------------- script

SCENES = {
    "hook": (
        "An AI coding agent works for twenty minutes, makes thirty edits, and your tests go red. "
        "Which edit broke it? Today, you only see the final error."
    ),
    "how": (
        "AgentBisect is git bisect for an agent's actions. It saves a snapshot after every step, "
        "then binary searches them with your own tests. The verdict comes from running tests, "
        "never from a language model."
    ),
    "terminal": (
        "Here is a real run. DeepSeek V four point one Flash, working in Alan Code, was asked to add "
        "a feature. At step nine, we planted a break in a file it never touched. AgentBisect names "
        "the step."
    ),
    "humanize": (
        "On a real open source repo, python humanize, we planted a break the same way. Press play, "
        "and watch it search. Each probe restores one snapshot and runs the tests. Four probes, "
        "step eight, ninety nine point seven percent confidence. Click the step, and you see "
        "exactly what changed."
    ),
    "fixed": (
        "In another run, the agent noticed the break at step thirteen, and fixed it. AgentBisect "
        "shows when it broke, when it was seen, and what the wait cost: four steps, a hundred "
        "twelve thousand tokens."
    ),
    "study": (
        "We report this honestly. Four practice runs, and the agent left no test broken. The "
        "bisect found the planted step two out of two times. Nothing more is claimed."
    ),
    "app": (
        "And it works on your own repository. Run agentbisect serve, paste a repo and a prompt, and "
        "set up the run. It copies the project, and hands you the folder and the prompt. Open the "
        "folder in Alan Code, let the agent work, and press find the step."
    ),
    "outro": (
        "AgentBisect. Open source, built on Alan AI. Find it at github dot com slash akshatsi "
        "slash Breaking Point."
    ),
}  # fmt: skip

# ------------------------------------------------------------------------------- slide and pages

CSS = """
:root{--bg:#131416;--panel:#1c1d20;--ink:#f4f4f5;--muted:#a1a4aa;--line:#2d2f33;
--red:#ff5c63;--red-bg:#3b1a1d;--green:#3fd27f;--green-bg:#123122}
*{box-sizing:border-box}html,body{margin:0;height:100%}
body{background:var(--bg);color:var(--ink);font:34px/1.4 system-ui,-apple-system,"Segoe UI",sans-serif;
display:flex;align-items:center;justify-content:center;-webkit-font-smoothing:antialiased;overflow:hidden}
.wrap{width:1400px}h1{font-size:92px;line-height:1.08;margin:0 0 26px;letter-spacing:-.02em}
h2{font-size:58px;margin:0 0 22px;letter-spacing:-.01em}p{margin:0 0 16px;color:var(--muted)}
.big{font-size:44px;color:var(--ink)}.red{color:var(--red)}.mono{font-family:ui-monospace,Menlo,monospace}
.tag{display:inline-block;border:1px solid var(--line);border-radius:999px;padding:8px 20px;font-size:25px;
color:var(--muted);margin-right:12px}
.fade{opacity:0;transform:translateY(14px);animation:in .7s ease forwards}
@keyframes in{to{opacity:1;transform:none}}
.strip{display:flex;gap:14px;margin:40px 0 12px}.cell{width:92px;height:92px;border-radius:16px;display:grid;
place-items:center;font-size:30px;font-weight:700;border:3px solid var(--line);background:var(--panel);
transition:all .35s}.cell.good{border-color:var(--green);color:var(--green);background:var(--green-bg)}
.cell.bad{border-color:var(--red);color:var(--red);background:var(--red-bg)}
.cell.probe{border-color:#e3a948;color:#e3a948;transform:translateY(-8px)}
.cell.cul{background:var(--red);color:#fff;border-color:var(--red)}
.steps{display:flex;gap:22px;margin-top:34px}.card{flex:1;background:var(--panel);border:1px solid var(--line);
border-radius:20px;padding:30px 32px}.card b{display:block;font-size:38px;margin-bottom:10px}
.card span{font-size:28px;color:var(--muted)}
.num{display:inline-grid;place-items:center;width:44px;height:44px;border-radius:50%;background:var(--ink);
color:var(--bg);font-weight:800;font-size:26px;margin-right:14px}
.term{width:1680px;background:#0f1012;border:1px solid var(--line);border-radius:18px;overflow:hidden}
.bar{background:#1c1d20;padding:16px 22px;display:flex;gap:11px}.dot{width:17px;height:17px;border-radius:50%}
.term pre{margin:0;padding:30px 36px;font:27px/1.45 ui-monospace,Menlo,Consolas,monospace;color:#e8e8ea;
white-space:pre;min-height:420px}.pr{color:var(--green)}
.cursor{display:inline-block;width:.6em;height:1.1em;background:#e8e8ea;vertical-align:-.15em;
animation:blink 1s steps(1) infinite}@keyframes blink{50%{opacity:0}}
"""


def page(body: str, script: str = "") -> str:
    return f"<!doctype html><meta charset=utf-8><style>{CSS}</style><body>{body}<script>{script}</script></body>"


def slide_hook() -> str:
    return page(
        '<div class="wrap"><p class="tag fade">AI coding agents</p>'
        '<h1><span class="fade" style="animation-delay:.3s">The agent made 30 edits.</span><br>'
        '<span class="red fade" style="animation-delay:1.9s">The tests went red.</span></h1>'
        '<p class="big fade" style="animation-delay:3.4s">Which edit broke it?</p>'
        '<p class="fade" style="animation-delay:5s">Today you only see the final error.</p></div>'
    )


def slide_how() -> str:
    cells = "".join(f'<div class="cell" id="c{i}">{i}</div>' for i in range(9))
    script = """
    var seq=[[1200,4,'probe'],[2000,4,'good'],[2300,6,'probe'],[3100,6,'bad'],[3500,5,'probe'],[4300,5,'bad'],
             [4700,0,'good'],[4750,1,'good'],[4800,2,'good'],[4850,3,'good'],[4900,7,'bad'],[4950,8,'bad'],[5600,5,'cul']];
    seq.forEach(function(s){setTimeout(function(){var e=document.getElementById('c'+s[1]);
      e.className='cell '+s[2];},s[0])});
    """
    return page(
        '<div class="wrap"><h2 class="fade">git bisect, for an agent\'s actions</h2>'
        f'<div class="strip">{cells}</div>'
        '<p class="fade" style="animation-delay:5.8s">Step 5 is the first step where the tests fail.</p>'
        '<div class="steps"><div class="card fade" style="animation-delay:6.4s"><b><span class="num">1</span>Record</b>'
        "<span>Snapshot your repo after every step the agent takes.</span></div>"
        '<div class="card fade" style="animation-delay:7.8s"><b><span class="num">2</span>Bisect</b>'
        "<span>Binary-search the snapshots with your own failing test.</span></div>"
        '<div class="card fade" style="animation-delay:9.2s"><b><span class="num">3</span>Measure</b>'
        "<span>Count the steps and tokens before the agent noticed.</span></div></div></div>",
        script,
    )


def slide_outro() -> str:
    return page(
        '<div class="wrap"><h1 class="fade">AgentBisect</h1>'
        '<p class="big fade" style="animation-delay:.6s">Find the step where your agent broke the code.</p>'
        '<p class="fade" style="animation-delay:1.4s"><span class="tag">open source</span>'
        '<span class="tag">built on Alan AI</span><span class="tag">DeepSeek V4.1 Flash in Alan Code</span></p>'
        '<p class="mono big fade" style="animation-delay:2.4s;margin-top:40px">github.com/akshatsi/Breaking-Point</p>'
        '<p class="fade" style="animation-delay:3.4s;margin-top:34px">'
        "Honest limits: 4 task runs, 1 model, Python and pytest only.</p></div>"
    )


def terminal_page(command: str, output: str) -> str:
    body = (
        '<div class="term"><div class="bar"><i class="dot" style="background:#ff5f57"></i>'
        '<i class="dot" style="background:#febc2e"></i><i class="dot" style="background:#28c840"></i></div>'
        '<pre><span class="pr">$ </span><span id="cmd"></span><span class="cursor" id="cur"></span>'
        '<span id="out"></span></pre></div>'
    )
    script = (
        "var cmd=%s,out=%s.split('\\n'),i=0,c=document.getElementById('cmd');"
        "function type(){if(i<cmd.length){c.textContent+=cmd[i++];setTimeout(type,48)}else{"
        "document.getElementById('cur').style.display='none';var o=document.getElementById('out'),k=0;"
        "o.textContent='\\n';(function line(){if(k<out.length){o.textContent+=out[k++]+'\\n';setTimeout(line,260)}})()}}"
        "setTimeout(type,900);"
    ) % (js(command), js(output))
    return page(body, script)


def js(text: str) -> str:
    import json

    return json.dumps(text)


# ---------------------------------------------------------------------------------- browser driver

CURSOR_JS = """
(function(){
 window.__moveCur=function(x,y){var c=document.getElementById('__cur');if(c)c.style.transform='translate('+x+'px,'+y+'px)'};
 window.__ripple=function(x,y){var r=document.getElementById('__rip');if(!r)return;r.style.left=x+'px';r.style.top=y+'px';
   r.animate([{opacity:.95,transform:'scale(.3)'},{opacity:0,transform:'scale(1.7)'}],{duration:480})};
 function init(){
  if(document.getElementById('__cur')||!document.body) return;
  document.body.style.zoom=%s;
  var c=document.createElement('div');c.id='__cur';
  c.innerHTML='<svg width="34" height="34" viewBox="0 0 24 24"><path d="M4 2l0 17 4.6-4 3 7 2.6-1.1-3-6.9 6.2-.2z" fill="#fff" stroke="#111" stroke-width="1.4" stroke-linejoin="round"/></svg>';
  c.style.cssText='position:fixed;left:0;top:0;z-index:2147483647;pointer-events:none;transform:translate(-200px,-200px);filter:drop-shadow(0 3px 5px rgba(0,0,0,.6))';
  var r=document.createElement('div');r.id='__rip';
  r.style.cssText='position:fixed;z-index:2147483646;width:60px;height:60px;margin:-30px 0 0 -30px;border-radius:50%%;border:4px solid #fff;opacity:0;pointer-events:none';
  document.documentElement.appendChild(r);document.documentElement.appendChild(c);
  if(window.__lastCur) window.__moveCur(window.__lastCur[0],window.__lastCur[1]);
 }
 if(document.readyState!=='loading') init(); else document.addEventListener('DOMContentLoaded',init);
})();
""" % ZOOM


class Ctl:
    """Drives one page like a person: gliding cursor, real clicks, typing, smooth scrolling."""

    def __init__(self, page) -> None:
        self.page = page
        self.pos = (W * 0.55, H * 0.6)
        self.t0 = time.monotonic()

    def now(self) -> float:
        return time.monotonic() - self.t0

    def hold_until(self, t: float) -> None:
        time.sleep(max(0.0, t - self.now()))

    def sleep(self, s: float) -> None:
        time.sleep(s)

    def _show(self, x: float, y: float) -> None:
        self.page.evaluate("([x,y])=>{window.__lastCur=[x,y];window.__moveCur&&window.__moveCur(x,y)}", [x, y])

    def glide(self, x: float, y: float, dur: float = 0.8) -> None:
        x0, y0 = self.pos
        n = max(int(dur * 50), 2)
        for i in range(1, n + 1):
            t = i / n
            e = t * t * (3 - 2 * t)
            px, py = x0 + (x - x0) * e, y0 + (y - y0) * e
            self.page.mouse.move(px, py)
            self._show(px, py)
            time.sleep(dur / n)
        self.pos = (x, y)

    def center(self, loc) -> tuple[float, float]:
        b = loc.bounding_box()
        return b["x"] + b["width"] / 2, b["y"] + b["height"] / 2

    def scroll_to(self, y: float, dur: float = 1.1) -> None:
        y0 = self.page.evaluate("window.scrollY")
        n = max(int(dur * 40), 2)
        for i in range(1, n + 1):
            t = i / n
            e = t * t * (3 - 2 * t)
            self.page.evaluate("y=>window.scrollTo(0,y)", y0 + (y - y0) * e)
            time.sleep(dur / n)

    def bring(self, loc, target_y: float = 0.5, dur: float = 1.0) -> None:
        """Scroll so the element sits at target_y (fraction of the screen height)."""
        b = loc.bounding_box()
        cur = self.page.evaluate("window.scrollY")
        want = cur + b["y"] + b["height"] / 2 - H * target_y
        if abs(want - cur) > 40:
            self.scroll_to(max(want, 0), dur)

    def hover(self, loc, dur: float = 0.8, target_y: float | None = 0.5) -> None:
        if target_y is not None:
            self.bring(loc, target_y)
        x, y = self.center(loc)
        self.glide(x, y, dur)

    def click(self, loc, dur: float = 0.8, target_y: float | None = 0.5) -> None:
        self.hover(loc, dur, target_y)
        x, y = self.center(loc)
        self.page.evaluate("([x,y])=>window.__ripple&&window.__ripple(x,y)", [x, y])
        time.sleep(0.12)
        loc.click()

    def type(self, loc, text: str, delay: int = 55) -> None:
        self.click(loc, 0.7)
        self.page.keyboard.type(text, delay=delay)


def new_scene(browser, video_dir: Path, url: str, with_cursor: bool = True):
    ctx = browser.new_context(
        viewport={"width": W, "height": H}, record_video_dir=str(video_dir), record_video_size={"width": W, "height": H}
    )  # fmt: skip
    ctx.add_init_script(CURSOR_JS)
    t_ctx = time.monotonic()
    page = ctx.new_page()
    page.goto(url)
    page.wait_for_timeout(700)
    ready = time.monotonic() - t_ctx
    c = Ctl(page)
    if with_cursor:
        c._show(*c.pos)
    return ctx, page, c, ready


# --------------------------------------------------------------------------------------- scenes


def scene_humanize(page, c: Ctl, dur: float) -> None:
    c.sleep(0.6)
    title = page.get_by_text("Step 8", exact=False).first
    x, y = c.center(title)
    c.glide(x + 250, y + 20, 1.0)  # read the verdict
    c.sleep(1.4)
    play = page.get_by_role("button", name="Play the replay")
    c.click(play, 1.0, target_y=0.55)
    page.get_by_text("Found step 8").first.wait_for(timeout=30000)
    c.sleep(1.2)
    cell = page.get_by_role("button", name=re.compile(r"^Step 8:"))
    c.click(cell, 0.9, target_y=0.35)
    c.sleep(0.8)
    diff = page.get_by_text("Changes in this step").first
    c.bring(diff, 0.25, 1.4)
    c.glide(900, 640, 0.9)
    c.hold_until(dur)


def scene_fixed(page, c: Ctl, dur: float) -> None:
    c.sleep(0.5)
    lag = page.get_by_text("Detection lag", exact=False).first
    c.glide(*c.center(lag), 1.0)
    c.sleep(1.6)
    c.click(page.get_by_role("button", name=re.compile(r"^Inspect step 9")), 1.0, target_y=0.5)
    c.sleep(1.2)
    c.bring(page.get_by_text("Changes in this step").first, 0.22, 1.3)
    c.glide(960, 620, 0.9)
    c.hold_until(dur)


def scene_study(page, c: Ctl, dur: float) -> None:
    c.sleep(0.5)
    c.glide(700, 520, 0.9)
    c.scroll_to(520, 1.2)
    c.sleep(0.7)
    c.bring(page.get_by_text("Headline numbers").first, 0.12, 1.6)
    c.glide(560, 640, 0.9)
    c.sleep(1.0)
    c.glide(1160, 700, 1.0)
    c.hold_until(dur)


def scene_app(page, c: Ctl, dur: float) -> None:
    c.sleep(0.5)
    repo = page.get_by_label("Repository")
    c.type(repo, "/tmp/shop-demo", 30)
    prompt = page.get_by_label("Task prompt")
    c.type(prompt, "Add a Catalog.count() method, with a test.", 24)
    box = page.get_by_label(re.compile("Skip installing"))
    c.click(box, 0.7, target_y=0.5)
    go = page.get_by_role("button", name="Set up the run")
    c.click(go, 0.7, target_y=0.55)
    page.get_by_text("Next step").first.wait_for(timeout=60000)
    c.sleep(0.8)
    c.bring(page.get_by_text("Next step").first, 0.18, 1.2)
    fin = page.get_by_role("button", name=re.compile("The agent has finished"))
    c.hover(fin, 1.0, target_y=0.6)
    c.sleep(0.8)
    c.bring(page.get_by_text(re.compile(r"^3\. Runs")).first, 0.2, 1.3)
    c.glide(1200, 760, 0.9)
    c.hold_until(dur)


# ------------------------------------------------------------------------------------- plumbing


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=True, capture_output=True, text=True, **kw)


def duration(path: Path) -> float:
    out = run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)])
    return float(out.stdout.strip())


def ensure_models() -> tuple[Path, Path]:
    CACHE.mkdir(parents=True, exist_ok=True)
    files = []
    for name in ("kokoro-v1.0.onnx", "voices-v1.0.bin"):
        p = CACHE / name
        if not p.exists():
            print(f"downloading {name} ...")
            urllib.request.urlretrieve(MODEL_URL + name, p)
        files.append(p)
    return files[0], files[1]


def synthesize(texts: dict[str, str], voice: str, speed: float, work: Path) -> dict[str, Path]:
    import numpy as np
    import soundfile as sf
    from kokoro_onnx import Kokoro

    model, voices = ensure_models()
    k = Kokoro(str(model), str(voices))
    lang = "en-gb" if voice.startswith("b") else "en-us"
    out = {}
    for name, text in texts.items():
        samples, sr = k.create(text, voice=voice, speed=speed, lang=lang)
        samples = np.concatenate([samples, np.zeros(int(sr * 0.5), dtype=samples.dtype)])
        wav = work / f"{name}.wav"
        sf.write(str(wav), samples, sr)
        out[name] = wav
        print(f"voice {name}: {len(samples) / sr:.1f}s")
    return out


def to_clip(raw: Path | None, still: Path | None, offset: float, total: float, wav: Path, out: Path) -> None:
    """One scene: the recording (or a still), cut to the narration's length, with the voice mixed in."""
    if raw is not None:
        src = ["-ss", f"{offset:.3f}", "-i", str(raw)]
    else:
        src = ["-loop", "1", "-i", str(still)]
    run([
        "ffmpeg", "-y", *src, "-i", str(wav), "-t", f"{total:.3f}",
        "-vf", f"fps=30,scale={W}:{H}:flags=lanczos,format=yuv420p,tpad=stop_mode=clone:stop_duration=8",
        "-af", "apad,afade=t=out:st=%.3f:d=0.25" % (total - 0.3), "-c:v", "libx264", "-preset", "medium",
        "-crf", "26", "-c:a", "aac", "-b:a", "192k", "-shortest", str(out),
    ])  # fmt: skip


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--voice", default="am_michael")
    ap.add_argument("--speed", type=float, default=1.18)
    ap.add_argument("--only", default="", help="comma-separated scene names, for testing")
    ap.add_argument("--out", default=str(ROOT / "docs/demo/agentbisect-demo.mp4"))
    args = ap.parse_args()
    from playwright.sync_api import sync_playwright

    work = Path(tempfile.mkdtemp(prefix="agentbisect-video-"))
    print("work dir:", work)
    names = [n for n in SCENES if not args.only or n in args.only.split(",")]
    wavs = synthesize({n: SCENES[n] for n in names}, args.voice, args.speed, work)

    # A private copy of the demo runs, plus an unanalyzed run and a small project to set up live.
    runs = work / "runs"
    runs.mkdir()
    for d in ("caught-and-fixed", "humanize-planted-break", "humanize-ordinal-fix"):
        shutil.copytree(ROOT / "runs/demo" / d, runs / d)
    shutil.copytree(ROOT / "runs/demo/study", runs / "study")
    proj = Path("/tmp/shop-demo")
    shutil.rmtree(proj, ignore_errors=True)
    shutil.copytree(ROOT / "tasks/fixture_repo", proj, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))

    env = {**os.environ, "NO_COLOR": "1", "COLUMNS": "92", "TERM": "dumb"}
    env.pop("VIRTUAL_ENV", None)
    real = subprocess.run(
        ["uv", "run", "agentbisect", "run", "--trace", str(runs / "caught-and-fixed")],
        cwd=ROOT, env=env, capture_output=True, text=True,
    )  # fmt: skip
    box = [ln.rstrip() for ln in real.stdout.splitlines() if ln.strip() and ln[0] in "╭│╰"]
    term_out = "\n".join(box[:6])

    pages = work / "pages"
    pages.mkdir()
    for name, text in (("hook", slide_hook()), ("how", slide_how()), ("outro", slide_outro()),
                       ("terminal", terminal_page("uv run agentbisect run --trace runs/demo/caught-and-fixed", term_out))):  # fmt: skip
        (pages / f"{name}.html").write_text(text, encoding="utf-8")

    server = subprocess.Popen(
        ["uv", "run", "agentbisect", "serve", "--no-open", "--port", str(PORT), "--runs", str(runs)],
        cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )  # fmt: skip
    token = ""
    t0 = time.time()
    while time.time() - t0 < 40 and not token:
        m = re.search(r"token=([\w-]+)", server.stdout.readline())
        token = m.group(1) if m else ""

    urls = {
        "hook": (pages / "hook.html").as_uri(),
        "how": (pages / "how.html").as_uri(),
        "terminal": (pages / "terminal.html").as_uri(),
        "humanize": (runs / "humanize-planted-break/report.html").as_uri(),
        "fixed": (runs / "caught-and-fixed/report.html").as_uri(),
        "study": (runs / "study/study.html").as_uri(),
        "app": f"http://127.0.0.1:{PORT}/#token={token}",
        "outro": (pages / "outro.html").as_uri(),
    }
    drivers = {"humanize": scene_humanize, "fixed": scene_fixed, "study": scene_study, "app": scene_app}
    clips: list[Path] = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(channel="chrome", headless=True)
            for n in names:
                total = duration(wavs[n]) + 0.4
                vdir = work / f"vid_{n}"
                ctx, pg, c, ready = new_scene(browser, vdir, urls[n], n in drivers)
                c.t0 = time.monotonic()
                if n in drivers:
                    drivers[n](pg, c, total)
                else:
                    c.hold_until(total)  # slides and the terminal animate themselves
                total = max(total, c.now() + 0.7)  # never cut the actions off; the voice just ends earlier
                raw = Path(pg.video.path())
                ctx.close()
                clip = work / f"clip_{n}.mp4"
                to_clip(raw, None, max(ready - 0.05, 0), total, wavs[n], clip)
                clips.append(clip)
                print(f"scene {n}: {total:.1f}s")
            browser.close()
    finally:
        server.terminate()

    (work / "all.txt").write_text("\n".join(f"file '{c}'" for c in clips))
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    joined = work / "joined.mp4"
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(work / "all.txt"), "-c", "copy", str(joined)])
    # One loudness pass over the whole video, so the voice is at a normal listening level (-16 LUFS).
    run(["ffmpeg", "-y", "-i", str(joined), "-c:v", "copy", "-af", "loudnorm=I=-16:LRA=11:TP=-1.5",
         "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(out)])  # fmt: skip
    print(f"wrote {out} ({out.stat().st_size / 1e6:.1f} MB, {duration(out):.1f}s)")
    shutil.rmtree(proj, ignore_errors=True)
    if not args.keep:
        shutil.rmtree(work, ignore_errors=True)
    else:
        print("kept", work)
    return 0


if __name__ == "__main__":
    sys.exit(main())
