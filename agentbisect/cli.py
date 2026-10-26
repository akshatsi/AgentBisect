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


@app.command()
def record(
    task: Path = typer.Option(..., "--task", help="Task YAML, e.g. tasks/t03.yaml."),
    model: str | None = typer.Option(
        None, "--model", help="Model alias from tasks/models.yaml (for the pick in Alan Code)."
    ),
    runs_dir: Path = typer.Option(Path("runs"), "--runs-dir", help="Where run folders go."),
    run_id: str | None = typer.Option(None, "--run-id", help="Name for the run folder."),
    harness: bool = typer.Option(
        False,
        "--harness",
        help="Backup: run our own agent loop through the Alan API. Spends credits.",
    ),
) -> None:
    """Set up a recorded run. Default: Alan Code (OpenCode in the Alan app), which you start by hand."""
    console = _console()
    spec = _load_task(console, task)
    runs_dir = runs_dir.resolve()
    if harness:
        _record_with_harness(console, spec, model, runs_dir, run_id)
    else:
        _record_with_alan_code(console, spec, model, runs_dir, run_id)


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

    workdir: Path | None = None
    try:
        workdir = TraceMeta.model_validate_json((run_dir / "meta.json").read_text()).workdir
    except Exception:
        workdir = None
    prompt_file = run_dir / "PROMPT.txt"
    model_text = "the model you want to test"
    if model:
        model_text = f"alias {model}"
        try:
            from agentbisect.llm import find_model, load_models

            entry = find_model(load_models(), model)
            if entry is not None and entry.display_name:
                model_text = f"{entry.display_name} (alias {model})"
        except Exception:
            pass

    folder = str(workdir) if workdir else str(run_dir / "workdir")
    console.print(
        f"[bold cyan]Record with Alan Code[/bold cyan]  (task {escape(spec.id)}: {escape(spec.title)})"
    )
    console.print(f"Run folder: {escape(str(run_dir))}\n", soft_wrap=True)
    console.print("1. Open the Alan app and start a new conversation in this folder:")
    console.print(f"   [bold]{escape(folder)}[/bold]", soft_wrap=True)
    console.print(f"2. Pick the model: {escape(model_text)}")
    console.print("3. Paste the prompt below (also saved in this file):")
    console.print(f"   {escape(str(prompt_file))}", soft_wrap=True)
    console.print("4. Let the agent work until it stops. Do not edit files yourself.")
    console.print("5. Then run:")
    console.print(
        f"   [bold]agentbisect run --trace {escape(shlex.quote(str(run_dir)))}[/bold]\n",
        soft_wrap=True,
    )
    if prompt_file.exists():
        console.print("[bold]Prompt[/bold]")
        console.print(escape(prompt_file.read_text()), soft_wrap=True)


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


@app.command()
def study(
    suite: Path = typer.Option(Path("tasks"), "--suite", help="Folder of task YAMLs."),
    models: str = typer.Option("qwen,flash,glm,kimi", "--models", help="Model aliases."),
    repeats: int = typer.Option(1, "--repeats", help="Runs per task and model."),
) -> None:
    """Run many tasks across models and compute detection-lag statistics (comes in M3)."""
    console = _console()
    table = Table(show_header=False, box=None)
    table.add_row("suite", escape(str(suite)))
    table.add_row("models", escape(models))
    table.add_row("repeats", str(repeats))
    console.print(table)
    console.print(
        "[yellow]`agentbisect study` comes in M3.[/yellow] It will print a cost estimate and ask "
        "you to confirm before it spends any credits."
    )
    raise typer.Exit(EXIT_USAGE)


if __name__ == "__main__":
    app()
