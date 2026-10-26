"""Command-line entry point. Four commands: record, run, report, study."""

from __future__ import annotations

import shlex
from pathlib import Path

import typer
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

from agentbisect.models import (
    BisectStatus,
    CulpritReport,
    RunReport,
    StudySummary,
    TaskSpec,
    Trace,
    TraceMeta,
)

app = typer.Typer(
    help="Find the exact step where a coding agent broke your code.",
    no_args_is_help=True,
    add_completion=False,
)

EXIT_OK = 0
EXIT_NOTHING_FOUND = 1
EXIT_USAGE = 2
EXIT_SPEND_CAP = 3


def _console() -> Console:
    return Console(highlight=False)


def _fail(console: Console, message: str, code: int = EXIT_USAGE) -> typer.Exit:
    console.print(f"[bold red]Error:[/bold red] {escape(message)}")
    return typer.Exit(code)


def _require_trace(console: Console, trace: Path) -> None:
    if not (trace / "meta.json").exists() or not (trace / "trace.jsonl").exists():
        raise _fail(console, f"No trace in {trace}. Expected meta.json and trace.jsonl there.")


def _load_trace(console: Console, trace: Path) -> Trace:
    _require_trace(console, trace)
    try:
        return Trace.load(trace)
    except Exception as e:  # pydantic or JSON errors: show one clean line, not a traceback
        raise _fail(console, f"Cannot read the trace in {trace}: {e}") from e


# --------------------------------------------------------------------------- record


def _load_task(console: Console, path: Path) -> TaskSpec:
    from agentbisect.harness.agent import load_task

    if not path.exists():
        raise _fail(console, f"Task file {path} does not exist.")
    try:
        return load_task(path)
    except Exception as e:
        raise _fail(console, f"Cannot read task {path}: {e}") from e


def _model_text(model: str | None) -> str:
    if not model:
        return "the model you want to test"
    text = f"alias {model}"
    try:
        from agentbisect.llm import find_model, load_models

        entry = find_model(load_models(), model)
        if entry is not None and entry.display_name:
            text = f"{entry.display_name} (alias {model})"
    except Exception:
        pass
    return text


def _print_alan_code_steps(
    console: Console, run_dir: Path, heading: str, model: str | None, *, copy_note: bool = False
) -> None:
    """The numbered instructions for starting a recorded Alan Code run by hand."""
    workdir: Path | None = None
    try:
        workdir = TraceMeta.model_validate_json((run_dir / "meta.json").read_text()).workdir
    except Exception:
        workdir = None
    prompt_file = run_dir / "PROMPT.txt"
    folder = str(workdir) if workdir else str(run_dir / "workdir")
    console.print(f"[bold cyan]Record with Alan Code[/bold cyan]  ({escape(heading)})")
    console.print(f"Run folder: {escape(str(run_dir))}\n", soft_wrap=True)
    console.print("1. Open the Alan app and start a new conversation in this folder:")
    console.print(f"   [bold]{escape(folder)}[/bold]", soft_wrap=True)
    console.print(f"2. Pick the model: {escape(_model_text(model))}")
    console.print("3. Paste the prompt below (also saved in this file):")
    console.print(f"   {escape(str(prompt_file))}", soft_wrap=True)
    console.print("4. Let the agent work until it stops. Do not edit files yourself.")
    console.print("5. Then run:")
    console.print(
        f"   [bold]agentbisect run --trace {escape(shlex.quote(str(run_dir)))}[/bold]\n",
        soft_wrap=True,
    )
    if copy_note:
        console.print(
            "Your original repo is not modified. The agent works on a copy in the folder above.\n"
        )
    if prompt_file.exists():
        console.print("[bold]Prompt[/bold]")
        console.print(escape(prompt_file.read_text()), soft_wrap=True)


