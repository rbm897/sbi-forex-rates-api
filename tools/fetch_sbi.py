"""Fetch the current SBI forex card rate PDF, validate it, and archive it.

Two things this does that a bare `curl` in a cron job does not:

1. It validates that the bytes are actually a rate card before keeping them.
   SBI's WAF answers with an HTML error page rendered as a PDF ("The requested
   URL was rejected", "Site Under Maintenance", a bot check). 315 of the 4,054
   files in the reference archive are exactly that, saved as if they were data.
2. It stores one copy per distinct card. SBI republishes the same card to every
   visitor until the next one is issued, so fetching several times a day
   otherwise archives the same document over and over.

Every fetch is logged either way, so "what the site served at this moment"
survives even when the document itself is a duplicate.
"""
import argparse
import datetime
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import parse_sbi
from build_cards import write_card
from extract_all import extract_one

URLS = [
    "https://sbi.bank.in/documents/16012/1400784/FOREX_CARD_RATES.pdf",
    "https://www.sbi.co.in/documents/16012/1400784/FOREX_CARD_RATES.pdf",
]

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Upgrade-Insecure-Requests": "1",
}

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
MIN_ROWS = 20


def download(url, timeout=45):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read(), resp.headers.get("Content-Type", "")


def fetch(urls, attempts=4):
    """Try each URL with backoff. Returns (body, url) or raises."""
    last = None
    for attempt in range(attempts):
        for url in urls:
            try:
                body, ctype = download(url)
                if body[:5] != b"%PDF-":
                    last = "not a PDF (content-type %s, %d bytes)" % (ctype, len(body))
                    continue
                return body, url
            except (urllib.error.URLError, OSError, TimeoutError) as exc:
                last = "%s: %s" % (type(exc).__name__, exc)
        if attempt < attempts - 1:
            time.sleep(5 * (2 ** attempt))
    raise RuntimeError("could not fetch a PDF: %s" % last)


def validate(path, captured_date):
    """Confirm the PDF really is a rate card; return its parsed tables.

    The test is the rate grid itself, not the date stamp: a handful of genuine
    cards print no date at all, and a card with 30 parsed currency rows is not
    something a WAF error page can produce by accident.
    """
    tables = parse_sbi.parse_pdf(path, captured_date=captured_date)
    tables = [t for t in tables if t.get("rates")]
    if not tables:
        raise ValueError("no rate table found (likely a WAF or maintenance page)")
    best = max(len(t["rates"]) for t in tables)
    if best < MIN_ROWS:
        raise ValueError("only %d rate rows found, expected >= %d" % (best, MIN_ROWS))
    return tables


def card_fingerprint(tables):
    """Stable hash of what the card actually says, ignoring PDF metadata."""
    payload = []
    for t in sorted(tables, key=lambda t: t.get("slab") or ""):
        payload.append([t.get("slab"), t.get("date"), t.get("time"), t["columns"],
                        [[r.get(k) for k in ["currency"] + t["columns"]]
                         for r in t["rates"]]])
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()


def append_log(path, entry):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a") as fh:
        fh.write(json.dumps(entry, separators=(",", ":")) + "\n")


def known_fingerprints(log_path):
    seen = {}
    if not os.path.exists(log_path):
        return seen
    with open(log_path) as fh:
        for line in fh:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("fingerprint") and e.get("archived_as"):
                seen[e["fingerprint"]] = e["archived_as"]
    return seen


def write_outputs(path, **kw):
    """Hand the result to the workflow so later steps can branch on it."""
    if not path:
        return
    with open(path, "a") as fh:
        for k, v in kw.items():
            fh.write("%s=%s\n" % (k, str(v).lower() if isinstance(v, bool) else v))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--archive", default="archive", help="where PDFs are kept")
    ap.add_argument("--log", default="data/observations.ndjson")
    ap.add_argument("--cards", default="data/cards",
                    help="where the parsed form of each new card is written")
    ap.add_argument("--url", action="append", help="override source URL(s)")
    ap.add_argument("--from-file", help="use a local PDF instead of downloading")
    ap.add_argument("--allow-duplicate", action="store_true",
                    help="archive even if this exact card is already stored")
    ap.add_argument("--github-output", default=os.environ.get("GITHUB_OUTPUT"))
    args = ap.parse_args()

    now = datetime.datetime.now(IST)
    captured_date = now.strftime("%Y-%m-%d")
    stamp = now.strftime("%Y-%m-%d-%H%M")
    entry = {"captured_at": now.isoformat(timespec="seconds"), "snapshot_id": stamp}

    tmp = os.path.join(args.archive, ".incoming-%s.pdf" % stamp)
    os.makedirs(args.archive, exist_ok=True)
    try:
        if args.from_file:
            body = open(args.from_file, "rb").read()
            entry["source_url"] = "file://" + os.path.abspath(args.from_file)
        else:
            body, url = fetch(args.url or URLS)
            entry["source_url"] = url
        entry["bytes"] = len(body)
        entry["sha256"] = hashlib.sha256(body).hexdigest()
        with open(tmp, "wb") as fh:
            fh.write(body)
        tables = validate(tmp, captured_date)
    except Exception as exc:
        if os.path.exists(tmp):
            os.remove(tmp)
        entry["status"] = "failed"
        entry["error"] = "%s: %s" % (type(exc).__name__, exc)
        append_log(args.log, entry)
        write_outputs(args.github_output, status="failed", new_card=False,
                      published_date="")
        print("FAILED: %s" % entry["error"], file=sys.stderr)
        return 1

    fp = card_fingerprint(tables)
    entry["fingerprint"] = fp
    entry["published_date"] = next((t["date"] for t in tables if t.get("date")), None)
    entry["published_time"] = next((t["time"] for t in tables if t.get("time")), None)
    entry["slabs"] = [t.get("slab") for t in tables]

    seen = known_fingerprints(args.log)
    new_card = fp not in seen
    if new_card or args.allow_duplicate:
        dest = os.path.join(args.archive, now.strftime("%Y"), now.strftime("%m"),
                            "%s.pdf" % stamp)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        os.replace(tmp, dest)
        entry["status"] = "new_card" if new_card else "duplicate_archived"
        entry["archived_as"] = os.path.relpath(dest)
        # Write the parsed card next to the log so the API can be rebuilt
        # without re-reading the archive.
        rec = extract_one(args.archive, os.path.relpath(dest, args.archive))
        if rec["status"] == "ok":
            entry["card_json"] = os.path.relpath(write_card(args.cards, rec))
        else:
            entry["card_json_error"] = rec["status"]
    else:
        os.remove(tmp)
        entry["status"] = "duplicate"
        entry["same_as"] = seen[fp]

    append_log(args.log, entry)
    print("%s  card=%s %s  %s" % (entry["status"], entry["published_date"],
                                  entry["published_time"] or "", fp[:12]))
    write_outputs(args.github_output, status=entry["status"], new_card=new_card,
                  published_date=entry["published_date"] or "")
    return 0


if __name__ == "__main__":
    sys.exit(main())
