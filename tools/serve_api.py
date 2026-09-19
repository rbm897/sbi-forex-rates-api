"""Tiny read-only HTTP API over the extracted rates. Standard library only.

    python3 tools/serve_api.py --data api/v1/bulk/rates.ndjson --port 8787

    GET /health
    GET /v1/currencies
    GET /v1/latest?currency=USD[&slab=]
    GET /v1/rates?currency=USD&from=2025-01-01&to=2025-12-31&slab=10_to_20_lakh
                 [&field=tt_buy&limit=500&offset=0&format=json|csv]
    GET /v1/snapshot/<snapshot_id>
    GET /v1/stats
"""
import argparse
import io
import json
import csv
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

FIELDS = ["tt_buy", "tt_sell", "bill_buy", "bill_sell", "travel_card_buy",
          "travel_card_sell", "currency_note_buy", "currency_note_sell", "pc_buy"]

# Fixed CSV column order. A row only carries the fields its card had -- pc_buy
# vanished in late 2023, and a blank cell in the PDF omits its key -- so columns
# cannot be taken from whichever row happens to come first in a page.
CSV_COLUMNS = ["snapshot_id", "captured_at", "published_date", "published_time",
               "card_age_days", "stale", "slab", "currency", "currency_name",
               "unit"] + FIELDS

ROWS = []
BY_CURRENCY = defaultdict(list)
BY_SNAPSHOT = defaultdict(list)
CURRENCIES = []


def load(path):
    with open(path) as fh:
        for line in fh:
            r = json.loads(line)
            ROWS.append(r)
            BY_CURRENCY[r["currency"]].append(r)
            BY_SNAPSHOT[r["snapshot_id"]].append(r)
    ROWS.sort(key=lambda r: r["snapshot_id"])
    seen = {}
    for r in ROWS:
        seen.setdefault(r["currency"], r["currency_name"])
    CURRENCIES.extend({"code": c, "name": n, "observations": len(BY_CURRENCY[c])}
                      for c, n in sorted(seen.items()))


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, indent=1).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        if not getattr(self, "_head_only", False):
            self.wfile.write(body)

    def do_HEAD(self):
        self._head_only = True
        try:
            self.do_GET()
        finally:
            self._head_only = False

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Max-Age", "86400")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        path = u.path.rstrip("/") or "/"
        # The static tree spells every endpoint with .json; accept either here so
        # the same URL works against both.
        if path.endswith(".json") and not path.startswith("/v1/snapshot"):
            path = path[:-5]
        try:
            if path == "/health":
                return self._send(200, {"ok": True, "rows": len(ROWS)})
            if path == "/v1/currencies":
                return self._send(200, CURRENCIES)
            if path == "/v1/stats":
                return self._send(200, {
                    "rows": len(ROWS),
                    "snapshots": len(BY_SNAPSHOT),
                    "currencies": len(BY_CURRENCY),
                    "first": ROWS[0]["snapshot_id"] if ROWS else None,
                    "last": ROWS[-1]["snapshot_id"] if ROWS else None,
                })
            if path.startswith("/v1/snapshot/") or path.startswith("/v1/snapshots/"):
                # Accept the static tree's spelling (/v1/snapshots/{id}.json) so
                # the same URL works against either.
                sid = path.rsplit("/", 1)[-1]
                if sid.endswith(".json"):
                    sid = sid[:-5]
                rows = BY_SNAPSHOT.get(sid)
                if not rows:
                    return self._send(404, {"error": "unknown snapshot", "snapshot_id": sid})
                return self._send(200, {"snapshot_id": sid, "count": len(rows), "rates": rows})
            if path in ("/v1/rates", "/v1/latest"):
                cur = (q.get("currency") or "").upper()
                rows = BY_CURRENCY.get(cur, []) if cur else ROWS
                if cur and not rows:
                    return self._send(404, {"error": "unknown currency", "currency": cur})
                slab = q.get("slab")
                if slab:
                    rows = [r for r in rows if r["slab"] == slab]
                fr, to = q.get("from"), q.get("to")
                if fr:
                    rows = [r for r in rows if r["snapshot_id"][:10] >= fr]
                if to:
                    rows = [r for r in rows if r["snapshot_id"][:10] <= to]
                if path == "/v1/latest":
                    # rows[-1] is one row of the newest card -- whichever
                    # currency sorts last. Take the whole snapshot instead.
                    latest_id = rows[-1]["snapshot_id"] if rows else None
                    rows = [r for r in rows if r["snapshot_id"] == latest_id]
                field = q.get("field")
                if field:
                    if field not in FIELDS:
                        return self._send(400, {"error": "unknown field", "field": field,
                                                "allowed": FIELDS})
                    rows = [{"snapshot_id": r["snapshot_id"], "slab": r["slab"],
                             "currency": r["currency"], field: r.get(field)} for r in rows]
                total = len(rows)
                try:
                    off = int(q.get("offset", 0))
                    lim = int(q.get("limit", 500))
                except ValueError:
                    return self._send(400, {"error": "offset and limit must be integers",
                                            "offset": q.get("offset"),
                                            "limit": q.get("limit")})
                if off < 0 or lim < 0:
                    return self._send(400, {"error": "offset and limit must not be negative"})
                lim = min(lim, 10000)
                page = rows[off:off + lim]
                if q.get("format") == "csv":
                    buf = io.StringIO()
                    cols = [c for c in CSV_COLUMNS
                            if any(c in r for r in page)] or CSV_COLUMNS
                    if field:
                        cols = [c for c in ("snapshot_id", "slab", "currency",
                                            field) if any(c in r for r in page)]
                    w = csv.DictWriter(buf, fieldnames=cols, restval="",
                                       extrasaction="ignore")
                    w.writeheader()
                    w.writerows(page)
                    return self._send(200, buf.getvalue(), "text/csv")
                return self._send(200, {"count": total, "offset": off, "limit": lim,
                                        "rates": page})
            return self._send(404, {"error": "not found", "path": path,
                                    "endpoints": ["/health", "/v1/stats", "/v1/currencies",
                                                  "/v1/rates", "/v1/latest",
                                                  "/v1/snapshot/{id}"]})
        except Exception as exc:
            self._send(500, {"error": "%s: %s" % (type(exc).__name__, exc)})


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--port", type=int, default=8787)
    a = ap.parse_args()
    load(a.data)
    print("loaded %d rows; serving on http://127.0.0.1:%d" % (len(ROWS), a.port))
    ThreadingHTTPServer(("127.0.0.1", a.port), Handler).serve_forever()
