# opera-houdini-mcp · Houdini MCP 的 Opera Fork

> 本仓库是 [`capoomgit/houdini-mcp`](https://github.com/capoomgit/houdini-mcp) 的独立 fork，作为可嵌入任意项目的 git submodule 使用。MIT 协议完整保留，Capoom 2025 原版权声明与致谢保留在最末。
>
> 上游基线：`capoomgit/houdini-mcp` @ `de4fd93`（2026-07-17 同步）。
> 同步策略：以 cherry-pick 为主，禁止 merge（保持 opera 自身的提交图干净可审计）。

---

## Why opera-houdini-mcp exists

`opera-houdini-mcp` 是 `capoomgit/houdini-mcp` 的独立增强 fork，专注 Tier 1 工具集与代码执行安全。它在功能上与上游完全兼容，但额外提供：

- **Tier 1 工具集**（13 个独立模块，详见 `Tier 1 工具清单`）：
  场景 CRUD、节点发现、图编辑增强、`get_node_info` 增强、材质、几何摘要、错误节点扫描（含 warnings）、`execute_code` 安全护栏、pane 截图与 base64 渲染、SideFX 在线文档查询、连接诊断、缓存管理、基础设施（`_common.py`）。
- **execute_code 安全模型**：三档 policy（read-only / normal / privileged）+ dangerous / heavy / mutation 三套模式黑名单（正则 + AST 别名双检）+ 双开关 bypass（请求端 `allow_dangerous` 配服务端环境变量 `HOUDINI_MCP_ALLOW_BYPASS`）+ 结构化 audit。
- **零新增 pip 依赖**：`get_houdini_help` 用 stdlib `html.parser` 替代 `beautifulsoup4`，仍然保持 `mcp[cli]==1.12.2 + requests + python-dotenv` 三件套。

For the full change history see `CHANGELOG.md`.

### Upgrading a submodule consumer

If you consume this fork as a git submodule in your project, bump it with the canonical submodule flow:

```bash
git submodule update --remote <submodule-path>
git submodule sync
```

无需重装 env（每个第三方工具的运行环境独立放在 `external/<工具名>-env/` 下，与 `<工具名>/` 源码完全解耦，互不影响）。

---

## Table of Contents

1. [Requirements](#requirements)
2. [Houdini MCP Plugin Installation](#houdini-mcp-plugin-installation)
   1. [Folder Layout](#folder-layout)
   2. [Shelf Tool (Optional)](#shelf-tool-optional)
   3. [Packages Integration (Optional)](#packages-integration-optional)
3. [Installing the `mcp` Python Package](#installing-the-mcp-python-package)
   1. [Using uv on Windows](#using-uv-on-windows)
   2. [Using pip Directly](#using-pip-directly)
4. [Bridging Script and Claude for Desktop](#bridging-script-and-claude-for-desktop)
   1. [The Bridging Script](#the-bridging-script)
   2. [Telling Claude Desktop to Use Your Script](#telling-claude-desktop-to-use-your-script)
5. [Testing & Usage](#testing--usage)
6. [Tier 1 工具清单](#tier-1-工具清单)
7. [execute_code 安全模型](#execute_code-安全模型)
8. [Troubleshooting](#troubleshooting)
9. [Acknowledgement](#acknowledgement)
10. [Embedding in your project](#embedding-in-your-project)

---

## Requirements

- **SideFX Houdini**
- **uv**
- **Claude Desktop** (latest version)

---

## 1. Houdini MCP Plugin Installation

### 1.1 Folder Layout

Create a folder in your Houdini scripts directory:
`C:/Users/YourUserName/Documents/houdini19.5/scripts/python/houdinimcp/`

Inside **`houdinimcp/`**, place:

- **`__init__.py`** – handles plugin initialization (start/stop server)
- **`server.py`** – defines the `HoudiniMCPServer` (listening on port `9876`)
- **`houdini_mcp_server.py`** – optional bridging script (some prefer a separate location)
- **`pyproject.toml`**

*(If you prefer, `houdini_mcp_server.py` can live elsewhere. As long as you know its path for running with `uv`.)*

### 1.2 Shelf Tool

create a **Shelf Tool** to toggle the server in Houdini:

1. **Right-click** a shelf → **"New Shelf..."**

   Name it "MCP" or something similar

2. **Right-click** again → **"New Tool..."**
   Name: "Toggle MCP Server"
   Label: "MCP"

3. Under **Script**, insert something like:

```python
   import hou
   import houdinimcp

   if hasattr(hou.session, "houdinimcp_server") and hou.session.houdinimcp_server:
       houdinimcp.stop_server()
       hou.ui.displayMessage("Houdini MCP Server stopped")
   else:
       houdinimcp.start_server()
       hou.ui.displayMessage("Houdini MCP Server started on localhost:9876")
```

### 1.3 Packages Integration

If you want Houdini to auto-load your plugin at startup, create a package file named `houdinimcp.json` in the Houdini packages folder (e.g. `C:/Users/YourUserName/Documents/houdini19.5/packages/`):

```json
{
  "path": "$HOME/houdini19.5/scripts/python/houdinimcp",
  "load_package_once": true,
  "version": "0.1",
  "env": [
    {
      "PYTHONPATH": "$PYTHONPATH;$HOME/houdini19.5/scripts/python"
    }
  ]
}
```

### 2 Using uv on Windows

```powershell
  # 1) Install uv
  powershell -c "irm https://astral.sh/uv/install.ps1 | iex"

  # 2) add uv to your PATH (depends on the user instructions) from cmd
  set Path=C:\Users\<YourUserName>\.local\bin;%Path%

  # 3) In a uv project or the plugin directory
  cd C:/Users/<YourUserName>/Documents/houdini19.5/scripts/python/houdinimcp/
  uv add "mcp[cli]"

  # 4) Verify
  uv run python -c "import mcp.server.fastmcp; print('MCP is installed!')"
```

### 3 Telling Claude for Desktop to Use Your Script

Go to File > Settings > Developer > Edit Config >
Open or create: `claude_desktop_config.json`

Add an entry:

```json
{
  "mcpServers": {
    "houdini": {
      "command": "uv",
      "args": [
        "run",
        "python",
        "C:/Users/<YourUserName>/Documents/houdini19.5/scripts/python/houdinimcp/houdini_mcp_server.py"
      ]
    }
  }
}
```

if uv run was successful and claude failed to load mcp, make sure claude is using the same python version, use:

```cmd
  python -c "import sys; print(sys.executable)"
```

to find python, and replace "python" with the path you got.

### 4 Use Cursor

Go to Settings > MCP > add new MCP server
add the same entry in `claude_desktop_config.json`
you might need to stop claude and restart houdini and the server

### 5 OPUS integration

OPUS provide a large set of furniture and environmental procedural assets.
you will need a Rapid API key to log in. Create an account at: [RapidAPI](https://rapidapi.com/)
Subscribe to OPUS API at: [OPUS API Subscribe](https://rapidapi.com/genel-gi78OM1rB/api/opus5/pricing)
Get your Rapid API key at [OPUS API](https://rapidapi.com/genel-gi78OM1rB/api/opus5)
copy `urls.env.example` to `urls.env` and add your key (the file is gitignored).
OPUS integration is optional — without a key the server still starts, only the OPUS tools are disabled.

---

## Tier 1 工具清单

> 以下工具以独立 PR 形式陆续合入。每次合入后会在 `CHANGELOG.md` 追加记录。本节列出最终交付时的目标清单。

| 类别 | 工具名 | 说明 |
|------|--------|------|
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
| 文档 | `get_houdini_help` | **本地 help server 优先** + 在线 SideFX 回退（urllib + stdlib html.parser）；返 `_source`/`_fallback_reason` |
| 文档 | `verify_hou_api` | python_hou 默认 + `_ai_hint` 合成 | AI-friendly wrapper over `get_houdini_help`（PR 18），自动继承 local-first |
| 诊断 | `check_connection` / `ping_houdini` | 不持久化连接的 ping |
| 缓存 | `manage_cache` | stats / invalidate / warmup |

---

## 强制约束：AI 调用 hou API 前必须查文档

> **任何 AI agent 通过 `execute_code` 调用 hou API 之前 MUST 先 verify。**

`hou` 是 C 扩展， 不同 major version 间会重命名 / 废弃 / 新增方法。 假定跨版本 hou API 等价 = bug 风险（hang / type-check 失败 / 行为不一致）。

### F-C bug 案例（2026-07-21）

orchestrator 在 `execute_code` 中尝试 `obj.setInput(0, sop, 0)`， 假定 `ObjNode.setInput` 与 `SopNode.setInput` 签名等价。 实际调用触发 hou 内部 type-check， 在 MCP worker thread 同步执行， Houdini **整进程 hang 30s+**， 最终返 `timed_out=True`， 且 `serialize_scene_state` 在同一 worker thread 排队导致 scene 状态无法快照。 诊断： 不在 doc 里假设签名 = 直接踩雷。

正确工作流：

1. **调 `verify_hou_api('ObjNode.<method>')` 先看 `_ai_hint`**， 绝不直接把假设的 hou API 写进 `execute_code` 的 `code` 参数；
2. 若返 `status="success", methods=[]`（API 不存在）， 走 `sop.setDisplayFlag(True) + sop.setRenderFlag(True)` 改在 SOP 子节点设 display/render flag， **不要**在 OBJ 容器调不存在的 setDisplayNode；
3. 若返 `status="success"` 且 `_ai_hint` 提到 thread 安全 caveat（如 `ObjNode.setInput` 需 input_index + item + output_index 三参）， 谨慎评估是否值得在 worker thread 冒险， 优先改用 SOP 子节点 flag。

### 三级 fallback 指南（F0 → F1 → F2 → F3）

按优先级从高到低：

- **F0 — 判断 hou 版本**： verification 第一步 MUST 先 `hou.version()` 确认 major version， 因为 hou API 在跨 major 时会重命名 / 废弃 / 新增。 同一份代码在 H20 / H21 / H22 行为可能不同。
- **F1 本地 hou help（优先， 无网络依赖， 最快）**： 调 `verify_hou_api(item_name=...)` 优先； 若 F2（网络侧）返 `status="error"`， 回退到本地：
  - `hou.node(item_path).help()` 对**已存在**的节点有效（拿到的是 SideFX 同步到本地的 help server 内容）；
  - `print(hou.<Class>.<method>.__doc__)` 或 `execute_code` 跑 `help(hou.<Class>.<method>)`， 把 stdout 拿到后自己解析 Python docstring。
- **F2 联网 SideFX 文档（F1 拿不到时）**： 调 `verify_hou_api(item_name="<Class>.<method>", help_type="python_hou")`， fork 的 PR 15 `get_houdini_help` 走 stdlib `urllib.request` 抓 `https://www.sidefx.com/docs/houdini/hom/hou/<name>.html`； 不引入新 pip 依赖。
  - **local-help-first（自动）**：`get_houdini_help` / `verify_hou_api` **优先**打 Houdini 本地 help server（GUI 启动时自带，默认 `http://127.0.0.1:48626/`，与在线同源 Sphinx build），本地不可达 / 超时 / 白屏（HTTP 200 但内容无效）时**自动回退在线**。返回 `_source` 字段（`"local"` / `"online"` / `""`）告知实际命中方，`_fallback_reason` 说明回退原因。健康缓存：本地失败后 60s cooldown 内跳过本地直查在线，避免每次都打白屏。
- **F3 让用户开梯子（F2 返 `status="error"` 且 `reason` 含网络关键字时）**： AI agent MUST 在自己输出里**显式**写出 "⚠ SideFX 文档站不可达（verify_hou_api 返 `status="error"` reason=`<reason>`）， 请检查网络/梯子， 或在 Houdini 内用 `hou.helpServerUrl()` 查本地帮助。"

跨工具说明： 底层 = `get_houdini_help`（PR 15）； AI-friendly wrapper = `verify_hou_api`（PR 18）。 建议优先用 `verify_hou_api` 调 hou API， `get_houdini_help` 用于 SOP/OBJ 节点本身或 vex_function 查询。

### 帮助查询环境变量（local-help-first-fallback）

进程启动时读一次（MCP server import `_help.py` 时）：

| 环境变量 | 默认 | 作用 |
|----------|------|------|
| `HOUDINI_MCP_LOCAL_HELP_URL` | `http://127.0.0.1:48626/` | 本地 help server base URL（多 Houdini 实例 / 自定义端口时覆盖） |
| `HOUDINI_MCP_LOCAL_HELP_TIMEOUT` | `2.5` | 本地探测短超时（秒，clamp `[0.5, 30.0]`）；白屏/卡顿时快速回退在线 |
| `HOUDINI_MCP_LOCAL_HELP_COOLDOWN` | `60` | 本地失败后 cooldown 窗口（秒，clamp `[0.0, 600.0]`），窗口内跳过本地直查在线 |
| `HOUDINI_MCP_LOCAL_HELP_DISABLE` | 未设 | `1`/`true`/`yes`/`on` 时完全禁用 local-first，退化到"仅在线"（`_source` 为 `""`） |

---

## execute_code 安全模型

| Policy | mutation | dangerous | heavy_geometry | import hou | 默认 bypass |
|--------|----------|-----------|----------------|------------|-------------|
| `read-only` | **拒绝**（命中 mutation 正则/AST） | 拒绝 | 拒绝 | 拒绝 | — |
| `normal`（默认） | 允许 | 拒绝（除非 `allow_dangerous=True`） | 拒绝（除非 `allow_heavy_geometry=True`） | 提示 | 仅在客户端显式开启 |
| `privileged` | 允许 | 允许（必须同时开启 `allow_dangerous=True` **和** `HOUDINI_MCP_ALLOW_BYPASS=1`） | 允许（必须同时开启 `allow_heavy_geometry=True` **和** `HOUDINI_MCP_ALLOW_BYPASS=1`） | 允许 | 必须服务端环境变量 |

**双开关原则**：任何 dangerous / heavy / privileged 操作都需要「请求端参数 + 服务端环境变量」同时开启。服务端不开环境变量，再多客户端请求也无效。

**Audit**：每次 `execute_code` 调用都会在响应里附 `_audit` 块（policy / dangerous_hits / heavy_hits / mutation_hits / bypass_used / elapsed_ms / undo_group / exception）。

**超时**：执行超时**不会**自动 `hou.undos.performUndo()`，避免误回滚正常操作。客户端需根据 `_audit.elapsed_ms` 自行决定。

---

## Troubleshooting

| 现象 | 排查 | 修复 |
|------|------|------|
| MCP Install 按钮失败 | 检查 Houdini Python 版本与 uv 版本 | 重装 uv，重启 Houdini |
| AI 连不上 9876 | `netstat -an | findstr 9876` | 关防火墙，或在 shelf 重新 Start MCP |
| License 相关 | Houdini license server 状态 | `hkey -n` 看 license，Houdini 21 试用版过期需要重新申请 |
| 升级后工具找不到 | Houdini 还加载着旧 plugin | 在 shelf 点 Stop MCP → 重启 Houdini → 点 Start MCP |
| `get_houdini_help` 失败 | 本地 help server（`127.0.0.1:48626`）是否可达 + 网络是否能访问 `www.sidefx.com` | 看 `_source`/`_fallback_reason`：`online`+`local_*` 说明本地挂了已自动回退在线；两边都挂设 `HOUDINI_MCP_LOCAL_HELP_DISABLE=1` 走纯在线，详见 `_help.py` |

---

## Acknowledgement

Houdini-MCP was built following [blender-mcp](https://github.com/ahujasid/blender-mcp). We thank them for the contribution.

opera-houdini-mcp 是 [capoomgit/houdini-mcp](https://github.com/capoomgit/houdini-mcp) 的独立 fork，遵循 MIT 协议，原版权归 Capoom 2025 所有。提交通过 cherry-pick 而非 merge 同步上游。

---

## Embedding in your project

opera-houdini-mcp is designed to live inside another project as a git submodule. The canonical wiring is:

```bash
# 从你的项目根目录执行
git submodule add <opera-houdini-mcp-url> external/houdinimcp
git submodule update --init --recursive
```

之后在你的项目里就可以直接 import 包（前提是把 `external/` 加进 `sys.path`）：

```python
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "external"))

from houdinimcp import start_server, is_server_running
```

每个第三方工具的运行环境独立放在 `external/<工具名>-env/` 下（内含 `python/` 与 `pylibs/`），与 `<工具名>/` 源码完全解耦。这样未来新增其他第三方工具时，各自环境互不冲突。

---

## License

本仓库全部代码沿用上游 [MIT License](./LICENSE)。`opera-houdini-mcp` 本身的改动部分同样以 MIT 协议发布。