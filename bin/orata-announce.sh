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
AUDIO_DIR="${ORATA_AUDIO_DIR:-/var/lib/orata/announce}"
AUDIO_KEEP="${ORATA_AUDIO_KEEP:-50}"
RECORD_AUDIO="${ORATA_RECORD_AUDIO:-1}"
TTS="${ORATA_TTS:-}"
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

# --- channel 0: local render -- hear it without an Echo ---------------
# Writes a WAV the web UI can play back. Entirely local: no network, no
# credentials, nothing to expire. Runs first so a recording exists even
# if every remote channel hangs. Never fails the script.
#
# This proves what was SAID, not that Amazon spoke it. For that, read the
# "alexa OK" line.
announce_audio() {
    [ "$RECORD_AUDIO" = "1" ] || return 0

    local tts="$TTS"
    if [ -z "$tts" ]; then
        for c in espeak-ng espeak; do
            if command -v "$c" >/dev/null 2>&1; then tts="$c"; break; fi
        done
    fi
    if [ -z "$tts" ]; then
        log "audio SKIP ${NUM} -- no espeak-ng/espeak on PATH"
        return 0
    fi

    mkdir -p "$AUDIO_DIR" 2>/dev/null

    # The web UI only serves names matching ^\d{8}-\d{6}-\d{3,15}\.wav$,
    # so the number must be digits or the file is unreachable from there.
    local safe
    safe="$(printf '%s' "$NUM" | tr -cd '0-9')"
    [ -n "$safe" ] || safe="000"

    local f="${AUDIO_DIR}/$(date +%Y%m%d-%H%M%S)-${safe}.wav"
    if timeout 10 "$tts" -w "$f" "$PHRASE" >/dev/null 2>&1 && [ -s "$f" ]; then
        log "audio OK   ${NUM} ${f}"
    else
        rm -f "$f" 2>/dev/null
        log "audio FAIL ${NUM} -- ${tts} could not write ${f}"
    fi

    # Ring buffer. Without this the SSD fills one call at a time.
    ls -1t "$AUDIO_DIR"/*.wav 2>/dev/null | tail -n +$((AUDIO_KEEP + 1)) \
        | while IFS= read -r old; do rm -f "$old"; done
}

announce_audio

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