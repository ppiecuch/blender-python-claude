"""Blender tool definitions for Claude's tool-use API.

Each tool has:
- A definition dict (sent to the API)
- An execution function (runs on Blender's main thread)
"""

import io
import json
import contextlib
import traceback

import bpy


# ---------------------------------------------------------------------------
# Tool definitions (Anthropic format)
# ---------------------------------------------------------------------------

TOOL_DEFINITIONS = [
    {
        "name": "get_scene_info",
        "description": (
            "Get an overview of the current Blender scene: object names, types, "
            "locations, active object, selected objects, and scene settings."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "get_object_info",
        "description": (
            "Get detailed information about a specific object: transforms, mesh stats "
            "(vertices/faces/edges), materials, modifiers, constraints, and parent/children."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Exact name of the object in bpy.data.objects",
                },
            },
            "required": ["name"],
        },
    },
    {
        "name": "execute_python",
        "description": (
            "Execute Python code in Blender. The code has access to bpy, mathutils, "
            "and bmesh. Returns stdout output and any errors. An undo step is created "
            "before execution. Use this for creating/modifying objects, materials, etc."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "Python code to execute",
                },
                "description": {
                    "type": "string",
                    "description": "Brief description of what the code does (for undo label)",
                },
            },
            "required": ["code"],
        },
    },
    {
        "name": "read_text_block",
        "description": (
            "Read the contents of a text data block from Blender's Text Editor. "
            "Use this to examine scripts the user is working on."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Name of the text block (e.g. 'Script.py'). "
                    "If empty, reads the active text block in the Text Editor.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "write_text_block",
        "description": (
            "Create or replace a text data block in Blender's Text Editor. "
            "Use this to write complete scripts for the user."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Name for the text block (e.g. 'MyScript.py')",
                },
                "content": {
                    "type": "string",
                    "description": "Full content to write",
                },
            },
            "required": ["name", "content"],
        },
    },
    {
        "name": "list_text_blocks",
        "description": (
            "List all text data blocks in Blender. Returns name, line count, "
            "byte size, and the currently active text block. Call this first "
            "to discover available scripts before reading or editing them."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "edit_text_block",
        "description": (
            "Edit a text block using find-and-replace. The old_string must match "
            "exactly once in the text block. Use this for targeted changes instead "
            "of rewriting the entire file with write_text_block."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Name of the text block to edit",
                },
                "old_string": {
                    "type": "string",
                    "description": "Exact text to find (must match exactly once)",
                },
                "new_string": {
                    "type": "string",
                    "description": "Replacement text",
                },
            },
            "required": ["name", "old_string", "new_string"],
        },
    },
]


# ---------------------------------------------------------------------------
# Tool execution (all run on main thread via bridge)
# ---------------------------------------------------------------------------

def execute_tool(name, tool_input):
    """Dispatch a tool call. Returns a result string."""
    dispatch = {
        "get_scene_info": _tool_get_scene_info,
        "get_object_info": _tool_get_object_info,
        "execute_python": _tool_execute_python,
        "read_text_block": _tool_read_text_block,
        "write_text_block": _tool_write_text_block,
        "list_text_blocks": _tool_list_text_blocks,
        "edit_text_block": _tool_edit_text_block,
    }
    fn = dispatch.get(name)
    if fn is None:
        return json.dumps({"error": f"Unknown tool: {name}"})
    try:
        return fn(tool_input)
    except Exception as e:
        return json.dumps({
            "error": str(e),
            "traceback": traceback.format_exc(),
        })


def _tool_get_scene_info(_input):
    scene = bpy.context.scene
    vl = bpy.context.view_layer

    objects = []
    for obj in scene.objects:
        info = {
            "name": obj.name,
            "type": obj.type,
            "location": [round(v, 3) for v in obj.location],
            "visible": obj.visible_get(),
        }
        if obj == vl.objects.active:
            info["active"] = True
        if obj.select_get():
            info["selected"] = True
        objects.append(info)

    result = {
        "scene_name": scene.name,
        "frame_current": scene.frame_current,
        "frame_range": [scene.frame_start, scene.frame_end],
        "object_count": len(objects),
        "objects": objects[:50],  # Cap at 50
        "active_object": vl.objects.active.name if vl.objects.active else None,
        "selected_count": len(bpy.context.selected_objects),
        "render_engine": scene.render.engine,
        "collections": [c.name for c in scene.collection.children],
    }
    if len(objects) > 50:
        result["note"] = f"Showing first 50 of {len(objects)} objects"

    return json.dumps(result, indent=2)


def _tool_get_object_info(tool_input):
    name = tool_input.get("name", "")
    obj = bpy.data.objects.get(name)
    if obj is None:
        return json.dumps({"error": f"Object '{name}' not found"})

    info = {
        "name": obj.name,
        "type": obj.type,
        "location": [round(v, 3) for v in obj.location],
        "rotation_euler": [round(v, 4) for v in obj.rotation_euler],
        "scale": [round(v, 3) for v in obj.scale],
        "dimensions": [round(v, 3) for v in obj.dimensions],
        "parent": obj.parent.name if obj.parent else None,
        "children": [c.name for c in obj.children],
        "visible": obj.visible_get(),
        "selected": obj.select_get(),
    }

    # Mesh-specific
    if obj.type == "MESH" and obj.data:
        mesh = obj.data
        info["mesh"] = {
            "vertices": len(mesh.vertices),
            "edges": len(mesh.edges),
            "polygons": len(mesh.polygons),
            "has_uv": len(mesh.uv_layers) > 0,
            "uv_layers": [uv.name for uv in mesh.uv_layers],
        }

    # Materials
    info["materials"] = []
    for slot in obj.material_slots:
        mat = slot.material
        if mat:
            mat_info = {"name": mat.name, "use_nodes": mat.use_nodes}
            if mat.use_nodes and mat.node_tree:
                mat_info["node_count"] = len(mat.node_tree.nodes)
            info["materials"].append(mat_info)

    # Modifiers
    info["modifiers"] = []
    for mod in obj.modifiers:
        info["modifiers"].append({"name": mod.name, "type": mod.type})

    # Constraints
    info["constraints"] = []
    for con in obj.constraints:
        info["constraints"].append({"name": con.name, "type": con.type})

    return json.dumps(info, indent=2)


