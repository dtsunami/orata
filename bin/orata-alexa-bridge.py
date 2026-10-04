#!/usr/bin/env python3
"""Experimental single-call acoustic Echo callback bridge; stdlib-only AGI.

No shell commands, public socket, AMI daemon, or Amazon credentials.
AGI reserves/claims a conference; a short-lived watcher controls fixed audio,
checks membership and tears down on deadlines/departures. Disabled by default.
"""
import configparser
from contextlib import contextmanager
import fcntl
import json
import logging
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time
import uuid

LOG = logging.getLogger("orata-alexa-bridge")


class Config:
    def __init__(self, path):
        parser = configparser.ConfigParser(interpolation=None)
        if not parser.read(path):
            raise ValueError("missing bridge config")
        cfg = parser["bridge"]
        self.enabled = cfg.getboolean("enabled", fallback=False)
        self.callback = cfg.get("callback_caller_id", "").strip()
        self.did = cfg.get("destination", "").strip()
        self.allowed = {n.strip() for n in cfg.get("allowed_callers", "").split(",") if n.strip()}
        self.state_dir = Path(cfg.get("state_dir", "/var/lib/orata/alexa-bridge"))
        self.audio_socket = cfg.get("audio_socket", "/run/orata-audio/play.sock")
        self.log = cfg.get("log", "/var/log/orata/alexa-bridge.log")
        self.window = cfg.getint("callback_window", fallback=30)
        self.limit = cfg.getint("call_limit", fallback=900)
        self.cooldown = cfg.getint("cooldown", fallback=60)
        if not 10 <= self.window <= 60 or not 60 <= self.limit <= 3600 or not 30 <= self.cooldown <= 300:
            raise ValueError("time limits out of range")
        if not all(os.path.isabs(str(p)) for p in (self.state_dir, self.audio_socket, self.log)):
            raise ValueError("absolute state/socket/log paths required")
        if self.enabled:
            if not cfg.getboolean("acknowledge_spoofable_callback", fallback=False):
                raise ValueError("explicit spoofable-callback risk acknowledgement required")
            if not all(re.fullmatch(r"[0-9]{7,15}", n) for n in (self.callback, self.did)):
                raise ValueError("exact numeric callback CID and destination required")
            if not self.allowed or any(not re.fullmatch(r"[0-9]{7,15}", n) for n in self.allowed):
                raise ValueError("explicit numeric test callers required; no wildcard")
            if self.callback in self.allowed:
                raise ValueError("callback CID cannot originate bridge requests")


class Store:
    def __init__(self, cfg):
        self.cfg = cfg
        cfg.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = cfg.state_dir / "state.json"

    @contextmanager
    def transaction(self):
        with (self.cfg.state_dir / "state.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            state = json.loads(self.path.read_text()) if self.path.exists() else {}
            yield state
            tmp = self.path.with_suffix(".new")
            with tmp.open("w") as fh:
                json.dump(state, fh)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)

    def snapshot(self):
        with self.transaction() as state:
            return dict(state.get("session", {}))

    def reserve(self, caller, destination, channel, now=None):
        now = time.time() if now is None else now
        if not self.cfg.enabled or destination != self.cfg.did or caller not in self.cfg.allowed:
            return None
        with self.transaction() as state:
            old = state.get("session")
            if old and now <= old["expires"]:
                return None
            if old:
                # Lost watcher: impose cooldown before allowing reuse.
                state.pop("session")
                state["cool_until"] = now + self.cfg.cooldown
            if now < state.get("cool_until", 0):
                return None
            token = uuid.uuid4().hex
            session = {
                "token": token, "room": "orata-ab-" + token,
                "caller": channel, "callback": "", "phase": "arming",
                "created": now, "deadline": now + 5 + self.cfg.window,
                "expires": now + self.cfg.window + self.cfg.limit + 10,
                "connected": False, "reason": "",
            }
            state["session"] = session
            return dict(session)

    def arm(self, token, now=None):
        now = time.time() if now is None else now
        with self.transaction() as state:
            session = state.get("session")
            if not session or session["token"] != token or session["phase"] != "arming":
                return False
            session["phase"] = "waiting"
            session["deadline"] = now + self.cfg.window
            return True

    def claim(self, caller, destination, channel, now=None):
        now = time.time() if now is None else now
        if not self.cfg.enabled or caller != self.cfg.callback:
            return "NORMAL", None
        if destination != self.cfg.did:
            return "REJECT", None
        with self.transaction() as state:
            session = state.get("session")
            if not session or session["phase"] != "waiting" or now >= session["deadline"]:
                return "REJECT", None
            session["phase"] = "claimed"
            session["callback"] = channel
            return "CALLBACK", dict(session)

    def connected(self, token):
        with self.transaction() as state:
            session = state.get("session")
            if not session or session["token"] != token or session["phase"] != "claimed":
                return False
            session["connected"] = True
            session["phase"] = "connected"
            return True

    def stop(self, token, reason):
        with self.transaction() as state:
            session = state.get("session")
            if not session or session["token"] != token:
                return None
            session["phase"] = "ended" if session["connected"] else "fallback"
            session["reason"] = reason
            return dict(session)

    def release(self, token, now=None):
        now = time.time() if now is None else now
        with self.transaction() as state:
            session = state.get("session")
            if not session or session["token"] != token:
                return None
            state.pop("session")
            state["cool_until"] = now + self.cfg.cooldown
            return dict(session)


