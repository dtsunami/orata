# Orata appliance image

**Status: not built or booted. The code exists; no image has been
flashed, no first boot has been observed.** Everything below is the
intended procedure, not a verified one.

This describes the cloned Raspberry Pi 4 image: power on, open the web UI,
configure the trunk, pair a speaker, done. It supersedes the hand-build in
[install.md](install.md) for appliance users; that document remains correct
for a manually built Pi and is still the reference for Asterisk itself.

## What changed from the hand-built design

The hand-built Pi assumed its owner would `sudoedit` configuration files.
An appliance owner will not, so two boundaries moved. The reasoning and
conditions are recorded in [web-ui.md](web-ui.md#boundary-reversal-accepted-2026-10-04--appliance-image).

| | Hand-built | Appliance |
|---|---|---|
| Auth | Shared `?token=` URL | Username + password, per-device |
| Asterisk config | `sudoedit` only | UI writes it, via privileged helper |
| Bluetooth | `bluetoothctl` only | UI scans, pairs, connects |
| Secrets | Operator-generated | Generated at first boot |

## Components

    orata-firstboot.service  -> orata-firstboot.sh      (oneshot, root)
    orata-admin.service      -> orata-admin-helper.py   (daemon, root)
    orata-web.service        -> orata_web.py            (daemon, asterisk)

`orata-web` still runs unprivileged with `NoNewPrivileges` and still cannot
write `/etc/asterisk`. When it needs to, it sends a JSON request over
`/run/orata-admin/admin.sock` to the helper, which checks the peer UID and
accepts only enumerated operations. There is deliberately **no**
"write this file" verb: every settable field is named in the helper's
`SCHEMA` with its own validator, so a compromised web process can set
`trunk.did` to a different number but cannot append arbitrary lines to
`pjsip.conf`, add a dialplan extension, or read `/etc/shadow`.

The single most important validator rejects `\r`, `\n` and NUL in any
value. A newline in an INI value is config injection; everything else is
defence in depth.

## First boot

`orata-firstboot.service` runs before `orata-web` can serve and is the
reason a cloned image is safe to distribute. It:

1. regenerates SSH host keys (a shipped host key lets anyone with the image
   impersonate every appliance),
2. regenerates `/etc/machine-id`,
3. generates a random 12-character admin password, stores it PBKDF2-hashed
   in `/var/lib/orata/admin-auth.json` with `must_change: true`,
4. generates the session-signing secret,
5. clears any `ORATA_WEB_TOKEN` baked into the image,
6. prints the URL and password to the console,
7. stamps `/var/lib/orata/.firstboot-done`.

The UI refuses every page except `/password` and `/logout` until the
password is changed. Lost it:

    sudo rm /var/lib/orata/.firstboot-done
    sudo systemctl start orata-firstboot

## Building the image — not yet done

Unverified; expect to iterate. On a working, configured Pi:

1. Remove every per-device secret: `/var/lib/orata/admin-auth.json`,
   `/var/lib/orata/.firstboot-done`, `ORATA_WEB_SECRET` and
   `ORATA_WEB_TOKEN` in `web.conf`, SSH host keys, `/etc/machine-id`,
   WiFi credentials in `wpa_supplicant.conf`, shell history,
   `/var/log/*`, astdb if it holds real numbers.
2. **Remove the SIP credentials** from `pjsip.conf` and the ARC refresh
   token from `announce.conf`. These are yours, not the user's.
3. Confirm `systemctl is-enabled orata-firstboot orata-admin orata-web`.
4. Shut down, image the card, shrink, compress.
5. **Flash it to a different card and boot it.** Verify the password is
   different from the source Pi and that `/password` is forced. An image
   that ships a working password is the failure this whole design exists
   to prevent.

## Security posture

Unchanged and non-negotiable: **no TLS, no port-forward.** Username and
password over plain HTTP on a home LAN is the same posture as a consumer
router, which is the explicit comparison the owner asked for. Anyone who
can sniff the LAN can take the session cookie.

- Login is throttled to 5 attempts per minute per IP, in memory only.
- Sessions are HMAC-signed, `HttpOnly`, `SameSite=Strict`, 12 hours.
- Config writes keep 20 numbered backups in
  `/var/lib/orata/config-backups`. There is no restore button; restore by
  hand over SSH.
- A UI write and a concurrent `sudoedit` can race. Last write wins.

**Toll fraud is the real risk.** The trunk dials premium-rate numbers. A
spending cap, international block and IP restriction in the voip.ms portal
are mandatory for an appliance, not optional hardening. The UI cannot
enforce them; they live in the carrier portal.

## Verified on this Pi, 2026-10-04

Deployed and exercised against the live Pi at 192.168.88.41. `orata-firstboot.sh`
was deliberately NOT run: it regenerates SSH host keys and machine-id, which
would have broken existing known_hosts entries on a working machine. Auth was
provisioned by hand using the same PBKDF2 code path. That means **the
first-boot script itself is still unexecuted** and remains the largest
untested piece.

Working, confirmed by request rather than by reading the code:

- Unauthenticated request to any page redirects to `/login`; login page
  serves; wrong password refused; correct password sets a session cookie;
  `must_change` forces `/password`; after the change the dashboard serves.
- `/trunk` reads the live `pjsip.conf` and shows the real POP and
  sub-account. The SIP password is never echoed into the HTML.
- `/bluetooth` lists the paired DCR010 and both PipeWire sinks.
- Helper rejects a malformed MAC and an unknown op.
- A POP change applied to a copy of the real config updated all four
  places, preserved the sub-account in `client_uri`, left the other 17
  sections untouched, and wrote a backup. The live trunk was not modified.

### Four defects this deployment exposed

Each failed only against the real system, not in tests:

1. **Wrong section names.** The schema guessed `voipms-auth`; the live
   config uses `voipms_auth`. Every field read back empty.
2. **The POP lives in four places.** `server_uri`, `client_uri`, the
   identify `match` and the AOR `contact`. Writing one would have left
   registration and inbound identification disagreeing. Fields are now a
   list of targets, validated before any write so a missing section cannot
   half-apply a change.
3. **`trunk.did` did not exist.** The dialplan routes inbound with
   `exten => _X.` and never compares a configured number. The field was
   removed rather than left as a box that does nothing.
4. **Socket unreachable, then sinks empty.** `RuntimeDirectory=` is
   `root:root`, so `asterisk` could not traverse it; an `ExecStartPre`
   chgrp silently succeeded against a private `/run` namespace. The daemon
   now sets the group itself. Separately, the combination of sandboxing
   directives dropped `CAP_SETUID`, so dropping to the desktop user for
   `pw-dump` failed with EPERM and the sink list came back empty with
   `ok: true`. Fixed with explicit `AmbientCapabilities`.

Defects 1-3 are the same class: the schema was written against the repo
template rather than the deployed file. Anything added to `SCHEMA` should be
checked against a live config first.

## Still missing before anyone ships this

- No image has been built or booted. Every step above is untested.
- No factory-reset path except re-flashing.
- No update mechanism. An appliance in someone's house needs one, and
  `git pull` plus `install` is not it.
- `admin-auth.json` supports one user. No recovery except SSH.
- The helper has no audit log: a config write leaves a backup file but no
  record of who made it or when.
- P3 in [next-steps.md](next-steps.md) — backup, log rotation, reboot
  recovery, failure notification — is unstarted and matters more for an
  appliance than for a Pi whose owner reads logs.