# Changelog

`opera-houdini-mcp` 的所有改动记录。本文件按版本倒序排列，每次合入独立 PR 时追加。

---

## [Unreleased] · Tier 1 工具集（计划中）

> 本节列出计划合入的 13 个 Tier 1 模块。每次合入时把该子项从「计划中」移到下方「已合入」对应版本块。

### Fork rebrand (option B 全面重塑)

`opera-houdinimcp-rebrand-and-e2e-demo` change 的 rebrand 部分落地。Fork 不再绑定具体宿主仓库，作为可嵌入任意项目的 git submodule 独立存在。

- **README 重塑（option B full rebrand）**：
  - 删除「关于本 Fork」/「老用户升级路径」/「Acknowledgement」三个章节中对宿主仓库（CsrLib-Houdini）的指代与 GitHub 链接；
  - 「关于本 Fork」改名为「Why opera-houdini-mcp exists」，定位为对上游 `capoomgit/houdini-mcp` 的独立增强 fork；
  - 「老用户升级路径」改名为「Upgrading a submodule consumer」，给通用 `git submodule update --remote` 片段，不指代任何宿主；
  - 新增「Embedding in your project」章节：canonical submodule wiring + `__init__.py` import note，nest-aware 但不指代宿主；
  - 保留 Capoom 2025 + blender-mcp 的 Attribution；
  - TOC 与新增章节对齐。
- **Install / UI 重塑**（`scripts/python/Soren/mcp_control.py`，宿主侧）：
  - `_info()` 弹窗标题 `Houdini-MCP` → `Opera Houdini MCP`；
  - `_print_ai_tool_config()` 提示语以 `Opera Houdini MCP 安装完成` 开头；
  - `_install_embedded_python()` `_pth` 追加注释 `# Opera Houdini MCP 依赖目录`；
  - `_patch_mcp_win32_imports()` marker `# CsrLib-patched: win32 容错导入` → `# opera-houdini-mcp-patched: win32 容错导入`，对应 `_progress()` 日志字符串同步；
  - 模块 header docstring 重写为通用 submodule 消费者使用说明，去掉 CsrLib-Houdini 特定路径/升级提示。
- **Toolbar 按钮重塑**（`toolbar/default.shelf`，宿主侧，design.md §7 Option B）：
  - `MCPInstall` label → `Opera Houdini MCP Install`；
  - `MCPStart` label → `Start Opera MCP`；
  - `MCPStop` label → `Stop Opera MCP`；
  - 三按钮 `<script>` body 保持不变（仍调用 `mcp_control.install()` / `start()` / `stop()`）。
- **不变量保留**：submodule 路径 `external/houdinimcp/`、TCP `127.0.0.1:9876`、MCP JSON 键（`mcpServers.houdini` / `mcp.houdini` / `mcp_servers.houdini`）一字不动，老用户 AI 工具配置零改动。
- **变更下游影响**：消费者升级后，Houdini 内 shelf 上点 Install/Start/Stop 按钮的标签变为 Opera 品牌；弹窗标题与提示语同步；运行环境的 `pylibs/mcp/os/win32/utilities.py` 里 marker grep 字符串由 `CsrLib-patched` 改为 `opera-houdini-mcp-patched`（已有 marker 不会自动迁移——若 env 目录里留有旧 marker 文本，下次 Install 会重写为新 marker，因为代码里读的是固定字符串常量）。

### 计划中（按 PR 顺序）

