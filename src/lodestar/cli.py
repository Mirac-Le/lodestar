"""Typer-powered CLI."""

from __future__ import annotations

from getpass import getpass
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
from lodestar.search import HybridSearch, PathFinder, build_reranker_from_settings
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


def _resolve_owner(repo: Repository, slug: str | None):
    """统一的 owner 解析：单 owner 库自动选；多 owner 必须显式 --owner。

    返回 ``Owner`` 对象（已校验 slug 存在）。失败时直接 ``typer.Exit``。
    多 owner 时不让算法静默走"第一个 owner"，否则 search/path 会跑在
    错误网络里——CLI 用户也是人，不应该比 web 端少这一层防护。
    """
    owners = repo.list_owners()
    if not owners:
        console.print("[red]No owners exist. Run `lodestar init` first.[/red]")
        raise typer.Exit(code=1)
    if slug is None:
        if len(owners) == 1:
            return owners[0]
        console.print(
            f"[red]Multiple owners exist; pass --owner one of: "
            f"{', '.join(o.slug for o in owners)}.[/red]"
        )
        raise typer.Exit(code=1)
    owner_obj = repo.get_owner_by_slug(slug)
    if owner_obj is None:
        console.print(
            f"[red]Owner [cyan]{slug}[/cyan] not found. Existing: "
            f"{', '.join(o.slug for o in owners)}.[/red]"
        )
        raise typer.Exit(code=1)
    return owner_obj


# --------------------------------------------------------------------- init
@app.command()
def init(
    name: Annotated[str | None, typer.Option(help="Your display name.")] = None,
    bio: Annotated[str | None, typer.Option(help="Short self-description.")] = None,
    slug: Annotated[
        str, typer.Option(help="Owner slug (URL-safe). Defaults to 'me'.")
    ] = "me",
) -> None:
    """Initialize the database and create the first owner.

    Multiple owners can coexist (Richard / Tommy / ...). Use
    `lodestar owner add` to register additional owners after init.
    """
    repo, _ = _open_repo()
    existing = repo.get_owner_by_slug(slug)
    if existing:
        console.print(
            f"[yellow]Owner [cyan]{slug}[/cyan] already initialised as "
            f"[cyan]{existing.display_name}[/cyan].[/yellow]"
        )
        return
    if name is None:
        name = Prompt.ask("Display name")
    owner = repo.ensure_owner(slug=slug, display_name=name, bio=bio)
    console.print(
        f"[green]Initialized.[/green] Database: [dim]{get_settings().db_path}[/dim]"
    )
    console.print(
        f"[green]Owner:[/green] [cyan]{owner.display_name}[/cyan] "
        f"(slug={owner.slug}, me_id={owner.me_person_id})"
    )


# --------------------------------------------------------------- owner mgmt
owner_app = typer.Typer(
    name="owner",
    help="Manage network owners (each gets their own `me` and subgraph).",
    no_args_is_help=True,
)
app.add_typer(owner_app, name="owner")


@owner_app.command("add")
def owner_add(
    slug: Annotated[str, typer.Argument(help="URL-safe slug, e.g. 'tommy'.")],
    display: Annotated[str, typer.Option("--display", help="Display name shown in the UI.")],
    bio: Annotated[str | None, typer.Option(help="Optional self-description for `me`.")] = None,
    color: Annotated[
        str | None, typer.Option(help="Optional accent hex color, e.g. '#7a8b9c'.")
    ] = None,
) -> None:
    """Register a new owner (creates their `me` person row)."""
    repo, _ = _open_repo()
    if repo.get_owner_by_slug(slug):
        console.print(f"[yellow]Owner [cyan]{slug}[/cyan] already exists.[/yellow]")
        return
    owner = repo.ensure_owner(
        slug=slug, display_name=display, bio=bio, accent_color=color
    )
    console.print(
        f"[green]Owner added:[/green] [cyan]{owner.display_name}[/cyan] "
        f"(slug={owner.slug}, me_id={owner.me_person_id})"
    )


