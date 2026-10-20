# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Novikov Laboratories LLC (Kazan, Tatarstan, Russian Federation)
"""The service over HTTP, alert sinks, and the command line."""
import contextlib
import io
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from sequentialist import Service
from sequentialist.alerts import Alert, JsonlSink, WebhookSink
from sequentialist.api import make_server
from sequentialist.cli import main
from sequentialist.config import parse

from .test_monitor import DETECTOR, latencies

CONFIG = {
    "service": {"checkpoint_every": 100, "sync": "flush"},
    "monitor": [
        {"name": DETECTOR.name, "detector": DETECTOR.detector, "params": DETECTOR.params},
        {"name": "once", "detector": DETECTOR.detector, "params": DETECTOR.params, "rearm": False},
    ],
}


class Collect(list):
    def __call__(self, alert):
        self.append(alert)


class ApiTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.sink = Collect()
        self.service = Service(parse(CONFIG, Path(tmp.name)), sinks=[self.sink])
        self.server = make_server(self.service, "127.0.0.1", 0)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.service.close)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def request(self, path, body=None):
        data = body if isinstance(body, bytes) or body is None else json.dumps(body).encode()
        try:
            with urllib.request.urlopen(urllib.request.Request(self.url + path, data=data)) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            return error.code, json.load(error)

    def test_values_in_alerts_out(self):
        status, body = self.request("/healthz")
        self.assertEqual((status, body["status"]), (200, "ok"))
        values = latencies()
        status, body = self.request("/v1/monitors/latency/observations", {"values": values[:400]})
        self.assertEqual((status, body), (200, {"accepted": 400, "invalid": 0, "alerts": []}))
        status, body = self.request("/v1/monitors/latency/observations",
                                    {"observations": [{"value": v, "time": float(i)} for i, v in enumerate(values)]})
        self.assertEqual(status, 200)
        self.assertGreaterEqual(len(body["alerts"]), 1)
        self.assertEqual([a.id for a in self.sink], [a["id"] for a in body["alerts"]])
        status, body = self.request("/v1/monitors/latency")
        self.assertEqual((status, body["state"], body["next_seq"]), (200, "running", 1200))
        self.assertEqual(body["detector"]["name"], "mean_e_detector")
        status, body = self.request("/v1/monitors/latency/alerts?limit=1")
        self.assertEqual((status, [a["id"] for a in body["alerts"]]), (200, [self.sink[-1].id]))
        status, body = self.request("/v1/monitors")
        self.assertEqual([m["name"] for m in body["monitors"]], ["latency", "once"])

    def test_errors(self):
        cases = [
            ("/v1/monitors/nope", None, 404),
            ("/v1/monitors/nope/observations", {"values": [1]}, 404),
            ("/v1/elsewhere", {"values": [1]}, 404),
            ("/v1/monitors/latency/observations", b"{", 400),
            ("/v1/monitors/latency/observations", b'{"values": [NaN]}', 400),
            ("/v1/monitors/latency/observations", {"values": 1}, 400),
            ("/v1/monitors/latency/observations", {"observations": [{"v": 1}]}, 400),
            ("/v1/monitors/latency/observations", {"values": [], "extra": 1}, 400),
            ("/v1/monitors/latency/alerts?limit=x", None, 400),
        ]
        for path, body, code in cases:
            with self.subTest(path=path, body=body):
                status, answer = self.request(path, body)
                self.assertEqual(status, code)
                self.assertIn("error", answer)

    def test_stopped_monitor_answers_409(self):
        status, body = self.request("/v1/monitors/once/observations", {"values": latencies()})
        self.assertEqual((status, len(body["alerts"])), (200, 1))
        status, body = self.request("/v1/monitors/once/observations", {"values": [0.2]})
        self.assertEqual(status, 409)


class SinkTest(unittest.TestCase):
    alert = Alert("m/0@5", "m", "m/0", 0, 5, 5, None, "cusum", {"statistic": 9.0}, 0.0)

    def test_webhook_retries_then_delivers(self):
        calls = []

        def post(url, body, headers, timeout):
            calls.append(json.loads(body))
            if len(calls) < 2:
                raise OSError("connection refused")

        sink = WebhookSink("http://hook", post=post, attempts=3)
        sink(self.alert)
        sink.close()
        self.assertEqual([c["id"] for c in calls], ["m/0@5", "m/0@5"])

    def test_jsonl(self):
        with tempfile.TemporaryDirectory() as tmp:
            sink = JsonlSink(f"{tmp}/a/alerts.jsonl")
            sink(self.alert)
            sink.close()
            self.assertEqual(json.loads(Path(f"{tmp}/a/alerts.jsonl").read_text())["detector"], "cusum")


class CliTest(unittest.TestCase):
    def test_check_and_replay(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "monitors.toml"
            config.write_text(
                '[[monitor]]\nname = "latency"\ndetector = "mean_e_detector"\n'
                'params = {m = 0.2, arl = 1000, direction = "increase", statistic = "cusum"}\n'
            )
            data = Path(tmp) / "latency.csv"
            data.write_text("seconds,host\n" + "".join(f"{v},a\n" for v in latencies()))
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                self.assertEqual(main(["check", str(config)]), 0)
                self.assertEqual(main(["--log-level", "ERROR", "replay", str(config), "latency", str(data)]), 0)
                self.assertEqual(main(["replay", str(config), "other", str(data)]), 2)
            lines = out.getvalue().splitlines()
            self.assertIn("latency: mean_e_detector (exact)", lines[1])
            alarms = [json.loads(line) for line in lines[2:]]
            self.assertGreater(alarms[0]["seq"], 400)
            self.assertIn(f"800 values, {len(alarms)} alarms", err.getvalue())
            self.assertIn("no monitor 'other'", err.getvalue())
            self.assertFalse((Path(tmp) / "state").exists())  # replay leaves no state behind


if __name__ == "__main__":
    unittest.main()
