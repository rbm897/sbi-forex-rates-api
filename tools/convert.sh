#!/usr/bin/env bash
# Rebuild the API locally.
#
# By default this works the way CI does: from the committed per-card JSON in
# data/cards, without touching the PDFs. Pass --from-pdfs to re-derive those
# cards from archive/ first, which is what you want after changing the parser.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
root="$here/.."

# Prefer a local virtualenv if one exists but is not activated, so the script
# does not fail on a system python that has no pymupdf.
py="python3"
for candidate in "$root/.venv/bin/python3" "$root/venv/bin/python3"; do
  [ -x "$candidate" ] && py="$candidate" && break
done

if [ "${1:-}" = "--from-pdfs" ]; then
  "$py" "$here/build_cards.py" --archive "$root/archive" --cards "$root/data/cards" --force
fi

"$py" "$here/build_api.py" \
  --cards "$root/data/cards" \
  --observations "$root/data/observations.ndjson" \
  --out "$root/api"
echo "done -> $root/api"
