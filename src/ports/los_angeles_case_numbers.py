"""LA Superior Court case-number construction.

Civil case numbers are predictable: ``YY`` (2-digit filing year) + ``DD``
(2-letter district/courthouse) + ``TT`` (2-letter case type) + a zero-padded
sequential filing number. Example: ``19STCV12345`` = 2019, Stanley Mosk
Central (ST), Civil unlimited (CV), filing #12345.

The sequence is assigned per filing and is not derivable from a date, so
enumeration means: pick the year(s) from the target date range, then sweep
sequence numbers and let the scraper search each one. The district/type are
what the notes call the "county number" and "type".
"""

from collections.abc import Iterator
from datetime import date

# Stanley Mosk Central handles the bulk of unlimited civil filings; enough for
# the assignment. Add more districts/types here to widen the sweep.
DISTRICTS: tuple[str, ...] = ("ST",)
TYPES: tuple[str, ...] = ("CV",)


def generate_case_numbers(
    from_date: date,
    to_date: date,
    *,
    districts: tuple[str, ...] = DISTRICTS,
    types: tuple[str, ...] = TYPES,
    seq_start: int = 1,
    seq_end: int = 200,
) -> Iterator[str]:
    """Yield candidate case numbers for every year in ``[from_date, to_date]``."""
    for year in range(from_date.year, to_date.year + 1):
        for district in districts:
            for case_type in types:
                for seq in range(seq_start, seq_end + 1):
                    yield f"{year % 100:02d}{district}{case_type}{seq:05d}"


def _demo() -> None:
    got = list(
        generate_case_numbers(
            date(2019, 1, 1), date(2020, 12, 31), seq_start=1, seq_end=2
        )
    )
    assert got == ["19STCV00001", "19STCV00002", "20STCV00001", "20STCV00002"], got
    print("ok")


if __name__ == "__main__":
    _demo()