@owner_app.command("list")
def owner_list() -> None:
    """List all owners."""
    repo, _ = _open_repo()
    owners = repo.list_owners()
    if not owners:
        console.print("[dim]No owners yet. Run `lodestar init` to create one.[/dim]")
        return
    table = Table(title=f"Owners ({len(owners)})")
    table.add_column("Slug", style="cyan")
    table.add_column("Display name")
    table.add_column("Me ID", justify="right")
    table.add_column("Contacts", justify="right")
    for o in owners:
        n = len(repo.list_people(owner_id=o.id))
        table.add_row(o.slug, o.display_name, str(o.me_person_id), str(n))
    console.print(table)


@owner_app.command("web-password")
def owner_web_password(
    slug: Annotated[str, typer.Argument(help="Owner slug, e.g. richard.")],
    new_password: Annotated[
        str | None,
        typer.Option("--set", help="New password (omit to type interactively)."),
    ] = None,
    clear: Annotated[
        bool, typer.Option("--clear", help="Remove the web UI lock for this owner.")
    ] = False,
) -> None:
    """Set or clear the password for an owner's tab in the web UI (`serve`)."""
    repo, _ = _open_repo()
    owner = repo.get_owner_by_slug(slug)
    if owner is None or owner.id is None:
        console.print(f"[red]Owner '{slug}' not found.[/red]")
        raise typer.Exit(1)
    if clear:
        repo.set_owner_web_password(owner.id, None)
        console.print(
            f"[green]Removed web lock for[/green] [cyan]{slug}[/cyan]."
        )
        return
    pw = new_password
    if not pw:
        pw = getpass("New web password: ")
        pw2 = getpass("Again: ")
        if pw != pw2:
            console.print("[red]Passwords do not match.[/red]")
            raise typer.Exit(1)
    if not pw:
        console.print(
            "[red]Empty password; use [bold]--clear[/bold] to remove lock.[/red]"
        )
        raise typer.Exit(1)
    repo.set_owner_web_password(owner.id, pw)
    console.print(f"[green]Web password set for[/green] [cyan]{slug}[/cyan].")


# -------------------------------------------------------------------- reset
@app.command()
def reset(
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation.")] = False,
) -> None:
    """Wipe the entire database file. Useful when reshaping schema or seeding demo data."""
    settings = get_settings()
    db_path = Path(settings.db_path)
    if not db_path.exists():
        console.print(f"[dim]No database at {db_path}.[/dim]")
        return
    if not yes and not Confirm.ask(f"Delete [red]{db_path}[/red]?"):
        return
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(db_path) + suffix)
        if p.exists():
            p.unlink()
    console.print(f"[green]Removed[/green] {db_path} (and WAL/SHM siblings).")


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
    owner: Annotated[
        str | None,
        typer.Option(
            "--owner",
            help="Owner slug (e.g. richard / tommy). Required when more than one owner exists.",
        ),
    ] = None,
) -> None:
    """Find the best people and paths for achieving a goal."""
    repo, _ = _open_repo()
    owner_obj = _resolve_owner(repo, owner)
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

    search = HybridSearch(repo=repo, embedder=embedder, owner_id=owner_obj.id)
    candidates = search.search(
        intent, top_k=settings.top_k, recall_k=settings.reranker_recall_k
    )

    if not candidates:
        console.print("[yellow]No candidates found. Try adding more contacts or a different goal.[/yellow]")
        return

    reranker = build_reranker_from_settings()
    candidates = reranker.rerank(intent, candidates, repo)[: settings.top_k]

    finder = PathFinder(
        repo=repo,
        max_hops=settings.max_hops,
        owner_id=owner_obj.id,
        weak_me_floor=settings.weak_me_floor,
    )
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
            help=(
                "Column preset: 'extended' (default, reads 公司/城市/认识), "
                "'richard' (Richard 的 8 列 richard_network.xlsx), or "
                "'tommy' (Tommy 的 16 列 tommy_network.xlsx)."
            ),
        ),
    ] = "extended",
    owner: Annotated[
        str | None,
        typer.Option(
            "--owner",
            help="Owner slug to attribute these contacts to. Defaults to the first owner.",
        ),
    ] = None,
) -> None:
    """Bulk-import contacts. Format auto-detected by file extension."""
    from lodestar.importers import (
        extended_network_preset,
        richard_finance_preset,
        tommy_contacts_preset,
    )

    preset_map = {
        "extended": extended_network_preset,
        "richard": richard_finance_preset,
        "tommy": tommy_contacts_preset,
        # Backwards-compatible alias for anyone still typing the old name.
        # Safe to remove once nobody is on the v0.1 CLI anymore.
        "finance": richard_finance_preset,
    }

    repo, _ = _open_repo()

    # Resolve owner; we MUST have one because every contact needs to be
    # attached via person_owner so it shows up in that owner's tab.
    owner_obj = repo.get_owner_by_slug(owner) if owner else None
    if owner_obj is None:
        owners = repo.list_owners()
        if not owners:
            console.print(
                "[red]No owners exist. Run `lodestar init` (or `lodestar owner add`) first.[/red]"
            )
            raise typer.Exit(code=1)
        if owner is not None:
            console.print(f"[red]Owner '{owner}' not found.[/red]")
            raise typer.Exit(code=1)
        owner_obj = owners[0]
        console.print(
            f"[dim]No --owner given; using first owner [cyan]{owner_obj.slug}[/cyan].[/dim]"
        )

    suffix = path.suffix.lower()
    if suffix in {".xlsx", ".xls", ".xlsm"}:
        if preset not in preset_map:
            console.print(
                f"[red]Unknown preset '{preset}'. Choose one of: "
                f"{', '.join(preset_map)}.[/red]"
            )
            raise typer.Exit(code=1)
        mapping = preset_map[preset]()
        xl = ExcelImporter(
            repo, mapping=mapping,
            infer_colleagues=infer_colleagues,
            owner_id=owner_obj.id,
        )
        stats = xl.import_with_stats(path)
        console.print(
            f"[green]Imported[/green] {stats.people} contacts · "
            f"{stats.peer_edges} peer edges (from 认识/关系) · "
            f"{stats.colleague_edges} colleague edges (inferred) "
            f"into owner [cyan]{owner_obj.slug}[/cyan] "
            f"from [cyan]{path.name}[/cyan]."
        )
    elif suffix == ".csv":
        count = CSVImporter(repo, owner_id=owner_obj.id).import_file(path)
        console.print(
            f"[green]Imported[/green] {count} contacts into owner "
            f"[cyan]{owner_obj.slug}[/cyan] from {path.name}."
        )
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


