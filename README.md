# orata

Home phone on a voip.ms DID, terminated on a Raspberry Pi 4 running stock
Asterisk from Debian's repo. Incoming calls are announced by name on Echo
devices.

No Docker. No FreePBX. No Home Assistant. No daemon beyond Asterisk itself.

## Moving parts

| Thing | What it is | Breaks when |
|---|---|---|
| Asterisk 20 | `apt install asterisk` on Debian 12 | ~never (LTS, config stable) |
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

On the Pi (Debian 12 arm64, **booted from USB SSD** — CDR logging kills SD cards):

    sudo apt update && sudo apt install asterisk curl jq

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

Fetch the script:

    sudo curl -fsSL -o /usr/local/bin/alexa_remote_control.sh \
      https://raw.githubusercontent.com/thorsten-gehrig/alexa-remote-control/master/alexa_remote_control.sh
    sudo chmod 755 /usr/local/bin/alexa_remote_control.sh

Read it before you run it. It is a third-party script that will hold a
credential for your Amazon account.

Set your Amazon domain near the top of that script (`amazon.com` /
`amazon.ca` / `amazon.co.uk`), then authenticate:

    sudo -u asterisk /usr/local/bin/alexa_remote_control.sh -a

This starts a local proxy on port 5601. Browse to `http://<pi-ip>:5601` from a
machine on your LAN, log in to Amazon there, and the script captures the
session cookie to `~asterisk/.alexa.cookie`. Run it as the `asterisk` user —
if you authenticate as root, Asterisk won't find the cookie.

Confirm devices are visible, then test:

    sudo -u asterisk /usr/local/bin/alexa_remote_control.sh -d ALL -e "announce:test"

List exact device names with `-l` and put them in `announce.conf` if `ALL` is
too broad.

**When it stops working** the symptom is `alexa FAIL` lines in
`/var/log/orata/announce.log`. Fix is re-running `-a`. That is the entire
maintenance burden.

### Two things that will confuse you

- **Do Not Disturb on an Echo suppresses announcements.** Useful at night,
  baffling at 2pm if you forgot it's on.
- **Announcement volume follows device volume**, and Echos drop their volume
  overnight if you have a routine doing that.

## Web UI

Optional. A single stdlib-only Python file (`orata-web.py`, no framework, no
pip) that edits the name book, whitelist, blocklist and Alexa sensor map from
a browser. It shells out to `orata-cnam.sh`, so the CLI stays authoritative
and nothing is generated or overwritten.

    sudo cp bin/orata-web.py /usr/local/bin/ && sudo chmod 755 /usr/local/bin/orata-web.py
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