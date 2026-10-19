# 接口与维护约定

本项目使用 Python 3.11+，直接依赖只有 jsonschema。主循环自行实现，不依赖 Agent 框架。

## 模型与主循环

`Model.complete(messages, tools) -> ModelReply` 是真实模型和离线模型共用的入口。`ToolCall` 包含 `id`、`name`、JSON 字符串形式的 `arguments`；`ModelReply` 包含公开文本 `content`、工具清单 `tool_calls` 和原生协议字段 `provider_items`。适配器以 `ModelError(code, message)` 报告不能继续的模型错误。

内部消息使用 `system/user/assistant/tool` 四种角色。assistant 的工具请求含 `id`、`type="function"` 和 `function={name, arguments}`；tool 结果通过 `tool_call_id` 与请求对应。Responses 的原生输出放在所属 assistant 消息的 `provider_items` 中，参与持久化、回放和上下文预算，不记录到 trace。协议中间进展不能单独作为最终答案。

`Runtime.ask(user_id, session_id, text) -> RunResult` 返回答案、状态、运行 ID、模型接口调用次数和工具执行次数。HTTP 请求次数由模型适配器单独统计，二者不必相同。主循环和 HTTP 层均不自动重试。

## 工具、会话和入口

`ToolRegistry.register(name, description, parameters, handler)` 注册工具；`schemas()` 给模型提供描述；`execute(name, arguments)` 返回 `{ok:true,data:...}` 或 `{ok:false,error:{code,message}}`。参数拒绝未知字段和隐式类型转换。现有 calculator、mock search、read_docs 均为同步只读工具。

会话按 `(user_id, session_id)` 隔离，以完整轮次为压缩单位，保持工具请求与结果配对。一个 Runtime 实例内拒绝同一会话并发运行；SQLite revision 防止多个实例或进程静默覆盖彼此的保存结果，但不能防止重复执行工具。摘录和完整历史都可淘汰，当前轮无法容纳时明确返回超限。

命令行入口是 `agent/__main__.py`。`python -m agent --demo` 使用固定规则模型；真实模式默认 Responses，也可显式选择有限支持的 Messages。后者不支持包含 thinking 或 redacted_thinking 的响应，会返回明确错误。

运行检查：`python -m unittest discover -s tests -v`。真实验收另用 `scripts.live_smoke`，需要显式配置且会产生 API 请求。架构第 4、5 题中的订单、幂等策略和业务版本属于设计内容，未加入当前工具注册表。
