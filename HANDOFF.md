# HANDOFF

Session 5. **The phone works.** Calls complete in both directions between a
real mobile over the PSTN and MicroSIP on the LAN, with audio. The trunk, the
dialplan, RTP and codec negotiation are all proven by execution. See "Session
5 results".

Sessions 1-2 wrote this repo on a Windows box with nothing deployed. Session 3
built and installed Asterisk from source and verified the dialplan logic
against synthetic channels. Session 4 moved root to a USB SSD. Session 5 made
it a telephone.

What remains is the *announcement* layer (the stated point of the project) and
the operational work that makes the thing survive unattended.

---

## What this project is

A home phone: one voip.ms DID, terminated on a Raspberry Pi 4 running stock
Asterisk 20 from Debian 12's repo. Incoming calls are announced by name on
Echo devices ("Call from Mom"). Robocalls are filtered before anything rings
or speaks.

The Alexa announcement is the *point* of the project, per the user. It is
also the only fragile part. See "The Alexa tradeoff" below.

---

## Hard constraint from the user

> "i don't want any framework or tech debt what is the bare metal option"

This drove every decision. Things that were explicitly proposed and then
**rejected** — do not reintroduce them without the user asking:

| Rejected | Why |
|---|---|
| Docker / docker-compose | SIP needs RTP 10000-20000/udp; Docker's userland proxy mangles it, so you end up on `network_mode: host` anyway. Pure indirection, zero isolation benefit. **Still rejected for runtime.** Session 2 added `test/` — a build-and-discard container for verifying config parsing and astdb plumbing on a dev box. It is `--rm`, never deployed, and networking results from it are meaningless. See `test/README.md`. |
| Kubernetes / k3s | Asked in session 2. RTP needs 10000 UDP ports (NodePort range is 2768 by default); SIP embeds IPs in the SDP body so kube-proxy DNAT breaks audio; astdb is a node-local sqlite file with no clustering, and `allow/` self-learns so replicas diverge; a SIP registration is a single binding, so replica 2 fights replica 1 for the trunk; a rescheduled pod drops every in-progress call anyway. The working config is `replicas: 1` + `hostNetwork` + `nodeSelector` + hostPath — i.e. every k8s feature disabled. Full analysis in `docs/kubernetes.md`. |
| FreePBX | PHP app that owns the configs, fights hand-editing, adds a web attack surface. |
| Home Assistant | Large Python app with its own upgrade cadence, proposed solely to get Alexa announcements. Not worth it for one notification. |
| AMI daemon / Python service | Unnecessary — the dialplan can background a shell script with `System()`. |

Current total runtime surface: **Asterisk, plus bash scripts it invokes.**
Keep it there.

---

## Files

```
README.md                  install, Alexa auth, security checklist
HANDOFF.md                 this file
docs/alexa-sensor.md       Alexa path B: virtual sensor + Lambda (session 2)
asterisk/pjsip.conf        trunk + endpoint templates (pc/mobile/desk)
asterisk/extensions.conf   inbound gate, announce hook, outbound, *99 test
bin/orata-announce.sh      ntfy + Alexa notifier, backgrounded, always exit 0
bin/orata-alexa-sensor.sh  Alexa path B sender: LWA token + ChangeReport
bin/orata-cnam.sh          name book + allow/block lists + sensor map, astdb
bin/orata_web.py           FastAPI web admin + diagnostics + test harness
etc/announce.conf          the only file tuned at runtime
etc/web.conf               web UI bind address + admin token
etc/orata-web.service      systemd unit for the web UI
docs/web-ui.md             web UI install, threat model, blocklist semantics
test/Dockerfile            dev-box verification container (NOT a deploy target)
test/run-tests.sh          parses configs, checks astdb key agreement, web UI
test/README.md             what the rig proves and what it cannot
```

### Deploy targets (from README)

| Repo path | Pi path |
|---|---|
| `asterisk/*.conf` | `/etc/asterisk/` (owner `asterisk`, pjsip.conf 640) |
| `bin/*.sh` | `/usr/local/bin/` (755) |
| `etc/announce.conf` | `/etc/orata/` (600) |
| — | `/var/log/orata/` (owned by `asterisk`) |
| — | `/var/lib/orata/` (owned by `asterisk`, 700 — LWA token cache) |

---

## Architecture, and why

**Call flow (`[from-voipms]` in extensions.conf):**

```
inbound -> resolve name (astdb cnam/ -> fallback carrier CNAM)
        -> known name OR previously whitelisted? -> skip gate
        -> else: Answer, "press 1 to continue", 7s Read
             pressed 1 -> write astdb allow/<num>=1, continue
             else      -> vm-goodbye, hangup
        -> (ring label) System(orata-announce.sh ... &)
        -> Dial(RINGALL, 30) -> VoiceMail(100)
```

