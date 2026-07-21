"""Unit tests for the F0→F1→F2→F3 three-tier help-fallback dispatcher.

These tests cover the AI-agent-side triage helper that picks WHICH source to
query when verifying a hou API before calling it (per
`openspec/changes/opera-houdinimcp-unknown-api-guard/specs/mcp-tools/spec.md`
Scenario "Three-tier help fallback (local → online → ask user)").

The helper is a small pure function — NO network calls, NO hou import, NO
new pip deps. It returns one of three strings:

    "F1_local_docstring"   — favor in-Houdini / no-network sources
    "F2_online_sidefx"     — favor verify_hou_api / get_houdini_help (HTTP)
    "F3_ask_user"          — return failure; agent must surface a message

Priority order (highest first):
    F1 if cache_hit           (any version, any network -> local wins)
    F1 if hou_version < 20    (old hou, prefer local; SideFX may not exist)
    F1 if method_name starts with "hou." (Python attr, F1 docstring)
    F2 if network_available   (modern hou, fresh lookup)
    F3 otherwise              (modern hou, no network -> ask user)

Run with:
    python -m unittest tests.test_three_tier_fallback -v
"""
import unittest


# ---------------------------------------------------------------------------
# The helper under test. Inlined here (per design.md Wave A scope) so tests
# fail correctly when this contract drifts. Wave B may move the helper into
# a shared utility module; tests should still pass against either impl.
# ---------------------------------------------------------------------------
def select_help_source(method_name, hou_version_tuple,
                       network_available, cache_hit):
    """Return which F-tier to query first.

    Args:
        method_name: e.g. "ObjNode.setDisplayNode", "Node.setInput",
                     "hou.ObjNode.setDisplayNode". Used to detect
                     pure Python attrs (F1 sweet-spot).
        hou_version_tuple: tuple[int, int, int], e.g. (21, 0, 0).
                          Pre-20 versions are considered "old"; F1 prefers
                          local docstring as SideFX docs may be stale.
        network_available: True if SideFX doc fetch is reachable.
        cache_hit: True if AI session has previously verified this method
                   in-memory; local F1 wins unconditionally.

    Returns:
        One of: "F1_local_docstring", "F2_online_sidefx", "F3_ask_user".
    """
    if cache_hit:
        return "F1_local_docstring"
    if hou_version_tuple is not None and hou_version_tuple < (20, 0):
        return "F1_local_docstring"
    if method_name.startswith("hou."):
        # Pure Python attribute (e.g. hou.ObjNode.setInput) — Python
        # docstring via help() / __doc__ is the F1 source of truth.
        return "F1_local_docstring"
    if network_available:
        return "F2_online_sidefx"
    return "F3_ask_user"