@app.command()
def record(
    task: Path | None = typer.Option(
        None, "--task", help="Task YAML, e.g. tasks/t03.yaml. Use this or --repo."
    ),
    repo: str | None = typer.Option(
        None,
        "--repo",
        help="Your own project: a local path or git URL. The agent works on a copy. "
        "Use this or --task.",
    ),
    prompt: str | None = typer.Option(None, "--prompt", help="With --repo: what to ask the agent."),
    prompt_file: Path | None = typer.Option(
        None, "--prompt-file", help="With --repo: read the prompt from this file."
    ),
    ref: str | None = typer.Option(
        None, "--ref", help="With --repo: branch, tag or commit to start from."
    ),
    no_setup: bool = typer.Option(
        False,
        "--no-setup",
        help="With --repo: skip installing the project's dependencies (the install runs the "
        "project's build code).",
    ),
    model: str | None = typer.Option(
        None, "--model", help="Model alias from tasks/models.yaml (for the pick in Alan Code)."
    ),
    runs_dir: Path = typer.Option(Path("runs"), "--runs-dir", help="Where run folders go."),
    run_id: str | None = typer.Option(None, "--run-id", help="Name for the run folder."),
    harness: bool = typer.Option(
        False,
        "--harness",
        help="Backup, with --task only: run our own agent loop through the Alan API. "
        "Spends credits.",
    ),
) -> None:
    """Set up a recorded run, from a task (--task) or your own project (--repo).

    Default: Alan Code (OpenCode in the Alan app), which you start by hand.
    """
    console = _console()
    if (task is None) == (repo is None):
        raise _fail(console, "Give exactly one of --task and --repo.")
    runs_dir = runs_dir.resolve()
    if repo is not None:
        if harness:
            raise _fail(console, "--harness works with --task only, not with --repo.")
        text = _read_prompt(console, prompt, prompt_file)
        _record_repo(console, repo, text, runs_dir, model, run_id, ref, not no_setup)
        return
    assert task is not None
    for flag, value in (
        ("--prompt", prompt),
        ("--prompt-file", prompt_file),
        ("--ref", ref),
    ):
        if value is not None:
            raise _fail(console, f"{flag} works with --repo only.")
    if no_setup:
        raise _fail(console, "--no-setup works with --repo only.")
    spec = _load_task(console, task)
    if harness:
        _record_with_harness(console, spec, model, runs_dir, run_id)
    else:
        _record_with_alan_code(console, spec, model, runs_dir, run_id)


def _read_prompt(console: Console, prompt: str | None, prompt_file: Path | None) -> str:
    if (prompt is None) == (prompt_file is None):
        raise _fail(console, "With --repo, give exactly one of --prompt and --prompt-file.")
    if prompt_file is not None:
        try:
            prompt = prompt_file.read_text()
        except OSError as e:
            raise _fail(console, f"Cannot read the prompt file {prompt_file}: {e}") from e
    assert prompt is not None
    if not prompt.strip():
        raise _fail(console, "The prompt is empty.")
    return prompt


def _setup_error_class() -> type[Exception]:
    try:
        from agentbisect.workspace import SetupError

        return SetupError
    except ImportError:  # workspace.py not landed yet: nothing can raise it
        return type("SetupError", (Exception,), {})


def _record_repo(
    console: Console,
    repo: str,
    prompt: str,
    runs_dir: Path,
    model: str | None,
    run_id: str | None,
    ref: str | None,
    setup: bool,
) -> None:
    try:
        from agentbisect.hooks.opencode import prepare_repo_run
    except ImportError as e:
        raise _fail(console, f"Recording your own repo is not available yet ({e}).", 1) from e

    setup_error = _setup_error_class()
    what = "Copying the repo and installing its dependencies" if setup else "Copying the repo"
    try:
        with console.status(f"{what}. This can take a minute."):
            run_dir = Path(
                prepare_repo_run(
                    repo, prompt, runs_dir, model_alias=model, run_id=run_id, ref=ref, setup=setup
                )
            )
    except setup_error as e:
        console.print(f"[bold red]Setup failed:[/bold red] {escape(str(e))}")
        log = getattr(e, "log_path", None)
        if log is None and run_id:
            log = runs_dir / run_id / "setup.log"
        if log is not None and str(log) not in str(e):
            console.print(f"Log: {escape(str(log))}", soft_wrap=True)
        raise typer.Exit(1) from e
    except Exception as e:
        raise _fail(console, f"Cannot prepare the run: {e}", 1) from e

    _print_alan_code_steps(console, run_dir, f"repo {repo}", model, copy_note=True)


