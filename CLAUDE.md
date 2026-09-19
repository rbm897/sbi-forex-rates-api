# CLAUDE.md

Guidance for Claude Code working in this repository. Read this before changing
the parser or the workflows — most of what follows was learned by getting it
wrong first.

## What this is

SBI publishes one FOREX CARD RATES PDF per bank working day. This repo fetches
it three times a day, archives every distinct card, extracts the rate tables,
and serves them as a static JSON API on GitHub Pages.

- Live API: https://rbm897.github.io/sbi-forex-rates-api/v1/index.json
- Source PDF: https://sbi.bank.in/documents/16012/1400784/FOREX_CARD_RATES.pdf
- History before 2026-09 imported from skbly7/sbi-tt-rates-historical

## Layout

```
archive/YYYY/MM/*.pdf     committed. one PDF per distinct card. source of truth
data/cards/YYYY/*.json    committed. parsed form of each card. what CI reads
data/observations.ndjson  committed. one line per fetch, duplicates + failures
api/                      gitignored. built and deployed to Pages

tools/parse_sbi.py        the extraction logic. all the subtlety lives here
tools/fetch_sbi.py        fetch + validate + dedupe + archive
tools/build_cards.py      archive PDFs -> data/cards. holds extract_one()
tools/build_api.py        cards -> the API tree
tools/seed_archive.py     one-time import of an external PDF tree
tools/serve_api.py        local HTTP API over the NDJSON, stdlib only
tools/verify.py           data integrity checks. run after any parser change
tools/convert.sh          rebuild api/ locally
```

## Common tasks

```bash
pip install -r tools/requirements.txt

./tools/convert.sh                  # rebuild api/ from data/cards   (~3 s)
./tools/convert.sh --from-pdfs      # re-derive cards from PDFs too  (~8 s)

python tools/fetch_sbi.py --archive archive --log data/observations.ndjson \
       --cards data/cards           # one real fetch
python tools/serve_api.py --data api/v1/bulk/rates.ndjson --port 8787
```

After changing `parse_sbi.py` you **must** re-derive the cards, or the API keeps
serving the old parse: `./tools/convert.sh --from-pdfs`, or run the *Reprocess
archive* workflow. That workflow is the only one that takes a full checkout,
because the PDFs are its input.

## Traps

**`git add` silently drops files in CI.** The fetch job uses a sparse checkout
(`tools`, `data`, `.github`), so `archive/` is outside the cone. A plain
`git add archive` there stages **nothing and still exits 0** — a green run that
loses the PDF. It must be `git add --sparse`. The workflow asserts a PDF is
staged whenever a card was archived; do not remove that guard.

**Never "fix" a rate that looks wrong.** SBI's own PDFs contain errors and the
extractor reproduces them deliberately. 2021-11-29 prints OMR bill-buy equal to
its TT-sell; 2026-02-11 prints SAR travel-card-sell as 22.99 among ~24.8
neighbours; THB's TT and bill columns routinely disagree. About 0.6% of rows
break `bill_buy <= tt_buy` for this reason. Verified against the source. If a
sanity check fires, check the PDF before touching the parser.

**Dates and times are not in one format.** Mostly `DD-MM-YYYY`, but a run of
2020 files uses slashes and mixes day-first (`08/06/2020`) with month-first
(`7/3/2020`) *in the same weeks*, so the token alone is ambiguous —
`resolve_date()` disambiguates against the capture date, since a card is never
issued after the day it was downloaded. Some print a two-digit year
(`05-10-20`). Times appear as `9:30 AM`, `01:30 P.M.` with dots, and 24-hour
readings that still carry a meridiem (`13:20 pm`) which must not be shifted
twice. The dot-separated time pattern only matches with a meridiem attached —
loosen that and every decimal rate on the page parses as a clock.

**Column headers drift.** `TC BUY` (2020-22) -> `FTC BUY` (2023) -> `FOREX
TRAVEL CARD BUY` (2024+), plus a `FOREIGN TRAVEL CARD` variant. `PC BUY`
disappears in late 2023. They are mapped by rule in `_field_from_label()`, not
by string match, so the series stay continuous. Any new wording goes there.

**`source_url` must point at this repo's archive.** `build_api.py` builds it
from `--repo-url` + `--branch` + `--archive-prefix`, because `source_pdf` is
stored relative to the archive root, not the repo root. This defaulted to the
upstream repo on `master` after the archive moved here, and every link in the
published API 404'd. `verify.py` now checks it offline, and
`--check-urls N` downloads N of them and compares SHA-256 against the fetch log.

**The fetch workflow commits before it verifies. Do not "fix" that.** It looks
inconsistent with `reprocess.yml`, which verifies first, and the asymmetry is
deliberate:

