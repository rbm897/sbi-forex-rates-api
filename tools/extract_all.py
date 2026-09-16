"""Walk the sbi-tt-rates-historical PDF archive and emit JSON."""
import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import parse_sbi

FNAME_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:-(\d{2}):?(\d{2}))?\.pdf$")
NOT_A_RATE_PDF = re.compile(
    r"requested URL was rejected|Site Under Maintenance|enable JavaScript|"
    r"Toggle navigation|Page Not Found|Access Denied", re.I)


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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--jobs", type=int, default=os.cpu_count())
    args = ap.parse_args()

    rels = []
    for dirpath, _, names in os.walk(args.root):
        if ".git" in dirpath.split(os.sep):
            continue
        for n in sorted(names):
            if n.lower().endswith(".pdf"):
                rels.append(os.path.relpath(os.path.join(dirpath, n), args.root))
    rels.sort()
    if args.limit:
        rels = rels[:args.limit]

    from concurrent.futures import ProcessPoolExecutor
    results = []
    with ProcessPoolExecutor(args.jobs) as ex:
        for i, rec in enumerate(ex.map(extract_one, [args.root] * len(rels), rels, chunksize=16)):
            results.append(rec)
            if (i + 1) % 250 == 0:
                print("  %d/%d" % (i + 1, len(rels)), file=sys.stderr)

    results.sort(key=lambda r: (r["source_pdf"]))
    with open(args.out, "w") as fh:
        for r in results:
            fh.write(json.dumps(r, separators=(",", ":")) + "\n")

    stat = Counter(r["status"] for r in results)
    print("\nstatus:", dict(stat), file=sys.stderr)
    unmapped = Counter()
    slabs = Counter()
    cols = Counter()
    for r in results:
        for t in r.get("tables", []):
            slabs[t["slab"]] += 1
            cols[tuple(t["columns"])] += 1
            for u in t.get("unmapped_headers", []):
                unmapped[u] += 1
    print("slabs:", dict(slabs), file=sys.stderr)
    print("unmapped headers:", unmapped.most_common(15), file=sys.stderr)
    print("column layouts:", file=sys.stderr)
    for c, n in cols.most_common(10):
        print("   %5d  %s" % (n, list(c)), file=sys.stderr)


if __name__ == "__main__":
    main()
