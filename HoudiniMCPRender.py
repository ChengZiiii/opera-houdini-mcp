import numpy as np
import math
import os
import tempfile
import hou
from . import _render_policy as _rp

# ======== 渲染临时节点治理（feat-mcp-round2-hardening §2）========
# 本模块渲染流程自建的临时节点精确名单：OBJ rig（相机 + 中心 null）与
# /out ROP 节点。名字前缀唯一（MCP_*），误删风险可控。
# - 渲染流程 finally 段按"本流程创建"清单清理（cleanup_temp_nodes）；
# - server.py 启动孤儿清扫共用该名单（上一会话残留）。
# 注意：karma ROP 名随 karma_engine 变化（MCP_<CPU|GPU>_KARMA）。
RENDER_TEMP_OBJ_PATHS = ("/obj/MCP_CAM_CENTER", "/obj/MCP_CAMERA")
RENDER_TEMP_OUT_NODE_NAMES = (
    "MCP_OGL_RENDER", "MCP_CPU_KARMA", "MCP_GPU_KARMA", "MCP_MANTRA")
RENDER_TEMP_OUT_PATHS = tuple(
    "/out/" + name for name in RENDER_TEMP_OUT_NODE_NAMES)
RENDER_TEMP_ALL_PATHS = RENDER_TEMP_OBJ_PATHS + RENDER_TEMP_OUT_PATHS


def cleanup_temp_nodes(node_paths):
    """按路径 best-effort 销毁渲染临时节点，返回失败描述列表。

    feat-mcp-round2-hardening §2：渲染流程 finally 段统一调用；任何
    单点失败都记录到返回列表而不是抛异常（清理失败 MUST NOT 吞掉
    渲染结果，由调用方把失败列表附到响应 _cleanup_warning）。
    节点已不存在（hou.node 返 None）视为清理成功。
    """
    failures = []
    for path in node_paths or ():
        try:
            node = hou.node(path)
            if node is None:
                continue
            node.destroy()
        except Exception as e:
            failures.append("{0}: {1}".format(path, e))
    return failures


def _finalize_render_cleanup(created_paths, cleanup_report=None):
    """渲染流程 finally 段统一清理入口。

    销毁 created_paths 里本流程创建的全部临时节点；失败描述写入
    cleanup_report（调用方传入 list 时），同时打日志。绝不抛异常。
    """
    failures = cleanup_temp_nodes(created_paths)
    if failures:
        for failure in failures:
            print("Warning: render temp node cleanup failed: " + failure)
        if cleanup_report is not None:
            cleanup_report.extend(failures)


def resolve_actual_backend(render_engine):
    """把 render_engine 映射为实际执行后端标识（feat-mcp-round2 §2c）。

    取值域：opengl_rop（opengl ROP 节点渲染）/ husk（karma ROP 交由
    husk CLI 离屏渲染）/ mantra_rop（H21 已移除 mantra，仅老版本
    ifd ROP 走 mantra-bin）。flipbook / qscreen_fallback 属
    _render_b64 的视口截图路径，不在本模块分支内。
    """
    engine = (render_engine or "opengl").lower()
    if engine == "karma":
        return "husk"
    if engine == "mantra":
        return "mantra_rop"
    return "opengl_rop"


def find_displayed_geometry():
    """Find all displayed geometry nodes in the scene."""
    displayed_geo = []
    
    # Iterate through all objects in /obj
    for node in hou.node("/obj").children():
        print(node.type().name())
        # Check if the node is a geometry node and is displayed
        if node.type().name() in ["geo", "subnet"] and node.isDisplayFlagSet():
            displayed_geo.append(node)
            
        # if node.type().name() == "gltf_hierarchy":
        #     obj = hou.node("/obj")
        #     geo = obj.createNode("geo",f"{node.name()}_geo")
        #     om = geo.createNode("object_merge")
        #     om.parm("objpath1").set(f"{node.path()}/*")
        #     node.setDisplayFlag(False)
        #     displayed_geo.append(geo)
            
        # Also check for geometry nodes inside subnets
        if node.type().name() == "subnet" or node.type().name() == "gltf_hierarchy":
            for child in node.allSubChildren():
                if child.type().category().name() == "Sop" and child.isDisplayFlagSet():
                    # Get the parent OBJ node
                    obj_parent = child.parent()
                    while obj_parent and obj_parent.type().category().name() != "Object":
                        obj_parent = obj_parent.parent()
                    
                    if obj_parent and obj_parent not in displayed_geo:
                        displayed_geo.append(obj_parent)
    
    return displayed_geo

