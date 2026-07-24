#!/usr/bin/env python
"""
houdini_mcp_server.py

This is the "bridge" or "driver" script that Claude will run via `uv run`.
It uses the MCP library (fastmcp) to communicate with Claude over stdio,
and relays each command to the local Houdini plugin on port 9876.
"""
import sys
import os
import time
import argparse

# 内嵌 Python 受 _pth 控制，启动独立脚本时不会自动把脚本目录加进
# sys.path。这里显式把脚本所在目录 prepend 进去，确保 sibling 模块
# （如 _render_policy）在 standalone 启动方式下也能被平铺 import 找到。
# 不影响走 ``-m houdinimcp.houdini_mcp_server`` 或 test_tools.py 主动
# sys.path.insert 的场景。
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

# Get the directory where the script is located (needed for dotenv path)
script_dir = _HERE

import json
import socket
import struct
import logging
from dataclasses import dataclass
from typing import Dict, Any, List
from contextlib import asynccontextmanager
from mcp.server.fastmcp import FastMCP, Context
import asyncio

# --- OPUS Imports and Setup ---
import requests
from dotenv import load_dotenv
from urllib.parse import urljoin # To construct RapidAPI URLs
try:
    from langchain_classic.output_parsers import ResponseSchema, StructuredOutputParser
    LANGCHAIN_AVAILABLE = True
except ImportError:
    try:
        from langchain.output_parsers import ResponseSchema, StructuredOutputParser
        LANGCHAIN_AVAILABLE = True
    except ImportError:
        LANGCHAIN_AVAILABLE = False
        print("Warning: Langchain not found. opus_get_model_params_schema tool will be limited.", file=sys.stderr)

# Load environment variables from urls.env located in the script's directory
dotenv_path = os.path.join(script_dir, 'urls.env')
if os.path.exists(dotenv_path):
    load_dotenv(dotenv_path=dotenv_path)
    print(f"Loaded environment variables from {dotenv_path}", file=sys.stderr)
else:
    print(f"Warning: urls.env not found at {dotenv_path}", file=sys.stderr)

# --- Use RapidAPI variables --- 
RAPIDAPI_HOST_URL = os.getenv("RAPIDAPI_HOST_URL") # e.g., https://opus5.p.rapidapi.com/
RAPIDAPI_HOST = os.getenv("RAPIDAPI_HOST") # e.g., opus5.p.rapidapi.com
RAPIDAPI_KEY = os.getenv("RAPIDAPI_KEY")

# Set paths for OPUS API endpoints relative to the RapidAPI host URL
GET_ATTRIBUTES_PATH = "/get_attributes_with_name" 
CREATE_BATCH_PATH = "/create_opus_batch_component" # Use batch endpoint
CREATE_COMPONENT_PATH = "/create_opus_component" # Keep old path if needed elsewhere, or remove
VARIATE_PATH = "/variate_opus_result"
GET_JOB_RESULT_PATH = "/get_opus_job_result"

TIMEOUT = 15 # seconds for RapidAPI
HOUDINI_CONNECTION_TIMEOUT = 300 # 5 minutes for Houdini operations (rendering can be slow)

if not RAPIDAPI_HOST_URL or not RAPIDAPI_HOST or not RAPIDAPI_KEY:
    print("Warning: RAPIDAPI_HOST_URL, RAPIDAPI_HOST, or RAPIDAPI_KEY not configured. OPUS API features will be disabled.", file=sys.stderr)
    # Set URL variables to None for safety
    GET_ATTRIBUTES_URL = None
    CREATE_COMPONENT_URL = None
    VARIATE_URL = None
    GET_JOB_RESULT_URL = None
else:
    # Construct full URLs
    GET_ATTRIBUTES_URL = urljoin(RAPIDAPI_HOST_URL, GET_ATTRIBUTES_PATH)
#    CREATE_BATCH_URL = urljoin(RAPIDAPI_HOST_URL, CREATE_BATCH_PATH)
    CREATE_COMPONENT_URL = urljoin(RAPIDAPI_HOST_URL, CREATE_COMPONENT_PATH)
    VARIATE_URL = urljoin(RAPIDAPI_HOST_URL, VARIATE_PATH)
    GET_JOB_RESULT_URL = urljoin(RAPIDAPI_HOST_URL, GET_JOB_RESULT_PATH)
    # Optionally warn if old OPUS_API is still set

# --- End OPUS Setup ---


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("HoudiniMCP_StdioServer")

# --- Render policy enforcement (fork-render-policy-redirect-and-consent) ---
# 入口校验 helper：6 个 render tool 共享，opengl 走 redirect，karma_* 走
# interrupt + consent token。其他 renderer（mantra / 未知值）原样放行。
# 设计契约：返回 ``{"_redirect": ...}`` / ``{"_interrupt": ...}`` 结构化
# dict，bridge 层透传到任何 AI 客户端 SDK，由 agent 框架识别处理。
#
# 注意：相对 import 会被 ``test_tools.py`` 的 flat ``import houdini_mcp_server``
# 模式破坏（无 parent package）。这里先尝试相对 import；fallback 到 flat
# import（hython / test_tools.py 把 fork 根目录加进 sys.path 的场景）。
try:
    from . import _render_policy as _rp
except ImportError:
    import _render_policy as _rp  # type: ignore


def _apply_render_policy_to_engine(render_engine, karma_engine=None,
                                   consent_token=None):
    """应用 fork-render-policy-redirect-and-consent 入口校验。

    Args:
        render_engine: ``render_engine`` 参数（``opengl`` / ``karma`` /
            ``mantra``）。
        karma_engine: ``karma_engine`` 参数（``cpu`` / ``gpu``），仅
            ``render_engine == "karma"`` 时有意义。
        consent_token: agent 重调时携带的 token（karma 路径需要）。

    Returns:
        dict_or_None: 命中 redirect / interrupt 时返回对应结构化 dict；
        ``None`` 时表示放行，调用方继续原逻辑。
    """
    action, payload = _rp.enforce_render_engine_policy(
        render_engine, karma_engine)
    if action == "allow":
        return None
    if action == "redirect":
        return payload
    # interrupt 路径
    if action == "interrupt":
        if consent_token and _rp.consume_consent_token(consent_token):
            return None  # consent 校验通过 → 放行
        return payload  # 未带 token / token 过期 / token 无效 → 返回 interrupt
    return None  # 防御性兜底


def _apply_render_policy_to_renderer(renderer, consent_token=None):
    """renderer 直接版本（PR 14 render_*_base64 工具用）。"""
    action, payload = _rp.enforce_render_policy(renderer)
    if action == "allow":
        return None
    if action == "redirect":
        return payload
    if action == "interrupt":
        if consent_token and _rp.consume_consent_token(consent_token):
            return None
        return payload
    return None

# --- Minimal api.utils.fix_rgb replication ---
# Assume it takes a list/tuple and returns [r, g, b] if valid, else None
def fix_rgb(color_val):
    if isinstance(color_val, (list, tuple)) and len(color_val) == 3:
        try:
            # Ensure they are numbers (int or float) and within typical 0-255 or 0-1 range
            # For simplicity, just check if they are numbers. API might expect 0-255 ints.
            rgb = [float(c) for c in color_val]
            # Basic check - could add range validation 0-255 or 0-1 if needed
            return rgb # Returning as floats for now
        except (ValueError, TypeError):
            return None
    return None
# --- End utils replication ---


# --- OPUS Helper Functions (Updated for RapidAPI) ---
def get_all_component_names() -> List[str]:
    # result = ["Sofa", "Chair", "Table", "CoffeeTable"] # Original subset
    result = [
        "Sofa", "Chair", "Table", "CoffeeTable",
         "Library", "StreetBench", "StreetLamp", "MailboxStandalone",
         "AntennaStandalone", "ParkingMeterStandalone", "AirConditionerStandalone",
         "BasketballHoop", "BusStop", "FloorLamp", "Bed", "TvUnit",
         "Sewer", "GarageDoorStandalone",
    ] # User provided list
    return result

def get_struct_params(struct: str) -> tuple[bool, dict]:
    if not RAPIDAPI_HOST_URL: return False, {"error": "RAPIDAPI_HOST_URL not configured"}
    url = GET_ATTRIBUTES_URL
    payload = {} # GET request, params in URL
    params = { "name": struct }
    headers = {
        'x-rapidapi-host': RAPIDAPI_HOST,
        'x-rapidapi-key': RAPIDAPI_KEY
    }
    try:
        response = requests.request("GET", url, headers=headers, params=params, data=payload, timeout=TIMEOUT)
        if str(response.status_code).startswith("2"):
            r = response.json()
            struct_result = r.get(struct) # Check if response structure changed
            if struct_result:
                return True, struct_result
            elif isinstance(r, dict) and not struct_result: # Maybe the top-level key is gone?
                 if struct in r.get("result", {}): # Check common patterns
                     return True, r["result"]
                 else:
                     # Fallback: return the whole response if structure unclear but success
                     logger.warning(f"Structure '{struct}' key not found directly in RapidAPI response, returning full JSON: {r}")
                     return True, r 
            else:
                return False, {"error": f"Structure '{struct}' not found in RapidAPI response: {r}"}
        else:
            return False, {"error": f"RapidAPI Error {response.status_code}: {response.text}"}
    except requests.exceptions.RequestException as e:
        return False, {"error": f"RapidAPI request failed: {str(e)}"}

