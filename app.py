# eBay Laptop Hunter v0.7.9
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

from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


# ============================================================
# VERSION / CONFIG
# ============================================================

VERSION = "0.7.9"

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
ACTIVE_BIN_RECHECKS_PER_CYCLE = 10
ACTIVE_BIN_RECHECK_MIN_AGE_MINUTES = 10
ACTIVE_BIN_RECHECK_INTERVAL_MINUTES = 15

CATEGORY = "177"
MARKETPLACE = "EBAY_GB"

# We only need the newest listings for routine discovery.
SEARCH_LIMIT = 50

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
DAILY_SAFETY_LIMIT = 4500

SEARCH_RESERVE = 1000
EMERGENCY_RESERVE = 250

MAX_DETAIL_CALLS_PER_CYCLE = 40
MAX_BACKFILL_DETAILS_PER_CYCLE = 10

POLL_NORMAL = 90
POLL_60_PERCENT = 120
POLL_75_PERCENT = 180
POLL_85_PERCENT = 300
POLL_90_PERCENT = 600

# Valuation
MIN_COMPARABLES = 3
HIGH_CONFIDENCE_COMPARABLES = 12
MEDIUM_CONFIDENCE_COMPARABLES = 6

# Don't use ancient active observations indefinitely.
COMPARABLE_MAX_AGE_DAYS = 30


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

        "usbc_pd_confidence": "TEXT",
        "usbc_pd_source": "TEXT",
        "usbc_pd_evidence": "TEXT",
        "usbc_pd_watts": "INTEGER",

        "win11_confidence": "TEXT",
        "win11_source": "TEXT",
        "win11_evidence": "TEXT",

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

    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_sold_model
        ON sold_comparables (brand, model)
    """)

    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_sold_cpu
        ON sold_comparables (cpu, ram_gb, storage_gb)
    """)

    conn.commit()
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


def browse_usage_today(conn):
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


def can_search(conn):
    return (
        browse_usage_today(conn)
        <
        DAILY_SAFETY_LIMIT
        - EMERGENCY_RESERVE
    )


def can_detail(conn):
    remaining = (
        DAILY_SAFETY_LIMIT
        - browse_usage_today(conn)
    )

    return (
        remaining
        >
        SEARCH_RESERVE
        + EMERGENCY_RESERVE
    )


def budget_percentage(conn):
    return (
        browse_usage_today(conn)
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

    with urllib.request.urlopen(
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
                "application/json"
        }
    )

    # Deliberately count attempts.
    record_api_call(
        conn,
        "BROWSE",
        operation
    )

    with urllib.request.urlopen(
        request,
        timeout=30
    ) as response:

        return json.load(
            response
        )


def ebay_search(
    conn,
    token
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
                "itemLocationCountry:GB",

            "sort":
                "newlyListed",

            "limit":
                str(
                    SEARCH_LIMIT
                )
        }
    )


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
    return (
        safe_float(
            item.get(
                "price",
                {}
            ).get(
                "value"
            )
        )
        or 0.0
    )


def shipping_price(item):
    options = (
        item.get(
            "shippingOptions"
        )
        or []
    )

    prices = []

    for option in options:

        value = safe_float(
            option.get(
                "shippingCost",
                {}
            ).get(
                "value"
            )
        )

        if value is not None:
            prices.append(
                value
            )

    if not prices:
        return 0.0

    return min(prices)


