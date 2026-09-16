"""Parse archived PDFs into the per-card JSON in data/cards/.

`extract_one` turns one PDF into a card record; `build_cards` walks the archive
in parallel and writes the ones that are missing.

The parsed form of each card is committed alongside the PDF so that routine CI
never has to download the archive: the API is rebuilt from these small files,
and the PDFs are only needed to re-derive them after a parser change.

One file per card, written once and never rewritten, so the repository grows by
~11 KB per card instead of by a rewritten bulk file.
"""
import argparse
import json
import os
import re
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import parse_sbi

FNAME_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:-(\d{2}):?(\d{2}))?\.pdf$")


def snapshot_id(rel):
    m = FNAME_RE.match(os.path.basename(rel))
    if not m:
        return None, None
    y, mo, d, hh, mm = m.groups()
    date = "%s-%s-%s" % (y, mo, d)
    return date, ("%s:%s" % (hh, mm) if hh else None)


def extract_one(root, rel):
    date, clock = snapshot_id(rel)
    rec = {"source_pdf": rel, "captured_date": date, "captured_time": clock}
    path = os.path.join(root, rel)
    if os.path.getsize(path) == 0:
        rec["status"] = "empty_file"
        return rec
    try:
        tables = parse_sbi.parse_pdf(path, captured_date=date)
    except Exception as exc:
        rec["status"] = "unreadable"
        rec["error"] = "%s: %s" % (type(exc).__name__, exc)
        return rec

    errs = [t["error"] for t in tables if t.get("error")]
    tables = [t for t in tables if t.get("rates")]
    if not tables:
        rec["status"] = "no_rate_table"
        if errs:
            rec["error"] = errs[0]
        return rec

    # Date/time are printed on the first page only; later pages inherit them.
    pub_date = next((t["date"] for t in tables if t.get("date")), None)
    pub_time = next((t["time"] for t in tables if t.get("time")), None)
    rec["status"] = "ok"
    rec["published_date"] = pub_date
    rec["published_time"] = pub_time
    out = []
    seen_slabs = Counter()
    for t in tables:
        slab = t["slab"]
        if slab is None:
            slab = "unknown_%d" % (len(out) + 1)
        seen_slabs[slab] += 1
        if seen_slabs[slab] > 1:
            slab = "%s__%d" % (slab, seen_slabs[slab])
        entry = {"slab": slab, "columns": t["columns"], "rates": t["rates"]}
        if any(c.startswith("col_") for c in t["columns"]):
            entry["unmapped_headers"] = [
                l for l, c in zip(t["labels"], t["columns"]) if c.startswith("col_")]
        out.append(entry)
    rec["tables"] = out
    return rec

def card_path(cards_dir, snapshot_id):
    return os.path.join(cards_dir, snapshot_id[:4], snapshot_id + ".json")


def write_card(cards_dir, rec):
    path = card_path(cards_dir, os.path.basename(rec["source_pdf"])[:-4])
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w") as fh:
        json.dump(rec, fh, separators=(",", ":"))
        fh.write("\n")
    return path


def load_cards(cards_dir):
    """Read every committed card back, newest last."""
    out = []
    for dirpath, _, names in os.walk(cards_dir):
        for n in sorted(names):
            if n.endswith(".json"):
                with open(os.path.join(dirpath, n)) as fh:
                    out.append(json.load(fh))
    out.sort(key=lambda r: os.path.basename(r["source_pdf"]))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--archive", default="archive")
    ap.add_argument("--cards", default="data/cards")
    ap.add_argument("--force", action="store_true",
                    help="re-derive cards that already exist (after a parser change)")
    ap.add_argument("--jobs", type=int, default=os.cpu_count())
    args = ap.parse_args()

    rels = []
    for dirpath, _, names in os.walk(args.archive):
        for n in sorted(names):
            if n.lower().endswith(".pdf"):
                rels.append(os.path.relpath(os.path.join(dirpath, n), args.archive))
    rels.sort()

    todo = rels if args.force else [
        r for r in rels
        if not os.path.exists(card_path(args.cards, os.path.basename(r)[:-4]))]
    print("%d PDFs in archive, %d to parse" % (len(rels), len(todo)), file=sys.stderr)
    if not todo:
        return

    from concurrent.futures import ProcessPoolExecutor
    written = failed = 0
    with ProcessPoolExecutor(args.jobs) as ex:
        for rec in ex.map(extract_one, [args.archive] * len(todo), todo, chunksize=16):
            if rec["status"] == "ok":
                write_card(args.cards, rec)
                written += 1
            else:
                failed += 1
                print("  skipped %s: %s" % (rec["source_pdf"], rec["status"]),
                      file=sys.stderr)
    print("wrote %d cards, %d skipped" % (written, failed), file=sys.stderr)


if __name__ == "__main__":
    main()