def _record_with_alan_code(
    console: Console, spec: TaskSpec, model: str | None, runs_dir: Path, run_id: str | None
) -> None:
    try:
        from agentbisect.hooks.opencode import prepare_opencode_run
    except ImportError as e:
        raise _fail(
            console,
            f"The Alan Code recorder is not available yet ({e}). "
            "Use --harness for the backup agent loop.",
        ) from e

    try:
        run_dir = Path(prepare_opencode_run(spec, runs_dir, model, run_id))
    except Exception as e:
        raise _fail(console, f"Cannot prepare the run: {e}", 1) from e

    _print_alan_code_steps(console, run_dir, f"task {spec.id}: {spec.title}", model)


def _record_with_harness(
    console: Console, spec: TaskSpec, model: str | None, runs_dir: Path, run_id: str | None
) -> None:
    from agentbisect.harness.agent import record as record_run
    from agentbisect.llm import LLMError, SpendCapExceeded, create_client

    try:
        client = create_client(model)
    except LLMError as e:
        raise _fail(console, f"Cannot start the model client: {e}") from e

    cap = getattr(getattr(client, "config", None), "spend_cap_usd", None)
    ledger = getattr(client, "ledger", None)
    spent = ledger.spent() if ledger is not None else None
    lines = [
        f"Task:   {escape(spec.id)}  {escape(spec.title)}",
        f"Model:  {escape(client.model)}" + (f"  (alias {escape(model)})" if model else ""),
    ]
    if cap is not None:
        left = f", ${spent:.2f} already spent" if spent is not None else ""
        lines.append(f"Cap:    ${cap:.2f} hard spend cap{left}")
    lines.append(f"Steps:  up to {spec.max_steps}")
    console.print(
        Panel("\n".join(lines), title="Recording (spends credits)", border_style="yellow")
    )

    try:
        run_dir = record_run(spec, client, runs_dir, model_alias=model, run_id=run_id)
    except SpendCapExceeded as e:
        raise _fail(console, f"Stopped at the spend cap: {e}", EXIT_SPEND_CAP) from e
    except LLMError as e:
        raise _fail(console, f"Model call failed: {e}", 1) from e

    console.print(f"[bold green]Recorded[/bold green] {escape(str(run_dir))}")
    try:
        recorded = Trace.load(Path(run_dir))
        cost = recorded.meta.cost_usd or client.usage_total.cost_usd
        console.print(
            f"{recorded.n} steps, {recorded.total_tokens:,} tokens, cost ${cost:.4f}"
            + ("" if recorded.meta.finished else " (the agent did not call finish)")
        )
    except Exception:
        console.print(f"Cost this run: ${client.usage_total.cost_usd:.4f}")
    console.print(f"Next: agentbisect run --trace {escape(shlex.quote(str(run_dir)))}")


# --------------------------------------------------------------------------- run


def _probe_line(culprit: CulpritReport) -> str:
    b = culprit.bisect
    parts = []
    for p in b.probes:
        colour = {"pass": "green", "fail": "red"}.get(p.verdict.value, "yellow")
        suffix = f"\u00d7{p.n_runs}" if p.n_runs > 1 else ""
        parts.append(f"{p.step_index}:[{colour}]{p.verdict.value}[/{colour}]{suffix}")
    return "  ".join(parts)


