# Design decisions & architecture

This explains *why* the scraper is built the way it is — how the code is laid
out, what each part does, and the reasoning behind the choices, including things
we tried, measured, and changed.

It's a companion to [`INVESTIGATION.md`](INVESTIGATION.md), which covers how we
reverse-engineered the LA Court site. This picks up from there: given how the
site works, how should the scraper be built?

One number drives almost everything below: **cases per minute**. The site puts a
reCAPTCHA in front of every document, and solving it takes 20–30 seconds. So
downloading — not searching or parsing — is what eats the clock, and most of the
design is about overlapping those captcha waits.

New to Browserbase? Read §2 first — it defines every term the rest of the
document uses.

---

## 1. The problem in three facts

All three come from `INVESTIGATION.md`:

1. **Everything must run inside a real, proxied browser.** The site's firewall
   blocks data-center IPs and non-browser clients, so a plain HTTP client can't
   even reach it. Every request goes through a Browserbase Chrome session.
2. **Every download is behind a reCAPTCHA** (~20–30 s to solve). This is the
   expensive step, and there's no way around it — only ways to run several at
   once.
3. **The PDF link is one-time and the browser is remote.** The link dies on
   first use, and the Chrome that opens it runs in Browserbase's cloud, so the
   bytes have to be captured on that side, not re-fetched by our code.

Fact 2 is the whole performance story: **how many captchas can we solve at once,
and how little time can we waste around each one.**

---

## 2. Browserbase and the knobs, explained

Everything runs on **Browserbase**, a service that rents real Chrome browsers in
the cloud. This section defines the vocabulary and then the dials we turn.

### 2.1 The core terms

- **Session** — one cloud Chrome instance. It's the unit of everything: you
  create it via the API (~0.8 s), connect to it, drive it, and close it. It holds
  its own cookies and login state, it exits to the internet through its own proxy
  IP, and **you're billed for every minute it's alive**. It's also the unit of
  concurrency: your plan caps how many sessions can run at once. In our code, one
  *worker* = one session.
- **Page / tab** — a single tab inside a session (Playwright calls it a `Page`).
  One session can hold many tabs, and they share the session's cookies, IP, and
  fingerprint. We use this heavily: each session parks one tab on the search form
  and opens extra tabs to download PDFs. Tabs are cheap; sessions are not.
- **Playwright** — the library we drive the browser with (`page.goto`,
  `page.evaluate`, …). It talks to the remote Chrome over CDP.
- **CDP (Chrome DevTools Protocol)** — the low-level wire protocol Chrome speaks.
  Playwright sits on top of it, but we also use it directly for two things Chrome
  only exposes there: telling Chrome where to save downloads
  (`setDownloadBehavior`), and getting live download events (`downloadWillBegin`
  / `downloadProgress`) so we know the instant a PDF finishes (§8.3).
- **Residential proxy** (`proxies=True`) — the session's traffic exits through a
  home-ISP IP address instead of a data-center one, so the court's firewall
  trusts it. Bandwidth through the proxy is billed per GB.
- **Captcha solving** (`solve_captchas=True`) — Browserbase automatically solves
  the reCAPTCHA that guards each download. It takes ~20–30 s and happens **per
  tab**. A single session solves only ~2–4 at a time (measured) — this per-session
  limit is the reason we spread downloads across many sessions (§7).
- **Downloads storage + downloads API** — when the remote Chrome downloads a
  file, it can't reach our machine, so it saves into Browserbase's own storage.
  We pull the bytes back with a REST call (`GET /v1/downloads`). That call goes
  straight to Browserbase's API, *not* through the browser or proxy, so it
  doesn't cost proxy bandwidth.
- **Plan concurrency limit** — the max number of sessions allowed alive at once
  (Developer plan: 25, Startup: 100). Going over returns HTTP 429, which we back
  off and retry (§9).

### 2.2 The knobs

Environment variables (see the README for exact usage) plus one internal
constant:

