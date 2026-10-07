# orata

Home phone on a voip.ms DID, terminated on a Raspberry Pi 4 running
source-built **Asterisk 22 LTS on Debian 13**. Incoming accepted calls are
announced by name on Echo devices.

No runtime Docker, FreePBX or Home Assistant. Core call handling uses Asterisk
and shell scripts; the optional admin UI is a small FastAPI/uvicorn service.

## Status — 2026-10-04

- PSTN inbound/outbound calls with MicroSIP and two-way audio were verified.
- **Alexa smoke test worked**, as reported by the user after refresh-token setup.
- Dashboard/configuration/audio fixes are in source; **19 regression tests passed**.
- Next: power up remaining Echos, verify each room, then real inbound-call and
  unattended-operation acceptance. Multi-Echo coverage is not yet confirmed.

Start here: [current handoff](HANDOFF.md), [install / safe update](docs/install.md),
[prioritized next steps](docs/next-steps.md).

## Moving parts

| Thing | What it is | Breaks when |
|---|---|---|
| Asterisk 22 LTS | **built from source** — not packaged in Debian 13 | you must rebuild for CVEs yourself; see `docs/build-from-source.md` |
| `orata-announce.sh` | shell notifier + local WAV render, fired by dialplan | permissions, dependencies, network/channel errors; must not break calls |
| `alexa_remote_control.sh` | third-party bash + curl/jq, refresh token and cookie cache | unofficial API/auth changes, revoked token, unavailable devices |
| ntfy | optional independent HTTP push | network/service/auth/subscription failures |
| `orata_web.py` | optional FastAPI admin service | service/config/dependency errors; phone does not depend on it |
| `orata-alexa-sensor.sh` | optional static-speech route, unverified deployment | skill/token/cloud/Routine changes |

The Alexa layer is deliberately quarantined. It is invoked backgrounded,
wrapped in `timeout`, and its exit code is ignored. If Amazon breaks it,
calls must still ring; phone push remains independent **if ntfy is configured
and its subscription/delivery work**.

Two Alexa paths are selected by `ORATA_ALEXA_MODE` in
`/etc/orata/announce.conf`:

- **`arc`** (default, initial smoke test user-confirmed) — dynamic "Call from
  Mom" from the name book, via refresh-token/cookie authentication and an
  unofficial API.
- **`sensor`** (optional, unverified deployment) — static speech per virtual
  contact sensor/Routine, official OAuth, additional AWS/skill setup:
  [design notes](docs/alexa-sensor.md).

Finish ARC coverage and operational acceptance first. Sensor mode is not
automatic failover and is not required to finish the current scope.

## Install or update

Use **[docs/install.md](docs/install.md)** for separate fresh-install and
working-Pi update paths. It covers source-build prerequisites, config ownership,
features/prompts/mailbox, UI installation, Alexa setup and verification.

Already running? Update scripts/UI without replacing live configs:

    sudo install -m 755 bin/orata_web.py bin/orata-cnam.sh bin/orata-clip.sh \
      bin/orata-announce.sh bin/orata-alexa-sensor.sh /usr/local/bin/
    sudo systemctl restart orata-web

This assumes the UI is installed. Merge dialplan/features changes separately
and reload only the affected component. **Never copy repo config templates over
working secrets, ring groups or NAT settings.**

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

On 2026-10-04 the user reported a successful Alexa smoke test after obtaining
a token and configuring ARC. Multi-Echo coverage and inbound-call announcements
still need explicit acceptance checks; see [next steps](docs/next-steps.md).

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
phrase to a WAV on the Pi (`apt install espeak-ng sox`) before it calls out, so
you can hear what was announced without an Echo in earshot. Useful well before
Alexa is set up at all — it is how you confirm the name book resolved and the
phrase came out right.

It proves the phrase, **not** audible Echo playback. `alexa OK` means ARC
returned success, not that Amazon accepted it or that an Echo spoke.
Local renders do not record conversations and are kept in a ring buffer
(`ORATA_AUDIO_KEEP`, default 50). Handset recording is separate and explicit:
`*96` or `3434`, described below.

