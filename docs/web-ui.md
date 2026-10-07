# Web UI

`bin/orata_web.py` — a FastAPI app with a status dashboard, guided configuration,
five astdb caller books, editable voice prompts, recordings, diagnostics and
a test harness. No frontend build system or additional runtime dependencies.

Installed from Debian packages (`python3-fastapi`, `python3-uvicorn`). No pip,
no venv, no database.

On 2026-10-04 all 19 isolated regression tests passed, including real WAV
synthesis and mocked ARC delivery. The user reported that Alexa's smoke test
worked. Full browser review and real handset
recording/hangup acceptance remain open; see [next-steps.md](next-steps.md).

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

- **Config template binds 127.0.0.1; installer defaults to a LAN IP.** Use
  explicit loopback + SSH tunnel or the Pi's WireGuard address for narrower
  access. Wildcard binds **require an explicit override**.
- **Refuses to start with an empty token.** An unconfigured instance is not a
  wide-open instance.
- Token compared with `hmac.compare_digest`; cookie is `HttpOnly` +
  `SameSite=Strict`.
- Numbers are digits-only, 3–15. Names and endpointIds are stripped of quotes,
  backslashes, backticks, `$` and control characters, capped at 64 chars.
  Subprocess is invoked without a shell.
- systemd unit is confined: `ProtectSystem=strict`, `NoNewPrivileges`,
  `ReadWritePaths` limited to the Asterisk run dir, Orata log dir and
  `/var/lib/orata` (audio, clips and authentication state).

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

It preserves an existing token and unrelated config entries, but always writes
the selected bind address and port (defaults: auto-detected LAN IP and 8088).
When re-running, pass your intended `--bind` and `--port` explicitly. It updates
only the web app and unit, not the recording/notifier scripts or dialplan.
For a binary-only update that preserves bind/port, see [install.md](install.md).

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

It never writes a config file. Caller-book mutations go through `orata-cnam.sh`
into astdb; voice clip and prompt mutations go through `orata-clip.sh`.
This is the line between orata-web and FreePBX, which HANDOFF rejected for
owning the configs and fighting hand-editing.

So these stay hand-edited, and no amount of UI polish should absorb them:

| Task | File |
|---|---|
| voip.ms POP + trunk credentials | `/etc/asterisk/pjsip.conf` |
| voicemail PIN | `/etc/asterisk/voicemail.conf` |
| Alexa mode, ARC refresh token/region, ntfy URL, optional LWA tokens | `/etc/orata/announce.conf` |
| selftest context `#include` | `/etc/asterisk/extensions.conf` |

The Diagnostics tab *detects and reports* every one of these — stubbed
credentials, missing ntfy URL, selftest context not loaded — and tells you
which file to edit. Reporting is the boundary.

`/etc/orata/alexa-bridge.conf` joins that list: the callback bridge's DIDs,
caller IDs and sink name are hand-edited with `sudoedit`, and the pinned
prompt WAV is installed by hand. Nothing in the UI reads or writes them.

## Boundary reversal, accepted 2026-10-04 — appliance image

**The read-only boundary above is superseded for the appliance build.** The
product changed: Orata is now a cloned Raspberry Pi 4 image that a user
powers on and configures entirely from the browser. There is no hand-editing
step for that user, so "Diagnostics reports it, you `sudoedit` the file" is
not a usable answer. The UI now gets write access to Asterisk configuration
and live BlueZ pairing, plus username/password authentication.

The original objection was correct for a hand-built Pi and is not withdrawn
on its merits. What changed is the alternative: an appliance whose owner
cannot edit `pjsip.conf` has no path to a working trunk at all.

Accepting this means accepting a real increase in exposure, so the following
are **conditions of the reversal, not refinements**:

1. **No shared secrets in the image.** A cloned image means every unit ships
   identical. Any password, token, SSH host key or SIP credential baked into
   it is public the moment one image leaks. The image ships with none; a
   first-boot unit generates per-device credentials before the UI serves a
   single page.
2. **Forced credential change on first login.** A default password that can
   remain set is the router failure mode being copied here. First boot
   generates a random one; the UI refuses all other routes until it is
   changed.
