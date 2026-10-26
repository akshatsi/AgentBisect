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
    _todo("run")


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
