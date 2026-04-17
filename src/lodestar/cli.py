"""Typer-powered CLI."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.prompt import Confirm, IntPrompt, Prompt
from rich.table import Table

from lodestar.config import get_settings
from lodestar.db import Repository, connect, init_schema
from lodestar.embedding import get_embedding_client
from lodestar.importers import CSVImporter, ExcelImporter
from lodestar.llm import GoalParser, get_llm_client
from lodestar.models import Frequency, Person, Relationship
from lodestar.search import HybridSearch, PathFinder
from lodestar.ui import render_paths, render_person

app = typer.Typer(
    name="lodestar",
    help="Your personal network navigator. Tell it what you want; it finds the path.",
    no_args_is_help=True,
    add_completion=False,
    rich_markup_mode="rich",
)
console = Console()


def _open_repo() -> tuple[Repository, int]:
    settings = get_settings()
    conn = connect(settings.db_path)
    init_schema(conn, embedding_dim=settings.embedding_dim)
    return Repository(conn), settings.embedding_dim


# --------------------------------------------------------------------- init
@app.command()
def init(
    name: Annotated[str | None, typer.Option(help="Your own name.")] = None,
    bio: Annotated[str | None, typer.Option(help="Short self-description.")] = None,
) -> None:
    """Initialize the database and create your own 'me' record."""
    repo, _ = _open_repo()
    existing = repo.get_me()
    if existing:
        console.print(f"[yellow]Already initialized as [cyan]{existing.name}[/cyan].[/yellow]")
        return
    if name is None:
        name = Prompt.ask("Your name")
    me = repo.ensure_me(name=name, bio=bio)
    console.print(
        f"[green]Initialized.[/green] Database: [dim]{get_settings().db_path}[/dim]"
    )
    console.print(f"[green]You are:[/green] [cyan]{me.name}[/cyan] (id={me.id})")


# ---------------------------------------------------------------- add person
@app.command(name="add")
def add_person(
    name: Annotated[str | None, typer.Option(help="Contact name.")] = None,
    embed: Annotated[bool, typer.Option(help="Generate embedding from bio.")] = True,
) -> None:
    """Interactively add a new contact and a relationship to 'me'."""
    repo, _ = _open_repo()
    me = repo.get_me()
    if me is None or me.id is None:
        console.print("[red]Run `lodestar init` first.[/red]")
        raise typer.Exit(code=1)

    if name is None:
        name = Prompt.ask("Name")
    bio = Prompt.ask("Bio / background", default="")
    tags = _prompt_list("Tags (semicolon-separated)")
    skills = _prompt_list("Skills (semicolon-separated)")
    companies = _prompt_list("Companies (semicolon-separated)")
    cities = _prompt_list("Cities (semicolon-separated)")
    notes = Prompt.ask("Private notes", default="")

    strength = IntPrompt.ask("Closeness 1-5", default=3)
    context = Prompt.ask("How you know them", default="")
    frequency_raw = Prompt.ask(
        "Contact frequency",
        choices=[f.value for f in Frequency],
        default=Frequency.YEARLY.value,
    )

    person = Person(
        name=name,
        bio=bio or None,
        notes=notes or None,
        tags=tags,
        skills=skills,
        companies=companies,
        cities=cities,
    )
    saved = repo.add_person(person)
    assert saved.id is not None

    repo.add_relationship(
        Relationship(
            source_id=me.id,
            target_id=saved.id,
            strength=max(1, min(5, strength)),
            context=context or None,
            frequency=Frequency(frequency_raw),
        )
    )

    if embed and saved.bio:
        try:
            embedder = get_embedding_client()
            vector = embedder.embed(_embed_text(saved))
            repo.upsert_embedding(saved.id, vector)
            console.print("[dim]Embedded bio.[/dim]")
        except Exception as exc:
            console.print(f"[yellow]Embedding skipped: {exc}[/yellow]")

    console.print(f"[green]Added[/green] [cyan]{saved.name}[/cyan] (id={saved.id})")


# --------------------------------------------------------------------- find
@app.command()
def find(
    goal: Annotated[str, typer.Argument(help="Natural-language goal, e.g. '我想做AI投资'.")],
    top: Annotated[int, typer.Option("--top", "-n", help="Number of results.")] = 5,
    no_llm: Annotated[
        bool, typer.Option("--no-llm", help="Skip LLM parsing; use raw goal as keywords.")
    ] = False,
) -> None:
    """Find the best people and paths for achieving a goal."""
    repo, _ = _open_repo()
    settings = get_settings()

    if no_llm:
        from lodestar.models import GoalIntent

        intent = GoalIntent(original=goal, keywords=[goal], summary=goal)
    else:
        try:
            parser = GoalParser(get_llm_client())
            intent = parser.parse(goal)
        except Exception as exc:
            console.print(f"[yellow]LLM parsing failed ({exc}); falling back to raw goal.[/yellow]")
            from lodestar.models import GoalIntent

            intent = GoalIntent(original=goal, keywords=[goal], summary=goal)

    try:
        embedder = get_embedding_client()
    except Exception:
        embedder = None

    search = HybridSearch(repo=repo, embedder=embedder)
    candidates = search.search(intent, top_k=settings.top_k)

    if not candidates:
        console.print("[yellow]No candidates found. Try adding more contacts or a different goal.[/yellow]")
        return

    finder = PathFinder(repo=repo, max_hops=settings.max_hops)
    results = finder.rank(candidates)
    render_paths(results, goal=goal, top_n=top)


# ---------------------------------------------------------------------- list
@app.command(name="list")
def list_people() -> None:
    """List all contacts."""
    repo, _ = _open_repo()
    people = repo.list_people()
    if not people:
        console.print("[dim]No contacts yet. Use `lodestar add` or `lodestar import`.[/dim]")
        return
    table = Table(title=f"Contacts ({len(people)})")
    table.add_column("ID", justify="right")
    table.add_column("Name", style="cyan")
    table.add_column("Tags")
    table.add_column("Needs", style="magenta")
    for p in people:
        table.add_row(
            str(p.id),
            p.name,
            ", ".join(p.tags[:4]),
            ", ".join(p.needs[:3]),
        )
    console.print(table)


# ---------------------------------------------------------------------- show
@app.command()
def show(name: Annotated[str, typer.Argument(help="Name of the contact to show.")]) -> None:
    """Show a single contact's profile."""
    repo, _ = _open_repo()
    person = repo.find_person_by_name(name)
    if person is None:
        console.print(f"[red]No contact named '{name}'.[/red]")
        raise typer.Exit(code=1)
    render_person(person)


