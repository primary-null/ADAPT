"""Stdio MCP server for ADAPT v1 using the official Python MCP SDK."""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from copy import deepcopy
from functools import partial
from typing import Any

from adapt_adb.mcp import tools as mcp_tools
from adapt_adb.mcp.access import (
    AccessPolicy,
    CONTENT_NOTICE,
    CONTENT_TRUST,
    DEVICE_TEXT_TOOLS,
    DEVICE_TOOLS,
    mark_untrusted_device_text,
)


_TOOL_SCHEMAS = {item["name"]: item["inputSchema"] for item in mcp_tools.TOOL_SCHEMAS}
# Per-event-loop limiter (an anyio RunVar), created on first tool call inside the loop.
_DEVICE_CALL_LIMITER: Any = None


def _matches_json_type(value: Any, expected: str) -> bool:
    if expected == "string":
        return isinstance(value, str)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "object":
        return isinstance(value, dict)
    return False


def _arguments_match_schema(name: str, arguments: Any) -> bool:
    """Validate the simple JSON-schema subset used by ADAPT tools."""
    schema = _TOOL_SCHEMAS.get(name)
    if schema is None:
        return True
    if not isinstance(arguments, dict):
        return False
    properties = schema.get("properties", {})
    if schema.get("additionalProperties") is False and any(k not in properties for k in arguments):
        return False
    if any(key not in arguments for key in schema.get("required", [])):
        return False
    for key, value in arguments.items():
        prop = properties.get(key)
        if prop is None:
            continue
        expected = prop.get("type")
        if expected and not _matches_json_type(value, expected):
            return False
        if "enum" in prop and value not in prop["enum"]:
            return False
        if isinstance(value, str):
            if len(value) < prop.get("minLength", 0):
                return False
            if len(value) > prop.get("maxLength", mcp_tools.DEFAULT_MAX_STRING_LENGTH):
                return False
        if "minimum" in prop and value < prop["minimum"]:
            return False
        if "maximum" in prop and value > prop["maximum"]:
            return False
    if name == "type" and any(ord(ch) < 32 or ord(ch) == 127 for ch in arguments.get("text", "")):
        return False
    return True


def _error_result(types: Any, error: str, message: str) -> Any:
    payload = {"error": error, "message": message, "devices": []}
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=json.dumps(payload))], isError=True
    )


def _configure_stderr_logging() -> logging.Logger:
    """Ensure all ADAPT and MCP logging goes exclusively to stderr (never stdout)."""
    logger = logging.getLogger("adapt_adb")
    logger.propagate = False
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("[%(levelname)s] %(name)s: %(message)s"))
        logger.addHandler(handler)
    logger.setLevel(logging.WARNING)

    root_mcp = logging.getLogger("mcp")
    root_mcp.propagate = False
    if not root_mcp.handlers:
        mcp_handler = logging.StreamHandler(sys.stderr)
        mcp_handler.setFormatter(logging.Formatter("[%(levelname)s] %(name)s: %(message)s"))
        root_mcp.addHandler(mcp_handler)
    root_mcp.setLevel(logging.WARNING)
    return logger


def _require_mcp_sdk() -> tuple[Any, Any, Any]:
    """Import and return (anyio, mcp.types, mcp.server) or raise ImportError if missing."""
    if sys.modules.get("mcp") is None and "mcp" in sys.modules:
        raise ImportError("MCP SDK is not installed.")
    import anyio
    import mcp.server as mcp_server_mod
    import mcp.types as types

    return anyio, types, mcp_server_mod


def get_mcp_tools(policy: AccessPolicy | None = None) -> list[Any]:
    """Advertise only tools available to this local session."""
    _, types, _ = _require_mcp_sdk()
    policy = policy or AccessPolicy()
    tools = []
    for schema in mcp_tools.TOOL_SCHEMAS:
        if not policy.exposes(schema["name"]):
            continue
        inputs = deepcopy(schema["inputSchema"])
        if policy.allowed_devices is not None and schema["name"] in DEVICE_TOOLS:
            inputs.setdefault("required", [])
            if "device" not in inputs["required"]:
                inputs["required"].append("device")
            inputs["properties"]["device"]["enum"] = list(policy.allowed_devices)
        tools.append(types.Tool(name=schema["name"], description=schema["description"], inputSchema=inputs))
    return tools