def format_params(opus_response: dict) -> dict:
    formatted = {}
    # Adjust based on actual RapidAPI response structure if needed
    # Assuming original structure: { "StructureName": { "assets": [...] } }
    # Or maybe it's now just { "assets": [...] } or similar?
    # This needs verification against actual RapidAPI output.
    
    # Attempt 1: Original structure
    for asset_key, asset_data in opus_response.items():
        if isinstance(asset_data, dict) and "assets" in asset_data:
             for element in asset_data.get("assets", []):
                name = element.get("name")
                params = element.get("parameters", [])
                if not name: continue
                for p in params:
                    pname = p.get("name")
                    prange = p.get("range")
                    ptype = p.get("type")
                    if pname and prange is not None and ptype is not None:
                         formatted[f"{name}/{pname}"] = (prange, ptype)
    
    # Attempt 2: If assets are directly under top level (heuristic)
    if not formatted and "assets" in opus_response and isinstance(opus_response["assets"], list):
        logger.warning("format_params: Using fallback structure parsing (assets at top level).")
        for element in opus_response.get("assets", []):
            name = element.get("name")
            params = element.get("parameters", [])
            if not name: continue
            for p in params:
                pname = p.get("name")
                prange = p.get("range")
                ptype = p.get("type")
                if pname and prange is not None and ptype is not None:
                        formatted[f"{name}/{pname}"] = (prange, ptype)
                        
    # Attempt 3: If params are directly under top level (another heuristic)
    elif not formatted and "parameters" in opus_response and isinstance(opus_response["parameters"], list):
        logger.warning("format_params: Using fallback structure parsing (parameters at top level).")
        # How to get the asset name here? Assume it's part of the param name?
        # This path is less likely or needs more info.
        pass # Add logic if this structure is encountered

    if not formatted:
         logger.warning(f"format_params: Could not extract parameters from response: {opus_response}")
         
    return formatted

def get_color_params(component_name: str, opus_asset_keys: List[str]) -> dict:
    result = {}
    # Component level color
    result.setdefault(
        f"{component_name}/color_rgb",
        (
            "List[float]", # Assuming List[float] based on fix_rgb output
            f"Valid RGB color [R, G, B] (values likely 0-1 or 0-255, check API docs). Use if the user sets the entire color of the {component_name} or provided a single color without specifying a part."
        ),
    )
    # Asset level colors
    for asset in opus_asset_keys:
        result.setdefault(
            f"{asset}/color_rgb",
            (
                "List[float]", # Assuming List[float]
                f"Valid RGB color [R, G, B] for the {asset} part. Use if user set the color of this specific part of the {component_name}."
            ),
        )
    return result

def get_param_json(param_json: dict, color_params: dict) -> str:
    if not LANGCHAIN_AVAILABLE:
        # Fallback: simple JSON representation if Langchain is missing
        combined = {}
        for key, value in param_json.items():
            combined[key] = {"range": value[0], "type": value[1], "description": f"Allowed range: {value[0]}"}
        for key, value in color_params.items():
            combined[key] = {"type": value[0], "description": value[1]}
        return json.dumps(combined, indent=2)

    # Langchain way
    response_schemas = []
    for key, value in param_json.items():
        response_schemas.append(
            ResponseSchema(name=key, description=f"Allowed range: {value[0]}", type=str(value[1])) # Ensure type is string
        )
    for key, value in color_params.items():
        response_schemas.append(
            ResponseSchema(name=key, description=str(value[1]), type=str(value[0])) # Ensure type is string
        )
    try:
        output_parser = StructuredOutputParser.from_response_schemas(response_schemas)
        prompt_var = output_parser.get_format_instructions(only_json=True)
        return prompt_var
    except Exception as e:
        logger.error(f"Langchain StructuredOutputParser failed: {e}")
        # Fallback if Langchain parsing fails
        combined = {key: {"range": value[0], "type": value[1]} for key, value in param_json.items()}
        combined.update({key: {"type": value[0], "description": value[1]} for key, value in color_params.items()})
        return json.dumps(combined, indent=2)


def get_formatted_opus_params(structure: str) -> dict:
    # this is the main function to be called, copy of lambda function
    f, structure_json = get_struct_params(structure)
    if f:
        formatted_params = format_params(structure_json)
        # Extract keys carefully, might need adjustment based on format_params heuristics
        asset_keys = list(structure_json.keys()) if isinstance(structure_json, dict) else [] 
        if not asset_keys and "assets" in structure_json and isinstance(structure_json["assets"], list):
             asset_keys = [a.get("name") for a in structure_json["assets"] if a.get("name")]
             
        color_params = get_color_params(structure, asset_keys) 
        schema_str = get_param_json(formatted_params, color_params)
        # Try to parse back to JSON for consistent return type
        try:
            schema_json = json.loads(schema_str)
            return {"statusCode": 200, "result": schema_json}
        except json.JSONDecodeError:
             # If get_param_json returned non-JSON string (e.g. Langchain format instructions)
             return {"statusCode": 200, "result_format_instructions": schema_str}
    else:
        # structure_json should contain the error from get_struct_params
        status_code = 500 # Default error code
        if isinstance(structure_json, dict) and "error" in structure_json:
             if "RapidAPI Error 4" in structure_json["error"]: #粗略检查 4xx 错误
                  status_code = 400 # Or map specific codes if needed
             elif "RapidAPI Error 5" in structure_json["error"]:
                  status_code = 503 # Service unavailable or internal error
                  
        return {"statusCode": status_code, "error": structure_json.get("error", "Unknown error retrieving parameters")} 

def check_rgbs(structure: str, params: dict) -> dict:
    clean_params = {}
    if not isinstance(params, dict): return {} # Guard against non-dict input
    for k, v in params.items():
        if "color_rgb" in k:
            # Handle simplified key case from get_color_params
            if k == f"{structure}/color_rgb":
                valid_rgb = fix_rgb(v)
                if valid_rgb is not None:
                    clean_params[k] = valid_rgb # Use the potentially simplified key
            elif "/" in k: # Assume format like "asset/color_rgb"
                 valid_rgb = fix_rgb(v)
                 if valid_rgb is not None:
                    clean_params[k] = valid_rgb
            # Optional: Add handling for _layout/color_rgb if needed? User code had it commented.
            # elif "_layout/color_rgb" in k:
            #     k_fixed = f"{structure}/color_rgb" # Map it?
            #     valid_rgb = fix_rgb(v)
            #     if valid_rgb is not None:
            #         clean_params[k_fixed] = valid_rgb
        else:
            clean_params[k] = v
    return clean_params

def create_opus_batch(component_type: str, params: dict, count: int = 1) -> tuple[bool, dict]:
    if not RAPIDAPI_HOST_URL: return False, {"error": "RAPIDAPI_HOST_URL not configured"}
    url = CREATE_COMPONENT_URL # Use the correct RapidAPI URL
    p = {
        "name": component_type,
        "parameters": params,
        "extensions": ["gltf"], # Hardcoded GLTF for now
    #    "count": count, # Add count parameter
        # Add texture_resolution? Required by user example?
        # "texture_resolution": "1024", # Assuming default, adjust if needed
    }
    payload = json.dumps(p)
    headers = {
        'Content-Type': 'application/json',
        'x-rapidapi-host': RAPIDAPI_HOST,
        'x-rapidapi-key': RAPIDAPI_KEY
    }
    try:
        response = requests.request("POST", url, headers=headers, data=payload, timeout=TIMEOUT)
        response.raise_for_status() # Raise HTTPError for bad responses (4xx or 5xx)
        r = response.json()
        # Check response structure for batch_id (might be different from old API)
        batch_id = r.get("batch_job_id") or r.get("batch_id") or r.get("job_id") # Check common keys
        if batch_id:
            return True, r # Return the full response which contains the ID
        else:
             logger.error(f"RapidAPI batch creation success but no batch_id found in response: {r}")
             return False, {"error": "API succeeded but batch_id missing in response."}
    except requests.exceptions.HTTPError as e:
        logger.error(f"RapidAPI Error {e.response.status_code} creating batch: {e.response.text}")
        try:
             # Try to return the JSON error body if possible
             error_json = e.response.json()
             error_json["status_code"] = e.response.status_code # Add status code for later use
             return False, error_json
        except json.JSONDecodeError:
             return False, {"error": f"RapidAPI Error {e.response.status_code}: {e.response.text}", "status_code": e.response.status_code}
    except requests.exceptions.RequestException as e:
        logger.error(f"RapidAPI request failed creating batch: {str(e)}")
        return False, {"error": f"RapidAPI request failed: {str(e)}"}
    except json.JSONDecodeError as e:
        logger.error(f"Failed to decode RapidAPI response: {str(e)}")
        return False, {"error": "Failed to decode RapidAPI response."}


def create_opus_component(structure: str, params: dict, count: int = 1) -> dict:
    # Ensure params is a dict
    if not isinstance(params, dict):
         return {"statusCode": 400, "error": "Parameters must be a valid JSON object (dict)."}
         
    clean_params = check_rgbs(structure, params)
    status, result_json = create_opus_batch(structure, clean_params, count)
    if status:
        # Extract batch ID (key might vary)
        batch_id = result_json.get("batch_job_id") or result_json.get("batch_id") or result_json.get("job_id")
        if batch_id:
             logger.info(f"OPUS (RapidAPI) batch job created: {batch_id}")
             # Return a consistent success structure
             return {"statusCode": 200, "batch_id": batch_id, "raw_response": result_json}
        else:
             # This case should be handled inside create_opus_batch now
             logger.error(f"API success but no batch_job_id found in response: {result_json}")
             return {"statusCode": 500, "error": "API succeeded but batch_id missing."}
    else:
        # result_json already contains the error from create_opus_batch
        return {"statusCode": result_json.pop("status_code", 500), **result_json} # Use status_code if available


