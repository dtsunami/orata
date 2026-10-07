#!/bin/sh
# Regenerate the pinned "Alexa, call <number>" acoustic prompt.
#
# The DIGITS ARE IN THE AUDIO. Changing the callback DID in alexa-bridge.conf
# or the dialplan does NOT change what the speaker says: this prompt must be
# regenerated and re-pinned whenever the callback DID changes.
#
#   orata-alexa-prompt.sh 9714412343 [outfile]
#
# Default outfile is ./alexa-call.wav; install it to the pinned path with the
# sudo command printed at the end. Never writes to /var/lib directly.
set -eu

die() { printf 'orata-alexa-prompt: %s\n' "$*" >&2; exit 1; }

num="$(printf '%s' "${1:-}" | tr -cd '0-9')"
out="${2:-./alexa-call.wav}"
[ -n "$num" ] || die "usage: $0 <digits> [outfile]"
case "${#num}" in
    10|11) ;;
    *) die "expected a 10- or 11-digit DID, got ${#num} digits" ;;
esac

# Speak NANP digits in 3-3-4 groups with pauses; a flat 10-digit run is the
# most common cause of Alexa mishearing the number.
if [ "${#num}" = 11 ]; then
    body="$(printf '%s' "$num" | cut -c1), $(printf '%s' "$num" | cut -c2-4), $(printf '%s' "$num" | cut -c5-7), $(printf '%s' "$num" | cut -c8-11)"
else
    body="$(printf '%s' "$num" | cut -c1-3), $(printf '%s' "$num" | cut -c4-6), $(printf '%s' "$num" | cut -c7-10)"
fi
# Space the digits so the TTS reads "nine seven one", not "nine hundred".
spaced="$(printf '%s' "$body" | sed 's/\([0-9]\)/\1 /g')"
text="Alexa, call ${spaced}"

tts="${ORATA_TTS:-}"
if [ -z "$tts" ]; then
    for c in espeak-ng espeak; do
        if command -v "$c" >/dev/null 2>&1; then tts="$c"; break; fi
    done
fi
[ -n "$tts" ] || die "no espeak-ng/espeak on PATH -- apt install espeak-ng"
command -v sox >/dev/null 2>&1 || die "sox not on PATH -- apt install sox"

raw="$(mktemp -t orata-prompt-XXXXXX.wav)"
trap 'rm -f "$raw"' EXIT INT TERM

# -s 140: slower than default so each digit is distinct to the far-field mic.
timeout 15 "$tts" -s 140 -w "$raw" "$text" >/dev/null 2>&1 || die "$tts failed"
[ -s "$raw" ] || die "$tts produced nothing"

# 16 kHz 16-bit mono PCM. The worker plays this through pw-play to a speaker,
# NOT through Asterisk, so the 8 kHz telephone rate used by orata-clip.sh is
# not required here and hurts far-field wake-word recognition.
# pad 0.4 0.6: leading silence so the wake word is not clipped by the
# Bluetooth sink ramping up, trailing silence so the last digit is not cut.
sox "$raw" -r 16000 -c 1 -b 16 -t wav "${out}.new" pad 0.4 0.6 >/dev/null 2>&1 \
    || die "sox could not resample"

# Enforce the audio worker's own contract (validate_prompt): PCM, 0.2-12 s.
python3 - "${out}.new" <<'EOF' || { rm -f "${out}.new"; die "generated prompt failed worker validation"; }
import sys, wave
with wave.open(sys.argv[1], "rb") as fh:
    d = fh.getnframes() / fh.getframerate()
    if fh.getcomptype() != "NONE" or not 0.2 <= d <= 12:
        raise SystemExit(1)
    print("  duration %.2fs, %d Hz, %d ch, PCM" % (d, fh.getframerate(), fh.getnchannels()))
EOF

mv -f "${out}.new" "$out"
printf 'spoken text: %s\n' "$text"
printf 'wrote: %s\n' "$out"
cat <<EOF

Next, on the Pi:
  1. Listen before trusting it:
       pw-play --target=bluez_output.08_EB_ED_71_F9_02.1 $out
  2. Pin it (the worker reads only this path):
       sudo install -m 640 -o root -g asterisk $out /var/lib/orata-audio/alexa-call.wav
  3. No restart needed: pw-play reopens the pinned path on every request, so
     the next call uses the new file. Restart only if you want the worker's
     startup validation to reject a bad file now rather than mid-call:
       sudo systemctl restart orata-audio
  4. Confirm the dialplan and INI agree with $num:
       sudo grep callback_destination /etc/orata/alexa-bridge.conf
       sudo asterisk -rx 'dialplan show globals' | grep ALEXABRIDGE_CALLBACK_DID
EOF