# ============================================================
# ASPECTS
# ============================================================

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
    "fujitsu": "Fujitsu"
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
    r"\bLatitude\s+"
    r"(?:E)?\d{4}\b",

    r"\bLatitude\s+"
    r"\d{4}\s+Detachable\b",

    r"\bVostro\s+\d{4}\b",

    r"\bInspiron\s+\d{4}\b",

    r"\bPrecision\s+"
    r"\d{4}\b",

    # XPS generation/model identifiers matter enormously to value.
    # Examples: XPS 13 L322X, XPS 13 9320, XPS 15 9520.
    r"\bXPS\s+(?:13|15|17)\s+(?:L\d{3,4}X|\d{4})\b",

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

    r"\bIdeaPad\s+"
    r"(?:Slim\s+)?"
    r"[A-Za-z0-9-]+"
    r"(?:\s+[A-Za-z0-9-]+)?\b",

    # Microsoft
    r"\bSurface\s+Pro\s+"
    r"\d{1,2}\b",

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

    # Strong known-specific shapes.
    patterns = (
        r"^Latitude\s+(?:E)?\d{4}(?:\s+Detachable)?$",
        r"^(?:Inspiron|Vostro|Precision)\s+\d{4}$",
        r"^XPS\s+(?:13|15|17)\s+(?:L\d{3,4}X|\d{4})$",
        r"^(?:ProBook|EliteBook)\s+\d{3}\s+G\d{1,2}$",
        r"^(?:240|245|250|255|340|348|430|440|450|455|470)\s+G\d{1,2}$",
        r"^ThinkPad\s+(?:(?:T|X|E|L|P)\d{2,3}[A-Za-z]?|X1\s+(?:Carbon|Yoga))(?:\s+Gen\s+\d+)?$",
        r"^IdeaPad\s+(?:Slim\s+)?[A-Za-z0-9-]+(?:\s+[A-Za-z0-9-]+)?$",
        r"^Surface\s+(?:Pro|Laptop)\s+\d{1,2}$",
    )
    if any(re.fullmatch(p, value, re.I) for p in patterns):
        return True

    # Generic safety net: a model normally needs a distinguishing number/code.
    return bool(re.search(r"\d", value)) and len(value) >= 4


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
    for pattern in MODEL_PATTERNS:

        match = re.search(
            pattern,
            title,
            re.I
        )

        if match:
            return clean_model(
                match.group(0)
            )

    if aspect_model:

        # Item specifics often contain a better generation-qualified model
        # than the title. Run the same recognizers over the aspect first.
        for pattern in MODEL_PATTERNS:
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

        return cpu_result(
            f"AMD Ryzen {tier} "
            f"{number}{suffix}",
            "AMD",
            f"Ryzen {tier}",
            int(number[0]),
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

    for value in processor_values:

        result = parse_cpu(
            value,
            "EBAY_PROCESSOR_ASPECT"
        )

        if result:
            return result

    title = summary.get(
        "title",
        ""
    )

    result = parse_cpu(
        title,
        "TITLE"
    )

    if result:
        return result

    text = all_text(
        summary,
        detail
    )

    result = parse_cpu(
        text,
        "ITEM_DETAILS"
    )

    if result:
        return result

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

def windows11_assessment(cpu):
    manufacturer = cpu.get(
        "manufacturer"
    )

    family = (
        cpu.get(
            "family"
        )
        or ""
    )

    generation = cpu.get(
        "generation"
    )

    name = (
        cpu.get(
            "name"
        )
        or ""
    )

    if manufacturer == "Intel":

        if family.startswith(
            "Core Ultra"
        ):
            return True

        if (
            family.startswith(
                "Core i"
            )
            or family == "Core"
        ):

            if generation is not None:
                return generation >= 8

        if (
            "N95" in name
            or "N97" in name
            or "N100" in name
            or "N150" in name
            or "N200" in name
            or "N250" in name
            or "N300" in name
            or "N305" in name
        ):
            return True

    if manufacturer == "AMD":

        match = re.search(
            r"Ryzen\s+[3579]\s+"
            r"(\d{4})",
            name,
            re.I
        )

        if match:

            # This remains deliberately a broad preliminary
            # family assessment. Exact Microsoft SKU-list
            # matching should replace it later.
            return (
                int(
                    match.group(1)
                )
                >= 3000
            )

    return None




def cpu_capability_key(cpu_name):
    if not cpu_name:
        return None
    return re.sub(r"\s+", "", normalise(cpu_name).lower())


def cached_win11_capability(conn, cpu_name):
    key = cpu_capability_key(cpu_name)
    if not key:
        return None
    row = conn.execute("""
        SELECT win11_approved, confidence, source, evidence
        FROM cpu_capabilities
        WHERE cpu_key=?
    """, (key,)).fetchone()
    if not row or row["win11_approved"] is None:
        return None
    return {
        "value": bool(row["win11_approved"]),
        "confidence": row["confidence"] or "VERIFIED",
        "source": row["source"] or "CPU_CAPABILITY_CACHE",
        "evidence": row["evidence"] or "Previously verified Windows 11 CPU eligibility",
    }


def queue_win11_verification(conn, cpu):
    name = cpu.get("name") if cpu else None
    key = cpu_capability_key(name)
    if not key:
        return
    capability_key_value = "win11:" + key
    now = iso_now()
    conn.execute("""
        INSERT INTO capability_queue(
            capability_key, brand, model, capability, status,
            attempts, first_seen
        )
        VALUES (?, NULL, ?, 'WIN11_APPROVED', 'PENDING', 0, ?)
        ON CONFLICT(capability_key) DO NOTHING
    """, (capability_key_value, name, now))
    conn.commit()


def win11_approved_assessment(conn, cpu):
    """
    'Approved' means supported hardware eligibility, not merely that a seller
    has installed Windows 11. Cached Microsoft-backed determinations are
    VERIFIED. Existing CPU-generation logic is retained as inferred evidence.
    """
    name = cpu.get("name") if cpu else None
    cached = cached_win11_capability(conn, name)
    if cached:
        return cached

    inferred = windows11_assessment(cpu)

    if inferred is True:
        queue_win11_verification(conn, cpu)
        return {
            "value": True,
            "confidence": "PROBABLE",
            "source": "CPU_SUPPORT_RULE",
            "evidence": "CPU family/generation indicates Windows 11 support; exact CPU verification pending",
        }

    if inferred is False:
        queue_win11_verification(conn, cpu)
        return {
            "value": False,
            "confidence": "PROBABLE",
            "source": "CPU_SUPPORT_RULE",
            "evidence": "CPU family/generation indicates it is outside supported Windows 11 CPU generations; exact CPU verification pending",
        }

    queue_win11_verification(conn, cpu)
    return {
        "value": None,
        "confidence": "UNKNOWN",
        "source": "UNVERIFIED_CPU",
        "evidence": "Exact Windows 11 approved CPU status has not yet been verified",
    }


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
    """Queue a precise unknown model; no web scraping is performed here."""
    key = capability_key(brand, model)
    if not key or not precise_model_for_capability(brand, model):
        return

    now = iso_now()
    conn.execute("""
        INSERT INTO capability_queue(
            capability_key, brand, model, capability, status,
            attempts, first_seen
        )
        VALUES (?, ?, ?, 'USB_C_PD', 'PENDING', 0, ?)
        ON CONFLICT(capability_key) DO NOTHING
    """, (key, brand, model, now))
    conn.commit()


def usb_c_pd_assessment(conn, brand, model, text, detail):
    """
    Evidence hierarchy:
      1. cached verified model capability
      2. explicit item-specific charging / power-input evidence
      3. explicit listing charging language
      4. known model-family hint (PROBABLE, not VERIFIED)
      5. UNKNOWN + verification queue

    Thunderbolt presence alone is deliberately NOT proof of charging input.
    """
    cached = cached_usb_c_capability(conn, brand, model)
    if cached:
        return cached

    aspects = aspects_dict(detail)

    # Strong item-specific evidence. We require charging/input semantics,
    # not merely "USB Type-C" or Thunderbolt.
    aspect_lines = []
    for name, values in aspects.items():
        for value in values:
            aspect_lines.append(f"{name}: {value}")
    aspect_text = normalise(" ".join(aspect_lines))

    explicit_positive = [
        r"\bUSB[- ]?C\s+(?:PD|Power Delivery|Charging)\b",
        r"\bUSB\s+Type[- ]?C\s+(?:PD|Power Delivery|Charging)\b",
        r"\bcharge[sd]?\s+(?:via|through|over|with)\s+USB[- ]?C\b",
        r"\bUSB[- ]?C\s+charger\b",
        r"\bAC\s+adapter\b.{0,50}\bUSB\s*(?:Type[- ]?)?C\b",
        r"\bpower\s+(?:input|connector|adapter)\b.{0,50}\bUSB\s*(?:Type[- ]?)?C\b",
    ]
    explicit_negative = [
        r"\bdoes\s+not\s+charge\s+(?:via|through|over)\s+USB[- ]?C\b",
        r"\bno\s+USB[- ]?C\s+charging\b",
        r"\bUSB[- ]?C\s+(?:is\s+)?(?:data|display)\s+only\b",
    ]

    for pattern in explicit_negative:
        m = re.search(pattern, aspect_text, re.I)
        if m:
            return {
                "value": False,
                "confidence": "HIGH",
                "source": "EBAY_ITEM_SPECIFICS",
                "evidence": normalise(m.group(0)),
                "watts": None,
            }

    for pattern in explicit_positive:
        m = re.search(pattern, aspect_text, re.I)
        if m:
            watts = None
            wm = re.search(r"\b(\d{2,3})\s*W\b", aspect_text, re.I)
            if wm:
                watts = int(wm.group(1))
            return {
                "value": True,
                "confidence": "HIGH",
                "source": "EBAY_ITEM_SPECIFICS",
                "evidence": normalise(m.group(0)),
                "watts": watts,
            }

    for pattern in explicit_negative:
        m = re.search(pattern, text, re.I)
        if m:
            return {
                "value": False,
                "confidence": "MEDIUM",
                "source": "EBAY_LISTING_TEXT",
                "evidence": normalise(m.group(0)),
                "watts": None,
            }

    for pattern in explicit_positive:
        m = re.search(pattern, text, re.I)
        if m:
            watts = None
            wm = re.search(r"\b(\d{2,3})\s*W\b", text, re.I)
            if wm:
                watts = int(wm.group(1))
            return {
                "value": True,
                "confidence": "MEDIUM",
                "source": "EBAY_LISTING_TEXT",
                "evidence": normalise(m.group(0)),
                "watts": watts,
            }

    # Thunderbolt 3/4 is intentionally only contextual evidence now.
    # A known family match is PROBABLE and must not masquerade as VERIFIED.
    model_text = normalise(f"{brand or ''} {model or ''}")
    for pattern in USB_PD_MODELS:
        if re.search(pattern, model_text, re.I):
            queue_capability_verification(conn, brand, model)
            return {
                "value": True,
                "confidence": "PROBABLE",
                "source": "KNOWN_MODEL_FAMILY",
                "evidence": "Known USB-C-charging family; manufacturer verification pending",
                "watts": None,
            }

    queue_capability_verification(conn, brand, model)
    return {
        "value": None,
        "confidence": "UNKNOWN",
        "source": "UNVERIFIED",
        "evidence": "No reliable USB-C power-input evidence found",
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
        "for parts",
        "parts or not working"
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
        "dent"
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
    fetch_detail=True
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

    detail = {}
    detail_status = "NOT_REQUESTED"

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
            title,
            condition
        )
    )

    price = item_price(
        summary
    )

    postage = shipping_price(
        summary
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
            price + postage,

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
            VERSION
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
            price + postage,

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
    with urllib.request.urlopen(req, timeout=45) as response:
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


def sold_search_queries(row):
    """Use exact model only when it is a real model identity, not a family."""
    brand = normalise(row["brand"])
    model = valuation_model(brand, row["model"])
    cpu = normalise(row["cpu"])
    ram = row["ram_gb"]
    storage = row["storage_gb"]
    queries = []

    if brand and model and precise_model_for_valuation(brand, model):
        queries.append(f"{brand} {model}")
    elif brand and cpu:
        q = f"{brand} {cpu}"
        if ram:
            q += f" {ram}GB"
        if storage:
            q += f" {storage}GB"
        queries.append(q)
    elif cpu:
        q = cpu
        if ram:
            q += f" {ram}GB"
        if storage:
            q += f" {storage}GB"
        queries.append(q)

    return list(dict.fromkeys(q for q in queries if research_query_key(q)))


def sold_search_is_fresh(conn, key):
    row = conn.execute(
        "SELECT searched_at, status FROM sold_searches WHERE query_key=?",
        (key,),
    ).fetchone()
    if not row or not row["searched_at"] or row["status"] != "OK":
        return False
    try:
        age = utcnow() - datetime.fromisoformat(row["searched_at"])
        return age.total_seconds() < PRODUCT_RESEARCH_CACHE_HOURS * 3600
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
    if avg_postage is None and result.get("freeshipping"):
        avg_postage = 0.0
    if avg_postage is None:
        avg_postage = 0.0

    units = _research_value(result.get("itemssold"))
    total_sales = _research_value(result.get("totalsales"))
    last_sold = _research_text(result.get("datelastsold"))
    formats = _research_text(listing.get("formatList") or result.get("formatList"))

    if not item_id:
        # Stable enough for a cached research row when eBay omits itemId.
        item_id = "title:" + research_query_key(title)[:180]

    cpu = parse_cpu(title, "SOLD_TITLE") or cpu_result()
    brand = identify_brand(title, {})
    model = identify_model(title, {})
    ram = identify_ram(title, {})
    storage = identify_storage(title, {})

    return {
        "item_id": item_id,
        "title": title,
        "brand": brand,
        "model": model,
        "cpu": cpu.get("name"),
        "cpu_generation": cpu.get("generation"),
        "ram_gb": ram,
        "storage_gb": storage,
        "avg_sold_price": avg_price,
        "avg_postage": avg_postage,
        "delivered_price": (
            avg_price + avg_postage
            if avg_price is not None
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
                    query_key,item_id,title,brand,model,cpu,cpu_generation,
                    ram_gb,storage_gb,avg_sold_price,avg_postage,delivered_price,
                    units_sold,total_sales,last_sold,formats,collected_at,source
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'EBAY_PRODUCT_RESEARCH')""",
                (
                    key, row["item_id"], row["title"], row["brand"], row["model"],
                    row["cpu"], row["cpu_generation"], row["ram_gb"],
                    row["storage_gb"], row["avg_sold_price"], row["avg_postage"],
                    row["delivered_price"], row["units_sold"], row["total_sales"],
                    row["last_sold"], row["formats"], iso_now(),
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


def collect_needed_sold_data(conn, maximum=PRODUCT_RESEARCH_SEARCHES_PER_CYCLE):
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
        LIMIT 500
    """).fetchall()

    done = 0
    attempted_listings = 0

    for row in rows:
        if calculate_sold_valuation(conn, row) is not None:
            continue

        queries = sold_search_queries(row)
        if not queries:
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
        f"Valuation backlog: attempted {attempted_listings} listings this cycle; "
        f"{remaining} identifiable listings currently unvalued"
    )
    return done


