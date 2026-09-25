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

- [ ] PR 3 — `_common.py` 基础设施（handle_connection_errors / validate_resolution / apply_response_cap / DANGEROUS_PATTERNS / HEAVY_GEOMETRY_PATTERNS / MUTATION_PATTERNS / _detect_dangerous_code / _detect_heavy_geometry_code / _detect_import_hou / _truncate_output / paginate_list / _json_safe_hou_value / _flatten_parm_templates；超时异常类已由 perf-mcp-round3 §4 移除）
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
- [x] PR 18 — AI hou-API verify 纪律（README 新增「强制约束」章节 + CHANGELOG 本行 + 测试 `tests/test_verify_hou_api.py` + `tests/test_three_tier_fallback.py`；bridge tool `verify_hou_api` 与 `_synthesize_ai_hint` 在 Wave B 合入）
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

## 0.5.0-opera · 2026-09-26 · feat-mcp-console-log-audit + feat-mcp-tool-guidance（console 日志 / 命令审计 / AI 调用引导）

> **状态**：已合入。openspec changes `feat-mcp-console-log-audit`（§1-§3）与 `feat-mcp-tool-guidance`（§1-§4）。

- **Console 日志（server 侧，change A）**：`_console_log.py` tee 环形缓冲（默认 4000 行，`HOUDINI_MCP_CONSOLE_LOG_LINES`）包装 Python 层 `sys.stdout` / `sys.stderr`；`execute_code` 执行期输出经 `append_capture` 接口同步入缓冲（不绕 tee）；`HOUDINI_MCP_CONSOLE_LOG=0` 总开关。新工具 `get_console_log`（READ_ONLY，offset/limit/tail/last_seconds 分页，时间窗优先）+ `clear_console_log`（NO_UNDO，返回清空前条数）。覆盖边界：仅 Python 层（节点 PythonModule / execute_code / server 自身日志），C++ 层直写不在范围。
- **命令审计（bridge 侧 JSONL，change A）**：`_audit_log.py` 包装 `ToolManager.call_tool`（含 protocol path 的 `list[TextContent]` 与 direct path 的 dict 双形态 `classify_result`），全部工具每次调用 append 一行 JSON 到 `$TEMP/houdini_mcp/audit/audit-<yyyymmdd>-<seq>.jsonl`（ts/session_id/tool/ok/duration_ms/args≤256，失败附 error_code/error_message）；空闲 15 分钟开新段、保留 30 段；落盘失败仅 warning 不影响调用；v1 只记录不重放。
- **Tool annotations 三分类映射（change B）**：`_tool_annotations.py` 启动时后处理全部工具 `annotations`——server READ_ONLY 命令 → `readOnlyHint=true`（+idempotent）；破坏性 12 工具（delete_node / load_scene / new_scene / execute_houdini_code / hda 装缷 / cache 清写等）→ `destructiveHint=true`；其余变更类显式双 false。对账测试与 server 三分类双向守卫（含 7 对 bridge↔server 改名映射）。显式全量设置规避 mcp 1.12.2 `destructiveHint` 默认 true 坑。
- **instructions 行为契约（change B）**：`initialize` 可见 instructions 重写为 ≤1200 字符四要点契约（专用工具优先 / execute_code 最后手段、verify_hou_api 前置、遇错先查 search_lessons/get_best_practices、渲染 start_render + capture_pane_screenshot 取证）。
- **execute_code 失败引导（change B）**：`_exec_hint.py` 返回文本含 `hou.*` 异常模式（Traceback / Stderr 段）时追加 `_ai_hint:` 行（提取 API 名去重 ≤3 + verify 指引，零噪音门控）；会话计数达阈值（默认 5，`HOUDINI_MCP_EXEC_HINT_THRESHOLD`，0 关闭）起每次追加 `_hint:` 专用工具指引行。hint 为文本行追加，返回形态仍 str。
- **描述全量瘦身 + lint（change B）**：全部工具 docstring 两层拆分——调用方信息留描述，实现备忘（PR 编号 / 设计史 / 路径怪癖）搬家函数体注释（信息搬家不裁剪）；`tests/test_tool_description_lint.py` 守卫 pattern 禁令（`PR \d` / issue 号 / change 代号 / `§`）+ 长度上限（默认 ≤1200，历史 8+12 工具更严特例）+ help 类首行触发时机 + 备忘搬家抽查（20 工具跨五批）。知识库工具按基线 spec 保留紧凑版注解关键词（主动沉淀触发 / 加深方法论 / 不替代）。
- **执行链顺序**：`apply_tool_annotations(mcp)` → `_install_capture_hook()` → `install_audit_hook(mcp)`（audit 最外层，duration 覆盖全链）。
- **工具数**：174 → 176（+get_console_log / clear_console_log）。基线：全量 pytest **2298 passed / 1 skipped / 0F**（74 测试文件）；hython 隔离实机 13/13（console log server 侧）；实机验收见主仓 CHECKPOINTS。