def variate_opus_result(result_id: str, count: int = 12) -> dict:
    if not RAPIDAPI_HOST_URL: return {"statusCode": 500, "error": "RAPIDAPI_HOST_URL not configured"}
    url = VARIATE_URL # Use RapidAPI URL
    p = {
         "base_job_uid": result_id, # Parameter name might change, check RapidAPI docs
         "count": count
         # Any other params needed for variation?
    }
    payload = json.dumps(p)
    headers = {
        'Content-Type': 'application/json',
        'x-rapidapi-host': RAPIDAPI_HOST,
        'x-rapidapi-key': RAPIDAPI_KEY
    }
    try:
        response = requests.request("POST", url, headers=headers, data=payload, timeout=TIMEOUT)
        response.raise_for_status()
        result_json = response.json()
        # Extract batch_id (key might vary)
        batch_id = result_json.get("batch_job_id") or result_json.get("batch_id") or result_json.get("job_id")
        if batch_id:
            logger.info(f"OPUS (RapidAPI) variation batch job created: {batch_id}")
            return {"statusCode": 200, "batch_id": batch_id, "raw_response": result_json}
        else:
            logger.error(f"RapidAPI variation success but no batch_id found: {result_json}")
            return {"statusCode": 500, "error": "API variation succeeded but batch_id missing."}
    except requests.exceptions.HTTPError as e:
        logger.error(f"RapidAPI Error {e.response.status_code} creating variation: {e.response.text}")
        try:
             error_json = e.response.json()
             return {"statusCode": e.response.status_code, "error": error_json}
        except json.JSONDecodeError:
             return {"statusCode": e.response.status_code, "error": e.response.text}
    except requests.exceptions.RequestException as e:
        logger.error(f"RapidAPI request failed creating variation: {str(e)}")
        return {"statusCode": 500, "error": f"Request failed: {str(e)}"}
    except json.JSONDecodeError as e:
        logger.error(f"Failed to decode variation RapidAPI response: {str(e)}")
        return {"statusCode": 500, "error": "Failed to decode variation RapidAPI response."}

# --- End OPUS Helper Functions ---


@dataclass
class HoudiniConnection:
    host: str
    port: int
    sock: socket.socket = None
    protocol_verified: bool = False

    def connect(self) -> bool:
        """Connect to the Houdini plugin (which is listening on self.host:self.port)."""
        if self.sock is not None:
            try:
                self.sock.getpeername()
                return True
            except (OSError, socket.error):
                logger.info("Stale socket detected, reconnecting...")
                self.disconnect()

        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.connect((self.host, self.port))
            logger.info(f"Connected to Houdini at {self.host}:{self.port}")
            return True
        except Exception as e:
            logger.error(f"Failed to connect to Houdini: {str(e)}")
            self.sock = None
            return False

    def disconnect(self):
        """Close socket if open."""
        if self.sock:
            try:
                self.sock.close()
            except Exception as e:
                logger.error(f"Error disconnecting from Houdini: {str(e)}")
            self.sock = None
        self.protocol_verified = False

    def send_command(self, cmd_type: str, params: Dict[str, Any] = None) -> Dict[str, Any]:
        """
        Send a JSON command to Houdini's server and wait for the JSON response.
        
        Protocol: each message is a 4-byte big-endian length prefix
        followed by that many bytes of UTF-8 JSON.
        
        Returns the parsed Python dict (e.g. {"status": "success", "result": {...}})
        """
        if not self.connect():
            error_msg = f"Could not connect to Houdini on {self.host}:{self.port}."
            logger.error(error_msg)
            return {"status": "error", "message": error_msg, "origin": "mcp_server_connection"}

        if not self.protocol_verified:
            try:
                ping_cmd = {"type": "ping", "params": {}}
                ping_data = json.dumps(ping_cmd).encode("utf-8")
                ping_frame = struct.pack('>I', len(ping_data)) + ping_data
                self.sock.sendall(ping_frame)

                self.sock.settimeout(HOUDINI_CONNECTION_TIMEOUT)
                hdr = b""
                while len(hdr) < 4:
                    chunk = self.sock.recv(4 - len(hdr))
                    if not chunk:
                        raise ConnectionAbortedError("Connection closed during ping handshake.")
                    hdr += chunk
                resp_len = struct.unpack('>I', hdr)[0]
                MAX_MSG_LEN = 50 * 1024 * 1024
                if resp_len > MAX_MSG_LEN:
                    raise ValueError(f"Ping response too large ({resp_len} bytes)")
                resp_payload = b""
                while len(resp_payload) < resp_len:
                    chunk = self.sock.recv(min(resp_len - len(resp_payload), 65536))
                    if not chunk:
                        raise ConnectionAbortedError("Connection closed during ping response transfer.")
                    resp_payload += chunk
                resp = json.loads(resp_payload.decode("utf-8"))
                result = resp.get("result", {})
                if result.get("pong"):
                    self.protocol_verified = True
                    logger.info(f"Protocol handshake verified (v{result.get('protocol', '?')})")
                else:
                    self.protocol_verified = True
                    logger.warning("Ping not recognized by plugin (old version?), but framing protocol works — proceeding")
            except Exception as e:
                logger.error(f"Protocol handshake failed: {str(e)}")
                self.disconnect()
                return {
                    "status": "error",
                    "message": "Houdini plugin protocol mismatch. Please restart the HoudiniMCP server in Houdini using the shelf tool, then retry.",
                    "origin": "mcp_server_protocol_handshake",
                }

        command = {"type": cmd_type, "params": params or {}}
        data_out = json.dumps(command).encode("utf-8")
        frame_out = struct.pack('>I', len(data_out)) + data_out

        try:
            self.sock.sendall(frame_out)
            logger.info(f"Sent command to Houdini: {command}")

            self.sock.settimeout(HOUDINI_CONNECTION_TIMEOUT)
            header = b""
            while len(header) < 4:
                chunk = self.sock.recv(4 - len(header))
                if not chunk:
                    raise ConnectionAbortedError("Connection closed by Houdini before sending response header.")
                header += chunk

            msg_len = struct.unpack('>I', header)[0]
            MAX_MSG_LEN = 50 * 1024 * 1024
            if msg_len > MAX_MSG_LEN:
                raise ValueError(f"Response too large ({msg_len} bytes)")

            payload = b""
            while len(payload) < msg_len:
                chunk = self.sock.recv(min(msg_len - len(payload), 65536))
                if not chunk:
                    raise ConnectionAbortedError("Connection closed by Houdini during response transfer.")
                payload += chunk

            decoded = payload.decode("utf-8")
            parsed = json.loads(decoded)
            logger.info(f"Received response from Houdini: {parsed}")
            return parsed

        except socket.timeout:
            error_msg = "Timeout receiving data from Houdini."
            logger.error(error_msg)
            self.disconnect()
            return {"status": "error", "message": error_msg, "origin": "mcp_server_send_command_timeout"}
        except Exception as e:
            error_msg = f"Error during Houdini communication for command '{cmd_type}': {str(e)}"
            logger.error(error_msg)
            self.disconnect()
            return {"status": "error", "message": error_msg, "origin": "mcp_server_send_command"}


# A global Houdini connection object
_houdini_connection: HoudiniConnection = None
_houdini_port: int = 9876  # Default port; override with --port

def get_houdini_connection() -> HoudiniConnection:
    """Get or create a persistent HoudiniConnection object."""
    global _houdini_connection
    if _houdini_connection is None:
        logger.info(f"Creating new HoudiniConnection on port {_houdini_port}.")
        _houdini_connection = HoudiniConnection(host="127.0.0.1", port=_houdini_port)

    # Always try to connect, returns True if already connected or successful now
    if not _houdini_connection.connect():
         # Connection failed, reset _houdini_connection to allow retry next time?
         host, port = _houdini_connection.host, _houdini_connection.port
         _houdini_connection = None
         raise ConnectionError(f"Could not connect to Houdini on {host}:{port}. Is the plugin running?")
         
    return _houdini_connection


# Now define the MCP server that Claude will talk to over stdio
mcp = FastMCP(
    "HoudiniMCP",
    description="A bridging server that connects Claude to Houdini via MCP stdio + TCP, with OPUS API integration."
)

@asynccontextmanager
async def server_lifespan(app: FastMCP):
    """Startup/shutdown logic. Called automatically by fastmcp."""
    logger.info("Houdini MCP server starting up (stdio).")
    # Attempt to connect right away? Or lazily on first call? Lazy seems safer.
    # try:
    #     get_houdini_connection()
    #     logger.info("Successfully connected to Houdini on startup.")
    # except Exception as e:
    #     logger.warning(f"Could not connect to Houdini on startup: {e}")
    #     logger.warning("Make sure Houdini is running with the plugin on port 9876.")
    yield {} # Context is empty for now
    logger.info("Houdini MCP server shutting down.")
    global _houdini_connection
    if _houdini_connection is not None:
        _houdini_connection.disconnect()
        _houdini_connection = None
    logger.info("Connection to Houdini closed.")

mcp.lifespan = server_lifespan


# -------------------------------------------------------------------
# Original Houdini Tools (Get/Create Node, Execute Code)
# -------------------------------------------------------------------
@mcp.tool()
def get_scene_info(ctx: Context) -> str:
    """
    Ask Houdini for scene info. Returns JSON as a string.
    """
    try:
        conn = get_houdini_connection()
        response = conn.send_command("get_scene_info")
        # response should look like {"status": "success", "result": {...}} or {"status": "error", ...}
        if response.get("status") == "error":
            # Include origin if available
            origin = response.get('origin', 'houdini')
            return f"Error ({origin}): {response.get('message', 'Unknown error')}"
        return json.dumps(response.get("result", {}), indent=2) # Return empty dict if no result
    except ConnectionError as e:
         return f"Connection Error getting scene info: {str(e)}"
    except Exception as e:
        # Catch-all for unexpected errors in this function
        logger.error(f"Unexpected error in get_scene_info tool: {str(e)}", exc_info=True)
        return f"Server Error retrieving scene info: {str(e)}"

@mcp.tool()
def create_node(ctx: Context, node_type: str, parent_path: str = "/obj", name: str = None) -> str:
    """
    Create a new node in Houdini.
    """
    try:
        conn = get_houdini_connection()
        params = { "node_type": node_type, "parent_path": parent_path }
        if name: params["name"] = name
        response = conn.send_command("create_node", params)

        if response.get("status") == "error":
            origin = response.get('origin', 'houdini')
            return f"Error ({origin}): {response.get('message', 'Unknown error')}"
        # Assuming result contains node info like {'name': ..., 'path': ..., 'type': ...}
        return f"Node created: {json.dumps(response.get('result', {}), indent=2)}"
    except ConnectionError as e:
         return f"Connection Error creating node: {str(e)}"
    except Exception as e:
        logger.error(f"Unexpected error in create_node tool: {str(e)}", exc_info=True)
        return f"Server Error creating node: {str(e)}"