def dispatch_tool_call(name: str, arguments: dict[str, Any] | None = None, *, policy: AccessPolicy | None = None) -> Any:
    """Execute a named ADAPT MCP tool and return `mcp.types.CallToolResult`."""
    _, types, _ = _require_mcp_sdk()
    policy = policy or AccessPolicy()
    if name not in _TOOL_SCHEMAS:
        return _error_result(types, "unknown_tool", "Unknown ADAPT tool.")
    raw_args: Any = {} if arguments is None else arguments
    if not _arguments_match_schema(name, raw_args):
        return _error_result(
            types, "invalid_params", "Arguments do not match tool schema."
        )
    args = dict(raw_args)
    if not policy.permits(name, args):
        return _error_result(types, "access_denied", "This tool or target is not allowed in this MCP session.")

    try:
        if name == "doctor":
            payload = mcp_tools.tool_doctor()
        elif name in {"app_validate", "app_logs", "debug_app_inspect", "build_modes", "diagnostic_logs", "logging_guide"}:
            payload = mcp_tools.tool_development(name, **args)
        elif name == "dex_inspect":
            payload = mcp_tools.tool_dex_inspect(device=args["device"])
        elif name == "dex_displays":
            payload = mcp_tools.tool_dex_inspect(device=args["device"], displays_only=True)
        elif name == "android_versions":
            payload = mcp_tools.tool_android_versions()
        elif name == "android_capabilities":
            payload = mcp_tools.tool_android_capabilities(device=args["device"])
        elif name == "developer_options_inspect":
            payload = mcp_tools.tool_developer_options_inspect(device=args["device"])
        elif name == "tool_catalog":
            payload = mcp_tools.tool_catalog(goal=args.get("goal"))
        elif name == "setup_status":
            payload = mcp_tools.tool_setup_status()
        elif name == "list_devices":
            payload = mcp_tools.tool_list_devices()
        elif name == "device_info":
            payload = mcp_tools.tool_device_info(device=args.get("device"))
        elif name == "screenshot":
            b64_jpeg, meta_or_err = mcp_tools.tool_screenshot(
                device=args.get("device"),
                max_dimension=int(args.get("max_dimension", 1024)),
                quality=int(args.get("quality", 80)),
            )
            meta_or_err = policy.filter_result(meta_or_err)
            if b64_jpeg is None:
                return types.CallToolResult(
                    content=[types.TextContent(type="text", text=json.dumps(meta_or_err))],
                    isError=True,
                )
            meta_or_err = {**meta_or_err, "content_trust": CONTENT_TRUST, "content_notice": CONTENT_NOTICE}
            return types.CallToolResult(
                content=[
                    types.ImageContent(
                        type="image",
                        data=b64_jpeg,
                        mimeType="image/jpeg",
                    ),
                    types.TextContent(
                        type="text",
                        text=json.dumps(meta_or_err),
                    ),
                ],
                isError=False,
            )
        elif name == "ui_summary":
            payload = mcp_tools.tool_ui_summary(
                device=args.get("device"),
                allow_device_file=not policy.read_only,
            )
        elif name == "tap":
            if "x" not in args or "y" not in args:
                payload = {
                    "error": "invalid_params",
                    "message": "'x' and 'y' integer coordinates are required.",
                    "devices": [],
                }
            else:
                payload = mcp_tools.tool_tap(
                    x=int(args["x"]),
                    y=int(args["y"]),
                    device=args.get("device"),
                )
        elif name == "swipe":
            missing = [k for k in ("x1", "y1", "x2", "y2") if k not in args]
            if missing:
                payload = {
                    "error": "invalid_params",
                    "message": f"Missing required swipe coordinates: {', '.join(missing)}",
                    "devices": [],
                }
            else:
                payload = mcp_tools.tool_swipe(
                    x1=int(args["x1"]),
                    y1=int(args["y1"]),
                    x2=int(args["x2"]),
                    y2=int(args["y2"]),
                    duration_ms=int(args.get("duration_ms", 300)),
                    device=args.get("device"),
                )
        elif name == "key":
            if "key" not in args:
                payload = {
                    "error": "invalid_params",
                    "message": "'key' parameter is required.",
                    "devices": [],
                }
            else:
                payload = mcp_tools.tool_key(
                    key=str(args["key"]),
                    device=args.get("device"),
                )
        elif name == "type":
            if "text" not in args:
                payload = {
                    "error": "invalid_params",
                    "message": "'text' parameter is required.",
                    "devices": [],
                }
            else:
                payload = mcp_tools.tool_type(
                    text=str(args["text"]),
                    device=args.get("device"),
                )
        elif name == "install_apk":
            if "apk" not in args:
                payload = {
                    "error": "invalid_params",
                    "message": "'apk' parameter is required.",
                    "devices": [],
                }
            else:
                payload = mcp_tools.tool_install_apk(
                    apk=str(args["apk"]),
                    reinstall=bool(args.get("reinstall", True)),
                    grant=bool(args.get("grant", False)),
                    device=args.get("device"),
                )
        elif name == "logcat_tail":
            payload = mcp_tools.tool_logcat_tail(
                lines=int(args.get("lines", 100)),
                tag=args.get("tag"),
                level=args.get("level"),
                device=args.get("device"),
            )
        else:
            payload = {
                "error": "unknown_tool",
                "message": f"Unknown tool: {name}",
                "devices": [],
            }
    except subprocess.TimeoutExpired:
        payload = {
            "error": "adb_timeout",
            "message": "ADB operation timed out.",
            "devices": [],
        }
    except OSError:
        payload = {
            "error": "adb_launch_failed",
            "message": "Could not execute ADB.",
            "devices": [],
        }
    except Exception:
        payload = {
            "error": "internal_error",
            "message": "Tool execution failed.",
            "devices": [],
        }

    payload = policy.filter_result(payload)
    is_err = "error" in payload
    if not is_err and name in DEVICE_TEXT_TOOLS:
        payload = mark_untrusted_device_text(payload)
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=json.dumps(payload))],
        isError=is_err,
    )


