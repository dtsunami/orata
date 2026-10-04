# Web UI

`bin/orata_web.py` — a FastAPI app that edits the four astdb books through a
browser, plus a read-only diagnostics page and a test harness.

Installed from Debian packages (`python3-fastapi`, `python3-uvicorn`). No pip,
no venv, no database.

**Unverified.** Never run on the Pi.

## The tension, stated up front

HANDOFF rejected FreePBX partly because it "adds a web attack surface." This
adds one too. That objection was really about three things, and it is worth
being precise about which of them this inherits:

| FreePBX objection | Does orata-web inherit it? |
|---|---|
| PHP app *owns* the configs, fights hand-editing | **No.** It shells out to `orata-cnam.sh`. The CLI stays authoritative; the UI is a second face on it. Nothing is generated, nothing is overwritten. |
| Large dependency with its own upgrade cadence | **Partly.** It was stdlib-only; it is now FastAPI + uvicorn + starlette + pydantic, from Debian packages. They track the distro's cadence rather than their own, and `apt` upgrades them with everything else — but this is a real dependency that did not exist before, and it is the honest cost of the UI. |
| Web attack surface | **Yes.** Unavoidably. |

So the third one is real and has to be managed rather than argued away. It
runs as the `asterisk` user (it needs the CLI), which means a compromise is a
compromise of call routing — and call routing is attached to a trunk that can
dial premium-rate numbers.

Mitigations, all in the shipped defaults:

- **Binds 127.0.0.1.** Reach it over an SSH tunnel, or bind to the Pi's
  WireGuard address. It **refuses to start on 0.0.0.0** without an explicit
  override flag.
- **Refuses to start with an empty token.** An unconfigured instance is not a
  wide-open instance.
- Token compared with `hmac.compare_digest`; cookie is `HttpOnly` +
  `SameSite=Strict`.
- Numbers are digits-only, 3–15. Names and endpointIds are stripped of quotes,
  backslashes, backticks, `$` and control characters, capped at 64 chars.
  Subprocess is invoked without a shell.
- systemd unit is confined: `ProtectSystem=strict`, `NoNewPrivileges`,
  `ReadWritePaths` limited to the Asterisk run dir and the log dir.

**There is no TLS and no login rate limit.** Do not put this on the internet.
The invariant from README stands: no port-forward, remote access over
WireGuard.

## Install

One command, from the repo root on the Pi:

    sudo ./bin/orata-web-install.sh

That installs the Debian packages, copies `orata_web.py` into
`/usr/local/bin`, creates `/etc/orata`, `/var/log/orata` and `/var/lib/orata`
with the right ownership, generates a 24-byte token, installs and starts the
systemd unit, and prints the URL with the token already in it.

It is idempotent. Re-run it after editing the repo copy of `orata_web.py` and
it reinstalls and restarts without touching your existing token.

The directory creation is not optional detail: the unit sets
`ProtectSystem=strict` with `ReadWritePaths=/var/run/asterisk /var/log/orata
/var/lib/orata`, and systemd refuses to start a unit whose `ReadWritePaths`
do not exist.

### Choosing the bind address

With no arguments the installer auto-detects the Pi's LAN address and binds
that. The three options, narrowest first:

    sudo ./bin/orata-web-install.sh --bind 127.0.0.1   # SSH tunnel only
    sudo ./bin/orata-web-install.sh                    # auto-detected LAN IP
    sudo ./bin/orata-web-install.sh --bind 0.0.0.0     # every interface

**Binding a specific LAN or WireGuard IP needs no override flag** — the
server only fails closed on the literal `0.0.0.0` and `::`. So the
auto-detected default gives you browser access from any device on the LAN
while keeping the guardrail intact. If the Pi's address comes from DHCP, give
it a reservation or the bind will break on renewal.

`--bind 0.0.0.0` sets `ORATA_WEB_ALLOW_ANY_BIND=1` for you and prints a
warning. It exposes the UI on every interface including wifi and any guest
VLAN. There is no TLS and no rate limit on the token; the UI runs as the
`asterisk` user, so a compromise is a compromise of call routing on a trunk
that can dial premium-rate numbers. It is defensible on a trusted home LAN
with no port-forward, and indefensible anywhere else.

For tunnel-only access from a desktop:

    ssh -L 8088:127.0.0.1:8088 pi@raspberrypi

then browse `http://127.0.0.1:8088/?token=YOUR_TOKEN`. The token goes into an
HttpOnly cookie; after that, plain `http://127.0.0.1:8088/`.

### If the token link 401s

`orata: missing or bad token` on the one-shot link almost always means the
token contains a `+`. In a query string a literal `+` decodes to a **space**,
so the value reaching `hmac.compare_digest` is not the value in `web.conf`.
Percent-encode it as `%2B` (and `/` as `%2F`) to get in once, then rotate to a
query-safe token:

    head -c 24 /dev/urandom | base64 | tr '+/' '-_' | tr -d '='

`bin/orata-web-install.sh` generates base64url and percent-encodes the printed
URL, so this only bites tokens created by hand or by an older installer.

## What the UI deliberately will not do