This is the one place the project takes a framework dependency. It is
installed from Debian packages, not pip — trixie's python3 is
PEP 668 externally-managed, so `pip install` into the system interpreter is
refused, and a venv would be a second thing to maintain.

    sudo ./bin/orata-web-install.sh --bind 127.0.0.1 --port 8088

The config template defaults to loopback; the installer **without --bind**
auto-detects a LAN IP. It generates a query-safe token and preserves it on
rerun, but re-applies bind/port settings. Use explicit flags to retain those.

The server refuses an empty token or wildcard bind without explicit override.
Reach loopback via `ssh -L 8088:127.0.0.1:8088 USER@PI`, or bind a specific
trusted LAN/WireGuard address. No TLS or public exposure.

Full threat model in `docs/web-ui.md`.

## The blocklist

    orata-cnam.sh block-add 15558675309
    orata-cnam.sh block-list
    orata-cnam.sh block-del 15558675309

Blocked numbers are hung up on before anything answers, rings or speaks. The
dialplan deliberately does not `Answer()` first — answering confirms to a
robocaller that the line is live.

## The name book

The local name book takes priority over carrier CNAM. Add a number here for
a predictable "Call from Mom". Callers without a resolved name say "Call from
an unknown number" (set `ORATA_SPEAK_DIGITS=1` to hear digits instead).

    orata-cnam.sh add 15551234567 "Mom"
    orata-cnam.sh list
    orata-cnam.sh del 15551234567

This is stored in astdb — Asterisk's built-in key/value store. No external
database.

## The robocall gate

With STRICT off, callers without a resolved name or whitelist entry get
"press 1 to continue". Pressing 1 self-whitelists them for future calls.
Named/allowed non-owners skip the gate; owners always hear it so they can
enter 3434. Carrier CNAM counts as a resolved name.

    orata-cnam.sh allow-list
    orata-cnam.sh allow-del 15558675309

You need a `press-one` prompt. Generate one with espeak + sox:

    sudo apt install espeak-ng sox
    sudo mkdir -p /var/lib/asterisk/sounds/en/custom
    espeak-ng -w /tmp/p1.wav "Press 1 to continue."
    sudo sox /tmp/p1.wav -r 8000 -c 1 -t gsm \
      /var/lib/asterisk/sounds/en/custom/press-one.gsm

This is the source-build sound path. Verify Asterisk can see it (the dialplan
refers to `custom/press-one`, without the extension):

    sudo asterisk -rx "file convert /var/lib/asterisk/sounds/en/custom/press-one.gsm /tmp/verify.wav"

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

Call the house from your mobile, record a clip, then broadcast the saved clip
to the configured SIP handsets. This is not live audio or Echo playback. The flow:

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
| `call-from` | spliced before a recorded caller name in local/SIP audio |
| `owner-menu` | **nothing — accepted by the CLI but never played.** See below |

`orata-clip.sh` accepts all six roles, but the dialplan only plays the first
four and `orata-announce.sh` only uses `call-from`. Recording `owner-menu`
has no audible effect anywhere; it is a leftover in `VALID_ROLES`. The web UI
lists five (it omits `owner-menu`), so the CLI, the UI and the dialplan three
disagree. Do not treat an `owner-menu` override as a working prompt.

    orata-clip.sh say press-one "Press 1 to continue."
    orata-clip.sh roles                     # what is overridden
    orata-clip.sh unassign press-one        # back to the built-in

Overrides live in `/var/lib/orata/clips/role-*.wav`. The dialplan falls back to
the installed `custom/` sound file when an override is absent. Check Diagnostics
before clearing a prompt: a missing built-in file can leave the caller in silence.

Needs `apt install espeak-ng sox`. **sox is not optional**: espeak emits
22050 Hz and Asterisk wants 8 kHz, so without it the audio plays at the
wrong pitch.

The UI deliberately writes only to `/var/lib/orata/clips`, never to the
Asterisk sounds tree — that is root-owned and outside the service unit's
`ReadWritePaths`.

### Built-in prompt files (optional)

The overrides above make these unnecessary, but if you want working prompts
before touching the UI:

    sudo mkdir -p /var/lib/asterisk/sounds/en/custom
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