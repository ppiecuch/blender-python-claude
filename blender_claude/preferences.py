"""Addon preferences: API key, model selection, backend, settings."""

import os
import subprocess
import json

import bpy
from bpy.props import EnumProperty, IntProperty, StringProperty


_cached_keychain_key = None


def _read_keychain_api_key():
    """Try to read the Anthropic API key from macOS Keychain (cached)."""
    global _cached_keychain_key
    if _cached_keychain_key is not None:
        return _cached_keychain_key

    import sys
    if sys.platform != "darwin":
        _cached_keychain_key = ""
        return ""

    # Method 1: Internet password (raw API key stored by Claude Code)
    # Keychain entry: server="https://api.anthropic.com", account="Bearer"
    try:
        result = subprocess.run(
            ["security", "find-internet-password",
             "-s", "https://api.anthropic.com", "-a", "Bearer", "-w"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            key = result.stdout.strip()
            if key.startswith("sk-ant-"):
                _cached_keychain_key = key
                return key
    except Exception:
        pass

    # Method 2: Generic password (Claude Code credentials JSON)
    # Keychain entry: service="Claude Code-credentials"
    try:
        result = subprocess.run(
            ["security", "find-generic-password",
             "-s", "Claude Code-credentials", "-w"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            raw = result.stdout.strip()
            try:
                data = json.loads(raw)
                for key_name in ("claudeAiApiKey", "apiKey", "anthropic_api_key"):
                    if key_name in data and data[key_name]:
                        _cached_keychain_key = data[key_name]
                        return _cached_keychain_key
            except (json.JSONDecodeError, TypeError):
                if raw.startswith("sk-ant-"):
                    _cached_keychain_key = raw
                    return raw
    except Exception:
        pass

    _cached_keychain_key = ""
    return ""


def _check_cli_available():
    """Check if claude CLI is installed (cached)."""
    import shutil
    return shutil.which("claude") is not None


class CLAUDE_AddonPreferences(bpy.types.AddonPreferences):
    bl_idname = __package__

    backend: EnumProperty(
        name="Backend",
        description="How to connect to Claude",
        items=[
            ("CLI", "Claude Code CLI",
             "Uses your Claude subscription (Pro/Max/Team). Requires 'claude' CLI installed"),
            ("API", "Direct API",
             "Uses prepaid API credits from console.anthropic.com. Requires API key"),
        ],
        default="CLI",
    )  # type: ignore

    api_key: StringProperty(
        name="API Key",
        description="Anthropic API key (auto-detected from Keychain, env var, or set manually)",
        default="",
        subtype="PASSWORD",
    )  # type: ignore

    model: EnumProperty(
        name="Model",
        description="Claude model to use (API backend only; CLI uses its configured model)",
        items=[
            ("claude-sonnet-4-5-20250929", "Claude Sonnet 4.5", "Fast and capable (recommended)"),
            ("claude-opus-4-6", "Claude Opus 4.6", "Most capable, slower"),
            ("claude-haiku-4-5-20251001", "Claude Haiku 4.5", "Fastest, lightweight tasks"),
        ],
        default="claude-sonnet-4-5-20250929",
    )  # type: ignore

    max_tool_iterations: IntProperty(
        name="Max tool iterations",
        description="Maximum tool-use round-trips per request (API backend only)",
        default=10,
        min=1,
        max=30,
    )  # type: ignore

    max_tokens: IntProperty(
        name="Max tokens",
        description="Maximum tokens in Claude's response (API backend only)",
        default=4096,
        min=256,
        max=32768,
    )  # type: ignore

    def draw(self, context):
        layout = self.layout

        # Backend selection
        layout.prop(self, "backend")

        cli_ok = _check_cli_available()

        if self.backend == "CLI":
            if cli_ok:
                layout.label(text="Claude CLI found - uses your subscription", icon="CHECKMARK")
            else:
                box = layout.box()
                box.label(text="Claude CLI not found", icon="ERROR")
                box.label(text="Install: npm install -g @anthropic-ai/claude-code")
                box.label(text="Then run: claude login")
        else:
            # API backend settings
            layout.prop(self, "api_key")
            layout.prop(self, "model")

            row = layout.row()
            row.prop(self, "max_tool_iterations")
            row.prop(self, "max_tokens")

            key = self.get_api_key()
            if key:
                source = "preferences" if self.api_key else "auto-detected"
                layout.label(text=f"API key: {source} (sk-ant-...{key[-4:]})", icon="CHECKMARK")
            else:
                layout.label(text="No API key found. Set above or ANTHROPIC_API_KEY env var.", icon="ERROR")

    def get_api_key(self):
        """Get API key from preferences, environment, or macOS Keychain."""
        if self.api_key:
            return self.api_key
        env_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if env_key:
            return env_key
        return _read_keychain_api_key()


def get_prefs():
    """Get addon preferences."""
    return bpy.context.preferences.addons[__package__].preferences
