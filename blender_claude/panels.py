"""UI panels for Claude Code in the Text Editor sidebar."""

import textwrap

import bpy

from . import cli
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


def _draw_message(container, msg):
    """Draw a single chat message within the chat container."""
    role = msg.role
    content = msg.content

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
        prefix = ""
    else:
        icon = "DOT"
        prefix = role.title()

    # Role header
    if prefix:
        container.label(text=prefix, icon=icon)

    # Content lines
    lines = _wrap_text(content, MAX_LINE_WIDTH)
    in_code_block = False
    code_col = None
    displayed = 0

    for line in lines:
        if displayed >= MAX_LINES_PER_MESSAGE:
            container.label(text=f"  ... ({len(lines) - displayed} more lines)")
            break

        stripped = line.strip()
        if stripped.startswith("```"):
            in_code_block = not in_code_block
            if in_code_block:
                code_col = container.box().column(align=True)
                code_col.scale_y = 0.7
            else:
                code_col = None
            continue

        target = code_col if in_code_block else container
        if in_code_block:
            target.label(text="  " + line)
        else:
            target.label(text=line if line else " ")

        displayed += 1

    container.separator(factor=0.3)


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

        use_cli = (prefs.backend == "CLI")

        # -- Backend validation --
        if use_cli:
            if not cli.is_available():
                box = layout.box()
                box.label(text="Claude CLI not found", icon="ERROR")
                box.label(text="Install: npm install -g @anthropic-ai/claude-code")
                box.operator(
                    "preferences.addon_show",
                    text="Open Preferences",
                    icon="PREFERENCES",
                ).module = __package__
                return
        else:
            if not prefs.get_api_key():
                box = layout.box()
                box.label(text="API key not configured", icon="ERROR")
                box.operator(
                    "preferences.addon_show",
                    text="Open Preferences",
                    icon="PREFERENCES",
                ).module = __package__
                return

        # -- Backend indicator + workspace button --
        row = layout.row(align=True)
        if use_cli:
            row.label(text="CLI (Subscription)", icon="CONSOLE")
        else:
            row.label(text=f"API ({prefs.model.split('-')[1].title()})", icon="URL")
        row.operator("claude.open_workspace", text="", icon="FILE_FOLDER")

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

        # -- Settings (collapsible) --
        layout.separator()
        header = layout.row()
        header.prop(
            state, "show_settings",
            icon="TRIA_DOWN" if state.show_settings else "TRIA_RIGHT",
            text="Settings", emboss=False,
        )
        if state.show_settings:
            box = layout.box()
            col = box.column()
            col.prop(prefs, "backend", text="Backend")
            if prefs.backend == "API":
                col.prop(prefs, "model", text="Model")
            col.separator()
            col.prop(state, "auto_context")
            if state.auto_context:
                col.prop(state, "selection_only")
            col.prop(state, "auto_switch_text")
            if prefs.backend == "API":
                col.prop(state, "show_tool_calls")

        # -- Code action buttons --
        if state.last_code and not state.is_generating:
            layout.separator()
            row = layout.row(align=True)
            row.operator("claude.apply_code", text="Apply", icon="IMPORT")
            row.operator("claude.execute_code", text="Run", icon="PLAY")
            row.operator("claude.copy_code", text="Copy", icon="COPYDOWN")

        layout.separator()

        # -- Chat area --
        messages = state.messages
        total = len(messages)

        if total == 0 and not state.is_generating:
            layout.label(text="Send a message to start", icon="INFO")
            return

        chat = layout.box()

        # Show last N messages (auto-scroll to bottom)
        start = max(0, total - MAX_DISPLAY_MESSAGES)
        if start > 0:
            chat.label(text=f"({start} earlier messages)", icon="DOT")

        for i in range(start, total):
            _draw_message(chat, messages[i])

        # -- Streaming response --
        if state.is_generating:
            streaming = state.streaming_text
            if streaming:
                chat.separator(factor=0.3)
                row = chat.row()
                row.label(text="Claude", icon="LIGHT")
                row.label(text="", icon="SORTTIME")

                lines = _wrap_text(streaming, MAX_LINE_WIDTH)
                display_lines = lines[-30:] if len(lines) > 30 else lines
                in_code = False
                code_col = None
                for line in display_lines:
                    stripped = line.strip()
                    if stripped.startswith("```"):
                        in_code = not in_code
                        if in_code:
                            code_col = chat.box().column(align=True)
                            code_col.scale_y = 0.7
                        else:
                            code_col = None
                        continue
                    target = code_col if in_code else chat
                    if in_code:
                        target.label(text="  " + line)
                    else:
                        target.label(text=line if line else " ")

            elif state.status_message:
                chat.label(text=state.status_message, icon="SORTTIME")
