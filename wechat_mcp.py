#!/usr/bin/env python3
"""
MCP Server for controlling the WeChat (微信) desktop app on macOS.

Provides tools to:
  - wechat_status : check whether WeChat is running and whether it is logged in
  - open_wechat   : launch WeChat (or bring it to the front)
  - login_wechat  : open WeChat and auto-click the login / "进入微信" button in
                    the login window (final QR scan / phone confirmation, if any,
                    must be completed by the user)

Detection strategy (heuristic, version-tolerant):
  1. Running    -> System Events reports a process named "WeChat".
  2. Logged in  -> The login window is a small fixed-size window that contains a
                   login button ("进入微信" / "登录" / "Log In"). If WeChat is
                   running and NO such login window/button is present, we treat it
                   as logged in. A running process with no windows (minimized to
                   the menu bar) is also treated as logged in.

Requirements:
  - macOS with WeChat installed.
  - The process that launches this server (e.g. the Claude desktop app, Terminal,
    or python) must be granted **Accessibility** permission in
    System Settings -> Privacy & Security -> Accessibility, otherwise UI scripting
    (window/button inspection and clicking) will fail.

Transport: stdio (local tool).
"""

import asyncio
import json
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field
from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

mcp = FastMCP("wechat_mcp")

# Tencent WeChat bundle identifier (stable across CN "微信" / EN "WeChat" builds).
WECHAT_BUNDLE_ID = "com.tencent.xinWeChat"
# Process name reported by System Events / the kernel.
WECHAT_PROCESS = "WeChat"

# Keywords that identify a "log in / enter WeChat" button in the login window.
LOGIN_BUTTON_KEYWORDS = [
    "进入微信",
    "登录",
    "登 录",
    "登入",
    "log in",
    "login",
    "sign in",
    "enter wechat",
]

# A WeChat window is considered the (logged-out) login window when it is this
# small. The main chat window is always substantially larger.
LOGIN_WINDOW_MAX_WIDTH = 520
LOGIN_WINDOW_MAX_HEIGHT = 640

OSASCRIPT_TIMEOUT = 20.0
LAUNCH_SETTLE_SECONDS = 2.0


# ---------------------------------------------------------------------------
# Enums / response format
# ---------------------------------------------------------------------------


class ResponseFormat(str, Enum):
    """Output format for tool responses."""

    MARKDOWN = "markdown"
    JSON = "json"


# ---------------------------------------------------------------------------
# Input models
# ---------------------------------------------------------------------------


class StatusInput(BaseModel):
    """Input model for wechat_status."""

    model_config = ConfigDict(
        str_strip_whitespace=True, validate_assignment=True, extra="forbid"
    )

    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="Output format: 'markdown' for human-readable or 'json' for machine-readable.",
    )


class OpenInput(BaseModel):
    """Input model for open_wechat."""

    model_config = ConfigDict(
        str_strip_whitespace=True, validate_assignment=True, extra="forbid"
    )

    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="Output format: 'markdown' for human-readable or 'json' for machine-readable.",
    )


class LoginInput(BaseModel):
    """Input model for login_wechat."""

    model_config = ConfigDict(
        str_strip_whitespace=True, validate_assignment=True, extra="forbid"
    )

    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="Output format: 'markdown' for human-readable or 'json' for machine-readable.",
    )


# ---------------------------------------------------------------------------
# AppleScript snippets
# ---------------------------------------------------------------------------

# Inspect WeChat: returns a single line:
#   NOT_RUNNING
# or
#   RUNNING|<winCount>|<w1Width>|<w1Height>|<btnName1>~~<btnName2>~~...
_INSPECT_SCRIPT = r'''
on collectButtons(uiEl, depth)
    set names to {}
    if depth > 6 then return names
    tell application "System Events"
        try
            repeat with el in (UI elements of uiEl)
                try
                    if (class of el) is button then
                        set nm to ""
                        try
                            set nm to name of el
                        end try
                        if nm is missing value then set nm to ""
                        set end of names to nm
                    end if
                end try
                try
                    set names to names & my collectButtons(el, depth + 1)
                end try
            end repeat
        end try
    end tell
    return names
end collectButtons

on joinText(lst, delim)
    set out to ""
    repeat with i from 1 to count of lst
        if i = 1 then
            set out to (item i of lst) as text
        else
            set out to out & delim & ((item i of lst) as text)
        end if
    end repeat
    return out
end joinText

tell application "System Events"
    if not (exists process "WeChat") then return "NOT_RUNNING"
    tell process "WeChat"
        set winCount to count of windows
        set w1w to 0
        set w1h to 0
        set btnNames to {}
        if winCount > 0 then
            try
                set theSize to size of window 1
                set w1w to item 1 of theSize
                set w1h to item 2 of theSize
            end try
            repeat with w in windows
                try
                    set btnNames to btnNames & my collectButtons(w, 0)
                end try
            end repeat
        end if
    end tell
end tell
return "RUNNING|" & winCount & "|" & w1w & "|" & w1h & "|" & my joinText(btnNames, "~~")
'''