def sold_candidates(conn, target):
    tb = normalise(target["brand"]).lower()
    tm_raw = valuation_model(target["brand"], target["model"])
    tm = normalise(tm_raw).lower()
    tc = normalise(target["cpu"]).lower()
    precise = precise_model_for_valuation(target["brand"], tm_raw)

    where = ["delivered_price IS NOT NULL", "delivered_price > 0"]
    params = []
    if tb and tm and precise:
        where.append("query_key=?")
        params.append(research_query_key(f"{normalise(target['brand'])} {tm_raw}"))
    elif tb and tc:
        q = f"{normalise(target['brand'])} {normalise(target['cpu'])}"
        if target["ram_gb"]:
            q += f" {target['ram_gb']}GB"
        if target["storage_gb"]:
            q += f" {target['storage_gb']}GB"
        where.append("query_key=?")
        params.append(research_query_key(q))

    rows = conn.execute(
        "SELECT * FROM sold_comparables WHERE " + " AND ".join(where), params
    ).fetchall()
    best = {}
    for row in rows:
        rb = normalise(row["brand"]).lower()
        rm = normalise(valuation_model(row["brand"], row["model"])).lower()
        rc = normalise(row["cpu"]).lower()
        sold_title = normalise(row["title"]).lower()
        specific_token_match = bool(tm and tm in sold_title)
        exact_model = bool(
            precise and tb and tm and rb == tb and rm == tm
            and specific_token_match
        )
        exact_cpu = bool(tc and rc == tc)
        same_brand = bool(tb and rb == tb)
        same_gen = bool(
            target["cpu_generation"] and row["cpu_generation"]
            and target["cpu_generation"] == row["cpu_generation"]
        )

        if precise and not exact_model:
            continue

        ram_score = 12 if target["ram_gb"] and row["ram_gb"] == target["ram_gb"] else (
            5 if target["ram_gb"] and row["ram_gb"] and abs(target["ram_gb"] - row["ram_gb"]) <= 8 else 0
        )
        storage_score = 10 if target["storage_gb"] and row["storage_gb"] == target["storage_gb"] else (
            4 if target["storage_gb"] and row["storage_gb"] and max(target["storage_gb"], row["storage_gb"]) / min(target["storage_gb"], row["storage_gb"]) <= 2 else 0
        )

        if exact_model:
            similarity = 55 + (23 if exact_cpu else 10 if same_gen else 0) + ram_score + storage_score
            tier = "EXACT_MODEL"
        elif not precise and same_brand and exact_cpu:
            similarity = 48 + ram_score + storage_score
            tier = "BRAND_CPU_SPEC"
        elif not precise and exact_cpu:
            similarity = 38 + ram_score + storage_score
            tier = "CPU_SPEC"
        else:
            continue

        units = max(1, int(row["units_sold"] or 1))
        weight = max(1.0, math.sqrt(min(units, 100))) * max(.35, similarity / 100)
        c = {"row": row, "total": float(row["delivered_price"]), "similarity": similarity,
             "tier": tier, "units": units, "weight": weight}
        if row["item_id"] not in best or similarity > best[row["item_id"]]["similarity"]:
            best[row["item_id"]] = c
    return list(best.values())


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
    candidates = sold_candidates(conn, target)

    exact_strong = [c for c in candidates if c["tier"] == "EXACT_MODEL" and c["similarity"] >= 80]
    exact_model = [c for c in candidates if c["tier"] == "EXACT_MODEL"]
    brand_cpu = [c for c in candidates if c["tier"] == "BRAND_CPU_SPEC"]
    cpu_spec = [c for c in candidates if c["tier"] == "CPU_SPEC"]

    if sum(c["units"] for c in exact_strong) >= 3:
        selected, basis = exact_strong, "SOLD_EXACT_MODEL_SPEC"
    elif sum(c["units"] for c in exact_model) >= 3:
        selected, basis = exact_model, "SOLD_EXACT_MODEL"
    elif sum(c["units"] for c in brand_cpu) >= 3:
        selected, basis = brand_cpu, "SOLD_BRAND_CPU_SPEC"
    elif sum(c["units"] for c in cpu_spec) >= 3:
        selected, basis = cpu_spec, "SOLD_CPU_SPEC"
    else:
        return None

    # Basic weighted IQR trimming.
    q1 = weighted_percentile(selected, 0.25)
    q3 = weighted_percentile(selected, 0.75)
    if q1 is not None and q3 is not None and q3 > q1 and len(selected) >= 4:
        iqr = q3 - q1
        low, high = q1 - 1.5 * iqr, q3 + 1.5 * iqr
        trimmed = [c for c in selected if low <= c["total"] <= high]
        if trimmed:
            selected = trimmed

    estimate = weighted_percentile(selected, 0.50)
    q1 = weighted_percentile(selected, 0.25)
    q3 = weighted_percentile(selected, 0.75)
    units = sum(c["units"] for c in selected)
    unique = len(selected)

    if basis == "SOLD_EXACT_MODEL_SPEC" and units >= 8:
        confidence = "HIGH"
    elif basis.startswith("SOLD_EXACT_MODEL") and units >= 5:
        confidence = "MEDIUM"
    else:
        confidence = "LOW"

    asking = float(target["total"] or 0)
    under_gbp = estimate - asking if estimate is not None else None
    under_pct = (under_gbp / estimate * 100) if estimate and under_gbp is not None else None

    score = None
    if estimate and asking > 0:
        pct_component = max(0, min(60, (under_pct or 0) * 1.2))
        gbp_component = max(0, min(25, max(0, under_gbp or 0) / 4))
        conf_component = {"HIGH": 15, "MEDIUM": 10, "LOW": 5}.get(confidence, 0)
        score = max(0, min(100, pct_component + gbp_component + conf_component))
        if basis == "SOLD_CPU_SPEC":
            score = min(score, 45)
        elif basis == "SOLD_BRAND_CPU_SPEC":
            score = min(score, 60)

        if target["status"] == "HIGH_RISK":
            score = min(score, 45)
        elif target["status"] == "MODERATE":
            score = min(score, 70)

    return {
        "estimated_value": estimate,
        "q1": q1,
        "q3": q3,
        "count": units,
        "confidence": confidence,
        "undervaluation_gbp": under_gbp,
        "undervaluation_pct": under_pct,
        "deal_score": score,
        "basis": basis + f":{unique}_ROWS/{units}_SALES",
    }