# --------------------------------------------------------------------- enrich
@app.command()
def enrich(
    owner: Annotated[
        str | None,
        typer.Option("--owner", help="Owner slug. Defaults to the only owner if there's one."),
    ] = None,
    limit: Annotated[
        int | None,
        typer.Option("--limit", help="Process at most N contacts (useful for smoke tests)."),
    ] = None,
    only_missing: Annotated[
        bool,
        typer.Option(
            "--only-missing/--all",
            help="Only enrich rows missing companies/cities (default). Use --all to re-process every row.",
        ),
    ] = True,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run/--apply",
            help="--dry-run prints what would be added without writing. --apply persists.",
        ),
    ] = True,
    show_n: Annotated[
        int,
        typer.Option(
            "--show", help="In dry-run mode, print this many sample diffs in full."
        ),
    ] = 10,
) -> None:
    """Use the configured LLM (Qwen via DashScope) to backfill structured
    attributes (companies / cities / titles / tags) on each contact.

    Privacy: every request anonymizes in-table person names → Pxxx tokens
    before the prompt leaves the machine. The reverse map lives only in
    process memory.
    """
    from lodestar.enrich import L1Extractor, LLMClient, LLMError

    repo, _ = _open_repo()
    owners = repo.list_owners()
    if not owners:
        console.print("[red]No owners exist. Run `lodestar init` first.[/red]")
        raise typer.Exit(code=1)

    if owner is None:
        if len(owners) == 1:
            owner_obj = owners[0]
        else:
            console.print(
                f"[red]Multiple owners exist; pass --owner one of: "
                f"{', '.join(o.slug for o in owners)}.[/red]"
            )
            raise typer.Exit(code=1)
    else:
        owner_obj = repo.get_owner_by_slug(owner)
        if owner_obj is None:
            console.print(f"[red]Owner '{owner}' not found.[/red]")
            raise typer.Exit(code=1)

    assert owner_obj.id is not None
    try:
        client = LLMClient()
    except LLMError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc

    console.print(
        f"[dim]Owner: [cyan]{owner_obj.slug}[/cyan] · model: "
        f"[cyan]{client.model}[/cyan] · only_missing={only_missing} · "
        f"dry_run={dry_run}[/dim]"
    )

    extractor = L1Extractor(repo, owner_id=owner_obj.id, client=client)
    with console.status("LLM 抽取中..."):
        results = extractor.run(limit=limit, only_missing=only_missing)

    n_total = len(results)
    n_err = sum(1 for r in results if r.error)
    n_change = sum(1 for r in results if not r.error and not r.is_empty())
    n_noop = n_total - n_err - n_change

    console.print(
        f"[bold]L1 result[/bold]: 处理 {n_total} 条 · 有补全 {n_change} 条 · "
        f"无变化 {n_noop} 条 · 失败 {n_err} 条"
    )

    sample = [r for r in results if not r.error and not r.is_empty()][:show_n]
    if sample:
        table = Table(title=f"Sample diffs (前 {len(sample)} 条)", show_lines=True)
        table.add_column("姓名", style="cyan", no_wrap=True)
        table.add_column("+companies", style="green")
        table.add_column("+cities", style="green")
        table.add_column("+titles", style="yellow")
        table.add_column("+tags", style="magenta")
        for r in sample:
            table.add_row(
                r.name,
                ", ".join(r.add_companies) or "—",
                ", ".join(r.add_cities) or "—",
                ", ".join(r.add_titles) or "—",
                ", ".join(r.add_tags) or "—",
            )
        console.print(table)

    if n_err:
        for r in results:
            if r.error:
                console.print(f"[red]  ✗ {r.name}: {r.error}[/red]")

    if dry_run:
        console.print(
            "[dim]Dry-run，未写库。确认效果后用 [bold]--apply[/bold] 重跑同一命令即可写入。[/dim]"
        )
        return

    touched = extractor.apply(results)
    console.print(f"[green]Applied[/green] · 实际更新 {touched} 条 person 行。")