def calculate_bounding_box(nodes):
    """Calculate the collective bounding box of all given nodes."""
    if not nodes:
        return None
    
    # Initialize with extreme values
    min_bounds = np.array([float('inf'), float('inf'), float('inf')])
    max_bounds = np.array([float('-inf'), float('-inf'), float('-inf')])
    
    for node in nodes:
        try:
            # Get the geometry
            display_node = node.displayNode()
            if display_node is None:
                continue
                
            geo = display_node.geometry()
            if geo is None:
                continue
            
            # Get the bounding box
            bbox = geo.boundingBox()
            if bbox is None:
                continue
            
            # Get node's transform
            transform = node.worldTransform()
            
            # Transform the bounding box corners
            for x in [bbox.minvec()[0], bbox.maxvec()[0]]:
                for y in [bbox.minvec()[1], bbox.maxvec()[1]]:
                    for z in [bbox.minvec()[2], bbox.maxvec()[2]]:
                        point = hou.Vector4(x, y, z, 1.0)
                        transformed_point = point * transform
                        
                        # Update min and max bounds
                        min_bounds[0] = min(min_bounds[0], transformed_point[0])
                        min_bounds[1] = min(min_bounds[1], transformed_point[1])
                        min_bounds[2] = min(min_bounds[2], transformed_point[2])
                        
                        max_bounds[0] = max(max_bounds[0], transformed_point[0])
                        max_bounds[1] = max(max_bounds[1], transformed_point[1])
                        max_bounds[2] = max(max_bounds[2], transformed_point[2])
        except Exception as e:
            print(f"Error processing node {node.name()}: {e}")
            continue
    
    if np.isinf(min_bounds).any() or np.isinf(max_bounds).any():
        return None
    
    center = [(min_bounds[0] + max_bounds[0]) / 2,
              (min_bounds[1] + max_bounds[1]) / 2,
              (min_bounds[2] + max_bounds[2]) / 2]
    
    return {
        'min': min_bounds.tolist(),
        'max': max_bounds.tolist(),
        'center': center
    }

def setup_camera_rig(bbox_center, orthographic=False):
    """
    Set up a null and camera rig at the given position.
    
    Args:
        bbox_center: The center position for the null
        orthographic: If True, create an orthographic camera
    """
    # Define node names
    null_name = "MCP_CAM_CENTER"
    cam_name = "MCP_CAMERA"
    
    # Delete existing nodes if they exist
    existing_null = hou.node("/obj/" + null_name)
    if existing_null:
        existing_null.destroy()
        
    existing_camera = hou.node("/obj/" + cam_name)
    if existing_camera:
        existing_camera.destroy()
    
    # Create a null at the center of the bounding box
    null = hou.node("/obj").createNode("null", null_name)
    
    # Set the null's position in the network (not world space)
    null.setPosition(hou.Vector2(0, 0))
    
    # Set the null's translation to the center of the bounding box
    null.parmTuple("t").set(bbox_center)
    
    # Create a camera as a child of the null
    camera = hou.node("/obj").createNode("cam", cam_name)
    
    # Set the camera's network position
    camera.setPosition(hou.Vector2(3, 0))
    
    # Set the camera's transform
    camera.parmTuple("t").set([0, 0, 5])
    
    # Set the camera's resolution to 512x512
    camera.parm("resx").set(512)
    camera.parm("resy").set(512)
    
    # Set the aspect ratio to 1.0 (square)
    camera.parm("aspect").set(1.0)
    
    # Set projection type
    if orthographic:
        camera.parm("projection").set(1)  # 1 = Orthographic
        print("Created orthographic camera")
    else:
        camera.parm("projection").set(0)  # 0 = Perspective
        print("Created perspective camera")
    
    # Make the camera a child of the null
    camera.setFirstInput(null)
    
    return null

