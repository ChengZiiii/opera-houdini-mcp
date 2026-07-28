"""_hda.py — opera-houdini-mcp HDA/OTL 管理（add-hda-management-tools）。

集中承载 10 个 HDA 管理工具的 ``hou`` 调用与硬约束：
- 定义枚举固定使用 ``hou.hda.loadedFiles()`` + ``hou.hda.definitionsInFile()``，
  按 ``(libraryFilePath, nameWithCategory())`` 去重。
- ``node_type`` 解析器只接受 ``hou.NodeType.nameWithCategory()`` 的完整
  类别名；短名称、未知、歧义一律返回 ``ambiguous_node_type``。
- ``hda_create`` 先 ``node.canCreateDigitalAsset()``，成功调
  ``node.createDigitalAsset(name=, hda_file_name=, description=)``，**不**传
  ``quiet`` 等 HOM 不存在的参数。
- ``update_hda`` 验证节点存在、拥有 definition、实例类型匹配后调
  ``definition.updateFromNode(node)``，**不**使用 ``definition.save()``。
- section 读取：``get_hda_sections`` 通过 ``size()`` / ``binaryContents()``
  严格 UTF-8 探测；``get_hda_section_content`` 强制显式
  ``encoding="utf8"|"base64"``，两种模式只调用一次 ``binaryContents()``
  并以原始 bytes 为唯一分页真相。
- section 写入：大小写敏感 allowlist ``Help`` / ``IconSVG``，UTF-8 入站
  字节上限 65536；其他全部 ``section_write_denied`` 且零写入。
- 不实现 override / ``allow_protected`` / ``authorization`` / 隐藏绕过参数。
- 所有响应过 ``apply_response_cap``（defense-in-depth）。

模块职责：
- hou 通过第一参数注入；顶层不 ``import hou``。
- 错误统一 ``{"status": "error", "error": {"code", "message", "details"}}``
  envelope，code 稳定字符串。
- path / node_type 校验在 hou 调用之前；任何 hou 异常降级为 error
  dict 而非抛出。

设计依据：
- D1（去重与完整名）：``loadedFiles + definitionsInFile`` 是 HOM 唯
  一公开的稳定枚举面；``nameWithCategory()`` 是唯一稳定类别名。
- D2（创建 / 更新用真 HOM）：``canCreateDigitalAsset + createDigitalAsset``
  与 ``updateFromNode`` 是 H21 / H22 跨版本稳定的写入路径。
- D3（双模式分页预算）：``binaryContents()`` 一次拿 raw bytes，UTF-8 严
  格解码决定 ``utf8`` capability；分页必须以 raw bytes 为真相，避免
  二次转换偏差。预算算法使用与 ``apply_response_cap`` 相同的
  ``json.dumps(..., default=str).encode("utf-8")`` serializer。
- D4（写入 allowlist）：``Help`` / ``IconSVG`` 是 Houdini 文档明确
  的 HDA 静态可写 section 真实名称；其他全部由 allowlist 默认拒
  绝，无黑名单副作用。
- D5（无 override）：任何 "绕过 allowlist" / "授权" / 隐藏参数路径
  均不存在；测试断言 bridge / server kwargs 集合无此类字段。

约束：
- hou 通过参数注入；不新增 pip 依赖。
- 4 空格缩进 / snake_case / 中文 docstring / 无 f-string / 无类型注解。
- 错误一律 ``status=error`` + ``error.code/message/details`` envelope。
"""
import base64
import copy
import json
import os

from . import _common as cmn


# ---------------------------------------------------------------------------
# Section 1: 常量与允许写入 allowlist
# ---------------------------------------------------------------------------
# 大小写敏感、精确匹配的固定 allowlist。``Help`` / ``IconSVG`` 是
# Houdini 官方 HDA section 真实名称；任何其他 section 名（含
# ``PythonModule``、事件 / cook / create / internal section、自定义
# section、前后空白名称与 ``help`` / ``iconsvg`` 大小写变体）一律
# ``section_write_denied``，且不触碰定义。
SECTION_WRITE_ALLOWLIST = frozenset({"Help", "IconSVG"})

# UTF-8 入站正文字节上限（design.md §"D3 写入 allowlist"）。
SECTION_WRITE_MAX_BYTES = 65536

# 默认响应 cap（与 ``apply_response_cap`` 默认一致）。
DEFAULT_RESPONSE_CAP = 16384

# 显式必填 ``encoding`` 的两个合法值。
_VALID_ENCODINGS = frozenset({"utf8", "base64"})

# ``limit`` 合法范围（design.md §"正文读取"）。
_LIMIT_MIN = 1
_LIMIT_MAX = 8192

# ``get_hda_section_content`` 返回 envelope 必含固定字段（除 ``content``
# 或 ``content_b64`` 外），用于构造 budget 预演 envelope。
_PAGINATION_FIELDS = (
    "status", "node_type", "section", "encoding",
    "offset", "limit", "next_offset", "total_bytes",
)


