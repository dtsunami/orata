# Experimental acoustic Echo callback bridge

**Disabled by default in the repo template. One user-confirmed live
connection on 2026-10-04; still a prototype.** One Echo, one pending call.

The user confirmed a complete run on the two-DID path: an allowed caller
reached the main DID, the pinned acoustic prompt played to the DCR010
speaker, Alexa called the separate callback DID, and the conference carried
the connected call. Earlier single-DID attempts did not establish automatic
bridging. ConfBridge and AGI are loaded on this host.

That single success does NOT establish: playback/timeout fallback to
SIP/voicemail, teardown on either party hanging up, cooldown behaviour, two
concurrent inbound legs under carrier limits, rejection of a spoofed callback
CID during the window, behaviour across multiple Echos, or recovery after a
reboot or Bluetooth reconnect. Exercise these deliberately with test traffic
before treating the bridge as available.

Known operational trap, hit once already: the pinned prompt on this
deployment speaks the callback digits aloud, so the DID exists in three
places — the WAV, INI `callback_destination`, and the
`ALEXABRIDGE_CALLBACK_DID` global. All three must agree. The INI and global
are cross-checked by the AGI; the WAV is not checked by anything.

## Flow

    Explicitly allowed caller -> MAIN DID -> existing gate -> waiting conference
         -> speaker plays the pinned "Alexa, call Orata" recording once
         -> Echo's Orata contact calls the NEW CALLBACK DID
         -> matching callback CID joins that conference
         -> two inbound carrier legs carry the conversation

The main and callback DIDs must be **different telephone numbers**. Both
must reach this Asterisk trunk with the called DID preserved in `${EXTEN}`.
The account/trunk must allow **two concurrent inbound calls across the DIDs**.
Verify carrier limits and charges. Bluetooth carries only the activation
command; no Echo SIP registration, direct microphone API or WAV upload to Amazon.

## Privacy and limits — read before enabling

**Caller ID is not authentication.** A spoofed callback CID during the
window can join the conversation. Config requires explicit acknowledgement
of this unresolved risk. Use controlled test traffic, not sensitive calls
or a secure unattended intercom. Household members must consent to the
room microphone becoming part of a telephone call.

Alexa presents the account's telephone CID, not a unique Echo device ID.
Your mobile may present the SAME CID. It can now originate on the main DID
if listed in `allowed_callers`; calls to that DID are never claimed as
callbacks. Only the separate callback DID can join the waiting conference.

The callback DID is RESERVED: wrong-CID, unmatched, late and duplicate calls
are declined and never trigger playback, SIP ringing or voicemail. The
dialplan also rejects that DID when the feature is disabled or AGI fails.
This safety depends on setting the correct `ALEXABRIDGE_CALLBACK_DID` global
and preserving the carrier's called DID. The AGI cross-checks that global
against INI `callback_destination` before allowing offers or claims.

Caller IDs, main `destination` and `callback_destination` match exact
received digits; do not assume 10/11-digit equivalence. Config rejects
identical main/callback numbers, including a US country-prefix alias.
The extra DID separates roles, NOT authenticates Alexa. Spoofing the callback
CID on that DID during the waiting window can still join. The test allowlist
is also CID-based and spoofable.

Only one pending/connected session is reserved; other calls ring SIP
normally. Callback window defaults to 30 seconds, cooldown to 60 seconds.
No acoustic retries. A sufficiently delayed callback may be mistaken for
a newer callback after cooldown: CID is not transaction correlation.
Wrong-room wake-word activation and multiple listening Echos are not solved.

Playback/sink failure or callback timeout kicks the original channel back
to SIP/voicemail. After an observed connection, departure ends the call,
not a new SIP ring attempt. Membership is polled: an extremely short
connection can escape observation. A hard-killed watcher is bounded by
participant timeouts, but immediate fallback/teardown is not guaranteed
in that case. These are prototype limitations requiring live validation.