---

## 0.4.0-opera · 2026-09-10 · feat-mcp-round2-hardening（主线程 execute_code / 渲染治理 / 知识库并发 / RAG 接线）

> **状态**：已合入。openspec change `feat-mcp-round2-hardening`（§1-§4）。

- **§1 execute_code 主线程同步执行**：worker 线程模型 → QTimer dispatch 内直接执行（与其他 MUTATING 命令同路径）；`normal` / `privileged` 包 `hou.undos.group("MCP: execute_code (<policy>)")`，场景变更可经 `performUndo()` / Ctrl+Z **真回滚**（hython 隔离实例实证验收）；`timeout` 参数保留但不再生效（audit 恒 `timed_out=false` + `execution_mode="main_thread"` + `timeout_ignored=true`，工具描述明示"死循环脚本 = 服务不可用直至重启"）；`_run_code_thread` / grace poll dead code 移除；跨线程 stdout 进程级劫持窗口随之消失。
- **§2 渲染链路资源治理**：渲染 rig 临时节点（`MCP_CAM_CENTER` / `MCP_CAMERA` / `MCP_OGL_RENDER` / `MCP_*_KARMA` / `MCP_MANTRA`）渲染流程 finally 清理（成功/失败两路径，清理失败附 `_cleanup_warning` 不吞渲染结果；多视图单 rig 复用）；server 启动对精确已知名字做孤儿清扫并打日志；bridge `render_path` 默认 `None` → server 端 `default_capture_path`（`$TEMP/houdini_mcp/<日期>/`，7 天清理规范）；响应新增 `requested_renderer` + `actual_backend`（`opengl_rop` / `husk` / `flipbook` / `qscreen_fallback`，karma 截图回退不再单标 `karma_cpu`）。
- **§3 知识库写路径并发防护**：per-root 双层锁（进程内 `threading.Lock` + 跨进程 root 下 `.locks/` 锁文件，Windows `msvcrt.locking` / POSIX `fcntl` best-effort；获取失败 3×0.05s 重试后降级仅进程内锁 + 日志）；新建 lesson 文件 `O_CREAT|O_EXCL` + 撞号 id 序号 +1 重算（≤5 次，锁失效也零覆盖）；`capture_workflow_snapshot` 的 `probe_mode` 显式优先（`include_hda_internals` 仅在缺省时映射，conform 主 spec 既有 SHALL）；并发回归 12 用例（多线程 save 无同 id / strength 累积 / recipe 无块丢失 / 锁降级）。
- **§4 杂项包（RAG 接线 / 分页 / 文档同步）**：
  - `scripts/build_rag_index.py` zip 感知扫描 + wiki 文本解析器——H21 帮助源实况为 **zip 打包 wiki 文本**（nodes/vex/hom/expressions/commands 五核心 zip，~7800 条）；解析剥 `@parameters` 参数段与 `#key:` 元数据行、`#context/#internal/#tags` 值回灌检索 token、`= 标题 =` / `"""摘要"""` / `== 段名 ==` 提取；doc path 用 `<zip 名>/<entry>` 稳定 POSIX 标识；散装 HTML 目录模式保留（同目录混扫超集）；CLI 新增 `--source` / `--output` / `--zips`；0 doc 拒绝发布（保护既有索引）；实跑产出 7781 docs / avgdl 119 / ~14.6MB → `~/.opera-houdini-mcp/rag/index.v1.json`
  - `_rag.py` 默认索引位置解析序：`HOUDINI_MCP_RAG_INDEX_DIR` env → `~/.opera-houdini-mcp/rag/`（存在即用）→ 旧 fork 模块目录（向后兼容）；生成命令文档化（tests/README + 主仓库 docs/HOUDINI_MCP.md）
  - `list_material_types(category, limit=100, cursor=0)` 分页信封：limit clamp `[1,500]`、多取 1 判 `has_more`（lookahead 项不返回）、越界 cursor 空页 + `cursor=None`、信封含 `total`（全量翻页总和 == total，H21 实测 Vop 1321 项不再被 response cap 截断）；bridge 注解补 int
  - 文档同步：README（知识工具计数 4→6、base64 渲染入口已注销说明、配置表补 `HOUDINI_MCP_RAG_INDEX_DIR` / `HOUDINI_MCP_ALLOW_NEW_SCENE`）、本 CHANGELOG 补账、主仓库 `docs/HOUDINI_MCP.md`
