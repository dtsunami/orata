# Web UI

`bin/orata_web.py` — a FastAPI app that edits the four astdb books through a
browser, plus a read-only diagnostics page and a test harness.

Installed from Debian packages (`python3-fastapi`, `python3-uvicorn`). No pip,
no venv, no database.

**Unverified.** Never run on the Pi.

## The tension, stated up front

HANDOFF rejected FreePBX partly because it "adds a web attack surface." This
adds one too. That objection was really about three things, and it is worth
being precise about which of them this inherits:

| FreePBX objection | Does orata-web inherit it? |
|---|---|
| PHP app *owns* the configs, fights hand-editing | **No.** It shells out to `orata-cnam.sh`. The CLI stays authoritative; the UI is a second face on it. Nothing is generated, nothing is overwritten. |
| Large dependency with its own upgrade cadence | **Partly.** It was stdlib-only; it is now FastAPI + uvicorn + starlette + pydantic, from Debian packages. They track the distro's cadence rather than their own, and `apt` upgrades them with everything else — but this is a real dependency that did not exist before, and it is the honest cost of the UI. |
| Web attack surface | **Yes.** Unavoidably. |

So the third one is real and has to be managed rather than argued away. It
runs as the `asterisk` user (it needs the CLI), which means a compromise is a
compromise of call routing — and call routing is attached to a trunk that can
dial premium-rate numbers.

Mitigations, all in the shipped defaults:

- **Binds 127.0.0.1.** Reach it over an SSH tunnel, or bind to the Pi's
  WireGuard address. It **refuses to start on 0.0.0.0** without an explicit
  override flag.
- **Refuses to start with an empty token.** An unconfigured instance is not a
  wide-open instance.
- Token compared with `hmac.compare_digest`; cookie is `HttpOnly` +
  `SameSite=Strict`.
- Numbers are digits-only, 3–15. Names and endpointIds are stripped of quotes,
  backslashes, backticks, `$` and control characters, capped at 64 chars.
  Subprocess is invoked without a shell.
- systemd unit is confined: `ProtectSystem=strict`, `NoNewPrivileges`,
  `ReadWritePaths` limited to the Asterisk run dir and the log dir.

**There is no TLS and no login rate limit.** Do not put this on the internet.
The invariant from README stands: no port-forward, remote access over
WireGuard.

## Install

    sudo apt install python3-fastapi python3-uvicorn
    sudo cp bin/orata_web.py /usr/local/bin/
    sudo chmod 755 /usr/local/bin/orata_web.py

    sudo cp etc/web.conf /etc/orata/
    sudo chmod 600 /etc/orata/web.conf
    sudo chown asterisk:asterisk /etc/orata/web.conf

    # generate a token and paste it into ORATA_WEB_TOKEN
    head -c 24 /dev/urandom | base64

    sudo cp etc/orata-web.service /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo systemctl enable --now orata-web
    journalctl -u orata-web -f

From your desktop:

    ssh -L 8088:127.0.0.1:8088 pi@raspberrypi

then browse `http://127.0.0.1:8088/?token=YOUR_TOKEN`. The token goes into a
cookie; after that, plain `http://127.0.0.1:8088/`.

To reach it from a phone on WireGuard, set `ORATA_WEB_BIND` to the Pi's
WireGuard address instead.

## What it manages

Four books, all live in astdb. Edits apply to the **next call** — no reload.

| Book | Meaning |
|---|---|
| `cnam` | spoken name. Present = skips the robocall gate. |
| `allow` | whitelist. Past the gate, permanently. |
| `block` | hung up on immediately. Never rings, never announces. |
| `alexasensor` | per-number Alexa sensor endpointId (path B only) |

Plus the last 40 lines of `announce.log`, which is where `alexa FAIL` and
`sensor FAIL` show up.

## Whitelist, gate, blocklist — how they interact

The whitelist is not new; it has existed since session 1 and **self-learns**.
Order of evaluation in `[from-voipms]`:

1. `block/<num>` set? → `Hangup()`. No answer, no ring, no announce.
2. Name resolved from `cnam/` or carrier CNAM? → straight to ring.
3. `allow/<num>` set? → straight to ring.
4. Otherwise → the gate: "press 1 to continue". Pressing 1 writes
   `allow/<num>=1`, so they are never gated again.
5. `STRICT=1` in `[globals]` → skip step 4 and reject outright.

Strict mode turns the system from "filter robots" into "invitation only."
Legitimate strangers — a doctor's office, a delivery driver, a school — get
`vm-goodbye` and no way through. Leave it at `0` unless the gate is
demonstrably failing you.

Note step 1 does not `Answer()`. Answering a robocall confirms the number is
live and typically increases traffic.

## Testing

    # does it start and refuse the obvious mistakes
    sudo -u asterisk ORATA_WEB_CONF=/etc/orata/web.conf python3 /usr/local/bin/orata_web.py

Empty token should exit 1 with a message. Then, with a token set:

    curl -si http://127.0.0.1:8088/            # expect 401
    curl -si "http://127.0.0.1:8088/?token=T"  # expect 303 + Set-Cookie

Add a number in the browser, then confirm the CLI agrees — this is the check
that matters, because it proves the UI is a face on the CLI and not a second
store:

    orata-cnam.sh list
    orata-cnam.sh block-list

Then the dialplan:

    sudo asterisk -rx "core reload"
    sudo asterisk -rx "dialplan show from-voipms"

Look for the `blocked` label and the `STRICT` GotoIf. Block your own mobile
and call in; you should get a decline, and `BLOCKED <num>` in the Asterisk
console.

## Known gaps

- **No TLS.** SSH tunnel or WireGuard only.
- **No rate limit on the token.** A LAN attacker can brute force it; 24 random
  bytes makes that impractical, but it is not defence in depth.
- **No CSRF token.** `SameSite=Strict` on the cookie is the only protection.
  Adequate for a single-user LAN tool, not for anything wider.
- **No logrotate for `announce.log`.** Predates this change and still unfixed;
  the UI only reads the last 8 KB, so it will not choke, but the file grows
  forever.
- **Number format is not normalised.** The UI strips non-digits, so
  `(555) 123-4567` becomes `5551234567` — which will **not** match what
  voip.ms presents (`15551234567`). Enter numbers exactly as they appear in
  `announce.log` or the Asterisk console.