| Knob | Default | What it controls |
| --- | --- | --- |
| `LA_CASE_NUMBERS` | *(empty)* | Explicit case numbers to scrape, skipping enumeration. Handy for a quick demo. |
| `LA_MAX_CASES` | 50 | Stop after this many cases yield documents. The main "how much work" dial. |
| `LA_MAX_DOCS` | 0 (= all) | Documents downloaded per case; `0`/unset means every document in the case. |
| `LA_CONCURRENCY` | 16 | **Worker sessions** run in parallel. The main throughput dial (§2.3). |
| `BROWSERBASE_MAX_CONCURRENCY` | 25 | Hard ceiling on live sessions, matching your plan. A safety cap so a big run can't exceed what you're paying for. |
| `_TABS_PER_SESSION` (constant) | 3 | Download tabs each session runs for the shared pool. |

### 2.3 How concurrency actually affects speed

The captcha is the bottleneck, so throughput is set by **how many captchas are
being solved at the same time**, which is:

```
concurrent captchas  ≈  LA_CONCURRENCY  ×  _TABS_PER_SESSION
```

At the default that's 16 × 3 = 48 downloads in flight. More sessions means more
concurrent captchas means more docs/minute — but only up to a point, because
Browserbase's solver has its own capacity. We measured the curve:

| `LA_CONCURRENCY` | Concurrent captchas | Throughput | Reliability |
| --- | --- | --- | --- |
| 8 | ~24 | 20.6 docs/min | fine |
| **16** | ~48 | **23.7 docs/min** | fine |
| 25 | ~75 | 16.0 docs/min | ~11% of docs failed |

So there are **two ceilings**, and they pull in opposite directions:

1. **Your plan's session limit** (`BROWSERBASE_MAX_CONCURRENCY`) — a hard cap;
   exceed it and session creation gets 429'd.
2. **The solver's capacity** — a *soft* cap that bites first. Past ~16 sessions,
   piling on more concurrent captchas *lowers* throughput and starts failing
   downloads, because the solver is overwhelmed. 16 is the measured sweet spot.

There's also a **cost** dimension: since sessions are billed per minute, doubling
`LA_CONCURRENCY` roughly doubles the browser-minute cost of a run even when it
doesn't double the speed. So past the sweet spot you pay more for *less*
throughput — another reason 16 is the default.

The rest of this document is how the code is arranged to keep those concurrent
captchas as busy and as correct as possible.

---

## 3. Code layout

```
src/
  entry.py                       CLI: parse dates, load env, wire deps, run.
  browser_base_factory.py        Owns a Browserbase session + Playwright browser.
  application/
    scraping_pipeline.py         Runs scrapers, saves results to JSON.
  ports/
    los_angeles_scraper.py       The scraper: probe → paginate → download.
    los_angeles_case_numbers.py  Builds candidate case numbers.
  models/                        Dataclasses for cases/documents + TrialScraper.
tests/
  test_los_angeles_scraper.py    Pure-function unit tests (no network).
```

This is a light **ports-and-adapters** layout. `application/` orchestrates and
knows nothing about LA; `ports/` holds the court-specific adapter; `models/`
holds the shared types. A second court would be a new file under `ports/`
implementing the same `TrialScraper`, reusing the pipeline and models unchanged.
We didn't build any second-court machinery — there's one court — but the seam is
kept because that's where a real system would grow.

### Why the factory moved out of `ports/`

The browser factory started at `src/ports/browser_base_factory.py` and was moved
up to `src/browser_base_factory.py` (commit `b35a7be`, a pure rename). The reason
is the ports boundary: `ports/` is for *court-specific* adapters, but Browserbase
is **shared infrastructure** every adapter uses the same way. Under `ports/` it
looked like one court's concern; at the `src/` root it reads as the browser
substrate all ports sit on.

The same commit wired the factory through dependency injection — a scraper is
handed a `BrowserBaseFactory` instead of building browsers itself. That's what
keeps the scraper agnostic about how a session is obtained, and lets it be
swapped in tests.

