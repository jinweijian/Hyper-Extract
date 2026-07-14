"""CLI entry point for Hyper-Extract."""

from pathlib import Path
from typing import Optional
import os

import typer
from rich.console import Console
from rich.table import Table
from rich.text import Text
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.prompt import Prompt

from hyperextract.utils.template_engine import Gallery, Template
from hyperextract.utils.logging import configure_logging, get_logger

from .utils import (
    LOGO,
    read_input,
    validate_config,
    validate_ka_path,
    validate_ka_with_data,
    validate_ka_with_index,
    get_template_from_ka,
)
from .config import (
    load_ka_metadata,
)

from .commands import config_app, evaluate_app, list_app, model_app, profile_app

console = Console()
logger = get_logger("he")

app = typer.Typer(
    name="he",
    help="Hyper-Extract CLI - A command-line tool for knowledge extraction",
    add_completion=False,
    invoke_without_command=True,
)

app.add_typer(list_app, name="list")
app.add_typer(config_app, name="config")
app.add_typer(profile_app, name="profile")
app.add_typer(evaluate_app, name="evaluate")
app.add_typer(model_app, name="model")


@app.callback()
def main(
    ctx: typer.Context,
    version: bool = typer.Option(
        False,
        "--version",
        "-v",
        help="Show version information",
        is_eager=True,
    ),
):
    # Configure logging after all imports complete so dependency loggers
    # (e.g. ontosight) don't override our level settings.
    # Log level is controlled solely by the HYPER_EXTRACT_LOG_LEVEL env var.
    configure_logging()
    if version:
        from . import __version__

        console.print(f"[bold]Hyper-Extract CLI[/bold] version {__version__}")
        raise typer.Exit()

    if ctx.invoked_subcommand is None:
        from . import __version__

        console.print()
        console.print(Text(LOGO, style="bold cyan"))

        title_text = Text("HYPER-EXTRACT", style="bold cyan")
        version_text = Text(f"v{__version__}", style="dim white")
        desc_text = Text(
            "Transform document into knowledge-abstract", style="dim", no_wrap=True
        )

        header = Table(box=None, show_header=False, pad_edge=False)
        header.add_column(no_wrap=True)
        header.add_column(style="dim white", no_wrap=True)
        header.add_row(title_text, version_text)

        console.print(header)
        console.print(desc_text)
        console.print()

        from rich.rule import Rule

        console.print(Rule(style="cyan dim"))
        console.print()

        from rich.panel import Panel

        def make_section(title: str, commands: list[tuple[str, str]]) -> Panel:
            table = Table(box=None, show_header=False, pad_edge=False)
            table.add_column(style="green bold", no_wrap=True)
            table.add_column(style="white", no_wrap=True)
            for cmd, desc in commands:
                table.add_row(f"  {cmd}", desc)
            return Panel(
                table,
                title=f"[bold cyan]{title}[/]",
                border_style="cyan dim",
                padding=(0, 1),
                title_align="center",
                width=80,
            )

        sections = [
            make_section(
                "🚀 Getting Started",
                [
                    ("he list template", "List available templates"),
                    ("he list method", "List extraction methods"),
                    ("he config --help", "Manage LLM/Embedder config"),
                ],
            ),
            make_section(
                "✨ Create Knowledge Abstract (KA)",
                [
                    (
                        "he parse <input_document> -o <ka_path>",
                        "Extract KA from document",
                    ),
                    (
                        "he feed <ka_path> <input_document>",
                        "Add document to existing KA",
                    ),
                    ("he build-index <ka_path>", "Build semantic search index"),
                ],
            ),
            make_section(
                "🔍 Explore Knowledge Abstract (KA)",
                [
                    ("he info <ka_path>", "View KA info & stats"),
                    ("he talk <ka_path> [-i]", "Chat with KA"),
                    ("he search <ka_path> <query>", "Semantic search"),
                    ("he show <ka_path>", "Visualize KA"),
                    (
                        "he export obsidian <ka_path> -o <vault>",
                        "Export to Obsidian vault",
                    ),
                ],
            ),
        ]

        for section in sections:
            console.print(section)
        console.print()
        console.print(Rule(style="cyan dim"))
        console.print()

        hint_text = Text("💡 Tip: Run ", style="dim")
        hint_text.append("he --help", style="bold cyan")
        hint_text.append(" for detailed documentation", style="dim")
        console.print(hint_text)
        console.print()
        raise typer.Exit()


