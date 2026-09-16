"""Derive the per-card JSON in data/cards/ from the archived PDFs.

The parsed form of each card is committed alongside the PDF so that routine CI
never has to download the archive: the API is rebuilt from these small files,
and the PDFs are only needed to re-derive them after a parser change.

One file per card, written once and never rewritten, so the repository grows by
~11 KB per card instead of by a rewritten bulk file.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from extract_all import extract_one


def card_path(cards_dir, snapshot_id):
    return os.path.join(cards_dir, snapshot_id[:4], snapshot_id + ".json")


def write_card(cards_dir, rec):
    path = card_path(cards_dir, os.path.basename(rec["source_pdf"])[:-4])
    os.makedirs(os.path.dirname(path), exist_ok=True)
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
