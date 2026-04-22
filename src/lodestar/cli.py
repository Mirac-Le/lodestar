"""Typer-powered CLI.

一人一库（v4）：所有命令都只面对**一个 db 文件**。无 ``--owner``，无 owner
子命令；用全局 ``--db`` 切换数据库（或 ``LODESTAR_DB_PATH`` 环境变量）。
多人共用同一进程在 web 层用 ``serve --mount slug=path``，CLI 层不混合。
"""

from __future__ import annotations

import os
from getpass import getpass
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.prompt import Confirm, IntPrompt, Prompt
from rich.table import Table

from lodestar.config import get_settings, reset_settings
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


@app.callback()
def _global_options(
    db: Annotated[
        Path | None,
        typer.Option(
            "--db",
            help="Path to the SQLite db file. Overrides LODESTAR_DB_PATH / default.",
            envvar="LODESTAR_DB_PATH",
        ),
    ] = None,
) -> None:
    """Global flags applied to every subcommand."""
    if db is not None:
        # Settings is a module-level singleton; reset so the new --db wins
        # over any cached value from earlier process state (e.g. pytest).
        os.environ["LODESTAR_DB_PATH"] = str(db)
        reset_settings()


def _open_repo() -> tuple[Repository, int]:
    settings = get_settings()
    conn = connect(settings.db_path)
    init_schema(conn, embedding_dim=settings.embedding_dim)
    return Repository(conn), settings.embedding_dim


# --------------------------------------------------------------------- init
@app.command()
def init(
    name: Annotated[str | None, typer.Option(help="Your display name.")] = None,
    bio: Annotated[str | None, typer.Option(help="Short self-description.")] = None,
    color: Annotated[
        str | None,
        typer.Option(
            "--color",
            help="Accent hex color shown in the web tab, e.g. '#7a8b9c'. Optional.",
        ),
    ] = None,
) -> None:
    """Initialize this database file and create the singleton ``me`` person.

    一人一库：每个 db 文件只有一位 owner（``person.is_me=1``，UNIQUE 约束）。
    要给别人也加一个网络，新建另一个 db 路径再 ``lodestar --db <path> init``。
    """
    repo, _ = _open_repo()
    existing = repo.get_me()
    if existing is not None:
        console.print(
            f"[yellow]Already initialised as [cyan]{existing.name}[/cyan] "
            f"at [dim]{get_settings().db_path}[/dim].[/yellow]"
        )
        return
    if name is None:
        name = Prompt.ask("Display name")
    me = repo.ensure_me(name=name, bio=bio)
    if color:
        repo.accent_color = color
    console.print(f"[green]Initialized.[/green] Database: [dim]{get_settings().db_path}[/dim]")
    console.print(f"[green]Me:[/green] [cyan]{me.name}[/cyan] (id={me.id})")


# ---------------------------------------------------------- web-password
@app.command("web-password")
def web_password_cmd(
    new_password: Annotated[
        str | None,
        typer.Option("--set", help="New password (omit to type interactively)."),
    ] = None,
    clear: Annotated[
        bool,
        typer.Option("--clear", help="Remove the web UI lock for this database."),
    ] = False,
    status: Annotated[
        bool,
        typer.Option("--status", help="Print whether a password is currently set."),
    ] = False,
) -> None:
    """Set / clear / inspect the web UI password for this db file.

    Empty password = unlocked tab. Hash uses PBKDF2-HMAC-SHA256 with a fresh
    16-byte salt; verification is constant-time.
    """
    repo, _ = _open_repo()
    if status:
        if repo.web_password_hash:
            console.print(f"[green]Locked[/green] · {get_settings().db_path}")
        else:
            console.print(f"[yellow]Unlocked[/yellow] · {get_settings().db_path}")
        return
    if clear:
        repo.set_web_password(None)
        console.print(f"[green]Removed web lock[/green] for {get_settings().db_path}.")
        return
    pw = new_password
    if not pw:
        pw = getpass("New web password: ")
        pw2 = getpass("Again: ")
        if pw != pw2:
            console.print("[red]Passwords do not match.[/red]")
            raise typer.Exit(1)
    if not pw:
        console.print("[red]Empty password; use [bold]--clear[/bold] to remove lock.[/red]")
        raise typer.Exit(1)
    repo.set_web_password(pw)
    console.print(f"[green]Web password set[/green] for {get_settings().db_path}.")


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
    candidates = search.search(intent, top_k=settings.top_k, recall_k=settings.reranker_recall_k)

    if not candidates:
        console.print(
            "[yellow]No candidates found. Try adding more contacts or a different goal.[/yellow]"
        )
        return

    reranker = build_reranker_from_settings()
    candidates = reranker.rerank(intent, candidates, repo)[: settings.top_k]

    finder = PathFinder(
        repo=repo,
        max_hops=settings.max_hops,
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
        str | None,
        typer.Option(
            help=(
                "Column preset (xlsx 必填): "
                "'richard' → richard_network.xlsx 这类 13 列通用表 "
                "（template.xlsx / demo_network.xlsx 也用它）; "
                "'tommy' → tommy 的 16 列机构合作画像表。"
            ),
        ),
    ] = None,
) -> None:
    """Bulk-import contacts into the current db file. Format auto-detected."""
    from lodestar.importers import (
        richard_network_preset,
        tommy_contacts_preset,
    )

    preset_map = {
        "richard": richard_network_preset,
        "tommy": tommy_contacts_preset,
    }

    repo, _ = _open_repo()

    me = repo.get_me()
    if me is None:
        console.print("[red]Run `lodestar init` first.[/red]")
        raise typer.Exit(code=1)

    suffix = path.suffix.lower()
    if suffix in {".xlsx", ".xls", ".xlsm"}:
        if preset is None:
            console.print(
                f"[red]--preset 必填（xlsx 导入）。[/red] 可选值: {', '.join(preset_map)}."
            )
            raise typer.Exit(code=1)
        if preset not in preset_map:
            console.print(
                f"[red]Unknown preset '{preset}'. Choose one of: {', '.join(preset_map)}.[/red]"
            )
            raise typer.Exit(code=1)
        mapping = preset_map[preset]()
        xl = ExcelImporter(
            repo,
            mapping=mapping,
            infer_colleagues=infer_colleagues,
        )
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


