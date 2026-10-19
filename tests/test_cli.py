"""CLI failure boundaries using isolated temporary data and the offline model."""
import io
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from agent.__main__ import main
from agent.memory import SessionStore


class CliTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name)

    def run_cli(self, *arguments, inputs=None):
        output = io.StringIO()
        argv = ["agent", "--demo", "--data-dir", str(self.path), *arguments]
        input_effect = inputs if inputs is not None else AssertionError("Unexpected interactive prompt")
        with patch.object(sys, "argv", argv), patch("builtins.input", side_effect=input_effect), redirect_stdout(output):
            code = main()
        return code, output.getvalue()

    def test_corrupt_database_has_controlled_exit_and_is_not_overwritten(self):
        database = self.path / "demo" / "sessions.sqlite3"
        database.parent.mkdir()
        original = b"synthetic damaged database; preserve these bytes"
        database.write_bytes(original)
        code, output = self.run_cli("--ask", "hello")
        self.assertEqual(code, 2)
        self.assertIn("会话数据库无法打开", output)
        self.assertNotIn("Traceback", output)
        self.assertEqual(database.read_bytes(), original)

    def test_session_list_database_error_allows_the_next_question(self):
        private_detail = "synthetic internal database detail"
        with patch.object(SessionStore, "list_sessions", side_effect=sqlite3.OperationalError(private_detail)):
            code, output = self.run_cli(inputs=["/sessions", "计算 7+8", "/quit"])
        self.assertEqual(code, 0)
        self.assertIn("暂时无法读取会话列表", output)
        self.assertNotIn(private_detail, output)
        self.assertIn("[completed]", output)
        self.assertIn('"value":15', output)

    def test_explicit_empty_ask_is_invalid_without_interactive_or_model_call(self):
        with patch("agent.__main__.DemoModel.complete") as complete:
            code, output = self.run_cli("--ask", "")
        self.assertEqual(code, 1)
        self.assertIn("invalid_input", output)
        complete.assert_not_called()


if __name__ == "__main__":
    unittest.main()
