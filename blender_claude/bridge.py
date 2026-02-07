"""Threading bridge: safely execute functions on Blender's main thread.

Pattern: background thread -> queue.Queue -> bpy.app.timers callback -> main thread
All bpy.* calls MUST go through this bridge when called from a background thread.
"""

import queue
import threading
import traceback

import bpy


class _ResultHolder:
    """Holds a result from a main-thread execution, with event-based waiting."""

    __slots__ = ("_event", "result", "error")

    def __init__(self):
        self._event = threading.Event()
        self.result = None
        self.error = None

    def set(self, result):
        self.result = result
        self._event.set()

    def set_error(self, error):
        self.error = error
        self._event.set()

    def wait(self, timeout=60):
        self._event.wait(timeout)
        if not self._event.is_set():
            raise TimeoutError("Main thread execution timed out")
        if self.error:
            raise self.error
        return self.result


class MainThreadBridge:
    """Bridges background threads to Blender's main thread via a timer."""

    def __init__(self):
        self._queue = queue.Queue()
        self._timer_registered = False
        self._streaming_text = ""
        self._lock = threading.Lock()
        self._poll_tick = 0

    # -- Streaming text (lockless-ish, updated frequently) --

    def set_streaming_text(self, text):
        """Called from background thread to update streaming display."""
        with self._lock:
            self._streaming_text = text

    def get_streaming_text(self):
        """Called from main thread (timer/UI) to read current streaming text."""
        with self._lock:
            return self._streaming_text

    def clear_streaming_text(self):
        with self._lock:
            self._streaming_text = ""

    # -- Queue-based execution --

    def schedule(self, fn):
        """Schedule a no-arg callable to run on the main thread. Fire-and-forget."""
        self._queue.put(fn)

    def execute_on_main(self, fn, *args, **kwargs):
        """Execute fn(*args, **kwargs) on the main thread and return a ResultHolder.

        The background thread should call holder.wait() to block until completion.
        """
        holder = _ResultHolder()

        def _wrapper():
            try:
                result = fn(*args, **kwargs)
                holder.set(result)
            except Exception as e:
                holder.set_error(e)

        self._queue.put(_wrapper)
        return holder

    # -- Timer management --

    def _process_queue(self):
        """Timer callback: drain the queue and update streaming UI."""
        # Guard: if we've been unregistered, stop immediately
        if not self._timer_registered:
            return None  # Unregister timer

        # Process all queued functions
        processed = 0
        while not self._queue.empty() and processed < 50:
            fn = self._queue.get()
            try:
                fn()
            except Exception:
                traceback.print_exc()
            processed += 1

        # Update streaming text in the UI property
        try:
            scene = getattr(bpy.context, "scene", None)
            if scene is None:
                return 0.1
            claude_state = getattr(scene, "claude", None)
            if claude_state and claude_state.is_generating:
                text = self.get_streaming_text()
                if claude_state.streaming_text != text:
                    claude_state.streaming_text = text
                    _tag_redraw_text_editors()
        except (ReferenceError, AttributeError):
            pass

        # Poll workspace for external changes every ~2s (20 ticks × 100ms)
        # Skip during generation — the CLI worker does explicit sync
        self._poll_tick += 1
        if self._poll_tick >= 20:
            self._poll_tick = 0
            is_generating = False
            try:
                s = getattr(bpy.context, "scene", None)
                if s and hasattr(s, "claude"):
                    is_generating = s.claude.is_generating
            except (ReferenceError, AttributeError):
                pass
            if not is_generating:
                try:
                    from . import workspace as _ws
                    ws = _ws._workspace
                    if ws is not None:
                        ws.poll_changes()
                except Exception:
                    pass

        return 0.1  # Check every 100ms

    def register(self):
        """Register the timer. Call from register()."""
        if not self._timer_registered:
            bpy.app.timers.register(self._process_queue, persistent=True)
            self._timer_registered = True

    def unregister(self):
        """Unregister the timer. Call from unregister()."""
        self._timer_registered = False  # Signal timer to stop on next tick
        try:
            if bpy.app.timers.is_registered(self._process_queue):
                bpy.app.timers.unregister(self._process_queue)
        except (ValueError, RuntimeError):
            pass
        # Drain remaining queue items to unblock any waiting threads
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break


def _tag_redraw_text_editors():
    """Tag all TEXT_EDITOR areas for redraw."""
    try:
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                if area.type == "TEXT_EDITOR":
                    area.tag_redraw()
    except Exception:
        pass


# Singleton bridge instance
bridge = MainThreadBridge()