- *fetch* records something perishable. SBI serves one card until the next is
  issued, so a card not archived now is gone. And SBI's own cards sometimes
  break an invariant — 2026-02-11 printed SAR travel-card sell *below* its buy,
  which would have failed verify on the day it arrived, before the quirk was
  known. Verifying first would discard that day's genuine document because the
  bank made a typo, and every later run would fetch the same card and fail the
  same way, leaving a permanent hole.
- *reprocess* re-derives cards from PDFs that are already archived. Nothing
  perishable is at stake, so a bad re-derivation should never be committed.

Committing is recording; deploying is publishing. A verify failure blocks the
deploy — callers keep the last good build — and leaves the record intact.

**CI rebuilds on any committed change, not just a new card.** `effective.json`
runs to the latest *observation*, so on a Sunday, a 2nd/4th Saturday or a
holiday — when every fetch is a duplicate — skipping the rebuild leaves the
published map short of today, which are exactly the days it exists to answer.
The workflow gates on `steps.commit.outputs.changed`, not on `new_card`.

**`effective.json` maps the last card of a day, not the first.** SBI revises a
card intraday on ~36 dates in the archive; the revision is what was in force at
the close of that day and through the non-working days that follow. Build it by
assignment in capture order, never `setdefault`.

**`verify.py` runs in CI under a sparse checkout**, where `archive/` is not on
disk. Any check that reads the PDFs must be skipped when the archive is partial
(`full_archive`), or it fails on every run. Two checks needed this; the gate
caught the second one itself.

**Pushing something large fails with HTTP 400.** git's default
`http.postBuffer` is 1 MB. Set `git config http.postBuffer 524288000` and
`git config http.version HTTP/1.1`. Already set in this working copy.

## Why the parser reads coordinates, not text

Reading the PDF text layer in document order does not work: adjacent cells merge
(`"231.8 246.89"` arrives as one line) and the slab caption interleaves with the
header row. `parse_sbi.py` works off word bounding boxes.

- Rows cluster by **vertical overlap**, not baseline distance. The currency name
  and its numbers are often set in different point sizes, so their midpoints sit
  several points apart while the glyph boxes still overlap. This was a real bug:
  midpoint clustering dropped every USD row in ~1,260 tables.
- Column centres come from the **numbers themselves** (the modal cell layout
  across data rows), so a row with a blank cell cannot shift the whole row.
- Header text is assigned as **word clusters**, not individual words, because
  wrapped headers ("FOREX TRAVEL" over "CARD BUY") are not centred on their
  column. Clusters glued by tight spacing are split at BUY/SELL.

## How the rates actually work

SBI issues one card per bank working day, normally 09:00-10:00 IST, revised
intraday only rarely (~1.3% of days). The site serves the latest card until the
next is issued, so a fetch on a non-working day returns the previous working
day's card unchanged. Three rules, all confirmed from the data:

1. **Never on a Sunday.** Zero cards in the entire archive are dated one.
2. **Not on the 2nd or 4th Saturday** (the RBI/IBA rule). Same-day publication
   is ~89% on the 1st/3rd/5th Saturday and exactly 0% on the 2nd/4th.
3. **Not on bank holidays.** 90% of weekday dates that served a stale card have
   no card bearing that date anywhere in the archive.

Hence `captured_at` (when we fetched) and `published_date` (what SBI printed)
differ for ~30% of snapshots, and `card_age_days` / `stale` expose the gap.
`v1/effective.json` maps every calendar date to the card in force, which is the
only endpoint that answers "what rate applied on a Sunday".

## Verification before you ship a parser change

```bash
./tools/convert.sh --from-pdfs
python tools/verify.py --sample 300      # exits non-zero on failure
```

`verify.py` checks that every PDF has a card and vice versa, that buy <= sell
holds on every pair, that no card is dated after its capture, that every path in
the fetch log exists, and — by re-opening a random sample of PDFs — that every
number in the JSON actually appears in the page it came from. Baseline is 0
mismatches; 300 snapshots is ~127k values.

Known SBI source errors live in `data/known_source_quirks.json` and are excluded
from the invariant check, so a genuinely new violation stands out. If verify
reports an unexpected one, **read the PDF before touching the parser** — then
either fix the parser or add a quirk entry explaining what you confirmed.

## Conventions

- Match the existing style: plain stdlib where it will do, comments that explain
  *why* rather than restate the code, no framework dependencies. `pymupdf` is
  the only runtime requirement.
- `data/cards/` is append-only: one file per card, written once, never
  rewritten. A rewritten bulk file would add ~15 MB of git history per card.
- Don't commit `api/`. It is derived and deploys straight to Pages.
- Repo is ~31 MB packed and grows ~5.7 MB/year. PDFs delta-compress well because
  most of each file is identical embedded fonts — the on-disk 175 KB is 22 KB
  packed. Don't reason about repo size from `du`.
