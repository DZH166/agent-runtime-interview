"""Bounded real API verification using synthetic, non-personal test inputs.

Run explicitly: python -m scripts.live_smoke. At most 12 HTTP attempts, no retry.
Default credentials come from env. --saved-config-dir is an optional Windows
read-only bridge; it never imports, initializes or writes another application's DB.
"""
import argparse
import json
import tempfile
from dataclasses import asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

from agent.memory import MemoryPolicy, SessionStore
from agent.models import ModelError
from agent.provider import MessagesModel
from agent.runtime import Runtime
from agent.tools import make_default_registry
from agent.trace import Trace


# Backwards-compatible name for explicit, read-only local credential loading.
from agent.config import load_saved_model as load_saved_config, from_environment


class BudgetModel:
    def __init__(self, model):
        self.model = model
        self.calls = []

    def complete(self, messages, tools):
        if self.model.request_count >= 12:
            raise ModelError("verification_budget", "已达到验收请求上限。")
        reply = self.model.complete(messages, tools)
        self.calls.append({"request_number": self.model.request_count,
                           "input_roles": [m["role"] for m in messages],
                           "tool_names": [t.name for t in reply.tool_calls],
                           "usage": self.model.last_usage.copy()})
        return reply


def run(model, evidence_path: Path):
    evidence = {"kind": "real_api", "date": datetime.now(timezone(timedelta(hours=8))).isoformat(),
                "endpoint": model.endpoint, "requested_model": model.model,
                "model_identity_note": "模型名称由中转平台提供，本验收不证明上游模型身份。",
                "automatic_retries": 0, "request_budget": 12, "cases": [], "checks": {}}
    evidence["timeout_seconds"] = model.timeout
    evidence["max_output_tokens"] = model.max_tokens
    evidence["reasoning_effort"] = getattr(model, "reasoning_effort", None)
    budget = BudgetModel(model)
    with tempfile.TemporaryDirectory() as temp:
        store = SessionStore(Path(temp) / "sessions.sqlite3")
        registry = make_default_registry()
        trace_path = Path(temp) / "trace.jsonl"
        runtime = Runtime(budget, registry, store, memory=MemoryPolicy(max_recent_turns=2),
                          trace=Trace(trace_path), max_steps=4)
        cases = [
            ("remember", "a", "请记住：本会话项目代号是青松42。只回复已记住，不使用工具。"),
            ("plain_followup", "a", "项目代号是什么？只回答代号，不使用工具。"),
            ("three_tools", "a", "请实际使用三个对应工具：计算(17+25)*3；在本地模拟资料搜索Agent；读取session文档。最后简短汇总，注明搜索为模拟资料。"),
            ("tool_followup", "a", "把刚才的计算结果再加8，请用计算器确认。"),
            ("isolated_window", "b", "当前会话此前是否约定了项目代号？若没有，只回复未知。不使用工具。"),
            ("resume_after_compaction", "a", "请回答最开始约定的项目代号和最近一次计算结果。不使用工具。"),
        ]
        tools_by_case = {}
        for name, session, prompt in cases:
            if name == "resume_after_compaction":
                # Reload persisted state in this process. Actual process restart
                # is checked separately by the offline release verifier.
                store = SessionStore(Path(temp) / "sessions.sqlite3")
                runtime = Runtime(budget, registry, store, memory=MemoryPolicy(max_recent_turns=2),
                                  trace=Trace(trace_path), max_steps=4)
            result = runtime.ask("smoke-user", session, prompt)
            state = store.load("smoke-user", session)
            turn = state.turns[-1] if state.turns else []
            tool_results = []
            calls = {c["id"]: c["function"]["name"] for m in turn for c in m.get("tool_calls", [])}
            for message in turn:
                if message["role"] == "tool":
                    tool_results.append({"tool": calls[message["tool_call_id"]],
                                         "result": json.loads(message["content"])})
            tools_by_case[name] = tool_results
            evidence["cases"].append({"name": name, "session": session, "prompt": prompt,
                                      **asdict(result), "tool_results": tool_results,
                                      "summary_present": bool(state.summaries)})
            print(f"{name}: {result.status}; model={result.model_calls}; tools={result.tool_calls}", flush=True)
            if result.status != "completed":
                break
        by_name = {case["name"]: case for case in evidence["cases"]}
        all_done = len(by_name) == len(cases) and all(c["status"] == "completed" for c in by_name.values())
        three = tools_by_case.get("three_tools", [])
        follow = tools_by_case.get("tool_followup", [])
        checks = {
            "all_runs_completed": all_done,
            "plain_followup": "青松42" in by_name.get("plain_followup", {}).get("answer", ""),
            "three_tools_success": {t["tool"] for t in three if t["result"]["ok"]} == {"calculator", "search", "read_docs"},
            "calculation_126": any(t["tool"] == "calculator" and t["result"].get("data", {}).get("value") == 126 for t in three),
            "tool_followup_134": any(t["tool"] == "calculator" and t["result"].get("data", {}).get("value") == 134 for t in follow),
            "session_isolation": "未知" in by_name.get("isolated_window", {}).get("answer", "") and "青松42" not in by_name.get("isolated_window", {}).get("answer", ""),
            "resume_and_compaction": bool(by_name.get("resume_after_compaction", {}).get("summary_present")) and all(v in by_name.get("resume_after_compaction", {}).get("answer", "") for v in ("青松42", "134")),
        }
        evidence["checks"] = checks
        evidence["passed"] = all(checks.values())
        evidence["http_attempts"] = model.request_count
        evidence["model_calls"] = budget.calls
        evidence["trace"] = [json.loads(line) for line in trace_path.read_text("utf-8").splitlines()]
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("PASS" if evidence["passed"] else "FAIL (inspect evidence; no automatic rerun)")
    return 0 if evidence["passed"] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--saved-config-dir", type=Path)
    parser.add_argument("--output", type=Path, default=Path("verification/live-smoke.json"))
    args = parser.parse_args()
    try:
        model = load_saved_config(args.saved_config_dir) if args.saved_config_dir else from_environment()
    except Exception:
        print("真实验收未启动：配置缺失或无法安全读取。未打印凭据。")
        return 2
    return run(model, args.output)


if __name__ == "__main__":
    raise SystemExit(main())
