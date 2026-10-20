import contextlib
import io
import unittest

from scripts.configure_model import parse_args


class ConfigureModelArgumentTests(unittest.TestCase):
    def test_reasoning_effort_is_opt_in_and_responses_only(self):
        base = ["--base-url", "https://provider.example/v1", "--model", "model-id"]
        self.assertIsNone(parse_args(base).reasoning_effort)
        self.assertEqual(parse_args(base + ["--reasoning-effort", "low"]).reasoning_effort, "low")

        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parse_args(base + ["--protocol", "messages", "--reasoning-effort", "low"])


if __name__ == "__main__":
    unittest.main()