**Three deliberate choices, don't undo them by accident:**

1. **Announce fires at the `ring` label, after the gate.** Spam never reaches
   the Echos. If you move the `System()` call earlier, robocalls will announce.
2. **`System(... &)` with the trailing ampersand.** The shell detaches, the
   dialplan proceeds to `Dial()` immediately. Even if Amazon's API hangs for
   the full 20s timeout, phones are already ringing. This is the entire reason
   no daemon is needed.
3. **`orata-announce.sh` always `exit 0` and logs failures to a file.** A
   broken notifier must never affect call handling.

**State lives in astdb** (Asterisk's built-in key/value store — no external DB):
- `cnam/<number>` -> spoken name
- `allow/<number>` -> `1` if past the robocall gate

Managed via `orata-cnam.sh`. The gate self-learns: press 1 once, whitelisted
forever.

---

## The Alexa tradeoff (explain this again if asked)

The constraint: **dynamic speech or stable auth, pick one.** Amazon publishes
no supported way to speak *dynamic* text on an Echo.

| Path | Auth | Speech |
|---|---|---|
| Proactive Events (private skill) | real OAuth, never breaks | chime + yellow ring only |
| Routine triggered by virtual device | real OAuth, never breaks | spoken, but text is **static** |
| `/api/behaviors/preview` (what ARC drives) | session cookie, breaks ~annually | arbitrary dynamic text |

Both viable paths are now built. `ORATA_ALEXA_MODE` in `announce.conf`
selects: `arc` | `sensor` | `both` | `off`.

### Path A — `alexa_remote_control.sh` (SHIPPED DEFAULT)

Dynamic caller names via thorsten-gehrig's script (single bash file, curl +
jq, no HA). It authenticates by impersonating the Alexa web app and stores a
session cookie.

This is the default **because per-caller names are the stated point of the
project** — "Call from Mom", driven straight off the astdb name book, no GUI
chore per contact.

Mitigation for the fragility, already built in:
- invoked backgrounded, wrapped in `timeout 20`, exit code ignored
- ntfy HTTP push runs first as an independent channel, so a dead cookie
  degrades to a phone notification rather than silence
- failure symptom is `alexa FAIL` lines in `/var/log/orata/announce.log`
- fix is re-running `alexa_remote_control.sh -a`

Auth must be run **as the `asterisk` user** or Asterisk won't find the cookie:

    sudo -u asterisk /usr/local/bin/alexa_remote_control.sh -a

Starts a proxy on :5601; log in to Amazon from a LAN browser.

### Path B — virtual contact sensor (MIGRATION TARGET)

Built in session 2, **not yet deployed or tested**. Full setup in
`docs/alexa-sensor.md`.

A Smart Home skill reports virtual contact sensors. The Pi trips one via a
ChangeReport to the Alexa Event Gateway; a Routine bound to that sensor
speaks a **static** phrase. LWA OAuth refresh token — auth does not rot.

Note the inversion: path B's *hot path* is more bare-metal than path A (one
documented curl vs ~1000 lines of third-party bash). Its *setup* is much
heavier, and the setup is what conflicts with "no tech debt" — you acquire a
second deployment target in AWS with its own lifecycle. Lambda is setup-time
only (Discovery / AcceptGrant / ReportState); it is never in the call path.

Sensor IDs map per-number in astdb (`alexasensor/<num>`, managed by
`orata-cnam.sh sensor-add`) with `ORATA_SENSOR_DEFAULT` as fallback — so
per-person sensors and tiered sensors are the same code, a data choice only.
`docs/alexa-sensor.md` argues for tiered (`orata-family` / `orata-known` /
`orata-unknown`): three Routines built once, and ntfy still carries the exact
name to your phone.

**When path A breaks (it will), the switch is one line:**
`ORATA_ALEXA_MODE=sensor` — provided the AWS side has been set up. Doing that
setup *before* the cookie dies is the difference between a config change and
an afternoon.

`both` is for migration verification only; you will hear every call twice.

---

## Open questions for the user

1. **Nearest voip.ms POP.** `pjsip.conf` is stubbed with `newyork.voip.ms` in
   four places (`voipms_aor` contact, `server_uri`, `client_uri`,
   `voipms_identify` match). User should check latency in the portal.
2. **Dedicated `orata` user.** Offered but not built. Currently Asterisk holds
   the Amazon cookie directly. The more-correct variant is a separate unix user
   with Asterisk calling out via sudo. User hasn't said yes or no.
