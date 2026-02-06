"""PropertyGroups for chat state and messages."""

import bpy
from bpy.props import (
    BoolProperty,
    CollectionProperty,
    EnumProperty,
    IntProperty,
    StringProperty,
)


class CLAUDE_ChatMessage(bpy.types.PropertyGroup):
    """A single chat message for UI display."""

    role: StringProperty(
        name="Role",
        description="Message role: user, assistant, info, error",
        default="user",
    )  # type: ignore

    content: StringProperty(
        name="Content",
        description="Message text content",
        default="",
    )  # type: ignore


class CLAUDE_State(bpy.types.PropertyGroup):
    """Main state for the Claude addon, attached to Scene."""

    # Chat history (display only - full API history kept in memory)
    messages: CollectionProperty(type=CLAUDE_ChatMessage)  # type: ignore

    # Prompt input
    prompt: StringProperty(
        name="Prompt",
        description="Message to send to Claude",
        default="",
    )  # type: ignore

    # Generation state
    is_generating: BoolProperty(
        name="Generating",
        description="Whether Claude is currently generating a response",
        default=False,
    )  # type: ignore

    streaming_text: StringProperty(
        name="Streaming Text",
        description="Current streaming response text",
        default="",
    )  # type: ignore

    status_message: StringProperty(
        name="Status",
        description="Current status message",
        default="",
    )  # type: ignore

    # Last extracted code block (for Apply/Run buttons)
    last_code: StringProperty(
        name="Last Code",
        description="Last code block from Claude response",
        default="",
    )  # type: ignore

    # Settings
    auto_context: BoolProperty(
        name="Auto-include script",
        description="Automatically include the active script as context",
        default=True,
    )  # type: ignore

    selection_only: BoolProperty(
        name="Selection only",
        description="Only include selected text, not the full script",
        default=False,
    )  # type: ignore

    show_tool_calls: BoolProperty(
        name="Show tool calls",
        description="Show tool call details in the chat",
        default=False,
    )  # type: ignore

    # Scroll position
    message_scroll: IntProperty(
        name="Scroll",
        description="Scroll offset for chat messages",
        default=0,
        min=0,
    )  # type: ignore


# In-memory conversation history (full API format, lost on restart)
conversation_history = []


def add_display_message(scene, role, content):
    """Add a message to the display chat history."""
    state = scene.claude
    msg = state.messages.add()
    msg.role = role
    msg.content = content
    # Auto-scroll to bottom
    total = len(state.messages)
    if total > 20:
        state.message_scroll = total - 20


def clear_conversation(scene):
    """Clear both display and API conversation history."""
    state = scene.claude
    state.messages.clear()
    state.message_scroll = 0
    state.last_code = ""
    state.streaming_text = ""
    state.status_message = ""
    conversation_history.clear()
