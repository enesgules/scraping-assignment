import datetime
from dataclasses import dataclass
from uuid import UUID


@dataclass
class _RootTrialDocument:
    id: UUID


@dataclass
class _BaseTrialDocument:
    docket_entry_date: datetime.datetime
    content_hash: str
    is_opinion: bool
    description: str
    document_name: str


@dataclass
class ScrapedTrialDocument(_BaseTrialDocument):
    raw_content: bytes


@dataclass
class NewTrialDocument(_BaseTrialDocument):
    case_id: UUID


@dataclass
class TrialDocument(_RootTrialDocument, NewTrialDocument): ...
