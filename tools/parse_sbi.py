"""Parse SBI FOREX CARD RATES PDFs into structured records.

Works off word coordinates rather than reading-order text, because the PDFs are
produced by Word and the naive text order merges adjacent cells.
"""
import datetime
import re
import unicodedata

import pymupdf

PAIR_RE = re.compile(r"^([A-Z]{3})/INR$")
NUM_RE = re.compile(r"^-?\d{1,3}(?:,\d{3})*(?:\.\d+)?$|^-?\d+(?:\.\d+)?$")
# Year may be 2- or 4-digit; the (?!\d) stops "05-10-20" matching inside "05-10-2020".
DATE_RE = re.compile(r"\b(\d{1,2})([-/])(\d{1,2})\2(\d{4}|\d{2})(?!\d)")
TIME_RE = re.compile(r"\b(\d{1,2})[:.](\d{2})\s*([AaPp])\.?[Mm]\.?")

SLAB_PATTERNS = [
    (re.compile(r"BETWEEN\s+Rs\.?\s*10\s+(?:LACS|LAKHS).*?20\s+(?:LACS|LAKHS)", re.I), "10_to_20_lakh"),
    (re.compile(r"BELOW\s+Rs\.?\s*10\s+(?:LACS|LAKHS)", re.I), "below_10_lakh"),
    (re.compile(r"(?:ABOVE|MORE THAN)\s+Rs\.?\s*20\s+(?:LACS|LAKHS)", re.I), "above_20_lakh"),
]

# Header label -> canonical field name. Matched after stripping non-alphanumerics.
FIELD_ALIASES = {
    "TTBUY": "tt_buy",
    "TTSELL": "tt_sell",
    "BILLBUY": "bill_buy",
    "BILLSELL": "bill_sell",
    "TCBUY": "travel_card_buy",
    "TCSELL": "travel_card_sell",
    "FTCBUY": "travel_card_buy",
    "FTCSELL": "travel_card_sell",
    "FOREXTRAVELCARDBUY": "travel_card_buy",
    "FOREXTRAVELCARDSELL": "travel_card_sell",
    "CNBUY": "currency_note_buy",
    "CNSELL": "currency_note_sell",
    "FOREIGNTRAVELCARDBUY": "travel_card_buy",
    "FOREIGNTRAVELCARDSELL": "travel_card_sell",
    "PCBUY": "pc_buy",
    "PCSELL": "pc_sell",
}

# Currencies quoted per 100 units of foreign currency (stated in the PDF notes).
PER_100 = {"JPY", "THB", "KRW"}


def _norm(s):
    return unicodedata.normalize("NFKC", s).replace("\xa0", " ").strip()


def _rows(words, min_overlap=0.4):
    """Cluster words into visual rows.

    Grouping is by vertical overlap rather than baseline distance: a currency
    name and its rate cells are often set in different point sizes, so their
    midpoints can sit several points apart while the glyph boxes still overlap.
    """
    items = sorted(words, key=lambda w: (w[1], w[0]))
    out, cur, top, bot = [], [], None, None
    for w in items:
        if cur:
            overlap = min(bot, w[3]) - max(top, w[1])
            height = min(bot - top, w[3] - w[1])
            same = height > 0 and overlap / height >= min_overlap
        else:
            same = True
        if same:
            cur.append(w)
            top = w[1] if top is None else min(top, w[1])
            bot = w[3] if bot is None else max(bot, w[3])
        else:
            out.append(sorted(cur, key=lambda w: w[0]))
            cur, top, bot = [w], w[1], w[3]
    if cur:
        out.append(sorted(cur, key=lambda w: w[0]))
    return out


def _to_float(tok):
    try:
        return float(tok.replace(",", ""))
    except ValueError:
        return None