def _print_culprit(console: Console, culprit: CulpritReport, loaded: Trace, many: bool) -> None:
    from agentbisect.report.text import check_label, verdict_sentences

    b = culprit.bisect
    label = check_label(culprit.check)
    title = f"Verdict: {escape(label)}" if many else "Verdict"
    if b.status is BisectStatus.FOUND and b.decisive_step is not None:
        step = culprit.step
        if step is None and 1 <= b.decisive_step <= loaded.n:
            step = loaded.steps[b.decisive_step - 1]
        sentences = verdict_sentences(culprit, step)
        body = f"[bold]{escape(sentences[0])}[/bold]"
        if len(sentences) > 1:
            body += "\n" + escape(" ".join(sentences[1:]))
        if b.non_monotone:
            steps = ", ".join(str(t) for t in b.transitions)
            body += (
                f"\n[yellow]Non-monotone history: pass to fail at steps {steps}. "
                "The last one is the decisive step.[/yellow]"
            )
        console.print(Panel(body, title=title, border_style="red", expand=False))
    else:
        reason = escape(b.reason or "no reason recorded")
        body = f"[bold]Not bisectable:[/bold] {reason}\nCheck: {escape(culprit.check)}"
        console.print(Panel(body, title=title, border_style="yellow", expand=False))
    if b.probes:
        conf = f", confidence {b.confidence * 100:.1f}%" if b.confidence is not None else ""
        console.print(f"[dim]Probes ({b.n_probes}{conf}):[/dim] {_probe_line(culprit)}")


@app.command()
def run(
    trace: Path = typer.Option(..., "--trace", help="Run directory, e.g. runs/<id>."),
    check: str | None = typer.Option(
        None, "--check", help="Oracle command. If omitted, bisect every newly failing test."
    ),
    timeout: float = typer.Option(120.0, "--timeout", help="Seconds allowed per oracle run."),
) -> None:
    """Bisect a recorded trace to find the decisive step."""
    from agentbisect.bisect import bisect_run

    console = _console()
    trace = trace.resolve()  # the oracle runs in scratch dirs, so it needs an absolute path
    if (trace / "events.jsonl").exists():  # Alan Code run: finalize in case the agent never idled
        from agentbisect.hooks.opencode import finalize_run

        if finalize_run(trace).get("resumed_conversation"):
            console.print(
                "[bold yellow]Warning:[/bold yellow] Alan resumed an earlier conversation into this "
                "run, so the agent also saw an old task. Do not use it as study data; record again "
                "in a new conversation."
            )
    loaded = _load_trace(console, trace)
    if loaded.n == 0:
        raise _fail(console, "The trace has no steps.")

    run_report = bisect_run(loaded, trace, check, timeout)
    (trace / "bisect.json").write_text(run_report.model_dump_json(indent=2))

    if not run_report.culprits:
        console.print(
            Panel(
                "No failing check to bisect. Nothing passed at the start and failed at the end.",
                title="Verdict",
                border_style="yellow",
                expand=False,
            )
        )
    many = len(run_report.culprits) > 1
    for culprit in run_report.culprits:
        _print_culprit(console, culprit, loaded, many)
    console.print(f"Wrote {escape(str(trace / 'bisect.json'))}")
    n_found = sum(1 for c in run_report.culprits if c.bisect.status is BisectStatus.FOUND)
    if n_found:
        console.print(f"Next: agentbisect report --trace {escape(shlex.quote(str(trace)))}")
        return
    raise typer.Exit(EXIT_NOTHING_FOUND)


# --------------------------------------------------------------------------- report


def _add_explanations(console: Console, trace: Path, model: str | None) -> None:
    """Fill ``explanation`` on each found culprit and save bisect.json. Never fatal."""
    path = trace / "bisect.json"
    if not path.exists():
        console.print("[yellow]No bisect.json, so nothing to explain.[/yellow]")
        return
    try:
        from agentbisect.explain import explain
    except ImportError:
        console.print("[yellow]No AI explanation: explain.py is not available yet.[/yellow]")
        return
    from agentbisect.llm import create_client

    try:
        client = create_client(model)
    except Exception as e:
        console.print(f"[yellow]No AI explanation: {escape(str(e))}[/yellow]")
        return
    cap = getattr(getattr(client, "config", None), "spend_cap_usd", None)
    cap_text = f", spend cap ${cap:.2f}" if cap is not None else ""
    console.print(f"AI explanation from {escape(client.model)}{cap_text}.")

    run_report = RunReport.model_validate_json(path.read_text())
    changed = False
    for culprit in run_report.culprits:
        if culprit.bisect.status is not BisectStatus.FOUND or culprit.explanation:
            continue
        try:
            culprit.explanation = explain(culprit, client)
            changed = True
        except Exception as e:
            console.print(
                f"[yellow]No AI explanation for {escape(culprit.check)}: {escape(str(e))}[/yellow]"
            )
    if changed:
        path.write_text(run_report.model_dump_json(indent=2))


