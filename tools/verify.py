"""Check the extracted data against the PDFs it came from.

Run this after any change to parse_sbi.py. Exits non-zero if a check fails, so
it works in CI or a pre-push hook.

    python tools/verify.py                 # fast checks + 100 sampled PDFs
    python tools/verify.py --sample 500     # sample harder
    python tools/verify.py --sample 0       # skip the PDF re-read
"""
import argparse
import collections
import json
import os
import random
import re
import sys

NUM = re.compile(r"^-?\d+(?:\.\d+)?$")
PAIRS = [("tt_buy", "tt_sell"), ("bill_buy", "bill_sell"),
         ("travel_card_buy", "travel_card_sell"),
         ("currency_note_buy", "currency_note_sell")]


def check(name, ok, detail=""):
    print("  %-46s %s%s" % (name, "PASS" if ok else "FAIL",
                            ("  " + detail) if detail else ""))
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default="api/v1")
    ap.add_argument("--archive", default="archive")
    ap.add_argument("--cards", default="data/cards")
    ap.add_argument("--log", default="data/observations.ndjson")
    ap.add_argument("--quirks", default="data/known_source_quirks.json",
                    help="rows where SBI's own PDF breaks an invariant")
    ap.add_argument("--sample", type=int, default=100,
                    help="PDFs to re-read and compare against the JSON (0 skips)")
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()
    ok = True

    print("structure")
    pdfs = {os.path.basename(p)[:-4]
            for d, _, ns in os.walk(args.archive) for p in ns if p.endswith(".pdf")}
    cards = {os.path.basename(p)[:-5]
             for d, _, ns in os.walk(args.cards) for p in ns if p.endswith(".json")}
    ok &= check("every archived PDF has a parsed card", pdfs <= cards,
                "missing: %d" % len(pdfs - cards))
    ok &= check("every parsed card has an archived PDF", cards <= pdfs,
                "orphans: %d" % len(cards - pdfs))

    rows_path = os.path.join(args.api, "bulk", "rates.ndjson")
    if not os.path.exists(rows_path):
        print("\n  %s missing -- run ./tools/convert.sh first" % rows_path)
        return 1
    rows = [json.loads(l) for l in open(rows_path)]

    # SBI's own PDFs contain errors. Those are reproduced faithfully, so the
    # invariant check excludes the ones already confirmed against the source --
    # otherwise it fails on every run and stops being read.
    known = set()
    if os.path.exists(args.quirks):
        for q in json.load(open(args.quirks))["buy_gt_sell"]:
            known.add((q["published_date"], q["currency"], q["pair"]))

    print("\ninvariants (%d rows)" % len(rows))
    viol = collections.Counter()
    tot = collections.Counter()
    seen_known = set()
    for r in rows:
        for a, b in PAIRS:
            x, y = r.get(a), r.get(b)
            if x and y:
                tot[a] += 1
                if x > y:
                    key = (r.get("published_date"), r.get("currency"),
                           a.replace("_buy", ""))
                    if key in known:
                        seen_known.add(key)
                    else:
                        viol[a] += 1
                        print("    unexpected: %s %s %s buy=%s sell=%s"
                              % (r["snapshot_id"], r["currency"], a, x, y))
    for a, _ in PAIRS:
        ok &= check("buy <= sell for %s" % a.replace("_buy", ""),
                    viol[a] == 0, "%d unexpected / %d pairs" % (viol[a], tot[a]))
    if seen_known:
        print("  (%d known SBI source error%s excluded, see %s)"
              % (len(seen_known), "" if len(seen_known) == 1 else "s", args.quirks))
    stale_quirks = known - seen_known
    if stale_quirks:
        print("  note: %d quirk entr%s no longer match any row: %s"
              % (len(stale_quirks), "y" if len(stale_quirks) == 1 else "ies",
                 sorted(stale_quirks)))

    bad_date = [r for r in rows
                if r.get("published_date") and r["published_date"] > r["snapshot_id"][:10]]
    ok &= check("no card dated after its capture", not bad_date,
                "%d bad" % len(bad_date))
    ok &= check("every row has a currency", all(r.get("currency") for r in rows))

    if os.path.exists(args.log):
        obs = [json.loads(l) for l in open(args.log)]
        counts = collections.Counter(o.get("status") for o in obs)
        archived = {o["archived_as"] for o in obs if o.get("archived_as")}
        print("\nfetch log (%d observations: %s)"
              % (len(obs), dict(counts)))
        ok &= check("every archived path exists on disk",
                    all(os.path.exists(p) for p in archived),
                    "%d missing" % sum(1 for p in archived if not os.path.exists(p)))

    if args.sample:
        try:
            import pymupdf
        except ImportError:
            print("\n  pymupdf not installed; skipping the PDF re-read")
            return 0 if ok else 1
        print("\nvalue fidelity (re-reading %d PDFs)" % args.sample)
        card_files = [os.path.join(d, n)
                      for d, _, ns in os.walk(args.cards) for n in ns
                      if n.endswith(".json")]
        random.seed(args.seed)
        picked = random.sample(card_files, min(args.sample, len(card_files)))
        checked = bad = 0
        for cf in picked:
            rec = json.load(open(cf))
            doc = pymupdf.open(os.path.join(args.archive, rec["source_pdf"]))
            pages = [p for p in doc
                     if any(w[4].endswith("/INR") for w in p.get_text("words"))]
            if len(pages) != len(rec["tables"]):
                bad += 1
                print("    page count mismatch: %s" % rec["source_pdf"])
                continue
            for page, tab in zip(pages, rec["tables"]):
                inpdf = collections.Counter(
                    float(w[4]) for w in page.get_text("words") if NUM.match(w[4]))
                injson = collections.Counter(
                    row[f] for row in tab["rates"] for f in tab["columns"] if f in row)
                checked += sum(injson.values())
                if injson - inpdf:
                    bad += 1
                    print("    value not in PDF: %s %s"
                          % (rec["source_pdf"], list((injson - inpdf).items())[:3]))
            doc.close()
        ok &= check("every JSON value appears in its PDF", bad == 0,
                    "%d values checked, %d problems" % (checked, bad))

    print("\n%s" % ("all checks passed" if ok else "FAILURES -- see above"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
