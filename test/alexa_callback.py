#!/usr/bin/env python3
"""Isolated acoustic callback tests: no live CLI, playback, SIP or Amazon."""
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import unittest
from unittest import mock
import wave

ROOT = Path(__file__).resolve().parents[1]


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / "bin" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bridge = load("callback_bridge", "orata-alexa-bridge.py")
worker = load("audio_worker", "orata-audio-worker.py")


class BridgeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix=".callback-test-", dir=ROOT / "test")
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.path = self.base / "config"
        self.path.write_text(
            "[bridge]\nenabled=true\nacknowledge_spoofable_callback=true\n"
            "callback_caller_id=15551230002\ndestination=15551230003\n"
            "allowed_callers=15551230001\nstate_dir=%s\n"
            "callback_window=30\ncall_limit=900\ncooldown=60\n"
            % (self.base / "state"))
        self.cfg = bridge.Config(self.path)
        self.store = bridge.Store(self.cfg)

    def reserve(self, armed=True):
        session = self.store.reserve("15551230001", self.cfg.did, "PJSIP/original", now=100)
        self.assertIsNotNone(session)
        if armed:
            self.store.arm(session["token"], now=101)
        return session

    def test_disabled_and_unlisted_calls_keep_normal_routing(self):
        self.assertIsNone(self.store.reserve("15551239999", self.cfg.did, "other", now=100))
        self.assertEqual(self.store.claim("15551239999", self.cfg.did, "other", now=100),
                         ("NORMAL", None))
        self.cfg.enabled = False
        self.assertIsNone(self.store.reserve("15551230001", self.cfg.did, "original", now=100))
        self.assertEqual(self.store.claim(self.cfg.callback, self.cfg.did, "echo", now=100),
                         ("NORMAL", None))

    def test_config_requires_acknowledgement_and_distinct_exact_numbers(self):
        text = self.path.read_text()
        for bad in (
            text.replace("acknowledge_spoofable_callback=true", "acknowledge_spoofable_callback=false"),
            text.replace("allowed_callers=15551230001", "allowed_callers=15551230002"),
            text.replace("allowed_callers=15551230001", "allowed_callers=*"),
            text.replace("callback_window=30", "callback_window=600"),
        ):
            self.path.write_text(bad)
            with self.assertRaises(ValueError):
                bridge.Config(self.path)

    def test_callback_cannot_trigger_and_unmatched_callback_is_rejected(self):
        self.assertIsNone(self.store.reserve(self.cfg.callback, self.cfg.did, "echo", now=100))
        self.assertEqual(self.store.claim(self.cfg.callback, self.cfg.did, "echo", now=100),
                         ("REJECT", None))

    def test_destination_and_readiness_are_required(self):
        self.assertIsNone(self.store.reserve("15551230001", "other-did", "original", now=100))
        session = self.reserve(armed=False)
        self.assertEqual(self.store.claim(self.cfg.callback, self.cfg.did, "echo", now=100),
                         ("REJECT", None))
        self.store.arm(session["token"], now=101)
        self.assertEqual(self.store.claim(self.cfg.callback, "other-did", "echo", now=102),
                         ("REJECT", None))

    def test_one_original_and_one_callback(self):
        session = self.reserve()
        self.assertIsNone(self.store.reserve("15551230001", self.cfg.did, "second", now=102))
        outcome, claimed = self.store.claim(self.cfg.callback, self.cfg.did, "echo", now=102)
        self.assertEqual(outcome, "CALLBACK")
        self.assertEqual(claimed["token"], session["token"])
        self.assertEqual(self.store.claim(self.cfg.callback, self.cfg.did, "duplicate", now=103),
                         ("REJECT", None))

    def test_deadline_and_cancelled_callback_rejection(self):
        session = self.reserve()
        self.assertEqual(self.store.claim(self.cfg.callback, self.cfg.did, "late", now=131),
                         ("REJECT", None))
        self.store.stop(session["token"], "cancelled")
        self.assertEqual(self.store.claim(self.cfg.callback, self.cfg.did, "echo", now=102),
                         ("REJECT", None))

    def test_simultaneous_callbacks_have_exactly_one_winner(self):
        self.reserve()
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(
                lambda n: self.store.claim(self.cfg.callback, self.cfg.did, "echo-%d" % n, now=102),
                range(8)))
        self.assertEqual(sum(action == "CALLBACK" for action, _ in results), 1)

    def test_cooldown_and_old_hangup_handler_do_not_clear_new_session(self):
        session = self.reserve()
        self.store.release(session["token"], now=110)
        self.assertIsNone(self.store.reserve("15551230001", self.cfg.did, "new", now=120))
        second = self.store.reserve("15551230001", self.cfg.did, "new", now=171)
        self.assertIsNotNone(second)
        self.assertIsNone(self.store.release(session["token"], now=172))
        self.assertEqual(self.store.snapshot()["token"], second["token"])

    def test_connected_call_never_falls_back_after_departure(self):
        session = self.reserve()
        self.store.claim(self.cfg.callback, self.cfg.did, "echo", now=102)
        self.store.connected(session["token"])
        stopped = self.store.stop(session["token"], "callback-hangup")
        self.assertEqual(stopped["phase"], "ended")
        self.assertTrue(self.store.release(session["token"])["connected"])

    def test_lost_watcher_requires_cooldown_before_reuse(self):
        session = self.reserve()
        now = session["expires"] + 1
        self.assertIsNone(self.store.reserve("15551230001", self.cfg.did, "new", now=now))
        self.assertIsNotNone(self.store.reserve("15551230001", self.cfg.did, "new", now=now + 61))

    def test_channel_parser_and_command_injection_guard(self):
        with mock.patch.object(bridge, "cli", return_value=
                               "PJSIP/original!ctx!s!1!Up!ConfBridge!orata-ab-test,,!cid\n"):
            self.assertTrue(bridge.in_room(bridge.channels(), "PJSIP/original", "orata-ab-test"))
        with mock.patch.object(bridge, "cli") as cli:
            bridge.teardown({"room": "bad\ncommand", "callback": "bad\ncommand"})
        cli.assert_not_called()

    def test_watch_connects_then_tears_down_on_callback_departure(self):
        session = self.reserve(armed=False)
        original = {"PJSIP/original": ("ConfBridge", session["room"])}
        both = dict(original, echo=("ConfBridge", session["room"]))
        sock = mock.Mock()
        sock.recv.return_value = b"OK\n"

        def arm(token):
            result = bridge.Store.arm(self.store, token, now=101)
            self.store.claim(self.cfg.callback, self.cfg.did, "echo", now=102)
            return result

        with mock.patch.object(self.store, "arm", side_effect=arm), \
             mock.patch.object(bridge, "channels", side_effect=[original, both, original]), \
             mock.patch.object(bridge.socket, "socket", return_value=sock), \
             mock.patch.object(bridge, "teardown") as teardown, \
             mock.patch.object(bridge.time, "time", return_value=105), \
             mock.patch.object(bridge.time, "sleep"):
            bridge.watch(self.cfg, self.store, session["token"])
        self.assertTrue(self.store.snapshot()["connected"])
        self.assertEqual(self.store.snapshot()["phase"], "ended")
        teardown.assert_called_once()
        sock.sendall.assert_called_once_with(b"PLAY\n")

    def test_watch_timeout_audio_failure_and_caller_departure(self):
        for case in ("timeout", "audio-failure", "caller-left"):
            with self.subTest(case=case):
                self.store.path.unlink(missing_ok=True)
                session = self.reserve(armed=False)
                original = {"PJSIP/original": ("ConfBridge", session["room"])}
                sock = mock.Mock()
                sock.recv.return_value = b"FAIL\n" if case == "audio-failure" else b"OK\n"
                with mock.patch.object(self.store, "arm",
                                       side_effect=lambda token: bridge.Store.arm(self.store, token, now=101)), \
                     mock.patch.object(bridge, "channels",
                                       side_effect=[original, {} if case == "caller-left" else original]), \
                     mock.patch.object(bridge.socket, "socket", return_value=sock), \
                     mock.patch.object(bridge, "teardown") as teardown, \
                     mock.patch.object(bridge.time, "time", return_value=132):
                    bridge.watch(self.cfg, self.store, session["token"])
                self.assertEqual(self.store.snapshot()["phase"], "fallback")
                teardown.assert_called_once()
                sock.close.assert_called()

    def test_disabled_agi_creates_no_state_or_live_actions(self):
        self.path.write_text("[bridge]\nenabled=false\nstate_dir=%s\n" % (self.base / "unused"))
        result = subprocess.run(
            ["python3", str(ROOT / "bin/orata-alexa-bridge.py"), "route"],
            input="agi_callerid: 15551230002\nagi_channel: fake\n\n" + "200 result=1\n" * 8,
            env=dict(os.environ, ORATA_BRIDGE_CONF=str(self.path)),
            capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('SET VARIABLE AB_ACTION "NORMAL"', result.stdout)
        self.assertFalse((self.base / "unused").exists())

    def invoke_agi(self, action, env, *args):
        """Exercise real main()/protocol/state, with all live effects mocked."""
        source = "".join("%s: %s\n" % item for item in env.items())
        source += "\n" + "200 result=1\n" * 16
        output = io.StringIO()
        with mock.patch.dict(os.environ, {"ORATA_BRIDGE_CONF": str(self.path)}), \
             mock.patch.object(bridge.sys, "argv", ["bridge", action, *args]), \
             mock.patch.object(bridge.sys, "stdin", io.StringIO(source)), \
             mock.patch.object(bridge.sys, "stdout", output), \
             mock.patch.object(bridge.subprocess, "Popen") as spawn, \
             mock.patch.object(bridge, "cli", return_value="") as cli:
            bridge.main()
        return output.getvalue(), spawn, cli

    def test_enabled_agi_offer_claim_and_result_with_gosub_did(self):
        with self.path.open("a") as fh:
            fh.write("log=%s\n" % (self.base / "watch.log"))
        output, spawn, _ = self.invoke_agi("offer", {
            "agi_extension": "s", "agi_callerid": "15551230001",
            "agi_channel": "PJSIP/original", "agi_arg_2": self.cfg.did,
        }, self.cfg.did)
        self.assertIn('SET VARIABLE AB_ACTION "WAIT"', output)
        session = self.store.snapshot()
        self.assertTrue(session)
        self.assertIn('SET VARIABLE AB_ROOM "%s"' % session["room"], output)
        spawn.assert_called_once()
        self.assertEqual(spawn.call_args.args[0][-2:], ["watch", session["token"]])
        self.assertIsNotNone(spawn.call_args.kwargs["stderr"])

        self.store.arm(session["token"])
        output, spawn, cli = self.invoke_agi("route", {
            "agi_extension": self.cfg.did, "agi_callerid": self.cfg.callback,
            "agi_channel": "PJSIP/echo",
        })
        self.assertIn('SET VARIABLE AB_ACTION "CALLBACK"', output)
        spawn.assert_not_called()
        cli.assert_not_called()
        self.assertEqual(self.store.snapshot()["callback"], "PJSIP/echo")
        self.store.connected(session["token"])

        output, _, cli = self.invoke_agi("result", {
            "agi_extension": "s", "agi_arg_2": session["token"],
        }, session["token"])
        self.assertIn('SET VARIABLE AB_RESULT "CONNECTED"', output)
        self.assertEqual(self.store.snapshot(), {})
        self.assertTrue(any("confbridge kick" in call.args[0] for call in cli.call_args_list))

    def test_enabled_agi_missing_did_never_plays(self):
        for args in ({}, {"agi_arg_2": "15551239999"}):
            with self.subTest(args=args):
                output, spawn, cli = self.invoke_agi("offer", {
                    "agi_extension": "s", "agi_callerid": "15551230001",
                    "agi_channel": "PJSIP/original", **args,
                })
                self.assertNotIn('SET VARIABLE AB_ACTION "WAIT"', output)
                self.assertEqual(self.store.snapshot(), {})
                spawn.assert_not_called()
                cli.assert_not_called()

    def test_enabled_callback_with_corrupt_state_fails_closed(self):
        self.store.path.write_text("{broken")
        output, spawn, cli = self.invoke_agi("route", {
            "agi_extension": self.cfg.did, "agi_callerid": self.cfg.callback,
            "agi_channel": "PJSIP/echo",
        })
        self.assertTrue(output.rstrip().endswith('SET VARIABLE AB_ACTION "REJECT"'))
        spawn.assert_not_called()
        cli.assert_not_called()

    def test_enabled_agi_hangup_cleanup_releases_and_kicks(self):
        session = self.reserve()
        output, spawn, cli = self.invoke_agi("cleanup", {
            "agi_extension": "s", "agi_arg_2": session["token"], "agi_arg_3": "caller",
        }, session["token"], "caller")
        self.assertEqual(self.store.snapshot(), {})
        cli.assert_called_once_with("confbridge kick %s all" % session["room"])
        spawn.assert_not_called()

    def test_dialplan_preserves_block_and_gate_order_and_default_off(self):
        dp = (ROOT / "asterisk/extensions.conf").read_text()
        inbound = dp.split("[from-voipms]", 1)[1].split("[orata-record]", 1)[0]
        self.assertLess(inbound.index("DB(block/"), inbound.index("${ALEXABRIDGE},route"))
        self.assertLess(inbound.index("${ALEXABRIDGE},route"), inbound.index("DB(owner/"))
        self.assertLess(inbound.index("Gosub(orata-alexa-offer"), inbound.index("System(${ANNOUNCE}"))
        self.assertIn("ALEXABRIDGE_ENABLED=0", dp)
        self.assertIn("Gosub(orata-alexa-offer,s,1(${EXTEN}))", inbound)
        contexts = (ROOT / "asterisk/alexa-callback.conf").read_text()
        self.assertIn("AGI(${ALEXABRIDGE},offer,${ARG1})", contexts)
        self.assertIn("CONFBRIDGE(bridge,max_members)=2", contexts)
        self.assertIn("CONFBRIDGE(bridge,record_conference)=no", contexts)
        self.assertIn("CONFBRIDGE(user,timeout)=${AB_LIMIT}", contexts)
        self.assertNotIn("System(", contexts)


class AudioTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix=".audio-test-", dir=ROOT / "test")
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "command.wav"
        with wave.open(str(self.path), "wb") as fh:
            fh.setnchannels(1)
            fh.setsampwidth(2)
            fh.setframerate(8000)
            fh.writeframes(b"\0\0" * 8000)
        self.cfg = {"prompt": str(self.path), "target": "bluez_output.test"}

    def test_fixed_sink_no_shell_and_no_default_fallback(self):
        nodes = [{"info": {"props": {"node.name": self.cfg["target"], "media.class": "Audio/Sink"}}}]
        with mock.patch.object(worker.subprocess, "run",
                               return_value=subprocess.CompletedProcess([], 0, stdout=json.dumps(nodes))):
            self.assertTrue(worker.sink_ready(self.cfg["target"]))
        with mock.patch.object(worker, "sink_ready", return_value=False), \
             mock.patch.object(worker.subprocess, "Popen") as popen:
            with self.assertRaises(RuntimeError):
                worker.play(mock.Mock(), self.cfg)
        popen.assert_not_called()

    def test_playback_arguments_and_disconnect_cancellation(self):
        process = mock.Mock()
        process.poll.side_effect = [None, None]
        with mock.patch.object(worker, "sink_ready", return_value=True), \
             mock.patch.object(worker.subprocess, "Popen", return_value=process) as popen, \
             mock.patch.object(worker.select, "select", return_value=([1], [], [])):
            with self.assertRaises(RuntimeError):
                worker.play(mock.Mock(), self.cfg)
        self.assertEqual(popen.call_args.args[0],
                         ["/usr/bin/pw-play", "--target=bluez_output.test", str(self.path)])
        self.assertNotIn("shell", popen.call_args.kwargs)
        process.terminate.assert_called_once()

    def test_uid_and_fixed_request_validation(self):
        for payload, allowed, response in (
            (b"PLAY\n", set(), b"DENIED\n"),
            (b"/tmp/evil.wav\n", {os.getuid()}, b"INVALID\n"),
            (b"PLAY\n", {os.getuid()}, b"OK\n"),
        ):
            with self.subTest(payload=payload, allowed=allowed):
                server, client = socket.socketpair()
                with client, mock.patch.object(worker, "play") as play:
                    client.sendall(payload)
                    worker.handle(server, self.cfg, allowed)
                    self.assertEqual(client.recv(100), response)
                self.assertEqual(play.call_count, int(response == b"OK\n"))

    def test_busy_worker_does_not_overlap_playback(self):
        server, client = socket.socketpair()
        worker.LOCK.acquire()
        try:
            with client, mock.patch.object(worker, "play") as play:
                client.sendall(b"PLAY\n")
                worker.handle(server, self.cfg, {os.getuid()})
                self.assertEqual(client.recv(100), b"BUSY\n")
                play.assert_not_called()
        finally:
            worker.LOCK.release()


if __name__ == "__main__":
    unittest.main(verbosity=2)