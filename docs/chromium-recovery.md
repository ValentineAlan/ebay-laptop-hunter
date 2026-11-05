# Chromium stall recovery on TrueNAS

Two independent layers recover the failure where Chromium accepts a WebSocket
connection but never completes Playwright initialization or a browser operation.

* The helper image runs a supervisor outside Playwright. A worker that makes no
  loop progress for 180 seconds is killed with its Playwright subprocesses and
  replaced. The browser profile and research database are untouched.
* A host cron job runs once a minute, with an exclusive lock. It connects over CDP,
  executes Browser.getVersion and a trivial expression in each existing tab, and
  checks the helper status heartbeat. It makes no eBay network requests. The probe
  is killed inside the container after 22 seconds; the host Docker command also
  has a 25-second deadline.

Three failed checks trigger helper recovery. If CDP is still unresponsive after
three probes, the watchdog stops the helper, restarts the browser, and starts the
helper again. Recovery is limited to twice per rolling hour, at least 15 minutes
apart, with history persisted before any restart. Stopped or absent containers
are left alone. Invalid history fails closed. Use a dedicated state directory per
stack and disable the cron job during app maintenance.

CAPTCHA, sign-in and rate-limit responses are not restart triggers: this probe
checks local responsiveness, not the eBay response. A responsive browser on a
challenge page remains responsive. Normal research pacing and circuit state are
unchanged. CPU/memory/process counts and OOM/restart metadata are logged before
recovery; cookies, environment variables and page content are not logged.

## Install

Use the reviewed commit or branch in place of REVISION below. Rebuild the
sid-helper image from that same revision to install the supervisor, following
[the existing deployment instructions](truenas-updates.md). Preserve the live
TrueNAS YAML and all persistent volumes; do not start a second Compose stack.
The host watchdog can also recover the currently deployed helper without a
rebuild, but the independent worker deadline requires the new image.

As root on TrueNAS:

```sh
mkdir -p /mnt/Decathlon/appdata/laptoplander-watchdog
chmod 700 /mnt/Decathlon/appdata/laptoplander-watchdog
curl --fail --location https://raw.githubusercontent.com/ValentineAlan/ebay-laptop-hunter/REVISION/tools/chromium_watchdog.py -o /mnt/Decathlon/appdata/laptoplander-watchdog/chromium_watchdog.py
python3 /mnt/Decathlon/appdata/laptoplander-watchdog/chromium_watchdog.py --state-dir /mnt/Decathlon/appdata/laptoplander-watchdog/state
```

In TrueNAS **System Settings → Advanced → Cron Jobs**, add a root job, enabled,
with schedule `* * * * *` and this command:

```sh
/usr/bin/python3 /mnt/Decathlon/appdata/laptoplander-watchdog/chromium_watchdog.py --state-dir /mnt/Decathlon/appdata/laptoplander-watchdog/state
```

Keep stdout/stderr available through cron reporting. Healthy checks produce no
output. Failed checks and recovery actions produce timestamped output. The
watchdog does not send Telegram messages. Its defaults target only:

* `ix-ebay-hunter-stack-sid-helper-1`
* `ix-ebay-hunter-stack-browser-1`

Override `--helper` and `--browser` only if the deployed names differ. The NAS
needs Python 3 and Docker; the helper image supplies Playwright and GNU timeout.

## Verify and rollback

Run the command manually first. On a healthy deployment it exits silently;
`state/state.json` shows `failures: 0`. Review live hunter logs separately for
successful research/valuations: CDP responsiveness alone does not prove eBay
access or suitable sold evidence. Do not force a live browser hang to test it.

Disable the cron job to disable host recovery. Restore the previous helper image
revision and redeploy to remove worker supervision. Neither step requires data
or browser-profile deletion. Do not delete state history while recovery is active,
as that resets the restart budget.
