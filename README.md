# SBI Forex Card Rates — self-hosted archive and JSON API

Fetches SBI's forex card rate PDF directly from the bank, archives every distinct card,
extracts the rate tables to JSON, and publishes them as a static API — all from one GitHub
Action. The PDFs are Word-generated with a real text layer, not scans, so extraction is exact:
no OCR, no guessing.

History from
[skbly7/sbi-tt-rates-historical](https://github.com/skbly7/sbi-tt-rates-historical) is
imported once at setup, so the series starts in June 2020 rather than today.

Everything here was produced locally; nothing has been committed or pushed.

## What came out

| | |
|---|---|
| Source PDFs seen | 4,054 (2020-06-02 → today) |
| Archived as distinct cards | 1,410 — 450 MB, down from 1.2 GB |
| Unusable downloads | 341 (bad fetches, not parse failures) |
| Rate rows in the API | 67,575 |
| Currencies | 31 |
| Full rebuild | ~12 s for 4,054 PDFs, ~5 s for the archive (8 cores) |

The 341 unusable files are not parsing failures. They are what the daily `curl` in the
upstream `run.sh` actually saved on days when SBI's site did not serve the PDF: 24 zero-byte
files, 2 truncated files, and 315 HTML-rendered-to-PDF error pages ("The requested URL was
rejected", "Site Under Maintenance", the SBI homepage, a bot-check page). Most cluster in
2021 (208). They are listed with reasons in `api/v1/unavailable.json`.

## Layout

```
archive/YYYY/MM/*.pdf          committed. one PDF per distinct card. ~175 KB each
data/cards/YYYY/*.json         committed. the parsed form of each card. ~11 KB each
data/observations.ndjson       committed. one line per fetch, duplicates and failures too
api/v1/...                     built, not committed. deployed to Pages

tools/fetch_sbi.py             fetch + validate + dedupe + archive   (runs in the Action)
tools/build_cards.py           archive PDFs -> data/cards            (after a parser change)
tools/seed_archive.py          import an existing PDF tree           (run once)
tools/parse_sbi.py             one PDF -> tables (the extraction logic)
tools/extract_all.py           a PDF tree -> NDJSON, parallel
tools/build_api.py             cards or NDJSON -> static JSON API tree
tools/serve_api.py             NDJSON -> live HTTP API (stdlib only)
tools/convert.sh               rebuild the API locally
```

`data/cards/` holds the parsed form of every card, committed next to the PDF that produced
it. It exists so that routine CI never has to download the archive: the API is rebuilt from
these 11 KB files, and the PDFs are only read when the parser changes. One file per card,
written once and never rewritten, so the repo grows ~3 MB/year from it rather than by
rewriting a bulk file on every run.

## Running it

One-time, to start the archive with six years of history:

```bash
pip install -r tools/requirements.txt          # pymupdf
git clone https://github.com/skbly7/sbi-tt-rates-historical /tmp/sbi
python tools/seed_archive.py --source /tmp/sbi --archive archive \
       --log data/observations.ndjson          # ~11 s
```

Then enable Pages (Settings → Pages → Source: GitHub Actions) and push. After that the
workflow runs on its own. To do a cycle by hand:

```bash
python tools/fetch_sbi.py --archive archive --log data/observations.ndjson \
       --cards data/cards
./tools/convert.sh                  # rebuild api/ from data/cards  (~3 s)
./tools/convert.sh --from-pdfs      # re-derive cards from the PDFs first (~5 s)
```

After changing `parse_sbi.py`, re-derive every card with `./tools/convert.sh --from-pdfs`
locally, or run the **Reprocess archive** workflow, which is the one job that takes a full
checkout because the PDFs are its input.

## The GitHub Action

`.github/workflows/fetch-and-publish.yml` runs at 04:15, 07:15 and 12:15 UTC
(≈09:45, 12:45, 17:45 IST), Monday–Saturday. Sunday is skipped because no rate card has
ever been issued on one. Each run fetches, commits, and — only when the card is actually new
— rebuilds the API and deploys it to Pages.

**It does not download the archive.** The job writes one PDF and rebuilds the API from
`data/cards/`, so it checks out with `filter: blob:none` and a sparse checkout of `tools`,
`data` and `.github`. Measured on the real repo: **567 MB → 23 MB per run, 24× smaller**, with
1 PDF on disk instead of 1,412. New PDFs still commit into `archive/` normally — but this
needs `git add --sparse`, because a plain `git add` on a path outside the sparse cone stages
nothing *and still exits 0*, which would drop the PDF while the run stayed green. The workflow
asserts a PDF is actually staged whenever a new card was archived, so that failure is loud
rather than a silent gap discovered months later.

Three more things it does that a bare `curl` in a cron job does not:

**It validates before archiving.** SBI's WAF answers with an HTML error page rendered as a
PDF. 315 of the 4,054 files in the reference archive are exactly that, saved as if they were
data, plus 24 zero-byte files. `fetch_sbi.py` parses the document and requires a real rate
grid (≥20 currency rows) before it will keep it. A block is recorded as a failed observation,
never archived as if it were a rate.

**It stores one copy per distinct card.** SBI serves the same card to everyone until the next
one is issued, so fetching three times a day otherwise archives the same document repeatedly.
Identity is a hash of what the card *says* — date, time and every rate — not of the PDF bytes,
so a re-render with new metadata is still recognised as the same card. On the imported
history this collapses 4,054 files (1.2 GB) to **1,410 cards (450 MB)**.

**It keeps the fetch log anyway.** Deduplication would otherwise destroy the record of what
the site served when, which is what makes the staleness analysis above possible.
`data/observations.ndjson` keeps one line per attempt — duplicates and failures included —
carrying the capture time, both hashes, and which archived card it matched.

A failed fetch still commits its log line, then fails the run so you get a notification.

### What could go wrong

- **GitHub's runner IPs may be blocked.** This is the real risk and I could not test it from
  here — it needs a push to find out. SBI's WAF blocked the upstream author often enough that
  they switched to `curl-impersonate`; plain `curl` and plain Python both work fine from a
  residential IP today. The fetcher retries with backoff across both SBI hostnames and sends
  browser headers, and failures are recorded rather than silently archived, so you will know
  immediately. If runners turn out to be blocked, the options are `curl-impersonate` in the
  workflow, a self-hosted runner, or a proxy — the URL and headers are in one place at the top
  of `fetch_sbi.py`.
- **Scheduled workflows are best-effort.** GitHub delays or drops cron runs under load, so
  treat three runs a day as "usually three". They are also disabled after 60 days of repo
  inactivity, which the daily commits prevent.
- **Repo size.** 450 MB seeded, growing ~45 MB/year (260 cards/year at ~175 KB; the PDFs
  shrank from 476 KB in 2022-23). Nothing hard stops you: GitHub's hard limits are 100 MB per
  file and 2 GB per push, neither reachable here. The 1 GB "ideally under" figure is advice,
  not enforcement, and arrives around 2039. Pages' 1 GB site limit is ~70 years away at
  13.5 MB/year. If you would rather not carry the history at all, skip the seeding step and
  link to the upstream repo for pre-2026.
- **Pages bandwidth is a 100 GB/month soft limit** — enforced by a human reading usage
  reports, not by a cutoff. The query endpoints are tiny gzipped (index 1.5 KB, a currency
  series 33 KB), but `bulk/rates.ndjson` is 1.6 MB and could add up if scripts re-fetch it
  rather than caching. Put Cloudflare in front, or move the bulk dumps to release assets, if
  that ever shows up.

## The static API

`api/v1/` is plain files, so it needs no server — drop it on GitHub Pages, S3, or any CDN
and it is an API. Paths are in `api/v1/index.json`.

| Endpoint | What it gives |
|---|---|
| `v1/index.json` | manifest: currencies, slabs, field docs, date range |
| `v1/latest.json` | the most recent snapshot, fully expanded |
| `v1/snapshots/{YYYY-MM-DD-HHMM}.json` | one capture, both slabs, all currencies |
| `v1/snapshots.json` | list of all 3,713 snapshot ids |
| `v1/daily/{YYYY-MM-DD}.json` | everything captured on one day |
| `v1/currencies/{CODE}.json` | full history for one currency (USD ≈ 50 KB gzipped) |
| `v1/bulk/rates.ndjson` / `rates.csv` | all 177,367 rows, flat |
| `v1/unavailable.json` | the 341 snapshots that never downloaded |
| `v1/effective.json` | **calendar date → the card in force that day** |
| `v1/observations.ndjson` | every fetch ever made, duplicates and failures included |

`currencies/` and `daily/` are columnar (`columns` + `rows` arrays) because repeating keys
177k times tripled the payload for no gain.

```bash
curl -s .../v1/currencies/USD.json | jq '.rows[-1]'

# "what rate applied on a Sunday" — effective.json answers it, the others cannot
curl -s .../v1/effective.json | jq '.dates["2026-09-13"]'   # -> "2026-09-11-1415"
```

`effective.json` exists because the archive holds cards, not days: there is no card dated
2026-09-13, but a rate was in force that day. It maps every calendar date to the card that
applied, so weekend and holiday lookups resolve instead of 404ing.

## The live API

If you want query parameters rather than static files:

```bash
python3 tools/serve_api.py --data api/v1/bulk/rates.ndjson --port 8787

curl "localhost:8787/v1/latest?currency=USD&slab=10_to_20_lakh"
curl "localhost:8787/v1/rates?currency=EUR&from=2025-01-01&to=2025-12-31&field=tt_buy"
curl "localhost:8787/v1/rates?currency=JPY&from=2026-09-01&format=csv"
curl "localhost:8787/v1/snapshot/2026-09-14-1915"
curl "localhost:8787/v1/currencies"
```

## Record shape

```json
{
  "snapshot_id": "2026-09-14-1915",
  "captured_at": "2026-09-14T19:15:00+05:30",
  "published_date": "2026-09-11",
  "published_time": "09:11",
  "source_pdf": "2026/09/2026-09-14-19:15.pdf",
  "tables": [{
    "slab": "10_to_20_lakh",
    "columns": ["tt_buy", "tt_sell", "..."],
    "rates": [{
      "currency": "USD", "currency_name": "UNITED STATES DOLLAR", "unit": 1,
      "tt_buy": 95.3, "tt_sell": 96.15,
      "bill_buy": 95.23, "bill_sell": 96.32,
      "travel_card_buy": 95.23, "travel_card_sell": 96.32,
      "currency_note_buy": 94.1, "currency_note_sell": 96.7
    }]
  }]
}
```

**`captured_at` vs `published_date` are different things and both matter.** The filename is
when the cron job fetched the PDF; the date printed inside the PDF is when SBI issued that
rate card. They disagree for 1,098 of 3,706 snapshots, and the reason is the bank calendar —
see below. Use `published_date` for "what rate was in force", `captured_at` for "what was on
the website at that moment". `card_age_days` and `stale` carry the gap so you can filter on it.

## How the rates actually work

SBI issues **one FOREX CARD RATES card per bank working day**, normally around 9–10 AM IST,
and occasionally revises it intraday. The website always serves the latest card, so fetching
on a non-working day hands you the previous working day's card unchanged. That is the entire
explanation for the `captured_at` / `published_date` gap, and the data confirms it exactly:

| Capture weekday | Snapshots | Same-day card | Stale |
|---|---:|---:|---:|
| Mon | 526 | 461 | 12.4% |
| Tue | 528 | 468 | 11.4% |
| Wed | 528 | 471 | 10.8% |
| Thu | 527 | 483 | 8.3% |
| Fri | 529 | 469 | 11.3% |
| **Sat** | 538 | 256 | **52.4%** |
| **Sun** | 530 | **0** | **100%** |

Three rules fall straight out of the archive:

1. **Never on a Sunday.** 275 Sundays captured; across all 3,713 snapshots, not one rate card
   is dated a Sunday. Zero.
2. **Not on the 2nd or 4th Saturday** — the RBI/IBA rule that Indian banks close on those
   days. Split by which Saturday of the month it is, the pattern is unmistakable:

   | | 1st Sat | 2nd Sat | 3rd Sat | 4th Sat | 5th Sat |
   |---|---:|---:|---:|---:|---:|
   | same-day card | 89.3% | **0%** | 86.9% | **0%** | 93.2% |

   128 second/fourth Saturdays were captured, and no card anywhere in the archive is dated one.
3. **Not on bank holidays.** That accounts for the residual ~10% on weekdays and the ~11% on
   working Saturdays. 157 weekday dates served a stale card; for 142 of them (90%) *no rate
   card bearing that date exists anywhere in the archive*, meaning SBI published nothing that
   day. They read exactly like the holiday list — 2025-12-25, 2026-01-26, 2026-05-01,
   2026-03-31/04-01 (bank annual closing), and assorted festival dates.

So a lag of 1 day is a Sunday-or-holiday fetch, 2 days is typically a Sunday after a closed
Saturday, and 3 days is a closed-Saturday + Sunday + Monday-holiday run. The maximum observed
is 6 days. The latest snapshot in the archive shows it: captured Mon 2026-09-14, serving the
card issued Fri 2026-09-11, because the 12th was a second Saturday and the 13th a Sunday.

**Intraday revisions are rare.** Of 1,779 days with both a 14:15 and a 19:15 fetch, the card
stamp changed between them on 24 (1.3%) and at least one TT-buy number moved on 47 (2.6%).
The twice-daily fetch is therefore near-duplicate by design; dedupe on `published_date` +
`published_time` if you want distinct cards rather than distinct observations.

**Issue times** cluster hard in the morning: 2,535 cards at 09:xx and 487 at 10:xx, with a
thin tail through 17:xx for revisions. 245 snapshots carry no legible time stamp.

**Rate types.** `tt_*` (telegraphic transfer) is the wire-remittance rate and the one most
people want; `bill_*` applies to trade bills, `travel_card_*` to forex cards, `currency_note_*`
to physical cash — note rates carry the widest spread. Buy is the bank buying foreign currency
from you (inward remittance), sell is the bank selling it to you (outward).

**Slabs.** Most PDFs carry two tables: `below_10_lakh` and `10_to_20_lakh` (reference
rates). Since late 2023 SBI publishes only `10_to_20_lakh`, so that slab exists for all
3,713 snapshots and `below_10_lakh` for 2,534.

**`unit`.** JPY, THB and KRW are quoted per 100 units of foreign currency, per the note in
the PDF. The field carries `100` for those and `1` for everything else; values are left
exactly as printed.

**Missing keys mean a blank cell in the source PDF**, not a parse gap. There are only 4 in
the entire archive.

**Date and time stamps needed normalising.** Most cards print `DD-MM-YYYY`, but a run of 2020
files uses slashes and mixes day-first (`08/06/2020`) with month-first (`7/3/2020`) *in the
same weeks*, so the token alone is ambiguous; the parser resolves it against the capture date,
since a card is never issued after the day it was downloaded. Times are similarly uneven —
`9:30 AM`, `01:30 P.M.` with dots, and 24-hour readings that still carry a meridiem
(`13:20 pm`) which must not be shifted twice. Every published date and time in the output is
now valid and no card is dated after its capture.

## Why the parser is coordinate-based

Reading the text layer in document order does not work: adjacent cells merge
(`"231.8 246.89"` arrives as one line) and the slab caption interleaves with the header row.
So `parse_sbi.py` works off word bounding boxes:

1. Rows are clustered by **vertical overlap**, not baseline distance — the currency name and
   its numbers are often set in different point sizes, so their midpoints sit several points
   apart while the glyph boxes still overlap.
2. Column centres are derived from the numbers themselves (the modal cell layout across all
   data rows), so a row with a blank cell cannot shift the whole row.
3. Header text is assigned to columns as **word clusters**, not individual words, because
   wrapped headers ("FOREX TRAVEL" over "CARD BUY") are not centred on their column.
4. Headers are mapped by rule, not by string match, because the wording drifts across the
   archive: `TC BUY` (2020–22) → `FTC BUY` (2023) → `FOREX TRAVEL CARD BUY` (2024+), plus a
   `FOREIGN TRAVEL CARD BUY` variant. `PC BUY` disappears in late 2023. All of these land on
   the same canonical field names, so the series are continuous.

Every one of the 6,247 tables has all of its columns mapped — there are no `col_N` fallbacks
left in the output.

## Verification

Run `python tools/verify.py` after any change to the parser — structure,
invariants, fetch-log consistency, and a sampled re-read of the PDFs, exiting
non-zero on failure. Known SBI source errors are listed in
`data/known_source_quirks.json` and excluded, so a new violation is visible.

- **Value fidelity.** 300 randomly sampled snapshots re-opened and checked against the raw
  PDF word tokens: 127,012 values, 0 mismatches.
- **Column assignment.** Buy/sell ordering holds on every row that has both values —
  0 violations in 120,733 TT pairs, 148,088 bill pairs, 150,324 currency-note pairs.
- **Known source quirks, reproduced faithfully.** ~0.6% of rows break the weaker
  `bill_buy ≤ tt_buy` expectation. Spot-checked against the PDFs: they are SBI's own
  errors. `2021-11-29` prints OMR bill-buy as `199.74`, the same as its TT-sell.
  `2026-02-11` prints SAR travel-card-sell as `22.99` when every neighbouring cell says
  ~24.8. THB accounts for 785 of them, where SBI's TT and bill columns routinely disagree.
  The extractor does not silently "fix" these; the numbers are what the bank published.

## Licence

Code is MIT (see `LICENSE`). The archived PDFs are SBI's and are redistributed
unmodified for provenance; `NOTICE` sets out what each licence covers.

## Data and attribution

The rates are published by **State Bank of India** in its daily FOREX CARD RATES PDF, which
is the only authoritative source. This repository re-publishes those figures in a machine
readable form and keeps the original PDFs unmodified so any number can be traced back to the
document it came from.

History before September 2026 was imported from
[skbly7/sbi-tt-rates-historical](https://github.com/skbly7/sbi-tt-rates-historical), which has
collected the PDF twice a day since June 2020. That archive is the reason this series starts
in 2020 rather than today. `data/observations.ndjson` records all 4,054 imported fetches with
their SHA-256, so every card here can be verified against the file it came from.

This is an unofficial project with no connection to or endorsement from SBI.

## Caveats

- Rates are published by SBI and are indicative; the PDFs say the applicable rate is the one
  at the time of debit/credit.
- Times are IST (+05:30). The single 2020-06-02 file has no time in its name and gets
  `0000`. 245 snapshots have no legible time printed on the card itself.
- The archive is a record of *what the website served*, not of every card SBI issued. If the
  scraper missed a day (see `unavailable.json`), a card issued that day may not appear at all.
- This is a derived dataset. The PDFs remain the record of truth.
