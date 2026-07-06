"""Live terminal progress display (Rich).

One shared ``console`` for the whole process: every line must print through it
(via ``log``) so output lands *above* the live bars instead of tearing them.

The display is one overall status bar (cases saved vs. quota, plus probe/doc
counters that used to be one printed line each) and one transient bar per case
currently downloading, advancing as its documents land.

When stdout is not a terminal (piped to a file), Rich drops the live/ANSI
rendering automatically; log lines still come through, and ``status`` emits a
periodic plain-text heartbeat so long probe streaks don't look frozen.
"""

from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TaskID,
    TextColumn,
    TimeElapsedColumn,
)

console = Console()

# Piped-output heartbeat cadence, in probes.
_BEAT_EVERY = 25


def log(message: str, style: str | None = None) -> None:
    """Print one line, safely above any live bars. ``message`` is treated as
    plain text — case numbers look like [BC712345], which Rich markup would
    otherwise swallow as a style tag."""
    console.print(message, style=style, markup=False, highlight=False)


class ScrapeProgress:
    def __init__(self, max_cases: int) -> None:
        self._bars = Progress(
            SpinnerColumn(),
            TextColumn("{task.description}", markup=False),
            BarColumn(),
            MofNCompleteColumn(),
            TimeElapsedColumn(),
            console=console,
        )
        self._overall = self._bars.add_task("cases", total=max_cases)
        self._last_beat = 0
        self._case_count = 0  # running case index shown on each row

    def __enter__(self) -> "ScrapeProgress":
        self._bars.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._bars.stop()

    def status(
        self, *, probed: int, scraped: int, docs_saved: int, docs_failed: int
    ) -> None:
        """Refresh the overall bar from the scraper's counters."""
        desc = f"cases · {probed} probed · {docs_saved} docs saved"
        if docs_failed:
            desc += f" · {docs_failed} failed"
        self._bars.update(self._overall, description=desc, completed=scraped)
        if not console.is_terminal and probed - self._last_beat >= _BEAT_EVERY:
            self._last_beat = probed
            log(f"[los_angeles] {scraped} case(s) saved · {desc}")

    def start_case(self, case_number: str, total_docs: int) -> TaskID:
        self._case_count += 1
        return self._bars.add_task(
            f"{self._case_count:>3}. {case_number}", total=total_docs
        )

    def doc_done(self, task: TaskID) -> None:
        self._bars.advance(task)

    def end_case(self, task: TaskID) -> None:
        self._bars.remove_task(task)
