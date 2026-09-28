# Laptop Lander
#
# Features:
#   - eBay GB laptop discovery
#   - Persistent API budgeting
#   - Adaptive polling
#   - CPU identification
#   - Evidence-based Windows 11 approved-hardware assessment
#   - Persistent exact-CPU Windows 11 capability cache and verification queue
#   - Evidence-based USB-C charging capability assessment
#   - Persistent USB-C model capability cache and verification queue
#   - Fault classification
#   - Brand/model/RAM/storage extraction
#   - Persistent price observations
#   - eBay Product Research sold-data collection/cache
#   - Automatic TrueNAS Chromium ebaysid refresh + session dashboard status
#   - Sold-first comparable valuation with active-market fallback
#   - Persistent /data/hunter.log
#   - £ and % apparent undervaluation
#   - Confidence scoring
#   - Deal score
#   - Local web dashboard on port 8080
#
# Dashboard:
#   http://<TRUENAS-IP>:8080
#
# No third-party Python packages required.

import os
import re
import json
import time
import math
import html
import base64
import sqlite3
import statistics
import threading
import urllib.parse
import urllib.request
import urllib.error
import shlex
import sys
import hashlib
import hmac
import secrets
from http.cookies import SimpleCookie

from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from html.parser import HTMLParser


# ============================================================
# CLASSIFIER_VERSION / CONFIG
# ============================================================

APP_VERSION = "0.9.39"
CLASSIFIER_VERSION = "0.8.3"
MIN_UNDERVALUE_GBP = 20.0
MIN_UNDERVALUE_PCT = 10.0
IMAGE_BACKFILL_PER_CYCLE = 100
IMAGE_BACKFILL_DAILY_ALLOWANCE = 100

# Backward-compatible internal alias.
# Existing classifier_version DB logic continues to use CLASSIFIER_VERSION.
# eBay short-term rate-limit protection.
EBAY_429_COOLDOWN_SECONDS = 600


class EbayRateLimited(Exception):
    """Raised when an eBay API request is rate limited."""
    pass



def ebay_rate_limit_remaining():
    return max(
        0,
        int(_ebay_rate_limited_until - time.time())
    )

_ebay_rate_limited_until = 0.0


def ebay_detail_rate_limited():
    return time.time() < _ebay_rate_limited_until


def mark_ebay_rate_limited(seconds=EBAY_429_COOLDOWN_SECONDS):
    global _ebay_rate_limited_until

    _ebay_rate_limited_until = max(
        _ebay_rate_limited_until,
        time.time() + seconds,
    )

    print(
        f"eBay rate limit: pausing detail calls for "
        f"{int(seconds)} seconds"
    )


DB = "/data/hunter.db"
LOG_FILE = "/data/hunter.log"
PRODUCT_RESEARCH_CURL = "/data/product-research.curl"
PRODUCT_RESEARCH_LIVE_SID = "/data/ebaysid.current"
PRODUCT_RESEARCH_REFRESH_REQUEST = "/data/ebaysid.refresh-request"
PRODUCT_RESEARCH_SESSION_STATE = "/data/product-research-session.json"
PRODUCT_RESEARCH_HELPER_STATE = "/data/ebaysid-helper-status.json"
PRODUCT_RESEARCH_REFRESH_WAIT_SECONDS = 25
PRODUCT_RESEARCH_REFRESH_POLL_SECONDS = 0.5
PRODUCT_RESEARCH_SESSION_PROBE_SECONDS = 300
PRODUCT_RESEARCH_SESSION_PROBE_QUERY = "Dell Latitude 5340"
PRODUCT_RESEARCH_CACHE_HOURS = 24
PRODUCT_RESEARCH_DAY_RANGE = 90
PRODUCT_RESEARCH_LIMIT = 50
PRODUCT_RESEARCH_MAX_PAGES = 2
PRODUCT_RESEARCH_SEARCHES_PER_CYCLE = 12
ACTIVE_BIN_RECHECKS_PER_CYCLE = 40
ACTIVE_BIN_RECHECKS_DURING_REANALYSIS = 5
ACTIVE_BIN_RECHECK_MIN_AGE_MINUTES = 10
ACTIVE_BIN_RECHECK_INTERVAL_MINUTES = 15

CATEGORY = "177"
MARKETPLACE = "EBAY_GB"

# Timestamp-window discovery.
SEARCH_LIMIT = 200
SEARCH_INTERVAL_SECONDS = 15 * 60
SEARCH_WINDOW_OVERLAP_SECONDS = 2 * 60
SEARCH_INITIAL_LOOKBACK_SECONDS = 17 * 60
SEARCH_MAX_CATCHUP_WINDOW_SECONDS = 60 * 60
SEARCH_CHECKPOINT_FILE = "/data/ebay-search-checkpoint.json"

DASHBOARD_HOST = "0.0.0.0"
DASHBOARD_PORT = int(
    os.environ.get(
        "DASHBOARD_PORT",
        "8080"
    )
)

# eBay Browse budget.
#
# Keep our own safety ceiling below the account allowance.
EBAY_DAILY_LIMIT = 5000
DAILY_SAFETY_LIMIT = 4950

SEARCH_RESERVE = 200
MIN_SEARCH_RESERVE = 50
SEARCH_RESERVE_TAPER_START_HOUR = 12
EMERGENCY_RESERVE = 75
MAX_DETAIL_CALLS_PER_CYCLE = 40
MAX_BACKFILL_DETAILS_PER_CYCLE = 40
POLL_NORMAL = 900
POLL_60_PERCENT = 900
POLL_75_PERCENT = 900
POLL_85_PERCENT = 900
POLL_90_PERCENT = 900
# Valuation
MIN_COMPARABLES = 2
MEDIUM_CONFIDENCE_COMPARABLES = 6

# Don't use ancient active observations indefinitely.
COMPARABLE_MAX_AGE_DAYS = 30
SOLD_CACHE_MAX_AGE_DAYS = 7
SOLD_EVIDENCE_VERSION = "2"


# PassMark CPU benchmark cache.
CPU_BENCHMARK_URL = "https://www.cpubenchmark.net/cpu-list/all"
CPU_BENCHMARK_REFRESH_HOURS = 24
CPU_BENCHMARK_USER_AGENT = (
    "Mozilla/5.0 (compatible; LaptopLander/1.0; "
    "+https://github.com/ValentineAlan/ebay-laptop-hunter)"
)

# ============================================================
# RUNTIME SETTINGS / ADMIN UI
# ============================================================

SETTINGS_PASSWORD_ENV = "SETTINGS_PASSWORD"
SETTINGS_SESSION_SECONDS = 8 * 60 * 60
SETTINGS_PBKDF2_ITERATIONS = 310000

# Only application behaviour belongs here. TrueNAS remains responsible for
# ports, bind addresses, volumes, networking, secrets and container wiring.
SETTINGS_SCHEMA = [
    {"key": "DAILY_SAFETY_LIMIT", "label": "Daily Browse API safety limit", "group": "API & polling", "type": "int", "min": 1000, "max": 5000, "step": 1, "apply": "Applies live"},
    {"key": "SEARCH_RESERVE", "label": "Search reserve", "group": "API & polling", "type": "int", "min": 0, "max": 2000, "step": 1, "apply": "Applies live"},
    {"key": "MIN_SEARCH_RESERVE", "label": "Minimum search reserve", "group": "API & polling", "type": "int", "min": 0, "max": 1000, "step": 1, "apply": "Applies live"},
    {"key": "SEARCH_RESERVE_TAPER_START_HOUR", "label": "Reserve taper start (hours into eBay window)", "group": "API & polling", "type": "int", "min": 0, "max": 23, "step": 1, "apply": "Applies live"},
    {"key": "EMERGENCY_RESERVE", "label": "Emergency reserve", "group": "API & polling", "type": "int", "min": 0, "max": 1000, "step": 1, "apply": "Applies live"},
    {"key": "MAX_DETAIL_CALLS_PER_CYCLE", "label": "Maximum detail calls per cycle", "group": "API & polling", "type": "int", "min": 1, "max": 500, "step": 1, "apply": "Next polling cycle"},
    {"key": "MAX_BACKFILL_DETAILS_PER_CYCLE", "label": "Maximum reanalysis details per cycle", "group": "API & polling", "type": "int", "min": 1, "max": 500, "step": 1, "apply": "Next polling cycle"},
    {"key": "POLL_NORMAL", "label": "Polling interval (seconds)", "group": "API & polling", "type": "int", "min": 60, "max": 86400, "step": 1, "apply": "Next polling cycle"},
    {"key": "SEARCH_WINDOW_OVERLAP_SECONDS", "label": "Timestamp overlap (seconds)", "group": "API & polling", "type": "int", "min": 0, "max": 3600, "step": 1, "apply": "Next polling cycle"},
    {"key": "SEARCH_INITIAL_LOOKBACK_SECONDS", "label": "Initial lookback (seconds)", "group": "API & polling", "type": "int", "min": 60, "max": 86400, "step": 1, "apply": "Next polling cycle"},
    {"key": "SEARCH_MAX_CATCHUP_WINDOW_SECONDS", "label": "Maximum catch-up window (seconds)", "group": "API & polling", "type": "int", "min": 60, "max": 86400, "step": 1, "apply": "Next polling cycle"},
    {"key": "SEARCH_LIMIT", "label": "Browse page size", "group": "API & polling", "type": "int", "min": 1, "max": 200, "step": 1, "apply": "Next polling cycle"},

    {"key": "MIN_UNDERVALUE_GBP", "label": "Minimum saving (£)", "group": "Deals", "type": "float", "min": 0, "max": 5000, "step": 0.01, "apply": "Applies live"},
    {"key": "MIN_UNDERVALUE_PCT", "label": "Minimum saving (%)", "group": "Deals", "type": "float", "min": 0, "max": 100, "step": 0.1, "apply": "Applies live"},
    {"key": "MIN_COMPARABLES", "label": "Minimum sold comparables", "group": "Valuation", "type": "int", "min": 1, "max": 100, "step": 1, "apply": "Applies live"},
    {"key": "MEDIUM_CONFIDENCE_COMPARABLES", "label": "Medium confidence comparables", "group": "Valuation", "type": "int", "min": 1, "max": 100, "step": 1, "apply": "Applies live"},
    {"key": "COMPARABLE_MAX_AGE_DAYS", "label": "Maximum active-comparable age (days)", "group": "Valuation", "type": "int", "min": 1, "max": 3650, "step": 1, "apply": "Applies live"},
    {"key": "SOLD_CACHE_MAX_AGE_DAYS", "label": "Sold cache maximum age (days)", "group": "Valuation", "type": "int", "min": 1, "max": 365, "step": 1, "apply": "Applies live"},
    {"key": "PRODUCT_RESEARCH_CACHE_HOURS", "label": "Product Research cache (hours)", "group": "Valuation", "type": "int", "min": 1, "max": 720, "step": 1, "apply": "Applies live"},
    {"key": "PRODUCT_RESEARCH_DAY_RANGE", "label": "Product Research sold range (days)", "group": "Valuation", "type": "int", "min": 1, "max": 365, "step": 1, "apply": "Applies live"},
    {"key": "PRODUCT_RESEARCH_LIMIT", "label": "Product Research results per page", "group": "Valuation", "type": "int", "min": 1, "max": 200, "step": 1, "apply": "Applies live"},
    {"key": "PRODUCT_RESEARCH_MAX_PAGES", "label": "Product Research maximum pages", "group": "Valuation", "type": "int", "min": 1, "max": 20, "step": 1, "apply": "Applies live"},
    {"key": "PRODUCT_RESEARCH_SEARCHES_PER_CYCLE", "label": "Product Research searches per cycle", "group": "Valuation", "type": "int", "min": 1, "max": 100, "step": 1, "apply": "Next polling cycle"},

    {"key": "ACTIVE_BIN_RECHECKS_PER_CYCLE", "label": "Active BIN rechecks per cycle", "group": "Maintenance", "type": "int", "min": 0, "max": 500, "step": 1, "apply": "Next polling cycle"},
    {"key": "ACTIVE_BIN_RECHECKS_DURING_REANALYSIS", "label": "BIN rechecks during reanalysis", "group": "Maintenance", "type": "int", "min": 0, "max": 500, "step": 1, "apply": "Next polling cycle"},
    {"key": "ACTIVE_BIN_RECHECK_MIN_AGE_MINUTES", "label": "Minimum age before BIN recheck (minutes)", "group": "Maintenance", "type": "int", "min": 1, "max": 10080, "step": 1, "apply": "Next polling cycle"},
    {"key": "ACTIVE_BIN_RECHECK_INTERVAL_MINUTES", "label": "BIN recheck interval (minutes)", "group": "Maintenance", "type": "int", "min": 1, "max": 10080, "step": 1, "apply": "Next polling cycle"},
    {"key": "IMAGE_BACKFILL_PER_CYCLE", "label": "Image backfill per cycle", "group": "Maintenance", "type": "int", "min": 0, "max": 1000, "step": 1, "apply": "Next polling cycle"},
    {"key": "IMAGE_BACKFILL_DAILY_ALLOWANCE", "label": "Image backfill daily allowance", "group": "Maintenance", "type": "int", "min": 0, "max": 5000, "step": 1, "apply": "Applies live"},
]

SETTINGS_SCHEMA_BY_KEY = {
    item["key"]: item
    for item in SETTINGS_SCHEMA
}

DEFAULT_RULE_GROUPS = {'fault_high': ['liquid damage',
                'water damage',
                'motherboard',
                'mainboard',
                'no power',
                "doesn't power",
                'does not power',
                "doesn't post",
                'does not post',
                'bios locked',
                'bios password',
                'faulty screen',
                'screen faulty',
                'faulty lcd',
                'lcd faulty',
                'screen damage',
                'damaged screen',
                'cracked screen',
                'broken screen',
                'broken display',
                'no display',
                'nf screen',
                'unknown fault',
                'untested',
                'spare parts',
                'spares',
                'faulty',
                'not working',
                'mdm locked',
                'activation locked',
                'autopilot locked',
                'for parts',
                'parts or not working',
                'parts only',
                'spares or repair',
                'spares or repairs'],
 'fault_moderate': ['keyboard faulty',
                    'faulty keyboard',
                    'kb faulty',
                    'trackpad faulty',
                    'faulty trackpad',
                    'faulty tp',
                    'missing key',
                    'missing keys',
                    'keycap',
                    'hinge',
                    'case damage',
                    'case dmg',
                    'damaged case',
                    'cracked case',
                    'cracks',
                    'dented',
                    'dents',
                    'dent',
                    'scratch',
                    'scratches',
                    'scratched',
                    'grade b',
                    'grade c'],
 'fault_low': ['no hdd',
               'no ssd',
               'no storage',
               'missing ssd',
               'no battery',
               'no batt',
               'missing battery',
               'dead battery',
               'low battery',
               "doesn't hold charge",
               'doesnt hold charge',
               'does not hold charge',
               'no charger',
               'missing charger',
               'no os'],
 'model_patterns': ['\\bLatitude\\s+\\d{4}\\s+Detachable\\b',
                    '\\bLatitude\\s+(?:E)?\\d{4}\\b',
                    '\\bVostro\\s+\\d{4}\\b',
                    '\\bInspiron\\s+\\d{4}\\b',
                    '\\bPrecision\\s+\\d{4}\\b',
                    '\\bXPS\\s+(?:13|15|17)\\s+(?:L\\d{3,4}X|\\d{4})\\b',
                    '\\bXPS\\s+(?:13|15|17)\\b',
                    '\\b(?:HP\\s+)?(?:Laptop\\s+)?(?:240|245|250|255|340|348|430|440|450|455|470)\\s+G\\d{1,2}\\b',
                    '\\bEliteBook\\s+\\d{3}\\s+G\\d{1,2}\\b',
                    '\\bProBook\\s+\\d{3}\\s+G\\d{1,2}\\b',
                    '\\bZBook\\s+[A-Za-z0-9 ]+G\\d{1,2}\\b',
                    '\\bHP\\s+\\d{2,3}s-[A-Za-z0-9-]+\\b',
                    '\\bHP\\s+\\d{3}\\s+G\\d{1,2}\\b',
                    '\\bThinkPad\\s+(?:T|X|E|L|P)\\d{2,3}[A-Za-z]?(?:\\s+Gen\\s+\\d+)?\\b',
                    '\\bThinkPad\\s+X1\\s+Carbon(?:\\s+Gen\\s+\\d+)?\\b',
                    '\\bThinkPad\\s+X1\\s+Yoga(?:\\s+Gen\\s+\\d+)?\\b',
                    '\\bIdeaPad\\s+(?:Slim\\s+)?[A-Za-z0-9-]+(?:\\s+[A-Za-z0-9-]+)?\\b',
                    '\\bSurface\\s+Pro\\s+\\d{1,2}(?:\\+|\\s+Plus)?(?!\\w)',
                    '\\bSurface\\s+Laptop\\s+\\d{1,2}\\b',
                    '\\bGalaxy\\s+Book(?:\\d)?(?:\\s+Pro)?(?:\\s+360)?\\b',
                    '\\bLG\\s+Gram\\s+\\d{2,4}[A-Za-z0-9-]*\\b']}

_RUNTIME_RULES = {
    key: list(values)
    for key, values in DEFAULT_RULE_GROUPS.items()
}
_SETTINGS_SESSIONS = {}


# ============================================================
# HELPERS
# ============================================================

def utcnow():
    return datetime.now(
        timezone.utc
    )


def iso_now():
    return utcnow().isoformat()


def utc_day():
    return utcnow().date().isoformat()


def relative_age(value):
    if not value:
        return "—"
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        seconds = max(0, int((utcnow() - dt.astimezone(timezone.utc)).total_seconds()))
        if seconds < 60:
            return f"{seconds}s"
        minutes = seconds // 60
        if minutes < 60:
            return f"{minutes}m"
        hours = minutes // 60
        if hours < 24:
            return f"{hours}h {minutes % 60}m"
        days = hours // 24
        if days < 30:
            return f"{days}d {hours % 24}h"
        return f"{days // 30}mo {days % 30}d"
    except Exception:
        return "—"


def normalise(text):
    if text is None:
        return ""

    text = str(text)

    text = (
        text
        .replace("–", "-")
        .replace("—", "-")
        .replace("-", "-")
    )

    text = re.sub(
        r"\s+",
        " ",
        text
    )

    return text.strip()


def yesno(value):
    if value is True or value == 1:
        return "YES"

    if value is False or value == 0:
        return "NO"

    return "UNKNOWN"


def safe_float(value):
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def percentile(values, p):
    if not values:
        return None

    values = sorted(values)

    if len(values) == 1:
        return values[0]

    k = (len(values) - 1) * p

    floor = math.floor(k)
    ceil = math.ceil(k)

    if floor == ceil:
        return values[int(k)]

    return (
        values[floor] * (ceil - k)
        + values[ceil] * (k - floor)
    )


# ============================================================
# DATABASE
# ============================================================

def connect_db():
    conn = sqlite3.connect(
        DB,
        timeout=30
    )

    conn.row_factory = sqlite3.Row

    return conn


def ensure_column(
    conn,
    table,
    column,
    sql_type
):
    columns = {
        row["name"]
        for row in conn.execute(
            f"PRAGMA table_info({table})"
        )
    }

    if column not in columns:
        conn.execute(
            f"ALTER TABLE {table} "
            f"ADD COLUMN {column} {sql_type}"
        )



def _setting_default(key):
    if key not in SETTINGS_SCHEMA_BY_KEY:
        raise KeyError(key)
    return globals()[key]


def _coerce_setting(meta, value):
    kind = meta["type"]
    if kind == "int":
        parsed = int(str(value).strip())
    elif kind == "float":
        parsed = float(str(value).strip())
    elif kind == "bool":
        parsed = str(value).strip().lower() in ("1", "true", "yes", "on")
    else:
        parsed = str(value)

    if isinstance(parsed, (int, float)):
        if "min" in meta and parsed < meta["min"]:
            raise ValueError(f'{meta["label"]} must be at least {meta["min"]}')
        if "max" in meta and parsed > meta["max"]:
            raise ValueError(f'{meta["label"]} must be no more than {meta["max"]}')
    return parsed


def _setting_text(value):
    if isinstance(value, bool):
        return "1" if value else "0"
    return str(value)


def app_setting(conn, key):
    meta = SETTINGS_SCHEMA_BY_KEY[key]
    row = conn.execute(
        "SELECT value FROM app_settings WHERE key=?",
        (key,),
    ).fetchone()
    if not row:
        return _setting_default(key)
    try:
        return _coerce_setting(meta, row["value"])
    except Exception:
        return _setting_default(key)


def refresh_runtime_settings(conn=None):
    own = conn is None
    if own:
        conn = connect_db()
    try:
        for meta in SETTINGS_SCHEMA:
            globals()[meta["key"]] = app_setting(conn, meta["key"])
        # All adaptive poll bands deliberately follow the single editable
        # polling interval used by timestamp-window discovery.
        globals()["POLL_60_PERCENT"] = globals()["POLL_NORMAL"]
        globals()["POLL_75_PERCENT"] = globals()["POLL_NORMAL"]
        globals()["POLL_85_PERCENT"] = globals()["POLL_NORMAL"]
        globals()["POLL_90_PERCENT"] = globals()["POLL_NORMAL"]
    finally:
        if own:
            conn.close()


def _password_hash(password):
    if not password:
        raise ValueError("Password cannot be empty")
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        SETTINGS_PBKDF2_ITERATIONS,
    )
    return (
        f"pbkdf2_sha256${SETTINGS_PBKDF2_ITERATIONS}$"
        f"{salt.hex()}${digest.hex()}"
    )


def _password_matches(password, encoded):
    try:
        algorithm, rounds, salt_hex, digest_hex = str(encoded).split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        candidate = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            bytes.fromhex(salt_hex),
            int(rounds),
        )
        return hmac.compare_digest(candidate.hex(), digest_hex)
    except Exception:
        return False


def _ensure_settings_password(conn):
    row = conn.execute(
        "SELECT value FROM settings_auth WHERE key='password_hash'"
    ).fetchone()
    if row:
        return
    initial = os.environ.get(SETTINGS_PASSWORD_ENV, "")
    if initial:
        conn.execute(
            "INSERT INTO settings_auth(key,value,updated_at) VALUES ('password_hash',?,?)",
            (_password_hash(initial), iso_now()),
        )


def current_rules_revision(conn=None):
    own = conn is None
    if own:
        conn = connect_db()
    try:
        row = conn.execute(
            "SELECT value FROM app_meta WHERE key='rules_revision'"
        ).fetchone()
        return int(row["value"]) if row else 1
    finally:
        if own:
            conn.close()


def refresh_classifier_rules(conn=None):
    global _RUNTIME_RULES
    own = conn is None
    if own:
        conn = connect_db()
    try:
        result = {}
        for category, fallback in DEFAULT_RULE_GROUPS.items():
            rows = conn.execute(
                """
                SELECT value
                FROM classifier_rules
                WHERE category=? AND enabled=1
                ORDER BY position,id
                """,
                (category,),
            ).fetchall()
            result[category] = [r["value"] for r in rows] if rows else list(fallback)
        _RUNTIME_RULES = result
    finally:
        if own:
            conn.close()


def classifier_rule_values(category, fallback=None):
    values = _RUNTIME_RULES.get(category)
    if values is not None:
        return values
    return list(fallback or [])


def _seed_classifier_rules(conn):
    """
    Seed built-in classifier rules without overwriting user configuration.

    Existing rules, including disabled rules, are retained. Newly-added
    built-in defaults are appended only when that exact rule does not already
    exist in the category.
    """
    for category, values in DEFAULT_RULE_GROUPS.items():
        existing_rows = conn.execute(
            """
            SELECT value, position
            FROM classifier_rules
            WHERE category=?
            ORDER BY position,id
            """,
            (category,),
        ).fetchall()

        existing = {
            row["value"]
            for row in existing_rows
        }

        next_position = (
            max(
                (row["position"] or 0)
                for row in existing_rows
            ) + 1
            if existing_rows
            else 0
        )

        for value in values:
            if value in existing:
                continue

            conn.execute(
                """
                INSERT INTO classifier_rules(
                    category,
                    position,
                    value,
                    enabled,
                    updated_at
                )
                VALUES (?,?,?,?,?)
                """,
                (
                    category,
                    next_position,
                    value,
                    1,
                    iso_now(),
                ),
            )

            existing.add(value)
            next_position += 1


def _bump_rules_revision(conn):
    revision = current_rules_revision(conn) + 1
    conn.execute(
        """
        INSERT INTO app_meta(key,value)
        VALUES ('rules_revision',?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """,
        (str(revision),),
    )
    # Existing rows are now stale for the rule set. The reanalysis worker
    # understands rules_revision separately from the Python classifier version.
    conn.execute(
        """
        UPDATE listings
        SET valuation_basis='REANALYSIS_REQUIRED'
        WHERE COALESCE(active,1)=1
        """
    )
    return revision


def _audit_setting(conn, kind, key, old_value, new_value):
    conn.execute(
        """
        INSERT INTO settings_audit(changed_at,kind,setting_key,old_value,new_value)
        VALUES (?,?,?,?,?)
        """,
        (iso_now(), kind, key, None if old_value is None else str(old_value),
         None if new_value is None else str(new_value)),
    )


def init_db():
    conn = connect_db()

    conn.execute("""
        CREATE TABLE IF NOT EXISTS listings (
            item_id TEXT PRIMARY KEY,
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            title TEXT,
            condition TEXT,
            price REAL,
            postage REAL,
            total REAL,
            buying_options TEXT,
            url TEXT,
            end_date TEXT,
            win11 INTEGER,
            usbc_pd INTEGER,
            status TEXT,
            notified INTEGER DEFAULT 0
        )
    """)

    additions = {
        "cpu": "TEXT",
        "cpu_confidence": "TEXT",
        "cpu_source": "TEXT",
        "cpu_generation": "INTEGER",

        "brand": "TEXT",
        "model": "TEXT",
        "ram_gb": "INTEGER",
        "storage_gb": "INTEGER",
        "image_url": "TEXT",

        "fault_reasons": "TEXT",
        "detail_status": "TEXT",

        "estimated_value": "REAL",
        "valuation_q1": "REAL",
        "valuation_q3": "REAL",
        "comparable_count": "INTEGER",
        "valuation_confidence": "TEXT",

        "undervaluation_gbp": "REAL",
        "undervaluation_pct": "REAL",
        "deal_score": "REAL",

        "valuation_basis": "TEXT",

        "classifier_version": "TEXT",
        "rules_revision": "INTEGER DEFAULT 0",

        "usbc_pd_confidence": "TEXT",
        "usbc_pd_source": "TEXT",
        "usbc_pd_evidence": "TEXT",
        "usbc_pd_watts": "INTEGER",

        "win11_confidence": "TEXT",
        "win11_source": "TEXT",
        "win11_evidence": "TEXT",
        "win11_state": "TEXT",
        "win11_official": "INTEGER",
        "win11_cpu_mark": "INTEGER",

        "valuation_research_at": "TEXT",

        "listed_at": "TEXT",
        "active": "INTEGER DEFAULT 1",
        "inactive_since": "TEXT",
        "availability_checked_at": "TEXT",
        "inactive_reason": "TEXT",
    }

    for name, sql_type in additions.items():
        ensure_column(
            conn,
            "listings",
            name,
            sql_type
        )

    conn.execute("""
        CREATE TABLE IF NOT EXISTS observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            price REAL,
            postage REAL,
            total REAL,
            buying_options TEXT,
            end_date TEXT
        )
    """)

    conn.execute("""
        CREATE INDEX IF NOT EXISTS
        idx_observations_item_time
        ON observations (
            item_id,
            observed_at
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS cpu_benchmarks (
            lookup_key TEXT PRIMARY KEY,
            cpu_name TEXT NOT NULL,
            cpu_mark INTEGER NOT NULL,
            cpu_rank INTEGER,
            source_url TEXT,
            updated_at TEXT NOT NULL
        )
    """)

    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_cpu_benchmarks_name
        ON cpu_benchmarks (cpu_name)
    """)


    conn.execute("""
        CREATE TABLE IF NOT EXISTS api_usage (
            day TEXT NOT NULL,
            api TEXT NOT NULL,
            operation TEXT NOT NULL,
            calls INTEGER NOT NULL DEFAULT 0,

            PRIMARY KEY (
                day,
                api,
                operation
            )
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS model_capabilities (
            model TEXT PRIMARY KEY,
            usbc_pd INTEGER,
            win11_capable INTEGER,
            source TEXT,
            updated TEXT
        )
    """)

    # v0.7 USB-C capability evidence. Keep the old table/key intact so this
    # migration is safe for existing hunter.db files.
    for name, sql_type in {
        "brand": "TEXT",
        "usbc_pd_confidence": "TEXT",
        "usbc_pd_source": "TEXT",
        "usbc_pd_evidence": "TEXT",
        "usbc_pd_watts": "INTEGER",
        "verified_at": "TEXT",
    }.items():
        ensure_column(conn, "model_capabilities", name, sql_type)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS capability_queue (
            capability_key TEXT PRIMARY KEY,
            brand TEXT,
            model TEXT,
            capability TEXT NOT NULL DEFAULT 'USB_C_PD',
            status TEXT NOT NULL DEFAULT 'PENDING',
            attempts INTEGER NOT NULL DEFAULT 0,
            first_seen TEXT NOT NULL,
            last_attempt TEXT,
            next_attempt TEXT,
            last_error TEXT
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS cpu_capabilities (
            cpu_key TEXT PRIMARY KEY,
            cpu TEXT NOT NULL,
            win11_approved INTEGER,
            confidence TEXT,
            source TEXT,
            evidence TEXT,
            verified_at TEXT
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS sold_searches (
            query_key TEXT PRIMARY KEY,
            keywords TEXT NOT NULL,
            searched_at TEXT,
            status TEXT,
            result_count INTEGER DEFAULT 0,
            error TEXT
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS sold_comparables (
            query_key TEXT NOT NULL,
            item_id TEXT NOT NULL,
            title TEXT,
            brand TEXT,
            model TEXT,
            cpu TEXT,
            cpu_generation INTEGER,
            ram_gb INTEGER,
            storage_gb INTEGER,
            avg_sold_price REAL,
            avg_postage REAL,
            delivered_price REAL,
            units_sold INTEGER DEFAULT 1,
            total_sales REAL,
            last_sold TEXT,
            formats TEXT,
            collected_at TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'EBAY_PRODUCT_RESEARCH',
            PRIMARY KEY (query_key, item_id)
        )
    """)

    for name, sql_type in {
        "evidence_version": "TEXT",
        "currency": "TEXT",
        "extended_title": "TEXT",
    }.items():
        ensure_column(conn, "sold_comparables", name, sql_type)

    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_sold_model
        ON sold_comparables (brand, model)
    """)

    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_sold_cpu
        ON sold_comparables (cpu, ram_gb, storage_gb)
    """)

    # Capability verification queues are no longer used:
    # Windows 11 suitability comes directly from CPU assessment and USB-C PD
    # is no longer a qualification requirement.
    conn.execute("""
        DELETE FROM capability_queue
        WHERE status='PENDING'
          AND capability IN (
              'USB_C_PD',
              'WIN11_APPROVED'
          )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS settings_auth (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS app_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)

    conn.execute("""
        INSERT INTO app_meta(key,value)
        VALUES ('rules_revision','1')
        ON CONFLICT(key) DO NOTHING
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS classifier_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT NOT NULL,
            position INTEGER NOT NULL DEFAULT 0,
            value TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            updated_at TEXT NOT NULL
        )
    """)

    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_classifier_rules_category
        ON classifier_rules(category,position)
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS settings_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            changed_at TEXT NOT NULL,
            kind TEXT NOT NULL,
            setting_key TEXT NOT NULL,
            old_value TEXT,
            new_value TEXT
        )
    """)

    _seed_classifier_rules(conn)
    _ensure_settings_password(conn)

    conn.commit()
    refresh_runtime_settings(conn)
    refresh_classifier_rules(conn)
    conn.close()


# ============================================================
# API BUDGET
# ============================================================

def record_api_call(
    conn,
    api,
    operation
):
    conn.execute("""
        INSERT INTO api_usage (
            day,
            api,
            operation,
            calls
        )
        VALUES (?, ?, ?, 1)

        ON CONFLICT (
            day,
            api,
            operation
        )
        DO UPDATE SET
            calls = calls + 1
    """, (
        utc_day(),
        api,
        operation
    ))

    conn.commit()

    if api == "BROWSE":
        global _ebay_browse_quota_local_delta

        with _ebay_browse_quota_lock:
            _ebay_browse_quota_local_delta += 1


# eBay Developer Analytics quota state.
#
# The Browse API quota is authoritative. We cache eBay's reported count and
# reset time, then add calls made locally after that snapshot so the safety
# budget remains current between Analytics refreshes.

EBAY_QUOTA_CACHE_SECONDS = 300

_ebay_browse_quota_cache = {
    "fetched_at": 0.0,
    "data": None,
    "error": None,
}

_ebay_browse_quota_local_delta = 0
_ebay_browse_quota_lock = threading.Lock()


def _parse_ebay_reset(value):
    if not value:
        return None

    try:
        text = str(value).strip()

        if text.endswith("Z"):
            text = text[:-1] + "+00:00"

        dt = datetime.fromisoformat(text)

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        return dt.astimezone(timezone.utc)

    except Exception:
        return None


def _format_quota_countdown(seconds):
    seconds = max(0, int(seconds))

    hours, remainder = divmod(seconds, 3600)
    minutes, _ = divmod(remainder, 60)

    if hours:
        return f"{hours}h {minutes}m"

    return f"{minutes}m"


def ebay_browse_quota(force=False):
    """
    Return eBay's application-level Browse API quota information.

    Uses Developer Analytics:
      GET /developer/analytics/v1_beta/rate_limit/
          ?api_context=buy&api_name=browse

    The response is cached because the quota endpoint itself does not need
    to be called for every Browse request.
    """
    global _ebay_browse_quota_local_delta

    now = time.time()

    with _ebay_browse_quota_lock:
        cached = _ebay_browse_quota_cache.get("data")
        fetched_at = float(
            _ebay_browse_quota_cache.get("fetched_at")
            or 0
        )

    if cached and not force:
        reset_dt = _parse_ebay_reset(
            cached.get("reset")
        )

        cache_fresh = (
            now - fetched_at
            < EBAY_QUOTA_CACHE_SECONDS
        )

        reset_still_current = (
            reset_dt is None
            or reset_dt.timestamp() > now
        )

        if cache_fresh and reset_still_current:
            return cached

    with _ebay_browse_quota_lock:
        delta_before = _ebay_browse_quota_local_delta

    try:
        token = get_token()

        params = urllib.parse.urlencode({
            "api_context": "buy",
            "api_name": "browse",
        })

        url = (
            "https://api.ebay.com/"
            "developer/analytics/v1_beta/"
            "rate_limit/?"
            + params
        )

        req = urllib.request.Request(
            url,
            headers={
                "Authorization":
                    f"Bearer {token}",
                "Accept":
                    "application/json",
            }
        )

        # Deliberately do not use api_get() here:
        # Analytics calls must not count against our Browse accounting.
        with urllib.request.urlopen(
            req,
            timeout=20
        ) as response:

            status = getattr(
                response,
                "status",
                200
            )

            if status == 204:
                raise RuntimeError(
                    "eBay Analytics returned HTTP 204"
                )

            payload = json.load(response)

        candidates = []

        for rate_limit in payload.get(
            "rateLimits",
            []
        ):
            api_name = str(
                rate_limit.get("apiName") or ""
            ).lower()

            if api_name and api_name != "browse":
                continue

            for resource in rate_limit.get(
                "resources",
                []
            ):
                resource_name = (
                    resource.get("name")
                    or "Browse"
                )

                for rate in resource.get(
                    "rates",
                    []
                ):
                    try:
                        limit = int(
                            rate.get("limit")
                            or 0
                        )

                        remaining = int(
                            rate.get("remaining")
                            or 0
                        )

                        time_window = int(
                            rate.get("timeWindow")
                            or 0
                        )

                        count_value = rate.get(
                            "count"
                        )

                        if count_value is None:
                            count = max(
                                0,
                                limit - remaining
                            )
                        else:
                            count = int(
                                count_value
                            )

                        reset = (
                            rate.get("reset")
                            or rate.get("resetTime")
                        )

                    except (
                        TypeError,
                        ValueError
                    ):
                        continue

                    if limit <= 0:
                        continue

                    candidates.append({
                        "limit": limit,
                        "remaining": remaining,
                        "count": count,
                        "reset": reset,
                        "time_window": time_window,
                        "resource": resource_name,
                    })

        if not candidates:
            raise RuntimeError(
                "No Browse quota returned by "
                "eBay Developer Analytics"
            )

        # Prefer the daily Browse quota. If eBay returns more than one
        # Browse resource, use the one currently showing the greatest
        # utilisation. This is conservative for our safety budget.
        daily = [
            candidate
            for candidate in candidates
            if candidate["time_window"]
            >= 23 * 3600
        ]

        pool = daily or candidates

        quota = max(
            pool,
            key=lambda candidate: (
                candidate["count"],
                -candidate["remaining"],
                candidate["time_window"],
            )
        )

        quota = dict(quota)
        quota["source"] = "eBay Analytics"

        with _ebay_browse_quota_lock:
            # Preserve calls made while the Analytics HTTP request
            # itself was in flight.
            current_delta = (
                _ebay_browse_quota_local_delta
            )

            _ebay_browse_quota_local_delta = max(
                0,
                current_delta - delta_before
            )

            _ebay_browse_quota_cache[
                "data"
            ] = quota

            _ebay_browse_quota_cache[
                "fetched_at"
            ] = time.time()

            _ebay_browse_quota_cache[
                "error"
            ] = None

        return quota

    except Exception as exc:
        with _ebay_browse_quota_lock:
            _ebay_browse_quota_cache[
                "error"
            ] = str(exc)

            old = _ebay_browse_quota_cache.get(
                "data"
            )

        # A still-valid previous response is preferable to guessing.
        if old:
            reset_dt = _parse_ebay_reset(
                old.get("reset")
            )

            if (
                reset_dt is not None
                and reset_dt.timestamp() > now
            ):
                return old

        return None


def browse_usage_local_today(conn):
    """
    Local UTC-day counter retained for diagnostics/fallback only.
    Budget enforcement uses eBay's real quota window whenever available.
    """
    row = conn.execute("""
        SELECT
            COALESCE(
                SUM(calls),
                0
            ) AS n
        FROM api_usage
        WHERE day=?
          AND api='BROWSE'
    """, (
        utc_day(),
    )).fetchone()

    return int(row["n"] or 0)


def operation_usage(
    conn,
    operation
):
    row = conn.execute("""
        SELECT
            COALESCE(
                SUM(calls),
                0
            ) AS n
        FROM api_usage
        WHERE day=?
          AND api='BROWSE'
          AND operation=?
    """, (
        utc_day(),
        operation
    )).fetchone()

    return int(row["n"] or 0)


def browse_budget_status(conn):
    quota = ebay_browse_quota()

    if quota:
        with _ebay_browse_quota_lock:
            delta = int(
                _ebay_browse_quota_local_delta
            )

            quota_error = (
                _ebay_browse_quota_cache.get(
                    "error"
                )
            )

        used = max(
            0,
            int(quota["count"]) + delta
        )

        limit = int(
            quota["limit"]
            or DAILY_SAFETY_LIMIT
        )

        reset_dt = _parse_ebay_reset(
            quota.get("reset")
        )

        reset_seconds = (
            max(
                0,
                int(
                    reset_dt.timestamp()
                    - time.time()
                )
            )
            if reset_dt
            else None
        )

        return {
            "used": used,
            "limit": limit,
            "remaining": max(
                0,
                limit - used
            ),
            "reset": reset_dt,
            "reset_seconds": reset_seconds,
            "time_window": int(
                quota.get("time_window")
                or 86400
            ),
            "resource": quota.get(
                "resource"
            ),
            "source": quota.get(
                "source"
            ) or "eBay Analytics",
            "error": quota_error,
        }

    # Fail safe: retain the old local UTC-day accounting if Analytics
    # is temporarily unavailable.
    local_used = browse_usage_local_today(
        conn
    )

    now = utcnow()

    seconds_today = (
        now.hour * 3600
        + now.minute * 60
        + now.second
    )

    seconds_until_midnight = max(
        0,
        86400 - seconds_today
    )

    return {
        "used": local_used,
        "limit": DAILY_SAFETY_LIMIT,
        "remaining": max(
            0,
            DAILY_SAFETY_LIMIT
            - local_used
        ),
        "reset": None,
        "reset_seconds":
            seconds_until_midnight,
        "time_window": 86400,
        "resource": None,
        "source":
            "Local UTC fallback",
        "error":
            _ebay_browse_quota_cache.get(
                "error"
            ),
    }


def browse_budget_used(conn):
    return int(
        browse_budget_status(conn)["used"]
    )


def browse_usage_today(conn):
    """
    Backwards-compatible name.

    Existing callers now receive usage for eBay's actual current quota
    window instead of an assumed UTC calendar day.
    """
    return browse_budget_used(conn)


def can_search(conn):
    return (
        browse_budget_used(conn)
        <
        DAILY_SAFETY_LIMIT
        - EMERGENCY_RESERVE
    )


def current_search_reserve(now=None):
    """
    Preserve the full search reserve during the early part of eBay's
    current quota window, then taper it down toward reset.

    SEARCH_RESERVE_TAPER_START_HOUR now means hours elapsed since the
    start of eBay's quota window rather than a UTC clock hour.
    """
    now = now or utcnow()

    quota = ebay_browse_quota()

    if quota:
        reset_dt = _parse_ebay_reset(
            quota.get("reset")
        )

        window_seconds = int(
            quota.get("time_window")
            or 86400
        )

        if reset_dt and window_seconds > 0:
            reset_ts = (
                reset_dt.timestamp()
            )

            now_ts = now.timestamp()

            window_start_ts = (
                reset_ts
                - window_seconds
            )

            elapsed = max(
                0.0,
                min(
                    float(window_seconds),
                    now_ts - window_start_ts
                )
            )

            taper_start = min(
                float(window_seconds),
                float(
                    SEARCH_RESERVE_TAPER_START_HOUR
                )
                * 3600.0
            )

            if elapsed <= taper_start:
                return SEARCH_RESERVE

            taper_span = max(
                1.0,
                float(window_seconds)
                - taper_start
            )

            progress = min(
                1.0,
                max(
                    0.0,
                    (
                        elapsed
                        - taper_start
                    )
                    / taper_span
                )
            )

        else:
            progress = None

    else:
        progress = None

    if progress is None:
        # Exact old fallback behaviour.
        hour = (
            now.hour
            + now.minute / 60.0
            + now.second / 3600.0
        )

        if (
            hour
            <= SEARCH_RESERVE_TAPER_START_HOUR
        ):
            return SEARCH_RESERVE

        taper_hours = (
            24.0
            - SEARCH_RESERVE_TAPER_START_HOUR
        )

        progress = min(
            1.0,
            max(
                0.0,
                (
                    hour
                    - SEARCH_RESERVE_TAPER_START_HOUR
                )
                / taper_hours
            )
        )

    reserve = round(
        SEARCH_RESERVE
        - (
            SEARCH_RESERVE
            - MIN_SEARCH_RESERVE
        )
        * progress
    )

    return max(
        MIN_SEARCH_RESERVE,
        min(
            SEARCH_RESERVE,
            reserve
        )
    )


def detail_api_cutoff(now=None):
    return (
        DAILY_SAFETY_LIMIT
        - EMERGENCY_RESERVE
        - current_search_reserve(now)
    )


def can_detail(conn):
    return (
        browse_budget_used(conn)
        < detail_api_cutoff()
    )


def budget_percentage(conn):
    return (
        browse_budget_used(conn)
        / DAILY_SAFETY_LIMIT
        * 100
    )


def polling_interval(conn):
    pct = budget_percentage(conn)

    if pct >= 90:
        return POLL_90_PERCENT

    if pct >= 85:
        return POLL_85_PERCENT

    if pct >= 75:
        return POLL_75_PERCENT

    if pct >= 60:
        return POLL_60_PERCENT

    return POLL_NORMAL


# ============================================================
# CPU BENCHMARKS
# ============================================================

def cpu_benchmark_key(value):
    """
    Normalise Laptop Lander CPU names and PassMark CPU names
    to a common lookup key.
    """
    value = normalise(value).lower()

    if not value:
        return ""

    value = (
        value
        .replace("®", "")
        .replace("™", "")
    )

    value = re.sub(
        r"\b(?:intel|amd)\b",
        " ",
        value,
        flags=re.I,
    )

    value = re.sub(
        r"\bcore\b",
        " ",
        value,
        flags=re.I,
    )

    value = re.sub(
        r"\b(?:processor|cpu|apu)\b",
        " ",
        value,
        flags=re.I,
    )

    value = re.sub(
        r"\bwith\s+radeon(?:\s+graphics)?\b",
        " ",
        value,
        flags=re.I,
    )

    # PassMark commonly includes PRO where our CPU parser deliberately
    # normalises the SKU without it.
    value = re.sub(
        r"\bpro\b",
        " ",
        value,
        flags=re.I,
    )

    value = value.replace("-", " ")

    value = re.sub(
        r"[^a-z0-9]+",
        " ",
        value,
    )

    return re.sub(
        r"\s+",
        " ",
        value,
    ).strip()


class PassMarkCpuListParser(HTMLParser):
    def __init__(self):
        super().__init__()

        self.in_row = False
        self.in_cell = False

        self.cells = []
        self.cell_text = []

        self.first_cell_cpu_link = None

        self.rows = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()

        if tag == "tr":
            self.in_row = True
            self.cells = []
            self.first_cell_cpu_link = None

        elif tag in ("td", "th") and self.in_row:
            self.in_cell = True
            self.cell_text = []

        elif tag == "a" and self.in_row and self.in_cell:
            # PassMark CPU rows link to cpu_lookup.php?cpu=...
            if len(self.cells) == 0:
                attrs = dict(attrs)
                href = attrs.get("href", "")

                if "cpu_lookup.php?cpu=" in href:
                    self.first_cell_cpu_link = href

    def handle_data(self, data):
        if self.in_cell:
            self.cell_text.append(data)

    def handle_endtag(self, tag):
        tag = tag.lower()

        if tag in ("td", "th") and self.in_cell:
            value = normalise(
                " ".join(self.cell_text)
            )

            self.cells.append(value)

            self.in_cell = False
            self.cell_text = []

        elif tag == "tr" and self.in_row:
            self.in_row = False

            # The CPU list contains:
            # CPU name, CPU Mark, rank, CPU value, price.
            if (
                self.first_cell_cpu_link
                and len(self.cells) >= 3
            ):
                self.rows.append((
                    list(self.cells),
                    self.first_cell_cpu_link,
                ))


def parse_passmark_cpu_list(page):
    parser = PassMarkCpuListParser()

    parser.feed(page)

    result = []

    for cells, href in parser.rows:
        name = normalise(cells[0])

        mark_text = (
            cells[1]
            .replace(",", "")
            .strip()
        )

        rank_text = (
            cells[2]
            .replace(",", "")
            .strip()
        )

        if not name:
            continue

        if not mark_text.isdigit():
            continue

        mark = int(mark_text)

        if mark <= 0:
            continue

        rank = (
            int(rank_text)
            if rank_text.isdigit()
            else None
        )

        key = cpu_benchmark_key(name)

        if not key:
            continue

        source_url = href

        if source_url.startswith("/"):
            source_url = (
                "https://www.cpubenchmark.net"
                + source_url
            )

        result.append({
            "lookup_key": key,
            "cpu_name": name,
            "cpu_mark": mark,
            "cpu_rank": rank,
            "source_url": source_url,
        })

    return result


def cpu_benchmark_cache_age_hours(conn):
    row = conn.execute("""
        SELECT MAX(updated_at) AS newest
        FROM cpu_benchmarks
    """).fetchone()

    if not row or not row["newest"]:
        return None

    try:
        dt = datetime.fromisoformat(
            str(row["newest"]).replace(
                "Z",
                "+00:00"
            )
        )

        if dt.tzinfo is None:
            dt = dt.replace(
                tzinfo=timezone.utc
            )

        return (
            utcnow()
            - dt.astimezone(timezone.utc)
        ).total_seconds() / 3600.0

    except Exception:
        return None


def refresh_cpu_benchmarks(conn, force=False):
    age = cpu_benchmark_cache_age_hours(conn)

    if (
        not force
        and age is not None
        and age < CPU_BENCHMARK_REFRESH_HOURS
    ):
        return 0

    request = urllib.request.Request(
        CPU_BENCHMARK_URL,
        headers={
            "User-Agent":
                CPU_BENCHMARK_USER_AGENT,

            "Accept":
                "text/html,application/xhtml+xml",
        },
        method="GET",
    )

    with urllib.request.urlopen(
        request,
        timeout=30,
    ) as response:
        page = response.read().decode(
            "utf-8",
            errors="replace",
        )

    rows = parse_passmark_cpu_list(page)

    if len(rows) < 1000:
        raise RuntimeError(
            "PassMark CPU list returned "
            f"only {len(rows)} usable rows"
        )

    now = iso_now()

    for row in rows:
        conn.execute("""
            INSERT INTO cpu_benchmarks (
                lookup_key,
                cpu_name,
                cpu_mark,
                cpu_rank,
                source_url,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?)

            ON CONFLICT (lookup_key)
            DO UPDATE SET
                cpu_name=excluded.cpu_name,
                cpu_mark=excluded.cpu_mark,
                cpu_rank=excluded.cpu_rank,
                source_url=excluded.source_url,
                updated_at=excluded.updated_at
        """, (
            row["lookup_key"],
            row["cpu_name"],
            row["cpu_mark"],
            row["cpu_rank"],
            row["source_url"],
            now,
        ))

    conn.commit()

    print(
        "CPU benchmarks: refreshed "
        f"{len(rows)} PassMark CPU records"
    )

    return len(rows)


def cpu_benchmark_for_cpu(conn, cpu):
    key = cpu_benchmark_key(cpu)

    if not key:
        return None

    row = conn.execute("""
        SELECT
            lookup_key,
            cpu_name,
            cpu_mark,
            cpu_rank,
            source_url,
            updated_at
        FROM cpu_benchmarks
        WHERE lookup_key=?
        LIMIT 1
    """, (
        key,
    )).fetchone()

    if row:
        return row

    rows = conn.execute("""
        SELECT
            lookup_key,
            cpu_name,
            cpu_mark,
            cpu_rank,
            source_url,
            updated_at
        FROM cpu_benchmarks
        WHERE lookup_key LIKE ?
        ORDER BY cpu_rank
        LIMIT 20
    """, (
        key + " %",
    )).fetchall()

    matches = []

    for candidate in rows:
        candidate_key = candidate["lookup_key"] or ""

        if not candidate_key.startswith(key + " "):
            continue

        suffix = candidate_key[len(key):].strip()

        if re.fullmatch(
            r"\d+(?:\s+\d+)?ghz",
            suffix,
            re.I
        ):
            matches.append(candidate)

    if len(matches) == 1:
        return matches[0]

    return None


def cpu_benchmark_refresh_worker():
    while True:
        try:
            conn = connect_db()

            try:
                refresh_cpu_benchmarks(
                    conn
                )

            finally:
                conn.close()

        except Exception as exc:
            print(
                "CPU benchmarks: refresh failed:",
                repr(exc)
            )

        # Check hourly. The actual web fetch only happens when
        # the cache is at least 24 hours old.
        time.sleep(3600)


def start_cpu_benchmark_refresh_worker():
    thread = threading.Thread(
        target=cpu_benchmark_refresh_worker,
        name="cpu-benchmark-refresh",
        daemon=True,
    )

    thread.start()



# ============================================================
# OAUTH
# ============================================================

_token = None
_token_expiry = 0


def get_token():
    global _token
    global _token_expiry

    if (
        _token
        and time.time()
        < _token_expiry - 120
    ):
        return _token

    client_id = os.environ[
        "EBAY_CLIENT_ID"
    ]

    secret = os.environ[
        "EBAY_CLIENT_SECRET"
    ]

    encoded = base64.b64encode(
        f"{client_id}:{secret}".encode()
    ).decode()

    body = urllib.parse.urlencode({
        "grant_type":
            "client_credentials",

        "scope":
            "https://api.ebay.com/oauth/api_scope",
    }).encode()

    req = urllib.request.Request(
        "https://api.ebay.com/"
        "identity/v1/oauth2/token",

        data=body,

        headers={
            "Authorization":
                f"Basic {encoded}",

            "Content-Type":
                "application/x-www-form-urlencoded"
        },

        method="POST"
    )

    with ebay_urlopen(
        req,
        timeout=30
    ) as response:

        data = json.load(
            response
        )

    _token = data[
        "access_token"
    ]

    _token_expiry = (
        time.time()
        + int(
            data.get(
                "expires_in",
                7200
            )
        )
    )

    return _token


# ============================================================
# EBAY API
# ============================================================
def api_get(
    conn,
    token,
    path,
    operation,
    params=None
):
    if operation == "SEARCH":

        if not can_search(conn):
            raise RuntimeError(
                "API_BUDGET_SEARCH_BLOCKED"
            )

    else:

        if not can_detail(conn):
            raise RuntimeError(
                "API_BUDGET_DETAIL_BLOCKED"
            )

    url = (
        "https://api.ebay.com"
        + path
    )

    if params:
        url += (
            "?"
            + urllib.parse.urlencode(
                params
            )
        )

    request = urllib.request.Request(
        url,

        headers={
            "Authorization":
                f"Bearer {token}",

            "X-EBAY-C-MARKETPLACE-ID":
                MARKETPLACE,

            "Accept":
                "application/json",
            **({"X-EBAY-C-ENDUSERCTX": "contextualLocation=" + urllib.parse.quote(
                "country=GB,zip=" + os.environ["BUYER_POSTCODE"], safe="")}
               if os.environ.get("BUYER_POSTCODE") else {})
        }
    )

    # Deliberately count attempts.
    record_api_call(
        conn,
        "BROWSE",
        operation
    )

    with ebay_urlopen(
        request,
        timeout=30
    ) as response:

        return json.load(
            response
        )


def ebay_urlopen(*args, **kwargs):
    """
    Central wrapper for eBay HTTP requests.

    HTTP 429 activates a shared cooldown and becomes EbayRateLimited.
    Other HTTP errors retain their original behaviour.
    """
    remaining = ebay_rate_limit_remaining()

    if remaining > 0:
        raise EbayRateLimited(
            f"eBay API cooldown active ({remaining}s remaining)"
        )

    try:
        return urllib.request.urlopen(
            *args,
            **kwargs
        )

    except urllib.error.HTTPError as exc:

        if exc.code == 429:
            mark_ebay_rate_limited()

            raise EbayRateLimited(
                "eBay returned HTTP 429 Too Many Requests"
            ) from exc

        raise


def ebay_datetime(value):
    return (
        value
        .astimezone(timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S.000Z")
    )


def read_search_checkpoint():
    try:
        with open(
            SEARCH_CHECKPOINT_FILE,
            "r",
            encoding="utf-8"
        ) as fh:
            data = json.load(fh)

        value = data.get("last_successful_end")
        if not value:
            return None

        dt = datetime.fromisoformat(
            str(value).replace("Z", "+00:00")
        )

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        return dt.astimezone(timezone.utc)

    except Exception:
        return None


def write_search_checkpoint(value):
    tmp = SEARCH_CHECKPOINT_FILE + ".tmp"

    data = {
        "last_successful_end":
            value.astimezone(timezone.utc).isoformat()
    }

    with open(
        tmp,
        "w",
        encoding="utf-8"
    ) as fh:
        json.dump(
            data,
            fh,
            indent=2
        )

    os.replace(
        tmp,
        SEARCH_CHECKPOINT_FILE
    )


def ebay_search_page(
    conn,
    token,
    start,
    end,
    offset=0
):
    return api_get(
        conn,
        token,

        "/buy/browse/v1/"
        "item_summary/search",

        "SEARCH",

        {
            "category_ids":
                CATEGORY,

            "filter":
                (
                    "itemLocationCountry:GB,"
                    f"itemStartDate:[{ebay_datetime(start)}"
                    f"..{ebay_datetime(end)}]"
                ),

            "sort":
                "newlyListed",

            "limit":
                str(SEARCH_LIMIT),

            "offset":
                str(offset)
        }
    )


def ebay_search(
    conn,
    token
):
    """
    Retrieve every listing from the current timestamp window.

    A small overlap deliberately repeats the edge of the previous window.
    Existing item IDs make that harmless while protecting against boundary
    timing issues.

    The checkpoint only advances after every page succeeds.
    """
    now = utcnow()

    checkpoint = read_search_checkpoint()

    if checkpoint is None:
        start = (
            now
            - timedelta(
                seconds=SEARCH_INITIAL_LOOKBACK_SECONDS
            )
        )
    else:
        start = (
            checkpoint
            - timedelta(
                seconds=SEARCH_WINDOW_OVERLAP_SECONDS
            )
        )

    # Avoid one enormous query after prolonged downtime. Catch up in chunks;
    # the checkpoint advances to the end of each successful chunk.
    end = min(
        now,
        start
        + timedelta(
            seconds=SEARCH_MAX_CATCHUP_WINDOW_SECONDS
        )
    )

    all_summaries = []
    seen_ids = set()
    offset = 0
    pages = 0

    while True:
        if not can_search(conn):
            raise RuntimeError(
                "Timestamp search window incomplete because "
                "Browse API emergency reserve was reached"
            )

        result = ebay_search_page(
            conn,
            token,
            start,
            end,
            offset
        )

        pages += 1

        page = (
            result.get("itemSummaries")
            or []
        )

        for summary in page:
            item_id = summary.get("itemId")

            if item_id and item_id in seen_ids:
                continue

            if item_id:
                seen_ids.add(item_id)

            all_summaries.append(summary)

        if len(page) < SEARCH_LIMIT:
            break

        offset += SEARCH_LIMIT

        # Browse search cannot paginate beyond 10,000 matches.
        if offset >= 10000:
            raise RuntimeError(
                "Timestamp search window exceeded "
                "Browse API pagination limit"
            )

    # Only move the checkpoint after the entire window completed.
    write_search_checkpoint(end)

    return {
        "itemSummaries": all_summaries,
        "_window_start": start,
        "_window_end": end,
        "_pages": pages,
        "_catching_up": end < now,
    }


def get_item(
    conn,
    token,
    item_id
):
    encoded = urllib.parse.quote(
        item_id,
        safe=""
    )

    return api_get(
        conn,
        token,

        f"/buy/browse/v1/"
        f"item/{encoded}",

        "GET_ITEM"
    )


# ============================================================
# PRICE
# ============================================================

def item_price(item):
    price = item.get("price") or {}
    value = safe_float(price.get("value"))
    if price.get("currency") != "GBP" or value is None or not math.isfinite(value) or value <= 0:
        return None
    return value

def shipping_price(item):
    # Missing/calculated/collection-only postage is not free delivery.
    prices = []
    for option in item.get("shippingOptions") or []:
        if "PICKUP" in str(option.get("shippingServiceCode", "")).upper():
            continue
        cost = option.get("shippingCost") or {}
        value = safe_float(cost.get("value"))
        if cost.get("currency") == "GBP" and value is not None and math.isfinite(value) and value >= 0:
            prices.append(value)
    return min(prices) if prices else None

def aspects_dict(detail):
    result = {}

    for aspect in (
        detail.get(
            "localizedAspects"
        )
        or []
    ):

        name = normalise(
            aspect.get(
                "name"
            )
        ).lower()

        value = normalise(
            aspect.get(
                "value"
            )
        )

        if not name or not value:
            continue

        result.setdefault(
            name,
            []
        ).append(
            value
        )

    return result


def aspect_first(
    detail,
    names
):
    aspects = aspects_dict(
        detail
    )

    for requested in names:

        requested = requested.lower()

        for name, values in aspects.items():

            if (
                name == requested
                or requested in name
            ):

                if values:
                    return values[0]

    return None


def all_text(
    summary,
    detail
):
    parts = [
        summary.get(
            "title",
            ""
        ),

        detail.get(
            "title",
            ""
        ),

        detail.get(
            "shortDescription",
            ""
        ),

        detail.get(
            "description",
            ""
        )
    ]

    for name, values in (
        aspects_dict(
            detail
        ).items()
    ):

        parts.append(name)

        parts.extend(values)

    return normalise(
        " ".join(
            str(x)
            for x in parts
            if x
        )
    )


# ============================================================
# BRAND
# ============================================================

BRANDS = {
    "dell": "Dell",
    "hp": "HP",
    "hewlett packard": "HP",
    "lenovo": "Lenovo",
    "asus": "ASUS",
    "acer": "Acer",
    "microsoft": "Microsoft",
    "samsung": "Samsung",
    "dynabook": "Dynabook",
    "toshiba": "Toshiba",
    "apple": "Apple",
    "msi": "MSI",
    "lg": "LG",
    "panasonic": "Panasonic",
    "medion": "Medion",
    "huawei": "Huawei",
    "honor": "Honor",
    "razer": "Razer",
    "fujitsu": "Fujitsu",
    "geo": "Geo"
}


def identify_brand(
    title,
    detail
):
    value = aspect_first(
        detail,
        [
            "Brand",
            "Manufacturer"
        ]
    )

    if value:

        lower = value.lower()

        for key, brand in (
            BRANDS.items()
        ):

            if key in lower:
                return brand

        return normalise(
            value
        )

    lower = title.lower()

    for key, brand in (
        BRANDS.items()
    ):

        if re.search(
            rf"\b{re.escape(key)}\b",
            lower,
            re.I
        ):
            return brand

    return None


# ============================================================
# MODEL
# ============================================================

MODEL_PATTERNS = [

    # Dell
    r"\bLatitude\s+\d{4}\s+Detachable\b",
    r"\bLatitude\s+(?:E)?\d{4}\b",

    r"\bVostro\s+\d{4}\b",

    r"\bInspiron\s+\d{4}\b",

    r"\bPrecision\s+"
    r"\d{4}\b",

    # XPS generation/model identifiers matter enormously to value.
    # Examples: XPS 13 L322X, XPS 13 9320, XPS 15 9520.
    r"\bXPS\s+(?:13|15|17)\s+(?:L\d{3,4}X|\d{4})\b",

    # Hyphenated seller form, e.g. "XPS 15-7590".
    r"\bXPS\s+(?:13|15|17)-\d{4}\b",

    # Family-only XPS is retained only as a discovery identity; it is never
    # precise enough for exact-model valuation.
    r"\bXPS\s+(?:13|15|17)\b",

    # HP
    # Consumer/business numeric families: generation is essential to identity.
    r"\b(?:HP\s+)?(?:Laptop\s+)?"
    r"(?:240|245|250|255|340|348|430|440|450|455|470)\s+"
    r"G\d{1,2}\b",

    r"\bEliteBook\s+"
    r"\d{3}\s+G\d{1,2}\b",

    r"\bProBook\s+"
    r"\d{3}\s+G\d{1,2}\b",

    r"\bZBook\s+"
    r"[A-Za-z0-9 ]+"
    r"G\d{1,2}\b",

    r"\bHP\s+"
    r"\d{2,3}s-[A-Za-z0-9-]+\b",

    r"\bHP\s+"
    r"\d{3}\s+G\d{1,2}\b",

    # Narrow seller-title variants recovered from Product Research.

    # HP frequently inserts "Laptop" before the numbered business model.
    r"\bHP\s+Laptop\s+(?:240|245|250|255|340|348|430|440|450|455|470)\s+G\d{1,2}\b",

    # Dell XPS forms where screen family/model are separated inconsistently.
    r"\bXPS\s+Laptop\s+(?:13|15|17)\s+\d{4}\b",
    r"\bXPS\s+(?:13|15|17)-\d{4}\b",

    # 2024 XPS 13 platform often advertised simply as "XPS 9340".
    r"\bXPS\s+9340\b",

    # Specific Inspiron 7559 seller variants.
    r"\bInspiron\s+15[- ]7559\b",

    # Latitude model number may appear after "Laptop"/screen-size wording.
    r"\bLatitude(?:\s+i[3579])?\s+Laptop(?:\s+\d{2}(?:\.\d)?(?:\s*inch)?)?\s*[- ]+\s*5400\b",

    # ASUS sellers frequently omit "VivoBook" for this platform.
    r"\bASUS\s+E510MA\b",

    # Lenovo generation spacing variants: "Gen1", "Gen2".
    r"\bThinkPad\s+P15\s+Gen\s*[12]\b",

    # Lenovo
    r"\bThinkPad\s+"
    r"(?:T|X|E|L|P)"
    r"\d{2,3}[A-Za-z]?"
    r"(?:\s+Gen\s+\d+)?\b",

    r"\bThinkPad\s+"
    r"X1\s+Carbon"
    r"(?:\s+Gen\s+\d+)?\b",

    r"\bThinkPad\s+"
    r"X1\s+Yoga"
    r"(?:\s+Gen\s+\d+)?\b",

    # Lenovo platform-coded IdeaPads. Keep screen-size / Laptop text
    # out of the canonical model identity.
    r"\bIdeaPad\s+\d{3}[A-Za-z]?-\d{2}[A-Za-z]{3}\b",

    # Older ThinkPad Edge models are conventionally matched as ThinkPad E###.
    r"\bThinkPad\s+Edge\s+E\d{3}\b",

    r"\bIdeaPad\s+"
    r"(?:Slim\s+)?"
    r"[A-Za-z0-9-]+"
    r"(?:\s+[A-Za-z0-9-]+)?\b",

    # Lenovo Legion - require the machine/platform code.
    # Examples: Legion 7 16IRX9, Legion 5 15IRX10.
    r"\bLegion\s+\d(?:i)?(?:\s+Pro)?\s+"
    r"\d{2}[A-Z]{2,5}\d{1,2}\b",

    # Toshiba / Dynabook - require the platform code.
    # Examples: Satellite Pro C660, R50-B-12V, Tecra A40-E.
    r"\bSatellite\s+Pro\s+"
    r"[A-Z]\d{2,3}(?:-[A-Z0-9]+){0,3}\b",

    r"\bTecra\s+"
    r"[A-Z]\d{2,3}(?:-[A-Z0-9]+){0,3}\b",

    r"\bPortege\s+"
    r"[A-Z]\d{2,3}(?:-[A-Z0-9]+){0,3}\b",

    # ASUS - retain the useful family where present and require an ASUS
    # platform code such as UX433FN, X1504ZA, FX517ZM, G834.
    r"\bZenBook(?:\s+\d{2})?\s+"
    r"[A-Z]{1,3}\d{3,4}[A-Z0-9-]*\b",

    r"\bVivoBook(?:\s+\d{2})?\s+"
    r"[A-Z]{1,3}\d{3,4}[A-Z0-9-]*\b",

    r"\bTUF(?:\s+Gaming)?(?:\s+[AF]\d{2})?\s+"
    r"[A-Z]{2}\d{3}[A-Z0-9-]*\b",

    r"\bROG(?:\s+(?:Strix|Zephyrus|Scar))*\s+"
    r"[A-Z]{1,3}\d{3,4}[A-Z0-9-]*\b",

    # Acer - family plus real platform code.
    # Examples: Aspire V7-581, Aspire A515-55, Swift 5 SF514-52T.
    r"\bAspire\s+"
    r"(?:V\d-\d{3}[A-Z]?|A\d{3}-\d{2}[A-Z0-9-]*)\b",

    r"\bSwift(?:\s+\d)?\s+"
    r"SF\d{3}-\d{2}[A-Z0-9-]*\b",

    r"\bTravelMate\s+"
    r"[A-Z]\d{3,4}[A-Z0-9-]*\b",

    # MSI - require GE/GS/GP/etc platform code rather than just Raider,
    # Stealth, Katana, etc.
    r"\b(?:Raider|Stealth|Katana|Pulse|Prestige|Modern)\s+"
    r"[A-Z]{2}\d{2,3}[A-Z]*(?:-[A-Z0-9]+)?\b",

    # HP consumer product codes.
    # Examples: 15s-fq2037na, 15-fc0049na, 14-ce3600na.
    r"\b(?:HP\s+)?"
    r"\d{2}s?-[a-z]{2}\d{4}[a-z]{0,2}\b",

    # Geo machines where a numbered model is explicitly stated.
    r"\bGeoBook\s+[A-Za-z0-9-]+\b",
    r"\bGeoFlex\s+\d+[A-Za-z0-9-]*\b",

    # Microsoft
    r"\bSurface\s+Pro\s+\d{1,2}(?:\+|\s+Plus)?(?!\w)",

    r"\bSurface\s+Laptop\s+"
    r"\d{1,2}\b",

    # Samsung
    r"\bGalaxy\s+Book"
    r"(?:\d)?"
    r"(?:\s+Pro)?"
    r"(?:\s+360)?\b",

    # LG
    r"\bLG\s+Gram\s+"
    r"\d{2,4}[A-Za-z0-9-]*\b",
]

# MODEL_PATTERNS is the canonical built-in list.  The settings/runtime rule
# system persists these values in classifier_rules, so keep its defaults in
# sync with the parser automatically.
DEFAULT_RULE_GROUPS["model_patterns"] = list(MODEL_PATTERNS)


def clean_model(model):
    if not model:
        return None

    model = normalise(model)

    # Model fields/aspects often redundantly include the manufacturer.  Brand
    # is stored separately, so strip it here to avoid searches such as
    # "HP HP ProBook" and "ASUS ASUS TUF".
    model = re.sub(
        r"^(?:Dell|Lenovo|Microsoft|Samsung|HP|Hewlett[ -]?Packard|ASUS|Acer|MSI|Toshiba|Dynabook|LG)\s+",
        "",
        model,
        flags=re.I,
    )
    model = re.sub(r"(Surface\s+Pro\s+\d+)\s+Plus$", r"\1+", model, flags=re.I)

    # HP "Laptop 255 G7" -> "255 G7".
    model = re.sub(
        r"^Laptop\s+((?:240|245|250|255|340|348|430|440|450|455|470)\s+G\d{1,2})$",
        r"\1",
        model,
        flags=re.I,
    )

    # Dell XPS seller formatting.
    model = re.sub(
        r"^XPS\s+Laptop\s+(13|15|17)\s+(\d{4})$",
        r"XPS \1 \2",
        model,
        flags=re.I,
    )
    model = re.sub(
        r"^XPS\s+(13|15|17)-(\d{4})$",
        r"XPS \1 \2",
        model,
        flags=re.I,
    )

    # XPS 9340 is the XPS 13 9340 platform.
    model = re.sub(
        r"^XPS\s+9340$",
        r"XPS 13 9340",
        model,
        flags=re.I,
    )

    # Dell Inspiron screen-size prefix is not part of model identity.
    model = re.sub(
        r"^Inspiron\s+15[- ]7559$",
        r"Inspiron 7559",
        model,
        flags=re.I,
    )

    # ASUS E510MA belongs to the VivoBook E510MA platform.
    model = re.sub(
        r"^(?:ASUS\s+)?E510MA$",
        r"VivoBook E510MA",
        model,
        flags=re.I,
    )

    # Lenovo commonly omits the space in generation names.
    model = re.sub(
        r"^(ThinkPad\s+P15)\s+Gen\s*([12])$",
        r"\1 Gen \2",
        model,
        flags=re.I,
    )

    # Narrow Latitude 5400 seller-title forms.
    if re.search(r"\b5400$", model, re.I) and re.match(
        r"^Latitude\b",
        model,
        re.I,
    ):
        model = "Latitude 5400"
    # Canonical Lenovo platform-code identities.
    # Seller titles often append screen size or the word "Laptop".
    model = re.sub(
        r"^(IdeaPad\s+\d{3}[A-Za-z]?-\d{2}[A-Za-z]{3})(?:\s+(?:Laptop|\d{1,2}(?:in)?))?$",
        r"\1",
        model,
        flags=re.I,
    )

    # ThinkPad Edge E### is the same platform identity as ThinkPad E###.
    model = re.sub(
        r"^ThinkPad\s+Edge\s+(E\d{3})$",
        r"ThinkPad \1",
        model,
        flags=re.I,
    )

    return model.strip()


def valuation_model(brand, model):
    """Canonical model text used for sold-market queries/comparisons."""
    value = clean_model(model)
    if not value:
        return None
    # A second pass handles pathological values such as "HP HP ProBook".
    value2 = clean_model(value)
    return value2 or value


def precise_model_for_valuation(brand, model):
    """True only when model identity is specific enough to price as a model."""
    value = normalise(valuation_model(brand, model) or "")
    if not value:
        return False

    # Family/product-line names are useful discovery hints, but are not a
    # valuation identity.  They span materially different generations/specs.
    vague = {
        "latitude", "inspiron", "vostro", "precision", "xps",
        "probook", "elitebook", "zbook", "pavilion", "envy", "spectre",
        "thinkpad", "ideapad", "yoga", "legion", "thinkbook",
        "tuf", "tuf gaming", "vivobook", "zenbook", "rog", "rog strix",
        "portege", "satellite", "tecra", "travelmate", "aspire", "swift",
        "predator", "modern", "prestige", "pulse", "katana", "stealth",
        "surface", "surface pro", "surface laptop", "galaxy book", "gram",
    }
    if value.lower() in vague:
        return False

    # Screen-size-only XPS names span many unrelated generations.
    if re.fullmatch(r"XPS\s+(?:13|15|17)", value, re.I):
        return False

    # These names are reused across generations and Intel/AMD platforms.
    if re.fullmatch(r"ThinkPad\s+(?:[TXELP]\d{2}[A-Za-z]?|X1\s+(?:Carbon|Yoga))", value, re.I):
        return False
    if re.match(r"IdeaPad\s+(?:Slim\s+)?[13579](?:\s|$)", value, re.I):
        return False

    # ASUS machine/platform codes are specific when the brand itself is ASUS.
    if (
        normalise(brand).upper() == "ASUS"
        and re.fullmatch(
            r"(?:UX|UM|X|K|F|G|GL|GU|GX|FX|FA)"
            r"\d{3,4}[A-Z0-9-]*",
            value,
            re.I,
        )
    ):
        return True

    # Strong known-specific shapes.
    patterns = (
        r"^Latitude\s+(?:E)?\d{4}(?:\s+Detachable)?$",
        r"^(?:Inspiron|Vostro|Precision)\s+\d{4}$",
        r"^XPS\s+(?:13|15|17)\s+(?:L\d{3,4}X|\d{4})$",
        r"^(?:ProBook|EliteBook)\s+\d{3}\s+G\d{1,2}$",
        r"^(?:240|245|250|255|340|348|430|440|450|455|470)\s+G\d{1,2}$",
        r"^ThinkPad\s+(?:(?:T|X|E|L|P)\d{2,3}[A-Za-z]?|X1\s+(?:Carbon|Yoga))(?:\s+Gen\s+\d+)?$",
        r"^IdeaPad\s+(?:Slim\s+)?[A-Za-z0-9-]+(?:\s+[A-Za-z0-9-]+)?$",
        r"^Surface\s+(?:Pro|Laptop)\s+\d{1,2}\+?$",
        r"^GeoBook\\s+[A-Za-z0-9-]+$",

        # Lenovo Legion with explicit platform code.
        r"^Legion\s+\d(?:i)?(?:\s+Pro)?\s+\d{2}[A-Z]{2,5}\d{1,2}$",

        # Toshiba / Dynabook platform identities.
        r"^(?:Satellite\s+Pro|Tecra|Portege)\s+"
        r"[A-Z]\d{2,3}(?:-[A-Z0-9]+){0,3}$",

        # ASUS identities require a platform/model code.
        r"^(?:ZenBook|VivoBook)(?:\s+\d{2})?\s+"
        r"[A-Z]{1,3}\d{3,4}[A-Z0-9-]*$",

        r"^TUF(?:\s+Gaming)?(?:\s+[AF]\d{2})?\s+"
        r"[A-Z]{2}\d{3}[A-Z0-9-]*$",

        r"^ROG(?:\s+(?:Strix|Zephyrus|Scar))*\s+"
        r"[A-Z]{1,3}\d{3,4}[A-Z0-9-]*$",

        # Acer identities require the actual platform code.
        r"^Aspire\s+(?:V\d-\d{3}[A-Z]?|A\d{3}-\d{2}[A-Z0-9-]*)$",
        r"^Swift(?:\s+\d)?\s+SF\d{3}-\d{2}[A-Z0-9-]*$",
        r"^TravelMate\s+[A-Z]\d{3,4}[A-Z0-9-]*$",

        # MSI platform identities.
        r"^(?:Raider|Stealth|Katana|Pulse|Prestige|Modern)\s+"
        r"[A-Z]{2}\d{2,3}[A-Z]*(?:-[A-Z0-9]+)?$",

        # HP consumer product numbers.
        r"^\d{2}s?-[a-z]{2}\d{4}[a-z]{0,2}$",

        r"^GeoBook\s+[A-Za-z0-9-]+$",
        r"^GeoFlex\s+\d+[A-Za-z0-9-]*$",
    )
    if any(re.fullmatch(p, value, re.I) for p in patterns):
        return True

    # A digit alone does not identify a generation or platform.
    return False  # Unrecognised family codes need an explicit identity rule.


def identify_model(
    title,
    detail
):
    aspect_model = aspect_first(
        detail,
        [
            "Model",
            "Product Line"
        ]
    )

    # Prefer a recognizable model in title because seller
    # "Model" aspects can sometimes contain generic garbage.
    for pattern in classifier_rule_values("model_patterns", MODEL_PATTERNS):

        match = re.search(
            pattern,
            title,
            re.I
        )

        if match:
            return clean_model(
                match.group(0)
            )

    # Some ASUS titles contain only the machine/platform code rather than a
    # family-qualified model. Restrict these codes to ASUS so GPU names such
    # as NVIDIA K2100M cannot be mistaken for laptop models.
    if identify_brand(title, detail) == "ASUS":
        match = re.search(
            r"\b(?:UX|UM|X|K|F|G|GL|GU|GX|FX|FA)"
            r"\d{3,4}[A-Z0-9-]*\b",
            title,
            re.I,
        )
        if match:
            return clean_model(match.group(0))

    if aspect_model:

        # Item specifics often contain a better generation-qualified model
        # than the title. Run the same recognizers over the aspect first.
        for pattern in classifier_rule_values("model_patterns", MODEL_PATTERNS):
            match = re.search(
                pattern,
                aspect_model,
                re.I
            )
            if match:
                return clean_model(
                    match.group(0)
                )

        value = clean_model(
            aspect_model
        )

        # "HP 470", "250", etc. is a family, not a sufficiently precise
        # valuation identity. Require the G-generation where possible.
        if (
            identify_brand(title, detail) == "HP"
            and re.fullmatch(
                r"(?:HP\s+)?(?:240|245|250|255|340|348|430|440|450|455|470)",
                value or "",
                re.I
            )
        ):
            value = None

        if (
            value
            and len(value) >= 3
            and value.lower()
            not in {
                "laptop",
                "notebook",
                "unknown",
                "does not apply",
                "n/a"
            }
        ):
            return value

    return None


# ============================================================
# RAM
# ============================================================

def parse_ram_value(text):
    if not text:
        return None

    text = normalise(text)

    patterns = [
        r"\b(?:RAM|Memory)"
        r"[:\s-]*"
        r"(\d{1,3})\s*GB\b",

        r"\b(\d{1,3})\s*GB"
        r"\s+(?:RAM|DDR[345]|Memory)\b",

        r"\b(\d{1,3})GB"
        r"\s*(?:RAM|DDR[345])\b"
    ]

    for pattern in patterns:

        match = re.search(
            pattern,
            text,
            re.I
        )

        if match:

            value = int(
                match.group(1)
            )

            if value in {
                2, 4, 6, 8, 12,
                16, 20, 24, 32,
                40, 48, 64, 96,
                128
            }:
                return value

    return None


def identify_ram(
    title,
    detail
):
    value = aspect_first(
        detail,
        [
            "RAM Size",
            "Memory",
            "Installed RAM"
        ]
    )

    if value:

        match = re.search(
            r"(\d{1,3})\s*GB",
            value,
            re.I
        )

        if match:

            ram = int(
                match.group(1)
            )

            if ram <= 128:
                return ram

    return parse_ram_value(
        title
    )


# ============================================================
# STORAGE
# ============================================================

def convert_storage(
    amount,
    unit
):
    amount = float(amount)

    if unit.upper() == "TB":
        return int(
            round(
                amount * 1024
            )
        )

    return int(
        round(amount)
    )


def parse_storage_value(text):
    if not text:
        return None

    text = normalise(text)

    patterns = [
        r"\b(\d+(?:\.\d+)?)\s*"
        r"(TB|GB)"
        r"\s*(?:SSD|NVME|M\.2)\b",

        r"\b(?:SSD|NVME|M\.2)"
        r"[:\s-]*"
        r"(\d+(?:\.\d+)?)\s*"
        r"(TB|GB)\b",

        r"\b(\d+(?:\.\d+)?)\s*"
        r"(TB|GB)"
        r"\s+(?:Storage|Drive)\b"
    ]

    for pattern in patterns:

        match = re.search(
            pattern,
            text,
            re.I
        )

        if match:

            gb = convert_storage(
                match.group(1),
                match.group(2)
            )

            if 32 <= gb <= 8192:
                return gb

    return None


def identify_storage(
    title,
    detail
):
    value = aspect_first(
        detail,
        [
            "SSD Capacity",
            "Storage Capacity",
            "Hard Drive Capacity"
        ]
    )

    if value:

        match = re.search(
            r"(\d+(?:\.\d+)?)"
            r"\s*(TB|GB)",
            value,
            re.I
        )

        if match:

            gb = convert_storage(
                match.group(1),
                match.group(2)
            )

            if 32 <= gb <= 8192:
                return gb

    return parse_storage_value(
        title
    )


# ============================================================
# CPU
# ============================================================

def cpu_result(
    name=None,
    manufacturer=None,
    family=None,
    generation=None,
    confidence="UNKNOWN",
    source="UNKNOWN"
):
    return {
        "name": name,
        "manufacturer": manufacturer,
        "family": family,
        "generation": generation,
        "confidence": confidence,
        "source": source
    }


def intel_generation(
    number
):
    value = str(number)

    if value.startswith("10"):
        return 10

    if value.startswith("11"):
        return 11

    if value.startswith("12"):
        return 12

    if value.startswith("13"):
        return 13

    if value.startswith("14"):
        return 14

    if len(value) == 4:
        try:
            return int(
                value[0]
            )
        except ValueError:
            pass

    return None


def parse_cpu(
    text,
    source
):
    text = normalise(text)

    # Core Ultra
    match = re.search(
        r"\b(?:Intel\s+)?"
        r"(?:Core\s+)?"
        r"Ultra\s+"
        r"([3579])\s+"
        r"(\d{3}[A-Z]{0,2})\b",
        text,
        re.I
    )

    if match:

        tier = match.group(1)
        model = match.group(2).upper()

        return cpu_result(
            f"Intel Core Ultra {tier} {model}",
            "Intel",
            f"Core Ultra {tier}",
            None,
            "EXACT",
            source
        )

    # Core i CPUs.
    # Covers e.g.
    # i5-8250U
    # i5-10210U
    # i5-1135G7
    # i7-1185G7
    # i5-1235U
    # i7-1260P
    match = re.search(
        r"\b(?:Intel\s+)?"
        r"(?:Core\s+)?"
        r"(i[3579])"
        r"[\s-]*"
        r"(\d{4,5})"
        r"([A-Z]{0,2}\d?[A-Z]{0,2})"
        r"\b",
        text,
        re.I
    )

    if match:

        tier = match.group(1).lower()
        number = match.group(2)
        suffix = match.group(3).upper()

        return cpu_result(
            f"{tier}-{number}{suffix}",
            "Intel",
            f"Core {tier}",
            intel_generation(
                number
            ),
            "EXACT",
            source
        )

    # Core i3-N305 etc.
    match = re.search(
        r"\b(?:Intel\s+)?"
        r"(?:Core\s+)?"
        r"(i3)[\s-]*"
        r"(N\d{3})\b",
        text,
        re.I
    )

    if match:

        return cpu_result(
            f"i3-{match.group(2).upper()}",
            "Intel",
            "Core i3 N-series",
            None,
            "EXACT",
            source
        )

    # Intel N-series
    match = re.search(
        r"\b(?:Intel\s+)?"
        r"(?:Processor\s+)?"
        r"(N(?:95|97|100|150|200|250|300|305))"
        r"\b",
        text,
        re.I
    )

    if match:

        model = match.group(1).upper()

        return cpu_result(
            f"Intel {model}",
            "Intel",
            "Intel N-series",
            None,
            "EXACT",
            source
        )

    # Ryzen
    match = re.search(
        r"\b(?:AMD\s+)?"
        r"Ryzen\s+"
        r"([3579])"
        r"(?:\s+PRO)?"
        r"\s+"
        r"(\d{4})"
        r"([A-Z]{0,3})"
        r"\b",
        text,
        re.I
    )

    if match:

        tier = match.group(1)
        number = match.group(2)
        suffix = match.group(3).upper()

        # Marketing-series descriptions such as "Ryzen 5 7000" or
        # "Ryzen 7 4000" do not identify an exact processor. Exact mobile
        # SKUs such as 7520U, 7540U and 7940HS retain EXACT confidence.
        generic_series = (
            not suffix
            and number in {
                "4000",
                "5000",
                "6000",
                "7000",
                "8000",
                "9000",
            }
        )

        return cpu_result(
            f"AMD Ryzen {tier} "
            f"{number}{suffix}",
            "AMD",
            f"Ryzen {tier}",
            int(number[0]),
            "MODEL" if generic_series else "EXACT",
            source
        )

    # Intel Core 3 N-series, e.g. Core 3 N355
    match = re.search(
        r"\b(?:Intel\s+)?Core\s+3\s+"
        r"(N\d{3})\b",
        text,
        re.I
    )

    if match:
        model = match.group(1).upper()

        return cpu_result(
            f"Intel Core 3 {model}",
            "Intel",
            "Core 3 N-series",
            None,
            "EXACT",
            source
        )

    # Intel Celeron mobile processors, e.g. N4000, N4120, N4500
    match = re.search(
        r"\b(?:Intel\s+)?Celeron"
        r"(?:\s+Processor)?\s+"
        r"([NJ]\d{4})\b",
        text,
        re.I
    )

    if match:
        model = match.group(1).upper()

        return cpu_result(
            f"Intel Celeron {model}",
            "Intel",
            "Celeron",
            None,
            "EXACT",
            source
        )

    # Intel Pentium N/J-series processors.
    # Examples: Pentium N3700, N4200, N5000, N5030, N6000.
    match = re.search(
        r"\b(?:Intel\s+)?Pentium"
        r"(?:\s+(?:Silver|Gold))?\s+"
        r"([NJ]\d{4})\b",
        text,
        re.I
    )

    if match:
        model = match.group(1).upper()

        return cpu_result(
            f"Intel Pentium {model}",
            "Intel",
            "Pentium",
            None,
            "EXACT",
            source
        )

    # Common older Intel N-series listings sometimes omit the Pentium name,
    # e.g. "Intel N3700". Only accept the explicit Intel + Nxxxx form.
    match = re.search(
        r"\bIntel\s+"
        r"(N(?:3700|4200|5000|5030|6000|6005))\b",
        text,
        re.I
    )

    if match:
        model = match.group(1).upper()

        return cpu_result(
            f"Intel Pentium {model}",
            "Intel",
            "Pentium",
            None,
            "EXACT",
            source
        )

    # Intel Pentium processors where an exact mobile SKU is stated,
    # e.g. Pentium 4417U / 4415Y / 6405U.
    # Do not accept a bare numeric model such as "4415" as exact.
    match = re.search(
        r"\b(?:Intel\s+)?Pentium"
        r"(?:\s+(?:Silver|Gold))?\s+"
        r"(\d{4}[A-Z]{1,2})\b",
        text,
        re.I
    )

    if match:
        model = match.group(1).upper()

        return cpu_result(
            f"Intel Pentium {model}",
            "Intel",
            "Pentium",
            None,
            "EXACT",
            source
        )

    # Intel Atom processors, e.g. Atom Z520.
    match = re.search(
        r"\b(?:Intel\s+)?Atom\s+"
        r"([A-Z]\d{3,4})\b",
        text,
        re.I
    )

    if match:
        model = match.group(1).upper()

        return cpu_result(
            f"Intel Atom {model}",
            "Intel",
            "Atom",
            None,
            "EXACT",
            source
        )

    # Intel Core 2 Duo, e.g. T5800 / T7400.
    match = re.search(
        r"\b(?:Intel\s+)?Core\s*2\s*Duo\s+"
        r"([A-Z]\d{4})\b",
        text,
        re.I
    )

    if match:
        model = match.group(1).upper()

        return cpu_result(
            f"Intel Core 2 Duo {model}",
            "Intel",
            "Core 2 Duo",
            None,
            "EXACT",
            source
        )

    # AMD A-series APUs, e.g. A6-6310.
    match = re.search(
        r"\b(?:AMD\s+)?"
        r"(A(?:4|6|8|10|12))"
        r"[\s-]*"
        r"(\d{4})"
        r"([A-Z]{0,2})\b",
        text,
        re.I
    )

    if match:
        tier = match.group(1).upper()
        model = match.group(2)
        suffix = match.group(3).upper()

        return cpu_result(
            f"AMD {tier}-{model}{suffix}",
            "AMD",
            tier,
            None,
            "EXACT",
            source
        )

    # AMD Ryzen AI, e.g. Ryzen AI 9 HX 370 / Ryzen AI 7 350.
    # Reject numbers immediately followed by GB, which are usually RAM/SSD
    # capacities rather than a CPU model.
    match = re.search(
        r"\b(?:AMD\s+)?Ryzen\s+AI\s+"
        r"([579])\s+"
        r"(HX|PRO)?\s*"
        r"(\d{3})\b"
        r"(?!\s*GB\b)",
        text,
        re.I
    )

    if match:
        tier = match.group(1)
        modifier = (match.group(2) or "").upper()
        model = match.group(3)

        modifier_text = (
            f" {modifier}"
            if modifier
            else ""
        )

        return cpu_result(
            f"AMD Ryzen AI {tier}{modifier_text} {model}",
            "AMD",
            f"Ryzen AI {tier}",
            None,
            "EXACT",
            source
        )

    # MediaTek Chromebook processors, e.g. Kompanio 838.
    match = re.search(
        r"\b(?:MediaTek\s+)?Kompanio\s+"
        r"(\d{3,4})\b",
        text,
        re.I
    )

    if match:
        model = match.group(1)

        return cpu_result(
            f"MediaTek Kompanio {model}",
            "MediaTek",
            "Kompanio",
            None,
            "EXACT",
            source
        )

    # VIA C7 family, e.g. VIA C7-M.
    match = re.search(
        r"\bVIA\s+"
        r"(C7(?:-M)?)\b",
        text,
        re.I
    )

    if match:
        model = match.group(1).upper()

        return cpu_result(
            f"VIA {model}",
            "VIA",
            "C7",
            None,
            "EXACT",
            source
        )

    # Apple Silicon. Only accept M1-M4 when the text also clearly
    # identifies an Apple Mac product. This avoids false positives such as
    # Intel Core m3 and Panasonic FZ-M1.
    if re.search(
        r"\b(?:Apple|MacBook|Mac\s+Mini|Mac\s+Studio|iMac)\b",
        text,
        re.I
    ):
        match = re.search(
            r"\b(M[1-4])"
            r"(?:\s+(Pro|Max|Ultra))?\b",
            text,
            re.I
        )

        if match:
            generation = match.group(1).upper()
            variant = (
                match.group(2).title()
                if match.group(2)
                else None
            )

            name = (
                f"Apple {generation} {variant}"
                if variant
                else f"Apple {generation}"
            )

            return cpu_result(
                name,
                "Apple",
                generation,
                int(generation[1:]),
                "EXACT",
                source
            )

    # Generation description
    match = re.search(
        r"\b(\d{1,2})"
        r"(?:st|nd|rd|th)?"
        r"\s*(?:Gen|Generation)"
        r"\s+(?:Intel\s+)?"
        r"(?:Core\s+)?"
        r"(i[3579])\b",
        text,
        re.I
    )

    if match:

        generation = int(
            match.group(1)
        )

        tier = match.group(2).lower()

        return cpu_result(
            f"Intel Core {tier} "
            f"{generation}th Gen",
            "Intel",
            f"Core {tier}",
            generation,
            "GENERATION",
            source
        )

    match = re.search(
        r"\b(?:Intel\s+)?"
        r"(?:Core\s+)?"
        r"(i[3579])"
        r"\s+"
        r"(\d{1,2})"
        r"(?:st|nd|rd|th)?"
        r"\s*(?:Gen|Generation)\b",
        text,
        re.I
    )

    if match:

        tier = match.group(1).lower()
        generation = int(
            match.group(2)
        )

        return cpu_result(
            f"Intel Core {tier} "
            f"{generation}th Gen",
            "Intel",
            f"Core {tier}",
            generation,
            "GENERATION",
            source
        )

    return None


def identify_cpu(
    summary,
    detail
):
    aspects = aspects_dict(
        detail
    )

    candidates = []

    processor_values = []

    for name, values in (
        aspects.items()
    ):

        if (
            "processor" in name
            or name.startswith("cpu")
        ):
            processor_values.extend(
                values
            )

    # eBay processor aspects.
    for value in processor_values:

        result = parse_cpu(
            value,
            "EBAY_PROCESSOR_ASPECT"
        )

        if result:
            candidates.append(
                result
            )

    # Listing title.
    title = summary.get(
        "title",
        ""
    )

    result = parse_cpu(
        title,
        "TITLE"
    )

    if result:
        candidates.append(
            result
        )

    # Full item details.
    text = all_text(
        summary,
        detail
    )

    result = parse_cpu(
        text,
        "ITEM_DETAILS"
    )

    if result:
        candidates.append(
            result
        )

    # An exact processor identity must beat a generic generation-only
    # description, regardless of which source supplied the generic value.
    #
    # Within equal confidence, prefer structured eBay processor aspects,
    # followed by the title, then general item details.
    source_priority = {
        "EBAY_PROCESSOR_ASPECT": 0,
        "TITLE": 1,
        "ITEM_DETAILS": 2,
    }

    exact = [
        cpu
        for cpu in candidates
        if cpu.get(
            "confidence"
        ) == "EXACT"
    ]

    if exact:

        def exact_cpu_specificity(cpu):
            """
            Prefer a complete SKU over a truncated processor aspect.

            Examples:
                Ryzen 5 3500U > Ryzen 5 3500
                i7-1185G7     > i7-1185

            Source priority is used only after SKU specificity.
            """
            name = normalise(
                cpu.get("name")
                or ""
            )

            ryzen = re.search(
                r"\bRyzen\s+[3579]\s+"
                r"(\d{4})([A-Z]{1,3})\b",
                name,
                re.I
            )

            if ryzen:
                return 2

            intel = re.search(
                r"\bi[3579]-\d{4,5}"
                r"([A-Z]+\d*[A-Z]*)\b",
                name,
                re.I
            )

            if intel:
                return 2

            return 1

        exact.sort(
            key=lambda cpu: (
                -exact_cpu_specificity(cpu),
                source_priority.get(
                    cpu.get("source"),
                    99
                )
            )
        )

        return exact[0]

    generation = [
        cpu
        for cpu in candidates
        if cpu.get(
            "confidence"
        ) == "GENERATION"
    ]

    if generation:

        generation.sort(
            key=lambda cpu:
                source_priority.get(
                    cpu.get(
                        "source"
                    ),
                    99
                )
        )

        return generation[0]

    # Safe platform-level inference.
    if re.search(
        r"\bSurface\s+Pro\s+7\b",
        text,
        re.I
    ):
        return cpu_result(
            "Intel Core 10th Gen",
            "Intel",
            "Core",
            10,
            "MODEL",
            "Surface Pro 7"
        )

    return cpu_result()


# ============================================================
# WINDOWS 11
# ============================================================

WIN11_OFFICIAL = "OFFICIAL"
WIN11_UNOFFICIAL_OK = "UNOFFICIAL_OK"
WIN11_UNSUITABLE = "UNSUITABLE"
WIN11_UNKNOWN = "UNKNOWN"

WIN11_UNOFFICIAL_MIN_CPU_MARK = 5000
WIN11_MICROSOFT_VERSION = "25H2"


def _stored_cpu_descriptor(name, generation=None):
    """
    Reconstruct enough CPU metadata from an existing listings row
    to run the Windows 11 classifier without another eBay API call.
    """
    name = normalise(name)

    if not name:
        return cpu_result()

    manufacturer = None
    family = ""

    if re.search(r"\b(?:Intel|Core|i[3579]-)", name, re.I):
        manufacturer = "Intel"

    elif re.search(r"\b(?:AMD|Ryzen)\b", name, re.I):
        manufacturer = "AMD"

    elif re.search(
        r"\b(?:Qualcomm|Snapdragon|Microsoft SQ)\b",
        name,
        re.I
    ):
        manufacturer = "Qualcomm"

    m = re.search(r"\b(i[3579])[-\s]", name, re.I)

    if m:
        family = "Core " + m.group(1).lower()

    elif re.search(r"\bCore\s+Ultra\b", name, re.I):
        family = "Core Ultra"

    else:
        m = re.search(r"\bRyzen\s+([3579])\b", name, re.I)

        if m:
            family = "Ryzen " + m.group(1)

    return cpu_result(
        name,
        manufacturer,
        family,
        generation,
        "STORED",
        "DATABASE"
    )


def microsoft_windows11_cpu_status(cpu):
    """
    Processor-only Windows 11 support assessment using Microsoft's
    Windows 11 25H2 supported processor families.

    True  = CPU family is officially supported.
    False = recognised CPU family is outside Microsoft's supported set.
    None  = identity is insufficient to determine safely.
    """
    if not cpu:
        return None

    name = normalise(cpu.get("name") or "")
    manufacturer = cpu.get("manufacturer") or ""
    family = cpu.get("family") or ""
    generation = cpu.get("generation")

    if not name:
        return None

    if manufacturer == "Intel":

        if (
            family.startswith("Core Ultra")
            or re.search(r"\bCore\s+Ultra\b", name, re.I)
        ):
            return True

        if re.search(
            r"\bN(?:90|95|97|100|150|200|250|300|305|355)\b",
            name,
            re.I
        ):
            return True

        if (
            family.startswith("Core i")
            or re.search(r"\bi[3579]-\d", name, re.I)
        ):
            if generation is None:
                return None

            return generation >= 8

        if re.search(
            r"\bCeleron\s+[NJ](?:4|5)\d{3}\b",
            name,
            re.I
        ):
            return True

        if re.search(
            r"\bPentium(?:\s+Silver)?\s+"
            r"(?:J5\d{3}|N6\d{3})\b",
            name,
            re.I
        ):
            return True

        if generation is not None and generation < 8:
            return False

        return None

    if manufacturer == "AMD":

        if re.search(r"\bRyzen\s+AI\b", name, re.I):
            return True

        m = re.search(
            r"\bRyzen\s+[3579]"
            r"(?:\s+PRO)?\s+"
            r"(\d{4})"
            r"([A-Z]{0,3})\b",
            name,
            re.I
        )

        if not m:
            return None

        number = int(m.group(1))
        suffix = m.group(2).upper()

        series = (
            number // 1000
        )

        if series >= 4:
            return True

        if series == 3:
            return suffix in ("G", "GE")

        return False

    if manufacturer == "Qualcomm":

        if re.search(
            r"\b(?:Snapdragon\s+X|X1E-|X1P-|Microsoft\s+SQ[123])",
            name,
            re.I
        ):
            return True

        return None

    return None


def unofficial_windows11_family_ok(cpu):
    """
    Laptop Lander's deliberately conservative unsupported-CPU policy.

    This says the CPU family is a reasonable Windows 11 bypass candidate.
    Performance is checked separately using CPU Mark.
    """
    if not cpu:
        return False

    name = normalise(cpu.get("name") or "")
    manufacturer = cpu.get("manufacturer") or ""
    generation = cpu.get("generation")

    if manufacturer == "Intel":

        return (
            generation is not None
            and 4 <= generation <= 7
            and bool(
                re.search(
                    r"\bi[3579]-\d",
                    name,
                    re.I
                )
            )
        )

    if manufacturer == "AMD":

        m = re.search(
            r"\bRyzen\s+[3579]"
            r"(?:\s+PRO)?\s+"
            r"(\d{4})"
            r"([A-Z]{0,3})\b",
            name,
            re.I
        )

        if not m:
            return False

        number = int(m.group(1))
        suffix = m.group(2).upper()

        if 1000 <= number < 3000:
            return True

        # Ryzen 3000 mobile/non-G parts are not in Microsoft's
        # current 25H2 supported Ryzen 3000 G/GE group.
        if 3000 <= number < 4000:
            return suffix not in ("G", "GE")

        return False

    return False


def windows11_cpu_assessment(conn, cpu):
    """
    Classify processor suitability separately from whole-device
    requirements such as TPM 2.0 and Secure Boot.
    """
    name = cpu.get("name") if cpu else None

    if not name:
        return {
            "state": WIN11_UNKNOWN,
            "value": None,
            "official": None,
            "cpu_mark": None,
            "confidence": "UNKNOWN",
            "source": "CPU_NOT_IDENTIFIED",
            "evidence":
                "CPU identity is insufficient for Windows 11 classification",
        }

    official = microsoft_windows11_cpu_status(cpu)

    benchmark = cpu_benchmark_for_cpu(
        conn,
        name
    )

    cpu_mark = (
        int(benchmark["cpu_mark"])
        if benchmark
        else None
    )

    if official is True:
        return {
            "state": WIN11_OFFICIAL,
            "value": True,
            "official": True,
            "cpu_mark": cpu_mark,
            "confidence": "HIGH",
            "source": "MICROSOFT_WIN11_25H2_CPU_RULE",
            "evidence":
                "CPU falls within Microsoft's Windows 11 25H2 supported processor families",
        }

    if official is None:
        return {
            "state": WIN11_UNKNOWN,
            "value": None,
            "official": None,
            "cpu_mark": cpu_mark,
            "confidence": "UNKNOWN",
            "source": "CPU_SUPPORT_UNRESOLVED",
            "evidence":
                "CPU identity is not precise enough for Windows 11 classification",
        }

    family_ok = unofficial_windows11_family_ok(cpu)

    if (
        family_ok
        and cpu_mark is not None
        and cpu_mark >= WIN11_UNOFFICIAL_MIN_CPU_MARK
    ):
        return {
            "state": WIN11_UNOFFICIAL_OK,
            "value": True,
            "official": False,
            "cpu_mark": cpu_mark,
            "confidence": "HIGH",
            "source": "LAPTOP_LANDER_UNOFFICIAL_POLICY",
            "evidence":
                (
                    "CPU is not officially supported by Microsoft, "
                    "but is from a known practical Windows 11 family "
                    "and has CPU Mark "
                    f"{cpu_mark:,}, above the "
                    f"{WIN11_UNOFFICIAL_MIN_CPU_MARK:,} threshold"
                ),
        }

    if family_ok and cpu_mark is None:
        return {
            "state": WIN11_UNKNOWN,
            "value": None,
            "official": False,
            "cpu_mark": None,
            "confidence": "UNKNOWN",
            "source": "PASSMARK_UNAVAILABLE",
            "evidence":
                "Unsupported CPU family may be suitable, but no CPU Mark is available",
        }

    return {
        "state": WIN11_UNSUITABLE,
        "value": False,
        "official": False,
        "cpu_mark": cpu_mark,
        "confidence": "HIGH",
        "source": "LAPTOP_LANDER_UNSUPPORTED_CPU_POLICY",
        "evidence":
            "CPU is not officially supported and does not meet Laptop Lander's unofficial-good policy",
    }


def windows11_assessment(cpu):
    """
    Legacy compatibility wrapper.
    """
    return microsoft_windows11_cpu_status(cpu)


def win11_approved_assessment(conn, cpu):
    return windows11_cpu_assessment(
        conn,
        cpu
    )


def cpu_capability_key(cpu_name):
    if not cpu_name:
        return None

    return re.sub(
        r"\s+",
        "",
        normalise(cpu_name).lower()
    )


def cached_win11_capability(conn, cpu_name):
    key = cpu_capability_key(cpu_name)

    if not key:
        return None

    row = conn.execute("""
        SELECT
            win11_approved,
            confidence,
            source,
            evidence
        FROM cpu_capabilities
        WHERE cpu_key=?
    """, (
        key,
    )).fetchone()

    if (
        not row
        or row["win11_approved"] is None
    ):
        return None

    return {
        "value": bool(row["win11_approved"]),
        "confidence": row["confidence"] or "VERIFIED",
        "source": row["source"] or "CPU_CAPABILITY_CACHE",
        "evidence": row["evidence"] or "Stored CPU capability",
    }


def queue_win11_verification(conn, cpu):
    return



def set_cpu_win11_capability(
    conn, cpu_name, value, source, evidence, confidence="VERIFIED"
):
    """Administrative helper for Microsoft-backed exact CPU determinations."""
    key = cpu_capability_key(cpu_name)
    if not key:
        raise ValueError("CPU name required")
    now = iso_now()
    conn.execute("""
        INSERT INTO cpu_capabilities(
            cpu_key, cpu, win11_approved, confidence, source, evidence, verified_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(cpu_key) DO UPDATE SET
            cpu=excluded.cpu,
            win11_approved=excluded.win11_approved,
            confidence=excluded.confidence,
            source=excluded.source,
            evidence=excluded.evidence,
            verified_at=excluded.verified_at
    """, (
        key, cpu_name, None if value is None else int(bool(value)),
        confidence, source, evidence, now
    ))
    conn.execute("""
        UPDATE capability_queue
        SET status='VERIFIED', last_attempt=?, next_attempt=NULL, last_error=NULL
        WHERE capability_key=?
    """, (now, "win11:" + key))
    conn.commit()


# ============================================================
# USB-C POWER-INPUT CAPABILITY
# ============================================================

# These are retained only as "known family" hints. They are NOT automatically
# treated as VERIFIED manufacturer evidence. A cached verified capability or
# explicit charging language is stronger.
USB_PD_MODELS = [
    r"\bLatitude\s+(?:53|54|55|73|74)\d{2}\b",
    r"\bEliteBook\s+(?:830|840|850)\s+G(?:5|6|7|8|9|10|11)\b",
    r"\bProBook\s+(?:430|440|450)\s+G(?:7|8|9|10|11)\b",
    r"\bThinkPad\s+(?:T14|T15|E14|E15|L13|L14|L15)(?:\s+Gen\s+\d+)?\b",
    r"\bSurface\s+Pro\s+(?:7|8|9|10|11)\b",
    r"\bSurface\s+Laptop\s+(?:3|4|5|6|7)\b",
    r"\bGalaxy\s+Book\b",
]


def capability_key(brand, model):
    if not brand or not model:
        return None
    return normalise(f"{brand} {model}").lower()


def precise_model_for_capability(brand, model):
    """Reject identities too broad to safely share a hardware capability."""
    if not brand or not model:
        return False
    value = normalise(model)

    if brand == "HP" and re.fullmatch(
        r"(?:HP\s+)?(?:240|245|250|255|340|348|430|440|450|455|470)",
        value,
        re.I,
    ):
        return False

    # Require something more useful than a generic family word/number.
    return len(value) >= 4 and bool(re.search(r"\d", value))


def cached_usb_c_capability(conn, brand, model):
    if not precise_model_for_capability(brand, model):
        return None

    row = conn.execute("""
        SELECT usbc_pd, usbc_pd_confidence, usbc_pd_source,
               usbc_pd_evidence, usbc_pd_watts, verified_at
        FROM model_capabilities
        WHERE lower(model)=lower(?)
          AND (brand IS NULL OR lower(brand)=lower(?))
        LIMIT 1
    """, (model, brand)).fetchone()

    if not row or row["usbc_pd"] is None:
        return None

    return {
        "value": bool(row["usbc_pd"]),
        "confidence": row["usbc_pd_confidence"] or "VERIFIED",
        "source": row["usbc_pd_source"] or row["source"] or "MODEL_CAPABILITY_CACHE",
        "evidence": row["usbc_pd_evidence"] or "Previously verified model capability",
        "watts": row["usbc_pd_watts"],
    }



def queue_capability_verification(conn, brand, model):
    """
    Compatibility no-op.

    USB-C PD is no longer a requirement for deal qualification and is not
    researched or queued.
    """
    return



def usb_c_pd_assessment(conn, brand, model, text, detail):
    """
    USB-C PD is no longer a qualification requirement.

    Retain an already cached capability when one exists, otherwise return
    UNKNOWN without performing or scheduling any further capability research.
    """
    cached = cached_usb_c_capability(
        conn,
        brand,
        model
    )

    if cached:
        return cached

    return {
        "value": None,
        "confidence": "NOT_REQUIRED",
        "source": "NOT_REQUIRED",
        "evidence":
            "USB-C PD is not used for Laptop Lander deal qualification",
        "watts": None,
    }


def set_model_usb_c_capability(
    conn, brand, model, value, source, evidence,
    confidence="VERIFIED", watts=None
):
    """Administrative helper for adding manufacturer-verified capability data."""
    if not precise_model_for_capability(brand, model):
        raise ValueError("Model identity is not precise enough for capability caching")

    now = iso_now()
    conn.execute("""
        INSERT INTO model_capabilities(
            model, brand, usbc_pd, source, updated,
            usbc_pd_confidence, usbc_pd_source,
            usbc_pd_evidence, usbc_pd_watts, verified_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(model) DO UPDATE SET
            brand=excluded.brand,
            usbc_pd=excluded.usbc_pd,
            source=excluded.source,
            updated=excluded.updated,
            usbc_pd_confidence=excluded.usbc_pd_confidence,
            usbc_pd_source=excluded.usbc_pd_source,
            usbc_pd_evidence=excluded.usbc_pd_evidence,
            usbc_pd_watts=excluded.usbc_pd_watts,
            verified_at=excluded.verified_at
    """, (
        model, brand, None if value is None else int(bool(value)),
        source, now, confidence, source, evidence, watts, now
    ))
    conn.execute("""
        UPDATE capability_queue
        SET status='VERIFIED', last_attempt=?, next_attempt=NULL, last_error=NULL
        WHERE capability_key=?
    """, (now, capability_key(brand, model)))
    conn.commit()


# ============================================================
# FAULTS
# ============================================================

def classify_faults(
    title,
    condition=""
):
    text = (
        normalise(title)
        + " "
        + normalise(condition)
    ).lower()

    text = re.sub(r"\b(?:no|without)\s+(?:a\s+)?bios\s+password\b", "", text)
    text = re.sub(r"\bhinges?\s+(?:are\s+)?(?:good|fine|working|intact)\b", "", text)

    high = [
        "liquid damage",
        "water damage",
        "motherboard",
        "mainboard",
        "no power",
        "doesn't power",
        "does not power",
        "doesn't post",
        "does not post",
        "bios locked",
        "bios password",
        "faulty screen",
        "screen faulty",
        "faulty lcd",
        "lcd faulty",
        "screen damage",
        "damaged screen",
        "cracked screen",
        "broken screen",
        "broken display",
        "no display",
        "nf screen",
        "unknown fault",
        "untested",
        "spare parts",
        "spares",
        "faulty",
        "not working",
        "mdm locked",
        "activation locked",
        "autopilot locked",
        "for parts",
        "parts or not working",
        "parts only",
        "spares or repair",
        "spares or repairs",
    ]

    moderate = [
        "keyboard faulty",
        "faulty keyboard",
        "kb faulty",
        "trackpad faulty",
        "faulty trackpad",
        "faulty tp",
        "missing key",
        "missing keys",
        "keycap",
        "hinge",
        "case damage",
        "case dmg",
        "damaged case",
        "cracked case",
        "cracks",
        "dented",
        "dents",
        "dent",
        "scratch",
        "scratches",
        "scratched",
        "grade b",
        "grade c",
    ]

    low = [
        "no hdd",
        "no ssd",
        "no storage",
        "missing ssd",
        "no battery",
        "no batt",
        "missing battery",
        "dead battery",
        "low battery",
        "doesn't hold charge",
        "doesnt hold charge",
        "does not hold charge",
        "no charger",
        "missing charger",
        "no os"
    ]

    high = list(classifier_rule_values("fault_high", high))
    moderate = list(classifier_rule_values("fault_moderate", moderate))
    low = list(classifier_rule_values("fault_low", low))

    # Extra condition wording worth surfacing prominently
    # in Laptop Lander's Notes column.
    for phrase in (
        "parts only",
        "spares or repair",
        "spares or repairs",
    ):
        if phrase not in high:
            high.append(phrase)

    for phrase in (
        "scratch",
        "scratches",
        "scratched",
        "grade b",
        "grade c",
    ):
        if phrase not in moderate:
            moderate.append(phrase)

    matches = []

    for level, phrases in [
        ("HIGH_RISK", high),
        ("MODERATE", moderate),
        ("LOW_COST", low)
    ]:

        for phrase in phrases:

            if phrase in text:
                matches.append(
                    (
                        level,
                        phrase
                    )
                )

    if not matches:
        return "NORMAL", []

    severity = {
        "LOW_COST": 1,
        "MODERATE": 2,
        "HIGH_RISK": 3
    }

    worst = max(
        matches,
        key=lambda x:
            severity[x[0]]
    )[0]

    reasons = []

    for _, reason in matches:

        if reason not in reasons:
            reasons.append(
                reason
            )

    return worst, reasons


# ============================================================
# CONDITION
# ============================================================

def is_genuinely_new(
    condition
):
    condition = normalise(
        condition
    ).lower()

    return condition in {
        "new",
        "brand new",
        "new with tags",
        "new with box",
        "new without tags"
    }


# ============================================================
# ANALYSIS
# ============================================================

def analyse_listing(
    conn,
    token,
    summary,
    fetch_detail=True,
    supplied_detail=None
):
    title = normalise(
        summary.get(
            "title"
        )
    )

    condition = normalise(
        summary.get(
            "condition"
        )
    )

    if is_genuinely_new(
        condition
    ):
        return None

    item_id = summary[
        "itemId"
    ]

    detail = supplied_detail or {}
    detail_status = "COMPLETE" if supplied_detail else "NOT_REQUESTED"

    if (
        fetch_detail
        and can_detail(conn)
    ):

        try:

            detail = get_item(
                conn,
                token,
                item_id
            )

            detail_status = "COMPLETE"

        except urllib.error.HTTPError as exc:

            detail_status = (
                f"HTTP_{exc.code}"
            )

        except RuntimeError:

            detail_status = (
                "DEFERRED_API_BUDGET"
            )

        except Exception as exc:

            detail_status = (
                "ERROR:"
                + type(exc).__name__
            )

    text = all_text(
        summary,
        detail
    )

    cpu = identify_cpu(
        summary,
        detail
    )

    brand = identify_brand(
        title,
        detail
    )

    model = identify_model(
        title,
        detail
    )

    ram = identify_ram(
        title,
        detail
    )

    storage = identify_storage(
        title,
        detail
    )

    win11_assessment = win11_approved_assessment(
        conn,
        cpu
    )

    win11 = win11_assessment["value"]

    usbc_assessment = usb_c_pd_assessment(
        conn,
        brand,
        model,
        text,
        detail
    )

    usbc = usbc_assessment["value"]

    fault_level, reasons = (
        classify_faults(
            text,
            condition
        )
    )

    price = item_price(
        detail if detail.get("price") else summary
    )

    image_url = (
        (detail.get("image") or {}).get("imageUrl")
        or (summary.get("image") or {}).get("imageUrl")
        or (
            ((detail.get("thumbnailImages") or [{}])[0] or {})
            .get("imageUrl")
        )
        or (
            ((summary.get("thumbnailImages") or [{}])[0] or {})
            .get("imageUrl")
        )
    )

    postage = shipping_price(
        detail if detail.get("shippingOptions") else summary
    )

    return {
        "item_id":
            item_id,

        "title":
            title,

        "condition":
            condition,

        "price":
            price,

        "postage":
            postage,

        "total":
            price + postage if price is not None and postage is not None else None,

        "buying_options":
            summary.get(
                "buyingOptions"
            )
            or [],

        "url":
            summary.get(
                "itemWebUrl"
            )
            or "",

        "image_url":
            image_url,

        "end_date":
            summary.get(
                "itemEndDate"
            ),

        "listed_at":
            detail.get("itemCreationDate")
            or summary.get("itemCreationDate")
            or summary.get("itemStartDate")
            or detail.get("itemStartDate"),

        "brand":
            brand,

        "model":
            model,

        "ram_gb":
            ram,

        "storage_gb":
            storage,

        "cpu":
            cpu["name"],

        "cpu_generation":
            cpu["generation"],

        "cpu_confidence":
            cpu["confidence"],

        "cpu_source":
            cpu["source"],

        "win11":
            win11,

        "win11_state":
            win11_assessment["state"],

        "win11_official":
            win11_assessment["official"],

        "win11_cpu_mark":
            win11_assessment["cpu_mark"],

        "win11_confidence":
            win11_assessment["confidence"],

        "win11_source":
            win11_assessment["source"],

        "win11_evidence":
            win11_assessment["evidence"],

        "usbc_pd":
            usbc,

        "usbc_pd_confidence":
            usbc_assessment["confidence"],

        "usbc_pd_source":
            usbc_assessment["source"],

        "usbc_pd_evidence":
            usbc_assessment["evidence"],

        "usbc_pd_watts":
            usbc_assessment["watts"],

        "status":
            fault_level,

        "fault_reasons":
            reasons,

        "detail_status":
            detail_status
    }


# ============================================================
# OBSERVATIONS
# ============================================================

def save_observation(
    conn,
    item
):
    previous = conn.execute("""
        SELECT
            price,
            postage,
            total,
            buying_options,
            end_date
        FROM observations
        WHERE item_id=?
        ORDER BY id DESC
        LIMIT 1
    """, (
        item["item_id"],
    )).fetchone()

    options_json = json.dumps(
        item["buying_options"]
    )

    # Don't create thousands of identical observations.
    if previous:

        same = (
            previous["price"]
            == item["price"]
            and previous["postage"]
            == item["postage"]
            and previous["total"]
            == item["total"]
            and previous["buying_options"]
            == options_json
            and previous["end_date"]
            == item["end_date"]
        )

        if same:
            return

    conn.execute("""
        INSERT INTO observations (
            item_id,
            observed_at,
            price,
            postage,
            total,
            buying_options,
            end_date
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        item["item_id"],
        iso_now(),
        item["price"],
        item["postage"],
        item["total"],
        options_json,
        item["end_date"]
    ))


# ============================================================
# SAVE LISTING
# ============================================================

def save_listing(
    conn,
    item
):
    reconcile_item_specs_from_title(item)

    now = iso_now()

    existing = conn.execute("""
        SELECT item_id
        FROM listings
        WHERE item_id=?
    """, (
        item["item_id"],
    )).fetchone()

    values = {
        "last_seen":
            now,

        "title":
            item["title"],

        "condition":
            item["condition"],

        "price":
            item["price"],

        "postage":
            item["postage"],

        "total":
            item["total"],

        "buying_options":
            json.dumps(
                item[
                    "buying_options"
                ]
            ),

        "url":
            item["url"],

        "image_url":
            item.get("image_url"),

        "end_date":
            item["end_date"],

        "listed_at":
            item.get("listed_at"),

        "active":
            1,

        "inactive_since":
            None,

        "win11":
            None
            if item["win11"] is None
            else int(
                item["win11"]
            ),

        "win11_state":
            item.get("win11_state"),

        "win11_official":
            (
                None
                if item.get("win11_official") is None
                else int(bool(item.get("win11_official")))
            ),

        "win11_cpu_mark":
            item.get("win11_cpu_mark"),

        "win11_confidence":
            item.get("win11_confidence"),

        "win11_source":
            item.get("win11_source"),

        "win11_evidence":
            item.get("win11_evidence"),

        "usbc_pd":
            None
            if item["usbc_pd"] is None
            else int(
                item["usbc_pd"]
            ),

        "usbc_pd_confidence":
            item.get("usbc_pd_confidence"),

        "usbc_pd_source":
            item.get("usbc_pd_source"),

        "usbc_pd_evidence":
            item.get("usbc_pd_evidence"),

        "usbc_pd_watts":
            item.get("usbc_pd_watts"),

        "status":
            item["status"],

        "cpu":
            item["cpu"],

        "cpu_confidence":
            item["cpu_confidence"],

        "cpu_source":
            item["cpu_source"],

        "cpu_generation":
            item["cpu_generation"],

        "brand":
            item["brand"],

        "model":
            item["model"],

        "ram_gb":
            item["ram_gb"],

        "storage_gb":
            item["storage_gb"],

        "fault_reasons":
            json.dumps(
                item[
                    "fault_reasons"
                ]
            ),

        "detail_status":
            item["detail_status"],

        "classifier_version":
            CLASSIFIER_VERSION,

        "rules_revision":
            current_rules_revision(conn)
    }

    if existing:

        assignments = ", ".join(
            f"{key}=?"
            for key in values
        )

        conn.execute(
            f"""
            UPDATE listings
            SET {assignments}
            WHERE item_id=?
            """,
            list(
                values.values()
            )
            + [
                item[
                    "item_id"
                ]
            ]
        )

        inserted = False

    else:

        columns = [
            "item_id",
            "first_seen"
        ] + list(
            values.keys()
        )

        parameters = [
            item["item_id"],
            now
        ] + list(
            values.values()
        )

        placeholders = ",".join(
            "?"
            for _ in columns
        )

        conn.execute(
            f"""
            INSERT INTO listings (
                {",".join(columns)}
            )
            VALUES (
                {placeholders}
            )
            """,
            parameters
        )

        inserted = True

    save_observation(
        conn,
        item
    )

    conn.commit()

    return inserted


# ============================================================
# KNOWN LISTING SUMMARY UPDATE
# ============================================================

def update_known_summary(
    conn,
    summary
):
    item_id = summary[
        "itemId"
    ]

    price = item_price(
        summary
    )

    postage = shipping_price(
        summary
    )

    item = {
        "item_id":
            item_id,

        "price":
            price,

        "postage":
            postage,

        "total":
            price + postage if price is not None and postage is not None else None,

        "buying_options":
            summary.get(
                "buyingOptions"
            )
            or [],

        "end_date":
            summary.get(
                "itemEndDate"
            )
    }

    conn.execute("""
        UPDATE listings
        SET
            last_seen=?,
            price=?,
            postage=?,
            total=?,
            buying_options=?,
            end_date=?
        WHERE item_id=?
    """, (
        iso_now(),
        item["price"],
        item["postage"],
        item["total"],
        json.dumps(
            item[
                "buying_options"
            ]
        ),
        item["end_date"],
        item_id
    ))

    save_observation(
        conn,
        item
    )

    conn.commit()


# ============================================================
# PRODUCT RESEARCH / SOLD DATA
# ============================================================

def _curl_cookie_header():
    """Read the Cookie header from the user's locally saved Copy-as-cURL file."""
    try:
        raw = open(PRODUCT_RESEARCH_CURL, encoding="utf-8").read()
    except OSError:
        return None

    # Copy-as-cURL normally uses backslash-newline continuations.
    raw = raw.replace("\\\n", " ")
    try:
        args = shlex.split(raw)
    except ValueError:
        return None

    for i, arg in enumerate(args):
        if arg in ("-H", "--header") and i + 1 < len(args):
            header = args[i + 1]
            if header.lower().startswith("cookie:"):
                return header.split(":", 1)[1].strip()

    # Chrome may emit -b/--cookie instead.
    for i, arg in enumerate(args):
        if arg in ("-b", "--cookie") and i + 1 < len(args):
            return args[i + 1].strip()

    return None


def _sid_hash(value):
    if not value:
        return None
    import hashlib
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _atomic_json_write(path, payload):
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except Exception:
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)
        except Exception:
            pass


def _read_json_file(path):
    try:
        with open(path, encoding="utf-8") as f:
            value = json.load(f)
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _session_state_update(**changes):
    state = _read_json_file(PRODUCT_RESEARCH_SESSION_STATE)
    state.update(changes)
    state["updated_at"] = iso_now()
    _atomic_json_write(PRODUCT_RESEARCH_SESSION_STATE, state)
    return state


def _read_live_ebaysid():
    try:
        value = open(PRODUCT_RESEARCH_LIVE_SID, encoding="utf-8").read().strip()
        return value or None
    except OSError:
        return None


def _extract_ebaysid(cookie):
    if not cookie:
        return None
    m = re.search(r"(?:^|;\s*)ebaysid=([^;]+)", cookie)
    return m.group(1).strip() if m else None


def _replace_ebaysid(cookie, sid):
    if not cookie or not sid:
        return cookie
    if re.search(r"(?:^|;\s*)ebaysid=", cookie):
        return re.sub(
            r"(?:(?<=^)|(?<=;\s))ebaysid=[^;]+",
            "ebaysid=" + sid,
            cookie,
            count=1,
        )
    return cookie.rstrip("; ") + "; ebaysid=" + sid


def _cookie_with_live_sid(cookie):
    """Overlay the browser helper's current ebaysid onto the saved curl cookies."""
    live = _read_live_ebaysid()
    if not live:
        return cookie, False
    old = _extract_ebaysid(cookie)
    if old == live:
        return cookie, False
    cookie = _replace_ebaysid(cookie, live)
    live_hash = _sid_hash(live)
    state = _read_json_file(PRODUCT_RESEARCH_SESSION_STATE)
    if state.get("ebaysid_hash") != live_hash:
        _session_state_update(
            last_refresh_at=iso_now(),
            last_refresh_source="TrueNAS Chromium",
            ebaysid_hash=live_hash,
        )
        print(
            "Product Research session: applied new TrueNAS Chromium ebaysid "
            f"{live_hash}"
        )
    return cookie, True


def _request_browser_sid_refresh(keywords, current_sid):
    request = {
        "requested_at": iso_now(),
        "keywords": keywords,
        "current_ebaysid_hash": _sid_hash(current_sid),
    }
    _atomic_json_write(PRODUCT_RESEARCH_REFRESH_REQUEST, request)
    print("Product Research session: requested Chromium ebaysid refresh")


def _wait_for_new_live_sid(previous_sid):
    deadline = time.time() + PRODUCT_RESEARCH_REFRESH_WAIT_SECONDS
    previous_hash = _sid_hash(previous_sid)
    while time.time() < deadline:
        sid = _read_live_ebaysid()
        if sid and _sid_hash(sid) != previous_hash:
            return sid
        time.sleep(PRODUCT_RESEARCH_REFRESH_POLL_SECONDS)
    return None


def _product_research_invalid_session(raw, modules):
    # invalid_session is returned as a plain JSON object, not a SearchResultsModule.
    for obj in modules:
        if (
            obj.get("error") == "auth_required"
            and obj.get("reason_code") == "invalid_session"
        ):
            return True
    return '"reason_code":"invalid_session"' in raw.replace(" ", "")


def _product_research_fetch(url, cookie):
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "*/*",
            "Cookie": cookie,
            "Referer": "https://www.ebay.co.uk/sh/research",
            "User-Agent": "Mozilla/5.0",
            "X-Requested-With": "XMLHttpRequest",
        },
    )
    with ebay_urlopen(req, timeout=45) as response:
        return response.read().decode("utf-8", errors="replace")

def _decode_json_modules(raw):
    decoder = json.JSONDecoder()
    pos = 0
    objects = []
    while pos < len(raw):
        while pos < len(raw) and raw[pos].isspace():
            pos += 1
        if pos >= len(raw):
            break
        obj, pos = decoder.raw_decode(raw, pos)
        if isinstance(obj, dict):
            objects.append(obj)
    return objects


def _research_value(value):
    """Extract a numeric value from Product Research display structures."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        m = re.search(r"-?\d[\d,]*(?:\.\d+)?", value)
        return float(m.group(0).replace(",", "")) if m else None
    if isinstance(value, dict):
        # Prefer common direct fields first.
        for key in ("value", "text", "amount", "convertedFromValue"):
            if key in value:
                got = _research_value(value[key])
                if got is not None:
                    return got
        for v in value.values():
            got = _research_value(v)
            if got is not None:
                return got
    if isinstance(value, list):
        for v in value:
            got = _research_value(v)
            if got is not None:
                return got
    return None


def _research_text(value):
    """Extract only human-visible text from Product Research UI objects."""
    if value is None: return ""
    if isinstance(value, str): return normalise(value)
    if isinstance(value, (int, float)): return normalise(value)
    if isinstance(value, list):
        return normalise(" ".join(x for x in (_research_text(v) for v in value) if x))
    if isinstance(value, dict):
        if value.get("_type") == "TextSpan" and isinstance(value.get("text"), str):
            return normalise(value["text"])
        if value.get("_type") == "TextualDisplay":
            spans=value.get("textSpans")
            if isinstance(spans,list):
                text=normalise(" ".join(x for x in (_research_text(v) for v in spans) if x))
                if text: return text
        for key in ("title","text","accessibilityText","value"):
            v=value.get(key)
            if isinstance(v,str) and v.strip(): return normalise(v)
        for key in ("textSpans","content","children","items"):
            if key in value:
                text=_research_text(value[key])
                if text: return text
        return ""
    return ""


def product_research_search(keywords, offset=0):
    base_cookie = _curl_cookie_header()
    if not base_cookie:
        _session_state_update(
            status="NOT WORKING",
            last_failure_at=iso_now(),
            message="product-research.curl contains no usable Cookie header",
        )
        raise RuntimeError("PRODUCT_RESEARCH_COOKIE_NOT_FOUND")

    cookie, _ = _cookie_with_live_sid(base_cookie)

    now_ms = int(time.time() * 1000)
    start_ms = now_ms - PRODUCT_RESEARCH_DAY_RANGE * 86400 * 1000
    params = {
        "marketplace": "EBAY-UK",
        "keywords": keywords,
        "dayRange": str(PRODUCT_RESEARCH_DAY_RANGE),
        "endDate": str(now_ms),
        "startDate": str(start_ms),
        "categoryId": CATEGORY,
        "offset": str(offset),
        "limit": str(PRODUCT_RESEARCH_LIMIT),
        "tabName": "SOLD",
        "tz": "Europe/London",
        "modules": "searchResults",
    }
    url = "https://www.ebay.co.uk/sh/research/api/search?" + urllib.parse.urlencode(params)

    # First attempt uses the freshest SID already supplied by the local browser helper.
    raw = _product_research_fetch(url, cookie)
    modules = _decode_json_modules(raw)
    search = next(
        (x for x in modules if x.get("_type") == "SearchResultsModule"),
        None,
    )

    if search is not None:
        sid = _extract_ebaysid(cookie)
        _session_state_update(
            status="WORKING",
            last_checked_at=iso_now(),
            last_success_at=iso_now(),
            ebaysid_hash=_sid_hash(sid),
            message="Product Research sold search succeeded",
        )
        return search.get("results") or []

    if _product_research_invalid_session(raw, modules):
        failed_sid = _extract_ebaysid(cookie)
        _session_state_update(
            status="NOT WORKING",
            last_checked_at=iso_now(),
            last_failure_at=iso_now(),
            ebaysid_hash=_sid_hash(failed_sid),
            message="eBay returned auth_required / invalid_session; requesting browser refresh",
        )
        _request_browser_sid_refresh(keywords, failed_sid)
        refreshed_sid = _wait_for_new_live_sid(failed_sid)

        if refreshed_sid:
            retry_cookie = _replace_ebaysid(cookie, refreshed_sid)
            _session_state_update(
                last_refresh_at=iso_now(),
                last_refresh_source="TrueNAS Chromium",
                ebaysid_hash=_sid_hash(refreshed_sid),
            )
            print(
                "Product Research session refreshed from TrueNAS Chromium: "
                f"{_sid_hash(refreshed_sid)}"
            )
            raw = _product_research_fetch(url, retry_cookie)
            modules = _decode_json_modules(raw)
            search = next(
                (x for x in modules if x.get("_type") == "SearchResultsModule"),
                None,
            )
            if search is not None:
                _session_state_update(
                    status="WORKING",
                    last_checked_at=iso_now(),
                    last_success_at=iso_now(),
                    ebaysid_hash=_sid_hash(refreshed_sid),
                    message="Product Research succeeded after automatic Chromium refresh",
                )
                return search.get("results") or []

        _session_state_update(
            status="NOT WORKING",
            last_checked_at=iso_now(),
            last_failure_at=iso_now(),
            message="Product Research session refresh did not recover within timeout",
        )
        raise RuntimeError("PRODUCT_RESEARCH_INVALID_SESSION")

    types = ",".join(x.get("_type", "?") for x in modules)
    _session_state_update(
        status="WORKING",
        last_checked_at=iso_now(),
        message=(
            "eBay session authenticated, but Product Research returned "
            + (types or "an unexpected response")
        ),
    )
    raise RuntimeError("PRODUCT_RESEARCH_RESPONSE:" + (types or "?"))


def _product_research_session_monitor():
    """Keep dashboard session state current even when no valuation search is due."""
    while True:
        try:
            state = _read_json_file(PRODUCT_RESEARCH_SESSION_STATE)
            checked = state.get("last_checked_at")
            due = True
            if checked:
                try:
                    dt = datetime.fromisoformat(str(checked).replace("Z", "+00:00"))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    age = (utcnow() - dt.astimezone(timezone.utc)).total_seconds()
                    due = age >= PRODUCT_RESEARCH_SESSION_PROBE_SECONDS
                except Exception:
                    due = True
            if due:
                try:
                    product_research_search(PRODUCT_RESEARCH_SESSION_PROBE_QUERY, 0)
                    print("Product Research session monitor: WORKING")
                except Exception as exc:
                    print("Product Research session monitor:", repr(exc))
        except Exception as exc:
            print("Product Research session monitor error:", repr(exc))
        time.sleep(60)


def start_product_research_session_monitor():
    thread = threading.Thread(
        target=_product_research_session_monitor,
        name="product-research-session-monitor",
        daemon=True,
    )
    thread.start()


def research_query_key(keywords):
    return re.sub(r"\s+", " ", normalise(keywords).lower()).strip()



def repair_v078_model_and_sold_cache(conn):
    """One-time repair for vague XPS identities and malformed cached PR titles."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS app_migrations (
            migration_key TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL
        )
    """)
    key = "v0.7.8_specific_model_identity"
    if conn.execute(
        "SELECT 1 FROM app_migrations WHERE migration_key=?", (key,)
    ).fetchone():
        return

    # Reparse listing models from their titles so XPS 13 L322X/9320/etc.
    # become distinct identities immediately.
    rows = conn.execute("SELECT item_id,title,model FROM listings").fetchall()
    reparsed = 0
    for row in rows:
        title = normalise(row["title"])
        if not title:
            continue
        new_model = identify_model(title, {})
        if new_model and new_model != row["model"]:
            conn.execute(
                "UPDATE listings SET model=?, estimated_value=NULL, valuation_q1=NULL, "
                "valuation_q3=NULL, comparable_count=NULL, valuation_confidence=NULL, "
                "undervaluation_gbp=NULL, undervaluation_pct=NULL, deal_score=NULL, "
                "valuation_basis=NULL, valuation_research_at=NULL WHERE item_id=?",
                (new_model, row["item_id"]),
            )
            reparsed += 1

    # Cached rows produced by the old Product Research title parser are not
    # trustworthy. Remove them and invalidate only their searches for refetch.
    bad = conn.execute("""
        SELECT DISTINCT query_key FROM sold_comparables
        WHERE title LIKE '%TextualDisplay%'
           OR title LIKE '%textSpans%'
           OR title LIKE '{%'
    """).fetchall()
    bad_keys = [r["query_key"] for r in bad]
    for qk in bad_keys:
        conn.execute("DELETE FROM sold_comparables WHERE query_key=?", (qk,))
        conn.execute(
            "UPDATE sold_searches SET status='STALE', searched_at=NULL "
            "WHERE query_key=?", (qk,)
        )

    # Vague historical XPS family searches must not remain fresh when a
    # specific identifier is now available.
    for qk in ("dell xps 13", "dell xps 15", "dell xps 17"):
        conn.execute(
            "UPDATE sold_searches SET status='STALE', searched_at=NULL "
            "WHERE query_key=?", (qk,)
        )

    conn.execute(
        "INSERT INTO app_migrations(migration_key,applied_at) VALUES (?,?)",
        (key, iso_now()),
    )
    conn.commit()
    print(
        f"Migration v0.7.8: reparsed {reparsed} listing models; "
        f"invalidated {len(bad_keys)} malformed Product Research caches"
    )


def repair_v080_valuation_cache(conn):
    """Invalidate legacy price assumptions once; retain raw history for audit."""
    key = "v0.8.0_conservative_valuation"
    if conn.execute("SELECT 1 FROM app_migrations WHERE migration_key=?", (key,)).fetchone():
        return
    conn.execute("UPDATE sold_searches SET status='STALE', searched_at=NULL")
    conn.execute("""UPDATE listings SET estimated_value=NULL, valuation_q1=NULL,
        valuation_q3=NULL, comparable_count=NULL, valuation_confidence=NULL,
        undervaluation_gbp=NULL, undervaluation_pct=NULL, deal_score=NULL,
        valuation_basis='REANALYSIS_REQUIRED', valuation_research_at=NULL""")
    conn.execute("INSERT INTO app_migrations VALUES (?,?)", (key, iso_now()))
    conn.commit()


def sold_search_queries(row):
    """
    Search Product Research from most specific to broadest.

    Broad model-only searches can fill the Product Research result limit with
    unrelated configurations. Prefer exact model + CPU + RAM + storage first,
    then progressively relax the query.

    The valuation matcher remains strict; these queries only improve retrieval
    of potentially relevant sold evidence.
    """
    if target_valuation_problem(row):
        return []

    brand = normalise(
        row["brand"]
    )

    model = valuation_model(
        brand,
        row["model"]
    )

    cpu = normalise(
        row["cpu"]
    )

    ram = row_value(
        row,
        "ram_gb"
    )

    storage = row_value(
        row,
        "storage_gb"
    )

    if not brand or not model:
        return []

    base = f"{brand} {model}"

    queries = []

    if cpu and ram and storage:
        queries.append(
            f"{base} {cpu} {int(ram)}GB {int(storage)}GB"
        )

    if cpu and ram:
        queries.append(
            f"{base} {cpu} {int(ram)}GB"
        )

    if cpu:
        queries.append(
            f"{base} {cpu}"
        )

    queries.append(
        base
    )

    # Preserve order while removing accidental duplicates.
    unique = []
    seen = set()

    for query in queries:
        query = re.sub(
            r"\s+",
            " ",
            normalise(query)
        ).strip()

        key = query.lower()

        if not query or key in seen:
            continue

        seen.add(
            key
        )

        unique.append(
            query
        )

    return unique

def sold_search_is_fresh(conn, key):
    row = conn.execute(
        "SELECT searched_at, status FROM sold_searches WHERE query_key=?",
        (key,),
    ).fetchone()
    if not row or not row["searched_at"] or row["status"] != "OK":
        return False
    try:
        age = utcnow() - datetime.fromisoformat(row["searched_at"])
        return 0 <= age.total_seconds() < PRODUCT_RESEARCH_CACHE_HOURS * 3600
    except Exception:
        return False


def _parse_sold_result(result):
    listing = result.get("listing") or {}
    item_id = normalise(listing.get("itemId") or result.get("itemId"))
    title = _research_text(listing.get("title") or result.get("title"))
    if not title:
        title = _research_text(listing)

    avg_price = _research_value(result.get("avgsalesprice"))
    avg_postage = _research_value(
        result.get("averageshipping")
        if result.get("averageshipping") is not None
        else result.get("avgshipping")
    )
    if avg_postage is None and result.get("freeshipping") is True:
        avg_postage = 0.0
    currency = research_currency(result.get("avgsalesprice"))
    if currency != "GBP" or avg_price is None or not math.isfinite(avg_price):
        avg_price = None
    shipping_currency = research_currency(result.get("averageshipping") or result.get("avgshipping"))
    if shipping_currency not in (None, "GBP"):
        avg_postage = None
    if avg_postage is not None and not math.isfinite(avg_postage):
        avg_postage = None

    units = _research_value(result.get("itemssold"))
    if units is None or not math.isfinite(units) or units <= 0:
        units = 1
    total_sales = _research_value(result.get("totalsales"))
    last_sold = _research_text(result.get("datelastsold"))
    formats = _research_text(listing.get("formatList") or result.get("formatList"))

    if not item_id:
        # Stable enough for a cached research row when eBay omits itemId.
        item_id = "title:" + research_query_key(title)[:180]

    # Product Research does not expose structured item specifics here, but
    # listing.extendedTitle contains a flattened form of those specifics.
    # Keep brand/model tied to the visible title to avoid contamination from
    # noisy/multiple model names in extendedTitle, while allowing the extended
    # data to fill missing CPU/RAM/storage evidence.
    extended_title = _research_text(
        listing.get("extendedTitle")
    )

    title_cpu = (
        parse_cpu(title, "SOLD_TITLE")
        if title
        else None
    )

    extended_cpu = (
        parse_cpu(extended_title, "SOLD_TITLE")
        if extended_title
        else None
    )

    if (
        title_cpu
        and title_cpu.get("confidence") == "EXACT"
    ):
        cpu = title_cpu
    elif (
        extended_cpu
        and extended_cpu.get("confidence") == "EXACT"
    ):
        cpu = extended_cpu
    else:
        cpu = (
            title_cpu
            or extended_cpu
            or cpu_result()
        )

    brand = identify_brand(title, {})
    model = identify_model(title, {})

    ram = identify_ram(title, {})
    if ram is None and extended_title:
        ram = identify_ram(
            extended_title,
            {}
        )

    storage = identify_storage(title, {})
    if storage is None and extended_title:
        storage = identify_storage(
            extended_title,
            {}
        )

    return {
        "item_id": item_id,
        "title": title,
        "extended_title": extended_title,
        "brand": brand,
        "model": model,
        "cpu": cpu.get("name"),
        "cpu_generation": cpu.get("generation"),
        "ram_gb": ram,
        "storage_gb": storage,
        "currency": currency,
        "avg_sold_price": avg_price,
        "avg_postage": avg_postage,
        "delivered_price": (
            avg_price + avg_postage
            if avg_price is not None and avg_postage is not None and avg_postage >= 0
            else None
        ),
        "units_sold": max(1, int(units or 1)),
        "total_sales": total_sales,
        "last_sold": last_sold,
        "formats": formats,
    }


def collect_sold_search(conn, keywords):
    key = research_query_key(keywords)
    if sold_search_is_fresh(conn, key):
        return 0

    collected = 0
    try:
        conn.execute(
            """INSERT INTO sold_searches(query_key, keywords, searched_at, status, result_count, error)
               VALUES (?, ?, ?, 'RUNNING', 0, NULL)
               ON CONFLICT(query_key) DO UPDATE SET
                 keywords=excluded.keywords, searched_at=excluded.searched_at,
                 status='RUNNING', error=NULL""",
            (key, keywords, iso_now()),
        )
        conn.commit()

        # Replace this query's cached rows atomically after successful retrieval.
        parsed = []
        for page in range(PRODUCT_RESEARCH_MAX_PAGES):
            results = product_research_search(
                keywords,
                offset=page * PRODUCT_RESEARCH_LIMIT,
            )
            for result in results:
                row = _parse_sold_result(result)
                if row["delivered_price"] and row["delivered_price"] > 0:
                    parsed.append(row)
            if len(results) < PRODUCT_RESEARCH_LIMIT:
                break

        conn.execute("DELETE FROM sold_comparables WHERE query_key=?", (key,))
        for row in parsed:
            conn.execute(
                """INSERT OR REPLACE INTO sold_comparables(
                    query_key,item_id,title,extended_title,brand,model,cpu,cpu_generation,
                    ram_gb,storage_gb,avg_sold_price,avg_postage,delivered_price,
                    units_sold,total_sales,last_sold,formats,collected_at,currency,evidence_version,source
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'EBAY_PRODUCT_RESEARCH')""",
                (
                    key, row["item_id"], row["title"], row["extended_title"],
                    row["brand"], row["model"], row["cpu"], row["cpu_generation"],
                    row["ram_gb"],
                    row["storage_gb"], row["avg_sold_price"], row["avg_postage"],
                    row["delivered_price"], row["units_sold"], row["total_sales"],
                    row["last_sold"], row["formats"], iso_now(), row["currency"], SOLD_EVIDENCE_VERSION,
                ),
            )
        collected = len(parsed)
        conn.execute(
            """UPDATE sold_searches SET searched_at=?, status='OK',
               result_count=?, error=NULL WHERE query_key=?""",
            (iso_now(), collected, key),
        )
        conn.commit()
        print(f"Product Research: {keywords!r} -> {collected} sold rows")
        return collected

    except Exception as exc:
        conn.rollback()
        conn.execute(
            """INSERT INTO sold_searches(query_key,keywords,searched_at,status,result_count,error)
               VALUES (?,?,?,'ERROR',0,?)
               ON CONFLICT(query_key) DO UPDATE SET searched_at=excluded.searched_at,
               status='ERROR', error=excluded.error""",
            (key, keywords, iso_now(), type(exc).__name__ + ":" + str(exc)[:300]),
        )
        conn.commit()
        print("Product Research ERROR:", keywords, repr(exc))
        return 0


def collect_needed_sold_data(conn, maximum=None):
    if maximum is None:
        maximum = PRODUCT_RESEARCH_SEARCHES_PER_CYCLE
    """
    Work progressively through the valuation backlog.

    Valuation is deliberately independent of buying eligibility: Windows 11
    approval and USB-C charging are alert/qualification gates, not reasons to
    withhold market-value learning.

    Old behaviour always started with newest rows, which could repeatedly
    revisit the same cached-but-insufficient listings and make the valued
    count appear stuck. v0.7.2 prioritises never-valued / oldest-attempted
    listings and persists the last valuation-research attempt.
    """
    if not os.path.exists(PRODUCT_RESEARCH_CURL):
        return 0

    # Only attempt listings with enough identity to form a meaningful query.
    rows = conn.execute("""
        SELECT * FROM listings
        WHERE cpu IS NOT NULL
          AND active=1
          AND classifier_version=?
          AND COALESCE(rules_revision,0)=?
          AND trim(cpu) <> ''
          AND (
                (brand IS NOT NULL AND trim(brand) <> '')
                OR
                (model IS NOT NULL AND trim(model) <> '')
              )
        ORDER BY
            CASE WHEN estimated_value IS NULL THEN 0 ELSE 1 END,
            COALESCE(valuation_research_at, '1970-01-01') ASC,
            first_seen ASC
    """, (CLASSIFIER_VERSION, current_rules_revision(conn))).fetchall()

    done = 0
    attempted_listings = 0

    for row in rows:
        queries = sold_search_queries(row)
        if not queries or all(sold_search_is_fresh(conn, research_query_key(q)) for q in queries):
            continue

        attempted_listings += 1
        searched_this_listing = False

        for keywords in queries:
            key = research_query_key(keywords)

            if sold_search_is_fresh(conn, key):
                if calculate_sold_valuation(conn, row) is not None:
                    break
                continue

            collect_sold_search(conn, keywords)
            done += 1
            searched_this_listing = True

            valuation = calculate_sold_valuation(conn, row)
            if valuation is not None:
                print(
                    "Product Research: sufficient evidence; "
                    f"stopping at {valuation['basis']}"
                )
                break

            if done >= maximum:
                break

        # Mark the listing even when all useful queries were already cached.
        # This rotates the backlog instead of selecting the same rows forever.
        conn.execute(
            "UPDATE listings SET valuation_research_at=? WHERE item_id=?",
            (iso_now(), row["item_id"]),
        )
        conn.commit()

        if done >= maximum:
            break

    remaining = conn.execute("""
        SELECT COUNT(*) AS n
        FROM listings
        WHERE estimated_value IS NULL
          AND cpu IS NOT NULL AND trim(cpu) <> ''
          AND (
                (brand IS NOT NULL AND trim(brand) <> '')
                OR
                (model IS NOT NULL AND trim(model) <> '')
              )
    """).fetchone()["n"]

    print(
        f"Awaiting reanalysis: attempted {attempted_listings} listings this cycle; "
        f"{remaining} identifiable listings currently unvalued"
    )
    return done


def row_value(row, key, default=None):
    try:
        return row[key]
    except (KeyError, IndexError):
        return default


def research_currency(value):
    """Recognise explicit GBP evidence; never infer currency from a number."""
    if isinstance(value, dict):
        currency = value.get("currency") or value.get("currencyCode")
        if currency:
            return str(currency).upper()
        currencies = {research_currency(v) for v in value.values()} - {None}
    elif isinstance(value, list):
        currencies = {research_currency(v) for v in value} - {None}
    elif isinstance(value, str):
        currencies = set()
        for code, marker in (("GBP", "£"), ("USD", "$"), ("EUR", "€")):
            if marker in value or re.search(r"\b" + code + r"\b", value, re.I):
                currencies.add(code)
    else:
        return None
    return next(iter(currencies)) if len(currencies) == 1 else None


def evidence_age_days(value):
    text = normalise(value)
    if not text:
        return None
    try:
        date = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        date = None
        for fmt in ("%d %b %Y", "%d %B %Y", "%d/%m/%Y", "%b %d, %Y", "%d %b %Y %H:%M"):
            try:
                date = datetime.strptime(text, fmt)
                break
            except ValueError:
                pass
        if date is None:
            return None
    if date.tzinfo is None:
        date = date.replace(tzinfo=timezone.utc)
    age = (utcnow() - date).total_seconds() / 86400
    return age if age >= 0 else None


def ordinary_laptop(title, condition=""):
    text = normalise(title) + " " + normalise(condition)
    if classify_faults(text)[0] != "NORMAL":
        return False
    # Aggregates, options, accessories and retail/refurbished offerings are not
    # directly comparable to one ordinary used laptop.
    return not re.search(
        r"\b(?:lot(?:\s+of)?\s*\d+|bundle|job\s*lot|\d+\s*[x×]\s*(?:laptops?|Dell|HP|Lenovo)|"
        r"[x×]\s*\d+|\d+\s+laptops?|choose|choice|various|refurbished|renewed|"
        r"brand new|sealed|warranty|charger only|screen only|keyboard only|"
        r"replacement|for Dell|for HP|for Lenovo|no ram|no memory)\b|"
        r"\b\d+\s*(?:GB|TB)?\s*(?:/|or)\s*\d+\s*(?:GB|TB)\b", text, re.I
    ) and not is_genuinely_new(condition)



def normalise_storage_gb(value):
    """Normalise equivalent advertised/usable capacities for valuation."""
    if value is None:
        return None

    try:
        value = int(round(float(value)))
    except (TypeError, ValueError):
        return None

    # Common 256 GB class representations.
    if 230 <= value <= 256:
        return 256

    # Common 512 GB class representations.
    if 460 <= value <= 512:
        return 512

    # Common 1 TB class representations.
    if 900 <= value <= 1024:
        return 1024

    # Common 2 TB class representations.
    if 1800 <= value <= 2048:
        return 2048

    # Common 4 TB class representations.
    if 3600 <= value <= 4096:
        return 4096

    return value


def _title_capacity_to_gb(number, unit="GB"):
    try:
        value = float(number)
    except (TypeError, ValueError):
        return None

    unit = normalise(unit).upper()

    if unit in ("TB", "T"):
        value *= 1024

    return int(round(value))



def title_spec_overrides(title):
    """
    Conservatively derive advertised RAM and storage from a listing title.

    Rules:
      * Explicit RAM labels always win.
      * DDR-labelled capacity is RAM.
      * Explicit SSD/NVMe/HDD/eMMC/storage labels identify storage.
      * Unlabelled '16GB 512GB' style titles are accepted only when exactly
        two capacity tokens exist and they form a plausible RAM/storage pair.
      * A storage value must never be reinterpreted as RAM.
    """
    text = normalise(title)

    result = {
        "ram_gb": None,
        "storage_gb": None,
    }

    if not text:
        return result

    plausible_ram = {
        1, 2, 3, 4, 6, 8, 12, 16,
        20, 24, 32, 40, 48, 64,
        96, 128, 192, 256
    }

    # --------------------------------------------------------
    # RAM: only strongly labelled evidence.
    # --------------------------------------------------------

    ram_patterns = [
        # 16GB RAM / 16 GB Memory
        r"\b(\d{1,3})\s*GB\s*(?:RAM|MEMORY)\b",

        # 16GB DDR4 RAM, 16GB DDR5
        r"\b(\d{1,3})\s*GB\s*"
        r"(?:DDR[345](?:L)?(?:-\d+)?)"
        r"(?:\s*(?:RAM|MEMORY))?\b",

        # RAM 16GB / Memory: 16 GB
        r"\b(?:RAM|MEMORY)\s*[:=\-]?\s*"
        r"(\d{1,3})\s*GB\b",
    ]

    for pattern in ram_patterns:
        match = re.search(
            pattern,
            text,
            re.I
        )

        if not match:
            continue

        value = int(
            match.group(1)
        )

        if value in plausible_ram:
            result["ram_gb"] = value
            break

    # --------------------------------------------------------
    # Storage: must have an explicit storage device/label.
    # --------------------------------------------------------

    storage_patterns = [
        # 512GB SSD / 1TB NVMe / 500GB HDD
        r"\b(\d+(?:\.\d+)?)\s*(TB|GB)\s*"
        r"(?:M\.?2\s*)?"
        r"(?:PCIE\s*)?"
        r"(?:NVME|SSD|HDD|EMMC|STORAGE|DRIVE)\b",

        # SSD 512GB / NVMe: 1TB
        r"\b(?:NVME|SSD|HDD|EMMC|STORAGE|DRIVE)\s*"
        r"[:=\-]?\s*"
        r"(\d+(?:\.\d+)?)\s*(TB|GB)\b",

        # 256SSD / 512NVMe shorthand
        r"\b(\d{2,4})\s*"
        r"(SSD|NVME|HDD|EMMC)\b",
    ]

    for index, pattern in enumerate(
        storage_patterns
    ):
        match = re.search(
            pattern,
            text,
            re.I
        )

        if not match:
            continue

        if index < 2:
            number = match.group(1)
            unit = match.group(2)
        else:
            number = match.group(1)
            unit = "GB"

        try:
            value = float(number)
        except (TypeError, ValueError):
            continue

        if str(unit).upper() == "TB":
            value *= 1024

        value = int(
            round(value)
        )

        if 32 <= value <= 16384:
            result["storage_gb"] = normalise_storage_gb(
                value
            )
            break

    # --------------------------------------------------------
    # Very conservative unlabelled pair fallback.
    #
    # Examples:
    #   MacBook Air M1 8GB 256GB
    #   Latitude 7350 ... 32GB 512GB Win 11
    #
    # Do NOT use this when RAM/storage were already explicit.
    # --------------------------------------------------------

    if (
        result["ram_gb"] is None
        or result["storage_gb"] is None
    ):
        capacities = []

        for match in re.finditer(
            r"\b(\d+(?:\.\d+)?)\s*(TB|GB)\b",
            text,
            re.I
        ):
            number = float(
                match.group(1)
            )

            unit = match.group(2).upper()

            if unit == "TB":
                number *= 1024

            capacities.append(
                int(round(number))
            )

        # Only infer from the very common:
        #
        #   RAM-capacity first, storage-capacity second
        #
        # and only when there are exactly two capacity tokens.
        if len(capacities) == 2:
            first, second = capacities

            second_normalised = normalise_storage_gb(
                second
            )

            if (
                first in plausible_ram
                and second_normalised is not None
                and second_normalised >= 64
                and second_normalised > first
            ):
                if result["ram_gb"] is None:
                    result["ram_gb"] = first

                if result["storage_gb"] is None:
                    result["storage_gb"] = second_normalised

    return result

def effective_ram_storage(row):
    """
    Prefer unambiguous title-advertised specifications over stored parser
    values, then normalise storage capacity for comparable matching.
    """
    inferred = title_spec_overrides(
        row_value(row, "title")
    )

    ram = inferred["ram_gb"]

    if ram is None:
        ram = row_value(
            row,
            "ram_gb"
        )

    storage = inferred["storage_gb"]

    if storage is None:
        storage = row_value(
            row,
            "storage_gb"
        )

    storage = normalise_storage_gb(
        storage
    )

    return ram, storage


def reconcile_item_specs_from_title(item):
    """Correct a newly analysed listing when its title is unambiguous."""
    if not item:
        return item

    inferred = title_spec_overrides(
        item.get("title")
    )

    if inferred["ram_gb"] is not None:
        item["ram_gb"] = inferred["ram_gb"]

    if inferred["storage_gb"] is not None:
        item["storage_gb"] = inferred["storage_gb"]
    elif item.get("storage_gb") is not None:
        item["storage_gb"] = normalise_storage_gb(
            item["storage_gb"]
        )

    return item




def exact_spec_identity(row):
    cpu = parse_cpu(
        row_value(row, "cpu"),
        "VALUATION"
    )

    confidence = row_value(
        row,
        "cpu_confidence",
        "EXACT"
    )

    ram, storage = effective_ram_storage(
        row
    )

    return bool(
        precise_model_for_valuation(
            row_value(row, "brand"),
            row_value(row, "model")
        )
        and cpu
        and cpu["confidence"] == "EXACT"
        and confidence == "EXACT"
        and ram
        and storage
    )

VALUATION_COSMETIC_REASONS = {
    "scratch",
    "scratches",
    "scratched",
    "dent",
    "dents",
    "dented",
    "grade b",
    "grade c",
}


def valuation_condition_requires_review(target):
    """
    Allow cosmetic-only condition notes through valuation, while
    continuing to block functional faults, missing components and
    high-risk listings.
    """
    if not ordinary_laptop(
        row_value(target, "title"),
        row_value(target, "condition")
    ):
        return True

    status = row_value(target, "status")

    if status in (None, "", "NORMAL"):
        return False

    if status != "MODERATE":
        return True

    raw_reasons = row_value(
        target,
        "fault_reasons"
    ) or ""

    try:
        reasons = json.loads(raw_reasons)
    except Exception:
        reasons = [
            part.strip()
            for part in re.split(
                r"[,;|]",
                str(raw_reasons)
            )
            if part.strip()
        ]

    if isinstance(reasons, str):
        reasons = [reasons]

    if not isinstance(reasons, (list, tuple)):
        return True

    reasons = {
        normalise(reason).lower()
        for reason in reasons
        if normalise(reason)
    }

    if not reasons:
        return True

    return not reasons.issubset(
        VALUATION_COSMETIC_REASONS
    )


def target_valuation_problem(target, conn=None):
    if row_value(target, "classifier_version") != CLASSIFIER_VERSION:
        return "REANALYSIS_REQUIRED"

    if int(row_value(target, "rules_revision") or 0) != current_rules_revision(conn):
        return "REANALYSIS_REQUIRED"

    if valuation_condition_requires_review(target):
        return "CONDITION_REQUIRES_REVIEW"

    if not exact_spec_identity(target):
        return "INCOMPLETE_IDENTITY_OR_SPEC"

    if not row_value(target, "total") or row_value(target, "postage") is None:
        return "UNKNOWN_DELIVERED_COST"

    return None


def insufficient_valuation(reason, count=0):
    return dict(estimated_value=None, q1=None, q3=None, count=count,
                confidence="INSUFFICIENT_DATA", undervaluation_gbp=None,
                undervaluation_pct=None, deal_score=None, basis=reason)



def advertised_variants_compatible(left, right):
    """
    Reject explicit contradictions, but do not interpret omission as the
    opposite specification.
    """
    a = advertised_variants(left)
    b = advertised_variants(right)

    exclusive_groups = (
        {"touch", "non_touch"},
        {"fhd", "uhd"},
        {"oled", "ips"},
        {"hdd", "emmc"},
    )

    for group in exclusive_groups:
        left_values = a & group
        right_values = b & group

        if (
            left_values
            and right_values
            and left_values != right_values
        ):
            return False

    def gpu_markers(values):
        return {
            value
            for value in values
            if re.match(
                r"^(?:rtx|gtx|rx|quadro)",
                value
            )
        }

    gpu_a = gpu_markers(a)
    gpu_b = gpu_markers(b)

    if (
        gpu_a
        and gpu_b
        and gpu_a != gpu_b
    ):
        return False

    refresh_a = {
        x for x in a
        if re.fullmatch(
            r"\d{2,3}hz",
            x
        )
    }

    refresh_b = {
        x for x in b
        if re.fullmatch(
            r"\d{2,3}hz",
            x
        )
    }

    if (
        refresh_a
        and refresh_b
        and refresh_a != refresh_b
    ):
        return False

    return True





def sold_spec_evidence_tier(target, comp):
    """
    Match sold evidence conservatively.

    Brand/model are already constrained by sold_candidates().
    CPU remains an exact hard requirement.

    RAM/storage:
      - exact RAM + exact storage: full evidence
      - exactly one differing: usable with reduced weight
      - both differing: reject
      - missing RAM/storage: reject

    Large configuration jumps are also rejected.
    """
    target_cpu = normalise(
        row_value(target, "cpu")
    ).lower()

    comp_cpu = normalise(
        row_value(comp, "cpu")
    ).lower()

    if not target_cpu or not comp_cpu:
        return "INCOMPATIBLE", 0.0

    if target_cpu != comp_cpu:
        return "INCOMPATIBLE", 0.0

    parsed_comp_cpu = parse_cpu(
        row_value(comp, "cpu"),
        "VALUATION"
    )

    if (
        not parsed_comp_cpu
        or parsed_comp_cpu.get("confidence") != "EXACT"
    ):
        return "INCOMPATIBLE", 0.0

    target_ram, target_storage = effective_ram_storage(
        target
    )

    comp_ram, comp_storage = effective_ram_storage(
        comp
    )

    if (
        target_ram is None
        or target_storage is None
        or comp_ram is None
        or comp_storage is None
    ):
        return "INCOMPATIBLE", 0.0

    ram_exact = target_ram == comp_ram
    storage_exact = target_storage == comp_storage

    if ram_exact and storage_exact:
        return "EXACT", 1.0

    # Do not combine evidence where both major configurable
    # specifications differ from the target.
    if not ram_exact and not storage_exact:
        return "INCOMPATIBLE", 0.0

    if not ram_exact:
        # Do not value a lower-RAM target from a better-equipped
        # sold machine. Lower-spec sold evidence is conservative.
        if comp_ram > target_ram:
            return "INCOMPATIBLE", 0.0

        ram_ratio = (
            target_ram
            / comp_ram
        )

        if ram_ratio > 2:
            return "INCOMPATIBLE", 0.0

        return "RAM_NEAR", 0.65

    # Same principle for storage: a sold machine with more storage
    # must not establish the value of a lower-storage target.
    if comp_storage > target_storage:
        return "INCOMPATIBLE", 0.0

    storage_ratio = (
        target_storage
        / comp_storage
    )

    if storage_ratio > 4:
        return "INCOMPATIBLE", 0.0

    return "STORAGE_NEAR", 0.75


def variant_evidence_tier(target, comp):
    """
    Rate the quality of optional advertised variant evidence.

    Core model/CPU/RAM/storage matching is handled separately by same_spec().

    EXACT:
        Both adverts explicitly describe the same optional variant markers.

    COMPATIBLE:
        There is no explicit contradiction, but one or both adverts omit
        optional variant information.

    INCOMPATIBLE:
        The adverts explicitly contradict one another.
    """

    if not advertised_variants_compatible(
        target,
        comp
    ):
        return "INCOMPATIBLE", 0.0

    target_variants = advertised_variants(
        target
    )

    comp_variants = advertised_variants(
        comp
    )

    if (
        target_variants
        and comp_variants
        and target_variants == comp_variants
    ):
        return "EXACT", 1.0

    return "COMPATIBLE", 0.75


def same_spec(target, comp):
    if not exact_spec_identity(comp):
        return False

    target_ram, target_storage = effective_ram_storage(
        target
    )

    comp_ram, comp_storage = effective_ram_storage(
        comp
    )

    return (
        normalise(
            row_value(target, "brand")
        ).lower()
        ==
        normalise(
            row_value(comp, "brand")
        ).lower()

        and normalise(
            valuation_model(
                row_value(target, "brand"),
                row_value(target, "model")
            )
        ).lower()
        ==
        normalise(
            valuation_model(
                row_value(comp, "brand"),
                row_value(comp, "model")
            )
        ).lower()

        and normalise(
            row_value(target, "cpu")
        ).lower()
        ==
        normalise(
            row_value(comp, "cpu")
        ).lower()

        and target_ram == comp_ram

        and target_storage == comp_storage

        and advertised_variants_compatible(
            target,
            comp
        )
    )

def advertised_variants(row):
    """
    Return explicitly advertised display/storage/GPU characteristics.

    Absence of a characteristic means UNKNOWN, not the opposite.
    """
    text = normalise(
        row_value(row, "title")
    ).lower()

    markers = set()

    patterns = {
        "fhd":
            r"\b(?:fhd|full\s*hd|1080p|1920\s*[x×]\s*1080)\b",

        "uhd":
            r"\b(?:uhd|4k|3840\s*[x×]\s*2160)\b",

        "oled":
            r"\boled\b",

        "ips":
            r"\bips\b",

        "hdd":
            r"\b(?:hdd|hard\s+disk)\b",

        "emmc":
            r"\bemmc\b",
    }

    for key, pattern in patterns.items():
        if re.search(
            pattern,
            text,
            re.I
        ):
            markers.add(key)

    if re.search(
        r"\bnon[- ]?touch(?:screen)?\b",
        text,
        re.I
    ):
        markers.add("non_touch")

    elif re.search(
        r"\b(?:touch|touchscreen)\b",
        text,
        re.I
    ):
        markers.add("touch")

    markers.update(
        re.sub(
            r"\s+",
            "",
            m.lower()
        )
        for m in re.findall(
            r"\b(?:"
            r"rtx\s*\d{4}(?:\s*ti)?|"
            r"gtx\s*\d{3,4}(?:\s*ti)?|"
            r"rx\s*\d{3,4}[a-z]*|"
            r"quadro\s*[a-z]?\d{3,4}|"
            r"\d{2,3}\s*hz"
            r")\b",
            text,
            re.I
        )
    )

    return markers

def select_sold_evidence(conn, target):
    selected = remove_price_outliers(
        sold_candidates(conn, target)
    )

    if len(selected) < MIN_COMPARABLES:
        return []

    # If relaxed RAM/storage evidence is used, anchor the valuation
    # with at least one exact RAM+storage sold comparable.
    uses_relaxed_spec = any(
        candidate.get("spec_tier") != "EXACT"
        for candidate in selected
    )

    if uses_relaxed_spec and not any(
        candidate.get("spec_tier") == "EXACT"
        for candidate in selected
    ):
        return []

    return selected



def canonical_sold_item_id(value):
    """
    Return a stable eBay item ID from Product Research evidence.

    Product Research may expose an item ID as a normal string, a mapping-like
    textual-display object, or its string representation. Prefer a plausible
    9-15 digit eBay ID. Preserve a non-empty original string only when no
    numeric ID can safely be recovered.
    """
    if value is None:
        return None

    if isinstance(value, dict):
        for key in ("value", "text", "itemId", "item_id"):
            candidate = value.get(key)
            if candidate is not None:
                result = canonical_sold_item_id(candidate)
                if result:
                    return result

    text = normalise(value)

    if not text:
        return None

    # Normal Browse IDs sometimes look like:
    # v1|298374124379|0
    browse_match = re.search(
        r"(?:^|\|)(\d{9,15})(?:\||$)",
        text
    )
    if browse_match:
        return browse_match.group(1)

    # Product Research TextualDisplayValue representations contain the
    # numeric item ID in their 'value' field.
    value_match = re.search(
        r"""['"]value['"]\s*:\s*['"](\d{9,15})['"]""",
        text
    )
    if value_match:
        return value_match.group(1)

    # Plain eBay item ID.
    if re.fullmatch(r"\d{9,15}", text):
        return text

    # Conservative final recovery: accept exactly one plausible numeric ID.
    numbers = re.findall(r"(?<!\d)(\d{9,15})(?!\d)", text)

    if len(set(numbers)) == 1:
        return numbers[0]

    return text

def sold_candidates(conn, target):
    if target_valuation_problem(target, conn):
        return []

    rows = conn.execute(
        "SELECT * FROM sold_comparables WHERE delivered_price > 0 "
        "AND currency='GBP' AND evidence_version=? AND LOWER(brand)=LOWER(?) "
        "AND LOWER(model)=LOWER(?)",
        (
            SOLD_EVIDENCE_VERSION,
            target["brand"],
            target["model"]
        )
    ).fetchall()

    best = {}
    fingerprints = set()

    target_item_id = canonical_sold_item_id(
        row_value(target, "item_id")
    )

    for row in sorted(
        rows,
        key=lambda r: r["collected_at"],
        reverse=True
    ):
        sold_item_id = canonical_sold_item_id(
            row["item_id"]
        )

        if (
            not sold_item_id
            or sold_item_id.startswith("title:")
        ):
            continue

        # Never use the target listing itself as sold evidence.
        if (
            target_item_id
            and sold_item_id == target_item_id
        ):
            continue

        age = evidence_age_days(
            row["last_sold"]
        )

        cache_age = evidence_age_days(
            row["collected_at"]
        )

        if (
            age is None
            or age > PRODUCT_RESEARCH_DAY_RANGE
            or cache_age is None
            or cache_age > SOLD_CACHE_MAX_AGE_DAYS
        ):
            continue

        if not ordinary_laptop(
            row["title"]
        ):
            continue

        spec_tier, spec_weight = (
            sold_spec_evidence_tier(
                target,
                row
            )
        )

        if spec_tier == "INCOMPATIBLE":
            continue

        if (
            row["avg_postage"] is None
            or row["avg_postage"] < 0
        ):
            continue

        price = float(
            row["delivered_price"]
        )

        if not math.isfinite(price):
            continue

        # Repeated identical adverts must not manufacture
        # independent evidence.
        fingerprint = normalise(
            row["title"]
        ).lower()

        if (
            sold_item_id in best
            or fingerprint in fingerprints
        ):
            continue

        tier, variant_weight = variant_evidence_tier(
            target,
            row
        )

        if tier == "INCOMPATIBLE":
            continue

        fingerprints.add(
            fingerprint
        )

        recency_weight = (
            2 ** (-age / 60.0)
        )

        best[sold_item_id] = dict(
            row=row,
            total=price,
            similarity=(
                100
                if (
                    spec_tier == "EXACT"
                    and tier == "EXACT"
                )
                else 85
                if spec_tier == "EXACT"
                else 70
            ),
            tier=tier,
            spec_tier=spec_tier,
            units=max(
                1,
                int(row["units_sold"] or 1)
            ),
            weight=(
                recency_weight
                * variant_weight
                * spec_weight
            )
        )

    return list(
        best.values()
    )


def weighted_percentile(candidates, p):
    if not candidates:
        return None
    pairs = sorted((c["total"], c["weight"]) for c in candidates)
    total_weight = sum(w for _, w in pairs)
    threshold = total_weight * p
    running = 0.0
    for value, weight in pairs:
        running += weight
        if running >= threshold:
            return value
    return pairs[-1][0]


def calculate_sold_valuation(conn, target):
    selected = select_sold_evidence(
        conn,
        target
    )

    if not selected:
        return None

    estimate = weighted_percentile(
        selected,
        .5
    )

    q1 = weighted_percentile(
        selected,
        .25
    )

    q3 = weighted_percentile(
        selected,
        .75
    )

    weights = [
        candidate["weight"]
        for candidate in selected
    ]

    weight_sum = sum(weights)

    weight_square_sum = sum(
        weight * weight
        for weight in weights
    )

    effective_n = (
        weight_sum ** 2
        / weight_square_sum
        if weight_square_sum
        else 0
    )

    spread = (
        (q3 - q1) / estimate
        if estimate
        else float("inf")
    )

    if (
        effective_n >= 8
        and spread <= 0.20
    ):
        confidence = "HIGH"

    elif (
        effective_n >= 4
        and spread <= 0.35
    ):
        confidence = "MEDIUM"

    else:
        confidence = "LOW"

    under = (
        estimate
        - target["total"]
    )

    pct = (
        under
        / estimate
        * 100
    )

    score = deal_score(
        conn,
        target,
        estimate,
        under,
        pct,
        confidence,
    )

    # Q1 is useful only when the evidence pool is large enough
    # for the lower quartile to carry meaningful information.
    if (
        score is not None
        and score > 0
        and q1 is not None
        and effective_n >= 4
        and target["total"] < q1
    ):
        bonus = (
            10
            * deal_confidence_multiplier(
                confidence
            )
        )

        score = round(
            min(
                100,
                score + bonus
            ),
            1
        )

    variant_exact_count = sum(
        candidate["tier"] == "EXACT"
        for candidate in selected
    )

    variant_compatible_count = (
        len(selected)
        - variant_exact_count
    )

    spec_counts = {
        "EXACT": 0,
        "RAM_NEAR": 0,
        "STORAGE_NEAR": 0,
    }

    for candidate in selected:
        spec_tier, _ = sold_spec_evidence_tier(
            target,
            candidate["row"]
        )

        if spec_tier in spec_counts:
            spec_counts[spec_tier] += 1

    return dict(
        estimated_value=round(
            estimate,
            2
        ),
        q1=q1,
        q3=q3,
        count=len(selected),
        confidence=confidence,
        undervaluation_gbp=round(
            under,
            2
        ),
        undervaluation_pct=round(
            pct,
            1
        ),
        deal_score=score,
        basis=(
            "SOLD_MODEL_SPEC_WEIGHTED:"
            f"{len(selected)}_ROWS/"
            f"{sum(c['units'] for c in selected)}_SALES/"
            f"SPEC_EXACT={spec_counts['EXACT']}/"
            f"SPEC_RAM_NEAR={spec_counts['RAM_NEAR']}/"
            f"SPEC_STORAGE_NEAR={spec_counts['STORAGE_NEAR']}/"
            f"VAR_EXACT={variant_exact_count}/"
            f"VAR_COMPAT={variant_compatible_count}/"
            f"NEFF={effective_n:.1f}"
        )
    )



def auction_listing(row):
    try:
        options = json.loads(
            row["buying_options"]
            or "[]"
        )
    except Exception:
        options = []

    return "AUCTION" in options


def fixed_price_listing(row):
    try:
        options = json.loads(
            row["buying_options"]
            or "[]"
        )
    except Exception:
        options = []

    return (
        "FIXED_PRICE" in options
        or "BEST_OFFER" in options
    )


def comparable_candidates(conn, target):
    if target_valuation_problem(target, conn):
        return []
    rows = conn.execute("""SELECT * FROM listings
        WHERE item_id != ? AND brand = ? AND LOWER(model) = LOWER(?)
          AND total > 0 AND postage IS NOT NULL AND active=1
          AND status='NORMAL' AND classifier_version=?""",
        (target["item_id"], target["brand"], target["model"], CLASSIFIER_VERSION)).fetchall()
    candidates, seen = [], set()
    for row in rows:
        age = evidence_age_days(row["last_seen"])
        end_age = evidence_age_days(row["end_date"]) if row["end_date"] else None
        if age is None or age > COMPARABLE_MAX_AGE_DAYS or end_age is not None:
            continue
        if not fixed_price_listing(row) or not same_spec(target, row):
            continue
        if not ordinary_laptop(row["title"], row["condition"]):
            continue
        fingerprint = normalise(row["title"]).lower()
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        candidates.append(dict(row=row, similarity=100, total=float(row["total"]), weight=1))
    return candidates

def remove_price_outliers(candidates):
    if len(candidates) < 4:
        return candidates
    logs = [math.log(c["total"]) for c in candidates]
    centre = statistics.median(logs)
    mad = statistics.median(abs(x-centre) for x in logs)
    # A zero-MAD pool still rejects a grossly different price; do not let an
    # aggregate's units determine the centre or silently restore rejected rows.
    tolerance = max(math.log(1.5), 3 * 1.4826 * mad)
    return [c for c in candidates if abs(math.log(c["total"])-centre) <= tolerance]

def calculate_active_valuation(conn, target):
    selected = remove_price_outliers(comparable_candidates(conn, target))
    if len(selected) < MIN_COMPARABLES:
        return insufficient_valuation("ACTIVE_ASKING_REFERENCE", len(selected))
    prices = [c["total"] for c in selected]
    return dict(estimated_value=round(statistics.median(prices), 2),
        q1=percentile(prices, .25), q3=percentile(prices, .75), count=len(selected),
        confidence="ASKING_PRICES_ONLY", undervaluation_gbp=None,
        undervaluation_pct=None, deal_score=None, basis="ACTIVE_ASKING_REFERENCE")


def deal_confidence_multiplier(confidence):
    return {
        "LOW": 0.75,
        "MEDIUM": 0.90,
        "HIGH": 1.00,
    }.get(confidence, 0.75)


def deal_score(
    conn,
    target,
    value,
    undervalue,
    undervalue_pct,
    confidence
):
    if (
        target_valuation_problem(target, conn)
        or not fixed_price_listing(target)
    ):
        return None

    if (
        value is None
        or undervalue is None
        or undervalue_pct is None
    ):
        return None

    if (
        undervalue < MIN_UNDERVALUE_GBP
        or undervalue_pct < MIN_UNDERVALUE_PCT
    ):
        return 0

    raw_score = (
        min(
            60,
            undervalue_pct * 1.2
        )
        +
        min(
            25,
            undervalue / 4
        )
    )

    score = (
        raw_score
        * deal_confidence_multiplier(
            confidence
        )
    )

    return round(
        min(100, score),
        1
    )


def sold_evidence_for_basis(conn, target, basis):
    if not basis or not basis.startswith("SOLD_"):
        return []
    return sorted(select_sold_evidence(conn, target), key=lambda c: c["total"])


def valuation_label(basis):
    if basis.startswith("SOLD_"):
        return "Sold prices: matching model, CPU, RAM and storage"
    if basis.startswith("ACTIVE_FALLBACK"):
        return "Active asking prices only"
    return {
        "REANALYSIS_REQUIRED": "Waiting for refreshed listing details",
        "INCOMPLETE_IDENTITY_OR_SPEC": "Model or specification needs confirmation",
        "CONDITION_REQUIRES_REVIEW": "Condition or completeness needs review",
        "UNKNOWN_DELIVERED_COST": "Delivery cost or GBP price is unconfirmed",
    }.get(basis, "Not enough comparable evidence")


def calculate_valuation(conn, target):
    problem = target_valuation_problem(target, conn)
    if problem:
        return insufficient_valuation(problem)
    sold = calculate_sold_valuation(conn, target)
    if sold is not None:
        return sold

    active = calculate_active_valuation(conn, target)
    if active is not None:
        basis = active.get("basis") or "ACTIVE_MARKET"
        active["basis"] = "ACTIVE_FALLBACK:" + basis
    return active


def revalue_all(conn):
    rows = conn.execute("""
        SELECT *
        FROM listings
    """).fetchall()

    for row in rows:

        valuation = calculate_valuation(
            conn,
            row
        )

        conn.execute("""
            UPDATE listings
            SET
                estimated_value=?,
                valuation_q1=?,
                valuation_q3=?,
                comparable_count=?,
                valuation_confidence=?,
                undervaluation_gbp=?,
                undervaluation_pct=?,
                deal_score=?,
                valuation_basis=?
            WHERE item_id=?
        """, (
            valuation[
                "estimated_value"
            ],
            valuation["q1"],
            valuation["q3"],
            valuation["count"],
            valuation[
                "confidence"
            ],
            valuation[
                "undervaluation_gbp"
            ],
            valuation[
                "undervaluation_pct"
            ],
            valuation[
                "deal_score"
            ],
            valuation[
                "basis"
            ],
            row["item_id"]
        ))

    conn.commit()


# ============================================================
# BACKFILL
# ============================================================

def needs_reanalysis(row):
    if (
        row["classifier_version"]
        != CLASSIFIER_VERSION
    ):
        return True

    if not row["brand"]:
        return True

    if not row["model"]:
        return True

    if not row["cpu"]:
        return True

    return False


def backfill_from_search_results(
    conn,
    token,
    summaries,
    maximum
):
    completed = 0

    for summary in summaries:

        if completed >= maximum:
            break

        item_id = summary.get(
            "itemId"
        )

        row = conn.execute("""
            SELECT *
            FROM listings
            WHERE item_id=?
        """, (
            item_id,
        )).fetchone()

        if not row:
            continue

        if not needs_reanalysis(
            row
        ):
            continue

        if not can_detail(
            conn
        ):
            break

        try:
            item = analyse_listing(
                conn,
                token,
                summary,
                fetch_detail=True
            )

        except EbayRateLimited as exc:
            print(
                "Backfill: eBay API cooldown; "
                f"{exc}"
            )
            break

        if item:

            save_listing(
                conn,
                item
            )

            completed += 1

    return completed


# ============================================================
# TERMINAL DISPLAY
# ============================================================

def display_listing(item):
    print()
    print("=" * 90)

    print(
        item["title"]
    )

    print(
        "Delivered: " +
        money(item["total"])
    )

    print(
        "Brand/model:",
        item["brand"] or "?",
        "/",
        item["model"] or "?"
    )

    print(
        "CPU:",
        item["cpu"] or "UNKNOWN",
        f"({item['cpu_confidence']})"
    )

    print(
        "RAM:",
        (
            f"{item['ram_gb']}GB"
            if item["ram_gb"]
            else "UNKNOWN"
        )
    )

    print(
        "Storage:",
        (
            f"{item['storage_gb']}GB"
            if item["storage_gb"]
            else "UNKNOWN"
        )
    )

    print(
        "Windows 11 approved:",
        yesno(
            item["win11"]
        )
    )

    print(
        yesno(
            item["usbc_pd"]
        )
    )

    print(
        "Fault class:",
        item["status"]
    )

    if item["fault_reasons"]:

        print(
            "Fault indicators:",
            ", ".join(
                item[
                    "fault_reasons"
                ]
            )
        )

    print(
        item["url"]
    )


# ============================================================
# DASHBOARD
# ============================================================

CSS = """
body {
    font-family: Arial, sans-serif;
    margin: 24px;
    background: #f5f5f5;
    color: #222;
}
h1 {
    margin-bottom: 4px;
}
.sub {
    color: #666;
    margin-bottom: 20px;
}
.cards {
    display: flex;
    gap: 12px;
    flex-wrap: wrap;
    margin-bottom: 20px;
}
.card {
    background: white;
    padding: 14px 18px;
    border-radius: 8px;
    min-width: 150px;
    box-shadow: 0 1px 3px rgba(0,0,0,.12);
}
.big {
    font-size: 24px;
    font-weight: bold;
}
table {
    width: 100%;
    border-collapse: collapse;
    background: white;
}
th, td {
    padding: 9px;
    border-bottom: 1px solid #ddd;
    text-align: left;
    vertical-align: top;
}
th {
    position: sticky;
    top: 0;
    background: #eee;
}
.money {
    white-space: nowrap;
}
.good {
    font-weight: bold;
}
.unknown {
    color: #888;
}
.evidence-row td {
    padding: 0 14px 14px 14px;
    background: #fafafa;
}
.evidence summary {
    cursor: pointer;
    font-weight: 600;
    padding: 8px 0;
}
.evidence-table {
    width: 100%;
    margin-top: 6px;
    font-size: 13px;
}
.evidence-table th,
.evidence-table td {
    padding: 6px 8px;
    vertical-align: top;
}
a {
    color: #0645ad;
}
.small {
    font-size: 12px;
    color: #666;
}
.session-ok {
    color: #137333;
    font-weight: bold;
}
.session-bad {
    color: #b3261e;
    font-weight: bold;
}
.session-unknown {
    color: #666;
    font-weight: bold;
}
"""



def auction_time_left(value):
    if not value:
        return "—"

    try:
        end = datetime.fromisoformat(
            str(value).replace("Z", "+00:00")
        )

        if end.tzinfo is None:
            end = end.replace(
                tzinfo=timezone.utc
            )

        seconds = int(
            (
                end.astimezone(timezone.utc)
                - utcnow()
            ).total_seconds()
        )

        if seconds <= 0:
            return "Ended"

        days, seconds = divmod(
            seconds,
            86400
        )

        hours, seconds = divmod(
            seconds,
            3600
        )

        minutes = seconds // 60

        if days:
            return f"{days}d {hours}h"

        if hours:
            return f"{hours}h {minutes}m"

        return f"{max(1, minutes)}m"

    except Exception:
        return "—"


def money(value):
    if value is None:
        return "—"

    return (
        f"£{float(value):,.2f}"
    )


def _dashboard_html_base():
    conn = connect_db()

    buy_now_rows = conn.execute("""
        SELECT *
        FROM listings
        WHERE COALESCE(active, 1) = 1
          AND estimated_value IS NOT NULL
          AND deal_score > 0
          AND undervaluation_gbp IS NOT NULL
          AND undervaluation_gbp >= ?
          AND undervaluation_pct IS NOT NULL
          AND undervaluation_pct >= ?
        ORDER BY
            deal_score DESC,
            undervaluation_gbp DESC,
            first_seen DESC
        LIMIT 500
    """, (
        MIN_UNDERVALUE_GBP,
        MIN_UNDERVALUE_PCT,
    )).fetchall()

    auction_rows = conn.execute("""
        SELECT *
        FROM listings
        WHERE COALESCE(active, 1) = 1
          AND estimated_value IS NOT NULL
          AND deal_score IS NULL
          AND buying_options LIKE '%"AUCTION"%'
          AND undervaluation_gbp IS NOT NULL
          AND undervaluation_gbp >= ?
          AND undervaluation_pct IS NOT NULL
          AND undervaluation_pct >= ?
        ORDER BY
            CASE
                WHEN end_date IS NULL THEN 1
                ELSE 0
            END,
            end_date ASC,
            undervaluation_gbp DESC
        LIMIT 500
    """, (
        MIN_UNDERVALUE_GBP,
        MIN_UNDERVALUE_PCT,
    )).fetchall()

    rows = list(buy_now_rows) + list(auction_rows)

    total = conn.execute("""
        SELECT COUNT(*) AS n
        FROM listings
        WHERE COALESCE(active, 1) = 1
    """).fetchone()["n"]

    valued = conn.execute("""
        SELECT COUNT(*) AS n
        FROM listings
        WHERE estimated_value IS NOT NULL
          AND COALESCE(active, 1) = 1
    """).fetchone()["n"]

    buy_now_candidates = len(buy_now_rows)
    auction_candidates = len(auction_rows)
    candidates = buy_now_candidates + auction_candidates

    api_calls = browse_usage_today(
        conn
    )


    # Exact pipeline state using the same eligibility rules as valuation.
    # This avoids calling thousands of merely identifiable listings
    # "awaiting valuation".
    pipeline_rows = conn.execute("""
        SELECT *
        FROM listings
        WHERE COALESCE(active, 1) = 1
    """).fetchall()

    ready_unvalued = 0
    reanalysis_backlog = 0
    incomplete_spec = 0
    condition_review = 0
    unknown_cost = 0

    for pipeline_row in pipeline_rows:

        if pipeline_row["estimated_value"] is not None:
            continue

        problem = target_valuation_problem(
            pipeline_row,
            conn
        )

        if problem is None:
            ready_unvalued += 1

        elif problem == "REANALYSIS_REQUIRED":
            reanalysis_backlog += 1

        elif problem == "INCOMPLETE_IDENTITY_OR_SPEC":
            incomplete_spec += 1

        elif problem == "CONDITION_REQUIRES_REVIEW":
            condition_review += 1

        elif problem == "UNKNOWN_DELIVERED_COST":
            unknown_cost += 1

    # Backward-compatible name for the existing dashboard card.
    valuation_backlog = reanalysis_backlog

    session_state = _read_json_file(PRODUCT_RESEARCH_SESSION_STATE)
    helper_state = _read_json_file(PRODUCT_RESEARCH_HELPER_STATE)
    session_status = session_state.get("status", "UNKNOWN")
    session_class = (
        "session-ok" if session_status == "WORKING"
        else "session-bad" if session_status == "NOT WORKING"
        else "session-unknown"
    )
    session_last_checked = relative_age(session_state.get("last_checked_at"))
    session_last_refresh = relative_age(session_state.get("last_refresh_at"))
    helper_status = helper_state.get("status", "UNKNOWN")
    helper_seen = relative_age(helper_state.get("last_seen_at"))

    buy_now_body_rows = []
    auction_body_rows = []

    for row in rows:

        title = html.escape(
            row["title"]
            or ""
        )

        url = html.escape(
            row["url"]
            or "#"
        )

        spec = []

        if row["cpu"]:
            spec.append(
                html.escape(
                    row["cpu"]
                )
            )

        if row["ram_gb"]:
            spec.append(
                f"{row['ram_gb']}GB RAM"
            )

        if row["storage_gb"]:
            spec.append(
                f"{row['storage_gb']}GB"
            )

        identity = " ".join(
            x for x in [
                row["brand"],
                row["model"],
            ]
            if x
        )

        if identity:
            spec.insert(0, identity)

        spec_text = " / ".join(
            spec
        ) or "Specifications incomplete"

        age_source = row["listed_at"] or row["first_seen"]
        age_text = relative_age(age_source)
        age_label = "eBay listed" if row["listed_at"] else "First seen"
        fresh = False
        try:
            age_dt = datetime.fromisoformat(str(age_source).replace("Z", "+00:00"))
            if age_dt.tzinfo is None:
                age_dt = age_dt.replace(tzinfo=timezone.utc)
            fresh = (utcnow() - age_dt.astimezone(timezone.utc)).total_seconds() < 900
        except Exception:
            pass
        age_html = (
            f"<strong>NEW · {html.escape(age_text)}</strong>"
            if fresh else html.escape(age_text)
        )

        if (
            row["undervaluation_gbp"]
            is not None
        ):

            under = (
                f"{money(row['undervaluation_gbp'])}"
                f"<br>"
                f"<span class='small'>"
                f"{row['undervaluation_pct']:.1f}%"
                f"</span>"
            )

        else:
            under = (
                "<span class='unknown'>"
                + ("Asking-price reference" if (row["valuation_basis"] or "").startswith("ACTIVE_FALLBACK")
                 else "Not scored")
                + "</span>"
            )

        is_auction = auction_listing(
            row
        )

        auction_badge = (
            "<br>"
            "<span style='"
            "display:inline-block;"
            "margin-top:4px;"
            "padding:2px 7px;"
            "border-radius:999px;"
            "background:#fff3cd;"
            "color:#8a5a00;"
            "font-size:11px;"
            "font-weight:700;"
            "letter-spacing:.02em;"
            "'>"
            "AUCTION · current bid"
            "</span>"
            if is_auction
            else ""
        )

        score = (
            f"{row['deal_score']:.0f}"
            if row["deal_score"]
            is not None
            else "—"
        )

        basis = row["valuation_basis"] or ""
        evidence = sold_evidence_for_basis(
            conn,
            row,
            basis
        )

        if evidence:
            evidence_rows = []
            for candidate in evidence[:25]:
                sold = candidate["row"]
                evidence_rows.append(
                    "<tr>"
                    f"<td>{html.escape(sold['title'] or '')}</td>"
                    f"<td class='money'>{money(sold['delivered_price'])}</td>"
                    f"<td class='money'>{money(sold['avg_sold_price'])}</td>"
                    f"<td class='money'>{money(sold['avg_postage'])}</td>"
                    f"<td>{int(sold['units_sold'] or 1)}</td>"
                    f"<td>{html.escape(sold['last_sold'] or '')}</td>"
                    f"<td>{candidate['similarity']}</td>"
                    f"<td>{html.escape(candidate['tier'])}</td>"
                    "</tr>"
                )

            evidence_html = (
                "<details class='evidence'>"
                "<summary>Show sold evidence</summary>"
                "<table class='evidence-table'>"
                "<thead><tr>"
                "<th>Sold listing</th><th>Delivered</th>"
                "<th>Avg sold</th><th>Postage</th>"
                "<th>Sales</th><th>Last sold</th>"
                "<th>Match</th><th>Tier</th>"
                "</tr></thead><tbody>"
                + "".join(evidence_rows)
                + "</tbody></table></details>"
            )
            sales_wording = (
                f"{sum(c['units'] for c in evidence)} sales "
                f"from {len(evidence)} sold listings"
            )
        else:
            evidence_html = ""
            count = row["comparable_count"] or 0
            if basis.startswith("ACTIVE_FALLBACK"):
                sales_wording = f"{count} active comparables"
            else:
                sales_wording = f"{count} sold comparables"

        sales_total = sum(
            int(c["units"] or 1)
            for c in evidence
        )

        def compact_mark(value):
            if value is True or value == 1:
                return "✓"
            if value is False or value == 0:
                return "✗"

            text = normalise(value).lower()

            if text in ("yes", "true", "approved", "supported"):
                return "✓"

            if text in ("no", "false", "unsupported", "not approved"):
                return "✗"

            return "?"

        win11_mark = compact_mark(row["win11"])
        usbc_mark = compact_mark(row["usbc_pd"])

        raw_fault_reasons = row["fault_reasons"] or ""

        try:
            fault_reasons = json.loads(raw_fault_reasons)
        except Exception:
            fault_reasons = [
                part.strip()
                for part in re.split(
                    r"[,;|]",
                    str(raw_fault_reasons)
                )
                if part.strip()
            ]

        if isinstance(fault_reasons, str):
            fault_reasons = [fault_reasons]

        if not isinstance(fault_reasons, (list, tuple)):
            fault_reasons = []

        clean_notes = []

        for reason in fault_reasons:
            reason = normalise(reason)

            if (
                reason
                and reason.lower()
                not in {
                    existing.lower()
                    for existing in clean_notes
                }
            ):
                clean_notes.append(reason)

        notes_html = "".join(
            "<span class='condition-note'>"
            + html.escape(reason.upper())
            + "</span>"
            for reason in clean_notes
        )

        if (
            "win11_state" in row.keys()
            and row["win11_state"] == WIN11_UNOFFICIAL_OK
        ):
            notes_html += (
                "<span class='win11-ok-note' "
                "title='Runs Windows 11 well, but the CPU is not officially supported by Microsoft.'>"
                "Win11-OK"
                "</span>"
            )

        hover_evidence_html = (
            evidence_html
            .replace("<details class='evidence'>", "<div class='evidence'>")
            .replace("<summary>Show sold evidence</summary>", "")
            .replace("</details>", "</div>")
        )

        image_url = row["image_url"] or ""

        if image_url:
            thumb_html = (
                f'<a href="{url}" target="_blank" '
                f'class="product-thumb-link">'
                f'<img src="{html.escape(image_url, quote=True)}" '
                f'class="product-thumb" '
                f'loading="lazy" '
                f'decoding="async" '
                f'alt="">'
                f'</a>'
            )
        else:
            thumb_html = (
                "<div class='product-thumb-placeholder' "
                "title='Image will appear after the listing is refreshed'>"
                "<span>⌁</span>"
                "</div>"
            )

        auction_time_html = (
            (
                html.escape(
                    auction_time_left(
                        row["end_date"]
                    )
                )
                + "<div class='small'>time left</div>"
            )
            if is_auction
            else ""
        )

        target_rows = (
            auction_body_rows
            if is_auction
            else buy_now_body_rows
        )

        cpu_benchmark = cpu_benchmark_for_cpu(
            conn,
            row["cpu"]
        )

        delivered_price = safe_float(
            row["total"]
        )

        if (
            cpu_benchmark
            and delivered_price is not None
            and delivered_price > 0
        ):
            cpu_mark = int(
                cpu_benchmark["cpu_mark"]
            )

            power_per_pound_value = (
                cpu_mark
                / delivered_price
            )

            benchmark_url = html.escape(
                cpu_benchmark["source_url"]
                or "#",
                quote=True
            )

            price_text = money(
                delivered_price
            )

            power_per_pound_html = (
                f'<a href="{benchmark_url}" '
                f'target="_blank" '
                f'rel="noopener noreferrer" '
                f'title="Power/£: {power_per_pound_value:.1f}&#10;'
                f'CPU Mark: {cpu_mark:,}&#10;'
                f'Delivered price: {price_text}&#10;'
                f'CPU Mark ÷ delivered price&#10;'
                f'Higher is better&#10;'
                f'Click for CPU benchmark details">'
                f'{power_per_pound_value:.1f}'
                f'</a>'
            )

        else:
            power_per_pound_value = -1
            power_per_pound_html = "—"

        target_rows.append(
            f"""
            <tr>
                <td class="product-thumb-cell">
                    {thumb_html}
                </td>

                <td>
                    <a href="{url}" target="_blank" class="listing-title">
                        {title}
                    </a>
                    <div class="small">
                        {html.escape(spec_text)}
                    </div>
                </td>

                <td class="power-per-pound"
                    data-sort="{power_per_pound_value}">
                    {power_per_pound_html}
                </td>

                <td>
                    {
                        auction_time_html
                        if is_auction
                        else age_html
                    }
                </td>

                <td class="money"
                    data-sort="{row['total'] if row['total'] is not None else -1}">
                    {money(row["total"])}
                    {
                        (
                            "<div class='small'>current bid</div>"
                        )
                        if is_auction
                        else ""
                    }
                </td>

                <td class="money good"
                    data-sort="{row['undervaluation_gbp'] if row['undervaluation_gbp'] is not None else -999999}">
                    <span class="valuation-hover" tabindex="0">
                        {under}
                        <span
                            class="valuation-info"
                            aria-label="View valuation evidence"
                            title="View valuation evidence"
                        >i</span>

                        <span class="valuation-tooltip">
                            <div class="valuation-summary">
                                <strong>Estimated value:</strong>
                                {money(row["estimated_value"])}

                                {
                                    (
                                        "<br>Q1: "
                                        + money(row["valuation_q1"])
                                        + " · Median: "
                                        + money(row["estimated_value"])
                                        + " · Q3: "
                                        + money(row["valuation_q3"])
                                    )
                                    if row["valuation_q1"] is not None
                                    else ""
                                }

                                <br>
                                {html.escape(sales_wording)}
                            </div>

                            {hover_evidence_html}
                        </span>
                    </span>
                </td>

                <td data-sort="{row['deal_score'] if row['deal_score'] is not None else -1}">
                    <span class="big"
                          title="Deal score: {score}/100&#10;Higher means a stronger deal">
                        {score}
                    </span>
                </td>

                <td class="notes-cell">
                    {
                        notes_html
                        or "<span class='notes-clear'>—</span>"
                    }
                </td>
            </tr>
            """
        )

    conn.close()

    auction_section_html = ""

    if auction_candidates > 0:
        auction_section_html = f"""
    <section class="deal-section">

        <div class="deal-section-header">
            <h2>Auctions</h2>
            <span class="small listing-count">
                {auction_candidates} listing{
                    "" if auction_candidates == 1 else "s"
                }
            </span>
        </div>

        <table class="auction-table">

        <thead>
        <tr>
            <th class="image-header" aria-label="Product image"></th>
            <th>Listing</th>
            <th title="PassMark CPU Mark points per £1 of delivered price. Higher is better.">Power/£</th>
            <th>Time left</th>
            <th>Current bid</th>
            <th title="Estimated saving if the current bid wins.">Potential saving</th>
            <th title="Deal score out of 100. Higher means a stronger deal.">Deal score</th>
            <th>Notes</th>
        </tr>
        </thead>

        <tbody>
            {''.join(auction_body_rows)}
        </tbody>

        </table>

    </section>
    """

    hero_cards = []

    for hero_index, hero_row in enumerate(buy_now_rows[:8]):
        hero_url_raw = normalise(
            hero_row["url"] or ""
        )

        if not hero_url_raw:
            continue

        hero_url = html.escape(
            hero_url_raw,
            quote=True
        )

        hero_title_raw = (
            hero_row["title"]
            or "Laptop deal"
        )

        hero_title = html.escape(
            hero_title_raw
        )

        hero_title_attr = html.escape(
            hero_title_raw,
            quote=True
        )

        hero_price = money(
            hero_row["total"]
        )

        hero_saving = money(
            hero_row["undervaluation_gbp"]
        )

        hero_estimate = money(
            hero_row["estimated_value"]
        )

        hero_pct = safe_float(
            hero_row["undervaluation_pct"]
        )

        hero_score = safe_float(
            hero_row["deal_score"]
        )

        hero_pct_text = (
            f"{hero_pct:.0f}% under market"
            if hero_pct is not None
            else "Below estimate"
        )

        hero_score_text = (
            f"{hero_score:.0f}"
            if hero_score is not None
            else "—"
        )

        hero_image_url = (
            hero_row["image_url"]
            or ""
        )

        if hero_image_url:
            loading = (
                "eager"
                if hero_index < 2
                else "lazy"
            )

            hero_image = (
                '<img src="'
                + html.escape(
                    hero_image_url,
                    quote=True
                )
                + f'" alt="" loading="{loading}">'
            )

        else:
            hero_image = """
                <svg viewBox="0 0 64 48"
                     aria-hidden="true">
                    <rect x="11" y="7"
                          width="42"
                          height="28"
                          rx="3"></rect>
                    <path d="M6 40h52"></path>
                </svg>
            """

        top_badge = (
            '<span class="hero-deal-top-badge">'
            'Top deal'
            '</span>'
            if hero_index == 0
            else ""
        )

        top_class = (
            " hero-deal-card-top"
            if hero_index == 0
            else ""
        )

        hero_cards.append(
            f"""
            <a class="hero-deal-card{top_class}"
               href="{hero_url}"
               target="_blank"
               rel="noopener noreferrer"
               aria-label="{hero_title_attr}. {hero_price}. Open eBay listing">

                <div class="hero-deal-card-inner">

                    <div class="hero-deal-image">
                        {hero_image}
                        {top_badge}
                    </div>

                    <div class="hero-deal-copy">

                        <div class="hero-deal-meta">
                            <span class="hero-deal-score">
                                Score {hero_score_text}
                            </span>

                            <span class="hero-deal-percent">
                                {hero_pct_text}
                            </span>
                        </div>

                        <div class="hero-deal-title">
                            {hero_title}
                        </div>

                        <div class="hero-deal-bottom">

                            <div class="hero-deal-pricing">
                                <span class="hero-deal-price">
                                    {hero_price}
                                </span>

                                <span class="hero-deal-estimate">
                                    Est. value {hero_estimate}
                                </span>
                            </div>

                            <span class="hero-deal-saving">
                                {hero_saving} under market
                            </span>

                            <span class="hero-deal-arrow"
                                  aria-hidden="true">
                                ↗
                            </span>

                        </div>

                    </div>

                </div>
            </a>
            """
        )

    hero_cards_html = "".join(
        hero_cards
    )

    hero_card_count = len(
        hero_cards
    )

    return f"""
    <!doctype html>
    <html>
    <head>
        <meta charset="utf-8">
        <meta http-equiv="refresh"
              content="60">
        <title>Laptop Lander</title>
        <style>{CSS}</style>
    </head>

    <body>



    <section class="home-hero">

        <div class="hero-copy">

            <div class="hero-brand">
                <a href="/"
           class="public-logo"
           aria-label="Laptop Lander home">
            <img
                src="data:image/webp;base64,UklGRrwJAABXRUJQVlA4ILAJAADQKwCdASqWAFgAPlUmkEUjoiGUSYYUOAVEswBqXgq9m80CuP1z8ZcciZvs0/U/cB7+P977Evzf7An6kdLD9pP8d7Bv5z/fv2o94v/L/sd7s/7n6g/8//tfWU+gf5bX7l/Cn/bP9j+7PtR//+9YfyniP5L/SPtnoGfR19N/SuPfgBfiv88/vX5gZNH/P8cfbXchZQE/SXoW/8/nX+nvRy/5xuHzJPy+20B59oFVxB1Di9V2YMPA4BwO/gdC14AJvHuDaspstprQJ2nIPF4DHf97zJFiAEaVoWpERIlbLX1KHssmdUzpFrGs1XnZktes03c9zm5ZvekMptjqdav4HuPczoxtgP5HnwCRAgNuCwoTeruw2X1/R1EFZiVGR7ZnexLyupyaTIiWhKz2qvB1DEwgME+Wu5NqjTcfci8P4UZDWGlouZ72WlWz80r2jbZ/iKS2MXUE2BNJwQoAZmzMNbLufCNfqoLRgAD+/WuRtSV/A9VYXKqHAGVTGEAKeo9xtB2u97jWIRC+HZRLnXmfry9K22ObXj6QGHOb4LjdKh79pXcAP8MyxtAsTkxj4P/HDPzywcJTbot9kPlW5CnJ825hfu6aSzCEhe/Gxk9TpCnIN0HJNjd+HfaHvreYPEtTB1HTJMjqCBnrgasEgzlKxn/ZvCzHAfP9k1xFv5qIjyOMZkkit3iFy8E5KqSUfULASNc+9orUuBy7FW4UZdFNKwusDGdQ0nggpEc+CA+KmxYrVv21a9mCoed6A8kAgnFABVJdxktThGYkkSc1uuv2Z5nN6JBkEch9RyPwbwnGJMo/now+Z/s+2lmqHXxX/BMjDJ4LVESkdFdEJ4qhBzn9d8GS92KG43rqA2y+joQh6hHBgls80IPMx9ROHR3LjOoxt7RFHsO5WFAUjzH3t4blsV31NATPccstvg2vBegDNnGRLVvh8C8P/5tZyfVUSZTKPi/cdZMpCGOXeTtFjSjfkSnY7p/KCle03whM2lYuLTC3jElqsWy2zfaukVS6ObVzHEl9fvdYWwUu0b42ddTHy/XDdQsOCBhD9M2tWRt8SwkdqdtKnPQZ6KfpUfRQ4tKocp3X2pH0uNNZLbDOybT/9QoiioO4cAAUOxxaQevjej7n0vH1IVk10m/sss+R+Na9K7A6XCLpviTZKm6mE3nfZ02S7eVdDLZXnfJfd9uOIe7BDfv9jn/TGEOqZGFXEz7nqe1McNWTClzOEPUlRw6R9cJhI54N0kU5POmMzNlq4jbkkprJPjW8cSghBhihVNuGkh5ZACJllIn24IyWPEHjzC9haJUi20nQD266T+ErD2crEjvBJ5oPzOAu86ohhonP748/m84lacH67cdgTAufNs1lZArF0Q0nAyMhNKpt22MlJ7Sph62jqmlKOolyH/apPJjs1nnrtjV968gYvMGZ+fTX8AnDmMHX6fvSCHq8GSPGV4JG/R9aLBOsER6zjJea9i1x1v6KmaQPcuhBJQoVAKat7WhB3Mj/cMIBDmAV4B5YxNwHeNzo/u5KzFcm/MIJoFjdf5RCW5z3pM6NIpRawOrPEcrAEzZ0tdPnb1HLFsIf63qThC7UCO/n8D80e4ZDQUyxOC+0sje2NVvKom8GPR2U0Q2GD0PbYnxiwp5/QIBMkXvPecdIifaoSO766TK54dGFco/O90LLCbZXySI0X3mpdES22iEEI9uChCY94VSWfe2bUrypv7TYJb6AujWF4G4GjnDwenttnSw89AAA3CaxC7B+GSqnMe8casFuFPqmwqjYr2wcHprItsQ0KseaW7fovGD9cvxZNad/ZDgHTSj1gvewv7qQTEHYD+w7jxEfEWsqS21QU/S8BjWMjUfEQUtWbF/t9jqWuxw5fPlogYTQ/Xi9XS4lO2AT0hAqtPfMb7wI17eHue0VNyKb4EqNr+UkVDzsABJd8eotIP0NN847gv7h/KDfTad5PqCU0cHIH7rYJ4oal2XRa7fF5FmaoAweqJi/Nw7/Of89dT2Xkt+D36T1TWbqbdeo3MP4/dlteLm2aXHz+QK0RG/FFfHrL063VidORUh74uv/pOi8uhRSp5tNpMDafz9pqu3qJDUM/Fmh48AlfAYC7RAJy4AN47ODGyWkg3S2UA7hznXOO7MXlq+dnzsCILyBFAdWHkpakJipYHjH0m1L9VTzyjtCgTbnUycAFd1SvmOIjzACKet8H/410RdD5Yj09Y2xlMQbVte1CMuAMRCjw6wBfdRM6b0ZD8t0BeCzpfd7Txt7EIahCm5zbGFtv4kbXAl0eX60H50FS+EY5t9rREZIhioOrvq69u/cyjQ11PmAh8YHXmFj+4g+448+jBpeX5zDm1DZL1J2vFSGVWWXuXCyal9uR/TlbfcVl6922LOSTUCVZXsYxTEkiv0skMWbFRNGc/qUHCm+7IvnM9FtI0zCLr/zvBx1Xbh+7PlW0W18y7vuKv1BeiuP+0X9LeDOGx8pDIxmy3nqKPA6Uh7RUG5xcSTltYUZfNFqRljx0xaY3c9jKxOiI3sgeWw7e3pGSYi1CCGP0EeV08fHvZKZGOcfP92WzSwmg8hm934QTuD7p0FdHGzwUUXDPwgglnQYVh6Wy6LUl1aUaIf6NRH3w0Xz/94v8xy5Kn6tD1SLpeI7VAIUfGL37D7B/fJoWM84WJYa4a9jylkfk5wc+LSn4m/mbEnrL6ovfWAF4D/O8NB0HfAPX9nmVS8HWHaR+6rkg3poYE6HMp9WIfvGII+a62ie4aFT17yEcpHVljKIFu8mkod5WGc2WjzL+bnPi9Sq7HbKDPDjRecWTPl/kpYfXR6c0jNzprzRcvVs3pic5/Fw22inUPHDJ9DV0ehSmVNQJhkzyhZB5W9F3m7h8Zqn043Oqa2F6/XVghR+TOqlwQ+ufOQmx42kqqCPpVAnUMg1M9kqlTUul1Lh29kBxF6DL1EGcbNfBIis6328U4jNVg9LSLFAQ07ZS5W5mOneWI1NuEE9oyAKS+uQPPuTxh7KCjlJ3z+MDncoUOmDqDKtXbLhCAO54H3GxYFHQ4/5/p7+Xkt1JHZ0NotVNjuq9hLcGvb5EpfyO6fBCwjSgqxS5FvtH+2KuwMzemirhGuGSLw1RQ2vg/YWwDWBxBZar+jojOwHebu2Wie3DC8VaWvvIJFqlB0R4eEY5prXEuzbP39z6Y+sv9QJ8tDMhNQYQBzAtknvu+de25EFpB9tyoCbCK4mHYkmdwTbDAHX5pGo5WZ8V7407Fg15EC8IYeFS4W+/E2L2VK6kkWzRBjkbWDZVi2rM1huUK/8JI1DKaeB/01ipaUPogAAAA=="
                alt=""
                class="public-logo-image">
        </a>

                <h1>Laptop Lander</h1>
            </div>

            <p class="hero-subtitle">
                Find underpriced laptops on eBay UK.
            </p>

            <div class="hero-benefits">

                <div class="hero-benefit">
                    <svg class="hero-benefit-icon"
                         viewBox="0 0 64 64"
                         aria-hidden="true"
                         focusable="false">
                        <circle cx="32" cy="32" r="29"
                                fill="#eef7ff"/>
                        <circle cx="32" cy="32" r="28.5"
                                fill="none"
                                stroke="#dbeafe"
                                stroke-width="1"/>
                        <circle cx="27.5" cy="27.5" r="11.5"
                                fill="none"
                                stroke="#1769e0"
                                stroke-width="5"
                                stroke-linecap="round"/>
                        <path d="M36 36 L47 47"
                              fill="none"
                              stroke="#1769e0"
                              stroke-width="5"
                              stroke-linecap="round"/>
                    </svg>

                    <span>
                        Scours all new listings
                    </span>
                </div>

                <div class="hero-benefit">
                    <svg class="hero-benefit-icon"
                         viewBox="0 0 64 64"
                         aria-hidden="true"
                         focusable="false">
                        <circle cx="32" cy="32" r="29"
                                fill="#eef7ff"/>
                        <circle cx="32" cy="32" r="28.5"
                                fill="none"
                                stroke="#dbeafe"
                                stroke-width="1"/>
                        <rect x="16.5" y="36" width="7.5" height="12"
                              rx="2.75" fill="#1769e0"/>
                        <rect x="28.25" y="27" width="7.5" height="21"
                              rx="2.75" fill="#1769e0"/>
                        <rect x="40" y="17" width="7.5" height="31"
                              rx="2.75" fill="#1769e0"/>
                    </svg>

                    <span>
                        Compares previous sold prices
                    </span>
                </div>

                <div class="hero-benefit">
                    <svg class="hero-benefit-icon"
                         viewBox="0 0 64 64"
                         aria-hidden="true"
                         focusable="false">
                        <circle cx="32" cy="32" r="29"
                                fill="#eef7ff"/>
                        <circle cx="32" cy="32" r="28.5"
                                fill="none"
                                stroke="#dbeafe"
                                stroke-width="1"/>
                        <path d="M35.5 13.5L19.2 35.1C18.5 36 19.1 37.4 20.3 37.4H29.8L27.5 49.5C27.2 51.1 29.3 51.9 30.3 50.6L46.7 28.9C47.4 28 46.7 26.6 45.6 26.6H36.1L38.4 14.6C38.7 12.9 36.5 12.1 35.5 13.5Z"
                              fill="#1769e0"/>
                    </svg>

                    <span>
                        Finds underpriced laptops
                    </span>
                </div>

            </div>
        </div>

        <div class="hero-deals"
             data-hero-carousel
             role="region"
             aria-roledescription="carousel"
             aria-label="Current standout laptop deals">

            <div class="hero-deal-carousel-head">

                <div class="hero-deal-carousel-live">
                    <span class="hero-live-dot"
                          aria-hidden="true"></span>
                    Live standout deals
                </div>

                <div class="hero-deal-carousel-controls">

                    <span class="hero-deal-carousel-count">
                        1 / {hero_card_count}
                    </span>

                    <button type="button"
                            class="hero-deal-prev"
                            aria-label="Previous deal">
                        ‹
                    </button>

                    <button type="button"
                            class="hero-deal-next"
                            aria-label="Next deal">
                        ›
                    </button>

                </div>

            </div>

            <div class="hero-deal-viewport">

                <div class="hero-deal-track">
                    {hero_cards_html}
                </div>

            </div>

        </div>
    </section>

    <style>
        /* ======================================================
           Laptop Lander public hero — authoritative styles
           ====================================================== */

        .public-topbar {{
            display: flex;
            align-items: center;
            height: 48px;
            margin: 0 0 12px;
        }}

        .public-logo {{
            display: inline-flex;
            align-items: center;
            justify-content: center;
            width: 62px;
            height: 44px;
            overflow: hidden;
            border-radius: 12px;
            background: #fff;
            box-shadow:
                0 5px 15px
                rgba(30, 48, 90, .08);
        }}

        .public-logo-image {{
            display: block;
            width: 58px;
            height: auto;
        }}

        .home-hero {{
            position: relative;
            display: grid;
            grid-template-columns:
                minmax(0, 1.08fr)
                minmax(360px, .92fr);
            align-items: center;
            gap: 44px;

            min-height: 0;
            padding: 38px 48px;
            margin: 0 0 26px;

            overflow: hidden;

            border:
                1px solid
                rgba(70, 100, 175, .11);
            border-radius: 24px;

            background:
                radial-gradient(
                    circle at 88% 18%,
                    rgba(112, 143, 255, .16),
                    transparent 30%
                ),
                radial-gradient(
                    circle at 77% 110%,
                    rgba(140, 180, 255, .12),
                    transparent 35%
                ),
                linear-gradient(
                    135deg,
                    #fafcff 0%,
                    #f3f6fc 56%,
                    #edf2fc 100%
                );

            box-shadow:
                0 16px 42px
                rgba(28, 48, 90, .075);
        }}

        .home-hero::before {{
            content: "";
            position: absolute;
            right: -170px;
            bottom: -300px;

            width: 410px;
            height: 410px;

            border:
                52px solid
                rgba(80, 130, 255, .045);
            border-radius: 50%;

            pointer-events: none;
        }}

        .hero-copy {{
            position: relative;
            z-index: 2;
        }}

        .hero-copy h1 {{
            margin: 0;

            color: #16213f;

            font-size:
                clamp(44px, 4vw, 62px);
            line-height: .98;

            font-weight: 850;
            letter-spacing: -.045em;
        }}

        .hero-subtitle {{
            margin: 17px 0 0;

            color: #586d8e;

            font-size:
                clamp(18px, 1.6vw, 23px);
            line-height: 1.35;

            font-weight: 500;
        }}

        .hero-benefits {{
            display: grid;
            grid-template-columns:
                repeat(3, minmax(0, 1fr));

            gap: 24px;
            margin-top: 29px;
            max-width: 650px;
        }}

        .hero-benefit {{
            min-width: 0;
            max-width: 170px;

            color: #25385f;

            font-size: 13px;
            line-height: 1.3;
            font-weight: 750;
        }}

        .hero-benefit-icon {{
            display: block;

            width: 64px;
            height: 64px;

            margin: 0 0 9px;
            overflow: visible;

            filter:
                drop-shadow(
                    0 7px 14px
                    rgba(37, 99, 235, .10)
                );
        }}

        /* ----------------------------
           Featured live deal cards
           ---------------------------- */

        .hero-deals {{
            position: relative;
            z-index: 2;

            width: 100%;
            min-height: 220px;
        }}

        .hero-deal-card {{
            position: absolute;

            width: min(90%, 445px);

            border:
                1px solid
                rgba(60, 82, 140, .08);
            border-radius: 18px;

            background:
                rgba(255, 255, 255, .97);

            box-shadow:
                0 14px 30px
                rgba(28, 48, 90, .10);
        }}

        .hero-deal-card-inner {{
            display: grid;
            grid-template-columns: 92px 1fr;
            align-items: center;
            gap: 15px;

            padding: 15px 17px;
        }}

        .hero-deal-image {{
            display: flex;
            align-items: center;
            justify-content: center;

            width: 92px;
            height: 72px;

            overflow: hidden;

            border-radius: 12px;
            background: #f4f6fa;
        }}

        .hero-deal-image img {{
            width: 100%;
            height: 100%;
            object-fit: contain;
        }}

        .hero-deal-image svg {{
            width: 58px;
            height: 43px;

            fill: none;
            stroke: #8a98b1;
            stroke-width: 2.2;
        }}

        .hero-deal-copy {{
            min-width: 0;
        }}

        .hero-deal-title {{
            display: -webkit-box;
            overflow: hidden;

            -webkit-box-orient: vertical;
            -webkit-line-clamp: 2;

            color: #172343;

            font-size: 14px;
            line-height: 1.28;
            font-weight: 800;

            white-space: normal;
        }}

        .hero-deal-price {{
            margin-top: 7px;

            color: #14203d;

            font-size: 23px;
            line-height: 1.05;
            font-weight: 850;

            font-variant-numeric:
                tabular-nums;
        }}

        .hero-deal-saving {{
            margin-top: 4px;

            color: #15803d;

            font-size: 13px;
            font-weight: 800;
        }}

        /* One deal: compact featured card, not a giant banner. */

        .hero-deals-1 {{
            display: flex;
            align-items: center;
            justify-content: center;

            min-height: 210px;
        }}

        .hero-deals-1
        .hero-deal-card {{
            position: relative;

            width: 100%;
            max-width: 430px;

            margin: 0;
        }}

        .hero-deals-1
        .hero-deal-card-inner {{
            grid-template-columns: 105px 1fr;
            gap: 17px;

            min-height: 120px;
            padding: 17px 19px;
        }}

        .hero-deals-1
        .hero-deal-image {{
            width: 105px;
            height: 82px;
        }}

        .hero-deals-1
        .hero-deal-title {{
            font-size: 14px;
        }}

        .hero-deals-1
        .hero-deal-price {{
            font-size: 24px;
        }}

        /* Two deals */

        .hero-deals-2
        .hero-deal-card:nth-child(1) {{
            top: 12px;
            left: 0;
            z-index: 1;

            transform: rotate(-.5deg);
        }}

        .hero-deals-2
        .hero-deal-card:nth-child(2) {{
            top: 104px;
            right: 0;
            z-index: 2;

            transform: rotate(.4deg);
        }}

        /* Three deals */

        .hero-deals-3
        .hero-deal-card:nth-child(1) {{
            top: 0;
            left: 0;
            z-index: 1;

            transform: rotate(-.5deg);
        }}

        .hero-deals-3
        .hero-deal-card:nth-child(2) {{
            top: 74px;
            right: 0;
            z-index: 2;

            transform: rotate(.35deg);
        }}

        .hero-deals-3
        .hero-deal-card:nth-child(3) {{
            top: 148px;
            left: 26px;
            z-index: 3;

            transform: rotate(-.2deg);
        }}

        @media (max-width: 1000px) {{
            .home-hero {{
                grid-template-columns: 1fr;

                gap: 28px;
                padding: 32px 28px;
            }}

            .hero-deals,
            .hero-deals-1 {{
                min-height: auto;
            }}

            .hero-deals-1
            .hero-deal-card {{
                margin: 0;
            }}
        }}

        @media (max-width: 680px) {{
            .public-topbar {{
                height: 44px;
                margin-bottom: 8px;
            }}

            .public-logo {{
                width: 58px;
                height: 40px;
            }}

            .public-logo-image {{
                width: 54px;
            }}

            .home-hero {{
                padding: 25px 19px;

                border-radius: 18px;
            }}

            .hero-copy h1 {{
                font-size: 40px;
            }}

            .hero-subtitle {{
                margin-top: 13px;

                font-size: 18px;
            }}

            .hero-benefits {{
                grid-template-columns: 1fr;

                gap: 11px;
                margin-top: 23px;
            }}

            .hero-benefit-icon {{
                flex: 0 0 54px;

                width: 54px;
                height: 54px;

                margin: 0;
            }}

            .hero-deals,
            .hero-deals-1,
            .hero-deals-2,
            .hero-deals-3 {{
                display: block;

                min-height: auto;
            }}

            .hero-deal-card,
            .hero-deals-1
            .hero-deal-card,
            .hero-deals-2
            .hero-deal-card:nth-child(n),
            .hero-deals-3
            .hero-deal-card:nth-child(n) {{
                position: relative;

                top: auto;
                right: auto;
                left: auto;

                width: 100%;
                max-width: none;

                margin: 0 0 12px;

                transform: none;
            }}

            .hero-deal-card-inner,
            .hero-deals-1
            .hero-deal-card-inner {{
                grid-template-columns: 82px 1fr;

                min-height: auto;

                padding: 13px 14px;
            }}

            .hero-deal-image,
            .hero-deals-1
            .hero-deal-image {{
                width: 82px;
                height: 64px;
            }}
        }}

        .deal-section {{
            margin-top: 28px;
        }}

        .deal-section-header {{
            display: flex;
            align-items: baseline;
            gap: 10px;
            margin: 0 0 10px 0;
        }}

        .deal-section-header h2 {{
            margin: 0;
            font-size: 20px;
        }}

        .auction-table th:nth-child(7),
        .auction-table td:nth-child(7) {{
            display: none;
        }}

        /* ------------------------------------------------------
           Deal table layout / alignment
           ------------------------------------------------------ */

        .deal-section table {{
            width: 100%;
            table-layout: fixed;
            border-collapse: separate;
            border-spacing: 0;
        }}

        .deal-section th {{
            vertical-align: middle;
            background: rgba(15, 23, 42, 0.028);
            color: #344054;
            font-size: 12px;
            font-weight: 750;
            letter-spacing: 0.025em;
        }}

        /* Image */
        .deal-section th:nth-child(1),
        .deal-section td:nth-child(1) {{
            width: 7%;
            text-align: center;
        }}

        /* Listing */
        .deal-section th:nth-child(2),
        .deal-section td:nth-child(2) {{
            width: 35%;
            text-align: left;
        }}

        /* CPU Rating */
        .deal-section th:nth-child(3),
        .deal-section td:nth-child(3) {{
            width: 9%;
            text-align: right;
            white-space: nowrap;
        }}

        /* Listing age / time left */
        .deal-section th:nth-child(4),
        .deal-section td:nth-child(4) {{
            width: 9%;
            text-align: left;
            white-space: nowrap;
        }}

        /* Price / current bid */
        .deal-section th:nth-child(5),
        .deal-section td:nth-child(5) {{
            width: 9%;
            text-align: right;
            white-space: nowrap;
        }}

        /* Saving / potential undervaluation */
        .deal-section th:nth-child(6),
        .deal-section td:nth-child(6) {{
            width: 15%;
            text-align: left;
        }}

        /* Score */
        .deal-section th:nth-child(7),
        .deal-section td:nth-child(7) {{
            width: 7%;
            text-align: center;
            white-space: nowrap;
        }}

        /* Notes */
        .deal-section th:nth-child(8),
        .deal-section td:nth-child(8) {{
            width: 9%;
            text-align: left;
        }}

        /* Selective separators between major information groups. */
        .deal-section th:nth-child(2),
        .deal-section td:nth-child(2),

        .deal-section th:nth-child(3),
        .deal-section td:nth-child(3),

        .deal-section th:nth-child(5),
        .deal-section td:nth-child(5),

        .deal-section th:nth-child(6),
        .deal-section td:nth-child(6),

        .deal-section th:nth-child(7),
        .deal-section td:nth-child(7) {{
            border-right: 1px solid rgba(15, 23, 42, 0.05);
        }}

        .power-per-pound {{
            font-variant-numeric: tabular-nums;
            font-weight: 800;
        }}

        .power-per-pound a {{
            color: #176b3a;
            text-decoration: none;
        }}

        .power-per-pound a:hover {{
            color: #15803d;
            text-decoration: underline;
        }}


        /* Gentle hover makes wide rows easier to track visually. */
        .deal-section tbody tr {{
            transition: background-color 120ms ease;
        }}

        .deal-section tbody tr:hover td {{
            background-color: rgba(37, 99, 235, 0.04);
        }}


        /* --------------------------------------------------
           Final public dashboard polish
           -------------------------------------------------- */

        .brand-header {{
            margin-bottom: 14px;
        }}

        .deal-section {{
            margin-top: 20px;
        }}

        .deal-section th {{
            text-transform: none;
            letter-spacing: 0.015em;
            font-size: 12px;
            line-height: 1.25;
        }}

        .listing-count {{
            display: inline-flex;
            align-items: center;
            padding: 3px 8px;
            border-radius: 999px;
            background: rgba(15, 23, 42, 0.045);
            color: #667085;
            font-size: 11px;
            line-height: 1.25;
            white-space: nowrap;
        }}

        .listing-title {{
            display: -webkit-box;
            -webkit-line-clamp: 2;
            -webkit-box-orient: vertical;
            overflow: hidden;
        }}

        .power-per-pound a {{
            cursor: pointer;
        }}

        .notes-clear {{
            color: #b0b8c4;
            font-weight: 400;
        }}

        .empty-state {{
            padding: 22px 18px;
            border: 1px solid rgba(15, 23, 42, 0.08);
            border-radius: 14px;
            background: rgba(255, 255, 255, 0.58);
            color: #667085;
            text-align: center;
            font-size: 13px;
            box-shadow: 0 5px 18px rgba(16, 24, 40, 0.04);
        }}

        /* Numeric information should visually line up. */
        .deal-section th:nth-child(3),
        .deal-section td:nth-child(3),
        .deal-section th:nth-child(5),
        .deal-section td:nth-child(5) {{
            font-variant-numeric: tabular-nums;
        }}

        /* Power/£ shows PassMark CPU performance relative to delivered price. */
        .power-per-pound a {{
            color: #176b3a;
            font-weight: 800;
        }}

        .power-per-pound a:hover {{
            color: #15803d;
        }}

        /* Keep the green saving treatment as the main visual emphasis. */
        .deal-section td.good {{
            font-weight: 700;
        }}

        @media (max-width: 1050px) {{
            /* Notes is the first low-priority column to disappear. */
            .deal-section th:nth-child(8),
            .deal-section td:nth-child(8) {{
                display: none;
            }}
        }}

        @media (max-width: 850px) {{
            /* Then hide deal score. */
            .deal-section th:nth-child(7),
            .deal-section td:nth-child(7) {{
                display: none;
            }}
        }}

        @media (max-width: 700px) {{
            /* Then age/time-left, leaving the buying essentials. */
            .deal-section th:nth-child(4),
            .deal-section td:nth-child(4) {{
                display: none;
            }}

            .deal-section th:nth-child(1),
            .deal-section td:nth-child(1) {{
                width: 16%;
            }}

            .deal-section th:nth-child(2),
            .deal-section td:nth-child(2) {{
                width: 38%;
            }}

            .deal-section th:nth-child(3),
            .deal-section td:nth-child(3) {{
                width: 15%;
            }}

            .deal-section th:nth-child(5),
            .deal-section td:nth-child(5) {{
                width: 13%;
            }}

            .deal-section th:nth-child(6),
            .deal-section td:nth-child(6) {{
                width: 18%;
            }}
        }}

        /* Final column alignment:
           text left, numeric values right, score centred. */
        .deal-section th:nth-child(2),
        .deal-section td:nth-child(2) {{
            text-align: left;
        }}

        .deal-section th:nth-child(3),
        .deal-section td:nth-child(3),
        .deal-section th:nth-child(4),
        .deal-section td:nth-child(4),
        .deal-section th:nth-child(5),
        .deal-section td:nth-child(5),
        .deal-section th:nth-child(6),
        .deal-section td:nth-child(6) {{
            text-align: right;
        }}

        .deal-section th:nth-child(7),
        .deal-section td:nth-child(7) {{
            text-align: center;
        }}

        .deal-section th:nth-child(8),
        .deal-section td:nth-child(8) {{
            text-align: left;
        }}

        /* Keep rounded table ends visually clean. */
        .deal-section th:first-child {{
            border-top-left-radius: 8px;
        }}

        .deal-section th:last-child {{
            border-top-right-radius: 8px;
        }}

        .hero-brand {{
            display: flex;
            align-items: center;
            gap: 18px;
        }}

        .hero-brand .public-logo {{
            display: flex;
            align-items: center;
            justify-content: center;
            flex: 0 0 auto;
            width: 96px;
            height: 72px;
            overflow: visible;
            border-radius: 0;
            background: transparent;
            box-shadow: none;
        }}

        .hero-brand .public-logo-image {{
            display: block;
            width: 92px;
            height: auto;
        }}

        .hero-copy h1 {{
            margin: 0;
        }}

        .hero-deals {{
            position: relative;
            display: grid;
            grid-template-columns: 1fr;
            align-content: center;
            gap: 12px;
            width: 100%;
            max-width: 400px;
            min-height: 0;
            justify-self: end;
        }}

        .hero-deal-card {{
            position: relative !important;
            inset: auto !important;
            width: 100% !important;
            max-width: none !important;
            transform: none !important;
            border-radius: 15px;
            box-shadow:
                0 9px 24px
                rgba(28, 48, 90, .09);
        }}

        .hero-deal-card:nth-child(n+3) {{
            display: none !important;
        }}

        .hero-deal-card-inner {{
            grid-template-columns:
                72px minmax(0, 1fr);
            gap: 12px;
            padding: 11px 13px;
        }}

        .hero-deal-image {{
            width: 72px;
            height: 56px;
            border-radius: 9px;
        }}

        .hero-deal-card a,
        .hero-deal-card-title {{
            display: -webkit-box;
            overflow: hidden;
            -webkit-box-orient: vertical;
            -webkit-line-clamp: 2;
        }}

        @media (max-width: 980px) {{
            .home-hero {{
                grid-template-columns: 1fr;
                gap: 26px;
            }}

            .hero-deals {{
                max-width: none;
                justify-self: stretch;
            }}
        }}

        @media (max-width: 620px) {{
            .home-hero {{
                padding: 26px 22px;
            }}

            .hero-brand {{
                gap: 12px;
            }}

            .hero-brand .public-logo {{
                width: 72px;
                height: 54px;
            }}

            .hero-brand .public-logo-image {{
                width: 70px;
            }}

            .hero-benefits {{
                grid-template-columns: 1fr;
                gap: 14px;
            }}
        }}
        /* ======================================================
           Laptop Lander scrolling hero deals
           ====================================================== */

        .hero-deals[data-hero-carousel] {{
            position: relative;
            display: block;
            width: 100%;
            max-width: 520px;
            min-height: 0;
            justify-self: end;
            z-index: 2;
        }}

        .hero-deal-carousel-head {{
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 12px;
            margin-bottom: 10px;
        }}

        .hero-deal-carousel-live {{
            display: flex;
            align-items: center;
            gap: 8px;

            color: #26395f;

            font-size: 13px;
            font-weight: 850;
        }}

        .hero-live-dot {{
            width: 9px;
            height: 9px;
            flex: 0 0 9px;

            border-radius: 50%;

            background: #22c55e;

            box-shadow:
                0 0 0 4px
                rgba(34, 197, 94, .12);
        }}

        .hero-deal-carousel-controls {{
            display: flex;
            align-items: center;
            gap: 6px;
        }}

        .hero-deal-carousel-count {{
            margin-right: 3px;

            color: #7b879b;

            font-size: 10px;
            font-weight: 750;

            font-variant-numeric:
                tabular-nums;
        }}

        .hero-deal-prev,
        .hero-deal-next {{
            display: grid;
            place-items: center;

            width: 31px;
            height: 31px;
            padding: 0;

            border:
                1px solid
                rgba(58, 83, 145, .13);
            border-radius: 10px;

            background:
                rgba(255, 255, 255, .92);

            color: #29426f;

            box-shadow:
                0 5px 14px
                rgba(28, 48, 90, .07);

            cursor: pointer;

            font: inherit;
            font-size: 22px;
            line-height: 1;

            transition:
                transform 150ms ease,
                border-color 150ms ease,
                box-shadow 150ms ease;
        }}

        .hero-deal-prev:hover,
        .hero-deal-next:hover {{
            transform: translateY(-1px);

            border-color:
                rgba(37, 99, 235, .28);

            box-shadow:
                0 7px 18px
                rgba(28, 48, 90, .11);
        }}

        .hero-deal-viewport {{
            height: 304px;

            overflow: hidden;

            border-radius: 20px;

            scroll-behavior: smooth;
        }}

        .hero-deal-track {{
            display: flex;
            flex-direction: column;

            gap: 12px;

            width: 100%;
        }}

        .hero-deal-track
        .hero-deal-card,
        .hero-deal-track
        .hero-deal-card:nth-child(n) {{
            position: relative !important;

            inset: auto !important;

            display: block !important;

            flex: 0 0 146px;

            width: 100% !important;
            max-width: none !important;
            height: 146px;

            margin: 0 !important;

            overflow: hidden;

            border:
                1px solid
                rgba(72, 99, 166, .13);

            border-radius: 19px;

            background:
                radial-gradient(
                    circle at 100% 0%,
                    rgba(78, 119, 255, .11),
                    transparent 34%
                ),
                linear-gradient(
                    145deg,
                    #ffffff,
                    #f7faff
                );

            box-shadow:
                0 13px 29px
                rgba(28, 48, 90, .10);

            color: inherit;
            text-decoration: none;

            transform: none !important;

            scroll-snap-align: start;

            transition:
                transform 180ms ease,
                border-color 180ms ease,
                box-shadow 180ms ease;
        }}

        .hero-deal-track
        .hero-deal-card:hover {{
            transform:
                translateY(-3px) !important;

            border-color:
                rgba(37, 99, 235, .28);

            box-shadow:
                0 18px 38px
                rgba(28, 48, 90, .15);
        }}

        .hero-deal-track
        .hero-deal-card-top {{
            border-color:
                rgba(37, 99, 235, .25);
        }}

        .hero-deal-track
        .hero-deal-card-inner {{
            display: grid;

            grid-template-columns:
                116px minmax(0, 1fr);

            align-items: center;

            gap: 15px;

            height: 100%;

            box-sizing: border-box;

            padding: 14px;
        }}

        .hero-deal-track
        .hero-deal-image {{
            position: relative;

            display: grid;
            place-items: center;

            width: 116px;
            height: 116px;

            overflow: hidden;

            border:
                1px solid
                rgba(15, 23, 42, .06);

            border-radius: 14px;

            background: #f2f5fa;
        }}

        .hero-deal-track
        .hero-deal-image img {{
            width: 100%;
            height: 100%;

            object-fit: cover;

            transition:
                transform 200ms ease;
        }}

        .hero-deal-track
        .hero-deal-card:hover
        .hero-deal-image img {{
            transform: scale(1.035);
        }}

        .hero-deal-track
        .hero-deal-image svg {{
            width: 70px;
            height: 52px;

            fill: none;
            stroke: #8290aa;
            stroke-width: 2.1;
        }}

        .hero-deal-top-badge {{
            position: absolute;

            top: 7px;
            left: 7px;

            padding: 4px 7px;

            border-radius: 999px;

            background:
                rgba(17, 35, 72, .92);

            color: #fff;

            font-size: 9px;
            font-weight: 900;
            letter-spacing: .05em;

            text-transform: uppercase;
        }}

        .hero-deal-track
        .hero-deal-copy {{
            display: flex;
            flex-direction: column;

            min-width: 0;
            height: 100%;
        }}

        .hero-deal-meta {{
            display: flex;
            align-items: center;
            gap: 6px;

            margin-bottom: 6px;
        }}

        .hero-deal-score,
        .hero-deal-percent {{
            padding: 5px 7px;

            border-radius: 999px;

            white-space: nowrap;

            font-size: 10px;
            line-height: 1;
            font-weight: 850;
        }}

        .hero-deal-score {{
            background: #edf3ff;
            color: #315ba9;
        }}

        .hero-deal-percent {{
            background: #ebf8ef;
            color: #17743c;
        }}

        .hero-deal-track
        .hero-deal-title {{
            display: -webkit-box;

            overflow: hidden;

            -webkit-box-orient: vertical;
            -webkit-line-clamp: 2;

            color: #16213f;

            font-size: 14px;
            line-height: 1.24;
            font-weight: 850;

            letter-spacing: -.01em;
        }}

        .hero-deal-bottom {{
            display: grid;
            grid-template-columns:
                minmax(0, 1fr) auto;
            grid-template-areas:
                "pricing saving";
            align-items: end;

            gap: 8px 12px;

            min-width: 0;

            margin-top: auto;
        }}

        .hero-deal-pricing {{
            grid-area: pricing;

            display: grid;
            grid-template-columns: 1fr;

            gap: 3px;

            min-width: 0;
        }}

        .hero-deal-track
        .hero-deal-price {{
            margin: 0;

            color: #111c39;

            font-size: 23px;
            line-height: 1;
            font-weight: 900;

            letter-spacing: -.025em;

            white-space: nowrap;
        }}

        .hero-deal-estimate {{
            min-width: 0;
            overflow: hidden;

            color: #8a96aa;

            font-size: 9px;
            font-weight: 700;

            white-space: nowrap;
            text-overflow: ellipsis;
        }}

        .hero-deal-track
        .hero-deal-saving {{
            grid-area: saving;
            justify-self: end;

            max-width: 100%;
            margin: 0;
            padding: 5px 7px;

            overflow: hidden;

            border-radius: 999px;

            background: #e8f8ee;

            color: #14803c;

            font-size: 10px;
            line-height: 1;
            font-weight: 900;

            white-space: nowrap;
            text-overflow: ellipsis;
        }}

        .hero-deal-arrow {{
            display: grid;
            place-items: center;

            flex: 0 0 28px;

            width: 28px;
            height: 28px;

            border-radius: 9px;

            background: #2563eb;

            color: #fff;

            box-shadow:
                0 6px 13px
                rgba(37, 99, 235, .22);

            font-size: 15px;
            font-weight: 900;
        }}

        .hero-deal-card:focus-visible,
        .hero-deal-prev:focus-visible,
        .hero-deal-next:focus-visible {{
            outline:
                3px solid
                rgba(37, 99, 235, .28);

            outline-offset: 3px;
        }}

        @media (max-width: 980px) {{
            .hero-deals[data-hero-carousel] {{
                max-width: none;
                justify-self: stretch;
            }}
        }}

        @media (max-width: 680px) {{

            .hero-deal-viewport {{
                height: auto;

                margin-right: -19px;
                padding-right: 19px;

                overflow-x: auto;
                overflow-y: hidden;

                scroll-snap-type:
                    x mandatory;

                scrollbar-width: none;

                -webkit-overflow-scrolling:
                    touch;
            }}

            .hero-deal-viewport::-webkit-scrollbar {{
                display: none;
            }}

            .hero-deal-track {{
                flex-direction: row;

                width: max-content;
            }}

            .hero-deal-track
            .hero-deal-card {{
                flex:
                    0 0 min(84vw, 370px);

                width:
                    min(84vw, 370px) !important;

                height: 164px;
            }}

            .hero-deal-track
            .hero-deal-card-inner {{
                grid-template-columns:
                    96px minmax(0, 1fr);

                gap: 12px;

                padding: 13px;
            }}

            .hero-deal-track
            .hero-deal-image {{
                width: 96px;
                height: 136px;
            }}

            .hero-deal-track
            .hero-deal-title {{
                font-size: 13px;
            }}

            .hero-deal-bottom {{
                grid-template-columns: 1fr;
                grid-template-areas:
                    "pricing"
                    "saving";
            }}

            .hero-deal-pricing {{
                display: grid;
            }}

            .hero-deal-track
            .hero-deal-price,
            .hero-deal-estimate {{
                display: block;
            }}

            .hero-deal-estimate {{
                margin-top: 0;
            }}

            .hero-deal-track
            .hero-deal-saving {{
                justify-self: start;
            }}
        }}

        @media (max-width: 420px) {{

            .hero-deal-percent,
            .hero-deal-estimate {{
                display: none;
            }}

            .hero-deal-track
            .hero-deal-card {{
                flex-basis: 86vw;

                width: 86vw !important;
            }}
        }}

        @media (prefers-reduced-motion: reduce) {{

            .hero-deal-viewport {{
                scroll-behavior: auto;
            }}

            .hero-deal-card,
            .hero-deal-image img {{
                transition: none;
            }}
        }}
</style>

    <section class="deal-section">

        <div class="deal-section-header">
            <h2>Buy It Now deals</h2>
            <span class="small listing-count">
                {buy_now_candidates} listing{
                    "" if buy_now_candidates == 1 else "s"
                }
            </span>
        </div>

        <table>

        <thead>
        <tr>
            <th class="image-header" aria-label="Product image"></th>
            <th>Listing</th>
            <th title="PassMark CPU Mark points per £1 of delivered price. Higher is better.">Power/£</th>
            <th>Listing age</th>
            <th>Price</th>
            <th title="Difference between the listing price and estimated market value based on recent sold prices.">
    Under market
</th>
            <th title="Deal score out of 100. Higher means a stronger deal.">Deal score</th>
            <th>Notes</th>
        </tr>
        </thead>

        <tbody>
            {''.join(buy_now_body_rows)}
        </tbody>

        </table>

    </section>


    {auction_section_html}
    <script>
    (() => {{
        const root = document.querySelector(
            "[data-hero-carousel]"
        );

        if (!root) return;

        const viewport = root.querySelector(
            ".hero-deal-viewport"
        );

        const cards = Array.from(
            root.querySelectorAll(
                ".hero-deal-card"
            )
        );

        const count = root.querySelector(
            ".hero-deal-carousel-count"
        );

        const prev = root.querySelector(
            ".hero-deal-prev"
        );

        const next = root.querySelector(
            ".hero-deal-next"
        );

        if (!cards.length) {{
            root.hidden = true;
            return;
        }}

        const mobile = window.matchMedia(
            "(max-width: 680px)"
        );

        const reduced = window.matchMedia(
            "(prefers-reduced-motion: reduce)"
        ).matches;

        let index = 0;
        let timer = null;
        let paused = false;
        let scrollTimer = null;

        const visible = () =>
            mobile.matches ? 1 : 2;

        const maxIndex = () =>
            Math.max(
                0,
                cards.length - visible()
            );

        function schedule() {{
            if (
                reduced
                || paused
                || cards.length <= visible()
            ) {{
                return;
            }}

            clearTimeout(timer);

            timer = setTimeout(
                () => go(index + 1),
                4300
            );
        }}

        function go(target) {{
            clearTimeout(timer);

            const max = maxIndex();

            index = (
                target > max
                ? 0
                : target < 0
                ? max
                : target
            );

            const card = cards[index];

            const behavior = (
                reduced
                ? "auto"
                : "smooth"
            );

            viewport.scrollTo(
                mobile.matches
                ? {{
                    left: card.offsetLeft,
                    top: 0,
                    behavior
                }}
                : {{
                    left: 0,
                    top: card.offsetTop,
                    behavior
                }}
            );

            count.textContent =
                `${{index + 1}} / ${{cards.length}}`;

            schedule();
        }}

        function pause(value) {{
            paused = value;

            clearTimeout(timer);

            if (!paused) {{
                schedule();
            }}
        }}

        prev.addEventListener(
            "click",
            () => go(index - 1)
        );

        next.addEventListener(
            "click",
            () => go(index + 1)
        );

        root.addEventListener(
            "mouseenter",
            () => pause(true)
        );

        root.addEventListener(
            "mouseleave",
            () => pause(false)
        );

        root.addEventListener(
            "focusin",
            () => pause(true)
        );

        root.addEventListener(
            "focusout",
            event => {{
                if (
                    !root.contains(
                        event.relatedTarget
                    )
                ) {{
                    pause(false);
                }}
            }}
        );

        viewport.addEventListener(
            "pointerdown",
            () => pause(true),
            {{ passive: true }}
        );

        viewport.addEventListener(
            "pointerup",
            () => {{
                setTimeout(
                    () => pause(false),
                    700
                );
            }},
            {{ passive: true }}
        );

        viewport.addEventListener(
            "scroll",
            () => {{
                if (!mobile.matches) return;

                clearTimeout(scrollTimer);

                scrollTimer = setTimeout(
                    () => {{
                        let nearest = 0;
                        let distance = Infinity;

                        cards.forEach(
                            (card, i) => {{
                                const d = Math.abs(
                                    card.offsetLeft
                                    - viewport.scrollLeft
                                );

                                if (d < distance) {{
                                    nearest = i;
                                    distance = d;
                                }}
                            }}
                        );

                        index = nearest;

                        count.textContent =
                            `${{index + 1}} / ${{cards.length}}`;
                    }},
                    80
                );
            }},
            {{ passive: true }}
        );

        mobile.addEventListener(
            "change",
            () => {{
                index = Math.min(
                    index,
                    maxIndex()
                );

                requestAnimationFrame(
                    () => go(index)
                );
            }}
        );

        document.addEventListener(
            "visibilitychange",
            () => pause(document.hidden)
        );

        if (cards.length <= visible()) {{
            root.querySelector(
                ".hero-deal-carousel-controls"
            ).style.display = "none";
        }}

        schedule();
    }})();
    </script>
    </body>
    </html>
    """



def _site_nav(active="deals"):
    links = (
        ("deals", "/", "Deals"),
        ("diagnostics", "/diagnostics", "Diagnostics"),
        ("settings", "/settings", "Settings"),
    )
    items = []
    for name, href, label in links:
        cls = " active" if name == active else ""
        items.append(
            f'<a class="nav-link{cls}" href="{href}">{label}</a>'
        )
    return (
        '<nav class="site-nav">'
        + "".join(items)
        + '</nav>'
    )


def diagnostics_html():
    conn = connect_db()
    refresh_runtime_settings(conn)
    refresh_classifier_rules(conn)

    def scalar(sql, params=()):
        row = conn.execute(sql, params).fetchone()

        if not row:
            return 0

        value = row[0]

        return 0 if value is None else value

    def safe_scalar(sql, params=(), default=0):
        try:
            return scalar(sql, params)
        except Exception:
            return default

    def safe_rows(sql, params=()):
        try:
            return conn.execute(
                sql,
                params
            ).fetchall()
        except Exception:
            return []

    def stat_card(label, value, note=""):
        return (
            '<div class="stat-card">'
            f'<div class="stat-label">{html.escape(str(label))}</div>'
            f'<div class="stat-value">{html.escape(str(value))}</div>'
            + (
                f'<div class="stat-note">{html.escape(str(note))}</div>'
                if note
                else ""
            )
            + '</div>'
        )

    def table_rows(items):
        if not items:
            return (
                "<tr>"
                "<td colspan='2' class='muted'>No data</td>"
                "</tr>"
            )

        output = []

        for key, value in items:
            output.append(
                "<tr>"
                f"<td>{html.escape(str(key))}</td>"
                f"<td class='number'>{html.escape(str(value))}</td>"
                "</tr>"
            )

        return "".join(output)

    def queue_card(
        title,
        remaining,
        progress_pct,
        eta,
        detail="",
        state="Working"
    ):
        progress_pct = max(
            0.0,
            min(
                100.0,
                float(progress_pct)
            )
        )

        state_class = {
            "WORKING": "good",
            "COMPLETE": "good",
            "PAUSED": "warn",
            "BLOCKED": "bad",
            "WAITING": "warn",
        }.get(
            str(state).upper(),
            ""
        )

        detail_html = (
            '<div class="queue-detail">'
            + html.escape(str(detail))
            + '</div>'
            if detail
            else ''
        )

        return f"""
        <div class="queue-card">

            <div class="queue-head">

                <div>
                    <div class="queue-title">
                        {html.escape(str(title))}
                    </div>

                    <div class="queue-state {state_class}">
                        {html.escape(str(state))}
                    </div>
                </div>

                <div class="queue-count">
                    {int(remaining):,} remaining
                </div>

            </div>

            <div class="queue-progress">
                <div style="width:{progress_pct:.1f}%"></div>
            </div>

            <div class="queue-meta">
                <span>
                    {progress_pct:.1f}% complete
                </span>

                <span>
                    ETA: {html.escape(str(eta))}
                </span>
            </div>

            {detail_html}

        </div>
        """


    total_all = safe_scalar(
        "SELECT COUNT(*) FROM listings"
    )

    active_total = safe_scalar(
        """
        SELECT COUNT(*)
        FROM listings
        WHERE COALESCE(active,1)=1
        """
    )

    inactive_total = safe_scalar(
        """
        SELECT COUNT(*)
        FROM listings
        WHERE COALESCE(active,1)=0
        """
    )

    valued = safe_scalar(
        """
        SELECT COUNT(*)
        FROM listings
        WHERE COALESCE(active,1)=1
          AND estimated_value IS NOT NULL
        """
    )

    unvalued = max(
        0,
        active_total - valued
    )

    scored = safe_scalar(
        """
        SELECT COUNT(*)
        FROM listings
        WHERE COALESCE(active,1)=1
          AND deal_score > 0
        """
    )

    qualifying = safe_scalar(
        """
        SELECT COUNT(*)
        FROM listings
        WHERE COALESCE(active,1)=1
          AND estimated_value IS NOT NULL
          AND undervaluation_gbp >= ?
          AND undervaluation_pct >= ?
        """,
        (
            MIN_UNDERVALUE_GBP,
            MIN_UNDERVALUE_PCT,
        )
    )

    auctions = safe_scalar(
        """
        SELECT COUNT(*)
        FROM listings
        WHERE COALESCE(active,1)=1
          AND buying_options LIKE '%"AUCTION"%'
        """
    )

    fixed_price = safe_scalar(
        """
        SELECT COUNT(*)
        FROM listings
        WHERE COALESCE(active,1)=1
          AND (
              buying_options LIKE '%"FIXED_PRICE"%'
              OR buying_options LIKE '%"BUY_IT_NOW"%'
              OR buying_options LIKE '%"BEST_OFFER"%'
          )
        """
    )

    reanalysis = safe_scalar(
        """
        SELECT COUNT(*)
        FROM listings
        WHERE COALESCE(active,1)=1
          AND valuation_basis='REANALYSIS_REQUIRED'
        """
    )

    incomplete = safe_scalar(
        """
        SELECT COUNT(*)
        FROM listings
        WHERE COALESCE(active,1)=1
          AND valuation_basis='INCOMPLETE_IDENTITY_OR_SPEC'
        """
    )

    condition_review = safe_scalar(
        """
        SELECT COUNT(*)
        FROM listings
        WHERE COALESCE(active,1)=1
          AND valuation_basis='CONDITION_REQUIRES_REVIEW'
        """
    )

    active_fallback = safe_scalar(
        """
        SELECT COUNT(*)
        FROM listings
        WHERE COALESCE(active,1)=1
          AND valuation_basis LIKE 'ACTIVE_FALLBACK:%'
        """
    )

    unknown_cost = safe_scalar(
        """
        SELECT COUNT(*)
        FROM listings
        WHERE COALESCE(active,1)=1
          AND valuation_basis='UNKNOWN_DELIVERED_COST'
        """
    )

    missing_brand = safe_scalar(
        """
        SELECT COUNT(*)
        FROM listings
        WHERE COALESCE(active,1)=1
          AND valuation_basis='INCOMPLETE_IDENTITY_OR_SPEC'
          AND (brand IS NULL OR trim(brand)='')
        """
    )

    missing_model = safe_scalar(
        """
        SELECT COUNT(*)
        FROM listings
        WHERE COALESCE(active,1)=1
          AND valuation_basis='INCOMPLETE_IDENTITY_OR_SPEC'
          AND (model IS NULL OR trim(model)='')
        """
    )

    missing_cpu = safe_scalar(
        """
        SELECT COUNT(*)
        FROM listings
        WHERE COALESCE(active,1)=1
          AND valuation_basis='INCOMPLETE_IDENTITY_OR_SPEC'
          AND (cpu IS NULL OR trim(cpu)='')
        """
    )

    missing_ram = safe_scalar(
        """
        SELECT COUNT(*)
        FROM listings
        WHERE COALESCE(active,1)=1
          AND valuation_basis='INCOMPLETE_IDENTITY_OR_SPEC'
          AND ram_gb IS NULL
        """
    )

    missing_storage = safe_scalar(
        """
        SELECT COUNT(*)
        FROM listings
        WHERE COALESCE(active,1)=1
          AND valuation_basis='INCOMPLETE_IDENTITY_OR_SPEC'
          AND storage_gb IS NULL
        """
    )

    api_budget = browse_budget_status(conn)

    api_used = int(
        api_budget["used"]
    )

    api_remaining = max(
        0,
        DAILY_SAFETY_LIMIT - api_used
    )

    normal_budget_remaining = max(
        0,
        DAILY_SAFETY_LIMIT
        - EMERGENCY_RESERVE
        - api_used
    )

    ebay_quota_limit = api_budget.get(
        "limit"
    )

    ebay_quota_remaining = (
        max(
            0,
            int(ebay_quota_limit)
            - api_used
        )
        if ebay_quota_limit
        is not None
        else None
    )

    ebay_reset = api_budget.get(
        "reset"
    )

    ebay_reset_seconds = (
        api_budget.get(
            "reset_seconds"
        )
    )

    if ebay_reset is not None:
        ebay_reset_text = (
            ebay_reset.strftime(
                "%Y-%m-%d %H:%M:%S UTC"
            )
        )

        if (
            ebay_reset_seconds
            is not None
        ):
            ebay_reset_text += (
                " (in "
                + _format_quota_countdown(
                    ebay_reset_seconds
                )
                + ")"
            )
    else:
        ebay_reset_text = (
            "Unavailable — local UTC "
            "fallback active"
        )

    image_backfill_used = operation_usage(
        conn,
        "IMAGE_BACKFILL"
    )

    search_calls = operation_usage(
        conn,
        "SEARCH"
    )

    get_item_calls = operation_usage(
        conn,
        "GET_ITEM"
    )

    try:
        cooldown = ebay_rate_limit_remaining()
    except Exception:
        cooldown = 0

    # --------------------------------------------------------
    # ACTIVE QUEUES / PROGRESS
    # --------------------------------------------------------

    current_classifier_count = safe_scalar(
        """
        SELECT COUNT(*)
        FROM listings
        WHERE COALESCE(active,1)=1
          AND classifier_version=?
          AND COALESCE(rules_revision,0)=?
        """,
        (
            CLASSIFIER_VERSION,
            current_rules_revision(conn),
        )
    )

    classifier_queue_remaining = safe_scalar(
        """
        SELECT COUNT(*)
        FROM listings
        WHERE COALESCE(active,1)=1
          AND (
                classifier_version IS NULL
                OR classifier_version<>?
                OR COALESCE(rules_revision,0)<>?
              )
        """,
        (
            CLASSIFIER_VERSION,
            current_rules_revision(conn),
        )
    )

    classifier_queue_total = (
        current_classifier_count
        + classifier_queue_remaining
    )

    classifier_progress_pct = (
        (
            current_classifier_count
            / classifier_queue_total
        ) * 100.0
        if classifier_queue_total
        else 100.0
    )

    classifier_capacity_per_hour = (
        MAX_BACKFILL_DETAILS_PER_CYCLE
        * 3600.0
        / POLL_NORMAL
        if POLL_NORMAL
        else 0.0
    )

    if classifier_queue_remaining <= 0:
        classifier_queue_state = "Complete"
        classifier_eta = "Complete"

    elif cooldown > 0:
        classifier_queue_state = "Paused"

        minutes, seconds = divmod(
            int(cooldown),
            60
        )

        classifier_eta = (
            f"429 cooldown: "
            f"{minutes}m {seconds:02d}s"
        )

    elif normal_budget_remaining <= 0:
        classifier_queue_state = "Paused"
        classifier_eta = (
            "Normal API budget exhausted"
        )

    elif classifier_capacity_per_hour > 0:
        classifier_queue_state = "Working"

        eta_hours = (
            classifier_queue_remaining
            / classifier_capacity_per_hour
        )

        if eta_hours < 1:
            classifier_eta = (
                f"~{max(1, int(round(eta_hours * 60)))} min"
            )

        elif eta_hours < 24:
            classifier_eta = (
                f"~{eta_hours:.1f} hours"
            )

        else:
            classifier_eta = (
                f"~{eta_hours / 24:.1f} days"
            )

    else:
        classifier_queue_state = "Waiting"
        classifier_eta = "Unknown"


    image_queue_remaining = safe_scalar(
        """
        SELECT COUNT(*)
        FROM listings
        WHERE COALESCE(active,1)=1
          AND estimated_value IS NOT NULL
          AND deal_score > 0
          AND undervaluation_gbp >= ?
          AND (
                image_url IS NULL
                OR trim(image_url)=''
              )
        """,
        (
            MIN_UNDERVALUE_GBP,
        )
    )

    image_allowance_remaining = max(
        0,
        IMAGE_BACKFILL_DAILY_ALLOWANCE
        - image_backfill_used
    )

    image_queue_session_total = (
        image_backfill_used
        + image_queue_remaining
    )

    image_progress_pct = (
        (
            image_backfill_used
            / image_queue_session_total
        ) * 100.0
        if image_queue_session_total
        else 100.0
    )

    if image_queue_remaining <= 0:
        image_queue_state = "Complete"
        image_eta = "Complete"

    elif cooldown > 0:
        image_queue_state = "Paused"
        image_eta = "429 cooldown active"

    elif image_allowance_remaining <= 0:
        image_queue_state = "Paused"
        image_eta = "Daily allowance exhausted"

    else:
        image_queue_state = "Working"

        processable_today = min(
            image_queue_remaining,
            image_allowance_remaining
        )

        cycles_needed = max(
            1,
            math.ceil(
                processable_today
                / max(
                    1,
                    IMAGE_BACKFILL_PER_CYCLE
                )
            )
        )

        eta_seconds = (
            cycles_needed
            * POLL_NORMAL
        )

        if image_queue_remaining > image_allowance_remaining:
            image_eta = (
                f"~{max(1, eta_seconds // 60)} min "
                f"for today's allowance; "
                f"{image_queue_remaining - image_allowance_remaining:,} "
                f"will remain"
            )

        elif eta_seconds < 3600:
            image_eta = (
                f"~{max(1, eta_seconds // 60)} min"
            )

        else:
            image_eta = (
                f"~{eta_seconds / 3600:.1f} hours"
            )


    win11_state_rows = safe_rows(
        """
        SELECT
            COALESCE(win11_state, 'UNCLASSIFIED') AS state,
            COUNT(*) AS n
        FROM listings
        WHERE COALESCE(active,1)=1
        GROUP BY COALESCE(win11_state, 'UNCLASSIFIED')
        ORDER BY n DESC
        """
    )

    win11_counts = {
        row["state"]: row["n"]
        for row in win11_state_rows
    }

    win11_items = [
        (
            "Officially supported",
            win11_counts.get(WIN11_OFFICIAL, 0)
        ),
        (
            "Runs well, unsupported CPU",
            win11_counts.get(WIN11_UNOFFICIAL_OK, 0)
        ),
        (
            "Not recommended",
            win11_counts.get(WIN11_UNSUITABLE, 0)
        ),
        (
            "Unknown",
            (
                win11_counts.get(WIN11_UNKNOWN, 0)
                + win11_counts.get("UNCLASSIFIED", 0)
            )
        ),
        (
            "Unofficial CPU Mark minimum",
            f"{WIN11_UNOFFICIAL_MIN_CPU_MARK:,}"
        ),
        (
            "Microsoft processor rule",
            WIN11_MICROSOFT_VERSION
        ),
    ]



    try:
        session = _read_json_file(
            PRODUCT_RESEARCH_SESSION_STATE
        ) or {}
    except Exception:
        session = {}

    try:
        helper = _read_json_file(
            PRODUCT_RESEARCH_HELPER_STATE
        ) or {}
    except Exception:
        helper = {}

    session_status = session.get(
        "status",
        "UNKNOWN"
    )

    helper_status = helper.get(
        "status",
        "UNKNOWN"
    )

    latest_seen = safe_scalar(
        """
        SELECT MAX(last_seen)
        FROM listings
        """,
        default="—"
    ) or "—"

    latest_research = safe_scalar(
        """
        SELECT MAX(valuation_research_at)
        FROM listings
        """,
        default="—"
    ) or "—"

    latest_availability = safe_scalar(
        """
        SELECT MAX(availability_checked_at)
        FROM listings
        """,
        default="—"
    ) or "—"

    never_availability_checked = safe_scalar(
        """
        SELECT COUNT(*)
        FROM listings
        WHERE COALESCE(active,1)=1
          AND availability_checked_at IS NULL
        """
    )

    recently_inactive = safe_scalar(
        """
        SELECT COUNT(*)
        FROM listings
        WHERE COALESCE(active,1)=0
          AND inactive_since >= datetime('now', '-24 hours')
        """
    )

    valuation_basis_rows = safe_rows(
        """
        SELECT
            COALESCE(valuation_basis, 'NO_VALUATION_BASIS') AS basis,
            COUNT(*) AS n
        FROM listings
        WHERE COALESCE(active,1)=1
        GROUP BY COALESCE(valuation_basis, 'NO_VALUATION_BASIS')
        ORDER BY n DESC
        """
    )

    valuation_basis_items = [
        (
            row["basis"],
            row["n"]
        )
        for row in valuation_basis_rows
    ]

    classifier_rows = safe_rows(
        """
        SELECT
            COALESCE(classifier_version, 'NULL') AS version,
            COUNT(*) AS n
        FROM listings
        WHERE COALESCE(active,1)=1
        GROUP BY COALESCE(classifier_version, 'NULL')
        ORDER BY n DESC
        """
    )

    classifier_items = [
        (
            row["version"],
            row["n"]
        )
        for row in classifier_rows
    ]

    capability_rows = safe_rows(
        """
        SELECT
            capability || ' / ' || status AS label,
            COUNT(*) AS n
        FROM capability_queue
        GROUP BY capability, status
        ORDER BY capability, status
        """
    )

    capability_items = [
        (
            row["label"],
            row["n"]
        )
        for row in capability_rows
    ]

    sold_search_rows = safe_rows(
        """
        SELECT
            COALESCE(status, 'UNKNOWN') AS status,
            COUNT(*) AS n
        FROM sold_searches
        GROUP BY COALESCE(status, 'UNKNOWN')
        ORDER BY n DESC
        """
    )

    sold_search_items = [
        (
            row["status"],
            row["n"]
        )
        for row in sold_search_rows
    ]

    sold_comparables = safe_scalar(
        "SELECT COUNT(*) FROM sold_comparables"
    )

    sold_search_total = safe_scalar(
        "SELECT COUNT(*) FROM sold_searches"
    )

    migration_rows = safe_rows(
        """
        SELECT migration_key, applied_at
        FROM app_migrations
        ORDER BY applied_at DESC
        LIMIT 10
        """
    )

    migration_html = ""

    if migration_rows:
        migration_html = "".join(
            (
                "<tr>"
                f"<td>{html.escape(str(row['migration_key']))}</td>"
                f"<td>{html.escape(str(row['applied_at']))}</td>"
                "</tr>"
            )
            for row in migration_rows
        )
    else:
        migration_html = (
            "<tr><td colspan='2' class='muted'>No migrations recorded</td></tr>"
        )

    session_items = [
        ("Status", session_status),
        (
            "Last checked",
            relative_age(
                session.get("last_checked_at")
            )
        ),
        (
            "Last success",
            relative_age(
                session.get("last_success_at")
            )
        ),
        (
            "Last refresh",
            relative_age(
                session.get("last_refresh_at")
            )
        ),
    ]

    helper_items = [
        ("Status", helper_status),
        (
            "Last seen",
            relative_age(
                helper.get("last_seen_at")
            )
        ),
        (
            "Page",
            helper.get("page_title")
            or "—"
        ),
        (
            "URL",
            helper.get("page_url")
            or "—"
        ),
    ]

    queue_items = [
        ("REANALYSIS_REQUIRED", reanalysis),
        ("INCOMPLETE_IDENTITY_OR_SPEC", incomplete),
        ("CONDITION_REQUIRES_REVIEW", condition_review),
        ("ACTIVE_FALLBACK", active_fallback),
        ("UNKNOWN_DELIVERED_COST", unknown_cost),
    ]

    missing_items = [
        ("Missing brand", missing_brand),
        ("Missing model", missing_model),
        ("Missing CPU", missing_cpu),
        ("Missing RAM", missing_ram),
        ("Missing storage", missing_storage),
    ]

    api_items = [
        ("Browse calls this eBay window", api_used),
        (
            "eBay quota limit",
            ebay_quota_limit
            if ebay_quota_limit is not None
            else "—"
        ),
        (
            "eBay quota remaining",
            ebay_quota_remaining
            if ebay_quota_remaining is not None
            else "—"
        ),
        ("eBay quota reset", ebay_reset_text),
        (
            "Budget source",
            api_budget.get("source")
            or "Unknown"
        ),
        ("Local safety limit", DAILY_SAFETY_LIMIT),
        ("Safety budget remaining", api_remaining),
        ("Normal budget remaining", normal_budget_remaining),
        ("Emergency reserve", EMERGENCY_RESERVE),
        ("SEARCH", search_calls),
        ("GET_ITEM", get_item_calls),
        ("IMAGE_BACKFILL", image_backfill_used),
        (
            "Image allowance",
            IMAGE_BACKFILL_DAILY_ALLOWANCE
        ),
        (
            "429 cooldown",
            (
                f"{int(cooldown)}s remaining"
                if cooldown > 0
                else "Clear"
            )
        ),
    ]

    maintenance_items = [
        ("Latest listing seen", latest_seen),
        ("Latest valuation research", latest_research),
        ("Latest availability check", latest_availability),
        (
            "Active listings never availability checked",
            never_availability_checked
        ),
        ("Listings made inactive in last 24h", recently_inactive),
    ]

    status_class = (
        "good"
        if str(session_status).upper() == "WORKING"
        else "bad"
    )

    helper_class = (
        "good"
        if str(helper_status).upper()
        in ("CONNECTED", "WORKING", "OK")
        else "warn"
    )

    usage_pct = (
        (api_used / DAILY_SAFETY_LIMIT) * 100
        if DAILY_SAFETY_LIMIT
        else 0
    )

    html_page = f"""<!doctype html>
<html>
<head>
    <meta charset="utf-8">
    <meta http-equiv="refresh" content="60">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Laptop Lander Diagnostics</title>

    <style>
        * {{
            box-sizing: border-box;
        }}

        body {{
            margin: 0;
            background: #f4f7fb;
            color: #172033;
            font-family:
                Inter,
                ui-sans-serif,
                system-ui,
                -apple-system,
                BlinkMacSystemFont,
                "Segoe UI",
                sans-serif;
        }}

        .page {{
            max-width: 1500px;
            margin: 0 auto;
            padding: 22px;
        }}

        .top {{
            display: flex;
            justify-content: space-between;
            align-items: flex-start;
            gap: 20px;
            margin-bottom: 18px;
        }}

        h1 {{
            margin: 0;
            font-size: 28px;
        }}

        .subtitle {{
            margin-top: 5px;
            color: #667085;
        }}

        .version {{
            text-align: right;
            color: #667085;
            font-size: 13px;
        }}

        .site-nav {{
            display: flex;
            gap: 8px;
            margin: 0 0 18px;
        }}

        .site-nav a {{
            text-decoration: none;
            color: #344054;
            background: #fff;
            border: 1px solid #d0d5dd;
            border-radius: 9px;
            padding: 8px 13px;
            font-weight: 600;
        }}

        .site-nav a.active {{
            color: #fff;
            background: #2563eb;
            border-color: #2563eb;
        }}

        .stats {{
            display: grid;
            grid-template-columns:
                repeat(auto-fit, minmax(165px, 1fr));
            gap: 12px;
            margin-bottom: 18px;
        }}

        .stat-card,
        .panel {{
            background: #fff;
            border: 1px solid #e4e7ec;
            border-radius: 13px;
            box-shadow: 0 2px 7px rgba(16, 24, 40, .05);
        }}

        .stat-card {{
            padding: 15px;
        }}

        .stat-label {{
            color: #667085;
            font-size: 12px;
            text-transform: uppercase;
            letter-spacing: .04em;
        }}

        .stat-value {{
            font-size: 27px;
            font-weight: 700;
            margin-top: 5px;
        }}

        .stat-note {{
            color: #98a2b3;
            margin-top: 4px;
            font-size: 12px;
        }}

        .grid {{
            display: grid;
            grid-template-columns:
                repeat(auto-fit, minmax(390px, 1fr));
            gap: 16px;
        }}

        .panel {{
            overflow: hidden;
        }}

        .panel h2 {{
            margin: 0;
            padding: 14px 17px;
            font-size: 17px;
            background: #f9fafb;
            border-bottom: 1px solid #eaecf0;
        }}

        table {{
            width: 100%;
            border-collapse: collapse;
        }}

        th,
        td {{
            padding: 9px 15px;
            text-align: left;
            border-bottom: 1px solid #f0f2f5;
            vertical-align: top;
        }}

        th {{
            color: #667085;
            font-size: 12px;
            text-transform: uppercase;
        }}

        td {{
            font-size: 14px;
        }}

        td.number {{
            text-align: right;
            font-variant-numeric: tabular-nums;
            font-weight: 600;
        }}

        tr:last-child td {{
            border-bottom: 0;
        }}

        .good {{
            color: #067647;
            font-weight: 700;
        }}

        .warn {{
            color: #b54708;
            font-weight: 700;
        }}

        .bad {{
            color: #b42318;
            font-weight: 700;
        }}

        .muted {{
            color: #98a2b3;
        }}

        .progress {{
            height: 12px;
            overflow: hidden;
            background: #eaecf0;
            border-radius: 999px;
            margin: 12px 15px 15px;
        }}

        .progress > div {{
            height: 100%;
            width: {min(100, usage_pct):.1f}%;
            background: #2563eb;
        }}

        .status-line {{
            padding: 12px 15px 0;
            font-size: 14px;
        }}

        @media (max-width: 700px) {{
            .page {{
                padding: 12px;
            }}

            .top {{
                display: block;
            }}

            .version {{
                margin-top: 8px;
                text-align: left;
            }}

            .grid {{
                grid-template-columns: 1fr;
            }}
        }}

        .queue-section {{
            margin-bottom: 18px;
        }}

        .queue-stack {{
            display: grid;
            gap: 12px;
            padding: 16px;
        }}

        .queue-card {{
            background: #fff;
            border: 1px solid #e4e7ec;
            border-radius: 11px;
            padding: 15px;
        }}

        .queue-head {{
            display: flex;
            justify-content: space-between;
            align-items: flex-start;
            gap: 16px;
        }}

        .queue-title {{
            font-size: 16px;
            font-weight: 700;
        }}

        .queue-state {{
            margin-top: 3px;
            color: #667085;
            font-size: 13px;
            font-weight: 600;
        }}

        .queue-count {{
            font-weight: 700;
            white-space: nowrap;
            font-variant-numeric: tabular-nums;
        }}

        .queue-progress {{
            height: 14px;
            margin-top: 12px;
            overflow: hidden;
            background: #eaecf0;
            border-radius: 999px;
        }}

        .queue-progress > div {{
            height: 100%;
            background: #2563eb;
            border-radius: 999px;
            transition: width .25s ease;
        }}

        .queue-meta {{
            display: flex;
            justify-content: space-between;
            gap: 16px;
            margin-top: 7px;
            color: #667085;
            font-size: 13px;
        }}

        .queue-detail {{
            margin-top: 8px;
            color: #98a2b3;
            font-size: 12px;
            line-height: 1.4;
        }}

</style>
</head>

<body>
<div class="page">

    {_site_nav("diagnostics")}

    <div class="top">
        <div>
            <h1>Laptop Lander Diagnostics</h1>
            <div class="subtitle">
                Live operational state — refreshes every 60 seconds
            </div>
        </div>

        <div class="version">
            App v{html.escape(str(APP_VERSION))}<br>
            Classifier v{html.escape(str(CLASSIFIER_VERSION))}<br>
            {html.escape(utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"))}
        </div>
    </div>

    <div class="stats">
        {stat_card("Active listings", active_total)}
        {stat_card("Valued", valued,
                   f"{(valued / active_total * 100):.1f}% of active" if active_total else "0%")}
        {stat_card("Unvalued", unvalued)}
        {stat_card("Reanalysis queue", reanalysis)}
        {stat_card("Scored deals", scored)}
        {stat_card("Qualifying value gaps", qualifying)}
        {stat_card("Auctions", auctions)}
        {stat_card("Fixed price", fixed_price)}
    </div>

    <section class="panel queue-section">

        <h2>Active work queues</h2>

        <div class="queue-stack">

            {queue_card(
                "Classifier reanalysis / detail refresh",
                classifier_queue_remaining,
                classifier_progress_pct,
                classifier_eta,
                (
                    f"{current_classifier_count:,} active listings "
                    f"already use classifier "
                    f"{CLASSIFIER_VERSION}. "
                    f"Maximum "
                    f"{MAX_BACKFILL_DETAILS_PER_CYCLE} "
                    f"detail refreshes per cycle; "
                    f"normal cycle {POLL_NORMAL}s. "
                    f"ETA is based on maximum configured capacity."
                ),
                classifier_queue_state
            )}

            {queue_card(
                "Dashboard image backfill",
                image_queue_remaining,
                image_progress_pct,
                image_eta,
                (
                    f"{image_backfill_used:,}/"
                    f"{IMAGE_BACKFILL_DAILY_ALLOWANCE:,} "
                    f"thumbnail calls used today; "
                    f"{image_allowance_remaining:,} "
                    f"remaining in today's allowance."
                ),
                image_queue_state
            )}

        </div>

    </section>

    <div class="grid">

        <section class="panel">
            <h2>Valuation pipeline</h2>
            <table>
                {table_rows(queue_items)}
            </table>
        </section>

        <section class="panel">
            <h2>Incomplete specification</h2>
            <table>
                {table_rows(missing_items)}
            </table>
        </section>

        <section class="panel">
            <h2>Valuation basis — active listings</h2>
            <table>
                {table_rows(valuation_basis_items)}
            </table>
        </section>

        <section class="panel">
            <h2>Classifier versions — active listings</h2>
            <table>
                {table_rows(classifier_items)}
            </table>
        </section>

        <section class="panel">
            <h2>Windows 11 CPU classification</h2>
            <div class="status-line">
                CPU-only assessment; TPM, Secure Boot and other
                machine-level requirements are assessed separately.
            </div>
            <table>
                {table_rows(win11_items)}
            </table>
        </section>

        <section class="panel">
            <h2>eBay API budget</h2>
            <div class="status-line">
                Usage:
                <strong>{api_used:,} / {DAILY_SAFETY_LIMIT:,}</strong>
                ({usage_pct:.1f}%)
            </div>
            <div class="progress"><div></div></div>
            <table>
                {table_rows(api_items)}
            </table>
        </section>

        <section class="panel">
            <h2>Product Research session</h2>
            <div class="status-line {status_class}">
                {html.escape(str(session_status))}
            </div>
            <table>
                {table_rows(session_items)}
            </table>
        </section>

        <section class="panel">
            <h2>Chromium session helper</h2>
            <div class="status-line {helper_class}">
                {html.escape(str(helper_status))}
            </div>
            <table>
                {table_rows(helper_items)}
            </table>
        </section>

        <section class="panel">
            <h2>Product Research cache</h2>
            <table>
                {table_rows(
                    [
                        ("Search cache rows", sold_search_total),
                        ("Sold comparable rows", sold_comparables),
                    ]
                    + sold_search_items
                )}
            </table>
        </section>

        <section class="panel">
            <h2>Capability queues</h2>
            <table>
                {table_rows(capability_items)}
            </table>
        </section>

        <section class="panel">
            <h2>Maintenance / freshness</h2>
            <table>
                {table_rows(maintenance_items)}
            </table>
        </section>

        <section class="panel">
            <h2>Database</h2>
            <table>
                {table_rows([
                    ("All listings", total_all),
                    ("Active listings", active_total),
                    ("Inactive listings", inactive_total),
                    ("Valued active", valued),
                    ("Unvalued active", unvalued),
                ])}
            </table>
        </section>

        <section class="panel">
            <h2>Recent migrations</h2>
            <table>
                <thead>
                    <tr>
                        <th>Migration</th>
                        <th>Applied</th>
                    </tr>
                </thead>
                <tbody>
                    {migration_html}
                </tbody>
            </table>
        </section>

    </div>
</div>
</body>
</html>
"""

    conn.close()

    return html_page



def _settings_cookie_token(handler):
    raw = handler.headers.get("Cookie", "")
    cookie = SimpleCookie()
    try:
        cookie.load(raw)
    except Exception:
        return ""
    morsel = cookie.get("ll_settings_session")
    return morsel.value if morsel else ""


def _settings_session(handler):
    token = _settings_cookie_token(handler)
    session = _SETTINGS_SESSIONS.get(token)
    if not session:
        return None
    if session["expires_at"] < time.time():
        _SETTINGS_SESSIONS.pop(token, None)
        return None
    return session


def _new_settings_session():
    token = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(24)
    _SETTINGS_SESSIONS[token] = {
        "csrf": csrf,
        "expires_at": time.time() + SETTINGS_SESSION_SECONDS,
    }
    return token, csrf


def _csrf_ok(handler, form):
    session = _settings_session(handler)
    return bool(
        session
        and form.get("csrf", [""])[0]
        and hmac.compare_digest(
            session["csrf"],
            form.get("csrf", [""])[0],
        )
    )


def _settings_password_configured(conn):
    return bool(conn.execute(
        "SELECT 1 FROM settings_auth WHERE key='password_hash'"
    ).fetchone())


def _settings_login_html(message=""):
    message_html = (
        f'<div class="message">{html.escape(message)}</div>'
        if message else ""
    )
    return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Laptop Lander Settings Login</title>
<style>
body{{font-family:Inter,system-ui,sans-serif;background:#f4f7fb;color:#172033;margin:0}}
.box{{max-width:430px;margin:10vh auto;background:white;padding:28px;border-radius:16px;box-shadow:0 8px 28px rgba(16,24,40,.10)}}
h1{{margin-top:0}} label{{display:block;font-weight:700;margin:14px 0 6px}}
input{{width:100%;box-sizing:border-box;padding:11px;border:1px solid #d0d5dd;border-radius:8px}}
button{{margin-top:18px;background:#2563eb;color:white;border:0;border-radius:8px;padding:10px 16px;font-weight:700;cursor:pointer}}
.message{{padding:10px 12px;background:#fff7ed;color:#9a3412;border-radius:8px;margin:12px 0}}
.small{{font-size:13px;color:#667085}}
</style>
</head>
<body>
<div class="box">
<h1>Laptop Lander Settings</h1>
{message_html}
<form method="post" action="/settings/login">
<label for="password">Admin password</label>
<input id="password" name="password" type="password" autocomplete="current-password" required autofocus>
<button type="submit">Sign in</button>
</form>
<p class="small">The initial password is read once from the TrueNAS environment variable <code>{SETTINGS_PASSWORD_ENV}</code> and stored only as a PBKDF2 hash.</p>
</div>
</body>
</html>"""


def _settings_value(conn, key):
    row = conn.execute(
        "SELECT value FROM app_settings WHERE key=?",
        (key,),
    ).fetchone()
    if row:
        return row["value"]
    return _setting_text(_setting_default(key))


def settings_html(csrf, message="", regex_result=""):
    conn = connect_db()
    refresh_runtime_settings(conn)
    refresh_classifier_rules(conn)

    grouped = {}
    for meta in SETTINGS_SCHEMA:
        grouped.setdefault(meta["group"], []).append(meta)

    sections = []
    for group, items in grouped.items():
        rows = []
        for meta in items:
            value = _settings_value(conn, meta["key"])
            rows.append(
                "<tr>"
                f"<td><strong>{html.escape(meta['label'])}</strong>"
                f"<div class='muted'>{html.escape(meta['key'])}</div></td>"
                "<td>"
                f"<input name='setting__{html.escape(meta['key'])}' "
                f"type='number' value='{html.escape(str(value), quote=True)}' "
                f"min='{meta.get('min','')}' max='{meta.get('max','')}' "
                f"step='{meta.get('step',1)}'>"
                "</td>"
                f"<td><span class='apply'>{html.escape(meta['apply'])}</span></td>"
                "</tr>"
            )
        sections.append(
            f"<section class='panel'><h2>{html.escape(group)}</h2>"
            "<table><thead><tr><th>Setting</th><th>Value</th><th>Effect</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table></section>"
        )

    rule_titles = {
        "fault_high": ("High-risk condition phrases", "Triggers reanalysis"),
        "fault_moderate": ("Moderate condition phrases", "Triggers reanalysis"),
        "fault_low": ("Low-cost condition phrases", "Triggers reanalysis"),
        "model_patterns": ("Model recognition regexes", "Triggers reanalysis"),
    }
    rule_blocks = []
    for category, (title, effect) in rule_titles.items():
        values = classifier_rule_values(category, DEFAULT_RULE_GROUPS[category])
        rule_blocks.append(
            "<div class='rule-block'>"
            f"<label>{html.escape(title)} <span class='apply'>{effect}</span></label>"
            f"<textarea name='rules__{category}' rows='10'>"
            + html.escape("\n".join(values))
            + "</textarea></div>"
        )

    history = conn.execute(
        """
        SELECT id,changed_at,kind,setting_key,old_value,new_value
        FROM settings_audit
        ORDER BY id DESC
        LIMIT 30
        """
    ).fetchall()
    history_rows = []
    for row in history:
        restore = ""
        if row["kind"] == "SETTING" and row["setting_key"] in SETTINGS_SCHEMA_BY_KEY:
            restore = (
                "<form method='post' action='/settings/restore' class='inline'>"
                f"<input type='hidden' name='csrf' value='{html.escape(csrf, quote=True)}'>"
                f"<input type='hidden' name='audit_id' value='{row['id']}'>"
                "<button class='small-button' type='submit'>Restore</button></form>"
            )
        history_rows.append(
            "<tr>"
            f"<td>{html.escape(relative_age(row['changed_at']))}</td>"
            f"<td>{html.escape(row['kind'])}</td>"
            f"<td>{html.escape(row['setting_key'])}</td>"
            f"<td>{html.escape(str(row['old_value'] or '—'))}</td>"
            f"<td>{html.escape(str(row['new_value'] or '—'))}</td>"
            f"<td>{restore}</td>"
            "</tr>"
        )

    revision = current_rules_revision(conn)
    configured = _settings_password_configured(conn)
    conn.close()

    notice = ""
    if message:
        notice += f"<div class='message'>{html.escape(message)}</div>"
    if regex_result:
        notice += f"<div class='message'>{html.escape(regex_result)}</div>"

    return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Laptop Lander Settings</title>
<style>
*{{box-sizing:border-box}} body{{margin:0;background:#f4f7fb;color:#172033;font-family:Inter,system-ui,sans-serif}}
.page{{max-width:1450px;margin:0 auto;padding:22px}} .top{{display:flex;justify-content:space-between;gap:20px;align-items:flex-start}}
h1{{margin:0}} h2{{margin-top:0;font-size:19px}} .muted{{color:#667085;font-size:12px;margin-top:3px}}
.panel{{background:white;border:1px solid #e3e8ef;border-radius:14px;padding:18px;margin:16px 0;box-shadow:0 4px 18px rgba(16,24,40,.04)}}
table{{width:100%;border-collapse:collapse}} th,td{{text-align:left;padding:9px;border-bottom:1px solid #eaecf0;vertical-align:top}}
input[type=number],input[type=password],input[type=text],textarea{{width:100%;padding:9px;border:1px solid #d0d5dd;border-radius:8px;font:inherit}}
textarea{{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:13px;min-height:160px}}
button{{background:#2563eb;color:white;border:0;border-radius:8px;padding:9px 14px;font-weight:700;cursor:pointer}}
button.secondary{{background:#475467}} .small-button{{font-size:12px;padding:5px 8px}} .inline{{display:inline}}
.apply{{display:inline-block;padding:2px 7px;border-radius:999px;background:#eff6ff;color:#1d4ed8;font-size:11px;font-weight:700}}
.message{{padding:11px 13px;background:#ecfdf3;color:#166534;border-radius:8px;margin:12px 0}}
.rule-grid{{display:grid;grid-template-columns:1fr 1fr;gap:16px}} .rule-block label{{display:block;font-weight:700;margin-bottom:7px}}
.actions{{display:flex;gap:10px;flex-wrap:wrap}} code{{background:#f2f4f7;padding:2px 5px;border-radius:4px}}
.site-nav{{display:flex;gap:8px;margin-bottom:18px}} .nav-link{{text-decoration:none;padding:8px 12px;border-radius:8px;background:white;border:1px solid #d0d5dd;color:#344054;font-weight:700}}
.nav-link.active{{background:#2563eb;color:white;border-color:#2563eb}}
@media(max-width:850px){{.rule-grid{{grid-template-columns:1fr}} .top{{display:block}}}}
</style>
</head>
<body>
<div class="page">
{_site_nav("settings")}
<div class="top">
<div>
<h1>Settings</h1>
<div class="muted">App {html.escape(APP_VERSION)} · classifier {html.escape(CLASSIFIER_VERSION)} · rules revision {revision}</div>
</div>
<form method="post" action="/settings/logout">
<input type="hidden" name="csrf" value="{html.escape(csrf, quote=True)}">
<button class="secondary" type="submit">Sign out</button>
</form>
</div>
{notice}

<form method="post" action="/settings/save">
<input type="hidden" name="csrf" value="{html.escape(csrf, quote=True)}">
{''.join(sections)}
<div class="actions"><button type="submit">Save application settings</button></div>
</form>

<section class="panel">
<h2>Classifier and recognition rules</h2>
<p class="muted">One phrase or regular expression per line. Invalid model regexes are rejected before saving. Any change increments the rules revision and automatically puts existing active listings back into the reanalysis queue.</p>
<form method="post" action="/settings/rules">
<input type="hidden" name="csrf" value="{html.escape(csrf, quote=True)}">
<div class="rule-grid">{''.join(rule_blocks)}</div>
<div class="actions" style="margin-top:14px"><button type="submit">Save rules &amp; trigger reanalysis</button></div>
</form>
</section>

<section class="panel">
<h2>Test a regular expression</h2>
<form method="post" action="/settings/test-regex">
<input type="hidden" name="csrf" value="{html.escape(csrf, quote=True)}">
<label><strong>Pattern</strong></label>
<input type="text" name="pattern" required>
<label><strong>Sample listing text</strong></label>
<input type="text" name="sample" required>
<div class="actions" style="margin-top:12px"><button type="submit">Test regex</button></div>
</form>
</section>

<section class="panel">
<h2>Change Settings password</h2>
<form method="post" action="/settings/password">
<input type="hidden" name="csrf" value="{html.escape(csrf, quote=True)}">
<label><strong>New password</strong></label>
<input type="password" name="new_password" minlength="10" required>
<label><strong>Confirm password</strong></label>
<input type="password" name="confirm_password" minlength="10" required>
<div class="actions" style="margin-top:12px"><button type="submit">Change password</button></div>
</form>
</section>

<section class="panel">
<h2>Change history</h2>
<table>
<thead><tr><th>When</th><th>Type</th><th>Setting/rules</th><th>Old</th><th>New</th><th></th></tr></thead>
<tbody>{''.join(history_rows) or "<tr><td colspan='6' class='muted'>No changes yet</td></tr>"}</tbody>
</table>
</section>
</div>
</body>
</html>"""


def _save_settings_form(form):
    conn = connect_db()
    try:
        pending = {}
        for key, meta in SETTINGS_SCHEMA_BY_KEY.items():
            field = "setting__" + key
            if field not in form:
                continue
            value = _coerce_setting(meta, form[field][0])
            pending[key] = value

        # Cross-field safety checks.
        safety = int(pending.get("DAILY_SAFETY_LIMIT", app_setting(conn, "DAILY_SAFETY_LIMIT")))
        emergency = int(pending.get("EMERGENCY_RESERVE", app_setting(conn, "EMERGENCY_RESERVE")))
        reserve = int(pending.get("SEARCH_RESERVE", app_setting(conn, "SEARCH_RESERVE")))
        min_reserve = int(pending.get("MIN_SEARCH_RESERVE", app_setting(conn, "MIN_SEARCH_RESERVE")))
        if safety > EBAY_DAILY_LIMIT:
            raise ValueError(f"Daily safety limit cannot exceed eBay limit ({EBAY_DAILY_LIMIT})")
        if min_reserve > reserve:
            raise ValueError("Minimum search reserve cannot exceed search reserve")
        if emergency + min_reserve >= safety:
            raise ValueError("Emergency + minimum search reserve must stay below daily safety limit")

        for key, value in pending.items():
            old = _settings_value(conn, key)
            new = _setting_text(value)
            if old == new:
                continue
            conn.execute(
                """
                INSERT INTO app_settings(key,value,updated_at)
                VALUES (?,?,?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at
                """,
                (key, new, iso_now()),
            )
            _audit_setting(conn, "SETTING", key, old, new)
        conn.commit()
        refresh_runtime_settings(conn)
    finally:
        conn.close()


def _save_rules_form(form):
    conn = connect_db()
    try:
        changed = []
        for category in DEFAULT_RULE_GROUPS:
            field = "rules__" + category
            if field not in form:
                continue
            values = []
            for raw in form[field][0].splitlines():
                value = raw.strip()
                if value and value not in values:
                    values.append(value)
            if not values:
                raise ValueError(f"{category} cannot be empty")
            if category == "model_patterns":
                for pattern in values:
                    re.compile(pattern, re.I)

            old_rows = conn.execute(
                "SELECT value FROM classifier_rules WHERE category=? AND enabled=1 ORDER BY position,id",
                (category,),
            ).fetchall()
            old = [r["value"] for r in old_rows]
            if old == values:
                continue

            conn.execute("DELETE FROM classifier_rules WHERE category=?", (category,))
            for position, value in enumerate(values):
                conn.execute(
                    """
                    INSERT INTO classifier_rules(category,position,value,enabled,updated_at)
                    VALUES (?,?,?,?,?)
                    """,
                    (category, position, value, 1, iso_now()),
                )
            _audit_setting(
                conn,
                "RULES",
                category,
                json.dumps(old, ensure_ascii=False),
                json.dumps(values, ensure_ascii=False),
            )
            changed.append(category)

        if changed:
            revision = _bump_rules_revision(conn)
            conn.commit()
            refresh_classifier_rules(conn)
            return revision, changed
        conn.commit()
        return current_rules_revision(conn), []
    finally:
        conn.close()


def _change_settings_password(new_password):
    if len(new_password) < 10:
        raise ValueError("Password must be at least 10 characters")
    conn = connect_db()
    try:
        old = conn.execute(
            "SELECT value FROM settings_auth WHERE key='password_hash'"
        ).fetchone()
        conn.execute(
            """
            INSERT INTO settings_auth(key,value,updated_at)
            VALUES ('password_hash',?,?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at
            """,
            (_password_hash(new_password), iso_now()),
        )
        _audit_setting(conn, "AUTH", "settings_password", "configured" if old else "unset", "changed")
        conn.commit()
    finally:
        conn.close()


class DashboardHandler(
    BaseHTTPRequestHandler
):

    def _send_html(self, page, status=200, headers=None):
        content = page.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        for key, value in (headers or []):
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(content)

    def _redirect(self, location, headers=None):
        self.send_response(303)
        self.send_header("Location", location)
        for key, value in (headers or []):
            self.send_header(key, value)
        self.end_headers()

    def _read_form(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length < 0 or length > 1024 * 1024:
            raise ValueError("Invalid form size")
        body = self.rfile.read(length).decode("utf-8", errors="replace")
        return urllib.parse.parse_qs(body, keep_blank_values=True)

    def _require_settings_auth(self):
        session = _settings_session(self)
        if not session:
            self._redirect("/settings/login")
            return None
        return session

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path

        if path in ("/", "/index.html"):
            page = dashboard_html()
            self._send_html(page)
            return

        if path in ("/diagnostics", "/diagnostics/"):
            page = diagnostics_html()
            self._send_html(page)
            return

        if path in ("/settings/login", "/settings/login/"):
            if _settings_session(self):
                self._redirect("/settings")
                return
            conn = connect_db()
            configured = _settings_password_configured(conn)
            conn.close()
            message = "" if configured else (
                f"Settings password is not configured. Add {SETTINGS_PASSWORD_ENV} "
                "to the TrueNAS app environment and restart the app once."
            )
            self._send_html(_settings_login_html(message))
            return

        if path in ("/settings", "/settings/"):
            session = self._require_settings_auth()
            if not session:
                return
            self._send_html(settings_html(session["csrf"]))
            return

        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path

        try:
            form = self._read_form()
        except Exception as exc:
            self._send_html(
                _settings_login_html(f"Invalid request: {exc}"),
                status=400,
            )
            return

        if path == "/settings/login":
            password = form.get("password", [""])[0]
            conn = connect_db()
            row = conn.execute(
                "SELECT value FROM settings_auth WHERE key='password_hash'"
            ).fetchone()
            conn.close()

            if not row or not _password_matches(password, row["value"]):
                self._send_html(
                    _settings_login_html("Incorrect password or password not configured."),
                    status=401,
                )
                return

            token, _ = _new_settings_session()
            cookie = (
                f"ll_settings_session={token}; Path=/settings; "
                f"Max-Age={SETTINGS_SESSION_SECONDS}; HttpOnly; SameSite=Strict"
            )
            self._redirect("/settings", [("Set-Cookie", cookie)])
            return

        session = self._require_settings_auth()
        if not session:
            return

        if not _csrf_ok(self, form):
            self._send_html(
                settings_html(session["csrf"], "CSRF validation failed."),
                status=403,
            )
            return

        try:
            if path == "/settings/logout":
                token = _settings_cookie_token(self)
                _SETTINGS_SESSIONS.pop(token, None)
                cookie = (
                    "ll_settings_session=; Path=/settings; "
                    "Max-Age=0; HttpOnly; SameSite=Strict"
                )
                self._redirect("/settings/login", [("Set-Cookie", cookie)])
                return

            if path == "/settings/save":
                _save_settings_form(form)
                self._send_html(
                    settings_html(session["csrf"], "Settings saved. Changes are active now or on the next cycle as labelled.")
                )
                return

            if path == "/settings/rules":
                revision, changed = _save_rules_form(form)
                message = (
                    f"Rules saved. Rules revision is now {revision}; "
                    f"reanalysis queued for active listings. Changed: {', '.join(changed)}"
                    if changed else
                    "No classifier rule changes detected."
                )
                self._send_html(settings_html(session["csrf"], message))
                return

            if path == "/settings/test-regex":
                pattern = form.get("pattern", [""])[0]
                sample = form.get("sample", [""])[0]
                compiled = re.compile(pattern, re.I)
                match = compiled.search(sample)
                result = (
                    f"MATCH: {match.group(0)!r} at {match.span()}"
                    if match else "No match"
                )
                self._send_html(settings_html(session["csrf"], regex_result=result))
                return

            if path == "/settings/password":
                new_password = form.get("new_password", [""])[0]
                confirm = form.get("confirm_password", [""])[0]
                if new_password != confirm:
                    raise ValueError("Password confirmation does not match")
                _change_settings_password(new_password)
                self._send_html(settings_html(session["csrf"], "Settings password changed."))
                return

            if path == "/settings/restore":
                audit_id = int(form.get("audit_id", ["0"])[0])
                conn = connect_db()
                try:
                    row = conn.execute(
                        """
                        SELECT * FROM settings_audit
                        WHERE id=? AND kind='SETTING'
                        """,
                        (audit_id,),
                    ).fetchone()
                    if not row or row["setting_key"] not in SETTINGS_SCHEMA_BY_KEY:
                        raise ValueError("Change cannot be restored")
                    meta = SETTINGS_SCHEMA_BY_KEY[row["setting_key"]]
                    restored = _coerce_setting(meta, row["old_value"])
                    current = _settings_value(conn, row["setting_key"])
                    text_value = _setting_text(restored)
                    conn.execute(
                        """
                        INSERT INTO app_settings(key,value,updated_at)
                        VALUES (?,?,?)
                        ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at
                        """,
                        (row["setting_key"], text_value, iso_now()),
                    )
                    _audit_setting(
                        conn, "SETTING", row["setting_key"], current, text_value
                    )
                    conn.commit()
                    refresh_runtime_settings(conn)
                finally:
                    conn.close()
                self._send_html(settings_html(session["csrf"], "Setting restored."))
                return

            self.send_response(404)
            self.end_headers()

        except re.error as exc:
            self._send_html(
                settings_html(session["csrf"], f"Invalid regular expression: {exc}"),
                status=400,
            )
        except Exception as exc:
            self._send_html(
                settings_html(session["csrf"], f"Could not save: {exc}"),
                status=400,
            )

    def log_message(
        self,
        format,
        *args
    ):
        pass


def _dashboard_state_age_seconds(value):
    if not value:
        return None

    try:
        stamp = datetime.fromisoformat(
            str(value).replace("Z", "+00:00")
        )

        if stamp.tzinfo is None:
            stamp = stamp.replace(
                tzinfo=timezone.utc
            )

        return max(
            0,
            (
                utcnow()
                - stamp.astimezone(timezone.utc)
            ).total_seconds()
        )

    except Exception:
        return None


def _dashboard_health_alert():
    """
    Healthy operational state is silent.

    Only return visible UI when something needs attention.
    """
    problems = []

    try:
        session = _read_json_file(
            PRODUCT_RESEARCH_SESSION_STATE
        ) or {}
    except Exception:
        session = {}

    try:
        helper = _read_json_file(
            PRODUCT_RESEARCH_HELPER_STATE
        ) or {}
    except Exception:
        helper = {}

    session_status = normalise(
        session.get("status")
    ).upper()

    if session_status != "WORKING":
        problems.append(
            "Product Research session is not working"
        )

    success_age = _dashboard_state_age_seconds(
        session.get("last_success_at")
    )

    # Don't alert merely because the app has only just started.
    if (
        success_age is not None
        and success_age > 15 * 60
    ):
        problems.append(
            "No successful Product Research sold search "
            f"for {int(success_age // 60)} minutes"
        )

    helper_status = normalise(
        helper.get("status")
    ).upper()

    if helper_status and helper_status not in {
        "CONNECTED",
        "WORKING",
    }:
        problems.append(
            "Chromium session helper is disconnected"
        )

    helper_age = _dashboard_state_age_seconds(
        helper.get("last_seen_at")
    )

    if (
        helper_age is not None
        and helper_age > 5 * 60
    ):
        problems.append(
            "Chromium session helper has not been seen "
            f"for {int(helper_age // 60)} minutes"
        )

    # Shared central eBay 429 cooldown, if available.
    try:
        remaining = ebay_rate_limit_remaining()
    except Exception:
        remaining = 0

    if remaining > 0:
        minutes, seconds = divmod(
            int(remaining),
            60,
        )

        problems.append(
            "eBay API rate limit active — "
            f"detail requests paused for "
            f"{minutes}m {seconds:02d}s; "
            "other processing continues"
        )

    if not problems:
        return ""

    items = "".join(
        "<li>"
        + html.escape(problem)
        + "</li>"
        for problem in problems
    )

    return (
        "<div id='system-health-alert' "
        "class='system-health-alert'>"
        "<strong>SYSTEM HEALTH ALERT</strong>"
        "<ul>"
        + items
        + "</ul>"
        "</div>"
    )


_DASHBOARD_UI_ENHANCEMENT = r"""
<style>
.site-nav {
    display: flex;
    justify-content: flex-end;
    margin: 14px 18px 0;
}

.site-nav a {
    text-decoration: none;
    color: #fff;
    background: #2563eb;
    border: 1px solid #2563eb;
    border-radius: 9px;
    padding: 8px 13px;
    font-weight: 600;
}

.system-health-alert {
    margin: 12px;
    padding: 14px 18px;
    border: 2px solid #b3261e;
    border-radius: 8px;
    background: #fff4f3;
    color: #7d1712;
}

.sortable-header {
    cursor: pointer;
    user-select: none;
}

.sort-indicator {
    display: inline-block;
    width: 1em;
    margin-left: 4px;
    font-size: 10px;
}

.valuation-hover {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    cursor: help;
    outline: none;
}

.valuation-info {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 16px;
    height: 16px;
    border: 1px solid #2563eb;
    border-radius: 50%;
    color: #2563eb;
    background: #eff6ff;
    font-size: 11px;
    font-weight: 700;
    line-height: 1;
    cursor: help;
    flex: 0 0 auto;
}

.valuation-info.discovery-pulse {
    animation: valuation-info-pulse 0.9s ease-in-out 2;
}

@keyframes valuation-info-pulse {
    0%, 100% {
        transform: scale(1);
        box-shadow: 0 0 0 0 rgba(37, 99, 235, 0);
    }
    50% {
        transform: scale(1.15);
        box-shadow: 0 0 0 5px rgba(37, 99, 235, 0.16);
    }
}

.valuation-tooltip {
    display: none;
    position: fixed;
    z-index: 99999;
    width: min(620px, calc(100vw - 24px));
    max-height: min(460px, calc(100vh - 24px));
    overflow-y: auto;
    padding: 12px;
    border: 1px solid #777;
    border-radius: 8px;
    background: white;
    color: #111;
    text-align: left;
    white-space: normal;
    font-weight: normal;
    font-size: 13px;
    line-height: 1.4;
    box-shadow: 0 8px 28px rgba(0,0,0,.24);
}

.valuation-hover.tooltip-open .valuation-tooltip {
    display: block;
}

.valuation-summary {
    margin-bottom: 8px;
}

.valuation-tooltip .evidence-table {
    margin-top: 8px;
    font-size: 12px;
}

.confidence-meta {
    white-space: nowrap;
    margin-top: 3px;
}

.notes-cell {
    min-width: 120px;
    max-width: 240px;
}

.condition-note {
    display: inline-block;
    margin: 2px 4px 2px 0;
    padding: 3px 7px;
    border: 1px solid #f3a4a4;
    border-radius: 999px;
    background: #fff1f1;
    color: #b42318;
    font-size: 11px;
    font-weight: 700;
    line-height: 1.25;
    white-space: nowrap;
}

.win11-ok-note {
    display: inline-block;
    margin: 2px 4px 2px 0;
    padding: 3px 7px;
    border: 1px solid #f59e0b;
    border-radius: 999px;
    background: #fff7ed;
    color: #b45309;
    font-size: 11px;
    font-weight: 700;
    line-height: 1.25;
    white-space: nowrap;
    cursor: help;
}

.notes-clear {
    color: #98a2b3;
}


/* ==========================================================
   LAPTOP LANDER v0.9.6
   ========================================================== */

:root {
    --ll-bg: #f4f7fb;
    --ll-surface: #ffffff;
    --ll-surface-soft: #f8fafc;
    --ll-text: #172033;
    --ll-muted: #667085;
    --ll-border: #e3e8ef;

    --ll-blue: #2563eb;
    --ll-blue-dark: #1d4ed8;
    --ll-blue-soft: #eff6ff;

    --ll-green: #15803d;
    --ll-green-soft: #ecfdf3;

    --ll-amber: #b45309;
    --ll-amber-soft: #fff7ed;

    --ll-red: #b42318;
    --ll-red-soft: #fef3f2;

    --ll-radius: 14px;
    --ll-shadow:
        0 1px 2px rgba(16, 24, 40, .04),
        0 8px 24px rgba(16, 24, 40, .06);
}

body {
    background:
        radial-gradient(
            circle at 90% -10%,
            #dbeafe 0,
            transparent 32rem
        ),
        var(--ll-bg);

    color: var(--ll-text);
    font-family:
        Inter,
        ui-sans-serif,
        system-ui,
        -apple-system,
        BlinkMacSystemFont,
        "Segoe UI",
        sans-serif;

    padding: 28px;
}

.brand-header {
    display: flex;
    align-items: center;
    gap: 15px;
    margin: 2px 0 2px;
}

.brand-mark {
    display: grid;
    place-items: center;

    width: 52px;
    height: 52px;

    border-radius: 15px;

    background:
        linear-gradient(
            145deg,
            var(--ll-blue),
            #60a5fa
        );

    box-shadow:
        0 10px 22px rgba(37, 99, 235, .25);
}

.brand-mark svg {
    width: 34px;
    height: 34px;

    fill: none;
    stroke: white;
    stroke-width: 3;
    stroke-linecap: round;
    stroke-linejoin: round;
}

.brand-header h1 {
    margin: 0;

    color: #101828;

    font-size: 30px;
    line-height: 1.05;
    letter-spacing: -.6px;
}

.brand-tagline {
    margin-top: 4px;

    color: var(--ll-muted);

    font-size: 13px;
}

.sub {
    margin:
        8px
        0
        22px
        67px;

    color: var(--ll-muted);
}

.cards {
    display: grid;

    grid-template-columns:
        repeat(
            auto-fit,
            minmax(180px, 1fr)
        );

    gap: 14px;

    margin:
        18px
        0
        22px;
}

.card {
    padding: 17px 18px;

    border:
        1px solid
        var(--ll-border);

    border-radius:
        var(--ll-radius);

    background:
        rgba(255, 255, 255, .93);

    box-shadow:
        var(--ll-shadow);
}

.card .big {
    color: var(--ll-blue-dark);

    font-size: 28px;
    font-weight: 760;
    letter-spacing: -.4px;
}

table {
    width: 100%;

    border:
        1px solid
        var(--ll-border);

    border-radius:
        var(--ll-radius);

    border-spacing: 0;

    background:
        var(--ll-surface);

    box-shadow:
        var(--ll-shadow);

    overflow: hidden;
}

thead th {
    position: sticky;
    top: 0;
    z-index: 5;

    padding: 13px 12px;

    border-bottom:
        1px solid
        var(--ll-border);

    background:
        #f8fafc;

    color:
        #475467;

    font-size: 11px;
    font-weight: 720;

    letter-spacing:
        .045em;

    text-transform:
        uppercase;
}

tbody td {
    padding: 13px 12px;

    border-bottom:
        1px solid
        #edf1f5;

    vertical-align:
        middle;
}

tbody tr:last-child td {
    border-bottom: 0;
}

tbody tr:nth-child(even) {
    background:
        #fcfdff;
}

tbody tr {
    transition:
        background .12s ease;
}

tbody tr:hover {
    background:
        #f3f7ff;
}

a {
    color:
        var(--ll-blue-dark);

    text-decoration:
        none;
}

a:hover {
    color:
        var(--ll-blue);

    text-decoration:
        underline;
}

td:nth-child(2) > a {
    color:
        #172033;

    font-weight:
        650;

    line-height:
        1.3;
}

.small {
    color:
        var(--ll-muted);

    font-size:
        12px;
}

.money {
    font-variant-numeric:
        tabular-nums;

    white-space:
        nowrap;
}

.good {
    color:
        var(--ll-green);
}

.good .valuation-hover {
    padding:
        5px
        8px;

    border-radius:
        8px;

    background:
        var(--ll-green-soft);
}

.big {
    font-variant-numeric:
        tabular-nums;
}

td[data-sort] > .big {
    display:
        inline-flex;

    align-items:
        center;

    justify-content:
        center;

    min-width:
        42px;

    padding:
        5px
        9px;

    border-radius:
        9px;

    background:
        var(--ll-blue-soft);

    color:
        var(--ll-blue-dark);

    font-weight:
        760;
}

.product-thumb-cell {
    width: 90px;
    min-width: 90px;

    padding-right:
        6px;
}

.image-header {
    width: 90px;
    min-width: 90px;
}

.product-thumb-link {
    display:
        block;

    width: 74px;
    height: 74px;

    border-radius:
        11px;

    overflow:
        hidden;

    background:
        white;

    border:
        1px solid
        var(--ll-border);

    box-shadow:
        0 2px 7px
        rgba(16, 24, 40, .08);
}

.product-thumb {
    display:
        block;

    width:
        100%;

    height:
        100%;

    object-fit:
        contain;

    background:
        white;

    transition:
        transform .16s ease;
}

.product-thumb-link:hover
.product-thumb {
    transform:
        scale(1.045);
}

.product-thumb-placeholder {
    display:
        grid;

    place-items:
        center;

    width:
        74px;

    height:
        74px;

    border:
        1px dashed
        #cbd5e1;

    border-radius:
        11px;

    background:
        #f8fafc;

    color:
        #94a3b8;

    font-size:
        24px;
}

.confidence-meta {
    color:
        var(--ll-muted);
}

.valuation-tooltip {
    border:
        1px solid
        var(--ll-border);

    border-radius:
        12px;

    box-shadow:
        0 18px 48px
        rgba(16, 24, 40, .18);
}

.system-health-alert {
    border-color:
        #f04438;

    background:
        var(--ll-red-soft);

    color:
        var(--ll-red);
}

.sortable-header:hover {
    background:
        #eef4ff;
}

.sorted-asc,
.sorted-desc {
    color:
        var(--ll-blue-dark);

    background:
        #eaf2ff;
}

@media (max-width: 900px) {

    body {
        padding:
            14px;
    }

    .brand-header h1 {
        font-size:
            25px;
    }

    .sub {
        margin-left:
            0;
    }

    table {
        font-size:
            12px;
    }

    tbody td,
    thead th {
        padding:
            9px 7px;
    }

    .product-thumb-cell,
    .image-header {
        width:
            68px;

        min-width:
            68px;
    }

    .product-thumb-link,
    .product-thumb-placeholder {
        width:
            58px;

        height:
            58px;
    }
}


/* ==========================================================
   Laptop Lander refinements
   ========================================================== */

/* Image=1, Listing=2, Age=3 */
tbody td:nth-child(3),
thead th:nth-child(3) {
    white-space: nowrap;
}

/* Less unused vertical space in dashboard statistics */
.cards {
    gap: 12px;
    margin-top: 14px;
    margin-bottom: 18px;
}

.card {
    padding: 13px 16px;
}

.card .big {
    font-size: 25px;
    line-height: 1.15;
}

/* Slightly denser rows */
tbody td {
    padding-top: 11px;
    padding-bottom: 11px;
}

/* Confidence pills */
.confidence-badge {
    display: inline-flex;
    align-items: center;
    padding: 3px 8px;
    border-radius: 999px;
    font-size: 11px;
    line-height: 1.3;
    font-weight: 750;
    letter-spacing: .035em;
}

.confidence-high {
    color: #166534;
    background: #dcfce7;
}

.confidence-medium {
    color: #92400e;
    background: #fef3c7;
}

.confidence-low {
    color: #475467;
    background: #eef2f6;
}

</style>

<script>
document.addEventListener("DOMContentLoaded", () => {

    const tables = Array.from(
        document.querySelectorAll(".deal-section table")
    );

    if (!tables.length) return;

    function numberValue(text) {
        const m = String(text)
            .replace(/,/g, "")
            .match(/-?\d+(?:\.\d+)?/);

        return m ? Number(m[0]) : -Infinity;
    }

    function durationValue(text) {
        text = String(text).toLowerCase();

        let value = 0;

        const d = text.match(/(\d+)\s*d/);
        const h = text.match(/(\d+)\s*h/);
        const m = text.match(/(\d+)\s*m/);
        const s = text.match(/(\d+)\s*s/);

        if (d) value += Number(d[1]) * 86400;
        if (h) value += Number(h[1]) * 3600;
        if (m) value += Number(m[1]) * 60;
        if (s) value += Number(s[1]);

        return value;
    }

    tables.forEach((table, tableIndex) => {
        const head = table.querySelector("thead tr");
        const body = table.querySelector("tbody");

        if (!head || !body) return;

        const headers = Array.from(head.children);
        const section = table.closest(".deal-section");
        const sectionTitle = section?.querySelector("h2")
            ?.textContent.trim() || `table-${tableIndex}`;
        const storageKey =
            `laptopLanderSort:${sectionTitle}`;

        function cellValue(row, index, name) {
            const cell = row.children[index];

            if (!cell) return "";

            if (
                cell.dataset.sort !== undefined
                && cell.dataset.sort !== ""
            ) {
                const value = Number(cell.dataset.sort);
                return Number.isFinite(value)
                    ? value
                    : -Infinity;
            }

            const text = cell.textContent.trim();

            if (
                name === "Listing age"
                || name === "Time left"
            ) {
                return durationValue(text);
            }

            if (
                name === "Power/£"
                || name === "Price"
                || name === "Current bid"
                || name === "Under market"
                || name === "Potential saving"
                || name === "Deal score"
            ) {
                return numberValue(text);
            }

            return text.toLowerCase();
        }

        function sort(index, direction, save=true) {
            const th = headers[index];
            if (!th) return;

            const name = th.dataset.sortName;
            const rows = Array.from(
                body.querySelectorAll(":scope > tr")
            );

            rows.sort((a, b) => {
                const av = cellValue(a, index, name);
                const bv = cellValue(b, index, name);

                let result;

                if (
                    typeof av === "number"
                    && typeof bv === "number"
                ) {
                    result = av - bv;
                } else {
                    result = String(av).localeCompare(
                        String(bv),
                        undefined,
                        {
                            numeric: true,
                            sensitivity: "base"
                        }
                    );
                }

                return direction === "asc"
                    ? result
                    : -result;
            });

            rows.forEach(row => body.appendChild(row));

            headers.forEach(h => {
                h.classList.remove(
                    "sorted-asc",
                    "sorted-desc"
                );

                h.setAttribute("aria-sort", "none");

                const indicator =
                    h.querySelector(".sort-indicator");

                if (indicator) {
                    indicator.textContent = "";
                }
            });

            th.classList.add(
                direction === "asc"
                    ? "sorted-asc"
                    : "sorted-desc"
            );

            th.setAttribute(
                "aria-sort",
                direction === "asc"
                    ? "ascending"
                    : "descending"
            );

            const indicator =
                th.querySelector(".sort-indicator");

            if (indicator) {
                indicator.textContent =
                    direction === "asc" ? "▲" : "▼";
            }

            if (save) {
                localStorage.setItem(
                    storageKey,
                    JSON.stringify({
                        name,
                        direction
                    })
                );
            }
        }

        headers.forEach((th, index) => {
            const name = th.textContent.trim();

            /* The image column has no useful sort value. */
            if (!name && th.classList.contains("image-header")) {
                return;
            }

            th.dataset.sortName = name;
            th.classList.add("sortable-header");
            th.setAttribute("tabindex", "0");
            th.setAttribute("aria-sort", "none");

            const indicator =
                document.createElement("span");

            indicator.className = "sort-indicator";
            indicator.setAttribute("aria-hidden", "true");

            th.appendChild(indicator);

            const activate = () => {
                const direction =
                    th.classList.contains("sorted-asc")
                        ? "desc"
                        : "asc";

                sort(index, direction, true);
            };

            th.addEventListener("click", activate);

            th.addEventListener("keydown", event => {
                if (
                    event.key === "Enter"
                    || event.key === " "
                ) {
                    event.preventDefault();
                    activate();
                }
            });
        });

        try {
            const saved = JSON.parse(
                localStorage.getItem(storageKey) || "null"
            );

            if (saved?.name && saved?.direction) {
                const index = headers.findIndex(
                    th =>
                        th.dataset.sortName
                        === saved.name
                );

                if (index >= 0) {
                    sort(
                        index,
                        saved.direction,
                        false
                    );
                }
            }
        } catch (e) {
            console.warn(
                "Unable to restore table sort",
                e
            );
        }
    });
});
</script>
"""


def dashboard_html():
    refresh_runtime_settings()
    refresh_classifier_rules()
    page = _dashboard_html_base()

    health = _dashboard_health_alert()

    if health:
        body_pos = page.lower().find("<body")

        if body_pos >= 0:
            close = page.find(
                ">",
                body_pos
            )

            if close >= 0:
                page = (
                    page[:close + 1]
                    + health
                    + page[close + 1:]
                )
        else:
            page = health + page

    valuation_tooltip_script = r"""
<script>
(function () {
    const GAP = 8;
    const EDGE = 12;

    function positionTooltip(wrapper) {
        const tooltip = wrapper.querySelector(".valuation-tooltip");
        if (!tooltip) return;

        tooltip.style.left = "0px";
        tooltip.style.top = "0px";

        const triggerRect = wrapper.getBoundingClientRect();
        const tipRect = tooltip.getBoundingClientRect();

        let left =
            triggerRect.left
            + (triggerRect.width / 2)
            - (tipRect.width / 2);

        left = Math.max(
            EDGE,
            Math.min(
                left,
                window.innerWidth - tipRect.width - EDGE
            )
        );

        let top = triggerRect.bottom + GAP;

        if (
            top + tipRect.height
            > window.innerHeight - EDGE
        ) {
            top =
                triggerRect.top
                - tipRect.height
                - GAP;
        }

        top = Math.max(
            EDGE,
            Math.min(
                top,
                window.innerHeight - tipRect.height - EDGE
            )
        );

        tooltip.style.left =
            Math.round(left) + "px";

        tooltip.style.top =
            Math.round(top) + "px";
    }

    function openTooltip(wrapper) {
        wrapper.classList.add("tooltip-open");

        requestAnimationFrame(
            () => positionTooltip(wrapper)
        );
    }

    function closeTooltip(wrapper) {
        wrapper.classList.remove("tooltip-open");
    }

    document
        .querySelectorAll(".valuation-hover")
        .forEach((wrapper) => {
            wrapper.addEventListener(
                "mouseenter",
                () => openTooltip(wrapper)
            );

            wrapper.addEventListener(
                "mouseleave",
                () => closeTooltip(wrapper)
            );

            wrapper.addEventListener(
                "focusin",
                () => openTooltip(wrapper)
            );

            wrapper.addEventListener(
                "focusout",
                () => closeTooltip(wrapper)
            );
        });

    window.addEventListener(
        "resize",
        () => {
            document
                .querySelectorAll(
                    ".valuation-hover.tooltip-open"
                )
                .forEach(positionTooltip);
        }
    );

    window.addEventListener(
        "scroll",
        () => {
            document
                .querySelectorAll(
                    ".valuation-hover.tooltip-open"
                )
                .forEach(positionTooltip);
        },
        true
    );

    try {
        const key =
            "laptop-lander-valuation-hint-seen";

        if (!localStorage.getItem(key)) {
            const info =
                document.querySelector(
                    ".valuation-info"
                );

            if (info) {
                info.classList.add(
                    "discovery-pulse"
                );

                setTimeout(
                    () => {
                        info.classList.remove(
                            "discovery-pulse"
                        );
                    },
                    2200
                );
            }

            localStorage.setItem(
                key,
                "1"
            );
        }
    } catch (e) {
        // localStorage unavailable; ignore.
    }
})();
</script>
"""

    if "</body>" in page:
        page = page.replace(
            "</body>",
            _DASHBOARD_UI_ENHANCEMENT
            + "\n"
            + valuation_tooltip_script
            + "\n</body>",
            1,
        )
    else:
        page += (
            _DASHBOARD_UI_ENHANCEMENT
            + valuation_tooltip_script
        )

    return page


def start_dashboard():
    server = ThreadingHTTPServer(
        (
            DASHBOARD_HOST,
            DASHBOARD_PORT
        ),
        DashboardHandler
    )

    thread = threading.Thread(
        target=server.serve_forever,
        daemon=True
    )

    thread.start()

    print(
        f"Dashboard listening on "
        f"port {DASHBOARD_PORT}"
    )


def ebay_get_item(token, item_id):
    """
    Lightweight Browse getItem call used to confirm that a previously-seen
    fixed-price listing is still live. A normal item response means active.
    A definitive not-found/ended response means inactive. Transient API errors
    are left alone rather than hiding a potentially live bargain.
    """
    url = (
        "https://api.ebay.com/buy/browse/v1/item/"
        + urllib.parse.quote(str(item_id), safe="")
    )
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "X-EBAY-C-MARKETPLACE-ID": MARKETPLACE,
            "Accept": "application/json",
            **({"X-EBAY-C-ENDUSERCTX": "contextualLocation=" + urllib.parse.quote(
                "country=GB,zip=" + os.environ["BUYER_POSTCODE"], safe="")}
               if os.environ.get("BUYER_POSTCODE") else {}),
        },
        method="GET",
    )
    try:
        with ebay_urlopen(request, timeout=20) as response:
            return "ACTIVE", json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        # Browse commonly returns 404 for an item that is no longer retrievable.
        # Some ended/removed states are returned as 400 with an item-not-found
        # style error. Only classify clear terminal responses as inactive.
        low = body.lower()
        if exc.code == 404 or (
            exc.code == 400
            and (
                "not found" in low
                or "itemid" in low and "invalid" in low
                or "item is not available" in low
                or "listing has ended" in low
            )
        ):
            return "INACTIVE", body
        return "ERROR", f"HTTP {exc.code}: {body[:300]}"
    except Exception as exc:
        return "ERROR", repr(exc)

def is_fixed_price_listing(row):
    options = normalise(row["buying_options"]).upper()
    return (
        "FIXED_PRICE" in options
        or "BUY_IT_NOW" in options
        or "BEST_OFFER" in options
    )




DYNAMIC_REANALYSIS_MIN_PER_CYCLE = 5
DYNAMIC_REANALYSIS_MAX_PER_CYCLE = 200


def dynamic_reanalysis_allowance(
    conn,
    backlog=None
):
    """
    Pace classifier/detail backlog work across the remainder of eBay's
    actual Browse quota window.

    The calculation only uses quota that is currently available to detail
    work after protecting:
      - the emergency reserve
      - the current search reserve

    It therefore cannot deliberately consume capacity reserved for search.
    can_detail() remains the final hard stop inside the worker.
    """

    if backlog is None:
        backlog = conn.execute(
            """
            SELECT COUNT(*) AS n
            FROM listings
            WHERE COALESCE(active,1)=1
              AND (
                    classifier_version IS NULL
                    OR classifier_version<>?
                  )
            """,
            (
                CLASSIFIER_VERSION,
            )
        ).fetchone()["n"]

    backlog = max(
        0,
        int(backlog or 0)
    )

    if backlog <= 0:
        return 0

    status = browse_budget_status(
        conn
    )

    used = int(
        status.get("used")
        or 0
    )

    reset_seconds = status.get(
        "reset_seconds"
    )

    if reset_seconds is None:
        # Conservative fallback: one normal batch.
        return min(
            backlog,
            MAX_BACKFILL_DETAILS_PER_CYCLE
        )

    reserve = current_search_reserve()

    detail_cutoff = (
        DAILY_SAFETY_LIMIT
        - EMERGENCY_RESERVE
        - reserve
    )

    usable_calls = max(
        0,
        detail_cutoff
        - used
    )

    if usable_calls <= 0:
        return 0

    # Work out how many normal polling opportunities remain before
    # eBay resets the quota. Always count the current cycle.
    cycle_seconds = max(
        60,
        int(POLL_NORMAL)
    )

    cycles_remaining = max(
        1,
        (
            int(reset_seconds)
            + cycle_seconds
            - 1
        )
        // cycle_seconds
    )

    # Spread currently usable quota evenly over the remaining cycles.
    paced = max(
        1,
        (
            usable_calls
            + cycles_remaining
            - 1
        )
        // cycles_remaining
    )

    # A very large backlog benefits from modest acceleration, but never
    # more than 25% above the even-pacing figure. This lets unused quota
    # actually drain stale work without exhausting the day early.
    if backlog >= 1000:
        paced = max(
            paced,
            int(
                round(
                    paced * 1.25
                )
            )
        )

    allowance = max(
        DYNAMIC_REANALYSIS_MIN_PER_CYCLE,
        paced
    )

    allowance = min(
        DYNAMIC_REANALYSIS_MAX_PER_CYCLE,
        allowance,
        usable_calls,
        backlog
    )

    return max(
        0,
        int(allowance)
    )


def drain_reanalysis_queue(
    conn,
    token,
    maximum=None
):
    if maximum is None:
        maximum = MAX_BACKFILL_DETAILS_PER_CYCLE
    """
    Explicitly drain active listings created by older classifier versions.

    This is separate from newest-search processing and ordinary availability
    housekeeping so classifier migrations make deterministic progress.
    """

    remaining_before = conn.execute(
        """
        SELECT COUNT(*) AS n
        FROM listings
        WHERE COALESCE(active,1)=1
          AND (
                classifier_version IS NULL
                OR classifier_version<>?
                OR COALESCE(rules_revision,0)<>?
              )
        """,
        (
            CLASSIFIER_VERSION,
            current_rules_revision(conn),
        )
    ).fetchone()["n"]

    if remaining_before <= 0:
        print("Reanalysis queue: complete")
        return 0

    if ebay_detail_rate_limited():
        print(
            "Reanalysis queue: paused — eBay 429 cooldown active; "
            f"{remaining_before} remaining"
        )
        return 0

    if not can_detail(conn):
        print(
            "Reanalysis queue: paused — detail budget reserve reached; "
            f"{remaining_before} remaining"
        )
        return 0

    rows = conn.execute(
        """
        SELECT *
        FROM listings
        WHERE COALESCE(active,1)=1
          AND (
                classifier_version IS NULL
                OR classifier_version<>?
                OR COALESCE(rules_revision,0)<>?
              )
        ORDER BY
            CASE
                WHEN estimated_value IS NULL
                THEN 0
                ELSE 1
            END,
            CASE
                WHEN valuation_basis='REANALYSIS_REQUIRED'
                THEN 0
                ELSE 1
            END,
            first_seen ASC,
            COALESCE(
                availability_checked_at,
                '1970-01-01'
            ) ASC
        LIMIT ?
        """,
        (
            CLASSIFIER_VERSION,
            current_rules_revision(conn),
            maximum,
        )
    ).fetchall()

    attempted = 0
    completed = 0
    inactivated = 0
    errors = 0

    for row in rows:

        if attempted >= maximum:
            break

        if ebay_detail_rate_limited():
            break

        if not can_detail(conn):
            break

        record_api_call(
            conn,
            "BROWSE",
            "REANALYSIS"
        )

        attempted += 1

        try:
            state, detail = ebay_get_item(
                token,
                row["item_id"]
            )

        except EbayRateLimited:
            print(
                "Reanalysis queue: 429 encountered; "
                "queue paused"
            )
            break

        except Exception as exc:
            errors += 1
            print(
                "Reanalysis queue: error fetching "
                f"{row['item_id']}: {exc!r}"
            )
            continue

        now = iso_now()

        conn.execute(
            """
            UPDATE listings
            SET availability_checked_at=?
            WHERE item_id=?
            """,
            (
                now,
                row["item_id"],
            )
        )

        if state == "INACTIVE":

            conn.execute(
                """
                UPDATE listings
                SET
                    active=0,
                    inactive_since=?,
                    inactive_reason='ENDED_OR_UNAVAILABLE'
                WHERE item_id=?
                """,
                (
                    now,
                    row["item_id"],
                )
            )

            conn.commit()

            inactivated += 1
            continue

        if state != "ACTIVE" or not detail:
            conn.commit()
            continue

        item = analyse_listing(
            conn,
            token,
            detail,
            fetch_detail=False,
            supplied_detail=detail
        )

        if not item:
            conn.commit()
            continue

        save_listing(
            conn,
            item
        )

        # Force valuation to be recalculated using the freshly
        # classified identity/specification.
        conn.execute(
            """
            UPDATE listings
            SET
                estimated_value=NULL,
                valuation_q1=NULL,
                valuation_q3=NULL,
                comparable_count=NULL,
                valuation_confidence=NULL,
                undervaluation_gbp=NULL,
                undervaluation_pct=NULL,
                deal_score=NULL,
                valuation_basis=NULL,
                valuation_research_at=NULL
            WHERE item_id=?
            """,
            (
                row["item_id"],
            )
        )

        conn.commit()

        completed += 1

    remaining_after = conn.execute(
        """
        SELECT COUNT(*) AS n
        FROM listings
        WHERE COALESCE(active,1)=1
          AND (
                classifier_version IS NULL
                OR classifier_version<>?
                OR COALESCE(rules_revision,0)<>?
              )
        """,
        (
            CLASSIFIER_VERSION,
            current_rules_revision(conn),
        )
    ).fetchone()["n"]

    print(
        "Reanalysis queue: "
        f"attempted {attempted}; "
        f"completed {completed}; "
        f"inactivated {inactivated}; "
        f"errors {errors}; "
        f"{remaining_after} remaining"
    )

    return completed


def active_bin_rechecks_for_cycle(conn):
    """
    Prioritise classifier reanalysis over routine BIN availability checks.

    While any active listings still use an old classifier version, perform
    only a small number of ordinary BIN checks per cycle. Automatically
    restore the normal rate once reanalysis completes.
    """
    remaining = conn.execute("""
        SELECT COUNT(*) AS n
        FROM listings
        WHERE COALESCE(active, 1)=1
          AND (
                classifier_version IS NULL
                OR classifier_version<>?
                OR COALESCE(rules_revision,0)<>?
              )
    """, (
        CLASSIFIER_VERSION,
        current_rules_revision(conn),
    )).fetchone()["n"]

    if remaining:
        return ACTIVE_BIN_RECHECKS_DURING_REANALYSIS

    return ACTIVE_BIN_RECHECKS_PER_CYCLE


def recheck_active_bin_listings(conn, token, maximum=None):
    if maximum is None:
        maximum = ACTIVE_BIN_RECHECKS_PER_CYCLE
    """
    Recheck a rotating set of active BIN/Best Offer listings.

    This catches listings that sold or ended early, before their originally
    advertised end_date. We retain the DB row but mark it inactive so it
    disappears from the live-deals dashboard.

    We deliberately do not hide a listing on a timeout, rate-limit, 5xx, or
    ambiguous API error.
    """
    cutoff = (
        utcnow() - timedelta(minutes=ACTIVE_BIN_RECHECK_MIN_AGE_MINUTES)
    ).isoformat()

    interval_cutoff = (
        utcnow() - timedelta(minutes=ACTIVE_BIN_RECHECK_INTERVAL_MINUTES)
    ).isoformat()

    rows = conn.execute("""
        SELECT *
        FROM listings
        WHERE COALESCE(active, 1)=1
          AND first_seen <= ?
          AND (
                availability_checked_at IS NULL
                OR availability_checked_at <= ?
              )
        ORDER BY
            CASE
                WHEN valuation_basis='REANALYSIS_REQUIRED' THEN 0
                ELSE 1
            END,
            CASE WHEN deal_score IS NULL THEN 1 ELSE 0 END,
            deal_score DESC,
            COALESCE(availability_checked_at, '1970-01-01') ASC,
            first_seen ASC
        LIMIT 100
    """, (cutoff, interval_cutoff)).fetchall()

    checked = 0
    inactivated = 0

    if ebay_detail_rate_limited():
        print(
            "Listing housekeeping: eBay detail calls skipped "
            "because 429 cooldown is active"
        )
        return checked, inactivated

    for row in rows:
        if checked >= maximum or not can_detail(conn):
            break
        if (
            not needs_reanalysis(row)
            and not is_fixed_price_listing(row)
        ):
            continue

        record_api_call(conn, "BROWSE", "GET_ITEM")

        try:
            state, detail = ebay_get_item(
                token,
                row["item_id"]
            )

        except EbayRateLimited as exc:
            print(
                "Listing housekeeping: "
                f"{exc}; stopping detail calls for this cycle"
            )
            break

        except urllib.error.HTTPError:
            raise

        checked += 1
        now = iso_now()

        conn.execute(
            "UPDATE listings SET availability_checked_at=? WHERE item_id=?",
            (now, row["item_id"]),
        )

        if state == "INACTIVE":
            conn.execute("""
                UPDATE listings
                SET active=0,
                    inactive_since=COALESCE(inactive_since, ?),
                    inactive_reason='EBAY_NO_LONGER_AVAILABLE'
                WHERE item_id=?
            """, (now, row["item_id"]))
            inactivated += 1
            print(
                "Listing housekeeping: BIN no longer available -> "
                f"{row['item_id']} {row['title'] or ''}"
            )
        elif state == "ACTIVE" and detail:
            # Reuse the detail already fetched, including its shipping quote.
            # This also brings older active listings through version migrations
            # even when they no longer appear among the newest search results.
            was_reanalysis = needs_reanalysis(row)

            item = analyse_listing(conn, token, detail, fetch_detail=False, supplied_detail=detail)
            if item:
                save_listing(conn, item)

                if was_reanalysis:
                    conn.execute(
                        """
                        UPDATE listings
                        SET
                            estimated_value=NULL,
                            valuation_q1=NULL,
                            valuation_q3=NULL,
                            comparable_count=NULL,
                            valuation_confidence=NULL,
                            undervaluation_gbp=NULL,
                            undervaluation_pct=NULL,
                            deal_score=NULL,
                            valuation_basis=NULL,
                            valuation_research_at=NULL
                        WHERE item_id=?
                        """,
                        (row["item_id"],),
                    )
                    conn.commit()
        elif state == "ERROR":
            print(
                "Listing housekeeping: availability check error -> "
                f"{row['item_id']}: {detail}"
            )

        conn.commit()

    if checked:
        print(
            f"Listing housekeeping: rechecked {checked} active BIN/Best Offer "
            f"listings; marked {inactivated} inactive"
        )
    return checked, inactivated



# ============================================================
# DASHBOARD IMAGE BACKFILL
# ============================================================

def backfill_dashboard_images(
    conn,
    token,
    maximum=IMAGE_BACKFILL_PER_CYCLE
):
    """
    Populate eBay images only for listings currently eligible
    to appear on Laptop Lander.

    Thumbnail calls may use a dedicated allowance beyond the
    normal detail-call reserve, but never consume the emergency
    reserve.
    """

    already_used = operation_usage(
        conn,
        "IMAGE_BACKFILL"
    )

    allowance_left = max(
        0,
        IMAGE_BACKFILL_DAILY_ALLOWANCE
        - already_used
    )

    if allowance_left <= 0:
        return 0

    maximum = min(
        maximum,
        allowance_left
    )

    rows = conn.execute("""
        SELECT item_id
        FROM listings
        WHERE COALESCE(active, 1)=1
          AND estimated_value IS NOT NULL
          AND undervaluation_gbp >= ?
          AND undervaluation_pct >= ?
          AND (
                deal_score > 0
                OR (
                    deal_score IS NULL
                    AND buying_options LIKE '%"AUCTION"%'
                )
              )
          AND (
                image_url IS NULL
                OR trim(image_url)=''
              )
        ORDER BY
            CASE
                WHEN deal_score IS NULL THEN 1
                ELSE 0
            END,
            deal_score DESC,
            undervaluation_gbp DESC,
            first_seen DESC
        LIMIT ?
    """, (
        MIN_UNDERVALUE_GBP,
        MIN_UNDERVALUE_PCT,
        maximum
    )).fetchall()

    if not rows:
        return 0

    updated = 0
    attempted = 0

    for row in rows:

        remaining = (
            DAILY_SAFETY_LIMIT
            - browse_usage_today(conn)
        )

        # Thumbnail override is allowed to use the normal search
        # reserve, but never the emergency reserve.
        if remaining <= EMERGENCY_RESERVE:
            print(
                "Dashboard image backfill: "
                "stopped at emergency reserve"
            )
            break

        if attempted >= allowance_left:
            break

        try:
            record_api_call(
                conn,
                "BROWSE",
                "IMAGE_BACKFILL"
            )

            state, detail = ebay_get_item(
                token,
                row["item_id"]
            )

            attempted += 1

        except Exception as exc:
            print(
                "Dashboard image backfill:",
                row["item_id"],
                type(exc).__name__,
                str(exc)[:160]
            )
            continue

        if state != "ACTIVE" or not detail:
            continue

        image_url = (
            (detail.get("image") or {})
            .get("imageUrl")
            or
            (
                (
                    (
                        detail.get("thumbnailImages")
                        or [{}]
                    )[0]
                    or {}
                )
                .get("imageUrl")
            )
        )

        if not image_url:
            continue

        conn.execute(
            """
            UPDATE listings
            SET image_url=?
            WHERE item_id=?
            """,
            (
                image_url,
                row["item_id"]
            )
        )

        updated += 1

    if updated:
        conn.commit()

    print(
        f"Dashboard image backfill: "
        f"{updated}/{attempted} populated "
        f"({already_used + attempted}/"
        f"{IMAGE_BACKFILL_DAILY_ALLOWANCE} "
        f"thumbnail calls today)"
    )

    return updated


# ============================================================
# ACTIVE LISTING HOUSEKEEPING
# ============================================================

def mark_ended_listings(conn):
    """
    Hide listings whose eBay end time has passed, while retaining their DB
    history/valuation evidence. This does not assume that every ended listing
    sold; it simply marks it inactive.
    """
    now = iso_now()
    cur = conn.execute("""
        UPDATE listings
        SET active=0,
            inactive_since=COALESCE(inactive_since, ?)
        WHERE COALESCE(active, 1)=1
          AND end_date IS NOT NULL
          AND end_date <> ''
          AND end_date < ?
    """, (now, now))
    conn.commit()
    if cur.rowcount:
        print(f"Listing housekeeping: marked {cur.rowcount} ended listings inactive")
    return cur.rowcount


# ============================================================
# POLLER
# ============================================================

def run_cycle(
    conn,
    cycle
):
    refresh_runtime_settings(conn)
    refresh_classifier_rules(conn)
    print()
    print("#" * 90)

    print(
        f"{iso_now()} "
        f"cycle {cycle}"
    )

    token = get_token()

    before_search = (
        browse_usage_today(
            conn
        )
    )

    result = ebay_search(
        conn,
        token
    )

    summaries = (
        result.get(
            "itemSummaries"
        )
        or []
    )

    window_start = result.get("_window_start")
    window_end = result.get("_window_end")
    search_pages = result.get("_pages", 1)

    print(
        f"eBay timestamp window: "
        f"{window_start.isoformat() if window_start else '?'} "
        f"-> "
        f"{window_end.isoformat() if window_end else '?'}"
    )

    print(
        f"eBay returned "
        f"{len(summaries)} listings "
        f"across {search_pages} search page(s)"
    )

    if result.get("_catching_up"):
        print(
            "Timestamp discovery: catching up "
            "from an earlier checkpoint"
        )

    new_count = 0
    known_count = 0
    excluded_new = 0
    details = 0

    for summary in summaries:

        condition = normalise(
            summary.get(
                "condition"
            )
        )

        if is_genuinely_new(
            condition
        ):
            excluded_new += 1
            continue

        item_id = summary.get(
            "itemId"
        )

        existing = conn.execute("""
            SELECT *
            FROM listings
            WHERE item_id=?
        """, (
            item_id,
        )).fetchone()

        if existing:

            known_count += 1

            update_known_summary(
                conn,
                summary
            )

            # Reanalyse old records created by previous
            # classifier versions while we have budget.
            if (
                needs_reanalysis(
                    existing
                )
                and details
                < MAX_BACKFILL_DETAILS_PER_CYCLE
                and can_detail(
                    conn
                )
                and not ebay_detail_rate_limited()
            ):

                item = analyse_listing(
                    conn,
                    token,
                    summary,
                    fetch_detail=True
                )

                if item:

                    save_listing(
                        conn,
                        item
                    )

                    details += 1

            continue

        fetch_detail = (
            details
            < MAX_DETAIL_CALLS_PER_CYCLE
            and can_detail(
                conn
            )
        )

        item = analyse_listing(
            conn,
            token,
            summary,
            fetch_detail=fetch_detail
        )

        if not item:
            continue

        if (
            item["detail_status"]
            == "COMPLETE"
        ):
            details += 1

        inserted = save_listing(
            conn,
            item
        )

        if inserted:

            new_count += 1

            display_listing(
                item
            )

    mark_ended_listings(conn)
    # Prioritise thumbnails for visible Laptop Lander deals.
    backfill_dashboard_images(
        conn,
        token
    )

    # Drain old classifier versions before ordinary availability
    # housekeeping consumes the remaining detail-call budget.
    #
    # Pace this work against eBay's real remaining quota window rather
    # than using a fixed 40-call ceiling every cycle.
    reanalysis_remaining = conn.execute(
        """
        SELECT COUNT(*) AS n
        FROM listings
        WHERE COALESCE(active,1)=1
          AND (
                classifier_version IS NULL
                OR classifier_version<>?
              )
        """,
        (
            CLASSIFIER_VERSION,
        )
    ).fetchone()["n"]

    reanalysis_allowance = (
        dynamic_reanalysis_allowance(
            conn,
            reanalysis_remaining
        )
    )

    budget_status = browse_budget_status(
        conn
    )

    print(
        "Dynamic reanalysis budget: "
        f"{reanalysis_allowance} this cycle; "
        f"{reanalysis_remaining} queued; "
        f"{budget_status['used']} used; "
        f"{current_search_reserve()} search reserve; "
        f"{_format_quota_countdown(budget_status['reset_seconds']) if budget_status.get('reset_seconds') is not None else 'unknown'} until reset"
    )

    if reanalysis_allowance > 0:
        drain_reanalysis_queue(
            conn,
            token,
            reanalysis_allowance
        )
    elif reanalysis_remaining:
        print(
            "Reanalysis queue: paused — "
            "no safely allocatable detail quota this cycle"
        )

    recheck_active_bin_listings(
        conn,
        token,
        active_bin_rechecks_for_cycle(conn)
    )

    repair_v078_model_and_sold_cache(conn)
    repair_v080_valuation_cache(conn)

    print()
    print("Collecting sold-market evidence...")

    research_searches = collect_needed_sold_data(
        conn,
        PRODUCT_RESEARCH_SEARCHES_PER_CYCLE
    )

    print(
        f"Product Research searches: "
        f"{research_searches}"
    )

    print()
    print(
        "Recalculating valuations..."
    )

    revalue_all(
        conn
    )

    total_api = browse_usage_today(
        conn
    )

    print()
    print("-" * 90)

    print(
        f"Returned             : "
        f"{len(summaries)}"
    )

    print(
        f"Known                : "
        f"{known_count}"
    )

    print(
        f"New                  : "
        f"{new_count}"
    )

    print(
        f"Brand-new excluded   : "
        f"{excluded_new}"
    )

    print(
        f"Detail calls cycle   : "
        f"{details}"
    )

    print()
    print(
        f"Browse API today     : "
        f"{total_api} / "
        f"{DAILY_SAFETY_LIMIT}"
    )

    print(
        f"Search calls today   : "
        f"{operation_usage(conn, 'SEARCH')}"
    )

    print(
        f"Detail calls today   : "
        f"{operation_usage(conn, 'GET_ITEM')}"
    )

    print(
        f"Budget used          : "
        f"{budget_percentage(conn):.1f}%"
    )

    print(
        f"Next poll            : "
        f"{polling_interval(conn)}s"
    )

    print("-" * 90)


# ============================================================
# PERSISTENT LOGGING
# ============================================================

class Tee:
    def __init__(self, *streams):
        self.streams = streams

    def write(self, data):
        for stream in self.streams:
            try:
                stream.write(data)
                stream.flush()
            except Exception:
                pass
        return len(data)

    def flush(self):
        for stream in self.streams:
            try:
                stream.flush()
            except Exception:
                pass


_log_handle = None


def start_persistent_logging():
    global _log_handle
    if _log_handle is not None:
        return
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        _log_handle = open(LOG_FILE, "a", buffering=1, encoding="utf-8")
        sys.stdout = Tee(sys.__stdout__, _log_handle)
        sys.stderr = Tee(sys.__stderr__, _log_handle)
    except Exception as exc:
        print("Could not enable persistent logging:", repr(exc))


# ============================================================
# MAIN
# ============================================================

def main():
    start_persistent_logging()
    init_db()
    with connect_db() as migration_conn:
        repair_v078_model_and_sold_cache(migration_conn)
        repair_v080_valuation_cache(migration_conn)
    migration_conn.close()

    print(
        f"Laptop Lander "
        f"v{APP_VERSION}"
    )

    if not os.path.exists(PRODUCT_RESEARCH_SESSION_STATE):
        _session_state_update(
            status="UNKNOWN",
            message="Waiting for first Product Research check",
        )

    start_dashboard()
    start_product_research_session_monitor()
    start_cpu_benchmark_refresh_worker()

    cycle = 0

    while True:

        cycle += 1

        conn = connect_db()

        try:

            if not can_search(
                conn
            ):

                print(
                    "API safety budget reached. "
                    "Sleeping 30 minutes."
                )

                conn.close()

                time.sleep(
                    1800
                )

                continue

            run_cycle(
                conn,
                cycle
            )

            sleep_for = (
                polling_interval(
                    conn
                )
            )

        except KeyboardInterrupt:

            conn.close()
            raise

        except Exception as exc:

            print(
                "CYCLE ERROR:",
                repr(exc)
            )

            sleep_for = 120

        finally:

            try:
                conn.close()
            except Exception:
                pass

        time.sleep(
            sleep_for
        )


if __name__ == "__main__":
    main()