- 基线：全量 pytest 2084 passed / 1 skipped / 0F。

---

## 0.3.0-opera · 2026-09-09 · fix-mcp 四连修（dead tools / H21 API 对齐 / help 链路 / 测试套件）

> **状态**：已合入。openspec changes `fix-mcp-dead-tools-p0` / `fix-mcp-h21-api-parity` / `fix-mcp-help-cap-protocol` / `fix-mcp-test-suite-repair`（均已归档）。

- **fix-mcp-dead-tools-p0**：bridge 数值/布尔参数补 `int` / `float` / `bool` 注解（无注解参数被旧 MCP client 拒收导致工具"全死"）；`NodeTypeCache` 复活（populate 幂等 + hits/misses/TTL 失效 + `manage_cache`）；list/find 发现类统一 `{field, count, total, has_more, cursor}` 信封分页（多取 1 判 has_more）。
- **fix-mcp-h21-api-parity**：13 项 H21 HOM API 对齐（`parm.isAtDefault()` 语义、GeometryViewport、`hipFile.isUntitled` 等），H21 live smoke 全绿。
- **fix-mcp-help-cap-protocol**：help 链路复活——`Class.method` 点号名自动拆分（不再直接拼 URL 双 404）、本地 HTTP 404 不进 cooldown（页面不存在是合法答案，直接回退在线）、本地探测预算 2.5→8s + gzip、methods 列表 ≤50、cap 降级契约（`_truncated` 标记）、`timed_out` 竞态与 socket 收发修复、`load_scene` 的 LoadWarning 结构化上报。
- **fix-mcp-test-suite-repair**：pytest 声明进 `[dependency-groups] dev`（`uv sync` 可恢复，不再被卸载）；conftest autouse fixture 默认把桥端口改死端口（`HOUDINI_MCP_TEST_ALLOW_LIVE=1` 才放行真机），测试不再泄漏到生产 9876。
- 基线：2019 passed / 0F。

---

## 0.2.0-opera · 2026-07-22 ~ 2026-08-07 · 工具大扩容 + 渲染 policy + 自进化知识库 + slim 工具集

> **状态**：已合入。本节汇总 2026-07-21（0.1.1）之后至 09-09 修复前的主体增量（对应 openspec archive 同期 changes）。

