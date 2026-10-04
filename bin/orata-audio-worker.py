#!/usr/bin/env python3
"""Fixed local prompt player in dfstar's PipeWire session. No network API.

Peer UID must be asterisk or the service owner. Only PLAY is accepted.
Requests cannot change the WAV, sink, volume, or executable. No retries.
Disconnecting the requester cancels playback. A missing sink fails closed.
"""
import configparser
import json
import os
from pathlib import Path
import pwd
import select
import signal
import socket
import stat
import struct
import subprocess
import sys
import threading
import time
import wave

LOCK = threading.Lock()


def validate_prompt(path):
    with wave.open(str(path), "rb") as fh:
        duration = fh.getnframes() / fh.getframerate()
        if fh.getcomptype() != "NONE" or not 0.2 <= duration <= 12:
            raise ValueError("fixed prompt must be a PCM WAV of 0.2 to 12 seconds")


def sink_ready(target):
    result = subprocess.run(["/usr/bin/pw-dump"], capture_output=True, text=True, timeout=3)
    if result.returncode:
        return False
    return any(n.get("info", {}).get("props", {}).get("node.name") == target
               and n.get("info", {}).get("props", {}).get("media.class") == "Audio/Sink"
               for n in json.loads(result.stdout))


def play(conn, cfg):
    if not sink_ready(cfg["target"]):
        raise RuntimeError("configured Bluetooth sink is unavailable")
    validate_prompt(cfg["prompt"])
    process = subprocess.Popen(
        ["/usr/bin/pw-play", "--target=" + cfg["target"], cfg["prompt"]],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        end = time.monotonic() + 15
        while process.poll() is None:
            ready, _, _ = select.select([conn], [], [], 0.1)
            if ready:
                # A disconnect (or any further request bytes) cancels the fixed
                # playback, rather than placing a call for an abandoned caller.
                conn.recv(1)
                raise RuntimeError("request cancelled")
            if time.monotonic() >= end:
                raise RuntimeError("playback timed out")
        if process.returncode:
            raise RuntimeError("pw-play failed")
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def handle(conn, cfg, allowed):
    acquired = False
    with conn:
        conn.settimeout(2)
        try:
            _, uid, _ = struct.unpack("3i", conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if uid not in allowed:
                conn.sendall(b"DENIED\n")
                return
            request = b""
            while b"\n" not in request and len(request) <= 16:
                chunk = conn.recv(16)
                if not chunk:
                    return
                request += chunk
            if request != b"PLAY\n":
                conn.sendall(b"INVALID\n")
                return
            acquired = LOCK.acquire(blocking=False)
            if not acquired:
                conn.sendall(b"BUSY\n")
                return
            play(conn, cfg)
            conn.sendall(b"OK\n")
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, wave.Error):
            try:
                conn.sendall(b"FAIL\n")
            except OSError:
                pass
            print("orata-audio: playback failed/cancelled; check user audio and speaker",
                  file=sys.stderr, flush=True)
        finally:
            if acquired:
                LOCK.release()


def main():
    os.umask(0o007)
    parser = configparser.ConfigParser(interpolation=None)
    if not parser.read(os.environ.get("ORATA_BRIDGE_CONF", "/etc/orata/alexa-bridge.conf")):
        raise ValueError("missing audio config")
    cfg = dict(parser["audio"])
    path = Path(cfg["socket"])
    if not path.is_absolute() or not Path(cfg["prompt"]).is_absolute() or not cfg["target"]:
        raise ValueError("absolute paths and explicit sink required")
    validate_prompt(cfg["prompt"])
    allowed = {os.getuid(), pwd.getpwnam("asterisk").pw_uid}
    if path.exists():
        if not stat.S_ISSOCK(path.lstat().st_mode):
            raise ValueError("socket path is occupied by a non-socket")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
            try:
                probe.connect(str(path))
            except ConnectionRefusedError:
                path.unlink()
            else:
                raise ValueError("audio worker already running")
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    os.chmod(path, 0o660)
    server.listen(4)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        while True:
            conn, _ = server.accept()
            threading.Thread(target=handle, args=(conn, cfg, allowed), daemon=True).start()
    finally:
        server.close()
        path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()