#!/bin/bash
#
# orata-announce.sh NUMBER [NAME] [ALEXA_ENDPOINT_ID]
#
# Announce an incoming call. Called from the dialplan, backgrounded.
# MUST NOT block and MUST NOT fail loudly -- a broken notifier must never
# affect call handling. Always exits 0.
#

set -u

NUM="${1:-unknown}"
NAME="${2:-}"
SENSOR="${3:-}"

[ -r /etc/orata/announce.conf ] && . /etc/orata/announce.conf

ARC="${ORATA_ARC:-/usr/local/bin/alexa_remote_control.sh}"
DEVICES="${ORATA_ALEXA_DEVICES:-ALL}"
ALEXA_CMD="${ORATA_ALEXA_CMD:-announce}"
NTFY_URL="${ORATA_NTFY_URL:-}"
SPEAK_DIGITS="${ORATA_SPEAK_DIGITS:-0}"
LOG="${ORATA_LOG:-/var/log/orata/announce.log}"

# arc | sensor | both | off
ALEXA_MODE="${ORATA_ALEXA_MODE:-arc}"
SENSOR_SH="${ORATA_SENSOR_SH:-/usr/local/bin/orata-alexa-sensor.sh}"
SENSOR_DEFAULT="${ORATA_SENSOR_DEFAULT:-}"

log() { printf '%s %s\n' "$(date -Is)" "$*" >>"$LOG" 2>/dev/null; }

if [ -n "$NAME" ]; then
    PHRASE="Call from ${NAME}"
elif [ "$SPEAK_DIGITS" = "1" ]; then
    # comma-separate so Alexa reads digits, not a cardinal number
    PHRASE="Call from $(printf '%s' "$NUM" | sed 's/./&, /g')"
else
    PHRASE="Call from an unknown number"
fi

# --- channel 1: ntfy -- boring, reliable, never breaks ----------------
if [ -n "$NTFY_URL" ]; then
    timeout 10 curl -fsS \
        -H "Title: Incoming call" \
        -H "Tags: telephone_receiver" \
        -d "${NAME:-Unknown} (${NUM})" \
        "$NTFY_URL" >/dev/null 2>&1 \
        || log "ntfy  FAIL ${NUM}"
fi

# --- channel 2a: Alexa via alexa_remote_control -----------------------
# Dynamic text, unofficial, expect annual breakage.
announce_arc() {
    if [ -x "$ARC" ]; then
        if timeout 20 "$ARC" -d "$DEVICES" -e "${ALEXA_CMD}:${PHRASE}" >/dev/null 2>&1; then
            log "alexa OK   ${NUM} \"${PHRASE}\""
        else
            log "alexa FAIL ${NUM} -- cookie expired? re-run: sudo -u asterisk ${ARC} -a"
        fi
    else
        log "alexa SKIP ${NUM} -- ${ARC} not executable"
    fi
}

# --- channel 2b: Alexa via virtual contact sensor ---------------------
# Static text (lives in the Routine), official endpoints, auth does not rot.
# Caller-specific sensor from the dialplan; falls back to the default.
announce_sensor() {
    local ep="${SENSOR:-$SENSOR_DEFAULT}"
    if [ -z "$ep" ]; then
        log "sensor SKIP ${NUM} -- no sensor mapped and no ORATA_SENSOR_DEFAULT"
        return
    fi
    if [ -x "$SENSOR_SH" ]; then
        "$SENSOR_SH" "$ep"
    else
        log "sensor SKIP ${NUM} -- ${SENSOR_SH} not executable"
    fi
}

case "$ALEXA_MODE" in
    arc)    announce_arc ;;
    sensor) announce_sensor ;;
    both)   announce_arc; announce_sensor ;;
    off)    log "alexa OFF  ${NUM}" ;;
    *)      log "alexa FAIL ${NUM} -- bad ORATA_ALEXA_MODE=${ALEXA_MODE}" ;;
esac

exit 0