@mcp.tool()
def execute_houdini_code(ctx: Context, code: str,
                          policy: str = "normal",
                          allow_dangerous: bool = False,
                          allow_heavy_geometry: bool = False,
                          capture_diff: bool = False) -> str:
    """
    Execute arbitrary Python code in Houdini's environment. LAST RESORT:
    prefer the dedicated tools (connect_nodes, set_parameters, create_wrangle,
    get_geometry_info, ...) — they validate input, report structured errors
    and are undoable as a single step. Use this only for operations no
    dedicated tool covers.

    Args:
        code: Python source to exec inside Houdini.
        policy: "read-only" / "normal" / "privileged" (PR 4 safety policy).
        allow_dangerous: explicit per-call dangerous-code override (privileged only).
        allow_heavy_geometry: explicit per-call heavy-geometry override.
        capture_diff: when True, server snapshots scene state before & after.

    Returns status, any stdout/stderr, and an optional audit block.
    """
    try:
        conn = get_houdini_connection()
        response = conn.send_command("execute_code", {
            "code": code,
            "policy": policy,
            "allow_dangerous": allow_dangerous,
            "allow_heavy_geometry": allow_heavy_geometry,
            "capture_diff": capture_diff,
        })

        # Handle Houdini-side errors first (could be connection error or execution error)
        if response.get("status") == "error":
            origin = response.get('origin', 'houdini')
            return f"Error ({origin}): {response.get('message', 'Unknown error')}"

        # Handle success case (response should have status=success and a result dict)
        result = response.get("result", {}) # Default to empty dict
        if result.get("executed"): # Check if executed flag is True
            stdout = result.get("stdout", "").strip()
            stderr = result.get("stderr", "").strip()
            audit = result.get("_audit")

            output_message = "Code executed successfully."
            if stdout:
                output_message += f"\n--- Stdout ---\n{stdout}"
            if stderr:
                output_message += f"\n--- Stderr ---\n{stderr}"
            if audit:
                output_message += "\n--- Audit ---\n" + json.dumps(audit, indent=2)
            return output_message
        elif result.get("blocked"):
            # PR 4 policy rejection: server returned blocked dict
            reason = result.get("reason", "blocked by policy")
            output_message = "Execution blocked: " + reason
            hits = result.get("hits") or {}
            if hits:
                output_message += "\n--- Hits ---\n" + json.dumps(hits, indent=2)
            audit = result.get("_audit")
            if audit:
                output_message += "\n--- Audit ---\n" + json.dumps(audit, indent=2)
            return output_message
        else:
            # Unexpected success response format or executed flag missing/false
            logger.warning(f"execute_houdini_code received success status but unexpected result format: {response}")
            return f"Execution status unclear from Houdini response: {json.dumps(response)}"

    except ConnectionError as e:
         return f"Connection Error executing code: {str(e)}"
    except Exception as e:
        # Errors during communication or parsing in this script
        logger.error(f"Unexpected error in execute_houdini_code tool: {str(e)}", exc_info=True)
        return f"Server Error executing code: {str(e)}"


@mcp.tool()
def get_last_scene_diff(ctx: Context) -> str:
    """Return the last execute_code (capture_diff=True) scene before/after diff.

    The Houdini-side server caches the most recent serialize_scene_state pair;
    this tool fetches and pretty-prints the diff so the agent can verify what
    a privileged execution actually changed in the scene.
    """
    try:
        conn = get_houdini_connection()
        response = conn.send_command("get_last_scene_diff", {})

        if response.get("status") == "error":
            origin = response.get('origin', 'houdini')
            return f"Error ({origin}): {response.get('message', 'Unknown error')}"

        result = response.get("result", {}) or {}
        if not result.get("available", False):
            return ("No scene diff available yet. Run execute_houdini_code "
                    "with capture_diff=True first.")
        # Server (server.py get_last_scene_diff) returns
        # {available, changed, before, after}; align bridge field reads.
        payload = {
            "available": result.get("available"),
            "changed": result.get("changed"),
            "before": result.get("before"),
            "after": result.get("after"),
        }
        return json.dumps(payload, indent=2)
    except ConnectionError as e:
         return f"Connection Error getting scene diff: {str(e)}"
    except Exception as e:
        logger.error(f"Unexpected error in get_last_scene_diff tool: {str(e)}", exc_info=True)
        return f"Server Error getting scene diff: {str(e)}"


@mcp.tool()
def save_scene(ctx: Context, file_path: str) -> str:
    """Save the current Houdini scene to file_path.

    Returns JSON like {"saved": true, "file_path": "..."} or an error string.
    """
    return _houdini_call("save_scene", {"file_path": file_path})


@mcp.tool()
def load_scene(ctx: Context, file_path: str) -> str:
    """Load a .hip file as the current Houdini scene.

    Server-side also calls cmn.invalidate_all_caches() so downstream caches
    (NodeTypeCache coming in PR 6) reset on scene switch.
    """
    return _houdini_call("load_scene", {"file_path": file_path})


@mcp.tool()
def new_scene(ctx: Context) -> str:
    """Reset Houdini to an empty scene (suppress_save_prompt=True).

    Server-side also calls cmn.invalidate_all_caches().
    """
    return _houdini_call("new_scene", {})


@mcp.tool()
def serialize_scene(ctx: Context, root_path: str = "/obj",
                    include_params: bool = False,
                    max_depth: int = 10) -> str:
    """递归序列化 root_path 下的节点树为 dict。

    只读操作，AI 用于场景结构对比 / 文档生成。
    include_params=False 时每节点只含 path/type/name/children；
    True 时增加 parameters dict。
    """
    return _houdini_call("serialize_scene", {
        "root_path": root_path,
        "include_params": include_params,
        "max_depth": max_depth,
    })


# -------------------------------------------------------------------
# PR 6: Node Discovery & Cache Management Tools
# -------------------------------------------------------------------
@mcp.tool()
def list_node_types(ctx: Context, category: str = None,
                    name_filter: str = None, limit: int = 50,
                    cursor: int = None) -> dict:
    """List Houdini node types with optional category / name filter, paginated.

    PR 6: relays to server-side disc.list_node_types, which populates the
    NodeTypeCache on first call and reuses it across invocations.
    """
    return _houdini_call("list_node_types", {
        "category": category,
        "name_filter": name_filter,
        "limit": limit,
        "cursor": cursor,
    })


@mcp.tool()
def list_children(ctx: Context, node_path: str = "/",
                  recursive: bool = False, max_depth: int = 5,
                  max_nodes: int = 1000, compact: bool = False,
                  limit: int = 50, cursor: int = None) -> dict:
    """List the children of node_path. With recursive=True walk the subtree up
    to max_depth. compact=True returns only {path, type, children_count}.

    PR 6: relays to server-side disc.list_children.
    """
    return _houdini_call("list_children", {
        "node_path": node_path,
        "recursive": recursive,
        "max_depth": max_depth,
        "max_nodes": max_nodes,
        "compact": compact,
        "limit": limit,
        "cursor": cursor,
    })


@mcp.tool()
def find_nodes(ctx: Context, root_path: str = "/", pattern: str = None,
               node_type: str = None, limit: int = 50,
               cursor: int = None) -> dict:
    """Find nodes under root_path matching a glob / substring pattern or
    node_type. Default root_path is "/".

    PR 6: relays to server-side disc.find_nodes.
    """
    return _houdini_call("find_nodes", {
        "root_path": root_path,
        "pattern": pattern,
        "node_type": node_type,
        "limit": limit,
        "cursor": cursor,
    })


@mcp.tool()
def manage_cache(ctx: Context, action: str = "stats") -> dict:
    """Manage the Houdini-side NodeTypeCache.

    action="stats"     -> return cache hits/misses/size/last_populated_at
    action="invalidate"-> clear all registered caches (calls
                          cmn.invalidate_all_caches under the hood)
    action="warmup"    -> pre-populate the NodeTypeCache

    PR 6: relays to server-side disc.manage_cache. ValueError on unknown
    action surfaces as an error dict with origin="houdini".
    """
    return _houdini_call("manage_cache", {"action": action})

# -------------------------------------------------------------------
# Graph Editing & Introspection Tools
# -------------------------------------------------------------------

def _houdini_call(cmd_type: str, params: Dict[str, Any] = None) -> dict:
    """Relay a command to Houdini and normalize the response envelope."""
    try:
        conn = get_houdini_connection()
        response = conn.send_command(cmd_type, params or {})
    except ConnectionError as e:
        return {"status": "error", "message": str(e), "origin": "connection"}
    except Exception as e:
        logger.error(f"Bridge error relaying '{cmd_type}': {e}", exc_info=True)
        return {"status": "error", "message": str(e), "origin": "mcp_bridge"}

    if response.get("status") == "error":
        return {
            "status": "error",
            "message": response.get("message", "Unknown error"),
            "origin": response.get("origin", "houdini"),
        }
    return {"status": "success", "result": response.get("result", {})}


@mcp.tool()
def connect_nodes(ctx: Context, from_path: str, to_path: str,
                  input_index: int = 0, output_index: int = 0) -> dict:
    """
    Wire one node's output into another node's input. Both nodes must live in
    the same network. input_index selects which input of to_path to connect
    (0-based); output_index selects which output of from_path to use.
    """
    return _houdini_call("connect_nodes", {
        "from_path": from_path,
        "to_path": to_path,
        "input_index": input_index,
        "output_index": output_index,
    })


@mcp.tool()
def disconnect_node_input(ctx: Context, path: str, input_index: int = 0) -> dict:
    """Disconnect one input of a node (reports what it was connected to)."""
    return _houdini_call("disconnect_input", {"path": path, "input_index": input_index})


