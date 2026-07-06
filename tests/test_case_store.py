"""Checks for the JSON persistence adapter (writes to a temp dir, no network)."""

import asyncio
import json
from datetime import datetime
from pathlib import Path

from src.case_store import CaseStore
from src.models import ScrapedTrialCase, ScrapedTrialDocument


def _case() -> ScrapedTrialCase:
    return ScrapedTrialCase(
        case_number="24STCV00001",
        court_id="CA_LA_SUPERIOR",
        court_name="Test Court",
        meta_data={"case_title": "X VS Y"},
        html="<html/>",
        document_list=[
            ScrapedTrialDocument(
                docket_entry_date=datetime(2024, 1, 2),
                content_hash="abc123",
                is_opinion=False,
                description="Complaint",
                document_name="Complaint",
                raw_content=b"%PDF real %%EOF",
            )
        ],
    )


def test_insert_case_writes_metadata_only(tmp_path: Path):
    store = CaseStore(tmp_path / "cases.json", tmp_path / "failures.json")
    asyncio.run(store.insert_case(_case()))

    rows = json.loads((tmp_path / "cases.json").read_text())
    assert len(rows) == 1
    doc = rows[0]["documents"][0]
    assert doc["size_bytes"] == len(b"%PDF real %%EOF")  # size, not the bytes
    assert "raw_content" not in doc and "html" not in rows[0]  # no heavy fields
    assert not (tmp_path / "cases.json.tmp").exists()  # temp cleaned up


def test_record_failure_appends_verbatim(tmp_path: Path):
    store = CaseStore(tmp_path / "cases.json", tmp_path / "failures.json")

    async def run():
        await store.record_failure(
            {"case_number": "24STCV00001", "reason": "download failed", "docId": "5"}
        )
        await store.record_failure({"case_number": "24STCV00099", "reason": "boom"})

    asyncio.run(run())
    rows = json.loads((tmp_path / "failures.json").read_text())
    assert [r["reason"] for r in rows] == ["download failed", "boom"]
    assert rows[0]["docId"] == "5"  # refetch detail kept verbatim


def test_stores_are_independent(tmp_path: Path):
    # Writing a failure must not touch cases.json, and vice versa.
    store = CaseStore(tmp_path / "cases.json", tmp_path / "failures.json")
    asyncio.run(store.record_failure({"case_number": "X", "reason": "boom"}))
    assert not (tmp_path / "cases.json").exists()