# ============================================================
# VALUATION
# ============================================================

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


def comparable_candidates(
    conn,
    target
):
    if (
        not target["brand"]
        or not target["model"]
    ):
        return []

    rows = conn.execute("""
        SELECT *
        FROM listings
        WHERE item_id != ?
          AND brand = ?
          AND LOWER(model) = LOWER(?)
          AND total IS NOT NULL
          AND total > 0
          AND status = 'NORMAL'
    """, (
        target["item_id"],
        target["brand"],
        target["model"]
    )).fetchall()

    candidates = []

    for row in rows:

        if not fixed_price_listing(
            row
        ):
            continue

        similarity = 0

        # Exact model is already mandatory.
        similarity += 50

        # CPU matching.
        if (
            target["cpu"]
            and row["cpu"]
            and target["cpu"].lower()
            == row["cpu"].lower()
        ):
            similarity += 25

        elif (
            target["cpu_generation"]
            and row["cpu_generation"]
            and target["cpu_generation"]
            == row["cpu_generation"]
        ):
            similarity += 12

        # RAM
        if (
            target["ram_gb"]
            and row["ram_gb"]
        ):

            if (
                target["ram_gb"]
                == row["ram_gb"]
            ):
                similarity += 12

            elif abs(
                target["ram_gb"]
                - row["ram_gb"]
            ) <= 8:
                similarity += 5

        # Storage
        if (
            target["storage_gb"]
            and row["storage_gb"]
        ):

            if (
                target["storage_gb"]
                == row["storage_gb"]
            ):
                similarity += 10

            elif (
                max(
                    target[
                        "storage_gb"
                    ],
                    row[
                        "storage_gb"
                    ]
                )
                /
                min(
                    target[
                        "storage_gb"
                    ],
                    row[
                        "storage_gb"
                    ]
                )
                <= 2
            ):
                similarity += 4

        candidates.append({
            "row":
                row,

            "similarity":
                similarity,

            "total":
                float(
                    row["total"]
                )
        })

    return candidates


