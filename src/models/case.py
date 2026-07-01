from dataclasses import dataclass
from uuid import UUID

from .case_document import ScrapedTrialDocument


@dataclass
class RootTrialCase:
    id: UUID


@dataclass
class NewTrialCase:
    case_number: str
    court_id: str
    court_name: str
    meta_data: object | None


@dataclass
class ScrapedTrialCase(NewTrialCase):
    html: str
    document_list: list[ScrapedTrialDocument]


@dataclass
class TrialCase(RootTrialCase, NewTrialCase): ...