Keeping the factory separate from the scraper also paid off later: when we
replaced the download mechanism (§8), nearly all the change was in the factory
and the scraper barely moved.

---

## 4. Case numbers (`los_angeles_case_numbers.py`)

LA civil case numbers are structured — `YY` + district + type + sequence, e.g.
`19STCV12345` — and the sequence just counts up each year. So they can be
**built** instead of looked up, which is what lets the scraper run from just a
date range.

The choices here are deliberately small:

- **`generate_case_numbers` is a generator, not a list.** A year is tens of
  thousands of numbers; yielding lazily lets the sweep stop as soon as the quota
  is filled.
- **Districts/types are constants, not config.** Stanley Mosk Central +
  unlimited civil covers most filings. They're a tuple at the top of the file, so
  widening the sweep is a one-line edit — but there's no env knob for a value
  that never changes at runtime.
- **`seq_start`/`seq_end`** let a caller probe a specific band. We measured that
  low sequence numbers are dense: ~90–100% of the first 60 in a busy year are
  real cases with documents. Probing is cheap and usually hits — which is exactly
  why downloading, not probing, is the bottleneck.

The `_demo()` at the bottom is the file's test: it checks the year-rollover
ordering without pulling in a framework for one pure function.

---

## 5. The scraper's core flow (`los_angeles_scraper.py`)

Per case, the scraper does four things: **probe** (search for documents),
**paginate** (collect all document rows), **download** (preview through the
captcha, capture the PDF), and **map** (build the record). The decisions worth
explaining are below.

### 5.1 Searching without navigating

The first version navigated to the search page, typed the case number, and
submitted — two full page loads per probe.

Now (commit `e021bba`) the session parks once on the search form and each probe
is an in-page `fetch()` POST to `SearchCaseNumber`, with the response parsed
off-DOM. One round-trip instead of two page loads, and the fetch inherits the
form's cookies, TLS, and fingerprint, so the firewall can't tell it from a real
submit. Empty probes — most of a sparse sweep — get much cheaper.

Two details in the code:

- **The antiforgery token is read once and reused** across many fetches. It only
  reloads if the page navigated away (pagination does) or the session went stale.
- **Only a found case sends its HTML back.** Empty probes dominate and nobody
  reads their redisplayed form, so returning that HTML would waste bandwidth on
  the hot path.

### 5.2 The cross-contamination guard

Added in commit `c9e789a` after a live experiment.

`SearchCaseNumber` renders from **per-session server state** — the server
remembers the case this session is currently viewing. So if two searches run
concurrently in the same session, they race, and a response can come back with
*another case's* rows. We measured it: 10 concurrent searches in one session, **6
came back wrong**.

The risk is silent corruption — filing case X's PDFs under case Y. The fix uses a
lucky property: every row's `preview(...)` call embeds its own case number. So
the search marks a response `foreign` if any row belongs to a different case, and
`_search` treats that like a stale session — re-establish the guest session and
retry once.

The rule this locks in: **searches stay serial within a session; parallelism
comes from more sessions.** The current design already works that way, so the
guard is a safety net, not load-bearing. (One gap: an empty contaminated response
has no rows to check. It never showed up in testing, but it's another reason to
keep searches serial rather than trust the guard.)

### 5.3 Pagination stays in the searching session

Results hold 50 documents per page; longer cases spill onto
`SelectDocuments?page=N`. Those URLs carry **no case number** — the server serves
them from the current case in the session cookie. So pagination must run in the
same session that just searched, before it searches anything else.

`_collect_all_documents` is a standalone function (commit `c5702b5`) so it can be
unit-tested against a fake page — the paging loop is exactly the off-by-one-prone
code worth a test. An earlier blind `sleep` waiting for the next page was
replaced with `wait_for_selector` on a document row, so a slow page can't quietly
truncate the list.

### 5.4 Quota accounting

