# SBI Forex Card Rates — JSON API

State Bank of India publishes its forex card rates as a PDF, once per bank working day. This
fetches that PDF three times a day, archives every distinct card, extracts the rate tables and
serves them as JSON. No OCR: the PDFs have a real text layer, so extraction is exact.

**https://rbm897.github.io/sbi-forex-rates-api/v1/index.json**

Static files on GitHub Pages — no key, no rate limit, `Access-Control-Allow-Origin: *`, gzipped.

```bash
BASE=https://rbm897.github.io/sbi-forex-rates-api

curl -s $BASE/v1/latest.json | jq '.tables[0].rates[] | select(.currency=="USD")'
curl -s $BASE/v1/currencies/USD.json | jq '.rows[-1]'          # full history, 33 KB gzipped
curl -s $BASE/v1/effective.json | jq '.dates["2026-09-13"]'    # which card applied that day
```

| | |
|---|---|
| Coverage | 2020-06-02 → today, 1,400+ cards, 67,000+ rate rows, 31 currencies |
| Updated | 04:15, 07:15, 12:15 UTC, Mon–Sat |
| Source | `FOREX_CARD_RATES.pdf` from sbi.bank.in, archived in `archive/` |

Exact counts are in [`v1/index.json`](https://rbm897.github.io/sbi-forex-rates-api/v1/index.json),
which is regenerated on every update — the figures above are rounded because the archive grows
about a card a day.

## Endpoints

| Path | What you get |
|---|---|
| `v1/index.json` | manifest: currencies, slabs, field docs, date range |
| `v1/latest.json` | the most recent card, fully expanded |
| `v1/effective.json` | **calendar date → the card in force that day** |
| `v1/currencies/{CODE}.json` | one currency's full history (columnar) |
| `v1/snapshots/{id}.json` | one card, both slabs, all currencies |
| `v1/snapshots.json` | every snapshot id |
| `v1/daily/{YYYY-MM-DD}.json` | everything captured on one day |
| `v1/bulk/rates.{ndjson,csv}` | every row, flat (~1.6 MB gzipped) |
| `v1/observations.ndjson` | every fetch ever made, duplicates and failures included |
| `v1/unavailable.json` | the 341 fetches that never returned a rate card |

## Record shape

```json
{
  "snapshot_id": "2026-09-16-1921",
  "captured_at": "2026-09-16T19:21:00+05:30",
  "published_date": "2026-09-16",
  "published_time": "09:11",
  "card_age_days": 0,
  "stale": false,
  "source_pdf": "2026/09/2026-09-16-1921.pdf",
  "source_url": "https://raw.githubusercontent.com/rbm897/sbi-forex-rates-api/main/archive/2026/09/2026-09-16-1921.pdf",
  "tables": [{
    "slab": "10_to_20_lakh",
    "rates": [{
      "currency": "USD", "currency_name": "UNITED STATES DOLLAR", "unit": 1,
      "tt_buy": 95.5, "tt_sell": 96.35,
      "bill_buy": 95.43, "bill_sell": 96.52,
      "travel_card_buy": 95.43, "travel_card_sell": 96.52,
      "currency_note_buy": 94.3, "currency_note_sell": 96.9
    }]
  }]
}
```

`tt_*` (telegraphic transfer) is the wire-remittance rate and the one most people want.
`bill_*` applies to trade bills, `travel_card_*` to forex cards, `currency_note_*` to physical
cash — note rates carry the widest spread. **Buy** is the bank buying foreign currency from you
(inward remittance); **sell** is the bank selling it to you. `pc_buy` (pre-shipment credit)
exists only before late 2023, when SBI dropped the column.

**Slabs.** `10_to_20_lakh` is on every card. `below_10_lakh` appears only up to late 2023, when
SBI stopped publishing it.

**`unit`.** JPY, THB and KRW are quoted per 100 units of foreign currency, per the note in the
PDF. Values are left exactly as printed.

**A missing key means a blank cell in the source PDF**, not a parse gap. There are 3 in the
whole archive.

## The thing that will bite you

`captured_at` is when the PDF was fetched. `published_date` is what SBI printed on it. **They
differ for 30% of fetches**, because SBI issues a card only on bank working days and serves the
last one until the next is due. Three rules, all confirmed from the data:

1. **Never on a Sunday.** 530 Sundays fetched; not one card in the archive is dated one.
2. **Not on the 2nd or 4th Saturday** (the RBI/IBA rule). Same-day publication by ordinal
   Saturday: 89% / **0%** / 87% / **0%** / 93%.
3. **Not on bank holidays** — the residual ~10% on weekdays.

So use `published_date` for "what rate was in force" and `captured_at` for "what the site was
serving". `card_age_days` and `stale` give you the gap directly.

This is also why there is one snapshot per *card*, not per day or per fetch: SBI serves the same
card until the next is issued, so repeated fetches collapse into one. The imported history shows
the ratio — 4,054 fetches, 1,410 distinct cards. Nothing is lost: every attempt is logged in
`observations.ndjson`, duplicates and failures included.

`v1/effective.json` exists for the same reason: no card is dated Sunday 2026-09-13, but a rate
applied that day. It maps every calendar date to the card in force, so weekend and holiday
lookups resolve instead of 404ing.

## Data quality

Every PDF is parsed before it is archived, so a fetch that returns something other than a rate
card never becomes data. Of the 4,054 imported fetches, 341 were unusable: 315 were error or
maintenance pages rendered as PDFs by SBI's WAF, 24 were empty and 2 truncated. All are listed
in `v1/unavailable.json`.

Figures are otherwise reproduced exactly as printed, including SBI's own mistakes. About 0.6% of
rows break the weak expectation `bill_buy ≤ tt_buy`; spot-checked against the PDFs, those are the
bank's errors, not extraction bugs — 2021-11-29 prints OMR bill-buy identical to its TT-sell,
2026-02-11 prints SAR travel-card-sell as 22.99 among ~24.8 neighbours. Confirmed cases
are listed in `data/known_source_quirks.json`. Nothing is silently corrected.

Invariants that hold across the whole dataset: buy ≤ sell on every buy/sell pair, no card dated
after its capture, and a sampled re-read of the PDFs confirming every number in the JSON appears
in the page it came from. `python tools/verify.py` runs the lot.

Every row is traceable: `source_url` downloads the exact PDF, and `observations.ndjson` holds
the SHA-256 recorded when it was fetched.

## Local use and development

```bash
pip install -r tools/requirements.txt
python tools/serve_api.py --data api/v1/bulk/rates.ndjson --port 8787

curl "localhost:8787/v1/rates?currency=EUR&from=2025-01-01&field=tt_buy"
curl "localhost:8787/v1/rates?currency=JPY&from=2026-09-01&format=csv"
```

That gives query parameters over the same data. To rebuild the API: `./tools/convert.sh`, and
`python tools/verify.py` to check it.

Changing the parser: read **[CLAUDE.md](CLAUDE.md)** first. It covers why extraction works off
word coordinates rather than the text layer, the date and header formats that drift across six
years, and the CI traps.

## Licence and attribution

Code is MIT (`LICENSE`). It does not cover the data — see `NOTICE`. The rates are published by
**State Bank of India**; the PDFs are redistributed unmodified so any number stays traceable to
its source. PDFs before September 2026 were imported from
[skbly7/sbi-tt-rates-historical](https://github.com/skbly7/sbi-tt-rates-historical), which has
collected them twice daily since June 2020.

Unofficial project, not affiliated with or endorsed by SBI. The rates are indicative — the PDFs
state that the applicable rate is the one prevailing at the time of debit or credit. Don't rely
on this for settlement.