def select_template_interactive() -> Optional[str]:
    """Interactive template selection when user doesn't specify one."""
    templates = Gallery.list()

    if not templates:
        console.print("[yellow]No templates available.[/yellow]")
        return None

    template_list = list(templates.items())

    console.print()
    console.print("[bold cyan]Select a template:[/bold cyan]")
    console.print()

    for i, (path, cfg) in enumerate(template_list, 1):
        desc = cfg.description if cfg.description else ""
        if isinstance(desc, dict):
            desc = desc.get("zh", desc.get("en", ""))
        console.print(f"  [{i}] {path}")
        if desc:
            console.print(f"      {desc}")

    console.print()

    while True:
        choice = Prompt.ask(
            "Enter number or search keyword",
            default="1",
            show_default=True,
        )

        if choice.isdigit():
            idx = int(choice) - 1
            if 0 <= idx < len(template_list):
                return template_list[idx][0]
            else:
                console.print(
                    f"[red]Invalid number. Please choose 1-{len(template_list)}[/red]"
                )
        else:
            query_lower = choice.lower()
            matches = [
                (i, p, c)
                for i, (p, c) in enumerate(template_list)
                if query_lower in p.lower()
                or (c.description and query_lower in str(c.description).lower())
            ]

            if len(matches) == 1:
                return matches[0][1]
            elif len(matches) > 1:
                console.print(f"[yellow]Found {len(matches)} matches:[/yellow]")
                for i, path, cfg in matches:
                    console.print(f"  [{i + 1}] {path}")
                continue
            else:
                console.print("[yellow]No matches found. Try another keyword.[/yellow]")