# ---------------------------------------------------------------------------
# Section 2: 错误 envelope helper
# ---------------------------------------------------------------------------
def _error(code, message, details=None):
    """构造统一错误 envelope；``details`` 可为 None。"""
    payload = {"status": "error", "error": {"code": code,
                                            "message": message}}
    if details is not None:
        payload["error"]["details"] = details
    return payload


def _success(data):
    """构造成功 envelope；保留调用方传入的全部字段。"""
    payload = {"status": "success"}
    for key, value in data.items():
        payload[key] = value
    return payload


# ---------------------------------------------------------------------------
# Section 3: 路径与 node_type 解析
# ---------------------------------------------------------------------------
def _normalize_existing_file_path(raw):
    """规范化 ``file_path``：要求非空字符串、文件存在、是常规文件。

    install / uninstall / reload 走此路径，文件必须已存在。
    """
    if not isinstance(raw, str) or not raw.strip():
        return None, _error("invalid_file_path",
                             "file_path must be a non-empty string",
                             {"field": "file_path"})
    try:
        normalized = os.path.abspath(raw)
    except Exception as error:
        return None, _error("invalid_file_path",
                             "failed to normalize file_path: %s" % error,
                             {"field": "file_path",
                              "exception": error.__class__.__name__})
    if not os.path.isfile(normalized):
        return None, _error("file_not_found",
                             "file_path %r is not an existing regular file"
                             % normalized,
                             {"field": "file_path",
                              "path": normalized})
    return normalized, None


def _normalize_create_file_path(raw):
    """规范化 ``save_path``：要求非空字符串、父目录存在。文件本身由
    ``createDigitalAsset`` 创建，**不**要求已存在。
    """
    if not isinstance(raw, str) or not raw.strip():
        return None, _error("invalid_save_path",
                             "save_path must be a non-empty string",
                             {"field": "save_path"})
    try:
        normalized = os.path.abspath(raw)
    except Exception as error:
        return None, _error("invalid_save_path",
                             "failed to normalize save_path: %s" % error,
                             {"field": "save_path",
                              "exception": error.__class__.__name__})
    parent_dir = os.path.dirname(normalized)
    if parent_dir and not os.path.isdir(parent_dir):
        return None, _error("save_path_invalid",
                             "save_path parent directory %r does not exist"
                             % parent_dir,
                             {"field": "save_path", "path": normalized,
                              "parent": parent_dir})
    return normalized, None


def _category_from_node_type(raw):
    """从 ``Sop/box::2.0`` 拆出 category（``Sop``）与 base（``box``）。"""
    if not isinstance(raw, str) or not raw.strip():
        return "", ""
    value = raw.strip()
    if "/" in value:
        category, _, remainder = value.partition("/")
    else:
        category, remainder = "", value
    base = remainder.split("::", 1)[0] if remainder else ""
    return category.strip(), base.strip()


def _resolve_node_type(hou, node_type):
    """把 ``node_type`` 解析为 ``hou.NodeType``；不接受短名称。

    解析顺序：
    1) ``hou.nodeTypeCategories()`` 中精确匹配 ``category/base``。
    2) 若 category 不存在或 base 在该 category 中未注册（H21 刚
       ``createDigitalAsset`` 后的 HDA 可能只在 ``loadedFiles`` 中
       而尚未在 ``nodeTypeCategories`` 注册），回退扫描
       ``loadedFiles`` + ``definitionsInFile``，按 ``nodeType().
       nameWithCategory()`` 精确匹配。

    Returns:
        tuple: ``(node_type_obj, error_dict)``；任一成功时
        ``error_dict is None``；失败时 ``node_type_obj is None``。
    """
    if not isinstance(node_type, str) or not node_type.strip():
        return None, _error("invalid_node_type",
                             "node_type must be a non-empty string",
                             {"field": "node_type"})
    raw = node_type.strip()
    category, base = _category_from_node_type(raw)
    if not category or not base:
        return None, _error("invalid_node_type",
                             "node_type must be a full nameWithCategory() "
                             "such as 'Sop/box'; got %r" % raw,
                             {"field": "node_type", "value": raw})
    target_full = "%s/%s" % (category, base)
    # Step 1: ``nodeTypeCategories()`` 精确匹配
    categories = hou.nodeTypeCategories()
    category_obj = categories.get(category)
    if category_obj is not None:
        try:
            node_types = category_obj.nodeTypes()
        except Exception:
            node_types = {}
        if target_full in node_types:
            nt = node_types[target_full]
            if nt is not None:
                return nt, None
    # Step 2: 回退扫描 ``loadedFiles`` + ``definitionsInFile``；H21
    # 刚 ``createDigitalAsset`` 后的 HDA 可能只在 ``loadedFiles`` 中
    # 而尚未在 ``nodeTypeCategories`` 注册。
    try:
        loaded = hou.hda.loadedFiles()
    except Exception:
        loaded = []
    matched_full_names = []
    matched_nt = None
    for file_path in loaded:
        try:
            definitions = hou.hda.definitionsInFile(file_path)
        except Exception:
            continue
        for defn in definitions:
            try:
                nt = defn.nodeType()
            except Exception:
                continue
            try:
                full_name = nt.nameWithCategory()
            except Exception:
                full_name = ""
            if full_name == target_full:
                return nt, None
            if full_name == raw:
                matched_nt = nt
                matched_full_names.append(full_name)
            else:
                # base 名字相同但 category 不同时记录为歧义候选
                c, b = _category_from_node_type(full_name)
                if c and b == base:
                    matched_nt = nt
                    matched_full_names.append(full_name)
    if matched_full_names:
        return None, _error(
            "ambiguous_node_type",
            "node_type %r is ambiguous; %d candidates: %s"
            % (raw, len(matched_full_names),
               sorted(set(matched_full_names))),
            {"field": "node_type", "value": raw,
             "candidates": sorted(set(matched_full_names))})
    if category_obj is None:
        return None, _error("unknown_node_type",
                             "unknown category %r in node_type %r"
                             % (category, raw),
                             {"field": "node_type", "value": raw,
                              "category": category})
    return None, _error("unknown_node_type",
                         "node_type %r not found in category %r"
                         % (raw, category),
                         {"field": "node_type", "value": raw,
                          "category": category, "base": base})


