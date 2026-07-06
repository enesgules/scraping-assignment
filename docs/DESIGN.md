# Design decisions & architecture

Why the scraper is built the way it is. Companion to
[`INVESTIGATION.md`](INVESTIGATION.md), which covers how we reverse-engineered
the LA Court site; this picks up from there.

**One number drives everything: cases per minute.** The site puts a reCAPTCHA
in front of every document download, and downloading — not searching or
parsing — is what eats the clock. Most of the design is about overlapping those
captcha waits across many browser sessions.

---

## 1. The problem, in three facts

From `INVESTIGATION.md`:

1. **Everything runs in a real, proxied browser.** The site's firewall blocks
   data-center IPs and non-browser clients, so every request goes through a
   Browserbase Chrome session on a residential proxy.
2. **Every download is behind a reCAPTCHA.** This is the expensive step, and
   there's no way around it — only ways to run several at once (see §6 for what
   the solve actually costs).
3. **The PDF link is one-time and the browser is remote.** The link dies on
   first use, and the Chrome that opens it runs in Browserbase's cloud, so the
   bytes are captured there, not re-fetched by our code.

---

## 2. Browserbase and the knobs

Browserbase rents real Chrome browsers in the cloud. The vocabulary:

- **Session** — one cloud Chrome instance. Billed per minute it's alive, and
  capped per plan (Developer 25, Startup 100). One *worker* = one session.
- **Tab** (`Page`) — a tab inside a session, sharing its cookies, IP, and
  fingerprint. Tabs are cheap; sessions are not. Each session parks one tab on
  the search form and opens more to download PDFs.
- **CDP** — the low-level Chrome protocol. We use it directly to route downloads
  into Browserbase storage (`setDownloadBehavior`) and to get live download
  completion events (§5).
- **`proxies=True`** — traffic exits through a residential IP the firewall
  trusts. Billed per GB.
- **`solve_captchas=True`** — Browserbase solves the reCAPTCHA automatically.
- **Downloads API** — the remote Chrome saves files into Browserbase storage;
  we pull the bytes back with a REST call that skips the proxy.

### The knobs

| Knob | Default | Controls |
| --- | --- | --- |
| `LA_CASE_NUMBERS` | *(empty)* | Explicit case numbers, skipping enumeration. Handy for a demo. |
| `LA_MAX_CASES` | 50 | Stop after this many cases yield documents. |
| `LA_MAX_DOCS` | 0 (= all) | Documents per case; `0` means all. |
| `BROWSERBASE_CONCURRENCY` | 16 | Parallel browser sessions = workers. The main throughput dial. |
| `_TABS_PER_SESSION` (constant) | 3 | Download tabs per session for the shared pool. |

There is **one** concurrency number. Each worker owns exactly one browser
session, so the session count and the worker count are the same thing — no
separate cap to keep in sync (an earlier two-knob version could deadlock if the
session cap was set below the worker count; collapsing them removed that).

### How the knobs interact

- **Throughput ≈ sessions × `_TABS_PER_SESSION` captchas in flight** (16 × 3 = 48
  at the default). More sessions means more docs/min — but only up to a point:
  past ~16 the solver saturates and reliability drops. Measured:

  | Sessions | Throughput | Reliability |
  | --- | --- | --- |
  | 8 | 20.6 docs/min | fine |
  | **16** | **23.7 docs/min** | fine |
  | 25 | 16.0 docs/min | ~11% of docs failed |

  Sessions are billed per minute, so past the sweet spot you also pay more for
  less. Keep this at or below your plan's concurrent-browser limit (Developer
  25, Startup 100); 16 is both the sweet spot and safely under Developer's cap.
- **Sessions cap cases in flight, not `LA_MAX_CASES`.** A worker handles one case
  at a time; `LA_MAX_CASES` only says when to stop claiming new ones.
- **A quota smaller than the session count doesn't waste sessions.** A worker
  with nothing to claim parks, and its 3 pool tabs keep solving captchas for the
  claimed cases — extra sessions become download-only muscle.