# --------------------------------------------------------------------- enrich
@app.command()
def enrich(
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
        typer.Option("--show", help="In dry-run mode, print this many sample diffs in full."),
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
    me = repo.get_me()
    if me is None:
        console.print("[red]Run `lodestar init` first.[/red]")
        raise typer.Exit(code=1)

    try:
        client = LLMClient()
    except LLMError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc

    console.print(
        f"[dim]DB: [cyan]{get_settings().db_path}[/cyan] · model: "
        f"[cyan]{client.model}[/cyan] · only_missing={only_missing} · "
        f"dry_run={dry_run}[/dim]"
    )

    extractor = L1Extractor(repo, client=client)
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
    """Re-build same-company peer edges from the current `person.companies`
    field (idempotent, safe to re-run).

    Useful right after `enrich`: L1 backfills companies from free text,
    so a follow-up `infer-colleagues` materializes the implied
    "two contacts at the same firm" edges. Manual edges are never
    downgraded — provenance hierarchy in the repository protects them.
    """
    from lodestar.importers import infer_colleague_edges

    repo, _ = _open_repo()
    if repo.get_me() is None:
        console.print("[red]Run `lodestar init` first.[/red]")
        raise typer.Exit(code=1)

    cliques, edges, top = infer_colleague_edges(
        repo,
        strength=strength,
        dry_run=dry_run,
    )

    verb = "Would add" if dry_run else "Added"
    console.print(
        f"[bold]同事推断[/bold]: {cliques} 家公司 ≥2 人 · "
        f"{verb} {edges} 条 [magenta]colleague_inferred[/magenta] 边 "
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
    alias_file: Annotated[
        Path | None,
        typer.Option(
            "--alias-file",
            help="JSON/YAML file with `{canonical: [aliases]}` "
            "or `[{canonical, aliases}]`. Trumps --builtin on conflict.",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
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
    if repo.get_me() is None:
        console.print("[red]Run `lodestar init` first.[/red]")
        raise typer.Exit(code=1)

    rows = repo.list_companies()
    if not rows:
        console.print(
            "[yellow]当前 db 没有任何 person_company 记录；先跑 import / enrich 再回来。[/yellow]"
        )
        return
    present = {name: n for _cid, name, n in rows}

    user_aliases: dict[str, list[str]] | None = None
    if alias_file is not None:
        try:
            user_aliases = load_alias_file(alias_file)
        except Exception as exc:
            console.print(f"[red]读取 alias 文件失败：{exc}[/red]")
            raise typer.Exit(code=1) from exc
        console.print(f"[dim]从 {alias_file} 读取 {len(user_aliases)} 条用户 alias 规则。[/dim]")

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
        console.print("[green]当前 db 没有发现任何 alias 合并机会。[/green]")
        return

    n_present = len(present)
    n_after = n_present - sum(len(g.aliases) for g in groups)
    console.print(
        f"[bold]当前 db[/bold] · 将 {n_present} 个公司名归并为 {n_after} 个 "
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
        reassigned, deleted = repo.merge_companies(g.canonical, g.aliases)
        total_reassigned += reassigned
        total_deleted += deleted

    console.print(
        f"[green]Applied[/green] · 合并了 [bold]{total_deleted}[/bold] 个 alias 行 · "
        f"重定向 [bold]{total_reassigned}[/bold] 条 person_company 关联。"
    )
    console.print(
        "[dim]接下来：[bold]uv run lodestar infer-colleagues --apply[/bold] "
        "把新挖出的同事关系连成 peer 边。[/dim]"
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
) -> None:
    """Render the network as an interactive HTML graph."""
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
        candidates = HybridSearch(
            repo=repo,
            embedder=embedder,
        ).search(intent, top_k=settings.top_k, recall_k=settings.reranker_recall_k)
        if candidates:
            reranker = build_reranker_from_settings()
            candidates = reranker.rerank(intent, candidates, repo)[: settings.top_k]
            highlighted = PathFinder(
                repo=repo,
                max_hops=settings.max_hops,
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
    mount: Annotated[
        list[str] | None,
        typer.Option(
            "--mount",
            "-m",
            help=(
                "挂载一个网络: ``slug=path/to/lodestar.db``。可重复多次，例如 "
                "``--mount richard=./richard.db --mount tommy=./tommy.db``。"
                "不传时挂载全局 ``--db`` / 默认路径，slug 取数据库文件名 stem。"
            ),
        ),
    ] = None,
    open_browser: Annotated[
        bool, typer.Option("--open/--no-open", help="Auto-open browser.")
    ] = True,
    reload: Annotated[bool, typer.Option(help="Hot reload (dev). Conflicts with --mount.")] = False,
) -> None:
    """Launch the interactive web UI at http://HOST:PORT/.

    每个 ``--mount`` 在 URL 前缀 ``/r/<slug>/`` 下独立运行：拥有自己的 db、
    自己的 ``me``、自己的 web 密码（``lodestar --db <path> web-password``
    设置）；切到另一个 tab **强制重新解锁**。
    """
    import json
    import threading
    import webbrowser

    import uvicorn

    mounts: list[dict[str, str]] = []
    seen: set[str] = set()
    if mount:
        for spec in mount:
            if "=" not in spec:
                console.print(f"[red]--mount 必须形如 slug=path，收到 [bold]{spec}[/bold][/red]")
                raise typer.Exit(1)
            slug, _, raw_path = spec.partition("=")
            slug = slug.strip()
            db_path = Path(raw_path.strip()).expanduser().resolve()
            if not slug or not slug.replace("-", "").replace("_", "").isalnum():
                console.print(
                    f"[red]Slug 必须是 URL-safe (字母/数字/-/_)，收到 [bold]{slug}[/bold][/red]"
                )
                raise typer.Exit(1)
            if slug in seen:
                console.print(f"[red]重复的 mount slug: [bold]{slug}[/bold][/red]")
                raise typer.Exit(1)
            if not db_path.exists():
                console.print(f"[red]--mount {slug} 指向的 db 不存在: [bold]{db_path}[/bold][/red]")
                raise typer.Exit(1)
            mounts.append({"slug": slug, "db_path": str(db_path)})
            seen.add(slug)
    else:
        default_path = Path(get_settings().db_path)
        if not default_path.exists():
            console.print(
                f"[red]Default db 不存在: [bold]{default_path}[/bold]。"
                "先 `lodestar init`，或用 `--mount slug=path`。[/red]"
            )
            raise typer.Exit(1)
        mounts.append({"slug": "me", "db_path": str(default_path)})

    if reload and len(mounts) > 1:
        console.print(
            "[red]--reload 与多 mount 不兼容（reload 走 import-string 工厂，"
            "无法把动态 mount 传给子进程）。去掉 --reload 或改用单 mount。[/red]"
        )
        raise typer.Exit(1)

    os.environ["LODESTAR_MOUNTS_JSON"] = json.dumps(mounts, ensure_ascii=False)

    url = f"http://{host}:{port}/"
    console.print(f"[bold green]★ Lodestar serving at[/bold green] [cyan]{url}[/cyan]")
    for m in mounts:
        console.print(f"  · /r/{m['slug']}/  [dim]→ {m['db_path']}[/dim]")
    console.print("[dim]Press Ctrl+C to stop.[/dim]")

    if open_browser:
        threading.Timer(1.2, lambda: webbrowser.open_new_tab(url)).start()

    uvicorn.run(
        "lodestar.web.app:create_app",
        host=host,
        port=port,
        factory=True,
        reload=reload,
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
