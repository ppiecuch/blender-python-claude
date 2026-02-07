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


def _tmp_root():
    """Platform-appropriate temp base directory."""
    if sys.platform == "win32":
        return os.environ.get("TEMP", tempfile.gettempdir())
    return tempfile.gettempdir()


# ---------------------------------------------------------------------------
# Workspace class
# ---------------------------------------------------------------------------

class Workspace:
    """Manages a disk directory that mirrors Blender's text blocks."""

    def __init__(self, blend_name=None):
        if blend_name is None:
            blend_name = _blend_name()
        self._blend_name = blend_name

        self._root = os.path.join(_tmp_root(), "blender_claude", _sanitize_name(blend_name))
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

    # -- Helpers --

    def _unique_sanitized(self, sanitized, seen):
        """Return a sanitized name that doesn't collide with *seen*."""
        if sanitized not in seen:
            return sanitized
        base, ext = os.path.splitext(sanitized)
        i = 2
        while f"{base}_{i}{ext}" in seen:
            i += 1
        return f"{base}_{i}{ext}"

    # -- Sync: Blender → disk --

    def sync_out(self):
        """Write all text blocks to disk. MUST run on main thread.

        Detects text-block renames via content-hash matching.
        """
        current_names = {t.name for t in bpy.data.texts}
        # Reverse map: blender_name → sanitized
        old_reverse = {v: k for k, v in self._name_map.items()}

        seen = set()

        for text in bpy.data.texts:
            name = text.name
            content = text.as_string()
            content_hash = self._hash(content)

            if name in old_reverse:
                # Existing mapping — reuse sanitized name
                sanitized = old_reverse[name]
                sanitized = self._unique_sanitized(sanitized, seen)
            else:
                # New or renamed text block — check for rename by hash match
                sanitized = None
                for old_san, old_bname in list(self._name_map.items()):
                    if (old_bname not in current_names
                            and self._hashes.get(old_san) == content_hash):
                        # Rename detected: old_bname → name
                        new_san = self._unique_sanitized(
                            _sanitize_name(name), seen)
                        old_path = os.path.join(self._root, old_san)
                        new_path = os.path.join(self._root, new_san)
                        if os.path.isfile(old_path):
                            os.rename(old_path, new_path)
                        # Migrate state
                        self._hashes[new_san] = self._hashes.pop(old_san, "")
                        self._mtimes[new_san] = self._mtimes.pop(old_san, 0)
                        self._name_map.pop(old_san, None)
                        sanitized = new_san
                        break

                if sanitized is None:
                    # Truly new text block
                    sanitized = self._unique_sanitized(
                        _sanitize_name(name), seen)

            seen.add(sanitized)
            self._name_map[sanitized] = name

            # Write if content changed
            if self._hashes.get(sanitized) != content_hash:
                filepath = os.path.join(self._root, sanitized)
                with open(filepath, "w", encoding="utf-8") as f:
                    f.write(content)
                self._hashes[sanitized] = content_hash
                self._mtimes[sanitized] = os.path.getmtime(filepath)

        # Remove orphaned files (deleted text blocks not caught as renames)
        for san, bname in list(self._name_map.items()):
            if bname not in current_names:
                filepath = os.path.join(self._root, san)
                if os.path.isfile(filepath):
                    os.remove(filepath)
                self._hashes.pop(san, None)
                self._mtimes.pop(san, None)
                del self._name_map[san]

        self._save_name_map()

    # -- Sync: disk → Blender --

    def sync_back(self):
        """Read modified/new disk files back into Blender. MUST run on main thread.

        Returns list of {action, name, lines_changed} dicts.
        Last-write-wins: disk content overwrites Blender if hashes differ.
        """
        changes = []

        for filename in os.listdir(self._root):
            filepath = os.path.join(self._root, filename)
            if not os.path.isfile(filepath) or filename.startswith("."):
                continue

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

        if changes:
            self._save_name_map()

        return changes

    # -- Lightweight polling --

    def poll_changes(self):
        """Check for changes via mtime. Returns True if any sync was needed.

        Call from a timer. Only does hash check when mtime differs.
        MUST run on main thread (calls sync_out / sync_back internally).
        """
        disk_changed = False
        blender_changed = False

        # Check disk side (files modified externally)
        try:
            entries = os.listdir(self._root)
        except OSError:
            return False

        for filename in entries:
            filepath = os.path.join(self._root, filename)
            if not os.path.isfile(filepath) or filename.startswith("."):
                continue
            try:
                mtime = os.path.getmtime(filepath)
            except OSError:
                continue
            if mtime != self._mtimes.get(filename):
                try:
                    with open(filepath, "r", encoding="utf-8") as f:
                        content = f.read()
                except OSError:
                    continue
                if self._hash(content) != self._hashes.get(filename):
                    disk_changed = True
                    break
                else:
                    self._mtimes[filename] = mtime

        # Check Blender side (text blocks modified in UI)
        for text in bpy.data.texts:
            found = False
            for sname, bname in self._name_map.items():
                if bname == text.name:
                    found = True
                    if self._hash(text.as_string()) != self._hashes.get(sname):
                        blender_changed = True
                    break
            if not found:
                blender_changed = True  # New/renamed text block
            if blender_changed:
                break

        if not disk_changed and not blender_changed:
            return False

        # Sync: push Blender state first, then pull disk changes.
        # Net effect: if BOTH sides changed the same file, disk wins
        # (sync_back overwrites what sync_out just wrote).
        self.sync_out()
        self.sync_back()
        return True

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
    """Get or create the workspace for the current blend file. Main thread only.

    Detects blend-file renames (Save As) and migrates to a new workspace.
    """
    global _workspace
    if _workspace is not None:
        current = _blend_name()
        if _workspace._blend_name != current:
            # Blend file was renamed / Save As — migrate
            _workspace.cleanup()
            _workspace = None
    if _workspace is None:
        _workspace = Workspace()
    return _workspace


def clear_workspace():
    """Cleanup and reset the workspace singleton."""
    global _workspace
    if _workspace is not None:
        _workspace.cleanup()
        _workspace = None
