"""Operators for the Claude Code addon."""

import json
import os
import platform
import re
import subprocess
import threading

import bpy
from bpy.props import IntProperty, StringProperty

from . import api, cli, prompts, tools, workspace
from .bridge import bridge
from .preferences import get_prefs
from .properties import add_display_message, conversation_history


# ---------------------------------------------------------------------------
# Module-level state for the background generation thread
# ---------------------------------------------------------------------------

_generation_thread = None
_cancel_flag = threading.Event()


def _extract_code_blocks(text):
    """Extract python code blocks from markdown-fenced text."""
    pattern = r"```(?:python)?\s*\n(.*?)```"
    blocks = re.findall(pattern, text, re.DOTALL)
    return blocks


def _switch_text_editor_to(text_name):
    """Switch all TEXT_EDITOR areas to display the named text block.

    Called on main thread after sync when auto_switch_text is enabled.
    """
    text = bpy.data.texts.get(text_name)
    if text is None:
        return
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == "TEXT_EDITOR":
                area.spaces.active.text = text
                return  # Switch the first one found


def _get_active_script_context(context):
    """Get the active text block content from the Text Editor."""
    # Try the current space first
    space = getattr(context, "space_data", None)
    if space and hasattr(space, "text") and space.text:
        text_block = space.text
        return text_block.name, text_block.as_string()

    # Fallback: search all TEXT_EDITOR areas
    for window in context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == "TEXT_EDITOR":
                sp = area.spaces.active
                if sp and sp.text:
                    return sp.text.name, sp.text.as_string()

    return None, None


def _get_selected_text(context):
    """Get only the selected text from the active text block."""
    space = getattr(context, "space_data", None)
    if not space or not hasattr(space, "text") or not space.text:
        return None

    text = space.text
    # Check if there's a selection
    if (text.current_line_index == text.select_end_line_index and
            text.current_character == text.select_end_character):
        return None  # No selection

    # Get selected lines
    start_line = min(text.current_line_index, text.select_end_line_index)
    end_line = max(text.current_line_index, text.select_end_line_index)

    lines = []
    for i in range(start_line, end_line + 1):
        if i < len(text.lines):
            lines.append(text.lines[i].body)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Background generation worker
# ---------------------------------------------------------------------------

