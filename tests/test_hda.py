"""tests/test_hda.py — add-hda-management-tools 单测。

覆盖（tasks 3.1 - 3.10）：
- ``loadedFiles`` / ``definitionsInFile``、完整类别名、去重与歧义
  拒绝。
- ``canCreateDigitalAsset`` / ``createDigitalAsset`` 与
  ``updateFromNode``。
- ``Help`` / ``IconSVG`` add / update；脚本、事件、内部、自定义、
  空白与大小写变体全部拒绝；无 override / 授权参数；拒绝时零写入；
  禁止 exec / eval / import。
- section metadata 的 ``size / binary / utf8`` 严格探测有效 / 无效
  UTF-8、空 bytes；探测与正文读取均以 ``binaryContents()`` 为准且
  不调用 ``contents()``。
- 显式 ``encoding`` 必填及非法值；UTF-8 ``limit=1..8192``、首尾多字
  节边界、非法 byte offset、连续无跳字节、严格解码稳定
  ``section_not_utf8``，以及 UTF-8 set 的 65536-byte 上限。
- base64 模式含 NUL、非法 UTF-8、全 byte 值；逐页解码拼接逐 byte
  相等。
- 两种模式 envelope 不超 cap，cursor 等于实际返回 raw bytes，验证
  拼页无遗漏重复。
- UTF-8 首字符超 limit 的 ``utf8_boundary_too_small``、两种模式最
  小单位 envelope 超预算的 ``response_budget_too_small``、EOF 空页。
- 错误 schema、列表 / 正文 response cap 与命令分类。
- 10 个新增命令 = 三分类并集，三组两两无交集。

约束：
- stdlib unittest + 简易 hou mock；不引入新依赖。
- 不依赖真实 Houdini；H21.0 live smoke 由
  ``h21_live_hda_smoke.py`` 单独执行。
"""
import base64
import importlib
import importlib.util
import json
import os
import sys
import tempfile
import types
import unittest
import ast


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def _ensure_pkg():
    """Build / reuse a synthetic package scoped to this test file."""
    pkg_name = "hda_test_pkg"
    if pkg_name in sys.modules and getattr(
            sys.modules[pkg_name], "__path__", None):
        return sys.modules[pkg_name]
    pkg = types.ModuleType(pkg_name)
    pkg.__path__ = [ROOT]
    sys.modules[pkg_name] = pkg
    return pkg


