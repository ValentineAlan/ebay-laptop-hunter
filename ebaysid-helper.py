#!/usr/bin/env python3

import hashlib
import json
import os
import re
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright


DATA = Path(
    os.environ.get(
        "HUNTER_DATA",
        "/data",
    )
)

CDP_URL = os.environ.get(
    "EBAY_CDP_URL",
    "http://127.0.0.1:9222",
)

SID_FILE = DATA / "ebaysid.current"

REFRESH_REQUEST_FILE = (
    DATA / "ebaysid.refresh-request"
)

STATUS_FILE = (
    DATA / "ebaysid-helper-status.json"
)

RESEARCH_REQUEST_FILE = (
    DATA / "product-research-browser-request.json"
)

RESEARCH_RESPONSE_FILE = (
    DATA / "product-research-browser-response.json"
)

STATUS_SECONDS = float(
    os.environ.get(
        "EBAY_SID_POLL_SECONDS",
        "5",
    )
)

REQUEST_POLL_SECONDS = float(
    os.environ.get(
        "EBAY_RESEARCH_POLL_SECONDS",
        "0.20",
    )
)


RECOVERY_SIGNAL_FILE = DATA / "product-research-recovery.signal"
MANUAL_CHALLENGE_FILE = DATA / "product-research-manual-challenge.json"


def now_iso():
    return datetime.now(
        timezone.utc
    ).isoformat()


def sid_hash(value):
    if not value:
        return None

    return hashlib.sha256(
        value.encode("utf-8")
    ).hexdigest()[:12]


def atomic_write(path, text):
    tmp = path.with_suffix(
        path.suffix + ".tmp"
    )

    tmp.write_text(
        text,
        encoding="utf-8",
    )

    os.replace(
        tmp,
        path,
    )


def atomic_json(path, payload):
    atomic_write(
        path,
        json.dumps(
            payload,
            indent=2,
            sort_keys=True,
        ),
    )


def read_json(path):
    try:
        value = json.loads(
            path.read_text(
                encoding="utf-8"
            )
        )

        return (
            value
            if isinstance(value, dict)
            else {}
        )

    except Exception:
        return {}


def current_sid(context):
    for cookie in context.cookies(
        "https://www.ebay.co.uk"
    ):
        if cookie.get("name") == "ebaysid":
            return cookie.get("value")

    return None


def choose_page(context):
    # Prefer an already-open Product Research page.
    for page in context.pages:
        if (
            "ebay.co.uk/sh/research"
            in (page.url or "")
        ):
            return page

    # Otherwise prefer any eBay page.
    for page in context.pages:
        if (
            "ebay.co.uk"
            in (page.url or "")
        ):
            return page

    if context.pages:
        return context.pages[0]

    return context.new_page()


def research_url(keywords):
    params = {
        "marketplace": "EBAY-UK",
        "keywords": (
            keywords
            or "Dell Latitude 5340"
        ),
        "dayRange": "90",
        "offset": "0",
        "limit": "50",
        "tabName": "SOLD",
        "tz": "Europe/London",
    }

    return (
        "https://www.ebay.co.uk/sh/research?"
        + urllib.parse.urlencode(params)
    )


def page_state(page):
    url = page.url or ""

    try:
        title = page.title()
    except Exception:
        title = ""

    low = (
        title
        + " "
        + url
    ).lower()

    if (
        "splashui" in low
        or "pardon our interruption" in low
        or "security measure" in low
    ):
        return (
            "CHALLENGED",
            title,
            url,
        )

    if (
        "signin.ebay" in low
        or "sign in" in low
    ):
        return (
            "SIGNIN_REQUIRED",
            title,
            url,
        )

    return (
        "CONNECTED",
        title,
        url,
    )


def write_status(
    status,
    sid=None,
    **extra,
):
    previous = read_json(
        STATUS_FILE
    )

    payload = dict(
        previous
    )

    if (
        status != "ERROR"
        and "last_error" not in extra
    ):
        payload.pop(
            "last_error",
            None,
        )

    payload.update(
        extra
    )

    payload.update({
        "status": status,
        "last_seen_at": now_iso(),
        "ebaysid_hash": sid_hash(sid),
        "cdp_url": CDP_URL,
    })

    atomic_json(
        STATUS_FILE,
        payload,
    )


