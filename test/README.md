# test/ — verification rigs

Two things live here now:

- **`selftest.conf`** — a dialplan context deployed *on the Pi* that drives
  the real inbound paths with a spoofed caller ID. This is what actually
  verified the call logic in session 3. See "On-Pi selftest" below.
- **`Dockerfile` + `run-tests.sh`** — the original dev-box container rig.
  **Now stale: it is `FROM debian:12`, and the Pi runs Debian 13 with
  Asterisk built from source.** The drift this file warned about has
  happened. Its results no longer transfer; two of its techniques were also
  found to be wrong (see below).

**Neither is a deployment target.** HANDOFF rejects Docker for running
orata; that decision stands.

## On-Pi selftest

    sudo cp test/selftest.conf /etc/asterisk/orata-selftest.conf
    sudo chown asterisk:asterisk /etc/asterisk/orata-selftest.conf
    printf '\n#include "orata-selftest.conf"\n' | sudo tee -a /etc/asterisk/extensions.conf
    sudo asterisk -rx 'dialplan reload'

Note the leading `\n` in that `printf` — it matters. See the trailing-newline
note in HANDOFF.

The verbose log must be enabled or you will see nothing — `core set verbose`
affects only the **console**, not logfiles. In `/etc/asterisk/logger.conf`:

    orata.log => notice,warning,error,verbose(5)

Then drive each path and watch `/var/log/asterisk/orata.log`:

    sudo asterisk -rx 'channel originate Local/7101@orata-selftest application Wait 9'  # known
    sudo asterisk -rx 'channel originate Local/7102@orata-selftest application Wait 9'  # blocked
    sudo asterisk -rx 'channel originate Local/7103@orata-selftest application Wait 9'  # unknown
    sudo asterisk -rx 'channel originate Local/7001@orata-selftest application Wait 2'  # DB() reads

Remove the `#include` line when done, delete
`/etc/asterisk/orata-selftest.conf`, and purge the test numbers from astdb.

### Two testing techniques that DO NOT work

Both were used by `run-tests.sh`, so treat its green runs with suspicion:

1. **`dialplan eval function ${DB(...)}`** returns `Failure (-1)` outside a
   channel on Asterisk 22 — even for keys `database get` reads back fine. It
   cannot verify astdb/dialplan key agreement. Use a real channel.
2. **`channel originate Local/NUM@from-voipms`** sets the *extension* to NUM
   but leaves `CALLERID(num)` **empty**, so every
   `DB(block/${CALLERID(num)})` lookup becomes `DB(block/)` and the whole
   gate falls through. Every caller then looks identical. You must
   `Set(CALLERID(num)=...)` first and `Goto()` in — which is what the 71xx
   extensions in `selftest.conf` do.

## The original container rig

**This is not a deployment target and never will be.** A throwaway container
on a dev box for checking that the configs parse and the scripts plumb
together before anything reaches the Pi.

No compose file, deliberately. One Dockerfile, one script, `--rm`.

## Run it

From the repo root, cmd.exe:

    docker build -t orata-test -f test/Dockerfile .
    docker run --rm -v "%cd%:/repo:ro" orata-test

PowerShell: `-v "${PWD}:/repo:ro"`. Bash: `-v "$PWD:/repo:ro"`.

The repo is mounted read-only; everything happens to copies inside the
container. Rebuild after editing `run-tests.sh` (it is COPYed, not mounted —
a CRLF-laden script mounted from Windows cannot bootstrap itself).

## What a green run proves

Debian 12 + the distro Asterisk 20 package, same as the Pi. So these results
transfer:

- No CRLF damage; `bash -n` and `py_compile` clean
- `extensions.conf` and `pjsip.conf` **load** — context exists, all four
  endpoints parse, no silent priority drops
- The astdb key names `orata-cnam.sh` writes are the ones `extensions.conf`
  reads. Checked in both directions with `dialplan eval function ${DB(...)}`
  for `cnam/`, `allow/`, `block/`, `alexasensor/`. This is the seam most
  likely to be quietly wrong.
- `orata-announce.sh` exits 0 and logs a skip in every degraded mode — the
  invariant that a broken notifier must never affect call handling
- `orata_web.py` fails closed on empty token and on `0.0.0.0`; 401s
  unauthenticated GET **and** POST; its writes travel
  web → `orata-cnam.sh` → astdb → readable by the dialplan
- `db_show()`'s regex actually matches real `database show` output
- The README's espeak+sox recipe yields a file Asterisk can convert

## What it cannot prove

Networking results here are meaningless. On Windows, Docker Desktop runs the
container inside a WSL2/Hyper-V VM, so even `--network host` gets you the
Linux VM's stack, not Windows'. SIP signalling and RTP 10000-20000/udp do not
reach your LAN or a softphone on the desktop.

Untested, and only testable on the Pi:

- Registration to voip.ms (the POP and credentials are stubs anyway)
- RTP, audio, codec negotiation
- `Read(digit,custom/press-one,1,,1,7)` — argument order and the 7s timeout
- Actual inbound routing: does a blocked caller really get hung up on, does
  a known caller really skip the gate
- Anything Alexa: both paths need real credentials

**A green run means the config and plumbing layer is sound. It does not mean
the phone works.** Rungs 2–5 of the HANDOFF plan are unaffected.

## Is this the tech debt we said no to?

The objection to Docker was that it buys nothing for a *running* PBX — SIP
forces host networking, so the isolation is illusory while the indirection is
real. That reasoning is about the deployment, and it is untouched.

A build-and-discard test container has a different cost profile: it is
`--rm`, nothing persists, the Pi never learns it exists, and deleting `test/`
removes it without trace. The one real risk is drift — if the container's
Asterisk stops matching the Pi's, results quietly stop transferring. Both
track Debian 12 stable, so that should not happen before the next Debian
release.