# -------------------------------------------------------------------- delete
@app.command(name="delete")
def delete_person(
    name: Annotated[str, typer.Argument(help="Name of the contact to delete.")],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation.")] = False,
) -> None:
    """Delete a contact."""
    repo, _ = _open_repo()
    person = repo.find_person_by_name(name)
    if person is None or person.id is None:
        console.print(f"[red]No contact named '{name}'.[/red]")
        raise typer.Exit(code=1)
    if not yes and not Confirm.ask(f"Delete [cyan]{person.name}[/cyan]?"):
        return
    repo.delete_person(person.id)
    console.print(f"[green]Deleted[/green] {person.name}.")


# -------------------------------------------------------------------- import
@app.command(name="import")
def import_spreadsheet(
    path: Annotated[Path, typer.Argument(help="Path to .csv, .xlsx, or .xls file.")],
    embed: Annotated[bool, typer.Option(help="Generate embeddings for imported rows.")] = False,
    infer_colleagues: Annotated[
        bool,
        typer.Option(
            "--infer-colleagues/--no-infer-colleagues",
            help="Auto-connect people sharing a company (strength 4).",
        ),
    ] = True,
    preset: Annotated[
        str,
        typer.Option(
            help="Column preset: 'extended' (default, reads 公司/城市/认识), "
                 "or 'finance' (legacy pyq.xlsx).",
        ),
    ] = "extended",
) -> None:
    """Bulk-import contacts. Format auto-detected by file extension."""
    from lodestar.importers import (
        chinese_finance_preset,
        extended_network_preset,
    )

    repo, _ = _open_repo()
    suffix = path.suffix.lower()
    if suffix in {".xlsx", ".xls", ".xlsm"}:
        mapping = (
            extended_network_preset()
            if preset == "extended"
            else chinese_finance_preset()
        )
        xl = ExcelImporter(repo, mapping=mapping, infer_colleagues=infer_colleagues)
        stats = xl.import_with_stats(path)
        console.print(
            f"[green]Imported[/green] {stats.people} contacts · "
            f"{stats.peer_edges} peer edges (from 认识/关系) · "
            f"{stats.colleague_edges} colleague edges (inferred) "
            f"from [cyan]{path.name}[/cyan]."
        )
    elif suffix == ".csv":
        count = CSVImporter(repo).import_file(path)
        console.print(f"[green]Imported[/green] {count} contacts from {path.name}.")
    else:
        console.print(f"[red]Unsupported file type: {suffix}[/red]")
        raise typer.Exit(code=1)

    if embed:
        embedder = get_embedding_client()
        people = repo.list_people()
        with console.status(f"Embedding {len(people)} bios...") as status:
            for idx, p in enumerate(people, start=1):
                if not p.bio or p.id is None:
                    continue
                try:
                    vec = embedder.embed(_embed_text(p))
                    repo.upsert_embedding(p.id, vec)
                except Exception as exc:
                    console.print(f"[yellow]Skipped {p.name}: {exc}[/yellow]")
                status.update(f"Embedding {idx}/{len(people)}: {p.name}")
        console.print("[green]Embedding complete.[/green]")


