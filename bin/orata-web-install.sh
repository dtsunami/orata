#!/bin/bash
#
# orata-web-install.sh -- one-shot installer for the orata web admin UI.
#
#   sudo ./bin/orata-web-install.sh                 # bind auto-detected LAN IP
#   sudo ./bin/orata-web-install.sh --bind 127.0.0.1
#   sudo ./bin/orata-web-install.sh --bind 0.0.0.0  # every interface, see below
#
# Idempotent. Safe to re-run after editing the repo copy of orata_web.py --
# it reinstalls the binary and restarts the service without touching an
# existing token.
#
# It does NOT edit pjsip.conf, voicemail.conf, announce.conf or
# extensions.conf. Those are hand-edited on purpose; see docs/web-ui.md.
#

set -eu

BIND=""
PORT=8088

while [ $# -gt 0 ]; do
    case "$1" in
        --bind) BIND="${2:-}"; shift 2 ;;
        --port) PORT="${2:-}"; shift 2 ;;
        -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 1 ;;
    esac
done

[ "$(id -u)" = "0" ] || { echo "must run as root (use sudo)" >&2; exit 1; }

REPO="$(cd "$(dirname "$0")/.." && pwd)"
CONF=/etc/orata/web.conf
UNIT=/etc/systemd/system/orata-web.service

for f in bin/orata_web.py etc/web.conf etc/orata-web.service; do
    [ -r "$REPO/$f" ] || { echo "missing $REPO/$f -- run from the repo" >&2; exit 1; }
done

id asterisk >/dev/null 2>&1 || { echo "no 'asterisk' user -- is Asterisk installed?" >&2; exit 1; }

# --- bind address ----------------------------------------------------
if [ -z "$BIND" ]; then
    BIND="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src") print $(i+1)}' | head -1)"
    [ -n "$BIND" ] || BIND=127.0.0.1
    echo "==> auto-detected bind address: $BIND"
fi

ANY=0
case "$BIND" in
    0.0.0.0|::) ANY=1 ;;
esac

# --- packages --------------------------------------------------------
echo "==> installing python3-fastapi python3-uvicorn"
apt-get install -y python3-fastapi python3-uvicorn

# --- directories -----------------------------------------------------
# ProtectSystem=strict means systemd refuses to start the unit if any
# ReadWritePaths entry does not exist.
echo "==> creating directories"
install -d -m 755 /etc/orata
install -d -m 755 -o asterisk -g asterisk /var/log/orata
install -d -m 700 -o asterisk -g asterisk /var/lib/orata

# --- binary ----------------------------------------------------------
echo "==> installing /usr/local/bin/orata_web.py"
install -m 755 "$REPO/bin/orata_web.py" /usr/local/bin/orata_web.py

# --- privileged helper -----------------------------------------------
# The UI cannot write /etc/asterisk or pair Bluetooth devices; this root
# service does, behind an enumerated unix-socket protocol.
echo "==> installing privileged helper (orata-admin)"
install -m 755 "$REPO/bin/orata-admin-helper.py" /usr/local/bin/orata-admin-helper.py
install -m 755 "$REPO/bin/orata-firstboot.sh" /usr/local/bin/orata-firstboot.sh
install -d -m 700 -o asterisk -g asterisk /var/lib/orata/config-backups
install -m 644 "$REPO/etc/orata-admin.service" /etc/systemd/system/
install -m 644 "$REPO/etc/orata-firstboot.service" /etc/systemd/system/

# --- config ----------------------------------------------------------
if [ -f "$CONF" ]; then
    echo "==> keeping existing $CONF"
else
    echo "==> installing $CONF"
    install -m 600 -o asterisk -g asterisk "$REPO/etc/web.conf" "$CONF"
fi

TOKEN="$(sed -n 's/^ORATA_WEB_TOKEN=//p' "$CONF" | head -1)"
if [ -z "$TOKEN" ]; then
    # base64url alphabet. A literal '+' in a query string decodes to a SPACE,
    # so a plain-base64 token makes the one-shot ?token= link fail with
    # "orata: missing or bad token". '-' and '_' are query-safe.
    TOKEN="$(head -c 24 /dev/urandom | base64 | tr '+/' '-_' | tr -d '=')"
    echo "==> generated admin token"
else
    echo "==> keeping existing admin token"
fi

# Percent-encode for the printed URL so that a pre-existing token containing
# '+' or '/' still yields a working link.
URL_TOKEN="$(printf '%s' "$TOKEN" \
    | sed -e 's/%/%25/g' -e 's/+/%2B/g' -e 's|/|%2F|g' -e 's/=/%3D/g')"

# base64 never contains '|', so it is a safe sed delimiter here
sed -i \
    -e "s|^ORATA_WEB_BIND=.*|ORATA_WEB_BIND=$BIND|" \
    -e "s|^ORATA_WEB_PORT=.*|ORATA_WEB_PORT=$PORT|" \
    -e "s|^ORATA_WEB_TOKEN=.*|ORATA_WEB_TOKEN=$TOKEN|" \
    -e "s|^ORATA_WEB_ALLOW_ANY_BIND=.*|ORATA_WEB_ALLOW_ANY_BIND=$ANY|" \
    "$CONF"

chown asterisk:asterisk "$CONF"
chmod 600 "$CONF"

# --- service ---------------------------------------------------------
echo "==> installing systemd unit"
install -m 644 "$REPO/etc/orata-web.service" "$UNIT"
systemctl daemon-reload
systemctl enable orata-admin >/dev/null 2>&1 || true
systemctl restart orata-admin
systemctl enable orata-firstboot >/dev/null 2>&1 || true
systemctl enable orata-web >/dev/null 2>&1 || true
systemctl restart orata-web

if ! systemctl is-active --quiet orata-admin; then
    echo "!! orata-admin is not running; Trunk and Bluetooth pages will show" >&2
    echo "   'helper unavailable'. Check: journalctl -u orata-admin -n 20" >&2
fi

sleep 2
if ! systemctl is-active --quiet orata-web; then
    echo
    echo "!! orata-web did not start. Last 20 log lines:" >&2
    journalctl -u orata-web -n 20 --no-pager >&2
    exit 1
fi

# --- done ------------------------------------------------------------
URL_HOST="$BIND"
[ "$ANY" = "1" ] && URL_HOST="$(hostname -I 2>/dev/null | awk '{print $1}')"
[ -n "$URL_HOST" ] || URL_HOST=127.0.0.1

echo
echo "orata-web is running."
echo
echo "  http://$URL_HOST:$PORT/?token=$URL_TOKEN"
echo
echo "Open that once; the token moves into an HttpOnly cookie and the URL"
echo "cleans itself. Token is stored in $CONF (mode 600)."
echo

if [ "$ANY" = "1" ]; then
    cat <<'WARN'
WARNING: bound to 0.0.0.0 -- every interface, including wifi and any guest
VLAN that reaches this host. There is no TLS and no rate limit on the token.
This UI runs as the asterisk user, so a compromise is a compromise of call
routing, on a trunk that can dial premium-rate numbers.

Acceptable only if BOTH hold:
  1. no port-forward reaches this host (the README invariant), and
  2. you trust every device on the LAN, including IoT things.

Narrower option that needs no override flag:
  sudo ./bin/orata-web-install.sh --bind <this-pi-lan-ip>

WARN
fi

echo "Logs:  journalctl -u orata-web -f"