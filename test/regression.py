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
import time
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
        """Unauthenticated requests are refused -- never served.

        The appliance build redirects a browser to /login instead of
        returning 401. What must hold is that the page body is not
        delivered; the exact status is secondary.
        """
        for path, method in (("/", "GET"), ("/audio", "GET"), ("/configure", "GET"),
                             ("/add", "POST"), ("/clips/say", "POST")):
            for tok in (None, "wrong"):
                with self.subTest(path=path, token=tok):
                    status, headers, body = self.get(path, method=method, token=tok)
                    self.assertIn(status, (303, 401))
                    if status == 303:
                        self.assertEqual(headers[b"location"], b"/login")
                    self.assertNotIn(b"<h1>", body)

    def test_login_page_is_reachable_without_auth(self):
        status, _, body = self.get("/login", token=None)
        self.assertEqual(status, 200)
        self.assertIn(b"Sign in", body)

    def test_password_verify_and_change_roundtrip(self):
        auth = self.base / "admin-auth.json"
        with mock.patch.object(web, "AUTH_PATH", str(auth)):
            web.set_password("correct-horse-battery")
            self.assertTrue(web.verify_password("correct-horse-battery"))
            self.assertFalse(web.verify_password("wrong"))
            self.assertFalse(web.must_change_password())
            self.assertEqual(oct(auth.stat().st_mode & 0o777), "0o600")

    def test_firstboot_record_forces_password_change(self):
        auth = self.base / "admin-auth.json"
        auth.write_text('{"algo":"pbkdf2_sha256","iterations":10,'
                        '"salt":"AAAA","hash":"AAAA","must_change":true}')
        with mock.patch.object(web, "AUTH_PATH", str(auth)):
            self.assertTrue(web.must_change_password())

    def test_session_cookie_is_signed_and_expires(self):
        with mock.patch.object(web, "SESSION_SECRET", "unit-test-secret"):
            good = web.make_session()
            self.assertTrue(web.valid_session(good))
            self.assertFalse(web.valid_session("garbage"))
            self.assertFalse(web.valid_session(""))
            # tampering with the expiry invalidates the signature
            exp, nonce, sig = good.split(".")
            self.assertFalse(web.valid_session("%d.%s.%s" % (int(exp) + 9999, nonce, sig)))
            # a correctly signed but expired cookie is still rejected
            stale = "%d.%s" % (int(time.time()) - 10, "abcd")
            self.assertFalse(web.valid_session("%s.%s" % (stale, web._sign(stale))))

    def test_login_throttle_locks_out_after_repeated_failures(self):
        ip = "203.0.113.9"
        web.clear_failures(ip)
        self.addCleanup(web.clear_failures, ip)
        for _ in range(web.LOCKOUT_AFTER):
            self.assertEqual(web.throttled(ip), 0)
            web.note_failure(ip)
        self.assertGreater(web.throttled(ip), 0)
        web.clear_failures(ip)
        self.assertEqual(web.throttled(ip), 0)

    def test_admin_call_reports_missing_helper_without_raising(self):
        with mock.patch.object(web, "ADMIN_SOCK", str(self.base / "absent.sock")):
            reply = web.admin_call("get")
        self.assertFalse(reply["ok"])
        self.assertIn("helper", reply["error"])

    def test_privileged_pages_degrade_when_helper_is_absent(self):
        """A dead helper must not 500 the UI or hide why."""
        with mock.patch.object(web, "ADMIN_SOCK", str(self.base / "absent.sock")):
            for path in ("/trunk", "/speaker"):
                with self.subTest(path=path):
                    status, _, body = self.get(path)
                    self.assertEqual(status, 200)
                    self.assertIn(b"orata-admin", body)

    def test_trunk_page_renders_schema_from_helper(self):
        fake = {
            "schema": {"ok": True, "fields": {"trunk.pop": {
                "label": "voip.ms POP hostname",
                "help": "Hostname of the nearest voip.ms server.",
                "file": "pjsip.conf", "section": "voipms_reg",
                "option": "server_uri", "targets": 4}}},
            "get": {"ok": True, "values": {"trunk.pop": "sanjose2.voip.ms"}},
            "backups": {"ok": True, "backups": ["pjsip.conf.20261004-120000"]},
        }
        with mock.patch.object(web, "admin_call",
                               side_effect=lambda op, **k: fake.get(op, {"ok": True})):
            status, _, body = self.get("/trunk")
        self.assertEqual(status, 200)
        self.assertIn(b"voip.ms POP hostname", body)
        self.assertIn(b"sanjose2.voip.ms", body)
        self.assertIn(b'aria-label="Help"', body)

    def test_trunk_page_only_renders_fields_it_knows_how_to_order(self):
        """A field the UI does not list must not vanish silently.

        The page renders a fixed `order` list. If the helper gains a field
        and the UI is not updated, that field is simply absent -- which is
        how a removed trunk.did went unnoticed. Assert the contract.
        """
        fake = {
            "schema": {"ok": True, "fields": {"trunk.unknown_future_field": {
                "label": "Not In Order List", "help": "x", "file": "pjsip.conf",
                "section": "s", "option": "o", "targets": 1}}},
            "get": {"ok": True, "values": {}},
            "backups": {"ok": True, "backups": []},
        }
        with mock.patch.object(web, "admin_call",
                               side_effect=lambda op, **k: fake.get(op, {"ok": True})):
            status, _, body = self.get("/trunk")
        self.assertEqual(status, 200)
        self.assertNotIn(b"Not In Order List", body)
        # every key in the helper's real SCHEMA must be in the UI order list
        import importlib.util as _ilu
        _spec = _ilu.spec_from_file_location(
            "oa", Path(web.__file__).parent / "orata-admin-helper.py")
        _mod = _ilu.module_from_spec(_spec)
        _spec.loader.exec_module(_mod)
        page = Path(web.__file__).read_text()
        order = page.split('order = [', 1)[1].split(']', 1)[0]
        for key in _mod.SCHEMA:
            self.assertIn(key, order, "%s is not rendered by /trunk" % key)

    def test_trunk_password_value_is_never_echoed_to_the_browser(self):
        fake = {
            "schema": {"ok": True, "fields": {"trunk.password": {
                "label": "Sub-account password", "help": "SIP password.",
                "file": "pjsip.conf", "section": "voipms-auth",
                "option": "password"}}},
            "get": {"ok": True, "values": {"trunk.password": "set"}},
            "backups": {"ok": True, "backups": []},
        }
        with mock.patch.object(web, "admin_call",
                               side_effect=lambda op, **k: fake.get(op, {"ok": True})):
            status, _, body = self.get("/trunk")
        self.assertEqual(status, 200)
        self.assertIn(b'type="password"', body)
        self.assertNotIn(b'value="set"', body)

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


