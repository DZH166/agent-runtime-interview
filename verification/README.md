# 验证记录

更新日期：2026-10-09（北京时间）。修复后 **64 项离线测试通过，6 项真实场景全部通过，干净包复现通过**。本次真实验收使用 8 次 HTTP 请求，零自动重试。`reproduction.json` 的全部检查为 true，包括独立环境安装、测试、CLI、会话切换、进程重启以及源文件与实际测试副本相等。

2026-10-19 为 GitHub 发布重新运行离线测试，64 项全部通过，记录见 [发布前检查](github-publish-2026-10-19.json)。本次整理说明和打包范围，排除个人学习记录，未改动 Agent 行为，也没有再次请求真实模型。下述真实模型验收和干净包复现仍对应 10 月 9 日版本。

## 本轮真实验收

证据：[live-smoke-gpt-6.1-sol-review-2026-10-09.json](live-smoke-gpt-6.1-sol-review-2026-10-09.json)。请求 `https://api.anmoxuan.xyz/v1/responses`，模型 ID `gpt-6.1-sol`；推理强度 low，输出预算 2400，单次请求超时 90 秒。准确模型 ID 在此前平台目录查询中确认。

| 场景 | 实际检查结果 |
|---|---|
| 记录项目代号 | 回复已记住 |
| 普通追问 | 回答青松42 |
| 实际执行三个工具 | calculator 得到 126；search 返回本地模拟资料；read_docs 读取 session |
| 工具结果追问 | calculator 计算 126+8，得到 134 |
| 同一用户的新会话 | 回答未知，没有带入原会话代号 |
| 原会话压缩后重新加载 | 摘录已产生，重新构造 Store 和 Runtime 后答出青松42与134 |

JSON 的 7 个检查均为 true。每个场景保存了结构化工具结果，不能只凭模型文案认定工具执行成功。最后一项在同一进程中重新加载 SQLite，不是实际结束进程再启动；进程重启由下面的离线复现单独验证。

本轮供应商返回的 usage 合计为 input_tokens 6026、output_tokens 209，仅代表这轮提供的统计，不是全部调试用量或费用。所有输入都是合成测试文本。模型名称由中转平台提供，本记录不证明上游模型身份，也不保证未来每次请求都成功。三工具场景明确要求使用工具，不能据此证明任意含糊输入都能正确选择工具。

## 离线测试与干净包复现

| 证据 | 内容 |
|---|---|
| [unit-tests.txt](unit-tests.txt)、[unit-tests.json](unit-tests.json) | 完整离线测试的输出、时间与运行环境 |
| [reproduction.json](reproduction.json) | 源码 ZIP 独立解压、新 venv 安装依赖、全部测试、三个工具、会话切换、退出进程后恢复 |
| [responses-model-catalog.json](responses-model-catalog.json) | 此前的模型目录查询，HTTP 200，确认准确模型 ID |

本轮新增回归保护：仅进展说明不能完成任务；普通无 phase 回答与历史回放继续兼容；Messages 明确拒绝未支持的 thinking；摘要转义后按实际总预算裁剪；损坏数据库不被重建覆盖；会话列表查询失败后可继续提问；显式空提问不进入交互模式。

交付复现还会比较解压后实际测试的代码、测试、语料和说明文件与最终源文件，避免测试旧副本后打包另一套内容。验证记录自身会在检查后更新，不参加这项相等比较。打包内容不包括 `.local`、虚拟环境、凭据和会话数据库。

## 保留的历史阶段

10 月 8 日初版的 55 项离线测试和干净包记录保存在 [history/2026-10-08](history/2026-10-08/)。以下真实请求记录均保留，没有覆盖：

| 文件 | 当时发生的情况 |
|---|---|
| [live-smoke.json](live-smoke.json) | 旧 claude-sonnet-5 首轮 HTTP 错误，停止 |
| [http-diagnostic.json](http-diagnostic.json) | 单次诊断得到 HTTP 502/upstream_error，上游访问被拒 |
| [live-smoke-gpt-6.1.json](live-smoke-gpt-6.1.json) | 截图 OCR 误读凭据导致 HTTP 401，随后改用隐藏输入准确录入 |
| [live-smoke-gpt-6.1-sol.json](live-smoke-gpt-6.1-sol.json) | 文本、追问及三工具执行成功，工具汇总超过 40 秒超时 |
| [live-smoke-gpt-6.1-sol-final.json](live-smoke-gpt-6.1-sol-final.json) | 10 月 8 日最终 6 项场景通过，8 次请求 |
| [live-smoke-gpt-6.1-sol-review-2026-10-09.json](live-smoke-gpt-6.1-sol-review-2026-10-09.json) | 本轮代码修复后的 6 项场景通过，8 次请求 |

10 月 8 日各阶段共 15 次生成请求尝试，本轮新增 8 次，累计记录为 23 次；包含认证拒绝、上游拒绝和超时，不等于 23 次成功生成。模型目录查询另计。所有阶段均无自动重试。

复测应输出新文件，保留现有记录。unittest 不访问真实模型；真实验收会产生 API 请求。