@app.command(name="parse")
def parse(
    input: str = typer.Argument(
        ..., help="Input file path, directory, or '-' for stdin"
    ),
    output: str = typer.Option(..., "--output", "-o", help="Output directory"),
    template: Optional[str] = typer.Option(
        None, "--template", "-t", help="Template (omit for interactive selection)"
    ),
    method: Optional[str] = typer.Option(
        None, "--method", "-m", help="Method template (e.g., light_rag, hyper_rag)"
    ),
    lang: Optional[str] = typer.Option(
        None,
        "--lang",
        "-l",
        help="Language (zh/en). Required for knowledge templates, optional for methods (default: en)",
    ),
    force: bool = typer.Option(False, "--force", "-f", help="Force overwrite"),
    no_index: bool = typer.Option(
        False, "--no-index", help="Skip building search index"
    ),
    input_format: str = typer.Option(
        "auto",
        "--input-format",
        help="Input format: auto, text, docling-json, or document-package",
    ),
    resume: bool = typer.Option(
        True, "--resume/--no-resume", help="Resume a matching structured-document run"
    ),
    chunk_target_tokens: int = typer.Option(
        4000, "--chunk-target-tokens", help="Target tokens per section-aware chunk"
    ),
    chunk_max_tokens: int = typer.Option(
        6000, "--chunk-max-tokens", help="Maximum tokens per section-aware chunk"
    ),
    max_workers: int = typer.Option(
        2, "--max-workers", help="Maximum concurrent chunk extractions"
    ),
    retry_attempts: int = typer.Option(
        4, "--retry-attempts", help="Attempts for transient model/API failures"
    ),
    request_timeout: int = typer.Option(
        900, "--request-timeout", help="Timeout in seconds for one model request"
    ),
    heartbeat_interval: int = typer.Option(
        30, "--heartbeat-interval", help="Seconds between long-running heartbeats"
    ),
    model_context_tokens: int = typer.Option(
        32768,
        "--model-context-tokens",
        help="Model context window used for request budgeting",
    ),
    output_reserve_tokens: int = typer.Option(
        8192,
        "--output-reserve-tokens",
        help="Tokens reserved for one structured response",
    ),
    semantic_dedup: bool = typer.Option(
        True,
        "--semantic-dedup/--no-semantic-dedup",
        help="Use embeddings and model checks to merge synonymous knowledge points",
    ),
    community_reports: bool = typer.Option(
        False,
        "--community-reports/--no-community-reports",
        help="Generate model-written community summaries",
    ),
    combined_local_extraction: bool = typer.Option(
        False,
        "--combined-local-extraction/--separate-local-extraction",
        help="Extract chunk nodes and local edges in one structured model call",
    ),
    global_edge_top_k: int = typer.Option(
        1,
        "--global-edge-top-k",
        min=0,
        max=10,
        help="Maximum cross-section relation candidates retained per node",
    ),
    global_edge_similarity_threshold: float = typer.Option(
        0.70,
        "--global-edge-similarity-threshold",
        min=0.0,
        max=1.0,
        help="Minimum embedding similarity for global relation candidates",
    ),
    course_profile: Optional[str] = typer.Option(
        None,
        "--profile",
        help="CourseExtractionProfile YAML (course_knowledge_graph only)",
    ),
):
    """Extract knowledge from text to a new directory."""
    logger.info(
        "command=parse input=%s output=%s template=%s lang=%s",
        input,
        output,
        template or "auto",
        lang or "auto",
    )
    validate_config()
    logger.info("stage=config_validated")

    if method:
        template = f"method/{method}"
    elif template is None:
        template = select_template_interactive()
        if template is None:
            console.print("[red]No template selected. Exiting.[/red]")
            raise typer.Exit(1)

    is_method_template = template.startswith("method/")

    if is_method_template:
        method_config = Template.get(template)
        method_language = (
            getattr(method_config, "language", "en") if method_config else "en"
        )
        if lang is not None and lang != method_language:
            console.print(
                f"[dim]Note: This method uses {method_language} prompts. --lang is ignored.[/dim]"
            )
        lang = method_language
    elif lang is None:
        console.print(
            "[red]Error:[/red] --lang is required for knowledge templates. Use --lang en or --lang zh."
        )
        raise typer.Exit(1)

    output_path = Path(output)

    input_path = Path(input)
    structured_course_run = template == "method/course_knowledge_graph" and (
        input_format in {"docling-json", "document-package"}
        or (input_format == "auto" and str(input).lower().endswith(".json"))
        or (
            input_format == "auto"
            and input_path.is_dir()
            and (input_path / "manifest.json").is_file()
        )
    )

    if output_path.exists() and not force and not (structured_course_run and resume):
        if any(output_path.iterdir()):
            console.print(
                "[red]Error:[/red] Output directory already exists and is not empty. Use --force to overwrite."
            )
            raise typer.Exit(1)

    output_path.mkdir(parents=True, exist_ok=True)

    console.print(f"[blue]Input:[/blue] {input}")
    console.print(f"[blue]Output:[/blue] {output}")
    console.print(f"[blue]Template:[/blue] {template}")
    console.print(f"[blue]Language:[/blue] {lang}")
    console.print(f"[blue]Build Index:[/blue] {'No' if no_index else 'Yes'}")
    console.print()

    try:
        template_config = Template.get(template)
        if template_config is None:
            raise ValueError(f"Template '{template}' not found")
        console.print(f"[green]Template resolved:[/green] {template_config.name}")
        logger.info("stage=template_resolved template=%s", template_config.name)
    except ValueError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)

    if structured_course_run:
        if input_format == "document-package" and not input_path.is_dir():
            console.print(
                "[red]Error:[/red] Document Package input must be a directory."
            )
            raise typer.Exit(1)
        if input_format == "docling-json" and not input_path.is_file():
            console.print("[red]Error:[/red] Docling JSON input must be a file.")
            raise typer.Exit(1)
        os.environ["HYPER_EXTRACT_REQUEST_TIMEOUT"] = str(max(1, request_timeout))
        try:
            from hyperextract.documents.course_pipeline import (
                PipelineOptions,
                run_course_document,
            )
            from hyperextract.documents.document_package import (
                validate_document_package,
            )
            from hyperextract.methods.rag.course_knowledge_graph import (
                CourseKnowledgeGraph,
            )
            from hyperextract.profiles.course import load_course_profile

            resolved_package_input = input_format == "document-package" or (
                input_format == "auto"
                and input_path.is_dir()
                and (input_path / "manifest.json").is_file()
            )
            if resolved_package_input:
                validate_document_package(input_path)
                console.print("[green]Document Package validated.[/green]")
            else:
                console.print(
                    "[yellow]Migration note:[/yellow] docling-json is a compatibility input; "
                    "new integrations should produce a Document Package."
                )
            ka = Template.create(template, lang, max_workers=1)
            if not isinstance(ka, CourseKnowledgeGraph):
                raise TypeError(
                    "course_knowledge_graph method did not create the expected graph type"
                )
            if course_profile:
                loaded_profile = load_course_profile(course_profile)
                ka.apply_profile(loaded_profile)
                console.print(
                    "[green]Course profile:[/green] "
                    f"{loaded_profile.name} v{loaded_profile.version} "
                    f"({loaded_profile.content_hash[:12]})"
                )
            summary = run_course_document(
                input_path,
                output_path,
                ka,
                options=PipelineOptions(
                    target_tokens=chunk_target_tokens,
                    max_tokens=chunk_max_tokens,
                    max_workers=max_workers,
                    retry_attempts=retry_attempts,
                    heartbeat_interval=heartbeat_interval,
                    build_index=not no_index,
                    model_context_tokens=model_context_tokens,
                    output_reserve_tokens=output_reserve_tokens,
                    semantic_dedup=semantic_dedup,
                    community_reports=community_reports,
                    combined_local_extraction=combined_local_extraction,
                    global_edge_top_k=global_edge_top_k,
                    global_edge_similarity_threshold=global_edge_similarity_threshold,
                ),
                input_format=input_format,
                resume=resume,
                force=force,
            )
        except KeyboardInterrupt:
            console.print(
                "\n[yellow]Interrupted. Checkpoint saved; run the same command to resume.[/yellow]"
            )
            raise typer.Exit(130)
        except Exception as error:
            console.print(f"[red]Error:[/red] {error}")
            console.print(f"[dim]Result: {output_path / 'run-summary.json'}[/dim]")
            raise typer.Exit(1)
        console.print()
        console.print(
            f"[bold green]Success![/bold green] {summary['nodes']} nodes / {summary['edges']} edges"
        )
        console.print(f"[dim]Output: {output_path}[/dim]")
        return

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        task = progress.add_task("Creating template instance...", total=None)

        ka = Template.create(template, lang)
        logger.info("stage=template_created")

        if input_path.is_dir():
            progress.update(task, description="Processing directory...")
            text_files = list(input_path.glob("*.txt")) + list(input_path.glob("*.md"))
            if not text_files:
                console.print(
                    f"[red]Error:[/red] No .txt or .md files found in {input}"
                )
                raise typer.Exit(1)

            all_text = []
            for file_path in text_files:
                text = read_input(str(file_path))
                all_text.append(text)
                console.print(f"[dim]Loaded {file_path.name}: {len(text)} chars[/dim]")

            combined_text = "\n\n".join(all_text)
            console.print(f"[dim]Total input: {len(combined_text)} characters[/dim]")

            progress.update(task, description="Extracting knowledge...")
            logger.debug("stage=feed_text_invoked")
            ka.feed_text(combined_text)
            logger.info("stage=knowledge_extracted chars=%d", len(combined_text))
        else:
            progress.update(task, description="Reading input...")
            text = read_input(input)
            console.print(f"[dim]Input text: {len(text)} characters[/dim]")

            progress.update(task, description="Extracting knowledge...")
            logger.debug("stage=feed_text_invoked")
            ka.feed_text(text)
            logger.info("stage=knowledge_extracted chars=%d", len(text))

        progress.update(task, description="Saving data...")

        template_config = Template.get(template)
        if template_config is None:
            if template.endswith(".yaml"):
                import shutil

                filename = Path(template).name
                shutil.copy(template, output_path / filename)
                console.print(
                    f"[dim]Custom template '{filename}' saved to KA directory[/dim]"
                )

        ka.dump(output_path)
        logger.info("stage=data_saved output=%s", output_path)

        if not no_index:
            progress.update(task, description="Building search index...")
            ka.build_index()
            console.print("[dim]Index built successfully[/dim]")
            logger.info("stage=index_built")
            progress.update(task, description="Saving index...")
            ka.dump(output_path)
            logger.info("stage=index_saved")

    console.print()
    console.print(
        f"[bold green]Success![/bold green] Knowledge extracted to {output_path}"
    )
    console.print()
    if no_index:
        console.print("[dim]Note: Index was not built.[/dim]")
        console.print(
            f"[dim]  he build-index {output}       # Build index to enable search/talk[/dim]"
        )
        console.print(
            f"[dim]  he feed {output} <new_document>  # Append more documents[/dim]"
        )
    else:
        console.print("[dim]What's next?[/dim]")
        console.print(
            f"[dim]  he show {output}                    # Visualize knowledge graph[/dim]"
        )
        console.print(
            f"[dim]  he feed {output} <new_document>     # Append more documents[/dim]"
        )
        console.print(
            f'[dim]  he search {output} "keyword"        # Semantic search[/dim]'
        )
        console.print(
            f"[dim]  he talk {output} -i                 # Interactive chat[/dim]"
        )
        console.print(
            f'[dim]  he talk {output} -q "your question" # Single query[/dim]'
        )