def _generation_worker(api_key, model, system_prompt, messages, tool_defs,
                       max_iterations, max_tokens, scene_name):
    """Background thread: runs the agentic tool-use loop."""
    global _generation_thread

    current_messages = [m for m in messages]  # Shallow copy

    for iteration in range(max_iterations):
        if _cancel_flag.is_set():
            bridge.schedule(lambda: _on_cancelled(scene_name))
            return

        # -- Stream from API --
        text_content = ""
        tool_uses = []
        stop_reason = ""

        try:
            bridge.set_streaming_text("")
            for event in api.stream_messages(
                api_key, model, system_prompt, current_messages,
                tools=tool_defs, max_tokens=max_tokens,
            ):
                if _cancel_flag.is_set():
                    bridge.schedule(lambda: _on_cancelled(scene_name))
                    return

                etype = event["type"]

                if etype == "text_delta":
                    text_content += event["text"]
                    bridge.set_streaming_text(text_content)

                elif etype == "tool_use_complete":
                    tool_uses.append(event)

                elif etype == "message_complete":
                    stop_reason = event.get("stop_reason", "")

                elif etype == "error":
                    err_msg = event.get("message", "Unknown API error")
                    bridge.schedule(
                        lambda m=err_msg, s=scene_name: _on_error(s, m)
                    )
                    return

        except api.APIError as e:
            bridge.schedule(
                lambda m=str(e), s=scene_name: _on_error(s, m)
            )
            return
        except Exception as e:
            bridge.schedule(
                lambda m=str(e), s=scene_name: _on_error(s, m)
            )
            return

        if _cancel_flag.is_set():
            bridge.schedule(lambda: _on_cancelled(scene_name))
            return

        # -- Build the assistant message for the API history --
        assistant_content = []
        if text_content:
            assistant_content.append({"type": "text", "text": text_content})
        for tu in tool_uses:
            assistant_content.append({
                "type": "tool_use",
                "id": tu["id"],
                "name": tu["name"],
                "input": tu["input"],
            })
        current_messages.append({"role": "assistant", "content": assistant_content})

        # -- If no tool use, we're done --
        if stop_reason == "end_turn" or not tool_uses:
            final_text = text_content
            final_msgs = current_messages

            def _finish(t=final_text, m=final_msgs, s=scene_name):
                _on_complete(s, t, m)
            bridge.schedule(_finish)
            return

        # -- Execute tools on main thread --
        tool_results = []
        for tu in tool_uses:
            if _cancel_flag.is_set():
                bridge.schedule(lambda: _on_cancelled(scene_name))
                return

            tool_name = tu["name"]
            tool_input = tu["input"]

            # Update streaming display with tool status
            status = text_content + f"\n\n[Running: {tool_name}...]"
            bridge.set_streaming_text(status)

            # Show tool call in chat if enabled
            def _show_tool(n=tool_name, i=tool_input, s=scene_name):
                scene = bpy.data.scenes.get(s)
                if scene and scene.claude.show_tool_calls:
                    input_str = json.dumps(i, indent=2)[:200]
                    add_display_message(scene, "info", f"Tool: {n}({input_str})")
            bridge.schedule(_show_tool)

            # Execute on main thread and wait for result
            holder = bridge.execute_on_main(tools.execute_tool, tool_name, tool_input)
            try:
                result = holder.wait(timeout=60)
            except Exception as e:
                result = json.dumps({"error": str(e)})

            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tu["id"],
                "content": result,
            })

            # Show tool result in chat if enabled
            def _show_result(r=result, n=tool_name, s=scene_name):
                scene = bpy.data.scenes.get(s)
                if scene and scene.claude.show_tool_calls:
                    short = r[:200] + ("..." if len(r) > 200 else "")
                    add_display_message(scene, "info", f"Result: {short}")
            bridge.schedule(_show_result)

        # Add tool results to messages
        current_messages.append({"role": "user", "content": tool_results})

        # Reset for next iteration
        text_content = ""
        tool_uses = []

    # Exhausted max iterations
    bridge.schedule(
        lambda s=scene_name: _on_error(s, "Reached maximum tool iterations")
    )


# ---------------------------------------------------------------------------
# CLI backend generation worker
# ---------------------------------------------------------------------------

