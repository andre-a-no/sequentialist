# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Novikov Laboratories LLC (Kazan, Tatarstan, Russian Federation)
"""Configuration and detectors: what is accepted, and what is refused with a message naming the place."""
import re
import tempfile
import tomllib
import unittest
from pathlib import Path

from sequentialist import DETECTORS, ConfigError, build_detector, load
from sequentialist.config import parse

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "monitors.toml"


def config(text: str):
    return parse(tomllib.loads(text), Path("/srv/seq"))


class ConfigTest(unittest.TestCase):
    def test_example_is_valid(self):
        loaded = load(EXAMPLE)
        self.assertEqual(loaded.state_dir, EXAMPLE.parent / "state")
        self.assertEqual({m.detector for m in loaded.monitors}, set(DETECTORS))

    def test_defaults(self):
        c = config('[[monitor]]\nname = "a"\ndetector = "quantile_e_detector"\nparams = {q0 = 1.0}\n')
        self.assertEqual((c.host, c.port, c.checkpoint_every, c.sync), ("127.0.0.1", 8080, 1000, "fsync"))
        self.assertEqual(c.state_dir, Path("/srv/seq/state"))
        self.assertTrue(c.monitors[0].rearm)

    def test_detector_config_round_trips_through_the_procedure(self):
        params = {"family": {"family": "gaussian", "sigma": 0.14}, "theta0": 0.2, "theta1": 0.3, "threshold": 6.9}
        procedure = build_detector("cusum", params)
        self.assertEqual(procedure.config(), {**params, "input": "x"})
        self.assertEqual(build_detector("cusum", {k: v for k, v in procedure.config().items() if k != "input"})
                         .identity(), procedure.identity())

    def test_refusals(self):
        cases = {
            '[[monitor]]\nname = "a"\ndetector = "nope"': "unknown detector 'nope'",
            '[[monitor]]\nname = "a/b"\ndetector = "cusum"': "letters, digits",
            '[[monitor]]\nname = "a"\ndetector = "quantile_e_detector"\nparams = {q0 = 1.0, colour = 1}':
                "unexpected keyword argument 'colour'",
            '[[monitor]]\nname = "a"\ndetector = "quantile_e_detector"\nparams = {q0 = 1.0, p = 2.0}':
                "p must lie in",
            '[[monitor]]\nname = "a"\ndetector = "quantile_e_detector"\nparams = {q0 = 1.0, input = "y"}':
                "remove 'input'",
            '[[monitor]]\nname = "a"\ndetector = "cusum"\nparams = {theta0 = 0.0, theta1 = 1.0, threshold = 5.0}':
                "family must be a table",
            '[[monitor]]\nname = "a"\ndetector = "cusum"\n'
            'params = {family = {family = "cauchy"}, theta0 = 0.0, theta1 = 1.0, threshold = 5.0}':
                "unknown family 'cauchy'",
            '[[monitor]]\nname = "a"\ndetector = "bayesian_change_point"\nparams = {model = {model = "normal_gamma"}}':
                "needs alarm_probability",
            '[[monitor]]\nname = "a"\ndetector = "quantile_e_detector"\nparams = {q0 = 1.0}\n'
            '[[monitor]]\nname = "a"\ndetector = "quantile_e_detector"\nparams = {q0 = 1.0}': "defined twice",
            '[[monitor]]\nname = "a"\ndetector = "cusum"\nthreshold = 3': "unknown keys ['threshold']",
            '[service]\nport = "80"': "'port' must be int",
            '[service]\nsync = "never"': "sync must be",
            '[[sink]]\ntype = "webhook"': "needs a url",
            '[[sink]]\ntype = "webhook"\nurl = "http://x"\nretries = 3': "unknown keys ['retries']",
            '[[sink]]\ntype = "pager"': "unknown type 'pager'",
            "[servce]": "unknown keys ['servce']",
        }
        for text, message in cases.items():
            with self.subTest(text=text), self.assertRaisesRegex(ConfigError, re.escape(message)):
                config(text)

    def test_load_names_the_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.toml"
            path.write_text("[service\n")
            with self.assertRaisesRegex(ConfigError, "bad.toml"):
                load(path)
            with self.assertRaisesRegex(ConfigError, "missing.toml"):
                load(Path(tmp) / "missing.toml")


if __name__ == "__main__":
    unittest.main()
