"""Claude Code - AI-powered Python assistant for Blender's Text Editor."""

import bpy

from . import operators, panels, preferences, properties
from .bridge import bridge


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
    # Panels
    panels.CLAUDE_PT_MainPanel,
    panels.CLAUDE_PT_SettingsPanel,
)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)

    bpy.types.Scene.claude = bpy.props.PointerProperty(type=properties.CLAUDE_State)

    bridge.register()

    print(f"Claude Code addon registered ({__package__})")


def unregister():
    bridge.unregister()

    try:
        del bpy.types.Scene.claude
    except AttributeError:
        pass

    for cls in reversed(_classes):
        try:
            bpy.utils.unregister_class(cls)
        except RuntimeError:
            pass

    print(f"Claude Code addon unregistered ({__package__})")
