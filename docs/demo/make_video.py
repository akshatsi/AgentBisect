#!/usr/bin/env python3
"""Build the AgentBisect demo video from the real demo runs.

Everything on screen comes from this repository: the HTML pages are the ones `agentbisect report`,
`study` and `serve` write, and the terminal text is the real output of `agentbisect run`.
The voiceover is macOS `say`. Needs: macOS, Google Chrome, ffmpeg, and `uv sync` done.

    uv run python docs/demo/make_video.py            # writes docs/demo/agentbisect-demo.mp4
    uv run python docs/demo/make_video.py --keep     # keep the work dir (frames, audio)
"""

from __future__ import annotations

import argparse
import html
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
VOICE, RATE = "Samantha", "172"
W, H, SCALE = 1280, 720, 1.5  # a 1280x720 viewport at 1.5x is a 1920x1080 frame

CSS = """
:root{--bg:#131416;--panel:#1c1d20;--panel2:#232427;--ink:#f4f4f5;--muted:#a1a4aa;--line:#2d2f33;
--red:#ff5c63;--red-bg:#3b1a1d;--green:#3fd27f;--green-bg:#123122;--amber:#e3a948;--blue:#7db4ff}
*{box-sizing:border-box}html,body{margin:0;height:100%}
body{background:var(--bg);color:var(--ink);font:28px/1.4 system-ui,-apple-system,"Segoe UI",sans-serif;
display:flex;align-items:center;justify-content:center;-webkit-font-smoothing:antialiased}
.wrap{width:1120px}h1{font-size:68px;line-height:1.1;margin:0 0 22px;letter-spacing:-.02em}
h2{font-size:46px;margin:0 0 18px;letter-spacing:-.01em}p{margin:0 0 14px;color:var(--muted)}
.big{font-size:34px;color:var(--ink)}.red{color:var(--red)}.green{color:var(--green)}
.tag{display:inline-block;border:1px solid var(--line);border-radius:999px;padding:6px 16px;font-size:20px;
color:var(--muted);margin-right:10px}
.mono{font-family:ui-monospace,Menlo,Consolas,monospace}
.steps{display:flex;gap:18px;margin-top:30px}.card{flex:1;background:var(--panel);border:1px solid var(--line);
border-radius:16px;padding:24px 26px}.card b{display:block;font-size:30px;margin-bottom:8px}
.card span{font-size:23px;color:var(--muted)}.num{display:inline-grid;place-items:center;width:34px;height:34px;
border-radius:50%;background:var(--ink);color:var(--bg);font-weight:800;font-size:20px;margin-right:10px}
.strip{display:flex;gap:10px;margin:34px 0 8px}.cell{width:62px;height:62px;border-radius:12px;display:grid;
place-items:center;font-size:20px;font-weight:700;border:2px solid var(--line);background:var(--panel)}
.cell.good{border-color:var(--green);color:var(--green)}.cell.bad{border-color:var(--red);color:var(--red);
background:var(--red-bg)}.cell.cul{background:var(--red);color:#fff;border-color:var(--red)}
.term{width:1240px;background:#0f1012;border:1px solid var(--line);border-radius:14px;overflow:hidden}
.bar{background:#1c1d20;padding:12px 18px;display:flex;gap:9px}.dot{width:14px;height:14px;border-radius:50%}
.term pre{margin:0;padding:22px 26px;font:22px/1.42 ui-monospace,Menlo,Consolas,monospace;color:#e8e8ea;
white-space:pre;overflow:hidden}.pr{color:var(--green)}.cmd{color:#fff}
"""


def page(body: str) -> str:
    return f"<!doctype html><meta charset=utf-8><style>{CSS}</style><body>{body}</body>"


def slide_hook() -> str:
    return page(
        '<div class="wrap"><p class="tag">AI coding agents</p><h1>The agent made 30 edits.<br>'
        '<span class="red">The tests went red.</span></h1><p class="big">Which edit broke it?</p>'
        "<p>Today you only see the final error.</p></div>"
    )


