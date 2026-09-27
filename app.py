# eBay Laptop Hunter v0.8.1
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

APP_VERSION = "0.9.2"
CLASSIFIER_VERSION = "0.8.1"
MIN_UNDERVALUE_GBP = 20.0

# Backward-compatible internal alias.
# Existing classifier_version DB logic continues to use VERSION.
VERSION = CLASSIFIER_VERSION
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
MAX_BACKFILL_DETAILS_PER_CYCLE = 20
POLL_NORMAL = 90
POLL_60_PERCENT = 120
POLL_75_PERCENT = 180
POLL_85_PERCENT = 300
POLL_90_PERCENT = 600

# Valuation
MIN_COMPARABLES = 2
MEDIUM_CONFIDENCE_COMPARABLES = 6

# Don't use ancient active observations indefinitely.
COMPARABLE_MAX_AGE_DAYS = 30
SOLD_CACHE_MAX_AGE_DAYS = 7
SOLD_EVIDENCE_VERSION = "2"


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

    for name, sql_type in {"evidence_version": "TEXT", "currency": "TEXT"}.items():
        ensure_column(conn, "sold_comparables", name, sql_type)

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
    r"\bLatitude\s+\d{4}\s+Detachable\b",
    r"\bLatitude\s+(?:E)?\d{4}\b",

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

        exact.sort(
            key=lambda cpu:
                source_priority.get(
                    cpu.get(
                        "source"
                    ),
                    99
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
                    query_key,item_id,title,brand,model,cpu,cpu_generation,
                    ram_gb,storage_gb,avg_sold_price,avg_postage,delivered_price,
                    units_sold,total_sales,last_sold,formats,collected_at,currency,evidence_version,source
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'EBAY_PRODUCT_RESEARCH')""",
                (
                    key, row["item_id"], row["title"], row["brand"], row["model"],
                    row["cpu"], row["cpu_generation"], row["ram_gb"],
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
          AND active=1
          AND classifier_version=?
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
    """, (VERSION,)).fetchall()

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

def target_valuation_problem(target):
    if row_value(target, "classifier_version") != VERSION:
        return "REANALYSIS_REQUIRED"
    if not exact_spec_identity(target):
        return "INCOMPLETE_IDENTITY_OR_SPEC"
    if row_value(target, "status") != "NORMAL" or not ordinary_laptop(
        row_value(target, "title"), row_value(target, "condition")
    ):
        return "CONDITION_REQUIRES_REVIEW"
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
    selected = remove_price_outliers(sold_candidates(conn, target))
    return selected if len(selected) >= MIN_COMPARABLES else []



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
    if target_valuation_problem(target):
        return []
    rows = conn.execute(
        "SELECT * FROM sold_comparables WHERE delivered_price > 0 "
        "AND currency='GBP' AND evidence_version=? AND LOWER(brand)=LOWER(?) "
        "AND LOWER(model)=LOWER(?)", (SOLD_EVIDENCE_VERSION, target["brand"], target["model"])
    ).fetchall()
    best = {}
    fingerprints = set()
    target_item_id = canonical_sold_item_id(
        row_value(target, "item_id")
    )

    for row in sorted(rows, key=lambda r: r["collected_at"], reverse=True):
        sold_item_id = canonical_sold_item_id(
            row["item_id"]
        )

        if not sold_item_id or sold_item_id.startswith("title:"):
            continue

        # Never use the target listing itself as sold evidence.
        if target_item_id and sold_item_id == target_item_id:
            continue
        age = evidence_age_days(row["last_sold"])
        cache_age = evidence_age_days(row["collected_at"])
        if (age is None or age > PRODUCT_RESEARCH_DAY_RANGE or cache_age is None
                or cache_age > SOLD_CACHE_MAX_AGE_DAYS):
            continue
        if not same_spec(target, row) or not ordinary_laptop(row["title"]):
            continue
        if row["avg_postage"] is None or row["avg_postage"] < 0:
            continue
        price = float(row["delivered_price"])
        if not math.isfinite(price):
            continue
        # Repeated identical adverts must not manufacture independent evidence.
        fingerprint = normalise(row["title"]).lower()
        if sold_item_id in best or fingerprint in fingerprints:
            continue
        fingerprints.add(fingerprint)
        best[sold_item_id] = dict(row=row, total=price, similarity=100,
            tier="EXACT_MODEL_SPEC", units=max(1, int(row["units_sold"] or 1)),
            weight=2 ** (-age / 60.0))
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
    selected = select_sold_evidence(conn, target)
    if not selected:
        return None
    estimate = weighted_percentile(selected, .5)
    q1, q3 = weighted_percentile(selected, .25), weighted_percentile(selected, .75)
    weights = [c["weight"] for c in selected]
    effective_n = sum(weights) ** 2 / sum(w * w for w in weights)
    # Title-only condition/display evidence and unknown seller diversity cannot
    # justify HIGH confidence, even when many units have sold.
    confidence = "MEDIUM" if effective_n >= MEDIUM_CONFIDENCE_COMPARABLES and (q3-q1)/estimate <= .3 else "LOW"
    under = estimate - target["total"]
    pct = under / estimate * 100

    # Median sold value defines ordinary undervaluation.
    # Q1 is retained as a separate conservative strong-deal signal.
    score = deal_score(
        target,
        estimate,
        under,
        pct,
        confidence,
    )

    if (
        score is not None
        and score > 0
        and q1 is not None
        and target["total"] < q1
    ):
        # Price is below the lower quartile of matching sold evidence.
        # This is substantially stronger than merely being below median.
        score_cap = 45 if confidence == "LOW" else 75
        score = round(
            min(
                score_cap,
                score + 10,
            ),
            1,
        )
    return dict(estimated_value=round(estimate, 2), q1=q1, q3=q3,
        count=len(selected), confidence=confidence, undervaluation_gbp=round(under, 2),
        undervaluation_pct=round(pct, 1), deal_score=score,
        basis=f"SOLD_EXACT_MODEL_SPEC:{len(selected)}_ROWS/"
              f"{sum(c['units'] for c in selected)}_SALES/NEFF={effective_n:.1f}")

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
    if target_valuation_problem(target):
        return []
    rows = conn.execute("""SELECT * FROM listings
        WHERE item_id != ? AND brand = ? AND LOWER(model) = LOWER(?)
          AND total > 0 AND postage IS NOT NULL AND active=1
          AND status='NORMAL' AND classifier_version=?""",
        (target["item_id"], target["brand"], target["model"], VERSION)).fetchall()
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

