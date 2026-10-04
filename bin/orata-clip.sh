#!/bin/bash
#
# orata-clip.sh -- recorded announcement clips, in your own voice.
#
#   init                   create the directories (called by the dialplan)
#   commit <tmp.wav>       move a finished recording into the library
#   discard <tmp.wav>      throw an abandoned recording away
#   list                   NAME<TAB>LABEL, newest first
#   label <name> [text]    set or clear the human label
#   del <name>             delete a clip and its label
#   assign <role> <name>   install a clip as a dialplan role
#   say <role> <text>      synthesise a role prompt from text
#   unassign <role>        drop back to the built-in prompt
#   roles                  ROLE<TAB>SOURCE for every assigned role
#   name-set <num> <name>  use a clip as a caller's spoken name
#   name-say <num> <text>  synthesise a caller's spoken name
#   name-del <num>         back to the generated name
#   name-list              NUMBER<TAB>SOURCE for every recorded name
#   broadcast <name>       play a clip out to the SIP endpoints
#
# Clips are recorded by dialling *96. Names are timestamps because a
# handset has no keyboard; labels get added later from the web UI.
#
# This is the write path for clips. The web UI shells out to it, so the
# CLI stays authoritative -- same arrangement as orata-cnam.sh.
#
set -u

CONF="${ORATA_ANNOUNCE_CONF:-/etc/orata/announce.conf}"
[ -r "$CONF" ] && . "$CONF"

CLIP_DIR="${ORATA_CLIP_DIR:-/var/lib/orata/clips}"
TMP_DIR="${CLIP_DIR}/tmp"
KEEP="${ORATA_CLIP_KEEP:-100}"
PAGE_ENDPOINTS="${ORATA_PAGE_ENDPOINTS:-pc mobile desk}"
LOG="${ORATA_LOG:-/var/log/orata/announce.log}"

# Roles a prompt may be installed as. The dialplan looks for the
# resulting role-<name>.wav and falls back to the built-in sound file if
# it is absent, so an unassigned role is always safe.
# call-from is the "Call from" lead-in spliced before a recorded caller
# name. orata-announce.sh synthesises it on demand if it is absent.
VALID_ROLES="press-one rec-start rec-menu rec-saved call-from owner-menu"

# Asterisk's format_wav wants 8 kHz 16-bit mono. espeak emits 22050 Hz,
# so sox is not optional here -- a 22 kHz file plays at the wrong pitch
# or not at all.
TTS="${ORATA_TTS:-}"

log() { printf '%s %s\n' "$(date -Is)" "$*" >>"$LOG" 2>/dev/null; }
die() { printf 'orata-clip: %s\n' "$1" >&2; exit 1; }

# Accept "20260104-143052", "20260104-143052.wav" or a full path.
stem_of() {
    local s="${1##*/}"
    printf '%s' "${s%.wav}"
}

# Timestamp shape only. Everything else is refused rather than cleaned --
# this value becomes a filesystem path.
check_stem() {
    case "$1" in
        [0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]-[0-9][0-9][0-9][0-9][0-9][0-9]) ;;
        *) die "bad clip name '$1' -- expected YYYYMMDD-HHMMSS" ;;
    esac
}

check_role() {
    case " $VALID_ROLES " in
        *" $1 "*) ;;
        *) die "unknown role '$1' -- known: $VALID_ROLES" ;;
    esac
}

prune() {
    # Only library clips are disposable. Never prune role-* or name-*.
    ls -1t "$CLIP_DIR"/[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]-[0-9][0-9][0-9][0-9][0-9][0-9].wav 2>/dev/null | tail -n +$((KEEP + 1)) \
        | while IFS= read -r old; do
              rm -f "$old" "${old%.wav}.txt"
          done
}

cmd="${1:-}"
[ -n "$cmd" ] || die "usage: orata-clip.sh {init|commit|discard|list|label|del|assign|say|unassign|roles|broadcast}"
shift || true

case "$cmd" in

init)
    mkdir -p "$TMP_DIR" 2>/dev/null || die "cannot create $TMP_DIR"
    ;;

