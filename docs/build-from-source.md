# Building Asterisk from source

## Why this file exists

`apt install asterisk` **fails on Debian 13 (trixie)**:

    $ apt-cache policy asterisk
    asterisk:
      Installed: (none)
      Candidate: (none)
      Version table:
                        <- empty

`apt-cache madison asterisk` returns nothing. `apt-cache showpkg asterisk`
lists only *reverse* dependencies (`asterisk-core-sounds-en`, `libopenr2-3`,
the `asterisk-prompt-*` packages) pointing at a package with no versions —
the signature of a package dropped from the release. The ~30 surviving
`asterisk-*` hits are all sound/prompt data, which are separate source
packages and still present.

Bookworm (Debian 12) was the last stable release to carry it. Raspberry Pi OS
images based on trixie inherit the gap; the `archive.raspberrypi.com` repo
does not carry it either.

The original README claimed this dependency breaks "~never (LTS, config
stable)". That was an unhedged bet on Debian continuing to ship the package,
and it lost.

## Is this the tech debt we said no to?

Asterisk is not a framework layered over the phone — it *is* the phone.
Something has to register SIP, negotiate RTP and run a dialplan. The rejected
things (FreePBX, Home Assistant, Docker, k8s) all sat *above* Asterisk and
added surface without adding capability. `apt install asterisk` was the
bare-metal choice.

Be honest about what building from source costs, though: **it increases the
maintenance surface.** You now own the build toolchain, a bundled pjproject,
and CVE patching by hand — Asterisk ships remotely-triggerable SIP advisories
a few times a year, and no unattended-upgrades path will apply them.

What it buys is predictability. An invisible dependency on distro packaging
becomes an explicit pinned version with a scheduled rebuild. Surprise cost
becomes calendar cost. That is the only defensible reading of this choice.

### The alternative, if you'd rather not own a build

Reflash Pi OS **bookworm** and `apt install asterisk` as originally written.
Cheaper to run, but bookworm's support window is finite, so you inherit a
migration deadline instead of a rebuild chore. Either way the "breaks ~never"
line was wrong.

## Version choice

Pinned **22.11.0**. Of the four upstream branches offering current tarballs
(20, 21, 22, 23), only **20 and 22 are LTS** — 21 and 23 are standard
releases with short support windows. 22 is the LTS with the longest remaining
life. The repo's `pjsip.conf` and `extensions.conf` are forward-compatible
from 20: same PJSIP stack, same dialplan applications.

## Build deps

    sudo apt install -y --no-install-recommends \
      build-essential pkg-config wget xz-utils \
      libedit-dev libjansson-dev libsqlite3-dev libxml2-dev \
      libssl-dev uuid-dev libncurses-dev

These are build-time only. Recorded at `/tmp/orata-builddeps.txt` during the
first build so they can be removed afterwards if you want the toolchain off
the box.

## Fetch and verify

    V=22.11.0
    mkdir -p ~/src && cd ~/src
    curl -fsSL -O https://downloads.asterisk.org/pub/telephony/asterisk/asterisk-$V.tar.gz
    curl -fsSL -O https://downloads.asterisk.org/pub/telephony/asterisk/asterisk-$V.tar.gz.asc

**Signature verification is unresolved and you should close this gap.**
Upstream publishes no `.sha256` next to the tarball, the keyserver was
unreachable from this Pi, and both candidate HTTPS key URLs 404'd. So the
tarball's provenance currently rests on TLS to `downloads.asterisk.org`
alone.

The detached signature claims signer fingerprint:

    F2FC93DB7587BD1FB49E045A5D984BE337191CE7   (key ID 5D984BE337191CE7)

SHA256 of the 22.11.0 tarball as fetched here, for cross-checking against
another machine:

    3bd5ee040509a3d3cd9b1ba9520c18e6ec0a7e7981ca68c457dcd36ba3c54d94

Verify that fingerprint out-of-band before this box handles real calls:

    gpg --recv-keys 5D984BE337191CE7        # from a host with keyserver access
    gpg --verify asterisk-$V.tar.gz.asc asterisk-$V.tar.gz

## Configure

    tar xzf asterisk-$V.tar.gz && cd asterisk-$V
    ./configure \
      --with-pjproject-bundled \
      --with-jansson-bundled \
      --with-libedit \
      --disable-xmldoc

