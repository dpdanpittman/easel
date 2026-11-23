#!/bin/bash
# Started by cage (systemd/kiosk-browser.service). Waits for the local server, then runs Chromium full screen.
# Chromium may only resolve localhost: every other host name fails, so the page cannot reach the network and the
# browser does not phone home. IP-literal URLs skip the resolver, so every request also goes to a proxy that does not
# exist (127.0.0.1:9); Chromium always connects to localhost/127.0.0.1 directly. The server adds a
# Content-Security-Policy that lets the page load only from itself. The profile holds no secrets.
# KIOSK_URL and CHROMIUM override the defaults.
set -u
URL="${KIOSK_URL:-http://127.0.0.1:8090/}"
CHROMIUM="${CHROMIUM:-$(command -v chromium || command -v chromium-browser || echo /snap/bin/chromium)}"
for _ in $(seq 1 60); do
  if /usr/bin/python3 -I -c 'import urllib.request,sys; urllib.request.urlopen(sys.argv[1], timeout=2)' "${URL}api/status" 2>/dev/null; then
    break
  fi
  sleep 2
done
exec "$CHROMIUM" \
  --kiosk "$URL" \
  --ozone-platform=wayland \
  --user-data-dir="${XDG_STATE_HOME:-$HOME/.local/state}/kiosk/chromium" \
  --no-first-run --noerrdialogs --disable-infobars --disable-translate \
  --disable-session-crashed-bubble --hide-crash-restore-bubble \
  --disable-features=Translate,MediaRouter,OptimizationHints,AutofillServerCommunication \
  --disable-background-networking --disable-component-update --disable-sync --disable-breakpad \
  --metrics-recording-only --no-default-browser-check --password-store=basic \
  --check-for-update-interval=31536000 --overscroll-history-navigation=0 --disable-pinch \
  --host-resolver-rules="MAP * ~NOTFOUND, EXCLUDE localhost, EXCLUDE 127.0.0.1" \
  --proxy-server="http://127.0.0.1:9"