The worker cancels playback on requester disconnect. A wake word already
heard cannot be recalled; resulting late calls are rejected.
No new conference recording is enabled by this dialplan.

## Components

- `bin/orata-alexa-bridge.py`: stdlib AGI + per-session watcher as `asterisk`,
  locked private state, validated tokens/CLI arguments, no AMI daemon.
- `asterisk/alexa-callback.conf`: private two-person conference and cleanup.
- `bin/orata-audio-worker.py`: fixed WAV/sink player as `dfstar`, in that
  user's existing PipeWire session; local socket with peer-UID checks.
- `etc/alexa-bridge.conf`: private INI DATA; not sourced shell config.
- `etc/orata-audio.service` / tmpfiles: host-specific user, UID and speaker.

The web installer does not deploy this feature. Repo templates do not
change live routing. No existing live config should be overwritten.

## Order and route the extra DID

1. In the voip.ms portal, order one additional **voice-capable DID**. Review
   recurring, inbound and channel charges before purchasing. Keep the main
   DID unchanged; a second registration is not inherently required.
2. Route the new DID to the same SIP account/subaccount and POP/server used
   by the registered Orata trunk. Confirm the account accepts two simultaneous
   inbound legs and that the carrier sends each original called DID.
   Do NOT forward the new DID to the main number: that loses role separation.
3. With the bridge disabled, reserve the new DID in the live dialplan global
   as described below, reload, and call it manually. In the Asterisk console,
   verify `${EXTEN}` is the new DID and the call is rejected without ringing.
   If the carrier replaces both destinations with one extension, stop and
   correct carrier routing before enabling.
4. Point the acoustic command at the NEW DID. Which artefact you change
   depends on how the prompt was recorded, and getting this wrong is the
   classic stale-prompt failure:
   - **Prompt names a contact** ("Alexa, call Orata"): change the telephone
     number on that Alexa contact to the NEW DID and keep the contact name
     unambiguous. The WAV only needs re-recording if the contact name changes.
   - **Prompt speaks the digits** ("Alexa, call 971 441 2343"): the number is
     baked into the audio. No config or dialplan edit can change it. Produce a
     new WAV either with `bin/orata-alexa-prompt.sh <new-did>` (synthesised,
     16 kHz wideband) or by recording one through a handset with `*96` and
     labelling it in the web UI (8 kHz telephone band). Then pin it:

         sudo install -m 640 -o root -g asterisk \
           /var/lib/orata/clips/<stem>.wav /var/lib/orata-audio/alexa-call.wav

     `pw-play` reopens the pinned path on every request, so a replaced file
     takes effect on the next call with no restart. Restart `orata-audio` only
     to make the startup validation reject a bad file immediately rather than
     at call time. Pinning copies: the clip library is pruned by retention,
     the pinned path is not.

   A stale digit prompt fails silently in a specific way: Alexa dials the OLD
   DID, nothing claims the callback on the new one, and the waiting caller is
   dropped to SIP/voicemail at `callback-timeout`. Check
   `/var/log/orata/alexa-bridge.log` for that reason before re-testing.

   Verify a manual command actually calls the new DID; Alexa contact sync,
   dialing availability and account behavior still require a live check.
5. Ordinary callers, including your mobile, continue to dial the MAIN DID.

## Migrate an existing single-DID prototype

This revision requires deploying the script, optional contexts, main-dialplan
hooks and new settings together. The old script is not compatible with the
new two-DID routing. Do not overwrite the live main dialplan or INI template.

- Disable live activation with
  `sudo asterisk -rx 'dialplan set global ALEXABRIDGE_ENABLED 0'`.
  Keep the on-disk global at 0 too. Wait for all bridge calls/watchers to
  finish and cooldown to expire; back up private live configs.
- Install the updated bridge script and optional contexts from the repo:

      sudo install -m 755 bin/orata-alexa-bridge.py /usr/local/bin/
      sudo install -m 640 -o asterisk -g asterisk asterisk/alexa-callback.conf /etc/asterisk/

