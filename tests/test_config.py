"""Offline tests for the settings file (config.py) and the example settings."""
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import _env  # noqa: E402,F401  (settings for the tests)
import config  # noqa: E402

ROOT = os.path.dirname(HERE)


def write(text):
    f = tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False)
    f.write(text)
    f.close()
    return f.name


class Settings(unittest.TestCase):
    def test_defaults_without_a_file(self):
        cfg = config.load(write(""))
        self.assertEqual(cfg["linear"]["workspaces"], [])
        self.assertEqual(cfg["server"]["host"], "127.0.0.1")
        self.assertFalse(cfg["remote"]["enabled"])

    def test_example_file_loads(self):
        cfg = config.load(os.path.join(ROOT, "easel.example.toml"))
        self.assertTrue(cfg["linear"]["workspaces"])
        for ws in cfg["linear"]["workspaces"]:
            self.assertIsInstance(ws["key_cmd"], list)

    def test_a_key_in_the_file_is_refused(self):
        for bad in ('[[linear.workspaces]]\nname = "a"\nkey_cmd = ["x"]\nkey = "lin_api_x"\n',
                    '[[linear.workspaces]]\nname = "a"\nkey_cmd = "pass show x"\n',
                    '[[linear.workspaces]]\nkey_cmd = ["x"]\n',
                    '[github]\norgs = ["a b"]\n',
                    '[time]\nwork_start = "25:00"\n'):
            with self.assertRaises(ValueError, msg=bad):
                config.load(write(bad))

    def test_test_settings_are_in_use(self):
        self.assertEqual(config.get()["branding"]["owner"], "Sam")
        self.assertTrue(config.find().endswith("easel.test.toml"))