def save_sid_if_changed(sid):
    if not sid:
        return False

    old = None

    try:
        old = (
            SID_FILE
            .read_text(
                encoding="utf-8"
            )
            .strip()
            or None
        )
    except OSError:
        pass

    if old == sid:
        return False

    atomic_write(
        SID_FILE,
        sid,
    )

    status = read_json(
        STATUS_FILE
    )

    status["last_sid_change_at"] = now_iso()
    status["ebaysid_hash"] = sid_hash(sid)
    status["last_seen_at"] = now_iso()

    status.setdefault(
        "status",
        "CONNECTED",
    )

    if status.get("status") != "ERROR":
        status.pop(
            "last_error",
            None,
        )

    atomic_json(
        STATUS_FILE,
        status,
    )

    print(
        f"ebaysid updated: {sid_hash(sid)}",
        flush=True,
    )

    return True


def ensure_ebay_context(
    context,
    page,
):
    url = page.url or ""

    if "ebay.co.uk" in url:
        return page

    page.goto(
        "https://www.ebay.co.uk/sh/research",
        wait_until="domcontentloaded",
        timeout=60000,
    )

    return page


def process_refresh_request(context):
    if not REFRESH_REQUEST_FILE.exists():
        return

    request = read_json(
        REFRESH_REQUEST_FILE
    )

    requested_at = request.get(
        "requested_at"
    )

    keywords = (
        request.get("keywords")
        or "Dell Latitude 5340"
    )

    before = current_sid(
        context
    )

    print(
        "refresh requested"
        + (
            f" at {requested_at}"
            if requested_at
            else ""
        )
        + f"; current sid={sid_hash(before)}",
        flush=True,
    )

    page = choose_page(
        context
    )

    try:
        page.goto(
            research_url(keywords),
            wait_until="domcontentloaded",
            timeout=60000,
        )

        time.sleep(2)

        after = current_sid(
            context
        )

        state, title, url = page_state(
            page
        )

        save_sid_if_changed(
            after
        )

        write_status(
            state,
            after,
            last_refresh_request_at=(
                requested_at
                or now_iso()
            ),
            last_refresh_completed_at=now_iso(),
            page_title=title,
            page_url=url,
            refresh_changed_sid=(
                after != before
            ),
        )

        print(
            "refresh complete: "
            f"state={state} "
            f"sid={sid_hash(after)} "
            f"changed={after != before}",
            flush=True,
        )

    except Exception as exc:
        write_status(
            "ERROR",
            before,
            last_refresh_request_at=(
                requested_at
                or now_iso()
            ),
            last_error=repr(exc),
        )

        print(
            "refresh error:",
            repr(exc),
            flush=True,
        )

    finally:
        try:
            REFRESH_REQUEST_FILE.unlink()
        except FileNotFoundError:
            pass



def mark_manual_challenge(page, request_id=None):
    """
    Put the visible browser on Product Research so the user can complete
    eBay's verification interactively.

    Recovery is only signalled after a real CHALLENGED page has been observed
    and subsequently becomes CONNECTED.
    """
    payload = {
        "challenged_at": now_iso(),
        "request_id": request_id,
        "challenge_seen_in_page": False,
    }

    try:
        page.goto(
            "https://www.ebay.co.uk/sh/research",
            wait_until="domcontentloaded",
            timeout=60000,
        )

        time.sleep(1)

        state, title, url = page_state(page)

        payload.update({
            "page_state": state,
            "page_title": title,
            "page_url": url,
            "challenge_seen_in_page": (
                state == "CHALLENGED"
            ),
        })

        print(
            "Product Research manual verification page: "
            f"state={state} url={url}",
            flush=True,
        )

    except Exception as exc:
        payload["navigation_error"] = repr(exc)

        print(
            "Product Research challenge-page navigation error:",
            repr(exc),
            flush=True,
        )

    atomic_json(
        MANUAL_CHALLENGE_FILE,
        payload,
    )



