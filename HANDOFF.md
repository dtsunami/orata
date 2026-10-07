# Orata — current status and handoff

Updated **2026-10-04**. This is the current handoff, not a fresh-install guide.

**The phone works; the user reports that Alexa's smoke test worked.**
The user also confirmed that the saved `alexa_call_orata` command, played
through the Pi's DCR010 Bluetooth speaker, causes Alexa to place a call.

**2026-10-04: the two-DID callback bridge connected end to end once.**
The user confirmed a VoIP call to Alexa initiated by Orata via the callback
path: acoustic prompt played, Echo called the separate callback DID, and the
conference carried the connected call. Earlier single-DID attempts had not
established automatic bridging; separating the main and callback DIDs is what
made the claim path work. This is ONE user-confirmed connection, not a tested
feature: fallback, teardown, cooldown, concurrency, spoofed-CID rejection and
repeatability remain unverified, and the repo template stays
`enabled = false`. See [callback prototype](docs/alexa-callback.md).

The pinned prompt at `/var/lib/orata-audio/alexa-call.wav` SPEAKS THE CALLBACK
DIGITS on this deployment. Changing the callback DID in the INI or dialplan
does not change the audio: the WAV must be re-cut and re-pinned, or Alexa
dials the old number and the waiting caller is dropped at `callback-timeout`.
Caller-ID spoofing and acoustic activation remain explicit limitations.
Echo coverage and the complete inbound-call acceptance matrix remain open.

## Evidence and remaining uncertainty

| Area | Evidence | Not yet established |
|---|---|---|
| Platform | Raspberry Pi 4, Pi OS/Debian 13 arm64, Asterisk 22.11.0 built from source | Security update/provenance follow-up |
| Storage/power | Earlier on-Pi verification: USB SSD root with no SD card; throttling `0x0` | Cold-boot recovery and current power health |
| Telephony | Earlier executed PSTN inbound/outbound calls with MicroSIP and two-way audio | Off-LAN mobile push, post-reboot registration |
| Gate/blocklist | Earlier real-channel probes verified known/unknown/blocked branches | Human DTMF timing and current owner branches |
| Web UI | Dashboard, Configure, five books, prompt/clip editing implemented in source | Full browser/accessibility review |
| Audio fixes | Audio-page crash, WAV format handling, retention and hangup-save guards corrected in source | Exact deployed revision and live hangup behavior not independently audited |
| Automated tests | All **19 regressions passed**; shell syntax and whitespace checks passed | Tests mock Asterisk/ARC; no Amazon or live-call proof |
| Alexa ARC | User obtained refresh token after Windows helper packaging failure, then reported smoke-test success | Exact tested Echo(s)/command, multi-device coverage, real inbound name speech |
| Alexa callback bridge | 2026-10-04: one user-confirmed end-to-end connection on the two-DID path, after re-pinning the digit prompt | Fallback, teardown, cooldown, concurrent legs, spoofed-CID rejection, repeat runs, cold-boot speaker reconnect |
| ntfy | Implemented independent notification channel | Subscription and actual fallback delivery |
| Alexa sensor | Script and design document exist | AWS/skill/Routine deployment; entirely optional |

User-confirmed smoke-test success is not expanded into a claim that every
Echo or every inbound path has been tested. `alexa OK` means ARC returned
success; `clip CAST` counts dispatched commands, not completed playback.

## Runtime and scope

Core: Asterisk plus backgrounded shell scripts. Optional admin UI:
FastAPI/uvicorn from Debian packages, running as `asterisk`. Node.js is a
desktop login-helper dependency, not a new Pi daemon.

Preserve the decisions: no runtime Docker/k8s, FreePBX, Home Assistant or
AMI daemon. Do not introduce AWS/skill infrastructure merely to finish ARC.
Sensor mode remains an optional future tradeoff: static speech for additional
cloud setup. It is not an automatic fallback.

Caller handling:
1. Blocklist wins: hang up before answering or announcing.
2. Resolve local name, then carrier CNAM.
3. Owners always get the gate; `3434` records/broadcasts saved clips to SIP
   handsets. Caller ID is spoofable: ownership is not strong authentication.