def remove_price_outliers(
    candidates
):
    if len(candidates) < 4:
        return candidates

    prices = [
        c["total"]
        for c in candidates
    ]

    q1 = percentile(
        prices,
        0.25
    )

    q3 = percentile(
        prices,
        0.75
    )

    iqr = q3 - q1

    if iqr <= 0:
        return candidates

    low = q1 - 1.5 * iqr
    high = q3 + 1.5 * iqr

    filtered = [
        c
        for c in candidates
        if low <= c["total"] <= high
    ]

    if len(filtered) < 3:
        return candidates

    return filtered


def calculate_active_valuation(
    conn,
    target
):
    candidates = comparable_candidates(
        conn,
        target
    )

    # Prefer closer matches if enough exist.
    strong = [
        c
        for c in candidates
        if c["similarity"] >= 75
    ]

    medium = [
        c
        for c in candidates
        if c["similarity"] >= 62
    ]

    if len(strong) >= 3:
        selected = strong
        basis = "EXACT_MODEL_STRONG_SPEC"

    elif len(medium) >= 3:
        selected = medium
        basis = "EXACT_MODEL_SIMILAR_SPEC"

    else:
        selected = candidates
        basis = "EXACT_MODEL"

    selected = remove_price_outliers(
        selected
    )

    count = len(
        selected
    )

    if count < MIN_COMPARABLES:

        return {
            "estimated_value":
                None,

            "q1":
                None,

            "q3":
                None,

            "count":
                count,

            "confidence":
                "INSUFFICIENT_DATA",

            "undervaluation_gbp":
                None,

            "undervaluation_pct":
                None,

            "deal_score":
                None,

            "basis":
                basis
        }

    prices = [
        c["total"]
        for c in selected
    ]

    median = statistics.median(
        prices
    )

    q1 = percentile(
        prices,
        0.25
    )

    q3 = percentile(
        prices,
        0.75
    )

    average_similarity = (
        sum(
            c["similarity"]
            for c in selected
        )
        / count
    )

    if (
        count
        >= HIGH_CONFIDENCE_COMPARABLES
        and average_similarity >= 70
    ):
        confidence = "HIGH"

    elif (
        count
        >= MEDIUM_CONFIDENCE_COMPARABLES
        and average_similarity >= 60
    ):
        confidence = "MEDIUM"

    else:
        confidence = "LOW"

    delivered = float(
        target["total"]
        or 0
    )

    undervalue = (
        median
        - delivered
    )

    undervalue_pct = (
        undervalue
        / median
        * 100
        if median > 0
        else None
    )

    score = deal_score(
        target,
        median,
        undervalue,
        undervalue_pct,
        confidence
    )

    return {
        "estimated_value":
            round(
                median,
                2
            ),

        "q1":
            round(
                q1,
                2
            ),

        "q3":
            round(
                q3,
                2
            ),

        "count":
            count,

        "confidence":
            confidence,

        "undervaluation_gbp":
            round(
                undervalue,
                2
            ),

        "undervaluation_pct":
            round(
                undervalue_pct,
                1
            ),

        "deal_score":
            score,

        "basis":
            basis
    }