3. **Writes go through a privileged helper with a fixed command surface**,
   not by widening the web service's own privileges. The helper validates
   every field, writes via temp-file-and-rename, keeps a numbered backup, and
   never accepts a free-text config blob. This reuses the pattern already
   proven in `orata-audio-worker.py`: unix socket, `SO_PEERCRED` check,
   enumerated operations.
4. **Toll-fraud containment stays mandatory.** The trunk can dial
   premium-rate numbers. Portal spending cap, international block and IP
   restriction are not optional for an appliance, and the UI must surface
   their state rather than assume them.
5. **Still no TLS, still no public exposure.** Username/password over plain
   HTTP on a LAN is the accepted posture, identical to a consumer router.
   Port-forwarding this remains prohibited; remote access is WireGuard.

Hand-editing continues to work and remains authoritative for anyone who
prefers it. The helper reads and rewrites the same files rather than owning
a separate database, so a hand edit is never clobbered silently — but a
UI write and a concurrent `sudoedit` can still race, and last-write-wins.

## Dashboard and Configure

`/` is the landing page after the token is exchanged for a cookie. It shows
Asterisk reachability, trunk registration, available handset contacts, Alexa
mode, recent failures, audio counts, free space, recording-directory access
and recent log activity. All probes are read-only; refresh to re-run them.
Registration is not proof of DID routing or two-way audio.

`/configure` provides ordered entry points for setup, caller policy, prompts
and testing, plus current announcement settings and instructions explaining
each option. Help popovers work by touch or keyboard. Private ntfy URLs, ARC
refresh tokens and LWA credentials are not displayed.

Caller books and prompts are browser-editable. Trunk credentials, Alexa/ntfy
settings, voicemail and dialplan globals remain file-managed. Announcement
scripts read their config on each invocation; restart the web service after
changing audio directories because it resolves those paths at startup.

## Setup walkthrough

`/setup`, linked from Configure and the dashboard, is the checked first-run
walkthrough. It reads live state and marks steps done / do this now / waiting.
Portal actions cannot be verified automatically. Reload after changing
something and it re-evaluates.

| Step | Detected by |
|---|---|
| 0. voip.ms portal | not detectable — always shown, it is the step that costs money if skipped |
| 1. Credentials into pjsip.conf | the four stub strings are gone |
| 2. Reload and register | `pjsip show registrations` says `Registered` |
| 3. Register a softphone | `pjsip show contacts` has pc/mobile/desk |
| 4. Load the name book | `cnam` book is non-empty |
| 5. Announcements | ARC executable + jq + token + `speak`, sensor credentials, or mode `off`; both requires both paths |

Step 5 checks configuration presence only, not authentication or audible speech.

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

Five books at `/books`, all live in astdb. Edits apply to the **next call** — no reload.

| Book | Meaning |
|---|---|
| `cnam` | spoken name. Skips the robocall gate unless also an owner. |
| `owner` | may enter 3434 at the inbound gate to record/broadcast; always hears the gate. Caller ID is spoofable, so this is not strong authentication. |
| `allow` | whitelist. Past the gate, permanently. |
| `block` | hung up on immediately. Never rings, never announces. |
| `alexasensor` | per-number Alexa sensor endpointId (path B only) |

Plus the last 200 lines of `announce.log`, where notification and recording
failures show up. Prompt, clip and caller-name mutations use `orata-clip.sh`.

## Whitelist, gate, blocklist — how they interact

The whitelist is not new; it has existed since session 1 and **self-learns**.
Order of evaluation in `[from-voipms]`:

1. `block/<num>=1`? → `Hangup()`. No answer, no ring, no announce.
2. Resolve name from `cnam/`, then carrier CNAM.
3. `owner/<num>=1`? → gate, even if named/allowed; bypasses STRICT. Press 1
   for normal calling, or owner-authorised 3434 for SIP clip broadcast.
4. For non-owners, `allow/<num>=1` or a resolved name? → straight to ring.
5. Remaining unknown with `STRICT=1`? → reject.
6. Otherwise → "press 1 to continue". Passing writes `allow/<num>=1`.