- **Raising `_TABS_PER_SESSION` barely helps** (measured: 3→6 tabs at 4 sessions
  moved throughput only 20.8→22.6 docs/min, within run-to-run noise, with more
  resubmits). Per-session throughput saturates fast — the extra tabs likely
  queue behind a per-session bottleneck (the solver or the shared proxy pipe; we
  didn't isolate which) — so it stays a constant at 3.

---

## 3. Code layout

```
src/
  entry.py                       CLI: parse dates, load env, wire deps, run.
  browser_base_factory.py        Browser adapter: owns a Browserbase session.
  case_store.py                  Persistence adapter: writes results to JSON.
  progress.py                    Live terminal progress display (Rich).
  application/
    scraping_pipeline.py         Use case: run scrapers → store. Orchestration only.
  ports/
    los_angeles_scraper.py       The scraper: probe → paginate → download.
    los_angeles_case_numbers.py  Builds candidate case numbers.
  models/                        Dataclasses + the TrialScraper base.
tests/                           Network-free unit tests.
```

A light **ports-and-adapters** layout: `application/` orchestrates and knows
nothing about LA; `ports/` holds the court-specific scraper; the two adapters at
the `src/` root (`browser_base_factory`, `case_store`) are shared infrastructure
the pipeline drives but doesn't contain. That's what lets the pipeline read as
pure orchestration — "for each scraper, run it, feed results to the store." A
second court would be a new file under `ports/`, reusing everything else. We
didn't build second-court machinery — there's one court — but the seam is kept
because that's where a real system would grow.

Scrapers get a `BrowserBaseFactory` by dependency injection rather than building
browsers themselves, which keeps them agnostic about how a session is obtained
and swappable in tests.

---

## 4. Case numbers (`los_angeles_case_numbers.py`)

LA civil case numbers are structured — `YY` + district + type + sequence, e.g.
`19STCV12345` — and the sequence counts up each year, so they can be **built**
from a date range instead of looked up.

- **`generate_case_numbers` is a generator**, so a sweep of tens of thousands of
  numbers stops the moment the quota fills.
- **Districts/types are constants** (Stanley Mosk Central + unlimited civil
  covers most filings), a one-line edit to widen — no runtime knob for a value
  that never changes.
- Low sequence numbers are **dense**: ~90–100% of the first 60 in a busy year
  are real cases. Probing is cheap and usually hits, which is exactly why
  *downloading* is the bottleneck.

---

## 5. The scraper flow (`los_angeles_scraper.py`)

Per case: **probe** → **paginate** → **download** → **map**.

### Probe — search without navigating

The session parks once on the search form; each probe is an in-page `fetch()`
POST to `SearchCaseNumber`, parsed off-DOM (commit `e021bba`). One round-trip
instead of two page loads, and the fetch inherits the form's cookies, TLS, and
fingerprint, so the firewall can't tell it from a real submit. Empty probes —
most of a sweep — get much cheaper. The antiforgery token is read once and
reused; only a found case sends its HTML back, since empty probes dominate.

**Cross-contamination guard** (`c9e789a`): `SearchCaseNumber` renders from
per-session server state, so two searches racing in one session can return each
other's rows — measured 6 of 10 wrong at 10 concurrent. Every row's
`preview(...)` call embeds its own case number, so a response with any foreign
row is flagged and retried. The rule: **searches stay serial per session;
parallelism comes from more sessions.** The design already works that way, so
the guard is a safety net.

### Paginate — stays in the searching session

Results hold 50 documents per page; longer cases spill onto
`SelectDocuments?page=N`, which carry no case number — the server serves them
from the session's current case. So pagination must run in the same session that
searched, before it searches anything else. `_collect_all_documents` is a
standalone function so the off-by-one-prone paging loop can be unit-tested
against a fake page.

### Quota — claim before download

`LA_MAX_CASES` uses a claim-before-download counter (`114532f`): a worker bumps
`_claimed` the moment it decides to download, *before* paying for captchas, so
two workers can't both grab the last slot. A case that fails frees its slot.
`_scraped` counts only saved cases — two counters keep the gate both correct
(never over-download) and non-wasteful.

---

## 6. The captcha, measured

The captcha is the whole performance story, so it's worth knowing what it
actually costs. We measured this directly (single-session runs of a known
10-document case, plus Browserbase's own console signals).

**It's mandatory.** With `solve_captchas=False`, 0 of 3 downloads ever
started — the preview navigation never turns into a download. There's no
ungated path to the PDF.

**Browserbase emits solve events.** The remote Chrome logs
`browserbase-solving-started` / `browserbase-solving-finished` to the console
(with a structured `{"key":"browserbase-captcha-event",...}` twin). These fire
reliably and can be hooked with `page.on("console", …)` — an accurate signal
that beats blind waiting.

**The solve itself is usually fast; the wait is mostly elsewhere.** Hooking the
events showed the actual solve typically completes in **under a second** once
the challenge is detected, with an occasional outlier around **20 s**. A
document's ~10 s end-to-end is dominated by *navigation* — loading the
`PreviewWait` page through the residential proxy and following the redirect to
the one-time PDF — not by solving. So the flat "20–30 s per captcha" intuition
is really the slow tail, not the common case.

**The dominant risk is a stalled preview, not a slow one.** When a preview never
starts a download (an overloaded solver, a proxy hiccup), it sits in the
120 s `expect_download` timeout before we give up and resubmit. In back-to-back
runs of the same case, wall-clock swung 50 s → 166 s purely on whether one
preview hit that timeout — tab count (3 vs 6 per session) made no reliable
difference at one session. The resubmit path (§7) is what recovers these; the
long timeout is the cost when it triggers.

**Alternatives considered (not adopted).** External solver APIs (CapSolver,
2Captcha, ~$0.80–3 per 1,000 v2 solves) inject a token into the
`g-recaptcha-response` field instead of using Browserbase's built-in solver.
They'd buy *unbounded* solve concurrency and ~9 s solves, but on raw cost
they're roughly a wash with Browserbase's included solving, and reCAPTCHA tokens
can be IP-bound — a token solved on the vendor's IP may be rejected when our
residential-proxy session submits it, which would force routing the solver
through the same proxy. Not worth the complexity unless the built-in solver's
per-session ceiling becomes the proven bottleneck; today it isn't. (Proxy
*bandwidth* at ~$10/GB is more likely the real cost driver on a big run than
captcha spend — worth measuring GB/case before optimizing here.)

---

## 7. The download mechanism

Capturing the bytes is the hardest part. The current design is the sum of fixes
for a handful of problems:

- **The browser is remote**, so our code can't grab the file. → Downloads route
  into Browserbase storage (`setDownloadBehavior`), pulled back through the
  per-file API — never fetched directly.
- **The link is one-time**, so the bytes must be captured on first open.
- **Closing a tab too early truncated PDFs.** → Never close a tab until its
  transfer is confirmed, and check every capture for the `%%EOF` trailer
  (`_is_complete_pdf`).
- **Storage sync lags the completion event.** → A short re-list loop before
  giving up.
- **Previews sometimes never start under load.** → Resubmit the document up to
  twice; a fresh preview, usually on another session, recovers it. A full-sweep
  run measured ~14% of first attempts failing and a single resubmit recovering
  all but 3 of 347 docs (99%).

**Trigger.** Opening the `PreviewWait` URL starts the captcha, then a navigation
that *aborts* when the download begins — so the code wraps `page.goto` in
`page.expect_download` and swallows the expected navigation error. A download
starting *is* the success signal.

**Confirm via CDP events, not polling** (`c4f4384`). The factory listens to
`downloadWillBegin` / `downloadProgress` and exposes a `completed_downloads`
set; a file appears there the instant Chrome finishes the transfer — in-memory,
no network. The transfer completes ~0.3 s after it begins, so waiting on the
event is essentially free, and the tab closes the moment its PDF is across.

**Fetch and validate.** Once completion fires, `_execute_job` fetches the file
once via the per-file API, rejects truncated captures (`_is_complete_pdf`), and
matches each PDF back to its row by the `docId` embedded in the filename
(`_doc_id_in`). Every failure path returns a *named* reason — never started,
never completed, truncated, missing from listing — and the last attempt's reason
lands in `failures.json` (§9).

---

## 8. How the concurrency model got here

Each stage answered a measured limit in the one before it:

1. **One serial session** — correct but every captcha blocked the next case.
2. **Concurrent workers, fresh session per download** — needed because
   `get_downloads()` returned the whole session's zip, so reusing a session made
   confirming each PDF re-fetch every earlier file (quadratic).
3. **One session per worker, recycled every 8 cases** — a compromise to bound
   that zip.
4. **Per-file downloads API** (`c4f4384`) — fetching one PDF by id killed the
   quadratic outright, so recycling went away; one session now serves a worker
   for the whole run.
5. **Pooled downloads across sessions** (`292abef`, current) — the unlock is that
   **a `securityKey` works from any session**. So downloads go through a shared
   `asyncio.Queue` of `(doc, future)` jobs; each session runs 3 consumer tabs
   that take *any* case's job. One case's captchas now solve across all sessions
   at once, not serially in the one that found it. The future lets the
   submitting worker await a per-document result without caring which session
   did the work.
6. **The pool stays full to the finish line.** A worker that runs out of work
   *parks* instead of closing — its tabs keep consuming — so the run's tail
   solves on the full pool, not a shrinking one. Deleting a starving
   `wait_for_selector` at the same time took a live 10-case baseline from
   503 s → 325 s (+55%), zero lost cases.

---

## 9. Persistence (`case_store.py`)

`CaseStore` is its own adapter, not part of the pipeline — filesystem I/O is
infrastructure, so it sits beside `browser_base_factory`. It takes its file
paths as constructor args, so tests point it at a temp dir. It writes two files
through one atomic-write helper (write `.tmp`, rename — a crash can't leave a
half-written file), under one `asyncio.Lock` since workers call it concurrently:

- **`cases.json`** — each scraped case, **metadata only** (no PDF bytes, no page
  HTML). Each document keeps its `content_hash` and `size_bytes`, so a record
  stays verifiable and dedupable without carrying the file.
- **`failures.json`** — anything found but not captured, so a later run can retry
  it instead of the failure vanishing into logs. A failed document records its
  whole row (`docId`, `securityKey`, …) plus a named reason; a crashed case
  records at the case level. A probe that merely *errored* is **not** recorded —
  it was never confirmed real, and in a big sweep that would flood the file.
  Retry is a manual `LA_CASE_NUMBERS` re-run; auto-consuming the file is left
  unbuilt (§11).

---

## 10. Retries and startup timing

Four retry layers, each isolating the smallest unit that can fail:

| Layer | Guards against | Behaviour |
| --- | --- | --- |
| Session creation | Browserbase 429 under load | Backoff 2s/4s/8s, then give up |
| Search | Stale session, contaminated response | Re-establish guest session, retry once |
| Per-case | One bad case crashing the sweep | Skip it, free its slot |
| Download | Preview never starts, truncated, storage lag | Resubmit up to twice; reason recorded |

Each worker also logs one startup line — `ready in 8.2s (session 1.8s · connect
2.5s · guest page 3.3s)` — from timings the factory records on the session. It
makes a slow start legible: a 429 backoff shows up as a slow *create*, not a
mystery stall. (This is also why the slowest worker's case bar appears last —
its guest-page load through a fresh proxy simply took longest.)

---

## 11. What we deliberately didn't build

So the gaps read as intent:

- **No second-court abstraction** beyond the `TrialScraper` seam.
- **No real database** — a JSON file with atomic writes fits the scale, and the
  hash+size schema is already dedup-friendly.
- **No document-type taxonomy** — `_is_opinion` is a keyword heuristic with a
  `TODO`; a real system would map type codes.
- **No auto-retry of `failures.json`** — failures are recorded but re-run
  manually; wiring the file back in as an input is a clean next step, not
  speculative machinery to build now.
- **No external captcha solver** (§6) — the built-in solver isn't the proven
  bottleneck, and tokens may be IP-bound.

---

## Appendix: decision → commit

| Decision | Commit |
| --- | --- |
| Initial: one session, serial cases | `c5bf677` |
| Per-case error isolation | `1333c8d` |
| Download preview retry | `6d41027` |
| Concurrent downloads; paging helper extracted | `c5702b5` |
| Concurrent workers; in-page search | `e021bba` |
| Quota reserved before scrape | `114532f` |
| Persist to JSON; decode HTML entities | `c30ca7c` |
| Env-configurable session cap | `f0a9736` |
| One session per worker | `9ac3f3a` |
| Session-creation 429 backoff | `eb29a42` |
| Cross-contaminated search detection | `c9e789a` |
| CDP download events + per-file API; drop recycling | `c4f4384` |
| Pooled downloads across sessions | `292abef` |
| Factory moved to shared `src/` | `b35a7be` |
| Failures recorded; `CaseStore` extracted | `9316a0a` |
| Live progress display (Rich) | `8b9bf24` |
| Skip futile re-list when download never completed | `76a89d2` |
| Download resubmit ×2; failure reasons named | *(uncommitted)* |