def slide_how() -> str:
    cells = "".join(
        f'<div class="cell {c}">{i}</div>'
        for i, c in enumerate(["good", "good", "good", "good", "bad", "bad", "cul", "bad", "bad"])
    )
    return page(
        '<div class="wrap"><h2>git bisect, for an agent\'s actions</h2><div class="strip">'
        + cells
        + "</div>"
        '<p>Step 6 is the first step where the tests fail.</p><div class="steps">'
        '<div class="card"><b><span class="num">1</span>Record</b><span>Snapshot your repo after every step the agent takes.</span></div>'
        '<div class="card"><b><span class="num">2</span>Bisect</b><span>Binary-search the snapshots with your own failing test.</span></div>'
        '<div class="card"><b><span class="num">3</span>Measure</b><span>Count the steps and tokens before the agent noticed.</span></div>'
        '</div><p style="margin-top:26px">The verdict comes from running tests. No language model decides pass or fail.</p></div>'
    )


def slide_outro() -> str:
    return page(
        '<div class="wrap"><h1>AgentBisect</h1><p class="big">Find the step where your agent broke the code.</p>'
        '<p><span class="tag">open source</span><span class="tag">built on Alan AI</span>'
        '<span class="tag">DeepSeek V4.1 Flash in Alan Code</span></p>'
        '<p class="mono big" style="margin-top:34px">github.com/akshatsi/Breaking-Point</p>'
        '<p style="margin-top:30px">Honest limits: 4 task runs, 1 model, Python and pytest only.</p></div>'
    )


def terminal(command: str, output: str | None) -> str:
    out = "" if output is None else "\n" + html.escape(output)
    return page(
        '<div class="term"><div class="bar"><i class="dot" style="background:#ff5f57"></i>'
        '<i class="dot" style="background:#febc2e"></i><i class="dot" style="background:#28c840"></i></div>'
        f'<pre><span class="pr">$ </span><span class="cmd">{html.escape(command)}</span>{out}</pre></div>'
    )


def caption(text: str) -> str:
    return (
        "<!doctype html><meta charset=utf-8><style>html,body{margin:0;background:transparent;height:100%}"
        "div{position:fixed;left:0;right:0;bottom:0;padding:18px 40px;background:rgba(10,10,11,.92);"
        "color:#f4f4f5;font:27px/1.35 system-ui,-apple-system,sans-serif;text-align:center}"
        f"</style><body><div>{html.escape(text)}</div></body>"
    )


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=True, capture_output=True, text=True, **kw)


def shot(
    url: str, out: Path, *, w: int = W, h: int = H, transparent: bool = False, wait_ms: int = 3500
) -> None:
    args = [
        CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars", f"--window-size={w},{h}",
        f"--force-device-scale-factor={SCALE}", f"--virtual-time-budget={wait_ms}", f"--screenshot={out}", url,
    ]  # fmt: skip
    if transparent:
        args.insert(2, "--default-background-color=00000000")
    subprocess.run(args, check=True, capture_output=True, timeout=120)
    if not out.exists():
        raise RuntimeError(f"no screenshot for {url}")


def inject(src: Path, dst: Path, js: str) -> Path:
    text = src.read_text(encoding="utf-8")
    dst.write_text(text.replace("</body>", f"<script>{js}</script></body>"), encoding="utf-8")
    return dst


def click_steps(n: int) -> str:
    return (
        "setTimeout(function(){var b=[].slice.call(document.querySelectorAll('button')).filter(function(x){"
        "return x.textContent.trim()==='Step'})[0];var n=" + str(n) + ";(function t(){"
        "if(b&&n-->0){b.click();setTimeout(t,200)}})()},400);"
    )


