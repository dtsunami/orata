# Next steps — finish the household phone

Updated 2026-10-04. Alexa's initial smoke test is **user-confirmed**; the
tested device count/command was not recorded. The two-DID callback bridge
also connected end to end once on 2026-10-04; its acceptance work is P5 and
does not block the release. This plan scopes remaining work, not a commitment
to add new infrastructure.

## P0 — Power up and verify the remaining Echos

No new token is needed just to plug in more devices on the same account.

1. Retrieve Echos, place them, power them up and reconnect Wi-Fi if required.
   Check the Alexa app: online, same Amazon account, unambiguous device name.
2. Check volume, Do Not Disturb and volume-changing Routines.
3. List devices as `asterisk` after sourcing the live config:

       sudo -u asterisk bash -c '
         set -e
         . /etc/orata/announce.conf
         : "${REFRESH_TOKEN:?Missing token in live config}"
         : "${TMP:?Missing private cookie directory}"
         umask 077
         exec "$ORATA_ARC" -a
       '

4. Set `ORATA_ALEXA_DEVICES` to one exact name in the live config, then send
   a test and listen. No reload needed. This sends real Alexa/ntfy traffic:

       sudo -u asterisk /usr/local/bin/orata-announce.sh 15551234567 "Test Caller"

5. Repeat for every intended room. Then test `ALL` with the desired devices
   online. If it reaches unwanted supported devices, choose a narrower policy
   supported by upstream rather than assuming a comma-separated list works.
6. Repeat a second announcement. Measure delivery time; sequential speech
   and offline devices may exceed Orata's 20-second ARC timeout. Change timeout
   or targeting only if measurement demonstrates the problem.

Use this evidence table; do not record tokens or authentication traces:

| Room / exact Echo name | Online/account | DND off/volume | First + repeat audible | Delay / issue |
|---|---|---|---|---|
| Fill in during rollout | | | | |

**Exit:** every intended room hears the correct test phrase repeatedly;
unwanted devices do not. An `alexa OK` line alone is not a pass.

## P1 — End-to-end inbound acceptance

