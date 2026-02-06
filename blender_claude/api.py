"""Lightweight Anthropic Messages API client using requests + SSE streaming.

Zero external dependencies beyond requests (bundled with Bforartists).
"""

import json
import requests


API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
DEFAULT_MAX_TOKENS = 4096


class APIError(Exception):
    """Raised when the Anthropic API returns an error."""

    def __init__(self, message, status_code=None, error_type=None):
        super().__init__(message)
        self.status_code = status_code
        self.error_type = error_type


def stream_messages(api_key, model, system, messages, tools=None,
                    max_tokens=DEFAULT_MAX_TOKENS):
    """Stream a message from the Anthropic API.

    Yields event dicts with these types:
        {"type": "text_delta", "text": "..."}
        {"type": "tool_use_start", "index": N, "id": "...", "name": "..."}
        {"type": "tool_input_delta", "index": N, "partial_json": "..."}
        {"type": "tool_use_complete", "index": N, "id": "...", "name": "...", "input": {...}}
        {"type": "content_block_stop", "index": N}
        {"type": "message_complete", "stop_reason": "...", "usage": {...}}
        {"type": "error", "message": "..."}
    """
    headers = {
        "x-api-key": api_key,
        "anthropic-version": API_VERSION,
        "content-type": "application/json",
    }

    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "stream": True,
        "system": system,
        "messages": messages,
    }
    if tools:
        payload["tools"] = tools

    response = requests.post(
        API_URL,
        headers=headers,
        json=payload,
        stream=True,
        timeout=300,
    )

    if response.status_code != 200:
        try:
            err = response.json()
            msg = err.get("error", {}).get("message", response.text)
            etype = err.get("error", {}).get("type", "unknown")
        except (json.JSONDecodeError, KeyError):
            msg = response.text
            etype = "http_error"
        raise APIError(msg, response.status_code, etype)

    # Parse SSE stream
    current_event = None
    # Track tool_use blocks being built
    tool_blocks = {}  # index -> {"id": ..., "name": ..., "input_json": ""}

    try:
        for line in response.iter_lines(decode_unicode=True):
            if line is None:
                continue
            if not line:
                continue

            if line.startswith("event: "):
                current_event = line[7:]
                continue

            if not line.startswith("data: "):
                continue

            try:
                data = json.loads(line[6:])
            except json.JSONDecodeError:
                continue

            event_type = data.get("type", "")

            if event_type == "content_block_start":
                block = data.get("content_block", {})
                index = data.get("index", 0)
                if block.get("type") == "tool_use":
                    tool_blocks[index] = {
                        "id": block.get("id", ""),
                        "name": block.get("name", ""),
                        "input_json": "",
                    }
                    yield {
                        "type": "tool_use_start",
                        "index": index,
                        "id": block["id"],
                        "name": block["name"],
                    }

            elif event_type == "content_block_delta":
                index = data.get("index", 0)
                delta = data.get("delta", {})
                delta_type = delta.get("type", "")

                if delta_type == "text_delta":
                    yield {
                        "type": "text_delta",
                        "text": delta.get("text", ""),
                    }
                elif delta_type == "input_json_delta":
                    partial = delta.get("partial_json", "")
                    if index in tool_blocks:
                        tool_blocks[index]["input_json"] += partial
                    yield {
                        "type": "tool_input_delta",
                        "index": index,
                        "partial_json": partial,
                    }

            elif event_type == "content_block_stop":
                index = data.get("index", 0)
                if index in tool_blocks:
                    tb = tool_blocks.pop(index)
                    try:
                        tool_input = json.loads(tb["input_json"]) if tb["input_json"] else {}
                    except json.JSONDecodeError:
                        tool_input = {"_raw": tb["input_json"]}
                    yield {
                        "type": "tool_use_complete",
                        "index": index,
                        "id": tb["id"],
                        "name": tb["name"],
                        "input": tool_input,
                    }
                else:
                    yield {"type": "content_block_stop", "index": index}

            elif event_type == "message_delta":
                delta = data.get("delta", {})
                usage = data.get("usage", {})
                yield {
                    "type": "message_complete",
                    "stop_reason": delta.get("stop_reason", ""),
                    "usage": usage,
                }

            elif event_type == "message_stop":
                pass  # Final event, nothing to do

            elif event_type == "error":
                err = data.get("error", {})
                yield {
                    "type": "error",
                    "message": err.get("message", str(data)),
                }

    finally:
        response.close()


def send_messages(api_key, model, system, messages, tools=None,
                  max_tokens=DEFAULT_MAX_TOKENS):
    """Non-streaming message send. Returns the full response dict."""
    headers = {
        "x-api-key": api_key,
        "anthropic-version": API_VERSION,
        "content-type": "application/json",
    }

    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": messages,
    }
    if tools:
        payload["tools"] = tools

    response = requests.post(
        API_URL,
        headers=headers,
        json=payload,
        timeout=300,
    )

    if response.status_code != 200:
        try:
            err = response.json()
            msg = err.get("error", {}).get("message", response.text)
            etype = err.get("error", {}).get("type", "unknown")
        except (json.JSONDecodeError, KeyError):
            msg = response.text
            etype = "http_error"
        raise APIError(msg, response.status_code, etype)

    return response.json()
