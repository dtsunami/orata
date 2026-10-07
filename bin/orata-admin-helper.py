#!/usr/bin/env python3
"""Privileged config helper for the Orata appliance UI. Root; stdlib only.

The web UI runs as `asterisk` with NoNewPrivileges and cannot write
/etc/asterisk or talk to BlueZ. Rather than widening the web service, it
sends enumerated requests here over a unix socket.

Security model, deliberately narrow:
  * Peer UID must be asterisk or root (SO_PEERCRED). No network socket.
  * Operations are ENUMERATED. There is no "write this file" verb and no
    free-text config blob. Every settable field is named in SCHEMA with its
    own validator.
  * Every value is rejected if it contains a newline, carriage return or
    NUL. This is the single most important check: a newline in an INI value
    injects arbitrary configuration lines.
  * Writes are temp-file + fsync + atomic rename, with a numbered backup
    kept first. A hand edit is re-read, never cached, so the UI and
    sudoedit see the same file -- but concurrent writes are last-wins.
  * Subprocesses use fixed argv, never a shell, always with a timeout.
"""
import grp
import json
import os
import pwd
import re
import shutil
import signal
import socket
import stat
import struct
import subprocess
import sys
import threading
import time
import wave

SOCKET_PATH = os.environ.get("ORATA_ADMIN_SOCK", "/run/orata-admin/admin.sock")
PJSIP = os.environ.get("ORATA_PJSIP_CONF", "/etc/asterisk/pjsip.conf")
VOICEMAIL = os.environ.get("ORATA_VOICEMAIL_CONF", "/etc/asterisk/voicemail.conf")
BRIDGE_CONF = os.environ.get("ORATA_BRIDGE_CONF", "/etc/orata/alexa-bridge.conf")
BACKUP_DIR = os.environ.get("ORATA_ADMIN_BACKUPS", "/var/lib/orata/config-backups")
BACKUP_KEEP = 20
MAX_REQUEST = 64 * 1024

LOCK = threading.Lock()

# Values may never contain these. Newline injection into an INI file is the
# whole threat; the rest are defence in depth.
FORBIDDEN = re.compile(r"[\r\n\x00]")


def _plain(maxlen, pattern=None):
    rx = re.compile(pattern) if pattern else None

    def check(v):
        if not isinstance(v, str):
            raise ValueError("expected a string")
        if FORBIDDEN.search(v):
            raise ValueError("line breaks and NUL are not allowed in a value")
        if len(v) > maxlen:
            raise ValueError("longer than %d characters" % maxlen)
        if rx and not rx.fullmatch(v):
            raise ValueError("not in the expected format")
        return v
    return check


# --- the logical Orata fields -----------------------------------------
# Each entry is (targets, validator, label, help), where targets is a LIST
# of (file, section, option) that are all updated together.
#
# The list matters. The voip.ms POP hostname appears in FOUR places in a
# working pjsip.conf -- server_uri, client_uri, the identify match and the
# AOR contact. Writing only server_uri leaves registration pointed at the
# new POP while inbound identification still matches the old one. That was
# a real defect found by testing against the live config on this Pi.
#
# Section names are the UNDERSCORE form used by the shipped
# asterisk/pjsip.conf (voipms_auth, NOT voipms-auth). A wrong name reads
# back empty and the write is refused, so verify against the live file.
SCHEMA = {
    "trunk.pop": (
        [(PJSIP, "voipms_reg", "server_uri"),
         (PJSIP, "voipms_reg", "client_uri"),
         (PJSIP, "voipms_identify", "match"),
         (PJSIP, "voipms_aor", "contact")],
        _plain(60, r"[A-Za-z0-9][A-Za-z0-9.\-]{2,58}"),
        "voip.ms POP hostname",
        "Just the hostname of the voip.ms server nearest you, like "
        "sanjose2.voip.ms -- no 'sip:' prefix, no port, no username. This "
        "one value is written to four places in pjsip.conf that have to "
        "agree: the registration URI, your account URI, the inbound identify "
        "match and the contact address. Saving it drops and re-establishes "
        "registration, so avoid doing it during a call."),
    "trunk.username": (
        [(PJSIP, "voipms_auth", "username")],
        _plain(64, r"[A-Za-z0-9_.\-]+"),
        "Sub-account username",
        "Your voip.ms SUB-ACCOUNT username, which looks like 574149_orata -- "
        "not the email you log into the portal with. A sub-account means "
        "this Pi's credentials can be revoked on their own. Changing it also "
        "requires re-saving the POP field, because your account URI is built "
        "from both."),
    "trunk.password": (
        [(PJSIP, "voipms_auth", "password")],
        _plain(128, r"\S+"),
        "Sub-account password",
        "The SIP password for that sub-account, set in the voip.ms portal -- "
        "not your portal login password. SIP requires it in plain text, so "
        "it lives in pjsip.conf (mode 0640, readable only by Asterisk). "
        "Treat this Pi as holding a live, billable credential."),
    "voicemail.pin": (
        [(VOICEMAIL, "default", "100")],
        None,  # handled specially; see op_set_pin
        "Voicemail PIN",
        "The PIN for mailbox 100, 4 to 10 digits. Anyone who can call your "
        "number can try it, so avoid 1234, 0000 and your street number."),
}