# ---------------------------------------------------------------------------
# Section 4: 枚举 loaded files 与 definitions
# ---------------------------------------------------------------------------
def _enumerate_definitions(hou):
    """遍历 ``loadedFiles`` + ``definitionsInFile``；返回 list of dict。

    每项 ``{"definition", "name", "node_type", "category", "version",
    "file_path", "library_file_path"}``。``nameWithCategory()`` 与
    ``libraryFilePath()`` 调用失败时降级为 ``""``，不抛异常。
    """
    out = []
    try:
        files = hou.hda.loadedFiles()
    except Exception:
        files = []
    seen = set()  # (library_file_path, node_type_name)
    for raw_path in files:
        if not isinstance(raw_path, str):
            continue
        try:
            normalized = os.path.abspath(raw_path)
        except Exception:
            normalized = raw_path
        try:
            definitions = hou.hda.definitionsInFile(normalized)
        except Exception:
            continue
        for definition in definitions:
            try:
                node_type_obj = definition.nodeType()
            except Exception:
                continue
            try:
                node_type_name = node_type_obj.nameWithCategory()
            except Exception:
                node_type_name = ""
            try:
                library_path = definition.libraryFilePath()
            except Exception:
                library_path = normalized
            key = (library_path, node_type_name)
            if key in seen:
                continue
            seen.add(key)
            out.append({
                "definition": definition,
                "node_type": node_type_name,
                "file_path": library_path,
            })
    return out


def _safe_int(value, default=0):
    try:
        return int(value)
    except Exception:
        return default


# ---------------------------------------------------------------------------
# Section 5: 公共 API — hda_list
# ---------------------------------------------------------------------------
def hda_list(hou, category=None):
    """枚举已加载 HDA，按 (library, nameWithCategory) 去重。

    Returns:
        dict: ``{"status": "success", "hdas": [...], "count": N}``，
        每项 ``name / node_type / category / version / file_path``。
        响应过 ``apply_response_cap``。``category`` 仅做透传过滤；
        解析逻辑仍依赖完整 ``nameWithCategory()``。
    """
    entries = _enumerate_definitions(hou)
    hdas = []
    for entry in entries:
        definition = entry["definition"]
        node_type_name = entry["node_type"]
        cat, _ = _category_from_node_type(node_type_name)
        if category is not None and category != cat:
            continue
        try:
            name = definition.nodeType().name()
        except Exception:
            name = ""
        try:
            version = _safe_int(definition.version())
        except Exception:
            version = 0
        hdas.append({
            "name": name,
            "node_type": node_type_name,
            "category": cat,
            "version": version,
            "file_path": entry["file_path"],
        })
    payload = _success({
        "hdas": hdas,
        "count": len(hdas),
    })
    return cmn.apply_response_cap(payload)


