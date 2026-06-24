"""
Codex Proxy (FastAPI) — OpenAI-compatible API backed by a ChatGPT login.

Accepts OpenAI-format requests (chat completions), authenticates
to ChatGPT via OAuth PKCE, and forwards to the Codex backend
(`chatgpt.com/backend-api/codex/responses`), translating the Responses API SSE
stream back into chat completions format.

Interactive docs (OpenAPI/Swagger): http://localhost:5001/docs
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
from contextlib import asynccontextmanager
from typing import Any, Optional, Union

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field


# ─── Config (env) ────────────────────────────────────────────────────


def _env_bool(name: str, default: str = "") -> bool:
    return os.getenv(name, default).lower() in {"1", "true", "yes", "on"}


CODEX_BACKEND = os.getenv("CODEX_BACKEND", "https://chatgpt.com/backend-api/codex/responses")
CODEX_MODEL = os.getenv("CODEX_MODEL", "gpt-5.4-mini")
CODEX_REASONING_EFFORT = os.getenv("CODEX_REASONING_EFFORT", "medium")
CODEX_REASONING_SUMMARY = os.getenv("CODEX_REASONING_SUMMARY", "auto")
BACKEND_TIMEOUT = float(os.getenv("BACKEND_TIMEOUT", "600"))
PROXY_DEBUG_EVENTS = _env_bool("PROXY_DEBUG_EVENTS")
PROXY_API_KEY = os.getenv("PROXY_API_KEY", "")
TOKEN_FILE = os.getenv("TOKEN_FILE", "tokens.json")

# OAuth (public codex-cli client — https://github.com/openai/codex)
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
AUTH_URL = "https://auth.openai.com/oauth/authorize"
TOKEN_URL = "https://auth.openai.com/oauth/token"
CALLBACK_PORT = 1455
CALLBACK_PATH = "/auth/callback"
REDIRECT_URI = f"http://localhost:{CALLBACK_PORT}{CALLBACK_PATH}"
SCOPES = "openid profile email offline_access"
AUDIENCE = "https://api.openai.com/v1"

TERMINAL_RESPONSE_EVENTS = {
    "response.completed",
    "response.failed",
    "response.cancelled",
    "response.incomplete",
}

# Models listed by GET /v1/models — overridable via CODEX_MODELS (comma-separated)
# so the list survives OpenAI model updates. Cosmetic: does not restrict usage.
MODEL_IDS = [
    m.strip()
    for m in (
        os.getenv("CODEX_MODELS")
        or "gpt-5.5,gpt-5.5-pro,gpt-5.4,gpt-5.4-pro,gpt-5.4-mini,gpt-5.4-nano"
    ).split(",")
    if m.strip()
]


# ─── JWT / PKCE utils ────────────────────────────────────────────────


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _decode_jwt_payload(token: str) -> dict:
    try:
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        return json.loads(base64.urlsafe_b64decode(payload_b64))
    except Exception:
        return {}


# ─── OAuth PKCE flow ─────────────────────────────────────────────────


def oauth_login() -> dict:
    code_verifier = _b64url(secrets.token_bytes(32))
    code_challenge = _b64url(hashlib.sha256(code_verifier.encode()).digest())
    state = _b64url(secrets.token_bytes(16))

    params = urllib.parse.urlencode(
        {
            "client_id": CLIENT_ID,
            "response_type": "code",
            "redirect_uri": REDIRECT_URI,
            "scope": SCOPES,
            "audience": AUDIENCE,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "state": state,
            "codex_cli_simplified_flow": "true",
            "id_token_add_organizations": "true",
        }
    )
    auth_url = f"{AUTH_URL}?{params}"
    result: dict[str, Optional[str]] = {"code": None, "error": None}

    class CallbackHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            qs = urllib.parse.parse_qs(parsed.query)
            if parsed.path == CALLBACK_PATH:
                if qs.get("state", [None])[0] != state:
                    result["error"] = "state_mismatch"
                    html = "<h2>Error: state mismatch</h2>"
                elif "code" in qs:
                    result["code"] = qs["code"][0]
                    html = "<h2>Login successful!</h2><p>You can close this tab.</p>"
                else:
                    result["error"] = qs.get("error", ["unknown"])[0]
                    html = f"<h2>Error: {result['error']}</h2>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(f"<html><body>{html}</body></html>".encode())
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("0.0.0.0", CALLBACK_PORT), CallbackHandler)
    print("\n" + "=" * 64)
    print("  LOGIN REQUIRED — open this URL in your browser:")
    print(f"\n  {auth_url}\n")
    print("  (the redirect returns to http://localhost:1455 — publish that")
    print("   port in Docker for the callback to work)")
    print("=" * 64 + "\n")
    try:
        webbrowser.open(auth_url)
    except Exception:
        pass

    server.timeout = 300
    while result["code"] is None and result["error"] is None:
        server.handle_request()
    server.server_close()

    if result["error"]:
        raise RuntimeError(f"OAuth failed: {result['error']}")
    if not result["code"]:
        raise RuntimeError("OAuth timeout (no callback received)")

    token_data = json.dumps(
        {
            "grant_type": "authorization_code",
            "client_id": CLIENT_ID,
            "code": result["code"],
            "redirect_uri": REDIRECT_URI,
            "code_verifier": code_verifier,
        }
    ).encode()
    req = urllib.request.Request(
        TOKEN_URL,
        data=token_data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


# ─── Token manager ───────────────────────────────────────────────────


class TokenManager:
    def __init__(self, token_file: str):
        self.token_file = token_file
        self.access_token = ""
        self.refresh_token = ""
        self.id_token = ""
        self.token_expiry = 0
        self.account_id = ""
        self._lock = threading.Lock()

    def load_or_login(self):
        if os.path.exists(self.token_file):
            try:
                with open(self.token_file, "r") as f:
                    data = json.load(f)
                self.access_token = data.get("access_token", "")
                self.refresh_token = data.get("refresh_token", "")
                self.id_token = data.get("id_token", "")
                self.token_expiry = _decode_jwt_payload(self.access_token).get("exp", 0)
                self._extract_account_id()
                if self.refresh_token:
                    remaining = self.token_expiry - time.time()
                    if remaining > 0:
                        print(f"[AUTH] Tokens loaded (valid for {int(remaining)}s)")
                    else:
                        print("[AUTH] Token expired, refreshing...")
                        self._refresh()
                    return
            except Exception as e:
                print(f"[AUTH] Warning while loading tokens: {e}")
        self._apply_tokens(oauth_login())

    def _extract_account_id(self):
        payload = _decode_jwt_payload(self.access_token)
        self.account_id = payload.get("https://api.openai.com/auth", {}).get(
            "chatgpt_account_id", ""
        )

    def _apply_tokens(self, tokens: dict):
        self.access_token = tokens["access_token"]
        self.refresh_token = tokens.get("refresh_token", self.refresh_token)
        self.id_token = tokens.get("id_token", self.id_token)
        self.token_expiry = _decode_jwt_payload(self.access_token).get("exp", 0)
        self._extract_account_id()
        self._save()
        print(f"[AUTH] Account: {self.account_id}")

    def _refresh(self):
        print("[AUTH] Refreshing token...")
        data = json.dumps(
            {
                "grant_type": "refresh_token",
                "client_id": CLIENT_ID,
                "refresh_token": self.refresh_token,
                "scope": SCOPES,
            }
        ).encode()
        req = urllib.request.Request(
            TOKEN_URL,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                self._apply_tokens(json.loads(resp.read()))
            print("[AUTH] Token refreshed")
        except urllib.error.HTTPError:
            print("[AUTH] Refresh failed, logging in again...")
            self._apply_tokens(oauth_login())

    def _save(self):
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self.token_file)), exist_ok=True)
            with open(self.token_file, "w") as f:
                json.dump(
                    {
                        "access_token": self.access_token,
                        "refresh_token": self.refresh_token,
                        "id_token": self.id_token,
                    },
                    f,
                    indent=2,
                )
        except Exception as e:
            print(f"[AUTH] Warning while saving tokens: {e}")

    def get_token(self) -> str:
        with self._lock:
            if time.time() > (self.token_expiry - 60):
                self._refresh()
            return self.access_token


token_manager = TokenManager(TOKEN_FILE)


def backend_headers() -> dict:
    return {
        "Authorization": f"Bearer {token_manager.get_token()}",
        "Content-Type": "application/json",
        "chatgpt-account-id": token_manager.account_id,
        "originator": "codex_cli_rs",
        "OpenAI-Beta": "responses=experimental",
        "User-Agent": "codex-cli/0.111.0",
    }


# ─── SSE parsing ─────────────────────────────────────────────────────


def iter_sse_events(resp):
    for raw_line in resp:
        line = raw_line.decode(errors="replace").strip()
        if not line.startswith("data: "):
            continue
        data = line[6:]
        if data == "[DONE]":
            break
        try:
            yield json.loads(data)
        except json.JSONDecodeError:
            continue


def _delta_to_text(delta) -> str:
    if isinstance(delta, str):
        return delta
    if isinstance(delta, dict):
        for key in ("text", "content", "summary"):
            value = delta.get(key)
            if isinstance(value, str):
                return value
    return ""


def extract_reasoning_delta(event: dict) -> str:
    event_type = event.get("type", "")
    if "reasoning" not in event_type or not event_type.endswith(".delta"):
        return ""
    return _delta_to_text(event.get("delta", ""))


def _content_to_text(content) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict):
                if isinstance(part.get("text"), str):
                    parts.append(part["text"])
                elif part.get("type") == "text" and isinstance(part.get("content"), str):
                    parts.append(part["content"])
                else:
                    parts.append(json.dumps(part, ensure_ascii=False))
            else:
                parts.append(str(part))
        return "".join(parts)
    return str(content)


# ─── Tool-call conversion (chat <-> responses) ───────────────────────


def normalize_tool_parameters(parameters) -> dict:
    if not isinstance(parameters, dict):
        return {"type": "object", "properties": {}}
    schema = dict(parameters)
    if schema.get("type") is None:
        schema["type"] = "object"
    if schema.get("type") == "object":
        properties = schema.get("properties")
        if not isinstance(properties, dict):
            properties = {}
        for name in schema.get("required") or []:
            if isinstance(name, str):
                properties.setdefault(name, {})
        schema["properties"] = properties
    return schema


def convert_chat_tools(tools) -> list:
    if not isinstance(tools, list):
        return []
    converted = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        if tool.get("type") == "function" and isinstance(tool.get("function"), dict):
            function = tool["function"]
            name = function.get("name")
            if not name:
                continue
            response_tool = {
                "type": "function",
                "name": name,
                "parameters": normalize_tool_parameters(function.get("parameters")),
            }
            if isinstance(function.get("description"), str):
                response_tool["description"] = function["description"]
            if "strict" in function:
                response_tool["strict"] = bool(function["strict"])
            converted.append(response_tool)
        elif tool.get("type") == "function" and isinstance(tool.get("name"), str):
            response_tool = dict(tool)
            response_tool["parameters"] = normalize_tool_parameters(response_tool.get("parameters"))
            converted.append(response_tool)
        else:
            converted.append(dict(tool))
    return converted


def convert_tool_choice(tool_choice):
    if tool_choice is None:
        return None
    if isinstance(tool_choice, str):
        return tool_choice
    if not isinstance(tool_choice, dict):
        return None
    if tool_choice.get("type") == "function":
        function = tool_choice.get("function")
        name = function.get("name") if isinstance(function, dict) else tool_choice.get("name")
        if name:
            return {"type": "function", "name": name}
    return dict(tool_choice)


def convert_chat_tool_call(tool_call: dict) -> Optional[dict]:
    if not isinstance(tool_call, dict):
        return None
    function = tool_call.get("function") if isinstance(tool_call.get("function"), dict) else {}
    name = function.get("name") or tool_call.get("name")
    if not name:
        return None
    return {
        "type": "function_call",
        "call_id": tool_call.get("id") or tool_call.get("call_id") or f"call_{uuid.uuid4().hex}",
        "name": name,
        "arguments": function.get("arguments") or tool_call.get("arguments") or "",
        "status": "completed",
    }


def response_function_call_to_chat(item: dict) -> Optional[dict]:
    if not isinstance(item, dict) or item.get("type") != "function_call":
        return None
    name = item.get("name")
    if not name:
        return None
    return {
        "id": item.get("call_id") or item.get("id") or f"call_{uuid.uuid4().hex}",
        "type": "function",
        "function": {"name": name, "arguments": item.get("arguments") or ""},
    }


def extract_response_tool_calls(response: dict) -> list:
    if not isinstance(response, dict):
        return []
    tool_calls = []
    for item in response.get("output") or []:
        tool_call = response_function_call_to_chat(item)
        if tool_call:
            tool_calls.append(tool_call)
    return tool_calls


# ─── chat request -> responses payload ───────────────────────────────


def _convert_content(content, role):
    if content is None:
        return []
    if isinstance(content, str):
        t = "output_text" if role == "assistant" else "input_text"
        return [{"type": t, "text": content}]
    if not isinstance(content, list):
        t = "output_text" if role == "assistant" else "input_text"
        return [{"type": t, "text": str(content)}]
    converted = []
    for part in content:
        if part.get("type") == "text":
            t = "output_text" if role == "assistant" else "input_text"
            converted.append({"type": t, "text": part["text"]})
        elif part.get("type") == "image_url":
            url = (
                part["image_url"].get("url", "")
                if isinstance(part.get("image_url"), dict)
                else part.get("image_url", "")
            )
            converted.append({"type": "input_image", "image_url": url})
        else:
            converted.append(part)
    return converted


def build_responses_payload(chat_data: dict) -> dict:
    model = chat_data.get("model") or CODEX_MODEL
    messages = chat_data.get("messages", [])

    system_msgs = [
        m["content"]
        for m in messages
        if m.get("role") == "system" and isinstance(m.get("content"), str)
    ]
    instructions = "\n".join(system_msgs) if system_msgs else "You are a helpful assistant."

    input_items: list[dict] = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if role == "system":
            continue
        if role == "tool":
            call_id = (
                msg.get("tool_call_id")
                or msg.get("call_id")
                or msg.get("name")
                or f"call_{uuid.uuid4().hex}"
            )
            input_items.append(
                {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": _content_to_text(content),
                }
            )
            continue
        if role == "assistant":
            if content:
                input_items.append(
                    {"type": "message", "role": role, "content": _convert_content(content, role)}
                )
            for tool_call in msg.get("tool_calls") or []:
                converted = convert_chat_tool_call(tool_call)
                if converted:
                    input_items.append(converted)
            legacy = msg.get("function_call")
            if isinstance(legacy, dict):
                converted = convert_chat_tool_call(
                    {"id": legacy.get("name") or f"call_{uuid.uuid4().hex}", "function": legacy}
                )
                if converted:
                    input_items.append(converted)
            continue
        if role == "function":
            call_id = msg.get("tool_call_id") or msg.get("name") or f"call_{uuid.uuid4().hex}"
            input_items.append(
                {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": _content_to_text(content),
                }
            )
            continue
        input_items.append(
            {"type": "message", "role": role, "content": _convert_content(content, role)}
        )

    resp_body: dict[str, Any] = {
        "model": model,
        "instructions": instructions,
        "input": input_items,
        "store": False,
        "stream": True,  # backend requires stream=true
    }

    reasoning = chat_data.get("reasoning")
    reasoning_payload: dict[str, Any] = {}
    if isinstance(reasoning, dict):
        reasoning_payload.update(reasoning)
    else:
        effort = chat_data.get("reasoning_effort") or CODEX_REASONING_EFFORT
        if isinstance(effort, str) and effort:
            reasoning_payload["effort"] = effort
        summary = chat_data.get("reasoning_summary") or CODEX_REASONING_SUMMARY
        if isinstance(summary, str) and summary:
            reasoning_payload["summary"] = summary
    if reasoning_payload:
        resp_body["reasoning"] = reasoning_payload

    tools = convert_chat_tools(chat_data.get("tools"))
    if tools:
        resp_body["tools"] = tools
    tool_choice = convert_tool_choice(chat_data.get("tool_choice"))
    if tool_choice is not None:
        resp_body["tool_choice"] = tool_choice
    if "parallel_tool_calls" in chat_data:
        resp_body["parallel_tool_calls"] = bool(chat_data["parallel_tool_calls"])
    # temperature/top_p/max_tokens are dropped: the codex backend may reject them.
    return resp_body


def collect_sse_response(resp) -> dict:
    text_parts, reasoning_parts = [], []
    tool_call_states: dict[Any, dict] = {}
    tool_calls: list = []
    usage: dict = {}
    model = CODEX_MODEL
    response_id = ""

    for event in iter_sse_events(resp):
        event_type = event.get("type", "")
        if event_type == "response.created":
            response = event.get("response", {})
            response_id = response.get("id", response_id)
            model = response.get("model", model)
        elif event_type == "response.output_text.delta":
            text_parts.append(event.get("delta", ""))
        elif event_type == "response.output_item.added":
            item = event.get("item", {})
            if isinstance(item, dict) and item.get("type") == "function_call":
                key = event.get("output_index", item.get("id"))
                tool_call_states[key] = {
                    "id": item.get("call_id") or item.get("id") or f"call_{uuid.uuid4().hex}",
                    "type": "function",
                    "function": {
                        "name": item.get("name", ""),
                        "arguments": item.get("arguments") or "",
                    },
                }
        elif event_type == "response.function_call_arguments.delta":
            key = event.get("output_index", event.get("item_id"))
            state = tool_call_states.setdefault(
                key,
                {
                    "id": f"call_{uuid.uuid4().hex}",
                    "type": "function",
                    "function": {"name": "", "arguments": ""},
                },
            )
            state["function"]["arguments"] += event.get("delta") or ""
        elif event_type == "response.output_item.done":
            item = event.get("item", {})
            tool_call = response_function_call_to_chat(item)
            if tool_call:
                key = event.get("output_index", item.get("id"))
                tool_call_states[key] = tool_call
        elif event_type in TERMINAL_RESPONSE_EVENTS:
            response = event.get("response", {})
            usage = response.get("usage", usage)
            response_id = response.get("id", response_id)
            model = response.get("model", model)
            final_tool_calls = extract_response_tool_calls(response)
            if final_tool_calls:
                tool_calls = final_tool_calls
            if event_type != "response.completed":
                return {
                    "text": "".join(text_parts),
                    "reasoning": "".join(reasoning_parts),
                    "tool_calls": tool_calls or list(tool_call_states.values()),
                    "model": model,
                    "response_id": response_id,
                    "usage": usage,
                    "error": response.get("error") or event.get("error") or {"message": event_type},
                }
            break
        else:
            reasoning_delta = extract_reasoning_delta(event)
            if reasoning_delta:
                reasoning_parts.append(reasoning_delta)

    return {
        "text": "".join(text_parts),
        "reasoning": "".join(reasoning_parts),
        "tool_calls": tool_calls or list(tool_call_states.values()),
        "model": model,
        "response_id": response_id,
        "usage": usage,
    }


def stream_openai_chunks(resp, model: str):
    chat_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    created = int(time.time())

    def chunk(delta, finish=None) -> bytes:
        payload = {
            "id": chat_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }
        return f"data: {json.dumps(payload)}\n\n".encode()

    def comment(text) -> bytes:
        return f": {text}\n\n".encode()

    yield comment("backend connected")
    yield chunk({"role": "assistant"})

    sent_final = False
    tool_calls_seen = False
    tool_call_states: dict[Any, dict] = {}

    for event in iter_sse_events(resp):
        event_type = event.get("type", "")
        if PROXY_DEBUG_EVENTS and event_type:
            print(f"[PROXY] backend event: {event_type}")

        if event_type == "response.output_text.delta":
            yield chunk({"content": event.get("delta", "")})

        elif event_type == "response.output_item.added":
            item = event.get("item", {})
            if isinstance(item, dict) and item.get("type") == "function_call":
                output_index = event.get("output_index", 0)
                tool_calls_seen = True
                tool_call_states[output_index] = {
                    "id": item.get("call_id") or item.get("id") or f"call_{uuid.uuid4().hex}",
                    "name": item.get("name", ""),
                    "arguments": item.get("arguments") or "",
                }
                yield chunk(
                    {
                        "tool_calls": [
                            {
                                "index": output_index,
                                "id": tool_call_states[output_index]["id"],
                                "type": "function",
                                "function": {
                                    "name": tool_call_states[output_index]["name"],
                                    "arguments": tool_call_states[output_index]["arguments"],
                                },
                            }
                        ]
                    }
                )
            elif event_type:
                yield comment(event_type)

        elif event_type == "response.function_call_arguments.delta":
            output_index = event.get("output_index", 0)
            delta_args = event.get("delta") or ""
            tool_calls_seen = True
            state = tool_call_states.setdefault(
                output_index, {"id": f"call_{uuid.uuid4().hex}", "name": "", "arguments": ""}
            )
            state["arguments"] += delta_args
            yield chunk(
                {"tool_calls": [{"index": output_index, "function": {"arguments": delta_args}}]}
            )

        elif event_type == "response.output_item.done":
            item = event.get("item", {})
            tool_call = response_function_call_to_chat(item)
            if tool_call:
                output_index = event.get("output_index", 0)
                tool_calls_seen = True
                state = tool_call_states.setdefault(
                    output_index,
                    {"id": tool_call["id"], "name": tool_call["function"]["name"], "arguments": ""},
                )
                final_args = tool_call["function"].get("arguments", "")
                current_args = state.get("arguments", "")
                if final_args.startswith(current_args):
                    missing_args = final_args[len(current_args) :]
                elif not current_args:
                    missing_args = final_args
                else:
                    missing_args = ""
                state.update(
                    {
                        "id": tool_call["id"],
                        "name": tool_call["function"]["name"],
                        "arguments": final_args,
                    }
                )
                if missing_args:
                    yield chunk(
                        {
                            "tool_calls": [
                                {"index": output_index, "function": {"arguments": missing_args}}
                            ]
                        }
                    )
            elif event_type:
                yield comment(event_type)

        elif event_type in TERMINAL_RESPONSE_EVENTS:
            if event_type == "response.completed":
                final_tool_calls = extract_response_tool_calls(event.get("response", {}))
                if final_tool_calls and not tool_calls_seen:
                    for output_index, tool_call in enumerate(final_tool_calls):
                        yield chunk({"tool_calls": [{"index": output_index, **tool_call}]})
                    tool_calls_seen = True
                finish_reason = "tool_calls" if tool_calls_seen else "stop"
            else:
                finish_reason = "error"
            print(f"[PROXY] backend terminal event: {event_type}")
            yield chunk({}, finish_reason)
            sent_final = True
            break

        else:
            reasoning_delta = extract_reasoning_delta(event)
            if reasoning_delta:
                yield chunk({"reasoning_content": reasoning_delta})
            elif event_type:
                yield comment(event_type)

    if not sent_final:
        yield chunk({}, "stop")
    yield b"data: [DONE]\n\n"


# ─── Pydantic models (populate the /docs schema) ─────────────────────


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="allow")
    role: str = Field(examples=["user"])
    content: Optional[Union[str, list]] = None
    name: Optional[str] = None
    tool_call_id: Optional[str] = None
    tool_calls: Optional[list] = None


class ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    model: Optional[str] = Field(default=None, examples=["gpt-5.4-mini"])
    messages: list[ChatMessage]
    stream: bool = False
    tools: Optional[list] = None
    tool_choice: Optional[Union[str, dict]] = None
    parallel_tool_calls: Optional[bool] = None
    reasoning: Optional[dict] = None
    reasoning_effort: Optional[str] = Field(default=None, examples=["medium"])
    reasoning_summary: Optional[str] = None
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    max_tokens: Optional[int] = None


# ─── Auth dependency ─────────────────────────────────────────────────

# Bearer scheme so Swagger UI (/docs) shows an "Authorize" button.
# Only enforced when PROXY_API_KEY is set; otherwise the proxy is open.
bearer_scheme = HTTPBearer(auto_error=False, description="Proxy API key (PROXY_API_KEY)")


def require_auth(credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme)):
    if not PROXY_API_KEY:
        return
    token = credentials.credentials if credentials else ""
    if token != PROXY_API_KEY:
        raise HTTPException(
            status_code=401,
            detail={"error": {"message": "Invalid proxy API key", "type": "auth_error"}},
        )


# ─── FastAPI app ─────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    token_manager.load_or_login()
    print(f"[PROXY] ready. Backend={CODEX_BACKEND} Default model={CODEX_MODEL}")
    print("[PROXY] docs: /docs")
    yield


app = FastAPI(
    title="Codex Proxy",
    description="OpenAI-compatible proxy backed by a ChatGPT login (Codex backend).",
    version="0.0.1",
    lifespan=lifespan,
    redoc_url=None,
)


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse(url="/docs")


@app.get("/v1", include_in_schema=False)
@app.get("/v1/", include_in_schema=False)
def v1_root():
    return {
        "service": "Codex Proxy",
        "docs": "/docs",
        "endpoints": ["/v1/chat/completions", "/v1/models", "/health", "/auth/status"],
    }


@app.exception_handler(404)
async def not_found(request: Request, exc):
    return JSONResponse(
        status_code=404,
        content={
            "error": {
                "message": f"Unknown endpoint: {request.url.path} — see /docs for the API.",
                "type": "invalid_request_error",
                "code": "not_found",
            }
        },
    )


@app.get("/health", tags=["meta"])
def health():
    return {"status": "ok"}


@app.get("/auth/status", tags=["meta"])
def auth_status(_: None = Depends(require_auth)):
    authed = bool(token_manager.access_token)
    auth_block = (
        _decode_jwt_payload(token_manager.access_token).get("https://api.openai.com/auth", {})
        if authed
        else {}
    )
    expires_in = (
        int(token_manager.token_expiry - time.time()) if token_manager.token_expiry else None
    )
    return {
        "authenticated": authed,
        "account_id": token_manager.account_id or None,
        "plan": auth_block.get("chatgpt_plan_type"),
        "expires_in_seconds": max(0, expires_in) if expires_in is not None else None,
    }


@app.get("/v1/models", tags=["meta"])
@app.get("/models", include_in_schema=False)
def list_models(_: None = Depends(require_auth)):
    data = [{"id": m, "object": "model", "owned_by": "openai"} for m in MODEL_IDS]
    return {"object": "list", "data": data}


@app.post("/v1/chat/completions", tags=["openai"])
@app.post("/chat/completions", include_in_schema=False)
def chat_completions(req: ChatCompletionRequest, _: None = Depends(require_auth)):
    chat_data = req.model_dump(exclude_none=True)
    model = chat_data.get("model") or CODEX_MODEL
    is_stream = bool(chat_data.get("stream", False))
    payload = build_responses_payload(chat_data)
    print(f"[PROXY] chat/completions -> backend (model={model}, stream={is_stream})")

    request = urllib.request.Request(
        CODEX_BACKEND,
        data=json.dumps(payload).encode(),
        headers=backend_headers(),
        method="POST",
    )
    try:
        resp = urllib.request.urlopen(request, timeout=BACKEND_TIMEOUT)
    except urllib.error.HTTPError as e:
        body = e.read()
        try:
            detail = json.loads(body)
        except Exception:
            detail = {"error": {"message": body.decode(errors="replace")}}
        return JSONResponse(status_code=e.code, content=detail)
    except urllib.error.URLError as e:
        return JSONResponse(
            status_code=502,
            content={"error": {"message": f"Backend connection failed: {e.reason}"}},
        )

    if is_stream:

        def gen():
            try:
                yield from stream_openai_chunks(resp, model)
            finally:
                resp.close()

        return StreamingResponse(
            gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"}
        )

    try:
        parsed = collect_sse_response(resp)
    finally:
        resp.close()
    if parsed.get("error"):
        return JSONResponse(status_code=502, content={"error": parsed["error"]})

    tool_calls = parsed.get("tool_calls") or []
    message = {"role": "assistant", "content": parsed["text"] if not tool_calls else None}
    if parsed.get("reasoning"):
        message["reasoning_content"] = parsed["reasoning"]
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {
        "id": f"chatcmpl-{parsed['response_id'] or uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": parsed["model"],
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": "tool_calls" if tool_calls else "stop",
            }
        ],
        "usage": parsed.get("usage", {}),
    }
