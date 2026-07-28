"""_opus.py — OPUS RapidAPI 可选模块（refactor-opus-optional-and-debt-cleanup）。

把原内联在 bridge ``houdini_mcp_server.py`` 的 OPUS RapidAPI helper 拆为
独立可选模块。顶层仅 stdlib 常量 / catalog，**不** import requests /
dotenv / langchain，使无 RapidAPI key（亦无 langchain）的默认安装仍能启动
bridge 并提供 ``opus_get_model_names`` / ``opus_import_model_url`` 等无 key
工具。

四个真正发 RapidAPI 请求的入口（schema / create / variate / check-status）
按 ``_load_config()`` 检查 RAPIDAPI_HOST_URL / HOST / KEY：配置不全返回稳定
disabled error，且不触发 requests / langchain import。配置完整后才在 HTTP
helper 内 ``import requests``；schema 格式化时才尝试 import langchain（含
raw JSON 降级，不影响另外三个 API 工具）。

兼容约束（R1 不改对外 API 契约 / R4 零新 pip）：
- 公开参数与返回结构与原 bridge 内联实现逐函数一致。
- dotenv 先 ``load_dotenv(<module>/urls.env, override=False)`` 再读环境变量；
  process environment 优先于文件（override=False 仅填充未设置变量）。
- ``urls.env`` 不存在或 dotenv 不可用时静默，环境变量仍可单独配置。

模块不 import hou，可被 bridge 在 package / flat 两种布局下容错加载。
"""
import json
import logging
import os
from urllib.parse import urljoin


logger = logging.getLogger("HoudiniMCP_OPUS")

# 模块所在目录（与原 bridge script_dir 一致），用于定位 urls.env
_MODULE_DIR = os.path.dirname(os.path.abspath(__file__))

# OPUS endpoint 路径常量（与原 bridge 完全一致，仅迁入本模块）
GET_ATTRIBUTES_PATH = "/get_attributes_with_name"
CREATE_BATCH_PATH = "/create_opus_batch_component"  # Use batch endpoint
CREATE_COMPONENT_PATH = "/create_opus_component"
VARIATE_PATH = "/variate_opus_result"
GET_JOB_RESULT_PATH = "/get_opus_job_result"

TIMEOUT = 15  # seconds for RapidAPI


def get_all_component_names():
    # result = ["Sofa", "Chair", "Table", "CoffeeTable"] # Original subset
    result = [
        "Sofa", "Chair", "Table", "CoffeeTable",
         "Library", "StreetBench", "StreetLamp", "MailboxStandalone",
         "AntennaStandalone", "ParkingMeterStandalone", "AirConditionerStandalone",
         "BasketballHoop", "BusStop", "FloorLamp", "Bed", "TvUnit",
         "Sewer", "GarageDoorStandalone",
    ]  # User provided list
    return result


def _load_config():
    """加载 RapidAPI 配置；延迟 import dotenv，先读 urls.env 再读环境变量。

    process environment 优先于 urls.env 文件（``load_dotenv(override=False)``
    语义：仅填充未设置的环境变量，不覆盖已存在的）。

    Returns:
        dict: ``{"host_url": ..., "host": ..., "key": ..., "urls": {...}}``；
        配置不全时 ``urls`` 为 None，对应项为 None。
    """
    dotenv_path = os.path.join(_MODULE_DIR, "urls.env")
    try:
        from dotenv import load_dotenv
        if os.path.exists(dotenv_path):
            load_dotenv(dotenv_path=dotenv_path, override=False)
    except ImportError:
        # python-dotenv 是必需依赖；缺失时静默，环境变量仍可单独配置
        pass
    host_url = os.getenv("RAPIDAPI_HOST_URL")
    host = os.getenv("RAPIDAPI_HOST")
    key = os.getenv("RAPIDAPI_KEY")
    urls = None
    if host_url and host and key:
        urls = {
            "get_attributes": urljoin(host_url, GET_ATTRIBUTES_PATH),
            "create_component": urljoin(host_url, CREATE_COMPONENT_PATH),
            "variate": urljoin(host_url, VARIATE_PATH),
            "get_job_result": urljoin(host_url, GET_JOB_RESULT_PATH),
        }
    return {"host_url": host_url, "host": host, "key": key, "urls": urls}


