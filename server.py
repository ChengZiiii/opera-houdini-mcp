import hou
import json
import struct
import threading
import socket
import time
import difflib
import fnmatch
from itertools import islice
from contextlib import contextmanager
import requests
import tempfile
import traceback
import os
import shutil
import sys
# Try PySide6 first (Houdini 21.0+), fall back to PySide2 (older versions)
try:
    from PySide6 import QtWidgets, QtCore
    print("Using PySide6 (Houdini 21.0+)")
except ImportError:
    try:
        from PySide2 import QtWidgets, QtCore
        print("Using PySide2 (Houdini 19.5-20.x)")
    except ImportError:
        print("Warning: Neither PySide6 nor PySide2 found. Some features may not work.")
        # Create dummy classes to prevent import errors
        class QtCore:
            class QTimer:
                pass
        QtWidgets = None
import io
from contextlib import redirect_stdout, redirect_stderr
from . import _common as cmn
from . import _scene as scn
from . import _error_nodes as en
from . import _discovery as disc
from . import _materials as mats
from . import _hscript as hsc
from . import _graph_edit as ge
from . import _render_policy as _rp
from . import _node_info as ni
from . import _geo_summary as gs
from . import _pane_capture as pcp
from . import _capture_paths as cap
from . import _render_b64 as rb64
from . import _help as hlp
from . import _events as evs
from . import _animation as anim
from . import _render_jobs as _rjobs
from . import _render_settings as _rset

RENDER_POLICY_COMMANDS = getattr(_rp, "RENDER_POLICY_COMMANDS", {})
register_render_policy_command = getattr(
    _rp, "register_render_policy_command", None)

# PR 4 scene-diff cache：execute_code(capture_diff=True) 时填充；get_last_scene_diff 读取。
_before_scene = None
_after_scene = None


# ---------------------------------------------------------------------------
# PR 18：verify_hou_api 调 hou API 时合成给 AI 的简短提示（_ai_hint）
#
# 纯字符串模板函数，无 hou / 无网络依赖，模块级以便单元测试直接 import。
# 规则与 openspec/changes/opera-houdinimcp-unknown-api-guard/design.md §2
# 一致；改逻辑时同步更新 tests/test_verify_hou_api.py 内的契约。
# ---------------------------------------------------------------------------
def _synthesize_ai_hint(item_name, help_result):
    """根据 get_houdini_help 的返回 dict 合成一段简短 AI 提示。

    返回字符串（非空 = 给 AI 看的提示；空串 = 无可用提示）。

    规则：
      - 空 / None help_result → "" （防御性）
      - status == "error"  → F3 fallback：提示用 hou.node(path).help() 或
        print(hou.<Class>.<method>.__doc__) 拿本地 docstring；若仍失败
        请在输出 `## Assumptions` 段记录假设。
      - status == "success" 且 methods == []：
          - python_hou + item_name.startswith("ObjNode.") → F-C known
            pattern：OBJ 容器无 setDisplayNode 等 display 方法，改用
            SOP 子节点的 setDisplayFlag(True) + setRenderFlag(True)。
          - 其他 → "API 不存在 / 方法集合空" 提示，建议 hasattr
            (obj, item_name.split('.')[-1]) 兜底。
      - status == "success" 且 methods != [] → 抽第一条 method 行
        拼成 "已找到方法: <signature>"，让 AI 直接拿来用。
      - 其它未知 status → "" （防御性）
    """
    if not help_result:
        return ""
    status = help_result.get("status")
    methods = help_result.get("methods") or []
    help_type = help_result.get("help_type", "")

    if status == "error":
        # F3 fallback：本地 hou help + 假设日志
        err = help_result.get("error") or help_result.get("message") or ""
        return (
            "⚠ SideFX 文档站不可达: %s。 fallback: 试 "
            "hou.node(item_path).help() 或 print(hou.<Class>.<method>.__doc__)"
            " 拿本地 docstring； 若仍失败请在输出 `## Assumptions` 段记录假设。"
            % err
        )

    if status == "success":
        if not methods:
            # Empty methods：API 不存在
            if help_type == "python_hou" and item_name.startswith("ObjNode."):
                # F-C known pattern：OBJ 容器无 setDisplayNode 等 display 方法，
                # 改用 SOP 子节点的 setDisplayFlag + setRenderFlag
                return (
                    "方法不存在于 hou.ObjNode； OBJ 容器显示请设子 SOP 的 "
                    "setDisplayFlag(True) + setRenderFlag(True)"
                )
            return (
                "API 不存在 / 方法集合空； 建议 hasattr(obj, %r) 兜底"
                % item_name.split(".")[-1]
            )
        # Non-empty methods：抽第一条 method 行拼接
        first = (methods[0].get("text", "")
                 if isinstance(methods[0], dict) else str(methods[0]))
        return "已找到方法: %s" % first

    # Unknown status
    return ""

# Imports for OPUS import
import zipfile
from urllib.parse import urlparse
import uuid # For unique temp dirs and file processing

# --- NEW: Import render functions --- 
# try:
from .HoudiniMCPRender import *
# HMCPLib = HoudiniMCPRender # Alias for easier use
print("HoudiniMCPRender module loaded successfully.")
# except ImportError:
#     HMCPLib = None
#     print("Warning: HoudiniMCPRender.py not found or failed to import. Rendering tools will be unavailable.")
# ----------------------------------

# Info about the extension (optional metadata)
EXTENSION_NAME = "Houdini MCP"
EXTENSION_VERSION = (0, 1)
EXTENSION_DESCRIPTION = "Connect Houdini to Claude via MCP"


def _safe_server_print(message):
    try:
        print(message)
    except Exception:
        pass


_BATCH_DEFAULT_MAX_OPERATIONS = 50
_BATCH_MIN_OPERATIONS = 1
_BATCH_MAX_OPERATIONS = 200


def _evaluate_render_policy_command(command, params):
    """通过共享 registry 执行 server-side Layer 2 policy preflight。"""
    evaluator = getattr(_rp, "evaluate_render_policy_command", None)
    if evaluator is None:
        return None
    return evaluator(command, params)