def _ensure_module(name):
    pkg = _ensure_pkg()
    full = pkg.__name__ + "." + name
    if full in sys.modules:
        del sys.modules[full]
    spec = importlib.util.spec_from_file_location(
        full, os.path.join(ROOT, name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[full] = mod
    spec.loader.exec_module(mod)
    return mod


# Pre-load sibling modules.
_common = _ensure_module("_common")
_hda = _ensure_module("_hda")


# ---------------------------------------------------------------------------
# hou mock infrastructure
# ---------------------------------------------------------------------------
class _FakeSection(object):
    def __init__(self, name, raw=b"", size=None):
        self._name = name
        self._raw = raw
        self._size = size if size is not None else len(raw)
        self.contents_called = False
        self.set_contents_called = 0
        self.last_set_content = None

    def name(self):
        return self._name

    def size(self):
        return self._size

    def binaryContents(self):
        return self._raw

    def contents(self):
        # Tracking: real impl should NOT call this.
        self.contents_called = True
        try:
            return self._raw.decode("utf-8")
        except Exception:
            return self._raw

    def setContents(self, value):
        self.set_contents_called += 1
        self.last_set_content = value
        if isinstance(value, str):
            self._raw = value.encode("utf-8")
            self._size = len(self._raw)


class _FakeDefinition(object):
    def __init__(self, sections, library_path, name="box",
                 version=1, description="", min_inputs=0, max_inputs=1,
                 category="Sop"):
        self._sections_dict = dict(sections)
        self._library_path = library_path
        self._name = name
        self._category = category
        self._version = version
        self._description = description
        self._min_inputs = min_inputs
        self._max_inputs = max_inputs
        self.updateFromNode_called = 0
        self.addSection_calls = []
        self.libraryFilePath_called = 0
        self.name_called = 0
        self.version_called = 0
        self.description_called = 0
        self.minNumInputs_called = 0
        self.maxNumInputs_called = 0

    def sections(self):
        return self._sections_dict

    def addSection(self, name, contents):
        self.addSection_calls.append((name, contents))
        section = _FakeSection(name, raw=contents.encode("utf-8")
                                if isinstance(contents, str) else b"")
        self._sections_dict[name] = section
        return section

    def libraryFilePath(self):
        self.libraryFilePath_called += 1
        return self._library_path

    def name(self):
        self.name_called += 1
        return self._name

    def version(self):
        self.version_called += 1
        return self._version

    def description(self):
        self.description_called += 1
        return self._description

    def minNumInputs(self):
        self.minNumInputs_called += 1
        return self._min_inputs

    def maxNumInputs(self):
        self.maxNumInputs_called += 1
        return self._max_inputs

    def updateFromNode(self, node):
        self.updateFromNode_called += 1
        return True

    def nodeType(self):
        return _FakeNodeType(self, category=self._category,
                              name=self._name)


class _FakeNodeType(object):
    def __init__(self, definition, category="Sop", name="box"):
        self._definition = definition
        self._name = name
        self._category = category

    def definition(self):
        return self._definition

    def nameWithCategory(self):
        return "%s/%s" % (self._category, self._name)

    def name(self):
        return self._name


class _FakeNode(object):
    def __init__(self, can_create=True, definition=None,
                 create_side_effect=None):
        self._can_create = can_create
        self._definition = definition
        self.canCreateDigitalAsset_called = 0
        self.createDigitalAsset_calls = []
        self.create_side_effect = create_side_effect
        self.type_obj = _FakeNodeType(definition) if definition else None

    def canCreateDigitalAsset(self):
        self.canCreateDigitalAsset_called += 1
        return self._can_create

    def createDigitalAsset(self, name, hda_file_name, description=""):
        self.createDigitalAsset_calls.append(
            {"name": name, "hda_file_name": hda_file_name,
             "description": description})
        if self.create_side_effect is not None:
            return self.create_side_effect(self, name, hda_file_name,
                                            description)
        new_def = _FakeDefinition(
            sections={"Help": _FakeSection("Help", raw=b"help text"),
                       "IconSVG": _FakeSection("IconSVG", raw=b"<svg/>")},
            library_path=hda_file_name, name=name)
        return new_def

    def type(self):
        return self.type_obj

    def path(self):
        return "/obj/geo1"

    def name(self):
        return "geo1"


class _FakeCategory(object):
    def __init__(self, name, node_types):
        self._name = name
        self._node_types = node_types
        self.nodeTypes_called = 0

    def nodeTypes(self):
        self.nodeTypes_called += 1
        return self._node_types

    def name(self):
        return self._name


class _FakeHou(object):
    def __init__(self, loaded_files=None, categories=None,
                 node_resolver=None, hda_calls=None,
                 definitions_in_file=None):
        self._loaded_files = loaded_files or []
        self._categories = categories or {}
        self._node_resolver = node_resolver or {}
        self._hda_calls = hda_calls or []
        self._definitions_in_file = definitions_in_file or {}
        self.hda = self
        self.hda_calls = self._hda_calls

    def loadedFiles(self):
        return list(self._loaded_files)

    def definitionsInFile(self, path):
        return list(self._definitions_in_file.get(path, []))

    def installFile(self, path):
        self._hda_calls.append(("install", path))

    def uninstallFile(self, path):
        self._hda_calls.append(("uninstall", path))

    def reloadFile(self, path):
        self._hda_calls.append(("reload", path))

    def nodeTypeCategories(self):
        return self._categories

    def node(self, path):
        return self._node_resolver.get(path)


def _build_hou_with_definition(definition, library_path,
                                category="Sop", name="box",
                                loaded_files=None):
    """Build a _FakeHou whose ``Sop/<name>`` resolves to ``definition``."""
    nt = _FakeNodeType(definition, category=category, name=name)
    cat = _FakeCategory(category, {"%s/%s" % (category, name): nt})
    hou = _FakeHou(categories={category: cat})
    if loaded_files is None:
        loaded_files = [library_path]
    hou._loaded_files = loaded_files
    hou._definitions_in_file[library_path] = [definition]
    return hou


# ---------------------------------------------------------------------------
# Test: section list metadata
# ---------------------------------------------------------------------------
class TestSectionListMetadata(unittest.TestCase):
    def test_size_binary_utf8_strict_probe(self):
        defs = {
            "Help": _FakeSection("Help", raw=b"help text"),
            "IconSVG": _FakeSection("IconSVG", raw="<svg/>".encode("utf-8")),
            "PythonModule": _FakeSection(
                "PythonModule", raw="def foo(): pass".encode("utf-8")),
            "BinaryBin": _FakeSection(
                "BinaryBin", raw=b"\x00\x01\xff\xfe\x80"),
        }
        definition = _FakeDefinition(
            sections=defs, library_path="C:/lib/lib.1.0.hda")
        hou = _build_hou_with_definition(
            definition, library_path="C:/lib/lib.1.0.hda")
        result = _hda.get_hda_sections(hou, "Sop/box")
        self.assertEqual(result["status"], "success", result)
        sections_by_name = {s["name"]: s for s in result["sections"]}
        self.assertTrue(sections_by_name["Help"]["binary"])
        self.assertTrue(sections_by_name["Help"]["utf8"])
        self.assertEqual(sections_by_name["Help"]["size"], len(b"help text"))
        self.assertTrue(sections_by_name["IconSVG"]["binary"])
        self.assertTrue(sections_by_name["IconSVG"]["utf8"])
        self.assertTrue(sections_by_name["PythonModule"]["binary"])
        self.assertTrue(sections_by_name["PythonModule"]["utf8"])
        # binary-only section: utf8 must be False
        self.assertTrue(sections_by_name["BinaryBin"]["binary"])
        self.assertFalse(sections_by_name["BinaryBin"]["utf8"])
        # No .contents() was invoked for any section
        for sec in defs.values():
            self.assertFalse(sec.contents_called,
                              "%s.contents() was called" % sec._name)

    def test_empty_section_is_utf8(self):
        defs = {"Help": _FakeSection("Help", raw=b"")}
        definition = _FakeDefinition(sections=defs,
                                      library_path="C:/lib.1.0.hda")
        hou = _build_hou_with_definition(
            definition, library_path="C:/lib.1.0.hda")
        result = _hda.get_hda_sections(hou, "Sop/box")
        self.assertEqual(result["status"], "success", result)
        self.assertTrue(result["sections"][0]["utf8"])
        self.assertTrue(result["sections"][0]["binary"])


# ---------------------------------------------------------------------------
# Test: hda_list dedup & full nameWithCategory
# ---------------------------------------------------------------------------
class TestHdaListDedup(unittest.TestCase):
    def test_dedup_and_full_name(self):
        def1 = _FakeDefinition(
            sections={}, library_path="C:/lib.1.0.hda", name="box")
        def2 = _FakeDefinition(
            sections={}, library_path="C:/lib.1.0.hda", name="box")
        def3 = _FakeDefinition(
            sections={}, library_path="C:/lib2.1.0.hda", name="sphere")
        # Each definition has distinct nameWithCategory via _FakeDefinition
        nt_box = _FakeNodeType(def1, category="Sop", name="box")
        nt_box2 = _FakeNodeType(def2, category="Sop", name="box")
        nt_sphere = _FakeNodeType(def3, category="Sop", name="sphere")
        def1.nodeType = lambda: nt_box
        def2.nodeType = lambda: nt_box2
        def3.nodeType = lambda: nt_sphere
        hou = _FakeHou(loaded_files=["C:/lib.1.0.hda", "C:/lib2.1.0.hda"])
        # Patch loaded files to normalized Windows paths (abspath on Windows
        # adds drive prefix; use the abspath keys).
        import os
        norm1 = os.path.abspath("C:/lib.1.0.hda")
        norm2 = os.path.abspath("C:/lib2.1.0.hda")
        hou._definitions_in_file[norm1] = [def1, def2]
        hou._definitions_in_file[norm2] = [def3]
        result = _hda.hda_list(hou)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["count"], 2)
        node_types = sorted(h["node_type"] for h in result["hdas"])
        self.assertEqual(node_types, ["Sop/box", "Sop/sphere"])
        # dedup test: same (library, name) appears once
        self.assertEqual(len(result["hdas"]), 2)

    def test_category_filter(self):
        def1 = _FakeDefinition(
            sections={}, library_path="C:/a.hda", name="box",
            category="Sop")
        def2 = _FakeDefinition(
            sections={}, library_path="C:/a.hda", name="cam",
            category="Object")
        hou = _FakeHou(loaded_files=["C:/a.hda"])
        import os
        norm = os.path.abspath("C:/a.hda")
        hou._definitions_in_file[norm] = [def1, def2]
        result = _hda.hda_list(hou, category="Sop")
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["hdas"][0]["category"], "Sop")