@mcp.tool()
def delete_node(ctx: Context, path: str) -> dict:
    """Delete a node from the scene by path."""
    return _houdini_call("delete_node", {"path": path})


@mcp.tool()
def set_parameters(ctx: Context, path: str, parameters: Dict[str, Any]) -> dict:
    """
    Set one or more parameters on a node in a single undoable call.
    Values: scalar for single parms (e.g. {"scale": 2.0}), a list for parm
    tuples (e.g. {"t": [0, 1, 0]}), and menu token or label strings for menu
    parms. Unknown names fail per-parameter with did-you-mean suggestions —
    check the "failed" list in the result. Use get_parameter_schema first if
    unsure of names, types or valid menu values.
    """
    return _houdini_call("set_parameters", {"path": path, "parameters": parameters})


@mcp.tool()
def get_parameter_schema(ctx: Context, path: str, pattern: str = None,
                         offset: int = 0, limit: int = 50) -> dict:
    """
    Describe a node's parameters: names, labels, types, tuple sizes, current
    values, defaults, numeric ranges and menu options. Use this to discover
    valid parameter names/values before calling set_parameters. Filter with a
    glob pattern (e.g. "*scale*", matched against name and label); paginate
    with offset/limit when a node has many parameters.
    """
    params = {"path": path, "offset": offset, "limit": limit}
    if pattern:
        params["pattern"] = pattern
    return _houdini_call("get_parameter_schema", params)


@mcp.tool()
def set_node_flags(ctx: Context, path: str, display: bool = None,
                   render: bool = None, bypass: bool = None,
                   template: bool = None) -> dict:
    """
    Set node flags (display/render/bypass/template). Only flags you pass are
    changed. Flags a node type doesn't support are reported as 'unsupported'.
    """
    return _houdini_call("set_node_flags", {
        "path": path, "display": display, "render": render,
        "bypass": bypass, "template": template,
    })


@mcp.tool()
def layout_network(ctx: Context, path: str) -> dict:
    """Auto-layout all children of a network node for a tidy graph."""
    return _houdini_call("layout_children", {"path": path})


# PR 11 Error Nodes
# ---------------------------------------------------------------------------


@mcp.tool()
def find_error_nodes(ctx, root_path="/", include_warnings=True,
                     max_warnings=50, max_errors=None):
    """扫描场景中的错误与警告节点。

    从 root_path 出发，单次调用 node.allSubChildren() 收集所有后代节点，
    返回 errors 与 warnings 双列表。include_warnings 默认 True（PR 11 行为）；
    max_warnings 限制警告条目数（超过返 _warnings_truncated 标记）；
    max_errors 限制错误条目数（None 表示不限）。适合场景构建完成后做
    一次性体检，比逐节点 cook_node 更快。
    """
    return _houdini_call("find_error_nodes", {
        "root_path": root_path,
        "include_warnings": include_warnings,
        "max_warnings": max_warnings,
        "max_errors": max_errors,
    })


# ---------------------------------------------------------------------------
# PR 12 Geometry Summary (thin relay to server-side _geo_summary)
# ---------------------------------------------------------------------------


@mcp.tool()
def get_geo_summary(ctx, node_path, max_points_for_full=1000000,
                    sample_size=10):
    """获取几何节点的轻量级概要信息。

    返回 SOP 节点的 point / primitive / vertex 计数、bbox 6 元、attributes /
    groups 列表（带 name/type/size），以及前 sample_size 个点的属性采样。
    point_count 超过 max_points_for_full 时自动降级 — 跳过 sample_points 与
    详细 attributes/groups，避免大几何撑爆 MCP。比 get_geometry_info 更轻，
    比 get_geometry_data 更结构化。适用于“先看看节点生成了什么规模的几何”。
    """
    return _houdini_call("get_geo_summary", {
        "node_path": node_path,
        "max_points_for_full": max_points_for_full,
        "sample_size": sample_size,
    })


@mcp.tool()
def cook_node(ctx: Context, path: str) -> dict:
    """
    Force-cook a node and report whether it cooked cleanly, with errors,
    warnings and cook time. The definitive way to verify a node works.
    """
    return _houdini_call("cook_node", {"path": path})


@mcp.tool()
def create_wrangle(ctx: Context, parent_path: str, vex_code: str,
                   name: str = None, run_over: str = "points",
                   input_node: str = None) -> dict:
    """
    Create an Attribute Wrangle SOP with the given VEX snippet, optionally
    wiring input_node into its first input. run_over: points, primitives,
    vertices, detail or numbers. The node is cooked immediately and the
    result includes a 'validation' report — check it for VEX compile errors
    before building on top. On invalid input the node is removed, never left
    half-configured.
    """
    params = {"parent_path": parent_path, "vex_code": vex_code, "run_over": run_over}
    if name:
        params["name"] = name
    if input_node:
        params["input_node"] = input_node
    return _houdini_call("create_wrangle", params)


@mcp.tool()
def set_wrangle_code(ctx: Context, path: str, vex_code: str,
                     validate: bool = True) -> dict:
    """
    Replace the VEX snippet on an existing wrangle node. With validate=True
    (default) the node is re-cooked and the result includes a 'validation'
    report with any VEX compile errors.
    """
    return _houdini_call("set_wrangle_code", {
        "path": path, "vex_code": vex_code, "validate": validate,
    })


@mcp.tool()
def get_geometry_info(ctx: Context, path: str) -> dict:
    """
    Summarize a node's geometry: point/primitive/vertex counts, bounding box,
    attribute listings per class, and group names. Accepts a SOP path or a
    geometry container (its display SOP is used). Use this to verify what a
    network actually produced instead of judging from a render.
    """
    return _houdini_call("get_geometry_info", {"path": path})


@mcp.tool()
def get_geometry_data(ctx: Context, path: str, element: str = "points",
                      attributes: List[str] = None, start: int = 0,
                      limit: int = 100) -> dict:
    """
    Read actual attribute values from geometry, paginated (limit capped at
    500 — use start to page through large geometry). element: 'points' or
    'primitives'. attributes: names to read (default: P for points); call
    get_geometry_info first to see what exists.
    """
    params = {"path": path, "element": element, "start": start, "limit": limit}
    if attributes:
        params["attributes"] = attributes
    return _houdini_call("get_geometry_data", params)


# -------------------------------------------------------------------
# NEW rendering Tools
# -------------------------------------------------------------------
@mcp.tool()
def render_single_view(ctx: Context,
                       orthographic: bool = False,
                       rotation: List[float] = [0, 90, 0],
                       render_path: str = "C:/temp/",
                       render_engine: str = "opengl",
                       karma_engine: str = "cpu",
                       consent_token: str = None) -> dict:
    """
    IMPORTANT (fork-render-policy-redirect-and-consent):
        在用户机 H21 缺 OGL 3.3 环境下，本工具的 opengl renderer 已被 fork
        强制 redirect 到 ``capture_pane_screenshot(SceneViewer)``（不再
        触发 opengl output node 链路，避免 Houdini 主线程死锁）；karma_cpu /
        karma_xpu renderer 需带 ``consent_token`` 重调，token 在首次调用返
        回的 ``_interrupt`` 字段中获得。详见 ``_render_policy.py``。

    Render a single view inside Houdini and return a structured result dict.

    Returns a dict (carrying renderer / image_path / size_bytes / etc.)
    instead of a string. Pydantic-typed MCP output models reject dicts
    when the return annotation is `str`; this tool is the one that broke
    live with `1 validation error for render_single_viewOutput / result
    Input should be a valid string [type=string_type, input_type=dict]`.
    Server-side always returns a dict; we forward it verbatim and only
    fall back to an error envelope on exception.
    """
    policy_resp = _apply_render_policy_to_engine(
        render_engine, karma_engine, consent_token=consent_token)
    if policy_resp is not None:
        return policy_resp
    try:
        conn = get_houdini_connection()
        response = conn.send_command("render_single_view", {
            "orthographic": orthographic,
            "rotation": rotation,
            "render_path": render_path,
            "render_engine": render_engine,
            "karma_engine": karma_engine,
        })

        if response.get("status") == "error":
            origin = response.get("origin", "houdini")
            return {"status": "error", "origin": origin,
                    "message": response.get("message", "Unknown error")}

        result = response.get("result")
        if isinstance(result, dict):
            return result
        return {"status": "unknown", "raw": str(result)}
    except Exception as e:
        logger.error(f"render_single_view failed: {e}", exc_info=True)
        return {"status": "error", "origin": "bridge",
                "message": f"Render failed: {str(e)}"}

@mcp.tool()
def render_quad_views(ctx: Context,
                      render_path: str = "C:/temp/",
                      render_engine: str = "opengl",
                      karma_engine: str = "cpu",
                      consent_token: str = None) -> dict:
    """
    IMPORTANT (fork-render-policy-redirect-and-consent):
        在用户机 H21 缺 OGL 3.3 环境下，本工具的 opengl renderer 已被 fork
        强制 redirect 到 ``capture_pane_screenshot(SceneViewer)``；karma_cpu
        / karma_xpu 需带 ``consent_token`` 重调。详见 ``_render_policy.py``。

    Render 4 canonical views from Houdini and return a structured result dict.

    Returns a dict (4 views × {image_path, size_bytes, ...}) instead of a
    string. See render_single_view docstring for the dict-vs-str Pydantic
    background. The legacy bridge command name is `render_quad_view`
    (singular) — kept for backward compatibility with the server-side
    handler dictionary in opera-houdini-mcp/server.py.
    """
    policy_resp = _apply_render_policy_to_engine(
        render_engine, karma_engine, consent_token=consent_token)
    if policy_resp is not None:
        return policy_resp
    try:
        conn = get_houdini_connection()
        response = conn.send_command("render_quad_view", {
            "render_path": render_path,
            "render_engine": render_engine,
            "karma_engine": karma_engine,
        })

        if response.get("status") == "error":
            origin = response.get("origin", "houdini")
            return {"status": "error", "origin": origin,
                    "message": response.get("message", "Unknown error")}

        result = response.get("result")
        if isinstance(result, dict):
            return result
        return {"status": "unknown", "raw": str(result)}
    except Exception as e:
        logger.error(f"render_quad_views failed: {e}", exc_info=True)
        return {"status": "error", "origin": "bridge",
                "message": f"Render failed: {str(e)}"}

