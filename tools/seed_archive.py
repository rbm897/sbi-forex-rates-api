"""Import an existing PDF tree into the deduped archive + observation log.

Used once, to carry the six years already collected in
skbly7/sbi-tt-rates-historical into a self-hosted archive without losing either
the documents or the record of when each one was served.
"""
import argparse
import datetime
import hashlib
import json
import os
import re
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import parse_sbi
from fetch_sbi import card_fingerprint, validate

FNAME_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:-(\d{2}):?(\d{2}))?\.pdf$")


def scan(root):
    out = []
    for dirpath, _, names in os.walk(root):
        if ".git" in dirpath.split(os.sep):
            continue
        for n in names:
            if n.lower().endswith(".pdf"):
                out.append(os.path.relpath(os.path.join(dirpath, n), root))
    return sorted(out)


def one(root, rel):
    m = FNAME_RE.match(os.path.basename(rel))
    if not m:
        return None
    y, mo, d, hh, mm = m.groups()
    date = "%s-%s-%s" % (y, mo, d)
    stamp = "%s-%s%s" % (date, hh or "00", mm or "00")
    path = os.path.join(root, rel)
    entry = {"captured_at": "%sT%s:%s:00+05:30" % (date, hh or "00", mm or "00"),
             "snapshot_id": stamp, "source_url": None, "imported_from": rel}
    try:
        body = open(path, "rb").read()
        entry["bytes"] = len(body)
        entry["sha256"] = hashlib.sha256(body).hexdigest()
        if not body:
            raise ValueError("empty file")
        tables = validate(path, date)
    except Exception as exc:
        entry["status"] = "failed"
        entry["error"] = "%s: %s" % (type(exc).__name__, exc)
        return entry, None
    entry["fingerprint"] = card_fingerprint(tables)
    entry["published_date"] = next((t["date"] for t in tables if t.get("date")), None)
    entry["published_time"] = next((t["time"] for t in tables if t.get("time")), None)
    entry["slabs"] = [t.get("slab") for t in tables]
    return entry, path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True, help="existing YYYY/MM/*.pdf tree")
    ap.add_argument("--archive", default="archive")
    ap.add_argument("--log", default="data/observations.ndjson")
    ap.add_argument("--jobs", type=int, default=os.cpu_count())
    args = ap.parse_args()

    rels = scan(args.source)
    print("scanning %d PDFs" % len(rels), file=sys.stderr)
    from concurrent.futures import ProcessPoolExecutor
    results = []
    with ProcessPoolExecutor(args.jobs) as ex:
        for r in ex.map(one, [args.source] * len(rels), rels, chunksize=16):
            if r:
                results.append(r)
    results.sort(key=lambda r: r[0]["snapshot_id"])

    os.makedirs(os.path.dirname(args.log) or ".", exist_ok=True)
    seen = {}
    kept = dups = failed = 0
    with open(args.log, "w") as log:
        for entry, path in results:
            if entry.get("status") == "failed":
                failed += 1
            fp = entry.get("fingerprint")
            if fp:
                if fp in seen:
                    entry["status"] = "duplicate"
                    entry["same_as"] = seen[fp]
                    dups += 1
                else:
                    sid = entry["snapshot_id"]
                    dest = os.path.join(args.archive, sid[:4], sid[5:7], sid + ".pdf")
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    shutil.copyfile(path, dest)
                    seen[fp] = os.path.relpath(dest)
                    entry["status"] = "new_card"
                    entry["archived_as"] = seen[fp]
                    kept += 1
            log.write(json.dumps(entry, separators=(",", ":")) + "\n")

    print("archived %d distinct cards, %d duplicates skipped, %d unusable"
          % (kept, dups, failed), file=sys.stderr)


if __name__ == "__main__":
    main()