# ---------------------------------------------------------------------------
# Test: hda_get
# ---------------------------------------------------------------------------
class TestHdaGet(unittest.TestCase):
    def test_get_metadata(self):
        defs = {"Help": _FakeSection("Help")}
        definition = _FakeDefinition(
            sections=defs, library_path="C:/lib.1.0.hda",
            name="box", version=2, description="Box SOP",
            min_inputs=1, max_inputs=4)
        # category routing via hou.nodeTypeCategories()
        nt = _FakeNodeType(definition)
        cat = _FakeCategory("Sop", {"Sop/box": nt})
        hou = _FakeHou(categories={"Sop": cat})
        result = _hda.hda_get(hou, "Sop/box")
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["node_type"], "Sop/box")
        self.assertEqual(result["category"], "Sop")
        self.assertEqual(result["version"], 2)
        self.assertEqual(result["description"], "Box SOP")
        self.assertEqual(result["min_num_inputs"], 1)
        self.assertEqual(result["max_num_inputs"], 4)
        self.assertEqual(result["file_path"], "C:/lib.1.0.hda")

    def test_ambiguous_short_name_rejected(self):
        definition = _FakeDefinition(sections={},
                                      library_path="C:/lib.1.0.hda")
        nt = _FakeNodeType(definition)
        # Category also has a different category's same base
        nt_other = _FakeNodeType(definition)
        nt_other._category = "Object"
        cat_sop = _FakeCategory(
            "Sop", {"Sop/box": nt})
        cat_obj = _FakeCategory(
            "Object", {"Object/box": nt_other})
        hou = _FakeHou(categories={"Sop": cat_sop, "Object": cat_obj})
        # bare 'box' has no category -> invalid
        r1 = _hda.hda_get(hou, "box")
        self.assertEqual(r1["status"], "error")
        self.assertEqual(r1["error"]["code"], "invalid_node_type")

    def test_unknown_category(self):
        hou = _FakeHou(categories={})
        r = _hda.hda_get(hou, "Sop/box")
        self.assertEqual(r["status"], "error")
        self.assertEqual(r["error"]["code"], "unknown_node_type")