The `LA_MAX_CASES` quota uses a **claim-before-download** counter (commit
`114532f`). A worker bumps `_claimed` the moment it decides to download a case,
*before* paying for captchas, so two workers can't both grab the "last" slot and
overshoot. If the case then fails or is empty, it decrements to free the slot.
`_scraped` counts only saved cases. Two separate counters is what makes the gate
both correct (never over-download) and non-wasteful (a failed case doesn't burn a
slot for good).

---

## 6. Persistence (`scraping_pipeline.py`)

`insert_case` appends to a JSON array in `cases.json` (commit `c30ca7c`):

- **Metadata only — no PDF bytes, no page HTML.** Each document keeps its
  `content_hash` and `size_bytes`, so a record stays verifiable and dedupable
  without carrying the file. The bytes live in memory during the run (they get
  hashed) but aren't what the JSON is for.
- **Atomic write under a lock.** Workers call this concurrently, so the
  read-modify-write is serialized with an `asyncio.Lock` and the file is written
  to `.tmp` then renamed — a crash can't leave a half-written DB. This is the one
  place workers share external state, so it's the one place that needs the care.

---

## 7. How the concurrency model evolved

This changed the most, because it's where cases/minute is won. Each stage was a
response to a measured limit in the one before it.

**Stage 1 — one session, one worker (`c5bf677`).** The first version used a
single session and scraped cases one at a time. Correct but fully serial: every
captcha was waited on before the next case started.

**Stage 2 — concurrent workers; download in a fresh session (`e021bba`).** Added
the multi-worker model (`LA_CONCURRENCY` workers over a shared iterator) and the
in-page search. Workers probed cheaply in their own session; a case with
documents was handed to a **fresh session per download**. That fresh session
existed because `get_downloads()` returned a zip of the *whole session's*
downloads — reusing one session made confirming each PDF re-fetch every earlier
file (quadratic). A session per case kept the zip small, but cost session-create
latency and counted against the plan's session-per-minute limit.

**Stage 3 — one session per worker (`9ac3f3a`).** Probing and downloading merged
back into one session per worker, since a second session per case was itself
costly. To bound the zip, a worker **recycled its session every 8 cases**
(`_RECYCLE_SESSION_EVERY`). A deliberate compromise: reuse to save session
creates, recycle to keep the zip small.

**Stage 4 — per-file downloads (`c4f4384`).** We found Browserbase's per-file
downloads API (list files, fetch one by id), which the SDK doesn't wrap, so the
factory calls it with `httpx`. Fetching each PDF on its own means the session's
history never has to be re-downloaded wholesale. That deleted the entire reason
for recycling, so `_RECYCLE_SESSION_EVERY` went away — one session now serves a
worker for the whole run. (Details in §8.)

**Stage 5 — pooled downloads across sessions (`292abef`, current).** The last
bottleneck was structural. Downloads ran only in the session that found the case,
and **Browserbase solves ~2–4 captchas at a time per session** (measured). So a
10-document case took ~5 serial waves in one session while other sessions sat
idle.

The unlock is another measured fact: **a `securityKey` works from any session.**
A key found in session A downloads fine in session B, even one that never
searched the case and is busy searching something else. So a document's preview
doesn't have to happen where the case was found.

So downloads now go through a **shared pool**:

- One `asyncio.Queue` of `(doc, future)` jobs.
- Each worker session runs 3 consumer tasks (`_TABS_PER_SESSION`) next to its
  probing loop. A consumer takes any case's job, previews it in *its own*
  session, waits for that session's completion event, fetches the bytes, and
  resolves the future.
- A worker that finds a case paginates locally, then drops each document on the
  queue and awaits the futures.

Now one case's captchas solve across **all** sessions at once. We verified the
scaling directly — 3 sessions handled 12 previews in the time one session needs
for 4 — with no account-wide solver cap showing up at 12 concurrent.

The queue of futures is the key: handing each worker its own case to download
start-to-finish is exactly what *doesn't* scale, because it re-confines a case to
one session. The future lets the submitting worker await a per-document result
without caring which session did the work.

`_TABS_PER_SESSION = 3` matches the solver's per-session appetite; more tabs just
queue behind it. The old global `_MAX_CONCURRENT_DOWNLOADS` semaphore is gone —
the ceiling is now simply 3 × workers.