# --------------------------------------------------------------- re-embed all
@app.command(name="reembed")
def reembed_all() -> None:
    """Recompute embeddings for every contact with a bio."""
    repo, _ = _open_repo()
    embedder = get_embedding_client()
    people = [p for p in repo.list_people() if p.bio and p.id is not None]
    if not people:
        console.print("[dim]Nothing to embed (no bios).[/dim]")
        return
    with console.status(f"Embedding {len(people)} bios...") as status:
        for idx, p in enumerate(people, start=1):
            assert p.id is not None
            vec = embedder.embed(_embed_text(p))
            repo.upsert_embedding(p.id, vec)
            status.update(f"{idx}/{len(people)}: {p.name}")
    console.print(f"[green]Re-embedded {len(people)} contacts.[/green]")


# ----------------------------------------------------------------------- viz
@app.command()
def viz(
    goal: Annotated[
        str | None,
        typer.Argument(help="Optional goal; matching paths get highlighted."),
    ] = None,
    output: Annotated[
        Path,
        typer.Option("--output", "-o", help="Output HTML file."),
    ] = Path("lodestar-graph.html"),
    top: Annotated[int, typer.Option("--top", "-n", help="Number of paths to highlight.")] = 5,
    no_llm: Annotated[
        bool, typer.Option("--no-llm", help="Skip LLM parsing for the goal.")
    ] = False,
    open_browser: Annotated[
        bool, typer.Option("--open/--no-open", help="Auto-open the HTML in your browser.")
    ] = True,
) -> None:
    """Render the network as an interactive cyberpunk HTML graph."""
    from lodestar.viz import GraphExporter

    repo, _ = _open_repo()
    settings = get_settings()

    highlighted: list = []
    title = "Network"
    if goal:
        title = f"Goal: {goal}"
        intent = _make_intent(goal, no_llm=no_llm)
        try:
            embedder = get_embedding_client()
        except Exception:
            embedder = None
        candidates = HybridSearch(repo=repo, embedder=embedder).search(
            intent, top_k=settings.top_k
        )
        if candidates:
            highlighted = PathFinder(repo=repo, max_hops=settings.max_hops).rank(
                candidates
            )[:top]

    exporter = GraphExporter(repo)
    out = exporter.export(output.resolve(), highlighted=highlighted, title=title)
    console.print(f"[green]Graph written to[/green] [cyan]{out}[/cyan]")
    if highlighted:
        console.print(f"[dim]Highlighted {len(highlighted)} path(s) for goal: {goal}[/dim]")
    if open_browser:
        import webbrowser

        webbrowser.open_new_tab(out.as_uri())