def rotate_camera_center(null_node, rotation=(0, 90, 0)):
    """
    Rotate the camera center null node by the specified angles around each axis.
    
    Args:
        null_node: The null node to rotate
        rotation: Tuple of (rx, ry, rz) rotation angles in degrees
    """
    if not null_node:
        print("No null node provided for rotation.")
        return
        
    try:
        # Get current rotation
        current_rotation = null_node.parmTuple("r").eval()
        
        # Set rotation to specified angles
        new_rotation = [
            current_rotation[0] + rotation[0],  # Add X rotation
            current_rotation[1] + rotation[1],  # Add Y rotation
            current_rotation[2] + rotation[2]   # Add Z rotation
        ]
        
        # Apply the rotation
        null_node.parmTuple("r").set(new_rotation)
        print(f"Rotated camera center. New rotation: {new_rotation}")
        
    except Exception as e:
        print(f"Error rotating camera center: {e}")

# Keep the old function for backward compatibility
def rotate_camera_center_y90(null_node):
    """
    Rotate the camera center null node 90 degrees around the Y axis.
    Maintained for backward compatibility.
    
    Args:
        null_node: The null node to rotate
    """
    rotate_camera_center(null_node, rotation=(0, 90, 0))

def reset_camera_center(null_node):
    """
    Reset the camera center null node's rotation to (0, 0, 0).

    feat-mcp-round2-hardening §2：多视图渲染改单 rig 复用后，逐视图
    切换前必须先归零旋转——rotate_camera_center 是**叠加式**旋转，
    原实现逐视图 destroy+recreate 时旋转天然从 0 开始，单 rig 复用
    下不复位会累积前一视图的角度。
    
    Args:
        null_node: The null node to reset
    """
    if not null_node:
        print("No null node provided for rotation reset.")
        return
    try:
        null_node.parmTuple("r").set([0, 0, 0])
    except Exception as e:
        print(f"Error resetting camera center rotation: {e}")