def _parse_time(text):
    """Read the issue time off the rate card.

    The stamp is inconsistent across the archive: "9:30 AM", "01:30 P.M." with
    dots, and 24-hour readings that still carry a meridiem ("13:20 pm"), which
    must not be shifted by another twelve hours.
    """
    m = TIME_RE.search(text) or re.search(r"\b(\d{1,2}):(\d{2})\b", text)
    if not m:
        return None
    h, mnt = int(m.group(1)), m.group(2)
    ap = m.group(3).upper() if m.lastindex and m.lastindex >= 3 else ""
    if ap == "P" and h < 12:
        h += 12
    elif ap == "A" and h == 12:
        h = 0
    if h > 23 or int(mnt) > 59:
        return None
    return "%02d:%s" % (h, mnt)


def _field_from_label(label):
    """Map a printed column header to a canonical field name.

    Header wording drifts across the archive (TC -> FTC -> FOREX TRAVEL CARD),
    and wrapped headers can come back with their words out of order, so fall
    back to matching on the words present rather than the exact string.
    """
    key = re.sub(r"[^A-Z0-9]", "", label.upper())
    if key in FIELD_ALIASES:
        return FIELD_ALIASES[key]
    words = set(re.findall(r"[A-Z]+", label.upper()))
    if "BUY" in words:
        side = "buy"
    elif "SELL" in words:
        side = "sell"
    else:
        return None
    if "TT" in words:
        return "tt_" + side
    if "BILL" in words:
        return "bill_" + side
    if "CN" in words:
        return "currency_note_" + side
    if "PC" in words:
        return "pc_" + side
    if words & {"TC", "FTC", "CARD", "TRAVEL"}:
        return "travel_card_" + side
    return None


def parse_page(page):
    words = [(w[0], w[1], w[2], w[3], _norm(w[4])) for w in page.get_text("words")]
    words = [w for w in words if w[4]]
    if not words:
        return None
    text = _norm(page.get_text())

    slab = None
    for pat, name in SLAB_PATTERNS:
        if pat.search(text.replace("\n", " ")):
            slab = name
            break

    rows = _rows(words)

    # Data rows: contain an XXX/INR token.
    data_rows = []
    for row in rows:
        for i, w in enumerate(row):
            m = PAIR_RE.match(w[4])
            if m:
                data_rows.append((row, i, m.group(1)))
                break
    if not data_rows:
        return None

    # Column centres: take the modal cell layout across data rows.
    from collections import Counter
    cell_sets = []
    for row, idx, _ in data_rows:
        centres = [((w[0] + w[2]) / 2, _to_float(w[4])) for w in row[idx + 1:] if NUM_RE.match(w[4])]
        centres = [c for c in centres if c[1] is not None]
        cell_sets.append(centres)
    modal_n = Counter(len(c) for c in cell_sets).most_common(1)[0][0]
    if modal_n == 0:
        return None
    ref = [c for c in cell_sets if len(c) == modal_n]
    centres = [sum(r[i][0] for r in ref) / len(ref) for i in range(modal_n)]

    # Column boundaries = midpoints between adjacent centres.
    bounds = []
    for i in range(modal_n):
        lo = -1e9 if i == 0 else (centres[i - 1] + centres[i]) / 2
        hi = 1e9 if i == modal_n - 1 else (centres[i] + centres[i + 1]) / 2
        bounds.append((lo, hi))

    # Header labels: words above the first data row, right of the currency column.
    first_data_y = min(min(w[1] for w in row) for row, _, _ in data_rows)
    name_left = min(w[0] for row, idx, _ in data_rows for w in row[:idx + 1])
    pair_right = max(row[idx][2] for row, idx, _ in data_rows)
    above = [w for w in words if w[3] <= first_data_y + 1]
    # The column headers are the contiguous block of text lines sitting directly
    # on top of the data; anything further up (page title, slab caption, date) is
    # separated by a noticeably larger vertical gap.
    header_words = []
    band_top = None
    for line in reversed(_rows(above)):
        top = min(w[1] for w in line)
        bottom = max(w[3] for w in line)
        if band_top is not None and band_top - bottom > 10:
            break
        joined = " ".join(w[4] for w in line).upper()
        if "TRANSACTIONS" in joined or "CARD RATES" in joined:
            break
        header_words.extend(line)
        band_top = top
    header_words = [w for w in header_words if (w[0] + w[2]) / 2 > pair_right]
    # Header text wraps across lines ("FOREX TRAVEL" / "CARD BUY"), and the wrapped
    # halves are not centred on the column, so assign whole word-clusters (not
    # individual words) to the nearest column centre.
    chunks = []
    for line in _rows(header_words):
        group = [line[0]]
        for w in line[1:]:
            if w[0] - group[-1][2] > 6:
                chunks.append(group)
                group = [w]
            else:
                group.append(w)
        chunks.append(group)

    # Tight inter-column spacing can glue two headers into one chunk
    # ("TC SELL CN BUY"); every header ends on BUY or SELL, so split there.
    split = []
    for group in chunks:
        cut = [i for i, w in enumerate(group) if w[4].upper() in ("BUY", "SELL")]
        if len(cut) > 1:
            prev = 0
            for i in cut:
                split.append(group[prev:i + 1])
                prev = i + 1
            if group[prev:]:
                split.append(group[prev:])
        else:
            split.append(group)
    chunks = split


    parts = [[] for _ in range(modal_n)]
    for group in chunks:
        cx = (group[0][0] + group[-1][2]) / 2
        i = min(range(modal_n), key=lambda k: abs(centres[k] - cx))
        parts[i].append((min(w[1] for w in group), group[0][0],
                         " ".join(w[4] for w in group)))
    labels = [" ".join(t for _, _, t in sorted(p)) for p in parts]

    fields = [_field_from_label(lab) or ("col_%d" % (i + 1))
              for i, lab in enumerate(labels)]

    # Rows -> records.
    rates = []
    for (row, idx, code), cells in zip(data_rows, cell_sets):
        name = " ".join(w[4] for w in row[:idx]).strip()
        rec = {"currency": code, "currency_name": name or None,
               "unit": 100 if code in PER_100 else 1}
        for cx, val in cells:
            for i, (lo, hi) in enumerate(bounds):
                if lo <= cx < hi:
                    rec.setdefault(fields[i], val)
                    break
        rates.append(rec)

    dm = DATE_RE.search(text)
    date_token = dm.group(0) if dm else None
    time = _parse_time(text)

    return {"slab": slab, "date_token": date_token, "time": time,
            "columns": fields, "labels": labels, "rates": rates}