# ---------------------------------------------------------------------------
# Section 6: 公共 API — hda_get
# ---------------------------------------------------------------------------
def hda_get(hou, node_type):
    """读取 definition metadata；不读取 section 全文。"""
    nt, err = _resolve_node_type(hou, node_type)
    if err is not None:
        return err
    definition = nt.definition()
    if definition is None:
        return _error("definition_not_found",
                       "node_type %r has no definition" % node_type,
                       {"field": "node_type", "value": node_type})
    try:
        name = definition.nodeType().name()
    except Exception:
        name = ""
    try:
        version = _safe_int(definition.version())
    except Exception:
        version = 0
    try:
        description = definition.description()
    except Exception:
        description = ""
    try:
        min_inputs = _safe_int(definition.minNumInputs(), default=-1)
    except Exception:
        min_inputs = -1
    try:
        max_inputs = _safe_int(definition.maxNumInputs(), default=-1)
    except Exception:
        max_inputs = -1
    try:
        library_path = definition.libraryFilePath()
    except Exception:
        library_path = ""
    try:
        node_type_name = nt.nameWithCategory()
    except Exception:
        node_type_name = node_type
    cat, _ = _category_from_node_type(node_type_name)
    payload = _success({
        "name": name,
        "node_type": node_type_name,
        "category": cat,
        "version": version,
        "file_path": library_path,
        "description": description,
        "min_num_inputs": min_inputs,
        "max_num_inputs": max_inputs,
    })
    return cmn.apply_response_cap(payload)


# ---------------------------------------------------------------------------
# Section 7: 公共 API — hda_install / uninstall_hda / reload_hda
# ---------------------------------------------------------------------------
def hda_install(hou, file_path):
    """安装 HDA 库（``hou.hda.installFile``）。persistent external side effect。"""
    normalized, err = _normalize_existing_file_path(file_path)
    if err is not None:
        return err
    try:
        hou.hda.installFile(normalized)
    except Exception as error:
        return _error("hda_install_failed",
                       "hou.hda.installFile(%r) failed: %s"
                       % (normalized, error),
                       {"field": "file_path",
                        "path": normalized,
                        "exception": error.__class__.__name__})
    return _success({
        "file_path": normalized,
        "action": "install",
    })


def uninstall_hda(hou, file_path):
    """卸载 HDA 库（``hou.hda.uninstallFile``）。persistent external side effect。"""
    normalized, err = _normalize_existing_file_path(file_path)
    if err is not None:
        return err
    try:
        hou.hda.uninstallFile(normalized)
    except Exception as error:
        return _error("hda_uninstall_failed",
                       "hou.hda.uninstallFile(%r) failed: %s"
                       % (normalized, error),
                       {"field": "file_path",
                        "path": normalized,
                        "exception": error.__class__.__name__})
    return _success({
        "file_path": normalized,
        "action": "uninstall",
    })


def reload_hda(hou, file_path):
    """重载 HDA 库（``hou.hda.reloadFile``）。persistent external side effect。"""
    normalized, err = _normalize_existing_file_path(file_path)
    if err is not None:
        return err
    try:
        hou.hda.reloadFile(normalized)
    except Exception as error:
        return _error("hda_reload_failed",
                       "hou.hda.reloadFile(%r) failed: %s"
                       % (normalized, error),
                       {"field": "file_path",
                        "path": normalized,
                        "exception": error.__class__.__name__})
    return _success({
        "file_path": normalized,
        "action": "reload",
    })


# ---------------------------------------------------------------------------
# Section 8: 公共 API — hda_create / update_hda
# ---------------------------------------------------------------------------
def _resolve_node(hou, node_path):
    """把 ``node_path`` 解析为 hou.Node；失败返 error dict。"""
    if not isinstance(node_path, str) or not node_path.strip():
        return None, _error("invalid_node_path",
                             "node_path must be a non-empty string",
                             {"field": "node_path"})
    node = hou.node(node_path)
    if node is None:
        return None, _error("node_not_found",
                             "node not found at path %r" % node_path,
                             {"field": "node_path", "value": node_path})
    return node, None


