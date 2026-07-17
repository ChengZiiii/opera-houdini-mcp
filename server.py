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
from . import _node_info as ni

# PR 4 scene-diff cache：execute_code(capture_diff=True) 时填充；get_last_scene_diff 读取。
_before_scene = None
_after_scene = None

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

class HoudiniMCPServer:
    def __init__(self, host='127.0.0.1', port=9876):
        self.host = host
        self.port = port
        self.running = False
        self.server_socket = None
        self.client = None
        self.buffer = b''
        self.timer = None

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
            print(f"HoudiniMCP server started on {self.host}:{self.port}")
        except Exception as e:
            print(f"Failed to start server: {str(e)}")
            self.stop()
            
    def stop(self):
        """Stop listening; close sockets and timers."""
        self.running = False
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
                        print(f"Error accepting connection: {str(e)}")
                        break
                    if self.client is not None:
                        print(f"New connection from {address}; replacing existing client")
                        self._cleanup_client()
                    new_client.setblocking(False)
                    self.client = new_client
                    print(f"Connected to client: {address}")
            
            if self.client:
                try:
                    data = self.client.recv(8192)
                    if data:
                        self.buffer += data
                        while True:
                            if len(self.buffer) < 4:
                                break
                            msg_len = struct.unpack('>I', self.buffer[:4])[0]
                            MAX_MSG_LEN = 50 * 1024 * 1024
                            if msg_len > MAX_MSG_LEN:
                                print(f"Message too large ({msg_len} bytes), disconnecting client")
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
                                except (BrokenPipeError, ConnectionResetError, OSError) as send_err:
                                    print(f"Failed to send response (client likely disconnected): {send_err}")
                                    self._cleanup_client()
                                    break
                            except json.JSONDecodeError as e:
                                print(f"Invalid JSON in message: {e}")
                    else:
                        print("Client disconnected (empty recv)")
                        self._cleanup_client()
                except BlockingIOError:
                    pass
                except (ConnectionResetError, BrokenPipeError, OSError) as e:
                    print(f"Client connection lost: {str(e)}")
                    self._cleanup_client()

        except Exception as e:
            print(f"Server error: {str(e)}")

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
        cmd_type = command.get("type")
        params = command.get("params", {})

        # Always-available handlers
        handlers = {
            "get_scene_info": self.get_scene_info,
            "save_scene": self.save_scene,
            "load_scene": self.load_scene,
            "new_scene": self.new_scene,
            "create_node": self.create_node,
            "modify_node": self.modify_node,
            "delete_node": self.delete_node,
            "get_node_info": self.get_node_info,
            "execute_code": self.execute_code,
            "get_last_scene_diff": self.get_last_scene_diff,
            "set_material": self.set_material,
            "get_asset_lib_status": self.get_asset_lib_status,
            "import_opus_url": self.handle_import_opus_url,
            # Graph editing & introspection
            "connect_nodes": self.connect_nodes,
            "disconnect_input": self.disconnect_input,
            "set_parameters": self.set_parameters,
            "get_parameter_schema": self.get_parameter_schema,
            "set_node_flags": self.set_node_flags,
            "layout_children": self.layout_children,
            # PR 9: 图编辑增强（重写 reorder_inputs + layout_children + 3 新 handler）
            "reorder_inputs": self.reorder_inputs,
            "set_node_position": self.set_node_position,
            "set_node_color": self.set_node_color,
            "create_network_box": self.create_network_box,
            "find_error_nodes": self.find_error_nodes,
            "cook_node": self.cook_node,
            # VEX wrangles
            "create_wrangle": self.create_wrangle,
            "set_wrangle_code": self.set_wrangle_code,
            # Geometry introspection
            "get_geometry_info": self.get_geometry_info,
            "get_geometry_data": self.get_geometry_data,
            # Add new render handlers
            "render_single_view": self.handle_render_single_view,
            "render_quad_view": self.handle_render_quad_view,
            "render_specific_camera": self.handle_render_specific_camera,
            # PR 6: node discovery + cache management (NodeTypeCache)
            "list_node_types": self.list_node_types,
            "list_children": self.list_children,
            "find_nodes": self.find_nodes,
            "manage_cache": self.manage_cache,
            "ping": self._handle_ping,
            # PR 7: 材质 CRUD + 参数白名单 + texture 识别
            "create_material": self.create_material,
            "assign_material": self.assign_material,
            "get_material_info": self.get_material_info,
            # PR 8: HScript 执行包装（薄封装到 _hscript.execute_hscript）
            "execute_hscript": self.execute_hscript,
        }
        
        # If user has toggled asset library usage
        if getattr(hou.session, "houdinimcp_use_assetlib", False):
            asset_handlers = {
                "get_asset_categories": self.get_asset_categories,
                "search_assets": self.search_assets,
                "import_asset": self.import_asset,
            }
            handlers.update(asset_handlers)

        handler = handlers.get(cmd_type)
        if not handler:
            return {"status": "error", "message": f"Unknown command type: {cmd_type}"}

        print(f"Executing handler for {cmd_type}")
        with self._undo_group(cmd_type):
            result = handler(**params)
        print(f"Handler execution complete for {cmd_type}")
        return {"status": "success", "result": result}

    # Commands that mutate the scene get wrapped in a single undo group so the
    # artist can Ctrl+Z any agent action as one step.
    MUTATING_COMMANDS = frozenset({
        "create_node", "modify_node", "delete_node", "set_material",
        "import_opus_url", "import_asset", "connect_nodes", "disconnect_input",
        "set_parameters", "set_node_flags", "layout_children",
        "create_wrangle", "set_wrangle_code",
        # PR 7: 材质 CRUD 命令也属于场景变更
        "create_material", "assign_material",
        # PR 9: 图编辑增强命令均修改场景
        "reorder_inputs", "set_node_position", "set_node_color",
        "create_network_box",
    })

    @contextmanager
    def _undo_group(self, cmd_type):
        if cmd_type in self.MUTATING_COMMANDS and hasattr(hou, "undos"):
            with hou.undos.group(f"MCP: {cmd_type}"):
                yield
        else:
            yield

    def _handle_ping(self):
        return {"pong": True, "protocol": 1}

    # -------------------------------------------------------------------------
    # Basic Info & Node Operations
    # -------------------------------------------------------------------------

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

    def handle_render_single_view(self, orthographic=False, rotation=(0, 90, 0), render_path=None, render_engine="opengl", karma_engine="cpu"):
        """Handles the 'render_single_view' command."""
        # self._check_render_lib()
        
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
                karma_engine=karma_engine
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

    def handle_render_quad_view(self, orthographic=True, render_path=None, render_engine="opengl", karma_engine="cpu"):
        """Handles the 'render_quad_view' command."""
        # self._check_render_lib()
        
        if not render_path:
            render_path = tempfile.gettempdir()

        try:
            print(f"Calling HoudiniMCPRender.render_quad_view with ortho={orthographic}, engine={render_engine}...")
            filepaths = render_quad_view(
                orthographic=orthographic,
                render_path=render_path,
                render_engine=render_engine,
                karma_engine=karma_engine
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

    def handle_render_specific_camera(self, camera_path, render_path=None, render_engine="opengl", karma_engine="cpu"):
        """Handles the 'render_specific_camera' command."""
        # self._check_render_lib()
        
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
                karma_engine=karma_engine
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
    # Existing Placeholder asset library methods
    # -------------------------------------------------------------------------
    def get_asset_categories(self):
        """Placeholder for an asset library feature (e.g., Poly Haven)."""
        return {"error": "get_asset_categories not implemented"}

    def search_assets(self):
        """Placeholder for asset search logic."""
        return {"error": "search_assets not implemented"}

    def import_asset(self):
        """Placeholder for asset import logic."""
        return {"error": "import_asset not implemented"}
