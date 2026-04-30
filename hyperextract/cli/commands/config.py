"""Config command for Hyper-Extract CLI."""

from typing import Optional
from rich.console import Console
from rich.table import Table
from rich.text import Text

from ..config import ConfigManager
from hyperextract.utils.logging import get_logger
import typer

logger = get_logger("he.config")
console = Console()

app = typer.Typer(
    name="config",
    help="Manage LLM and Embedder configuration",
    invoke_without_command=True,
)


@app.callback()
def config_callback(
    ctx: typer.Context,
):
    """Show configuration help when running 'he config' without subcommand."""
    if ctx.invoked_subcommand is not None:
        return

    from ..utils import LOGO

    console.print()
    console.print(Text(LOGO, style="bold cyan"))

    from rich.rule import Rule

    console.print(Rule(style="cyan dim"))
    console.print()

    title_text = Text("CONFIGURATION", style="bold cyan")
    desc_text = Text("Manage LLM and Embedder settings", style="dim")

    header = Table(box=None, show_header=False, pad_edge=False)
    header.add_column(no_wrap=True)
    header.add_column(style="dim white", no_wrap=True)
    header.add_row(title_text, desc_text)

    console.print(header)
    console.print()
    console.print(Rule(style="cyan dim"))
    console.print()

    console.print("[bold cyan]Available Commands:[/bold cyan]")
    console.print()

    commands_info = [
        (
            "he config init",
            "Interactive configuration setup (recommended for first-time users)",
        ),
        ("he config show", "Display current configuration"),
        ("he config llm", "Configure LLM settings"),
        ("he config embedder", "Configure Embedder settings"),
    ]

    for cmd, desc in commands_info:
        console.print(f"  [green]{cmd:<30}[/green] {desc}")

    console.print()
    console.print(Rule(style="cyan dim"))
    console.print()

    console.print("[bold cyan]Quick Start:[/bold cyan]")
    console.print()
    console.print(
        "  [yellow]1.[/yellow] Run [green]he config init[/green] for interactive setup"
    )
    console.print("  [yellow]2.[/yellow] Or configure individually:")
    console.print("     [green]he config llm --api-key YOUR_KEY[/green]")
    console.print("     [green]he config embedder --api-key YOUR_KEY[/green]")
    console.print()

    console.print("[bold cyan]Environment Variables (alternative):[/bold cyan]")
    console.print()
    console.print(
        "  [green]OPENAI_API_KEY[/green] - OpenAI API key (used if not set in config)"
    )
    console.print("  [green]OPENAI_BASE_URL[/green] - Custom API base URL (optional)")
    console.print()

    console.print(Rule(style="cyan dim"))
    console.print()

    hint_text = Text("💡 Tip: Run ", style="dim")
    hint_text.append("he config <command> --help", style="bold cyan")
    hint_text.append(" for detailed command usage", style="dim")
    console.print(hint_text)
    console.print()

    raise typer.Exit()


def _show_config():
    """Show current configuration."""
    config = ConfigManager()
    cfg = config.show()

    table = Table(title="Hyper-Extract Configuration")
    table.add_column("Service", style="cyan", width=15)
    table.add_column("Model", style="yellow", width=30)
    table.add_column("API Key", style="magenta", width=25)
    table.add_column("Base URL", style="green", width=25)

    llm_cfg = cfg["llm"]
    emb_cfg = cfg["embedder"]

    table.add_row(
        "LLM",
        llm_cfg["model"],
        llm_cfg["api_key"][:10] + "..." if llm_cfg["api_key"] else "(not set)",
        llm_cfg["base_url"] or "(default)",
    )
    table.add_row(
        "Embedder",
        emb_cfg["model"],
        emb_cfg["api_key"][:10] + "..." if emb_cfg["api_key"] else "(not set)",
        emb_cfg["base_url"] or "(default)",
    )

    console.print(table)


@app.command(name="show")
def show(
    show_all: bool = typer.Option(False, "--show", help="Show all configuration"),
):
    """Show current configuration."""
    logger.info("command=config-show")
    _show_config()


@app.command(name="llm")
def llm(
    api_key: Optional[str] = typer.Option(
        None,
        "--api-key",
        "-k",
        help="LLM API key",
    ),
    model: Optional[str] = typer.Option(
        None,
        "--model",
        "-m",
        help="LLM model name",
    ),
    base_url: Optional[str] = typer.Option(
        None,
        "--base-url",
        "-u",
        help="Custom API base URL",
    ),
    show: bool = typer.Option(False, "--show", help="Show current LLM configuration"),
    unset: bool = typer.Option(False, "--unset", help="Unset LLM configuration"),
):
    """Configure LLM settings."""
    logger.info("command=config-llm show=%s unset=%s", show, unset)
    config = ConfigManager()

    if show:
        cfg = config.get_llm_config()
        table = Table(title="LLM Configuration", show_header=False)
        table.add_column("Key", style="cyan")
        table.add_column("Value", style="green")
        table.add_row("Model", cfg.model)
        table.add_row(
            "API Key", cfg.api_key[:10] + "..." if cfg.api_key else "(not set)"
        )
        table.add_row("Base URL", cfg.base_url or "(default)")
        console.print(table)
        return

    if unset:
        config.unset_llm()
        console.print("[green]LLM configuration cleared[/green]")
        return

    config.set_llm(
        model=model,
        api_key=api_key,
        base_url=base_url,
    )
    console.print("[green]LLM configuration updated[/green]")


