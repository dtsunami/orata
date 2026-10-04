# Experimental acoustic Echo callback bridge

**Disabled by default; not deployed or live-verified.** One Echo, one pending call.

The user confirmed that the saved `alexa_call_orata` command played through
the separate DCR010 Bluetooth speaker causes Alexa to call the home DID.
ConfBridge and AGI are loaded on this host. These facts do NOT prove that
the automatic bridge, fallback or teardown works.

## Flow

    Explicitly allowed caller -> existing gate -> private waiting conference
         -> speaker plays the pinned "Alexa, call Orata" recording once
         -> Echo calls the same home DID
         -> reserved callback caller ID joins that conference
         -> two inbound carrier legs carry the conversation

The DID/trunk must allow **two concurrent inbound calls**. Verify carrier
limits and charges. Bluetooth carries only the activation command; no
Echo SIP registration, direct microphone API or WAV upload to Amazon.

## Privacy and limits — read before enabling

**Caller ID is not authentication.** A spoofed callback CID during the
window can join the conversation. Config requires explicit acknowledgement
of this unresolved risk. Use controlled test traffic, not sensitive calls
or a secure unattended intercom. Household members must consent to the
room microphone becoming part of a telephone call.

Alexa presents the account's telephone CID, not a unique Echo device ID.
It is RESERVED while enabled: unmatched, late or duplicate calls from that
CID are declined and can never trigger playback. Your mobile may share
the same CID and therefore cannot originate a test bridge request.
Use a **different** trusted phone number in `allowed_callers`.

Both caller IDs and `destination` match exact received digits; do not
assume 10/11-digit equivalence. Only the configured DID participates.
Caller-ID spoofing also limits the trustworthiness of the test allowlist.

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

## Reviewed deployment — operator steps, not yet run

Back up live dialplan/config privately and check no active calls. Review
host-specific `dfstar`, UID `1000`, group `asterisk` and Bluetooth node name.
Install the NEW INI config only if it does not already exist; on update,
edit/merge rather than replacing it.

    sudo install -m 755 bin/orata-alexa-bridge.py bin/orata-audio-worker.py /usr/local/bin/
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

Edit with `sudoedit /etc/orata/alexa-bridge.conf`:
- Exact Alexa callback CID, called DID, and DIFFERENT allowed originating CID.
- Stable sink name `bluez_output.08_EB_ED_71_F9_02.1` on this host, not ID 77.
- Both socket entries must match.
- Keep `enabled=false` until ready for a controlled test.
- Enabling requires `acknowledge_spoofable_callback=true`.

Merge the globals, callback hook after blocklist, and offer hook at ring
from repo `asterisk/extensions.conf` into the live file. The offer Gosub
must receive `${EXTEN}` and forward `${ARG1}` to AGI: inside the Gosub,
`agi_extension` is `s`, not the home DID. Preserve local
ring group, credentials, NAT, includes and unrelated recorder fixes.
Install the optional contexts and add `#tryinclude "alexa-callback.conf"`:

    sudo install -m 640 -o asterisk -g asterisk asterisk/alexa-callback.conf /etc/asterisk/
    sudo asterisk -rx 'dialplan reload'
    sudo asterisk -rx 'dialplan show orata-alexa-offer'
    sudo asterisk -rx 'dialplan show orata-alexa-callback'
    sudo systemctl status orata-audio --no-pager

Only for the controlled test: set INI `enabled=true` and live global
`ALEXABRIDGE_ENABLED=1`, then reload the dialplan. No Asterisk restart.
Both enable switches are required. Do not enable with the contexts missing.

## Acceptance

Observe `sudo asterisk -rvvv`, `sudo journalctl -u orata-audio -f`, and the
private watcher log. Avoid sharing real caller numbers or call content.

1. Allowed DIFFERENT phone -> one command -> Echo callback -> two-way voice;
   MicroSIP does not ring after successful bridging.
2. Hang up each side separately: both legs clear, no abandoned conference.
3. Disconnect speaker: no default-output playback; original rings SIP.
4. Prevent callback: original rings SIP after window; late return declined.
5. Hang up while waiting: playback cancelled; any late return declined.
6. Concurrent original/duplicate return: no cross-connection or repeat command.
7. Blocked/unlisted/unknown calls retain existing block/gate/SIP policy.
8. Repeat after cooldown and recheck after a maintenance-window reboot.

Isolated tests do not establish any live acceptance:

    python3 test/alexa_callback.py
    python3 test/regression.py

## Rollback

Set live `ALEXABRIDGE_ENABLED=0` and reload. After current bridge calls end,
set INI `enabled=false` and stop/disable `orata-audio`. Normal Echo/mobile
calls then follow existing gate/SIP policy. No token rotation, astdb deletion,
Bluetooth re-pairing or Asterisk restart is needed.