commit)
    src="${1:-}"
    [ -n "$src" ] || die "commit needs a path"
    [ -s "$src" ] && [ "$(stat -c %s "$src" 2>/dev/null)" -gt 44 ] \
        || { log "clip FAIL empty or missing ${src}"; die "empty recording: $src"; }
    st="$(stem_of "$src")"
    check_stem "$st"
    mkdir -p "$CLIP_DIR" 2>/dev/null
    mv -f "$src" "${CLIP_DIR}/${st}.wav" || die "cannot move into $CLIP_DIR"
    log "clip OK   ${st} $(stat -c %s "${CLIP_DIR}/${st}.wav" 2>/dev/null) bytes"
    prune
    printf '%s\n' "$st"
    ;;

discard)
    src="${1:-}"
    [ -n "$src" ] || die "discard needs a path"
    st="$(stem_of "$src")"
    check_stem "$st"
    rm -f "$src"
    log "clip DROP ${st}"
    ;;

list)
    for f in $(ls -1t "$CLIP_DIR"/*.wav 2>/dev/null); do
        st="$(stem_of "$f")"
        lbl=""
        [ -r "${CLIP_DIR}/${st}.txt" ] && lbl="$(head -c 120 "${CLIP_DIR}/${st}.txt")"
        printf '%s\t%s\n' "$st" "$lbl"
    done
    ;;

label)
    st="$(stem_of "${1:-}")"
    check_stem "$st"
    shift || true
    txt="$*"
    if [ -z "$txt" ]; then
        rm -f "${CLIP_DIR}/${st}.txt"
    else
        printf '%s\n' "$txt" | head -c 120 >"${CLIP_DIR}/${st}.txt" || die "cannot write label"
    fi
    ;;

del)
    st="$(stem_of "${1:-}")"
    check_stem "$st"
    rm -f "${CLIP_DIR}/${st}.wav" "${CLIP_DIR}/${st}.txt"
    log "clip DEL  ${st}"
    ;;

assign)
    role="${1:-}"; check_role "$role"
    st="$(stem_of "${2:-}")"; check_stem "$st"
    [ -s "${CLIP_DIR}/${st}.wav" ] || die "no such clip: $st"
    # Copy, never symlink: deleting the clip must not silently break the
    # dialplan prompt.
    cp -f "${CLIP_DIR}/${st}.wav" "${CLIP_DIR}/role-${role}.wav.new" \
        || die "cannot write role file"
    mv -f "${CLIP_DIR}/role-${role}.wav.new" "${CLIP_DIR}/role-${role}.wav"
    printf 'recording %s\n' "$st" >"${CLIP_DIR}/role-${role}.txt"
    log "clip ROLE ${role} <- ${st}"
    ;;

say)
    role="${1:-}"; check_role "$role"
    shift || true
    txt="$*"
    [ -n "$txt" ] || die "say needs some text"

    tts="$TTS"
    if [ -z "$tts" ]; then
        for c in espeak-ng espeak; do
            if command -v "$c" >/dev/null 2>&1; then tts="$c"; break; fi
        done
    fi
    [ -n "$tts" ] || die "no espeak-ng/espeak on PATH -- apt install espeak-ng"
    command -v sox >/dev/null 2>&1 \
        || die "sox not on PATH -- apt install sox (needed to resample to 8 kHz)"

    mkdir -p "$CLIP_DIR" 2>/dev/null
    raw="${CLIP_DIR}/.say-$$.wav"
    out="${CLIP_DIR}/role-${role}.wav"

    if ! timeout 15 "$tts" -w "$raw" "$txt" >/dev/null 2>&1 || [ ! -s "$raw" ]; then
        rm -f "$raw"
        die "$tts could not synthesise"
    fi
    # 8 kHz 16-bit mono, or Asterisk plays it at the wrong pitch.
    if ! sox "$raw" -r 8000 -c 1 -b 16 -t wav "${out}.new" >/dev/null 2>&1; then
        rm -f "$raw" "${out}.new"
        die "sox could not resample to 8 kHz"
    fi
    rm -f "$raw"
    mv -f "${out}.new" "$out" || die "cannot write $out"
    printf 'text: %s\n' "$txt" | head -c 200 >"${CLIP_DIR}/role-${role}.txt"
    log "clip SAY  ${role} \"${txt}\""
    ;;

roles)
    for r in $VALID_ROLES; do
        [ -s "${CLIP_DIR}/role-${r}.wav" ] || continue
        src=""
        [ -r "${CLIP_DIR}/role-${r}.txt" ] && src="$(head -c 200 "${CLIP_DIR}/role-${r}.txt")"
        printf '%s\t%s\n' "$r" "$src"
    done
    ;;

unassign)
    role="${1:-}"; check_role "$role"
    rm -f "${CLIP_DIR}/role-${role}.wav" "${CLIP_DIR}/role-${role}.txt"
    log "clip ROLE ${role} <- (generated)"
    ;;

name-set|name-say|name-del)
    num="$(printf '%s' "${1:-}" | tr -cd '0-9')"
    case "${#num}" in
        3|4|5|6|7|8|9|10|11|12|13|14|15) ;;
        *) die "bad number '${1:-}' -- 3 to 15 digits" ;;
    esac
    shift || true
    out="${CLIP_DIR}/name-${num}.wav"
    mkdir -p "$CLIP_DIR" 2>/dev/null

    case "$cmd" in
    name-del)
        rm -f "$out" "${CLIP_DIR}/name-${num}.txt"
        log "name DEL  ${num}"
        ;;
    name-set)
        st="$(stem_of "${1:-}")"; check_stem "$st"
        [ -s "${CLIP_DIR}/${st}.wav" ] || die "no such clip: $st"
        cp -f "${CLIP_DIR}/${st}.wav" "${out}.new" || die "cannot write $out"
        mv -f "${out}.new" "$out"
        printf 'recording %s\n' "$st" >"${CLIP_DIR}/name-${num}.txt"
        log "name SET  ${num} <- ${st}"
        ;;
    name-say)
        txt="$*"
        [ -n "$txt" ] || die "name-say needs some text"
        tts="$TTS"
        if [ -z "$tts" ]; then
            for c in espeak-ng espeak; do
                if command -v "$c" >/dev/null 2>&1; then tts="$c"; break; fi
            done
        fi
        [ -n "$tts" ] || die "no espeak-ng/espeak on PATH"
        command -v sox >/dev/null 2>&1 || die "sox not on PATH -- apt install sox"
        raw="${CLIP_DIR}/.name-$$.wav"
        timeout 15 "$tts" -w "$raw" "$txt" >/dev/null 2>&1 && [ -s "$raw" ] \
            || { rm -f "$raw"; die "$tts could not synthesise"; }
        # Must match Record()'s 8 kHz mono or the concat in announce.sh fails.
        sox "$raw" -r 8000 -c 1 -b 16 -t wav "${out}.new" >/dev/null 2>&1 \
            || { rm -f "$raw" "${out}.new"; die "sox could not resample"; }
        rm -f "$raw"
        mv -f "${out}.new" "$out"
        printf 'text: %s\n' "$txt" | head -c 200 >"${CLIP_DIR}/name-${num}.txt"
        log "name SAY  ${num} \"${txt}\""
        ;;
    esac
    ;;

name-list)
    for f in $(ls -1 "$CLIP_DIR"/name-*.wav 2>/dev/null); do
        b="${f##*/}"; n="${b%.wav}"; n="${n#name-}"
        src=""
        [ -r "${CLIP_DIR}/name-${n}.txt" ] && src="$(head -c 200 "${CLIP_DIR}/name-${n}.txt")"
        printf '%s\t%s\n' "$n" "$src"
    done
    ;;

broadcast)
    st="$(stem_of "${1:-}")"
    check_stem "$st"
    [ -s "${CLIP_DIR}/${st}.wav" ] || die "no such clip: $st"
    n=0
    for ep in $PAGE_ENDPOINTS; do
        # Each handset rings and plays the clip on answer. True paging
        # (auto-answer) needs per-device Alert-Info/Call-Info headers,
        # which vary by model -- deliberately not attempted here.
        asterisk -rx "channel originate PJSIP/${ep} application Playback ${CLIP_DIR}/${st}" \
            >/dev/null 2>&1 && n=$((n + 1))
    done
    log "clip CAST ${st} -> ${n}/$(set -- $PAGE_ENDPOINTS; echo $#) endpoint(s)"
    printf 'sent to %d endpoint(s)\n' "$n"
    ;;

*)
    die "unknown command: $cmd"
    ;;
esac

exit 0