def _cli_generation_worker(prompt_text, system_prompt, scene_name):
    """Background thread: streams response from claude CLI with workspace sync."""
    global _generation_thread

    try:
        # 1. Get/create workspace and sync Blender text blocks to disk
        ws = bridge.execute_on_main(workspace.get_workspace).wait(timeout=10)
        bridge.execute_on_main(ws.sync_out).wait(timeout=10)

        if _cancel_flag.is_set():
            bridge.schedule(lambda s=scene_name: _on_cancelled(s))
            return

        # 2. Stream CLI response with cwd pointing at the workspace
        accumulated_text = ""

        for event in cli.stream_response(
            prompt=prompt_text,
            context_text=None,  # Files are on disk, no stdin needed
            system_prompt=system_prompt,
            cancel_flag=_cancel_flag,
            cwd=ws.root,
            allowed_tools=["Read", "Edit", "Write", "Glob", "Grep"],
        ):
            if _cancel_flag.is_set():
                bridge.schedule(lambda s=scene_name: _on_cancelled(s))
                return

            etype = event["type"]

            if etype == "text":
                accumulated_text = event["text"]
                bridge.set_streaming_text(accumulated_text)

            elif etype == "result":
                final_text = event.get("text", accumulated_text)
                cost = event.get("cost_usd", 0)
                duration = event.get("duration_ms", 0)

                # 3. Sync disk changes back to Blender
                try:
                    changes = bridge.execute_on_main(ws.sync_back).wait(timeout=10)
                except Exception:
                    changes = []

                # Build a simple messages list for conversation history
                final_msgs = list(conversation_history)
                final_msgs.append({
                    "role": "assistant",
                    "content": [{"type": "text", "text": final_text}],
                })

                def _finish(t=final_text, m=final_msgs, s=scene_name,
                            c=cost, d=duration, ch=changes):
                    _on_complete(s, t, m)
                    scene = bpy.data.scenes.get(s)
                    if not scene:
                        return
                    # Show change summary if any files were synced
                    if ch:
                        summary = workspace.Workspace.format_summary(ch)
                        add_display_message(scene, "info", summary)
                        # Auto-switch to the first modified/created text block
                        if scene.claude.auto_switch_text:
                            _switch_text_editor_to(ch[0]["name"])
                    # Show cost info if available
                    if c > 0:
                        cost_str = f"${c:.4f}"
                        dur_str = f"{d / 1000:.1f}s" if d else ""
                        info = f"CLI: {cost_str}"
                        if dur_str:
                            info += f" / {dur_str}"
                        add_display_message(scene, "info", info)

                bridge.schedule(_finish)
                return

            elif etype == "error":
                err_msg = event.get("message", "Unknown CLI error")
                bridge.schedule(
                    lambda m=err_msg, s=scene_name: _on_error(s, m)
                )
                return

        # If we get here without a result event, the stream ended unexpectedly
        # Still sync back any changes CLI may have made
        try:
            changes = bridge.execute_on_main(ws.sync_back).wait(timeout=10)
        except Exception:
            changes = []

        if accumulated_text:
            final_msgs = list(conversation_history)
            final_msgs.append({
                "role": "assistant",
                "content": [{"type": "text", "text": accumulated_text}],
            })

            def _finish_fallback(t=accumulated_text, m=final_msgs,
                                 s=scene_name, ch=changes):
                _on_complete(s, t, m)
                if ch:
                    scene = bpy.data.scenes.get(s)
                    if scene:
                        summary = workspace.Workspace.format_summary(ch)
                        add_display_message(scene, "info", summary)
                        if scene.claude.auto_switch_text:
                            _switch_text_editor_to(ch[0]["name"])

            bridge.schedule(_finish_fallback)
        else:
            bridge.schedule(
                lambda s=scene_name: _on_error(s, "No response from Claude CLI")
            )

    except Exception as e:
        bridge.schedule(
            lambda m=str(e), s=scene_name: _on_error(s, m)
        )


# ---------------------------------------------------------------------------
# Callbacks (run on main thread via bridge.schedule)
# ---------------------------------------------------------------------------

def _on_complete(scene_name, text, messages):
    """Called when generation is complete."""
    scene = bpy.data.scenes.get(scene_name)
    if not scene:
        return
    state = scene.claude

    state.is_generating = False
    state.streaming_text = ""
    state.status_message = ""
    bridge.clear_streaming_text()

    # Add assistant message to display
    if text.strip():
        add_display_message(scene, "assistant", text.strip())

    # Extract code blocks for Apply/Run
    code_blocks = _extract_code_blocks(text)
    if code_blocks:
        state.last_code = code_blocks[-1].strip()

    # Auto-switch to last text block modified by API tools
    if state.auto_switch_text and tools._last_modified_text:
        _switch_text_editor_to(tools._last_modified_text)
        tools._last_modified_text = None

    # Update the in-memory API conversation history
    conversation_history.clear()
    conversation_history.extend(messages)


def _on_error(scene_name, message):
    """Called when an error occurs."""
    scene = bpy.data.scenes.get(scene_name)
    if not scene:
        return
    state = scene.claude

    state.is_generating = False
    state.streaming_text = ""
    state.status_message = ""
    bridge.clear_streaming_text()

    add_display_message(scene, "error", f"Error: {message}")


def _on_cancelled(scene_name):
    """Called when generation is cancelled."""
    scene = bpy.data.scenes.get(scene_name)
    if not scene:
        return
    state = scene.claude

    state.is_generating = False
    state.streaming_text = ""
    state.status_message = ""
    bridge.clear_streaming_text()

    add_display_message(scene, "info", "Generation cancelled")