- Merge the globals and callback-routing block from
  `asterisk/extensions.conf` into the live file. The old
  `GotoIf(...?resolve)` enable shortcut must be removed: it would skip
  callback-DID rejection while disabled. Keep the blocklist first.
- Set `ALEXABRIDGE_CALLBACK_DID=NEW_DID` in `[globals]`. Replace the offer
  call at `ring` with:

      same => n(ring),ExecIf($["${ALEXABRIDGE_ENABLED}" = "1"]?Gosub(orata-alexa-offer,s,1(${EXTEN},${ALEXABRIDGE_CALLBACK_DID})))

- Edit the existing `/etc/orata/alexa-bridge.conf`; replace placeholders
  below with exact received digits, not literal words:

      [bridge]
      enabled = false
      acknowledge_spoofable_callback = true
      callback_caller_id = ECHO_RECEIVED_CID
      destination = MAIN_DID
      callback_destination = NEW_DID
      allowed_callers = MOBILE_RECEIVED_CID

  These are edits to existing keys, not a replacement for the other settings.
  `MOBILE_RECEIVED_CID` may equal `ECHO_RECEIVED_CID`. Keep all audio, socket,
  state, log and timing settings. Do not change astdb ownership or whitelist
  policy automatically: owners still receive the existing press-1 gate on
  the main DID before the offer path.
- Reload while disabled, check globals and both contexts:

      sudo asterisk -rx 'dialplan reload'
      sudo asterisk -rx 'dialplan show globals'
      sudo asterisk -rx 'dialplan show orata-alexa-offer'
      sudo asterisk -rx 'dialplan show orata-alexa-callback'

- Complete the carrier/contact checks above, then run controlled acceptance.
  The audio worker/unit does not need replacing solely for this migration.
  Do not delete state files to bypass cooldown.

The watcher now logs playback requested/completed, connection and the first
termination reason without logging caller numbers. This is local playback
evidence, not proof that Alexa heard or placed the call.

## Reviewed fresh deployment — operator steps

Back up live dialplan/config privately and check no active calls. Review
host-specific `dfstar`, UID `1000`, group `asterisk` and Bluetooth node name.
Install the NEW INI config only if it does not already exist; on update,
edit/merge rather than replacing it.

    sudo install -m 755 bin/orata-alexa-bridge.py bin/orata-audio-worker.py /usr/local/bin/
    sudo test -e /etc/orata/alexa-bridge.conf || \
      sudo install -m 640 -o root -g asterisk etc/alexa-bridge.conf /etc/orata/
    sudo install -d -m 700 -o asterisk -g asterisk /var/lib/orata/alexa-bridge
    sudo install -d -m 750 -o root -g asterisk /var/lib/orata-audio

Pin the existing recording outside the timestamped library so retention
cannot prune it. This copies, not moves, the original:

    sudo install -m 640 -o root -g asterisk \
      /var/lib/orata/clips/20261004-115024.wav /var/lib/orata-audio/alexa-call.wav

Ensure `/var/log/orata` exists and is writable by `asterisk`; the watcher
uses `alexa-bridge.log` there. Install the runtime directory and service:

    sudo install -m 644 etc/orata-audio-tmpfiles.conf /etc/tmpfiles.d/orata-audio.conf
    sudo systemd-tmpfiles --create /etc/tmpfiles.d/orata-audio.conf
    sudo install -m 644 etc/orata-audio.service /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo systemctl enable --now orata-audio

`dfstar`'s PipeWire/WirePlumber must remain available without an interactive
login. If appropriate, `sudo loginctl enable-linger dfstar` retains the user
manager, but does not guarantee Bluetooth reconnect. Verify cold boot.
Do not run PipeWire as root or weaken Orata's private clip-directory modes.
The service uses `ProtectHome=tmpfs` with a read-only bind of
`/run/user/1000`: home directories stay hidden, but the existing PipeWire
socket remains accessible. `ProtectHome=yes` without that exception hides
`/run/user` and causes playback failure even when the speaker is connected.
Update both runtime paths in the unit if the owner's UID is not 1000.

