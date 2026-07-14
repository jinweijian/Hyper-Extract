"""Inspect and validate course extraction profiles."""

from __future__ import annotations

import json
from pathlib import Path

import typer
from rich.console import Console

from hyperextract.profiles.course import (
    compile_course_profile,
    load_course_profile,
    profile_summary,
)


app = typer.Typer(help="Validate and inspect course extraction profiles")
console = Console()


@app.command(name="validate")
def validate_profile(
    profile: Path = typer.Argument(..., exists=True, dir_okay=False),
    json_output: bool = typer.Option(False, "--json", help="Print JSON"),
) -> None:
    """Validate a profile without initializing LLM or embedding clients."""
    loaded = load_course_profile(profile)
    summary = profile_summary(loaded)
    if json_output:
        typer.echo(json.dumps(summary, ensure_ascii=False, sort_keys=True))
        return
    console.print(f"[bold green]Valid[/bold green] {loaded.name} v{loaded.version}")
    console.print(f"[dim]Content hash: {loaded.content_hash}[/dim]")


@app.command(name="render")
def render_profile(
    profile: Path = typer.Argument(..., exists=True, dir_okay=False),
    stage: str = typer.Option(
        "nodes",
        "--stage",
        help="nodes, local-edges, global-edges, dedup, or community",
    ),
) -> None:
    """Render the exact prompt compiled for one extraction stage."""
    compiled = compile_course_profile(load_course_profile(profile))
    try:
        typer.echo(compiled.stage(stage))
    except ValueError as error:
        raise typer.BadParameter(str(error), param_hint="--stage") from error
