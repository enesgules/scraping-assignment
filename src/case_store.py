"""Persistence adapter: writes scraper results to JSON on disk.

Two files, both written atomically (temp file + rename) so a crash can never
leave a half-written file:

- ``cases.json`` — one record per scraped case (metadata only; no PDF bytes or
  page HTML, both large). Each document keeps its hash and size, so a row stays
  verifiable and dedupable without carrying the file.
- ``failures.json`` — documents/cases found but not captured, so a later run can
  retry them (feed the case numbers back via ``LA_CASE_NUMBERS``).

``insert_case`` and ``record_failure`` ARE the InsertCase / RecordFailure sinks
the scrapers write to (see models.base_scraper). Workers call them concurrently,
so each is serialized under a lock. This is infrastructure — it lives beside
``browser_base_factory`` (the browser adapter), not in ``application`` (the use
case), so the pipeline stays pure orchestration.
"""

import asyncio
import json
from pathlib import Path

from .models import ScrapedTrialCase


class CaseStore:
    """A per-run JSON store. Paths are constructor args so a test can point it
    at a temp directory; the defaults are the real output files."""

    def __init__(
        self,
        cases_path: Path = Path("cases.json"),
        failures_path: Path = Path("failures.json"),
    ) -> None:
        self._cases_path = cases_path
        self._failures_path = failures_path
        self._cases: list[dict[str, object]] = []
        self._failures: list[dict[str, object]] = []
        self._lock = asyncio.Lock()

    async def insert_case(self, case: ScrapedTrialCase) -> None:
        async with self._lock:
            self._cases.append(_record(case))
            _write_json_atomic(self._cases_path, self._cases)
        print(
            f"[{case.case_number}] saved — {len(case.document_list)} doc(s); "
            f"{len(self._cases)} case(s) now in {self._cases_path}"
        )

    async def record_failure(self, failure: dict[str, object]) -> None:
        async with self._lock:
            self._failures.append(failure)
            _write_json_atomic(self._failures_path, self._failures)
        print(
            f"[{failure.get('case_number')}] failure recorded "
            f"({failure.get('reason')}); {len(self._failures)} in "
            f"{self._failures_path}"
        )


def _record(case: ScrapedTrialCase) -> dict[str, object]:
    """Shape a case into its JSON row (the persisted schema)."""
    return {
        "case_number": case.case_number,
        "court_id": case.court_id,
        "court_name": case.court_name,
        "meta_data": case.meta_data,
        "documents": [
            {
                "docket_entry_date": d.docket_entry_date.date().isoformat(),
                "description": d.description,
                "document_name": d.document_name,
                "content_hash": d.content_hash,
                "is_opinion": d.is_opinion,
                "size_bytes": len(d.raw_content),
            }
            for d in case.document_list
        ],
    }


def _write_json_atomic(path: Path, data: object) -> None:
    # Temp file + rename: a crash mid-write can't leave a half-written file.
    # Rename is atomic on the same filesystem.
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)