def hda_create(hou, node_path, name, save_path, label=None):
    """从节点创建 HDA（design.md §"hda_create"）。

    先 ``node.canCreateDigitalAsset()``，再 ``node.createDigitalAsset(
    name=, hda_file_name=, description=)``。**不**传 ``quiet``。
    """
    if not isinstance(name, str) or not name.strip():
        return _error("invalid_hda_name",
                       "name must be a non-empty string",
                       {"field": "name"})
    normalized_save, err = _normalize_create_file_path(save_path)
    if err is not None:
        return err
    node, err = _resolve_node(hou, node_path)
    if err is not None:
        return err
    try:
        can_create = node.canCreateDigitalAsset()
    except Exception as error:
        return _error("can_create_check_failed",
                       "node.canCreateDigitalAsset() failed: %s" % error,
                       {"field": "node_path",
                        "exception": error.__class__.__name__})
    if not can_create:
        return _error("not_convertible_to_hda",
                       "node at %r cannot be converted to a digital asset"
                       % node_path,
                       {"field": "node_path", "value": node_path})
    try:
        new_def = node.createDigitalAsset(
            name=name,
            hda_file_name=normalized_save,
            description=label if isinstance(label, str) else "",
        )
    except TypeError:
        # 兼容 H21 不支持 description 关键字；回退到仅 name + hda_file_name
        try:
            new_def = node.createDigitalAsset(
                name=name, hda_file_name=normalized_save)
        except Exception as error:
            return _error("hda_create_failed",
                           "createDigitalAsset() failed: %s" % error,
                           {"field": "node_path",
                            "exception": error.__class__.__name__})
    except Exception as error:
        return _error("hda_create_failed",
                       "createDigitalAsset() failed: %s" % error,
                       {"field": "node_path",
                        "exception": error.__class__.__name__})
    if new_def is None:
        return _error("hda_create_failed",
                       "createDigitalAsset() returned None",
                       {"field": "node_path"})
    # In H21, ``createDigitalAsset`` writes the file but may not auto-load
    # it into the current session; ``nodeType().nameWithCategory()`` on the
    # returned definition can therefore return "". To get a stable full
    # category name, fall back to scanning ``loadedFiles`` for the new
    # definition (the file is the one we just wrote).
    new_node_type = ""
    try:
        candidate = new_def.nodeType().nameWithCategory()
        if candidate:
            new_node_type = candidate
    except Exception:
        pass
    if not new_node_type:
        try:
            for file_path in hou.hda.loadedFiles():
                try:
                    normalized_lookup = os.path.abspath(file_path)
                except Exception:
                    normalized_lookup = file_path
                if normalized_lookup != normalized_save:
                    continue
                for other in hou.hda.definitionsInFile(normalized_lookup):
                    if other is new_def:
                        try:
                            new_node_type = (
                                other.nodeType().nameWithCategory())
                        except Exception:
                            new_node_type = ""
                        break
                if new_node_type:
                    break
        except Exception:
            pass
    try:
        new_file_path = new_def.libraryFilePath()
    except Exception:
        new_file_path = normalized_save
    return _success({
        "name": name,
        "node_type": new_node_type,
        "file_path": new_file_path,
    })


def update_hda(hou, node_path):
    """从实例更新定义（design.md §"update_hda"）。

    验证节点存在、拥有 definition、实例类型匹配；调
    ``definition.updateFromNode(node)``。**不**使用 ``definition.save()``。
    """
    node, err = _resolve_node(hou, node_path)
    if err is not None:
        return err
    try:
        node_type_obj = node.type()
    except Exception as error:
        return _error("update_hda_failed",
                       "node.type() failed: %s" % error,
                       {"field": "node_path",
                        "exception": error.__class__.__name__})
    if node_type_obj is None:
        return _error("update_hda_failed",
                       "node %r has no node type" % node_path,
                       {"field": "node_path"})
    try:
        definition = node_type_obj.definition()
    except Exception as error:
        return _error("update_hda_failed",
                       "node.type().definition() failed: %s" % error,
                       {"field": "node_path",
                        "exception": error.__class__.__name__})
    if definition is None:
        return _error("not_a_digital_asset",
                       "node %r is not a digital asset instance"
                       % node_path,
                       {"field": "node_path", "value": node_path})
    try:
        definition.updateFromNode(node)
    except Exception as error:
        return _error("update_from_node_failed",
                       "definition.updateFromNode() failed: %s" % error,
                       {"field": "node_path",
                        "exception": error.__class__.__name__})
    try:
        nt_name = node_type_obj.nameWithCategory()
    except Exception:
        nt_name = ""
    return _success({
        "node_path": node.path(),
        "node_type": nt_name,
    })


# ---------------------------------------------------------------------------
# Section 9: section 工具 — 通用 helper
# ---------------------------------------------------------------------------
def _get_definition(hou, node_type):
    """解析 ``node_type`` 并返回 ``(definition, node_type_obj, error)``。"""
    nt, err = _resolve_node_type(hou, node_type)
    if err is not None:
        return None, None, err
    try:
        definition = nt.definition()
    except Exception as error:
        return None, None, _error(
            "definition_lookup_failed",
            "nodeType.definition() failed: %s" % error,
            {"field": "node_type",
             "exception": error.__class__.__name__})
    if definition is None:
        return None, None, _error(
            "definition_not_found",
            "node_type %r has no definition" % node_type,
            {"field": "node_type", "value": node_type})
    return definition, nt, None


def _list_sections(hou, definition):
    """枚举 ``definition.sections()``；每项是 ``HDASection``。"""
    try:
        sections = definition.sections()
    except Exception as error:
        raise RuntimeError("definition.sections() failed: %s" % error)
    out = []
    for name, section in sections.items():
        if not isinstance(name, str):
            continue
        out.append((name, section))
    return out