A number absent from the local name book may still skip the gate if carrier
CNAM is present. Owner checks rely on spoofable caller ID, not strong auth.

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

## The Audio tab

The problem this solves: until an Echo is authenticated, "did the
announcement work?" has no answer you can observe from the Pi. The log says
`alexa SKIP`, and that is all you get.

So `orata-announce.sh` now renders the phrase locally as well. Before it
touches ntfy or Amazon, it runs espeak into a WAV:

    /var/lib/orata/announce/20260104-143052-15551234567.wav

and logs `audio OK <path>`. The web UI serves those back — the **Audio** tab
lists the most recent 50 newest-first with an inline player, the **Log** tab
puts a `play` link on any line that produced one, and firing an announcement
from the **Harness** tab shows a player in the result card.

**What this does and does not prove.** It proves the script composed/rendered
the supplied phrase. A Harness invocation bypasses the inbound dialplan, so
it does not independently prove inbound routing or name-book resolution.
Neither the WAV nor `alexa OK` proves audible playback: ARC returning success
can hide HTTP errors, and Echo volume/DND can suppress speech. Test a real
call and listen on the target Echo for end-to-end confirmation.

The practical use is debugging the *phrase*, which is where the mistakes
actually are: a number in the book in the wrong format, `ORATA_SPEAK_DIGITS`
not doing what you expected, a name with punctuation espeak mangles.

### Config

In `announce.conf` (the source of truth; `web.conf`'s
`ORATA_WEB_AUDIO_DIR` only overrides where the UI looks):

    ORATA_RECORD_AUDIO=1                      # 0 disables entirely
    ORATA_AUDIO_DIR=/var/lib/orata/announce
    ORATA_AUDIO_KEEP=50                       # ring buffer
    ORATA_TTS=                                # blank = autodetect

Needs `apt install espeak-ng sox` for rendering and recorded-name assembly.
Without TTS you get `audio SKIP`; remote announcements can still work.

`ORATA_AUDIO_KEEP` is a hard ring buffer — the newest N survive, everything
older is deleted on **every** call. Size depends on phrase length; 8 kHz 16-bit mono uses about 16 KB/second. This matters: unbounded recordings are how you fill the
SSD, which is the failure mode this project already designed around once.

The default directory is inside `/var/lib/orata`, which is `700
asterisk:asterisk` and already in the service unit's `ReadWritePaths`. If you
move it, add the new path there or `ProtectSystem=strict` will make writes
fail in a way that looks like a TTS problem.

### Why this is safe to leave on

Local announcement rendering does not record conversations. It synthesises
the same caller phrase sent to Amazon, optionally using a recorded name.
Handset clips are separate: `*96`, owner-mode `3434`, and in-call `3434`
explicitly capture voice. In-call capture may record another party; obtain
consent where required.

Serving them is the one place the UI returns a file from disk, so the
filename handling is deliberately rigid: names must match
`^\d{8}-\d{6}-\d{3,15}\.wav$`, are basenamed before matching, and anything
else is refused rather than sanitised. Nothing the user types is ever joined
to a path.

## Recording regression fixes and updating an existing install

The symptom:

    clip OK   20261004-060014 119564 bytes
    clip CAST 20261004-060014 -> 3/3 endpoint(s)
    clip FAIL empty or missing /var/lib/orata/clips/tmp/20261004-060022.wav

The first clip was saved. `CAST` counts dispatched Asterisk CLI commands,
not handset answers or successful playback. The later failure refers to a
different recording. The old owner loop armed its hangup commit before the
next start prompt; disconnecting there could try to commit a file never
recorded. The revised loop arms after the prompt and the hangup path checks
file existence/size. Failed commits cannot broadcast or play a saved confirmation.

Separately, the old Audio page crashed as soon as a saved clip existed:
`clips_card()` unpacked four-field prompt definitions into three variables.
That UI error did not mean the saved WAV was lost.

Other fixes: explicit WAV formats for sox `.new`/`.raw` files, retention
restricted to timestamped clips (never assigned prompts or caller names),
full prompt text readback, and log playback links accepting the caller
number emitted by the announcement script.