def deal_score(
    target,
    value,
    undervalue,
    undervalue_pct,
    confidence
):
    if (
        value is None
        or undervalue is None
        or undervalue_pct is None
    ):
        return None

    # £ opportunity: maximum 35 points.
    pounds_score = min(
        35,
        max(
            0,
            undervalue / 3
        )
    )

    # Percentage opportunity: maximum 40 points.
    percentage_score = min(
        40,
        max(
            0,
            undervalue_pct * 0.8
        )
    )

    confidence_points = {
        "HIGH": 20,
        "MEDIUM": 14,
        "LOW": 7
    }.get(
        confidence,
        0
    )

    # Small bonus for a completely normal listing.
    condition_points = {
        "NORMAL": 5,
        "LOW_COST": 2,
        "MODERATE": 0,
        "HIGH_RISK": 0
    }.get(
        target["status"],
        0
    )

    score = (
        pounds_score
        + percentage_score
        + confidence_points
        + condition_points
    )

    # Risk caps prevent broken machines receiving the same
    # headline score as a straightforward working bargain.
    if target["status"] == "HIGH_RISK":
        score = min(
            score,
            69
        )

    elif target["status"] == "MODERATE":
        score = min(
            score,
            84
        )

    return round(
        min(
            100,
            max(
                0,
                score
            )
        ),
        1
    )