# NOTE: there is deliberately no "your phone number" field. This dialplan
# routes inbound calls with `exten => _X.` in [from-voipms]: it accepts
# whatever the carrier delivers and never compares against a configured
# DID. A box here would be a lie, because nothing would read it. The one
# number that IS configured, ALEXABRIDGE_CALLBACK_DID, belongs to the
# optional callback bridge and lives in extensions.conf.

PIN_RE = re.compile(r"[0-9]{4,10}")
WEAK_PINS = {"1234", "0000", "1111", "123456", "4321", "9999"}


# =====================================================================
# INI editing -- line-oriented, preserves comments and unknown options
# =====================================================================

def read_option(path, section, option):
    """Return the current value, or '' if unset. Never raises on absence."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError:
        return ""
    cur = None
    for line in lines:
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            cur = s[1:-1].strip()
            continue
        if cur != section or "=" not in s or s.startswith(";"):
            continue
        k, _, v = s.partition("=")
        if k.strip() == option:
            return v.strip()
    return ""


def section_exists(path, section):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                s = line.strip()
                if s.startswith("[") and s.endswith("]") and s[1:-1].strip() == section:
                    return True
    except OSError:
        return False
    return False


def backup(path):
    os.makedirs(BACKUP_DIR, mode=0o700, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = os.path.join(BACKUP_DIR, "%s.%s" % (os.path.basename(path), stamp))
    try:
        shutil.copy2(path, dest)
        os.chmod(dest, 0o600)
    except OSError:
        return ""
    keep = sorted(f for f in os.listdir(BACKUP_DIR)
                  if f.startswith(os.path.basename(path) + "."))
    for old in keep[:-BACKUP_KEEP]:
        try:
            os.unlink(os.path.join(BACKUP_DIR, old))
        except OSError:
            pass
    return dest


def write_option(path, section, option, value):
    """Set option within section, atomically. Preserves everything else.

    Refuses to create the file or the section: an appliance edits a shipped
    template, and silently inventing a section is how you end up with a
    config that parses but does not route.
    """
    with open(path, "r", encoding="utf-8") as fh:
        lines = fh.readlines()

    out, cur, done, in_target = [], None, False, False
    for line in lines:
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            # Leaving the target section without having found the option:
            # append it as the last line of that section.
            if in_target and not done:
                out.append("%s = %s\n" % (option, value))
                done = True
            cur = s[1:-1].strip()
            in_target = cur == section
            out.append(line)
            continue
        if in_target and not done and "=" in s and not s.startswith(";"):
            k, _, _v = s.partition("=")
            if k.strip() == option:
                indent = line[:len(line) - len(line.lstrip())]
                out.append("%s%s = %s\n" % (indent, option, value))
                done = True
                continue
        out.append(line)

    if in_target and not done:
        out.append("%s = %s\n" % (option, value))
        done = True
    if not done:
        raise ValueError("section [%s] not found in %s" % (section, os.path.basename(path)))

    saved = backup(path)
    st = os.stat(path)
    tmp = path + ".orata-new"
    # Any failure here must leave the original untouched AND not litter a
    # half-written temp file next to a live Asterisk config.
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.writelines(out)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, stat.S_IMODE(st.st_mode))
        try:
            os.chown(tmp, st.st_uid, st.st_gid)
        except PermissionError:
            # Only possible when not root (tests); the rename still works
            # and ownership is inherited from the caller.
            pass
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    dirfd = os.open(os.path.dirname(path) or "/", os.O_RDONLY)
    try:
        os.fsync(dirfd)
    finally:
        os.close(dirfd)
    return saved


# =====================================================================
# subprocess helpers -- fixed argv, no shell, always bounded
# =====================================================================

def run(argv, timeout=10, stdin_text=None):
    try:
        p = subprocess.run(argv, capture_output=True, text=True,
                           timeout=timeout, input=stdin_text)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "timed out after %ss" % timeout
    except OSError as exc:
        return 127, str(exc)


def asterisk(cmd_argv, timeout=10):
    return run(["/usr/sbin/asterisk", "-rx", " ".join(cmd_argv)], timeout)


# =====================================================================
# Bluetooth -- bluetoothctl with fixed argv
# =====================================================================

MAC_RE = re.compile(r"(?:[0-9A-F]{2}:){5}[0-9A-F]{2}")


def bt_mac(value):
    if not isinstance(value, str):
        raise ValueError("expected a string")
    mac = value.strip().upper()
    if not MAC_RE.fullmatch(mac):
        raise ValueError("not a MAC address")
    return mac


def bt_scan(seconds=12):
    seconds = max(3, min(int(seconds), 30))
    run(["/usr/bin/bluetoothctl", "power", "on"], 6)
    # --timeout makes bluetoothctl exit by itself; no stray background scan.
    run(["/usr/bin/bluetoothctl", "--timeout", str(seconds), "scan", "on"],
        seconds + 8)
    rc, out = run(["/usr/bin/bluetoothctl", "devices"], 8)
    found = []
    for line in out.splitlines():
        parts = line.strip().split(" ", 2)
        if len(parts) >= 3 and parts[0] == "Device" and MAC_RE.fullmatch(parts[1]):
            found.append({"mac": parts[1], "name": parts[2][:64]})
    return {"ok": rc == 0 or bool(found), "devices": found}


def bt_info(mac):
    rc, out = run(["/usr/bin/bluetoothctl", "info", mac], 8)
    flags = {}
    for key in ("Paired", "Trusted", "Connected"):
        m = re.search(r"^\s*%s:\s*(yes|no)" % key, out, re.M)
        flags[key.lower()] = bool(m and m.group(1) == "yes")
    name = re.search(r"^\s*Name:\s*(.+)$", out, re.M)
    flags["name"] = name.group(1).strip()[:64] if name else ""
    flags["mac"] = mac
    return flags


def bt_pair(mac):
    """Pair, trust and connect. Trust is what survives a reboot."""
    steps = []
    for verb, tmo in (("pair", 25), ("trust", 8), ("connect", 20)):
        rc, out = run(["/usr/bin/bluetoothctl", verb, mac], tmo)
        tail = out.strip().splitlines()[-1][:160] if out.strip() else ""
        steps.append({"step": verb, "rc": rc, "detail": tail})
        if verb == "pair" and rc != 0 and "AlreadyExists" not in out:
            break
    info = bt_info(mac)
    return {"ok": info.get("connected", False), "steps": steps, "info": info}


# PipeWire runs inside the desktop user's session bus, not root's. Running
# pw-dump as root returns nothing at all, so the helper must drop to that
# user and point at their runtime directory. Host-specific, like the
# orata-audio unit: change both if the owner's UID is not 1000.
AUDIO_USER = os.environ.get("ORATA_AUDIO_USER", "dfstar")


def run_as_audio_user(argv, timeout=10):
    """Run argv as the desktop user, dropping privileges in the child.

    Deliberately NOT sudo/runuser: the unit sets NoNewPrivileges=yes, which
    blocks setuid binaries. Root dropping to an unprivileged uid with
    setresuid needs no setuid bit and is unaffected.
    """
    try:
        pw = pwd.getpwnam(AUDIO_USER)
    except KeyError:
        return 127, "no such user: %s" % AUDIO_USER

    def drop():
        os.setgroups([])
        os.setresgid(pw.pw_gid, pw.pw_gid, pw.pw_gid)
        os.setresuid(pw.pw_uid, pw.pw_uid, pw.pw_uid)

    env = {"XDG_RUNTIME_DIR": "/run/user/%d" % pw.pw_uid,
           "HOME": pw.pw_dir, "PATH": "/usr/bin:/bin", "USER": AUDIO_USER}
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                           user=pw.pw_uid, group=pw.pw_gid, extra_groups=[],
                           env=env)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "timed out after %ss" % timeout
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, "%r" % (exc,)


def bt_sinks(diag=None):
    """PipeWire sink names, so the UI can show the exact string to configure.

    Pass a dict as `diag` to capture the raw pw-dump result; an empty sink
    list is otherwise indistinguishable from "PipeWire not reachable".
    """
    rc, out = run_as_audio_user(["/usr/bin/pw-dump"], 8)
    if diag is not None:
        diag.update({"rc": rc, "len": len(out), "head": out[:160]})
    if rc:
        return []
    try:
        data = json.loads(out)
    except ValueError:
        return []
    sinks = []
    for node in data:
        props = node.get("info", {}).get("props", {})
        if props.get("media.class") == "Audio/Sink":
            sinks.append({"name": props.get("node.name", ""),
                          "desc": props.get("node.description", "")[:64]})
    return sinks


# =====================================================================
# operations
# =====================================================================

def op_schema(_req):
    fields = {}
    for key, (targets, _v, label, help_text) in SCHEMA.items():
        path, section, option = targets[0]
        fields[key] = {"label": label, "help": help_text,
                       "file": os.path.basename(path), "section": section,
                       "option": option,
                       # The UI shows this so a multi-target field does not
                       # look like it edits one harmless line.
                       "targets": len(targets)}
    return {"ok": True, "fields": fields}


def _pop_of(uri):
    """Pull the bare hostname out of sip:user@host:port or sip:host."""
    v = uri.strip()
    for prefix in ("sip:", "sips:"):
        if v.startswith(prefix):
            v = v[len(prefix):]
    if "@" in v:
        v = v.split("@", 1)[1]
    return v.split(":", 1)[0].strip()


def op_get(_req):
    values = {}
    for key, (targets, _v, _l, _h) in SCHEMA.items():
        path, section, option = targets[0]
        if key == "voicemail.pin":
            values[key] = "set" if read_option(path, section, option) else ""
            continue
        raw = read_option(path, section, option)
        if key.endswith("password"):
            values[key] = "set" if raw else ""
        elif key == "trunk.pop":
            # Stored as a URI; shown as the bare hostname the user types.
            values[key] = _pop_of(raw)
        else:
            values[key] = raw
    return {"ok": True, "values": values}


def _render_target(key, option, value, current):
    """Build the literal string written for one target of a field.

    The POP is typed as a bare hostname but stored in four different
    shapes, so reconstruct each from the existing line rather than
    overwriting the user portion of client_uri.
    """
    if key != "trunk.pop":
        return value
    if option == "server_uri":
        return "sip:%s" % value
    if option == "client_uri":
        user = ""
        if current.startswith("sip:") and "@" in current:
            user = current[4:].split("@", 1)[0]
        return "sip:%s@%s" % (user, value) if user else "sip:%s" % value
    if option == "match":
        return value
    if option == "contact":
        port = ""
        if ":" in current.replace("sip:", "", 1):
            tail = current.replace("sip:", "", 1)
            if ":" in tail:
                port = ":" + tail.split(":", 1)[1]
        return "sip:%s%s" % (value, port or ":5060")
    return value


def op_set(req):
    key = req.get("field", "")
    if key not in SCHEMA:
        raise ValueError("unknown field")
    if key == "voicemail.pin":
        return op_set_pin(req)
    targets, validator, _l, _h = SCHEMA[key]
    value = validator(req.get("value", ""))

    # Validate every target BEFORE writing any, so a field that spans four
    # options cannot be left half-applied by a missing section.
    for path, section, _option in targets:
        if not os.path.exists(path):
            raise ValueError("%s does not exist" % os.path.basename(path))
        if not section_exists(path, section):
            raise ValueError("section [%s] not found in %s"
                             % (section, os.path.basename(path)))

    written, backups = [], []
    for path, section, option in targets:
        current = read_option(path, section, option)
        saved = write_option(path, section, option,
                             _render_target(key, option, value, current))
        written.append("[%s] %s" % (section, option))
        if saved:
            backups.append(os.path.basename(saved))
    needs_pjsip = any(p == PJSIP for p, _s, _o in targets)
    return {"ok": True, "backup": backups[0] if backups else "",
            "written": written,
            "note": ("updated %d settings; pjsip reload required" % len(written))
            if needs_pjsip else "voicemail reload required"}


def op_set_pin(req):
    pin = req.get("value", "")
    if not isinstance(pin, str) or not PIN_RE.fullmatch(pin):
        raise ValueError("PIN must be 4 to 10 digits")
    if pin in WEAK_PINS:
        raise ValueError("that PIN is on the guessed-first list; choose another")
    with open(VOICEMAIL, "r", encoding="utf-8") as fh:
        lines = fh.readlines()
    out, cur, done = [], None, False
    for line in lines:
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            cur = s[1:-1].strip()
            out.append(line)
            continue
        if cur == "default" and not done and re.match(r"^100\s*=>", s):
            rest = s.split("=>", 1)[1].split(",")
            rest[0] = pin
            # keep name/email columns the operator already set
            out.append("100 => %s\n" % ",".join(x.strip() for x in rest))
            done = True
            continue
        out.append(line)
    if not done:
        raise ValueError("mailbox 100 not found under [default] in voicemail.conf")
    saved = backup(VOICEMAIL)
    st = os.stat(VOICEMAIL)
    tmp = VOICEMAIL + ".orata-new"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.writelines(out)
        fh.flush()
        os.fsync(fh.fileno())
    os.chmod(tmp, stat.S_IMODE(st.st_mode))
    os.chown(tmp, st.st_uid, st.st_gid)
    os.replace(tmp, VOICEMAIL)
    return {"ok": True, "backup": os.path.basename(saved) if saved else "",
            "note": "voicemail reload required"}


def op_reload(req):
    what = req.get("what", "")
    allowed = {"pjsip": ["pjsip", "reload"],
               "dialplan": ["dialplan", "reload"],
               "voicemail": ["module", "reload", "app_voicemail"]}
    if what not in allowed:
        raise ValueError("unknown reload target")
    rc, out = asterisk(allowed[what], 20)
    return {"ok": rc == 0, "output": out.strip()[:400]}


def op_audio_target(req):
    """Point the callback bridge at a PipeWire sink the UI just selected.

    This exists because the sink name was previously hand-copied from
    pw-dump into alexa-bridge.conf. Nothing checked the two agreed, and a
    stale value fails in the worst way available: the bridge reports a
    missing sink mid-call, after a caller is already waiting.

    The name is matched against the sinks PipeWire currently reports rather
    than pattern-matched, so a typo cannot be written at all.
    """
    want = req.get("target", "")
    if not isinstance(want, str) or FORBIDDEN.search(want) or len(want) > 200:
        raise ValueError("invalid sink name")
    want = want.strip()
    available = [s["name"] for s in bt_sinks()]
    if want not in available:
        raise ValueError(
            "sink is not currently present; connect the speaker first"
            if available else
            "PipeWire reported no sinks; is the desktop session running?")
    if not os.path.exists(BRIDGE_CONF):
        raise ValueError("alexa-bridge.conf does not exist; the callback "
                         "bridge is not installed")
    saved = write_option(BRIDGE_CONF, "audio", "target", want)
    return {"ok": True, "target": want,
            "backup": os.path.basename(saved) if saved else "",
            "note": "restart orata-audio for the worker to re-read it"}


def op_audio_state(_req):
    """What the bridge is configured to use, and whether that sink exists."""
    configured = read_option(BRIDGE_CONF, "audio", "target")
    prompt = read_option(BRIDGE_CONF, "audio", "prompt")
    sinks = bt_sinks()
    names = [s["name"] for s in sinks]
    prompt_info = {}
    if prompt:
        try:
            with wave.open(prompt, "rb") as fh:
                prompt_info = {
                    "exists": True,
                    "seconds": round(fh.getnframes() / fh.getframerate(), 2),
                    "rate": fh.getframerate(),
                    # The worker enforces this range; show it before a call
                    # discovers it.
                    "valid": fh.getcomptype() == "NONE"
                    and 0.2 <= fh.getnframes() / fh.getframerate() <= 12,
                }
        except (OSError, wave.Error):
            prompt_info = {"exists": os.path.exists(prompt), "valid": False}
    return {"ok": True, "configured": configured, "prompt": prompt,
            "prompt_info": prompt_info, "sinks": sinks,
            "present": configured in names if configured else False,
            "installed": os.path.exists(BRIDGE_CONF)}


def op_restart_audio(_req):
    rc, out = run(["/usr/bin/systemctl", "restart", "orata-audio"], 20)
    return {"ok": rc == 0, "output": out.strip()[:300]}


def op_backups(_req):
    try:
        names = sorted(os.listdir(BACKUP_DIR), reverse=True)
    except OSError:
        return {"ok": True, "backups": []}
    return {"ok": True, "backups": names[:40]}


OPS = {
    "schema": op_schema,
    "get": op_get,
    "set": op_set,
    "reload": op_reload,
    "backups": op_backups,
    "bt.scan": lambda r: {"ok": True, **bt_scan(r.get("seconds", 12))},
    "bt.info": lambda r: {"ok": True, "info": bt_info(bt_mac(r.get("mac", "")))},
    "bt.pair": lambda r: bt_pair(bt_mac(r.get("mac", ""))),
    "bt.remove": lambda r: (lambda rc_out: {"ok": rc_out[0] == 0,
                                            "output": rc_out[1][:200]})(
        run(["/usr/bin/bluetoothctl", "remove", bt_mac(r.get("mac", ""))], 10)),
    "bt.sinks": lambda _r: {"ok": True, "sinks": bt_sinks()},
    "audio.state": op_audio_state,
    "audio.target": op_audio_target,
    "audio.restart": op_restart_audio,
}


def handle(conn, allowed_uids):
    with conn:
        conn.settimeout(45)
        try:
            creds = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
            _pid, uid, _gid = struct.unpack("3i", creds)
            if uid not in allowed_uids:
                conn.sendall(b'{"ok":false,"error":"denied"}\n')
                return
            buf = b""
            while b"\n" not in buf:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                buf += chunk
                if len(buf) > MAX_REQUEST:
                    conn.sendall(b'{"ok":false,"error":"request too large"}\n')
                    return
            req = json.loads(buf.split(b"\n", 1)[0].decode("utf-8"))
            if not isinstance(req, dict):
                raise ValueError("request must be an object")
            op = req.get("op", "")
            if op not in OPS:
                raise ValueError("unknown op")
            with LOCK:
                reply = OPS[op](req)
        except ValueError as exc:
            reply = {"ok": False, "error": str(exc)[:200]}
        except Exception as exc:  # never leak a traceback to the socket
            print("orata-admin: %r" % exc, file=sys.stderr, flush=True)
            reply = {"ok": False, "error": "helper error; see journalctl"}
        try:
            conn.sendall((json.dumps(reply) + "\n").encode("utf-8"))
        except OSError:
            pass


def main():
    if os.geteuid() != 0:
        sys.exit("orata-admin: must run as root")
    os.umask(0o077)
    allowed = {0}
    try:
        allowed.add(pwd.getpwnam("asterisk").pw_uid)
    except KeyError:
        sys.exit("orata-admin: no asterisk user")

    path = SOCKET_PATH
    sockdir = os.path.dirname(path)
    os.makedirs(sockdir, mode=0o750, exist_ok=True)
    # The socket is root:asterisk 0660, but the web UI also needs to
    # TRAVERSE this directory. systemd's RuntimeDirectory= makes it
    # root:root, and an ExecStartPre=chgrp does not work: ProtectSystem
    # gives that helper process a private /run namespace, so it silently
    # succeeds against a copy. Do it here, in the daemon that owns it.
    try:
        os.chown(sockdir, 0, grp.getgrnam("asterisk").gr_gid)
        os.chmod(sockdir, 0o750)
    except (KeyError, OSError) as exc:
        print("orata-admin: cannot set socket dir group: %r" % exc,
              file=sys.stderr, flush=True)
    if os.path.exists(path):
        if not stat.S_ISSOCK(os.lstat(path).st_mode):
            sys.exit("orata-admin: socket path occupied by a non-socket")
        os.unlink(path)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(path)
    try:
        gid = grp.getgrnam("asterisk").gr_gid
        os.chown(path, 0, gid)
    except KeyError:
        pass
    os.chmod(path, 0o660)
    server.listen(8)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    print("orata-admin: listening on %s" % path, flush=True)
    try:
        while True:
            conn, _ = server.accept()
            threading.Thread(target=handle, args=(conn, allowed),
                             daemon=True).start()
    finally:
        server.close()
        try:
            os.unlink(path)
        except OSError:
            pass


if __name__ == "__main__":
    main()