`--with-pjproject-bundled` is deliberate: PJSIP versions are tightly coupled
to Asterisk's PJSIP stack, and bundling removes a system library whose
upgrades could silently break SIP. It also means **a pjproject CVE is your
rebuild**, not apt's.

`--disable-xmldoc` drops CLI help text; it roughly halves build time on a
Pi 4 and nothing in orata reads it.

Expect the banner `Package configured for: ... Host CPU : aarch64` and
`checking for mandatory modules: JANSSON PJPROJECT LIBEDIT... ok`.

## Module selection

    make menuselect.makeopts

Defaults are already close to what orata wants — no ODBC/PostgreSQL/MySQL,
no DAHDI, no test modules, `BUILD_NATIVE` off (keeps the binary portable
across Pi models).

Reading `menuselect.makeopts` has one trap worth knowing: the
`MENUSELECT_<CATEGORY>` lines are **exclusion** lists, but
`MENUSELECT_BUILD_DEPS` is *not* — it lists modules that other modules
depend on. `res_pjsip` and `res_pjsip_outbound_registration` appear there and
are **built**. Grep for a module across all `MENUSELECT_*` lines and you will
scare yourself. Check the module's own category instead:

    grep '^MENUSELECT_RES' menuselect.makeopts | tr ' ' '\n' | grep -x res_pjsip

Modules orata actually needs: `chan_pjsip`, `res_pjsip`,
`res_pjsip_outbound_registration`, `app_dial`, `app_voicemail`, `app_read`,
`app_system`, `app_playback`, `func_db`, `format_gsm`, `codec_gsm`,
`res_rtp_asterisk`.

## Build

    make -j3

`-j3` not `-j4`: leaves a core free and keeps the SoC under the 80°C throttle
point. Expect 30–60 min on a Pi 4. Run it detached (`nohup ... &`) and tail
the log — an SSH drop should not kill a 40-minute build.

Watch thermals and power:

    vcgencmd measure_temp
    vcgencmd get_throttled     # 0x0 is clean

`get_throttled` bits are sticky since boot: `0x10000` = under-voltage has
occurred, `0x40000` = throttling has occurred. Under-voltage on a Pi 4 is a
leading cause of SD/SSD corruption — fix the PSU (5V 3A) before trusting this
box with 24/7 call handling.

## Install

    sudo make install
    sudo make samples          # only on a first install -- see warning below
    sudo make config           # installs the systemd unit
    sudo ldconfig

**`make samples` overwrites `/etc/asterisk/*`.** Never run it on a box that
already has orata's configs deployed — it will clobber `pjsip.conf` and
`extensions.conf`. On a first install it is what gives you `voicemail.conf`
(closing the "no mailbox 100" gap the test rig flagged) and the other stock
configs.

Order matters: `make samples` first, *then* copy orata's configs over the
samples.

## What source install changes vs the distro package

| | Distro package | Source |
|---|---|---|
| sounds | `/usr/share/asterisk/sounds` | `/var/lib/asterisk/sounds` |
| binary | `/usr/sbin/asterisk` | `/usr/sbin/asterisk` |
| `asterisk` user | created by postinst | **you create it** |
| systemd unit | shipped | `make config` |
| `voicemail.conf` | shipped (mailboxes commented) | `make samples` |
| CVE patching | `apt upgrade` | **manual rebuild** |

The sounds path change matters: older install instructions targeted
`/usr/share/asterisk/sounds/en/custom`, which is **wrong for this source
install**. The current [install guide](install.md) and README use:

    /var/lib/asterisk/sounds/en/custom/press-one.gsm

Confirm the real path on your box before generating the prompt:

    sudo asterisk -rx 'core show settings' | grep -i sounds

## Maintenance obligation

This is the calendar cost you accepted:

1. Subscribe to `asterisk-security` announcements (`lists.digium.com`).
2. On an advisory affecting 22.x, rebuild: re-fetch tarball, same configure
   line, `make -j3 && sudo make install`, then `sudo systemctl restart
   asterisk`. Do **not** run `make samples` again.
3. Keep `~/src/asterisk-22.11.0` around — an unchanged configure line makes
   the rebuild mechanical.
4. Re-verify the module list after a minor version bump; upstream does
   occasionally move modules between categories.