def adjust_camera_to_fit_bbox(camera, bbox, padding_factor=1.1):
    """
    Adjust camera's distance or ortho width to fully encompass the bounding box,
    accounting for any rotation of the parent null node.
    
    Args:
        camera: The camera node to adjust
        bbox: The bounding box dictionary with 'min' and 'max' keys
        padding_factor: Extra space factor around the bbox (1.1 = 10% extra)
    """
    if not camera or not bbox:
        return
    
    try:
        # Check if the camera is orthographic
        is_ortho = camera.parm("projection").eval() == 1
        
        # Calculate bounding box dimensions
        bbox_width = bbox['max'][0] - bbox['min'][0]
        bbox_height = bbox['max'][1] - bbox['min'][1]
        bbox_depth = bbox['max'][2] - bbox['min'][2]
        
        # Get bounding box diagonals
        bbox_diagonal = math.sqrt(bbox_width**2 + bbox_height**2 + bbox_depth**2)
        bbox_view_diagonal = math.sqrt(bbox_width**2 + bbox_height**2)
        
        # Get the parent null node
        null_node = hou.node("/obj/MCP_CAM_CENTER")
        
        if null_node:
            # Get null's rotation
            null_r = hou.Vector3(null_node.parmTuple("r").eval())
            
            # Check if we have rotation around any axis
            has_x_rotation = abs(null_r[0] % 360) > 5
            has_y_rotation = abs(null_r[1] % 360) > 5
            has_z_rotation = abs(null_r[2] % 360) > 5
            
            # For significant rotation, we'll use a more conservative approach based on diagonal
            has_significant_rotation = has_x_rotation or has_y_rotation or has_z_rotation
            
            # For heavily rotated cameras, use the full diagonal as the controlling dimension
            if has_significant_rotation:
                # For rotated cameras, use larger dimensions to ensure everything is visible
                controlling_dimension = bbox_view_diagonal * 1.2
                depth_for_clipping = bbox_diagonal / 2
                
                print(f"Camera has rotation ({null_r[0]}, {null_r[1]}, {null_r[2]}), using diagonal dimensions")
            else:
                # Standard case - no significant rotation
                controlling_dimension = max(bbox_width, bbox_height)
                depth_for_clipping = bbox_depth
            
            # Calculate camera parameters
            fov_parm = camera.parm("aperture")
            if not fov_parm:
                # Try resx and resy if aperture isn't available
                resx = camera.parm("resx").eval()
                resy = camera.parm("resy").eval()
                aspect_ratio = float(resx) / float(resy)
                fov = 36.0  # Default 36mm film back
            else:
                fov = fov_parm.eval()
                aspect_ratio = camera.parm("aspect").eval()
            
            # Convert aperture to horizontal FOV in radians
            focal_parm = camera.parm("focal")
            focal_length = focal_parm.eval() if focal_parm else 30.0
            horizontal_fov = 2 * math.atan((fov/2) / focal_length)
            vertical_fov = 2 * math.atan(math.tan(horizontal_fov/2) / aspect_ratio)
            min_fov = min(horizontal_fov, vertical_fov)
            
            # Calculate required distance from the center of the bbox
            # Using the formula: distance = (size/2) / tan(FOV/2)
            required_distance = (controlling_dimension * padding_factor / 2) / math.tan(min_fov / 2)
            
            # Add extra distance to prevent clipping
            required_distance += depth_for_clipping
            
            # Make sure distance is positive and reasonable (at least 5 units)
            required_distance = max(5.0, required_distance)
            
            # Set the camera local Z position (in forward direction)
            # Do this for both perspective and orthographic cameras to prevent clipping
            camera.parmTuple("t").set([0, 0, required_distance])
            
            if is_ortho:
                # For orthographic camera, also set the ortho width
                ortho_width = controlling_dimension * padding_factor
                camera.parm("orthowidth").set(ortho_width)
                
                print(f"Orthographic camera adjusted:")
                print(f"  - Distance set to {required_distance} to prevent clipping")
                print(f"  - Ortho width set to {ortho_width} to encompass bounding box")
            else:
                print(f"Perspective camera distance adjusted to {required_distance} to encompass bounding box")
                
            print(f"Controlling dimension used: {controlling_dimension}")
        else:
            # Fallback calculation if null not found
            max_dimension = max(bbox_width, bbox_height)
            controlling_dimension = bbox_view_diagonal
            
            # Calculate distance for both camera types
            fov_parm = camera.parm("aperture")
            if not fov_parm:
                resx = camera.parm("resx").eval()
                resy = camera.parm("resy").eval()
                aspect_ratio = float(resx) / float(resy)
                fov = 36.0
            else:
                fov = fov_parm.eval()
                aspect_ratio = camera.parm("aspect").eval()
            
            focal_parm = camera.parm("focal")
            focal_length = focal_parm.eval() if focal_parm else 50.0
            horizontal_fov = 2 * math.atan((fov/2) / focal_length)
            vertical_fov = 2 * math.atan(math.tan(horizontal_fov/2) / aspect_ratio)
            min_fov = min(horizontal_fov, vertical_fov)
            
            required_distance = (controlling_dimension * padding_factor / 2) / math.tan(min_fov / 2)
            required_distance += bbox_depth
            required_distance = max(5.0, required_distance)
            
            # Set distance for both camera types
            camera.parmTuple("t").set([0, 0, required_distance])
            
            if is_ortho:
                # For orthographic, also set width
                ortho_width = controlling_dimension * padding_factor
                camera.parm("orthowidth").set(ortho_width)
                print(f"Null node not found. Orthographic camera adjusted:")
                print(f"  - Distance set to {required_distance} to prevent clipping")
                print(f"  - Ortho width set to {ortho_width}")
            else:
                print(f"Null node not found. Perspective camera distance set to {required_distance}")
            
    except Exception as e:
        print(f"Error adjusting camera: {e}")
        import traceback
        traceback.print_exc()