# Locate the login button and return its on-screen center so the caller can
# deliver a *real* mouse click there. WeChat's login buttons only expose the
# AXRaise action (no AXPress), so AppleScript's `click` is a no-op on them —
# only a synthesized hardware click (see _synth_click) actually logs in.
# Returns FOUND|<label>|<cx>|<cy>, NO_BUTTON, or NOT_RUNNING.
_CLICK_LOGIN_SCRIPT_TEMPLATE = r'''
on collectButtonRefs(uiEl, depth)
    set refs to {}
    if depth > 6 then return refs
    tell application "System Events"
        try
            repeat with el in (UI elements of uiEl)
                try
                    if (class of el) is button then set end of refs to (contents of el)
                end try
                try
                    set refs to refs & my collectButtonRefs(el, depth + 1)
                end try
            end repeat
        end try
    end tell
    return refs
end collectButtonRefs

set keywords to {%KEYWORDS%}

tell application "System Events"
    if not (exists process "WeChat") then return "NOT_RUNNING"
    tell process "WeChat"
        set frontmost to true
        set allButtons to {}
        repeat with w in windows
            try
                set allButtons to allButtons & my collectButtonRefs(w, 0)
            end try
        end repeat
        repeat with b in allButtons
            set bn to ""
            try
                set bn to name of b
            end try
            if bn is missing value then set bn to ""
            set lbn to my toLower(bn)
            repeat with kw in keywords
                if lbn contains kw then
                    try
                        set p to position of b
                        set s to size of b
                        set cx to (item 1 of p) + ((item 1 of s) / 2)
                        set cy to (item 2 of p) + ((item 2 of s) / 2)
                        return "FOUND|" & bn & "|" & cx & "|" & cy
                    end try
                end if
            end repeat
        end repeat
        return "NO_BUTTON"
    end tell
end tell

on toLower(t)
    set upperChars to "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    set lowerChars to "abcdefghijklmnopqrstuvwxyz"
    set out to ""
    repeat with c in (characters of t)
        set c to c as text
        set off to offset of c in upperChars
        if off > 0 then
            set out to out & (character off of lowerChars)
        else
            set out to out & c
        end if
    end repeat
    return out
end toLower
'''


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