def deal_score(target, value, undervalue, undervalue_pct, confidence):
    if target_valuation_problem(target) or not fixed_price_listing(target):
        return None
    if value is None or undervalue is None or undervalue_pct is None:
        return None
    if undervalue < MIN_UNDERVALUE_GBP:
        return 0
    score = min(60, undervalue_pct * 1.2) + min(25, undervalue / 4)
    # LOW means review evidence, not a high-priority buying recommendation.
    return round(min(45 if confidence == "LOW" else 75, score), 1)

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
    problem = target_valuation_problem(target)
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


def _dashboard_html_base():
    conn = connect_db()

    rows = conn.execute("""
        SELECT *
        FROM listings
        WHERE COALESCE(active, 1) = 1
          AND estimated_value IS NOT NULL
          AND deal_score IS NOT NULL
          AND undervaluation_gbp IS NOT NULL
          AND undervaluation_gbp >= 20.0
          AND undervaluation_pct IS NOT NULL
          AND undervaluation_pct > 0
        ORDER BY
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
        WHERE COALESCE(active, 1) = 1
          AND estimated_value IS NOT NULL
          AND deal_score IS NOT NULL
          AND undervaluation_gbp IS NOT NULL
          AND undervaluation_gbp >= 20.0
          AND undervaluation_pct IS NOT NULL
          AND undervaluation_pct > 0
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
            pipeline_row
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
                + ("Asking-price reference" if (row["valuation_basis"] or "").startswith("ACTIVE_FALLBACK")
                 else "Not scored")
                + "</span>"
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
                    <br>
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
                            valuation_label(row["valuation_basis"] or "")
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
            <div class="small">
            Ready but insufficient sales: <strong>{ready_unvalued}</strong>
             &nbsp;·&nbsp; Incomplete identity/spec: <strong>{incomplete_spec}</strong>
             &nbsp;·&nbsp; Condition/review: <strong>{condition_review}</strong>
             &nbsp;·&nbsp; Unknown delivered cost: <strong>{unknown_cost}</strong>
            </div>
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
        <th>Value / asking reference</th>
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
.system-health-alert {
    margin: 12px;
    padding: 14px 18px;
    border: 2px solid #b3261e;
    border-radius: 8px;
    background: #fff4f3;
    color: #7d1712;
}

.system-health-alert strong {
    font-size: 18px;
}

.system-health-alert ul {
    margin: 7px 0 0 20px;
    padding: 0;
}

.sortable-header {
    cursor: pointer;
    user-select: none;
}

.sortable-header:hover {
    background: #e3e3e3;
}

.sort-indicator {
    display: inline-block;
    width: 1em;
    margin-left: 4px;
    font-size: 11px;
}

.evidence-cell {
    text-align: center;
    white-space: nowrap;
}

.evidence-badge {
    position: relative;
    display: inline-block;
    min-width: 28px;
    padding: 4px 8px;
    border: 1px solid #bbb;
    border-radius: 12px;
    background: #f5f5f5;
    font-weight: bold;
    cursor: help;
}

.evidence-tooltip {
    display: none;
    position: absolute;
    z-index: 10000;
    left: 50%;
    top: calc(100% + 8px);
    transform: translateX(-50%);
    width: min(520px, 80vw);
    max-height: 420px;
    overflow: auto;
    white-space: normal;
    text-align: left;
    font-weight: normal;
    font-size: 13px;
    line-height: 1.35;
    padding: 12px;
    border: 1px solid #777;
    border-radius: 6px;
    background: white;
    color: #111;
    box-shadow: 0 4px 18px rgba(0,0,0,.22);
}

.evidence-badge:hover .evidence-tooltip {
    display: block;
}

.evidence-tooltip .summary {
    margin-bottom: 8px;
    font-weight: bold;
}

.evidence-tooltip .sale {
    padding: 6px 0;
    border-top: 1px solid #ddd;
}

.evidence-tooltip .sale-title {
    margin-top: 2px;
    color: #444;
}

th.sorted-asc,
th.sorted-desc {
    background: #dedede;
}
</style>

<script>
document.addEventListener("DOMContentLoaded", () => {

    const SORT_KEY = "ebayLaptopHunterSort";

    // --------------------------------------------------------
    // Hide operational/debug cards when healthy.
    // --------------------------------------------------------

    const unwantedCardText = [
        "API calls today",
        "USB-C charging models awaiting verification",
        "Windows 11 CPUs awaiting verification",
        "Product Research session",
        "Since session refresh",
        "Chromium session helper"
    ];

    document.querySelectorAll(".card").forEach(card => {
        const text = card.textContent || "";

        if (
            unwantedCardText.some(
                phrase => text.includes(phrase)
            )
        ) {
            card.remove();
        }
    });


    // --------------------------------------------------------
    // Locate listings table.
    // --------------------------------------------------------

    const tables = Array.from(
        document.querySelectorAll("table")
    );

    const table = tables.find(t => {

        const headers = Array.from(
            t.querySelectorAll("th")
        ).map(
            h => h.textContent.trim()
        );

        return (
            headers.includes("Listing")
            && headers.includes("Score")
        );
    });

    if (!table) {
        return;
    }

    const headerRow =
        table.querySelector("thead tr")
        || table.querySelector("tr");

    if (!headerRow) {
        return;
    }

    const body =
        table.tBodies.length
        ? table.tBodies[0]
        : table;


    // --------------------------------------------------------
    // Helpers
    // --------------------------------------------------------

    function headerIndex(name) {

        return Array.from(
            headerRow.children
        ).findIndex(
            th => th.textContent.trim() === name
        );
    }


    function findHeaderContaining(text) {

        return Array.from(
            headerRow.children
        ).findIndex(
            th => th.textContent.trim().includes(text)
        );
    }


    function statusSymbol(text) {

        const t = String(text || "")
            .trim()
            .toUpperCase();

        if (
            t.includes("YES")
            || t.includes("SUPPORTED")
            || t.includes("APPROVED")
            || t.includes("PASS")
            || t.includes("TRUE")
            || t.includes("✓")
        ) {
            return "✓";
        }

        if (
            t.includes("NO")
            || t.includes("UNSUPPORTED")
            || t.includes("NOT APPROVED")
            || t.includes("FAIL")
            || t.includes("FALSE")
            || t.includes("✗")
        ) {
            return "✗";
        }

        return "?";
    }


    function numericValue(text) {

        const match = String(text)
            .replace(/,/g, "")
            .match(/-?\d+(?:\.\d+)?/);

        return match
            ? parseFloat(match[0])
            : Number.NEGATIVE_INFINITY;
    }


    function ageSeconds(text) {

        text = String(text).toLowerCase();

        let seconds = 0;

        const d = text.match(/(\d+)\s*d/);
        const h = text.match(/(\d+)\s*h/);
        const m = text.match(/(\d+)\s*m/);

        if (d) seconds += Number(d[1]) * 86400;
        if (h) seconds += Number(h[1]) * 3600;
        if (m) seconds += Number(m[1]) * 60;

        return seconds;
    }


    function confidenceRank(text) {

        const t = String(text).toUpperCase();

        if (t.includes("HIGH")) return 3;
        if (t.includes("MEDIUM")) return 2;
        if (t.includes("LOW")) return 1;

        return 0;
    }


    // --------------------------------------------------------
    // Convert old evidence detail rows into Sold evidence cell
    // first, if previous enhancement has not already done so.
    // --------------------------------------------------------

    if (headerIndex("Sold evidence") < 0) {

        let scoreIndex = headerIndex("Score");

        if (scoreIndex < 0) {
            scoreIndex = Math.min(
                6,
                headerRow.children.length - 1
            );
        }

        const evidenceHeader =
            document.createElement("th");

        evidenceHeader.textContent =
            "Sold evidence";

        headerRow.insertBefore(
            evidenceHeader,
            headerRow.children[scoreIndex + 1] || null
        );


        const rows = Array.from(
            body.querySelectorAll(":scope > tr")
        );

        const listingRows = rows.filter(row => {

            if (
                row === headerRow
                || row.closest("thead")
            ) {
                return false;
            }

            if (
                row.children.length === 1
                && (
                    row.textContent.includes(
                        "Show sold evidence"
                    )
                    || row.querySelector("details")
                )
            ) {
                return false;
            }

            return row.children.length >= 5;
        });


        listingRows.forEach(row => {

            const next = row.nextElementSibling;

            let evidenceRow = null;

            if (
                next
                && (
                    next.textContent.includes(
                        "Show sold evidence"
                    )
                    || next.querySelector("details")
                )
            ) {
                evidenceRow = next;
            }

            const cell =
                document.createElement("td");

            cell.className =
                "evidence-cell";

            let totalSales = 0;
            let soldListingCount = 0;
            const soldRows = [];

            if (evidenceRow) {

                const nested =
                    evidenceRow.querySelector("table");

                if (nested) {

                    const nestedRows = Array.from(
                        nested.querySelectorAll("tr")
                    ).filter(
                        tr =>
                            tr.querySelectorAll("td").length >= 5
                    );

                    nestedRows.forEach(tr => {

                        const td = Array.from(
                            tr.querySelectorAll("td")
                        );

                        const units =
                            parseInt(
                                (
                                    td[4]?.textContent
                                    || "1"
                                ).replace(/[^\d]/g, ""),
                                10
                            ) || 1;

                        totalSales += units;
                        soldListingCount += 1;

                        soldRows.push({
                            title:
                                td[0]?.textContent.trim()
                                || "",
                            price:
                                td[1]?.textContent.trim()
                                || "—",
                            units,
                            soldDate:
                                td[5]?.textContent.trim()
                                || "—"
                        });
                    });
                }

                evidenceRow.remove();
            }

            if (soldListingCount > 0) {

                const badge =
                    document.createElement("span");

                badge.className =
                    "evidence-badge";

                badge.appendChild(
                    document.createTextNode(
                        String(totalSales)
                    )
                );

                const tooltip =
                    document.createElement("div");

                tooltip.className =
                    "evidence-tooltip";

                const summary =
                    document.createElement("div");

                summary.className =
                    "summary";

                summary.textContent =
                    soldListingCount
                    + (
                        soldListingCount === 1
                        ? " sold listing / "
                        : " sold listings / "
                    )
                    + totalSales
                    + (
                        totalSales === 1
                        ? " sale"
                        : " sales"
                    );

                tooltip.appendChild(summary);

                soldRows.forEach(sale => {

                    const line =
                        document.createElement("div");

                    line.className = "sale";

                    const first =
                        document.createElement("div");

                    first.textContent =
                        sale.price
                        + " — "
                        + sale.soldDate
                        + (
                            sale.units > 1
                            ? " — "
                              + sale.units
                              + " sales"
                            : ""
                        );

                    const title =
                        document.createElement("div");

                    title.className =
                        "sale-title";

                    title.textContent =
                        sale.title;

                    line.appendChild(first);
                    line.appendChild(title);

                    tooltip.appendChild(line);
                });

                badge.appendChild(tooltip);
                cell.appendChild(badge);

                cell.dataset.sort =
                    String(totalSales);

            } else {

                cell.textContent = "—";
                cell.dataset.sort = "0";
            }

            row.insertBefore(
                cell,
                row.children[scoreIndex + 1] || null
            );
        });


        Array.from(
            body.querySelectorAll(":scope > tr")
        ).forEach(row => {

            if (
                row.children.length === 1
                && row.textContent.includes(
                    "Show sold evidence"
                )
            ) {
                row.remove();
            }
        });
    }


    // --------------------------------------------------------
    // Merge Sold evidence + Condition + Eligibility into
    // Confidence.
    // --------------------------------------------------------

    let confidenceIndex =
        headerIndex("Confidence");

    const evidenceIndex =
        headerIndex("Sold evidence");

    const conditionIndex =
        headerIndex("Condition");

    const eligibilityIndex =
        headerIndex("Eligibility");


    if (confidenceIndex >= 0) {

        const rows = Array.from(
            body.querySelectorAll(":scope > tr")
        ).filter(
            row =>
                row !== headerRow
                && !row.closest("thead")
                && row.children.length
                   === headerRow.children.length
        );


        rows.forEach(row => {

            const cells =
                Array.from(row.children);

            const confidenceCell =
                cells[confidenceIndex];

            if (!confidenceCell) {
                return;
            }

            const baseConfidence =
                confidenceCell.textContent
                    .trim()
                    .split(/\s+/)[0]
                || "—";


            // Preserve evidence node/tooltip.
            let evidenceNode = null;
            let salesCount = 0;

            if (
                evidenceIndex >= 0
                && cells[evidenceIndex]
            ) {

                const evidenceCell =
                    cells[evidenceIndex];

                salesCount =
                    Number(
                        evidenceCell.dataset.sort
                        || numericValue(
                            evidenceCell.textContent
                        )
                        || 0
                    );

                const badge =
                    evidenceCell.querySelector(
                        ".evidence-badge"
                    );

                if (badge) {
                    evidenceNode =
                        badge.cloneNode(true);
                }
            }


            const conditionText =
                conditionIndex >= 0
                && cells[conditionIndex]
                    ? cells[
                        conditionIndex
                    ].textContent.trim()
                    : "";


            const eligibilityText =
                eligibilityIndex >= 0
                && cells[eligibilityIndex]
                    ? cells[
                        eligibilityIndex
                    ].textContent.trim()
                    : "";


            // The current Eligibility cell contains the
            // Windows 11 / USB-C capability status text.
            const win11 =
                statusSymbol(
                    eligibilityText.match(
                        /win(?:dows)?\s*11[^|,;]*/i
                    )?.[0]
                    || eligibilityText
                );

            const usbc =
                statusSymbol(
                    eligibilityText.match(
                        /usb[- ]?c[^|,;]*/i
                    )?.[0]
                    || eligibilityText
                );


            confidenceCell.innerHTML = "";

            const main =
                document.createElement("div");

            main.style.fontWeight = "bold";
            main.textContent =
                baseConfidence;

            confidenceCell.appendChild(main);


            const meta =
                document.createElement("div");

            meta.className = "small";
            meta.style.whiteSpace = "nowrap";

            meta.appendChild(
                document.createTextNode(
                    "Win 11: "
                    + win11
                    + "  USB-C PD: "
                    + usbc
                    + "  Sales: "
                )
            );

            if (evidenceNode) {

                // Make the evidence badge compact inside
                // Confidence.
                evidenceNode.style.padding =
                    "0 4px";

                evidenceNode.style.minWidth =
                    "auto";

                meta.appendChild(
                    evidenceNode
                );

            } else {

                meta.appendChild(
                    document.createTextNode(
                        String(salesCount)
                    )
                );
            }


            if (
                conditionText
                && conditionText !== "—"
            ) {

                const cond =
                    document.createElement("div");

                cond.className = "small";
                cond.textContent =
                    "Condition: "
                    + conditionText;

                confidenceCell.appendChild(
                    meta
                );

                confidenceCell.appendChild(
                    cond
                );

            } else {

                confidenceCell.appendChild(
                    meta
                );
            }

            confidenceCell.dataset.sort =
                String(
                    confidenceRank(
                        baseConfidence
                    )
                );
        });


        // Remove old columns right-to-left so indexes don't move.
        const indexesToRemove = [
            evidenceIndex,
            conditionIndex,
            eligibilityIndex
        ]
        .filter(
            i => i >= 0
        )
        .sort(
            (a, b) => b - a
        );


        indexesToRemove.forEach(index => {

            if (
                headerRow.children[index]
            ) {
                headerRow.children[index].remove();
            }

            Array.from(
                body.querySelectorAll(":scope > tr")
            ).forEach(row => {

                if (
                    row !== headerRow
                    && !row.closest("thead")
                    && row.children[index]
                ) {
                    row.children[index].remove();
                }
            });
        });
    }



    // --------------------------------------------------------
    // Compact table presentation
    // --------------------------------------------------------

    function cleanHeaderText() {
        Array.from(headerRow.children).forEach(th => {

            const text = th.textContent.trim();

            if (text === "Delivered") {
                th.textContent = "Price";
            }

            if (
                text === "Apparent undervalue"
                || text === "Apparent undervaluation"
            ) {
                th.textContent = "Undervaluation";
            }
        });
    }

    cleanHeaderText();


    // --------------------------------------------------------
    // Remove "eBay listed" repetition from Listing age.
    // --------------------------------------------------------

    let ageIndex = Array.from(
        headerRow.children
    ).findIndex(
        th =>
            th.textContent.trim() === "Listing age"
            || th.textContent.trim() === "Age"
    );

    if (ageIndex >= 0) {

        const ageHeader =
            headerRow.children[ageIndex];

        ageHeader.textContent = "Age";

        Array.from(
            body.querySelectorAll(":scope > tr")
        ).forEach(row => {

            const cell =
                row.children[ageIndex];

            if (!cell) {
                return;
            }

            // Keep the visible relative age / NEW marker,
            // remove repetitive label text.
            cell.innerHTML =
                cell.innerHTML
                    .replace(
                        /eBay listed\s*/gi,
                        ""
                    )
                    .replace(
                        /First seen\s*/gi,
                        ""
                    );
        });
    }


    // --------------------------------------------------------
    // Merge Model into specs line in Listing column.
    // --------------------------------------------------------

    let headersNow =
        Array.from(headerRow.children);

    const listingIndex =
        headersNow.findIndex(
            th =>
                th.textContent.trim() === "Listing"
        );

    const modelIndex =
        headersNow.findIndex(
            th =>
                th.textContent.trim() === "Model"
        );

    if (
        listingIndex >= 0
        && modelIndex >= 0
    ) {

        Array.from(
            body.querySelectorAll(":scope > tr")
        ).forEach(row => {

            if (
                row.children.length
                !== headerRow.children.length
            ) {
                return;
            }

            const listingCell =
                row.children[listingIndex];

            const modelCell =
                row.children[modelIndex];

            if (
                !listingCell
                || !modelCell
            ) {
                return;
            }

            const model =
                modelCell.textContent.trim();

            if (
                model
                && model !== "—"
            ) {

                // Locate the existing specification line.
                const smalls =
                    listingCell.querySelectorAll(
                        ".small"
                    );

                let specLine = null;

                for (const el of smalls) {

                    const text =
                        el.textContent || "";

                    if (
                        /GB|RAM|SSD|HDD|NVMe|i[3579]-|Ryzen/i.test(
                            text
                        )
                    ) {
                        specLine = el;
                        break;
                    }
                }

                if (specLine) {

                    const existing =
                        specLine.textContent.trim();

                    if (
                        !existing
                            .toLowerCase()
                            .includes(
                                model.toLowerCase()
                            )
                    ) {
                        specLine.textContent =
                            model
                            + " / "
                            + existing;
                    }

                } else {

                    const line =
                        document.createElement(
                            "div"
                        );

                    line.className =
                        "small";

                    line.textContent =
                        model;

                    listingCell.appendChild(
                        line
                    );
                }
            }
        });


        // Remove Model column after copying its contents.
        headerRow.children[
            modelIndex
        ].remove();

        Array.from(
            body.querySelectorAll(":scope > tr")
        ).forEach(row => {

            if (
                row.children[modelIndex]
            ) {
                row.children[
                    modelIndex
                ].remove();
            }
        });
    }


    // --------------------------------------------------------
    // Merge Value/reference + sold evidence tooltip into
    // Undervaluation.
    // --------------------------------------------------------

    headersNow =
        Array.from(headerRow.children);

    let valueIndex =
        headersNow.findIndex(
            th =>
                th.textContent.trim()
                    .includes("Value")
                || th.textContent.trim()
                    .includes(
                        "asking reference"
                    )
        );

    let undervalueIndex =
        headersNow.findIndex(
            th =>
                th.textContent.trim()
                    === "Undervaluation"
                || th.textContent.trim()
                    .includes("undervalue")
        );

    let confidenceIndex =
        headersNow.findIndex(
            th =>
                th.textContent.trim()
                    === "Confidence"
        );


    if (
        valueIndex >= 0
        && undervalueIndex >= 0
    ) {

        Array.from(
            body.querySelectorAll(":scope > tr")
        ).forEach(row => {

            if (
                row.children.length
                !== headerRow.children.length
            ) {
                return;
            }

            const valueCell =
                row.children[valueIndex];

            const underCell =
                row.children[
                    undervalueIndex
                ];

            const confidenceCell =
                confidenceIndex >= 0
                    ? row.children[
                        confidenceIndex
                    ]
                    : null;

            if (
                !valueCell
                || !underCell
            ) {
                return;
            }


            // Build one clean hover panel.
            const hover =
                document.createElement(
                    "div"
                );

            hover.className =
                "evidence-tooltip";


            const valueText =
                valueCell.textContent
                    .replace(
                        /Comparable middle 50%[^£]*/gi,
                        ""
                    )
                    .replace(
                        /not a confidence interval/gi,
                        ""
                    )
                    .trim();


            const valueBlock =
                document.createElement(
                    "div"
                );

            valueBlock.className =
                "summary";

            valueBlock.textContent =
                valueText;

            hover.appendChild(
                valueBlock
            );


            // Move existing sold-evidence tooltip out of Confidence.
            if (confidenceCell) {

                const oldBadge =
                    confidenceCell.querySelector(
                        ".evidence-badge"
                    );

                if (oldBadge) {

                    const oldTooltip =
                        oldBadge.querySelector(
                            ".evidence-tooltip"
                        );

                    if (oldTooltip) {

                        Array.from(
                            oldTooltip.children
                        ).forEach(child => {

                            hover.appendChild(
                                child.cloneNode(
                                    true
                                )
                            );
                        });
                    }


                    // Replace "Sales: [badge]" with plain sales count.
                    const count =
                        oldBadge.childNodes.length
                            ? (
                                oldBadge.childNodes[0]
                                    .textContent
                                || "0"
                            ).trim()
                            : "0";

                    oldBadge.replaceWith(
                        document.createTextNode(
                            count
                        )
                    );
                }
            }


            // Wrap existing undervaluation text with hover trigger.
            const wrapper =
                document.createElement(
                    "span"
                );

            wrapper.className =
                "evidence-badge";

            wrapper.style.minWidth =
                "auto";

            wrapper.style.padding =
                "2px 6px";

            wrapper.style.whiteSpace =
                "nowrap";


            while (
                underCell.firstChild
            ) {
                wrapper.appendChild(
                    underCell.firstChild
                );
            }

            wrapper.appendChild(
                hover
            );

            underCell.appendChild(
                wrapper
            );
        });


        // Remove standalone Value/reference column.
        headerRow.children[
            valueIndex
        ].remove();

        Array.from(
            body.querySelectorAll(":scope > tr")
        ).forEach(row => {

            if (
                row.children[valueIndex]
            ) {
                row.children[
                    valueIndex
                ].remove();
            }
        });
    }


    // --------------------------------------------------------
    // Final header labels after column removal.
    // --------------------------------------------------------

    Array.from(
        headerRow.children
    ).forEach(th => {

        const text =
            th.textContent.trim();

        if (text === "Delivered") {
            th.textContent = "Price";
        }

        if (
            text === "Apparent undervalue"
            || text === "Apparent undervaluation"
        ) {
            th.textContent =
                "Undervaluation";
        }

        if (text === "Listing age") {
            th.textContent = "Age";
        }
    });

    // --------------------------------------------------------
    // Sorting
    // --------------------------------------------------------

    const headers =
        Array.from(
            headerRow.children
        );

    const numericNames = new Set([
        "Delivered",
        "Value / asking reference",
        "Apparent undervalue",
        "Score"
    ]);


    function sortValue(
        row,
        index,
        name
    ) {

        const td =
            row.children[index];

        if (!td) {
            return "";
        }

        if (
            td.dataset.sort !== undefined
        ) {
            return Number(
                td.dataset.sort
            );
        }

        const text =
            td.textContent.trim();

        if (name === "Listing age") {
            return ageSeconds(text);
        }

        if (name === "Confidence") {
            return confidenceRank(text);
        }

        if (numericNames.has(name)) {
            return numericValue(text);
        }

        return text.toLowerCase();
    }


    function applySort(
        index,
        direction,
        save=true
    ) {

        const headersNow =
            Array.from(
                headerRow.children
            );

        const th =
            headersNow[index];

        if (!th) {
            return;
        }

        const name =
            th.dataset.sortName
            || th.childNodes[0]?.textContent?.trim()
            || th.textContent.trim();

        headersNow.forEach(other => {

            other.classList.remove(
                "sorted-asc",
                "sorted-desc"
            );

            const marker =
                other.querySelector(
                    ".sort-indicator"
                );

            if (marker) {
                marker.textContent = "";
            }
        });


        const rows =
            Array.from(
                body.querySelectorAll(
                    ":scope > tr"
                )
            ).filter(
                row =>
                    row.children.length
                    === headersNow.length
            );


        rows.sort((a, b) => {

            const av =
                sortValue(
                    a,
                    index,
                    name
                );

            const bv =
                sortValue(
                    b,
                    index,
                    name
                );

            let result;

            if (
                typeof av === "number"
                && typeof bv === "number"
            ) {
                result = av - bv;

            } else {

                result =
                    String(av).localeCompare(
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


        rows.forEach(
            row => body.appendChild(row)
        );


        th.classList.add(
            direction === "asc"
            ? "sorted-asc"
            : "sorted-desc"
        );

        const marker =
            th.querySelector(
                ".sort-indicator"
            );

        if (marker) {
            marker.textContent =
                direction === "asc"
                ? "▲"
                : "▼";
        }


        if (save) {

            localStorage.setItem(
                SORT_KEY,
                JSON.stringify({
                    name,
                    direction
                })
            );
        }
    }


    headers.forEach((th, index) => {

        const name =
            th.textContent.trim();

        th.dataset.sortName =
            name;

        th.classList.add(
            "sortable-header"
        );

        let indicator =
            th.querySelector(
                ".sort-indicator"
            );

        if (!indicator) {

            indicator =
                document.createElement(
                    "span"
                );

            indicator.className =
                "sort-indicator";

            th.appendChild(
                indicator
            );
        }


        th.addEventListener(
            "click",
            () => {

                let direction = "asc";

                if (
                    th.classList.contains(
                        "sorted-asc"
                    )
                ) {
                    direction = "desc";
                }

                applySort(
                    index,
                    direction,
                    true
                );
            }
        );
    });


    // --------------------------------------------------------
    // Restore selected ordering after auto-refresh.
    // --------------------------------------------------------

    try {

        const saved =
            JSON.parse(
                localStorage.getItem(
                    SORT_KEY
                )
                || "null"
            );

        if (
            saved
            && saved.name
            && saved.direction
        ) {

            const index =
                Array.from(
                    headerRow.children
                ).findIndex(
                    th =>
                        th.dataset.sortName
                        === saved.name
                );

            if (index >= 0) {

                applySort(
                    index,
                    saved.direction,
                    false
                );
            }
        }

    } catch (e) {

        console.warn(
            "Could not restore dashboard sort",
            e
        );
    }

});
</script>
"""


def dashboard_html():
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

    if "</body>" in page:
        page = page.replace(
            "</body>",
            _DASHBOARD_UI_ENHANCEMENT
            + "\n</body>",
            1,
        )
    else:
        page += _DASHBOARD_UI_ENHANCEMENT

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
    recheck_active_bin_listings(
        conn,
        token,
        ACTIVE_BIN_RECHECKS_PER_CYCLE
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