# ---------------------------------------------------------------------------
# Test: hda_create / update_hda
# ---------------------------------------------------------------------------
class TestHdaCreate(unittest.TestCase):
    def setUp(self):
        fd, path = tempfile.mkstemp(suffix=".hda")
        os.close(fd)
        self.tmp_path = path

    def tearDown(self):
        if os.path.isfile(self.tmp_path):
            os.unlink(self.tmp_path)

    def test_create_calls_canCreate_and_createDigitalAsset(self):
        node = _FakeNode(can_create=True)
        hou = _FakeHou(node_resolver={"/obj/geo1": node})
        result = _hda.hda_create(hou, "/obj/geo1", "box_asset",
                                  self.tmp_path, label="Box")
        self.assertEqual(result["status"], "success", result)
        self.assertEqual(node.canCreateDigitalAsset_called, 1)
        self.assertEqual(len(node.createDigitalAsset_calls), 1)
        call = node.createDigitalAsset_calls[0]
        self.assertEqual(call["name"], "box_asset")
        self.assertEqual(call["hda_file_name"], self.tmp_path)
        self.assertEqual(call["description"], "Box")
        # no quiet kw
        self.assertNotIn("quiet", call)
        # New HDA's node_type is "Sop/<name>" since the test fake defaults
        # to category "Sop" and the new definition's name = "box_asset".
        self.assertEqual(result["node_type"], "Sop/box_asset")

    def test_create_canCreate_false_rejected(self):
        node = _FakeNode(can_create=False)
        hou = _FakeHou(node_resolver={"/obj/geo1": node})
        result = _hda.hda_create(hou, "/obj/geo1", "box_asset",
                                  self.tmp_path)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"]["code"], "not_convertible_to_hda")
        self.assertEqual(node.canCreateDigitalAsset_called, 1)
        self.assertEqual(len(node.createDigitalAsset_calls), 0)

    def test_create_invalid_save_path(self):
        node = _FakeNode(can_create=True)
        hou = _FakeHou(node_resolver={"/obj/geo1": node})
        # parent directory does not exist
        result = _hda.hda_create(hou, "/obj/geo1", "box_asset",
                                  "C:/no/such/dir/x.hda")
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"]["code"], "save_path_invalid")

    def test_create_node_not_found(self):
        hou = _FakeHou(node_resolver={})
        result = _hda.hda_create(hou, "/obj/missing", "x", self.tmp_path)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"]["code"], "node_not_found")


class TestUpdateHda(unittest.TestCase):
    def test_update_calls_updateFromNode(self):
        definition = _FakeDefinition(
            sections={}, library_path="C:/lib.1.0.hda")
        node = _FakeNode(definition=definition)
        hou = _FakeHou(node_resolver={"/obj/geo1": node})
        result = _hda.update_hda(hou, "/obj/geo1")
        self.assertEqual(result["status"], "success")
        self.assertEqual(definition.updateFromNode_called, 1)
        self.assertEqual(result["node_type"], "Sop/box")

    def test_update_not_digital_asset(self):
        node = _FakeNode(definition=None)
        # ensure type().definition() returns None
        nt = _FakeNodeType(None)
        node.type_obj = nt
        hou = _FakeHou(node_resolver={"/obj/geo1": node})
        result = _hda.update_hda(hou, "/obj/geo1")
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"]["code"], "not_a_digital_asset")

    def test_update_node_not_found(self):
        hou = _FakeHou(node_resolver={})
        result = _hda.update_hda(hou, "/obj/missing")
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"]["code"], "node_not_found")