- **本地 help 优先（07-22）**：`get_houdini_help` / `verify_hou_api` 优先打 Houdini 本地 help server（`127.0.0.1:48626`），失败自动回退在线 SideFX；返 `_source` / `_fallback_reason`；同日 H21 compat audit 修正一批 H21 实机差异。
- **渲染 policy 与 consent（07-24）**：缺 OGL 3.3 环境下 opengl renderer 强制 redirect 到 `capture_pane_screenshot(SceneViewer)`（不再触发 opengl output node 链路，避免主线程死锁）；`karma_cpu` / `karma_xpu` 需 consent token 重调；新增 `start_render`（ROP 同步渲染，四层防御）+ `monitor_render`（husk / mantra OS 进程 best-effort 监控，bridge-only）；SceneViewer flipbook 视图采集修复。
- **20 个 add-* 工具模块（07-29）**：场景上下文/选择/材质发现（`get_network_overview` / `get_cook_chain` / `explain_node` / `get_scene_summary` / `get_selection` / `set_selection` / `list_materials` / `list_material_types` / `create_material_network`）、CHOPs、Copernicus、DOPs、几何导出与测量、HDA 管理、headless hython 按需启动、.hip 离线解析、Houdini 事件系统、MCP resources、节点/参数/VEX 工具（`create_wrangle` / `set_wrangle_code` / `validate_vex` 等）、PDG/TOPs、渲染 workflow（ifd/opengl/karmarender 白名单）、takes 与缓存、USD/Solaris、viewport 控制、动画与帧控制、batch + undo 分组；同日落地 BM25 文档 RAG（`search_docs` / `get_doc` / `_rag.py` + `build_rag_index.py`）、`get_best_practices` advisory recipes、OPUS 重构为可选依赖（无 key 时其余工具不受影响）。
- **save_scene 修复（08-01）**：untitled 会话保存返回结构化错误而非弹模态保存框（避免挂死单线程 MCP 管道）。
- **自进化知识库（08-02 ~ 08-03）**：`search_lessons` / `save_lesson` / `read_lesson` / `knowledge_stats` + 自动错误捕获（inbox 同指纹去重、≥3 次晋升 draft 骨架）；工作流知识捕获 `capture_workflow_snapshot` + `save_recipe`（分层探测 `probe_mode`、资产级标识 `type_full` / `is_hda`、禁止本机路径入正文）；场景工具 bridge 返回类型修复。
- **slim-mcp-toolset（08-02）**：注销 10 个冗余/别名工具的 `@mcp.tool()` 注册——含 `render_viewport_base64` / `render_quad_views_base64` / `render_specific_camera_base64`（恢复 = 取消对应装饰器注释），MCP 工具列表瘦身；server 端命令与协议保留。
- **团队 root 绝对路径（08-07）**：`config.json` 的 `path` 接受 `${VAR}` 占位符 / 相对路径 / 绝对路径（Windows 盘符、UNC `\\server\share`、前导 `\`），适配团队 NAS 盘符差异。

---

## 0.1.1-opera · 2026-07-21 · F-C bug 复盘 + AI 调 hou API 硬约束

> **状态**：已合入。补强 `execute_code` 安全护栏 + 桥接 `verify_hou_api` 工具。

### F-C bug 案例（2026-07-21）

orchestrator 在 `execute_code` 中尝试 `obj.setInput(0, sop, 0)`，假定 `ObjNode.setInput` 与 `SopNode.setInput` 签名等价。实际调用触发 hou 内部 type-check，在 MCP worker thread 同步执行，Houdini **整进程 hang 30s+**，最终返 `timed_out=True`，且 `serialize_scene_state` 在同一 worker thread 排队导致 scene 状态无法快照。

**根因**：AI agent 假定跨版本 / 跨类的 hou API 签名等价，未先 verify 直接写进 `execute_code` 的 `code` 参数。

**修复**：

- README 新增「AI 调用 hou API 的硬约束」章节，明确 verify-first 工作流
- 新增桥接工具 `verify_hou_api(item_name, help_type="python_hou")`（AI-friendly wrapper over `get_houdini_help`），返 `_ai_hint` 字段提示 thread 安全 caveat
- 测试覆盖：`tests/test_verify_hou_api.py` + `tests/test_three_tier_fallback.py`

**教训**：不在 doc 里假设签名 = 直接踩雷。`hou` 是 C 扩展，跨 major version 间会重命名 / 废弃 / 新增方法。

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