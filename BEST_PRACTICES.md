# BEST_PRACTICES 知识库

> advisory recipes：本文件每条记录都是「先查线索」，不替代
> verify_hou_api / get_houdini_help，也不替代目标 Houdini 版本的 live
> verification。未知 API 仍应先做 runtime 探测，再参考此处 recipe。

## 严格 schema

> 每个 `### BP-NNN` 块恰好含 9 个必填字段：category、severity、
> affected_versions、verified_versions、source、advisory、problem、
> symptom、fix。heading 提供 id，故每条共 id + 9 个字段。advisory 必须
> 为 true；source / verified_versions 不得为空。duplicate id、重复 /
> 未知 / 缺失 field、非法 severity/bool、recipe 外正文都使整个文件
> parse 失败，绝不返回 partial entries。

## 追加审查规则

> 新增 recipe 前：1) 标出真实 source（官方 URL 或仓库结构 / 测试 /
> incident evidence）；2) verified_versions 只写实际完成 live/package
> verification 的精确范围，不得用 `>=21` / `all` 或把文档存在性当
> live verification；3) H22 只有 live smoke PASS 后才可写入
> verified_versions；4) recipe 是 advisory，不写成普遍 HOM 定律。

### BP-001

- category: api-versioning
- severity: medium
- affected_versions: H21.0
- verified_versions: H21.0 fork live smoke
- source: https://www.sidefx.com/docs/houdini/hom/hou/applicationVersion; tests/h21_live_smoke.py connection section
- advisory: true
- problem: fork 早期误用 hou.version()（返回主版本构造字符串）做精确版本判断
- symptom: 版本分支判断在不同小版本间不稳定，行为与预期不符
- fix: H21.0 改用 hou.applicationVersionString() 或 hou.applicationVersion() 做精确版本判定

### BP-002

- category: scene-state
- severity: medium
- affected_versions: H21.0
- verified_versions: H21.0 fork live smoke
- source: https://www.sidefx.com/docs/houdini/hom/hou/hipFile; tests/h21_live_smoke.py connection section
- advisory: true
- problem: 用 hou.hipFile.name() 是否为空来猜测场景是否未保存
- symptom: 空路径或临时名导致未保存误判，覆盖已有场景判断
- fix: 用 hou.hipFile.isNewFile() 判定未保存场景，不以 name() 判空替代

### BP-003

- category: networking
- severity: high
- affected_versions: H21.0 incident environment
- verified_versions: H21.0 incident environment
- source: 对应 incident checkpoint; scripts/python 内 SOP↔OBJ wiring regression
- advisory: true
- problem: SOP 连到不兼容 OBJ input 的 wiring 曾长时间阻塞
- symptom: 连线后 cook 失败或行为异常，难以定位真实 input 不兼容
- fix: 连线前先用 network/category 校验两端兼容，不用 display/render flag 冒充连线

### BP-004

- category: viewport-capture
- severity: medium
- affected_versions: H21.0
- verified_versions: H21.0 fork live smoke
- source: https://www.sidefx.com/docs/houdini/hom/hou/GeometryViewport; test_render_b64.py H21 section
- advisory: true
- problem: 误把 saveImage 能力挂在 hou.SceneViewer.saveImage 上
- symptom: 调 SceneViewer.saveImage 在缺该能力的 H21.0 环境报 AttributeError
- fix: saveImage capability 属于当前 hou.GeometryViewport; 缺失时 feature-detect 并复用 _pane_capture.py flipbook

### BP-005

- category: viewport-capture
- severity: high
- affected_versions: H21.0 affected workstation (OGL)
- verified_versions: H21.0 affected workstation
- source: _pane_capture.py SceneViewer contract; live flipbook E2E evidence
- advisory: true
- problem: SceneViewer 回退用 Qt widget.grab() 抓屏
- symptom: 特定 H21.0/OGL 环境 widget.grab() 触发 fatal 崩溃
- fix: SceneViewer 禁止回退 Qt grab，必须走 flipbook capture 路径

### BP-006

- category: undo
- severity: high
- affected_versions: H21.0 affected workstation
- verified_versions: H21.0 affected workstation
- source: server.py NO_UNDO_COMMANDS undo classification; live smoke evidence
- advisory: true
- problem: 用 hou.undos.group 包裹 flipbook capture
- symptom: H21.0 实测触发 SWIG error，capture 失败
- fix: capture/render 保持 no-undo，归入 NO_UNDO_COMMANDS，不进 undo group

### BP-007

- category: materials
- severity: medium
- affected_versions: H21.0
- verified_versions: H21.0 fork tests/live
- source: material runtime parm schema; fork RGB 子参数 whitelist 测试
- advisory: true
- problem: 假设 principledshader::2.0 的 parm 名跨版本固定
- symptom: 用推测 parm 名设置材质失败或写错通道
- fix: principledshader::2.0 parm 名必须 runtime schema 验证; fork whitelist 仅覆盖已验证 RGB 子参数

### BP-008

- category: environment
- severity: medium
- affected_versions: Python 3.12 embedded + pinned mcp versions
- verified_versions: Python 3.12 embedded + pinned/compared mcp versions
- source: external/houdinimcp-env env lock/config; MCP package changelog/test evidence
- advisory: true
- problem: 升级 MCP/FastMCP 版本未重跑 constructor 与 tool registration
- symptom: FastMCP constructor 组合在新版本下注册行为变化，工具丢失或异常
- fix: 升级 mcp 前按已锁版本重跑 constructor/tool registration tests

### BP-009

- category: rendering
- severity: medium
- affected_versions: H21.0 fork policy
- verified_versions: H21.0 fork policy tests
- source: _render_policy.py interrupt/token contract; policy tests
- advisory: true
- problem: 把 karma_cpu/xpu consent gate 当成 HOM 本身要求
- symptom: 误以为 karma 必然可渲染，忽略 consent 流程导致 _interrupt
- fix: karma_cpu/xpu 在本 fork 是 consent-gated policy，需 consent_token; 非 HOM 本身要求

### BP-010

- category: rendering
- severity: medium
- affected_versions: H21.0 affected workstation (renderer)
- verified_versions: H21.0 affected workstation
- source: _render_policy.py redirect contract; live flipbook bridge E2E
- advisory: true
- problem: opengl 渲染请求在受影响环境直接走 GPU 路径
- symptom: 受影响 H21.0 renderer 环境下 opengl 渲染失败或不稳定
- fix: 本 fork 在受影响环境将 opengl 请求 redirect 到既有 SceneViewer flipbook capture