def is_configured():
    """检查三项 RapidAPI 配置是否齐全（不 import requests / langchain）。

    Returns:
        bool: True 表示三项配置均存在，可走 RapidAPI 调用链。
    """
    cfg = _load_config()
    return bool(cfg["host_url"] and cfg["host"] and cfg["key"])


# --- Minimal api.utils.fix_rgb replication ---
# Assume it takes a list/tuple and returns [r, g, b] if valid, else None
def fix_rgb(color_val):
    if isinstance(color_val, (list, tuple)) and len(color_val) == 3:
        try:
            # Ensure they are numbers (int or float) and within typical 0-255 or 0-1 range
            # For simplicity, just check if they are numbers. API might expect 0-255 ints.
            rgb = [float(c) for c in color_val]
            # Basic check - could add range validation 0-255 or 0-1 if needed
            return rgb  # Returning as floats for now
        except (ValueError, TypeError):
            return None
    return None
# --- End utils replication ---


# --- OPUS Helper Functions (Updated for RapidAPI) ---
def get_struct_params(struct):
    if not is_configured():
        return False, {"error": "RAPIDAPI_HOST_URL not configured"}
    cfg = _load_config()
    import requests
    url = cfg["urls"]["get_attributes"]
    payload = {}  # GET request, params in URL
    params = {"name": struct}
    headers = {
        'x-rapidapi-host': cfg["host"],
        'x-rapidapi-key': cfg["key"],
    }
    try:
        response = requests.request("GET", url, headers=headers, params=params, data=payload, timeout=TIMEOUT)
        if str(response.status_code).startswith("2"):
            r = response.json()
            struct_result = r.get(struct)  # Check if response structure changed
            if struct_result:
                return True, struct_result
            elif isinstance(r, dict) and not struct_result:  # Maybe the top-level key is gone?
                if struct in r.get("result", {}):  # Check common patterns
                    return True, r["result"]
                else:
                    # Fallback: return the whole response if structure unclear but success
                    logger.warning("Structure '{0}' key not found directly in RapidAPI response, returning full JSON: {1}".format(struct, r))
                    return True, r
            else:
                return False, {"error": "Structure '{0}' not found in RapidAPI response: {1}".format(struct, r)}
        else:
            return False, {"error": "RapidAPI Error {0}: {1}".format(response.status_code, response.text)}
    except requests.exceptions.RequestException as e:
        return False, {"error": "RapidAPI request failed: {0}".format(str(e))}


def _get_langchain_parsers():
    """延迟尝试 import langchain 输出解析器；不可用返回 None。

    优先 langchain_classic，回退 langchain；均不可用时 schema 工具走 raw
    JSON 降级，不影响另外三个 API 工具。
    """
    try:
        from langchain_classic.output_parsers import ResponseSchema, StructuredOutputParser
        return ResponseSchema, StructuredOutputParser
    except ImportError:
        try:
            from langchain.output_parsers import ResponseSchema, StructuredOutputParser
            return ResponseSchema, StructuredOutputParser
        except ImportError:
            return None