def _probe_utf8(raw_bytes):
    """严格 UTF-8 解码；成功返 ``(True, decoded_text)``，失败 ``(False, "")``。

    不使用 errors='replace' / 'ignore'；解码失败直接返 ``(False, "")``。
    """
    try:
        decoded = raw_bytes.decode("utf-8")
    except (UnicodeDecodeError, ValueError):
        return False, ""
    return True, decoded


def _build_empty_envelope(node_type, section, encoding, offset, limit,
                          total_bytes):
    """构造空正文 envelope（用于预算预演）。"""
    envelope = {
        "status": "success",
        "node_type": node_type,
        "section": section,
        "encoding": encoding,
        "offset": offset,
        "limit": limit,
        "next_offset": offset,
        "total_bytes": total_bytes,
    }
    if encoding == "utf8":
        envelope["content"] = ""
    else:
        envelope["content_b64"] = ""
    return envelope


def _serialize_envelope(envelope):
    """使用与 ``apply_response_cap._serialized_size`` 相同的 serializer。"""
    return json.dumps(envelope, default=str).encode("utf-8")


# ---------------------------------------------------------------------------
# Section 10: 公共 API — get_hda_sections
# ---------------------------------------------------------------------------
def get_hda_sections(hou, node_type):
    """枚举 definition 的 sections，返回 ``size / protected / binary / utf8``。"""
    definition, _, err = _get_definition(hou, node_type)
    if err is not None:
        return err
    try:
        section_pairs = _list_sections(hou, definition)
    except Exception as error:
        return _error("sections_lookup_failed",
                       str(error),
                       {"exception": error.__class__.__name__})
    sections_out = []
    for name, section in section_pairs:
        try:
            size = _safe_int(section.size(), default=0)
        except Exception:
            size = 0
        is_protected = name not in SECTION_WRITE_ALLOWLIST
        try:
            raw_bytes = section.binaryContents()
        except Exception:
            raw_bytes = b""
        utf8_ok, _ = _probe_utf8(raw_bytes)
        sections_out.append({
            "name": name,
            "size": size,
            "protected": is_protected,
            "binary": True,
            "utf8": utf8_ok,
        })
    payload = _success({
        "sections": sections_out,
        "count": len(sections_out),
    })
    return cmn.apply_response_cap(payload)