@mcp.tool()
def render_specific_camera(ctx: Context,
                           camera_path: str,
                           render_path: str = "C:/temp/",
                           render_engine: str = "opengl",
                           karma_engine: str = "cpu",
                           consent_token: str = None) -> dict:
    """
    IMPORTANT (fork-render-policy-redirect-and-consent):
        在用户机 H21 缺 OGL 3.3 环境下，本工具的 opengl renderer 已被 fork
        强制 redirect 到 ``capture_pane_screenshot(SceneViewer)``；karma_cpu
        / karma_xpu 需带 ``consent_token`` 重调。详见 ``_render_policy.py``。

    Render from a specific camera path in the Houdini scene.

    Returns a structured dict (renderer / image_path / size_bytes) instead
    of a string. See render_single_view docstring for the dict-vs-str
    Pydantic background.
    """
    policy_resp = _apply_render_policy_to_engine(
        render_engine, karma_engine, consent_token=consent_token)
    if policy_resp is not None:
        return policy_resp
    try:
        conn = get_houdini_connection()
        response = conn.send_command("render_specific_camera", {
            "camera_path": camera_path,
            "render_path": render_path,
            "render_engine": render_engine,
            "karma_engine": karma_engine,
        })

        if response.get("status") == "error":
            origin = response.get("origin", "houdini")
            return {"status": "error", "origin": origin,
                    "message": response.get("message", "Unknown error")}

        result = response.get("result")
        if isinstance(result, dict):
            return result
        return {"status": "unknown", "raw": str(result)}
    except Exception as e:
        logger.error(f"render_specific_camera failed: {e}", exc_info=True)
        return {"status": "error", "origin": "bridge",
                "message": f"Render failed: {str(e)}"}

# -------------------------------------------------------------------
# NEW OPUS API Tools
# -------------------------------------------------------------------

@mcp.tool()
def opus_get_model_names(ctx: Context) -> List[str]:
    """
    Returns a list of available OPUS component/structure names.
    """
    # Currently uses the hardcoded list from helpers
    return get_all_component_names()

@mcp.tool()
def opus_get_model_params_schema(ctx: Context, structure: str) -> dict:
    """
    Retrieves the parameter schema or format instructions for a given OPUS model structure.
    Returns a dictionary, which might contain 'result' (JSON schema) or 'result_format_instructions' (string).
    Check 'statusCode' for success (200) or failure (e.g., 500).
    """
    if not structure:
        return {"statusCode": 400, "error": "Structure name cannot be empty."}
    # This function now returns a dict with statusCode and result/error
    return get_formatted_opus_params(structure)

@mcp.tool()
def opus_create_model(ctx: Context, structure: str, parameters: Dict[str, Any], count: int = 1) -> dict:
    """
    Starts a batch job to create one or more 3D models using the OPUS API.
    Requires the model structure name and a dictionary of parameters.
    Returns a dictionary containing the 'batch_id' on success (statusCode 200) or an error message.
    """
    if not structure:
        return {"statusCode": 400, "error": "Structure name cannot be empty."}
    if not isinstance(parameters, dict):
         return {"statusCode": 400, "error": "Parameters must be a valid JSON object (dict)."}
    if not isinstance(count, int) or count < 1:
         return {"statusCode": 400, "error": "Count must be a positive integer."}
         
    # This function handles API call and returns dict with statusCode and batch_id/error
    return create_opus_component(structure, parameters, count)

@mcp.tool()
def opus_variate_model(ctx: Context, result_id: str, count: int = 12) -> dict:
    """
    Starts a batch job to create variations of an existing OPUS model result.
    Requires the result_id of the base model.
    Returns a dictionary containing the 'batch_id' on success (statusCode 200) or an error message.
    """
    if not result_id:
        return {"statusCode": 400, "error": "Result ID cannot be empty."}
    if not isinstance(count, int) or count < 1:
         return {"statusCode": 400, "error": "Count must be a positive integer."}

    # This function handles API call and returns dict with statusCode and batch_id/error
    return variate_opus_result(result_id, count)

# -------------------------------------------------------------------
# NEW Tools Forwarding to Houdini for OPUS Job Handling
# -------------------------------------------------------------------

@mcp.tool()
def opus_check_job_status(ctx: Context, batch_id: str) -> dict:
    """
    Checks the status of an OPUS batch job directly via the API.
    Requires the batch_id returned by opus_create_model or opus_variate_model.
    Returns the JSON response from the OPUS API, including status and potential download URLs, or an error dictionary.
    """
    if not batch_id:
        return {"error": "Batch ID cannot be empty."}
    
    # Call the helper function directly
    result = get_opus_job_result(batch_job_id=batch_id)
    return result # Return the dictionary (contains result or error)

@mcp.tool()
def opus_import_model_url(ctx: Context, download_url: str, node_name: str = None) -> str:
    """
    Asks Houdini to download a model (zip containing USD) from a URL and import it into the scene.
    Requires the download URL (likely obtained from opus_check_job_status).
    Optionally specify a base name for the new container node.
    (Houdini needs a corresponding 'import_opus_url' command handler)
    """
    if not download_url:
        return "Error: Download URL cannot be empty."
    try:
        conn = get_houdini_connection()
        params = {"url": download_url}
        # Use provided name or generate one from URL
        if node_name:
             params["node_name"] = node_name
        else:
             try:
                 from urllib.parse import urlparse as _urlparse
                 parsed_name = os.path.splitext(os.path.basename(_urlparse(download_url).path))[0]
                 params["node_name"] = parsed_name if parsed_name else "opus_import"
             except Exception:
                 params["node_name"] = "opus_import"
             
        logger.info(f"Requesting Houdini import: URL={download_url}, NodeName={params['node_name']}")
        # Send command to Houdini's server.py
        response = conn.send_command("import_opus_url", params)

        if response.get("status") == "error":
            origin = response.get('origin', 'houdini')
            return f"Error ({origin}) importing model: {response.get('message', 'Unknown error')}"

        # Assuming success returns a dict in 'result' with import info (e.g., new node path)
        result_data = response.get('result', {})
        return f"Import Result: {json.dumps(result_data)}"

    except ConnectionError as e:
         return f"Connection Error importing model: {str(e)}"
    except Exception as e:
        logger.error(f"Unexpected error in opus_import_model_url tool: {str(e)}", exc_info=True)
        return f"Server Error importing model: {str(e)}"

# --- Add get_opus_job_result helper (Updated for RapidAPI) --- 
def get_opus_job_result(batch_job_id: str) -> dict:
    """
    Query OPUS API via RapidAPI for latest job info (including download URLs).
    Uses GET_JOB_RESULT_URL constructed from RapidAPI env vars.
    Returns the JSON response as a dictionary.
    On error, returns a dictionary with an 'error' key.
    """
    if not RAPIDAPI_HOST_URL: # Check RapidAPI config
        return {"error": "RAPIDAPI_HOST_URL not configured."}
    if not batch_job_id:
        return {"error": "batch_job_id cannot be empty."}
        
    url = GET_JOB_RESULT_URL # Use RapidAPI URL
    params = { "result_uid": batch_job_id } # Parameter name from user example, check RapidAPI docs
    headers = { 
        "accept": "application/json",
        'x-rapidapi-host': RAPIDAPI_HOST,
        'x-rapidapi-key': RAPIDAPI_KEY
    }
    try:
        logger.info(f"Querying job status (RapidAPI): URL={url}, Params={params}")
        resp = requests.get(url, params=params, headers=headers, timeout=TIMEOUT)
        resp.raise_for_status() # Raise HTTPError for bad responses (4xx or 5xx)
        return resp.json()
    except requests.exceptions.HTTPError as e:
        logger.error(f"RapidAPI Error {e.response.status_code} getting job result: {e.response.text}")
        try:
             # Return the error structure from the API if possible
             return {"error": e.response.json(), "status_code": e.response.status_code} 
        except json.JSONDecodeError:
             return {"error": f"RapidAPI Error {e.response.status_code}: {e.response.text}", "status_code": e.response.status_code}
    except requests.exceptions.RequestException as e:
        logger.error(f"RapidAPI request failed getting job result: {str(e)}")
        return {"error": f"RapidAPI request failed: {str(e)}"}
    except json.JSONDecodeError as e:
        logger.error(f"Failed to decode job status RapidAPI response: {str(e)}")
        return {"error": "Failed to decode job status RapidAPI response."}
# --- End get_opus_job_result helper ---


# ... (rest of existing code, main function etc) ...


# -------------------------------------------------------------------
# PR 8 HScript Tools (thin relay to server-side _hscript)
# -------------------------------------------------------------------
@mcp.tool()
def execute_hscript(ctx, code):
    """在 Houdini 中执行 HScript 命令字符串。

    HScript 是 Houdini 的传统脚本语言（与 Python/HScript 两套接口并存），
    适合执行 `ls`、`cd`、`opset` 等内建命令。调用结果以 stdout / stderr
    形式返回。

    参数说明：
    - code: HScript 命令字符串（如 "cd /obj; ls"）。空字符串 / 纯空白
      会被服务端拒绝并返回错误。

    返回字符串包含 stdout / stderr 两段；连接或服务端出错时返回
    "Error (...): ..." 形式的提示。
    """
    try:
        conn = get_houdini_connection()
        response = conn.send_command("execute_hscript", {"code": code})

        if response.get("status") == "error":
            origin = response.get('origin', 'houdini')
            return "Error ({0}): {1}".format(
                origin, response.get('message', 'Unknown error'))

        result = response.get("result", {}) or {}
        stdout = (result.get("stdout") or "").rstrip()
        stderr = (result.get("stderr") or "").rstrip()
        output_message = "HScript executed."
        if stdout:
            output_message += "\n--- Stdout ---\n{0}".format(stdout)
        if stderr:
            output_message += "\n--- Stderr ---\n{0}".format(stderr)
        return output_message
    except ConnectionError as e:
        return "Connection Error executing HScript: {0}".format(e)
    except Exception as e:
        logger.error("Unexpected error in execute_hscript tool: {0}".format(e),
                     exc_info=True)
        return "Server Error executing HScript: {0}".format(e)


