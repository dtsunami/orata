# Install and update Orata

Current platform: Raspberry Pi OS/Debian 13 arm64, USB SSD, Asterisk 22 LTS
built from source. Current status: [HANDOFF](../HANDOFF.md).
Alexa smoke-test success is user-confirmed; [acceptance remains](next-steps.md).

**Already working? Use the update section, not fresh install.** Repo configs
are templates. Never overwrite working credentials, NAT settings, tokens or
dialplan includes to pick up a script/UI fix.

## Fresh install only

### 1. Platform and phone engine

- Use stable power and USB SSD storage: [boot notes](usb-ssd-boot.md).
- Configure voip.ms spending cap, international restriction, sub-account,
  applicable IP whitelist, DID routing and codecs before registering.
- Build Asterisk: [source instructions](build-from-source.md). Debian 13
  does not provide the package used by the old Debian 12 instructions.
- Source installs require an `asterisk` system account and writable runtime,
  state/spool/log directories. Verify the service actually runs as that user
  before continuing. Do not run `make samples` on an existing installation.
- Retain the default Asterisk sound set, including `vm-goodbye` and beep.
- Keep SIP/UI off the public internet; use WireGuard for remote access.
  Do not change a working NAT posture or add SIP port-forwards to fix Alexa.

### 2. Dependencies and files

From the repo root, **on a new installation only**, after Asterisk and its
user exist:

    sudo apt update
    sudo apt install curl jq espeak-ng sox
    sudo install -d -m 755 /etc/orata
    sudo install -d -m 755 -o asterisk -g asterisk /var/log/orata
    sudo install -d -m 700 -o asterisk -g asterisk /var/lib/orata

    sudo install -m 640 -o asterisk -g asterisk asterisk/pjsip.conf /etc/asterisk/
    sudo install -m 640 -o asterisk -g asterisk asterisk/extensions.conf /etc/asterisk/
    sudo install -m 640 -o asterisk -g asterisk asterisk/features.conf /etc/asterisk/
    sudo install -m 600 -o asterisk -g asterisk etc/announce.conf /etc/orata/

    sudo install -m 755 bin/orata-cnam.sh bin/orata-clip.sh \
      bin/orata-announce.sh bin/orata-alexa-sensor.sh /usr/local/bin/

This list is the core install only. The experimental Echo callback bridge
(`orata-alexa-bridge.py`, `orata-audio-worker.py`, `orata-alexa-prompt.sh`,
`asterisk/alexa-callback.conf`, `etc/alexa-bridge.conf`, the tmpfiles and
`orata-audio` unit) is deliberately NOT installed here. It is optional,
disabled by default and has its own procedure with carrier prerequisites:
[alexa-callback.md](alexa-callback.md). Do not install it during a fresh
build; finish acceptance first.

Giving announce.conf mode 600 without changing its owner makes it unreadable
to Asterisk; the ownership above is intentional. Keep all credentials out of
the repo. Use `sudoedit` on live files.

### 3. Trunk, mailbox and prompts

Edit `/etc/asterisk/pjsip.conf`: select the POP, fill real sub-account and
endpoint credentials, and review network addresses. Route the DID to that
sub-account in the portal. Match allowed codecs to the config.

In `/etc/asterisk/voicemail.conf`, configure mailbox `100` under `[default]`
with a new private PIN; the dialplan sends unanswered calls there. Do not
reuse the historical test PIN.