def format_params(opus_response):
    formatted = {}
    # Adjust based on actual RapidAPI response structure if needed
    # Attempt 1: Original structure
    for asset_key, asset_data in opus_response.items():
        if isinstance(asset_data, dict) and "assets" in asset_data:
             for element in asset_data.get("assets", []):
                name = element.get("name")
                params = element.get("parameters", [])
                if not name:
                    continue
                for p in params:
                    pname = p.get("name")
                    prange = p.get("range")
                    ptype = p.get("type")
                    if pname and prange is not None and ptype is not None:
                         formatted["{0}/{1}".format(name, pname)] = (prange, ptype)

    # Attempt 2: If assets are directly under top level (heuristic)
    if not formatted and "assets" in opus_response and isinstance(opus_response["assets"], list):
        logger.warning("format_params: Using fallback structure parsing (assets at top level).")
        for element in opus_response.get("assets", []):
            name = element.get("name")
            params = element.get("parameters", [])
            if not name:
                continue
            for p in params:
                pname = p.get("name")
                prange = p.get("range")
                ptype = p.get("type")
                if pname and prange is not None and ptype is not None:
                        formatted["{0}/{1}".format(name, pname)] = (prange, ptype)

    # Attempt 3: If params are directly under top level (another heuristic)
    elif not formatted and "parameters" in opus_response and isinstance(opus_response["parameters"], list):
        logger.warning("format_params: Using fallback structure parsing (parameters at top level).")
        pass  # Add logic if this structure is encountered

    if not formatted:
         logger.warning("format_params: Could not extract parameters from response: {0}".format(opus_response))

    return formatted


def get_color_params(component_name, opus_asset_keys):
    result = {}
    # Component level color
    result.setdefault(
        "{0}/color_rgb".format(component_name),
        (
            "List[float]",  # Assuming List[float] based on fix_rgb output
            "Valid RGB color [R, G, B] (values likely 0-1 or 0-255, check API docs). Use if the user sets the entire color of the {0} or provided a single color without specifying a part.".format(component_name),
        ),
    )
    # Asset level colors
    for asset in opus_asset_keys:
        result.setdefault(
            "{0}/color_rgb".format(asset),
            (
                "List[float]",  # Assuming List[float]
                "Valid RGB color [R, G, B] for the {0} part. Use if user set the color of this specific part of the {1}.".format(asset, component_name),
            ),
        )
    return result


def get_param_json(param_json, color_params):
    parsers = _get_langchain_parsers()
    if parsers is None:
        # Fallback: simple JSON representation if Langchain is missing
        combined = {}
        for key, value in param_json.items():
            combined[key] = {"range": value[0], "type": value[1], "description": "Allowed range: {0}".format(value[0])}
        for key, value in color_params.items():
            combined[key] = {"type": value[0], "description": value[1]}
        return json.dumps(combined, indent=2)

    # Langchain way
    ResponseSchema, StructuredOutputParser = parsers
    response_schemas = []
    for key, value in param_json.items():
        response_schemas.append(
            ResponseSchema(name=key, description="Allowed range: {0}".format(value[0]), type=str(value[1]))  # Ensure type is string
        )
    for key, value in color_params.items():
        response_schemas.append(
            ResponseSchema(name=key, description=str(value[1]), type=str(value[0]))  # Ensure type is string
        )
    try:
        output_parser = StructuredOutputParser.from_response_schemas(response_schemas)
        prompt_var = output_parser.get_format_instructions(only_json=True)
        return prompt_var
    except Exception as e:
        logger.error("Langchain StructuredOutputParser failed: {0}".format(e))
        # Fallback if Langchain parsing fails
        combined = {key: {"range": value[0], "type": value[1]} for key, value in param_json.items()}
        combined.update({key: {"type": value[0], "description": value[1]} for key, value in color_params.items()})
        return json.dumps(combined, indent=2)


def get_formatted_opus_params(structure):
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
        status_code = 500  # Default error code
        if isinstance(structure_json, dict) and "error" in structure_json:
             if "RapidAPI Error 4" in structure_json["error"]:  # 粗略检查 4xx 错误
                  status_code = 400  # Or map specific codes if needed
             elif "RapidAPI Error 5" in structure_json["error"]:
                  status_code = 503  # Service unavailable or internal error

        return {"statusCode": status_code, "error": structure_json.get("error", "Unknown error retrieving parameters")}


