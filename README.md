# opera-houdini-mcp

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12-blue)](https://www.python.org)
[![MCP](https://img.shields.io/badge/MCP-1.12.2-green)](https://github.com/modelcontextprotocol/python-sdk)
[![Upstream](https://img.shields.io/badge/upstream-capoomgit%2Fhoudini--mcp-lightgrey)](https://github.com/capoomgit/houdini-mcp)

`opera-houdini-mcp` 是 [`capoomgit/houdini-mcp`](https://github.com/capoomgit/houdini-mcp) 的独立增强 fork，定位为**可作为 git submodule 嵌入到任意 Houdini 插件库**的 MCP server。MIT 协议，与上游完全兼容，额外提供 Tier 1 工具集、`execute_code` 安全护栏、零新增 pip 依赖。

> **当前上游基线**：`capoomgit/houdini-mcp` @ `de4fd93`（2026-07-17 同步）
> **同步策略**：cherry-pick only，禁止 merge
> **集成方式**：作为 git submodule 嵌入到消费方 Houdini 项目（见 [§3](#embedding-as-a-git-submodule)）

---

## 目录

1. [Features](#features)
2. [Architecture](#architecture)
3. [Embedding as a git submodule](#embedding-as-a-git-submodule)
4. [Tier 1 工具清单](#tier-1-工具清单)
5. [`execute_code` 安全模型](#execute_code-安全模型)
6. [AI 调用 hou API 的硬约束](#ai-调用-hou-api-的硬约束)
7. [Configuration](#configuration)
8. [Upstream Sync Policy](#upstream-sync-policy)
9. [Testing](#testing)
10. [Troubleshooting](#troubleshooting)
11. [Edge Cases & 集成陷阱](#edge-cases--集成陷阱)
12. [Contributing](#contributing)
13. [Security](#security)
14. [License & Acknowledgement](#license--acknowledgement)

---

## Features

- **13 个 Tier 1 工具** — 场景 CRUD / 节点发现 / 图编辑 / 错误扫描（含 warnings）/ 几何摘要 / 材质 / 截图 / 文档查询 / 缓存管理 / 诊断，独立模块化
- **`execute_code` 三档安全 policy** — `read-only` / `normal` / `privileged` × dangerous / heavy / mutation 三类黑名单（正则 + AST 别名双检）
- **双开关 bypass** — 任何 dangerous / heavy / privileged 操作都需「请求端参数 + 服务端 `HOUDINI_MCP_ALLOW_BYPASS=1`」同时开启
- **零新增 pip 依赖** — `get_houdini_help` 用 stdlib `html.parser` 替代 `beautifulsoup4`，维持 `mcp[cli]==1.12.2 + requests + python-dotenv` 三件套
- **结构化 audit** — 每次 `execute_code` 响应附 `_audit` 块（policy / dangerous_hits / heavy_hits / mutation_hits / bypass_used / elapsed_ms / undo_group）
- **local-help-first** — `get_houdini_help` / `verify_hou_api` 优先打 Houdini 本地 help server（`127.0.0.1:48626`），失败自动回退在线 SideFX
- **自进化知识库** — 4 个 bridge-local 知识工具（`search_lessons` / `save_lesson` / `read_lesson` / `knowledge_stats`）+ 自动错误捕获 hook（零上下文成本）；多 root（个人库自动发现 + 团队库注册表声明，默认只读）；**无嵌入模型**（BM25 + 指纹 + 统计，全 stdlib）

---

## Architecture

```mermaid
flowchart LR
    A[AI Agent<br/>Claude Desktop / Cursor / ...] -->|stdio / MCP JSON| B[houdini_mcp_server.py<br/>bridge]
    B -->|TCP 127.0.0.1:9876| C[server.py<br/>HoudiniMCPServer]
    C -->|hou API| D[Houdini main thread<br/>H21+ / Python 3.11+]
    D -.->|hou.helpServerUrl| E[Local help server<br/>127.0.0.1:48626]
    E -.->|F1 失败 fallback| F[SideFX online docs]
    C -->|RAG index build| G[scripts/build_rag_index.py]
```

**关键约束**：

- `server.py` **必须**运行在 Houdini 主进程内（`hou` 是 C 扩展，跨进程 import 会 hang）
- `bridge`（`houdini_mcp_server.py`）与 `server` 通过 **TCP `127.0.0.1:9876`** 通信，每次 tool call 短连接
- AI 工具看到的 MCP tool 列表来自 **bridge** 端（`mcp[cli]` SDK），实际 `hou` 调用发生在 Houdini 进程内

---

## Embedding as a git submodule

### 1. 添加 submodule

```bash
# 在你的 Houdini 插件库根目录
git submodule add https://github.com/ChengZiiii/opera-houdini-mcp.git external/houdinimcp
git submodule update --init --recursive
```

### 2. 隔离 Python 环境

opera-houdini-mcp 与你项目里的其他工具运行环境解耦。建议目录布局：

```
<your-project>/
├── external/
│   ├── houdinimcp/                  # 本仓库（submodule）
│   ├── houdinimcp-env/              # 本仓库专用的 venv（python/ + pylibs/）
│   ├── <other-tool>/                # 你的其他第三方工具
│   └── <other-tool>-env/            # 各工具独立环境，互不冲突
```

`<dirname>-env/` 不要提交进 git。**env 目录名 = package 目录名 + `-env`**，自动派生，详见 [Configuration](#configuration)。如果你把 submodule 改名为 `external/opera-houdini-mcp/`，env 自动变成 `external/opera-houdini-mcp-env/`，跟着 rename 一下就行。

环境初始化细节（`python/` 解释器 + `pylibs/` 依赖）由消费方项目侧决定，可参考 `pyproject.toml` 的 `dependencies` 三件套自行装配，或用 `uv pip install -p <venv-python> mcp[cli]==1.12.2 requests python-dotenv` 一行命令起步。

### 3. 启动 server

从你的项目代码里直接 import `houdinimcp` 包：

```python
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "external"))

from houdinimcp import start_server, stop_server, is_server_running

# 启动（默认 127.0.0.1:9876）
start_server()

if is_server_running():
    print("opera-houdini-mcp is up")
```

`houdinimcp` 包对外暴露的完整 API：

| 函数 | 用途 |
|------|------|
| `start_server(host='127.0.0.1', port=9876)` | 启动 TCP server（幂等，重复调用早退） |
| `stop_server()` | 停止 server |
| `restart_server(host, port)` | stop + start |
| `is_server_running() -> bool` | 查询状态 |
| `initialize_plugin()` | 一次性初始化 `hou.session` 标志位 |

### 4. 配置 AI 工具

任一兼容 MCP 的客户端（Claude Desktop / Cursor / ZCode / OpenCode / Codex）：

```json
{
  "mcpServers": {
    "houdini": {
      "command": "uv",
      "args": [
        "run",
        "python",
        "<your-project>/external/houdinimcp/houdini_mcp_server.py"
      ]
    }
  }
}
```

> MCP JSON key 固定为 `houdini`（`mcpServers.houdini` / `mcp.houdini` / `mcp_servers.houdini` 三种形式都接受），与上游完全兼容，老用户配置零改动。

### 5. 升级 submodule 消费者

```bash
git submodule update --remote external/houdinimcp
git submodule sync
```

无需重装 env —— 运行环境独立放在 `external/<工具名>-env/` 下，与源码完全解耦。

---

## Tier 1 工具清单

> 以独立 PR 形式合入。完整计划与进度见 `CHANGELOG.md`。

| 类别 | 工具 | 说明 |
|------|------|------|
| 场景 | `get_scene_info` | 增强版场景元信息（houdini_version / node_count） |
| 场景 | `save_scene` / `load_scene` / `new_scene` | 场景 CRUD，自动失效缓存 |
| 节点发现 | `list_node_types` | 按 category 过滤 + name 模糊匹配 + 分页 |
| 节点发现 | `list_children` | 递归子树 + compact 模式 + 分页 |
| 节点发现 | `find_nodes` | glob + 类型过滤，Houdini 端单次扫描 |
| 图编辑 | `reorder_inputs` / `layout_children` / `set_node_position` / `set_node_color` / `create_network_box` | 节点位置/颜色/网络盒 |
| 节点信息 | `get_node_info` | 增强：errors / cook_state / compact / input details |
| 错误扫描 | `find_error_nodes` | 默认含 warnings，单次 `allSubChildren` 扫描 |
| 几何 | `get_geo_summary` | counts / bbox / attributes / groups + 大几何降级 |
| 材质 | `create_material` / `assign_material` / `get_material_info` | 50+ 参数白名单 + texture 引用识别 |
| HScript | `execute_hscript` | 包装 `hou.hscript` |
| 安全代码 | `execute_code` | 三档 policy + bypass 双开关 + 结构化 audit |
| 安全代码 | `get_last_scene_diff` | 仅 mutation 模式提供前后场景快照 |
| 截图 | `capture_pane_screenshot` / `render_node_network` / `list_visible_panes` / `capture_multiple_panes` | pane 截图，响应走 `apply_response_cap` |
| 渲染 | `render_viewport_base64` / `render_quad_views_base64` | base64 版，karma cpu/xpu 双 renderer |
| 文档 | `get_houdini_help` | **本地 help server 优先** + 在线 SideFX 回退（stdlib `urllib` + `html.parser`）；返 `_source` / `_fallback_reason` |
| 文档 | `verify_hou_api` | python_hou 默认 + `_ai_hint` 合成，AI-friendly wrapper over `get_houdini_help` |
| 诊断 | `check_connection` / `ping_houdini` | 不持久化连接的 ping |
| 缓存 | `manage_cache` | stats / invalidate / warmup |
| 知识库 | `get_best_practices` | fork 人工审查 advisory recipes（bridge-local，不建立 Houdini 连接） |
| 知识库 | `search_docs` / `get_doc` / `parse_hip_offline` | BM25 离线文档检索 / 全文 / 离线 .hip 解析 |
| 知识库 | `search_lessons` / `save_lesson` / `read_lesson` / `knowledge_stats` | 自进化知识沉淀：跨 root BM25 融合检索 / 沉淀 / 全文 / 统计 |

---

## 自进化知识库

agent 操作 Houdini 的试错经验跨 session 持久化为可检索 lesson（模块 `_lessons.py` +
`_lessons_search.py`，纯 stdlib、无嵌入模型）。**触发时机**：遇到报错、重试第 2 次
仍未解决、或遇到不认识的 API/参数时，先 `search_lessons` 检索既往经验；命中后用
`read_lesson` 拉全文；解决问题后用 `save_lesson` 沉淀。lesson 是 advisory，不替代
`verify_hou_api` / `get_houdini_help` / `get_best_practices`。

**自动捕获（零上下文成本）**：bridge 在响应出口检测 `status=error` 响应，把错误
事件以 append-only 方式写入个人库 `inbox/events.jsonl`（同指纹去重、≥3 次自动生成
draft 骨架并在检索时提示「已踩 N 次，请补充 fix」）。

**存储位置**（全部在个人目录 `~/.opera-houdini-mcp/`，不入仓库 git）：

```
~/.opera-houdini-mcp/
├── config.json              # 注册表（仅声明额外团队 root，个人库自动发现）
├── knowledge/
│   ├── lessons/*.md         # draft + published lesson（9 字段 + id/status/strength/root/时间戳）
│   ├── recipes/BEST_PRACTICES.md   # 个人人工 recipes（可空）
│   └── inbox/events.jsonl   # 自动捕获原始事件
└── cache/index/<root-name>/ # 各 root BM25 索引缓存
```

**团队库注册**（`config.json`，可选）：路径只接受 `${VAR}` 环境占位符或相对路径
（相对 `~/.opera-houdini-mcp/`），拒绝裸绝对路径；`writable` 默认 `false`（AI 只读，
晋升人工把关）；占位符未定义 → `unconfigured` 静默跳过，路径不可达 → `unavailable`
跳过并附 `_warning`，均不影响个人库。

```json
[
  { "name": "team_knowledge", "path": "${TEAM_SHARE}/houdini/knowledge", "priority": 0.8, "writable": false }
]
```

---

## `execute_code` 安全模型

| Policy | mutation | dangerous | heavy_geometry | import hou | 默认 bypass |
|--------|----------|-----------|----------------|------------|-------------|
| `read-only` | **拒绝**（命中 mutation 正则/AST） | 拒绝 | 拒绝 | 拒绝 | — |
| `normal`（默认） | 允许 | 拒绝（除非 `allow_dangerous=True`） | 拒绝（除非 `allow_heavy_geometry=True`） | 提示 | 仅在客户端显式开启 |
| `privileged` | 允许 | 允许（必须同时开启 `allow_dangerous=True` **和** `HOUDINI_MCP_ALLOW_BYPASS=1`） | 允许（必须同时开启 `allow_heavy_geometry=True` **和** `HOUDINI_MCP_ALLOW_BYPASS=1`） | 允许 | 必须服务端环境变量 |

**双开关原则**：任何 dangerous / heavy / privileged 操作都需要「请求端参数 + 服务端环境变量」同时开启。服务端不开环境变量，再多客户端请求也无效。

**Audit**：每次 `execute_code` 调用都会在响应里附 `_audit` 块（policy / dangerous_hits / heavy_hits / mutation_hits / bypass_used / elapsed_ms / undo_group / exception）。

**超时**：执行超时**不会**自动 `hou.undos.performUndo()`，避免误回滚正常操作。客户端需根据 `_audit.elapsed_ms` 自行决定。

---

## AI 调用 hou API 的硬约束

> 任何 AI agent 通过 `execute_code` 调用 hou API 之前 **MUST** 先 verify。`hou` 是 C 扩展，跨 major version 间会重命名 / 废弃 / 新增方法。假定跨版本 hou API 等价 = bug 风险（hang / type-check 失败 / 行为不一致）。

**正确工作流**：

1. **调 `verify_hou_api('Class.method')` 先看 `_ai_hint`**，绝不直接把假设的 hou API 写进 `execute_code` 的 `code` 参数
2. 若返 `status="success", methods=[]`（API 不存在），改用其他等价 API（例如在 SOP 子节点设 display/render flag，而不在 OBJ 容器调不存在的 setDisplayNode）
3. 若返 `status="success"` 且 `_ai_hint` 提到 thread 安全 caveat（如 `ObjNode.setInput` 需 input_index + item + output_index 三参），谨慎评估是否值得在 worker thread 冒险

### 三级 fallback（F0 → F1 → F2 → F3）

按优先级从高到低：

- **F0 — 判断 hou 版本**：verification 第一步必须先 `hou.version()` 确认 major version，因为 hou API 在跨 major 时会重命名 / 废弃 / 新增
- **F1 本地 hou help**（优先，无网络依赖，最快）：调 `verify_hou_api(item_name=...)`；若需进一步信息，`hou.node(path).help()`（已存在节点）或 `execute_code` 跑 `help(hou.<Class>.<method>)`
- **F2 联网 SideFX 文档**（F1 拿不到时）：`verify_hou_api(item_name="<Class>.<method>", help_type="python_hou")` 走 stdlib `urllib.request` 抓 `https://www.sidefx.com/docs/houdini/hom/hou/<name>.html`；不引入新 pip 依赖
  - **local-help-first（自动）**：`get_houdini_help` / `verify_hou_api` 优先打 Houdini 本地 help server（默认 `http://127.0.0.1:48626/`），本地不可达 / 超时 / 白屏（HTTP 200 但内容无效）时**自动回退在线**。返回 `_source` 字段（`"local"` / `"online"` / `""`）告知实际命中方，`_fallback_reason` 说明回退原因。健康缓存：本地失败后 60s cooldown 内跳过本地直查在线
- **F3 让用户开梯子**（F2 返 `status="error"` 且 `reason` 含网络关键字时）：AI agent 必须在输出里**显式**写出"⚠ SideFX 文档站不可达，请检查网络/梯子，或在 Houdini 内用 `hou.helpServerUrl()` 查本地帮助"

跨工具说明：底层 = `get_houdini_help`；AI-friendly wrapper = `verify_hou_api`。建议优先用 `verify_hou_api` 调 hou API，`get_houdini_help` 用于 SOP/OBJ 节点本身或 vex_function 查询。

> 详细复盘 / postmortem（含 2026-07-21 `ObjNode.setInput` hang 案例）见 `CHANGELOG.md`。

---

## Configuration

| 环境变量 | 默认 | 作用 | 适用工具 |
|----------|------|------|----------|
| `HOUDINI_MCP_ALLOW_BYPASS` | 未设 | `privileged` policy 启用开关（**不设则任何 bypass 请求都失败**） | `execute_code` |
| `HOUDINI_MCP_ENV_DIR` | 见下方约定 | embedded env 目录**绝对路径**覆盖；未设时从 package 目录名自动派生（`<dirname>-env/`，与 package 平级） | `_env_dir()`（3 处 prod + 2 处 test） |
| `HOUDINI_MCP_LOCAL_HELP_URL` | `http://127.0.0.1:48626/` | 本地 help server base URL | `get_houdini_help` / `verify_hou_api` |
| `HOUDINI_MCP_LOCAL_HELP_TIMEOUT` | `2.5` | 本地探测短超时（秒，clamp `[0.5, 30.0]`） | `get_houdini_help` / `verify_hou_api` |
| `HOUDINI_MCP_LOCAL_HELP_COOLDOWN` | `60` | 本地失败后 cooldown 窗口（秒，clamp `[0.0, 600.0]`） | `get_houdini_help` / `verify_hou_api` |
| `HOUDINI_MCP_LOCAL_HELP_DISABLE` | 未设 | `1` / `true` / `yes` / `on` 时完全禁用 local-first，退化到"仅在线" | `get_houdini_help` / `verify_hou_api` |
| `RAPIDAPI_KEY` | 未设 | OPUS 资产库 API key | `_opus.py` |
| `RAPIDAPI_HOST` | `opus5.p.rapidapi.com` | OPUS API host | `_opus.py` |
| `RAPIDAPI_HOST_URL` | `https://opus5.p.rapidapi.com/` | OPUS API base URL | `_opus.py` |

**`HOUDINI_MCP_ENV_DIR` 派生约定**：

```
package at  <parent>/<dirname>/                 env 派生为  <parent>/<dirname>-env/

examples:
  opera-houdini-mcp/                            opera-houdini-mcp-env/
  external/houdinimcp/                          external/houdinimcp-env/
  external/mcp/                                 external/mcp-env/
  D:/我的项目/external/houdinimcp/              D:/我的项目/external/houdinimcp-env/
```

- **相对路径的 override 会被静默忽略**（fallback 到默认派生），因为 bridge 进程的 cwd 取决于 AI 工具怎么 spawn 它（Claude Desktop / Cursor / ZCode 各不相同），相对路径不可靠
- 99% 的场景**不需要设这个变量**，保持默认派生即可

### OPUS 集成（可选）

OPUS 提供大量家具 / 环境程序化资产。订阅步骤：

1. 注册 [RapidAPI](https://rapidapi.com/) 账号
2. 订阅 [OPUS API](https://rapidapi.com/genel-gi78OM1rB/api/opus5/pricing)
3. 复制本地配置文件：

```bash
cp urls.env.example urls.env  # urls.env 已在 .gitignore
```

4. 编辑 `urls.env` 填入 key：

```env
RAPIDAPI_HOST_URL=https://opus5.p.rapidapi.com/
RAPIDAPI_HOST=opus5.p.rapidapi.com
RAPIDAPI_KEY=<your-key>
```

> **不设 key 时 server 仍可启动，仅 OPUS 工具被禁用**。OPUS 集成是可选的。

---

## Upstream Sync Policy

| 字段 | 值 |
|------|-----|
| 上游仓库 | [`capoomgit/houdini-mcp`](https://github.com/capoomgit/houdini-mcp) |
| 同步基线 | `de4fd93`（2026-07-17） |
| 同步方式 | **cherry-pick only**（禁止 merge） |
| 提交规范 | 标题前缀 `[opera]`，正文引用上游 PR 号（如 `[upstream PR #42]`） |
| 同步窗口 | 手动触发，每次有上游合入后 7 天内 |

**为什么禁止 merge**：保持 opera 自身的提交图干净可审计，区分"上游原样"与"opera 独有"的改动点。

**贡献到上游**：opera 独有的改进建议优先以 PR 形式回提给 `capoomgit/houdini-mcp`，合入后再 cherry-pick 回来。这样全社区都能受益。

---

## Testing

```bash
# 单元测试（不依赖 Houdini）
cd tests
pytest test_common.py test_execute_code_safety.py test_help.py \
       test_three_tier_fallback.py test_verify_hou_api.py

# Live smoke（依赖运行中的 Houdini 21+）
pytest tests/h21_live_*.py

# 完整回归
pytest tests/phase5_full_regression.py
```

测试基线：Houdini 21.0 + Python 3.11。详细 fixture / 共享 helper 见 `tests/conftest.py` 与 `tests/_e2e_helpers.py`。

---

## Troubleshooting

| 现象 | 排查 | 修复 |
|------|------|------|
| AI 连不上 9876 | `netstat -an \| findstr 9876` | 关防火墙，或在 shelf 重新 Start MCP |
| License 相关 | Houdini license server 状态 | `hkey -n` 看 license，Houdini 21 试用版过期需要重新申请 |
| 升级后工具找不到 | Houdini 还加载着旧 plugin | 在 shelf 点 Stop MCP → 重启 Houdini → 点 Start MCP |
| `get_houdini_help` 失败 | 本地 help server（`127.0.0.1:48626`）是否可达 + 网络是否能访问 `www.sidefx.com` | 看 `_source` / `_fallback_reason`：`online` + `local_*` 说明本地挂了已自动回退在线；两边都挂设 `HOUDINI_MCP_LOCAL_HELP_DISABLE=1` 走纯在线，详见 `_help.py` |
| `execute_code` 永远 `bypass_used=false` | 服务端 `HOUDINI_MCP_ALLOW_BYPASS` 未设 | 服务端 `export HOUDINI_MCP_ALLOW_BYPASS=1` 并重启 server |
| `import houdinimcp` 找不到 | `external/` 不在 `sys.path` | 按 [Embedding §3](#3-启动-server) 把 `external/` 加进 `sys.path` |

---

## Edge Cases & 集成陷阱

### 改 package 目录名

env 路径自动从 package 目录名派生（`<dirname>-env/`）。**改了 package 目录名要同步改 env 目录名**，或用 `HOUDINI_MCP_ENV_DIR` 指过去。env 内的 Python + 依赖无需重装，**纯文件系统 rename 即可**：

```bash
mv external/houdinimcp external/opera-houdini-mcp
mv external/houdinimcp-env external/opera-houdini-mcp-env   # 跟着改
```

### 多个项目共享一个 env

每个项目派生的 env 路径按各自 package 目录名走，默认不会共享。多项目共享：

```bash
export HOUDINI_MCP_ENV_DIR=/shared/envs/opera-houdini-mcp-env
```

**建议绝对路径**。相对路径的 override 会被静默忽略（bridge 进程的 cwd 取决于 AI 工具怎么 spawn，跨进程不稳定）。

### Windows 目录大小写

Windows 不区分大小写但 git checkout 保留原始大小写。如果你在 Windows 上 clone 后看到 `Houdinimcp/` 而代码 baseline 用 `houdinimcp/`，basename 派生会按实际拼写走，可能导致 env 路径大小写不一致：

```powershell
# 强制 git 严格大小写敏感
git config core.ignorecase false
# 如已 checkout 成错误大小写，重命名
Rename-Item external/Houdinimcp external/houdinimcp
```

### env 跨 OS 不通用

env 内嵌的 Python 是平台相关 wheel（`cp311-cp311-win_amd64` 这种）。**Windows env 拷到 Linux 跑不了**，反之亦然。跨 OS 迁移必须重装。

### 改 env 路径不需要重装依赖

env 改名、移位置、改 owner，**依赖本身可以原地保留**：

```bash
mv external/houdinimcp-env /new-location/my-env
export HOUDINI_MCP_ENV_DIR=/new-location/my-env
```

无需 `pip install`，无需重下 Python 包。

### git worktree / 多分支并行

每个 worktree 派生独立 env，不会互窜。共享 env 见上方「多个项目共享一个 env」。

### 权限与只读 env

env 目录存在但**不可写**（网络盘权限、只读 checkout）时，`_consent_dir()` 的 `os.makedirs` 兜底会抛 `PermissionError`。安装时确保 env 所在目录对运行用户可写。

### `pip install -e .` 会污染全局

不要把 opera-houdini-mcp 装到全局 Python。env 是隔离的，依赖会随 env 走；全局装会污染系统 Python，且后续覆盖会破坏 env 完整性。

---

## Contributing

1. Fork → 建分支 → commit（标题前缀 `[opera]`，正文引用对应上游 PR 号如有）
2. `pytest tests/` 全绿
3. PR 描述里说明：
   - 改动的 Tier 1 工具编号（如果适用）
   - 是否新增 pip 依赖（**不允许**，除非有充分理由并单独标注）
   - 是否动到不变量（见下）
4. 等 CI / 维护者 review

### 不变量清单（动到任一需先在 issue 里讨论）

| 不变量 | 值 |
|--------|-----|
| 监听端口 | `127.0.0.1:9876` |
| pip 依赖基线 | `mcp[cli]==1.12.2 + requests + python-dotenv` |
| MCP JSON key | `mcpServers.houdini` / `mcp.houdini` / `mcp_servers.houdini` |
| 公共 API | `start_server` / `stop_server` / `restart_server` / `is_server_running` / `initialize_plugin` |

---

## Security

- `execute_code` 是 LLM 驱动 Python 执行入口，三档 policy + 双开关 + 结构化 audit 是当前安全基线
- 漏洞报告：通过 GitHub Issue 的 **Private vulnerability reporting** 渠道（**不要**在公开 issue 里贴 PoC），或直接联系维护者

---

## License & Acknowledgement

本仓库全部代码沿用上游 [MIT License](./LICENSE)。`opera-houdini-mcp` 本身的改动部分同样以 MIT 协议发布。

Houdini-MCP 的最初设计参考了 [blender-mcp](https://github.com/ahujasid/blender-mcp)，感谢他们的贡献。`opera-houdini-mcp` 是 [capoomgit/houdini-mcp](https://github.com/capoomgit/houdini-mcp) 的独立 fork，遵循 MIT 协议，原版权归 Capoom 2025 所有。提交通过 cherry-pick 而非 merge 同步上游。