def resolve_date(token, hint=None):
    """Turn the date printed on the rate card into an ISO date.

    Most PDFs print DD-MM-YYYY, but a run of 2020 files uses slashes and mixes
    both day-first (08/06/2020) and month-first (7/3/2020) in the same weeks, so
    the token alone is ambiguous. `hint` is the date the file was downloaded:
    the rate card is either that day's or an earlier one, never a later one, so
    prefer the reading that lands closest to the hint without overshooting it.
    """
    if not token:
        return None
    m = DATE_RE.search(token)
    if not m:
        return None
    a, b, year = int(m.group(1)), int(m.group(3)), int(m.group(4))
    if len(m.group(4)) == 2:          # "05-10-20"
        year += 2000
    cands = []
    for day, month in ((a, b), (b, a)):
        try:
            cands.append(datetime.date(year, month, day))
        except ValueError:
            continue
    if not cands:
        return None
    if len(cands) > 1 and hint:
        try:
            h = datetime.date(*(int(x) for x in hint.split("-")))
        except ValueError:
            h = None
        if h:
            cands.sort(key=lambda d: (d > h, abs((h - d).days)))
    return cands[0].isoformat()


def parse_pdf(path, captured_date=None):
    """Return list of table dicts found in the PDF (one per rate table page)."""
    tables = []
    with pymupdf.open(path) as doc:
        for page in doc:
            try:
                t = parse_page(page)
            except Exception as exc:  # keep going; report per page
                t = {"error": "%s: %s" % (type(exc).__name__, exc)}
            if t and not t.get("error") and t.get("rates"):
                t["date"] = resolve_date(t.pop("date_token", None), captured_date)
                tables.append(t)
            elif t and t.get("error"):
                tables.append(t)
    return tables


if __name__ == "__main__":
    import json
    import sys
    for p in sys.argv[1:]:
        print(json.dumps({"file": p, "tables": parse_pdf(p)}, indent=2))