3. **Device count / names.** `pjsip.conf` ships three endpoints (pc 101,
   mobile 102, desk 103) as a guess. Adding one is six lines using the
   templates.
4. **SMS/MMS.** Mentioned in session 1, never pursued. voip.ms has a REST API
   and webhooks; would need a small separate service. Out of scope so far.

---

## Known-unverified (highest risk first)

- **`announce:` with `-d ALL`** — believed correct for hitting every Echo at
  once, not confirmed. If only one device speaks, set
  `ORATA_ALEXA_CMD=speak` in `announce.conf`. Test with `*99`.
- **`Read(digit,custom/press-one,1,,1,7)`** — argument order and the 7s
  timeout are untested against a live call.
- ~~**`custom/press-one` prompt does not exist yet.**~~ Generated and
  verified playable in session 3. Note the path: source installs use
  `/var/lib/asterisk/sounds/`, **not** `/usr/share/asterisk/sounds/`.
- ~~**CRLF.**~~ Non-issue on this Pi: git checked out `i/lf w/lf`, and all
  nine sources were confirmed LF-only. (Session 3 did find and fix a
  *missing trailing newline* on `extensions.conf`, `pjsip.conf` and
  `announce.conf` — appending to those files previously corrupted the last
  line.)
- **`local_net` in pjsip.conf** assumes 192.168/16 and 10/8. The
  `external_media_address` / `external_signaling_address` lines are commented
  out — uncomment **both** only if the Pi is behind NAT.
- Carrier CNAM from voip.ms is unreliable on mobile-originated calls. This is
  why the local name book takes priority in the dialplan.

---

## Session 3 results (on-Pi)

The Pi is **Raspberry Pi OS trixie = Debian 13**, not Debian 12. This
invalidated step 2 below outright: **Asterisk is not in Debian 13.** Empty
`apt-cache policy` version table, nothing from `madison`, reverse-deps-only
`showpkg`. Bookworm was its last stable home.

Resolved by building **Asterisk 22.11.0 LTS from source** (user's call, on
the grounds that the packaging dependency was the debt that came due). Full
rationale, configure line, and the maintenance obligation you now own:
`docs/build-from-source.md`.

### Verified by execution, not inspection

These ran on the Pi against the real dialplan:

- All 12 required modules built; 303 total; 368 sound files incl.
  `vm-goodbye.gsm`
- **astdb key agreement** — `cnam=[Mom] block=[1] allow=[1]
  sensor=[orata-mom]` resolved through a live channel. The seam the test rig
  called most likely to be wrong is correct.
- **Known caller** skips the gate: `Set(CNAM=Mom)` -> `GotoIf(1?ring)` ->
  `System()` fires -> `Dial(...)` -> `VoiceMail(100@default,u)`. No
  `Answer()`, no `Read()`.
- **Blocked caller**: `GotoIf(1?blocked)` -> `NoOp(BLOCKED)` -> `Hangup()` in
  three priorities, **never answers**. Security property holds.
- **Unknown caller** reaches `Read(digit,custom/press-one,1,,1,7)` and
  announce correctly does *not* fire — robocalls never reach the Echos.
- `System(... &)` -> `orata-announce.sh` fires from a real channel and exits
  0 with `alexa SKIP` logged. **The no-daemon architecture works.**
- `press-one.gsm` plays: `Playing 'custom/press-one.gsm' (language 'en')`
- `orata-web.py` imports clean on Python 3.13 (uses none of the modules
  trixie removed: `cgi`, `crypt`, `telnetlib`)

---

## Session 5 results (on-Pi) — END-TO-END CALLS WORK

**Milestone: complete calls in both directions between a mobile phone on the
PSTN and MicroSIP on the LAN.** Inbound DID -> voip.ms -> Pi -> softphone
rings -> answered -> two-way audio. Outbound softphone -> Pi -> voip.ms ->
mobile, same.

This retires, by execution, every item that was on the "needs real SIP" list
except the two noted below:

- Registration to voip.ms — **Registered**. The POP and sub-account
  credentials in `/etc/asterisk/pjsip.conf` are real now, not stubs. (The
  repo copy of `pjsip.conf` still carries the `newyork.voip.ms` placeholder
  by design — credentials never land in git. If the deployed POP differs,
  that divergence is intentional and undocumented on purpose.)
- RTP, audio, codec negotiation — working in both directions. No one-way
  audio, so whatever `local_net` / NAT posture is deployed is correct for
  this network. **Do not touch `external_media_address` /
  `external_signaling_address` without a reason** — they are currently in a
  known-good state.