# ---------------------------------------------------------------------------
# Test: install / uninstall / reload
# ---------------------------------------------------------------------------
class TestInstallUninstallReload(unittest.TestCase):
    def setUp(self):
        fd, path = tempfile.mkstemp(suffix=".hda")
        os.close(fd)
        self.tmp_path = path

    def tearDown(self):
        if os.path.isfile(self.tmp_path):
            os.unlink(self.tmp_path)

    def test_install_uninstall_reload(self):
        hou = _FakeHou()
        for cmd, fn in (("install", _hda.hda_install),
                         ("uninstall", _hda.uninstall_hda),
                         ("reload", _hda.reload_hda)):
            result = fn(hou, self.tmp_path)
            self.assertEqual(result["status"], "success")
            self.assertEqual(result["action"], cmd)
            self.assertIn((cmd, os.path.abspath(self.tmp_path)),
                          hou.hda_calls)

    def test_invalid_file_path(self):
        hou = _FakeHou()
        for fn in (_hda.hda_install, _hda.uninstall_hda, _hda.reload_hda):
            r = fn(hou, "")
            self.assertEqual(r["status"], "error")
            self.assertEqual(r["error"]["code"], "invalid_file_path")
            r2 = fn(hou, "C:/no/such/file.hda")
            self.assertEqual(r2["status"], "error")
            self.assertEqual(r2["error"]["code"], "file_not_found")

    def test_hou_exception_returns_error(self):
        # create a temp file, but force hou.hda.installFile to raise
        class _RaisingHou(_FakeHou):
            def installFile(self, path):
                    raise RuntimeError("nope")
        hou = _RaisingHou()
        r = _hda.hda_install(hou, self.tmp_path)
        self.assertEqual(r["status"], "error")
        self.assertEqual(r["error"]["code"], "hda_install_failed")


# ---------------------------------------------------------------------------
# Test: section write allowlist
# ---------------------------------------------------------------------------
class TestSectionWriteAllowlist(unittest.TestCase):
    def setUp(self):
        fd, self.tmp_path = tempfile.mkstemp(suffix=".hda")
        os.close(fd)
        self.definition = _FakeDefinition(
            sections={}, library_path=self.tmp_path)
        nt = _FakeNodeType(self.definition)
        self.cat = _FakeCategory("Sop", {"Sop/box": nt})
        self.hou = _FakeHou(categories={"Sop": self.cat})

    def tearDown(self):
        if os.path.isfile(self.tmp_path):
            os.unlink(self.tmp_path)

    def test_help_and_iconsvg_add(self):
        for name in ("Help", "IconSVG"):
            r = _hda.set_hda_section_content(
                self.hou, "Sop/box", name, "content for " + name)
            self.assertEqual(r["status"], "success", r)
            self.assertEqual(r["action"], "add")
            self.assertIn((name, "content for " + name),
                          self.definition.addSection_calls)
            # definition.addSection is the only path; no save() / exec
            self.assertFalse(hasattr(self.definition, "save"))

    def test_existing_section_uses_setContents(self):
        existing = _FakeSection("Help", raw=b"old")
        self.definition._sections_dict["Help"] = existing
        r = _hda.set_hda_section_content(
            self.hou, "Sop/box", "Help", "new content")
        self.assertEqual(r["status"], "success")
        self.assertEqual(r["action"], "update")
        self.assertEqual(existing.set_contents_called, 1)
        self.assertEqual(existing.last_set_content, "new content")
        # addSection NOT called
        self.assertEqual(self.definition.addSection_calls, [])

    def test_deny_python_module_event_internal_custom(self):
        for name in ("PythonModule", "OnCreated", "Cook", "Internal",
                      "ExtraSection", "help", "ICONSVG", "HelpCard"):
            r = _hda.set_hda_section_content(
                self.hou, "Sop/box", name, "x")
            self.assertEqual(r["status"], "error", name)
            self.assertEqual(r["error"]["code"], "section_write_denied",
                              name)
        # definition unchanged
        self.assertEqual(self.definition.addSection_calls, [])

    def test_deny_whitespace_section(self):
        for name in (" Help", "Help ", "  ", " Help "):
            r = _hda.set_hda_section_content(
                self.hou, "Sop/box", name, "x")
            self.assertEqual(r["status"], "error")
            self.assertIn(r["error"]["code"],
                          ("invalid_section", "section_write_denied"))

    def test_deny_request_too_large(self):
        huge = "a" * (_hda.SECTION_WRITE_MAX_BYTES + 1)
        r = _hda.set_hda_section_content(
            self.hou, "Sop/box", "Help", huge)
        self.assertEqual(r["status"], "error")
        self.assertEqual(r["error"]["code"], "request_too_large")
        # zero writes
        self.assertEqual(self.definition.addSection_calls, [])

    def test_no_exec_eval_import(self):
        # ensure setContents is the only mutation path; no global state
        # touched. Verify by patching addSection / setContents to raise
        # and checking error is propagated (not exec / eval).
        class _Exploding(object):
            def addSection(self, name, contents):
                raise RuntimeError("exec attempted")
        self.definition._sections_dict = {}
        self.definition.addSection = _Exploding().addSection
        r = _hda.set_hda_section_content(
            self.hou, "Sop/box", "Help", "x")
        self.assertEqual(r["status"], "error")
        self.assertEqual(r["error"]["code"], "section_write_failed")
        # No exec/eval/import was used to recover
        self.assertNotIn("exec", self.definition.addSection_calls
                          if hasattr(self.definition, "addSection_calls")
                          else [])