def cli(command):
    result = subprocess.run(["asterisk", "-rx", command], capture_output=True, text=True, timeout=3)
    if result.returncode:
        raise RuntimeError("Asterisk CLI unavailable")
    return result.stdout


def channels():
    live = {}
    for line in cli("core show channels concise").splitlines():
        parts = line.split("!")
        if len(parts) >= 7:
            live[parts[0]] = (parts[5], parts[6])
    return live


def in_room(live, channel, room):
    app, data = live.get(channel, ("", ""))
    return app == "ConfBridge" and data.split(",", 1)[0] == room


def teardown(session):
    if not session:
        return
    room = session.get("room", "")
    callback = session.get("callback", "")
    commands = []
    if re.fullmatch(r"orata-ab-[a-f0-9]{32}", room):
        commands.append("confbridge kick %s all" % room)
    # Also clear callbacks still between route/Answer/ConfBridge. Channel names
    # originate from Asterisk, but validate before assembling CLI arguments.
    if callback and re.fullmatch(r"[A-Za-z0-9_./;@+:-]+", callback):
        commands.append("channel request hangup %s" % callback)
    for command in commands:
        try:
            cli(command)
        except (OSError, RuntimeError, subprocess.TimeoutExpired):
            LOG.warning("conference teardown failed")


def watch(cfg, store, token):
    session = store.snapshot()
    if not session or session["token"] != token:
        return
    audio = None
    last = session
    try:
        start = time.monotonic()
        while not in_room(channels(), session["caller"], session["room"]):
            current = store.snapshot()
            if not current or current["token"] != token:
                return
            if time.monotonic() - start >= 5:
                raise RuntimeError("original channel did not enter conference")
            time.sleep(0.2)
        if not store.arm(token):
            return
        audio = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        audio.settimeout(2)
        audio.connect(cfg.audio_socket)
        audio.sendall(b"PLAY\n")
        audio.setblocking(False)
        response = b""
        audio_deadline = time.monotonic() + 20
        while True:
            current = store.snapshot()
            if not current or current["token"] != token:
                break
            last = current
            live = channels()
            if not in_room(live, current["caller"], current["room"]):
                store.stop(token, "caller-left")
                break
            if current["phase"] in ("fallback", "ended"):
                break
            if audio is not None:
                try:
                    chunk = audio.recv(256)
                except BlockingIOError:
                    chunk = None
                if chunk is not None:
                    response += chunk
                    if not chunk or b"\n" in response:
                        audio.close()
                        audio = None
                        if response.strip() != b"OK":
                            raise RuntimeError("fixed audio playback failed")
                if audio is not None and time.monotonic() >= audio_deadline:
                    raise RuntimeError("audio timed out")
            together = in_room(live, current["callback"], current["room"])
            if together and not current["connected"]:
                if store.connected(token):
                    LOG.info("connected session %s", token)
            elif current["connected"] and not together:
                store.stop(token, "callback-left")
                break
            elif current["phase"] == "claimed" and current["callback"] not in live:
                store.stop(token, "callback-left-before-join")
                break
            if not current["connected"] and time.time() >= current["deadline"]:
                store.stop(token, "callback-timeout")
                break
            if time.time() >= current["expires"]:
                store.stop(token, "call-limit")
                break
            time.sleep(0.25)
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError):
        LOG.warning("watch/playback failure for session %s", token)
        store.stop(token, "watch-or-playback-failure")
    finally:
        # Closing during playback tells the worker to terminate pw-play.
        if audio is not None:
            audio.close()
        stopped = store.stop(token, "watch-ended")
        teardown(stopped or last)