def setup_render_node(render_engine="opengl", karma_engine="cpu", render_path=None, camera_path="/obj/MCP_CAMERA", view_name=None, rotation=None, is_ortho=False):
    """
    Create a render node based on the specified render engine.
    
    Args:
        render_engine: The render engine to use ("opengl", "karma", or "mantra")
        karma_engine: For Karma, which engine to use ("cpu" or "gpu")
        render_path: Path to save the render；缺省时由调用方（server 端）
            回退 $TEMP/houdini_mcp/<日期>/ 规范目录，本函数不再默认
            C:/temp/（feat-mcp-round2-hardening §2b）
        camera_path: Path to the camera to use for rendering
        view_name: Optional name of the view (for filename)
        rotation: Camera rotation (for filename if view_name not provided)
        is_ortho: Whether the camera is orthographic (for filename)
        
    Returns:
        Tuple of (render node, filepath)
    """
    try:
        # Set default render path if not specified
        # §2b：缺省回退 $TEMP/houdini_mcp/<日期>/ 规范目录（7 天清理
        # 规范内），不再默认 C:/temp/。default_capture_path 返回完整
        # 文件路径，这里取其日期目录（ROP 节点自行命名输出文件）。
        if not render_path:
            from . import _capture_paths as _cp
            try:
                render_path = os.path.dirname(_cp.default_capture_path(
                    hou=hou, pane_type="rop",
                    engine=(render_engine or "opengl").lower()))
            except Exception as path_err:
                print(f"Warning: default render dir fallback failed: {path_err}")
                render_path = os.path.join(tempfile.gettempdir(), "houdini_mcp")
        
        # Ensure directory exists
        if not os.path.exists(render_path):
            os.makedirs(render_path)
        
        # Base render node name on render engine
        if render_engine.lower() == "karma":
            render_node_name = f"MCP_{karma_engine.upper()}_KARMA"
            node_type = "karma"
        elif render_engine.lower() == "mantra":
            render_node_name = "MCP_MANTRA"
            node_type = "ifd"
        else:  # Default to opengl
            render_node_name = "MCP_OGL_RENDER"
            node_type = "opengl"
        
        # Create filename based on projection type and rotation/view name
        proj_type = "ortho" if is_ortho else "persp"
        
        # Use view name if provided, otherwise use rotation values
        if view_name:
            filename = f"{render_node_name}_{view_name}_{proj_type}.jpg"
        elif rotation:
            rot_str = f"rot_{int(rotation[0])}_{int(rotation[1])}_{int(rotation[2])}"
            filename = f"{render_node_name}_{proj_type}_{rot_str}.jpg"
        else:
            # Fallback if neither is provided
            filename = f"{render_node_name}_{proj_type}.jpg"
            
        filepath = os.path.join(render_path, filename)
        
        # Check if the render node exists and delete it if it does
        render_node = hou.node("/out/" + render_node_name)
        if render_node:
            render_node.destroy()
        
        # Create a new render node
        render_node = hou.node("/out").createNode(node_type, render_node_name)
        
        if not render_node:
            print(f"Failed to create {render_engine} render node. Check if /out context exists.")
            return None, None
        
        # Check if the camera exists
        camera = hou.node(camera_path)
        if not camera:
            print(f"Camera not found at {camera_path}")
            return render_node, filepath
            
        # Get camera resolution
        resx = camera.parm("resx").eval()
        resy = camera.parm("resy").eval()
            
        # Set up parameters based on render engine
        if render_engine.lower() == "opengl":
            # Set the camera
            if render_node.parm("camera"):
                render_node.parm("camera").set(camera_path)
            
            # Set resolution
            if render_node.parm("tres"):
                render_node.parm("tres").set(True)
                render_node.parm("res1").set(resx)
                render_node.parm("res2").set(resy)
            
            # Set output path
            if render_node.parm("picture"):
                render_node.parm("picture").set(filepath)
            
        elif render_engine.lower() == "karma":
            # Set the camera
            if render_node.parm("camera"):
                render_node.parm("camera").set(camera_path)
            
            # Set Karma engine (CPU or GPU)
            if render_node.parm("engine"):  # Newer versions use "engine"
                if karma_engine.lower() == "gpu":
                    render_node.parm("engine").set("xpu")  # "xpu" for GPU
                else:
                    render_node.parm("engine").set("cpu")  # "cpu" for CPU
            elif render_node.parm("XPU"):  # Older versions use "XPU"
                if karma_engine.lower() == "gpu":
                    render_node.parm("XPU").set(1)  # 1 for GPU
                else:
                    render_node.parm("XPU").set(0)  # 0 for CPU
            
            # Set resolution directly in Karma
            if render_node.parm("resolution1"):
                render_node.parm("resolution1").set(resx)
                render_node.parm("resolution2").set(resy)
            
            # Set output path
            if render_node.parm("picture"):
                render_node.parm("picture").set(filepath)
            
        elif render_engine.lower() == "mantra":
            # Set the camera
            if render_node.parm("camera"):
                render_node.parm("camera").set(camera_path)
            
            # Set resolution
            if render_node.parm("override_camerares"):
                render_node.parm("override_camerares").set(True)
                render_node.parm("res_fraction").set("specific")
                # Use the correct parameter names for Mantra
                if render_node.parm("res_overridex"):
                    render_node.parm("res_overridex").set(resx)
                    render_node.parm("res_overridey").set(resy)
                elif render_node.parm("res_override_x"):  # Try alternate names
                    render_node.parm("res_override_x").set(resx)
                    render_node.parm("res_override_y").set(resy)
            
            # Set output path
            if render_node.parm("vm_picture"):
                render_node.parm("vm_picture").set(filepath)
        
        # Set to render 1 frame for all render engines
        if render_node.parm("trange"):
            render_node.parm("trange").set(0)  # Set to render current frame
        
        return render_node, filepath
        
    except Exception as e:
        print(f"Error setting up render node: {e}")
        import traceback
        traceback.print_exc()
        return None, None

