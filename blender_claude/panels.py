"""UI panels for Claude Code in the Text Editor sidebar."""

import textwrap

import bpy

from .preferences import get_prefs


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

MAX_LINE_WIDTH = 55  # Characters per line in the sidebar
MAX_DISPLAY_MESSAGES = 30
MAX_LINES_PER_MESSAGE = 25


def _wrap_text(text, width=MAX_LINE_WIDTH):
    """Split text into lines that fit the sidebar width."""
    result = []
    for line in text.split("\n"):
        if not line:
            result.append("")
        else:
            result.extend(textwrap.wrap(line, width=width) or [""])
    return result


def _draw_message(layout, msg, show_full=False):
    """Draw a single chat message in the panel."""
    role = msg.role
    content = msg.content

    # Style based on role
    if role == "user":
        icon = "USER"
        prefix = "You"
    elif role == "assistant":
        icon = "LIGHT"
        prefix = "Claude"
    elif role == "error":
        icon = "ERROR"
        prefix = "Error"
    elif role == "info":
        icon = "INFO"
        prefix = "Info"
    else:
        icon = "DOT"
        prefix = role.title()

    box = layout.box()
    header = box.row()
    header.label(text=prefix, icon=icon)

    lines = _wrap_text(content, MAX_LINE_WIDTH)
    in_code_block = False
    code_col = None
    displayed = 0

    for line in lines:
        if displayed >= MAX_LINES_PER_MESSAGE and not show_full:
            box.label(text=f"  ... ({len(lines) - displayed} more lines)")
            break

        # Detect code fences
        stripped = line.strip()
        if stripped.startswith("```"):
            in_code_block = not in_code_block
            if in_code_block:
                code_col = box.box().column(align=True)
                code_col.scale_y = 0.7
            else:
                code_col = None
            continue

        target = code_col if in_code_block else box
        if in_code_block:
            target.label(text="  " + line)
        else:
            target.label(text=line if line else " ")

        displayed += 1


# ---------------------------------------------------------------------------
# Main Panel
# ---------------------------------------------------------------------------

class CLAUDE_PT_MainPanel(bpy.types.Panel):
    """Claude Code assistant panel in the Text Editor sidebar."""

    bl_label = "Claude Code"
    bl_idname = "CLAUDE_PT_main"
    bl_space_type = "TEXT_EDITOR"
    bl_region_type = "UI"
    bl_category = "Claude"

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        state = scene.claude
        prefs = get_prefs()

        # -- API key warning --
        if not prefs.get_api_key():
            box = layout.box()
            box.label(text="API key not configured", icon="ERROR")
            box.operator(
                "preferences.addon_show",
                text="Open Preferences",
                icon="PREFERENCES",
            ).module = __package__
            return

        # -- Active script indicator --
        space = context.space_data
        if space and hasattr(space, "text") and space.text:
            row = layout.row(align=True)
            row.label(text=f"Script: {space.text.name}", icon="TEXT")
            if state.auto_context:
                row.label(text="", icon="CHECKMARK")
        else:
            layout.label(text="No active script", icon="INFO")

        layout.separator()

        # -- Chat history --
        messages = state.messages
        total = len(messages)

        if total > 0:
            # Scroll controls if many messages
            if total > MAX_DISPLAY_MESSAGES:
                row = layout.row(align=True)
                op = row.operator("claude.scroll_chat", text="", icon="TRIA_UP")
                op.direction = -1
                row.label(text=f"{state.message_scroll + 1}-{min(state.message_scroll + MAX_DISPLAY_MESSAGES, total)} / {total}")
                op = row.operator("claude.scroll_chat", text="", icon="TRIA_DOWN")
                op.direction = 1

            # Display messages
            start = state.message_scroll
            end = min(start + MAX_DISPLAY_MESSAGES, total)
            for i in range(start, end):
                if i < total:
                    _draw_message(layout, messages[i])

        # -- Streaming response --
        if state.is_generating:
            streaming = state.streaming_text
            if streaming:
                box = layout.box()
                header = box.row()
                header.label(text="Claude", icon="LIGHT")
                header.label(text="", icon="SORTTIME")

                lines = _wrap_text(streaming, MAX_LINE_WIDTH)
                # Show last 30 lines of streaming content
                display_lines = lines[-30:] if len(lines) > 30 else lines
                in_code = False
                code_col = None
                for line in display_lines:
                    stripped = line.strip()
                    if stripped.startswith("```"):
                        in_code = not in_code
                        if in_code:
                            code_col = box.box().column(align=True)
                            code_col.scale_y = 0.7
                        else:
                            code_col = None
                        continue
                    target = code_col if in_code else box
                    if in_code:
                        target.label(text="  " + line)
                    else:
                        target.label(text=line if line else " ")

            elif state.status_message:
                box = layout.box()
                box.label(text=state.status_message, icon="SORTTIME")

        layout.separator()

        # -- Code action buttons --
        if state.last_code and not state.is_generating:
            row = layout.row(align=True)
            row.operator("claude.apply_code", text="Apply", icon="IMPORT")
            row.operator("claude.execute_code", text="Run", icon="PLAY")
            row.operator("claude.copy_code", text="Copy", icon="COPYDOWN")

        # -- Prompt input --
        layout.separator()

        col = layout.column(align=True)
        col.prop(state, "prompt", text="", placeholder="Ask Claude...")

        row = col.row(align=True)
        if state.is_generating:
            row.operator("claude.stop", text="Stop", icon="CANCEL")
        else:
            row.operator("claude.send_prompt", text="Send", icon="PLAY")
            row.operator("claude.clear_history", text="", icon="TRASH")


# ---------------------------------------------------------------------------
# Settings Sub-Panel
# ---------------------------------------------------------------------------

class CLAUDE_PT_SettingsPanel(bpy.types.Panel):
    """Settings sub-panel."""

    bl_label = "Settings"
    bl_idname = "CLAUDE_PT_settings"
    bl_space_type = "TEXT_EDITOR"
    bl_region_type = "UI"
    bl_category = "Claude"
    bl_parent_id = "CLAUDE_PT_main"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout = self.layout
        state = context.scene.claude
        prefs = get_prefs()

        layout.prop(prefs, "model", text="Model")
        layout.prop(state, "auto_context")
        if state.auto_context:
            layout.prop(state, "selection_only")
        layout.prop(state, "show_tool_calls")