- Inbound DID routing through `[from-voipms]` to a live endpoint.
- Outbound dialing through the trunk.
- The no-port-forward invariant holds in practice: inbound calls arrive on
  the outbound registration.

### Still unverified after session 5

- **Anything Alexa.** Neither path has real credentials. This is the whole
  point of the project and it is the only feature still missing.
- **The robocall gate against a human.** The logic is proven (session 3) and
  the prompt plays, but no real unknown caller has sat through
  `Read(digit,custom/press-one,1,,1,7)`. The 7s window is still a guess.
- **Mobile client off-LAN.** MicroSIP is a desktop on the same network.
  Groundwire + WireGuard is untested, and "home phone" is not really true
  until the phone rings when you are not home.
- **Reboot survival.** Unknown whether a cold boot comes back registered
  without hand-holding — the `asterisk` unit's enable state and start
  ordering have not been exercised since the source install.

---

## Strategy to finish — four phases, in order

The project is past the risky part. What is left splits cleanly into
"finish the feature", "make it survive", "insure the fragile bit", and
"polish". Do not interleave them; each phase is testable on its own.

### Phase 1 — Make it announce (the actual deliverable)

Nothing else matters until an Echo says "Call from Mom". Everything built so
far is scaffolding for this.

0. **First, with no Amazon involvement at all:** `apt install espeak-ng`,
   then fire an announcement from the web UI's Harness tab and play it back
   on the **Audio** tab. This confirms the dialplan reaches the script, the
   name book resolves, and the phrase is right ("Call from Mom", not "Call
   from an unknown number") — all the failure modes that are *not* Amazon's
   fault, separated out before you add one that is. Added session 5; see
   `docs/web-ui.md`.
1. `sudo -u asterisk /usr/local/bin/alexa_remote_control.sh -a` — browse to
   `http://<pi-ip>:5601` from the LAN, log in. **Must be the `asterisk`
   user** or the cookie lands in the wrong home directory.
2. `sudo -u asterisk alexa_remote_control.sh -l` to get exact device names.
3. `sudo -u asterisk alexa_remote_control.sh -d ALL -e "announce:test"`.
   If only one device speaks, set `ORATA_ALEXA_CMD=speak` in
   `announce.conf` — this is the top known-unknown in the repo.
4. Dial `*99`.
5. Put one real number in the name book and call from it. The success
   criterion is a spoken name, not a chime.
6. Check `/var/log/orata/announce.log` for `alexa FAIL`.

Watch for the two time-wasters: Do Not Disturb silently suppresses
announcements, and announcement volume follows device volume.

### Phase 2 — Make it survive unattended

A phone that needs a human is not a phone. These are the gaps the repo does
*not* currently cover, roughly in order of what bites first:

- **Reboot test.** `systemctl is-enabled asterisk`, then actually reboot and
  confirm `pjsip show registrations` comes back `Registered` with no
  intervention. Do this before trusting the line.
- **Back up astdb.** The name book, whitelist, blocklist and sensor map all
  live in Asterisk's sqlite astdb. It is now the only irreplaceable state on
  the box and there is **no backup path in this repo**. A cron'd
  `sqlite3 .backup` (or `database show` dumped to a text file, which is
  restorable via `orata-cnam.sh`) to the SSD plus somewhere off-box. The
  text dump is arguably better: human-readable, diffable, survives an
  Asterisk major-version schema change.
- **Log rotation.** `/var/log/orata/announce.log` grows without bound, and
  CDR/full logging from a source build does not get Debian's packaged
  logrotate config. Unbounded logs on the SSD are the slow-motion version of
  the SD-card failure this project already designed around.
- **Fail-loud.** Right now a dead Alexa cookie degrades to silence plus a
  log line nobody reads. The ntfy push covers the call itself, but nothing
  tells you the announce path rotted. Cheapest fix: a weekly cron that greps
  the log for `alexa FAIL` and pushes to ntfy if found.
- **Voicemail PIN.** Mailbox 100 is still `1357`. Outstanding since
  session 3.
- **Confirm the portal invariants actually got set** — spending cap,
  international block, sub-account, IP whitelist. These were step 1 of the
  session-4 plan; now that the trunk is live and registered they are load-
  bearing, not theoretical.

### Phase 3 — Insure the fragile bit (Alexa path B)

Path A's cookie will die, historically about once a year, and it will die on
a day you are not thinking about this project. Path B (`docs/alexa-sensor.md`)
is already written; what is missing is the AWS side — Smart Home skill,
Lambda, LWA credentials, one Routine per sensor.

Do this **while path A still works**, because then the failover is
`ORATA_ALEXA_MODE=sensor` and a reload. Do it after path A dies and it is an
afternoon of AWS console archaeology under pressure.

Verify with `ORATA_ALEXA_MODE=both` once (you hear every call twice), then
set it back to `arc`.

Start with the tiered sensor layout (`orata-family` / `orata-known` /
`orata-unknown`) — three Routines built once, versus one per contact forever.
ntfy still carries the exact name to your phone, so you lose less than it
sounds.

### Phase 4 — Reach and polish

- **Groundwire + WireGuard** on the mobile. This is what turns the DID into
  the household's actual number. Free SIP clients miss backgrounded calls;
  this is the one paid thing.
- Add the remaining endpoints from the `pjsip.conf` templates (six lines
  each) and decide the real ring group.
- Tune the gate's 7s `Read()` timeout once a real stranger has hit it.
- **Asterisk CVE watch.** The source build is a standing obligation — see
  `docs/build-from-source.md`. Subscribe to the AST-xxxx security advisories
  and know the rebuild steps before you need them urgently. This is the debt
  the project knowingly took on; the mitigation is a calendar reminder, not
  code.
- Still unresolved from session 3: the tarball signature was never verified
  (keyserver unreachable, key URLs 404). Check the fingerprint and SHA256
  from a machine with keyserver access.

### Explicitly out of scope unless asked

SMS/MMS, a dedicated `orata` unix user, call recording, and any reopening of
the Docker/k8s/FreePBX/HA questions. The rejection table at the top stands.

### Definition of done

The project is finished when: a call from a known number speaks their name on
the Echos; a call from an unknown number is gated and never reaches them; the
Pi survives a power cut and comes back registered on its own; the name book
is backed up somewhere other than the Pi; and the Alexa failover is a config
change rather than a project.

Phases 1 and 2 get you a phone you can rely on. Phase 3 is what keeps it
reliable a year from now.

### Do these too (found in session 3)

- ~~**PSU.**~~ Resolved. Was `0x50000` (sticky under-voltage + throttling);
  reads `0x0` as of session 4. Re-check after any power or cable change —
  the bits are sticky since boot, so a clean read only covers this boot.
- ~~**USB SSD.**~~ Done and verified in session 4. `findmnt /` =
  `/dev/sda2` on a 931.5G SanDisk Extreme Portable SSD, with **no SD card in
  the slot** (`blkid` shows one disk, so no duplicate-PARTUUID ambiguity), and
  root is 916G/8.5G used — resized to the full SSD, not stranded. The
  invariant holds.

  The disk was invisible on one USB port and booted first try on another; no
  diagnosis was done. Note that `cmdline.txt` also carries
  `usb-storage.quirks=0781:558c:u`, disabling UAS on that SanDisk bridge —
  so the port fault and the UAS fault are plausibly one marginal link, not
  two. Don't strip that quirk. See `docs/usb-ssd-boot.md`.
- **Verify the tarball signature.** Unresolved: keyserver unreachable, both
  published key URLs 404. Provenance rests on TLS alone. Fingerprint and
  SHA256 are in `docs/build-from-source.md` — check them from a machine with
  keyserver access.
- **Change the voicemail PIN.** Mailbox 100 was added to `[default]` in
  `/etc/asterisk/voicemail.conf` with PIN `1357`.

Path B (`docs/alexa-sensor.md`) is deliberately **not** on this list. Get the
phone working on path A first; B is a swap-in for when the cookie dies, and
its AWS setup is independent of everything above. Worth doing early anyway —
see the note at the end of "The Alexa tradeoff".

---

## Security invariants

Don't let these erode:

- Spending cap + international block set in the portal.
- Trunk uses a voip.ms **sub-account**, never the main login.
- **No port-forward on 5060.** Asterisk registers outbound; inbound calls ride
  that registration. Nothing needs to be open.
- Long random per-endpoint passwords. Most breaches are dictionary attacks on
  `1001/1001`.
- Remote devices connect over **WireGuard**, not exposed SIP.
- Pi boots from **USB SSD**, not SD — CDR logging kills cards.

---

## Client notes

Groundwire (~$10) is the only mobile SIP client with reliable push for a
self-hosted PBX; free clients miss calls when backgrounded. It's the one thing
here worth paying for. Desktop: MicroSIP (Windows), Linphone (Linux/macOS).

Two Alexa gotchas that will waste an hour if forgotten: **Do Not Disturb
suppresses announcements entirely**, and announcement volume follows device
volume (so an overnight volume routine will make calls inaudible).