def check_rgbs(structure, params):
    clean_params = {}
    if not isinstance(params, dict):
        return {}  # Guard against non-dict input
    for k, v in params.items():
        if "color_rgb" in k:
            # Handle simplified key case from get_color_params
            if k == "{0}/color_rgb".format(structure):
                valid_rgb = fix_rgb(v)
                if valid_rgb is not None:
                    clean_params[k] = valid_rgb  # Use the potentially simplified key
            elif "/" in k:  # Assume format like "asset/color_rgb"
                 valid_rgb = fix_rgb(v)
                 if valid_rgb is not None:
                    clean_params[k] = valid_rgb
        else:
            clean_params[k] = v
    return clean_params


def create_opus_batch(component_type, params, count=1):
    if not is_configured():
        return False, {"error": "RAPIDAPI_HOST_URL not configured"}
    cfg = _load_config()
    import requests
    url = cfg["urls"]["create_component"]  # Use the correct RapidAPI URL
    p = {
        "name": component_type,
        "parameters": params,
        "extensions": ["gltf"],  # Hardcoded GLTF for now
    }
    payload = json.dumps(p)
    headers = {
        'Content-Type': 'application/json',
        'x-rapidapi-host': cfg["host"],
        'x-rapidapi-key': cfg["key"],
    }
    try:
        response = requests.request("POST", url, headers=headers, data=payload, timeout=TIMEOUT)
        response.raise_for_status()  # Raise HTTPError for bad responses (4xx or 5xx)
        r = response.json()
        # Check response structure for batch_id (might be different from old API)
        batch_id = r.get("batch_job_id") or r.get("batch_id") or r.get("job_id")  # Check common keys
        if batch_id:
            return True, r  # Return the full response which contains the ID
        else:
             logger.error("RapidAPI batch creation success but no batch_id found in response: {0}".format(r))
             return False, {"error": "API succeeded but batch_id missing in response."}
    except requests.exceptions.HTTPError as e:
        logger.error("RapidAPI Error {0} creating batch: {1}".format(e.response.status_code, e.response.text))
        try:
             # Try to return the JSON error body if possible
             error_json = e.response.json()
             error_json["status_code"] = e.response.status_code  # Add status code for later use
             return False, error_json
        except json.JSONDecodeError:
             return False, {"error": "RapidAPI Error {0}: {1}".format(e.response.status_code, e.response.text), "status_code": e.response.status_code}
    except requests.exceptions.RequestException as e:
        logger.error("RapidAPI request failed creating batch: {0}".format(str(e)))
        return False, {"error": "RapidAPI request failed: {0}".format(str(e))}
    except json.JSONDecodeError as e:
        logger.error("Failed to decode RapidAPI response: {0}".format(str(e)))
        return False, {"error": "Failed to decode RapidAPI response."}


def create_opus_component(structure, params, count=1):
    # Ensure params is a dict
    if not isinstance(params, dict):
         return {"statusCode": 400, "error": "Parameters must be a valid JSON object (dict)."}

    clean_params = check_rgbs(structure, params)
    status, result_json = create_opus_batch(structure, clean_params, count)
    if status:
        # Extract batch ID (key might vary)
        batch_id = result_json.get("batch_job_id") or result_json.get("batch_id") or result_json.get("job_id")
        if batch_id:
             logger.info("OPUS (RapidAPI) batch job created: {0}".format(batch_id))
             # Return a consistent success structure
             return {"statusCode": 200, "batch_id": batch_id, "raw_response": result_json}
        else:
             # This case should be handled inside create_opus_batch now
             logger.error("API success but no batch_job_id found in response: {0}".format(result_json))
             return {"statusCode": 500, "error": "API succeeded but batch_id missing."}
    else:
        # result_json already contains the error from create_opus_batch
        return {"statusCode": result_json.pop("status_code", 500), **result_json}  # Use status_code if available