# ======== RENDERING FUNCTIONS ========

def render_single_view(orthographic=False, rotation=(0, 90, 0), render_path=None, render_engine="opengl", karma_engine="cpu", consent_token=None, cleanup_report=None):
    """
    Set up camera rig and render a single view with specified rotation.

    fork-render-policy-redirect-and-consent: 入口先做 render policy 校验，
    opengl 走 redirect dict，karma_* 需 consent_token 才放行。

    feat-mcp-round2-hardening §2：本流程创建的全部临时节点（rig +
    ROP）在 finally 段销毁（成功 / 失败两路径都清）；清理失败不吞
    渲染结果——失败描述 append 到 ``cleanup_report``（调用方传 list
    才有），由调用方附到响应 ``_cleanup_warning``。

    Args:
        orthographic: If True, create an orthographic camera
        rotation: Tuple of (rx, ry, rz) rotation angles in degrees to apply to the camera center
        render_path: Path to save the render (server 端缺省回退
            $TEMP/houdini_mcp/<日期>/ 规范目录)
        render_engine: The render engine to use ("opengl", "karma", or "mantra")
        karma_engine: For Karma, which engine to use ("cpu" or "gpu")
        consent_token: 用户对 karma 渲染的 consent uuid4（首次调用 karma
            路径时会得到 interrupt dict + 新 token；用户回复 yes 后带该
            token 重调）。opengl / mantra 路径忽略。
        cleanup_report: 可选 list；finally 清理失败描述追加于此。

    Returns:
        Path to the rendered file。redirect 时返 dict（与 HoudiniMCPRender
        原约定 ``None`` 失败信号不同，但调用方应已先判 type）。
    """
    _action, _payload = _rp.enforce_render_engine_policy(
        render_engine, karma_engine)
    if _action == "redirect":
        return _payload
    if _action == "interrupt":
        if not (consent_token
                and _rp.consume_consent_token(consent_token)):
            return _payload

    # Find all displayed geometry
    displayed_geo = find_displayed_geometry()

    if not displayed_geo:
        print("No displayed geometry found in the scene.")
        return None

    print(f"Found {len(displayed_geo)} displayed geometry nodes.")

    # Calculate the bounding box
    bbox = calculate_bounding_box(displayed_geo)

    if not bbox:
        print("Could not calculate bounding box.")
        return None

    print(f"Bounding box min: {bbox['min']}")
    print(f"Bounding box max: {bbox['max']}")
    print(f"Bounding box center: {bbox['center']}")

    # 本流程创建的临时节点路径（rig + ROP），finally 段统一清理
    created_paths = []
    try:
        # Set up the camera rig
        null = setup_camera_rig(bbox['center'], orthographic)
        created_paths.extend(RENDER_TEMP_OBJ_PATHS)
        print(f"Created/updated camera rig at {bbox['center']}")

        # Rotate the camera center by the specified angles
        rotate_camera_center(null, rotation)

        # Get the camera node
        camera = hou.node("/obj/MCP_CAMERA")
        if camera:
            # Adjust camera to fit bounding box
            adjust_camera_to_fit_bbox(camera, bbox)
        else:
            print("Camera not found, couldn't adjust position.")
            return None

        # Create render node and render a frame
        render_node, filepath = setup_render_node(
            render_engine=render_engine,
            karma_engine=karma_engine,
            render_path=render_path,
            camera_path="/obj/MCP_CAMERA",
            rotation=rotation,
            is_ortho=orthographic
        )

        if not render_node:
            print("Failed to create render node.")
            return None
        if render_node.path() not in created_paths:
            created_paths.append(render_node.path())

        # Render the frame
        print(f"Rendering with {render_engine.upper()}" +
              (f" ({karma_engine.upper()})" if render_engine.lower() == "karma" else ""))
        render_node.render()

        print(f"Rendered frame to: {filepath}")
        return filepath
    finally:
        _finalize_render_cleanup(created_paths, cleanup_report)