# ---------------------------------------------------------------------------
# Operators
# ---------------------------------------------------------------------------

class CLAUDE_OT_SendPrompt(bpy.types.Operator):
    """Send a prompt to Claude"""
    bl_idname = "claude.send_prompt"
    bl_label = "Send to Claude"
    bl_options = {"INTERNAL"}

    def execute(self, context):
        global _generation_thread

        scene = context.scene
        state = scene.claude
        prefs = get_prefs()
        use_cli = (prefs.backend == "CLI")

        # Validate backend availability
        if use_cli:
            if not cli.is_available():
                self.report({"ERROR"},
                            "Claude CLI not found. Install: npm install -g @anthropic-ai/claude-code")
                return {"CANCELLED"}
        else:
            api_key = prefs.get_api_key()
            if not api_key:
                self.report({"ERROR"},
                            "No API key set. Check addon preferences or ANTHROPIC_API_KEY env var.")
                return {"CANCELLED"}

        prompt_text = state.prompt.strip()
        if not prompt_text:
            self.report({"WARNING"}, "Enter a prompt first")
            return {"CANCELLED"}

        if state.is_generating:
            self.report({"WARNING"}, "Already generating a response")
            return {"CANCELLED"}

        # Gather script context
        script_context = ""
        if state.auto_context:
            if state.selection_only:
                selected = _get_selected_text(context)
                if selected:
                    script_context = f"Selected code:\n```python\n{selected}\n```"
            else:
                script_name, script_content = _get_active_script_context(context)
                if script_content:
                    script_context = f"Current script ({script_name}):\n```python\n{script_content}\n```"

        # Build user content for API history
        user_content = ""
        if script_context:
            user_content = script_context + "\n\n"
        user_content += prompt_text

        # Add to display
        add_display_message(scene, "user", prompt_text)
        state.prompt = ""

        # Add to conversation history
        conversation_history.append({
            "role": "user",
            "content": user_content,
        })

        # Start generation
        state.is_generating = True
        state.streaming_text = ""
        state.status_message = "Connecting..."
        state.last_code = ""

        _cancel_flag.clear()

        if use_cli:
            # CLI backend: workspace-based, files on disk
            # Enrich prompt with active script hint
            cli_prompt = prompt_text
            script_name, _ = _get_active_script_context(context)
            if script_name:
                cli_prompt = f"[Viewing '{script_name}' in Text Editor]\n\n{prompt_text}"

            _generation_thread = threading.Thread(
                target=_cli_generation_worker,
                args=(
                    cli_prompt,
                    prompts.CLI_SYSTEM_PROMPT,
                    scene.name,
                ),
                daemon=True,
            )
        else:
            # API backend: agentic tool-use loop
            _generation_thread = threading.Thread(
                target=_generation_worker,
                args=(
                    api_key,
                    prefs.model,
                    prompts.SYSTEM_PROMPT,
                    list(conversation_history),
                    tools.TOOL_DEFINITIONS,
                    prefs.max_tool_iterations,
                    prefs.max_tokens,
                    scene.name,
                ),
                daemon=True,
            )

        _generation_thread.start()
        return {"FINISHED"}


class CLAUDE_OT_Stop(bpy.types.Operator):
    """Stop the current Claude generation"""
    bl_idname = "claude.stop"
    bl_label = "Stop"
    bl_options = {"INTERNAL"}

    @classmethod
    def poll(cls, context):
        return context.scene.claude.is_generating

    def execute(self, context):
        _cancel_flag.set()
        context.scene.claude.status_message = "Stopping..."
        return {"FINISHED"}


