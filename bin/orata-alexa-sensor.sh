#!/bin/bash
#
# orata-alexa-sensor.sh ENDPOINT_ID
#
# Trip a virtual Alexa contact sensor, then reset it. An Alexa Routine
# bound to that sensor speaks the phrase. The phrase text lives in the
# Routine (Alexa app), NOT here -- that is the whole point of this path:
# no dynamic text means no cookie, no MFA, no annual breakage.
#
# Talks directly to the Alexa Event Gateway. Lambda is setup-time only.
# Always exits 0. Never blocks longer than ~RESET_DELAY + timeouts.
#

set -u

EPID="${1:-}"

[ -r /etc/orata/announce.conf ] && . /etc/orata/announce.conf

CLIENT_ID="${ORATA_LWA_CLIENT_ID:-}"
CLIENT_SECRET="${ORATA_LWA_CLIENT_SECRET:-}"
REFRESH_TOKEN="${ORATA_LWA_REFRESH_TOKEN:-}"
GATEWAY="${ORATA_ALEXA_GATEWAY:-https://api.amazonalexa.com/v3/events}"
STATE_DIR="${ORATA_STATE_DIR:-/var/lib/orata}"
RESET_DELAY="${ORATA_SENSOR_RESET:-3}"
LOG="${ORATA_LOG:-/var/log/orata/announce.log}"

TOKEN_CACHE="${STATE_DIR}/alexa.token"

log() { printf '%s %s\n' "$(date -Is)" "$*" >>"$LOG" 2>/dev/null; }

[ -n "$EPID" ] || { log "sensor SKIP -- no endpointId"; exit 0; }

if [ -z "$CLIENT_ID" ] || [ -z "$CLIENT_SECRET" ] || [ -z "$REFRESH_TOKEN" ]; then
    log "sensor SKIP ${EPID} -- LWA credentials not set in announce.conf"
    exit 0
fi

command -v jq >/dev/null 2>&1 || { log "sensor FAIL ${EPID} -- jq not installed"; exit 0; }

# --- access token: cached until 60s before expiry ---------------------
get_token() {
    if [ -r "$TOKEN_CACHE" ]; then
        local exp tok
        exp="$(cut -d' ' -f1 <"$TOKEN_CACHE" 2>/dev/null)"
        tok="$(cut -d' ' -f2- <"$TOKEN_CACHE" 2>/dev/null)"
        if [ -n "$exp" ] && [ -n "$tok" ] && [ "$(date +%s)" -lt "$exp" ]; then
            printf '%s' "$tok"
            return 0
        fi
    fi

    local resp tok ttl
    resp="$(timeout 10 curl -fsS -X POST https://api.amazon.com/auth/o2/token \
        -H 'Content-Type: application/x-www-form-urlencoded' \
        --data-urlencode 'grant_type=refresh_token' \
        --data-urlencode "refresh_token=${REFRESH_TOKEN}" \
        --data-urlencode "client_id=${CLIENT_ID}" \
        --data-urlencode "client_secret=${CLIENT_SECRET}" 2>/dev/null)" || return 1

    tok="$(printf '%s' "$resp" | jq -r '.access_token // empty')"
    ttl="$(printf '%s' "$resp" | jq -r '.expires_in // 3600')"
    [ -n "$tok" ] || return 1

    mkdir -p "$STATE_DIR" 2>/dev/null
    ( umask 077; printf '%s %s\n' "$(( $(date +%s) + ttl - 60 ))" "$tok" >"$TOKEN_CACHE" ) 2>/dev/null
    printf '%s' "$tok"
}

# --- one ChangeReport -------------------------------------------------
# $1 = DETECTED | NOT_DETECTED
send_state() {
    local state="$1" token="$2" body
    body="$(jq -nc \
        --arg mid "$(cat /proc/sys/kernel/random/uuid 2>/dev/null || date +%s%N)" \
        --arg tok "$token" \
        --arg ep "$EPID" \
        --arg st "$state" \
        --arg ts "$(date -u +%Y-%m-%dT%H:%M:%S.00Z)" \
        '{
          event: {
            header: { namespace:"Alexa", name:"ChangeReport",
                      payloadVersion:"3", messageId:$mid },
            endpoint: { scope:{ type:"BearerToken", token:$tok }, endpointId:$ep },
            payload: { change: {
              cause: { type:"PHYSICAL_INTERACTION" },
              properties: [ { namespace:"Alexa.ContactSensor",
                              name:"detectionState", value:$st,
                              timeOfSample:$ts, uncertaintyInMilliseconds:0 } ]
            } }
          },
          context: { properties: [ { namespace:"Alexa.EndpointHealth",
                                     name:"connectivity", value:{ value:"OK" },
                                     timeOfSample:$ts,
                                     uncertaintyInMilliseconds:0 } ] }
        }')"

    timeout 10 curl -fsS -X POST "$GATEWAY" \
        -H "Authorization: Bearer ${token}" \
        -H 'Content-Type: application/json' \
        -d "$body" >/dev/null 2>&1
}

TOKEN="$(get_token)" || TOKEN=""
if [ -z "$TOKEN" ]; then
    log "sensor FAIL ${EPID} -- token refresh failed (refresh_token revoked? re-link skill)"
    exit 0
fi

if send_state DETECTED "$TOKEN"; then
    log "sensor OK   ${EPID} DETECTED"
else
    log "sensor FAIL ${EPID} -- ChangeReport DETECTED rejected"
    exit 0
fi

# The sensor MUST return to NOT_DETECTED or the Routine will not fire
# again on the next call. Alexa edge-triggers on the transition.
sleep "$RESET_DELAY"
send_state NOT_DETECTED "$TOKEN" \
    || log "sensor WARN ${EPID} -- reset to NOT_DETECTED failed; next call may not trigger"

exit 0