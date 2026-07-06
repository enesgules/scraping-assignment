# Investigation: reverse-engineering the LA Court document system

How we figured out `lacourt.ca.gov` before writing a single line of the real
scraper. Each finding below came from driving the live site through an automated
browser, step by step, and watching what it did — pure detective work before any
building.

---

## The three obstacles we knew about going in

1. **The site blocks automated and data-center traffic.** A government web-app
   firewall rejects requests that don't look like a real person on a normal home
   internet connection. (More on exactly what "looks real" means below — it's
   the part people usually get wrong.)
2. **Every document download is behind a captcha** ("prove you're human").
3. **There's no simple download link.** You can't guess a document's address —
   you have to walk the whole search-and-click flow to reach it.

Everything we do is in service of getting past these three.

### What "looks like a real person" actually means

The site judges you on **two separate things**, and it helps to keep them apart:

- **Where the request comes from (the IP address).** A home internet connection
  is a "residential" address and is trusted. A rented cloud server — where any
  real scraper actually runs — has a "data-center" address, and those are
  blocked on sight. So even though *your own laptop* has a residential address,
  the moment you run the scraper on a server (which you must, to scrape at
  scale) it gets blocked.
- **Whether the client looks like a real browser (the "fingerprint").** A real
  Chrome window driven by a person sends hundreds of subtle signals a bare
  script simply doesn't have. So even from a home address, a plain script that
  fires requests faster than any human can click gets flagged as a bot.

You have to pass **both** checks. This is why the whole strategy is "make a real
browser do everything," not "grab the file with a quick script."

---

## Finding 0: we need a real-looking browser just to *look* at the site

Because of obstacle #1, we couldn't even investigate from a normal script — it
would be blocked at the door. So the first requirement was a browser that passes
both checks above.

**Browserbase** provides exactly that: a real Chrome browser running in the
cloud, reached through a **residential proxy** (so the site sees a home address
no matter where our code runs) and with **automatic captcha solving** built in.
Everything from here on — investigation and the final scraper — runs inside a
Browserbase browser.

---

## Finding 1: searching sends you to a login page first

**What we tried:** go straight to the documented search URL,
`…/DocumentImages/SearchCaseNumber`.

**What we found:** it doesn't show a search box. It **redirects to a login page**
with User ID / Password fields — and, crucially, a **"Continue as Guest"**
button. Reading the button's code showed it simply sends the browser to a page
called `GuestInformation`.

So the public way in is: don't log in — continue as guest.

---

## Finding 2: visiting "GuestInformation" gives us the cookies that unlock the site

**What we tried:** navigate to `…/GuestInformation`.

**What we found:** that one visit quietly hands our browser a set of **cookies**,
and then lands on the actual search form (a case-number box and a Search button).

**What a cookie is, plainly:** when you first arrive, the site gives your browser
a little ID token — a cookie. Your browser then automatically attaches that
token to *every* later request. That's how the site recognizes it's still you as
you click around, instead of treating every click as a brand-new stranger. We
never manage these by hand; the real browser stores and re-sends them for us.

Three cookies matter here, and each does a specific job:

- **`.AspNetCore.Session`** — the session ID. It's how the server keeps a little
  memory tied to *our* browser. This becomes important later: it's what lets the
  server remember *which case we're currently looking at* (see pagination).
- **`PAOSSessionInformation`** — the app's own "this visitor is an accepted
  guest" flag. Set the moment we visit `GuestInformation`. Without it, the site
  bounces us back to the login page.
- **`.AspNetCore.Antiforgery.*`** — a security cookie. It pairs with a hidden
  token inside the search form (`__RequestVerificationToken`). When we submit,
  the server checks that the cookie and the form token match — proof the form
  was genuinely served by them and not forged. Because a real browser submits
  the real form, this just works; we never touch it.

The takeaway: **the guest visit is what earns us the cookies, and the cookies are
what make every following step treat us as a legitimate visitor.**

Also worth noting: this is an old-style server-rendered site, not a modern app
with a clean data feed. Searching is a plain form **submission**, and the answer
comes back as a **full web page** we read — there's no tidy behind-the-scenes
data service to call.

---

## Finding 3: a search returns the case's documents as a web page

**What we tried:** type a case number we *constructed* from the format
(`19STCV12345` — the reasoning is in Finding 7) and submit.

**What we found:** a results page with the case details (title, type, filing
date) and a table of its documents. Each row shows the filing date, a description
("Request for Dismissal", "Complaint", "Minute Order", …), a page count, and a
checkbox with a machine ID like `Doc78869239`.

**How many documents?** It depends entirely on the case — there's no fixed
number. Some cases have **none** (nothing imaged online — we skip those); a
simple case has a **handful**; a big, long-running case has **dozens or
hundreds**. The site shows **50 documents per page** and splits the rest across
extra pages (how we handle that is Finding 4).

---

## Finding 4: long cases split documents across pages (50 per page)

**What we tried:** search several cases and look at the little "Page" bar at the
bottom of the results.