# ---------------------------------------------------------------------------
# Section 11: 公共 API — get_hda_section_content
# ---------------------------------------------------------------------------
def get_hda_section_content(hou, node_type, section, encoding,
                            offset=0, limit=8192):
    """分页读取 section 正文（design.md §"正文读取 + 预算"）。

    ``encoding`` 显式必填 ``utf8`` / ``base64``；两种模式均以
    ``binaryContents()`` 一次拿到的 raw bytes 为唯一分页真相。
    预算使用与 ``apply_response_cap`` 相同的 JSON serializer。
    """
    # 1) 参数校验
    if not isinstance(section, str) or not section.strip():
        return _error("invalid_section",
                       "section must be a non-empty string",
                       {"field": "section"})
    if encoding not in _VALID_ENCODINGS:
        return _error("invalid_encoding",
                       "encoding must be one of %r; got %r"
                       % (sorted(_VALID_ENCODINGS), encoding),
                       {"field": "encoding", "value": encoding})
    if (not isinstance(limit, int) or isinstance(limit, bool)
            or limit < _LIMIT_MIN or limit > _LIMIT_MAX):
        return _error("invalid_limit",
                       "limit must be an integer in [%d, %d]; got %r"
                       % (_LIMIT_MIN, _LIMIT_MAX, limit),
                       {"field": "limit", "value": limit})
    if (not isinstance(offset, int) or isinstance(offset, bool)
            or offset < 0):
        return _error("invalid_offset",
                       "offset must be a non-negative integer; got %r"
                       % offset,
                       {"field": "offset", "value": offset})
    # 2) 解析 definition + section
    definition, _, err = _get_definition(hou, node_type)
    if err is not None:
        return err
    try:
        section_obj = definition.sections().get(section)
    except Exception:
        section_obj = None
    if section_obj is None:
        return _error("section_not_found",
                       "section %r not found on node_type %r"
                       % (section, node_type),
                       {"field": "section", "value": section,
                        "node_type": node_type})
    # 3) 一次拿 raw bytes
    try:
        raw_bytes = section_obj.binaryContents()
    except Exception as error:
        return _error("section_read_failed",
                       "binaryContents() failed: %s" % error,
                       {"field": "section",
                        "exception": error.__class__.__name__})
    total_bytes = len(raw_bytes)
    if offset > total_bytes:
        return _error("invalid_offset",
                       "offset %d exceeds total_bytes %d"
                       % (offset, total_bytes),
                       {"field": "offset", "value": offset,
                        "total_bytes": total_bytes})
    # 4) UTF-8 模式：先严格解码整段
    if encoding == "utf8":
        utf8_ok, decoded = _probe_utf8(raw_bytes)
        if not utf8_ok:
            return _error("section_not_utf8",
                           "section %r is not valid UTF-8" % section,
                           {"field": "section", "value": section,
                            "total_bytes": total_bytes})
        # offset 必须在 code point 边界
        if offset > 0:
            prefix = raw_bytes[:offset]
            try:
                prefix.decode("utf-8")
            except UnicodeDecodeError as error:
                return _error("invalid_utf8_offset",
                               "offset %d is not on a UTF-8 code "
                               "point boundary: %s" % (offset, error),
                               {"field": "offset", "value": offset,
                                "total_bytes": total_bytes})
        # 已解码全文 → 用 raw bytes 切片后再解（offset 已在 code point
        # 边界上，bytes[offset:] 仍合法 UTF-8）；逐 code point 扩展
        if offset == total_bytes:
            # EOF 空页允许
            envelope = _build_empty_envelope(
                node_type, section, "utf8", offset, limit, total_bytes)
            envelope["content"] = ""
            envelope["next_offset"] = total_bytes
            return cmn.apply_response_cap(envelope)
        remaining = raw_bytes[offset:].decode("utf-8")
        # 逐 code point 扩展候选；UTF-8 字节数不能超 limit
        if not remaining:
            # EOF 空页允许
            envelope = _build_empty_envelope(
                node_type, section, "utf8", offset, limit, total_bytes)
            envelope["content"] = ""
            envelope["next_offset"] = total_bytes
            return cmn.apply_response_cap(envelope)
        # 取首个 code point 字节数
        first_cp_bytes = len(remaining[0].encode("utf-8"))
        if first_cp_bytes > limit:
            return _error("utf8_boundary_too_small",
                           "limit %d cannot hold the first UTF-8 code "
                           "point (%d bytes)" % (limit, first_cp_bytes),
                           {"field": "limit", "value": limit,
                            "first_code_point_bytes": first_cp_bytes})
        # 预算预演起点
        empty = _build_empty_envelope(
            node_type, section, "utf8", offset, limit, total_bytes)
        empty_bytes = _serialize_envelope(empty)
        budget = DEFAULT_RESPONSE_CAP - len(empty_bytes)
        if budget < 0:
            # 连空 envelope 都超 cap（极端小 cap）；返 ``response_budget_too_small``
            return _error("response_budget_too_small",
                           "even empty envelope exceeds response cap",
                           {"field": "limit", "value": limit,
                            "cap": DEFAULT_RESPONSE_CAP})
        # 逐 code point 扩展
        accepted_bytes = 0
        accepted_codepoints = 0
        for ch in remaining:
            cp_bytes = len(ch.encode("utf-8"))
            if accepted_bytes + cp_bytes > limit:
                break
            accepted_bytes += cp_bytes
            accepted_codepoints += 1
        # 用 ``accepted_bytes`` 真实消耗重算 envelope（与 cap 决定无关）
        if accepted_codepoints == 0:
            return _error("utf8_boundary_too_small",
                           "limit %d cannot hold any code point"
                           % limit,
                           {"field": "limit", "value": limit})
        page_bytes = raw_bytes[offset:offset + accepted_bytes]
        page_text = page_bytes.decode("utf-8")
        envelope = _build_empty_envelope(
            node_type, section, "utf8", offset, limit, total_bytes)
        envelope["content"] = page_text
        envelope["next_offset"] = offset + accepted_bytes
        # 预算预检：完整 envelope 序列化字节 <= cap
        serialized = _serialize_envelope(envelope)
        if len(serialized) > DEFAULT_RESPONSE_CAP:
            return _error("response_budget_too_small",
                           "page envelope exceeds response cap "
                           "(%d > %d)" % (len(serialized),
                                          DEFAULT_RESPONSE_CAP),
                           {"field": "limit", "value": limit,
                            "cap": DEFAULT_RESPONSE_CAP,
                            "envelope_bytes": len(serialized)})
        return cmn.apply_response_cap(envelope)
    # 5) base64 模式：任意 raw byte offset
    if offset == total_bytes:
        # EOF 空页
        envelope = _build_empty_envelope(
            node_type, section, "base64", offset, limit, total_bytes)
        envelope["content_b64"] = ""
        envelope["next_offset"] = total_bytes
        return cmn.apply_response_cap(envelope)
    remaining = raw_bytes[offset:]
    # 预算预演
    empty = _build_empty_envelope(
        node_type, section, "base64", offset, limit, total_bytes)
    empty_bytes = _serialize_envelope(empty)
    budget = DEFAULT_RESPONSE_CAP - len(empty_bytes)
    if budget < 0:
        return _error("response_budget_too_small",
                       "even empty envelope exceeds response cap",
                       {"field": "limit", "value": limit,
                        "cap": DEFAULT_RESPONSE_CAP})
    # 逐 raw byte 扩展（base64 膨胀比约 4/3；用同 serializer 校验）
    accepted = 0
    for index in range(len(remaining)):
        if index + 1 > limit:
            break
        candidate = remaining[:index + 1]
        envelope = _build_empty_envelope(
            node_type, section, "base64", offset, limit, total_bytes)
        envelope["content_b64"] = base64.b64encode(candidate).decode("ascii")
        envelope["next_offset"] = offset + len(candidate)
        serialized = _serialize_envelope(envelope)
        if len(serialized) > DEFAULT_RESPONSE_CAP:
            break
        accepted = len(candidate)
    if accepted == 0:
        return _error("response_budget_too_small",
                       "page envelope exceeds response cap",
                       {"field": "limit", "value": limit,
                        "cap": DEFAULT_RESPONSE_CAP})
    page_bytes = raw_bytes[offset:offset + accepted]
    envelope = _build_empty_envelope(
        node_type, section, "base64", offset, limit, total_bytes)
    envelope["content_b64"] = base64.b64encode(page_bytes).decode("ascii")
    envelope["next_offset"] = offset + accepted
    return cmn.apply_response_cap(envelope)


