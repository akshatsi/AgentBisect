"""Command-line entry point. Kept to four commands: record, run, report, study."""

from __future__ import annotations

from pathlib import Path

import typer

app = typer.Typer(
    help="Find the exact step where a coding agent broke your code.",
    no_args_is_help=True,
    add_completion=False,
)


def _todo(name: str) -> None:
    typer.echo(f"`agentbisect {name}` is not implemented yet.", err=True)
    raise typer.Exit(2)


@app.command()
def record(
    task: Path = typer.Option(..., "--task", help="Task YAML, e.g. tasks/t01.yaml."),
) -> None:
    """Run the open-model agent on a task and snapshot every step."""
    _todo("record")


@app.command()
def run(
    trace: Path = typer.Option(..., "--trace", help="Run directory, e.g. runs/<id>."),
    check: str | None = typer.Option(
        None, "--check", help="Oracle command. If omitted, bisect every newly failing test."
    ),
) -> None:
    """Bisect a recorded trace to find the decisive step."""
    from rich.console import Console
    from rich.table import Table

    from agentbisect import snapshot
    from agentbisect.bisect import bisect, describe_step
    from agentbisect.models import (
        BisectStatus,
        CulpritReport,
        OracleConfig,
        RunReport,
        Trace,
    )
    from agentbisect.oracle import count_runs

    console = Console()
    if check is None:
        console.print("Auto-oracle (bisecting every newly failing test) comes in M2. Pass --check.")
        raise typer.Exit(2)
    if not (trace / "meta.json").exists():
        console.print(f"[red]No trace found in {trace}[/red] (expected meta.json and trace.jsonl).")
        raise typer.Exit(2)

    loaded = Trace.load(trace)
    result = bisect(loaded, trace, OracleConfig(check=check))

    culprit = CulpritReport(run_id=loaded.meta.run_id, check=check, bisect=result)
    if result.status is BisectStatus.FOUND and result.decisive_step is not None:
        step = loaded.steps[result.decisive_step - 1]
        culprit.step = step
        culprit.diff = snapshot.diff(
            trace,
            loaded.snapshot_at(result.decisive_step - 1),
            loaded.snapshot_at(result.decisive_step),
        )
    report_ = RunReport(run_id=loaded.meta.run_id, checks=[check], culprits=[culprit])
    (trace / "bisect.json").write_text(report_.model_dump_json(indent=2))

    if culprit.step is not None:
        console.print(
            f"[bold green]Step {culprit.step.index}[/bold green] "
            f"({describe_step(culprit.step)}) broke [bold]{check}[/bold]. "
            f"{result.n_probes} probes.",
            soft_wrap=True,
        )
    else:
        console.print(f"[bold red]Not bisectable:[/bold red] {result.reason}")

    table = Table(title="Probe log", show_lines=False)
    for col in ("#", "step", "verdict", "runs", "fails", "seconds"):
        table.add_column(col)
    for i, probe in enumerate(result.probes, 1):
        seconds = sum(r.duration_s for r in probe.runs)
        n_fail, n_counted, _ = count_runs(probe.runs)
        table.add_row(
            str(i),
            str(probe.step_index),
            probe.verdict.value,
            str(probe.n_runs),
            str(n_fail),
            f"{seconds:.2f}",
        )
    console.print(table)
    console.print(f"Wrote {trace / 'bisect.json'}")
    if result.status is not BisectStatus.FOUND:
        raise typer.Exit(1)


@app.command()
def report(trace: Path = typer.Option(..., "--trace", help="Run directory.")) -> None:
    """Write runs/<id>/report.html."""
    _todo("report")


@app.command()
def study(
    suite: Path = typer.Option(Path("tasks"), "--suite"),
    models: str = typer.Option("qwen,flash,glm,kimi", "--models"),
    repeats: int = typer.Option(1, "--repeats"),
) -> None:
    """Run many tasks across models and compute detection-lag statistics."""
    _todo("study")


if __name__ == "__main__":
    app()