def variate_opus_result(result_id, count=12):
    if not is_configured():
        return {"statusCode": 500, "error": "RAPIDAPI_HOST_URL not configured"}
    cfg = _load_config()
    import requests
    url = cfg["urls"]["variate"]  # Use RapidAPI URL
    p = {
         "base_job_uid": result_id,  # Parameter name might change, check RapidAPI docs
         "count": count,
    }
    payload = json.dumps(p)
    headers = {
        'Content-Type': 'application/json',
        'x-rapidapi-host': cfg["host"],
        'x-rapidapi-key': cfg["key"],
    }
    try:
        response = requests.request("POST", url, headers=headers, data=payload, timeout=TIMEOUT)
        response.raise_for_status()
        result_json = response.json()
        # Extract batch_id (key might vary)
        batch_id = result_json.get("batch_job_id") or result_json.get("batch_id") or result_json.get("job_id")
        if batch_id:
            logger.info("OPUS (RapidAPI) variation batch job created: {0}".format(batch_id))
            return {"statusCode": 200, "batch_id": batch_id, "raw_response": result_json}
        else:
            logger.error("RapidAPI variation success but no batch_id found in response: {0}".format(result_json))
            return {"statusCode": 500, "error": "API variation succeeded but batch_id missing."}
    except requests.exceptions.HTTPError as e:
        logger.error("RapidAPI Error {0} creating variation: {1}".format(e.response.status_code, e.response.text))
        try:
             error_json = e.response.json()
             return {"statusCode": e.response.status_code, "error": error_json}
        except json.JSONDecodeError:
             return {"statusCode": e.response.status_code, "error": e.response.text}
    except requests.exceptions.RequestException as e:
        logger.error("RapidAPI request failed creating variation: {0}".format(str(e)))
        return {"statusCode": 500, "error": "Request failed: {0}".format(str(e))}
    except json.JSONDecodeError as e:
        logger.error("Failed to decode variation RapidAPI response: {0}".format(str(e)))
        return {"statusCode": 500, "error": "Failed to decode variation RapidAPI response."}


def get_opus_job_result(batch_job_id):
    """Query OPUS API via RapidAPI for latest job info (including download URLs).

    配置不全返回稳定 ``{"error": ...}``，不 import requests。
    """
    if not is_configured():
        return {"error": "RAPIDAPI_HOST_URL not configured."}
    if not batch_job_id:
        return {"error": "batch_job_id cannot be empty."}

    cfg = _load_config()
    import requests
    url = cfg["urls"]["get_job_result"]
    params = {"result_uid": batch_job_id}  # Parameter name from user example, check RapidAPI docs
    headers = {
        "accept": "application/json",
        'x-rapidapi-host': cfg["host"],
        'x-rapidapi-key': cfg["key"],
    }
    try:
        logger.info("Querying job status (RapidAPI): URL={0}, Params={1}".format(url, params))
        resp = requests.get(url, params=params, headers=headers, timeout=TIMEOUT)
        resp.raise_for_status()  # Raise HTTPError for bad responses (4xx or 5xx)
        return resp.json()
    except requests.exceptions.HTTPError as e:
        logger.error("RapidAPI Error {0} getting job result: {1}".format(e.response.status_code, e.response.text))
        try:
             # Return the error structure from the API if possible
             return {"error": e.response.json(), "status_code": e.response.status_code}
        except json.JSONDecodeError:
             return {"error": "RapidAPI Error {0}: {1}".format(e.response.status_code, e.response.text), "status_code": e.response.status_code}
    except requests.exceptions.RequestException as e:
        logger.error("RapidAPI request failed getting job result: {0}".format(str(e)))
        return {"error": "RapidAPI request failed: {0}".format(str(e))}
    except json.JSONDecodeError as e:
        logger.error("Failed to decode job status RapidAPI response: {0}".format(str(e)))
        return {"error": "Failed to decode job status RapidAPI response."}