# ---------------------------------------------------------------------------
# Section 12: 公共 API — set_hda_section_content
# ---------------------------------------------------------------------------
def set_hda_section_content(hou, node_type, section, content):
    """按 allowlist 写入 section（design.md §"D4 写入 allowlist"）。

    ``section`` 大小写敏感 allowlist ``Help`` / ``IconSVG``；其他名称
    全部 ``section_write_denied`` 且零写入。``content`` UTF-8 字节
    上限 65536。不执行、导入、评估 content。
    """
    if not isinstance(section, str) or not section.strip():
        return _error("invalid_section",
                       "section must be a non-empty string",
                       {"field": "section"})
    section_name = section.strip()
    if section_name != section:
        # 含前后空白：默认拒绝
        return _error("section_write_denied",
                       "section name %r has leading/trailing whitespace "
                       "and is not in the write allowlist" % section,
                       {"field": "section", "value": section})
    if section_name not in SECTION_WRITE_ALLOWLIST:
        return _error("section_write_denied",
                       "section %r is not in the write allowlist; only "
                       "Help and IconSVG may be written" % section,
                       {"field": "section", "value": section,
                        "allowlist": sorted(SECTION_WRITE_ALLOWLIST)})
    if not isinstance(content, str):
        return _error("invalid_content",
                       "content must be a UTF-8 string",
                       {"field": "content",
                        "value_type": type(content).__name__})
    encoded = content.encode("utf-8")
    if len(encoded) > SECTION_WRITE_MAX_BYTES:
        return _error("request_too_large",
                       "content UTF-8 byte length %d exceeds limit %d"
                       % (len(encoded), SECTION_WRITE_MAX_BYTES),
                       {"field": "content",
                        "size_bytes": len(encoded),
                        "limit_bytes": SECTION_WRITE_MAX_BYTES})
    definition, _, err = _get_definition(hou, node_type)
    if err is not None:
        return err
    # 不允许 override / allow_protected / authorization；一律 add / set。
    try:
        existing = definition.sections().get(section_name)
    except Exception:
        existing = None
    try:
        if existing is None:
            definition.addSection(section_name, content)
        else:
            existing.setContents(content)
    except Exception as error:
        return _error("section_write_failed",
                       "failed to write section %r: %s"
                       % (section_name, error),
                       {"field": "section", "value": section_name,
                        "exception": error.__class__.__name__})
    return _success({
        "node_type": node_type,
        "section": section_name,
        "size_bytes": len(encoded),
        "action": "add" if existing is None else "update",
    })


# ---------------------------------------------------------------------------
# Section 13: 公共 API 列表（供 server 注册 / 测试分类断言）
# ---------------------------------------------------------------------------
HDA_COMMANDS = (
    "hda_list",
    "hda_get",
    "hda_install",
    "hda_create",
    "uninstall_hda",
    "reload_hda",
    "update_hda",
    "get_hda_sections",
    "get_hda_section_content",
    "set_hda_section_content",
)

HDA_READ_ONLY_COMMANDS = frozenset({
    "hda_list", "hda_get",
    "get_hda_sections", "get_hda_section_content",
})

HDA_MUTATING_COMMANDS = frozenset({
    "hda_create", "update_hda", "set_hda_section_content",
})

HDA_NO_UNDO_COMMANDS = frozenset({
    "hda_install", "uninstall_hda", "reload_hda",
})


# ---------------------------------------------------------------------------
# Section 14: introspection helper for tests
# ---------------------------------------------------------------------------
def _envelope_for_test(envelope, cap=DEFAULT_RESPONSE_CAP):
    """测试 helper：序列化 envelope 测预算（与 ``apply_response_cap`` 同 source）。"""
    return len(_serialize_envelope(envelope)) <= cap