**What we found:** when a case has more than 50 documents, the bar shows extra
page links, e.g. page **2** and **3**. Each link is a plain address like
`…/DocumentImages/SelectDocuments?page=2`.

The clever part is *why that address needs no case number in it*: remember the
`.AspNetCore.Session` cookie from Finding 2. The server already remembers, tied
to our session, which case we just searched. So "give me page 2" is enough — the
server knows page 2 *of what*. We confirmed a 3-page case (`BC600000`) returns
50 + 50 + 16 documents, all distinct, just by visiting page 2 and page 3.

So to collect every document, we search (that's page 1), then walk to page 2,
3, … until there's no next page.

---

## Finding 5: each document has a free "Preview" path (with the keys we need)

The results table offers two ways to get a document:

- A **Submit** button that runs a *paid* checkout (there's a download fee). We
  ignore this.
- A per-row **Preview** button — the free path. Reading its code revealed the
  exact instruction the site runs:

  ```
  preview(docId, securityKey, caseType, source, caseNumber)
  ```

  e.g. `preview('78869237', '6B7NOHw7kb…', 'CV', 'DMS-6', '19STCV12345')`

That `securityKey` is the insight: it's a per-document pass, printed right into
the results page, that authorizes viewing that one document. So from a single
search we can read **every** document's ID and key straight out of the page —
no clicking required. Preview just opens this address:

```
…/DocumentImages/PreviewWait?id=<docId>&securityKey=<key>&…
```

---

## Finding 6: the Preview path ends at a one-time PDF link — guarded by a captcha

**What we tried:** follow that `PreviewWait` address and watch every step.

**What we found**, in order:

1. `PreviewWait` → forwards to a `Preview` page.
2. The `Preview` page shows a **captcha**. This is obstacle #2 — and Browserbase
   **solves it automatically** (takes roughly 20–60 seconds).
3. Once solved, the server sends a **one-time link** to the actual PDF, on a
   different server:

   ```
   https://ww2.lacourt.org/api/documents/v3.1/get/onetime/<docId>/<token>
   ```

The word **onetime** is the whole catch: the link works exactly once and then
dies. So we can't save the link and fetch it later — we have to capture the file
the instant the browser opens it.

---

## Finding 7: capturing the actual PDF file (the genuinely hard part)

Getting the *link* was easy. Getting the *bytes* took a few tries, because of
one core fact: **only the real cloud browser is behind the residential proxy.**
Anything *our* code fetches directly leaves from our own machine and gets
blocked (obstacle #1) — and the one-time link is spent the moment the browser
touches it anyway.

**What didn't work, and why:**

- Re-fetching the one-time link from our script → our script isn't behind the
  proxy, and the link is already used up.
- Asking the browser tooling to "save the download" → the browser is remote in
  Browserbase's cloud; the file never reaches our machine.

**What worked:** tell the cloud browser to save its downloads into Browserbase's
own storage (a one-line setup instruction). The browser downloads the PDF there;
then we ask Browserbase's API for the session's downloads and it hands us back a
zip of the files. First try after this fix: a real **383 KB PDF**. 

One convenience seals it: the saved filenames contain the document ID (e.g.
`e78869237(…).pdf`), so we can match each PDF back to the right document.

---

## Finding 8: case numbers are built, not looked up

LA Superior civil case numbers are predictable:

```
19 ST CV 12345
│  │  │  └── sequence: the Nth filing that year
│  │  └───── case type (CV = civil)
│  └──────── district / courthouse (ST = the big downtown courthouse)
└─────────── 2-digit filing year
```

The year comes from the date range you want; the sequence just counts up. That's
what lets us **generate** candidate case numbers to search, instead of needing a
list handed to us.

**This is also how we got our very first test case.** We didn't look
`19STCV12345` up anywhere — we built it from the pattern: 2019, downtown
courthouse, civil, a **low sequence number**. Low numbers in a busy year are
almost always real cases, because filings are numbered in order and that
courthouse handles tens of thousands a year. The guess landed on the first try —
a real case with documents — which both unblocked the investigation and proved
the generation idea works.

---

## Putting it all together — the full recipe

```
1. Open a Browserbase browser   → real-looking, proxied, solves captchas
2. Visit GuestInformation       → get the guest cookies (now trusted)
3. Search a case number         → a web page listing its documents,
                                   each with its own docId + securityKey
4. Walk the page bar            → 50 docs per page; collect pages 2, 3, …
                                   (the session remembers which case)
5. For each document:
     open PreviewWait?id=…&securityKey=…
       → captcha (Browserbase solves it)
       → one-time PDF link
       → the browser downloads it into Browserbase storage
6. Pull the downloads zip from Browserbase, match each PDF to its
   document by the ID in the filename → done
```

Every obstacle from the top maps to one piece of the solution:

| Obstacle | Beaten by |
| --- | --- |
| Blocks data-center addresses | Browserbase residential proxy |
| Blocks non-browser clients | Doing everything in a real Chrome |
| Captcha on every download | Browserbase automatic captcha solving |
| No direct download link | Walking guest → search → preview |
| One-time link, remote browser | Saving downloads into Browserbase storage |
