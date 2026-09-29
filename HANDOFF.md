# HANDOFF

Session 1. Greenfield -> config repo written. **Nothing has been deployed or
executed.** Every file here was authored on a Windows dev box and has never
run on a Pi. Treat all of it as unverified.

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
bin/orata-web.py           stdlib-only web admin for the astdb books
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
- **`custom/press-one` prompt does not exist yet.** README has espeak+sox
  steps to generate it. Until then the gate will fail on unknown callers.
- **CRLF.** Files authored on Windows. README has the `sed -i 's/\r$//'` fix;
  if bash says `bad interpreter` or Asterisk misparses a config, that's why.
- **`local_net` in pjsip.conf** assumes 192.168/16 and 10/8. The
  `external_media_address` / `external_signaling_address` lines are commented
  out — uncomment **both** only if the Pi is behind NAT.
- Carrier CNAM from voip.ms is unreliable on mobile-originated calls. This is
  why the local name book takes priority in the dialplan.

---

## Next session: do these in order

The staging matters — it means a failure tells you which layer owns it.

1. **voip.ms portal first.** Spending cap, block international dialing, create
   a sub-account for the trunk, enable IP whitelist. Before anything
   registers. This is the only irreversible mistake available; compromised
   PBXes get drained to premium-rate numbers overnight.
2. `apt install asterisk curl jq`, deploy configs, fill in POP + passwords,
   `core reload`, confirm `pjsip show registrations` shows `Registered`.
3. Get calls ringing on **one** softphone (MicroSIP on the PC). Ignore Alexa.
4. Generate the `press-one` prompt, test the gate with an unknown caller.
5. Only then: `alexa_remote_control.sh -a`, then dial `*99` to test the
   announcement path in isolation.

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