class SelectHelpSourceTests(unittest.TestCase):
    """Coverage of F0→F1→F2→F3 priority order.

    The 6 required cases (per dispatcher task 4.8):
        1. cache_hit=True -> F1
        2. cache_hit=False + network_available=True -> F2
        3. cache_hit=False + network_available=False -> F3
        4. hou_version_tuple < (20, 0) -> F1
        5. hou_version_tuple >= (21, 0) + network_available -> F2
        6. method_name starts with "hou." -> F1
    Plus extra coverage for the F0 baseline + version boundaries.
    """

    def test_cache_hit_returns_F1(self):
        """Case 1: cache wins over any other preference."""
        self.assertEqual(
            select_help_source(
                "ObjNode.setDisplayNode",
                hou_version_tuple=(21, 0, 0),
                network_available=True,
                cache_hit=True),
            "F1_local_docstring",
        )
        self.assertEqual(
            select_help_source(
                "ObjNode.setDisplayNode",
                hou_version_tuple=(21, 0, 0),
                network_available=False,
                cache_hit=True),
            "F1_local_docstring",
        )

    def test_no_cache_network_available_returns_F2(self):
        """Case 2: no cache + network up -> SideFX (F2)."""
        self.assertEqual(
            select_help_source(
                "Node.setInput",
                hou_version_tuple=(21, 0, 0),
                network_available=True,
                cache_hit=False),
            "F2_online_sidefx",
        )

    def test_no_cache_no_network_returns_F3(self):
        """Case 3: no cache + network down + modern hou -> ask user."""
        self.assertEqual(
            select_help_source(
                "ObjNode.setDisplayNode",
                hou_version_tuple=(21, 0, 0),
                network_available=False,
                cache_hit=False),
            "F3_ask_user",
        )

    def test_old_hou_returns_F1_regardless_of_network(self):
        """Case 4: pre-H20 prefers local docstring."""
        self.assertEqual(
            select_help_source(
                "Node.setInput",
                hou_version_tuple=(19, 5, 0),
                network_available=True,
                cache_hit=False),
            "F1_local_docstring",
        )
        self.assertEqual(
            select_help_source(
                "Node.setInput",
                hou_version_tuple=(19, 5, 0),
                network_available=False,
                cache_hit=False),
            "F1_local_docstring",
        )

    def test_h21_plus_with_network_returns_F2(self):
        """Case 5: H21+ + network available -> SideFX."""
        self.assertEqual(
            select_help_source(
                "ObjNode.setDisplayNode",
                hou_version_tuple=(21, 0, 0),
                network_available=True,
                cache_hit=False),
            "F2_online_sidefx",
        )
        self.assertEqual(
            select_help_source(
                "ObjNode.setDisplayNode",
                hou_version_tuple=(22, 0, 500),
                network_available=True,
                cache_hit=False),
            "F2_online_sidefx",
        )

    def test_hou_prefix_returns_F1(self):
        """Case 6: 'hou.X' Python attr -> docstring (F1)."""
        self.assertEqual(
            select_help_source(
                "hou.ObjNode.setDisplayNode",
                hou_version_tuple=(21, 0, 0),
                network_available=True,
                cache_hit=False),
            "F1_local_docstring",
        )
        self.assertEqual(
            select_help_source(
                "hou.node",
                hou_version_tuple=(22, 0, 0),
                network_available=False,
                cache_hit=False),
            "F1_local_docstring",
        )

    # ----- Extra coverage (boundary conditions) -----

    def test_hou_version_exactly_20_is_modern(self):
        """Boundary: H20 is 'modern' per this helper (< (20,0) is old)."""
        self.assertEqual(
            select_help_source(
                "ObjNode.setDisplayNode",
                hou_version_tuple=(20, 0, 0),
                network_available=True,
                cache_hit=False),
            "F2_online_sidefx",
        )
        # Just below boundary -> F1
        self.assertEqual(
            select_help_source(
                "ObjNode.setDisplayNode",
                hou_version_tuple=(19, 999, 999),
                network_available=True,
                cache_hit=False),
            "F1_local_docstring",
        )

    def test_cache_hit_beats_old_hou(self):
        """Boundary: cache_hit outranks old-hou F1 trigger (same result,
        but ensures priority ordering is intentional)."""
        self.assertEqual(
            select_help_source(
                "ObjNode.setDisplayNode",
                hou_version_tuple=(19, 5, 0),
                network_available=False,
                cache_hit=True),
            "F1_local_docstring",
        )

    def test_no_cache_modern_hou_no_network(self):
        """Boundary: H21+ with network down -> F3 (do NOT silently pick F1)."""
        self.assertEqual(
            select_help_source(
                "Node.setInput",
                hou_version_tuple=(21, 0, 0),
                network_available=False,
                cache_hit=False),
            "F3_ask_user",
        )

    def test_hou_prefix_beats_no_network(self):
        """Even with no network, hou.X Python attr still picks F1 (Python
        docstring is local, never needs network)."""
        self.assertEqual(
            select_help_source(
                "hou.node",
                hou_version_tuple=(21, 0, 0),
                network_available=False,
                cache_hit=False),
            "F1_local_docstring",
        )

    def test_class_method_form_does_NOT_short_circuit_to_F1(self):
        """'ObjNode.setInput' (no 'hou.' prefix) MUST hit F2/F3 by normal
        rules; F1 only kicks in for cache_hit OR old-hou OR hou. prefix."""
        # Modern hou + network -> F2
        self.assertEqual(
            select_help_source(
                "ObjNode.setInput",
                hou_version_tuple=(22, 0, 0),
                network_available=True,
                cache_hit=False),
            "F2_online_sidefx",
        )
        # Modern hou + NO network -> F3 (NOT F1)
        self.assertEqual(
            select_help_source(
                "ObjNode.setInput",
                hou_version_tuple=(22, 0, 0),
                network_available=False,
                cache_hit=False),
            "F3_ask_user",
        )


class SelectHelpSourceNetworkPolicyTests(unittest.TestCase):
    """No-network-call invariant (per spec: 'All paths NO network calls')."""

    def test_helper_does_not_import_network_modules(self):
        """The helper source MUST NOT import urllib / requests / socket.
        Detected by simple regex scan (definitive for ASCII imports)."""
        import re
        import inspect
        src = inspect.getsource(select_help_source)
        banned = [r"\bimport urllib\b", r"\bimport requests\b",
                  r"\bfrom urllib\b", r"\bfrom requests\b",
                  r"\bimport socket\b", r"\bfrom socket\b",
                  r"http\.client", r"urllib\.request\.urlopen"]
        for pat in banned:
            self.assertNotRegex(
                src, pat,
                "select_help_source must not import network modules "
                "(matched %s): %s" % (pat, src),
            )


if __name__ == "__main__":
    unittest.main()