For this source build, generate fallback prompts under `/var/lib/asterisk`,
not `/usr/share/asterisk`:

    sudo mkdir -p /var/lib/asterisk/sounds/en/custom
    for p in \
      "press-one:Press 1 to continue." \
      "rec-start:Speak after the beep, then press hash." \
      "rec-menu:Press 1 to replay, 2 to save, 3 to re-record, or star to cancel." \
      "rec-saved:Saved."
    do
      n=${p%%:*}; t=${p#*:}
      espeak-ng -w /tmp/"$n".wav "$t"
      sudo sox /tmp/"$n".wav -r 8000 -c 1 -t gsm \
        /var/lib/asterisk/sounds/en/custom/"$n".gsm
    done

Confirm sound paths with `core show settings` if your build differs.
UI prompt overrides are optional; missing built-ins are not a safe fallback.

    sudo asterisk -rx 'core reload'
    sudo asterisk -rx 'pjsip show registrations'

Confirm Registered, connect one softphone, and make inbound/outbound calls
with two-way audio before debugging announcements.

### 4. Optional Web UI

Narrowest bind, from the repo root:

    sudo ./bin/orata-web-install.sh --bind 127.0.0.1 --port 8088

Use an SSH tunnel from the desktop:

    ssh -L 8088:127.0.0.1:8088 USER@PI

Alternatively bind an explicit trusted LAN/WireGuard IP and give it a stable
address. With no arguments the installer auto-detects a LAN IP, **not**
loopback. There is no TLS; no public exposure.

The installer preserves the token but re-applies bind/port. Pass those
explicitly when rerunning. Protect the printed token URL like a password.
Full detail: [web-ui.md](web-ui.md).

### 5. Alexa

Follow [alexa-arc.md](alexa-arc.md), including the Windows source workaround.
Use `speak`, exported token/region settings in the live file, and private
persistent cookie storage. Test one Echo first. Do not build sensor/Lambda
infrastructure unless you deliberately choose that alternative.

Configure/subscribe to ntfy if you want independent phone push. It is not
enabled or proven merely because the script supports it.

Finish with the [acceptance checklist](next-steps.md).

## Updating the working Pi

### Preflight

- Record the deployed revision and local changes; back up live configs,
  astdb and assigned audio before replacing anything. Store secrets privately.
- Review the diff from the revision actually deployed, not just the last
  Git commit. Earlier source edits were tested but deployment was not audited.
- Check there are no active calls before dialplan/service maintenance.
- Never run `make samples`, copy template configs over live ones, rotate
  credentials casually, or modify known-good NAT as part of a UI update.

### Script / UI-only update

From the repo root (assumes the UI is already installed):

    sudo install -m 755 bin/orata_web.py bin/orata-cnam.sh bin/orata-clip.sh \
      bin/orata-announce.sh bin/orata-alexa-sensor.sh /usr/local/bin/
    sudo systemctl restart orata-web

This preserves config files and UI bind/token. It is not an Asterisk restart.
Announcement settings are read on every invocation; no reload needed.
Restart the UI after changing audio directory paths.

To update the web unit/packages too, rerun the web installer with the intended
bind/port. It does not deploy other scripts or any Asterisk config.

### Dialplan / features update

Merge reviewed recorder/owner changes from `asterisk/extensions.conf` into
the live file, preserving local globals, ring group, includes and credentials.
`CLIPDIR` must equal `ORATA_CLIP_DIR`. Merge `features.conf` only if needed.

After the reviewed merge:

    sudo asterisk -rx 'dialplan reload'
    sudo asterisk -rx 'module reload features'

Use `pjsip reload` only for intentional SIP configuration changes. Avoid a
full restart during active calls. Test *96, owner 3434 and hangup handling
after deploying the recorder fix, not just after running unit tests.

### Verify and rollback

    python3 test/regression.py
    for f in bin/orata-clip.sh bin/orata-announce.sh etc/announce.conf; do
      bash -n "$f" || exit 1
    done
    git diff --check

These do not send notifications or touch live astdb; the synthesis test
needs espeak-ng/espeak and sox. Review Web UI Diagnostics and the logs
without sharing credentials:

    sudo journalctl -u orata-web -n 50 --no-pager
    sudo asterisk -rx 'pjsip show registrations'

Use the acceptance checklist for real traffic. If an update regresses,
restore the prior binaries and only the reviewed config changes, then
reload/restart the relevant component. Retain the working token/cookie state;
a script rollback should not require a new Amazon login.