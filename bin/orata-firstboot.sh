#!/bin/sh
# orata-firstboot.sh -- make a cloned appliance image unique before it serves.
#
# A cloned image means every unit on earth ships byte-identical. Anything
# secret baked into it is public the moment one image leaks. This runs once,
# on first boot, BEFORE orata-web is allowed to start, and generates the
# per-device state that must never be shared:
#
#   * admin password (random, shown on the console, must be changed at login)
#   * web session secret
#   * SSH host keys (regenerated -- the image must not ship working ones)
#   * machine-id
#
# Idempotent: it stamps /var/lib/orata/.firstboot-done and exits early after.
set -eu

STAMP=/var/lib/orata/.firstboot-done
CONF=/etc/orata/web.conf
AUTH=/var/lib/orata/admin-auth.json

[ "$(id -u)" = "0" ] || { echo "must run as root" >&2; exit 1; }

if [ -e "$STAMP" ]; then
    echo "orata-firstboot: already provisioned; nothing to do"
    exit 0
fi

echo "orata-firstboot: provisioning this device"

install -d -m 755 /etc/orata
install -d -m 700 -o asterisk -g asterisk /var/lib/orata
install -d -m 700 -o asterisk -g asterisk /var/lib/orata/config-backups

# --- SSH host keys ---------------------------------------------------
# A shipped host key lets anyone with the image MITM every appliance.
if [ -d /etc/ssh ]; then
    rm -f /etc/ssh/ssh_host_*
    ssh-keygen -A >/dev/null 2>&1 || true
    echo "  regenerated SSH host keys"
fi

# --- machine-id ------------------------------------------------------
if [ -f /etc/machine-id ]; then
    : >/etc/machine-id
    systemd-machine-id-setup >/dev/null 2>&1 || true
    echo "  regenerated machine-id"
fi

# --- admin password --------------------------------------------------
# Human-typeable: no ambiguous characters, grouped. This is shown once on
# the console and in the UI's forced-change screen.
PASS="$(tr -dc 'abcdefghjkmnpqrstuvwxyz23456789' </dev/urandom | head -c 12)"
PRETTY="$(printf '%s' "$PASS" | sed 's/\(....\)/\1-/g; s/-$//')"

# PBKDF2-SHA256. Python is already a dependency of the UI.
python3 - "$PASS" "$AUTH" <<'PY'
import base64, hashlib, json, os, sys
password, dest = sys.argv[1], sys.argv[2]
salt = os.urandom(16)
digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)
record = {
    "algo": "pbkdf2_sha256",
    "iterations": 200_000,
    "salt": base64.b64encode(salt).decode(),
    "hash": base64.b64encode(digest).decode(),
    "username": "admin",
    "must_change": True,
    "created": __import__("time").strftime("%Y-%m-%dT%H:%M:%S"),
}
tmp = dest + ".new"
with open(tmp, "w") as fh:
    json.dump(record, fh, indent=1)
os.chmod(tmp, 0o600)
os.replace(tmp, dest)
PY
chown asterisk:asterisk "$AUTH"
chmod 600 "$AUTH"
echo "  generated admin password"

# --- web session secret ---------------------------------------------
SECRET="$(head -c 24 /dev/urandom | base64 | tr '+/' '-_' | tr -d '=')"
if [ -f "$CONF" ]; then
    if grep -q '^ORATA_WEB_SECRET=' "$CONF"; then
        sed -i "s|^ORATA_WEB_SECRET=.*|ORATA_WEB_SECRET=$SECRET|" "$CONF"
    else
        printf 'ORATA_WEB_SECRET=%s\n' "$SECRET" >>"$CONF"
    fi
    # The legacy single-token path is disabled on appliance images: the
    # token is in the shipped file and therefore public.
    sed -i "s|^ORATA_WEB_TOKEN=.*|ORATA_WEB_TOKEN=|" "$CONF" || true
    chown asterisk:asterisk "$CONF"
    chmod 600 "$CONF"
    echo "  generated web session secret"
fi

touch "$STAMP"
chmod 600 "$STAMP"

IP="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src") print $(i+1)}' | head -1)"
[ -n "$IP" ] || IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
[ -n "$IP" ] || IP="this-pi"

cat <<EOF

  ====================================================================
   ORATA is ready to configure.

     http://$IP:8088/

     username:  admin
     password:  $PRETTY

   You must change this password at first login. Write it down now --
   it is not shown again. To reset it:

     sudo rm /var/lib/orata/.firstboot-done && sudo systemctl \\
       start orata-firstboot

   This appliance has no TLS. Keep it on your home LAN; never
   port-forward it. Remote access over WireGuard only.
  ====================================================================

EOF