def duration(path: Path) -> float:
    out = run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)]
    )
    return float(out.stdout.strip())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--out", default=str(ROOT / "docs/demo/agentbisect-demo.mp4"))
    args = ap.parse_args()
    for tool in (CHROME, shutil.which("ffmpeg"), shutil.which("say")):
        if not tool or not Path(tool).exists():
            sys.exit(f"missing dependency: {tool}")
    work = Path(tempfile.mkdtemp(prefix="agentbisect-video-"))
    print("work dir:", work)
    demo = work / "runs"
    demo.mkdir()
    for d in ("caught-and-fixed", "humanize-planted-break", "humanize-ordinal-fix"):
        shutil.copytree(ROOT / "runs/demo" / d, demo / d)
    shutil.copytree(ROOT / "runs/demo/study", demo / "study")

    # The real terminal output of `agentbisect run` (no colors, fixed width).
    env = {**os.environ, "NO_COLOR": "1", "COLUMNS": "92", "TERM": "dumb"}
    env.pop("VIRTUAL_ENV", None)
    real = subprocess.run(
        ["uv", "run", "agentbisect", "run", "--trace", str(demo / "caught-and-fixed")],
        cwd=ROOT, env=env, capture_output=True, text=True,
    )  # fmt: skip
    lines = [ln.rstrip() for ln in real.stdout.splitlines() if ln.strip()]
    keep = [ln for ln in lines if ln[0] in "╭│╰"]  # the verdict box only
    term_out = "\n".join(keep[:6])
    print(term_out)

    # --- pages -------------------------------------------------------------------------------
    f = work / "f"
    f.mkdir()
    hz = demo / "humanize-planted-break/report.html"
    cf = demo / "caught-and-fixed/report.html"
    study = demo / "study/study.html"

    def save(name: str, html_text: str) -> Path:
        p = f / f"{name}.html"
        p.write_text(html_text, encoding="utf-8")
        return p

    server = subprocess.Popen(
        ["uv", "run", "agentbisect", "serve", "--no-open", "--port", "8795", "--runs", str(demo)],
        cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )  # fmt: skip
    token = ""
    t0 = time.time()
    while time.time() - t0 < 30 and not token:
        line = server.stdout.readline()
        m = re.search(r"token=([\w-]+)", line)
        if m:
            token = m.group(1)

    frames: dict[str, Path] = {}

    def grab(name: str, url: str, **kw) -> None:
        out = f / f"{name}.png"
        shot(url, out, **kw)
        frames[name] = out

    def grab_crop(name: str, url: str, y: int, tall: int = 2800) -> None:
        """Capture a tall page, then crop one 16:9 view starting y CSS pixels down.

        Scrolling inside headless Chrome renders blank, so we crop instead.
        """
        full = f / f"{name}_full.png"
        shot(url, full, h=tall)
        out = f / f"{name}.png"
        run(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(full),
                "-vf",
                f"crop={int(W * SCALE)}:{int(H * SCALE)}:0:{int(y * SCALE)}",
                str(out),
            ]
        )
        frames[name] = out

    try:
        grab("hook", save("hook", slide_hook()).as_uri())
        grab("how", save("how", slide_how()).as_uri())
        grab(
            "term0",
            save(
                "term0", terminal("uv run agentbisect run --trace runs/demo/caught-and-fixed", None)
            ).as_uri(),
        )
        grab(
            "term1",
            save(
                "term1",
                terminal("uv run agentbisect run --trace runs/demo/caught-and-fixed", term_out),
            ).as_uri(),
        )
        for i in range(5):
            grab_crop(f"replay{i}", inject(hz, f / f"replay{i}.html", click_steps(i)).as_uri(), 830)
        grab_crop("diff", cf.as_uri(), 1380)
        grab("study0", study.as_uri())
        grab_crop("study1", study.as_uri(), 2250, tall=3100)
        grab("app", f"http://127.0.0.1:8795/#token={token}", h=1500)
        grab("outro", save("outro", slide_outro()).as_uri())
    finally:
        server.terminate()

    # The tall app screenshot is cropped into two 16:9 views.
    app = frames["app"]
    for name, y in (("app0", 0), ("app1", int(780 * SCALE))):
        out = f / f"{name}.png"
        run(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(app),
                "-vf",
                f"crop={int(W * SCALE)}:{int(H * SCALE)}:0:{y}",
                str(out),
            ]
        )
        frames[name] = out

    # --- storyboard: (narration, [(frame, caption)]) ------------------------------------------
    scenes = [
        ("An AI coding agent works for twenty minutes, makes thirty edits, and your tests go red. "
         "Which edit broke it? Today, you only see the final error.",
         [("hook", None)]),
        ("AgentBisect works like git bisect, but over the agent's own actions. It saves a snapshot of your "
         "repo after every step, then binary searches those snapshots with your own tests. The verdict comes "
         "from running tests, never from a language model.",
         [("how", None)]),
        ("Here is a real run. DeepSeek V4 point 1 Flash, working inside Alan Code, was asked to add a feature. "
         "At step nine, we planted a break in an unrelated file. AgentBisect names it.",
         [("term0", "Real run: DeepSeek V4.1 Flash in Alan Code"), ("term1", "Step 9 broke the test. The agent saw it at step 13 and fixed it.")]),
        ("On a real open source repo, python humanize, we planted a break the same way, and the tool finds the exact step. Watch the search. "
         "Each probe restores one snapshot and runs the tests. Four probes, step eight, ninety nine point seven "
         "percent confidence.",
         [("replay0", "Planted break on python-humanize: the bisect replay"), ("replay1", "Probe 1"), ("replay2", "Probe 2"), ("replay3", "Probe 3"), ("replay4", "Found step 8 in 4 probes")]),
        ("Click any step to see what the agent said, and the exact diff it made.",
         [("diff", "Every step: reasoning, tool call, and a diff")]),
        ("We report this honestly. In four practice runs, the agent left no test broken. Zero of four. "
         "The bisect found the planted step two out of two times. We claim nothing beyond that.",
         [("study0", "Audited results, one model, a small study"), ("study1", "0 of 4 runs broken; 2 of 2 planted breaks found")]),
        ("It works on your own repository too. Run agentbisect serve, paste a repo and a prompt, open the folder "
         "in Alan Code, and press find the step.",
         [("app0", "uv run agentbisect serve"), ("app1", "Your repo, your prompt, your verdict")]),
        ("AgentBisect. Open source, built on Alan AI. Find it at github dot com slash akshatsi slash Breaking Point.",
         [("outro", None)]),
    ]  # fmt: skip

    parts: list[Path] = []
    for i, (text, shots) in enumerate(scenes):
        wav = work / f"s{i}.wav"
        run(["say", "-v", VOICE, "-r", RATE, "-o", str(work / f"s{i}.aiff"), text])
        run(["ffmpeg", "-y", "-i", str(work / f"s{i}.aiff"), "-ar", "44100", "-ac", "1", str(wav)])
        total = duration(wav) + 0.7
        per = total / len(shots)
        clips = []
        for j, (name, cap) in enumerate(shots):
            png = frames[name]
            if cap:
                cap_png = f / f"cap{i}_{j}.png"
                shot(
                    save(f"cap{i}_{j}", caption(cap)).as_uri(),
                    cap_png,
                    transparent=True,
                    wait_ms=500,
                )
                comp = f / f"comp{i}_{j}.png"
                run(
                    [
                        "ffmpeg",
                        "-y",
                        "-i",
                        str(png),
                        "-i",
                        str(cap_png),
                        "-filter_complex",
                        "overlay=0:0",
                        str(comp),
                    ]
                )
                png = comp
            clip = work / f"clip{i}_{j}.mp4"
            run([
                "ffmpeg", "-y", "-loop", "1", "-framerate", "30", "-t", f"{per:.3f}", "-i", str(png),
                "-vf", "scale=1920:1080:flags=lanczos,format=yuv420p", "-r", "30", "-c:v", "libx264",
                "-preset", "medium", "-crf", "20", "-an", str(clip),
            ])  # fmt: skip
            clips.append(clip)
        lst = work / f"s{i}.txt"
        lst.write_text("\n".join(f"file '{c}'" for c in clips))
        silent = work / f"silent{i}.mp4"
        run(
            [
                "ffmpeg",
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(lst),
                "-c",
                "copy",
                str(silent),
            ]
        )
        part = work / f"part{i}.mp4"
        run([
            "ffmpeg", "-y", "-i", str(silent), "-i", str(wav), "-c:v", "copy", "-c:a", "aac", "-b:a", "160k",
            "-af", "apad", "-t", f"{total:.3f}", str(part),
        ])  # fmt: skip
        parts.append(part)
        print(f"scene {i}: {total:.1f}s")

    (work / "all.txt").write_text("\n".join(f"file '{p}'" for p in parts))
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(work / "all.txt"),
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(out),
        ]
    )
    print(f"wrote {out} ({out.stat().st_size / 1e6:.1f} MB, {duration(out):.1f}s)")
    if not args.keep:
        shutil.rmtree(work, ignore_errors=True)
    else:
        print("kept", work)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