def watch_manual_recovery(context):
    """
    Watch for genuine manual Product Research recovery.

    Closing tabs, opening a blank page, or browsing ordinary eBay pages does
    not count as recovery. We only emit the recovery signal when a real
    ebay.co.uk/sh/research page is visible and is no longer challenged.
    """
    if not MANUAL_CHALLENGE_FILE.exists():
        return

    marker = read_json(
        MANUAL_CHALLENGE_FILE
    )

    pages = list(
        context.pages
    )

    # Do not create a new page here. The user may intentionally have closed
    # every Chromium tab to let the session remain completely idle.
    if not pages:
        return

    research_page = None

    for candidate in pages:
        url = (
            candidate.url
            or ""
        ).lower()

        if (
            "ebay.co.uk/sh/research"
            in url
            or "ebay.co.uk/splashui/"
            in url
        ):
            research_page = candidate
            break

    # Browsing ordinary eBay is useful as a manual test, but it is not proof
    # that Product Research itself has recovered.
    if research_page is None:
        return

    state, title, url = page_state(
        research_page
    )

    seen = bool(
        marker.get(
            "challenge_seen_in_page"
        )
    )

    if state == "CHALLENGED":
        if not seen:
            marker[
                "challenge_seen_in_page"
            ] = True

            marker[
                "challenge_seen_at"
            ] = now_iso()

            marker[
                "page_title"
            ] = title

            marker[
                "page_url"
            ] = url

            atomic_json(
                MANUAL_CHALLENGE_FILE,
                marker,
            )

            print(
                "Product Research block visible in Chromium",
                flush=True,
            )

        return

    if not seen:
        return

    lowered_url = (
        url
        or ""
    ).lower()

    # Strong requirement: the page that looks recovered must actually be
    # Product Research, not eBay home/search/account/etc.
    if (
        "ebay.co.uk/sh/research"
        not in lowered_url
    ):
        return

    if state == "CONNECTED":
        atomic_write(
            RECOVERY_SIGNAL_FILE,
            now_iso(),
        )

        try:
            MANUAL_CHALLENGE_FILE.unlink()
        except FileNotFoundError:
            pass

        print(
            "Product Research page appears available again; "
            "immediate recovery probe requested",
            flush=True,
        )

ALLOWED_RESEARCH_HOST = "www.ebay.co.uk"
ALLOWED_RESEARCH_PATH = "/sh/research/api/search"


def valid_research_request(request_id, url):
    if not isinstance(request_id, str) or not re.fullmatch(r"[0-9a-f]{32}", request_id):
        return False
    if not isinstance(url, str):
        return False
    try:
        parsed = urllib.parse.urlsplit(url)
    except Exception:
        return False
    return (
        parsed.scheme == "https"
        and parsed.hostname == ALLOWED_RESEARCH_HOST
        and parsed.port in (None, 443)
        and parsed.path == ALLOWED_RESEARCH_PATH
        and not parsed.username
        and not parsed.password
    )