It never writes a config file. Every mutation goes through
`orata-cnam.sh` into astdb. This is the line between orata-web and FreePBX,
which HANDOFF rejected for owning the configs and fighting hand-editing.

So these stay hand-edited, and no amount of UI polish should absorb them:

| Task | File |
|---|---|
| voip.ms POP + trunk credentials | `/etc/asterisk/pjsip.conf` |
| voicemail PIN | `/etc/asterisk/voicemail.conf` |
| Alexa mode, ntfy URL, LWA tokens | `/etc/orata/announce.conf` |
| selftest context `#include` | `/etc/asterisk/extensions.conf` |

The Diagnostics tab *detects and reports* every one of these — stubbed
credentials, missing ntfy URL, selftest context not loaded — and tells you
which file to edit. Reporting is the boundary.

## The Setup tab

`/setup` is the first tab and the intended landing page after install. It is a
six-step walkthrough that reads live state and marks each step done / do this
now / waiting — exactly one step is ever "do this now". Reload after changing
something and it re-evaluates.

| Step | Detected by |
|---|---|
| 0. voip.ms portal | not detectable — always shown, it is the step that costs money if skipped |
| 1. Credentials into pjsip.conf | the four stub strings are gone |
| 2. Reload and register | `pjsip show registrations` says `Registered` |
| 3. Register a softphone | `pjsip show contacts` has pc/mobile/desk |
| 4. Load the name book | `cnam` book is non-empty |
| 5. Announcements | ARC installed, or LWA credentials set, or mode `off` |

It names the portal fields and which pjsip.conf keys they map to, since the
POP hostname appears in four places and the sub-account username in three.
Step 0 also covers the two portal settings that are easy to miss and produce
confusing symptoms: **routing the DID to the sub-account** (a DID still
pointed at the main account never reaches the Pi) and the **codec list**,
which must include `ulaw` and `g722` to match pjsip.conf.

Like every other page, it **edits nothing**. It tells you which file to open
and what to put in it. The Books tab shows a banner linking here while the
trunk is unregistered.

## What it manages

Four books, all live in astdb. Edits apply to the **next call** — no reload.

| Book | Meaning |
|---|---|
| `cnam` | spoken name. Present = skips the robocall gate. |
| `allow` | whitelist. Past the gate, permanently. |
| `block` | hung up on immediately. Never rings, never announces. |
| `alexasensor` | per-number Alexa sensor endpointId (path B only) |

Plus the last 40 lines of `announce.log`, which is where `alexa FAIL` and
`sensor FAIL` show up.

## Whitelist, gate, blocklist — how they interact

The whitelist is not new; it has existed since session 1 and **self-learns**.
Order of evaluation in `[from-voipms]`:

1. `block/<num>` set? → `Hangup()`. No answer, no ring, no announce.
2. Name resolved from `cnam/` or carrier CNAM? → straight to ring.
3. `allow/<num>` set? → straight to ring.
4. Otherwise → the gate: "press 1 to continue". Pressing 1 writes
   `allow/<num>=1`, so they are never gated again.
5. `STRICT=1` in `[globals]` → skip step 4 and reject outright.

Strict mode turns the system from "filter robots" into "invitation only."
Legitimate strangers — a doctor's office, a delivery driver, a school — get
`vm-goodbye` and no way through. Leave it at `0` unless the gate is
demonstrably failing you.

Note step 1 does not `Answer()`. Answering a robocall confirms the number is
live and typically increases traffic.

## Testing

    # does it start and refuse the obvious mistakes
    sudo -u asterisk ORATA_WEB_CONF=/etc/orata/web.conf python3 /usr/local/bin/orata_web.py

Empty token should exit 1 with a message. Then, with a token set:

    curl -si http://127.0.0.1:8088/            # expect 401
    curl -si "http://127.0.0.1:8088/?token=T"  # expect 303 + Set-Cookie

Add a number in the browser, then confirm the CLI agrees — this is the check
that matters, because it proves the UI is a face on the CLI and not a second
store:

    orata-cnam.sh list
    orata-cnam.sh block-list

Then the dialplan:

    sudo asterisk -rx "core reload"
    sudo asterisk -rx "dialplan show from-voipms"

Look for the `blocked` label and the `STRICT` GotoIf. Block your own mobile
and call in; you should get a decline, and `BLOCKED <num>` in the Asterisk
console.

## Known gaps

- **No TLS.** SSH tunnel or WireGuard only.
- **No rate limit on the token.** A LAN attacker can brute force it; 24 random
  bytes makes that impractical, but it is not defence in depth.
- **No CSRF token.** `SameSite=Strict` on the cookie is the only protection.
  Adequate for a single-user LAN tool, not for anything wider.
- **No logrotate for `announce.log`.** Predates this change and still unfixed;
  the UI only reads the last 8 KB, so it will not choke, but the file grows
  forever.
- **Number format is not normalised.** The UI strips non-digits, so
  `(555) 123-4567` becomes `5551234567` — which will **not** match what
  voip.ms presents (`15551234567`). Enter numbers exactly as they appear in
  `announce.log` or the Asterisk console.