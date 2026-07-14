"""Evaluate Course Graph outputs against caller-owned Gold Datasets."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from hyperextract.evaluation.course_profile import (
    evaluate_course_profile,
    write_evaluation_report,
)
from hyperextract.profiles.course import load_course_profile


app = typer.Typer(help="Evaluate extraction quality without model calls")
console = Console()


@app.command(name="course-profile")
def evaluate_profile(
    dataset: Path = typer.Option(
        ..., "--dataset", exists=True, dir_okay=False, help="Gold Dataset JSON"
    ),
    graph: Path = typer.Option(
        ..., "--graph", exists=True, dir_okay=False, help="Course Graph v1 JSON"
    ),
    profile: Path | None = typer.Option(
        None,
        "--profile",
        exists=True,
        dir_okay=False,
        help="Use quality thresholds from this profile",
    ),
    output: Path | None = typer.Option(
        None, "--output", "-o", help="Write the JSON report"
    ),
) -> None:
    """Compare one Course Graph with a frozen human-labelled dataset."""
    thresholds = load_course_profile(profile).evaluation_thresholds if profile else None
    report = evaluate_course_profile(dataset, graph, thresholds=thresholds)
    if output:
        write_evaluation_report(report, output)

    table = Table(title="Course Profile Evaluation", show_header=True)
    table.add_column("Metric")
    table.add_column("Value", justify="right")
    table.add_column("Gate", justify="center")
    metrics = report.metrics.model_dump()
    for name, passed in report.gates.items():
        table.add_row(
            name,
            f"{metrics[name]:.2%}",
            "PASS" if passed else "FAIL",
        )
    console.print(table)
    console.print(
        "[bold green]PASS[/bold green]"
        if report.passed
        else "[bold red]FAIL[/bold red]"
    )
    if output:
        console.print(f"[dim]Report: {output}[/dim]")
    if not report.passed:
        raise typer.Exit(2)
