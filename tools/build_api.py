"""Turn the extracted NDJSON into a static, fetchable JSON API tree."""
import argparse
import csv
import json
import os
import shutil
import datetime
from collections import Counter, OrderedDict, defaultdict

FIELDS = ["tt_buy", "tt_sell", "bill_buy", "bill_sell", "travel_card_buy",
          "travel_card_sell", "currency_note_buy", "currency_note_sell", "pc_buy"]

FIELD_DOC = OrderedDict([
    ("tt_buy", "Telegraphic transfer buying rate (inward remittance)"),
    ("tt_sell", "Telegraphic transfer selling rate (outward remittance)"),
    ("bill_buy", "Bill buying rate (export bills)"),
    ("bill_sell", "Bill selling rate (import bills)"),
    ("travel_card_buy", "Forex travel card buying rate (printed as TC/FTC in older PDFs)"),
    ("travel_card_sell", "Forex travel card selling rate (printed as TC/FTC in older PDFs)"),
    ("currency_note_buy", "Currency note buying rate"),
    ("currency_note_sell", "Currency note selling rate"),
    ("pc_buy", "Pre-shipment credit buying rate (dropped from the PDF in late 2023)"),
])

SLAB_DOC = {
    "below_10_lakh": "Card rates for transactions below Rs. 10 lakh",
    "10_to_20_lakh": "Card rates for transactions between Rs. 10 lakh and Rs. 20 lakh (reference rates)",
}


def _date(s):
    return datetime.date(*(int(x) for x in s.split("-")))


def write_json(path, obj, indent=None):
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w") as fh:
        json.dump(obj, fh, indent=indent, separators=(",", ":") if indent is None else None)
        fh.write("\n")