def sold_evidence_for_basis(conn, target, basis):
    """Return the sold rows actually eligible for the displayed sold valuation."""
    if not basis or not basis.startswith("SOLD_"):
        return []

    candidates = sold_candidates(conn, target)

    if basis.startswith("SOLD_EXACT_MODEL_SPEC"):
        selected = [
            c for c in candidates
            if c["tier"] == "EXACT_MODEL"
            and c["similarity"] >= 80
        ]
    elif basis.startswith("SOLD_EXACT_MODEL"):
        selected = [
            c for c in candidates
            if c["tier"] == "EXACT_MODEL"
        ]
    elif basis.startswith("SOLD_BRAND_CPU_SPEC"):
        selected = [
            c for c in candidates
            if c["tier"] == "BRAND_CPU_SPEC"
        ]
    elif basis.startswith("SOLD_CPU_SPEC"):
        selected = [
            c for c in candidates
            if c["tier"] == "CPU_SPEC"
        ]
    else:
        return []

    q1 = weighted_percentile(selected, 0.25)
    q3 = weighted_percentile(selected, 0.75)

    if (
        q1 is not None
        and q3 is not None
        and q3 > q1
        and len(selected) >= 4
    ):
        iqr = q3 - q1
        low = q1 - 1.5 * iqr
        high = q3 + 1.5 * iqr
        trimmed = [
            c for c in selected
            if low <= c["total"] <= high
        ]
        if trimmed:
            selected = trimmed

    return sorted(
        selected,
        key=lambda c: (
            -c["similarity"],
            c["total"]
        )
    )

def calculate_valuation(conn, target):
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
        != VERSION
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
        f"Delivered: "
        f"£{item['total']:.2f}"
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
        "USB-C PD:",
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


def money(value):
    if value is None:
        return "—"

    return (
        f"£{float(value):,.2f}"
    )