@app.command(name="show")
def show(ka_path: str = typer.Argument(..., help="Knowledge Abstract directory")):
    """Visualize Knowledge Abstract using OntoSight."""
    logger.info("command=show ka_path=%s", ka_path)
    path = validate_ka_with_data(ka_path)

    template, lang = get_template_from_ka(path)

    console.print(f"[blue]Template:[/blue] {template}")
    console.print(f"[blue]Language:[/blue] {lang}")
    console.print()

    validate_config()

    with console.status("[bold blue]Loading Knowledge Abstract..."):
        try:
            ka = Template.create(template, lang)
            ka.load(path)

        except Exception as e:
            console.print(f"[red]Error loading Knowledge Abstract:[/red] {e}")
            raise typer.Exit(1)

    console.print("[bold blue]Visualizing with OntoSight...[/bold blue]")
    logger.info("stage=visualizing")

    try:
        ka.show()
        logger.info("stage=visualization_complete")
    except Exception as e:
        console.print(f"[red]Error during visualization:[/red] {e}")
        raise typer.Exit(1)

    console.print()
    console.print("[dim]Continue exploring:[/dim]")
    console.print(
        f'[dim]  he search {ka_path} "keyword"  # Search specific content[/dim]'
    )
    console.print(f"[dim]  he talk {ka_path} -i           # Interactive chat[/dim]")


