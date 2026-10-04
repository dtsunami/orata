# Alexa path A: dynamic caller speech

Checked against upstream source on 2026-10-04, not a completed Amazon-account
deployment. Current upstream supports `speak:`, not `announce:`; it needs an
Alexa app refresh token. This is unrelated to sensor-mode LWA credentials.

## 1. Install

On the Pi:

    sudo apt install curl jq
    sudo curl -fsSL -o /usr/local/bin/alexa_remote_control.sh \
      https://raw.githubusercontent.com/thorsten-gehrig/alexa-remote-control/master/alexa_remote_control.sh
    sudo chmod 755 /usr/local/bin/alexa_remote_control.sh

Review the third-party source before granting it Amazon account access.
For reproducible deployment, use a reviewed commit URL rather than moving master.

## 2. Obtain the token

On your desktop, download the appropriate executable from
https://github.com/adn77/alexa-cookie-cli/releases/latest and review the
helper project. This keeps browser login off the Pi; Pi/ARM64 execution
is not required or verified here.

For a **US Amazon account**, run the helper (replace its executable name):

    ./<helper-binary> -p amazon.com -a en_US -L en-US \
      -H 127.0.0.1 -B 127.0.0.1 -P 8080

On Windows run the corresponding `.exe`. Open http://127.0.0.1:8080 on the
same desktop, authenticate to the account owning your Echo devices and
complete MFA. The helper returns `refreshToken`, usually beginning `Atnr|`.
Stop the helper after login.

Its default proxy bind is all interfaces: the explicit localhost bind above
is intentional. Do not expose the login proxy or disable MFA to make this work.
The token permits access to your Alexa account. Never paste it into chat,
logs, a web field, shell command arguments or the repo.

## 3. Configure the live file

    sudoedit /etc/orata/announce.conf

Preserve existing settings. US account example:

    ORATA_ALEXA_MODE=arc
    ORATA_ARC=/usr/local/bin/alexa_remote_control.sh
    ORATA_ALEXA_CMD=speak
    ORATA_ALEXA_DEVICES=ALL

    export REFRESH_TOKEN='YOUR_REFRESH_TOKEN'
    export AMAZON=amazon.com
    export ALEXA=pitangui.amazon.com
    export TTS_LOCALE=en-US
    export TMP=/var/lib/orata/alexa

Use the domain, Alexa API host and locale matching **your account**; the
example above is US-specific. Upstream defaults to Germany. Consult the
upstream docs for other regions; do not assume a domain alone is sufficient.

The `export` declarations are required because ARC is a child process.
Keep the file readable by `asterisk`, restricted to its existing owner/group,
normally mode 600. Do not overwrite it with repo defaults or put credentials
in the installed third-party script.

## 4. Authenticate and discover

    sudo -u asterisk bash -c '
      . /etc/orata/announce.conf
      umask 077
      mkdir -p "$TMP"
      chmod 700 "$TMP"
      exec "$ORATA_ARC" -login
    '

    sudo -u asterisk bash -c '
      . /etc/orata/announce.conf
      umask 077
      exec "$ORATA_ARC" -a
    '

`TMP` stores sensitive cookies and device caches. Keep it private and
persistent, outside shared `/tmp`. Run all ARC invocations under the same user,
config and TMP. **`-a` lists devices. `-l` logs out.** ARC does not start a proxy.

Replace `ORATA_ALEXA_DEVICES=ALL` with one exact Echo name for the initial
test, quoting names with spaces. Do Not Disturb must be off and volume audible.

## 5. Test

The following sends real speech to selected devices and ntfy if configured:

    sudo -u asterisk /usr/local/bin/orata-announce.sh 15551234567 "Test Caller"

Check Web UI -> Log (or /var/log/orata/announce.log) and listen:

- `alexa SKIP`: script missing or not executable.
- `alexa FAIL`: check token, account region, jq and command; rerun sourced
  `-login` with the same private TMP to investigate. Obtain a new token if revoked.
- `alexa OK`: upstream returned success. Its HTTP error handling is incomplete,
  so this is not proof Amazon accepted the request or that an Echo spoke.

For raw diagnostic output, invoke ARC directly after sourcing the config:

    sudo -u asterisk bash -c '
      . /etc/orata/announce.conf
      umask 077
      exec "$ORATA_ARC" -d "$ORATA_ALEXA_DEVICES" -e "speak:Test announcement"
    '

This also sends real speech. Do not share raw cookies, tokens or debug traces.

No Asterisk reload is needed. The existing inbound dialplan runs the notifier
when a caller reaches the ring group; blocked/rejected callers never announce.
`*99` or the Harness announcement button can also test delivery.

Handset-recorded clips are a different channel. They broadcast to PJSIP
handsets; this Alexa path accepts text, not your recorded WAV or live voice.