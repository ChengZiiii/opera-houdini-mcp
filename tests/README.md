# opera-houdini-mcp / tests

本目录是 fork `opera-houdini-mcp`（基于 capoomgit/houdini-mcp）自带的一组
**端到端测试脚本**——绕过 MCP bridge，直接走 Houdini 端的 TCP socket
(`127.0.0.1:9876`)，用 4 字节大端长度前缀 + UTF-8 JSON 协议与 server.py 对话。
所有脚本纯 stdlib，无 pytest / hou 依赖，可在任意 Python 3.7+ 跑（`ast.parse`
静态校验也跑得过）。

## 文件清单

| 文件 | 角色 | 状态 |
|------|------|------|
| `phase4_e2e.py` | 历史 Phase 4 端到端回归（10 步核心流程）。已 refactor 从 `_e2e_helpers` 导入 `HoudiniConn` / `StepResult` / `assert_step` / `emit_summary`。原先硬编码的 host-specific `_capture_paths.py` 绝对路径已修掉，改为 `os.path.join(os.path.dirname(__file__), "_capture_paths.py")` 的相对路径模式。 | pre-existing，Wave B 维护 |
| `phase5_full_regression.py` | Phase 5 全量回归脚本（22 步），覆盖 PR 4 / PR 11 / PR 13 / PR 14 等新增能力。 | pre-existing，out of scope for this change |
| `_e2e_helpers.py` | 共享 socket 客户端 + 步骤断言 + Markdown 汇总。被 phase4_e2e / phase5_full_regression / e2e_demo_table 共用。 | Wave B 新增 |
| `e2e_demo_table.py` | 程序化木桌 demo。从 0 构建一张 1.4×0.05×0.8 桌面 + 4 桌腿 + 2 横撑 + 木纹 wrangle + 木质 PBR 材质，再截 pane / Karma CPU 渲 4 视图 / 走 privileged audit。 | Wave A 启动、Wave B 完成 build + verify (4.4-4.7)、Wave C 完成 capture + render + audit + summary (4.8-4.11) |
| `README.md` | 本文件。 | Wave C 新增 |

## 跑 e2e_demo_table

从 fork 根目录跑（任一可 import `_e2e_helpers` 的 Python 即可）：

```powershell
cd external/houdinimcp
python tests/e2e_demo_table.py
```

或从父仓库根目录：

```powershell
python external\houdinimcp\tests\e2e_demo_table.py
```

或用 fork 自带的嵌入式 Python（如果有 `external/houdinimcp-env/`）：

```powershell
C:/.../external/houdinimcp-env/python/python.exe tests/e2e_demo_table.py
```

## 前置条件

- **Houdini 必须启动**，且 shelf 点了 **Start Opera MCP**（socket 监听
  `127.0.0.1:9876`，timeout=300s）。
- 如果 Houdini 不在 / MCP 没启，demo 会**干净地 SKIP**：
  stdout 打 `[skip] Houdini socket not reachable on 127.0.0.1:9876 — is Houdini running + MCP started?`，
  exit 0，不抛 traceback。这是预期行为，demo 是 smoke test，不是回归门禁。

## 预期运行时

- 全程 ≤ 60s（H21 + Karma CPU 经验值）。
- Karma CPU 渲染步（4.9）内部单视图 timeout 较长（最长 ~300s）。
- 多次跑互不影响：每次跑建独立 artifact 目录
  `$TEMP/houdini_mcp/e2e_demo_table/<YYYYMMDD_HHMMSS>/`。

## 输出

1. **stdout** 一个 Markdown 表格 + 一行 `[summary] N steps: ... PASS / ... FAIL / ... SKIP / ... WARN` 汇总。
2. **落盘** 同表格到
   `$TEMP/houdini_mcp/e2e_demo_table/<timestamp>/summary.md`。
3. **截图** `$TEMP/houdini_mcp/e2e_demo_table/<timestamp>/NetworkEditor.png` /
   `SceneViewer.<frame>.png` / `Parm.png`（SceneViewer 走 flipbook 路径，
   文件名带 4 位帧号后缀）。
4. **Karma CPU 视图** `$TEMP/houdini_mcp/e2e_demo_table/<timestamp>/table_demo_views/{top,front,side,perspective}.png`。

### 退出码

- `0` — 所有 step 都是 PASS / WARN / SKIP。
- `1` — 至少一个 step FAIL。

## 已知 SKIP 场景

- **Karma CPU 渲染（4.9）** — 渲染器未装 / 机器忙 / bbox 计算失败 → 整步
  SKIP。Demo 不视作失败。
- **Privileged audit（4.10）** — Houdini 端
  `HOUDINI_MCP_ALLOW_BYPASS=true` 环境变量未设，且 audit body 命中
  dangerous 正则时 → 服务端返回 `blocked: True` → demo 整步 SKIP。
  注意：本次 demo 的 audit body 是 benign create+destroy，仅命中
  mutation，不命中 dangerous，所以**通常不需要** bypass env var 也能通过。

## 已知偏差（vs. spec 草案）

- `capture_multiple_panes` 当前不把 `_renderer` 字段透到 per-result dict
  （只透 `pane_type / save_path / success / error`），所以 demo 用
  SceneViewer 落盘文件名是否含 4 位帧号后缀作为 flipbook 路径的 proxy。
  若你读到的是 `SceneViewer.png`（无帧号后缀），detail 会记
  `renderer_proxy=qt_grab_or_other` —— 这是 Qt grab 降级路径，不视作失败。
- `get_last_scene_diff()` 服务端实返字段是 `changed: bool`（不是 spec 草案
  里的 `scene_changes: list`），demo 以 `changed` 为准。

## 已知良好的输出示例

> [PLACEHOLDER — fill in after first live run]
> 实机跑过一次后，把下面这块换成真实 stdout（裁 30 行内即可）：

```
[summary] 26 steps: 22 PASS / 0 FAIL / 2 SKIP / 2 WARN
| # | Step | Status | Artifact | Detail |
|---|------|--------|----------|--------|
| 1 | 4.4b create_node /obj/table_demo | PASS | /obj/table_demo | create_node -> ['result'] |
...
| 25 | 4.10 execute_code_audit | PASS | - | changed=False before_keys=['nodes'] after_keys=['nodes'] |
| 26 | 4.8 capture_multiple_panes (overall) | PASS | ... | passed=3/3 |
**Verdict:** pass_with_warn: 4.9 render_quad_views_karma_cpu
[artifact_dir] C:\Users\...\Temp\houdini_mcp\e2e_demo_table\20260721_215000
```

## 脚本是幂等的

`e2e_demo_table.py` 每次跑都从空场景起步（demo 入口调 `new_scene`
`suppress_save_prompt=True` 清场 best-effort），然后在 `/obj/table_demo`
下重建。**重新跑是安全的**，不会污染用户的 .hip 文件；唯一前提：跑前若你
的 .hip 文件含未保存内容，**自己先另存一份**（demo 不替你存）。

## 协议参考

socket 帧 = `>I len + utf8 json`，请求体：

```json
{"type": "<cmd_type>", "params": {...}}
```

响应体：

```json
{"status": "success" | "error", "result": ..., "message": ...}
```

任何 `status != "success"` 都抛 `HoudiniCallError(response=<full dict>)`，
调用方可读 `e.response` 做精细诊断。