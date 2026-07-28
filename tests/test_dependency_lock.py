"""test_dependency_lock.py — 依赖锁三重版本断言
（refactor-opus-optional-and-debt-cleanup tasks 5.10 / 6.6 / 6.7）。

验证 declaration（pyproject.toml）、resolution（uv.lock）、runtime（installed
distribution）三层均精确为 ``mcp==1.12.2``，且 langchain 已移入 ``opus`` extra、
``mcp[cli]`` 不再保留范围约束。同时验证校验逻辑对 stale lockfile / 范围约束 /
重复 resolved mcp / 其他版本均判失败（C21 前置门禁语义）。
"""
import os
import re
import subprocess
import sys
import tomllib
import unittest


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PYPROJECT_PATH = os.path.join(ROOT, "pyproject.toml")
UVLOCK_PATH = os.path.join(ROOT, "uv.lock")

TARGET_MCP_VERSION = "1.12.2"


def _dep_name(dep):
    """从依赖声明提取包名（剥离 extras 与版本约束）。"""
    return re.split(r"[\[<>=!~;]", dep)[0].lower().strip()


def _load_pyproject():
    with open(PYPROJECT_PATH, "rb") as f:
        return tomllib.load(f)


def _load_uvlock():
    with open(UVLOCK_PATH, "rb") as f:
        return tomllib.load(f)


def _packages_by_name(lock_data):
    """把 uv.lock 的 [[package]] 列表按 name 索引（同名返回 list）。"""
    out = {}
    for pkg in lock_data.get("package", []):
        out.setdefault(pkg["name"], []).append(pkg)
    return out


def validate_triple(pyproject_deps, pyproject_opus_extra,
                    lock_requires_dist, lock_mcp_versions,
                    runtime_version):
    """C21 前置门禁：declaration / resolution / runtime 三重一致才返 True。

    任一不一致（范围约束、错误版本、重复 resolved mcp、stale）返 False。
    """
    # declaration: 默认依赖精确 mcp[cli]==1.12.2，不含 langchain
    if "mcp[cli]=={0}".format(TARGET_MCP_VERSION) not in pyproject_deps:
        return False
    for dep in pyproject_deps:
        if _dep_name(dep) in ("langchain", "langchain-classic"):
            return False
    # opus extra 含两项 langchain
    opus_set = set(_dep_name(dep) for dep in pyproject_opus_extra)
    if not {"langchain", "langchain-classic"}.issubset(opus_set):
        return False
    # 不保留范围约束
    for dep in pyproject_deps:
        if dep.lower().startswith("mcp[cli]") and "==" not in dep:
            return False
    # resolution: requires-dist 精确 ==1.12.2
    if not any("mcp" in str(r.get("name", "")) and
               r.get("specifier") == "=={0}".format(TARGET_MCP_VERSION)
               for r in lock_requires_dist):
        return False
    # 恰一个 resolved mcp，版本精确 1.12.2
    if len(lock_mcp_versions) != 1:
        return False
    if lock_mcp_versions[0] != TARGET_MCP_VERSION:
        return False
    # runtime
    if runtime_version != TARGET_MCP_VERSION:
        return False
    return True


class PyprojectDeclarationTests(unittest.TestCase):
    """5.10 / 6.3：pyproject 默认依赖与 opus extra metadata。"""

    def test_default_deps_exact_mcp_pin_no_langchain(self):
        data = _load_pyproject()
        deps = list(data["project"]["dependencies"])
        self.assertIn("mcp[cli]=={0}".format(TARGET_MCP_VERSION), deps)
        self.assertIn("requests>=2.31.0", deps)
        self.assertIn("python-dotenv>=1.0.0", deps)
        # 默认依赖不得含 langchain
        for dep in deps:
            base = dep.split("[")[0].lower()
            self.assertNotIn(base, ("langchain", "langchain-classic"),
                             "默认依赖不应含 langchain: " + dep)

    def test_mcp_no_range_constraint(self):
        """mcp[cli] 必须精确锁定，不得保留 >= 范围约束。"""
        data = _load_pyproject()
        for dep in data["project"]["dependencies"]:
            if dep.lower().startswith("mcp[cli]"):
                self.assertIn("==", dep,
                              "mcp[cli] 必须精确锁定: " + dep)
                self.assertNotIn(">=", dep,
                                 "mcp[cli] 不得保留范围约束: " + dep)

    def test_opus_extra_has_both_langchain(self):
        data = _load_pyproject()
        opus = data["project"]["optional-dependencies"]["opus"]
        bases = set(_dep_name(dep) for dep in opus)
        self.assertIn("langchain", bases)
        self.assertIn("langchain-classic", bases)
        self.assertEqual(len(opus), 2)