export_app = typer.Typer(
    name="export",
    help="Export a Knowledge Abstract to other formats",
    no_args_is_help=True,
)


@export_app.command(name="obsidian")
def export_obsidian_cmd(
    ka_path: str = typer.Argument(..., help="Knowledge Abstract directory"),
    output: str = typer.Option(..., "--output", "-o", help="Output vault directory"),
    name: Optional[str] = typer.Option(
        None, "--name", help="Vault name used for the index note"
    ),
    no_index: bool = typer.Option(
        False, "--no-index", help="Skip writing the index/map-of-content note"
    ),
    force: bool = typer.Option(
        False, "--force", "-f", help="Write into an existing, non-empty directory"
    ),
):
    """Export a Knowledge Abstract to an Obsidian vault (Markdown + wikilinks)."""
    logger.info("command=export-obsidian ka_path=%s output=%s", ka_path, output)

    path = validate_ka_with_data(ka_path)
    template, lang = get_template_from_ka(path)

    output_path = Path(output)
    if output_path.exists() and any(output_path.iterdir()) and not force:
        console.print(
            "[red]Error:[/red] Output directory already exists and is not empty. "
            "Use --force to write into it."
        )
        raise typer.Exit(1)

    console.print(f"[blue]Knowledge Abstract:[/blue] {ka_path}")
    console.print(f"[blue]Template:[/blue] {template}")
    console.print(f"[blue]Output vault:[/blue] {output}")
    console.print()

    validate_config()

    vault_name = name or output_path.name or "Knowledge Vault"

    with console.status("[bold blue]Loading Knowledge Abstract..."):
        try:
            ka = Template.create(template, lang)
            ka.load(path)
        except Exception as e:
            console.print(f"[red]Error loading Knowledge Abstract:[/red] {e}")
            raise typer.Exit(1)

    if not hasattr(ka, "export_obsidian"):
        console.print(
            "[red]Error:[/red] Obsidian export is only supported for graph-type "
            "Knowledge Abstracts (graph, hypergraph, temporal/spatial graphs)."
        )
        raise typer.Exit(1)

    with console.status("[bold blue]Exporting to Obsidian vault..."):
        try:
            ka.export_obsidian(
                output_path,
                vault_name=vault_name,
                include_index=not no_index,
                overwrite=force,
            )
        except Exception as e:
            console.print(f"[red]Error during export:[/red] {e}")
            raise typer.Exit(1)

    note_count = len(list(output_path.glob("*.md")))
    console.print()
    console.print(
        f"[bold green]Success![/bold green] Exported {note_count} notes to {output_path}"
    )
    console.print()
    console.print("[dim]Open the folder as a vault in Obsidian to explore it.[/dim]")


app.add_typer(export_app, name="export")