def _device_call_limiter() -> Any:
    """Return this event loop's single-slot limiter that serializes device operations."""
    global _DEVICE_CALL_LIMITER
    import anyio
    from anyio.lowlevel import RunVar

    if _DEVICE_CALL_LIMITER is None:
        _DEVICE_CALL_LIMITER = RunVar("adapt_mcp_device_call_limiter")
    limiter = _DEVICE_CALL_LIMITER.get(None)
    if limiter is None:
        limiter = anyio.CapacityLimiter(1)
        _DEVICE_CALL_LIMITER.set(limiter)
    return limiter


async def _dispatch_in_worker(name: str, arguments: Any, policy: AccessPolicy) -> Any:
    """Run the synchronous dispatch in a worker thread so the event loop stays responsive.

    One call runs at a time. A cancelled call still waits for its adb process to finish,
    so the single slot is never released while a device operation is in flight.
    """
    from anyio import to_thread

    return await to_thread.run_sync(
        partial(dispatch_tool_call, name, arguments, policy=policy),
        limiter=_device_call_limiter(),
    )


class LegacyToolCallError(Exception):
    """Raised from the mcp 1.x decorator handler to make it return an error result."""


def _result_is_error(result: Any) -> bool:
    return bool(getattr(result, "is_error", False) or getattr(result, "isError", False))


def _result_text(result: Any) -> str:
    texts = [getattr(item, "text", "") for item in getattr(result, "content", []) or []]
    return "\n".join(text for text in texts if text)


def create_server(policy: AccessPolicy | None = None) -> Any:
    """Create and configure the official MCP `Server` instance for ADAPT."""
    _configure_stderr_logging()
    _, types, mcp_server_mod = _require_mcp_sdk()
    policy = policy or AccessPolicy()

    async def _on_list_tools(ctx: Any, params: Any = None) -> Any:
        return types.ListToolsResult(tools=get_mcp_tools(policy))

    async def _on_call_tool(ctx: Any, params: Any) -> Any:
        tool_name = getattr(params, "name", "")
        tool_args = getattr(params, "arguments", None) or {}
        return await _dispatch_in_worker(tool_name, tool_args, policy)

    try:
        server = mcp_server_mod.Server(
            "adapt",
            version="0.1.0",
            on_list_tools=_on_list_tools,
            on_call_tool=_on_call_tool,
        )
    except TypeError:
        # Fallback for older mcp 1.x Server decorator API
        server = mcp_server_mod.Server("adapt")
        if hasattr(server, "list_tools"):

            @server.list_tools()
            async def _legacy_list_tools() -> list[Any]:
                return get_mcp_tools(policy)

        if hasattr(server, "call_tool"):

            @server.call_tool()
            async def _legacy_call_tool(name: str, arguments: dict[str, Any] | None) -> list[Any]:
                res = await _dispatch_in_worker(name, arguments, policy)
                if _result_is_error(res):
                    # The 1.x decorator turns a handler exception into a result with
                    # isError=True whose text is str(exc); returning content would not.
                    raise LegacyToolCallError(_result_text(res))
                return list(res.content)

    return server


async def run_stdio_async(stdin: Any = None, stdout: Any = None, *, policy: AccessPolicy | None = None) -> None:
    """Run the ADAPT MCP server asynchronously over stdio until client disconnects."""
    _configure_stderr_logging()
    _require_mcp_sdk()
    from mcp.server.stdio import stdio_server

    server = create_server(policy)
    init_options = server.create_initialization_options()
    async with stdio_server(stdin=stdin, stdout=stdout) as (read_stream, write_stream):
        await server.run(read_stream, write_stream, init_options)


def run_stdio(*, policy: AccessPolicy | None = None) -> None:
    """Start the ADAPT stdio MCP server synchronously. Exits cleanly when stdin closes."""
    _configure_stderr_logging()
    anyio, _, _ = _require_mcp_sdk()
    anyio.run(partial(run_stdio_async, policy=policy))
