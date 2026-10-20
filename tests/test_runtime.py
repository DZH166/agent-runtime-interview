import copy
import json
import tempfile
import threading
import unittest
from unittest.mock import patch
from pathlib import Path

from agent.memory import MemoryPolicy, Session, SessionConflict, SessionStore
from agent.models import ModelError, ModelReply, ToolCall
from agent.runtime import Runtime, SYSTEM_PROMPT
from agent.tools import make_default_registry
from agent.trace import Trace


def call(name="calculator", arguments='{"expression":"7+8"}', id="calc-1"):
    return ModelReply("我先调用工具。", [ToolCall(id, name, arguments)])


class ScriptedModel:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.requests = []

    def complete(self, messages, tools):
        self.requests.append(copy.deepcopy(messages))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply(messages) if callable(reply) else reply


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.store = SessionStore(self.path / "sessions.db")

    def runtime(self, model, **kwargs):
        return Runtime(model, make_default_registry(), self.store,
                       trace=Trace(self.path / "trace.jsonl"), **kwargs)

    def test_direct_answer_and_persisted_followup(self):
        first = self.runtime(ScriptedModel(ModelReply("好的，小林。")))
        self.assertEqual(first.ask("u", "a", "我叫小林").status, "completed")
        model = ScriptedModel(lambda m: ModelReply("小林" if any(x.get("content") == "我叫小林" for x in m) else "未知"))
        reloaded = self.runtime(model)
        self.assertEqual(reloaded.ask("u", "a", "我叫什么").answer, "小林")
        self.assertEqual(len(self.store.load("u", "a").turns), 2)

    def test_same_user_windows_and_different_users_are_isolated(self):
        runtime = self.runtime(ScriptedModel(ModelReply("已记住"), ModelReply("窗口二"), ModelReply("其他用户"), ModelReply("切回")))
        runtime.ask("u", "a", "secret-in-a")
        runtime.ask("u", "b", "second")
        runtime.ask("v", "a", "third")
        runtime.ask("u", "a", "continue")
        requests = runtime.model.requests
        self.assertNotIn("secret-in-a", json.dumps(requests[1]))
        self.assertNotIn("secret-in-a", json.dumps(requests[2]))
        self.assertIn("secret-in-a", json.dumps(requests[3]))

    def test_tool_result_and_followup(self):
        model = ScriptedModel(call(), ModelReply("15"), call(arguments='{"expression":"15+10"}', id="calc-2"), ModelReply("25"))
        runtime = self.runtime(model)
        first = runtime.ask("u", "a", "算7+8")
        self.assertEqual((first.model_calls, first.tool_calls, first.answer), (2, 1, "15"))
        tool = model.requests[1][-1]
        self.assertEqual(tool["role"], "tool")
        self.assertEqual(tool["tool_call_id"], "calc-1")
        self.assertTrue(json.loads(tool["content"])["ok"])
        self.assertEqual(runtime.ask("u", "a", "再加10").answer, "25")
        self.assertIn(tool, model.requests[2])

    def test_multiple_tools_in_one_response_and_next_step(self):
        model = ScriptedModel(ModelReply(None, [ToolCall("a", "search", '{"query":"Agent"}'),
                                               ToolCall("b", "read_docs", '{"document_id":"session"}')]),
                              call(), ModelReply("完成"))
        result = self.runtime(model).ask("u", "a", "综合任务")
        self.assertEqual((result.status, result.tool_calls, result.model_calls), ("completed", 3, 3))
        self.assertEqual([m["tool_call_id"] for m in model.requests[1] if m["role"] == "tool"], ["a", "b"])

    def test_invalid_tool_can_be_corrected(self):
        for index, bad in enumerate((call(arguments="{"), call(arguments='{"expression":5}'), call(name="missing"), call(arguments='{"expression":"1/0"}'))):
            with self.subTest(bad=bad):
                model = ScriptedModel(bad, call(id="fixed"), ModelReply("已修正"))
                result = self.runtime(model).ask("u", f"errors-{index}", "测试")
                self.assertEqual(result.status, "completed")
                self.assertFalse(json.loads(model.requests[1][-1]["content"])["ok"])
                self.assertTrue(json.loads(model.requests[2][-1]["content"])["ok"])

    def test_model_error_after_tool_does_not_persist_incomplete_turn(self):
        model = ScriptedModel(ModelReply("之前已完成"), call(), ModelError("timeout", "raw-private-error"))
        runtime = self.runtime(model)
        self.assertEqual(runtime.ask("u", "a", "旧问题").status, "completed")
        result = runtime.ask("u", "a", "计算")
        self.assertEqual(result.status, "model_error")
        self.assertNotIn("raw-private-error", result.answer)

        state = self.store.load("u", "a")
        self.assertEqual(len(state.turns), 1)
        self.assertEqual(state.turns[0][0]["content"], "旧问题")
        self.assertNotIn("计算", json.dumps(state.turns, ensure_ascii=False))
        self.assertNotIn("模型调用未完成", json.dumps(state.turns, ensure_ascii=False))

        followup = ScriptedModel(ModelReply("可以继续"))
        self.runtime(followup).ask("u", "a", "继续")
        history = json.dumps([message for message in followup.requests[0]
                              if message["role"] != "system"], ensure_ascii=False)
        self.assertIn("旧问题", history)
        self.assertNotIn("计算", history)
        self.assertNotIn("模型调用未完成", history)

    def test_step_limit_does_not_persist_incomplete_turn(self):
        model = ScriptedModel(call())
        result = self.runtime(model, max_steps=1).ask("u", "a", "循环")
        self.assertEqual(result.status, "step_limit")
        self.assertEqual(self.store.load("u", "a").turns, [])

    def test_duplicate_id_and_empty_response_rejected(self):
        for bad in (ModelReply(), ModelReply(None, [ToolCall("same", "calculator", "{}"), ToolCall("same", "calculator", "{}")] )):
            with self.subTest(bad=bad):
                result = self.runtime(ScriptedModel(bad)).ask("u", "a", "问题")
                self.assertEqual((result.status, result.tool_calls), ("model_error", 0))

    def test_trace_is_correlated_and_does_not_record_content(self):
        result = self.runtime(ScriptedModel(call(), ModelReply("15"))).ask("private-user", "a", "private prompt")
        text = (self.path / "trace.jsonl").read_text("utf-8")
        events = [json.loads(line) for line in text.splitlines()]
        self.assertTrue(all(e["run_id"] == result.run_id for e in events))
        self.assertTrue(any(e["event"] == "tool_end" and e["ok"] for e in events))
        for secret in ("private-user", "private prompt", "7+8"):
            self.assertNotIn(secret, text)

    def test_compaction_preserves_complete_turns_and_recalls_excerpt(self):
        model = ScriptedModel(call(), ModelReply("15"), ModelReply("第二轮"), ModelReply("第三轮"))
        runtime = self.runtime(model, memory=MemoryPolicy(max_recent_turns=1))
        runtime.ask("u", "a", "早期事实：代号松树")
        runtime.ask("u", "a", "第二个问题")
        state = self.store.load("u", "a")
        self.assertEqual(len(state.turns), 1)
        self.assertIn("松树", "".join(state.summaries))
        self.assertIn("tool_result_excerpts", state.summaries[0])
        runtime.ask("u", "a", "回顾")
        messages = model.requests[-1]
        self.assertIn("松树", json.dumps(messages, ensure_ascii=False))
        self.assertFalse(any(m["role"] == "tool" for m in messages))

    def test_memory_budgets_and_oversized_current_turn(self):
        model = ScriptedModel(*[ModelReply("回答" * 300) for _ in range(15)])
        policy = MemoryPolicy(max_recent_turns=2, context_chars=7000, summary_chars=800)
        runtime = self.runtime(model, memory=policy)
        for i in range(15):
            runtime.ask("u", "a", f"{i}:" + "问题" * 250)
        state = self.store.load("u", "a")
        self.assertLessEqual(len(state.turns), 2)
        self.assertLessEqual(len("\n".join(state.summaries)), 800)
        tiny = self.runtime(ScriptedModel(), memory=MemoryPolicy(context_chars=2000, summary_chars=200))
        self.assertEqual(tiny.ask("u", "huge", "很长" * 1500).status, "context_limit")
        self.assertEqual(self.store.load("u", "huge").turns, [])

    def test_escaped_summary_fits_context_and_allows_next_short_question(self):
        policy = MemoryPolicy(max_recent_turns=1, context_chars=4000, summary_chars=1900)
        model = ScriptedModel(ModelReply('"' * 400), ModelReply("ok"), ModelReply("continued"))
        runtime = self.runtime(model, memory=policy)
        for prompt in ('"' * 400, "next"):
            self.assertEqual(runtime.ask("u", "escaped", prompt).status, "completed")
            state = self.store.load("u", "escaped")
            self.assertLessEqual(policy.size(state, SYSTEM_PROMPT, [], runtime.registry.schemas()), 4000)
        result = runtime.ask("u", "escaped", "continue")
        self.assertEqual((result.status, result.model_calls, result.answer), ("completed", 1, "continued"))

    def test_summary_only_history_is_trimmed_without_cutting_pending_messages(self):
        policy = MemoryPolicy(context_chars=4000, summary_chars=1900)
        schemas = make_default_registry().schemas()
        excerpt = policy._excerpt([
            {"role": "user", "content": '"' * 400},
            {"role": "assistant", "content": '"' * 400},
        ])
        for text in ("short question", "x" * 4000):
            with self.subTest(text_length=len(text)):
                state = Session(summaries=[excerpt])
                pending = [{"role": "user", "content": text}]
                original_pending = copy.deepcopy(pending)
                self.assertGreater(policy.size(state, SYSTEM_PROMPT, pending, schemas), 4000)
                policy.compact(state, SYSTEM_PROMPT, pending, schemas)
                self.assertEqual(state.summaries, [])
                self.assertEqual(pending, original_pending)
                # Any remaining overflow now belongs to the irreducible input.
                self.assertEqual(policy.size(state, SYSTEM_PROMPT, pending, schemas),
                                 policy.size(Session(), SYSTEM_PROMPT, pending, schemas))
                if text == "short question":
                    self.assertLessEqual(policy.size(state, SYSTEM_PROMPT, pending, schemas), 4000)
                else:
                    self.assertGreater(policy.size(state, SYSTEM_PROMPT, pending, schemas), 4000)

    def test_same_session_busy_without_blocking_other_session(self):
        started, release = threading.Event(), threading.Event()
        class BlockingModel:
            def complete(self, messages, tools):
                if messages[-1]["content"] == "wait":
                    started.set()
                    release.wait(3)
                return ModelReply("done")
        runtime = self.runtime(BlockingModel())
        outputs = []
        worker = threading.Thread(target=lambda: outputs.append(runtime.ask("u", "a", "wait")))
        worker.start()
        try:
            self.assertTrue(started.wait(2))
            self.assertEqual(runtime.ask("u", "a", "duplicate").status, "session_busy")
            self.assertEqual(runtime.ask("u", "b", "other").status, "completed")
        finally:
            release.set()
            worker.join(3)
        self.assertEqual(outputs[0].status, "completed")

    def test_cross_process_revision_prevents_overwrite(self):
        a, b = self.store.load("u", "a"), self.store.load("u", "a")
        a.turns.append([{"role": "user", "content": "first"}])
        self.store.save("u", "a", a)
        with self.assertRaises(SessionConflict):
            self.store.save("u", "a", b)
        self.assertEqual(self.store.load("u", "a").turns, a.turns)

    def test_rejects_bad_input_without_model_request(self):
        model = ScriptedModel()
        runtime = self.runtime(model)
        for text in ("", " ", "x" * 4001):
            self.assertEqual(runtime.ask("u", "a", text).status, "invalid_input")
        self.assertEqual(model.requests, [])

    def test_repeated_id_across_turns_is_rejected_before_execution(self):
        model = ScriptedModel(call(), ModelReply("15"), call(), ModelReply("恢复正常"))
        runtime = self.runtime(model)
        self.assertEqual(runtime.ask("u", "a", "第一轮").status, "completed")
        rejected = runtime.ask("u", "a", "第二轮")
        self.assertEqual((rejected.status, rejected.tool_calls), ("model_error", 0))
        self.assertEqual(runtime.ask("u", "a", "第三轮").status, "completed")
        ids = [c["id"] for turn in self.store.load("u", "a").turns
               for message in turn for c in message.get("tool_calls", [])]
        self.assertEqual(ids, ["calc-1"])

    def test_trace_write_failure_does_not_abort_run(self):
        trace = Trace(self.path)  # Existing directory, not a writable log file.
        runtime = Runtime(ScriptedModel(ModelReply("正常回答")), make_default_registry(), self.store, trace=trace)
        with patch("sys.stderr") as stderr:
            self.assertEqual(runtime.ask("u", "a", "问题").status, "completed")
        self.assertTrue(trace.failed)
        self.assertTrue(stderr.write.called)


if __name__ == "__main__":
    unittest.main()