@app.command(name="info")
def info(ka_path: str = typer.Argument(..., help="Knowledge Abstract directory")):
    """View Knowledge Abstract information and statistics."""
    logger.info("command=info ka_path=%s", ka_path)
    import json

    path = validate_ka_with_data(ka_path)

    metadata = load_ka_metadata(path)

    data_file = path / "data.json"
    with open(data_file, "r", encoding="utf-8") as f:
        data = json.load(f)

    if isinstance(data, dict):
        node_count = len(data.get("nodes", data.get("entities", [])))
        edge_count = len(data.get("edges", data.get("relations", [])))
    elif isinstance(data, list):
        node_count = len(data)
        edge_count = 0
    else:
        node_count = 0
        edge_count = 0

    index_exists = (path / "index").exists() and any((path / "index").iterdir())

    table = Table(title="Knowledge Abstract Info", show_header=False, box=None)
    table.add_column("Key", style="cyan", width=15)
    table.add_column("Value", style="green")

    table.add_row("Path", str(path))

    if metadata:
        table.add_row("Template", metadata.get("template", "unknown"))
        table.add_row("Language", metadata.get("lang", "unknown"))
        table.add_row("Created", metadata.get("created_at", "unknown"))
        table.add_row("Updated", metadata.get("updated_at", "unknown"))
    else:
        table.add_row("Template", "[yellow]unknown[/yellow]")
        table.add_row("Language", "[yellow]unknown[/yellow]")

    table.add_row("Nodes", str(node_count))
    table.add_row("Edges", str(edge_count))
    table.add_row(
        "Index", "[green]Built[/green]" if index_exists else "[red]Not Built[/red]"
    )

    console.print(table)


@app.command(name="search")
def search(
    ka_path: str = typer.Argument(..., help="Knowledge Abstract directory"),
    query: str = typer.Argument(..., help="Search query"),
    top_k: int = typer.Option(3, "--top-k", "-n", help="Number of results"),
):
    """Semantic search in Knowledge Abstract."""
    logger.info("command=search ka_path=%s query=%s top_k=%d", ka_path, query, top_k)
    import json

    validate_config()

    path = validate_ka_with_index(ka_path)
    template, lang = get_template_from_ka(path)

    console.print(f"[blue]Knowledge Abstract:[/blue] {ka_path}")
    console.print(f"[blue]Query:[/blue] {query}")
    console.print(f"[blue]Top K:[/blue] {top_k}")
    console.print()

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        task = progress.add_task("Searching...", total=None)

        try:
            ka = Template.create(template, lang)

            progress.update(task, description="Loading Knowledge Abstract...")
            ka.load(path)

            progress.update(task, description="Searching...")
            results = ka.search(query, top_k=top_k)
            logger.info("stage=search_complete results=%d", len(results))

        except Exception as e:
            console.print(f"[red]Error:[/red] {e}")
            raise typer.Exit(1)

    console.print()
    if not results:
        console.print("[yellow]No results found.[/yellow]")
    else:
        console.print(f"[bold green]Found {len(results)} result(s):[/bold green]")
        console.print()

        for i, result in enumerate(results, 1):
            console.print(f"[bold cyan]Result {i}:[/bold cyan]")
            if hasattr(result, "model_dump"):
                console.print_json(
                    json.dumps(result.model_dump(), indent=2, ensure_ascii=False)
                )
            elif hasattr(result, "dict"):
                console.print_json(
                    json.dumps(result.dict(), indent=2, ensure_ascii=False)
                )
            else:
                console.print(str(result))
            console.print()

    console.print("[dim]Continue:[/dim]")
    console.print(
        f'[dim]  he talk {ka_path} -q "question about results"  # Deep dive[/dim]'
    )
    console.print(
        f"[dim]  he talk {ka_path} -i                           # Interactive mode[/dim]"
    )
    console.print(
        f"[dim]  he show {ka_path}                              # Visualize[/dim]"
    )


def chat_loop(ka, ka_path: str):
    """Interactive chat loop."""
    console.print(
        "\n[bold green]Entering interactive mode. Type 'exit' or 'quit' to stop.[/bold green]\n"
    )
    while True:
        try:
            query = console.input("[bold cyan]>[/bold cyan] ")
            if query.lower() in ["exit", "quit", "q"]:
                console.print("\n[dim]Goodbye![/dim]")
                console.print()
                console.print("[dim]Other useful commands:[/dim]")
                console.print(
                    f"[dim]  he show {ka_path}              # Visualize[/dim]"
                )
                console.print(f'[dim]  he search {ka_path} "keyword"  # Search[/dim]')
                console.print(
                    f"[dim]  he info {ka_path}              # View info[/dim]"
                )
                break
            if not query.strip():
                continue
            response = ka.chat(query)
            console.print()
            console.print(response.content)
            console.print()
        except KeyboardInterrupt:
            console.print("\n[dim]Goodbye![/dim]")
            console.print()
            console.print("[dim]Other useful commands:[/dim]")
            console.print(f"[dim]  he show {ka_path}              # Visualize[/dim]")
            console.print(f'[dim]  he search {ka_path} "keyword"  # Search[/dim]')
            console.print(f"[dim]  he info {ka_path}              # View info[/dim]")
            break
        except Exception as e:
            console.print(f"[red]Error:[/red] {e}")


