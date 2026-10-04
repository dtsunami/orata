# Alexa path A: dynamic caller speech

**Status, 2026-10-04:** the user obtained a refresh token and reported that the
initial Alexa smoke test worked. The tested device count/command was not
recorded; remaining Echo rollout and real inbound-call acceptance are open.

Current upstream supports `speak:`, not `announce:`; it needs an Alexa app
refresh token. This is unrelated to sensor-mode LWA credentials. Current
source/options were inspected; smoke-test success is user-reported, not a
claim of independently audited deployment.

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

### Windows packaged executable fails with a V8 bytecode error

`[pkg] V8 rejected the bytecode cache` is a packaging failure before login,
not an Amazon/MFA problem. Changing system Node does not repair the executable's
bundled runtime. Bypass pkg and run the source with a current Node.js LTS
installation from https://nodejs.org/. Open a new PowerShell window afterward.

    node --version
    npm.cmd --version

Download and inspect the helper source (use fresh destination names if these
already exist):

    Set-Location "$env:USERPROFILE\Downloads"
    Invoke-WebRequest -Uri "https://github.com/adn77/alexa-cookie-cli/archive/refs/heads/master.zip" -OutFile "alexa-cookie-source.zip"
    Expand-Archive -Path "alexa-cookie-source.zip" -DestinationPath "alexa-cookie-source"
    Set-Location ".\alexa-cookie-source\alexa-cookie-cli-master"
    npm.cmd install --omit=dev --ignore-scripts

For a US account:

    node .\alexa-cookie-cli.js -q -p amazon.com -a en_US -L en-US -H 127.0.0.1 -B 127.0.0.1 -P 8080

Open http://127.0.0.1:8080 on the same Windows machine and complete login/MFA.
`-q` exits after token retrieval; without it, this version intentionally
busy-waits after printing the token. Successful output also contains other
sensitive authentication data: do not share it or enable debug logging.

The packaged Windows executable failed with this bytecode error during setup.
The user subsequently obtained a refresh token and reported a successful ARC
smoke test. The source invocation is the documented workaround, not a claim
that every Windows build or account region has been tested.

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
**Save them in this file**, not just your interactive terminal: sudo's
`asterisk` environment does not inherit your terminal exports by default.
`sudo echo $TMP` expands TMP in your current shell and proves nothing about
the child shell. An empty TMP plus obsolete-login warning usually means
the live file did not supply TMP/REFRESH_TOKEN.

Only the helper's refreshToken is used here; its device private key is not
an Orata setting. Keep successful helper output private.

Keep the file readable by `asterisk`, restricted to its existing owner/group,
normally mode 600. Do not overwrite it with repo defaults or put credentials
in the installed third-party script.

## 4. Authenticate and discover

    sudo -u asterisk bash -c '
      set -e
      . /etc/orata/announce.conf
      : "${REFRESH_TOKEN:?Missing refresh token in announce.conf}"
      : "${TMP:?Missing private cookie directory in announce.conf}"
      : "${ORATA_ARC:?Missing ARC path in announce.conf}"
      export REFRESH_TOKEN AMAZON ALEXA TTS_LOCALE TMP
      umask 077
      mkdir -p "$TMP"
      chmod 700 "$TMP"
      exec "$ORATA_ARC" -login
    '

    sudo -u asterisk bash -c '
      set -e
      . /etc/orata/announce.conf
      : "${TMP:?Missing private cookie directory}"
      umask 077
      exec "$ORATA_ARC" -a
    '

`TMP` stores sensitive cookies and device caches. Keep it private and
persistent, outside shared `/tmp`. Run all ARC invocations under the same user,
config and TMP. **`-a` lists devices. `-l` logs out.** ARC does not start a proxy.

Replace `ORATA_ALEXA_DEVICES=ALL` with one exact Echo name for the initial
test, quoting names with spaces. Do Not Disturb must be off and volume audible.

## 5. Test and roll out the remaining Echos

The initial smoke test is user-confirmed. Do not redo token setup merely to
add an Echo. Power up each device, check that it is online on the same Amazon
account, list devices with sourced `-a`, and test one exact name at a time.
Check volume and Do Not Disturb. Only switch to `ALL` after testing coverage;
ALL means devices supported by upstream, not guaranteed household coverage.
Offline devices and sequential delivery may hit Orata's 20-second timeout.
The full acceptance checklist is in [next-steps.md](next-steps.md).

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