"""Claude Code CLI backend — uses 'claude -p' subprocess.

Piggybacks on the user's Claude subscription (Pro/Max/Team) rather than
prepaid API credits. Requires the Claude Code CLI to be installed.
"""

import glob
import json
import os
import shutil
import subprocess
import sys
import threading


# Cached resolved path (None = not yet searched, "" = not found)
_cached_path = None


def _find_claude_binary():
    """Search common installation paths for the claude CLI binary."""
    global _cached_path
    if _cached_path is not None:
        return _cached_path or None

    # 1. Try PATH first (fastest)
    found = shutil.which("claude")
    if found:
        _cached_path = found
        return found

    home = os.path.expanduser("~")
    candidates = []

    if sys.platform == "darwin":
        # macOS common locations
        candidates = [
            os.path.join(home, ".local", "bin", "claude"),
            os.path.join(home, ".claude", "local", "claude"),
            "/usr/local/bin/claude",
            "/opt/homebrew/bin/claude",
            os.path.join(home, ".npm-global", "bin", "claude"),
            os.path.join(home, "node_modules", ".bin", "claude"),
        ]
        # nvm installations — pick latest node version
        nvm_pattern = os.path.join(home, ".nvm", "versions", "node", "*", "bin", "claude")
        candidates.extend(sorted(glob.glob(nvm_pattern), reverse=True))

    elif sys.platform == "win32":
        # Windows common locations
        appdata = os.environ.get("APPDATA", "")
        localappdata = os.environ.get("LOCALAPPDATA", "")
        programfiles = os.environ.get("ProgramFiles", r"C:\Program Files")

        for ext in (".cmd", ".exe", ""):
            candidates.extend([
                os.path.join(appdata, "npm", f"claude{ext}"),
                os.path.join(localappdata, "Programs", "claude", f"claude{ext}"),
                os.path.join(home, ".local", "bin", f"claude{ext}"),
                os.path.join(programfiles, "nodejs", f"claude{ext}"),
            ])
        # nvm-windows
        nvm_root = os.environ.get("NVM_HOME", os.path.join(appdata, "nvm"))
        for ext in (".cmd", ".exe"):
            nvm_pattern = os.path.join(nvm_root, "*", f"claude{ext}")
            candidates.extend(sorted(glob.glob(nvm_pattern), reverse=True))

    else:
        # Linux / other Unix
        candidates = [
            os.path.join(home, ".local", "bin", "claude"),
            os.path.join(home, ".claude", "local", "claude"),
            "/usr/local/bin/claude",
            "/usr/bin/claude",
            os.path.join(home, ".npm-global", "bin", "claude"),
            os.path.join(home, "node_modules", ".bin", "claude"),
            "/snap/bin/claude",
        ]
        # nvm installations
        nvm_dir = os.environ.get("NVM_DIR", os.path.join(home, ".nvm"))
        nvm_pattern = os.path.join(nvm_dir, "versions", "node", "*", "bin", "claude")
        candidates.extend(sorted(glob.glob(nvm_pattern), reverse=True))

    # 2. Check candidates
    for path in candidates:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            _cached_path = path
            return path

    _cached_path = ""
    return None


def is_available():
    """Check if the claude CLI is installed."""
    return _find_claude_binary() is not None


def get_claude_path():
    """Get the full path to the claude CLI."""
    return _find_claude_binary() or "claude"


def clear_path_cache():
    """Clear the cached path so next call re-scans. Useful after install."""
    global _cached_path
    _cached_path = None


# Current session ID for multi-turn conversation
_session_id = None
_session_lock = threading.Lock()


def get_session_id():
    with _session_lock:
        return _session_id


def set_session_id(sid):
    global _session_id
    with _session_lock:
        _session_id = sid


def clear_session():
    global _session_id
    with _session_lock:
        _session_id = None


def stream_response(prompt, context_text=None, system_prompt=None,
                    resume_session=True, cancel_flag=None,
                    cwd=None, allowed_tools=None):
    """Stream a response from claude -p.

    Args:
        prompt: The user's prompt (passed as -p argument)
        context_text: Optional context piped via stdin (script content, scene info)
        system_prompt: Optional system prompt appended to Claude's default
        resume_session: Whether to resume the previous session for multi-turn
        cancel_flag: threading.Event to signal cancellation
        cwd: Working directory for the subprocess (defaults to ~)
        allowed_tools: List of tool names to allow (e.g. ["Read","Edit","Write"])

    Yields event dicts:
        {"type": "session_init", "session_id": "..."}
        {"type": "text", "text": "full text so far"}
        {"type": "result", "text": "...", "session_id": "...", "cost_usd": ..., "duration_ms": ...}
        {"type": "error", "message": "..."}
    """
    cmd = [
        get_claude_path(),
        "-p", prompt,
        "--output-format", "stream-json",
        "--verbose",
    ]

    if system_prompt:
        cmd.extend(["--append-system-prompt", system_prompt])

    if allowed_tools:
        cmd.extend(["--allowedTools", ",".join(allowed_tools)])

    # Resume session for multi-turn conversation
    session_id = get_session_id() if resume_session else None
    if session_id:
        cmd.extend(["--resume", session_id])

    # Start subprocess
    stdin_data = context_text if context_text else None
    work_dir = cwd or os.path.expanduser("~")

    try:
        process = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE if stdin_data else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            cwd=work_dir,
        )
    except FileNotFoundError:
        yield {"type": "error", "message": "Claude CLI not found. Install with: npm install -g @anthropic-ai/claude-code"}
        return
    except OSError as e:
        yield {"type": "error", "message": f"Failed to start claude CLI: {e}"}
        return

    # Write stdin data (script context)
    if stdin_data:
        try:
            process.stdin.write(stdin_data)
            process.stdin.close()
        except (BrokenPipeError, OSError):
            pass

    # Parse NDJSON output
    accumulated_text = ""

    try:
        for line in process.stdout:
            # Check cancellation
            if cancel_flag and cancel_flag.is_set():
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                return

            line = line.strip()
            if not line:
                continue

            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue

            event_type = data.get("type", "")

            if event_type == "system" and data.get("subtype") == "init":
                sid = data.get("session_id", "")
                if sid:
                    set_session_id(sid)
                yield {"type": "session_init", "session_id": sid}

            elif event_type == "assistant":
                # Full or partial assistant message
                msg = data.get("message", {})
                content_blocks = msg.get("content", [])
                for block in content_blocks:
                    if block.get("type") == "text":
                        accumulated_text = block.get("text", "")
                        yield {"type": "text", "text": accumulated_text}

            elif event_type == "result":
                result_text = data.get("result", accumulated_text)
                sid = data.get("session_id", "")
                if sid:
                    set_session_id(sid)
                yield {
                    "type": "result",
                    "text": result_text,
                    "session_id": sid,
                    "cost_usd": data.get("total_cost_usd", 0),
                    "duration_ms": data.get("duration_ms", 0),
                }

    except Exception as e:
        yield {"type": "error", "message": f"Stream error: {e}"}
    finally:
        try:
            process.stdout.close()
            process.stderr.close()
        except Exception:
            pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()

    # Check for errors
    if process.returncode and process.returncode != 0:
        try:
            stderr = process.stderr.read() if process.stderr else ""
        except Exception:
            stderr = ""
        if stderr:
            yield {"type": "error", "message": stderr.strip()}
