from __future__ import annotations

import os
from pathlib import Path

import typer
from rich.console import Console
from rich.json import JSON

from hyperextract.providers.probe import CapabilityProbe, ProbeStore, describe_probe
from hyperextract.providers.registry import ProviderRegistry

app = typer.Typer(help="Validate, inspect, and probe model capability profiles.")
console = Console()


def _registry(path: Path | None) -> ProviderRegistry:
    configured = path or (
        Path(os.environ["HE_SERVICE_MODEL_PROFILES"])
        if os.environ.get("HE_SERVICE_MODEL_PROFILES")
        else None
    )
    return ProviderRegistry(configured)


@app.command("validate")
def validate(
    profile: str = typer.Option(..., "--profile", help="Profile name"),
    file: Path | None = typer.Option(None, "--file", help="Profile TOML path"),
    no_secrets: bool = typer.Option(
        False, "--no-secrets", help="Validate schema without requiring credentials"
    ),
) -> None:
    registry = _registry(file)
    warnings_found = registry.validate(profile, require_secrets=not no_secrets)
    descriptor = registry.public_descriptor(profile)
    console.print(f"[green]valid[/green] {profile} {descriptor['fingerprint']}")
    for warning in warnings_found:
        console.print(f"[yellow]warning[/yellow] {warning}")


@app.command("show")
def show(
    profile: str = typer.Option(..., "--profile", help="Profile name"),
    file: Path | None = typer.Option(None, "--file", help="Profile TOML path"),
) -> None:
    registry = _registry(file)
    descriptor = registry.public_descriptor(profile)
    descriptor["probe"] = describe_probe(ProbeStore().load(descriptor["fingerprint"]))
    console.print(JSON.from_data(descriptor))


@app.command("probe")
def probe(
    profile: str = typer.Option(..., "--profile", help="Profile name"),
    file: Path | None = typer.Option(None, "--file", help="Profile TOML path"),
) -> None:
    registry = _registry(file)
    selected = registry.get(profile)
    generation = registry.create_generation_adapter(profile)
    embedding = (
        registry.create_embedding_adapter(profile) if selected.embedder else None
    )
    result = CapabilityProbe(generation, embedding).run(selected)
    path = ProbeStore().save(result)
    console.print(JSON.from_data(describe_probe(result)))
    console.print(f"[green]saved[/green] {path}")
    if not all(result.checks.values()):
        raise typer.Exit(code=1)
