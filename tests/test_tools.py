import json
import unittest

from agent.tools import (
    MAX_ARGUMENT_CHARS,
    MAX_RESULT_BYTES,
    ToolError,
    ToolRegistry,
    calculator,
    make_default_registry,
    read_docs,
)


class ToolRegistryTests(unittest.TestCase):
    def setUp(self):
        self.registry = make_default_registry()

    def execute(self, name, **arguments):
        return self.registry.execute(name, json.dumps(arguments))

    def assertError(self, result, code):
        self.assertEqual(result["ok"], False)
        self.assertEqual(result["error"]["code"], code)
        json.dumps(result, allow_nan=False)

    def test_schema_names_and_copies(self):
        schemas = self.registry.schemas()
        self.assertEqual([item["function"]["name"] for item in schemas], ["calculator", "search", "read_docs"])
        for schema in schemas:
            self.assertEqual(schema["type"], "function")
            self.assertFalse(schema["function"]["parameters"]["additionalProperties"])
        schemas[0]["function"]["parameters"]["properties"]["expression"]["type"] = "number"
        self.assertTrue(self.execute("calculator", expression="2+3")["ok"])

    def test_missing_extra_and_wrong_types_are_rejected(self):
        cases = [
            ("calculator", {}),
            ("calculator", {"expression": 123}),
            ("calculator", {"expression": True}),
            ("calculator", {"expression": None}),
            ("calculator", {"expression": "1+1", "secret": "extra"}),
            ("search", {"query": []}),
            ("search", {"query": ""}),
            ("search", {"query": "a" * 201}),
            ("read_docs", {"document_id": "../secrets"}),
        ]
        for name, arguments in cases:
            with self.subTest(name=name, arguments=arguments):
                self.assertError(self.registry.execute(name, json.dumps(arguments)), "invalid_arguments")

    def test_unknown_tool(self):
        self.assertError(self.registry.execute("os.system", "{}"), "unknown_tool")
        self.assertError(self.registry.execute([], "{}"), "unknown_tool")

    def test_invalid_json(self):
        for arguments in ["{", "not json", "{'expression':'2'}", '{"expression":NaN}', '{"expression":1e999}', '{"expression":"1","expression":"2"}', None]:
            with self.subTest(arguments=arguments):
                self.assertError(self.registry.execute("calculator", arguments), "invalid_json")
        for arguments in ["[]", "null", '"text"', "123"]:
            with self.subTest(arguments=arguments):
                self.assertError(self.registry.execute("calculator", arguments), "invalid_arguments")

    def test_deep_and_large_json_do_not_escape(self):
        # Python versions differ in their JSON parser's recursion limit. Either
        # decoding or schema validation must reject this input without escaping.
        result = self.registry.execute("calculator", "[" * 1500 + "]" * 1500)
        self.assertFalse(result["ok"])
        self.assertIn(result["error"]["code"], ("invalid_json", "invalid_arguments"))
        self.assertError(self.registry.execute("calculator", " " * (MAX_ARGUMENT_CHARS + 1)), "arguments_too_large")

    def test_calculator_arithmetic(self):
        for expression, value in [("(12 + 3) * 2", 30), ("-2**2", -4), ("2**-3", 0.125), ("7//2 + 7%2", 4), ("1.5/2", 0.75), (" +2 ", 2)]:
            with self.subTest(expression=expression):
                result = self.execute("calculator", expression=expression)
                self.assertTrue(result["ok"])
                self.assertEqual(result["data"]["value"], value)

    def test_calculator_rejects_python_execution(self):
        for expression in ["__import__('os').system('echo bad')", "(1).__class__", "[x for x in []]", "open('secret')", "True + 1", "'x' * 3", "1 << 8", "x+1", "lambda:1", "1j", "2 < 3"]:
            with self.subTest(expression=expression):
                self.assertError(self.execute("calculator", expression=expression), "invalid_expression")

    def test_calculator_limits(self):
        for expression in ["2**1000", "10**(10**10)", "1e101", "+" * 20 + "1", "+".join("1" for _ in range(30))]:
            with self.subTest(expression=expression):
                self.assertError(self.execute("calculator", expression=expression), "calculation_limit")
        self.assertError(self.execute("calculator", expression="1" * 513), "invalid_arguments")
        with self.assertRaises(ToolError):
            calculator("1" * 513)

    def test_calculator_undefined_and_non_finite(self):
        for expression in ["1/0", "1//0", "1%0", "0**-1", "1e999", "(-1)**0.5", "(1e99)**100"]:
            with self.subTest(expression=expression):
                self.assertError(self.execute("calculator", expression=expression), "arithmetic_error")

    def test_search_is_explicitly_mock_and_supports_followup(self):
        result = self.execute("search", query="会话隔离")
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["source"], "mock-local-corpus")
        document_id = result["data"]["results"][0]["document_id"]
        self.assertEqual(document_id, "session")
        document = self.execute("read_docs", document_id=document_id)
        self.assertTrue(document["ok"])
        self.assertIn("user_id", document["data"]["content"])
        self.assertEqual(self.execute("search", query="nonexistentxyz")["data"]["results"], [])
        self.assertError(self.execute("search", query="  "), "invalid_query")

    def test_docs_allowlist(self):
        for document_id in ["runtime", "session", "memory"]:
            self.assertTrue(self.execute("read_docs", document_id=document_id)["ok"])
        for document_id in ["../../.env", "C:\\Users\\secret", "session.json"]:
            with self.subTest(document_id=document_id):
                with self.assertRaises(ToolError) as caught:
                    read_docs(document_id)
                self.assertEqual(caught.exception.code, "document_not_found")

    def register_handler(self, handler):
        registry = ToolRegistry()
        registry.register("test_tool", "Test result boundaries", {"type": "object", "properties": {}, "additionalProperties": False}, handler)
        return registry

    def test_exception_does_not_expose_upstream_details(self):
        def handler():
            raise RuntimeError("upstream api_key=secret_token")
        result = self.register_handler(handler).execute("test_tool", "{}")
        self.assertError(result, "tool_execution_failed")
        self.assertNotIn("secret_token", json.dumps(result))

    def test_invalid_outputs_never_break_runtime(self):
        circular = []
        circular.append(circular)
        for value in [object(), {1, 2}, float("nan"), float("inf"), circular, "x" * (MAX_RESULT_BYTES + 1), "\ud800"]:
            with self.subTest(type=type(value).__name__):
                registry = self.register_handler(lambda value=value: value)
                self.assertError(registry.execute("test_tool", "{}"), "invalid_tool_result")

    def test_json_normalizes_handler_result(self):
        result = self.register_handler(lambda: {"values": (1, 2)}).execute("test_tool", "{}")
        self.assertEqual(result, {"ok": True, "data": {"values": [1, 2]}})

    def test_duplicate_registration_and_loose_schema_fail_early(self):
        with self.assertRaises(ValueError):
            self.registry.register("calculator", "Duplicate", {"type": "object", "additionalProperties": False}, lambda: None)
        with self.assertRaises(ValueError):
            self.registry.register("loose", "Loose schema", {"type": "object"}, lambda: None)


if __name__ == "__main__":
    unittest.main()