@app.command(name="talk")
def talk(
    ka_path: str = typer.Argument(..., help="Knowledge Abstract directory"),
    query: Optional[str] = typer.Option(None, "--query", "-q", help="Question to ask"),
    top_k: int = typer.Option(3, "--top-k", "-n", help="Number of context items"),
    interactive: bool = typer.Option(
        False, "--interactive", "-i", help="Interactive mode"
    ),
):
    """Chat with Knowledge Abstract."""
    logger.info(
        "command=talk ka_path=%s query=%s interactive=%s",
        ka_path,
        query or "loop",
        interactive,
    )
    validate_config()

    path = validate_ka_with_index(ka_path)
    template, lang = get_template_from_ka(path)

    if interactive:
        console.print(f"[blue]Knowledge Abstract:[/blue] {ka_path}")
        console.print(f"[blue]Template:[/blue] {template}")
        console.print(f"[blue]Top K:[/blue] {top_k}")
        console.print()
    elif query is None:
        console.print(
            "[red]Error:[/red] Please provide a query or use --interactive mode"
        )
        raise typer.Exit(1)
    else:
        console.print(f"[blue]Query:[/blue] {query}")
        console.print(f"[blue]Knowledge Abstract:[/blue] {ka_path}")
        console.print(f"[blue]Top K:[/blue] {top_k}")
        console.print()

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        task = progress.add_task("Loading...", total=None)

        try:
            ka = Template.create(template, lang)

            progress.update(task, description="Loading Knowledge Abstract...")
            ka.load(path)

        except Exception as e:
            console.print(f"[red]Error:[/red] {e}")
            raise typer.Exit(1)

    if interactive:
        chat_loop(ka, ka_path)
    else:
        with console.status("[bold blue]Thinking..."):
            try:
                response = ka.chat(query, top_k=top_k)
                console.print(response.content)

                if response.additional_kwargs.get("retrieved_items"):
                    console.print()
                    console.print("[dim]Retrieved context:[/dim]")
                    items = response.additional_kwargs["retrieved_items"]
                    for i, item in enumerate(items, 1):
                        console.print(f"[dim]{i}. {str(item)[:100]}...[/dim]")
            except Exception as e:
                console.print(f"[red]Error:[/red] {e}")
                raise typer.Exit(1)

        console.print()
        console.print("[dim]Continue:[/dim]")
        console.print(
            f"[dim]  he talk {ka_path} -i           # Enter interactive mode[/dim]"
        )
        console.print(f'[dim]  he search {ka_path} "keyword"  # Search more[/dim]')
        console.print(f"[dim]  he show {ka_path}              # Visualize[/dim]")


@app.command(name="feed")
def feed(
    ka_path: str = typer.Argument(..., help="Knowledge Abstract directory"),
    input: str = typer.Argument(..., help="Input file path or '-' for stdin"),
    template: Optional[str] = typer.Option(None, "--template", "-t", help="Template"),
    lang: Optional[str] = typer.Option(None, "--lang", "-l", help="Language"),
):
    """Append knowledge to an existing Knowledge Abstract."""
    logger.info("command=feed ka_path=%s input=%s", ka_path, input)
    validate_config()

    output_path = validate_ka_path(ka_path)

    metadata = load_ka_metadata(output_path)
    if not metadata:
        console.print(
            f"[red]Error:[/red] Not a valid Knowledge Abstract directory: {ka_path}"
        )
        raise typer.Exit(1)

    if template is None:
        template = metadata.get("template", "general/graph")
    if lang is None:
        lang = metadata.get("lang", "zh")

    console.print(f"[blue]Knowledge Abstract:[/blue] {ka_path}")
    console.print(f"[blue]Input:[/blue] {input}")
    console.print(f"[blue]Template:[/blue] {template} (from metadata)")
    console.print(f"[blue]Language:[/blue] {lang} (from metadata)")
    console.print()

    try:
        ka = Template.create(template, lang)
        console.print(f"[green]Template loaded:[/green] {template}")
    except ValueError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        task = progress.add_task("Loading existing knowledge...", total=None)

        ka.load(output_path)

        progress.update(task, description="Reading input...")
        text = read_input(input)
        console.print(f"[dim]Input text: {len(text)} characters[/dim]")

        progress.update(task, description="Appending knowledge...")
        logger.debug("stage=feed_text_invoked")
        ka.feed_text(text)
        logger.info("stage=knowledge_appended chars=%d", len(text))

        progress.update(task, description="Saving data...")
        ka.dump(output_path)
        logger.info("stage=data_saved")

    console.print()
    console.print(
        f"[bold green]Success![/bold green] Knowledge appended to {output_path}"
    )
    console.print()
    console.print("[dim]Next steps:[/dim]")
    console.print(f"[dim]  he show {ka_path}              # Visualize[/dim]")
    console.print(
        f"[dim]  he build-index {ka_path}       # Rebuild index (if needed)[/dim]"
    )