Review live deployment first; see [safe update](install.md#updating-the-working-pi).
Use real phones and listen on an Echo while another person answers the SIP
handset. Test numbers must match actual caller-ID digits (usually 11 in NANP).

| Case | Expected result |
|---|---|
| Named, non-owner caller | Bypasses gate; correct name on Echo; SIP rings; two-way audio |
| Truly unknown, no carrier CNAM, not allowed | Gate audible; no Echo speech/ringing before press 1 |
| Unknown presses 1 | Gate passes; rings/announces; next call self-whitelisted |
| Unknown times out / wrong key | Rejected; no Echo speech or SIP ringing |
| Blocked number (even if named/owner) | Declined before answer; no ring, announcement or recording |
| Owner presses 1 | Gate retained, then normal ring/name announcement |
| Owner enters 3434 | Saved voice clip reaches configured SIP phones, not Echos |
| Unanswered accepted call | Mailbox 100 works with the new PIN |
| Repeat calls / announcement failure | Calling still works; notifier cannot stall ringing |

Do not remove a real caller's name/allow entries just to test without noting
and restoring them. A non-empty carrier CNAM bypasses the gate too; absence
from the local name book does not prove a caller is unknown.

**Exit:** record actual results, revision/date and failures without PII.
Local WAV playback and the Harness's direct script call do not prove the
inbound dialplan path.

## P2 — Recording and UI acceptance

Source fixes have regression coverage; live deployment is not independently
confirmed. Complete these after merging recorder changes:

- *96: save, replay, re-record, cancel; saved clip plays in Audio.
- Owner mode: save/broadcast, hang up during the next start prompt (no bogus
  missing-recording failure), then hang up mid-recording (save actual audio only).
- Confirm failed saves do not broadcast or say saved.
- Assigned prompts/caller-name copies survive clip-library retention.
- Check prompt fallback files before resetting overrides.
- Desktop/mobile browser: dashboard, help popovers, books, playback and
  useful errors. No token/private topic appears in pages.
- Leave in-call 3434 capture optional; confirm which party's channel is
  captured and obtain consent before using it.

**Exit:** real-channel behavior matches the UI; logs explain failures without
a server error. SIP clip broadcast remains record-then-play, not live paging
or arbitrary WAV playback on Echos.

## P3 — Operate unattended (highest-priority engineering follow-up)

Deliver small OS-level pieces, not a new daemon/framework:

1. **Backup/restore:** all five astdb books, live Asterisk/Orata configs,
   assigned role/name WAVs and metadata, deployment/version notes. Include
   needed authentication state only in a private encrypted backup. Use a
   consistent SQLite backup or validated book export, not a blind copy of
   active astdb. Keep a copy off-box and demonstrate restore.
2. **Log rotation:** Orata notification logs plus source-install Asterisk
   logs. Set retention and test rotation without losing ownership or logging.
   WAV retention does not bound log growth.
3. **Security checks:** new voicemail PIN; portal cap/international block,
   sub-account and appropriate IP restrictions; random endpoint passwords;
   private config/cookie permissions; no public SIP/admin ports.
4. **Reboot recovery:** during a maintenance window with no active calls,
   controlled reboot, then confirm trunk, UI, SIP clients, two-way audio and
   audible Alexa delivery without login intervention. Do not start with a
   forced power cut; unclean-shutdown testing needs a backup/recovery plan.
5. **Failure notification:** test ntfy subscription/delivery, then scope a
   timer/cron check for recent Alexa/trunk failures. Alert with bounded
   frequency and clear recovery semantics, not a weekly grep that reports
   old failures forever. No voice smoke test by default overnight.
6. **Maintenance:** document the reviewed ARC version; monitor Asterisk
   advisories and rebuild safely; close the tarball signature-verification gap.

**Exit:** reproducible restore, bounded logs, boot recovery, and a working
failure-notification channel. These capabilities are planned, not implemented
by this documentation change.

## P4 — Household usability, only as needed

- Select real SIP endpoints and ring group rather than assumed pc/mobile/desk.
- Verify mobile SIP background/push behavior over WireGuard outside the LAN;
  do not promise a client will work until tested.
- Tune gate wording/timing from real callers, preserving block-before-answer.
- Improve UI error guidance based on acceptance failures. System credentials
  stay file-managed; no generic config editor or new frontend build required.

## P5 — Callback bridge acceptance, after the release checks

One connection is not a feature. Do these with test traffic and the bridge
re-disabled afterwards if any fail; keep the repo template at
`enabled = false`. Do not delete state files to bypass cooldown.

| Case | Expected result |
|---|---|
| Playback fails (speaker off/unreachable sink) | Original caller falls back to SIP/voicemail, no hang |
| No callback within the window | `callback-timeout`, caller released to SIP/voicemail |
| Callback arrives after cooldown | Declined; no playback, ring or voicemail |
| Either party hangs up mid-call | Both legs torn down; no orphaned conference or watcher |
| Wrong CID on the callback DID | Declined; conference not joined |
| Second caller during a pending session | Rings SIP normally; no second acoustic prompt |
| Ordinary caller dials the callback DID | Rejected without ringing |
| Repeat the full path three times | Same outcome each time, including after cooldown |
| Reboot, then repeat | Bluetooth sink reconnects without interactive login |

Verify after any DID change that the pinned WAV, INI `callback_destination`
and `ALEXABRIDGE_CALLBACK_DID` all name the same number. Nothing checks the
WAV automatically; `md5sum` the clip against the pinned copy after re-pinning.

**Exit:** fallback and teardown observed directly, not inferred from a
successful call; `/var/log/orata/alexa-bridge.log` explains each outcome.
Spoofable callback CID remains an accepted, documented risk, not a solved one.

## Scope boundaries and definition of done

Required for the initial usable release: P0/P1 acceptance, applicable P2
recording checks, and P3 backup/rotation/security/reboot/failure notification.
P4 depends on the household's intended clients.

**Not required:** AWS Lambda/Smart Home skill migration, SMS/MMS, Home
Assistant, FreePBX, container runtime, AMI service, arbitrary Echo WAV/live
audio, automated credential entry, or a dedicated new Unix service user.
Sensor mode is an optional future decision, not a promised reliable failover.

Done means: intended rooms hear accepted calls by name; blocked/rejected calls
do not announce; phones still work if Alexa fails; recordings behave as stated;
state can be restored off-box; logs stay bounded; a reboot recovers without a
human; failures reach a monitored channel. Record evidence, not just green logs.