### Stage 6 — the pool stays full to the finish line

Two problems showed up in a live 10-case baseline run (503 s, 11.8 docs/min):

- **Workers retired too early.** A worker that saw the quota filled closed its
  session — taking its 3 pool tabs with it — while other cases still had
  captchas queued, so the run's tail solved on a shrinking pool. Now a worker
  that runs out of work *parks* (its tabs keep consuming) until every worker is
  done. Same idea applied to `LA_CASE_NUMBERS`: workers beyond the explicit
  list park immediately and serve as download-only sessions, so one case's 60
  documents solve across all sessions instead of one.
- **`wait_for_selector` on the parked search tab starved** while sibling tabs
  solved captchas — 7 spurious 15 s retries and 2 real cases silently dropped
  in that baseline. The form is server-rendered (present at
  `domcontentloaded`), and the fetch result already reports a missing form, so
  the wait was deleted outright.

Rerun with identical settings: **325 s, 18.3 docs/min (+55%), zero retries,
zero lost cases**, and every overlapping document hash byte-identical to the
baseline's.

---

## 8. The download mechanism

Capturing the bytes is the hardest part, and where most bugs lived. It went
through three forms.

### 8.1 The problems we hit

The current design is mostly the sum of fixes for these:

- **The browser is remote, so our code can't grab the file.** → Downloads are
  routed into Browserbase storage (`setDownloadBehavior`) and pulled back through
  its API, never fetched directly.
- **The link is one-time.** → The bytes must be captured on the first open, which
  is why confirmation timing matters.
- **Closing a tab too early truncated PDFs.** An early version closed the tab as
  soon as the download *started*, cutting off big transfers. The file began with
  `%PDF` but was incomplete. → Never close a tab until its transfer is confirmed,
  and check every capture for the `%%EOF` trailer (`_is_complete_pdf`).
- **The cumulative-zip quadratic.** `get_downloads()` returned the whole
  session's zip, so confirming one PDF re-downloaded all earlier ones. This drove
  two stages of the concurrency design before the per-file API removed it (§7).
- **Storage sync lags the browser.** A file's "completed" event can fire just
  before it's listable via the API. → A short re-list loop before giving up.
- **Previews sometimes never start under load.** With many captchas requested at
  once, some time out without starting. → Resubmit the document once; a fresh
  preview, usually on another session, recovers it. This is why runs still finish
  at 100%.

### 8.2 Trigger

Opening the `PreviewWait` URL starts a captcha, then a navigation that **aborts**
when the download begins. The code wraps `page.goto` in `page.expect_download`
and swallows the expected navigation error — a download starting *is* the success
signal. This shape has survived every refactor.

### 8.3 Confirmation — from polling to events

- **Before (`c4f4384`):** poll `get_downloads()` every 2 s, unzip, check if the
  PDFs are there.