def process_research_request(context):
    if not RESEARCH_REQUEST_FILE.exists():
        return

    request = read_json(
        RESEARCH_REQUEST_FILE
    )

    request_id = request.get(
        "request_id"
    )

    url = request.get(
        "url"
    )

    if not valid_research_request(request_id, url):
        try:
            RESEARCH_REQUEST_FILE.unlink()
        except FileNotFoundError:
            pass

        return

    print(
        "Product Research browser request: "
        f"{request_id[:12]}",
        flush=True,
    )

    response_payload = {
        "request_id": request_id,
        "completed_at": now_iso(),
    }

    try:
        page = choose_page(
            context
        )

        page = ensure_ebay_context(
            context,
            page,
        )

        result = page.evaluate(
            """
            async (url) => {
                const controller = new AbortController();
                const timer = setTimeout(() => controller.abort(), 40000);
                try {
                    const response = await fetch(url, {
                        method: "GET",
                        credentials: "include",
                        signal: controller.signal,
                        headers: {
                            "Accept": "*/*",
                            "X-Requested-With": "XMLHttpRequest"
                        }
                    });

                    const body = await response.text();

                    return {
                        ok: response.ok,
                        status: response.status,
                        content_type:
                            response.headers.get("content-type") || "",
                        retry_after:
                            response.headers.get("retry-after") || "",
                        final_url: response.url || "",
                        body: body
                    };

                } catch (error) {
                    return {
                        ok: false,
                        status: 0,
                        content_type: "",
                        retry_after: "",
                        final_url: "",
                        body: "",
                        error: String(error)
                    };
                } finally {
                    clearTimeout(timer);
                }
            }
            """,
            url,
        )

        if not isinstance(
            result,
            dict,
        ):
            raise RuntimeError(
                "Browser fetch returned no result"
            )

        response_payload.update(
            result
        )

        body = str(
            result.get("body")
            or ""
        )

        lowered = body.lower()

        sid = current_sid(
            context
        )

        if (
            "pardon our interruption"
            in lowered
            or "splashui/challenge"
            in lowered
            or "security measure"
            in lowered
        ):
            state = "CHALLENGED"

        elif (
            "signin.ebay"
            in lowered
            or "auth_required"
            in lowered
            or "invalid_session"
            in lowered
        ):
            state = "SIGNIN_REQUIRED"

        elif int(
            result.get("status")
            or 0
        ) == 200:
            state = "CONNECTED"

        else:
            state = "ERROR"

        write_status(
            state,
            sid,
            last_browser_fetch_at=now_iso(),
            last_browser_fetch_status=(
                result.get("status")
            ),
            last_browser_fetch_content_type=(
                result.get("content_type")
            ),
        )

        print(
            "Product Research browser response: "
            f"{request_id[:12]} "
            f"HTTP={result.get('status')} "
            f"bytes={len(body.encode('utf-8'))} "
            f"state={state}",
            flush=True,
        )

        # The fetch response itself does not automatically navigate the
        # visible Chromium tab. If eBay challenged the Product Research API,
        # explicitly put the browser on Product Research so the user can see
        # and complete the verification.
        if state == "CHALLENGED":
            mark_manual_challenge(
                page,
                request_id=request_id,
            )

    except Exception as exc:
        response_payload.update({
            "status": 0,
            "body": "",
            "error": repr(exc),
        })

        write_status(
            "ERROR",
            current_sid(context),
            last_browser_fetch_at=now_iso(),
            last_error=repr(exc),
        )

        print(
            "Product Research browser error:",
            repr(exc),
            flush=True,
        )

    finally:
        atomic_json(
            RESEARCH_RESPONSE_FILE,
            response_payload,
        )

        # A timed-out caller may already have removed request A and
        # published request B at the same pathname while A was still running.
        # Never let A's cleanup delete B.
        try:
            current_request = read_json(
                RESEARCH_REQUEST_FILE
            )

            if (
                current_request.get("request_id")
                == request_id
            ):
                RESEARCH_REQUEST_FILE.unlink()

        except FileNotFoundError:
            pass

def main():
    DATA.mkdir(
        parents=True,
        exist_ok=True,
    )

    print(
        "eBay Chromium helper starting; "
        f"CDP={CDP_URL}",
        flush=True,
    )

    while True:
        try:
            with sync_playwright() as p:
                browser = p.chromium.connect_over_cdp(
                    CDP_URL,
                    timeout=10000,
                )

                if not browser.contexts:
                    raise RuntimeError(
                        "No Chromium browser context"
                    )

                context = browser.contexts[0]

                print(
                    "connected to Chromium",
                    flush=True,
                )

                next_status = 0.0

                while True:
                    now = time.monotonic()

                    if now >= next_status:
                        sid = current_sid(
                            context
                        )

                        save_sid_if_changed(
                            sid
                        )

                        page = choose_page(
                            context
                        )

                        state, title, url = page_state(
                            page
                        )

                        write_status(
                            (
                                state
                                if sid
                                else "NO_SESSION"
                            ),
                            sid,
                            page_title=title,
                            page_url=url,
                        )

                        next_status = (
                            now
                            + STATUS_SECONDS
                        )

                    process_refresh_request(
                        context
                    )

                    process_research_request(
                        context
                    )

                    watch_manual_recovery(
                        context
                    )

                    time.sleep(
                        REQUEST_POLL_SECONDS
                    )

        except KeyboardInterrupt:
            raise

        except Exception as exc:
            write_status(
                "ERROR",
                last_error=repr(exc),
            )

            print(
                "helper connection error:",
                repr(exc),
                flush=True,
            )

            time.sleep(5)


if __name__ == "__main__":
    main()