@app.command()
def report(
    trace: Path = typer.Option(..., "--trace", help="Run directory."),
    explain: bool = typer.Option(
        False, "--explain", help="Add an AI explanation (needs Alan credentials; spends a little)."
    ),
    model: str | None = typer.Option(
        None, "--model", help="Model alias for the explanation. Default: ALAN_MODEL."
    ),
) -> None:
    """Write runs/<id>/report.html: one static page that opens offline."""
    console = _console()
    trace = trace.resolve()
    _load_trace(console, trace)
    if explain:
        _add_explanations(console, trace, model)
    if not (trace / "bisect.json").exists():
        console.print(
            "[yellow]No bisect.json yet.[/yellow] "
            f"Run `agentbisect run --trace {escape(shlex.quote(str(trace)))}` first for a full report."
        )

    from agentbisect.report.render import render_report

    try:
        out = render_report(trace)
    except Exception as e:
        raise _fail(console, f"Cannot build the report: {e}", 1) from e
    console.print(f"[bold green]Wrote[/bold green] {escape(str(out))}")
    console.print(f"Open it in a browser: {escape(out.resolve().as_uri())}")


# --------------------------------------------------------------------------- study


def _filter_summary(
    summary: StudySummary, models: set[str] | None, tasks: set[str] | None
) -> StudySummary:
    """Keep rows (and their repaired-break episodes) for the chosen models and tasks."""
    if models is None and tasks is None:
        return summary
    rows = [
        r
        for r in summary.rows
        if (models is None or r.model in models) and (tasks is None or r.task_id in tasks)
    ]
    kept = {r.run_id for r in rows}
    episodes = [e for e in summary.episodes if e.run_id in kept]
    return summary.model_copy(update={"rows": rows, "episodes": episodes})


def _print_study(console: Console, summary: StudySummary) -> None:
    from agentbisect.report.study import (
        broken_text,
        lag_text,
        metric_interval,
        metric_value,
        sabotage_text,
        share_text,
    )
    from agentbisect.report.text import check_label

    if summary.design:
        console.print(f"[dim]{escape(summary.design)}[/dim]")

    runs_table = Table(title="Runs", title_justify="left")
    for col in (
        "task",
        "run",
        "steps",
        "broken?",
        "t_err",
        "t_obs",
        "lag",
        "wasted",
        "sabotage ok",
    ):
        runs_table.add_column(col, overflow="fold")
    for r in summary.rows:
        runs_table.add_row(
            escape(r.task_id),
            escape(r.run_id),
            str(r.n_steps),
            escape(broken_text(r) + (f" ({check_label(r.check)})" if r.check else "")),
            "-" if r.t_err is None else str(r.t_err),
            "-" if r.t_obs is None else str(r.t_obs),
            escape(lag_text(r)),
            share_text(r),
            sabotage_text(r),
        )
    console.print(runs_table)

    if summary.excluded:
        console.print("[bold]Excluded runs[/bold]")
        for run_id, reason in sorted(summary.excluded.items()):
            console.print(f"  {escape(run_id)}: {escape(reason)}")

    if summary.episodes:
        ep_table = Table(title="Repaired breaks (seen, then fixed)", title_justify="left")
        for col in ("run", "test", "seen at", "t_err", "lag", "wasted tokens", "note"):
            ep_table.add_column(col)
        for e in summary.episodes:
            ep_table.add_row(
                escape(e.run_id),
                escape(check_label(e.check)),
                str(e.seen_at),
                "-" if e.t_err is None else str(e.t_err),
                "-" if e.lag is None else str(e.lag),
                "-" if e.wasted_tokens is None else f"{e.wasted_tokens:,}",
                escape(e.reason or ""),
            )
        console.print(ep_table)
    else:
        console.print("[dim]No repaired breaks found.[/dim]")

    if summary.metrics:
        m_table = Table(title="Metrics", title_justify="left")
        for col in ("metric", "value", "95% CI", "n", "method"):
            m_table.add_column(col)
        for m in summary.metrics:
            interval = metric_interval(m).removeprefix("95% CI ")
            m_table.add_row(escape(m.name), metric_value(m), interval, str(m.n), escape(m.method))
        console.print(m_table)