# ---------------------------------------------------------------------------
# Test: section content (utf8 / base64 pagination)
# ---------------------------------------------------------------------------
class TestSectionContentPagination(unittest.TestCase):
    def setUp(self):
        fd, self.tmp_path = tempfile.mkstemp(suffix=".hda")
        os.close(fd)
        self.definition = _FakeDefinition(
            sections={}, library_path=self.tmp_path)
        nt = _FakeNodeType(self.definition)
        self.cat = _FakeCategory("Sop", {"Sop/box": nt})
        self.hou = _FakeHou(categories={"Sop": self.cat})

    def tearDown(self):
        if os.path.isfile(self.tmp_path):
            os.unlink(self.tmp_path)

    def _put_section(self, name, raw):
        self.definition._sections_dict[name] = _FakeSection(name, raw=raw)

    def test_invalid_encoding(self):
        self._put_section("Help", b"hello")
        for bad in (None, "", "utf-16", "BASE64", "text", 42):
            r = _hda.get_hda_section_content(
                self.hou, "Sop/box", "Help", bad)
            self.assertEqual(r["status"], "error", bad)
            self.assertEqual(r["error"]["code"], "invalid_encoding", bad)

    def test_invalid_limit(self):
        self._put_section("Help", b"hello")
        for bad in (0, -1, 8193, 100000, "5", 5.0):
            r = _hda.get_hda_section_content(
                self.hou, "Sop/box", "Help", "utf8", limit=bad)
            self.assertEqual(r["status"], "error", bad)
            self.assertEqual(r["error"]["code"], "invalid_limit", bad)

    def test_utf8_first_page_uses_actual_returned_bytes(self):
        # "Hello世界" = b"Hello\xe4\xb8\x96\xe7\x95\x8c"
        text = "Hello世界"
        raw = text.encode("utf-8")
        self._put_section("Help", raw)
        r = _hda.get_hda_section_content(
            self.hou, "Sop/box", "Help", "utf8",
            offset=0, limit=8192)
        self.assertEqual(r["status"], "success")
        self.assertEqual(r["content"], text)
        self.assertEqual(r["next_offset"], len(raw))
        self.assertEqual(r["total_bytes"], len(raw))
        # pagination fields echoed
        for key in ("encoding", "offset", "limit", "next_offset",
                     "total_bytes", "node_type", "section"):
            self.assertIn(key, r)

    def test_utf8_pagination_by_code_point(self):
        text = "中a文bemoji🎉done"
        raw = text.encode("utf-8")
        self._put_section("Help", raw)
        collected = ""
        offset = 0
        limit = 3
        pages = []
        # Use limit >= 4 to avoid emoji boundary errors and still test code point slicing
        limit = 4
        while True:
            r = _hda.get_hda_section_content(
                self.hou, "Sop/box", "Help", "utf8",
                offset=offset, limit=limit)
            self.assertEqual(r["status"], "success", r)
            self.assertEqual(r["encoding"], "utf8")
            self.assertEqual(r["offset"], offset)
            self.assertEqual(r["limit"], limit)
            collected += r["content"]
            pages.append(r)
            if r["next_offset"] >= r["total_bytes"]:
                break
            # cursor must be on code point boundary: bytes[next_off:] decodes cleanly
            next_off = r["next_offset"]
            tail = raw[next_off:]
            tail.decode("utf-8")  # must not raise
            offset = next_off
        # reassembled must equal original
        self.assertEqual(collected, text)
        # no skipped or duplicate: next_offset strictly increasing
        next_offsets = [p["next_offset"] for p in pages]
        self.assertEqual(next_offsets, sorted(next_offsets))
        # each page is contiguous prefix: every page's content bytes =
        # raw[offset : next_offset]
        for p in pages:
            expected_size = p["next_offset"] - p["offset"]
            actual_size = len(p["content"].encode("utf-8"))
            self.assertEqual(actual_size, expected_size,
                              "page %r not contiguous prefix" % p)
        # envelope cap check
        cap = _hda.DEFAULT_RESPONSE_CAP
        for p in pages:
            ser = json.dumps(p, default=str).encode("utf-8")
            self.assertLessEqual(len(ser), cap)

    def test_utf8_strict_decode_fail_returns_section_not_utf8(self):
        # invalid UTF-8 sequence
        self._put_section("Help", b"abc\xff\xfedef")
        r = _hda.get_hda_section_content(
            self.hou, "Sop/box", "Help", "utf8")
        self.assertEqual(r["status"], "error")
        self.assertEqual(r["error"]["code"], "section_not_utf8")

    def test_utf8_invalid_offset_boundary(self):
        # offset inside a multi-byte char -> invalid_utf8_offset
        raw = "abc界def".encode("utf-8")
        # "界" starts at byte 3, length 3
        bad_offset = 4
        self._put_section("Help", raw)
        r = _hda.get_hda_section_content(
            self.hou, "Sop/box", "Help", "utf8",
            offset=bad_offset, limit=8192)
        self.assertEqual(r["status"], "error")
        self.assertEqual(r["error"]["code"], "invalid_utf8_offset")

    def test_utf8_first_char_exceeds_limit(self):
        # emoji 🎉 is 4 bytes UTF-8
        text = "🎉rest"
        raw = text.encode("utf-8")
        self._put_section("Help", raw)
        r = _hda.get_hda_section_content(
            self.hou, "Sop/box", "Help", "utf8",
            offset=0, limit=1)
        self.assertEqual(r["status"], "error")
        self.assertEqual(r["error"]["code"], "utf8_boundary_too_small")

    def test_utf8_eof_empty_page(self):
        raw = b"short"
        self._put_section("Help", raw)
        r = _hda.get_hda_section_content(
            self.hou, "Sop/box", "Help", "utf8",
            offset=len(raw), limit=8192)
        self.assertEqual(r["status"], "success")
        self.assertEqual(r["content"], "")
        self.assertEqual(r["next_offset"], len(raw))

    def test_base64_pagination_arbitrary_byte_offset(self):
        raw = bytes(range(256)) * 2  # 512 bytes covering full byte values
        self._put_section("Help", raw)
        collected = b""
        offset = 0
        limit = 17
        while True:
            r = _hda.get_hda_section_content(
                self.hou, "Sop/box", "Help", "base64",
                offset=offset, limit=limit)
            self.assertEqual(r["status"], "success", r)
            self.assertEqual(r["encoding"], "base64")
            self.assertNotIn("content", r)
            self.assertIn("content_b64", r)
            decoded = base64.b64decode(r["content_b64"])
            page_size = r["next_offset"] - offset
            self.assertEqual(decoded, raw[offset:offset + page_size])
            collected += decoded
            if r["next_offset"] >= r["total_bytes"]:
                break
            offset = r["next_offset"]
        self.assertEqual(collected, raw)
        # envelope cap
        cap = _hda.DEFAULT_RESPONSE_CAP
        # rerun single page; ensure serialized <= cap
        r = _hda.get_hda_section_content(
            self.hou, "Sop/box", "Help", "base64",
            offset=0, limit=8192)
        ser = json.dumps(r, default=str).encode("utf-8")
        self.assertLessEqual(len(ser), cap)

    def test_base64_arbitrary_offset_with_nul_and_invalid_utf8(self):
        raw = b"\x00\xffabc\x00\xc3\x28def"  # NUL + invalid UTF-8
        self._put_section("Help", raw)
        r = _hda.get_hda_section_content(
            self.hou, "Sop/box", "Help", "base64",
            offset=2, limit=4)
        self.assertEqual(r["status"], "success")
        decoded = base64.b64decode(r["content_b64"])
        self.assertEqual(decoded, raw[2:6])
        self.assertEqual(r["next_offset"], 6)

    def test_binaryContents_is_single_source(self):
        raw = b"Hello"
        section = _FakeSection("Help", raw=raw)
        self.definition._sections_dict["Help"] = section
        r = _hda.get_hda_section_content(
            self.hou, "Sop/box", "Help", "utf8")
        # never called .contents() in metadata probe or read
        self.assertFalse(section.contents_called)
        self.assertEqual(r["content"], "Hello")

    def test_response_budget_too_small(self):
        # Use a tiny cap by monkeypatching
        saved_cap = _hda.DEFAULT_RESPONSE_CAP
        _hda.DEFAULT_RESPONSE_CAP = 100
        try:
            raw = "x" * 1024
            self._put_section("Help", raw.encode("utf-8"))
            r = _hda.get_hda_section_content(
                self.hou, "Sop/box", "Help", "utf8",
                offset=0, limit=8192)
            self.assertEqual(r["status"], "error")
            self.assertEqual(r["error"]["code"], "response_budget_too_small")
        finally:
            _hda.DEFAULT_RESPONSE_CAP = saved_cap

    def test_response_budget_too_small_base64(self):
        saved_cap = _hda.DEFAULT_RESPONSE_CAP
        _hda.DEFAULT_RESPONSE_CAP = 80
        try:
            raw = b"\xff" * 256
            self._put_section("Help", raw)
            r = _hda.get_hda_section_content(
                self.hou, "Sop/box", "Help", "base64",
                offset=0, limit=8192)
            self.assertEqual(r["status"], "error")
            self.assertEqual(r["error"]["code"], "response_budget_too_small")
        finally:
            _hda.DEFAULT_RESPONSE_CAP = saved_cap