def _make_intent(goal: str, no_llm: bool):  # type: ignore[no-untyped-def]
    from lodestar.models import GoalIntent

    if no_llm:
        return GoalIntent(original=goal, keywords=[goal], summary=goal)
    try:
        return GoalParser(get_llm_client()).parse(goal)
    except Exception as exc:
        console.print(f"[yellow]LLM parsing failed ({exc}); using raw goal.[/yellow]")
        return GoalIntent(original=goal, keywords=[goal], summary=goal)


# --------------------------------------------------------------------- serve
@app.command()
def serve(
    host: Annotated[str, typer.Option(help="Bind host.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Bind port.")] = 8765,
    open_browser: Annotated[
        bool, typer.Option("--open/--no-open", help="Auto-open browser.")
    ] = True,
    reload: Annotated[bool, typer.Option(help="Hot reload (dev).")] = False,
) -> None:
    """Launch the interactive web UI at http://HOST:PORT/"""
    import uvicorn

    repo, _ = _open_repo()
    try:
        me = repo.get_me()
    finally:
        repo.conn.close()
    if me is None:
        console.print("[red]Run `lodestar init` first.[/red]")
        raise typer.Exit(code=1)

    url = f"http://{host}:{port}/"
    console.print(f"[bold green]★ Lodestar serving at[/bold green] [cyan]{url}[/cyan]")
    console.print("[dim]Press Ctrl+C to stop.[/dim]")

    if open_browser:
        import threading
        import webbrowser

        threading.Timer(1.2, lambda: webbrowser.open_new_tab(url)).start()

    uvicorn.run(
        "lodestar.web.app:create_app",
        host=host, port=port, factory=True, reload=reload,
        log_level="info",
    )


# --------------------------------------------------------------------- stats
@app.command()
def stats() -> None:
    """Show database statistics."""
    repo, _ = _open_repo()
    people = repo.list_people()
    rels = repo.list_relationships()
    me = repo.get_me()
    console.print("[bold]Lodestar database[/bold]")
    console.print(f"  Path: [dim]{get_settings().db_path}[/dim]")
    console.print(f"  Owner: {me.name if me else '[red]NOT INITIALIZED[/red]'}")
    console.print(f"  Contacts: [cyan]{len(people)}[/cyan]")
    console.print(f"  Relationships: [cyan]{len(rels)}[/cyan]")


# -------------------------------------------------------------------- helpers
def _prompt_list(label: str) -> list[str]:
    raw = Prompt.ask(label, default="")
    return [x.strip() for x in raw.split(";") if x.strip()]


def _embed_text(p: Person) -> str:
    """Compose a textual representation of a person for embedding."""
    parts: list[str] = [p.name]
    if p.bio:
        parts.append(p.bio)
    if p.tags:
        parts.append("Tags: " + ", ".join(p.tags))
    if p.skills:
        parts.append("Skills: " + ", ".join(p.skills))
    if p.companies:
        parts.append("Companies: " + ", ".join(p.companies))
    if p.cities:
        parts.append("Cities: " + ", ".join(p.cities))
    if p.needs:
        parts.append("Needs: " + ", ".join(p.needs))
    return " | ".join(parts)


if __name__ == "__main__":
    app()
