"""Durable Product Research transitions and a retryable private alert outbox.

State and its notifications are committed together by the supplied writer.
Telegram has no idempotency key: a crash after acceptance but before recording
delivery can repeat a message. Event IDs make such repeats identifiable.
"""

import threading
import time
import uuid


class Operations:
    def __init__(self, read, write, send, clock=time.time):
        self.read = read
        self.write = write
        self.send = send
        self.clock = clock
        self.lock = threading.RLock()
        self.delivery_lock = threading.Lock()

    def state(self):
        return self.read() or {}

    def commit(self, state, message=None, updates=None):
        """Caller holds lock; updates are committed in the same transaction."""
        if message:
            event_id = uuid.uuid4().hex
            state.setdefault("pending", []).append({
                "id": event_id,
                "text": message + "\n\nObserved: " + self.utc(self.clock()) + "\nEvent: " + event_id,
                "created_at": self.clock(),
                "attempts": 0,
                "retry_at": 0,
            })
        self.write(state, updates or {})

    def volume(self, count, limit, release_at, circuit_open=False):
        with self.lock:
            state = self.state()
            waiting = release_at > self.clock()
            previous = bool(state.get("volume_waiting"))
            message = None
            if waiting and not previous:
                message = (
                    "eBay Product Research waiting for volume allowance\n\n"
                    "No action required. The valuation backlog is preserved.\n"
                    f"Rolling 24h requests: {count}; normal-request limit: {limit}.\n"
                    f"Next eligible normal request: {self.utc(release_at)}.\n"
                    "Recovery probes can exceed the normal-request limit."
                )
            elif previous and not waiting:
                message = (
                    "eBay Product Research volume allowance available\n\n"
                    f"Rolling 24h requests: {count}; normal-request limit: {limit}.\n"
                    + ("The circuit is still open; recovery is required."
                       if circuit_open else
                       "Normal requests are eligible, subject to rate backoff and cadence.")
                )
            changed = (previous != waiting or state.get("volume_release_at") != release_at
                       or state.get("rolling_24h_count") != count)
            state.update(volume_waiting=waiting, volume_release_at=release_at,
                         rolling_24h_count=count)
            if changed:
                self.commit(state, message)

    @staticmethod
    def utc(epoch):
        from datetime import datetime, timezone
        return datetime.fromtimestamp(epoch, timezone.utc).isoformat()

    def throttled(self, retry_at, interval):
        with self.lock:
            state = self.state()
            message = None if state.get("rate_limited") else (
                "eBay Product Research rate limited\n\n"
                "No action required. HTTP 429 triggered automatic backoff; backlog preserved.\n"
                f"Backoff ends: {self.utc(retry_at)}. Minimum interval: {interval:.1f}s.\n"
                "The volume guard can delay the next request further."
            )
            state.update(rate_limited=True, rate_retry_at=retry_at,
                         failure_count=0, failure_alerted=False)
            self.commit(state, message)

    def failure(self, reason):
        with self.lock:
            state = self.state()
            count = int(state.get("failure_count") or 0) + 1
            state.update(failure_count=count, last_failure=reason)
            message = None
            if count >= 3 and not state.get("failure_alerted"):
                state["failure_alerted"] = True
                message = (
                    "eBay Product Research repeatedly failing\n\n"
                    f"{count} consecutive helper/response failures. Last outcome: {reason}.\n"
                    "The valuation backlog is preserved. Automatic checks continue; "
                    "inspect the helper if failures persist."
                )
            self.commit(state, message)

    def healthy(self, circuit_recovered=False):
        with self.lock:
            state = self.state()
            had_alert = state.get("rate_limited") or state.get("failure_alerted")
            changed = had_alert or state.get("failure_count")
            state.update(rate_limited=False, failure_count=0, failure_alerted=False)
            message = None
            if had_alert and not circuit_recovered:
                message = (
                    "eBay Product Research response healthy again\n\n"
                    "A real sold search succeeded. Rate/failure warnings cleared.\n"
                    + ("Normal processing still waits for rolling-volume allowance."
                       if state.get("volume_waiting") else
                       "Normal requests are eligible, subject to cadence.")
                )
            if changed:
                self.commit(state, message)

    def deliver_one(self):
        """One sender; preserve FIFO order and retry state across restarts."""
        if not self.delivery_lock.acquire(blocking=False):
            return False
        try:
            with self.lock:
                pending = self.state().get("pending") or []
                if not pending or pending[0].get("retry_at", 0) > self.clock():
                    return False
                event = dict(pending[0])
            try:
                ok, error = self.send(event["text"])
            except Exception as exc:
                ok, error = False, type(exc).__name__
            with self.lock:
                state = self.state()
                pending = state.get("pending") or []
                if not pending or pending[0]["id"] != event["id"]:
                    return False
                if ok:
                    pending.pop(0)
                    state["last_delivered_at"] = self.utc(self.clock())
                    state["last_delivered_event"] = event["id"]
                else:
                    event["attempts"] += 1
                    event["last_error"] = str(error)[:300]
                    event["retry_at"] = self.clock() + min(3600, 30 * 2 ** min(event["attempts"] - 1, 7))
                    pending[0] = event
                state["pending"] = pending
                self.commit(state)
            print("Product Research private alert: "
                  f"event={event['id']} "
                  + ("delivered" if ok else
                     f"delivery failed; retry at {self.utc(event['retry_at'])}"),
                  flush=True)
            return bool(ok)
        finally:
            self.delivery_lock.release()
