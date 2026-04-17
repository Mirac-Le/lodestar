"""Rich-based rendering helpers."""

from __future__ import annotations

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from lodestar.models import PathResult, Person

console = Console()


def render_person(person: Person) -> None:
    """Pretty-print a single person's profile."""
    lines: list[str] = []
    if person.bio:
        lines.append(f"[bold]Bio[/bold]: {person.bio}")
    if person.tags:
        lines.append(f"[bold]Tags[/bold]: {', '.join(person.tags)}")
    if person.skills:
        lines.append(f"[bold]Skills[/bold]: {', '.join(person.skills)}")
    if person.companies:
        lines.append(f"[bold]Companies[/bold]: {', '.join(person.companies)}")
    if person.cities:
        lines.append(f"[bold]Cities[/bold]: {', '.join(person.cities)}")
    if person.needs:
        lines.append(f"[bold magenta]Needs[/bold magenta]: {', '.join(person.needs)}")
    if person.notes:
        lines.append(f"[bold]Notes[/bold]: {person.notes}")
    body = "\n".join(lines) if lines else "[dim]No attributes recorded[/dim]"
    console.print(Panel(body, title=f"[cyan]{person.name}[/cyan]", border_style="cyan"))


def render_paths(results: list[PathResult], goal: str, top_n: int = 5) -> None:
    """Show ranked path recommendations."""
    if not results:
        console.print("[yellow]No matching contacts found for this goal.[/yellow]")
        return

    console.print(Panel(f"[bold]Goal:[/bold] {goal}", border_style="magenta"))

    table = Table(title="Top recommendations", show_lines=True)
    table.add_column("#", justify="right", style="dim")
    table.add_column("Target", style="bold cyan")
    table.add_column("Path", style="white")
    table.add_column("Score", justify="right", style="green")
    table.add_column("Why", style="dim")

    for idx, r in enumerate(results[:top_n], start=1):
        path_text = Text()
        for step_idx, step in enumerate(r.path):
            if step_idx > 0:
                arrow = "━━"
                if step.strength is not None:
                    arrow = f"━[{step.strength}]━"
                path_text.append(f" {arrow}> ", style="dim")
            style = "bold cyan" if step_idx == len(r.path) - 1 else "yellow"
            path_text.append(step.name, style=style)
            if step.relation_from_previous:
                path_text.append(f" ({step.relation_from_previous})", style="dim italic")

        table.add_row(
            str(idx),
            r.target.name,
            path_text,
            f"{r.combined_score:.3f}",
            r.rationale,
        )
    console.print(table)