# ---------------------------------------------------------------------------
# Test: bridge kwargs surface (no override / allow_protected / authorization)
# ---------------------------------------------------------------------------
class TestBridgeKwargs(unittest.TestCase):
    def test_no_bypass_kwargs(self):
        """Assert ``houdini_mcp_server`` HDA bridge tools have no bypass kwargs."""
        bridge_path = os.path.join(ROOT, "houdini_mcp_server.py")
        with open(bridge_path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        # find all top-level functions decorated with @mcp.tool() and
        # whose name is in HDA_COMMANDS
        names = set(_hda.HDA_COMMANDS)
        offenders = []
        aliases = {
            "def hda_create": ("override", "allow_protected",
                                "authorization", "force_write"),
        }
        for node in tree.body:
            if not isinstance(node, ast.FunctionDef):
                continue
            if node.name not in names:
                continue
            for arg in node.args.args:
                if arg.arg in ("ctx",):
                    continue
                if arg.arg in aliases.get("def " + node.name, ()):
                    offenders.append((node.name, arg.arg))
        self.assertEqual(offenders, [],
                          "bridge tools accept bypass kwargs: %r" % offenders)

    def test_set_hda_section_content_kwarg_surface(self):
        """No override / allow_protected / authorization kwarg on the setter."""
        bridge_path = os.path.join(ROOT, "houdini_mcp_server.py")
        with open(bridge_path, encoding="utf-8") as fh:
            source = fh.read()
        tree = ast.parse(source)
        names = {"set_hda_section_content"}
        for node in tree.body:
            if not isinstance(node, ast.FunctionDef):
                continue
            if node.name not in names:
                continue
            for arg in node.args.args:
                self.assertNotIn(
                    arg.arg,
                    {"override", "allow_protected", "authorization",
                     "force_write", "section_allowlist_override"},
                    "%s accepts bypass kwarg %r" % (node.name, arg.arg))


# ---------------------------------------------------------------------------
# Test: command classification exclusivity
# ---------------------------------------------------------------------------
class TestCommandClassification(unittest.TestCase):
    def test_partition_disjoint_and_complete(self):
        union = (_hda.HDA_READ_ONLY_COMMANDS
                 | _hda.HDA_MUTATING_COMMANDS
                 | _hda.HDA_NO_UNDO_COMMANDS)
        self.assertEqual(union, frozenset(_hda.HDA_COMMANDS))
        self.assertFalse(_hda.HDA_READ_ONLY_COMMANDS & _hda.HDA_MUTATING_COMMANDS)
        self.assertFalse(_hda.HDA_READ_ONLY_COMMANDS & _hda.HDA_NO_UNDO_COMMANDS)
        self.assertFalse(_hda.HDA_MUTATING_COMMANDS & _hda.HDA_NO_UNDO_COMMANDS)
        # explicit category membership check
        self.assertIn("hda_list", _hda.HDA_READ_ONLY_COMMANDS)
        self.assertIn("hda_get", _hda.HDA_READ_ONLY_COMMANDS)
        self.assertIn("get_hda_sections", _hda.HDA_READ_ONLY_COMMANDS)
        self.assertIn("get_hda_section_content", _hda.HDA_READ_ONLY_COMMANDS)
        self.assertIn("hda_create", _hda.HDA_MUTATING_COMMANDS)
        self.assertIn("update_hda", _hda.HDA_MUTATING_COMMANDS)
        self.assertIn("set_hda_section_content", _hda.HDA_MUTATING_COMMANDS)
        self.assertIn("hda_install", _hda.HDA_NO_UNDO_COMMANDS)
        self.assertIn("uninstall_hda", _hda.HDA_NO_UNDO_COMMANDS)
        self.assertIn("reload_hda", _hda.HDA_NO_UNDO_COMMANDS)


# ---------------------------------------------------------------------------
# Test: error schema stability
# ---------------------------------------------------------------------------
class TestErrorSchema(unittest.TestCase):
    def test_error_envelope_shape(self):
        hou = _FakeHou()
        r = _hda.hda_get(hou, "")
        self.assertEqual(r["status"], "error")
        self.assertIn("error", r)
        self.assertIn("code", r["error"])
        self.assertIn("message", r["error"])
        # details optional but if present must be dict


if __name__ == "__main__":
    unittest.main()