def _tool_execute_python(tool_input):
    code = tool_input.get("code", "")
    description = tool_input.get("description", "AI-generated code")

    if not code.strip():
        return json.dumps({"error": "No code provided"})

    # Create undo step
    try:
        bpy.ops.ed.undo_push(message=f"Claude: {description[:50]}")
    except Exception:
        pass  # May fail outside of correct context

    # Execute with stdout capture
    stdout_capture = io.StringIO()
    stderr_capture = io.StringIO()

    namespace = {
        "bpy": bpy,
        "__builtins__": __builtins__,
    }

    # Lazily import optional modules into namespace
    try:
        import mathutils
        namespace["mathutils"] = mathutils
    except ImportError:
        pass
    try:
        import bmesh
        namespace["bmesh"] = bmesh
    except ImportError:
        pass

    try:
        with contextlib.redirect_stdout(stdout_capture), \
             contextlib.redirect_stderr(stderr_capture):
            exec(code, namespace)
    except Exception as e:
        return json.dumps({
            "status": "error",
            "error": str(e),
            "traceback": traceback.format_exc(),
            "stdout": stdout_capture.getvalue(),
            "stderr": stderr_capture.getvalue(),
        })

    return json.dumps({
        "status": "success",
        "stdout": stdout_capture.getvalue(),
        "stderr": stderr_capture.getvalue(),
    })


def _tool_read_text_block(tool_input):
    name = tool_input.get("name", "")

    if not name:
        # Try to get the active text block from a TEXT_EDITOR area
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                if area.type == "TEXT_EDITOR":
                    space = area.spaces.active
                    if space and space.text:
                        name = space.text.name
                        break
            if name:
                break

    if not name:
        return json.dumps({"error": "No text block name provided and no active text found"})

    text = bpy.data.texts.get(name)
    if text is None:
        available = [t.name for t in bpy.data.texts]
        return json.dumps({
            "error": f"Text block '{name}' not found",
            "available_texts": available,
        })

    return json.dumps({
        "name": text.name,
        "content": text.as_string(),
        "line_count": len(text.lines),
        "filepath": text.filepath or None,
    })


def _tool_write_text_block(tool_input):
    name = tool_input.get("name", "Script.py")
    content = tool_input.get("content", "")

    text = bpy.data.texts.get(name)
    created = text is None
    if created:
        text = bpy.data.texts.new(name)
    text.clear()
    text.write(content)

    # Switch Text Editor to show the written text block if setting enabled
    try:
        scene = bpy.context.scene
        if not hasattr(scene, "claude") or scene.claude.auto_switch_text:
            for window in bpy.context.window_manager.windows:
                for area in window.screen.areas:
                    if area.type == "TEXT_EDITOR":
                        area.spaces.active.text = text
                        break
    except Exception:
        pass

    return json.dumps({
        "status": "created" if created else "updated",
        "name": text.name,
        "line_count": len(text.lines),
    })


def _tool_list_text_blocks(_input):
    text_blocks = []
    for text in bpy.data.texts:
        content = text.as_string()
        text_blocks.append({
            "name": text.name,
            "line_count": len(text.lines),
            "byte_size": len(content.encode("utf-8")),
            "is_modified": text.is_modified,
            "filepath": text.filepath or None,
        })

    # Find the active text block
    active_text = None
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == "TEXT_EDITOR":
                space = area.spaces.active
                if space and space.text:
                    active_text = space.text.name
                    break
        if active_text:
            break

    return json.dumps({
        "text_blocks": text_blocks,
        "active_text": active_text,
        "count": len(text_blocks),
    }, indent=2)


def _tool_edit_text_block(tool_input):
    name = tool_input.get("name", "")
    old_string = tool_input.get("old_string", "")
    new_string = tool_input.get("new_string", "")

    if not name:
        return json.dumps({"error": "No text block name provided"})
    if not old_string:
        return json.dumps({"error": "old_string is required"})

    text = bpy.data.texts.get(name)
    if text is None:
        available = [t.name for t in bpy.data.texts]
        return json.dumps({
            "error": f"Text block '{name}' not found",
            "available_texts": available,
        })

    content = text.as_string()
    count = content.count(old_string)

    if count == 0:
        return json.dumps({
            "error": "old_string not found in text block",
            "hint": "Check for exact whitespace and indentation",
        })
    if count > 1:
        return json.dumps({
            "error": f"old_string matches {count} times (must be unique). "
                     "Include more surrounding context to make it unique.",
        })

    new_content = content.replace(old_string, new_string, 1)
    text.clear()
    text.write(new_content)

    # Switch Text Editor to show the edited text block
    try:
        scene = bpy.context.scene
        if hasattr(scene, "claude") and scene.claude.auto_switch_text:
            for window in bpy.context.window_manager.windows:
                for area in window.screen.areas:
                    if area.type == "TEXT_EDITOR":
                        area.spaces.active.text = text
                        break
    except Exception:
        pass

    return json.dumps({
        "status": "edited",
        "name": text.name,
        "chars_removed": len(old_string),
        "chars_added": len(new_string),
        "line_count": len(text.lines),
    })