- [ ] PR 3 — `_common.py` 基础设施（handle_connection_errors / validate_resolution / apply_response_cap / DANGEROUS_PATTERNS / HEAVY_GEOMETRY_PATTERNS / MUTATION_PATTERNS / _detect_dangerous_code / _detect_heavy_geometry_code / _detect_import_hou / _truncate_output / paginate_list / _json_safe_hou_value / _flatten_parm_templates / ExecutionTimeoutError）
- [ ] PR 4 — `execute_code` 安全强化（policy / bypass 双开关 / AST 别名检测 / threading + timeout / undo 守护 / 结构化 audit / get_last_scene_diff）
- [ ] PR 5 — `_scene.py`（get_scene_info / save_scene / load_scene / new_scene）
- [ ] PR 6 — `_discovery.py`（NodeTypeCache / list_node_types / list_children / find_nodes / manage_cache）
- [ ] PR 7 — `_materials.py`（create_material / assign_material / get_material_info）
- [ ] PR 8 — `_hscript.py`（execute_hscript）
- [ ] PR 9 — 图编辑增强（reorder_inputs / layout_children / set_node_position / set_node_color / create_network_box）
- [ ] PR 10 — `get_node_info` 增强（include_errors / force_cook / include_input_details / compact / cook_state）
- [ ] PR 11 — `find_error_nodes` 增强（include_warnings 默认开 + 单次 allSubChildren 扫描）
- [ ] PR 12 — `get_geo_summary`（counts / bbox / attributes / groups + 大几何降级）
- [ ] PR 13 — `_pane_capture.py`（capture_pane_screenshot / list_visible_panes / capture_multiple_panes / render_node_network）
- [ ] PR 14 — `_render_b64.py`（render_viewport_base64 / render_quad_views_base64，karma cpu/xpu）
- [ ] PR 15 — `_help.py`（get_houdini_help，stdlib html.parser 替代 beautifulsoup4）
- [ ] PR 16 — 连接诊断（check_connection / ping_houdini）

### 与上游（capoomgit/houdini-mcp）的分歧点

| 类别 | 分歧 | 原因 |
|------|------|------|
| 仓库结构 | 不再使用 GitHub fork 关系，独立仓库 | 避免与 capoomgit 上游操作产生关联 |
| 同步方式 | 仅 cherry-pick，禁止 merge | 保持 opera 自身的提交图干净可审计 |
| README | 中英混合，新增 Tier 1 工具清单 / 安全模型 / 故障排查章节 | CsrLib-Houdini 工作流文档化 |
| CHANGELOG | 新增本文件 | 与上游的改动点显式可追溯 |
| LICENSE | 原样保留（MIT, Capoom 2025） | 协议义务 |
| Tier 1 工具 | 13 个新模块 + `_common.py` 基础设施 | CsrLib-Houdini 生产需求 |
| execute_code 安全 | 三档 policy + bypass 双开关 + AST 别名检测 + 结构化 audit | 防止 LLM 误操作破坏场景 |
| `get_houdini_help` | 用 stdlib `html.parser` 替代 `beautifulsoup4` | 零新增 pip 依赖 |
| `apply_response_cap` | 默认 16KB 二分截断 | base64 PNG 不撑爆 MCP 响应 |
| `get_node_info` | 新增 `compact` / `force_cook` / `include_input_details` | 控制响应大小 + 按需 cook |

### 不变更的不变量

- 监听端口：`localhost:9876`
- pip 依赖：`mcp[cli]==1.12.2` + `requests` + `python-dotenv`（**不新增任何 pip 依赖**）
- Shelf 按钮脚本：与上游 `shelf_tool_start_mcp.py` / `shelf_tool_stop_mcp.py` 兼容
- AI 工具 JSON 配置：在 CsrLib-Houdini 中以 submodule 形式消费，路径 `external/houdinimcp/`，老用户配置零改动

---

## 0.1.0-opera · 2026-07-17 · Fork 初始化

### 已合入

- 仓库初始化：从 `capoomgit/houdini-mcp` @ `de4fd93` 全量克隆并推送，独立仓库 `ChengZiiii/opera-houdini-mcp`
- README 重写：中英混合，新增「关于本 Fork」/「Tier 1 工具清单」/「execute_code 安全模型」/「Troubleshooting」/「Acknowledgement（fork 关系）」章节
- CHANGELOG 新建（本文件）
- LICENSE 原样保留（MIT, Capoom 2025）

### 与上游基线对比

- 起点：`capoomgit/houdini-mcp` @ `de4fd93acc207fc57c02b330d421461f5963a945`（main HEAD）
- 终点：`ChengZiiii/opera-houdini-mcp` @ 同 `de4fd93`（main HEAD），叠加 README + CHANGELOG 两个新提交
- 代码改动：**零**。所有 Tier 1 工具在后续 PR 中以独立 commit 形式叠加。