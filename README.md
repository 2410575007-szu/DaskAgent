# KoreAgent 

KoreAgent 是一套从零实现的本地 AI Agent Harness。它提供 Electron 桌面工作区、Python Host、JSON-RPC 通信、长期运行管理、审批、自动化、交付物、记忆、扩展能力，以及 macOS Computer Helper。

## 功能

- 对话与多步工具执行，支持流式回答、推理片段、工具状态、普通模式和无副作用计划模式
- 每个模型步骤的上下文预算检查、长工具结果压缩、滚动摘要，以及核心记忆/任务/Skill 上下文注入
- Run 生命周期、检查点、中断、恢复、用量统计、证据存档与完整 Trace；可按需查看持久化的模型输入和工具声明
- 敏感工具操作审批，电脑操作提供独立置顶审批窗口
- 一次性、间隔与 Cron 自动化
- 文件与 URL 交付物发布、预览和下载
- 长期记忆、核心记忆、运行后反思、容量维护、跨会话消息搜索，以及带步骤的持续任务
- Skill 安装、启停、删除、扩展预览导入和“重复成功任务 → 待审核 Skill 候选”学习闭环
- MCP Server 配置、启停、环境变量白名单、工作区约束和 macOS Seatbelt 策略；不支持隔离的平台默认拒绝启动需沙箱的进程
- OpenAI、DeepSeek、Qwen 和 Anthropic 模型配置
- macOS 应用观察、截图、可访问性元素树、元素引用点击、输入、按键、滚动、应用/窗口聚焦
- Computer Run 独占租约、Session 校验与过期 Observation 拒绝机制

## 知识与任务约束

- Agent 写入的长期记忆、运行后反思和重复成功任务提取的 Skill 均先生成候选。设置页提供来源、适用范围、敏感信息和提示词注入检查清单；人工确认前，候选不会进入模型上下文。升级前已有的未审核记忆会转为候选，原内容保留。
- 项目 Wiki 记录带来源的项目事实和历史经验。文件来源必须位于 Agent 工作区且文件存在，Run 来源必须能找到对应记录。Wiki 只向模型展示已审核条目的索引，正文通过 `wiki_read` 按需读取。
- Wiki、记忆和 Skill 可以隔离；隔离后不再进入后续 Run 的上下文。Wiki 与记忆的修订会保留旧版并建立替代关系；Skill 安装会保存版本，可在设置页恢复为一个新版本。自动敏感信息与提示词注入检查只覆盖明显模式，仍需人工核查事实和上下文。
- Task Spec 记录目标、约束和验收条件。Run 开始时绑定一个 Spec 版本；Agent 的 `task_update` 不能改动这些字段，用户可在设置页创建新版本。已有 Run 保持原版本，后续 Run 绑定最新版本。

Host 暴露 `GET /health`、`WS /rpc`、截图读取和 artifact 下载接口。业务操作均通过 JSON-RPC 完成。

## 源码导读

代码中的关键决策点已加中文注释。建议按下面的顺序阅读：

1. `backend/kore_next/application.py`：Run 主循环、事件、审批、中断与恢复。
2. `backend/kore_next/agent.py` 和 `context.py`：模型调用、工具执行与上下文预算。
3. `backend/kore_next/database.py`、`checkpoint.py` 和 `runtime_policy.py`：持久化、检查点与运行边界。
4. `backend/kore_next/memory.py`、`wiki.py`、`tasks.py` 和 `skill_learning.py`：长期知识、任务规格与 Skill 候选审核。
5. `backend/kore_next/permissions.py`、`sandbox.py` 和 `computer.py`：敏感工具授权、子进程隔离与桌面控制。
6. `desktop/src/rpc.ts`、`liveState.ts` 和 `App.tsx`：桌面端通信、流式状态和页面入口。

注释重点解释数据流、状态转换和安全边界；具体参数与返回值以代码和类型定义为准。

## Windows 开发启动

需要 Python 3.11+、Node.js 20+ 和 npm。在 PowerShell 中执行：

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\start.ps1
```

也可以分别启动：

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements-dev.txt
.\.venv\Scripts\python -m kore_next.server

cd ..\desktop
npm install
npm run electron:dev
```

首次运行在 `backend/.koreagent-next` 创建数据库、设置、记忆、技能和交付物目录。

如果 `backend/.env` 不存在，启动脚本会从 `.env.example` 创建一个空白文件，不会覆盖已有配置。Qwen 可在应用“设置”页面直接配置，也可以填写：

```dotenv
KORENEXT_PROVIDER=qwen
KORENEXT_MODEL=qwen-plus
KORENEXT_API_KEY=sk-你的密钥
KORENEXT_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
```

## macOS Computer Helper

```bash
cd native/macos-computer-helper
swift build -c release
swift test
```

启用前设置 `KORENEXT_COMPUTER_ENABLED=true`，并在系统设置中授予辅助功能与屏幕录制权限。

Computer Helper 当前仅在 macOS 上可用。Windows 没有可靠工作区沙箱时，Shell 和默认 MCP 配置会拒绝启动；如确需运行 MCP，须明确将文件系统设为 `host`、网络设为 `allowed`，每次工具调用仍需审批。宿主机模式不提供隔离，切勿运行不可信 Server。

搜索默认使用 DuckDuckGo；设置 `KORENEXT_TAVILY_API_KEY` 后，`KORENEXT_SEARCH_PROVIDER=auto` 优先使用 Tavily，遇到可恢复错误时回退。可显式选择 `tavily` 或 `duckduckgo`。HTTP 工具默认拒绝私网地址及重定向到私网的目标；只有明确设置 `KORENEXT_HTTP_ALLOW_PRIVATE_NETWORK=true` 才放行。用户级 Skill 默认保存在 `~/.koreagent-next/skills`，可通过 `KORENEXT_USER_SKILLS_DIR` 指定。

## 验证

```powershell
cd backend
python -m pytest
ruff check .
python -m compileall -q kore_next

cd ..\desktop
npm test
npm run typecheck
npm run build
```

## 数据隔离

默认运行数据位于 `backend/.koreagent-next`，工作文件位于 `backend/workspace`。可通过 `KORENEXT_DATA_DIR` 和 `KORENEXT_WORKSPACE_DIR` 覆盖。数据库升级前自动生成 `backup-v*` 快照并记录版本；旧的字段匹配授权规则保留但停用，需要重新审批。所有配置均使用 `KORENEXT_` 前缀。

模型密钥不会进入数据库导出或日志。桌面端保存密钥时使用独立的 KoreAgent Next 密钥命名空间；`.env`、数据库、虚拟环境、依赖与构建目录均被 Git 忽略。