class CLAUDE_OT_ApplyCode(bpy.types.Operator):
    """Apply the last generated code to the active text block"""
    bl_idname = "claude.apply_code"
    bl_label = "Apply Code"
    bl_options = {"INTERNAL"}

    @classmethod
    def poll(cls, context):
        return bool(context.scene.claude.last_code)

    def execute(self, context):
        state = context.scene.claude
        code = state.last_code

        # Get or create text block
        space = getattr(context, "space_data", None)
        if space and hasattr(space, "text") and space.text:
            text = space.text
        else:
            text = bpy.data.texts.get("Claude_Output.py")
            if not text:
                text = bpy.data.texts.new("Claude_Output.py")

        text.clear()
        text.write(code)

        # Show in Text Editor
        if space and hasattr(space, "text"):
            space.text = text

        self.report({"INFO"}, f"Code applied to '{text.name}'")
        return {"FINISHED"}


class CLAUDE_OT_ExecuteCode(bpy.types.Operator):
    """Execute the last generated code"""
    bl_idname = "claude.execute_code"
    bl_label = "Run Code"
    bl_options = {"INTERNAL", "UNDO"}

    code: StringProperty(default="")  # type: ignore

    @classmethod
    def poll(cls, context):
        return bool(context.scene.claude.last_code)

    def execute(self, context):
        state = context.scene.claude
        code = self.code if self.code else state.last_code

        if not code.strip():
            self.report({"WARNING"}, "No code to execute")
            return {"CANCELLED"}

        result = tools.execute_tool("execute_python", {
            "code": code,
            "description": "Manual execution",
        })

        try:
            result_data = json.loads(result)
        except json.JSONDecodeError:
            result_data = {"status": "unknown", "stdout": result}

        if result_data.get("status") == "error":
            error_msg = result_data.get("error", "Unknown error")
            self.report({"ERROR"}, error_msg)
            add_display_message(context.scene, "error", f"Execution error: {error_msg}")
        else:
            stdout = result_data.get("stdout", "").strip()
            msg = "Code executed successfully"
            if stdout:
                msg += f": {stdout[:100]}"
            self.report({"INFO"}, msg)

        return {"FINISHED"}


class CLAUDE_OT_ClearHistory(bpy.types.Operator):
    """Clear the conversation history"""
    bl_idname = "claude.clear_history"
    bl_label = "Clear"
    bl_options = {"INTERNAL"}

    def execute(self, context):
        from .properties import clear_conversation
        clear_conversation(context.scene)
        self.report({"INFO"}, "Chat cleared")
        return {"FINISHED"}


class CLAUDE_OT_CopyCode(bpy.types.Operator):
    """Copy the last generated code to clipboard"""
    bl_idname = "claude.copy_code"
    bl_label = "Copy Code"
    bl_options = {"INTERNAL"}

    @classmethod
    def poll(cls, context):
        return bool(context.scene.claude.last_code)

    def execute(self, context):
        context.window_manager.clipboard = context.scene.claude.last_code
        self.report({"INFO"}, "Code copied to clipboard")
        return {"FINISHED"}


class CLAUDE_OT_ScrollChat(bpy.types.Operator):
    """Scroll the chat history"""
    bl_idname = "claude.scroll_chat"
    bl_label = "Scroll Chat"
    bl_options = {"INTERNAL"}

    direction: IntProperty(default=1)  # type: ignore

    def execute(self, context):
        state = context.scene.claude
        total = len(state.messages)
        state.message_scroll = max(0, min(state.message_scroll + self.direction * 5,
                                          max(0, total - 5)))
        return {"FINISHED"}


class CLAUDE_OT_OpenWorkspace(bpy.types.Operator):
    """Open the workspace folder in the system file browser"""
    bl_idname = "claude.open_workspace"
    bl_label = "Open Workspace"
    bl_options = {"INTERNAL"}

    @classmethod
    def poll(cls, context):
        return workspace._workspace is not None

    def execute(self, context):
        ws = workspace._workspace
        if ws is None or not os.path.isdir(ws.root):
            self.report({"WARNING"}, "No workspace active")
            return {"CANCELLED"}

        path = ws.root
        if platform.system() == "Darwin":
            subprocess.Popen(["open", path])
        elif platform.system() == "Windows":
            os.startfile(path)
        else:
            subprocess.Popen(["xdg-open", path])

        return {"FINISHED"}