@app.command(name="build-index")
def build_index(
    ka_path: str = typer.Argument(..., help="Knowledge Abstract directory"),
    force: bool = typer.Option(False, "--force", "-f", help="Force rebuild"),
):
    """Build vector index for Knowledge Abstract."""
    logger.info("command=build-index ka_path=%s force=%s", ka_path, force)
    validate_config()

    path = validate_ka_with_data(ka_path)

    index_dir = path / "index"
    if index_dir.exists() and any(index_dir.iterdir()) and not force:
        console.print(
            "[yellow]Warning:[/yellow] Index already exists. Use --force to rebuild."
        )
        console.print(f"[dim]Index location: {index_dir}[/dim]")
        raise typer.Exit(0)

    template, lang = get_template_from_ka(path)

    console.print(f"[blue]Template:[/blue] {template}")
    console.print(f"[blue]Language:[/blue] {lang}")
    console.print()

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        task = progress.add_task("Initializing...", total=None)

        try:
            ka = Template.create(template, lang)

            progress.update(task, description="Loading Knowledge Abstract...")
            ka.load(path)

            if force:
                console.print("[dim]Force rebuild: clearing existing index...[/dim]")
                ka.clear_index()

            progress.update(task, description="Building index...")
            ka.build_index()
            logger.info("stage=index_built")

            progress.update(task, description="Saving index...")
            ka.dump(path)
            logger.info("stage=index_saved")

        except Exception as e:
            console.print(f"[red]Error:[/red] {e}")
            raise typer.Exit(1)

    console.print()
    console.print(f"[bold green]Success![/bold green] Index built for {ka_path}")
    console.print()
    console.print("[dim]Now you can:[/dim]")
    console.print(f'[dim]  he search {ka_path} "keyword"  # Semantic search[/dim]')
    console.print(f"[dim]  he talk {ka_path} -i           # Interactive chat[/dim]")


@app.command(name="clean")
def clean(
    ka_path: str = typer.Argument(..., help="Knowledge Abstract directory"),
    all_: bool = typer.Option(
        False,
        "--all",
        "-a",
        help="Remove the ENTIRE Knowledge Abstract (data, metadata, and index)",
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt"),
):
    """Clean a Knowledge Abstract: remove its search index, or the whole KA with --all."""
    import shutil

    logger.info("command=clean ka_path=%s all=%s", ka_path, all_)

    # validate_ka_with_data ensures the target really is a KA (has data.json),
    # so --all never rmtree's an arbitrary mistyped directory.
    path = validate_ka_with_data(ka_path)

    if all_:
        target = path
        what = f"the ENTIRE Knowledge Abstract '{path}' (data, metadata, index)"
    else:
        target = path / "index"
        if not target.exists() or not any(target.iterdir()):
            console.print("[yellow]Nothing to clean:[/yellow] no index found.")
            console.print(
                f"[dim]Tip: use --all to remove the whole KA at {path}.[/dim]"
            )
            raise typer.Exit(0)
        what = f"the search index of '{path}'"

    console.print(f"[yellow]This will permanently delete[/yellow] {what}.")
    if not yes and not typer.confirm("Are you sure?"):
        console.print("[dim]Aborted. Nothing was deleted.[/dim]")
        raise typer.Exit(0)

    try:
        shutil.rmtree(target)
    except Exception as e:
        console.print(f"[red]Error:[/red] Failed to delete {target}: {e}")
        raise typer.Exit(1)

    console.print(f"[bold green]Cleaned![/bold green] Removed {target}")
    if not all_:
        console.print(f"[dim]Rebuild it with: he build-index {ka_path}[/dim]")