- **After:** the factory listens to CDP `downloadWillBegin` / `downloadProgress`
  events (that's why `setDownloadBehavior` sets `eventsEnabled: true`) and exposes
  a `completed_downloads` set. A file shows up there the instant Chrome finishes
  the transfer — in-memory, no network, no unzip. The tab closes the moment its
  PDF is fully across.

A download completes ~0.3 s after it begins, so waiting on the event is
essentially free.

### 8.4 Fetch and validate

Once the completion event fires, `_execute_job` fetches the file once via the
per-file API. Two guards:

- **`_is_complete_pdf`** rejects truncated captures (starts with `%PDF`, no
  `%%EOF`); the document is re-previewed, and the largest capture wins if it
  downloads twice (`_pick_download_files`).
- **A short re-list loop** covers storage lag when the file isn't listed yet.

The `docId` in the filename (`e78869237(1)-….pdf`) matches each PDF back to its
document row (`_doc_id_in`) — same idea whether the bytes came from a zip or the
per-file API.

---

## 9. Retries, in one place

The scraper retries at four levels, each guarding a different failure:

| Layer | Guards against | Behaviour |
| --- | --- | --- |
| **Session creation** (`eb29a42`) | Browserbase 429 under load | Backoff 2s/4s/8s, then give up |
| **Search** (`c9e789a`) | Stale session/token, firewall hiccup, contaminated response | Re-establish guest session, retry once |
| **Per-case** (`1333c8d`) | One bad case crashing the sweep | `except` around each case; skip it, free its slot |
| **Download** (`6d41027`, current) | Preview never starts, truncated capture, storage lag | Resubmit the doc once; short re-list |

Same principle everywhere: **isolate the failure to the smallest unit — one
session create, one case, one document — and retry just that**, so a transient
error never takes down more than it has to.

---

## 10. The browser factory (`browser_base_factory.py`)

`BrowserBase` is an async context manager: on enter it creates the session,
connects Playwright over CDP, wires download capture, and returns
`(session, page)`; on exit it tears everything down with bounded timeouts so a
hung close can't wedge the run. Worth noting:

- **`solve_captchas` and `proxies` are always on** — they're what make the site
  reachable at all.
- **429 backoff on session creation** (`eb29a42`): the SDK already retries single
  429s; this is the outer net for sustained bursts, so a worker rides out a spike
  instead of dying.
- **`max_sessions`** caps concurrent sessions to the plan's limit, via
  `BROWSERBASE_MAX_CONCURRENCY` (`f0a9736`). Default 25; opt into 100 so a big
  sweep doesn't contend with production on the same key.
- **The `httpx` client lives on the session** and is closed in teardown. The
  per-file API is called against `api.browserbase.com` directly, not through the
  browser — which also means confirming downloads doesn't burn (billed) proxy
  bandwidth.

---

## 11. Testing

The tests are **pure-function only** — no network, no browser, just a tiny fake
page for the paging loop. The parts that can silently produce *wrong* output
(docId parsing, PDF completeness, the paging walk, metadata extraction, date
parsing, the pool's resubmission) are all pure and cheap to test. The parts that
need a real browser are checked by **live end-to-end runs** before each perf
commit. Fast tests guard the tricky logic; the live run guards the integration.

When the download mechanism changed, the old zip-parsing tests were replaced by
tests for the new helpers (`_pick_download_files`, `_is_complete_pdf`) rather than
left as dead coverage.

---

## 12. What we deliberately didn't build

So the gaps read as intent, not oversight:

- **No second-court abstraction** beyond the `TrialScraper` seam. One court
  exists; that's the extension point when a second arrives.
- **No real database.** A JSON file with atomic writes fits the scale, and the
  schema (hash + size, no bytes) is already dedup-friendly if a store is added.
- **No document-type taxonomy.** `_is_opinion` is a keyword heuristic with a
  `TODO`; a real system would map document-type codes.
- **No tuning of `_TABS_PER_SESSION` / `LA_CONCURRENCY`** beyond the measured
  sweet spot. 16 sessions is where throughput peaks (§2.3); higher saturates the
  solver and costs more for less, so the defaults are set there and the knobs are
  documented.

---

## Appendix: decision → commit

| Decision | Commit |
| --- | --- |
| Factory moved out of `ports/` to shared `src/` | `b35a7be` |
| Initial: one session, serial cases, pagination + downloads | `c5bf677` |
| Per-case error isolation; zip retry | `1333c8d` |
| Download preview retry | `6d41027` |
| Concurrent per-document downloads; blind sleeps removed; paging helper extracted | `c5702b5` |
| Concurrent workers; in-page search; fresh session per download | `e021bba` |
| Quota reserved before scrape | `114532f` |
| Persist to JSON; decode HTML entities | `c30ca7c` |
| Env-configurable session cap | `f0a9736` |
| One session per worker | `9ac3f3a` |
| Session-creation 429 backoff | `eb29a42` |
| Cross-contaminated search detection + retry | `c9e789a` |
| CDP download events + per-file API; drop recycling | `c4f4384` |
| Pooled downloads across sessions | `292abef` |
