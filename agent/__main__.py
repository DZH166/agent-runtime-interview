"""python -m agent [--demo]. Ctrl-D/Ctrl-Z or /quit exits."""
import argparse
import os
import sqlite3
from pathlib import Path

from .demo import DemoModel
from .memory import MemoryPolicy, SessionStore
from .models import ModelError
from .runtime import Runtime
from .tools import make_default_registry
from .trace import Trace


def main():
    parser = argparse.ArgumentParser(description="自写最小 Agent Runtime")
    parser.add_argument("--demo", action="store_true", help="使用明确标注的离线模拟模型")
    parser.add_argument("--user", default="local-user")
    parser.add_argument("--session", default="window-1")
    parser.add_argument("--config-dir", type=Path, help="显式加载本机DPAPI加密配置目录；不会写入配置")
    parser.add_argument("--data-dir", default=".local")
    parser.add_argument("--max-steps", type=int, default=6)
    parser.add_argument("--history-turns", type=int, default=6)
    parser.add_argument("--ask", help="单次提问后退出；便于复现验收")
    args = parser.parse_args()
    try:
        if args.demo:
            model = DemoModel()
        else:
            from .config import from_environment, load_saved_model
            model = load_saved_model(args.config_dir) if args.config_dir else from_environment()
        data = Path(args.data_dir) / ("demo" if args.demo else "live")
        store = SessionStore(data / "sessions.sqlite3")
        runtime = Runtime(model, make_default_registry(), store, trace=Trace(data / "trace.jsonl"),
                          memory=MemoryPolicy(max_recent_turns=args.history_turns), max_steps=args.max_steps)
    except sqlite3.Error:
        print("会话数据库无法打开：请检查数据目录、文件状态或数据库占用情况。")
        return 2
    except (ValueError, ModelError, OSError):
        print("配置失败：请检查协议、模型、环境变量或显式加密配置目录。")
        return 2
    print("模式：" + ("离线模拟（不调用真实 LLM）" if args.demo else "真实模型 API"))
    print(f"会话：{args.session}；数据目录：{data.resolve()}")
    if args.ask is not None:
        result = runtime.ask(args.user, args.session, args.ask)
        print(result.answer)
        print(f"状态={result.status} 模型调用={result.model_calls} 工具调用={result.tool_calls}")
        return 0 if result.status == "completed" else 1
    print("命令：/switch 会话名、/sessions、/quit。切换保留历史，重启可恢复。")
    try:
        while True:
            text = input(f"[{args.session}] > ").strip()
            if not text:
                continue
            if text == "/quit":
                break
            if text == "/sessions":
                try:
                    print("、".join(store.list_sessions(args.user)) or "暂无已保存会话")
                except sqlite3.Error:
                    print("暂时无法读取会话列表，请检查数据库状态后重试。")
                continue
            if text.startswith("/switch "):
                target = text.removeprefix("/switch ").strip()
                if 0 < len(target) <= 100:
                    args.session = target
                else:
                    print("会话名应为 1–100 字符。")
                continue
            result = runtime.ask(args.user, args.session, text)
            print(result.answer)
            print(f"[{result.status}] 模型 {result.model_calls} 次 / 工具 {result.tool_calls} 次 / run {result.run_id[:8]}")
    except (EOFError, KeyboardInterrupt):
        print("\n已退出。已完成轮次保留；中断的当前轮次不保存。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
