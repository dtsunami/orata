#!/bin/bash
#
# Runs INSIDE the container. Expects the repo read-only at /repo.
#
# Verifies everything that does not require a SIP packet:
#   - scripts parse, no CRLF damage
#   - extensions.conf / pjsip.conf actually load into Asterisk
#   - astdb key names written by orata-cnam.sh match what the dialplan reads
#   - orata-web.py fails closed, authenticates, and its writes land in astdb
#   - orata-announce.sh and orata-alexa-sensor.sh degrade quietly
#   - the README's espeak+sox prompt recipe produces a file Asterisk can read
#
# A green run means "parses and plumbs correctly". It does NOT mean the
# phone works.
#

set -u

PASS=0; FAIL=0; SKIP=0
ok()   { printf '  \033[32mPASS\033[0m  %s\n' "$*"; PASS=$((PASS+1)); }
no()   { printf '  \033[31mFAIL\033[0m  %s\n' "$*"; FAIL=$((FAIL+1)); }
skip() { printf '  \033[33mNOTE\033[0m  %s\n' "$*"; SKIP=$((SKIP+1)); }
sect() { printf '\n\033[1m== %s\033[0m\n' "$*"; }

AST() { asterisk -rx "$*" 2>/dev/null; }

#=====================================================================
sect "source hygiene"
#=====================================================================