def dashboard_html():
    conn = connect_db()

    rows = conn.execute("""
        SELECT *
        FROM listings
        WHERE COALESCE(active, 1) = 1
        ORDER BY
            CASE
                WHEN deal_score IS NULL
                THEN 1
                ELSE 0
            END,
            deal_score DESC,
            undervaluation_gbp DESC,
            first_seen DESC
        LIMIT 500
    """).fetchall()

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

    candidates = conn.execute("""
        SELECT COUNT(*) AS n
        FROM listings
        WHERE undervaluation_gbp > 0
          AND COALESCE(active, 1) = 1
    """).fetchone()["n"]

    api_calls = browse_usage_today(
        conn
    )

    capability_pending = conn.execute("""
        SELECT COUNT(*) AS n
        FROM capability_queue
        WHERE status='PENDING'
          AND capability='USB_C_PD'
    """).fetchone()["n"]

    win11_pending = conn.execute("""
        SELECT COUNT(*) AS n
        FROM capability_queue
        WHERE status='PENDING'
          AND capability='WIN11_APPROVED'
    """).fetchone()["n"]

    valuation_backlog = conn.execute("""
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

    body_rows = []

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
                "Insufficient data"
                "</span>"
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

        body_rows.append(
            f"""
            <tr>
                <td>
                    <a href="{url}"
                       target="_blank">
                       {title}
                    </a>
                    <div class="small">
                        {html.escape(spec_text)}
                    </div>
                </td>

                <td>
                    {age_html}
                    <div class="small">{age_label}</div>
                </td>

                <td>
                    {html.escape(
                        row["brand"]
                        or "?"
                    )}
                    <br>
                    <span class="small">
                    {html.escape(
                        row["model"]
                        or "?"
                    )}
                    </span>
                </td>

                <td class="money">
                    {money(row["total"])}
                </td>

                <td class="money">
                    {money(
                        row[
                            "estimated_value"
                        ]
                    )}
                    <div class="small">
                    {
                        (
                            money(
                                row[
                                    "valuation_q1"
                                ]
                            )
                            + " – "
                            + money(
                                row[
                                    "valuation_q3"
                                ]
                            )
                        )
                        if row[
                            "valuation_q1"
                        ] is not None
                        else ""
                    }
                    </div>
                </td>

                <td class="money good">
                    {under}
                </td>

                <td>
                    <span class="big">
                    {score}
                    </span>
                </td>

                <td>
                    {
                        html.escape(
                            row[
                                "valuation_confidence"
                            ]
                            or "—"
                        )
                    }
                    <div class="small">
                    {html.escape(sales_wording)}
                    <br>
                    {
                        html.escape(
                            row["valuation_basis"]
                            or ""
                        )
                    }
                    </div>
                </td>

                <td>
                    {
                        html.escape(
                            row["status"]
                            or ""
                        )
                    }
                </td>

                <td>
                    Windows 11 approved:
                    {yesno(row["win11"])}
                    <br>
                    USB-C charging:
                    {yesno(row["usbc_pd"])}
                    <br>
                    <span class="small">
                    {html.escape(row["usbc_pd_confidence"] or "UNKNOWN")}
                    {
                        " · " + html.escape(row["usbc_pd_source"])
                        if row["usbc_pd_source"]
                        else ""
                    }
                    {
                        " · " + str(row["usbc_pd_watts"]) + "W"
                        if row["usbc_pd_watts"]
                        else ""
                    }
                    </span>
                    {
                        "<br><span class='small'>"
                        + html.escape(row["usbc_pd_evidence"] or "")
                        + "</span>"
                        if row["usbc_pd_evidence"]
                        else ""
                    }
                </td>
            </tr>
            {
                (
                    "<tr class='evidence-row'><td colspan='10'>"
                    + evidence_html
                    + "</td></tr>"
                )
                if evidence_html
                else ""
            }
            """
        )

    conn.close()

    return f"""
    <!doctype html>
    <html>
    <head>
        <meta charset="utf-8">
        <meta http-equiv="refresh"
              content="60">
        <title>eBay Laptop Hunter</title>
        <style>{CSS}</style>
    </head>

    <body>

    <h1>eBay Laptop Hunter</h1>

    <div class="sub">
        v{VERSION} —
        sold-market bargain dashboard
    </div>

    <div class="cards">

        <div class="card">
            <div class="big">{total}</div>
            Listings collected
        </div>

        <div class="card">
            <div class="big">{valued}</div>
            Valued
        </div>

        <div class="card">
            <div class="big">{candidates}</div>
            Below estimated value
        </div>

        <div class="card">
            <div class="big">{api_calls}</div>
            API calls today
        </div>

        <div class="card">
            <div class="big">{capability_pending}</div>
            USB-C charging models awaiting verification
        </div>

        <div class="card">
            <div class="big">{win11_pending}</div>
            Windows 11 CPUs awaiting verification
        </div>

        <div class="card">
            <div class="big">{valuation_backlog}</div>
            Identifiable laptops awaiting valuation
        </div>

        <div class="card">
            <div class="big {session_class}">{html.escape(session_status)}</div>
            Product Research session
            <div class="small">
                Checked {html.escape(session_last_checked)} ago<br>
                {html.escape(session_state.get("message", "No session check yet"))}
            </div>
        </div>

        <div class="card">
            <div class="big">{html.escape(session_last_refresh)}</div>
            Since session refresh
            <div class="small">
                Source: {html.escape(session_state.get("last_refresh_source", "—"))}<br>
                SID: {html.escape(session_state.get("ebaysid_hash", "—"))}
            </div>
        </div>

        <div class="card">
            <div class="big">{html.escape(helper_status)}</div>
            Chromium session helper
            <div class="small">Last seen {html.escape(helper_seen)} ago</div>
        </div>

    </div>

    <table>

    <thead>
    <tr>
        <th>Listing</th>
        <th>Listing age</th>
        <th>Model</th>
        <th>Delivered</th>
        <th>Est. sold value</th>
        <th>Apparent undervalue</th>
        <th>Score</th>
        <th>Confidence</th>
        <th>Condition</th>
        <th>Eligibility</th>
    </tr>
    </thead>

    <tbody>
        {''.join(body_rows)}
    </tbody>

    </table>

    </body>
    </html>
    """


class DashboardHandler(
    BaseHTTPRequestHandler
):

    def do_GET(self):

        if self.path not in (
            "/",
            "/index.html"
        ):

            self.send_response(
                404
            )

            self.end_headers()

            return

        content = dashboard_html().encode(
            "utf-8"
        )

        self.send_response(
            200
        )

        self.send_header(
            "Content-Type",
            "text/html; charset=utf-8"
        )

        self.send_header(
            "Content-Length",
            str(len(content))
        )

        self.end_headers()

        self.wfile.write(
            content
        )

    def log_message(
        self,
        format,
        *args
    ):
        pass


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
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            record_api_call(conn=None, endpoint="getItem") if False else None
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


def recheck_active_bin_listings(conn, token, maximum=ACTIVE_BIN_RECHECKS_PER_CYCLE):
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
            CASE WHEN deal_score IS NULL THEN 1 ELSE 0 END,
            deal_score DESC,
            COALESCE(availability_checked_at, '1970-01-01') ASC,
            first_seen ASC
        LIMIT 100
    """, (cutoff, interval_cutoff)).fetchall()

    checked = 0
    inactivated = 0

    for row in rows:
        if checked >= maximum:
            break
        if not is_fixed_price_listing(row):
            continue

        state, detail = ebay_get_item(token, row["item_id"])
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

    print(
        f"eBay returned "
        f"{len(summaries)} newest listings"
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
    recheck_active_bin_listings(
        conn,
        token,
        ACTIVE_BIN_RECHECKS_PER_CYCLE
    )

    repair_v078_model_and_sold_cache(conn)

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

    print(
        f"eBay Laptop Hunter "
        f"v{VERSION}"
    )

    if not os.path.exists(PRODUCT_RESEARCH_SESSION_STATE):
        _session_state_update(
            status="UNKNOWN",
            message="Waiting for first Product Research check",
        )

    start_dashboard()
    start_product_research_session_monitor()

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