class AdminHelperTests(unittest.TestCase):
    """The privileged helper's validators and INI rewriter.

    Imported as a module and exercised directly: these functions are the
    trust boundary between the web UI and root, so they are tested without
    a socket or a running service in the way.
    """

    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location(
            "orata_admin", ROOT / "bin/orata-admin-helper.py")
        cls.helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.helper)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix=".admin-regression-",
                                               dir=ROOT / "test")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)

    def test_newline_injection_is_rejected(self):
        """The whole point of the enumerated surface: no smuggled INI lines."""
        check = self.helper._plain(120)
        for bad in ("a\nmalicious = 1", "a\r\nmalicious = 1", "a\x00b"):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    check(bad)

    def test_field_patterns_reject_plausible_mistakes(self):
        pop = self.helper.SCHEMA["trunk.pop"][1]
        self.assertEqual(pop("sanjose2.voip.ms"), "sanjose2.voip.ms")
        for bad in ("sip:sanjose2.voip.ms", "host with space", "", "ab"):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    pop(bad)

    def test_pop_is_written_to_every_place_it_appears(self):
        """A POP change must not leave registration and identify disagreeing."""
        conf = self.base / "pjsip.conf"
        conf.write_text(
            "[voipms_auth]\nusername=574149_orata\npassword=secret\n\n"
            "[voipms_aor]\ntype=aor\ncontact=sip:old.voip.ms:5060\n\n"
            "[voipms_reg]\ntype=registration\n"
            "server_uri=sip:old.voip.ms\n"
            "client_uri=sip:574149_orata@old.voip.ms\n\n"
            "[voipms_identify]\ntype=identify\nmatch=old.voip.ms\n")
        with mock.patch.object(self.helper, "PJSIP", str(conf)), \
             mock.patch.object(self.helper, "BACKUP_DIR", str(self.base / "bk")), \
             mock.patch.dict(self.helper.SCHEMA, {"trunk.pop": (
                 [(str(conf), "voipms_reg", "server_uri"),
                  (str(conf), "voipms_reg", "client_uri"),
                  (str(conf), "voipms_identify", "match"),
                  (str(conf), "voipms_aor", "contact")],
                 self.helper.SCHEMA["trunk.pop"][1], "POP", "help")}):
            reply = self.helper.op_set({"field": "trunk.pop",
                                        "value": "seattle.voip.ms"})
        self.assertTrue(reply["ok"])
        text = conf.read_text()
        self.assertNotIn("old.voip.ms", text)
        self.assertIn("server_uri = sip:seattle.voip.ms", text)
        # the sub-account must survive the client_uri rewrite
        self.assertIn("client_uri = sip:574149_orata@seattle.voip.ms", text)
        self.assertIn("match = seattle.voip.ms", text)
        self.assertIn("contact = sip:seattle.voip.ms:5060", text)

    def test_multi_target_write_is_refused_before_any_change(self):
        """A missing section must abort the whole field, not half-apply it."""
        conf = self.base / "pjsip.conf"
        original = ("[voipms_reg]\nserver_uri=sip:old.voip.ms\n"
                    "client_uri=sip:u@old.voip.ms\n")
        conf.write_text(original)
        with mock.patch.object(self.helper, "PJSIP", str(conf)), \
             mock.patch.object(self.helper, "BACKUP_DIR", str(self.base / "bk")), \
             mock.patch.dict(self.helper.SCHEMA, {"trunk.pop": (
                 [(str(conf), "voipms_reg", "server_uri"),
                  (str(conf), "voipms_absent", "match")],
                 self.helper.SCHEMA["trunk.pop"][1], "POP", "help")}):
            with self.assertRaises(ValueError):
                self.helper.op_set({"field": "trunk.pop", "value": "new.voip.ms"})
        self.assertEqual(conf.read_text(), original)

    def test_pop_hostname_is_extracted_for_display(self):
        self.assertEqual(self.helper._pop_of("sip:sanjose2.voip.ms"),
                         "sanjose2.voip.ms")
        self.assertEqual(self.helper._pop_of("sip:574149_orata@sanjose2.voip.ms"),
                         "sanjose2.voip.ms")
        self.assertEqual(self.helper._pop_of("sip:sanjose2.voip.ms:5060"),
                         "sanjose2.voip.ms")

    def test_mac_validator(self):
        self.assertEqual(self.helper.bt_mac("08:eb:ed:71:f9:02"),
                         "08:EB:ED:71:F9:02")
        for bad in ("08:EB:ED:71:F9", "not-a-mac", "08:EB:ED:71:F9:02; rm -rf /"):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    self.helper.bt_mac(bad)

    def test_write_option_preserves_comments_and_other_sections(self):
        conf = self.base / "pjsip.conf"
        conf.write_text("; leading comment\n"
                        "[voipms-auth]\n"
                        "type = auth\n"
                        "username = old_user\n"
                        "password = old_pass\n"
                        "\n"
                        "[other]\n"
                        "username = untouched\n")
        with mock.patch.object(self.helper, "BACKUP_DIR", str(self.base / "bk")):
            self.helper.write_option(str(conf), "voipms-auth", "username", "new_user")
        text = conf.read_text()
        self.assertIn("; leading comment", text)
        self.assertIn("username = new_user", text)
        self.assertNotIn("old_user", text)
        self.assertIn("username = untouched", text)
        self.assertIn("password = old_pass", text)

    def test_write_option_adds_missing_option_to_existing_section(self):
        conf = self.base / "pjsip.conf"
        conf.write_text("[voipms-reg]\ntype = registration\n")
        with mock.patch.object(self.helper, "BACKUP_DIR", str(self.base / "bk")):
            self.helper.write_option(str(conf), "voipms-reg", "server_uri",
                                     "sip:seattle.voip.ms")
        self.assertIn("server_uri = sip:seattle.voip.ms", conf.read_text())

    def test_write_option_refuses_unknown_section(self):
        conf = self.base / "pjsip.conf"
        conf.write_text("[voipms-auth]\ntype = auth\n")
        with mock.patch.object(self.helper, "BACKUP_DIR", str(self.base / "bk")):
            with self.assertRaises(ValueError):
                self.helper.write_option(str(conf), "nonexistent", "k", "v")

    def test_write_option_keeps_a_backup(self):
        conf = self.base / "pjsip.conf"
        conf.write_text("[voipms-auth]\nusername = before\n")
        bk = self.base / "bk"
        with mock.patch.object(self.helper, "BACKUP_DIR", str(bk)):
            self.helper.write_option(str(conf), "voipms-auth", "username", "after")
        saved = list(bk.iterdir())
        self.assertEqual(len(saved), 1)
        self.assertIn("username = before", saved[0].read_text())

    def test_weak_and_malformed_pins_are_refused(self):
        vm = self.base / "voicemail.conf"
        vm.write_text("[default]\n100 => 1234,Orata,\n")
        with mock.patch.object(self.helper, "VOICEMAIL", str(vm)), \
             mock.patch.object(self.helper, "BACKUP_DIR", str(self.base / "bk")):
            for bad in ("1234", "0000", "abc", "12", "1" * 11):
                with self.subTest(pin=bad):
                    with self.assertRaises(ValueError):
                        self.helper.op_set_pin({"value": bad})
            self.helper.op_set_pin({"value": "80531"})
        self.assertIn("100 => 80531", vm.read_text())

    def test_audio_target_rejects_a_sink_pipewire_does_not_report(self):
        """A typo must be impossible to write, not merely discouraged.

        The old workflow was copy-paste from pw-dump, and a wrong value
        surfaced only mid-call as a missing sink.
        """
        conf = self.base / "alexa-bridge.conf"
        conf.write_text("[audio]\ntarget = bluez_output.OLD.1\n")
        with mock.patch.object(self.helper, "BRIDGE_CONF", str(conf)), \
             mock.patch.object(self.helper, "BACKUP_DIR", str(self.base / "bk")), \
             mock.patch.object(self.helper, "bt_sinks",
                               return_value=[{"name": "bluez_output.REAL.1",
                                              "desc": "Speaker"}]):
            with self.assertRaises(ValueError):
                self.helper.op_audio_target({"target": "bluez_output.TYPO.1"})
            # the config must be untouched after a refusal
            self.assertIn("bluez_output.OLD.1", conf.read_text())
            reply = self.helper.op_audio_target({"target": "bluez_output.REAL.1"})
        self.assertTrue(reply["ok"])
        self.assertIn("target = bluez_output.REAL.1", conf.read_text())

    def test_audio_target_rejects_injection_and_missing_config(self):
        conf = self.base / "alexa-bridge.conf"
        conf.write_text("[audio]\ntarget = x\n")
        with mock.patch.object(self.helper, "BRIDGE_CONF", str(conf)), \
             mock.patch.object(self.helper, "bt_sinks", return_value=[]):
            for bad in ("a\nenabled = true", "a\x00b", 12345):
                with self.subTest(value=bad):
                    with self.assertRaises(ValueError):
                        self.helper.op_audio_target({"target": bad})
        with mock.patch.object(self.helper, "BRIDGE_CONF",
                               str(self.base / "absent.conf")), \
             mock.patch.object(self.helper, "bt_sinks",
                               return_value=[{"name": "s", "desc": ""}]):
            with self.assertRaises(ValueError):
                self.helper.op_audio_target({"target": "s"})

    def test_audio_state_reports_configured_sink_presence(self):
        conf = self.base / "alexa-bridge.conf"
        conf.write_text("[audio]\ntarget = bluez_output.GONE.1\n"
                        "prompt = /nonexistent.wav\n")
        with mock.patch.object(self.helper, "BRIDGE_CONF", str(conf)), \
             mock.patch.object(self.helper, "bt_sinks",
                               return_value=[{"name": "bluez_output.HERE.1",
                                              "desc": "Other"}]):
            state = self.helper.op_audio_state({})
        self.assertEqual(state["configured"], "bluez_output.GONE.1")
        self.assertFalse(state["present"])
        self.assertFalse(state["prompt_info"].get("exists"))

    def test_audio_state_validates_the_pinned_prompt(self):
        conf = self.base / "alexa-bridge.conf"
        good = self.base / "prompt.wav"
        wav(good)  # 0.1s -- deliberately too short for the worker's 0.2s floor
        conf.write_text("[audio]\ntarget = s\nprompt = %s\n" % good)
        with mock.patch.object(self.helper, "BRIDGE_CONF", str(conf)), \
             mock.patch.object(self.helper, "bt_sinks",
                               return_value=[{"name": "s", "desc": ""}]):
            state = self.helper.op_audio_state({})
        self.assertTrue(state["prompt_info"]["exists"])
        self.assertFalse(state["prompt_info"]["valid"])
        self.assertTrue(state["present"])

    def test_unknown_op_and_field_are_refused(self):
        with self.assertRaises(ValueError):
            self.helper.op_set({"field": "../../etc/shadow", "value": "x"})
        self.assertNotIn("definitely-not-an-op", self.helper.OPS)


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