Edit with `sudoedit /etc/orata/alexa-bridge.conf`:
- Exact Alexa callback CID, main `destination`, new `callback_destination`,
  and allowed originating CIDs (which may include the callback CID).
- Stable sink name `bluez_output.08_EB_ED_71_F9_02.1` on this host, not ID 77.
- Both socket entries must match.
- Keep `enabled=false` until ready for a controlled test.
- Enabling requires `acknowledge_spoofable_callback=true`.

Merge the globals, callback hook after blocklist, and offer hook at ring
from repo `asterisk/extensions.conf` into the live file. Set
`ALEXABRIDGE_CALLBACK_DID` to the exact new DID in `callback_destination`.
The offer Gosub must receive `${EXTEN},${ALEXABRIDGE_CALLBACK_DID}` and
forward `${ARG1},${ARG2}` to AGI: inside the Gosub, `agi_extension` is `s`,
not the home DID. Preserve local ring group, credentials, NAT, includes
and unrelated recorder fixes.
Install the optional contexts and add `#tryinclude "alexa-callback.conf"`:

    sudo install -m 640 -o asterisk -g asterisk asterisk/alexa-callback.conf /etc/asterisk/
    sudo asterisk -rx 'dialplan reload'
    sudo asterisk -rx 'dialplan show orata-alexa-offer'
    sudo asterisk -rx 'dialplan show orata-alexa-callback'
    sudo systemctl status orata-audio --no-pager

Only for the controlled test, after reload and verification: set INI
`enabled=true`, then `sudo asterisk -rx 'dialplan set global ALEXABRIDGE_ENABLED 1'`.
A later reload restores the globals from disk; leave the on-disk enable
switch at 0 during acceptance. No Asterisk restart. Both enable switches
are required. Do not enable with the contexts missing.

## Acceptance

Observe `sudo asterisk -rvvv`, `sudo journalctl -u orata-audio -f`, and the
private watcher log. Avoid sharing real caller numbers or call content.

1. Allowed mobile -> main DID -> one command -> Echo calls callback DID ->
   two-way voice; MicroSIP does not ring after successful bridging.
   Include a test where mobile and Echo present the SAME caller ID.
2. Hang up each side separately: both legs clear, no abandoned conference.
3. Disconnect speaker: no default-output playback; original rings SIP.
4. Prevent callback: original rings SIP after window; late return declined.
5. Hang up while waiting: playback cancelled; any late return declined.
6. Concurrent original/duplicate return: no cross-connection or repeat command.
7. Blocked/unlisted/unknown calls retain existing block/gate/SIP policy.
8. Repeat after cooldown and recheck after a maintenance-window reboot.
9. Callback DID with no pending session, wrong CID, or disabled bridge:
   rejected without answering, speaking, SIP ringing or voicemail.
10. In a controlled maintenance window, mismatch the global and INI callback
    DID: no activation; global-reserved callback DID still rejects. Restore
    both values before the next test. Never leave routing mismatched.

Isolated tests do not establish any live acceptance:

    python3 test/alexa_callback.py
    python3 test/regression.py

## Rollback

Set live `ALEXABRIDGE_ENABLED=0`; also keep the on-disk value at 0 for reloads.
After current bridge calls end, set INI `enabled=false` and stop/disable
`orata-audio`. Keep `ALEXABRIDGE_CALLBACK_DID` populated: that DID remains
reserved and rejects even when disabled. Calls to the main DID return to
existing gate/SIP policy, including calls from the shared mobile/Echo CID.
No token rotation, astdb deletion, Bluetooth re-pairing or Asterisk restart
is needed. To repurpose the extra DID later, review its routing explicitly.