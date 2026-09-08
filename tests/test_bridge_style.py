"""Style regression tests for PR 7 bridge tools in houdini_mcp_server.py.

PR 7 brief mandates for the 3 newly added @mcp.tool() functions:
- No type annotations in the signature (parameters or return).
- Docstring must be Chinese (CJK characters present), not English.

Amendment (fix-mcp-dead-tools-p0 task 1.4): annotations are now allowed
where the server-side handler requires a real JSON type. create_material's
``parameters`` MUST be annotated as a dict type (server-side _materials
calls ``parameters.items()`` directly; a JSON string crashes with
"'str' object has no attribute 'items'"). Other PR 7 params stay
un-annotated; return annotations stay forbidden.

These checks use AST parsing only — they do NOT import houdini_mcp_server.py
because that module has heavy runtime dependencies (mcp, requests, dotenv,
langchain). We parse the source file as text and inspect the AST node ranges
between the PR 7 section markers.

Run with:
    python -m unittest tests.test_bridge_style -v
"""
import ast
import os
import re
import unittest


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SERVER_PY = os.path.join(ROOT, "houdini_mcp_server.py")


# Sentinel markers in the source identifying the PR 7 section.
PR7_SECTION_HEADER = "# PR 7 Materials Tools"


def _parse_source():
    with open(SERVER_PY, "r", encoding="utf-8") as f:
        return f.read()


def _find_pr7_function_nodes():
    """Return ast.FunctionDef nodes that are PR 7 @mcp.tool() functions.

    Strategy: parse the whole file; pick function nodes whose source line is
    strictly greater than the PR 7 section header line and that have a
    preceding `@mcp.tool()` decorator. Stop scanning at the next non-PR-7
    section comment line.
    """
    src = _parse_source()
    tree = ast.parse(src)
    lines = src.splitlines()

    # Locate PR 7 section header
    header_line = None
    for i, line in enumerate(lines, start=1):
        if PR7_SECTION_HEADER in line:
            header_line = i
            break
    if header_line is None:
        raise AssertionError(
            "PR 7 section marker not found in houdini_mcp_server.py")

    # Collect function defs within PR 7 block (up to EOF or next major
    # section comment).
    fns = []
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        if node.lineno <= header_line:
            continue
        # Stop if we hit a non-PR-7 top-level section comment as decorator
        # (defensive — none expected after PR 7 in current source).
        if node.decorator_list:
            for dec in node.decorator_list:
                if isinstance(dec, ast.Call):
                    func = dec.func
                    if (isinstance(func, ast.Attribute)
                            and func.attr == "tool"):
                        fns.append(node)
                        break
    return fns


def _has_cjk(s):
    """Return True if s contains any CJK Unified Ideograph."""
    if not s:
        return False
    return any("\u4e00" <= ch <= "\u9fff" for ch in s)


def _signature_annotation_kinds(fn):
    """Collect kinds of annotations present on a function's signature.

    Returns a dict with boolean flags:
        - arg_annotations: any positional/keyword arg has annotation
        - return_annotation: function has a return annotation
    """
    args = fn.args
    has_arg = False
    for arg in (args.posonlyargs + args.args + args.kwonlyargs):
        if arg.annotation is not None:
            has_arg = True
            break
    if args.vararg and args.vararg.annotation is not None:
        has_arg = True
    if args.kwarg and args.kwarg.annotation is not None:
        has_arg = True
    return {
        "arg_annotations": has_arg,
        "return_annotation": fn.returns is not None,
    }


class PR7BridgeStyleTests(unittest.TestCase):
    """PR 7 brief: PR 7 new @mcp.tool() must have no type annotations and
    Chinese docstrings. Older PR 3-6 tools are explicitly out of scope for
    this fix and will be revisited in the final whole-branch review.
    """

    def setUp(self):
        self.fns = _find_pr7_function_nodes()
        # Sanity: there must be exactly 3 PR 7 tools.
        self.assertEqual(
            len(self.fns), 3,
            "Expected 3 PR 7 @mcp.tool() functions, found {0}: {1}".format(
                len(self.fns), [f.name for f in self.fns]))

    def test_create_material_no_type_annotations(self):
        # fix-mcp-dead-tools-p0 1.4：parameters 改 dict 注解；其余参数与
        # 返回值保持无注解。
        fn = next(f for f in self.fns if f.name == "create_material")
        by_name = {a.arg: a for a in fn.args.args}
        # parameters 必须是 dict 型注解（dict 或 Dict[...]）
        ann = by_name["parameters"].annotation
        self.assertIsNotNone(
            ann, "create_material.parameters must be annotated (dict type)")
        is_dict = (
            (isinstance(ann, ast.Name) and ann.id == "dict")
            or (isinstance(ann, ast.Subscript)
                and isinstance(ann.value, ast.Name)
                and ann.value.id in ("dict", "Dict"))
        )
        self.assertTrue(
            is_dict,
            "create_material.parameters annotation must be dict, got %s"
            % (ast.unparse(ann) if ann is not None else None))
        # 其余参数保持无注解
        for pname in ("ctx", "material_type", "name", "parent_path"):
            self.assertIsNone(
                by_name[pname].annotation,
                "create_material.%s must stay un-annotated" % pname)
        self.assertIsNone(
            fn.returns,
            "create_material must not have return type annotation")

    def test_assign_material_no_type_annotations(self):
        fn = next(f for f in self.fns if f.name == "assign_material")
        kinds = _signature_annotation_kinds(fn)
        self.assertFalse(
            kinds["arg_annotations"],
            "assign_material must not have parameter type annotations")
        self.assertFalse(
            kinds["return_annotation"],
            "assign_material must not have return type annotation")

    def test_get_material_info_no_type_annotations(self):
        fn = next(f for f in self.fns if f.name == "get_material_info")
        kinds = _signature_annotation_kinds(fn)
        self.assertFalse(
            kinds["arg_annotations"],
            "get_material_info must not have parameter type annotations")
        self.assertFalse(
            kinds["return_annotation"],
            "get_material_info must not have return type annotation")

    def test_create_material_chinese_docstring(self):
        fn = next(f for f in self.fns if f.name == "create_material")
        doc = ast.get_docstring(fn) or ""
        self.assertTrue(
            _has_cjk(doc),
            "create_material docstring must contain Chinese (CJK) text. "
            "Current docstring: {0!r}".format(doc))

    def test_assign_material_chinese_docstring(self):
        fn = next(f for f in self.fns if f.name == "assign_material")
        doc = ast.get_docstring(fn) or ""
        self.assertTrue(
            _has_cjk(doc),
            "assign_material docstring must contain Chinese (CJK) text. "
            "Current docstring: {0!r}".format(doc))

    def test_get_material_info_chinese_docstring(self):
        fn = next(f for f in self.fns if f.name == "get_material_info")
        doc = ast.get_docstring(fn) or ""
        self.assertTrue(
            _has_cjk(doc),
            "get_material_info docstring must contain Chinese (CJK) text. "
            "Current docstring: {0!r}".format(doc))


if __name__ == "__main__":
    unittest.main()