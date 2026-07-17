# Changelog

`opera-houdini-mcp` 的所有改动记录。本文件按版本倒序排列，每次合入独立 PR 时追加。

---

## [Unreleased] · Tier 1 工具集（计划中）

> 本节列出计划合入的 13 个 Tier 1 模块。每次合入时把该子项从「计划中」移到下方「已合入」对应版本块。

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