def render_quad_view(orthographic=True, render_path=None, render_engine="opengl", karma_engine="cpu", consent_token=None, cleanup_report=None):
    """
    Create four standard views and render them:
    - Front view (0,0,0)
    - Left view (0,-90,0)
    - Top view (-90,0,0)
    - Perspective view (-45,-45,0)

    fork-render-policy-redirect-and-consent: 入口先做 render policy 校验，
    opengl 走 redirect dict，karma_* 需 consent_token 才放行。

    feat-mcp-round2-hardening §2：多视图渲染**建一次 rig、清一次**
    （原实现逐视图 destroy+recreate）；全部视图渲完后 finally 段统一
    销毁本流程创建的临时节点，清理失败不吞渲染结果——失败描述
    append 到 ``cleanup_report``（调用方附响应 _cleanup_warning）。

    Args:
        orthographic: If True, use orthographic projection for ALL views including perspective
        render_path: Path to save the renders (server 端缺省回退
            $TEMP/houdini_mcp/<日期>/ 规范目录)
        render_engine: The render engine to use ("opengl", "karma", or "mantra")
        karma_engine: For Karma, which engine to use ("cpu" or "gpu")
        consent_token: karma consent uuid4。
        cleanup_report: 可选 list；finally 清理失败描述追加于此。

    Returns:
        A list of paths to the rendered files。redirect / interrupt 时返 dict。
    """
    _action, _payload = _rp.enforce_render_engine_policy(
        render_engine, karma_engine)
    if _action == "redirect":
        return _payload
    if _action == "interrupt":
        if not (consent_token
                and _rp.consume_consent_token(consent_token)):
            return _payload

    rendered_files = []

    # Define the four standard views
    views = [
        {"name": "Front", "rotation": (0, 0, 0), "ortho": orthographic},
        {"name": "Left", "rotation": (0, -90, 0), "ortho": orthographic},
        {"name": "Top", "rotation": (-90, 0, 0), "ortho": orthographic},
        {"name": "Perspective", "rotation": (-45, -45, 0), "ortho": orthographic}  # Use the orthographic parameter
    ]

    # Find displayed geometry once
    displayed_geo = find_displayed_geometry()

    if not displayed_geo:
        print("No displayed geometry found in the scene.")
        return rendered_files

    print(f"Found {len(displayed_geo)} displayed geometry nodes.")

    # Calculate the bounding box once
    bbox = calculate_bounding_box(displayed_geo)

    if not bbox:
        print("Could not calculate bounding box.")
        return rendered_files

    print(f"Bounding box min: {bbox['min']}")
    print(f"Bounding box max: {bbox['max']}")
    print(f"Bounding box center: {bbox['center']}")

    # 本流程创建的临时节点路径，finally 段统一清理（rig 一次 + 各视图 ROP）
    created_paths = []
    try:
        # §2：四个视图共用同一 projection（原行为即全部传同一
        # orthographic 参数），rig 只建一次，逐视图仅旋转切换视角
        null = setup_camera_rig(bbox['center'], orthographic)
        created_paths.extend(RENDER_TEMP_OBJ_PATHS)

        # Render each view
        for view in views:
            print(f"\n--- Setting up {view['name']} view ---")

            # §2：单 rig 复用——每次迭代先归零旋转再叠加本视图旋转
            # （rotate 是叠加式；首视图归零冗余但幂等，与原"逐视图重建
            # rig 角度天然从 0 开始"语义一致）
            reset_camera_center(null)
            rotate_camera_center(null, view['rotation'])

            # Get the camera node
            camera = hou.node("/obj/MCP_CAMERA")
            if camera:
                # Adjust camera to fit bounding box
                adjust_camera_to_fit_bbox(camera, bbox)
            else:
                print(f"Camera not found, couldn't adjust position for {view['name']} view.")
                continue

            # Create a specific name with the view
            view_name = view['name'].lower()

            # Create render node
            render_node, filepath = setup_render_node(
                render_engine=render_engine,
                karma_engine=karma_engine,
                render_path=render_path,
                camera_path="/obj/MCP_CAMERA",
                view_name=view_name,
                is_ortho=view['ortho']
            )

            if not render_node:
                print(f"Failed to create render node for {view_name} view.")
                continue
            if render_node.path() not in created_paths:
                created_paths.append(render_node.path())

            # Render the frame
            print(f"Rendering {view_name} view with {render_engine.upper()}" +
                  (f" ({karma_engine.upper()})" if render_engine.lower() == "karma" else ""))
            render_node.render()

            print(f"Rendered {view_name} view to: {filepath}")

            if filepath:
                rendered_files.append(filepath)

        print(f"\nRendered {len(rendered_files)} views:")
        for file in rendered_files:
            print(f"  - {file}")

        return rendered_files
    finally:
        _finalize_render_cleanup(created_paths, cleanup_report)