class AGI:
    def __init__(self):
        self.env = {}
        for line in sys.stdin:
            if not line.strip():
                break
            key, _, value = line.partition(":")
            self.env[key] = value.strip()

    def set(self, key, value):
        if not re.fullmatch(r"[A-Za-z0-9_-]*", str(value)):
            raise ValueError("invalid AGI variable value")
        print('SET VARIABLE %s "%s"' % (key, value), flush=True)
        reply = sys.stdin.readline()
        if not reply or reply.startswith("HANGUP"):
            raise EOFError("channel closed")
        if not reply.startswith("200 result="):
            raise RuntimeError("AGI command failed")


def main():
    os.umask(0o077)
    logging.basicConfig(stream=sys.stderr, level=logging.INFO,
                        format="orata-alexa-bridge: %(levelname)s %(message)s")
    path = os.environ.get("ORATA_BRIDGE_CONF", "/etc/orata/alexa-bridge.conf")
    action = sys.argv[1] if len(sys.argv) > 1 else ""
    if action == "watch":
        cfg = Config(path)
        watch(cfg, Store(cfg), sys.argv[2])
        return
    agi = AGI()
    reserved = None
    store = None
    try:
        agi.set("AB_ACTION", "NORMAL")
        agi.set("AB_RESULT", "FALLBACK")
        cfg = Config(path)
        if not cfg.enabled:
            return
        caller = agi.env.get("agi_callerid", "")
        destination = agi.env.get("agi_extension", "")
        channel = agi.env.get("agi_channel", "")
        # Identify the reserved CID before accessing state: corrupt/unwritable
        # state must not turn a callback into another originating request.
        if action == "route" and caller == cfg.callback:
            agi.set("AB_ACTION", "REJECT")
        store = Store(cfg)
        if action == "route":
            outcome, session = store.claim(caller, destination, channel)
            if session:
                for key, value in (("AB_TOKEN", session["token"]), ("AB_ROOM", session["room"]),
                                   ("AB_LIMIT", cfg.window + cfg.limit + 10)):
                    agi.set(key, value)
            agi.set("AB_ACTION", outcome)
        elif action == "offer":
            # Gosub changes agi_extension to s. The dialplan passes the original
            # DID explicitly; never substitute the configured DID for missing data.
            destination = agi.env.get("agi_arg_2", "")
            reserved = store.reserve(caller, destination, channel)
            if reserved:
                for key, value in (("AB_TOKEN", reserved["token"]), ("AB_ROOM", reserved["room"]),
                                   ("AB_LIMIT", cfg.window + cfg.limit + 10)):
                    agi.set(key, value)
                # Detach all AGI descriptors; retaining stderr can hold AGI open.
                with open(cfg.log, "a") as log:
                    subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "watch", reserved["token"]],
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=log, start_new_session=True)
                agi.set("AB_ACTION", "WAIT")
        elif action in ("result", "cleanup"):
            token = agi.env.get("agi_arg_2", "")
            if not re.fullmatch(r"[a-f0-9]{32}", token):
                raise ValueError("invalid session token")
            if action == "result":
                session = store.release(token)
                teardown(session)
                agi.set("AB_RESULT", "CONNECTED" if session and session["connected"] else "FALLBACK")
            else:
                role = agi.env.get("agi_arg_3", "")
                if role == "caller":
                    session = store.release(token)
                elif role == "callback":
                    session = store.stop(token, "callback-hangup")
                else:
                    raise ValueError("invalid cleanup role")
                teardown(session)
        else:
            raise ValueError("unknown AGI action")
    except (EOFError, OSError, ValueError, KeyError, RuntimeError,
            subprocess.SubprocessError, configparser.Error):
        if reserved:
            teardown(store.release(reserved["token"]))
        LOG.warning("AGI %s failed; check private config and audio service", action)


if __name__ == "__main__":
    main()