async def _run(cmd: List[str], timeout: float = OSASCRIPT_TIMEOUT) -> tuple[int, str, str]:
    """Run a subprocess, returning (returncode, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return 124, "", "Timed out"
    return proc.returncode, out.decode("utf-8", "replace").strip(), err.decode(
        "utf-8", "replace"
    ).strip()


async def _osascript(script: str) -> tuple[int, str, str]:
    return await _run(["osascript", "-e", script])


def _synth_click_sync(x: float, y: float) -> Optional[str]:
    """Deliver a real (hardware-level) left click at screen point (x, y).

    WeChat's login buttons are custom-drawn and only expose AXRaise (no
    AXPress), so AppleScript's `click` does nothing. A synthesized CGEvent
    mouse click — which actually moves the cursor and presses — is the only
    reliable way to activate them.

    AX coordinates and CGEvent coordinates are both top-left-origin points, so
    the center reported by System Events can be used directly.

    Returns None on success, or an error string if Quartz is unavailable.
    """
    try:
        import time

        import Quartz  # type: ignore
    except Exception as exc:  # pragma: no cover - import-time/runtime guard
        return (
            "Quartz (pyobjc-framework-Quartz) is required to click WeChat's "
            f"login button but could not be imported: {exc}. "
            "Install it with: pip install pyobjc-framework-Quartz"
        )

    pt = (float(x), float(y))
    btn = Quartz.kCGMouseButtonLeft
    move = Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventMouseMoved, pt, btn)
    down = Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventLeftMouseDown, pt, btn)
    up = Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventLeftMouseUp, pt, btn)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, move)
    time.sleep(0.08)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, down)
    time.sleep(0.06)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, up)
    return None


async def _synth_click(x: float, y: float) -> Optional[str]:
    """Async wrapper around _synth_click_sync (runs in a thread)."""
    return await asyncio.to_thread(_synth_click_sync, x, y)


def _looks_like_login(button_names: List[str]) -> Optional[str]:
    """Return the matching login button label if any button name matches a login
    keyword, else None."""
    for raw in button_names:
        low = raw.lower()
        for kw in LOGIN_BUTTON_KEYWORDS:
            if kw in low:
                return raw
    return None


async def _inspect() -> dict:
    """Inspect WeChat and return a normalized status dict.

    Returns a dict with keys:
        running (bool)
        logged_in (Optional[bool])   # None when undeterminable
        window_count (int)
        login_button (Optional[str]) # label of detected login button, if any
        detail (str)                 # human-readable explanation
        accessibility_ok (bool)      # whether UI scripting appeared to work
        raw (str)                    # raw osascript output (for debugging)
    """
    code, out, err = await _osascript(_INSPECT_SCRIPT)

    # Accessibility / automation permission errors surface on stderr.
    if code != 0:
        err_low = err.lower()
        permission_hint = (
            "1002" in err
            or "-25211" in err  # errAEEventNotPermitted: assistive access not allowed
            or "-1719" in err  # errAEIllegalIndex / access errors
            or "not allowed" in err_low
            or "assistive" in err_low
            or "accessibility" in err_low
            or "辅助访问" in err  # localized "assistive access"
            or "辅助功能" in err  # localized "accessibility"
            or "不允许" in err  # localized "not allowed"
        )
        return {
            "running": None,
            "logged_in": None,
            "window_count": 0,
            "login_button": None,
            "accessibility_ok": not permission_hint,
            "detail": (
                "Could not inspect WeChat. This usually means the host app lacks "
                "Accessibility permission. Grant it in System Settings -> Privacy & "
                "Security -> Accessibility. "
                f"(osascript error: {err or 'unknown'})"
            ),
            "raw": err or out,
        }

    if out == "NOT_RUNNING":
        return {
            "running": False,
            "logged_in": False,
            "window_count": 0,
            "login_button": None,
            "accessibility_ok": True,
            "detail": "WeChat is not running.",
            "raw": out,
        }

    # Parse: RUNNING|<winCount>|<w>|<h>|<btns>
    parts = out.split("|", 4)
    win_count = 0
    width = 0
    height = 0
    button_blob = ""
    try:
        win_count = int(parts[1])
        width = int(float(parts[2]))
        height = int(float(parts[3]))
        button_blob = parts[4] if len(parts) > 4 else ""
    except (ValueError, IndexError):
        pass

    button_names = [b for b in button_blob.split("~~") if b.strip()]
    login_label = _looks_like_login(button_names)

    small_window = (
        win_count > 0
        and 0 < width <= LOGIN_WINDOW_MAX_WIDTH
        and 0 < height <= LOGIN_WINDOW_MAX_HEIGHT
    )

    if login_label is not None or small_window:
        logged_in = False
        if login_label:
            detail = f"WeChat is at the login screen (found login button: '{login_label}')."
        else:
            detail = (
                f"WeChat is showing a small login window ({width}x{height}); "
                "it appears to be logged out."
            )
    elif win_count == 0:
        # Running but no windows -> minimized to menu bar, almost always logged in.
        logged_in = True
        detail = "WeChat is running with no visible window (minimized); it appears to be logged in."
    else:
        logged_in = True
        detail = "WeChat is running with a main window; it appears to be logged in."

    return {
        "running": True,
        "logged_in": logged_in,
        "window_count": win_count,
        "login_button": login_label,
        "accessibility_ok": True,
        "detail": detail,
        "raw": out,
    }


def _format_status(status: dict, fmt: ResponseFormat) -> str:
    if fmt == ResponseFormat.JSON:
        return json.dumps(status, ensure_ascii=False, indent=2)

    running = status["running"]
    logged_in = status["logged_in"]

    def mark(v: Optional[bool]) -> str:
        if v is True:
            return "✅ yes"
        if v is False:
            return "❌ no"
        return "❓ unknown"

    lines = [
        "# WeChat Status",
        "",
        f"- **Running**: {mark(running)}",
        f"- **Logged in**: {mark(logged_in)}",
    ]
    if status.get("login_button"):
        lines.append(f"- **Login button**: '{status['login_button']}'")
    lines.append(f"- **Open windows**: {status.get('window_count', 0)}")
    lines.append("")
    lines.append(status.get("detail", ""))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool(
    name="wechat_status",
    annotations={
        "title": "Check WeChat Status",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def wechat_status(params: StatusInput) -> str:
    """Check whether the WeChat desktop app is running and whether it is logged in.

    Inspects the running WeChat process on macOS via System Events UI scripting.
    Detection of the logged-in state is heuristic: a small login window or the
    presence of a login button ("进入微信" / "登录" / "Log In") means logged out;
    a running process with a normal/main window (or no window at all) means
    logged in. This tool does NOT change anything.

    Args:
        params (StatusInput): Validated input containing:
            - response_format (ResponseFormat): 'markdown' (default) or 'json'.

    Returns:
        str: Status report. JSON schema (when response_format='json'):
        {
            "running": bool | null,        # null if undeterminable (permission error)
            "logged_in": bool | null,      # null if undeterminable
            "window_count": int,
            "login_button": str | null,    # label of detected login button
            "accessibility_ok": bool,      # false if Accessibility permission missing
            "detail": str,                 # human-readable explanation
            "raw": str                     # raw osascript output (debug)
        }

    Examples:
        - "Is WeChat open and logged in?" -> call with defaults.
        - Programmatic check -> response_format='json'.

    Error Handling:
        - If the host app lacks Accessibility permission, running/logged_in are
          null and 'detail' explains how to grant it.
    """
    status = await _inspect()
    return _format_status(status, params.response_format)


@mcp.tool(
    name="open_wechat",
    annotations={
        "title": "Open WeChat",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def open_wechat(params: OpenInput) -> str:
    """Launch the WeChat desktop app (or bring it to the front if already running).

    Uses macOS `open` with the WeChat bundle id, falling back to the app name.
    After launching, it waits briefly and reports the resulting status. This does
    not log in.

    Args:
        params (OpenInput): Validated input containing:
            - response_format (ResponseFormat): 'markdown' (default) or 'json'.

    Returns:
        str: A short result line followed by the current WeChat status (same
        schema as wechat_status). In JSON mode:
        {
            "action": "open_wechat",
            "launched": bool,        # whether the open command succeeded
            "message": str,
            "status": { ... }        # same object returned by wechat_status
        }

    Examples:
        - "Open WeChat" / "打开微信" -> call with defaults.

    Error Handling:
        - Returns launched=false with the OS error if WeChat could not be opened
          (e.g. not installed).
    """
    code, out, err = await _run(["open", "-b", WECHAT_BUNDLE_ID])
    if code != 0:
        # Fallback to opening by application name.
        code, out, err = await _run(["open", "-a", "WeChat"])

    launched = code == 0
    if not launched:
        message = (
            "Failed to open WeChat. Make sure it is installed. "
            f"(error: {err or out or 'unknown'})"
        )
        if params.response_format == ResponseFormat.JSON:
            return json.dumps(
                {
                    "action": "open_wechat",
                    "launched": False,
                    "message": message,
                    "status": await _inspect(),
                },
                ensure_ascii=False,
                indent=2,
            )
        return f"❌ {message}"

    await asyncio.sleep(LAUNCH_SETTLE_SECONDS)
    status = await _inspect()

    if params.response_format == ResponseFormat.JSON:
        return json.dumps(
            {
                "action": "open_wechat",
                "launched": True,
                "message": "WeChat launched / brought to front.",
                "status": status,
            },
            ensure_ascii=False,
            indent=2,
        )

    return "✅ WeChat launched / brought to front.\n\n" + _format_status(
        status, ResponseFormat.MARKDOWN
    )


@mcp.tool(
    name="login_wechat",
    annotations={
        "title": "Log In to WeChat",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def login_wechat(params: LoginInput) -> str:
    """Open WeChat and auto-click the login / "进入微信" button in the login window.

    Workflow:
      1. If WeChat is not running, launch it and wait for the login window.
      2. If already logged in, report that and do nothing.
      3. Otherwise, find a button matching login keywords ("进入微信" / "登录" /
         "Log In") in the login window and click it.

    Note: clicking the button starts the login flow. Depending on the WeChat
    configuration, the final step (scanning the QR code on a phone, or confirming
    the login on the phone) must still be completed by the user — that cannot be
    automated.

    Args:
        params (LoginInput): Validated input containing:
            - response_format (ResponseFormat): 'markdown' (default) or 'json'.

    Returns:
        str: Result of the login attempt plus current status. JSON mode:
        {
            "action": "login_wechat",
            "result": "already_logged_in" | "clicked" | "no_button" |
                      "not_running" | "permission_error",
            "clicked_button": str | null,
            "message": str,
            "status": { ... }        # same object returned by wechat_status
        }

    Examples:
        - "Log in to WeChat" / "登录微信" -> call with defaults.

    Error Handling:
        - result='permission_error' if Accessibility permission is missing.
        - result='no_button' if no login button could be found (e.g. only a QR
          code is shown); the user must scan the QR code manually.
    """
    # Ensure WeChat is running.
    status = await _inspect()
    if status["running"] is False:
        await _run(["open", "-b", WECHAT_BUNDLE_ID])
        await asyncio.sleep(LAUNCH_SETTLE_SECONDS + 1.0)
        status = await _inspect()

    # Permission problem.
    if status["running"] is None or not status.get("accessibility_ok", True):
        result = "permission_error"
        message = status.get("detail", "Accessibility permission required.")
        return _format_login_result(result, None, message, status, params.response_format)

    if status["logged_in"] is True:
        return _format_login_result(
            "already_logged_in",
            None,
            "WeChat is already logged in.",
            status,
            params.response_format,
        )

    # Build and run the click script.
    keywords_literal = ", ".join('"' + kw.replace('"', '') + '"' for kw in LOGIN_BUTTON_KEYWORDS)
    click_script = _CLICK_LOGIN_SCRIPT_TEMPLATE.replace("%KEYWORDS%", keywords_literal)
    code, out, err = await _osascript(click_script)

    if code != 0:
        return _format_login_result(
            "permission_error",
            None,
            f"Could not interact with WeChat's login window. (error: {err or 'unknown'})",
            status,
            params.response_format,
        )

    if out == "NOT_RUNNING":
        return _format_login_result(
            "not_running",
            None,
            "WeChat is not running.",
            await _inspect(),
            params.response_format,
        )

    if out.startswith("FOUND|"):
        parts = out.split("|")
        label = parts[1] if len(parts) > 1 else ""
        try:
            cx = float(parts[2])
            cy = float(parts[3])
        except (IndexError, ValueError):
            return _format_login_result(
                "permission_error",
                None,
                f"Found the login button ('{label}') but could not read its "
                "position to click it.",
                status,
                params.response_format,
            )

        click_err = await _synth_click(cx, cy)
        if click_err:
            return _format_login_result(
                "permission_error",
                label,
                click_err,
                status,
                params.response_format,
            )

        await asyncio.sleep(2.0)
        new_status = await _inspect()
        if new_status.get("logged_in") is True:
            msg = f"Clicked '{label}' — WeChat is now logged in. ✅"
        else:
            msg = (
                f"Clicked the login button ('{label}'). "
                "If a QR code or phone confirmation is shown, complete it on your phone."
            )
        return _format_login_result(
            "clicked", label, msg, new_status, params.response_format
        )

    # NO_BUTTON
    return _format_login_result(
        "no_button",
        None,
        "No login button was found — WeChat is probably showing a QR code. "
        "Please scan it with your phone to finish logging in.",
        status,
        params.response_format,
    )


def _format_login_result(
    result: str,
    clicked_button: Optional[str],
    message: str,
    status: dict,
    fmt: ResponseFormat,
) -> str:
    if fmt == ResponseFormat.JSON:
        return json.dumps(
            {
                "action": "login_wechat",
                "result": result,
                "clicked_button": clicked_button,
                "message": message,
                "status": status,
            },
            ensure_ascii=False,
            indent=2,
        )

    icon = {
        "already_logged_in": "✅",
        "clicked": "✅",
        "no_button": "ℹ️",
        "not_running": "❌",
        "permission_error": "⚠️",
    }.get(result, "•")
    return f"{icon} {message}\n\n" + _format_status(status, ResponseFormat.MARKDOWN)


if __name__ == "__main__":
    mcp.run()