def snapshot_key(rec):
    t = (rec.get("captured_time") or "00:00").replace(":", "")
    return "%s-%s" % (rec["captured_date"], t)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cards", default="data/cards",
                    help="directory of per-card JSON written by build_cards")
    ap.add_argument("--out", required=True)
    ap.add_argument("--observations",
                    help="observations.ndjson: the log of every fetch, including "
                         "duplicates and failures")
    ap.add_argument("--repo-url",
                    default="https://github.com/rbm897/sbi-forex-rates-api",
                    help="this repository, where the archived PDFs live")
    ap.add_argument("--branch", default="main")
    ap.add_argument("--archive-prefix", default="archive",
                    help="path to the archive within the repository")
    args = ap.parse_args()

    # source_pdf is stored relative to the archive root, so a usable link needs
    # the repo, the branch and that prefix put back in front of it. Points at
    # raw rather than blob: this is an API, so callers want the bytes.
    owner_repo = args.repo_url.rstrip("/").split("github.com/")[-1]
    raw_base = "https://raw.githubusercontent.com/%s/%s/%s" % (
        owner_repo, args.branch, args.archive_prefix.strip("/"))

    from build_cards import load_cards
    recs = load_cards(args.cards)
    ok = [r for r in recs if r["status"] == "ok"]
    if not ok:
        raise SystemExit("no parsed cards found in %s" % args.cards)
    ok.sort(key=snapshot_key)

    out = args.out
    if os.path.isdir(out):
        shutil.rmtree(out)
    base = os.path.join(out, "v1")

    long_rows = []
    by_currency = defaultdict(list)
    by_date = defaultdict(list)
    currencies = {}
    snapshots_by_key = {}
    slabs = Counter()

    for rec in ok:
        key = snapshot_key(rec)
        captured_at = "%sT%s:00+05:30" % (rec["captured_date"],
                                          rec.get("captured_time") or "00:00")
        # How old the card SBI served was. SBI issues no card on Sundays, on the
        # 2nd/4th Saturday, or on bank holidays, so a weekend or holiday fetch
        # returns the previous business day's card unchanged.
        age = None
        if rec.get("published_date"):
            age = (_date(rec["captured_date"]) - _date(rec["published_date"])).days
        snap = {
            "snapshot_id": key,
            "captured_at": captured_at,
            "published_date": rec.get("published_date"),
            "published_time": rec.get("published_time"),
            "card_age_days": age,
            "stale": None if age is None else age > 0,
            "source_pdf": rec["source_pdf"],
            "source_url": "%s/%s" % (raw_base, rec["source_pdf"]),
            "tables": [],
        }
        for tab in rec["tables"]:
            slabs[tab["slab"]] += 1
            rates = []
            for row in tab["rates"]:
                currencies.setdefault(row["currency"], row["currency_name"])
                clean = {"currency": row["currency"],
                         "currency_name": row["currency_name"],
                         "unit": row["unit"]}
                for f in FIELDS:
                    if f in row:
                        clean[f] = row[f]
                rates.append(clean)
                flat = {"snapshot_id": key, "captured_at": captured_at,
                        "published_date": rec.get("published_date"),
                        "published_time": rec.get("published_time"),
                        "card_age_days": age,
                        "stale": None if age is None else age > 0,
                        "slab": tab["slab"], **clean}
                long_rows.append(flat)
                by_currency[row["currency"]].append(flat)
                by_date[rec["captured_date"]].append(flat)
            snap["tables"].append({"slab": tab["slab"],
                                   "columns": tab["columns"],
                                   "rates": rates})
        snapshots_by_key[key] = snap
        write_json(os.path.join(base, "snapshots", key + ".json"), snap)

    # Per-currency time series. These are the long ones, so they are columnar:
    # a "columns" header plus plain arrays, which is a third the size of
    # repeating every key on every row.
    series_cols = ["snapshot_id", "published_date", "published_time",
                   "card_age_days", "slab"] + FIELDS
    for cur, rows in by_currency.items():
        write_json(os.path.join(base, "currencies", cur + ".json"), {
            "currency": cur,
            "currency_name": currencies[cur],
            "unit": rows[-1]["unit"],
            "count": len(rows),
            "columns": series_cols,
            "rows": [[r.get(c) for c in series_cols] for r in rows],
        }, indent=None)

    # Per-day files (every snapshot captured that day, all currencies).
    day_cols = ["snapshot_id", "published_date", "published_time",
                "card_age_days", "slab", "currency", "unit"] + FIELDS
    for date, rows in by_date.items():
        write_json(os.path.join(base, "daily", date + ".json"),
                   {"date": date, "count": len(rows), "columns": day_cols,
                    "rows": [[r.get(c) for c in day_cols] for r in rows]},
                   indent=None)

    # Latest snapshot.
    latest_key = snapshot_key(ok[-1])
    write_json(os.path.join(base, "latest.json"), snapshots_by_key[latest_key], indent=1)

    # Flat dumps for bulk users.
    os.makedirs(os.path.join(base, "bulk"), exist_ok=True)
    with open(os.path.join(base, "bulk", "rates.ndjson"), "w") as fh:
        for r in long_rows:
            fh.write(json.dumps(r, separators=(",", ":")) + "\n")
    cols = ["snapshot_id", "captured_at", "published_date", "published_time",
            "card_age_days", "stale", "slab", "currency", "currency_name",
            "unit"] + FIELDS
    with open(os.path.join(base, "bulk", "rates.csv"), "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for r in long_rows:
            w.writerow({c: r.get(c) for c in cols})

    obs = []
    observations = None
    if args.observations and os.path.exists(args.observations):
        obs = [json.loads(l) for l in open(args.observations)]
        shutil.copyfile(args.observations,
                        os.path.join(base, "observations.ndjson"))
        observations = {"total": len(obs),
                        "by_status": dict(Counter(o.get("status") for o in obs))}

    # Fetches that produced nothing usable. Building from data/cards/ there are
    # no failures to report -- only successful cards are written there -- so the
    # fetch log is the authority whenever it is available.
    if obs:
        unavailable = [{"source_pdf": o.get("imported_from"),
                        "captured_date": o["captured_at"][:10],
                        "captured_time": o["captured_at"][11:16],
                        "reason": o.get("error", "failed")}
                       for o in obs if o.get("status") == "failed"]
    else:
        unavailable = [{"source_pdf": r["source_pdf"],
                        "captured_date": r["captured_date"],
                        "captured_time": r.get("captured_time"),
                        "reason": r["status"]}
                       for r in recs if r["status"] != "ok"]
    write_json(os.path.join(base, "unavailable.json"),
               {"count": len(unavailable), "snapshots": unavailable}, indent=1)

    # date -> the card that was in force that day. SBI issues nothing on Sundays,
    # 2nd/4th Saturdays or bank holidays, so without this a consumer asking for a
    # weekend date gets nothing rather than the card that actually applied.
    effective, cur = {}, None
    by_pub = {}
    for rec in ok:
        if rec.get("published_date"):
            by_pub.setdefault(rec["published_date"], snapshot_key(rec))
    day = _date(ok[0]["captured_date"])
    last = _date(ok[-1]["captured_date"])
    if observations:
        # The archive only holds distinct cards, so it ends on the last day a NEW
        # card appeared. The log knows how far the record actually runs.
        seen_to = max((o["captured_at"][:10] for o in obs if o.get("captured_at")),
                      default=None)
        if seen_to:
            last = max(last, _date(seen_to))
    while day <= last:
        iso = day.isoformat()
        if iso in by_pub:
            cur = by_pub[iso]
        if cur:
            effective[iso] = cur
        day += datetime.timedelta(days=1)
    write_json(os.path.join(base, "effective.json"),
               {"description": "calendar date -> snapshot_id of the rate card in "
                               "force on that date",
                "count": len(effective), "dates": effective}, indent=None)

    index = {
        "name": "SBI Forex Card Rates — historical archive, extracted from PDF",
        "source_repository": args.repo_url,
        "source_pdf_note": ("source_pdf is relative to %s/ in the repository; "
                            "source_url is a direct download of that file"
                            % args.archive_prefix),
        "upstream_history": ("PDFs before 2026-09 were imported from "
                             "https://github.com/skbly7/sbi-tt-rates-historical"),
        "generated_from_pdfs": len(recs),
        "snapshots_parsed": len(ok),
        "snapshots_unavailable": len(unavailable),
        "date_range": {"first": ok[0]["captured_date"], "last": ok[-1]["captured_date"]},
        "latest_snapshot": latest_key,
        "timezone": "Asia/Kolkata (+05:30)",
        "row_count": len(long_rows),
        "slabs": {s: {"description": SLAB_DOC.get(s, ""), "tables": n}
                  for s, n in slabs.most_common()},
        "fields": FIELD_DOC,
        "note_per_100_units": ["JPY", "THB", "KRW"],
        "publication_calendar": (
            "SBI issues a rate card on bank working days only: never on a Sunday, "
            "never on the 2nd or 4th Saturday of a month, and not on bank holidays. "
            "A fetch on a non-working day returns the previous working day's card "
            "unchanged; card_age_days gives that lag in days and stale flags it."),
        "currencies": [{"code": c, "name": n, "snapshots": len(by_currency[c])}
                       for c, n in sorted(currencies.items())],
        "cards_archived": len(ok),
        "observations": observations,
        "endpoints": {
            "index": "v1/index.json",
            "latest": "v1/latest.json",
            "snapshot": "v1/snapshots/{YYYY-MM-DD-HHMM}.json",
            "day": "v1/daily/{YYYY-MM-DD}.json",
            "currency_series": "v1/currencies/{CODE}.json",
            "snapshot_list": "v1/snapshots.json",
            "bulk_ndjson": "v1/bulk/rates.ndjson",
            "bulk_csv": "v1/bulk/rates.csv",
            "unavailable": "v1/unavailable.json",
            "effective_date_map": "v1/effective.json",
            "fetch_log": "v1/observations.ndjson",
        },
    }
    write_json(os.path.join(base, "index.json"), index, indent=1)
    write_json(os.path.join(base, "snapshots.json"),
               {"count": len(ok),
                "snapshots": [{"snapshot_id": snapshot_key(r),
                               "captured_date": r["captured_date"],
                               "captured_time": r.get("captured_time"),
                               "published_date": r.get("published_date"),
                               "published_time": r.get("published_time"),
                               "slabs": [t["slab"] for t in r["tables"]]}
                              for r in ok]}, indent=None)

    print("wrote %s: %d snapshots, %d rows, %d currencies"
          % (base, len(ok), len(long_rows), len(currencies)))


if __name__ == "__main__":
    main()