# -------------------------------------------------------------------
# PR 9 Graph Edit Tools (thin relay to server-side _graph_edit)
# -------------------------------------------------------------------
@mcp.tool()
def reorder_inputs(ctx, node_path, new_order=None, order=None):
    """重新排列节点的输入顺序。

    参数说明：
    - node_path: 目标节点路径。
    - new_order: list of input_index，按新顺序排列（如 [2, 0, 1] 表示把
      原 input 2 移到 input 0，依此类推）。空 list 表示全部断开。
    - order: 旧版别名；若同时传 new_order 与 order，以 new_order 为准。

    返回 dict 包含 path / old_order / new_order / success 四项；
    节点不存在时函数会抛 ValueError，bridge 不会再以 success:True 形式
    静默吞错。
    """
    effective_order = new_order if new_order is not None else order
    return _houdini_call("reorder_inputs", {
        "node_path": node_path, "new_order": effective_order,
    })


@mcp.tool()
def layout_children(ctx, parent_path=None, parent=None,
                    horizontal_spacing=None, vertical_spacing=None,
                    direction=None):
    """布局父节点下的子节点（按间距参数手动 setPosition，跨 Houdini
    版本可移植）。

    参数说明：
    - parent_path: 父节点路径（PR 9 推荐命名）。
    - parent: 旧版别名；若同时传 parent_path 与 parent，以 parent_path 为准。
    - horizontal_spacing: 水平间距（Houdini units），缺省 2.0。
    - vertical_spacing: 垂直间距，缺省 1.5。
    - direction: "horizontal"（默认）或 "vertical"。

    返回 dict 包含 parent_path / children_count / direction / spacing
    四项。后向兼容：现有调用 layout_children(ctx, parent) 仍 work。
    """
    effective_parent = parent_path if parent_path is not None else parent
    if horizontal_spacing is not None or vertical_spacing is not None \
            or direction is not None:
        return _houdini_call("layout_children", {
            "parent_path": effective_parent,
            "horizontal_spacing": horizontal_spacing,
            "vertical_spacing": vertical_spacing,
            "direction": direction,
        })
    return _houdini_call("layout_children", {"parent_path": effective_parent})


@mcp.tool()
def set_node_position(ctx, node_path, x, y):
    """设置节点在 network editor 中的位置。

    参数说明：
    - node_path: 节点路径。
    - x: x 坐标（Houdini units）。
    - y: y 坐标。

    返回 dict 包含 path / position / success 三项；
    节点不存在时函数会抛 ValueError。
    """
    return _houdini_call("set_node_position", {
        "node_path": node_path, "x": x, "y": y,
    })


@mcp.tool()
def set_node_color(ctx, node_path, r, g, b):
    """设置节点颜色（颜色分量自动 clamp 到 [0, 1]）。

    参数说明：
    - node_path: 节点路径。
    - r, g, b: 颜色分量；负值 clamp 为 0.0，>1 值 clamp 为 1.0。

    返回 dict 包含 path / color / success 三项；
    节点不存在时函数会抛 ValueError。
    """
    return _houdini_call("set_node_color", {
        "node_path": node_path, "r": r, "g": g, "b": b,
    })


@mcp.tool()
def create_network_box(ctx, parent_path, name=None, node_paths=None):
    """在父节点下创建 network box（network editor 中的分组框）。

    参数说明：
    - parent_path: 父节点路径。
    - name: 可选，box 名；缺省时由 Houdini 自动命名。
    - node_paths: 可选，要包含到此 box 的节点路径列表；
      不存在的节点静默跳过，不抛错。

    返回 dict 包含 path / name / nodes_in_box 三项；
    父节点不存在时函数会抛 ValueError。
    """
    return _houdini_call("create_network_box", {
        "parent_path": parent_path, "name": name, "node_paths": node_paths,
    })


# -------------------------------------------------------------------
# PR 10 Node Info Tool (thin relay to server-side _node_info)
# -------------------------------------------------------------------
@mcp.tool()
def get_node_info(ctx, node_path, include_errors=True, force_cook=False,
                  include_input_details=False, compact=False):
    """获取节点的详细信息。

    参数说明：
    - node_path: 目标节点路径。
    - include_errors: 可选，是否包含 errors / warnings 字段，默认 True。
    - force_cook: 可选，读取前是否调 node.cook(force=True)，默认 False。
    - include_input_details: 可选，是否包含每个 input 的详细连接
      （用 node.inputConnectors() 一次性取），默认 False。
    - compact: 可选，是否仅返精简字段 path/type/counts（不含 parameters /
      errors / warnings），默认 False。

    返回 dict：compact=True 时仅含 path / type / children_count / input_count
    / output_count 五项；否则包含完整字段（详见 _node_info.get_node_info）。
    节点不存在时函数会抛 ValueError，bridge 透传 error envelope 不静默吞错。
    """
    return _houdini_call("get_node_info", {
        "node_path": node_path,
        "include_errors": include_errors,
        "force_cook": force_cook,
        "include_input_details": include_input_details,
        "compact": compact,
    })


# -------------------------------------------------------------------
# PR 13 Pane Capture Tools
# -------------------------------------------------------------------
@mcp.tool()
def capture_pane_screenshot(ctx, pane_type_name, save_path=None,
                            fit_contents=True):
    """截图指定类型 pane（NetworkEditor / SceneViewer / Compositor /
    ChannelEditor 等 30 种）。

    pane_type_name 必须是 hou.paneTabType 的合法属性名。save_path 为 None
    时不落盘，size_bytes 改用 QBuffer 估算。fit_contents=True 时先按
    pane 类型调用 homeAll() / curViewport().home() 把可视范围对齐。
    响应走 apply_response_cap。无 PySide 环境返回 _warning dict。
    """
    return _houdini_call("capture_pane_screenshot", {
        "pane_type_name": pane_type_name,
        "save_path": save_path,
        "fit_contents": fit_contents,
    })


@mcp.tool()
def list_visible_panes(ctx):
    """列出当前所有 desktop 中可见的 pane tab。

    返回 {desktop, pane_type, name, is_current} 四元组列表；is_current
    标记该 desktop 当前激活的 pane。只读操作，响应过 apply_response_cap。
    """
    return _houdini_call("list_visible_panes", {})


@mcp.tool()
def capture_multiple_panes(ctx, pane_types, save_dir):
    """批量截图多种 pane 到 save_dir（不存在会自动创建）。

    pane_types 是 pane 类型名列表；返回与 pane_types 等长的 result 列表，
    每条 {pane_type, save_path, success, error} 独立报告。任意一种 pane
    抛异常不影响其他 pane。响应过 apply_response_cap。
    """
    return _houdini_call("capture_multiple_panes", {
        "pane_types": pane_types,
        "save_dir": save_dir,
    })


@mcp.tool()
def render_node_network(ctx, node_path, fit_contents=True,
                        save_path=None):
    """定位到节点所在 NetworkEditor pane，cd 到节点，再截图。

    node_path 必须存在；fit_contents=True 时截图前调用 homeAll() 把可视
    范围对齐到节点子树。save_path=None 时不落盘（size_bytes 改用 QBuffer
    估算）。响应过 apply_response_cap。
    """
    return _houdini_call("render_node_network", {
        "node_path": node_path,
        "fit_contents": fit_contents,
        "save_path": save_path,
    })


# -------------------------------------------------------------------
# PR 14 Render Base64 Tools (placed before PR 7 so existing test_bridge_style
# PR 7 section probe does not pick them up — the probe scans all
# @mcp.tool() after the PR 7 header without an explicit upper bound)
# -------------------------------------------------------------------
@mcp.tool(name="render_viewport_base64")
def render_viewport_base64(ctx, camera_path=None, geometry_path=None,
                           renderer="opengl", resolution=(640, 480),
                           format="PNG", consent_token=None):
    """渲染单个 viewport 视角并以 base64 形式返回图像（PR 14）。

    IMPORTANT (fork-render-policy-redirect-and-consent):
        在用户机 H21 缺 OGL 3.3 环境下，本工具的 ``renderer="opengl"`` 已被
        fork 强制 redirect 到 ``capture_pane_screenshot(SceneViewer)``（返
        回 ``_redirect`` dict，不进实际 render 引擎调用链路）；``karma_cpu``
        / ``karma_xpu`` 需带 ``consent_token`` 重调，token 在首次调用返回
        的 ``_interrupt`` 字段中获得。详见 ``_render_policy.py``。

    renderer 支持 opengl / karma_cpu / karma_xpu 三选一；resolution 为
    (width, height) 元组；format 支持 PNG / JPEG。响应含 image_base64 字段
    与 size_bytes，响应整体过 apply_response_cap 截断大 payload。无 hou /
    PySide 环境返回 _warning dict。
    """
    policy_resp = _apply_render_policy_to_renderer(
        renderer, consent_token=consent_token)
    if policy_resp is not None:
        return policy_resp
    return _houdini_call("render_viewport_base64", {
        "camera_path": camera_path,
        "geometry_path": geometry_path,
        "renderer": renderer,
        "resolution": list(resolution) if isinstance(resolution, tuple)
        else resolution,
        "format": format,
    })