@app.command(name="embedder")
def embedder(
    api_key: Optional[str] = typer.Option(
        None,
        "--api-key",
        "-k",
        help="Embedder API key",
    ),
    model: Optional[str] = typer.Option(
        None,
        "--model",
        "-m",
        help="Embedder model name",
    ),
    base_url: Optional[str] = typer.Option(
        None,
        "--base-url",
        "-u",
        help="Custom API base URL",
    ),
    show: bool = typer.Option(
        False, "--show", help="Show current Embedder configuration"
    ),
    unset: bool = typer.Option(False, "--unset", help="Unset Embedder configuration"),
):
    """Configure Embedder settings."""
    logger.info("command=config-embedder show=%s unset=%s", show, unset)
    config = ConfigManager()

    if show:
        cfg = config.get_embedder_config()
        table = Table(title="Embedder Configuration", show_header=False)
        table.add_column("Key", style="cyan")
        table.add_column("Value", style="green")
        table.add_row("Model", cfg.model)
        table.add_row(
            "API Key", cfg.api_key[:10] + "..." if cfg.api_key else "(not set)"
        )
        table.add_row("Base URL", cfg.base_url or "(default)")
        console.print(table)
        return

    if unset:
        config.unset_embedder()
        console.print("[green]Embedder configuration cleared[/green]")
        return

    config.set_embedder(
        api_key=api_key,
        model=model,
        base_url=base_url,
    )
    console.print("[green]Embedder configuration updated[/green]")


@app.command(name="init")
def init(
    api_key: Optional[str] = typer.Option(
        None,
        "--api-key",
        "-k",
        help="API key for both LLM and Embedder",
    ),
    base_url: Optional[str] = typer.Option(
        None,
        "--base-url",
        "-u",
        help="Custom API base URL",
    ),
):
    """Initialize configuration interactively."""
    logger.info("command=config-init api_key_provided=%s", api_key is not None)
    config = ConfigManager()

    if api_key:
        config.set_llm(
            model="gpt-4o-mini",
            api_key=api_key,
            base_url=base_url,
        )
        config.set_embedder(
            model="text-embedding-3-small",
            api_key=api_key,
            base_url=base_url,
        )
        console.print("[bold green]Configuration saved successfully![/bold green]")
        console.print()
        console.print("[bold]Current settings:[/bold]")
        console.print("  [cyan]LLM Model:[/cyan] gpt-4o-mini")
        console.print("  [cyan]Embedder Model:[/cyan] text-embedding-3-small")
        console.print("  [cyan]API Key:[/cyan] " + api_key[:10] + "...")
        if base_url:
            console.print(f"  [cyan]Base URL:[/cyan] {base_url}")
        return

    console.print("[bold blue]Hyper-Extract Configuration Setup[/bold blue]")
    console.print()

    console.print("[bold]Step 1: LLM Configuration[/bold]")
    model = console.input("  Model (default: gpt-4o-mini): ") or "gpt-4o-mini"

    llm_api_key = None
    while not llm_api_key:
        llm_api_key = console.input("  API Key: ")
        if not llm_api_key:
            console.print(
                "  [red]API Key is required. Please enter your API key.[/red]"
            )

    llm_base_url = console.input("  Base URL (optional, press Enter to skip): ") or None

    config.set_llm(
        model=model,
        api_key=llm_api_key,
        base_url=llm_base_url,
    )

    console.print()

    console.print("[bold]Step 2: Embedder Configuration[/bold]")
    emb_model = (
        console.input("  Model (default: text-embedding-3-small): ")
        or "text-embedding-3-small"
    )

    emb_api_key = None
    while not emb_api_key:
        emb_api_key = console.input("  API Key: ")
        if not emb_api_key:
            console.print(
                "  [red]API Key is required. Please enter your API key.[/red]"
            )

    emb_base_url = console.input("  Base URL (optional, press Enter to skip): ") or None

    config.set_embedder(
        model=emb_model,
        api_key=emb_api_key,
        base_url=emb_base_url,
    )

    console.print()
    console.print("[bold green]Configuration saved successfully![/bold green]")