def render_specific_camera(camera_path, render_path=None, render_engine="opengl", karma_engine="cpu", consent_token=None, cleanup_report=None):
    """
    Render using a specific camera that already exists in the scene.

    fork-render-policy-redirect-and-consent: 入口先做 render policy 校验，
    opengl 走 redirect dict，karma_* 需 consent_token 才放行。

    feat-mcp-round2-hardening §2：本流程只创建 ROP 节点（复用用户已有
    相机，rig 不动），渲染结束后 finally 段销毁该 ROP；清理失败不吞
    渲染结果——失败描述 append 到 ``cleanup_report``。

    Args:
        camera_path: Path to the camera node (e.g., "/obj/mycamera")
        render_path: Path to save the render (server 端缺省回退
            $TEMP/houdini_mcp/<日期>/ 规范目录)
        render_engine: The render engine to use ("opengl", "karma", or "mantra")
        karma_engine: For Karma, which engine to use ("cpu" or "gpu")
        consent_token: karma consent uuid4。
        cleanup_report: 可选 list；finally 清理失败描述追加于此。

    Returns:
        Path to the rendered file。redirect / interrupt 时返 dict。
    """
    _action, _payload = _rp.enforce_render_engine_policy(
        render_engine, karma_engine)
    if _action == "redirect":
        return _payload
    if _action == "interrupt":
        if not (consent_token
                and _rp.consume_consent_token(consent_token)):
            return _payload
    created_paths = []
    try:
        # Check if the camera exists
        camera = hou.node(camera_path)
        if not camera:
            print(f"Camera not found at path: {camera_path}")
            return None

        # Check if it's actually a camera
        if camera.type().name() != "cam":
            print(f"Node at {camera_path} is not a camera (type: {camera.type().name()})")
            return None

        print(f"Found camera: {camera.path()}")

        # Determine if the camera is orthographic
        is_ortho = camera.parm("projection").eval() == 1

        # Get camera rotation
        # If camera has a parent, we should get the effective rotation
        camera_parent = camera.parent()
        if camera_parent and camera_parent.type().name() == "null":
            # Get rotation from parent
            rotation = camera_parent.parmTuple("r").eval()
        else:
            # Get rotation from camera itself
            rotation = camera.parmTuple("r").eval()

        # Use camera name as view name
        view_name = camera.name()

        # Create render node
        render_node, filepath = setup_render_node(
            render_engine=render_engine,
            karma_engine=karma_engine,
            render_path=render_path,
            camera_path=camera_path,
            view_name=view_name,
            is_ortho=is_ortho
        )

        if not render_node:
            print("Failed to create render node.")
            return None
        if render_node.path() not in created_paths:
            created_paths.append(render_node.path())

        # Render the frame
        print(f"Rendering with {render_engine.upper()}" +
              (f" ({karma_engine.upper()})" if render_engine.lower() == "karma" else ""))
        render_node.render()

        print(f"Rendered frame using camera {camera_path} to: {filepath}")
        return filepath

    except Exception as e:
        print(f"Error rendering specific camera: {e}")
        import traceback
        traceback.print_exc()
        return None
    finally:
        _finalize_render_cleanup(created_paths, cleanup_report)

# ======== EXAMPLE USAGE ========

# Example 1: Render four standard views using orthographic projection with OpenGL
# render_quad_view(orthographic=True, render_path="C:/temp/", render_engine="opengl")

# Example 2: Render single view with custom rotation in perspective mode using Karma (GPU)
# render_single_view(orthographic=False, rotation=(-30, 45, 0), render_path="C:/temp/",render_engine="karma", karma_engine="gpu")

# Example 3: Render using a specific existing camera in the scene with Mantra
# render_specific_camera("/obj/my_camera", render_path="C:/temp/", render_engine="mantra")

# Example 4: Render a quad view with perspective projection using Karma (CPU)
# render_quad_view(orthographic=False, render_path="C:/temp/", 
#                 render_engine="karma", karma_engine="cpu")