# --------------------------------------------------------- infer-colleagues
@app.command(name="infer-colleagues")
def infer_colleagues_cmd(
    owner: Annotated[
        str | None,
        typer.Option(
            "--owner",
            help="Owner slug. Required when more than one owner exists.",
        ),
    ] = None,
    strength: Annotated[
        int,
        typer.Option(
            "--strength",
            min=1,
            max=5,
            help="Edge strength assigned to inferred colleague pairs (1-5).",
        ),
    ] = 4,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run/--apply",
            help="--dry-run prints what would be added; --apply writes.",
        ),
    ] = True,
) -> None:
    """Re-build same-company peer edges for an owner from the current
    `person.companies` field (idempotent, safe to re-run).

    Useful right after `enrich`: L1 backfills companies from free text,
    so a follow-up `infer-colleagues` materializes the implied
    "two contacts at the same firm" edges. Manual edges are never
    downgraded — provenance hierarchy in the repository protects them.
    """
    from lodestar.importers import infer_colleague_edges_for_owner

    repo, _ = _open_repo()
    owners = repo.list_owners()
    if not owners:
        console.print("[red]No owners exist. Run `lodestar init` first.[/red]")
        raise typer.Exit(code=1)

    if owner is None:
        if len(owners) == 1:
            owner_obj = owners[0]
        else:
            console.print(
                f"[red]Multiple owners exist; pass --owner one of: "
                f"{', '.join(o.slug for o in owners)}.[/red]"
            )
            raise typer.Exit(code=1)
    else:
        owner_obj = repo.get_owner_by_slug(owner)
        if owner_obj is None:
            console.print(f"[red]Owner '{owner}' not found.[/red]")
            raise typer.Exit(code=1)
    assert owner_obj.id is not None

    cliques, edges, top = infer_colleague_edges_for_owner(
        repo,
        owner_id=owner_obj.id,
        strength=strength,
        dry_run=dry_run,
    )

    verb = "Would add" if dry_run else "Added"
    console.print(
        f"[bold]Owner[/bold] [cyan]{owner_obj.slug}[/cyan] · 同事推断: "
        f"{cliques} 家公司 ≥2 人 · {verb} {edges} 条 [magenta]colleague_inferred[/magenta] 边 "
        f"(strength={strength})"
    )
    if top:
        table = Table(title=f"Top cliques (前 {len(top)})", show_lines=False)
        table.add_column("公司", style="cyan", no_wrap=True)
        table.add_column("人数", justify="right", style="green")
        table.add_column("将连边数", justify="right", style="dim")
        for company, n in top:
            table.add_row(company, str(n), str(n * (n - 1) // 2))
        console.print(table)
    if dry_run:
        console.print(
            "[dim]Dry-run，未写库。确认效果后用 [bold]--apply[/bold] 重跑同一命令即可写入。"
            "已有 manual 边不会被覆盖。[/dim]"
        )


# -------------------------------------------------------- normalize-companies
@app.command(name="normalize-companies")
def normalize_companies_cmd(
    owner: Annotated[
        str | None,
        typer.Option(
            "--owner",
            help="Owner slug. Required when more than one owner exists.",
        ),
    ] = None,
    alias_file: Annotated[
        Path | None,
        typer.Option(
            "--alias-file",
            help="JSON/YAML file with `{canonical: [aliases]}` "
                 "or `[{canonical, aliases}]`. Trumps --builtin on conflict.",
            exists=True, file_okay=True, dir_okay=False, readable=True,
        ),
    ] = None,
    use_builtin: Annotated[
        bool,
        typer.Option(
            "--builtin/--no-builtin",
            help="Use the built-in 中国金融机构 alias map "
                 "(国泰海通 / 申万宏源 / 中金 ...). Default on.",
        ),
    ] = True,
    use_llm: Annotated[
        bool,
        typer.Option(
            "--use-llm",
            help="Also ask the LLM to cluster the remaining company names. "
                 "Output is reviewed in dry-run before any write.",
        ),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run/--apply",
            help="--dry-run prints planned merges; --apply writes.",
        ),
    ] = True,
) -> None:
    """Merge alias / merger-renamed company rows so colleague inference
    can connect contacts that ended up under different writings of the
    same employer (e.g. 国泰君安 ↔ 国泰海通).

    Three contributing alias sources, all combined:

      1. Built-in map (公开合并改名 + 缩写). Toggle with --no-builtin.
      2. Optional user file (--alias-file). Wins over builtin.
      3. Optional LLM clustering (--use-llm). Fills the gap; never
         overrides 1 or 2.

    Always dry-run first. Re-run `lodestar infer-colleagues --apply`
    afterwards to materialise the new same-company peer edges.
    """
    from lodestar.enrich import (
        AliasGroup,
        LLMClient,
        LLMError,
        build_groups,
        cluster_with_llm,
        load_alias_file,
    )

    repo, _ = _open_repo()
    owners = repo.list_owners()
    if not owners:
        console.print("[red]No owners exist. Run `lodestar init` first.[/red]")
        raise typer.Exit(code=1)

    if owner is None:
        if len(owners) == 1:
            owner_obj = owners[0]
        else:
            console.print(
                f"[red]Multiple owners exist; pass --owner one of: "
                f"{', '.join(o.slug for o in owners)}.[/red]"
            )
            raise typer.Exit(code=1)
    else:
        owner_obj = repo.get_owner_by_slug(owner)
        if owner_obj is None:
            console.print(f"[red]Owner '{owner}' not found.[/red]")
            raise typer.Exit(code=1)
    assert owner_obj.id is not None

    rows = repo.list_owner_companies(owner_obj.id)
    if not rows:
        console.print(
            f"[yellow]Owner [cyan]{owner_obj.slug}[/cyan] 下没有任何 person_company "
            "记录；先跑 import / enrich 再回来。[/yellow]"
        )
        return
    present = {name: n for _cid, name, n in rows}

    user_aliases: dict[str, list[str]] | None = None
    if alias_file is not None:
        try:
            user_aliases = load_alias_file(alias_file)
        except Exception as exc:  # noqa: BLE001
            console.print(f"[red]读取 alias 文件失败：{exc}[/red]")
            raise typer.Exit(code=1) from exc
        console.print(
            f"[dim]从 {alias_file} 读取 {len(user_aliases)} 条用户 alias 规则。[/dim]"
        )

    llm_groups: list[AliasGroup] = []
    if use_llm:
        try:
            client = LLMClient()
        except LLMError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from exc
        # Strip names already covered by deterministic sources, so the LLM
        # only spends tokens on the residual.
        deterministic = build_groups(
            present=present,
            builtin=use_builtin,
            user_aliases=user_aliases,
        )
        already_claimed = {m for g in deterministic for m in g.members()}
        residual = [n for n in present if n not in already_claimed]
        console.print(
            f"[dim]LLM 二次清洗：builtin/file 已覆盖 {len(already_claimed)} 个名字，"
            f"剩下 {len(residual)} 个交给 LLM (model={client.model})...[/dim]"
        )
        with console.status("LLM 公司聚类中..."):
            try:
                llm_groups = cluster_with_llm(residual, client=client)
            except LLMError as exc:
                console.print(f"[red]LLM 聚类失败：{exc}[/red]")
                raise typer.Exit(code=1) from exc

    groups = build_groups(
        present=present,
        builtin=use_builtin,
        user_aliases=user_aliases,
        llm_groups=llm_groups,
    )

    if not groups:
        console.print(
            f"[green]Owner [cyan]{owner_obj.slug}[/cyan] 没有发现任何 alias 合并机会。[/green]"
        )
        return

    n_present = len(present)
    n_after = n_present - sum(len(g.aliases) for g in groups)
    console.print(
        f"[bold]Owner[/bold] [cyan]{owner_obj.slug}[/cyan] · "
        f"将 {n_present} 个公司名归并为 {n_after} 个 "
        f"({n_present - n_after} 条 alias)"
    )

    table = Table(title=f"Planned merges ({len(groups)} 组)", show_lines=True)
    table.add_column("canonical", style="cyan", no_wrap=True)
    table.add_column("aliases →", style="yellow")
    table.add_column("人数", justify="right", style="green")
    table.add_column("来源", style="magenta", no_wrap=True)
    for g in groups:
        table.add_row(
            g.canonical,
            ", ".join(f"{a}({present.get(a, 0)})" for a in g.aliases),
            str(g.headcount),
            g.source,
        )
    console.print(table)

    if dry_run:
        console.print(
            "[dim]Dry-run，未写库。确认无误后用 [bold]--apply[/bold] 重跑同一命令；"
            "之后再跑 [bold]lodestar infer-colleagues --apply[/bold] "
            "把新合并出的同事关系物化成 peer 边。[/dim]"
        )
        return

    total_reassigned = 0
    total_deleted = 0
    for g in groups:
        reassigned, deleted = repo.merge_companies(
            g.canonical,
            g.aliases,
            owner_id=owner_obj.id,
        )
        total_reassigned += reassigned
        total_deleted += deleted

    console.print(
        f"[green]Applied[/green] · 合并了 [bold]{total_deleted}[/bold] 个 alias 行 · "
        f"重定向 [bold]{total_reassigned}[/bold] 条 person_company 关联。"
    )
    console.print(
        "[dim]接下来：[bold]uv run lodestar infer-colleagues --owner "
        f"{owner_obj.slug} --apply[/bold] 把新挖出的同事关系连成 peer 边。[/dim]"
    )


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
    owner: Annotated[
        str | None,
        typer.Option(
            "--owner",
            help="Owner slug whose network drives the goal-based highlighting.",
        ),
    ] = None,
) -> None:
    """Render the network as an interactive cyberpunk HTML graph."""
    from lodestar.viz import GraphExporter

    repo, _ = _open_repo()
    settings = get_settings()
    owner_obj = _resolve_owner(repo, owner) if goal else None

    highlighted: list = []
    title = "Network"
    if goal:
        assert owner_obj is not None
        title = f"Goal: {goal}"
        intent = _make_intent(goal, no_llm=no_llm)
        try:
            embedder = get_embedding_client()
        except Exception:
            embedder = None
        candidates = HybridSearch(
            repo=repo, embedder=embedder, owner_id=owner_obj.id,
        ).search(intent, top_k=settings.top_k, recall_k=settings.reranker_recall_k)
        if candidates:
            reranker = build_reranker_from_settings()
            candidates = reranker.rerank(intent, candidates, repo)[: settings.top_k]
            highlighted = PathFinder(
                repo=repo,
                max_hops=settings.max_hops,
                owner_id=owner_obj.id,
                weak_me_floor=settings.weak_me_floor,
            ).rank(candidates)[:top]

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
        owners = repo.list_owners()
    finally:
        repo.conn.close()
    if not owners:
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