class UvLockResolutionTests(unittest.TestCase):
    """5.10 / 6.6：uv.lock houdinimcp requires-dist + 唯一 resolved mcp。"""

    def test_houdinimcp_requires_dist_mcp_exact_pin(self):
        lock = _load_uvlock()
        by_name = _packages_by_name(lock)
        hmcp = by_name["houdinimcp"][0]
        # uv.lock 的 [package.metadata] 嵌套在 package dict 内（tomllib 解析后）
        requires_dist = hmcp["metadata"]["requires-dist"]
        mcp_reqs = [r for r in requires_dist
                    if r.get("name") == "mcp"]
        self.assertEqual(len(mcp_reqs), 1,
                         "houdinimcp requires-dist 应恰有一个 mcp 条目")
        self.assertEqual(mcp_reqs[0]["specifier"],
                         "=={0}".format(TARGET_MCP_VERSION))
        # mcp 条目带 cli extras
        self.assertEqual(mcp_reqs[0].get("extras"), ["cli"])

    def test_exactly_one_resolved_mcp_at_target_version(self):
        lock = _load_uvlock()
        by_name = _packages_by_name(lock)
        mcp_pkgs = by_name.get("mcp", [])
        self.assertEqual(len(mcp_pkgs), 1,
                         "uv.lock 应恰有一个 resolved mcp package，实际: "
                         + repr([p["version"] for p in mcp_pkgs]))
        self.assertEqual(mcp_pkgs[0]["version"], TARGET_MCP_VERSION)

    def test_houdinimcp_default_deps_no_langchain(self):
        """uv.lock 中 houdinimcp 的默认 dependencies 不含 langchain。"""
        lock = _load_uvlock()
        by_name = _packages_by_name(lock)
        deps = [d["name"] for d in by_name["houdinimcp"][0]["dependencies"]]
        self.assertNotIn("langchain", deps)
        self.assertNotIn("langchain-classic", deps)
        # 默认依赖含 mcp / requests / python-dotenv
        for required in ("mcp", "requests", "python-dotenv"):
            self.assertIn(required, deps)

    def test_uvlock_in_sync_with_pyproject(self):
        """6.6：``uv lock --check`` 必须证明 lockfile 与 pyproject 同步。"""
        result = subprocess.run(
            [sys.executable, "-c",
             "import sys; sys.exit(0)"],  # placeholder, real check below
            capture_output=True)
        # 真正跑 uv lock --check（uv 在 PATH 中）
        proc = subprocess.run(
            ["uv", "lock", "--check"],
            cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(
            proc.returncode, 0,
            "uv lock --check 失败（lockfile stale）:\n" + proc.stderr)


class RuntimeVersionTests(unittest.TestCase):
    """6.7：实际测试环境 installed mcp distribution 精确 1.12.2。"""

    def test_installed_mcp_distribution_is_target(self):
        from importlib.metadata import version, PackageNotFoundError
        try:
            v = version("mcp")
        except PackageNotFoundError:
            self.fail("mcp distribution 未安装，无法做 runtime 版本断言")
        self.assertEqual(v, TARGET_MCP_VERSION,
                         "installed mcp 应为 {0}，实际 {1}".format(
                             TARGET_MCP_VERSION, v))


class TripleVersionGateTests(unittest.TestCase):
    """6.7：三重版本门禁对正确输入通过，对 stale / 范围 / 重复 / 错版本失败。"""

    GOOD_DEPS = ["mcp[cli]==1.12.2", "requests>=2.31.0",
                 "python-dotenv>=1.0.0"]
    GOOD_OPUS = ["langchain>=0.1.0", "langchain-classic>=1.0.0"]
    GOOD_REQUIRES_DIST = [
        {"name": "langchain", "specifier": ">=0.1.0",
         "marker": "extra == 'opus'"},
        {"name": "langchain-classic", "specifier": ">=1.0.0",
         "marker": "extra == 'opus'"},
        {"name": "mcp", "extras": ["cli"], "specifier": "==1.12.2"},
        {"name": "python-dotenv", "specifier": ">=1.0.0"},
        {"name": "requests", "specifier": ">=2.31.0"},
    ]

    def test_good_triple_passes(self):
        self.assertTrue(validate_triple(
            self.GOOD_DEPS, self.GOOD_OPUS,
            self.GOOD_REQUIRES_DIST, ["1.12.2"], "1.12.2"))

    def test_range_constraint_fails(self):
        bad = ["mcp[cli]>=1.4.1", "requests>=2.31.0",
               "python-dotenv>=1.0.0"]
        self.assertFalse(validate_triple(
            bad, self.GOOD_OPUS, self.GOOD_REQUIRES_DIST,
            ["1.12.2"], "1.12.2"))

    def test_wrong_declaration_version_fails(self):
        bad = ["mcp[cli]==1.4.1", "requests>=2.31.0",
               "python-dotenv>=1.0.0"]
        self.assertFalse(validate_triple(
            bad, self.GOOD_OPUS, self.GOOD_REQUIRES_DIST,
            ["1.12.2"], "1.12.2"))

    def test_stale_lockfile_version_fails(self):
        # declaration 对，但 resolved mcp 是旧版本（stale lockfile）
        self.assertFalse(validate_triple(
            self.GOOD_DEPS, self.GOOD_OPUS,
            self.GOOD_REQUIRES_DIST, ["1.4.1"], "1.12.2"))

    def test_duplicate_resolved_mcp_fails(self):
        self.assertFalse(validate_triple(
            self.GOOD_DEPS, self.GOOD_OPUS,
            self.GOOD_REQUIRES_DIST, ["1.12.2", "1.4.1"], "1.12.2"))

    def test_runtime_mismatch_fails(self):
        self.assertFalse(validate_triple(
            self.GOOD_DEPS, self.GOOD_OPUS,
            self.GOOD_REQUIRES_DIST, ["1.12.2"], "1.12.3"))

    def test_langchain_in_default_fails(self):
        bad = ["mcp[cli]==1.12.2", "requests>=2.31.0",
               "python-dotenv>=1.0.0", "langchain>=0.1.0"]
        self.assertFalse(validate_triple(
            bad, self.GOOD_OPUS, self.GOOD_REQUIRES_DIST,
            ["1.12.2"], "1.12.2"))


if __name__ == "__main__":
    unittest.main()