@app.command()
def study(
    runs: Path = typer.Option(
        Path("runs"), "--runs", help="Folder of recorded run folders (runs/<id>/)."
    ),
    rebisect: bool = typer.Option(
        False, "--rebisect", help="Bisect every run again instead of reusing its bisect.json."
    ),
    models: str | None = typer.Option(
        None, "--models", help="Only runs of these model aliases, comma-separated (e.g. flash)."
    ),
    suite: Path | None = typer.Option(
        None, "--suite", help="Only runs of tasks that have a YAML in this folder (e.g. tasks/)."
    ),
    repeats: int | None = typer.Option(
        None,
        "--repeats",
        hidden=True,
        help="Ignored. Runs are recorded by hand, so the repeat count is whatever you recorded.",
    ),
) -> None:
    """Add up recorded runs into lag statistics, a study page and a draft RESULTS.

    Reads the runs/<id>/ folders and never starts an agent. Writes study.json, study.html and
    RESULTS.draft.md into <runs>/study/. Exits 1 when no run is usable. The old --repeats flag is
    accepted and ignored: runs are recorded by hand, so the repeat count is what you recorded.
    """
    import importlib

    console = _console()
    runs = runs.resolve()
    if not runs.is_dir():
        raise _fail(console, f"The runs folder {runs} does not exist.")
    try:
        study_mod = importlib.import_module("agentbisect.study")
    except ImportError as e:
        raise _fail(console, f"The study module is not available yet ({e}).", 1) from e

    wanted = {m.strip() for m in models.split(",") if m.strip()} if models else None
    tasks: set[str] | None = None
    if suite is not None:
        if not suite.is_dir():
            raise _fail(console, f"The suite folder {suite} does not exist.")
        tasks = {p.stem for p in suite.glob("*.yaml") if p.stem != "models"}

    try:
        summary = study_mod.collect(runs, rebisect=rebisect)
    except Exception as e:
        raise _fail(console, f"Cannot read the runs in {runs}: {e}", 1) from e
    summary = _filter_summary(summary, wanted, tasks)

    if not summary.rows:
        console.print(f"[yellow]No usable runs in {escape(str(runs))}.[/yellow]")
        if summary.excluded:
            console.print("[bold]Excluded runs[/bold]")
            for run_id, reason in sorted(summary.excluded.items()):
                console.print(f"  {escape(run_id)}: {escape(reason)}")
        console.print(
            "Record runs with `agentbisect record`, run them in Alan Code, then try again."
        )
        raise typer.Exit(EXIT_NOTHING_FOUND)

    summary = study_mod.summarize(summary)
    out_dir = runs / "study"
    json_path = study_mod.write(summary, out_dir)

    from agentbisect.report.render import render_report
    from agentbisect.report.study import render_results_draft, render_study

    linkable: set[str] = set()
    for run_id in sorted({r.run_id for r in summary.rows}):
        run_dir = runs / run_id
        if not (run_dir / "meta.json").exists() or not (run_dir / "trace.jsonl").exists():
            continue
        try:
            render_report(run_dir)
            linkable.add(run_id)
        except Exception:  # a run whose page fails stays in the study, unlinked
            continue
    try:
        html_path = render_study(summary, out_dir, link_prefix="../", linkable=linkable)
        draft_path = render_results_draft(summary, out_dir)
    except Exception as e:
        raise _fail(console, f"Cannot build the study page: {e}", 1) from e

    _print_study(console, summary)
    console.print(f"[bold green]Wrote[/bold green] {escape(str(json_path))}")
    console.print(f"[bold green]Wrote[/bold green] {escape(str(html_path))}")
    console.print(f"[bold green]Wrote[/bold green] {escape(str(draft_path))}")
    console.print(
        "Audited by math-analyst: pending. Copy RESULTS.draft.md to docs/RESULTS.md after."
    )


if __name__ == "__main__":
    app()
