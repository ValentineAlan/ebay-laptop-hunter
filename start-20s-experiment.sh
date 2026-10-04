#!/bin/bash
set -euo pipefail

cd /mnt/Decathlon/appdata/ebay-laptop-hunter-git

echo "=== LaptopLander 20-second Product Research experiment ==="
echo

if ! git diff --quiet -- app.py; then
    echo "ERROR: app.py already contains uncommitted changes."
    echo "Nothing has been changed."
    exit 1
fi

OLD_VERSION=$(python3 - <<'PY'
import re
from pathlib import Path
s = Path("app.py").read_text()
m = re.search(r'^APP_VERSION\s*=\s*"([^"]+)"', s, re.M)
if not m:
    raise SystemExit("APP_VERSION not found")
print(m.group(1))
PY
)

echo "Current version: $OLD_VERSION"

python3 - <<'PY'
import re
from pathlib import Path

p = Path("app.py")
s = p.read_text()

replacements = {
    "PRODUCT_RESEARCH_INITIAL_INTERVAL_SECONDS = 15.0":
        "PRODUCT_RESEARCH_INITIAL_INTERVAL_SECONDS = 20.0",
    "PRODUCT_RESEARCH_MIN_INTERVAL_SECONDS = 15.0":
        "PRODUCT_RESEARCH_MIN_INTERVAL_SECONDS = 20.0",
}

for old, new in replacements.items():
    if s.count(old) != 1:
        raise SystemExit(f"Expected exactly one occurrence of {old!r}; refusing to patch")
    s = s.replace(old, new, 1)

# When a persisted adaptive-rate value is below the configured minimum,
# _product_research_load_rate_state() already clamps it in memory. Persist
# that effective value immediately as well so durable state matches the
# rate the worker is actually using after a configuration change.
needle = '''        _product_research_rate.update({
            "interval_seconds": interval,
            "success_streak": success_streak,
            "backoff_until": backoff_until,
            "last_429_at": saved.get(
                "last_429_at"
            ),
            "last_success_at": saved.get(
                "last_success_at"
            ),
        })

        _product_research_rate_loaded = True
'''

replacement = '''        _product_research_rate.update({
            "interval_seconds": interval,
            "success_streak": success_streak,
            "backoff_until": backoff_until,
            "last_429_at": saved.get(
                "last_429_at"
            ),
            "last_success_at": saved.get(
                "last_success_at"
            ),
        })

        # Persist the effective state after applying configured bounds.
        # This prevents durable rate state from continuing to report an
        # obsolete interval (for example 5s) while the worker is actually
        # constrained to a newer fixed minimum such as 20s.
        saved_interval = saved.get("interval_seconds")
        try:
            saved_interval = float(saved_interval)
        except Exception:
            saved_interval = None

        if saved_interval != interval:
            _product_research_save_rate_state()

        _product_research_rate_loaded = True
'''

if s.count(needle) != 1:
    raise SystemExit("Could not find the Product Research rate-load block; refusing to patch")
s = s.replace(needle, replacement, 1)

m = re.search(r'^(APP_VERSION\s*=\s*")(\d+)\.(\d+)\.(\d+)(")', s, re.M)
if not m:
    raise SystemExit("Could not parse APP_VERSION")

major, minor, patch = map(int, m.group(2, 3, 4))
new_version = f"{major}.{minor}.{patch + 1}"
s = s[:m.start()] + m.group(1) + new_version + m.group(5) + s[m.end():]

p.write_text(s)
print("Changed Product Research interval: 15.0s -> 20.0s")
print("Added persistence of the effective/clamped Product Research rate state")
print(f"New LaptopLander version: {new_version}")
PY

echo
echo "=== Syntax check ==="
python3 -m py_compile app.py

echo
echo "=== Patch ==="
git diff --check
git diff -- app.py

echo
echo "=== Commit ==="
git add app.py
git commit -m "Run Product Research at 20 seconds and sync rate state"

echo
echo "=== Push ==="
git push origin main

echo
echo "=== Hunter-only deployment ==="

PROJECT="ix-ebay-hunter-stack"
COMPOSE="/mnt/.ix-apps/app_configs/ebay-hunter-stack/versions/1.0.0/templates/rendered/docker-compose.yaml"
CONTAINER="ix-ebay-hunter-stack-hunter-1"

echo "Chromium and SID helper will NOT be recreated."

docker compose \
    -p "$PROJECT" \
    -f "$COMPOSE" \
    build --no-cache hunter

docker compose \
    -p "$PROJECT" \
    -f "$COMPOSE" \
    up -d --no-build --no-deps --force-recreate hunter

echo
echo "=== Running version ==="
docker exec "$CONTAINER" grep -m1 'APP_VERSION =' /app/app.py

echo
echo "=== Product Research configuration ==="
docker exec "$CONTAINER" grep -E \
    'PRODUCT_RESEARCH_(INITIAL|MIN)_INTERVAL_SECONDS =' \
    /app/app.py

echo
echo "=== Remove obsolete legacy JSON state file ==="
docker exec "$CONTAINER" rm -f /data/product-research-rate.json

echo
echo "=== Durable Product Research rate state (SQLite) ==="
docker exec "$CONTAINER" python3 - <<'PY'
import json
import sqlite3

db = "/data/laptop_hunter.db"
conn = sqlite3.connect(db)
row = conn.execute(
    "SELECT value_json, updated_at FROM runtime_state WHERE key=?",
    ("product_research_rate",),
).fetchone()
conn.close()

if not row:
    raise SystemExit("ERROR: product_research_rate is missing from runtime_state")

state = json.loads(row[0])
print(json.dumps(state, indent=2, sort_keys=True))
print("runtime_state.updated_at:", row[1])

interval = float(state.get("interval_seconds", -1))
if interval < 20.0:
    raise SystemExit(
        f"ERROR: durable interval is still {interval}s; expected at least 20.0s"
    )
PY

echo
echo "=== Legacy state check ==="
if docker exec "$CONTAINER" test -e /data/product-research-rate.json; then
    echo "ERROR: obsolete /data/product-research-rate.json still exists"
    exit 1
else
    echo "OK: obsolete /data/product-research-rate.json is absent"
fi

echo
echo "=== Containers ==="
docker ps \
    --filter name=ix-ebay-hunter-stack \
    --format 'table {{.Names}}\t{{.Status}}\t{{.Image}}'

echo
echo "=== Recent hunter logs ==="
docker logs --tail 40 "$CONTAINER"

echo
echo "20-second experiment deployed."
echo "Chromium and SID helper were not recreated."