crlf=0
for f in /repo/bin/*.sh /repo/bin/*.py /repo/asterisk/*.conf /repo/etc/*.conf; do
    [ -f "$f" ] || continue
    if grep -q $'\r' "$f" 2>/dev/null; then
        crlf=$((crlf+1))
    fi
done
if [ "$crlf" -eq 0 ]; then
    ok "no CRLF line endings in repo sources"
else
    skip "$crlf file(s) have CRLF -- stripped for this run, but you MUST run"
    skip "     sed -i 's/\\r\$//' ... on the Pi or bash says 'bad interpreter'"
fi

#=====================================================================
sect "deploy (mirrors the README install steps)"
#=====================================================================

install -d /etc/orata /var/log/orata /var/lib/orata
install -d -o asterisk -g asterisk /var/run/asterisk

cp /repo/asterisk/extensions.conf /repo/asterisk/pjsip.conf /etc/asterisk/ \
    && ok "configs copied to /etc/asterisk" || no "config copy failed"
cp /repo/bin/orata-cnam.sh /repo/bin/orata-announce.sh \
   /repo/bin/orata-alexa-sensor.sh /repo/bin/orata-web.py /usr/local/bin/ \
    && ok "scripts copied to /usr/local/bin" || no "script copy failed"
cp /repo/etc/announce.conf /etc/orata/

chmod 755 /usr/local/bin/orata-*
sed -i 's/\r$//' /usr/local/bin/orata-* /etc/asterisk/extensions.conf \
    /etc/asterisk/pjsip.conf /etc/orata/*.conf 2>/dev/null
chown -R asterisk:asterisk /var/log/orata /var/lib/orata /etc/orata
chown asterisk:asterisk /etc/asterisk/extensions.conf /etc/asterisk/pjsip.conf
chmod 640 /etc/asterisk/pjsip.conf
chmod 700 /var/lib/orata

#=====================================================================
sect "syntax"
#=====================================================================

for s in orata-cnam.sh orata-announce.sh orata-alexa-sensor.sh; do
    if err="$(bash -n "/usr/local/bin/$s" 2>&1)"; then
        ok "bash -n $s"
    else
        no "bash -n $s -- $err"
    fi
done

if err="$(python3 -m py_compile /usr/local/bin/orata-web.py 2>&1)"; then
    ok "py_compile orata-web.py"
else
    no "py_compile orata-web.py -- $err"
fi

#=====================================================================
sect "asterisk startup"
#=====================================================================

asterisk -U asterisk -G asterisk >/tmp/ast-start.log 2>&1
started=0
for _ in $(seq 1 40); do
    if AST "core show version" | grep -qi asterisk; then started=1; break; fi
    sleep 1
done
if [ "$started" -eq 1 ]; then
    ok "asterisk running -- $(AST 'core show version' | head -1)"
else
    no "asterisk did not start; see /tmp/ast-start.log"
    printf '\n%s\n' "$(tail -20 /tmp/ast-start.log)"
    printf '\nPASS %d  FAIL %d  NOTE %d\n' "$PASS" "$FAIL" "$SKIP"
    exit 1
fi

#=====================================================================
sect "extensions.conf"
#=====================================================================

dp="$(AST 'dialplan show from-voipms')"
if printf '%s' "$dp" | grep -qi "no existence"; then
    no "from-voipms context did not load -- parse error in extensions.conf"
else
    ok "from-voipms context loaded ($(printf '%s' "$dp" | grep -c '\[pbx_config\]') priorities)"
fi

for app in "NoOp" "System" "Dial" "VoiceMail" "Read" "Playback"; do
    if printf '%s' "$dp" | grep -q "$app("; then
        ok "dialplan: $app() present"
    else
        no "dialplan: $app() missing -- did a priority get dropped?"
    fi
done

for ctx in internal; do
    if AST "dialplan show $ctx" | grep -qi "no existence"; then
        no "$ctx context did not load"
    else
        ok "$ctx context loaded"
    fi
done

if AST "dialplan show *99@internal" | grep -q "System("; then
    ok "*99 announce test extension present"
else
    no "*99 extension missing"
fi

#=====================================================================
sect "pjsip.conf"
#=====================================================================

eps="$(AST 'pjsip show endpoints')"
for e in voipms pc mobile desk; do
    if printf '%s' "$eps" | grep -q "Endpoint:  $e"; then
        ok "endpoint $e parsed"
    else
        no "endpoint $e missing -- pjsip.conf parse problem"
    fi
done
skip "registration to newyork.voip.ms is expected to FAIL here (stub POP,"
skip "     stub credentials, and container networking). Not tested."

#=====================================================================
sect "orata-cnam.sh <-> astdb <-> dialplan"
#=====================================================================
# The real risk is a mismatch between the key orata-cnam.sh writes and the
# key extensions.conf reads. Test both ends.

N=15551234567
B=15558675309

su -s /bin/bash asterisk -c "/usr/local/bin/orata-cnam.sh add $N 'Mom'" >/dev/null 2>&1
if AST "database show cnam" | grep -q "$N"; then
    ok "cnam add wrote /cnam/$N"
else
    no "cnam add did not land in astdb"
fi
if [ "$(AST "dialplan eval function \${DB(cnam/$N)}" | tail -1)" = "Mom" ]; then
    ok "dialplan reads DB(cnam/$N) = Mom  <-- key names agree"
else
    no "dialplan cannot read back DB(cnam/$N) -- key mismatch or quoting bug"
fi

su -s /bin/bash asterisk -c "/usr/local/bin/orata-cnam.sh block-add $B" >/dev/null 2>&1
if [ "$(AST "dialplan eval function \${DB(block/$B)}" | tail -1)" = "1" ]; then
    ok "dialplan reads DB(block/$B) = 1  <-- blocklist wired correctly"
else
    no "blocklist key mismatch between orata-cnam.sh and extensions.conf"
fi

su -s /bin/bash asterisk -c "/usr/local/bin/orata-cnam.sh allow-add $B" >/dev/null 2>&1
if [ "$(AST "dialplan eval function \${DB(allow/$B)}" | tail -1)" = "1" ]; then
    ok "allow/ key agrees"
else
    no "allow/ key mismatch"
fi

su -s /bin/bash asterisk -c "/usr/local/bin/orata-cnam.sh sensor-add $N orata-mom" >/dev/null 2>&1
if [ "$(AST "dialplan eval function \${DB(alexasensor/$N)}" | tail -1)" = "orata-mom" ]; then
    ok "alexasensor/ key agrees"
else
    no "alexasensor/ key mismatch"
fi

su -s /bin/bash asterisk -c "/usr/local/bin/orata-cnam.sh del $N" >/dev/null 2>&1
if AST "database show cnam" | grep -q "$N"; then
    no "cnam del did not remove the key"
else
    ok "cnam del removed the key"
fi

#=====================================================================
sect "orata-announce.sh"
#=====================================================================

: >/var/log/orata/announce.log
chown asterisk:asterisk /var/log/orata/announce.log

su -s /bin/bash asterisk -c "/usr/local/bin/orata-announce.sh $N 'Mom'"
rc=$?
[ $rc -eq 0 ] && ok "announce exits 0 with no ARC present" \
              || no "announce exited $rc -- it must ALWAYS exit 0"

if grep -q "alexa SKIP" /var/log/orata/announce.log; then
    ok "announce logged 'alexa SKIP' (arc mode, no script installed)"
else
    no "expected 'alexa SKIP' in announce.log; got: $(cat /var/log/orata/announce.log)"
fi

sed -i 's/^ORATA_ALEXA_MODE=.*/ORATA_ALEXA_MODE=sensor/' /etc/orata/announce.conf
su -s /bin/bash asterisk -c "/usr/local/bin/orata-announce.sh $N 'Mom' orata-mom"
rc=$?
[ $rc -eq 0 ] && ok "announce exits 0 in sensor mode with no credentials" \
              || no "announce exited $rc in sensor mode"
