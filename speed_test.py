#!/usr/bin/env python3
"""
Internet Speed Tester — single-file CLI with a beautiful frontend-like UI.

Runs N sequential HTTP GET requests to a URL, measures per-request time and
downloaded bytes, and prints the average speed in MB/s with a rich, animated
terminal UI.

Usage:
    python3 speed_test.py                              # will prompt for URL
    python3 speed_test.py https://host/big.jpg         # 10 requests (default)
    python3 speed_test.py https://host/big.jpg -n 20   # custom request count

Runs on any Python 3.7+ — the only 3rd-party dep (`rich`) is auto-installed
into the current interpreter the first time the script runs.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# 1. Bootstrap — ensure `rich` is importable, install it if not.
#    Kept intentionally above the third-party imports so a fresh interpreter
#    with no `rich` still runs the script with a single command.
# ---------------------------------------------------------------------------
import subprocess
import sys


def _bootstrap() -> None:
    """Ensure `rich` is importable; auto-install via pip if missing."""
    try:
        import rich  # noqa: F401
        return
    except ImportError:
        pass

    print("[speed_test] Installing 'rich' for the pretty UI (one-time)…",
          flush=True)

    pip_base = [sys.executable, "-m", "pip", "install", "--quiet"]
    attempts = (
        ["rich>=13.0.0"],
        ["--user", "rich>=13.0.0"],
        ["--break-system-packages", "rich>=13.0.0"],
    )
    last_output = ""

    for extras in attempts:
        result = subprocess.run(
            pip_base + extras, capture_output=True, text=True,
        )
        if result.returncode != 0:
            last_output = (result.stdout or "") + (result.stderr or "")
            continue

        _refresh_import_paths()
        try:
            import rich  # noqa: F401
            return
        except ImportError:
            last_output = "pip reported success but 'import rich' still fails"
            continue

    sys.stderr.write(
        "\nCould not install 'rich' automatically.\n"
        f"Please run:  {sys.executable} -m pip install rich\n"
        + (f"\npip output:\n{last_output}\n" if last_output else "")
    )
    sys.exit(1)


def _refresh_import_paths() -> None:
    """After a `--user` install, `sys.path` may need to see the new site dir."""
    import importlib
    import site

    user_site = site.getusersitepackages()
    if user_site and user_site not in sys.path:
        sys.path.insert(0, user_site)
    importlib.invalidate_caches()


_bootstrap()

# ---------------------------------------------------------------------------
# 2. Real imports (`rich` is now guaranteed to be present).
# ---------------------------------------------------------------------------
import argparse
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from statistics import mean, median
from typing import List, Optional, Sequence, Tuple

from rich import box
from rich.align import Align
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table
from rich.text import Text


# ---------------------------------------------------------------------------
# 3. Constants — all knobs in one place.
# ---------------------------------------------------------------------------
DEFAULT_REQUEST_COUNT = 10
REQUEST_TIMEOUT_SEC = 60.0
CHUNK_SIZE = 64 * 1024                # bytes per socket read()
UI_REFRESH_HZ = 10
TABLE_GAUGE_WIDTH = 24
FINAL_GAUGE_WIDTH = 42
SPARKLINE_WIDTH = 24
ERROR_MSG_MAX_LEN = 32
SPARK_CHARS = " ▁▂▃▄▅▆▇█"
USER_AGENT = "speed_test.py/1.0 (+python)"

console: Console = Console()


# ---------------------------------------------------------------------------
# 4. Data model.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class RequestResult:
    """Outcome of a single HTTP download."""
    ok: bool
    elapsed_sec: float = 0.0
    n_bytes: int = 0
    status: int = 0
    error: str = ""

    @property
    def speed_bps(self) -> float:
        """Bytes per second (0 when the request failed or had no timing)."""
        if not self.ok or self.elapsed_sec <= 0:
            return 0.0
        return self.n_bytes / self.elapsed_sec


# ---------------------------------------------------------------------------
# 5. Formatting helpers — pure, side-effect free.
# ---------------------------------------------------------------------------
def humanize_bytes(n: float) -> str:
    """Format a byte count as a human-readable string ('1.18 MB')."""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.2f} {unit}"
        n /= 1024
    return f"{n:.2f} GB"


def humanize_speed(bps: float) -> str:
    """Format bytes-per-second as MB/s (or KB/s for slow speeds)."""
    mb = bps / (1024 * 1024)
    if mb >= 0.01:
        return f"{mb:.2f} MB/s"
    return f"{bps / 1024:.2f} KB/s"


def sparkline(values: Sequence[float], width: int = SPARKLINE_WIDTH) -> str:
    """Render values as a unicode-block sparkline (up to `width` cells)."""
    if not values:
        return ""
    tail = list(values)[-width:]
    lo, hi = min(tail), max(tail)
    span = (hi - lo) or 1.0
    step = len(SPARK_CHARS) - 1
    return "".join(SPARK_CHARS[int(((v - lo) / span) * step)] for v in tail)


def gauge_bar(
    value: float,
    best: float,
    width: int = 30,
    filled_style: str = "bright_green",
) -> Text:
    """Return a horizontal bar showing `value / best` filled portion."""
    if best <= 0:
        return Text("")
    ratio = min(value / best, 1.0)
    filled = int(width * ratio)
    return Text.assemble(
        ("█" * filled, filled_style),
        ("░" * (width - filled), "grey37"),
    )


def rate_speed(bps: float) -> Tuple[str, str, int]:
    """Classify a byte/s value into (label, rich_style, dots_out_of_5)."""
    mb = bps / (1024 * 1024)
    if mb < 0.5:
        return "Very Slow", "bold red", 1
    if mb < 2:
        return "Slow", "red", 2
    if mb < 10:
        return "Decent", "yellow", 3
    if mb < 50:
        return "Fast", "green", 4
    return "Blazing Fast", "bold bright_green", 5


def speed_dots(filled: int) -> Text:
    """Render N/5 filled dots as a Rich `Text`."""
    filled = max(0, min(5, filled))
    return Text.assemble(
        ("●" * filled, "bright_green"),
        ("○" * (5 - filled), "grey37"),
    )


def _clean_url_error(reason: object) -> str:
    """Strip noisy `[Errno N]` prefix and long tail from a URLError reason."""
    msg = re.sub(r"^\[Errno -?\d+\]\s*", "", str(reason))
    msg = msg.split(",", 1)[0].strip()
    return (msg or "network error")[:ERROR_MSG_MAX_LEN]


# ---------------------------------------------------------------------------
# 6. HTTP layer.
# ---------------------------------------------------------------------------
def download_once(
    url: str,
    timeout: float = REQUEST_TIMEOUT_SEC,
) -> Tuple[float, int, int]:
    """
    Fetch `url` once, draining the response body completely.

    Returns (elapsed_seconds, bytes_downloaded, http_status).
    Propagates any exception `urllib.request.urlopen` may raise so callers
    can classify errors in one place.
    """
    context = ssl.create_default_context()
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "*/*",
            "Cache-Control": "no-cache, no-store",
        },
    )
    started = time.perf_counter()
    total = 0
    with urllib.request.urlopen(request, timeout=timeout, context=context) as resp:
        status = resp.getcode()
        while True:
            chunk = resp.read(CHUNK_SIZE)
            if not chunk:
                break
            total += len(chunk)
    return time.perf_counter() - started, total, status


def perform_measurement(url: str) -> RequestResult:
    """Run one measurement and wrap the outcome into a `RequestResult`."""
    try:
        elapsed, downloaded, status = download_once(url)
    except urllib.error.HTTPError as exc:
        return RequestResult(ok=False, error=f"HTTP {exc.code}")
    except urllib.error.URLError as exc:
        return RequestResult(
            ok=False, error=_clean_url_error(getattr(exc, "reason", exc)),
        )
    except OSError as exc:      # socket.timeout, ConnectionResetError, …
        head = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
        return RequestResult(ok=False, error=head[:ERROR_MSG_MAX_LEN])
    return RequestResult(
        ok=True, elapsed_sec=elapsed, n_bytes=downloaded, status=status,
    )


# ---------------------------------------------------------------------------
# 7. UI components — each returns a Rich renderable, no side effects.
# ---------------------------------------------------------------------------
def build_header(url: str, count: int) -> Panel:
    """Top banner with target URL and plan."""
    title = Text.assemble(
        ("  🌐  ", "bright_yellow"),
        (" INTERNET SPEED TEST ", "bold white on bright_magenta"),
        ("  ⚡  ", "bright_yellow"),
    )
    subtitle = Text.assemble(
        ("Target:  ", "dim"), (url, "bold bright_cyan"),
        "\n",
        ("Plan:    ", "dim"),
        (f"{count} sequential GET requests", "bold white"),
    )
    return Panel(
        Group(Align.center(title), Text(""), Align.center(subtitle)),
        border_style="bright_magenta",
        box=box.DOUBLE_EDGE,
        padding=(1, 2),
    )


def build_results_table(rows: Sequence[RequestResult]) -> Table:
    """Per-request log with mini gauge bars."""
    best_bps = max((r.speed_bps for r in rows if r.ok), default=0.0)

    table = Table(
        title="[bold]Request Log[/]",
        title_style="cyan",
        box=box.ROUNDED,
        border_style="bright_blue",
        header_style="bold cyan",
        expand=True,
    )
    table.add_column("#", justify="right", width=4, style="dim")
    table.add_column("Status", justify="center", width=10)
    table.add_column("Time", justify="right", width=12)
    table.add_column("Size", justify="right", width=12)
    table.add_column("Speed", justify="right", width=14, style="bold green")
    table.add_column("", justify="left", ratio=1)

    for i, r in enumerate(rows, start=1):
        if r.ok:
            status_style = "bold green" if 200 <= r.status < 300 else "bold yellow"
            table.add_row(
                str(i),
                Text(f"✓ {r.status}", style=status_style),
                f"{r.elapsed_sec:.3f} s",
                humanize_bytes(r.n_bytes),
                humanize_speed(r.speed_bps),
                gauge_bar(r.speed_bps, best_bps or r.speed_bps, TABLE_GAUGE_WIDTH),
            )
        else:
            table.add_row(
                str(i),
                Text("✗ err", style="bold red"),
                "—", "—", "—",
                Text(r.error, style="red"),
            )
    return table


def build_stats_panel(rows: Sequence[RequestResult]) -> Panel:
    """Compact live stats: downloaded, avg speed, best, sparkline."""
    ok = [r for r in rows if r.ok]
    total_bytes = sum(r.n_bytes for r in ok)
    total_time = sum(r.elapsed_sec for r in ok)
    avg_bps = (total_bytes / total_time) if total_time > 0 else 0.0
    speeds = [r.speed_bps for r in ok if r.speed_bps > 0]
    best_bps = max(speeds, default=0.0)
    spark = sparkline(speeds) if speeds else "—"

    grid = Table.grid(padding=(0, 2), expand=True)
    grid.add_column(justify="left", style="dim", width=14)
    grid.add_column(justify="left", style="bold white")
    grid.add_row("Downloaded", humanize_bytes(total_bytes))
    grid.add_row("Avg speed", f"[bold bright_green]{humanize_speed(avg_bps)}[/]")
    grid.add_row("Best speed", humanize_speed(best_bps) if best_bps else "—")
    grid.add_row("Timeline", Text(spark, style="bright_cyan"))
    return Panel(
        grid,
        title="[bold]Live Statistics[/]",
        border_style="bright_green",
        box=box.ROUNDED,
    )


def render_scene(
    url: str,
    rows: Sequence[RequestResult],
    progress: Progress,
    count: int,
) -> Group:
    """Compose the whole live scene: header + progress + table + stats."""
    return Group(
        build_header(url, count),
        Panel(
            progress,
            title="[bold]Progress[/]",
            border_style="bright_yellow",
            box=box.ROUNDED,
        ),
        build_results_table(rows),
        build_stats_panel(rows),
    )


# ---------------------------------------------------------------------------
# 8. Final report.
# ---------------------------------------------------------------------------
def _empty_report() -> Panel:
    """Panel shown when every request failed."""
    return Panel(
        Text(
            "All requests failed. Please check your URL and network.",
            style="bold red",
        ),
        title="[bold red]FINAL REPORT[/]",
        border_style="red",
        box=box.HEAVY,
        padding=(1, 2),
    )


def print_final_summary(
    url: str,
    rows: Sequence[RequestResult],
    count: int,
) -> None:
    """Print the hero-speed panel with gauges and detailed facts."""
    ok = [r for r in rows if r.ok]
    console.print()
    if not ok:
        console.print(_empty_report())
        return

    total_bytes = sum(r.n_bytes for r in ok)
    total_time = sum(r.elapsed_sec for r in ok)
    avg_time = mean(r.elapsed_sec for r in ok)
    med_time = median(r.elapsed_sec for r in ok)
    avg_bps = total_bytes / total_time if total_time else 0.0
    speeds = [r.speed_bps for r in ok if r.speed_bps > 0]
    best_bps = max(speeds, default=0.0)
    worst_bps = min(speeds, default=0.0)
    label, style, dots = rate_speed(avg_bps)

    hero = Group(
        Align.center(Text("AVERAGE INTERNET SPEED", style="dim italic")),
        Text(""),
        Align.center(Text(
            f"{avg_bps / (1024 * 1024):.2f} MB/s",
            style="bold bright_green",
        )),
        Text(""),
        Align.center(Text.assemble(
            ("Connection:  ", "dim"),
            (label, style),
            ("   ", ""),
            speed_dots(dots),
        )),
    )

    gauges = Table.grid(padding=(0, 2), expand=True)
    gauges.add_column(justify="left", style="dim", width=8)
    gauges.add_column(ratio=1)
    gauges.add_column(justify="right", style="bold", width=14)
    gauges.add_row(
        "Avg",
        gauge_bar(avg_bps, best_bps, FINAL_GAUGE_WIDTH, "bright_green"),
        humanize_speed(avg_bps),
    )
    gauges.add_row(
        "Best",
        gauge_bar(best_bps, best_bps, FINAL_GAUGE_WIDTH, "green"),
        humanize_speed(best_bps),
    )
    gauges.add_row(
        "Worst",
        gauge_bar(worst_bps, best_bps, FINAL_GAUGE_WIDTH, "yellow"),
        humanize_speed(worst_bps),
    )

    facts = Table.grid(padding=(0, 2), expand=True)
    facts.add_column(justify="left", style="dim", width=22)
    facts.add_column(justify="left")
    facts.add_row("URL", Text(url, style="cyan"))
    facts.add_row("Requests", f"{len(ok)}/{count} succeeded")
    facts.add_row("Total downloaded", humanize_bytes(total_bytes))
    facts.add_row("Total wall time", f"{total_time:.3f} s")
    facts.add_row("Avg request time", f"{avg_time:.3f} s")
    facts.add_row("Median request time", f"{med_time:.3f} s")

    console.print(Panel(
        Group(hero, Text(""), gauges, Text(""), facts),
        title="[bold]FINAL REPORT[/]",
        border_style="bright_green",
        box=box.DOUBLE,
        padding=(1, 2),
    ))


# ---------------------------------------------------------------------------
# 9. Orchestration.
# ---------------------------------------------------------------------------
def resolve_url(cli_url: Optional[str]) -> Optional[str]:
    """Return a validated URL (from CLI arg or interactive prompt), or None."""
    url = cli_url
    if not url:
        console.print(Panel(
            Text.assemble(
                ("Enter the URL of a heavy file to test speed against.\n", "dim"),
                ("A big image, archive or video works best.\n\n", "dim"),
                ("Example: ", "dim"),
                ("https://speed.cloudflare.com/__down?bytes=10000000", "cyan"),
            ),
            title="[bold]🌐 Internet Speed Test[/]",
            border_style="bright_magenta",
            box=box.DOUBLE_EDGE,
            padding=(1, 2),
        ))
        try:
            url = console.input("[bold cyan]URL[/] ➜ ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[red]Aborted.[/]")
            return None

    if not url:
        console.print("[red]No URL provided.[/]")
        return None

    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        console.print(f"[red]Invalid URL:[/] {url}")
        return None
    return url


def _make_progress() -> Progress:
    return Progress(
        SpinnerColumn(style="bright_yellow"),
        TextColumn("[bold]{task.description}[/]"),
        BarColumn(
            bar_width=None,
            complete_style="bright_green",
            finished_style="green",
        ),
        TextColumn("[cyan]{task.completed}/{task.total}[/]"),
        TimeElapsedColumn(),
        expand=True,
    )


def run_measurements(url: str, count: int) -> List[RequestResult]:
    """Run `count` sequential measurements with a live-updating UI."""
    rows: List[RequestResult] = []
    progress = _make_progress()
    task = progress.add_task("Warming up…", total=count)

    try:
        with Live(
            render_scene(url, rows, progress, count),
            console=console,
            refresh_per_second=UI_REFRESH_HZ,
            screen=False,
            vertical_overflow="visible",
        ) as live:
            for i in range(count):
                progress.update(task, description=f"Request {i + 1} / {count}")
                rows.append(perform_measurement(url))
                progress.advance(task)
                live.update(render_scene(url, rows, progress, count))
            progress.update(task, description="Done")
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted by user.[/]")
    return rows


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Measure internet speed with sequential HTTP requests.",
    )
    parser.add_argument(
        "url", nargs="?",
        help="URL to fetch (e.g. a large image or archive)",
    )
    parser.add_argument(
        "-n", "--count", type=int, default=DEFAULT_REQUEST_COUNT,
        help=f"Number of requests (default: {DEFAULT_REQUEST_COUNT})",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Entry point. Returns the process exit code."""
    args = parse_args(argv)
    count = max(1, args.count)

    url = resolve_url(args.url)
    if url is None:
        return 1

    rows = run_measurements(url, count)
    print_final_summary(url, rows, count)
    return 0


if __name__ == "__main__":
    sys.exit(main())
