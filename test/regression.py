#!/usr/bin/env python3
"""Isolated regression tests. No live CLI, SIP calls, ntfy or Alexa traffic.

Run: python3 test/regression.py
Needs the web app's Debian dependencies. Real synthesis tests additionally
need espeak-ng (or espeak) and sox; otherwise those tests are explicitly skipped.
"""
import asyncio
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock
import urllib.parse
import wave

ROOT = Path(__file__).resolve().parents[1]
with mock.patch.dict(os.environ, {"ORATA_WEB_CONF": str(ROOT / "test/nonexistent.conf")}):
    spec = importlib.util.spec_from_file_location("orata_web", ROOT / "bin/orata_web.py")
    web = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(web)


def wav(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as fh:
        fh.setnchannels(1)
        fh.setsampwidth(2)
        fh.setframerate(8000)
        fh.writeframes(b"\x00\x00" * 800)


async def http(path, method="GET", token="test-token", data=None):
    """Minimal ASGI client, so no httpx/TestClient version dependency."""
    url = urllib.parse.urlsplit(path)
    headers = [(b"host", b"test")]
    if token is not None:
        headers.append((b"x-orata-token", token.encode()))
    body = urllib.parse.urlencode(data or {}).encode()
    headers.append((b"content-type", b"application/x-www-form-urlencoded"))
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": method, "scheme": "http", "path": url.path,
        "raw_path": url.path.encode(), "query_string": url.query.encode(),
        "headers": headers, "client": ("127.0.0.1", 10000), "server": ("test", 80),
    }
    messages = []
    received = False

    async def receive():
        nonlocal received
        if not received:
            received = True
            return {"type": "http.request", "body": body, "more_body": False}
        await asyncio.Event().wait()

    async def send(message):
        messages.append(message)

    await web.app(scope, receive, send)
    start = next(m for m in messages if m["type"] == "http.response.start")
    content = b"".join(m.get("body", b"") for m in messages
                       if m["type"] == "http.response.body")
    return start["status"], dict(start["headers"]), content


class WebTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix=".web-regression-", dir=ROOT / "test")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.audio = self.base / "audio"
        self.clips = self.base / "clips"
        self.audio.mkdir()
        self.clips.mkdir()
        self.conf = self.base / "announce.conf"
        self.conf.write_text("ORATA_ALEXA_MODE=off\nORATA_NTFY_URL=https://private.example/topic-secret\n")
        self.log = self.base / "announce.log"
        self.log.write_text("2026-10-04 clip OK   20261004-060014 1644 bytes\n")
        for key, value in {
            "TOKEN": "test-token", "AUDIO_DIR": str(self.audio), "CLIP_DIR": str(self.clips),
            "ANNOUNCE_CONF": str(self.conf), "LOG": str(self.log),
            "PJSIP_CONF": str(ROOT / "asterisk/pjsip.conf"),
        }.items():
            patch = mock.patch.object(web, key, value)
            patch.start()
            self.addCleanup(patch.stop)
        patch = mock.patch.object(web, "ast", side_effect=self.fake_ast)
        self.ast = patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch.object(web, "sh", return_value=(127, "unavailable in test"))
        patch.start()
        self.addCleanup(patch.stop)

    @staticmethod
    def fake_ast(cmd, timeout=10):
        if cmd == "core show version":
            return True, "Asterisk 22.11.0\n"
        if cmd == "pjsip show registrations":
            return True, "voipms_reg/sip:example.test voipms_auth Registered\n"
        if cmd == "pjsip show contacts":
            return True, "Contact: pc/sip:pc@192.0.2.10 abc Avail 3.2\n"
        if cmd.startswith("database show"):
            family = cmd.split()[-1]
            if family == "cnam":
                return True, "/cnam/15551234567 : Mom\n1 results found.\n"
            return True, "0 results found.\n"
        if cmd == "dialplan show globals":
            return True, "STRICT => 0\nRINGALL => PJSIP/pc\n"
        return True, "There is no existence of context\n"

    def get(self, path, **kwargs):
        return asyncio.run(http(path, **kwargs))

    def test_all_pages_with_empty_library(self):
        for path in ("/", "/configure", "/books", "/setup", "/devices", "/audio",
                     "/diag", "/harness", "/log"):
            with self.subTest(path=path):
                status, _, body = self.get(path)
                self.assertEqual(status, 200)
                self.assertIn(b"<html", body)

    def test_audio_page_with_saved_clip_and_assigned_name(self):
        # Regression: CLIP_ROLES has four fields, clips_card unpacked three.
        wav(self.clips / "20261004-060014.wav")
        wav(self.clips / "role-press-one.wav")
        wav(self.clips / "name-15551234567.wav")
        wav(self.audio / "20261004-060015-15551234567.wav")
        status, _, body = self.get("/audio")
        self.assertEqual(status, 200)
        self.assertIn(b"use as robocall gate", body)
        self.assertIn(b"20261004-060014", body)
        self.assertIn(b"role-press-one.wav", body)

    def test_dashboard_status_and_no_mutation(self):
        status, _, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn(b"<h1>Dashboard</h1>", body)
        self.assertIn(b"1 available", body)
        self.assertIn(b"Registered", body)
        self.assertFalse(any(call.args[0].startswith(("channel originate", "database put"))
                             for call in self.ast.call_args_list))

    def test_dashboard_when_asterisk_unreachable(self):
        with mock.patch.object(web, "ast", return_value=(False, "Unable to connect")):
            status, _, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn(b"Unreachable", body)

    def test_configure_does_not_show_private_topic(self):
        status, _, body = self.get("/configure")
        self.assertEqual(status, 200)
        self.assertIn(b"file-managed", body)
        self.assertNotIn(b"topic-secret", body)
        self.assertIn(b'aria-label="Help"', body)

    def test_exported_arc_settings_parse_without_executing_config(self):
        self.conf.write_text(
            "export REFRESH_TOKEN='dummy-private-token'\n"
            "export AMAZON=amazon.com\n"
            "ORATA_ALEXA_MODE=arc\nORATA_ALEXA_CMD=announce\n")
        cfg = web.load_conf(str(self.conf))
        self.assertEqual(cfg["REFRESH_TOKEN"], "dummy-private-token")
        self.assertEqual(cfg["AMAZON"], "amazon.com")
        for path in ("/", "/configure", "/setup", "/diag", "/harness"):
            with self.subTest(path=path):
                status, _, body = self.get(path)
                self.assertEqual(status, 200)
                self.assertNotIn(b"dummy-private-token", body)
        self.assertIn(b"Current upstream rejects announce", self.get("/diag")[2])

    def test_arc_diagnostics_missing_token_and_jq(self):
        self.conf.write_text("ORATA_ALEXA_MODE=arc\n")
        with mock.patch.object(web.shutil, "which", return_value=None):
            checks = web.diag_announce()
        by_name = {c["n"]: c for c in checks}
        self.assertEqual(by_name["ARC refresh token"]["s"], "warn")
        self.assertEqual(by_name["ARC jq dependency"]["s"], "warn")
        self.assertEqual(by_name["ARC speech command"]["d"], "speak")

    def test_auth_get_and_post(self):
        for path, method in (("/", "GET"), ("/audio", "GET"), ("/configure", "GET"),
                             ("/add", "POST"), ("/clips/say", "POST")):
            with self.subTest(path=path):
                self.assertEqual(self.get(path, method=method, token=None)[0], 401)
                self.assertEqual(self.get(path, method=method, token="wrong")[0], 401)

    def test_token_redirect_cookie(self):
        status, headers, _ = self.get("/?token=test-token", token=None)
        self.assertEqual(status, 303)
        self.assertEqual(headers[b"location"], b"/")
        self.assertIn(b"HttpOnly", headers[b"set-cookie"])
        self.assertIn(b"SameSite=strict", headers[b"set-cookie"])

    def test_owner_add_goes_through_cli_and_back_to_books(self):
        with mock.patch.object(web, "cnam_cmd", return_value=(True, "")) as cli:
            status, headers, _ = self.get("/add", method="POST",
                                          data={"family": "owner", "number": "15551234567"})
        self.assertEqual(status, 303)
        self.assertTrue(headers[b"location"].startswith(b"/books?"))
        cli.assert_called_once_with(["owner-add", "15551234567"])

    def test_owner_simulation_bypasses_strict(self):
        with mock.patch.object(web, "globals_map", return_value={"STRICT": "1"}), \
             mock.patch.object(web, "db_get", side_effect=lambda f, n: {
                 "owner": "1", "cnam": "Mom", "allow": "1",
             }.get(f, "")):
            steps, (outcome, detail) = web.simulate("15551234567")
        self.assertEqual(outcome, "OWNER GATE")
        self.assertIn("3434", detail)
        self.assertTrue(any("orata-record" in text for hit, text, sub in steps))

    def test_audio_log_name_parser_and_log_link(self):
        name = "20261004-060015-15551234567.wav"
        self.assertEqual(web.audio_name_in("audio OK   15551234567 /var/lib/orata/" + name), name)
        self.assertEqual(web.audio_name_in("audio OK   /var/lib/orata/" + name), name)
        self.assertEqual(web.audio_name_in("audio FAIL 15551234567"), "")
        self.log.write_text("audio OK   15551234567 /var/lib/orata/" + name + "\n")
        self.assertIn(b">play</a>", self.get("/log")[2])

    def test_clip_serving_and_invalid_names(self):
        wav(self.clips / "20261004-060014.wav")
        status, headers, body = self.get("/clips/file?f=20261004-060014.wav")
        self.assertEqual(status, 200)
        self.assertEqual(headers[b"content-type"], b"audio/wav")
        self.assertTrue(body.startswith(b"RIFF"))
        self.assertEqual(self.get("/clips/file?f=web.conf")[0], 400)
        self.assertEqual(self.get("/clips/file?f=20261004-000000.wav")[0], 404)

    def test_prompt_text_not_truncated_to_forty_characters(self):
        text = "text: Press 1 to replay, 2 to save, 3 to re-record, or star to cancel."
        (self.clips / "role-rec-menu.txt").write_text(text)
        self.assertEqual(web.role_source("rec-menu"), text)


class ClipTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix=".clip-regression-", dir=ROOT / "test")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.clips = self.base / "clips"
        self.audio = self.base / "audio"
        self.conf = self.base / "announce.conf"
        self.conf.write_text(
            "ORATA_CLIP_DIR='%s'\nORATA_AUDIO_DIR='%s'\nORATA_LOG='%s'\n"
            "ORATA_CLIP_KEEP=1\nORATA_AUDIO_KEEP=50\nORATA_ALEXA_MODE=off\n"
            "ORATA_RECORD_AUDIO=1\nORATA_NTFY_URL=\nORATA_TTS=\n"
            % (self.clips, self.audio, self.base / "announce.log"))
        self.env = dict(os.environ, ORATA_ANNOUNCE_CONF=str(self.conf))
        self.run_clip("init")

    def run_clip(self, *args, success=True):
        result = subprocess.run(["bash", str(ROOT / "bin/orata-clip.sh"), *map(str, args)],
                                capture_output=True, text=True, env=self.env, timeout=30)
        if success:
            self.assertEqual(result.returncode, 0, result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0)
        return result

    def test_arc_uses_speak_and_inherits_exported_settings(self):
        # Fake executable only; no Amazon requests or credentials.
        fake = self.base / "fake-arc"
        output = self.base / "arc-invocation"
        fake.write_text(
            "#!/bin/sh\n"
            "printf '%s\\n' \"$@\" \"$REFRESH_TOKEN\" \"$AMAZON\" \"$TMP\" > \"$ARC_CAPTURE\"\n")
        fake.chmod(0o700)
        with self.conf.open("a") as fh:
            fh.write(
                "ORATA_ALEXA_MODE=arc\nORATA_RECORD_AUDIO=0\n"
                "ORATA_ARC='%s'\n"
                "export REFRESH_TOKEN='dummy-token'\nexport AMAZON=amazon.com\n"
                "export TMP='%s'\nexport ARC_CAPTURE='%s'\n"
                % (fake, self.base, output))
        result = subprocess.run(
            ["bash", str(ROOT / "bin/orata-announce.sh"), "15551234567", "Mom"],
            env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = output.read_text().splitlines()
        self.assertEqual(lines[:4], ["-d", "ALL", "-e", "speak:Call from Mom"])
        self.assertEqual(lines[4:], ["dummy-token", "amazon.com", str(self.base)])
        log = (self.base / "announce.log").read_text()
        self.assertIn("alexa OK", log)
        self.assertNotIn("dummy-token", log)

    def test_retention_preserves_roles_names_and_their_sources(self):
        old = self.clips / "20261004-060014.wav"
        wav(old)
        (old.with_suffix(".txt")).write_text("old label")
        self.run_clip("assign", "press-one", old.stem)
        self.run_clip("name-set", "15551234567", old.stem)
        os.utime(old, (1, 1))
        new = self.clips / "tmp/20261004-060022.wav"
        wav(new)
        self.run_clip("commit", new)
        self.assertFalse(old.exists())
        self.assertFalse(old.with_suffix(".txt").exists())
        for name in ("role-press-one.wav", "role-press-one.txt",
                     "name-15551234567.wav", "name-15551234567.txt",
                     "20261004-060022.wav"):
            self.assertTrue((self.clips / name).exists(), name)

    def test_missing_and_header_only_recordings_are_not_committed(self):
        missing = self.clips / "tmp/20261004-060022.wav"
        self.run_clip("commit", missing, success=False)
        with wave.open(str(missing), "wb") as fh:
            fh.setnchannels(1)
            fh.setsampwidth(2)
            fh.setframerate(8000)
        self.run_clip("commit", missing, success=False)
        self.assertFalse((self.clips / missing.name).exists())

    @unittest.skipUnless(shutil.which("sox") and
                         (shutil.which("espeak-ng") or shutil.which("espeak")),
                         "real synthesis needs sox and espeak-ng/espeak")
    def test_prompt_name_synthesis_and_recorded_announcement(self):
        self.run_clip("say", "rec-menu", "Press 1 to replay, 2 to save.")
        self.run_clip("name-say", "15551234567", "Mom")
        for name in ("role-rec-menu.wav", "name-15551234567.wav"):
            with wave.open(str(self.clips / name), "rb") as fh:
                self.assertEqual(fh.getframerate(), 8000)
                self.assertEqual(fh.getnchannels(), 1)
                self.assertEqual(fh.getsampwidth(), 2)
                self.assertGreater(fh.getnframes(), 0)
        result = subprocess.run(
            ["bash", str(ROOT / "bin/orata-announce.sh"), "15551234567", "Mom"],
            env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        log = (self.base / "announce.log").read_text()
        self.assertIn("-- recorded name", log)
        self.assertNotIn("audio WARN", log)
        files = list(self.audio.glob("*.wav"))
        self.assertEqual(len(files), 1)
        with wave.open(str(files[0]), "rb") as fh:
            self.assertEqual(fh.getframerate(), 8000)


class DialplanTests(unittest.TestCase):
    def test_owner_hangup_save_is_armed_after_prompt_and_checks_file(self):
        # Structural regression only; runtime Record()/hangup needs a real channel.
        dp = (ROOT / "asterisk/extensions.conf").read_text()
        owner = dp.split("[orata-record]", 1)[1].split("[internal]", 1)[0]
        self.assertLess(owner.index("Playback(${P_RECSTART})"),
                        owner.index("Set(TMPCLIP=${CLIPDIR}"))
        hangup = owner.split("exten => h", 1)[1]
        self.assertIn("STAT(e,${TMPCLIP}.wav)", hangup)
        self.assertIn("STAT(s,${TMPCLIP}.wav)", hangup)
        self.assertIn('"${SYSTEMSTATUS}" = "SUCCESS"', hangup)
        self.assertIn('"${SYSTEMSTATUS}" != "SUCCESS"]?record-failed', owner)


if __name__ == "__main__":
    unittest.main(verbosity=2)