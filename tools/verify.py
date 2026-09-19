"""Check the extracted data against the PDFs it came from.

Run this after any change to parse_sbi.py. Exits non-zero if a check fails, so
it works in CI or a pre-push hook.

    python tools/verify.py                 # fast checks + 100 sampled PDFs
    python tools/verify.py --sample 500     # sample harder
    python tools/verify.py --sample 0       # skip the PDF re-read
"""
import argparse
import collections
import datetime
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
    ap.add_argument("--check-urls", type=int, default=0,
                    help="download this many source_urls and check their SHA-256 "
                         "against the fetch log (needs network)")
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
    # CI checks out sparsely and has almost no PDFs on disk, so the reverse only
    # means something when the archive is actually there.
    full_archive = len(pdfs) >= len(cards)
    if full_archive:
        ok &= check("every parsed card has an archived PDF", cards <= pdfs,
                    "orphans: %d" % len(cards - pdfs))
    else:
        print("  %-46s %s" % ("archive is partial (%d of %d PDFs on disk)"
                              % (len(pdfs), len(cards)), "skipped"))

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

    # A source_url must point into this repository's archive, at the same file
    # source_pdf names. This defaulted to the upstream repo once and every link
    # in the API 404'd.
    snap_dir = os.path.join(args.api, "snapshots")
    if os.path.isdir(snap_dir):
        snaps = sorted(os.listdir(snap_dir))
        print("\nsource links (%d snapshots)" % len(snaps))
        mismatched = []
        for n in snaps:
            d = json.load(open(os.path.join(snap_dir, n)))
            url, rel = d.get("source_url", ""), d.get("source_pdf", "")
            if not url.endswith("/" + rel) or "raw.githubusercontent.com" not in url:
                mismatched.append((d["snapshot_id"], url))
        ok &= check("source_url matches source_pdf", not mismatched,
                    "%d wrong" % len(mismatched))
        if mismatched:
            for sid, u in mismatched[:3]:
                print("    %s -> %s" % (sid, u))
        index_path = os.path.join(args.api, "index.json")
        if os.path.exists(index_path):
            repo = json.load(open(index_path)).get("source_repository", "")
            owner_repo = repo.rstrip("/").split("github.com/")[-1]
            wrong_repo = [s for s in snaps[:1] + snaps[-1:]
                          if owner_repo not in json.load(
                              open(os.path.join(snap_dir, s))).get("source_url", "")]
            ok &= check("source_url points at %s" % (owner_repo or "?"),
                        not wrong_repo)

        if args.check_urls:
            import hashlib
            import random as _r
            import urllib.request
            logged = {}
            if os.path.exists(args.log):
                for line in open(args.log):
                    o = json.loads(line)
                    if o.get("archived_as") and o.get("sha256"):
                        logged[o["archived_as"]] = o["sha256"]
            _r.seed(args.seed)
            picked = _r.sample(snaps, min(args.check_urls, len(snaps)))
            bad_url = 0
            for n in picked:
                d = json.load(open(os.path.join(snap_dir, n)))
                want = logged.get(os.path.join(args.archive, d["source_pdf"]))
                try:
                    body = urllib.request.urlopen(d["source_url"], timeout=60).read()
                except Exception as exc:
                    bad_url += 1
                    print("    %s unreachable: %s" % (d["snapshot_id"], exc))
                    continue
                if want and hashlib.sha256(body).hexdigest() != want:
                    bad_url += 1
                    print("    %s hash mismatch" % d["snapshot_id"])
            ok &= check("published PDFs match the fetch log", bad_url == 0,
                        "%d checked" % len(picked))

    eff_path = os.path.join(args.api, "effective.json")
    if os.path.exists(eff_path):
        eff = json.load(open(eff_path))["dates"]
        days = sorted(eff)
        print("\neffective date map (%d dates)" % len(days))
        first = datetime.date.fromisoformat(days[0])
        last = datetime.date.fromisoformat(days[-1])
        expected = (last - first).days + 1
        ok &= check("no gaps between first and last date", len(days) == expected,
                    "%d dates span %d days" % (len(days), expected))
        ok &= check("every mapped card exists",
                    all(os.path.exists(os.path.join(snap_dir, s + ".json"))
                        for s in set(eff.values())) if os.path.isdir(snap_dir) else True)
        # A card cannot be in force before it was published.
        acausal = []
        for day, sid in eff.items():
            f = os.path.join(snap_dir, sid + ".json")
            if not os.path.exists(f):
                continue
            pub = json.load(open(f)).get("published_date")
            if pub and pub > day:
                acausal.append((day, sid, pub))
        ok &= check("no card in force before it was published", not acausal,
                    "%d bad" % len(acausal))
        for a in acausal[:3]:
            print("    %s -> %s published %s" % a)
        stale_days = (datetime.date.today() - last).days
        print("  %-46s %s" % ("map reaches %s (%d day%s behind today)"
                              % (days[-1], stale_days, "" if stale_days == 1 else "s"),
                              "note" if stale_days <= 1 else "STALE"))

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
        card_files = [c for c in card_files
                      if os.path.exists(os.path.join(
                          args.archive, json.load(open(c))["source_pdf"]))]
        if not card_files:
            print("  no archived PDFs on disk to sample (sparse checkout?)")
            print("\n%s" % ("all checks passed" if ok else "FAILURES -- see above"))
            return 0 if ok else 1
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
