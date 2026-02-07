"""Persistent workspace — mirrors Blender text blocks as real files on disk.

Claude CLI operates on these files with its native Read/Edit/Write/Glob/Grep
tools. A bidirectional sync layer keeps them in sync with Blender text blocks.

Workspace location:
  macOS/Linux: /tmp/blender_claude/<sanitized_blend_name>/
  Windows:     %TEMP%\\blender_claude\\<sanitized_blend_name>\\
"""

import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time

# bpy is only available at runtime inside Blender
import bpy


# ---------------------------------------------------------------------------
# Name sanitization
# ---------------------------------------------------------------------------

_UNSAFE_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _sanitize_name(name):
    """Convert a Blender text block name into a safe filename."""
    safe = _UNSAFE_CHARS.sub("_", name)
    # Collapse runs of underscores
    safe = re.sub(r"_+", "_", safe).strip("_")
    if not safe:
        safe = "_unnamed"
    return safe


def _blend_name():
    """Return a sanitized name for the current blend file."""
    path = bpy.data.filepath
    if path:
        base = os.path.splitext(os.path.basename(path))[0]
        return _sanitize_name(base) or "untitled"
    return "untitled"


# ---------------------------------------------------------------------------
# Workspace class
# ---------------------------------------------------------------------------

class Workspace:
    """Manages a disk directory that mirrors Blender's text blocks."""

    def __init__(self, blend_name=None):
        if blend_name is None:
            blend_name = _blend_name()

        tmp = tempfile.gettempdir() if sys.platform != "win32" else os.environ.get("TEMP", tempfile.gettempdir())
        self._root = os.path.join(tmp, "blender_claude", _sanitize_name(blend_name))
        self._meta_dir = os.path.join(self._root, ".blender_claude")

        os.makedirs(self._root, exist_ok=True)
        os.makedirs(self._meta_dir, exist_ok=True)

        # {sanitized_filename: content_hash}
        self._hashes = {}
        # {sanitized_filename: original_blender_name}
        self._name_map = {}
        # {sanitized_filename: mtime_at_last_sync}
        self._mtimes = {}

        self._load_name_map()

    @property
    def root(self):
        return self._root

    # -- Name map persistence --

    def _name_map_path(self):
        return os.path.join(self._meta_dir, "name_map.json")

    def _load_name_map(self):
        path = self._name_map_path()
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    self._name_map = json.load(f)
            except (json.JSONDecodeError, OSError):
                self._name_map = {}

    def _save_name_map(self):
        path = self._name_map_path()
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self._name_map, f, indent=2)
        except OSError:
            pass

    # -- Hashing --

    @staticmethod
    def _hash(content):
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    # -- Sync: Blender → disk --

    def sync_out(self):
        """Write all text blocks to disk. MUST run on main thread."""
        seen = set()

        for text in bpy.data.texts:
            name = text.name
            sanitized = _sanitize_name(name)

            # Handle collisions
            if sanitized in seen:
                base, ext = os.path.splitext(sanitized)
                i = 2
                while f"{base}_{i}{ext}" in seen:
                    i += 1
                sanitized = f"{base}_{i}{ext}"
            seen.add(sanitized)

            self._name_map[sanitized] = name

            content = text.as_string()
            content_hash = self._hash(content)

            # Only write if changed
            if self._hashes.get(sanitized) != content_hash:
                filepath = os.path.join(self._root, sanitized)
                with open(filepath, "w", encoding="utf-8") as f:
                    f.write(content)
                self._hashes[sanitized] = content_hash
                self._mtimes[sanitized] = os.path.getmtime(filepath)

        # Remove files for deleted text blocks
        reverse_map = {v: k for k, v in self._name_map.items()}
        current_names = {t.name for t in bpy.data.texts}
        for blender_name, sanitized in list(reverse_map.items()):
            if blender_name not in current_names:
                filepath = os.path.join(self._root, sanitized)
                if os.path.isfile(filepath):
                    os.remove(filepath)
                self._hashes.pop(sanitized, None)
                self._mtimes.pop(sanitized, None)
                self._name_map.pop(sanitized, None)

        self._save_name_map()

    # -- Sync: disk → Blender --

    def sync_back(self):
        """Read modified/new disk files back into Blender. MUST run on main thread.

        Returns list of {action, name, lines_changed} dicts.
        """
        changes = []

        # Scan all files in workspace root (skip .blender_claude dir)
        for filename in os.listdir(self._root):
            filepath = os.path.join(self._root, filename)
            if not os.path.isfile(filepath) or filename.startswith("."):
                continue

            # Read file
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    content = f.read()
            except OSError:
                continue

            content_hash = self._hash(content)
            if self._hashes.get(filename) == content_hash:
                continue  # Unchanged

            # Resolve back to Blender name
            blender_name = self._name_map.get(filename, filename)

            # Update or create text block
            text = bpy.data.texts.get(blender_name)
            if text is None:
                text = bpy.data.texts.new(blender_name)
                action = "created"
            else:
                action = "modified"

            old_lines = len(text.lines)
            text.clear()
            text.write(content)
            new_lines = len(text.lines)

            self._hashes[filename] = content_hash
            self._mtimes[filename] = os.path.getmtime(filepath)
            self._name_map[filename] = blender_name

            changes.append({
                "action": action,
                "name": blender_name,
                "lines_changed": abs(new_lines - old_lines),
            })

        # Check for new files not in name_map
        # (already handled above — new files get blender_name = filename)

        if changes:
            self._save_name_map()

        return changes

    # -- Lightweight polling --

    def poll_changes(self):
        """Check for changes via mtime. Returns True if any sync was needed.

        Call from a timer. Only does hash check when mtime differs.
        MUST run on main thread (calls sync_out / sync_back internally).
        """
        changed = False

        # Check disk → Blender (files modified externally)
        for filename in os.listdir(self._root):
            filepath = os.path.join(self._root, filename)
            if not os.path.isfile(filepath) or filename.startswith("."):
                continue
            try:
                mtime = os.path.getmtime(filepath)
            except OSError:
                continue
            if mtime != self._mtimes.get(filename):
                # mtime changed — check hash
                try:
                    with open(filepath, "r", encoding="utf-8") as f:
                        content = f.read()
                except OSError:
                    continue
                if self._hash(content) != self._hashes.get(filename):
                    changed = True
                    break
                else:
                    # mtime changed but content same (e.g. touch)
                    self._mtimes[filename] = mtime

        # Check Blender → disk (text blocks modified in UI)
        if not changed:
            for text in bpy.data.texts:
                sanitized = None
                # Find the sanitized name for this text block
                for sname, bname in self._name_map.items():
                    if bname == text.name:
                        sanitized = sname
                        break
                if sanitized is None:
                    # New text block not yet synced
                    changed = True
                    break
                content_hash = self._hash(text.as_string())
                if content_hash != self._hashes.get(sanitized):
                    changed = True
                    break

        if changed:
            self.sync_out()
            result = self.sync_back()
            return True

        return False

    # -- Change summary --

    @staticmethod
    def format_summary(changes):
        """Format a human-readable change summary."""
        if not changes:
            return ""
        parts = []
        for c in changes:
            action = c["action"]
            name = c["name"]
            lines = c.get("lines_changed", 0)
            if action == "created":
                parts.append(f"  + {name} (new file)")
            elif action == "modified":
                detail = f" ({lines} lines changed)" if lines else ""
                parts.append(f"  ~ {name}{detail}")
        return "Workspace sync:\n" + "\n".join(parts)

    # -- Cleanup --

    def cleanup(self):
        """Remove the workspace directory."""
        try:
            shutil.rmtree(self._root, ignore_errors=True)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_workspace = None


def get_workspace():
    """Get or create the workspace for the current blend file. Main thread only."""
    global _workspace
    if _workspace is None:
        _workspace = Workspace()
    return _workspace


def clear_workspace():
    """Cleanup and reset the workspace singleton."""
    global _workspace
    if _workspace is not None:
        _workspace.cleanup()
        _workspace = None