From the repo root, update the binaries without replacing live settings:

    sudo install -m 755 bin/orata_web.py bin/orata-clip.sh bin/orata-announce.sh /usr/local/bin/
    sudo systemctl restart orata-web

Review the `asterisk/extensions.conf` diff and merge the recorder changes
into `/etc/asterisk/extensions.conf`, preserving local globals and includes.
Do **not** blindly overwrite a customised dialplan. Then:

    sudo asterisk -rx 'dialplan reload'

Never replace your existing `announce.conf`, `web.conf` or `pjsip.conf`
with the repo defaults to apply these fixes. The web installer updates only
the web binary; it does not deploy clip/announcement scripts or the dialplan.

Verify on a handset: save a `*96` clip and open Audio; enter owner announce
mode, save and broadcast once, then hang up during the next start prompt;
finally hang up mid-recording and confirm that only actual audio is saved.
Check both `announce.log` and `journalctl -u orata-web`.

Isolated regression checks (no live SIP, ntfy, Alexa or astdb writes):

    python3 test/regression.py
    for f in bin/orata-clip.sh bin/orata-announce.sh; do
      bash -n "$f" || exit 1
    done
    git diff --check

Real synthesis coverage needs `espeak-ng` (or `espeak`) and `sox`; absent
dependencies produce an explicit skip. Dialplan checks here are structural,
not proof of runtime hangup handling. Browser appearance needs visual review.

## Pages not documented in detail

`/diag` and `/devices` are substantial pages with no section of their own
here. Briefly, until that is written:

- **`/diag`** runs five grouped probe sets — platform, dialplan, pjsip,
  announce, media — and reports stubbed credentials, missing prompt files,
  an unloaded selftest context and absent sound paths. It only reports;
  every fix is a hand edit in the file it names.
- **`/devices`** parses `pjsip.conf` sections and live contacts to show each
  SIP endpoint, its registration state and the LAN address to point a
  handset at. Read-only, like the rest.

## Known gaps

- **No TLS.** SSH tunnel or WireGuard only.
- **Tooltips are uneven.** `help_tip()` is called on status cards, Configure
  rows, book headings and the spoken-prompt rows. Diagnostics rows carry
  their own inline `→ hint` text instead. Still bare: the Harness buttons,
  the Devices cards, and the caller-name recording fields.
- **No in-browser audio recording, and none planned.** `getUserMedia`
  requires a secure context: with no TLS, a browser grants microphone access
  only over an SSH tunnel to `127.0.0.1`, and silently refuses on a LAN or
  WireGuard address. Record prompts by dialling `*96` from a handset, which
  also captures them through the same 8 kHz telephone path they are played
  back on. Synthesised prompts come from `orata-clip.sh say`.
- **No Bluetooth discovery or pairing.** The UI runs as `asterisk` with
  `NoNewPrivileges`; BlueZ pairing needs privileged D-Bus and an interactive
  agent. Pair the speaker once with `bluetoothctl`. The callback bridge's
  own failure mode — sink missing after a reboot — is visible in
  `/var/log/orata/alexa-bridge.log`, not in this UI.
- **No rate limit on the token.** A LAN attacker can brute force it; 24 random
  bytes makes that impractical, but it is not defence in depth.
- **No CSRF token.** `SameSite=Strict` on the cookie is the only protection.
  Adequate for a single-user LAN tool, not for anything wider.
- **No logrotate for `announce.log`.** Predates this change and still unfixed;
  the UI reads a bounded tail, but the file grows forever. Note the asymmetry: the WAVs *are* bounded (`ORATA_AUDIO_KEEP`),
  the log they are indexed by is not.
- **Audio playback is not proof of announcement.** The Audio tab tells you
  what the Pi composed, not what an Echo said. Reading it as end-to-end
  confirmation is the single most likely misuse of this UI.
- **Number format is not normalised.** The UI strips non-digits, so
  `(555) 123-4567` becomes `5551234567` — which will **not** match what
  voip.ms presents (`15551234567`). Enter numbers exactly as they appear in
  `announce.log` or the Asterisk console.