4. Non-owner whitelist or resolved name skips the gate.
5. Unknown callers press 1 and self-whitelist, unless STRICT rejects them.
6. At ring: background the notifier, ring SIP devices, then voicemail.

Carrier CNAM can make a caller appear known. Test an unknown number with no
resolved name; do not assume every number absent from the local book is gated.

## Alexa configuration that matters

See [Alexa setup](docs/alexa-arc.md):
- `ORATA_ALEXA_MODE=arc`, `ORATA_ALEXA_CMD=speak`.
- Export refresh token, correct region and persistent private `TMP` from
  the protected **live** `/etc/orata/announce.conf`.
- `-login` authenticates, `-a` lists devices, **`-l` logs out**.
- Do not repeat the old `announce:`, `-a` proxy-login or `-l` list instructions.
- No Asterisk reload for announce.conf changes.
- Recorded WAVs/names affect local/SIP audio, not Echo text-to-speech.

Keep tokens, device private keys, ntfy topics, SIP credentials and UI tokens
out of Git and shared output. Only ARC's refresh token is needed from the
helper; its device private key is not an Orata setting.

## Documents and deploy boundaries

- [Appliance image (new direction, unbuilt)](docs/appliance.md)
- [Install / safe update](docs/install.md)
- [Prioritized next steps and acceptance](docs/next-steps.md)
- [Alexa ARC / Windows helper workaround](docs/alexa-arc.md)
- [Web UI / security / recording fixes](docs/web-ui.md)
- [Asterisk build and CVE maintenance](docs/build-from-source.md)
- [USB SSD history](docs/usb-ssd-boot.md)
- [Tests and limitations](test/README.md)
- [Optional sensor design](docs/alexa-sensor.md)
- [Experimental Echo callback bridge](docs/alexa-callback.md)
- [Kubernetes / k3s, evaluated and rejected](docs/kubernetes.md)

Repo configs are templates, not the deployed secrets. Updating binaries must
not overwrite `/etc/asterisk/*.conf` or `/etc/orata/*.conf`. Merge reviewed
dialplan changes; preserve known-good NAT, POP, credentials and includes.
The web installer updates only the web app/unit and its bind/port settings;
it does not deploy the clip/notifier scripts or dialplan.

## Next priorities

1. **Echo rollout + inbound acceptance:** inventory and listen on each device;
   test named caller, real gate, rejection, owner and repeated calls.
2. **Unattended operations:** protected off-box backup with restore test,
   log rotation, secure voicemail/portal settings, planned reboot recovery,
   and notification of Alexa/trunk failures.
3. **Household usability:** choose actual SIP endpoints/ring group, verify
   mobile-over-WireGuard behavior, finish recording and browser acceptance.
4. **Optional expansion only after acceptance:** sensor route, SMS/MMS,
   new services or arbitrary Echo audio playback.

The scoped release definition is in docs/next-steps.md. No claim that the
whole system is finished until those acceptance checks have evidence.

## Historical milestones retained

- Sessions 1–2: repo/design and disposable Debian 12 container rig.
- Session 3: Debian 13 lacked Asterisk; built 22.11.0, verified astdb and
  inbound branches on real channels, generated prompts.
- Session 4: USB SSD boot and PSU issues resolved; preserve the SanDisk
  `usb-storage.quirks=0781:558c:u` workaround unless investigating it.
- Session 5: MicroSIP/PSTN calls worked both directions with audio.
- 2026-10-04: UI/audio fixes, 19 passing regressions, corrected ARC setup,
  refresh token obtained and Alexa smoke test user-confirmed.
- 2026-10-04: second DID ordered and routed; callback bridge connected end to
  end once. Root cause of the preceding failure was a stale pinned prompt
  still speaking the old DID, fixed by recording a new clip and copying it to
  `/var/lib/orata-audio/alexa-call.wav`. Added `bin/orata-alexa-prompt.sh`.

Unclosed historical items: Asterisk tarball signature verification,
voicemail PIN (earlier shipped value must be replaced if still present),
portal spending/IP restrictions, off-box backup and reboot survival.
These are not presumed resolved simply because a call or smoke test worked.