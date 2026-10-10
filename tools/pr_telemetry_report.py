#!/usr/bin/env python3
import re
import sys
from collections import defaultdict

LINE_RE = re.compile(
    r"Product Research telemetry: PR #(?P<num>\d+) "
    r"\+(?P<elapsed>\d+:\d{2}:\d{2}) "
    r"interval=(?P<interval>[\d.]+)s "
    r"outcome=(?P<outcome>\S+) "
    r"rolling=1m:(?P<m1>\d+),5m:(?P<m5>\d+),"
    r"15m:(?P<m15>\d+),30m:(?P<m30>\d+),60m:(?P<m60>\d+)"
    r"(?:,6h:(?P<h6>\d+),12h:(?P<h12>\d+))?"
    r"(?:,24h:(?P<h24>\d+))? "
    r"session:(?P<session>\d+)"
)

TIMESTAMP_RE = re.compile(r"^(?P<ts>\d{4}-\d{2}-\d{2}T\S+)\s+")

events = []
for raw in sys.stdin:
    m = LINE_RE.search(raw)
    if not m:
        continue
    d = m.groupdict()
    t = TIMESTAMP_RE.match(raw)
    d["timestamp"] = t.group("ts") if t else ""
    for key in ("num","m1","m5","m15","m30","m60","session"):
        d[key] = int(d[key])
    d["h6"] = int(d["h6"]) if d.get("h6") is not None else None
    d["h12"] = int(d["h12"]) if d.get("h12") is not None else None
    d["h24"] = int(d["h24"]) if d.get("h24") is not None else None
    d["interval"] = float(d["interval"])
    events.append(d)

if not events:
    print("No Product Research telemetry lines found.")
    raise SystemExit(1)

sessions = defaultdict(list)
for e in events:
    sessions[e["session"] if False else 0].append(e)

# A process session is identified by PR numbering resetting to 1.
processes = []
current = []
last_num = None
for e in events:
    if last_num is not None and e["num"] <= last_num:
        processes.append(current)
        current = []
    current.append(e)
    last_num = e["num"]
if current:
    processes.append(current)

print(f"Telemetry lines: {len(events)}")
print(f"Process sessions: {len(processes)}")
print()

challenge_no = 0
for p_idx, proc in enumerate(processes, 1):
    challenges = [e for e in proc if e["outcome"] == "CHALLENGE"]
    if not challenges:
        continue

    for ch in challenges:
        challenge_no += 1
        prior = [e for e in proc if e["num"] < ch["num"]]
        prior_ok = [e for e in prior if e["outcome"] == "OK"]
        previous = prior[-1] if prior else None

        print(f"Challenge {challenge_no}  (process session {p_idx})")
        if ch["timestamp"]:
            print(f"  time:             {ch['timestamp']}")
        print(f"  PR request:       #{ch['num']}")
        print(f"  elapsed:          {ch['elapsed']}")
        print(f"  interval:         {ch['interval']:.1f}s")
        print(f"  prior OKs:        {len(prior_ok)}")
        print(
            "  rolling volume:   "
            f"1m={ch['m1']} 5m={ch['m5']} 15m={ch['m15']} "
            f"30m={ch['m30']} 60m={ch['m60']}"
            + (
                f" 6h={ch['h6']} 12h={ch['h12']}"
                if ch.get("h6") is not None
                else ""
            )
        )
        if ch.get("h24") is not None:
            print(f"  rolling 24h:      {ch['h24']}")
        if previous:
            print(
                f"  previous outcome: {previous['outcome']} "
                f"at PR #{previous['num']}"
            )

        at_interval = [
            e for e in prior
            if abs(e["interval"] - ch["interval"]) < 0.01
        ]
        ok_at_interval = [e for e in at_interval if e["outcome"] == "OK"]
        print(
            f"  prior requests at {ch['interval']:.1f}s: "
            f"{len(at_interval)} ({len(ok_at_interval)} OK)"
        )
        print()

# Summary for the current 30-second experiment.
ch30 = [e for e in events if e["outcome"] == "CHALLENGE" and abs(e["interval"] - 30.0) < 0.01]
print("30-second challenge summary")
print("---------------------------")
if not ch30:
    print("No CHALLENGE telemetry at 30.0s found.")
else:
    for i, ch in enumerate(ch30, 1):
        print(
            f"{i}. PR #{ch['num']} at +{ch['elapsed']} | "
            f"rolling 15m={ch['m15']}, 30m={ch['m30']}, 60m={ch['m60']}"
        )