@mcp.tool(name="render_quad_views_base64")
def render_quad_views_base64(ctx, geometry_path=None, renderer="opengl",
                              resolution=(480, 360), format="PNG",
                              consent_token=None):
    """渲染四视图（top / front / side / perspective）并以 base64 形式返回
    4 张图（PR 14）。

    IMPORTANT (fork-render-policy-redirect-and-consent):
        opengl 已 redirect 到 ``capture_pane_screenshot(SceneViewer)``；
        karma_cpu / karma_xpu 需带 ``consent_token`` 重调。详见
        ``_render_policy.py``。

    共享 bbox + camera rig，每个视图旋转 null 节点切换视角。响应以
    top/front/side/perspective 四键分别承载 base64 字符串，整体过
    apply_response_cap。无 hou 环境返回 _warning dict。
    """
    policy_resp = _apply_render_policy_to_renderer(
        renderer, consent_token=consent_token)
    if policy_resp is not None:
        return policy_resp
    return _houdini_call("render_quad_views_base64", {
        "geometry_path": geometry_path,
        "renderer": renderer,
        "resolution": list(resolution) if isinstance(resolution, tuple)
        else resolution,
        "format": format,
    })


@mcp.tool(name="render_specific_camera_base64")
def render_specific_camera_base64(ctx, camera_path, resolution=(640, 480),
                                   format="PNG", renderer="opengl",
                                   consent_token=None):
    """渲染指定相机视角并以 base64 形式返回图像（PR 14）。

    IMPORTANT (fork-render-policy-redirect-and-consent):
        opengl 已 redirect 到 ``capture_pane_screenshot(SceneViewer)``；
        karma_cpu / karma_xpu 需带 ``consent_token`` 重调。详见
        ``_render_policy.py``。

    camera_path 必须指向 /obj 下已存在的相机节点；renderer 支持
    opengl / karma_cpu / karma_xpu 三选一。响应整体过 apply_response_cap
    截断大 payload。
    """
    policy_resp = _apply_render_policy_to_renderer(
        renderer, consent_token=consent_token)
    if policy_resp is not None:
        return policy_resp
    return _houdini_call("render_specific_camera_base64", {
        "camera_path": camera_path,
        "resolution": list(resolution) if isinstance(resolution, tuple)
        else resolution,
        "format": format,
        "renderer": renderer,
    })


# -------------------------------------------------------------------
# PR 16 Connection Diagnostic Tools (placed before PR 15 / PR 7 sections
# so existing test_bridge_style (PR 7) and test_help PR 15 probes — which
# scan @mcp.tool() strictly after their own header lines — do not pick it
# up; PR 16 ships its own AST probe in tests.test_connection)
# -------------------------------------------------------------------
@mcp.tool()
def check_connection(ctx):
    """检查 Houdini 端连接信息（PR 16 连接诊断）。

    返回 dict 包含 hou_version / hou_build / hip_file / hip_file_basename /
    is_untitled / node_count / desktop_count / _status 八个字段。返回结构
    与 server.py 中 HoudiniMCPServer.check_connection 保持一致。仅做只读
    查询，不会修改 .hip 文件、节点或网络；适合 AI agent 在长会话开头调用
    一次以获取当前 Houdini 版本与场景规模。
    """
    return _houdini_call("check_connection", {})


@mcp.tool()
def ping_houdini(ctx, timeout=5):
    """轻量级 Houdini 端 ping，验证响应时间（PR 16 连接诊断）。

    参数说明：
    - timeout: 最长等待秒数（默认 5），超过则 within_timeout=False

    返回 dict 包含 pong / elapsed_ms / within_timeout / hou_version 四项；
    hou 抛异常时返 pong=False 并带 error 字段。该 ping 不持久化新连接，
    只在既有 hou 上下文里调用一次 hou.version()；适合作为健康检查或
    网络抖动场景下的快速探测。注意：与 bridge 协议的 "ping" 命令不同，
    后者只验证 socket / 帧协议，本工具测量 Houdini 端的实际响应时间。
    """
    return _houdini_call("ping_houdini", {"timeout": timeout})


# -------------------------------------------------------------------
# PR 15 Help Tools (placed before PR 7 section so test_bridge_style PR 7
# probe — which scans all @mcp.tool() strictly after the "# PR 7 Materials
# Tools" header line — does not pick it up; the trailing "Tools" also makes
# the PR 14 probe's "next section header" regex stop here)
# -------------------------------------------------------------------
@mcp.tool()
def get_houdini_help(ctx, help_type, item_name, timeout=10):
    """从 SideFX 在线文档查询 Houdini 节点、VEX 函数或 hou 方法的帮助（PR 15）。

    help_type 支持 11 种："sop" / "obj" / "dop" / "cop2" / "chop" /
    "vop" / "lop" / "top" / "rop" / "vex_function" / "python_hou"。
    item_name 是节点名 / VEX 函数名 / hou 方法名。timeout 是 HTTP 请求
    超时秒数（默认 10）。返回 dict 包含 title / summary / parameters /
    inputs / outputs / methods / status 等字段，HTML 解析使用 stdlib
    html.parser（零新增 pip 依赖）。HTTP 4xx / 5xx / 网络错误 / 超时
    全部降级为 status=error，不抛异常。响应整体过 apply_response_cap。
    """
    return _houdini_call("get_houdini_help", {
        "help_type": help_type,
        "item_name": item_name,
        "timeout": timeout,
    })


# -------------------------------------------------------------------
# PR 18 Help Wrapper Tools (placed before PR 7 section so test_bridge_style
# PR 7 probe — which scans all @mcp.tool() strictly after the "# PR 7
# Materials Tools" header line — does not pick it up; the trailing "Tools"
# also makes the PR 14 probe's "next section header" regex stop here)
# -------------------------------------------------------------------
@mcp.tool()
def verify_hou_api(ctx, item_name, help_type="python_hou", timeout=10):
    """AI-friendly wrapper over get_houdini_help（PR 18）。

    参数说明：
    - item_name: 要查询的 hou API / 节点 / VEX 函数名，如
      "ObjNode.setDisplayNode" 或 "Node.setInput"。
    - help_type: 可选，帮助类型，默认 "python_hou"；其他支持值见
      get_houdini_help（sop / obj / dop / cop2 / chop / vop / lop /
      top / rop / vex_function）。
    - timeout: 可选，HTTP 请求超时秒数，默认 10。

    返回 dict 包含 title / summary / parameters / inputs / outputs /
    methods / status 等字段，并在响应末尾附 `_ai_hint` 字段，给 AI
    一个可直接使用的简短提示（命中方法签名 / F-C pattern /
    SideFX 不可达 fallback）。响应整体过 apply_response_cap。
    """
    return _houdini_call("verify_hou_api", {
        "item_name": item_name,
        "help_type": help_type,
        "timeout": timeout,
    })


# -------------------------------------------------------------------
# PR 7 Materials Tools (thin relay to server-side _materials)
# -------------------------------------------------------------------
@mcp.tool()
def create_material(ctx, material_type,
                    name=None, parent_path="/mat",
                    parameters=None):
    """在 Houdini 中创建一个材质节点并返回节点信息。

    参数说明：
    - material_type: 材质节点类型，如 "principledshader"、"vopsurface"
    - name: 可选，节点名；缺省时由 Houdini 自动命名
    - parent_path: 可选，父节点路径，默认 "/mat"；不存在时回退到 /mat
    - parameters: 可选，dict 按 parm 名设置参数值；不存在的 parm 名会
      静默跳过（不影响调用）

    返回 dict 包含 path / type / name / parameters_set 四项，
    parameters_set 列出已尝试设置的 parm 名（含静默跳过的）。
    """
    return _houdini_call("create_material", {
        "material_type": material_type,
        "name": name,
        "parent_path": parent_path,
        "parameters": parameters or {},
    })


@mcp.tool()
def assign_material(ctx, geometry_path,
                    material_path, group=None):
    """把 material_path 处的材质绑定到 geometry_path 处的几何节点。

    参数说明：
    - geometry_path: SOP / OBJ 几何节点路径
    - material_path: 材质节点路径
    - group: 可选，指定要绑定到的 group 名（如 primitive / point group）；
      传 None 时整节点绑定，传具体名字时仅绑定到该 group

    返回 dict 包含 geometry_path / material_path / group / success；
    绑定失败时函数会抛 ValueError，bridge 不会再以 success:True 形式
    静默吞错。
    """
    return _houdini_call("assign_material", {
        "geometry_path": geometry_path,
        "material_path": material_path,
        "group": group,
    })


@mcp.tool()
def get_material_info(ctx, material_path):
    """获取材质节点的详细信息。

    返回 dict 包含 path / type / name / parameters / texture_references
    五项。parameters 仅保留 _materials.MATERIAL_PARM_WHITELIST 中列出的
    50+ parm，过滤后键集合稳定跨材质类型一致；texture_references 列出
    eval 值匹配已知贴图后缀（.png / .jpg / .jpeg / .exr / .hdr / .tif /
    .tiff / .rat / .tex）的 parm。
    """
    return _houdini_call("get_material_info", {"material_path": material_path})


def main():
    """Run the MCP server on stdio."""
    global _houdini_port

    parser = argparse.ArgumentParser(description='Houdini MCP Server Bridge')
    parser.add_argument('--port', type=int, default=9876,
                        help='Port to connect to Houdini (default: 9876)')
    args = parser.parse_args()
    _houdini_port = args.port
    logger.info(f"Configured to connect to Houdini on port {_houdini_port}")

    # Check necessary RapidAPI variables are set before running
    if not RAPIDAPI_HOST_URL or not RAPIDAPI_HOST or not RAPIDAPI_KEY:
         logger.warning("RAPIDAPI_HOST_URL, RAPIDAPI_HOST, or RAPIDAPI_KEY not configured. OPUS API features will be disabled.")
         logger.warning("To enable OPUS features, configure your RapidAPI key in urls.env")
         # Don't exit - allow server to start without OPUS features
    else:
        logger.info(f"Using RapidAPI Host URL: {RAPIDAPI_HOST_URL}")
        logger.info(f"Using RapidAPI Host Header: {RAPIDAPI_HOST}")

    logger.info(f"Langchain available: {LANGCHAIN_AVAILABLE}")
    mcp.run()

if __name__ == "__main__":
    main()