class HoudiniMCPServer:
    MUTATING_COMMANDS = frozenset({
        "create_node", "modify_node", "delete_node", "set_material",
        "connect_nodes", "disconnect_input", "set_parameters",
        "set_node_flags", "layout_children", "reorder_inputs",
        "set_node_position", "set_node_color", "create_network_box",
        "create_wrangle", "set_wrangle_code", "create_material",
        "assign_material",
        # PR 19: animation / frame / expression 数据写
        # （set_keyframe / set_keyframes / delete_keyframe / set_expression
        # 是参数通道持久写；set_frame_range / set_playback_range 是场景
        # 状态写，均可 undo）。set_frame / playbar_control 是运行态时间线
        # 写，在 NO_UNDO_COMMANDS 中，不进 undo group。
        "set_frame_range", "set_playback_range",
        "set_keyframe", "set_keyframes", "delete_keyframe",
        "set_expression",
        # C9: 受限可撤销 ROP 写入（白名单内 parm / 受限创建）。
        "set_render_settings", "create_render_node",
        # R10: capture_pane_screenshot, capture_multiple_panes and
        # render_node_network are intentionally classified in NO_UNDO_COMMANDS.
    })

    READ_ONLY_COMMANDS = frozenset({
        "get_scene_info", "serialize_scene", "get_node_info",
        "get_last_scene_diff", "get_asset_lib_status",
        "get_parameter_schema", "find_error_nodes", "get_geo_summary",
        "list_visible_panes", "get_geometry_info", "get_geometry_data",
        "list_node_types", "list_children", "find_nodes", "ping",
        "get_material_info", "get_houdini_help", "verify_hou_api",
        "check_connection", "ping_houdini", "get_asset_categories",
        "search_assets",
        # PR 19: 帧 / 关键帧状态读取，不修改场景或参数
        "get_frame", "get_keyframes",
        # C9: ROP 枚举 / 白名单 parm 读取，只查询不写。
        "list_render_nodes", "get_render_settings",
    })

    NO_UNDO_COMMANDS = frozenset({
        "save_scene", "load_scene", "new_scene", "execute_code",
        "execute_hscript", "import_opus_url", "import_asset", "cook_node",
        "manage_cache", "capture_pane_screenshot", "capture_multiple_panes",
        "capture_sceneviewer_flipbook_views", "render_node_network",
        "render_single_view", "render_quad_view", "render_specific_camera",
        "render_viewport_base64", "render_quad_views_base64",
        "render_specific_camera_base64",
        "get_pending_events", "subscribe_events", "unsubscribe_events",
        # PR 19: 运行态时间线写 — 帧和播放控制不产生可撤销场景编辑。
        # batch dispatcher 在 NO_UNDO 命令前关闭 undo segment，确保这些
        # 命令永远不在 hou.undos.group 中执行。
        "set_frame", "playbar_control",
        # C9: 同步 render + 落盘副作用 + OS 进程启动都不由 HIP undo
        # 恢复，必须 no-undo。
        "start_render",
    })

    OPTIONAL_ASSET_COMMANDS = frozenset({
        "get_asset_categories", "search_assets", "import_asset",
    })

    def __init__(self, host='127.0.0.1', port=9876):
        self.host = host
        self.port = port
        self.running = False
        self.server_socket = None
        self.client = None
        self.buffer = b''
        self.timer = None
        self._client_connected_at = None
        self._last_activity = time.monotonic()
        self._batch_active = False
        self._validate_handler_classification(self._get_command_handlers())

    def start(self):
        """Begin listening on the given port; sets up a QTimer to poll for data."""
        if self.running:
            print(f"HoudiniMCP server is already running on {self.host}:{self.port}")
            return

        self._cleanup_client()
        self._cleanup_socket()
        self._cleanup_timer()

        self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        try:
            self.server_socket.bind((self.host, self.port))
            self.server_socket.listen(4)
            self.server_socket.setblocking(False)

            self.timer = QtCore.QTimer()
            self.timer.timeout.connect(self._process_server)
            self.timer.start(100)

            self.running = True
            self._last_activity = time.monotonic()
            try:
                evs.attach_callbacks(hou)
            except Exception as callback_error:
                print("HoudiniMCP 事件 callback attach 失败（不影响服务）: "
                      + str(callback_error))
            print(f"HoudiniMCP server started on {self.host}:{self.port}")

            # Bug C（PR 21）：启动时清理 > 7 天的过期截图 / 渲染目录。
            # 不抛异常（启动失败不影响 MCP 服务本身）。
            try:
                import hou as _hou  # server.py 在 Houdini 内运行
                base = cap.resolve_base_dir(hou=_hou)
                result = cap.cleanup_old_captures(base, max_age_days=7)
                if result["scanned"] > 0:
                    print(
                        "HoudiniMCP 启动清理: base={0} scanned={1} deleted={2} kept={3} errors={4}".format(
                            base, result["scanned"], result["deleted"],
                            result["kept"], len(result["errors"])))
            except Exception as cleanup_err:
                print("HoudiniMCP 启动清理失败（不影响 MCP 服务）: " + str(cleanup_err))
        except Exception as e:
            print(f"Failed to start server: {str(e)}")
            self.stop()
            
    def stop(self):
        """Stop listening; close sockets and timers."""
        self.running = False
        try:
            evs.detach_callbacks()
        except Exception as callback_error:
            print("HoudiniMCP 事件 callback detach 失败（继续清理）: "
                  + str(callback_error))
        self._cleanup_timer()
        self._cleanup_client()
        self._cleanup_socket()
        print("HoudiniMCP server stopped")

    def _cleanup_timer(self):
        if self.timer is not None:
            try:
                self.timer.stop()
            except Exception:
                pass
            self.timer = None

    def _cleanup_client(self):
        if self.client is not None:
            try:
                self.client.close()
            except Exception:
                pass
            self.client = None
        self._client_connected_at = None
        self.buffer = b''

    def _cleanup_socket(self):
        if self.server_socket is not None:
            try:
                self.server_socket.close()
            except Exception:
                pass
            self.server_socket = None

    def _process_server(self):
        """
        Timer callback to accept connections and process any incoming data.
        This runs in the main Houdini thread to avoid concurrency issues.
        
        Protocol: each message is a 4-byte big-endian length prefix
        followed by that many bytes of UTF-8 JSON.
        """
        if not self.running:
            return
        
        try:
            # Accept all pending connections; the newest client wins. A stale
            # idle client (e.g. an abandoned bridge process) must never be able
            # to hold the slot and lock new clients out of the server.
            if self.server_socket:
                while True:
                    try:
                        new_client, address = self.server_socket.accept()
                    except BlockingIOError:
                        break
                    except Exception as e:
                        _safe_server_print(
                            "Error accepting connection: " + str(e))
                        break
                    if self.client is not None:
                        _safe_server_print(
                            "New connection from {0}; replacing existing client".format(address))
                        self._cleanup_client()
                    new_client.setblocking(False)
                    self.client = new_client
                    self._client_connected_at = time.monotonic()
                    self._last_activity = self._client_connected_at
                    _safe_server_print("Connected to client: " + str(address))
            
            if self.client:
                try:
                    data = self.client.recv(8192)
                    if data:
                        self._last_activity = time.monotonic()
                        self.buffer += data
                        while True:
                            if len(self.buffer) < 4:
                                break
                            msg_len = struct.unpack('>I', self.buffer[:4])[0]
                            MAX_MSG_LEN = 50 * 1024 * 1024
                            if msg_len > MAX_MSG_LEN:
                                _safe_server_print(
                                    "Message too large ({0} bytes), disconnecting client".format(msg_len))
                                self._cleanup_client()
                                break
                            if len(self.buffer) < 4 + msg_len:
                                break
                            payload = self.buffer[4:4 + msg_len]
                            self.buffer = self.buffer[4 + msg_len:]
                            try:
                                command = json.loads(payload.decode('utf-8'))
                                response = self.execute_command(command)
                                response_bytes = json.dumps(response).encode('utf-8')
                                response_frame = struct.pack('>I', len(response_bytes)) + response_bytes
                                try:
                                    self.client.sendall(response_frame)
                                    self._last_activity = time.monotonic()
                                except (BrokenPipeError, ConnectionResetError, OSError) as send_err:
                                    self._cleanup_client()
                                    _safe_server_print(
                                        "Failed to send response (client likely disconnected): " + str(send_err))
                                    break
                            except json.JSONDecodeError as e:
                                print(f"Invalid JSON in message: {e}")
                    else:
                        self._cleanup_client()
                        _safe_server_print("Client disconnected (empty recv)")
                except BlockingIOError:
                    pass
                except (ConnectionResetError, BrokenPipeError, OSError) as e:
                    self._cleanup_client()
                    _safe_server_print("Client connection lost: " + str(e))

        except Exception as e:
            _safe_server_print("Server error: " + str(e))

    # -------------------------------------------------------------------------
    # Command Handling
    # -------------------------------------------------------------------------
    
    def execute_command(self, command):
        """Entry point for executing a JSON command from the client."""
        try:
            return self._execute_command_internal(command)
        except Exception as e:
            print(f"Error executing command: {str(e)}")
            traceback.print_exc()
            return {"status": "error", "message": str(e)}

    def _execute_command_internal(self, command):
        """
        Internal dispatcher that looks up 'cmd_type' from the JSON,
        calls the relevant function, and returns a JSON-friendly dict.
        """
        if not isinstance(command, dict):
            return {"status": "error", "message": "Command must be an object"}
        cmd_type = command.get("type")
        params = command.get("params", {})
        handlers = self._get_command_handlers()
        handler = handlers.get(cmd_type)
        if not handler:
            return {"status": "error", "message": f"Unknown command type: {cmd_type}"}

        if not isinstance(params, dict):
            return {"status": "error", "message": "Command params must be an object"}

        print(f"Executing handler for {cmd_type}")
        with self._undo_group(cmd_type):
            result = handler(**params)
        if cmd_type == "batch":
            result = cmn.apply_response_cap(result)
        print(f"Handler execution complete for {cmd_type}")
        return {"status": "success", "result": result}

    def _get_command_handlers(self):
        """返回当前 Houdini 进程唯一的 command handler registry。"""
        handlers = {
            "get_scene_info": self.get_scene_info,
            "save_scene": self.save_scene,
            "load_scene": self.load_scene,
            "new_scene": self.new_scene,
            "serialize_scene": self.serialize_scene,
            "create_node": self.create_node,
            "modify_node": self.modify_node,
            "delete_node": self.delete_node,
            "get_node_info": self.get_node_info,
            "execute_code": self.execute_code,
            "get_last_scene_diff": self.get_last_scene_diff,
            "set_material": self.set_material,
            "get_asset_lib_status": self.get_asset_lib_status,
            "import_opus_url": self.handle_import_opus_url,
            "connect_nodes": self.connect_nodes,
            "disconnect_input": self.disconnect_input,
            "set_parameters": self.set_parameters,
            "get_parameter_schema": self.get_parameter_schema,
            "set_node_flags": self.set_node_flags,
            "layout_children": self.layout_children,
            "reorder_inputs": self.reorder_inputs,
            "set_node_position": self.set_node_position,
            "set_node_color": self.set_node_color,
            "create_network_box": self.create_network_box,
            "find_error_nodes": self.find_error_nodes,
            "cook_node": self.cook_node,
            "get_geo_summary": self.get_geo_summary,
            "capture_pane_screenshot": self.capture_pane_screenshot,
            "capture_sceneviewer_flipbook_views": self.capture_sceneviewer_flipbook_views,
            "list_visible_panes": self.list_visible_panes,
            "capture_multiple_panes": self.capture_multiple_panes,
            "render_node_network": self.render_node_network,
            "create_wrangle": self.create_wrangle,
            "set_wrangle_code": self.set_wrangle_code,
            "get_geometry_info": self.get_geometry_info,
            "get_geometry_data": self.get_geometry_data,
            "render_single_view": self.handle_render_single_view,
            "render_quad_view": self.handle_render_quad_view,
            "render_specific_camera": self.handle_render_specific_camera,
            "render_viewport_base64": self.render_viewport_base64,
            "render_quad_views_base64": self.render_quad_views_base64,
            "render_specific_camera_base64": self.render_specific_camera_base64,
            "list_node_types": self.list_node_types,
            "list_children": self.list_children,
            "find_nodes": self.find_nodes,
            "manage_cache": self.manage_cache,
            "ping": self._handle_ping,
            "create_material": self.create_material,
            "assign_material": self.assign_material,
            "get_material_info": self.get_material_info,
            "execute_hscript": self.execute_hscript,
            "get_houdini_help": self.get_houdini_help,
            "verify_hou_api": self.verify_hou_api,
            "check_connection": self.check_connection,
            "ping_houdini": self.ping_houdini,
            "get_pending_events": self.get_pending_events,
            "subscribe_events": self.subscribe_events,
            "unsubscribe_events": self.unsubscribe_events,
            "batch": self.batch,
# PR 19: 动画与帧控制（10 个 handler；分类见
        # MUTATING_COMMANDS / READ_ONLY_COMMANDS / NO_UNDO_COMMANDS。
        # 全部走 _animation 模块 + apply_response_cap）。
        "get_frame": self.get_frame,
        "set_frame": self.set_frame,
        "set_frame_range": self.set_frame_range,
        "set_playback_range": self.set_playback_range,
        "set_keyframe": self.set_keyframe,
        "set_keyframes": self.set_keyframes,
        "delete_keyframe": self.delete_keyframe,
        "get_keyframes": self.get_keyframes,
        "playbar_control": self.playbar_control,
        "set_expression": self.set_expression,
        # C9 add-render-workflow-tools：5 个 ROP 设置 / 渲染 handler。
        # 三分类见 MUTATING_COMMANDS / READ_ONLY_COMMANDS /
        # NO_UNDO_COMMANDS；start_render 加入共享 RENDER_POLICY_COMMANDS
        # registry（bridge Layer 1 / server Layer 2 / _render_jobs
        # Layer 3 / _render_node_sync Layer 4 同源 policy helper）。
        "list_render_nodes": self.handle_list_render_nodes,
        "get_render_settings": self.handle_get_render_settings,
        "set_render_settings": self.handle_set_render_settings,
        "create_render_node": self.handle_create_render_node,
        "start_render": self.handle_start_render,
    }

        if getattr(getattr(hou, "session", None),
                   "houdinimcp_use_assetlib", False):
            handlers.update({
                "get_asset_categories": self.get_asset_categories,
                "search_assets": self.search_assets,
                "import_asset": self.import_asset,
            })
        self._validate_handler_classification(handlers)
        return handlers

    @classmethod
    def _validate_handler_classification(cls, handlers):
        """断言 registry key 恰好属于一个 undo 分类。"""
        command_sets = (
            cls.MUTATING_COMMANDS,
            cls.READ_ONLY_COMMANDS,
            cls.NO_UNDO_COMMANDS,
        )
        if any(len(set(items)) != len(items) for items in command_sets):
            raise AssertionError("command classification contains duplicates")
        for left_index, left in enumerate(command_sets):
            for right in command_sets[left_index + 1:]:
                if left & right:
                    raise AssertionError("command classification overlaps")
        registered = set(handlers) - {"batch"}
        classified = set().union(*command_sets)
        missing = registered - classified
        extra = classified - registered - cls.OPTIONAL_ASSET_COMMANDS
        if missing or extra:
            raise AssertionError(
                "command classification mismatch: missing={0}, extra={1}".format(
                    sorted(missing), sorted(extra)))

    @contextmanager
    def _undo_group(self, cmd_type):
        if cmd_type in self.MUTATING_COMMANDS and hasattr(hou, "undos"):
            with hou.undos.group(f"MCP: {cmd_type}"):
                yield
        else:
            yield

    @staticmethod
    def _batch_operation_limit():
        raw_value = os.environ.get(
            "HOUDINI_MCP_BATCH_MAX_OPERATIONS",
            str(_BATCH_DEFAULT_MAX_OPERATIONS))
        try:
            value = int(raw_value)
        except (TypeError, ValueError):
            value = _BATCH_DEFAULT_MAX_OPERATIONS
        return max(_BATCH_MIN_OPERATIONS, min(_BATCH_MAX_OPERATIONS, value))

    @staticmethod
    def _batch_error(message, requested=0):
        return cmn.apply_response_cap({
            "status": "error",
            "requested": requested,
            "executed": 0,
            "succeeded": 0,
            "failed": 0,
            "results": [],
            "error": message,
        })

    @staticmethod
    def _classify_handler_result(result):
        """把 handler 原始返回值分类为 success/error/control。"""
        if not isinstance(result, dict):
            return "success"
        if "_redirect" in result:
            return "redirect"
        if "_interrupt" in result:
            return "interrupt"
        if str(result.get("status", "")).lower() == "error":
            return "error"
        if result.get("error"):
            return "error"
        return "success"

    @classmethod
    def _batch_control_response(cls, policy_result, index, command_type,
                                requested):
        control = dict(policy_result)
        control.setdefault("status", "error")
        control["requested"] = requested
        control["executed"] = 0
        control["succeeded"] = 0
        control["failed"] = 0
        control["results"] = []
        control["operation_index"] = index
        control["operation_type"] = command_type
        return cmn.apply_response_cap(control)

    def _batch_preflight(self, operations):
        if not isinstance(operations, list):
            return self._batch_error("operations must be a list"), None
        requested = len(operations)
        limit = self._batch_operation_limit()
        if requested > limit:
            return self._batch_error(
                "operations exceeds batch limit {0}".format(limit), requested), None

        handlers = self._get_command_handlers()
        for index, operation in enumerate(operations):
            if not isinstance(operation, dict):
                return self._batch_error(
                    "operation {0} must be an object".format(index), requested), None
            command_type = operation.get("type")
            if not isinstance(command_type, str) or not command_type.strip():
                return self._batch_error(
                    "operation {0} type must be a non-empty string".format(index),
                    requested), None
            if command_type == "batch":
                return self._batch_error(
                    "nested batch operations are not allowed", requested), None
            if "params" in operation and not isinstance(operation["params"], dict):
                return self._batch_error(
                    "operation {0} params must be an object".format(index),
                    requested), None
            if command_type not in handlers:
                return self._batch_error(
                    "Unknown command type: {0}".format(command_type), requested), None

        render_policy_commands = getattr(_rp, "RENDER_POLICY_COMMANDS", {})
        for index, operation in enumerate(operations):
            command_type = operation["type"]
            if command_type not in render_policy_commands:
                continue
            policy_result = _evaluate_render_policy_command(
                command_type, operation.get("params", {}))
            if policy_result is not None:
                return self._batch_control_response(
                    policy_result, index, command_type, requested), None
        return None, handlers

    @staticmethod
    def _open_batch_undo_segment():
        if not hasattr(hou, "undos"):
            return None
        segment = hou.undos.group("MCP: batch")
        segment.__enter__()
        return segment

    @staticmethod
    def _close_batch_undo_segment(segment):
        if segment is None:
            return None
        try:
            segment.__exit__(None, None, None)
        except Exception as error:
            print("Failed to close MCP batch undo segment: {0}".format(error))
        return None

    def batch(self, operations, continue_on_error=True):
        """顺序执行 operations，按 mutating segment 合并 undo 且不回滚。"""
        if self._batch_active:
            return self._batch_error("nested batch execution is not allowed")
        if not isinstance(continue_on_error, bool):
            requested = len(operations) if isinstance(operations, list) else 0
            return self._batch_error(
                "continue_on_error must be a boolean", requested)

        self._batch_active = True
        try:
            preflight_error, handlers = self._batch_preflight(operations)
            if preflight_error is not None:
                return preflight_error

            requested = len(operations)
            results = []
            succeeded = 0
            failed = 0
            stopped_by_control = False
            stopped_on_error = False
            segment = None
            try:
                for index, operation in enumerate(operations):
                    command_type = operation["type"]
                    params = operation.get("params", {})
                    raw_result = None
                    outcome = "error"
                    try:
                        if command_type in self.MUTATING_COMMANDS:
                            if segment is None:
                                segment = self._open_batch_undo_segment()
                        else:
                            segment = self._close_batch_undo_segment(segment)
                        raw_result = handlers[command_type](**params)
                        outcome = self._classify_handler_result(raw_result)
                    except Exception as error:
                        raw_result = {
                            "status": "error",
                            "message": str(error),
                            "origin": "batch",
                            "exception": error.__class__.__name__,
                        }
                        outcome = "error"

                    if outcome == "success":
                        succeeded += 1
                        results.append({
                            "operation_index": index,
                            "operation_type": command_type,
                            "status": "success",
                            "result": raw_result,
                        })
                        continue

                    failed += 1
                    if isinstance(raw_result, dict):
                        result_entry = dict(raw_result)
                    else:
                        result_entry = {"error": str(raw_result)}
                    if "status" not in result_entry:
                        result_entry["status"] = "error"
                    result_entry["operation_index"] = index
                    result_entry["operation_type"] = command_type
                    result_entry["response"] = raw_result
                    results.append(result_entry)

                    if outcome in ("redirect", "interrupt"):
                        stopped_by_control = True
                        break
                    if not continue_on_error:
                        stopped_on_error = True
                        break
            finally:
                self._close_batch_undo_segment(segment)

            if failed == 0:
                status = "success"
            elif stopped_by_control or stopped_on_error:
                status = "error"
            else:
                status = "partial"
            return cmn.apply_response_cap({
                "status": status,
                "requested": requested,
                "executed": len(results),
                "succeeded": succeeded,
                "failed": failed,
                "results": results,
            })
        finally:
            self._batch_active = False

    def _handle_ping(self):
        return {"pong": True, "protocol": 1}

    def client_presence(self):
        """只读返回当前是否存在 active TCP client。"""
        return self.client is not None

    def has_client(self):
        """client_presence 的兼容别名，供 headless host 观测。"""
        return self.client_presence()

    def get_client_presence(self):
        """返回 client presence，不改变 newest-client-wins 语义。"""
        return self.client_presence()

    def last_activity(self):
        """只读返回 monotonic activity timestamp。"""
        return self._last_activity

    def get_last_activity(self):
        """last_activity 的兼容别名，供 headless host 观测。"""
        return self.last_activity()

    # -------------------------------------------------------------------------
    # Basic Info & Node Operations
    # -------------------------------------------------------------------------

    def get_pending_events(self, limit=100, cursor=None):
        """分页 drain 进程级事件；响应 cap 不会丢弃未实际返回的事件。"""
        return evs.PROCESS_EVENT_STATE.drain(
            limit=limit,
            cursor=cursor,
            response_cap=cmn.apply_response_cap,
        )

    def subscribe_events(self, types=None):
        """设置进程级 newest-client-wins 事件订阅，不创建 scene undo。"""
        return cmn.apply_response_cap(
            evs.PROCESS_EVENT_STATE.subscribe(types))

    def unsubscribe_events(self, types=None):
        """移除事件订阅或清空订阅，操作保持幂等且不创建 scene undo。"""
        return cmn.apply_response_cap(
            evs.PROCESS_EVENT_STATE.unsubscribe(types))

    def get_asset_lib_status(self):
        """Checks if the user toggled asset library usage in hou.session."""
        use_assetlib = getattr(hou.session, "houdinimcp_use_assetlib", False)
        msg = ("Asset library usage is enabled." 
               if use_assetlib 
               else "Asset library usage is disabled.")
        return {"enabled": use_assetlib, "message": msg}

    def get_scene_info(self):
        """Returns basic info about the current .hip file and top-level nodes per context.

        PR 5 增强：合并 _scene.get_scene_info(hou) 输出，新增 houdini_version /
        node_count / file_path 字段；保留旧的 name / filepath / fps / frames / contexts。
        """
        try:
            hip_file = hou.hipFile.name()
            # PR 5: 先拿 _scene.get_scene_info 提供的版本 / 节点数 / file_path
            scene_meta = scn.get_scene_info(hou)
            scene_info = {
                "name": os.path.basename(hip_file) if hip_file else "Untitled",
                "filepath": hip_file or "",
                "houdini_version": scene_meta.get("houdini_version", ""),
                "node_count": scene_meta.get("node_count", 0),
                "file_path": scene_meta.get("file_path", ""),
                "fps": scene_meta.get("fps", hou.fps()),
                "start_frame": scene_meta.get("start_frame", hou.playbar.frameRange()[0]),
                "end_frame": scene_meta.get("end_frame", hou.playbar.frameRange()[1]),
                "contexts": {},
            }

            # Collect per-context node summaries (avoids expensive allSubChildren traversal)
            root = hou.node("/")
            contexts = ["obj", "shop", "out", "ch", "vex", "stage"]

            for ctx_name in contexts:
                ctx_node = root.node(ctx_name)
                if ctx_node:
                    children = ctx_node.children()
                    scene_info["contexts"][ctx_name] = {
                        "count": len(children),
                        "nodes": [
                            {
                                "name": node.name(),
                                "path": node.path(),
                                "type": node.type().name(),
                            }
                            for node in children[:20]
                        ],
                    }

            return scene_info

        except Exception as e:
            traceback.print_exc()
            return {"error": str(e)}

    def save_scene(self, file_path):
        """PR 5: 保存当前 .hip 文件到 file_path。thin wrapper around scn.save_scene."""
        return scn.save_scene(hou, file_path)

    def load_scene(self, file_path):
        """PR 5: 加载 file_path 为当前 .hip 文件；自动调用 cmn.invalidate_all_caches()。

        不在 MUTATING_COMMANDS 内（由 _scene 层负责 cache 失效）。
        """
        return scn.load_scene(hou, file_path)

    def new_scene(self):
        """PR 5: 新建空白场景（suppress_save_prompt=True）；自动调用 invalidate_all_caches()。"""
        return scn.new_scene(hou)

    def serialize_scene(self, root_path="/obj", include_params=False, max_depth=10):
        """PR 5: 递归序列化 root_path 下的节点树为 dict。

        thin wrapper to scn.serialize_scene（spec `Scenario: serialize_scene`）。
        不在 MUTATING_COMMANDS 内（只读，AI 用于场景结构对比 / 文档生成）。
        """
        return scn.serialize_scene(hou, root_path=root_path,
                                   include_params=include_params,
                                   max_depth=max_depth)

    def list_node_types(self, category=None, name_filter=None, limit=50, cursor=None):
        """PR 6: 列出 Houdini 节点类型（paginated）。thin wrapper to disc.list_node_types."""
        return disc.list_node_types(hou, category=category, name_filter=name_filter,
                                    limit=limit, cursor=cursor)

    def list_children(self, node_path="/", recursive=False, max_depth=5,
                      max_nodes=1000, compact=False, limit=50, cursor=None):
        """PR 6: 列出 node_path 的子节点。thin wrapper to disc.list_children."""
        return disc.list_children(hou, node_path=node_path, recursive=recursive,
                                  max_depth=max_depth, max_nodes=max_nodes,
                                  compact=compact, limit=limit, cursor=cursor)

    def find_nodes(self, root_path="/", pattern=None, node_type=None,
                   limit=50, cursor=None):
        """PR 6: 在 root_path 下用 pattern / node_type 过滤查找。thin wrapper to disc.find_nodes."""
        return disc.find_nodes(hou, root_path=root_path, pattern=pattern,
                               node_type=node_type, limit=limit, cursor=cursor)

    def manage_cache(self, action="stats"):
        """PR 6: cache 管理（stats / invalidate / warmup）。thin wrapper to disc.manage_cache."""
        return disc.manage_cache(hou, action=action)

    def create_material(self, material_type, name=None, parent_path="/mat",
                        parameters=None):
        """PR 7: 创建材质节点。thin wrapper to mats.create_material."""
        return mats.create_material(hou, material_type, name=name,
                                    parent_path=parent_path,
                                    parameters=parameters)

    def assign_material(self, geometry_path, material_path, group=None):
        """PR 7: 把材质绑定到几何节点。thin wrapper to mats.assign_material."""
        return mats.assign_material(hou, geometry_path, material_path,
                                    group=group)

    def get_material_info(self, material_path):
        """PR 7: 查询材质节点详细参数 + texture 引用列表。
        thin wrapper to mats.get_material_info."""
        return mats.get_material_info(hou, material_path)

    def execute_hscript(self, code):
        """PR 8: 在 Houdini 端执行 HScript 命令字符串。thin wrapper to hsc.execute_hscript.

        HScript 是 Houdini 传统脚本语言，可能修改场景（与 execute_code 同级别
        风险），但用户已显式调用 HScript，风险自担，因此不在
        MUTATING_COMMANDS 集合内。
        """
        return hsc.execute_hscript(hou, code)

    def create_node(self, node_type, parent_path="/obj", name=None, position=None, parameters=None):
        """Creates a new node in the specified parent."""
        try:
            parent = hou.node(parent_path)
            if not parent:
                raise ValueError(f"Parent path not found: {parent_path}")
            
            node = parent.createNode(node_type, node_name=name)
            if position and len(position) >= 2:
                node.setPosition([position[0], position[1]])
            if parameters:
                for p_name, p_val in parameters.items():
                    parm = node.parm(p_name)
                    if parm:
                        parm.set(p_val)
            
            return {
                "name": node.name(),
                "path": node.path(),
                "type": node.type().name(),
                "position": list(node.position()),
            }
        except Exception as e:
            raise Exception(f"Failed to create node: {str(e)}")

    def modify_node(self, path, parameters=None, position=None, name=None):
        """Modifies an existing node."""
        node = hou.node(path)
        if not node:
            raise ValueError(f"Node not found: {path}")
        
        changes = []
        old_name = node.name()
        
        if name and name != old_name:
            node.setName(name)
            changes.append(f"Renamed from {old_name} to {name}")
        
        if position and len(position) >= 2:
            node.setPosition([position[0], position[1]])
            changes.append(f"Position set to {position}")
        
        if parameters:
            for p_name, p_val in parameters.items():
                parm = node.parm(p_name)
                if parm:
                    old_val = parm.eval()
                    parm.set(p_val)
                    changes.append(f"Parameter {p_name} changed from {old_val} to {p_val}")
        
        return {"path": node.path(), "changes": changes}

    def delete_node(self, path):
        """Deletes a node from the scene."""
        node = hou.node(path)
        if not node:
            raise ValueError(f"Node not found: {path}")
        node_path = node.path()
        node_name = node.name()
        node.destroy()
        return {"deleted": node_path, "name": node_name}

    def get_node_info(self, node_path=None, path=None, include_errors=True,
                      force_cook=False, include_input_details=False,
                      compact=False):
        """PR 10 重写：委托到 _node_info.get_node_info，新增 include_errors /
        force_cook / include_input_details / compact 四个参数。

        后向兼容：
        - 旧调用 `get_node_info(path=...)` 仍 work（path 关键字回退为 node_path）。
        - 仅传 1 个位置参数（path / node_path）也兼容。
        """
        if node_path is None and path is not None:
            node_path = path
        return ni.get_node_info(hou, node_path, include_errors, force_cook,
                                include_input_details, compact)

    def execute_code(self, code, policy="normal", allow_dangerous=False,
                     allow_heavy_geometry=False, capture_diff=False, timeout=30):
        """Execute arbitrary Python code within Houdini with PR 4 safety layer.

        新签名（向后兼容：仅传 code 时等价于 policy=normal / 全 bypass 关闭 /
        不 capture diff / timeout=30s）。流程：
        1) 规范化 policy 2) 读 bypass config 3) policy 决策（hit 即返 blocked）
        4) capture_diff 时先 serialize 5) _run_code_thread 执行 6) capture_diff
        时再 serialize 7) _build_audit 组装审计块。
        """
        # Step 1+2: validate policy & bypass config
        try:
            norm_policy = cmn.validate_policy(policy)
        except ValueError as e:
            return {
                "executed": False,
                "blocked": True,
                "reason": str(e),
                "_audit": cmn._build_audit(
                    policy=str(policy),
                    bypass_used=False,
                    dangerous_hits=[],
                    heavy_hits=[],
                    mutation_hits=[],
                    elapsed_ms=0,
                    undo_group=None,
                    exception_type="ValueError",
                    exception_message=str(e),
                ),
            }
        bypass_enabled = cmn._bypass_config_enabled()

        # Step 3: policy decision
        decision = cmn.check_execute_code_policy(
            code, norm_policy, allow_dangerous, allow_heavy_geometry,
            bypass_enabled,
        )
        if not decision["allowed"]:
            # 不进入 thread，返 blocked dict
            return {
                "executed": False,
                "blocked": True,
                "reason": decision["reason"],
                "hits": decision["hits"],
                "_audit": cmn._build_audit(
                    policy=norm_policy,
                    bypass_used=False,
                    dangerous_hits=decision["hits"]["dangerous"],
                    heavy_hits=decision["hits"]["heavy"],
                    mutation_hits=decision["hits"]["mutation"],
                    elapsed_ms=0,
                    undo_group=None,
                ),
            }

        # Step 4: undo group name（保持向后兼容：execute_code 不属于 MUTATING_COMMANDS
        # 的硬编码集合，但 policy==privileged 时仍尝试 undo 包一层以便 agent 撤销）
        undo_group_name = None
        if norm_policy == "privileged" and hasattr(hou, "undos"):
            undo_group_name = "MCP: execute_code (privileged)"

        # Step 5: capture diff before
        global _before_scene, _after_scene
        if capture_diff:
            try:
                _before_scene = cmn.serialize_scene_state(hou)
            except Exception as e:
                # serialize 失败不阻断执行；audit 记录
                _before_scene = {"error": "before-snapshot failed: {0}".format(e)}
        else:
            _before_scene = None

        # Step 6: namespace + thread-exec
        namespace = {"hou": hou}
        # 把 undo 包成 context manager（如果可用）
        if undo_group_name and hasattr(hou, "undos") and hasattr(hou.undos, "group"):
            with hou.undos.group(undo_group_name):
                run_result = cmn._run_code_thread(code, namespace, timeout=timeout)
        else:
            run_result = cmn._run_code_thread(code, namespace, timeout=timeout)

        # Step 7: capture diff after
        if capture_diff:
            try:
                _after_scene = cmn.serialize_scene_state(hou)
            except Exception as e:
                _after_scene = {"error": "after-snapshot failed: {0}".format(e)}
        else:
            _after_scene = None

        # Step 8: 截断输出
        max_size = 16 * 1024
        stdout, stdout_truncated = cmn._truncate_output(
            run_result.get("stdout", ""), max_size
        )
        stderr, stderr_truncated = cmn._truncate_output(
            run_result.get("stderr", ""), max_size
        )

        # 异常时仍要打 traceback 到 host stderr（沿用 PR 3 之前行为）
        if run_result.get("exception_type") and not run_result.get("timed_out"):
            try:
                print("--- Houdini MCP: execute_code Error ---", file=sys.stderr)
                traceback.print_exc(file=sys.stderr)
                print("--- End Error ---", file=sys.stderr)
            except Exception:
                pass

        # Step 9: 组装 audit + 返回
        audit = cmn._build_audit(
            policy=norm_policy,
            bypass_used=(norm_policy == "privileged" and bypass_enabled),
            dangerous_hits=decision["hits"]["dangerous"],
            heavy_hits=decision["hits"]["heavy"],
            mutation_hits=decision["hits"]["mutation"],
            elapsed_ms=run_result.get("elapsed_ms", 0),
            undo_group=undo_group_name,
            exception_type=run_result.get("exception_type"),
            exception_message=run_result.get("exception_message"),
            timed_out=run_result.get("timed_out", False),
        )

        result = {
            "executed": True,
            "stdout": stdout,
            "stderr": stderr,
            "_audit": audit,
        }
        if stdout_truncated:
            result["stdout_truncated"] = True
        if stderr_truncated:
            result["stderr_truncated"] = True
        if run_result.get("exception_type"):
            # 保留 PR 3 之前向 host 抛异常的语义；通过 _audit.exception_* 已记录
            # 这里不再 raise，避免双重异常处理。bridge 端可读 _audit 字段判定。
            result["execution_error"] = run_result.get("exception_message", "")
        return result

    def get_last_scene_diff(self):
        """返回最近一次 execute_code(capture_diff=True) 的前后场景快照 diff。

        若从未以 capture_diff=True 执行过，返回 {"available": False, ...}。
        不修改场景（因此不在 MUTATING_COMMANDS 内）。
        """
        global _before_scene, _after_scene
        if _before_scene is None and _after_scene is None:
            return {
                "available": False,
                "message": "No scene diff captured yet. Run execute_code with capture_diff=True.",
            }
        changed = _before_scene != _after_scene
        return {
            "available": True,
            "changed": changed,
            "before": _before_scene,
            "after": _after_scene,
        }

    # -------------------------------------------------------------------------
    # Graph Editing & Introspection
    # -------------------------------------------------------------------------

    def _resolve_node(self, path):
        """Return the hou.Node at 'path' or raise a clear error."""
        node = hou.node(path)
        if not node:
            raise ValueError(f"Node not found: {path}")
        return node

    def _resolve_geometry_node(self, path):
        """
        Resolve 'path' to a SOP node that owns geometry. Accepts a SOP path
        directly, or a geometry container (OBJ node) whose display SOP is used.
        """
        node = self._resolve_node(path)
        if isinstance(node, hou.SopNode):
            return node
        display = getattr(node, "displayNode", lambda: None)()
        if display is not None:
            return display
        raise ValueError(
            f"{path} has no geometry. Pass a SOP path or a geometry container "
            f"(got {node.type().category().name()} node '{node.type().name()}')."
        )

    @staticmethod
    def _jsonable(value):
        """Convert HOM values (vectors, tuples, ...) to JSON-friendly types."""
        if isinstance(value, (bool, int, float, str)) or value is None:
            return value
        if isinstance(value, (hou.Vector2, hou.Vector3, hou.Vector4, hou.Quaternion)):
            return list(value)
        if isinstance(value, (tuple, list)):
            return [HoudiniMCPServer._jsonable(v) for v in value]
        return str(value)

    @staticmethod
    def _parm_value(parm_tuple):
        """Evaluate a parm tuple; single-component parms come back as scalars."""
        value = HoudiniMCPServer._jsonable(parm_tuple.eval())
        if isinstance(value, list) and len(parm_tuple) == 1:
            return value[0]
        return value

    def _cook_and_report(self, node):
        """Force-cook a node and return a structured pass/fail report."""
        start = time.time()
        cook_exception = None
        try:
            node.cook(force=True)
        except hou.OperationFailed as e:
            cook_exception = str(e)
        elapsed_ms = round((time.time() - start) * 1000.0, 1)

        errors = [e.strip() for e in node.errors() if e.strip()]
        warnings = [w.strip() for w in node.warnings() if w.strip()]
        if cook_exception and not errors:
            errors.append(cook_exception)

        return {
            "node": node.path(),
            "cooked": not errors,
            "cook_time_ms": elapsed_ms,
            "errors": errors,
            "warnings": warnings,
        }

    def connect_nodes(self, from_path, to_path, input_index=0, output_index=0):
        """Wire from_path's output into to_path's input."""
        src = self._resolve_node(from_path)
        dst = self._resolve_node(to_path)
        # B2 fix (H21 compat): OBJ-display cross-parent wiring (SOP
        # descendant → OBJ container). On H21, hou.ObjNode.setInput(
        # 0, sop_descendant) HANGS 30s+ (live-verified via
        # execute_houdini_code at 30002ms timeout). H21 OBJ containers
        # do not accept setInput for display wiring; the H21-correct
        # path is to set the SOP's display + render flag so Houdini
        # picks it up as the OBJ container's display SOP (pattern
        # documented in this fork's own _synthesize_ai_hint at lines
        # ~99-105). Pure cross-network SOP/SOP or OBJ/OBJ pairings
        # still fall through to the same-parent raise below.
        src_path = src.path() or ""
        dst_path = dst.path() or ""
        if src_path.startswith(dst_path + "/"):
            # SOP→OBJ: src has setDisplayFlag (hou.SopNode-only API).
            # Detect via callable getattr to avoid catching class-level
            # attributes on non-SOP nodes.
            if callable(getattr(src, "setDisplayFlag", None)):
                src.setDisplayFlag(True)
                # setRenderFlag is also SopNode-only; guard older Houdini
                # / non-SOP edge cases defensively.
                if callable(getattr(src, "setRenderFlag", None)):
                    src.setRenderFlag(True)
                return {
                    "from": src_path,
                    "to": dst_path,
                    "input_index": input_index,
                    "output_index": output_index,
                    "via": "sop_display_flag",
                }
            # Non-SOP descendant (rare; e.g. OBJ→OBJ container nesting):
            # fall back to the legacy cross-parent setInput. The H21 hang
            # only affects the SOP→OBJ case handled above.
            dst.setInput(input_index, src)
            return {
                "from": src_path,
                "to": dst_path,
                "input_index": input_index,
                "output_index": output_index,
                "_cross_parent": True,
            }
        if src.parent() != dst.parent():
            raise ValueError(
                f"Nodes must share a parent network: {src.parent().path()} != {dst.parent().path()}"
            )
        dst.setInput(input_index, src, output_index)
        return {
            "from": src.path(),
            "to": dst.path(),
            "input_index": input_index,
            "output_index": output_index,
        }

    def disconnect_input(self, path, input_index=0):
        """Disconnect one input of a node."""
        node = self._resolve_node(path)
        previous = None
        for connection in node.inputConnections():
            if connection.inputIndex() == input_index:
                previous = connection.inputNode()
                break
        node.setInput(input_index, None)
        return {
            "node": node.path(),
            "input_index": input_index,
            "was_connected_to": previous.path() if previous else None,
        }

    def _set_one_parm(self, node, name, value):
        """
        Set a single parameter (or parm tuple). Returns (previous, new).
        Resolves menu tokens/labels for string values on menu parms, and
        suggests close parameter names when the name doesn't exist.
        """
        parm_tuple = node.parmTuple(name)
        if parm_tuple is None:
            candidates = [pt.name() for pt in node.parmTuples()]
            close = difflib.get_close_matches(name, candidates, n=3, cutoff=0.5)
            hint = f" Did you mean: {', '.join(close)}?" if close else ""
            raise ValueError(f"Parameter '{name}' not found on {node.path()}.{hint}")

        previous = self._parm_value(parm_tuple)

        if isinstance(value, (list, tuple)):
            if len(value) != len(parm_tuple):
                raise ValueError(
                    f"'{name}' has {len(parm_tuple)} component(s), got {len(value)} values"
                )
            parm_tuple.set(tuple(value))
        else:
            if len(parm_tuple) != 1:
                raise ValueError(
                    f"'{name}' has {len(parm_tuple)} components; pass a list of {len(parm_tuple)} values"
                )
            parm = parm_tuple[0]
            try:
                parm.set(value)
            except (TypeError, hou.OperationFailed):
                # A string that isn't a valid menu token: resolve label to index.
                if not isinstance(value, str):
                    raise
                try:
                    tokens = list(parm.menuItems())
                    labels = list(parm.menuLabels())
                except hou.OperationFailed:
                    raise TypeError(
                        f"'{name}' does not accept a string value on {node.path()}"
                    )
                if value in tokens:
                    parm.set(tokens.index(value))
                elif value in labels:
                    parm.set(labels.index(value))
                else:
                    raise ValueError(
                        f"'{value}' is not a menu token or label of '{name}'. "
                        f"Tokens: {tokens[:20]}"
                    )

        return previous, self._parm_value(parm_tuple)

    def set_parameters(self, path, parameters):
        """
        Set multiple parameters on a node in one call.
        Values: scalar for single parms, list for tuples (e.g. "t": [0, 1, 0]),
        menu token/label strings for menu parms.
        """
        node = self._resolve_node(path)
        if not isinstance(parameters, dict) or not parameters:
            raise ValueError("'parameters' must be a non-empty dict of {name: value}")

        applied, failed = [], []
        for name, value in parameters.items():
            try:
                previous, new = self._set_one_parm(node, name, value)
                applied.append({"name": name, "previous": previous, "value": new})
            except Exception as e:
                failed.append({"name": name, "error": str(e)})

        return {"node": node.path(), "set": applied, "failed": failed}

    def get_parameter_schema(self, path, pattern=None, offset=0, limit=50):
        """
        Describe a node's parameters: name, label, type, size, current value,
        defaults, ranges and menu options. Filter with a glob 'pattern'
        (matched against name and label), paginate with offset/limit.
        """
        node = self._resolve_node(path)
        limit = max(1, min(int(limit), 200))
        offset = max(0, int(offset))

        parm_tuples = node.parmTuples()
        if pattern:
            pat = pattern.lower()
            parm_tuples = [
                pt for pt in parm_tuples
                if fnmatch.fnmatch(pt.name().lower(), pat)
                or fnmatch.fnmatch(pt.parmTemplate().label().lower(), pat)
            ]

        entries = []
        for pt in parm_tuples[offset:offset + limit]:
            template = pt.parmTemplate()
            entry = {
                "name": pt.name(),
                "label": template.label(),
                "type": template.type().name(),
                "size": len(pt),
                "value": self._parm_value(pt),
            }
            try:
                default = self._jsonable(template.defaultValue())
                if isinstance(default, list) and len(default) == 1:
                    default = default[0]
                entry["default"] = default
            except AttributeError:
                pass
            if isinstance(template, (hou.FloatParmTemplate, hou.IntParmTemplate)):
                entry["min"] = template.minValue()
                entry["max"] = template.maxValue()
            menu_items = getattr(template, "menuItems", lambda: ())()
            if menu_items:
                menu_labels = template.menuLabels()
                entry["menu"] = [
                    {"token": t, "label": l}
                    for t, l in islice(zip(menu_items, menu_labels), 30)
                ]
                if len(menu_items) > 30:
                    entry["menu_truncated"] = len(menu_items)
            entries.append(entry)

        return {
            "node": node.path(),
            "node_type": node.type().name(),
            "total": len(parm_tuples),
            "offset": offset,
            "parameters": entries,
        }

    def set_node_flags(self, path, display=None, render=None, bypass=None, template=None):
        """Set node flags; only the flags passed (non-None) are touched."""
        node = self._resolve_node(path)
        requested = {
            "display": (display, "setDisplayFlag"),
            "render": (render, "setRenderFlag"),
            "bypass": (bypass, "bypass"),
            "template": (template, "setTemplateFlag"),
        }
        applied, unsupported = {}, []
        for flag, (value, method_name) in requested.items():
            if value is None:
                continue
            method = getattr(node, method_name, None)
            if method is None:
                unsupported.append(flag)
                continue
            method(bool(value))
            applied[flag] = bool(value)

        return {"node": node.path(), "applied": applied, "unsupported": unsupported}

    def layout_children(self, path, horizontal_spacing=2.0,
                        vertical_spacing=1.5, direction="horizontal"):
        """PR 9 重写：按 horizontal_spacing / vertical_spacing / direction
        显式布局子节点。后向兼容：现有调用 layout_children(path) 用默认值。
        """
        return ge.layout_children(hou, path, horizontal_spacing,
                                  vertical_spacing, direction)

    def reorder_inputs(self, node_path, new_order):
        """PR 9：先全部断开当前输入，再按 new_order 重新连接。
        后向兼容：bridge 端如果收到 'order' 别名，会在传入前归一为 new_order。
        """
        return ge.reorder_inputs(hou, node_path, new_order)

    def set_node_position(self, node_path, x, y):
        """PR 9：设置节点在 network editor 中的位置。"""
        return ge.set_node_position(hou, node_path, x, y)

    def set_node_color(self, node_path, r, g, b):
        """PR 9：设置节点颜色（自动 clamp 到 [0, 1]）。"""
        return ge.set_node_color(hou, node_path, r, g, b)

    def create_network_box(self, parent_path, name=None, node_paths=None):
        """PR 9：在父节点下创建 network box，可选包含若干节点；缺失节点跳过。"""
        return ge.create_network_box(hou, parent_path, name, node_paths)

    def find_error_nodes(self, root_path="/", include_warnings=True,
                         max_warnings=50, max_errors=None):
        """PR 11：扫描场景中的错误与警告节点。

        薄封装到 _error_nodes.find_error_nodes，使用 node.allSubChildren()
        单次扫描（非递归），并返回 errors / warnings 双列表。
        """
        return en.find_error_nodes(
            hou, root_path=root_path, include_warnings=include_warnings,
            max_warnings=max_warnings, max_errors=max_errors)

    def get_geo_summary(self, node_path, max_points_for_full=1000000,
                        sample_size=10):
        """PR 12：获取几何节点的轻量级概要信息。

        薄封装到 _geo_summary.get_geo_summary，复用 server.py 的
        _resolve_geometry_node 解析 OBJ/SOP 路径。point_count 超过
        max_points_for_full 时自动降级（跳过 sample_points 与详细
        attributes/groups）。
        """
        return gs.get_geo_summary(
            hou, node_path=node_path,
            max_points_for_full=max_points_for_full,
            sample_size=sample_size)

    # -------------------------------------------------------------------------
    # PR 13: Pane Capture (thin wrappers to _pane_capture + apply_response_cap)
    # -------------------------------------------------------------------------
    def capture_pane_screenshot(self, pane_type_name, save_path=None,
                                fit_contents=True):
        """PR 13：截图指定类型 pane（NetworkEditor / SceneViewer / 等）。

        薄封装到 _pane_capture.capture_pane_screenshot，响应过
        cmn.apply_response_cap。无 PySide 环境返回 _warning dict 而非抛异常。
        """
        result = pcp.capture_pane_screenshot(
            hou, pane_type_name, save_path=save_path,
            fit_contents=fit_contents)
        return cmn.apply_response_cap(result)

    def capture_sceneviewer_flipbook_views(self, views=None, save_dir=None,
                                           desktop_name=None, pane_name=None,
                                           fit_contents=True):
        """采集确定 SceneViewer 的多视图 Houdini 内部 flipbook PNG。"""
        result = pcp.capture_sceneviewer_flipbook_views(
            hou, views=views, save_dir=save_dir,
            desktop_name=desktop_name, pane_name=pane_name,
            fit_contents=fit_contents)
        return cmn.apply_response_cap(result)

    def list_visible_panes(self):
        """PR 13：列出当前所有 desktop 中可见的 pane。

        只读操作，不在 MUTATING_COMMANDS 内。响应过 apply_response_cap 防止
        多 desktop 大场景撑爆 MCP。
        """
        result = {"panes": pcp.list_visible_panes(hou)}
        return cmn.apply_response_cap(result)

    def capture_multiple_panes(self, pane_types, save_dir):
        """PR 13：批量截图多种 pane 到 save_dir（自动创建目录）。

        薄封装到 _pane_capture.capture_multiple_panes，响应过
        apply_response_cap。每种 pane 独立报告 success/error。
        """
        result = pcp.capture_multiple_panes(hou, pane_types, save_dir)
        if not (isinstance(result, dict)
                and result.get("status") == "warning"):
            result = {"results": result}
        return cmn.apply_response_cap(result)

    def render_node_network(self, node_path, fit_contents=True,
                            save_path=None):
        """PR 13：定位到节点所在 NetworkEditor pane，cd 到节点，再截图。

        薄封装到 _pane_capture.render_node_network，响应过 apply_response_cap。
        """
        result = pcp.render_node_network(
            hou, node_path, fit_contents=fit_contents,
            save_path=save_path)
        return cmn.apply_response_cap(result)

    def cook_node(self, path):
        """Force-cook a node and report errors, warnings and cook time."""
        return self._cook_and_report(self._resolve_node(path))

    # -------------------------------------------------------------------------
    # VEX Wrangles
    # -------------------------------------------------------------------------

    def _set_run_over(self, node, run_over):
        """Match 'run_over' against the wrangle's class menu (token or label)."""
        class_parm = node.parm("class")
        if class_parm is None:
            return None  # e.g. volumewrangle has no class parm
        want = run_over.lower().rstrip("s")
        tokens = list(class_parm.menuItems())
        labels = list(class_parm.menuLabels())
        for index, (token, label) in enumerate(zip(tokens, labels)):
            if want in (token.lower().rstrip("s"), label.lower().rstrip("s")):
                class_parm.set(index)
                return token
        raise ValueError(
            f"Unknown run_over '{run_over}'. Valid options: {tokens}"
        )

    def create_wrangle(self, parent_path, vex_code, name=None, run_over="points",
                       input_node=None, wrangle_type="attribwrangle"):
        """
        Create a wrangle SOP, set its VEX snippet, optionally wire an input,
        then cook it so VEX compile errors are reported immediately.
        """
        parent = self._resolve_node(parent_path)
        if parent.childTypeCategory() != hou.sopNodeTypeCategory():
            raise ValueError(
                f"{parent_path} is not a SOP network (cannot contain wrangles). "
                f"Pass a geometry container or SOP subnet."
            )

        node = parent.createNode(wrangle_type, node_name=name)
        try:
            snippet = node.parm("snippet")
            if snippet is None:
                raise ValueError(f"'{wrangle_type}' has no 'snippet' parameter")
            snippet.set(vex_code)
            run_over_token = self._set_run_over(node, run_over)
            if input_node:
                node.setInput(0, self._resolve_node(input_node))
            node.moveToGoodPosition()
        except Exception:
            node.destroy()  # don't leave a half-configured node behind
            raise

        return {
            "path": node.path(),
            "type": wrangle_type,
            "run_over": run_over_token,
            "validation": self._cook_and_report(node),
        }

    def set_wrangle_code(self, path, vex_code, validate=True):
        """Replace the VEX snippet on an existing wrangle and re-validate."""
        node = self._resolve_node(path)
        snippet = node.parm("snippet")
        if snippet is None:
            raise ValueError(f"{path} has no 'snippet' parameter (not a wrangle)")
        snippet.set(vex_code)
        result = {"path": node.path(), "code_length": len(vex_code)}
        if validate:
            result["validation"] = self._cook_and_report(node)
        return result

    # -------------------------------------------------------------------------
    # Geometry Introspection
    # -------------------------------------------------------------------------

    @staticmethod
    def _attrib_summary(attribs):
        return [
            {"name": a.name(), "type": a.dataType().name(), "size": a.size()}
            for a in attribs
        ]

    def get_geometry_info(self, path):
        """
        Summarize a node's geometry: element counts, bounding box, attributes
        and group names. Accepts a SOP or a geometry container path.
        """
        sop = self._resolve_geometry_node(path)
        geo = sop.geometry()
        if geo is None:
            report = self._cook_and_report(sop)
            raise ValueError(
                f"{sop.path()} produced no geometry. Cook errors: {report['errors']}"
            )

        bbox = geo.boundingBox()
        return {
            "node": sop.path(),
            "point_count": geo.intrinsicValue("pointcount"),
            "primitive_count": geo.intrinsicValue("primitivecount"),
            "vertex_count": geo.intrinsicValue("vertexcount"),
            "bounding_box": {
                "min": list(bbox.minvec()),
                "max": list(bbox.maxvec()),
                "size": list(bbox.sizevec()),
                "center": list(bbox.center()),
            },
            "attributes": {
                "point": self._attrib_summary(geo.pointAttribs()),
                "primitive": self._attrib_summary(geo.primAttribs()),
                "vertex": self._attrib_summary(geo.vertexAttribs()),
                "detail": self._attrib_summary(geo.globalAttribs()),
            },
            "groups": {
                "point": [g.name() for g in geo.pointGroups()],
                "primitive": [g.name() for g in geo.primGroups()],
            },
        }

    def get_geometry_data(self, path, element="points", attributes=None,
                          start=0, limit=100):
        """
        Read actual attribute values from geometry, paginated.
        element: 'points' or 'primitives'. attributes: list of names
        (default: position for points, type info for prims).
        """
        sop = self._resolve_geometry_node(path)
        geo = sop.geometry()
        if geo is None:
            raise ValueError(f"{sop.path()} has no geometry (node may not cook)")

        start = max(0, int(start))
        limit = max(1, min(int(limit), 500))

        if element == "points":
            total = geo.intrinsicValue("pointcount")
            available = {a.name(): a for a in geo.pointAttribs()}
            iterator = geo.iterPoints()
        elif element == "primitives":
            total = geo.intrinsicValue("primitivecount")
            available = {a.name(): a for a in geo.primAttribs()}
            iterator = geo.iterPrims()
        else:
            raise ValueError(f"element must be 'points' or 'primitives', got '{element}'")

        if attributes:
            missing = [a for a in attributes if a not in available]
            if missing:
                raise ValueError(
                    f"Attribute(s) {missing} not found on {element}. "
                    f"Available: {sorted(available)}"
                )
            selected = [available[a] for a in attributes]
        else:
            selected = [available["P"]] if "P" in available else []

        rows = []
        for elem in islice(iterator, start, start + limit):
            row = {"number": elem.number()}
            if element == "primitives":
                row["type"] = elem.type().name()
            for attrib in selected:
                row[attrib.name()] = self._jsonable(elem.attribValue(attrib))
            rows.append(row)

        return {
            "node": sop.path(),
            "element": element,
            "total": total,
            "start": start,
            "count": len(rows),
            "data": rows,
        }

    # -------------------------------------------------------------------------
    # set_material (now completed)
    # -------------------------------------------------------------------------
    def set_material(self, node_path, material_type="principledshader", name=None, parameters=None):
        """
        Creates or applies a material to an OBJ node. 
        For example, we can create a Principled Shader in /mat 
        and assign it to a geometry node or set the 'shop_materialpath'.
        """
        try:
            target_node = hou.node(node_path)
            if not target_node:
                raise ValueError(f"Node not found: {node_path}")
            
            # Verify it's an OBJ node (i.e., category Object)
            if target_node.type().category().name() != "Object":
                raise ValueError(
                    f"Node {node_path} is not an OBJ-level node and cannot accept direct materials."
                )

            # Attempt to create/find a material in /mat (or /shop)
            mat_context = hou.node("/mat")
            if not mat_context:
                # Fallback: try /shop if /mat doesn't exist
                mat_context = hou.node("/shop")
                if not mat_context:
                    raise RuntimeError("No /mat or /shop context found to create materials.")

            mat_name = name or (f"{material_type}_auto")
            mat_node = mat_context.node(mat_name)
            if not mat_node:
                # Create a new material node
                mat_node = mat_context.createNode(material_type, mat_name)

            # Apply any parameter overrides
            if parameters:
                for k, v in parameters.items():
                    p = mat_node.parm(k)
                    if p:
                        p.set(v)

            # Now assign this material to the OBJ node
            # Typically, you either set a "shop_materialpath" parameter 
            # or inside the geometry, you create a Material SOP.
            mat_parm = target_node.parm("shop_materialpath")
            if mat_parm:
                mat_parm.set(mat_node.path())
            else:
                # If there's a geometry node inside, we might make or update a Material SOP
                geo_sop = target_node.node("geometry")
                if not geo_sop:
                    raise RuntimeError("No 'geometry' node found inside OBJ to apply material to.")
                
                material_sop = geo_sop.node("material1")
                if not material_sop:
                    material_sop = geo_sop.createNode("material", "material1")
                    # Hook it up to the chain
                    # For a brand-new geometry node, there's often a 'file1' SOP or similar
                    first_sop = None
                    for c in geo_sop.children():
                        if c.isDisplayFlagSet():
                            first_sop = c
                            break
                    if first_sop:
                        material_sop.setFirstInput(first_sop)
                    material_sop.setDisplayFlag(True)
                    material_sop.setRenderFlag(True)

                # The Material SOP typically has shop_materialpath1, shop_materialpath2, etc.
                mat_sop_parm = material_sop.parm("shop_materialpath1")
                if mat_sop_parm:
                    mat_sop_parm.set(mat_node.path())
                else:
                    raise RuntimeError(
                        "No shop_materialpath1 on Material SOP to assign the material."
                    )

            return {
                "status": "ok",
                "material_node": mat_node.path(),
                "applied_to": target_node.path(),
            }

        except Exception as e:
            traceback.print_exc()
            return {"status": "error", "message": str(e), "node": node_path}

    # -------------------------------------------------------------------------
    # NEW OPUS Import Handler and Helpers
    # -------------------------------------------------------------------------
    
    def _download_file(self, url, dest_folder):
        """
        Download from 'url' to local 'dest_folder', returning local filepath.
        Helper for import_opus_url.
        """
        if not url:
            raise ValueError("Download URL cannot be empty.")
        if not os.path.exists(dest_folder):
            os.makedirs(dest_folder, exist_ok=True)
    
        # Generate filename, ensure it ends with .zip if possible
        try:
            path_part = urlparse(url).path
            filename = os.path.basename(path_part) if path_part else f"{uuid.uuid4()}.zip"
            if not filename.lower().endswith('.zip'):
                filename += ".zip"
        except Exception:
             filename = f"{uuid.uuid4()}.zip" # Fallback
             
        local_path = os.path.join(dest_folder, filename)
        # Ensure forward slashes
        local_path = local_path.replace('\\', '/')
        print(f"  Downloading {url} => {local_path}")
    
        try:
            # Use requests (already imported) for downloading
            resp = requests.get(url, stream=True, timeout=60) # Add timeout
            resp.raise_for_status()
            with open(local_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
            print(f"  Download complete: {local_path}")
            return local_path
        except requests.exceptions.RequestException as e:
             print(f"  Download failed: {str(e)}")
             # Clean up potentially incomplete file
             if os.path.exists(local_path):
                  try: os.remove(local_path)
                  except: pass
             raise ConnectionError(f"Failed to download file: {str(e)}") from e

    def _unzip_file(self, zip_path, dest_folder):
        """
        Unzip 'zip_path' into 'dest_folder'. Return list of extracted file paths.
        Helper for import_opus_url.
        
        Validates each entry to prevent ZipSlip (path traversal) attacks.
        """
        extracted_files = []
        dest_folder = os.path.realpath(dest_folder)
        print(f"  Unzipping {zip_path} => {dest_folder}")
        try:
            with zipfile.ZipFile(zip_path, 'r') as z:
                for info in z.infolist():
                    extracted_path = os.path.realpath(os.path.join(dest_folder, info.filename))
                    if not extracted_path.startswith(dest_folder + os.sep) and extracted_path != dest_folder:
                        raise ValueError(f"ZipSlip detected: entry '{info.filename}' escapes destination folder")
                z.extractall(dest_folder)
                extracted_files = [os.path.join(dest_folder, p).replace('\\', '/') for p in z.namelist()]
            print(f"  Unzip complete. Extracted {len(extracted_files)} files.")
            return extracted_files
        except zipfile.BadZipFile as e:
             print(f"  Unzip failed: Bad zip file - {str(e)}")
             raise ValueError(f"Downloaded file is not a valid zip file: {str(e)}") from e
        except Exception as e:
             print(f"  Unzip failed: {str(e)}")
             raise IOError(f"Failed to unzip file: {str(e)}") from e

    def handle_import_opus_url(self, url, node_name="opus_import"):
        """
        Downloads a ZIP file from URL, unzips it, finds a USD file,
        and imports it into a new subnet in Houdini.
        """
        temp_dir = None
        zip_filepath = None
        try:
            # Create a unique temporary directory for download and extraction
            temp_dir = tempfile.mkdtemp(prefix="houdini_opus_import_")
            print(f"Created temporary directory: {temp_dir}")

            # Download the zip file
            zip_filepath = self._download_file(url, temp_dir)
            if not zip_filepath or not os.path.exists(zip_filepath):
                 raise FileNotFoundError("Download failed or file not found.")

            # Unzip the file
            extract_dir = os.path.join(temp_dir, "extracted")
            extracted_files = self._unzip_file(zip_filepath, extract_dir)
            if not extracted_files:
                 raise FileNotFoundError("Unzip failed or zip file was empty.")

            # Find the primary USD file (e.g., .usd, .usda, .usdc)
            # Also check for GLTF/GLB as the zip name was gltf.zip
            import_file = None
            possible_usd_extensions = (".usd", ".usda", ".usdc")
            possible_gltf_extensions = (".gltf", ".glb")
            
            # Prioritize USD files
            for f in extracted_files:
                if f.lower().endswith(possible_usd_extensions):
                    import_file = f
                    print(f"Found USD file: {import_file}")
                    break
            
            # If no USD found, check for GLTF/GLB
            if not import_file:
                for f in extracted_files:
                     if f.lower().endswith(possible_gltf_extensions):
                        import_file = f
                        print(f"Found GLTF/GLB file: {import_file}")
                        break # Take the first match
            
            if not import_file:
                 raise FileNotFoundError(f"No USD ({possible_usd_extensions}) or GLTF/GLB ({possible_gltf_extensions}) file found in the extracted contents.")

            # --- Import into Houdini using gltf_hierarchy node directly in /obj ---
            obj_context = hou.node("/obj")
            if not obj_context:
                 raise RuntimeError("Cannot find /obj context in Houdini.")
            
            # Create a gltf_hierarchy node directly in /obj
            node_actual_name = node_name or "opus_import"
            gltf_node = obj_context.createNode("gltf_hierarchy", node_actual_name)
            if not gltf_node:
                 raise RuntimeError(f"Failed to create gltf_hierarchy node '{node_actual_name}' in /obj.")
            print(f"Created gltf_hierarchy node: {gltf_node.path()}")

            # Set the filename parameter
            print(f"Setting filename on {gltf_node.path()} to {import_file}")
            try:
                 # Parameter name might vary slightly, check common names
                 param_name = "filename"
                 if not gltf_node.parm(param_name):
                      param_name = "file"
                      if not gltf_node.parm(param_name):
                           raise RuntimeError(f"Could not find filename parameter ('filename' or 'file') on {gltf_node.path()}")
                           
                 gltf_node.parm(param_name).set(import_file)
                 print(f"Set parameter '{param_name}' successfully.")
            except hou.Error as parm_e:
                 print(f"Error setting filename parameter on gltf_hierarchy node: {parm_e}")
                 raise RuntimeError(f"Failed to set filename on gltf_hierarchy node: {parm_e}") from parm_e

            # Press the Build Scene button
            build_scene_parm = gltf_node.parm("buildscene")
            if build_scene_parm:
                 print(f"Pressing 'Build Scene' button on {gltf_node.path()}")
                 build_scene_parm.pressButton()
            else:
                 print(f"Warning: Could not find 'buildscene' parameter on {gltf_node.path()}. Scene might not be built automatically.")

            # Layout nodes in /obj (optional, might be useful)
            obj_context.layoutChildren()
            
            # Return the path to the gltf_hierarchy node
            return {"status": "success", "imported_node_path": gltf_node.path(), "imported_file": import_file}

        except Exception as e:
            error_message = f"OPUS Import Failed: {str(e)}"
            print(error_message)
            traceback.print_exc() # Print full traceback to Houdini console
            # Re-raise to be caught by execute_command and sent back as standard error
            raise Exception(error_message) from e

        finally:
            # --- Cleanup --- 
            # Only delete the downloaded zip file, keep the extracted contents
            # as the gltf_hierarchy SOP needs to reference them.
            if zip_filepath and os.path.exists(zip_filepath):
                try:
                    os.remove(zip_filepath)
                    print(f"Cleaned up temporary zip file: {zip_filepath}")
                except Exception as cleanup_zip_e:
                    print(f"Warning: Failed to clean up temporary zip file {zip_filepath}: {cleanup_zip_e}")
            
            # Keep the temp_dir itself and the extracted folder for now
            # If keeping the temp dir is problematic, we could copy the needed files elsewhere
            # before deleting the temp_dir.
            # if temp_dir and os.path.exists(temp_dir):
            #     try:
            #         shutil.rmtree(temp_dir)
            #         print(f"Cleaned up temporary directory: {temp_dir}")
            #     except Exception as cleanup_e:
            #         print(f"Warning: Failed to clean up temporary directory {temp_dir}: {cleanup_e}")

    # -------------------------------------------------------------------------
    # NEW Render Command Handlers (using HoudiniMCPRender.py)
    # -------------------------------------------------------------------------
    # def _check_render_lib(self):
    #     """Helper to check if the render library was imported."""
    #     if HMCPLib is None:
    #         raise RuntimeError("HoudiniMCPRender library not available. Cannot execute render commands.")

    def _process_rendered_image(self, filepath, camera_path=None, view_name=None):
        """
        Helper to validate and return metadata for a rendered image file.
        Returns the file path so the caller can open it directly — avoids
        base64-encoding large image data into the response.
        """
        if not filepath or not os.path.exists(filepath):
            return {"status": "error", "message": f"Rendered file not found: {filepath}", "origin": "_process_rendered_image"}

        # Determine format from extension
        _, ext = os.path.splitext(filepath)
        fmt = ext[1:].lower() if ext else 'unknown'

        # Get resolution from the camera if possible
        resolution = [0, 0]
        if camera_path:
            cam_node = hou.node(camera_path)
            if cam_node and cam_node.parm("resx") and cam_node.parm("resy"):
                resolution = [cam_node.parm("resx").eval(), cam_node.parm("resy").eval()]

        result_data = {
            "status": "success",
            "format": fmt,
            "resolution": resolution,
            "filepath": filepath,
        }
        if view_name:
            result_data["view_name"] = view_name

        return result_data

        # except Exception as e:
        #     error_message = f"Failed to process rendered image {filepath}: {str(e)}"
        #     print(error_message)
        #     traceback.print_exc()
        #     return {"status": "error", "message": error_message, "origin": "_process_rendered_image"}
        # finally:
        #     # Clean up the temporary file
        #     if os.path.exists(filepath):
        #         try:
        #             os.remove(filepath)
        #             print(f"Cleaned up temporary render file: {filepath}")
        #         except Exception as cleanup_e:
        #             print(f"Warning: Failed to clean up temporary render file {filepath}: {cleanup_e}")

    def handle_render_single_view(self, orthographic=False, rotation=(0, 90, 0), render_path=None, render_engine="opengl", karma_engine="cpu", consent_token=None):
        """Handles the 'render_single_view' command.

        fork-render-policy-redirect-and-consent: 入口先做 render policy
        校验，opengl 走 redirect dict，karma_* 需 consent_token 才放行。
        """
        # self._check_render_lib()

        policy_result = _evaluate_render_policy_command(
            "render_single_view", {
                "render_engine": render_engine,
                "karma_engine": karma_engine,
                "consent_token": consent_token,
            })
        if policy_result is not None:
            return policy_result

        # Use a temporary directory for the render output
        if not render_path:
            render_path = tempfile.gettempdir()

        try:
            # Ensure rotation is a tuple
            if isinstance(rotation, list): rotation = tuple(rotation)
            
            print(f"Calling HoudiniMCPRender.render_single_view with rotation={rotation}, ortho={orthographic}, engine={render_engine}...")
            filepath = render_single_view(
                orthographic=orthographic,
                rotation=rotation,
                render_path=render_path,
                render_engine=render_engine,
                karma_engine=karma_engine,
                consent_token=consent_token
            )
            print(f"render_single_view returned filepath: {filepath}")

            # Process the result
            # Determine camera path used (it's always /obj/MCP_CAMERA for this func)
            camera_path = "/obj/MCP_CAMERA"
            return self._process_rendered_image(filepath, camera_path)

        except Exception as e:
            error_message = f"Render Single View Failed: {str(e)}"
            print(error_message)
            traceback.print_exc()
            return {"status": "error", "message": error_message, "origin": "handle_render_single_view"}

    def handle_render_quad_view(self, orthographic=True, render_path=None, render_engine="opengl", karma_engine="cpu", consent_token=None):
        """Handles the 'render_quad_view' command.

        fork-render-policy-redirect-and-consent: 入口先做 render policy
        校验，opengl 走 redirect dict，karma_* 需 consent_token 才放行。
        """
        # self._check_render_lib()

        policy_result = _evaluate_render_policy_command(
            "render_quad_view", {
                "render_engine": render_engine,
                "karma_engine": karma_engine,
                "consent_token": consent_token,
            })
        if policy_result is not None:
            return policy_result

        if not render_path:
            render_path = tempfile.gettempdir()

        try:
            print(f"Calling HoudiniMCPRender.render_quad_view with ortho={orthographic}, engine={render_engine}...")
            filepaths = render_quad_view(
                orthographic=orthographic,
                render_path=render_path,
                render_engine=render_engine,
                karma_engine=karma_engine,
                consent_token=consent_token
            )
            print(f"render_quad_view returned filepaths: {filepaths}")

            # Process each resulting file
            results = []
            camera_path = "/obj/MCP_CAMERA" # Same camera is reused and modified
            for fp in filepaths:
                # Extract view name from filename if possible (e.g., MCP_OGL_RENDER_front_ortho.jpg -> front)
                view_name = None
                try:
                     filename = os.path.basename(fp)
                     parts = filename.split('_')
                     if len(parts) > 2: # Look for the part after engine/render type
                         view_name = parts[2] 
                except:
                     pass # Ignore errors extracting view name
                     
                results.append(self._process_rendered_image(fp, camera_path, view_name))
                
            # Return the list of results
            return {"status": "success", "results": results}

        except Exception as e:
            error_message = f"Render Quad View Failed: {str(e)}"
            print(error_message)
            traceback.print_exc()
            return {"status": "error", "message": error_message, "origin": "handle_render_quad_view"}

    def handle_render_specific_camera(self, camera_path, render_path=None, render_engine="opengl", karma_engine="cpu", consent_token=None):
        """Handles the 'render_specific_camera' command.

        fork-render-policy-redirect-and-consent: 入口先做 render policy
        校验，opengl 走 redirect dict，karma_* 需 consent_token 才放行。
        """
        # self._check_render_lib()

        policy_result = _evaluate_render_policy_command(
            "render_specific_camera", {
                "render_engine": render_engine,
                "karma_engine": karma_engine,
                "consent_token": consent_token,
            })
        if policy_result is not None:
            return policy_result

        if not render_path:
            render_path = tempfile.gettempdir()

        if not camera_path or not hou.node(camera_path):
             return {"status": "error", "message": f"Camera path '{camera_path}' is invalid or node not found.", "origin": "handle_render_specific_camera"}

        try:
            print(f"Calling HoudiniMCPRender.render_specific_camera for camera={camera_path}, engine={render_engine}...")
            filepath = render_specific_camera(
                camera_path=camera_path,
                render_path=render_path,
                render_engine=render_engine,
                karma_engine=karma_engine,
                consent_token=consent_token
            )
            print(f"render_specific_camera returned filepath: {filepath}")

            # Process the result, using the provided camera_path
            return self._process_rendered_image(filepath, camera_path)

        except Exception as e:
            error_message = f"Render Specific Camera Failed: {str(e)}"
            print(error_message)
            traceback.print_exc()
            return {"status": "error", "message": error_message, "origin": "handle_render_specific_camera"}

    # -------------------------------------------------------------------------
    # PR 14: Render Base64 (thin wrappers to _render_b64 + apply_response_cap)
    # -------------------------------------------------------------------------
    def render_viewport_base64(self, camera_path=None, geometry_path=None,
                               renderer="opengl", resolution=(640, 480),
                               format="PNG", consent_token=None):
        """PR 14：渲染单个 viewport 并以 base64 形式返回图像。

        薄封装到 _render_b64.render_viewport，支持 opengl / karma_cpu /
        karma_xpu 三种 renderer。无 hou 环境返回 _warning dict。响应过
        cmn.apply_response_cap 截断大 base64 payload。

        fork-render-policy-defense-in-depth：bridge 透传的 consent_token
        必须继续下发给 _render_b64.render_viewport（Layer 4 兜底校验），
        否则 karma 路径会在 Layer 4 永远 interrupt。
        """
        policy_evaluator = globals().get("_evaluate_render_policy_command")
        policy_result = (policy_evaluator(
            "render_viewport_base64", {
                "renderer": renderer,
                "consent_token": consent_token,
            }) if policy_evaluator is not None else None)
        if policy_result is not None:
            return cmn.apply_response_cap(policy_result)
        result = rb64.render_viewport(
            hou, camera_path=camera_path, geometry_path=geometry_path,
            renderer=renderer, resolution=tuple(resolution)
            if isinstance(resolution, (list, tuple)) else resolution,
            format=format, consent_token=consent_token)
        return cmn.apply_response_cap(result)

    def render_quad_views_base64(self, geometry_path=None, renderer="opengl",
                                 resolution=(480, 360), format="PNG",
                                 consent_token=None):
        """PR 14：渲染四视图（top / front / side / perspective）并以 base64
        形式返回 4 张图。

        薄封装到 _render_b64.render_quad_views，共享 bbox + camera rig；
        响应过 apply_response_cap。无 hou 环境返回 _warning dict。

        fork-render-policy-defense-in-depth：bridge 透传的 consent_token
        必须继续下发给 _render_b64.render_quad_views（Layer 4 兜底校验）。
        """
        policy_evaluator = globals().get("_evaluate_render_policy_command")
        policy_result = (policy_evaluator(
            "render_quad_views_base64", {
                "renderer": renderer,
                "consent_token": consent_token,
            }) if policy_evaluator is not None else None)
        if policy_result is not None:
            return cmn.apply_response_cap(policy_result)
        result = rb64.render_quad_views(
            hou, geometry_path=geometry_path, renderer=renderer,
            resolution=tuple(resolution)
            if isinstance(resolution, (list, tuple)) else resolution,
            format=format, consent_token=consent_token)
        return cmn.apply_response_cap(result)

    def render_specific_camera_base64(self, camera_path, resolution=(640, 480),
                                      format="PNG", renderer="opengl",
                                      consent_token=None):
        """PR 14：渲染指定相机视角并以 base64 形式返回。

        薄封装到 _render_b64.render_specific_camera_base64，响应过
        apply_response_cap。camera_path 必须存在。

        fork-render-policy-defense-in-depth：bridge 透传的 consent_token
        必须继续下发给 _render_b64.render_specific_camera_base64（Layer 4
        兜底校验）。
        """
        policy_evaluator = globals().get("_evaluate_render_policy_command")
        policy_result = (policy_evaluator(
            "render_specific_camera_base64", {
                "renderer": renderer,
                "consent_token": consent_token,
            }) if policy_evaluator is not None else None)
        if policy_result is not None:
            return cmn.apply_response_cap(policy_result)
        result = rb64.render_specific_camera_base64(
            hou, camera_path=camera_path,
            resolution=tuple(resolution)
            if isinstance(resolution, (list, tuple)) else resolution,
            format=format, renderer=renderer,
            consent_token=consent_token)
        return cmn.apply_response_cap(result)

    # -------------------------------------------------------------------------
    # C9 add-render-workflow-tools：5 个 ROP handler（薄封装 _render_settings
    # / _render_jobs + apply_response_cap；start_render 在 server 这一层
    # 重复 Layer 2 校验，再交给 _render_jobs 的 Layer 3 / Layer 4）
    # -------------------------------------------------------------------------
    def handle_list_render_nodes(self, parent_path="/out"):
        """C9：枚举 parent_path 下可分类 ROP 节点（ifd / opengl / karmarender）。

        薄封装到 _render_settings.list_render_nodes；响应过
        apply_response_cap。
        """
        return _rset.list_render_nodes(hou, parent_path=parent_path)

    def handle_get_render_settings(self, node_path):
        """C9：读取 node_path 白名单 parm 值。

        薄封装到 _render_settings.get_render_settings；未知 ROP
        type 整体 error。响应过 apply_response_cap。
        """
        return _rset.get_render_settings(hou, node_path)

    def handle_set_render_settings(self, node_path, parameters):
        """C9：受限可撤销写入（design.md §"set_render_settings"）。

        薄封装到 _render_settings.set_render_settings；完整预校
        验 + 快照 + 显式恢复契约（render_settings_apply_failed /
        render_settings_restore_failed）由模块负责。响应过
        apply_response_cap。
        """
        return _rset.set_render_settings(
            hou, node_path, parameters)

    def handle_create_render_node(self, node_type, parent_path="/out",
                                  name=None, parameters=None):
        """C9：受限创建可分类 ROP 节点（ifd / opengl / karmarender）。

        薄封装到 _render_settings.create_render_node；创建后通过
        同一白名单设置参数并校验 renderer 可识别。未知 node type
        整体 error。响应过 apply_response_cap。
        """
        return _rset.create_render_node(
            hou, node_type, parent_path=parent_path, name=name,
            parameters=parameters)

    def handle_start_render(self, node_path, frame_range=None,
                            consent_token=None):
        """C9：同步启动 ROP 渲染（design.md §"start_render 四层防御"）。

        入口为 server Layer 2：从真实 node 重新推断 policy renderer，
        不信任 bridge 传入值（bridge 提供的 ``policy_renderer`` 仅
        Layer 1 用，server handler 已在 bridge batch preflight 中
        阻挡，无效的 bridge 提示根本不会到这里）。Layer 3-4 由
        ``_render_jobs.start_render`` 内部再次校验。任何 redirect /
        interrupt / error 立即 return，**不**调 ``node.render()``。

        响应过 apply_response_cap。同步阻塞到 render 完成 / 失败 /
        中断；不签发 progress handle。
        """
        # Layer 2：从真实 node 独立 infer + policy；client 给的 hint
        # 由 bridge Layer 1 处理，这里不接受。
        resolved = _rset._resolve_rop_node(hou, node_path)
        if resolved.get("status") == "error":
            return cmn.apply_response_cap(resolved)
        node = resolved["node"]
        type_name = resolved["type"]
        renderer = _rset._resolve_policy_renderer(node, type_name)
        decision, payload = _rp._enforce_render_policy_layer(
            renderer, consent_token)
        if decision == "redirect":
            return cmn.apply_response_cap(payload)
        if decision == "interrupt":
            return cmn.apply_response_cap(payload)
        if decision == "error":
            return cmn.apply_response_cap(payload)
        return _rjobs.start_render(
            hou, node_path, frame_range=frame_range,
            consent_token=consent_token)

    # -------------------------------------------------------------------------
    # Existing Placeholder asset library methods
    # -------------------------------------------------------------------------
    # -------------------------------------------------------------------------
    # PR 15: SideFX 在线文档查询（thin wrapper to _help + apply_response_cap）
    # -------------------------------------------------------------------------
    def get_houdini_help(self, help_type, item_name, timeout=10):
        """PR 15：查询 Houdini 帮助文档，**本地优先 + 在线回退**。

        薄封装到 _help.get_houdini_help，支持 11 个 help_type（sop / obj /
        dop / cop2 / chop / vop / lop / top / rop / vex_function /
        python_hou）。HTML 解析使用 stdlib html.parser，无需 beautifulsoup4。

        **local-help-first-fallback**：优先打本地 help server（Houdini GUI
        启动时自带，默认 `http://127.0.0.1:48626/`），本地不可用或白屏时
        自动回退 SideFX 在线。返回 dict 透传 `_source`（`"local"` /
        `"online"` / `""`）与 `_fallback_reason`（回退原因短串）供 AI 判断。

        HTTP 4xx / 5xx / 网络错误 / timeout / 白屏 均降级为 status=error 字典
        或回退在线，不抛异常。响应整体过 cmn.apply_response_cap 截断大 payload。
        """
        result = hlp.get_houdini_help(
            help_type, item_name, timeout=timeout)
        return cmn.apply_response_cap(result)

    # -------------------------------------------------------------------------
    # PR 18：verify_hou_api — AI-friendly wrapper over get_houdini_help
    # 在 PR 15 之上额外合成 _ai_hint 字段（F-C pattern / API 不存在 /
    # 已找到方法: <sig>），帮助 AI 在调未知 hou API 前拿到可直接使用的
    # 简短提示，避免 2026-07-21 F-C bug 实证的 hou type-check 卡死。
    # -------------------------------------------------------------------------
    def verify_hou_api(self, item_name, help_type="python_hou", timeout=10):
        """PR 18：AI-friendly wrapper over get_houdini_help（PR 15）。

        与 PR 15 相同：薄封装到 _help.get_houdini_help + apply_response_cap。
        不同：额外在 result 顶层加 `_ai_hint` 字段，由模块级
        `_synthesize_ai_hint(item_name, result)` 合成。

        **local-help-first-fallback**：自动继承本地优先 + 在线回退行为，
        返回 dict 透传 `_source`（`"local"` / `"online"` / `""`）与
        `_fallback_reason`，AI 可读 `_source` 判断本次命中本地还是在线。
        `_ai_hint` 合成逻辑不依赖这两个 advisory 字段。

        `_ai_hint` 规则（参考 design.md §2）：
          - status=error                       → F3 fallback 提示
          - status=success + methods=[]        → "API 不存在" / F-C 提示
          - status=success + methods=非空       → "已找到方法: <sig>" 提示
          - 空 / 未知 status                    → "" （防御性）

        AI 在调未知 hou API 前应先读 `_ai_hint`；若 hint 含 F-C
        fallback（如 setDisplayFlag）应优先按 hint 推荐的方式调，
        避免直接调不存在的 hou.ObjNode 方法导致 Houdini 卡死。
        """
        result = hlp.get_houdini_help(
            help_type, item_name, timeout=timeout)
        result["_ai_hint"] = _synthesize_ai_hint(item_name, result)
        return cmn.apply_response_cap(result)

    # -------------------------------------------------------------------------
    # PR 19: 动画与帧控制（thin wrapper to _animation + apply_response_cap）
    # 10 个 handler：2 只读 + 6 可 undo 数据写 + 2 no-undo 运行态写。
    # 详见 MUTATING_COMMANDS / READ_ONLY_COMMANDS / NO_UNDO_COMMANDS 注释。
    # 设计契约（来自 openspec/changes/add-animation-and-frame-control）：
    # - frame / fps / time / range 端点 / keyframe frame 与 value 全部按
    #   float 处理，sub-frame 不被截断；bool / NaN / ±inf 拒绝。
    # - get_frame / get_keyframes 只读；set_expression 与 keyframe /
    #   range 写同属 MUTATING_COMMANDS，可 undo；
    #   set_frame / playbar_control 是 NO_UNDO_COMMANDS，运行态时间线
    #   写，batch 中由 dispatcher 在 NO_UNDO 前关闭 undo segment，保证
    #   不进入 hou.undos.group。
    # -------------------------------------------------------------------------
    def get_frame(self):
        """PR 19：读取当前帧 / 时间 / fps / 三组 range / increment，全部 float。

        返回 dict 字段：frame / time / fps / frame_range /
        playback_range / frame_increment；任一 hou 调用抛异常时降级
        为 status=error 而非向调用方抛异常。响应整体过
        ``apply_response_cap`` 截断大 payload（虽然本接口规模小，仍
        保持 defense-in-depth）。
        """
        return cmn.apply_response_cap(anim.get_frame(hou))

    def set_frame(self, frame):
        """PR 19：写入当前帧（运行态时间线写，no-undo）。

        ``frame`` 接受 int / float；拒绝 bool / NaN / ±inf / 非数值；
        hou 接受 float 值并保留 sub-frame。任何 hou 异常降级为
        error dict。响应过 ``apply_response_cap``。
        """
        return cmn.apply_response_cap(anim.set_frame(hou, frame))

    def set_frame_range(self, start, end):
        """PR 19：写入全局 frame range。

        ``start`` / ``end`` 必须为有限浮点且 ``start <= end``；end 可
        sub-frame。错误（如 start > end）返回 status=error 不写；成功
        时由 hou.playbar.setFrameRange 持久化（场景写，可 undo）。
        响应过 ``apply_response_cap``。
        """
        return cmn.apply_response_cap(
            anim.set_frame_range(hou, start, end))

    def set_playback_range(self, start, end):
        """PR 19：写入 playback range（场景写，可 undo）。

        校验同 ``set_frame_range``；调 ``hou.playbar.setPlaybackRange``。
        响应过 ``apply_response_cap``。
        """
        return cmn.apply_response_cap(
            anim.set_playback_range(hou, start, end))

    def set_keyframe(self, path, parameter, frame, value):
        """PR 19：单关键帧写入（场景写，可 undo）。

        ``path`` / ``parameter`` 必为非空字符串；``frame`` / ``value``
        必须为有限浮点。value 创建 ``hou.Keyframe(float(value))`` 并
        ``keyframe.setFrame(float(frame))`` 后 ``parm.setKeyframe``。
        字符串参数 / NaN / inf 等返回 status=error 不写。响应过
        ``apply_response_cap``。
        """
        return cmn.apply_response_cap(
            anim.set_keyframe(hou, path, parameter, frame, value))

    def set_keyframes(self, keyframes):
        """PR 19：批量关键帧写入（场景写，可 undo）。

        ``keyframes`` 为 list，每项 dict 至少含 ``path`` /
        ``parameter`` / ``frame`` / ``value``；任一项无效则**整调用**
        失败、零写入。全部有效时在单个 ``hou.undos.group`` 内逐项
        写入并返回 ``set_count`` / ``requested``。错误列表（如有）
        响应过 ``apply_response_cap`` 截断。
        """
        return cmn.apply_response_cap(
            anim.set_keyframes(hou, keyframes))

    def delete_keyframe(self, path, parameter, frame):
        """PR 19：删除指定帧的关键帧（场景写，可 undo）。

        ``frame`` 必须为有限浮点（删除 sub-frame 精确点）。调用后
        再读 ``parm.keyframes()`` 验证目标帧已消失；不消失则
        status=error（"no keyframe found at frame ..."）。响应过
        ``apply_response_cap``。
        """
        return cmn.apply_response_cap(
            anim.delete_keyframe(hou, path, parameter, frame))

    def get_keyframes(self, path, parameter):
        """PR 19：读取 parm 的全部关键帧（只读）。

        返回 list 中每项 ``{"frame": float, "value": float}``，不
        做 ``int()`` 截断；空关键帧列表返回 ``keyframes=[]``。响应过
        ``apply_response_cap``。
        """
        return cmn.apply_response_cap(
            anim.get_keyframes(hou, path, parameter))

    def playbar_control(self, action):
        """PR 19：playbar 播放 / 步进 / 跳转（运行态时间线写，no-undo）。

        ``action`` 取值：
        - ``play`` / ``reverse`` / ``stop``：直接调 SideFX HOM 同名方法。
        - ``step_forward`` / ``step_backward``：仅通过
          ``hou.setFrame(current ± hou.playbar.frameIncrement())`` 路径
          并 clamp 到当前 playback range 闭区间，**不引入其他 step
          helper**（incremement 非有限正数 / range 不可用 → error 且
          **不**调 hou.setFrame）。
        - ``goto_start`` / ``goto_end``：直接设 playback range 端点。

        整个 action 集在 ``NO_UNDO_COMMANDS`` 中，batch dispatcher 在
        该命令前关闭 undo segment。响应过 ``apply_response_cap``。
        """
        return cmn.apply_response_cap(
            anim.playbar_control(hou, action))

    def set_expression(self, path, parameter, expression,
                       language="hscript"):
        """PR 19：写入 parm 表达式（参数通道持久写，**可 undo**）。

        ``language`` 接受 ``hscript`` / ``python``，映射到对应
        ``hou.exprLanguage``；其他值（包括 ``hscript`` 大小写
        变体）一律 status=error。该命令属于参数通道数据写，**不**
        归为只读或 no-undo（设计 D3）。响应过 ``apply_response_cap``。
        """
        return cmn.apply_response_cap(
            anim.set_expression(hou, path, parameter, expression,
                                 language=language))

    # -------------------------------------------------------------------------
    # PR 16: 连接诊断（check_connection / ping_houdini，不持久化连接）
    # -------------------------------------------------------------------------
    def check_connection(self):
        """PR 16：返回 Houdini 端连接信息（版本 / build / 当前 .hip 文件）。

        仅使用既有 hou 上下文查询，不开新 socket，也不修改任何场景状态。
        字段说明：
        - hou_version: hou.applicationVersionString() 字符串（H21+；旧 hou.version() 已移除）
        - hou_build: hou.applicationVersionString() 优先，回退到 str(hou.applicationVersion())
        - hip_file: 当前 .hip 文件绝对路径，未保存时为 None
        - hip_file_basename: 仅文件名（basename）
        - is_untitled: True 表示当前 hip 未保存（H21+ 用 hou.hipFile.isNewFile()）
        - node_count: root 节点 + 所有子节点总数（hou.node("/").allSubChildren() 长度 + 1）
        - desktop_count: hou.ui.desktops() 数量
        - _status: 固定为 "ok"，便于上层做健康检查
        """
        hip_path = hou.hipFile.path()
        if hou.hipFile.isNewFile():
            hip_file = None
            hip_basename = None
            untitled = True
        else:
            hip_file = hip_path
            hip_basename = os.path.basename(hip_path)
            untitled = False

        if hasattr(hou, "applicationVersionString"):
            build_str = hou.applicationVersionString()
        else:
            build_str = str(hou.applicationVersion())

        return {
            "hou_version": hou.applicationVersionString(),
            "hou_build": build_str,
            "hip_file": hip_file,
            "hip_file_basename": hip_basename,
            "is_untitled": untitled,
            "node_count": len(hou.node("/").allSubChildren()) + 1,
            "desktop_count": len(hou.ui.desktops()),
            "_status": "ok",
        }

    def ping_houdini(self, timeout=5):
        """PR 16：轻量级 Houdini 端 ping，验证响应时间。

        仅在既有 hou 上下文里调用一次 hou.applicationVersionString()（H21+
        真实存在；旧 hou.version() 已移除），不持久化新连接，不开新 socket，
        不修改场景。异常被捕获后以 pong=False + error 字段返回，不会传播到
        bridge。

        Args:
            timeout: 最长等待毫秒对应的秒数（默认 5 秒）

        Returns:
            成功: {"pong": True, "elapsed_ms": int, "within_timeout": bool,
                    "hou_version": str}
            失败: {"pong": False, "elapsed_ms": int, "within_timeout": False,
                    "error": str}
        """
        start = time.time()
        try:
            v = hou.applicationVersionString()
            elapsed_ms = int((time.time() - start) * 1000)
            return {
                "pong": True,
                "elapsed_ms": elapsed_ms,
                "within_timeout": elapsed_ms < int(timeout * 1000),
                "hou_version": v,
            }
        except Exception as e:
            elapsed_ms = int((time.time() - start) * 1000)
            return {
                "pong": False,
                "elapsed_ms": elapsed_ms,
                "within_timeout": False,
                "error": str(e),
            }

    def get_asset_categories(self):
        """Placeholder for an asset library feature (e.g., Poly Haven)."""
        return {"error": "get_asset_categories not implemented"}

    def search_assets(self):
        """Placeholder for asset search logic."""
        return {"error": "search_assets not implemented"}

    def import_asset(self):
        """Placeholder for asset import logic."""
        return {"error": "import_asset not implemented"}