if grep -q "sensor SKIP" /var/log/orata/announce.log; then
    ok "sensor path degrades quietly without LWA credentials"
else
    no "expected 'sensor SKIP'; got: $(tail -3 /var/log/orata/announce.log)"
fi
sed -i 's/^ORATA_ALEXA_MODE=.*/ORATA_ALEXA_MODE=arc/' /etc/orata/announce.conf

su -s /bin/bash asterisk -c "/usr/local/bin/orata-alexa-sensor.sh orata-mom"
rc=$?
[ $rc -eq 0 ] && ok "orata-alexa-sensor.sh exits 0 without credentials" \
              || no "orata-alexa-sensor.sh exited $rc"

#=====================================================================
sect "orata-web.py"
#=====================================================================

printf 'ORATA_WEB_BIND=127.0.0.1\nORATA_WEB_PORT=8089\nORATA_WEB_TOKEN=\n' >/tmp/w-notoken.conf
if ORATA_WEB_CONF=/tmp/w-notoken.conf timeout 10 python3 /usr/local/bin/orata-web.py >/dev/null 2>&1; then
    no "SECURITY: started with an empty token -- must fail closed"
else
    ok "refuses to start with an empty token"
fi

printf 'ORATA_WEB_BIND=0.0.0.0\nORATA_WEB_PORT=8089\nORATA_WEB_TOKEN=x\n' >/tmp/w-any.conf
if ORATA_WEB_CONF=/tmp/w-any.conf timeout 10 python3 /usr/local/bin/orata-web.py >/dev/null 2>&1; then
    no "SECURITY: bound 0.0.0.0 without the override flag"
else
    ok "refuses to bind 0.0.0.0 without ORATA_WEB_ALLOW_ANY_BIND=1"
fi

TOK="test-token-$$"
cat >/etc/orata/web.conf <<EOF
ORATA_WEB_BIND=127.0.0.1
ORATA_WEB_PORT=8088
ORATA_WEB_TOKEN=$TOK
ORATA_CNAM_SH=/usr/local/bin/orata-cnam.sh
ORATA_WEB_LOG=/var/log/orata/announce.log
EOF
chown asterisk:asterisk /etc/orata/web.conf
chmod 600 /etc/orata/web.conf

su -s /bin/bash asterisk -c \
  "ORATA_WEB_CONF=/etc/orata/web.conf python3 /usr/local/bin/orata-web.py" \
  >/tmp/web.log 2>&1 &
WEBPID=$!
for _ in $(seq 1 20); do
    curl -s -o /dev/null http://127.0.0.1:8088/ 2>/dev/null && break
    sleep 0.5
done

code() { curl -s -o /tmp/body -w '%{http_code}' "$@"; }

c="$(code http://127.0.0.1:8088/)"
[ "$c" = "401" ] && ok "unauthenticated GET / -> 401" \
                 || no "unauthenticated GET / -> $c (expected 401)"

c="$(code -H "X-Orata-Token: wrong" http://127.0.0.1:8088/)"
[ "$c" = "401" ] && ok "bad token -> 401" || no "bad token -> $c (expected 401)"

c="$(code -H "X-Orata-Token: $TOK" http://127.0.0.1:8088/)"
[ "$c" = "200" ] && ok "valid token -> 200" || no "valid token -> $c (expected 200)"

