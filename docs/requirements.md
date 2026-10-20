# 原题核对与验收映射

首次核对日期：2026-10-08；2026-10-09 再次核对实作要求与第 4、5 题（北京时间）。直接在浏览器读取原题，文档最近修改显示 9 月 26 日 16:36。网页原名为《2026年后端(Agent方向)开发笔试题》，公司名显示“光辰智能”；本地工作区沿用用户提供的“光尘智能面试”。

原文：[2026年后端(Agent方向)开发笔试题](https://ucnk0qbix8zv.feishu.cn/docx/EB1HdbV2UotooNxSNFIcUjCanYd)

以下为要求转述，不是自行增加的评分标准。原题由 Vibe coding 和九选一架构设计两部分组成；完成后提供 GitHub 代码链接，不需要部署一个 API。

2026-10-19 再次直接核对原题的“注意事项”和“提交内容”：完成后发送 GitHub 链接；同时提供运行、系统设计与 memory 说明，以及 AI Prompt 和问题解决记录。架构题选一道，未指定答案文件格式；提交说明未指定仓库必须公开，也未要求 ZIP、演示视频或在线网站。

| 原题要求 | 实现／材料 | 验证方式 |
| --- | --- | --- |
| 自行实现 Agent Runtime，不依赖既有 Agent 框架 | `agent/runtime.py` | 直接阅读 `_run` 的有界循环；依赖只有 jsonschema |
| 输入→决定回答或工具→执行→继续或结束 | `Runtime.ask/_run`、`ModelReply` | 普通回答、多工具、连续工具、终止测试 |
| 至少三个工具；注册名称、描述、参数 Schema；模型自主选择 | `agent/tools.py` | calculator、mock search、read_docs；注册/类型/额外字段/未知工具测试 |
| 解析模型的思考过程、工具调用或最终答案 | `agent/responses_provider.py`、`provider.py`、`models.py` | 解析公开文本和工具调用；仅进展说明不能视为最终答案；不展示或解密私有推理，协议续接所需的不透明加密字段随会话保存回传 |
| 同一用户两个独立会话，可切回续聊 | `SessionStore`，CLI `/switch` | 同用户不同 session、不同用户同名 session、持久化重载 |
| 最大轮次；持续对话记住先前状态；普通及工具追问 | `MemoryPolicy`、`Runtime` | 完整历史和结果回传；最大模型步骤6、最近完整轮次6 |
| 上下文过长有基础压缩，自行判断放入哪些信息 | `MemoryPolicy.compact/messages`、`docs/memory-design.md` | 完整轮次摘录与淘汰；摘要转义后的实际字符预算；超长单轮中止 |
| 基本异常处理、工具 trace/执行日志 | `Trace`、结构化工具错误、ModelError、CLI | 格式/HTTP/超时/重复ID/工具异常/日志故障/数据库故障测试；未完成轮次不会写入会话历史 |
| 构建以上功能的测试 | `tests/` | unittest 自动测试；模拟传输与真实请求明确区分 |
| 使用真实 LLM API | `ResponsesModel`、`scripts/live_smoke.py` | 2026-10-09 有 6 项场景通过、8 次 HTTP 请求；2026-10-21 两次独立复核均收到上游 HTTP 502，记录见验证说明 |
| README 含运行方式、系统设计、memory 召回时机与放置方式 | `README.md`、`docs/memory-design.md` | 按说明在独立目录复现 |
| AI Prompt 与问题解决记录 | `docs/ai-worklog.md`、`docs/prompts.md` | 记录真实使用、关键提示、采纳/拒绝和实际问题，不虚构候选人独立完成 |
| 架构题选择一道 | `docs/architecture-q4.md` | 第 4 题作为主答；第 5 题数据一致性作为完整补充，详细讲解在 `architecture-defense.md` |

九题完整范围也已核对：1 Redis；2 KV cache 与 context；3 同异步与首 token；4 幂等与重试；5 Agent/后端一致性；6 滑动窗口限流；7 硬件音频接入与 ASR 会话；8 多机型与 ASR 适配；9 端点检测和语音轮次状态机。原题仍是九选一；同时准备第 4、5 题是用户选择的准备方式，不是新增的笔试要求。

额外工程选择：SQLite 重启恢复、CAS 防覆盖、输入大小限制、HTTP 禁重定向、AST 计算限制均为本实现取舍。原题没有要求 Web 前端、FastAPI、部署、向量数据库、流式输出或生产级分布式系统，因此本项目没有增加这些依赖。

最近一次真实验证及各类证据见 [验证说明](../verification/README.md)。Messages 只保留有限兼容，不支持 thinking 响应，也没有通过本次真实验收。
