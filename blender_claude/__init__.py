"""Claude Code - AI-powered Python assistant for Blender's Text Editor."""

import atexit

import bpy

from . import cli, operators, panels, preferences, properties, workspace
from .bridge import bridge


VERSION = (0, 2, 0)
BUILD = "2026-02-07"


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

_classes = (
    # PropertyGroups first (dependencies for PointerProperty)
    properties.CLAUDE_ChatMessage,
    properties.CLAUDE_State,
    # Preferences
    preferences.CLAUDE_AddonPreferences,
    # Operators
    operators.CLAUDE_OT_SendPrompt,
    operators.CLAUDE_OT_Stop,
    operators.CLAUDE_OT_ApplyCode,
    operators.CLAUDE_OT_ExecuteCode,
    operators.CLAUDE_OT_ClearHistory,
    operators.CLAUDE_OT_CopyCode,
    operators.CLAUDE_OT_ScrollChat,
    operators.CLAUDE_OT_OpenWorkspace,
    # Panels
    panels.CLAUDE_PT_MainPanel,
)


def _atexit_cleanup():
    """Belt-and-suspenders cleanup if Blender exits without calling unregister."""
    workspace.clear_workspace()


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)

    bpy.types.Scene.claude = bpy.props.PointerProperty(type=properties.CLAUDE_State)

    bridge.register()
    atexit.register(_atexit_cleanup)

    ver = ".".join(str(v) for v in VERSION)
    print(f"Claude Code {ver} (build {BUILD}) registered ({__package__})")


def unregister():
    # 1. Stop the timer FIRST — prevents it from firing during teardown
    bridge.unregister()

    # 2. Cancel any in-flight generation
    operators._cancel_flag.set()
    thread = operators._generation_thread
    if thread and thread.is_alive():
        thread.join(timeout=2)

    # 2b. Clear CLI session state and workspace
    cli.clear_session()
    workspace.clear_workspace()

    try:
        atexit.unregister(_atexit_cleanup)
    except Exception:
        pass

    # 3. Remove the scene property BEFORE unregistering the PropertyGroup classes
    #    that define it — avoids RNA_struct_free crash
    try:
        del bpy.types.Scene.claude
    except (AttributeError, RuntimeError):
        pass

    # 4. Unregister classes in reverse order (panels first, then PropertyGroups last)
    for cls in reversed(_classes):
        try:
            bpy.utils.unregister_class(cls)
        except (RuntimeError, ValueError):
            pass

    print(f"Claude Code addon unregistered ({__package__})")