c="$(code "http://127.0.0.1:8088/?token=$TOK")"
[ "$c" = "303" ] && ok "?token= -> 303 redirect (sets cookie)" \
                 || no "?token= -> $c (expected 303)"

# does db_show()'s regex actually parse `database show` output?
if grep -q "$B" /tmp/body 2>/dev/null; then
    ok "rendered page lists astdb rows (ROW_RE parses CLI output)"
else
    curl -s -H "X-Orata-Token: $TOK" http://127.0.0.1:8088/ >/tmp/body
    if grep -q "$B" /tmp/body; then
        ok "rendered page lists astdb rows (ROW_RE parses CLI output)"
    else
        no "page did not list $B -- db_show() regex does not match Asterisk output"
    fi
fi

W=15557654321
c="$(code -X POST -H "X-Orata-Token: $TOK" \
      -d "family=block&number=$W" http://127.0.0.1:8088/add)"
[ "$c" = "303" ] && ok "POST /add -> 303" || no "POST /add -> $c (expected 303)"
if [ "$(AST "dialplan eval function \${DB(block/$W)}" | tail -1)" = "1" ]; then
    ok "web write reached astdb AND the dialplan can read it"
else
    no "web -> orata-cnam.sh -> astdb chain broken"
fi

c="$(code -X POST -H "X-Orata-Token: $TOK" \
      -d "family=block&number=notanumber" http://127.0.0.1:8088/add)"
if [ "$c" = "303" ] && grep -q "invalid" /tmp/body 2>/dev/null; then
    ok "non-numeric input rejected"
else
    AST "database show block" | grep -q "notanumber" \
        && no "SECURITY: non-numeric input reached astdb" \
        || ok "non-numeric input did not reach astdb"
fi

c="$(code -X POST -H "X-Orata-Token: $TOK" \
      -d "family=block&number=$W" http://127.0.0.1:8088/del)"
if [ "$(AST "dialplan eval function \${DB(block/$W)}" | tail -1)" = "1" ]; then
    no "web delete did not remove the key"
else
    ok "web delete removed the key"
fi

c="$(code -X POST -d "family=block&number=15550000000" http://127.0.0.1:8088/add)"
[ "$c" = "401" ] && ok "unauthenticated POST -> 401" \
                 || no "SECURITY: unauthenticated POST -> $c (expected 401)"

kill $WEBPID 2>/dev/null

#=====================================================================
sect "press-one prompt (README recipe)"
#=====================================================================

mkdir -p /usr/share/asterisk/sounds/en/custom
if espeak -w /tmp/p1.wav "Press 1 to continue." 2>/dev/null \
   && sox /tmp/p1.wav -r 8000 -c 1 -t gsm \
        /usr/share/asterisk/sounds/en/custom/press-one.gsm 2>/dev/null; then
    ok "espeak + sox produced press-one.gsm"
    if AST "file convert /usr/share/asterisk/sounds/en/custom/press-one.gsm /tmp/verify.wav" \
         | grep -qi "converted"; then
        ok "asterisk can read press-one.gsm"
    else
        no "asterisk could not convert press-one.gsm -- wrong format?"
    fi
else
    no "espeak/sox recipe from the README failed"
fi

#=====================================================================
sect "known gaps"
#=====================================================================

if AST "voicemail show users" | grep -q "^100"; then
    ok "mailbox 100 exists"
else
    skip "NO MAILBOX 100. VoiceMail(100@default) will fail on every"
    skip "     unanswered call. Debian ships voicemail.conf with mailboxes"
    skip "     commented out and this repo does not provide one."
fi

skip "NOT TESTED HERE (needs real SIP): registration, RTP/audio, the"
skip "     press-1 gate's Read() timing, actual call routing, Alexa."

#=====================================================================
printf '\n\033[1m----------------------------------------\033[0m\n'
printf '\033[1mPASS %d   FAIL %d   NOTE %d\033[0m\n' "$PASS" "$FAIL" "$SKIP"
printf '\033[1m----------------------------------------\033[0m\n'
[ "$FAIL" -eq 0 ] \
    && printf 'Config/plumbing layer is sound. The phone is still untested.\n' \
    || printf 'Fix the failures above before touching the Pi.\n'
exit $([ "$FAIL" -eq 0 ] && echo 0 || echo 1)