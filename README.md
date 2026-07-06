# Trial court scraper task

You are tasked with build a scraper for the los angeles [case record system](https://www.lacourt.ca.gov/). 

Based on my investigation the following can be done:
- Enumerate through predictable case numbers     
- Search for the case number to find documents
- Solve a captcha and get the document link
- download the document
  
Please implement the scraper in the [los_angeles_scraper.py](https://github.com/rreeves8/scraping-assignment/blob/main/src/ports/los_angeles_scraper.py).

You have access to a browser base account that does the following:
- Browser instance in a cloud environment, connected to the internet through a proxy
- Connect and use it through the factory and playwight

The code can be ran using `uv run scraper`

## Usage

Put your Browserbase credentials in `.env`:

```ini
BROWSERBASE_API_KEY=...
BROWSERBASE_PROJECT_ID=...
```

Run against a date range (the 2-digit year in a case number is derived from it):

```bash
uv run scraper --from-date 2019-01-01 --to-date 2019-12-31
```

Optional env knobs:

- `LA_CASE_NUMBERS` — comma-separated case numbers to scrape directly, bypassing
  enumeration (e.g. `LA_CASE_NUMBERS=19STCV12345`, a verified case with imaged
  documents). Recommended for a quick demo: the default sweep probes sequence
  numbers from `00001` and most are empty.
- `LA_MAX_CASES` — stop after this many cases yield documents (default `3`).
- `LA_MAX_DOCS` — documents downloaded per case (default `10`).
- `LA_CONCURRENCY` — parallel worker sessions probing/scraping cases (default
  `8`; each uses one Browserbase session). Every session also runs 3 preview
  tabs of a shared download pool, so a case's documents solve their captchas
  across all sessions at once. Raise toward `BROWSERBASE_MAX_CONCURRENCY` for
  large sweeps; throughput scales with sessions until the solver saturates.
- `BROWSERBASE_MAX_CONCURRENCY` — ceiling on concurrent Browserbase sessions,
  matching your plan's "concurrent browsers" limit (default `25` = Developer;
  set `100` for Startup only when this key won't contend with production).

## How it works

1. `GuestInformation` sets a guest session cookie.
2. `SearchCaseNumber` is POSTed a case number and returns the document list
   (date, description, and a per-document `securityKey`). Results hold 50 docs
   per page; extra pages are walked via `SelectDocuments?page=N`.
3. Each document's `PreviewWait` page is reCAPTCHA-gated — Browserbase solves it
   automatically, then the browser downloads the one-time PDF URL.
4. Browserbase captures the download; the PDF bytes are pulled back via its
   downloads API and matched to each document by the docId in the filename.

Case numbers are constructed in
[`los_angeles_case_numbers.py`](src/ports/los_angeles_case_numbers.py):
`YY` (year) + district + case type + zero-padded sequence, e.g. `19STCV12345`.
