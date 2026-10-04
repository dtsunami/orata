# orata

Home phone on a voip.ms DID, terminated on a Raspberry Pi 4 running stock
Asterisk from Debian's repo. Incoming calls are announced by name on Echo
devices.

No Docker. No FreePBX. No Home Assistant. No daemon beyond Asterisk itself.

## Moving parts

| Thing | What it is | Breaks when |
|---|---|---|
| Asterisk 22 LTS | **built from source** — not packaged in Debian 13 | you must rebuild for CVEs yourself; see `docs/build-from-source.md` |
| `orata-announce.sh` | bash + curl, fired by dialplan | never (it's 40 lines) |
| `alexa_remote_control.sh` | third-party bash script | Amazon changes auth, ~annually |
| ntfy | HTTP POST to a public topic | never |
| `orata-alexa-sensor.sh` | bash + curl, official Alexa endpoints | only if you unlink the skill |

The Alexa layer is deliberately quarantined. It is invoked backgrounded,
wrapped in `timeout`, and its exit code is ignored. If Amazon breaks it,
calls still ring and you still get a phone push.

There are two Alexa paths, selected by `ORATA_ALEXA_MODE` in
`/etc/orata/announce.conf`:

- **`arc`** (default) — `alexa_remote_control.sh`. Dynamic "Call from Mom"
  straight from the name book. Cookie auth, breaks about once a year.
- **`sensor`** — virtual contact sensor + Alexa Routine. Speech text is
  static per sensor, but the auth never rots. Needs an AWS Lambda and a
  Smart Home skill: see `docs/alexa-sensor.md`.

`arc` ships as the default because per-caller names are the point. `sensor`
is the planned migration when the cookie next dies — set it up before that
happens and the switch is one line.

## Install

On the Pi (Debian 13 trixie arm64, **booted from USB SSD** — CDR logging kills SD cards):

    sudo apt update && sudo apt install curl jq

**Asterisk is not in Debian 13** — `apt install asterisk` fails with `no
installation candidate`. It must be built from source. Do that first, then
return here: `docs/build-from-source.md`.

Copy this repo over, then:

    # configs
    sudo cp asterisk/pjsip.conf asterisk/extensions.conf /etc/asterisk/
    sudo chown asterisk:asterisk /etc/asterisk/pjsip.conf /etc/asterisk/extensions.conf
    sudo chmod 640 /etc/asterisk/pjsip.conf

    # scripts
    sudo cp bin/orata-*.sh /usr/local/bin/
    sudo chmod 755 /usr/local/bin/orata-*.sh

    # config, log dir, state dir (LWA token cache for the sensor path)
    sudo mkdir -p /etc/orata /var/log/orata /var/lib/orata
    sudo cp etc/announce.conf /etc/orata/
    sudo chown asterisk:asterisk /var/log/orata /var/lib/orata
    sudo chmod 600 /etc/orata/announce.conf
    sudo chmod 700 /var/lib/orata

If you edited anything on Windows, strip CRLFs first or bash will fail with
`bad interpreter`:

    sed -i 's/\r$//' /usr/local/bin/orata-*.sh /etc/asterisk/*.conf

Then edit `/etc/asterisk/pjsip.conf` (POP + credentials), `/etc/orata/announce.conf`,
and reload:

    sudo asterisk -rx "core reload"
    sudo asterisk -rx "pjsip show registrations"

You want to see `Registered`.

## Alexa setup

See **[docs/alexa-arc.md](docs/alexa-arc.md)** for the current dynamic-speech
setup. Install `jq` and the reviewed third-party `alexa_remote_control.sh`,
obtain an Alexa app refresh token with the separate `alexa-cookie-cli`
helper, then export the token, region settings and private cookie path from
your existing `/etc/orata/announce.conf`.

Important corrections to older instructions:

- Current upstream uses **`speak:`**, not `announce:`. Set `ORATA_ALEXA_CMD=speak`.
- **`-a` lists devices; `-l` logs out; `-login` exchanges the refresh token.**
  The remote-control script does not start a browser-login proxy.
- Run as `asterisk` after sourcing announce.conf; direct invocations otherwise
  miss the exported token and region settings.
- Never commit the token or paste it into chat. The sensor route's
  `ORATA_LWA_*` values are separate credentials.
- No dialplan reload is needed for changes to announce.conf.
- Do Not Disturb and low Echo volume can suppress speech. `alexa OK` only
  means upstream returned success; listen to confirm playback.

The instructions were checked against upstream source on 2026-10-04.
Authentication and audible playback still require testing on your account.

## Web UI

Optional. A FastAPI app (`orata_web.py`) with a **Dashboard** landing page
for phone status, handsets, storage and recent activity. **Configure** guides
users through setup, caller policy, prompts and announcement settings with
help popovers and instructions. **Books** edits names, announcement owners,
whitelist, blocklist and Alexa sensor maps. Diagnostics, a test harness and
an Audio tab provide verification.

Caller edits use `orata-cnam.sh`; voice prompts and clips use `orata-clip.sh`.
The CLI remains authoritative. System config files stay hand-edited.

The **Audio** tab plays back announcements. `orata-announce.sh` renders every
phrase to a WAV on the Pi (`apt install espeak-ng`) before it calls out, so
you can hear what was announced without an Echo in earshot. Useful well before
Alexa is set up at all — it is how you confirm the name book resolved and the
phrase came out right.

It proves the phrase, **not** audible Echo playback. `alexa OK` means the
API command succeeded; Do Not Disturb or volume can still suppress speech.
Local renders do not record conversations and are kept in a ring buffer
(`ORATA_AUDIO_KEEP`, default 50). Handset recording is separate and explicit:
`*96` or `3434`, described below.

This is the one place the project takes a framework dependency. It is
installed from Debian packages, not pip — trixie's python3 is
PEP 668 externally-managed, so `pip install` into the system interpreter is
refused, and a venv would be a second thing to maintain.

    sudo apt install python3-fastapi python3-uvicorn
    sudo cp bin/orata_web.py /usr/local/bin/ && sudo chmod 755 /usr/local/bin/orata_web.py
    sudo cp etc/web.conf /etc/orata/ && sudo chmod 600 /etc/orata/web.conf
    head -c 24 /dev/urandom | base64      # paste into ORATA_WEB_TOKEN
    sudo cp etc/orata-web.service /etc/systemd/system/
    sudo systemctl daemon-reload && sudo systemctl enable --now orata-web

Binds `127.0.0.1` by default and **refuses to start** on `0.0.0.0` or with an
empty token. Reach it via `ssh -L 8088:127.0.0.1:8088 pi@raspberrypi`, or bind
it to the Pi's WireGuard address. No TLS — this never goes on the internet.

Full threat model in `docs/web-ui.md`.

## The blocklist

    orata-cnam.sh block-add 15558675309
    orata-cnam.sh block-list
    orata-cnam.sh block-del 15558675309

Blocked numbers are hung up on before anything answers, rings or speaks. The
dialplan deliberately does not `Answer()` first — answering confirms to a
robocaller that the line is live.

## The name book

Announcements say "Call from Mom" only if the number is in the name book.
Unknown numbers say "Call from an unknown number" (set `ORATA_SPEAK_DIGITS=1`
to hear the digits instead).

    orata-cnam.sh add 15551234567 "Mom"
    orata-cnam.sh list
    orata-cnam.sh del 15551234567

This is stored in astdb — Asterisk's built-in key/value store. No external
database.

## The robocall gate

Unknown callers get "press 1 to continue". Anyone who presses 1 is whitelisted
in astdb permanently and goes straight through next time. Anyone in the name
book skips the gate entirely.

    orata-cnam.sh allow-list
    orata-cnam.sh allow-del 15558675309

You need a `press-one` prompt. Generate one with espeak + sox:

    sudo apt install espeak sox
    sudo mkdir -p /usr/share/asterisk/sounds/en/custom
    espeak -w /tmp/p1.wav "Press 1 to continue."
    sudo sox /tmp/p1.wav -r 8000 -c 1 -t gsm \
      /usr/share/asterisk/sounds/en/custom/press-one.gsm

Verify Asterisk can see it (the dialplan refers to it as `custom/press-one`,
without the extension):

    sudo asterisk -rx "file convert /usr/share/asterisk/sounds/en/custom/press-one.gsm /tmp/verify.wav"

## Recorded clips — your own voice

espeak's announcement is a robot. To use your own voice instead, dial
**`*96`** from any registered handset:

    speak, then press #
      1  replay
      2  save
      3  re-record
      *  cancel

Nothing enters the library until you press 2, so re-record as often as you
like. Saved clips appear on the web UI's **Audio** tab, where you can label
them, play them back, push one out to the handsets, or install one as the
robocall gate prompt in place of the generated `press-one`.

    orata-clip.sh list
    orata-clip.sh label 20260104-143052 "gate prompt, take 3"
    orata-clip.sh assign press-one 20260104-143052
    orata-clip.sh unassign press-one        # back to the generated one
    orata-clip.sh broadcast 20260104-143052

`broadcast` originates a call to each endpoint in `ORATA_PAGE_ENDPOINTS` and
plays the clip when answered — the phones **ring**. True auto-answer paging
needs per-model `Alert-Info` headers and is deliberately not attempted.

`*96` lives in `[internal]`, so it is reachable only from your own
registered devices, never from the PSTN.

## Announce mode — `3434` from your own phone

Call the house from your mobile and speak an announcement into every
handset, live. The flow:

1. Call the DID from a number you have marked as an owner.
2. You hear the gate prompt: *"Press 1 to continue."*
3. Press **`3434`** instead.
4. Beep. Speak. Press **`#`**.
5. The clip is logged to the library, broadcast to the handsets, and you
   hear the beep again — ready for the next one.
6. Hang up to leave. A recording cut short by hanging up is still saved.

Authorise your mobile first, or `3434` is rejected like any wrong entry:

    orata-cnam.sh owner-add 15551234567
    orata-cnam.sh owner-list
    orata-cnam.sh owner-del 15551234567

**The code alone is not authorisation.** The dialplan checks `owner/<num>`
in astdb as well, so a robocaller that happens to send `3434` cannot record
into your house. Caller ID is spoofable, which makes this a deterrent rather
than real security — do not treat announce mode as locked.

Owner numbers always hear the gate prompt even though they are in the name
book, because that prompt is the only way in. Everyone else still skips it
as before.

Pressing `1` at the gate remains a single keypress, so normal callers are
unaffected: the first digit decides the branch, and only a leading `3` makes
Asterisk wait for the remaining `434`.

### Recording mid-call — `3434`

Press **`3434`** during any live call to capture what you say straight into
the clip library. Beep, speak, `#` to stop, beep. The clip appears on the
Audio tab immediately.

This is the quickest way to get someone's name in their own voice: ask them
to say it, press `3434`, done — no second call, no `*96`.

The other party hears silence while you are recording. That is unavoidable —
a channel cannot stay bridged and run `Record()` at the same time — so the
caps are deliberately tight: `#`, or 2s of silence, or 30s. There is no
review menu for the same reason; delete it from the UI if it came out wrong.

Needs `features.conf` installed and `DYNAMIC_FEATURES` set on the channel.
Both ship here: the feature is armed on inbound calls and on outbound NANP
dialling.

    sudo cp asterisk/features.conf /etc/asterisk/
    sudo asterisk -rx "module reload features"

(`features reload` is not a command on Asterisk 22 — it was removed. Use
`module reload features`, or `core reload` if you are reloading everything.)

Change the code by editing the `recordclip` line in `features.conf`.

### Recorded caller names

espeak mispronounces most names. To fix that per caller, use the **Recorded
caller names** section of the Audio tab: every entry in the name book gets a
row where you can type a phonetic spelling and synthesise it, or assign a
recording of the person actually saying their name.

    orata-clip.sh name-say 15551234567 "Yoshita"
    orata-clip.sh name-set 15551234567 20260104-143052
    orata-clip.sh name-list
    orata-clip.sh name-del 15551234567

The announcement then becomes a `sox` splice of a cached "Call from" lead-in
and the name clip, instead of one espeak render.

**This does not change what Alexa says.** Amazon's announcement API takes
text, not audio — there is no way to hand it a WAV. Recorded names affect
the local render on the Audio tab and "play on handsets" only. If an Echo is
your primary announcement channel, a phonetic respelling in the *name book*
is what you want, since that is the text Alexa receives.

The name book in astdb remains the source of truth for identity and gate
behaviour. A caller with no recording falls back to espeak, so the two can
never disagree about who is calling.

### Editing the prompts

Every spoken prompt is editable from the **Audio** tab — type the words,
press **synthesise**, and it applies to the next call. No file copying, no
reload. Or assign a `*96` recording to use your own voice instead.

| Role | Heard when |
|---|---|
| `press-one` | an unknown caller reaches the robocall gate |
| `rec-start` | `*96`, before recording begins |
| `rec-menu` | `*96`, the replay / save / re-record menu |
| `rec-saved` | `*96`, after a clip is saved |

    orata-clip.sh say press-one "Press 1 to continue."
    orata-clip.sh roles                     # what is overridden
    orata-clip.sh unassign press-one        # back to the built-in

Overrides live in `/var/lib/orata/clips/role-*.wav`. The dialplan checks for
one and falls back to the `custom/` sound file, so an unset prompt is always
safe — the UI can never leave a caller in silence.

Needs `apt install espeak-ng sox`. **sox is not optional**: espeak emits
22050 Hz and Asterisk wants 8 kHz, so without it the audio plays at the
wrong pitch.

The UI deliberately writes only to `/var/lib/orata/clips`, never to the
Asterisk sounds tree — that is root-owned and outside the service unit's
`ReadWritePaths`.

### Built-in prompt files (optional)

The overrides above make these unnecessary, but if you want working prompts
before touching the UI:

    for p in \
      "press-one:Press 1 to continue." \
      "rec-start:Speak after the beep, then press hash." \
      "rec-menu:Press 1 to replay, 2 to save, 3 to re-record, or star to cancel." \
      "rec-saved:Saved."
    do
      n=${p%%:*}; t=${p#*:}
      espeak -w /tmp/$n.wav "$t"
      sudo sox /tmp/$n.wav -r 8000 -c 1 -t gsm \
        /var/lib/asterisk/sounds/en/custom/$n.gsm
    done

Note the path: a **source build uses `/var/lib/asterisk/sounds/`**, not
`/usr/share/asterisk/sounds/`. Diagnostics reports any prompt that has
neither a sound file nor an override.

## Endpoints

| Device | Client |
|---|---|
| Windows | MicroSIP |
| Linux/macOS | Linphone |
| Android/iOS | **Groundwire** (~$10) — only one with reliable push for a self-hosted PBX |
| Desk phone | Grandstream GRP261x, Yealink T3x |

Free mobile SIP clients miss calls when backgrounded. Groundwire is the one
thing here worth paying for.

Remote devices connect over **WireGuard**, not an exposed SIP port.

## Security — do this before you register anything

1. **Set a spending cap and block international dialing in the voip.ms portal.**
   Compromised PBXes get drained to premium-rate numbers overnight. This is how
   people lose thousands.
2. Use a voip.ms **sub-account** for the trunk, never your main login.
3. Turn on voip.ms's **IP whitelist** for that sub-account.
4. **Do not port-forward 5060.** Asterisk registers outbound; inbound calls ride
   that registration. Nothing needs to be open.
5. Long random passwords per endpoint. Most breaches are dictionary attacks
   against `1001/1001`.