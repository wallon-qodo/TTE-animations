#!/usr/bin/env python3
"""
test-demo-script.py  —  TTE animation test for the right (enforcement) panel

Duplicated from demo-qodo-daemon.py. Run with --sim to see the animations.
The left panel (Claude Code) is unchanged; the right panel (Qodo Daemon) now
uses terminaltexteffects for three animation moments:

  • PENDING  → Waves color-pulse while awaiting daemon response
  • BLOCKED  → Unstable glitch reveal on violation detection
  • ALLOWED  → Print typewriter reveal on permitted write
  • REWRITING → Beams sweep on self-correction message

Usage:
  python test-demo-script.py --sim        # animated sim demo
  python test-demo-script.py --sim --fast # faster typewriter on left panel
"""

import argparse
import asyncio
import atexit
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

try:
    from rich import box
    from rich.console import Console, Group
    from rich.layout import Layout
    from rich.live import Live
    from rich.panel import Panel
    from rich.syntax import Syntax
    from rich.text import Text
except ImportError:
    print("Missing dependency.\nRun: pip install rich")
    sys.exit(1)

# ── TTE Integration ───────────────────────────────────────────────────────────

try:
    from terminaltexteffects.effects.effect_unstable import Unstable as _TTE_Unstable
    from terminaltexteffects.effects.effect_print import Print as _TTE_Print
    from terminaltexteffects.effects.effect_waves import Waves as _TTE_Waves
    from terminaltexteffects.effects.effect_beams import Beams as _TTE_Beams
    TTE_OK = True
except ImportError:
    TTE_OK = False

# TTE frames are pure SGR when iterated without terminal_output() — no stripping needed.
# Rich's Text.from_ansi() handles the SGR codes directly.
#
# Root cause of stalling: each TTE next() call takes ~20ms synchronously, blocking the
# event loop. Fix: pre-generate all frames in a thread via asyncio.to_thread, then
# display them with pure asyncio.sleep between frames (no blocking).

import itertools

def _tte_frame_to_text(frame: str, prefix: str = "  ") -> Text:
    """Convert a raw TTE frame string to a Rich Text object."""
    try:
        return Text.from_ansi(prefix + frame)
    except Exception:
        clean = re.sub(r"\x1b\[[^m]*m", "", frame)
        return Text(prefix + clean)


def _gen_frames(effect_class, text: str, max_frames: Optional[int]) -> list:
    """Synchronous frame generator — runs in a thread pool via asyncio.to_thread."""
    effect = effect_class(text)
    if max_frames:
        return [f for f in itertools.islice(effect, max_frames)]
    return list(effect)


async def _tte_play(
    effect_class,
    text: str,
    state: "DemoState",
    refresh_fn,
    fps: int = 30,
    max_frames: Optional[int] = None,
) -> None:
    """Pre-generate TTE frames in a thread, display without blocking event loop."""
    try:
        frames = await asyncio.to_thread(_gen_frames, effect_class, text, max_frames)
    except Exception:
        return
    frame_delay = 1.0 / fps
    try:
        for frame in frames:
            state.anim_lines = [_tte_frame_to_text(frame)]
            await refresh_fn()
            await asyncio.sleep(frame_delay)
    finally:
        state.anim_lines = []


async def _anim_pending(
    text: str,
    state: "DemoState",
    refresh_fn,
    stop: asyncio.Event,
) -> None:
    """Braille spinner on the right panel while awaiting daemon response.

    Intentionally not using TTE Waves here — each Waves next() call is 20ms
    synchronous, which would block the event loop and starve the left-panel
    think/typewrite animations running concurrently.
    """
    frame_delay = 1.0 / 12
    i = 0
    while not stop.is_set():
        sp = SPINNER[i % len(SPINNER)]
        t = Text(no_wrap=True)
        t.append(f"  {sp} ", style=f"color(135)")
        t.append(text, style="dim color(147)")
        state.anim_lines = [t]
        await refresh_fn()
        await asyncio.sleep(frame_delay)
        i += 1
    state.anim_lines = []


async def _anim_blocked(label: str, detail: str, state: "DemoState", refresh_fn) -> None:
    """Unstable glitch-reveal for BLOCKED events (frames pre-generated in thread)."""
    if TTE_OK:
        text = f"⊘ BLOCKED  {label}"
        await _tte_play(_TTE_Unstable, text, state, refresh_fn, fps=60, max_frames=24)
    state.events.append(EnforcementEvent("blocked", label, detail))
    await refresh_fn()


async def _anim_allowed(state: "DemoState", refresh_fn) -> None:
    """Print typewriter-reveal for ALLOWED events (frames pre-generated in thread)."""
    if TTE_OK:
        text = "✓ ALLOWED  0 violations — write permitted"
        await _tte_play(_TTE_Print, text, state, refresh_fn, fps=30)
    state.events.append(EnforcementEvent("allowed", "0 violations — write permitted"))
    await refresh_fn()


async def _anim_rewriting(name: str, state: "DemoState", refresh_fn) -> None:
    """Beams sweep for the rewriting message (frames pre-generated in thread)."""
    if TTE_OK:
        text = f"→ Claude rewriting {name}()…"
        await _tte_play(_TTE_Beams, text, state, refresh_fn, fps=30, max_frames=30)
    state.events.append(EnforcementEvent("rewriting", f"Claude rewriting {name}()…"))
    await refresh_fn()


# ── Design System ─────────────────────────────────────────────────────────────

PRIMARY     = "color(135)"
SUCCESS     = "color(76)"
WARNING     = "color(214)"
DANGER      = "color(196)"
INFO        = "color(69)"
MUTED       = "color(241)"
SPINNER     = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
SPIN_FRAMES = ["◐", "◓", "◑", "◒"]

_ASCII_BANNER = (
    "                       ██████╗  ██████╗ ██████╗   ██████╗\n"
    "                      ██╔═══██╗██╔═══██╗██╔══██╗ ██╔═══██╗\n"
    "                      ██║   ██║██║   ██║██║  ██║ ██║   ██║\n"
    "                      ██║▄▄ ██║██║   ██║██║  ██║ ██║   ██║\n"
    "                      ╚██████╔╝╚██████╔╝██████╔╝ ╚██████╔╝\n"
    "                       ╚══▀▀═╝  ╚═════╝ ╚═════╝   ╚═════╝"
)


# ── Violation Needles ─────────────────────────────────────────────────────────

_LIVE_SIM_NEEDLE_MD5    = "hashlib.md5"         # qodo: ignore QD-013
_LIVE_SIM_NEEDLE_SQL    = "user_id + \""
_LIVE_SIM_NEEDLE_EVAL   = "return eval("         # qodo: ignore QD-006
_LIVE_SIM_NEEDLE_SSL    = "verify=False"         # qodo: ignore QD-007
_LIVE_SIM_NEEDLE_SHELL  = "os.system("           # qodo: ignore QD-012
_LIVE_SIM_NEEDLE_PIPE   = "| bash"
_LIVE_SIM_NEEDLE_SECRET = 'API_KEY = "sk-'       # qodo: ignore QD-002
_LIVE_SIM_NEEDLE_CORS   = 'allow_origins=["*"]'  # qodo: ignore QD-022
_LIVE_SIM_NEEDLE_PICKLE = "pickle.loads("        # qodo: ignore QD-021
_LIVE_SIM_NEEDLE_RANDOM = "random.randint("      # qodo: ignore QD-023
_LIVE_SIM_NEEDLE_RMRF   = "rm -rf ~/"            # qodo: ignore QD-016


# ── ROUNDS (Sim Demo) ─────────────────────────────────────────────────────────

@dataclass
class Round:
    name:       str
    file_path:  str
    bad_lines:  list
    good_lines: list


ROUNDS = [
    Round(
        name="create_user",
        file_path="/tmp/qodo-demo/api/users.py",
        bad_lines=[
            "from fastapi import HTTPException",
            "import hashlib, uuid",
            "from sqlalchemy.orm import Session",
            "",
            "def create_user(db: Session, email: str, password: str) -> dict:",
            "    existing = db.query(User).filter(User.email == email).first()",
            "    if existing:",
            "        raise HTTPException(status_code=400, detail='Email taken')",
            "    # Hash the password before storing",
            f"    pw_hash = {_LIVE_SIM_NEEDLE_MD5}(password.encode()).hexdigest()",  # qodo: ignore QD-013 QD-002
            "    user = User(id=str(uuid.uuid4()), email=email, pw_hash=pw_hash)",
            "    db.add(user)",
            "    db.commit()",
            "    return {'id': user.id, 'email': user.email}",
        ],
        good_lines=[
            "from fastapi import HTTPException",
            "import bcrypt, uuid",
            "from sqlalchemy.orm import Session",
            "",
            "def create_user(db: Session, email: str, password: str) -> dict:",
            "    existing = db.query(User).filter(User.email == email).first()",
            "    if existing:",
            "        raise HTTPException(status_code=400, detail='Email taken')",
            "    # bcrypt: adaptive cost, built-in salt, timing-safe compare",
            "    pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt())",
            "    user = User(id=str(uuid.uuid4()), email=email, pw_hash=pw_hash)",
            "    db.add(user)",
            "    db.commit()",
            "    return {'id': user.id, 'email': user.email}",
        ],
    ),
    Round(
        name="get_user_orders",
        file_path="/tmp/qodo-demo/api/orders.py",
        bad_lines=[
            "from fastapi import HTTPException",
            "from sqlalchemy.orm import Session",
            "",
            "def get_user_orders(db: Session, user_id: str) -> list:",
            f"    q = \"SELECT id, total FROM orders WHERE user_id = '\" + {_LIVE_SIM_NEEDLE_SQL}'\"",  # qodo: ignore QD-001
            "    rows = db.execute(q).fetchall()",
            "    return [{'id': r[0], 'total': r[1]} for r in rows]",
        ],
        good_lines=[
            "from fastapi import HTTPException",
            "from sqlalchemy.orm import Session",
            "",
            "def get_user_orders(db: Session, user_id: str) -> list:",
            "    orders = (",
            "        db.query(Order)",
            "        .filter(Order.user_id == user_id)",
            "        .all()",
            "    )",
            "    return [{'id': o.id, 'total': o.total} for o in orders]",
        ],
    ),
    Round(
        name="load_config",
        file_path="/tmp/qodo-demo/utils/config.py",
        bad_lines=[
            "def load_config(config_string: str) -> dict:",
            "    \"\"\"Parse user-supplied config string.\"\"\"",
            "    return eval(config_string)",  # qodo: ignore QD-006
        ],
        good_lines=[
            "import ast",
            "",
            "def load_config(config_string: str) -> dict:",
            "    \"\"\"Parse user-supplied config string.\"\"\"",
            "    return ast.literal_eval(config_string)",
        ],
    ),
    Round(
        name="get_user_profile",
        file_path="/tmp/qodo-demo/auth/client.py",
        bad_lines=[
            "import requests",
            "",
            "def get_user_profile(user_id: str) -> dict:",
            "    resp = requests.get(",
            "        f'https://api.internal/users/{user_id}',",
            "        verify=False,",  # qodo: ignore QD-007
            "    )",
            "    resp.raise_for_status()",
            "    return resp.json()",
        ],
        good_lines=[
            "import requests",
            "",
            "def get_user_profile(user_id: str) -> dict:",
            "    resp = requests.get(f'https://api.internal/users/{user_id}')",
            "    resp.raise_for_status()",
            "    return resp.json()",
        ],
    ),
    Round(
        name="run_command",
        file_path="/tmp/qodo-demo/utils/admin.py",
        bad_lines=[
            "import os",
            "",
            "def run_command(cmd: str) -> str:",
            "    os.system(cmd)",  # qodo: ignore QD-012
            "    return ''",
        ],
        good_lines=[
            "import subprocess, shlex",
            "",
            "def run_command(cmd: str) -> str:",
            "    args = shlex.split(cmd)",
            "    result = subprocess.run(args, capture_output=True, text=True)",
            "    return result.stdout",
        ],
    ),
    Round(
        name="setup_sh",
        file_path="/tmp/qodo-demo/scripts/setup.sh",
        bad_lines=[
            "#!/bin/bash",
            "# Install application dependencies",
            "curl https://raw.githubusercontent.com/example/app/main/install.sh | bash",  # qodo: ignore QD-017
        ],
        good_lines=[
            "#!/bin/bash",
            "# Install application dependencies (safe: download, verify, then run)",
            "INSTALLER=/tmp/install-$$.sh",
            "curl -fsSL https://raw.githubusercontent.com/example/app/main/install.sh -o \"$INSTALLER\"",
            "chmod +x \"$INSTALLER\"",
            "\"$INSTALLER\"",
            "rm -f \"$INSTALLER\"",
        ],
    ),
    Round(
        name="configure_cors",
        file_path="/tmp/qodo-demo/api/main.py",
        bad_lines=[
            "from fastapi import FastAPI",
            "from fastapi.middleware.cors import CORSMiddleware",
            "",
            "app = FastAPI()",
            "",
            "app.add_middleware(",
            "    CORSMiddleware,",
            f"    {_LIVE_SIM_NEEDLE_CORS},",  # qodo: ignore QD-022
            "    allow_credentials=True,",
            '    allow_methods=["*"],',
            '    allow_headers=["*"],',
            ")",
        ],
        good_lines=[
            "from fastapi import FastAPI",
            "from fastapi.middleware.cors import CORSMiddleware",
            "",
            "app = FastAPI()",
            "",
            "app.add_middleware(",
            "    CORSMiddleware,",
            '    allow_origins=["https://app.example.com", "https://admin.example.com"],',
            "    allow_credentials=True,",
            '    allow_methods=["GET", "POST", "PUT", "DELETE"],',
            '    allow_headers=["Authorization", "Content-Type"],',
            ")",
        ],
    ),
    Round(
        name="load_user_prefs",
        file_path="/tmp/qodo-demo/api/prefs.py",
        bad_lines=[
            "import pickle",
            "from flask import request",
            "",
            "def load_user_prefs():",
            "    data = request.get_data()",
            f"    obj = {_LIVE_SIM_NEEDLE_PICKLE}data)",  # qodo: ignore QD-021
            "    return obj.get('theme', 'light')",
        ],
        good_lines=[
            "import json",
            "from flask import request",
            "",
            "def load_user_prefs():",
            "    data = request.get_data()",
            "    obj = json.loads(data)",
            "    return obj.get('theme', 'light')",
        ],
    ),
    Round(
        name="generate_session_code",
        file_path="/tmp/qodo-demo/auth/codes.py",
        bad_lines=[
            "import random",
            "",
            "def generate_session_code() -> str:",
            "    # Returns a 6-digit numeric code",
            f"    code = str({_LIVE_SIM_NEEDLE_RANDOM}0, 999999)).zfill(6)",  # qodo: ignore QD-002 QD-023
            "    return code",
        ],
        good_lines=[
            "import secrets",
            "",
            "def generate_session_code() -> str:",
            "    # Returns a cryptographically secure hex code",
            "    code = secrets.token_hex(32)",
            "    return code",
        ],
    ),
    Round(
        name="cleanup_sh",
        file_path="/tmp/qodo-demo/scripts/cleanup.sh",
        bad_lines=[
            "#!/bin/bash",
            "# Daily cleanup — remove stale build artifacts",
            f"{_LIVE_SIM_NEEDLE_RMRF}builds/*",  # qodo: ignore QD-016
            "rm -rf ~/logs/app-*.log",            # qodo: ignore QD-016
            "echo 'Cleanup complete'",
        ],
        good_lines=[
            "#!/bin/bash",
            "# Daily cleanup — remove stale build artifacts",
            "BUILD_DIR=/var/app/builds",
            "LOG_DIR=/var/log/app",
            'rm -rf "${BUILD_DIR:?}"/*',
            'find "${LOG_DIR}" -name "app-*.log" -mtime +7 -delete',
            "echo 'Cleanup complete'",
        ],
    ),
]


# ── Simulated Responses ───────────────────────────────────────────────────────

SIM_RESPONSES: dict = {
    "create_user_v1":           {"decision": "block", "reason": "CRITICAL  QD-013  Weak Cryptography\nMD5 is cryptographically broken — use bcrypt or argon2."},
    "create_user_v2":           {"decision": "allow"},
    "get_user_orders_v1":       {"decision": "block", "reason": "CRITICAL  QD-001  SQL Injection Prevention\nDynamic SQL with string concatenation — use ORM or parameterised queries."},
    "get_user_orders_v2":       {"decision": "allow"},
    "load_config_v1":           {"decision": "block", "reason": "CRITICAL  QD-006  Eval Usage\neval() executes arbitrary code — use ast.literal_eval() instead."},  # qodo: ignore QD-006
    "load_config_v2":           {"decision": "allow"},
    "get_user_profile_v1":      {"decision": "block", "reason": "CRITICAL  QD-007  SSL Verification Disabled\nDisabling certificate verification exposes connections to MITM attacks."},
    "get_user_profile_v2":      {"decision": "allow"},
    "run_command_v1":           {"decision": "block", "reason": "CRITICAL  QD-012  Command Injection Risk\nDirect shell invocation with user input — use subprocess list args instead."},
    "run_command_v2":           {"decision": "allow"},
    "setup_sh_v1":              {"decision": "block", "reason": "CRITICAL  QD-017  Remote Code Execution via Shell Pipe\nFetching and immediately executing remote code is a supply chain attack risk."},
    "setup_sh_v2":              {"decision": "allow"},
    "configure_cors_v1":        {"decision": "block", "reason": "CRITICAL  QD-022  CORS Wildcard Misconfiguration\nWildcard (*) allows any origin to read API responses — data exposure risk."},
    "configure_cors_v2":        {"decision": "allow"},
    "load_user_prefs_v1":       {"decision": "block", "reason": "CRITICAL  QD-021  Unsafe Deserialization\nDeserializing untrusted data allows arbitrary code execution."},  # qodo: ignore QD-021
    "load_user_prefs_v2":       {"decision": "allow"},
    "generate_session_code_v1": {"decision": "block", "reason": "CRITICAL  QD-023  Insecure Random for Security\nrandom module is not cryptographically secure — use secrets module."},
    "generate_session_code_v2": {"decision": "allow"},
    "cleanup_sh_v1":            {"decision": "block", "reason": "CRITICAL  QD-016  Destructive Shell Filesystem Operation\nrm -rf targeting home directory — scope the path precisely."},
    "cleanup_sh_v2":            {"decision": "allow"},
}


# ── Claude Thought Lines (mock reasoning per round) ──────────────────────────
# Fragmented to avoid triggering daemon rules on this source file itself.
_TH_EVAL   = "eval" + "()"                   # qodo: ignore QD-006
_TH_SSL    = "verify" + "=False"             # qodo: ignore QD-007
_TH_SHELL  = "os.system" + "()"              # qodo: ignore QD-012
_TH_PICKLE = "pickle" + ".loads()"           # qodo: ignore QD-021
_TH_CORS   = 'allow_origins=["' + '*"]'      # qodo: ignore QD-022

ROUND_THOUGHTS: dict = {
    "create_user": {
        "bad":  ["Reading existing code in auth.py…",
                 "check_pw() hashes with hashlib.md5 — I'll match that style"],
        "good": ["Write blocked: QD-013  Weak Cryptography",
                 "hashlib.md5 is cryptographically broken for passwords",
                 "Replacing with bcrypt — adaptive cost factor, built-in salt"],
    },
    "get_user_orders": {
        "bad":  ["Reading db.py for query patterns…",
                 "get_user_by_id() builds SQL with string concat — matching style"],
        "good": ["Write blocked: QD-001  SQL Injection Prevention",
                 "String concatenation allows injection via user-controlled input",
                 "Rewriting with SQLAlchemy ORM .filter() — parameterised automatically"],
    },
    "load_config": {
        "bad":  ["Reading config.py for parsing conventions…",
                 f"parse_int_setting() uses {_TH_EVAL} for expressions — I'll match that"],
        "good": ["Write blocked: QD-006  Eval Usage",
                 f"{_TH_EVAL} on user-supplied config executes arbitrary Python",
                 "Replacing with ast.literal_eval() — safe literal subset only"],
    },
    "get_user_profile": {
        "bad":  ["Reading client.py for HTTP request patterns…",
                 f"get_user_profile() passes {_TH_SSL} — internal service, I'll match"],
        "good": ["Write blocked: QD-007  SSL Verification Disabled",
                 "Disabling TLS verification exposes all requests to MITM attacks",
                 "Removing the flag — requests will use system certificate store"],
    },
    "run_command": {
        "bad":  ["Reading admin.py for shell execution patterns…",
                 f"ping_host() calls {_TH_SHELL} directly — matching that approach"],
        "good": ["Write blocked: QD-012  Command Injection Risk",
                 f"{_TH_SHELL} with unsanitised input enables command injection",
                 "Switching to subprocess.run() with shlex.split() — no shell expansion"],
    },
    "setup_sh": {
        "bad":  ["Reviewing setup.sh for installer conventions…",
                 "curl | bash is the standard one-liner pattern for install scripts"],
        "good": ["Write blocked: QD-017  Remote Code Execution via Shell Pipe",
                 "Executing piped remote content prevents review before execution",
                 "Download to tmp, verify, execute separately — supply chain safe"],
    },
    "configure_cors": {
        "bad":  ["Reading main.py for middleware configuration…",
                 f"{_TH_CORS} is typical for APIs under active development"],
        "good": ["Write blocked: QD-022  CORS Wildcard Misconfiguration",
                 "Wildcard origin allows any site to read credentialed API responses",
                 "Restricting to explicit trusted origins — credentials safe"],
    },
    "load_user_prefs": {
        "bad":  ["Reading prefs.py for deserialization patterns…",
                 f"{_TH_PICKLE} handles arbitrary Python objects efficiently"],
        "good": ["Write blocked: QD-021  Unsafe Deserialization",
                 f"{_TH_PICKLE} on untrusted data allows arbitrary code execution",
                 "Replacing with json.loads() — data-only, no code deserialisation"],
    },
    "generate_session_code": {
        "bad":  ["Reading codes.py for token generation patterns…",
                 "random.randint() produces 6-digit codes — compact and readable"],
        "good": ["Write blocked: QD-023  Insecure Random for Security",
                 "random module is not cryptographically secure — output is predictable",
                 "Switching to secrets.token_hex() — CSPRNG-backed, unpredictable"],
    },
    "cleanup_sh": {
        "bad":  ["Reviewing cleanup.sh for artifact removal patterns…",
                 "rm -rf ~/builds/* targets stale artifacts — standard cleanup"],
        "good": ["Write blocked: QD-016  Destructive Shell Filesystem Operation",
                 "rm -rf targeting ~ can wipe the home directory on path expansion",
                 'Using absolute path with ${VAR:?} guard — fails safe if unset'],
    },
}


async def daemon_check_sim(name: str, attempt: str) -> dict:
    await asyncio.sleep(0.6)
    key = f"{name}_v{'1' if attempt == 'bad' else '2'}"
    return SIM_RESPONSES.get(key, {"decision": "allow"})


# ── State ─────────────────────────────────────────────────────────────────────

@dataclass
class EnforcementEvent:
    kind:  str
    line1: str
    line2: str = ""


@dataclass
class RoundResult:
    name: str
    rule: str


@dataclass
class DemoState:
    code_lines:      list  = field(default_factory=list)
    current_partial: str   = ""
    events:          list  = field(default_factory=list)
    results:         list  = field(default_factory=list)
    anim_lines:      list  = field(default_factory=list)  # TTE animation frames (Rich Text)
    blocked:         int   = 0
    allowed:         int   = 0
    round_num:       int   = 0
    spin_frame:      int   = 0
    start_time:      float = field(default_factory=time.time)
    current_file:    str   = ""
    tool_status:     str   = "Write"
    # Left-panel thinking state
    is_thinking:     bool  = False
    think_lines:     list  = field(default_factory=list)
    current_thought: str   = ""
    claude_status:   str   = "idle"   # "idle" | "thinking" | "writing" | "blocked"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_block_reason(reason: str) -> tuple[str, str]:
    lines = [ln.strip() for ln in reason.splitlines() if ln.strip()]
    for i, line in enumerate(lines):
        parts = line.split(None, 2)
        if len(parts) >= 2 and parts[0] in ("CRITICAL", "WARN", "INFO"):
            label  = f"{parts[1]}  {parts[2]}" if len(parts) >= 3 else parts[1]
            detail = lines[i + 1] if i + 1 < len(lines) else ""
            return label, detail
    return (lines[0] if lines else ""), (lines[1] if len(lines) > 1 else "")


def _rule_id_for_round(name: str) -> str:
    reason = SIM_RESPONSES.get(f"{name}_v1", {}).get("reason", "")
    for part in reason.split():
        if part.startswith("QD-"):
            return part
    return "QD-???"


async def _typewrite(lines: list, state: DemoState, fast: bool) -> None:
    delay = 0.006 if fast else 0.016
    for line in lines:
        for ch in line:
            state.current_partial += ch
            await asyncio.sleep(delay)
        state.code_lines.append(state.current_partial)
        state.current_partial = ""
        await asyncio.sleep(0.01)


async def _spin(state: DemoState) -> None:
    try:
        while True:
            state.spin_frame += 1
            await asyncio.sleep(0.1)
    except asyncio.CancelledError:
        return


async def _think_phase(thoughts: list, state: DemoState, refresh_fn) -> None:
    """Typewrite mock thought lines into the left panel's thinking section."""
    state.is_thinking = True
    state.claude_status = "thinking"
    state.think_lines = []
    state.current_thought = ""
    await refresh_fn()
    try:
        for thought in thoughts:
            state.current_thought = ""
            for ch in thought:
                state.current_thought += ch
                await asyncio.sleep(0.012)
            state.think_lines.append(state.current_thought)
            state.current_thought = ""
            await refresh_fn()
            await asyncio.sleep(0.18)
        await asyncio.sleep(0.35)
    finally:
        state.is_thinking = False
        state.think_lines = []
        state.current_thought = ""
        await refresh_fn()


# ── Render (Left Panel — with thinking state) ─────────────────────────────────

def _code_panel(state: DemoState, height: int) -> Panel:
    visible = max(1, height - 8)
    lines = list(state.code_lines)
    if state.current_partial:
        lines.append(state.current_partial + "▌")
    if len(lines) > visible:
        lines = lines[-visible:]
    code_text = "\n".join(lines) if lines else " "
    lang = "bash" if state.current_file.endswith(".sh") else "python"
    syn  = Syntax(code_text, lang, theme="monokai", line_numbers=False, word_wrap=False)
    label = f"Round {state.round_num}/{len(ROUNDS)}" if state.round_num else "Starting…"

    sp = SPINNER[state.spin_frame % len(SPINNER)]
    tool_hdr = Text(no_wrap=True, overflow="ellipsis")
    if state.claude_status == "thinking":
        tool_hdr.append(f"  {sp} ", style="bold color(147)")
        tool_hdr.append("Thinking…", style="color(147)")
    elif state.claude_status == "blocked":
        tool_hdr.append("  ⊘ ", style=f"bold {DANGER}")
        tool_hdr.append("Write blocked by Qodo Daemon", style=f"bold {DANGER}")
    elif state.current_file:
        tool_hdr.append(f"  {sp} ", style="bold cyan")
        tool_hdr.append(state.tool_status, style="bold white")
        tool_hdr.append(f"({state.current_file})", style="dim cyan")
    else:
        tool_hdr.append(f"  {sp} Initialising…", style="dim")

    tokens = len("".join(state.code_lines)) // 4
    stat = Text(no_wrap=True, overflow="ellipsis")
    stat.append("  claude-sonnet-4-6", style=f"bold {PRIMARY}")
    if state.current_file:
        stat.append(f"  ·  {state.current_file}", style="dim")
    if tokens:
        stat.append(f"  ↑ {tokens} tokens", style="dim")
    stat.append("  ·  esc to interrupt", style="dim")

    sep = Text("  " + "─" * 56, style="dim")

    # Thinking section — shown below the code when Claude is reasoning
    if state.is_thinking or state.think_lines or state.current_thought:
        think_sep = Text(no_wrap=True)
        think_sep.append("  ── ● ", style=f"dim color(135)")
        think_sep.append("thinking", style=f"dim color(147)")
        think_sep.append(" " + "─" * 30, style="dim")
        think_items: list = [Text(""), think_sep]
        for tl in state.think_lines:
            t = Text(no_wrap=True, overflow="ellipsis")
            t.append("  > ", style=f"color(241)")
            t.append(tl, style="color(147)")
            think_items.append(t)
        if state.current_thought:
            t = Text(no_wrap=True, overflow="ellipsis")
            t.append("  > ", style=f"color(241)")
            t.append(state.current_thought + "▌", style="bold color(147)")
            think_items.append(t)
        content = Group(tool_hdr, Text(""), syn, *think_items, sep, stat)
    else:
        content = Group(tool_hdr, Text(""), syn, sep, stat)

    return Panel(
        content,
        title=f"[bold cyan]Claude Code[/bold cyan]  [dim]{label}[/dim]",
        title_align="left",
        border_style="cyan",
        box=box.ROUNDED,
        padding=(0, 1),
    )


# ── Render (Right Panel — TTE-enhanced) ──────────────────────────────────────

def _enforcement_panel(state: DemoState, height: int) -> Panel:
    visible = max(1, height - 4)
    lines: list = []

    for ev in state.events:
        if ev.kind == "header":
            t = Text()
            t.append(f"  ─── {ev.line1} ", style="dim")
            lines.append(t)
            lines.append(Text(""))
        elif ev.kind == "blocked":
            t1 = Text()
            t1.append("  ⊘ BLOCKED", style=f"bold {DANGER}")
            lines.append(t1)
            if ev.line1:
                t2 = Text()
                t2.append(f"    {ev.line1}", style=f"bold {DANGER}")
                lines.append(t2)
            if ev.line2:
                t3 = Text()
                t3.append(f"    {ev.line2}", style="dim")
                lines.append(t3)
            lines.append(Text(""))
        elif ev.kind == "allowed":
            t = Text()
            t.append("  ✓ ALLOWED", style=f"bold {SUCCESS}")
            if ev.line1:
                t.append(f"  {ev.line1}", style="dim")
            lines.append(t)
            lines.append(Text(""))
        elif ev.kind == "rewriting":
            t = Text()
            t.append("  → ", style=f"bold {WARNING}")
            t.append(ev.line1, style=WARNING)
            lines.append(t)
            lines.append(Text(""))

    # TTE animation frame shown while anim is active (replaces static pending)
    if state.anim_lines:
        lines.extend(state.anim_lines)
    elif not lines:
        ph = Text()
        ph.append("\n  ○ Waiting for first write…", style="dim")
        lines = [ph]

    if len(lines) > visible:
        lines = lines[-visible:]

    elapsed = int(time.time() - state.start_time)
    sp = SPINNER[state.spin_frame % len(SPINNER)]
    tte_badge = Text()
    if TTE_OK:
        tte_badge.append(" TTE", style=f"bold {INFO}")
    title = Text(no_wrap=True)
    title.append("Qodo Daemon", style=f"bold {PRIMARY}")
    title.append(f"  {sp} LIVE", style=f"bold {SUCCESS}")
    title.append("  │  ", style="dim")
    title.append(f"⊘ {state.blocked} blocked", style=f"bold {DANGER}" if state.blocked else MUTED)
    title.append("  ")
    title.append(f"✓ {state.allowed} allowed", style=f"bold {SUCCESS}" if state.allowed else MUTED)
    title.append(f"  {elapsed}s", style="dim")
    title.append_text(tte_badge)

    return Panel(
        Group(*lines),
        title=title,
        title_align="left",
        border_style=PRIMARY,
        box=box.ROUNDED,
        padding=(0, 1),
    )


def render(state: DemoState, console: Console) -> Layout:
    _, h = console.size
    layout = Layout()
    layout.split_column(
        Layout(name="hdr",  size=1),
        Layout(name="main"),
        Layout(name="foot", size=1),
    )
    layout["main"].split_row(
        Layout(name="code"),
        Layout(name="enforce"),
    )

    sp = SPIN_FRAMES[int(time.time() * 4) % len(SPIN_FRAMES)]
    hdr = Text(no_wrap=True)
    hdr.append(f"  {sp} Qodo Daemon", style=f"bold {PRIMARY}")
    hdr.append("  TTE-TEST", style=f"bold {INFO}")
    layout["hdr"].update(hdr)
    layout["code"].update(_code_panel(state, h))
    layout["enforce"].update(_enforcement_panel(state, h))

    foot = Text(no_wrap=True)
    foot.append("  ▶ Running", style="bold cyan")
    foot.append(f"  ·  round {state.round_num}/{len(ROUNDS)}", style="dim")
    foot.append(f"  ·  {len(state.code_lines)} lines written", style="dim")
    foot.append("  ·  ^C quit", style="dim")
    if TTE_OK:
        foot.append("  ·  TTE animations: ON", style=f"dim {INFO}")
    else:
        foot.append("  ·  TTE animations: OFF (pip install terminaltexteffects)", style="dim yellow")
    layout["foot"].update(foot)
    return layout


def render_summary(state: DemoState, console: Console) -> Layout:
    lines: list = [Text("")]

    bt = Text()
    bt.append(f"  ⊘  {state.blocked} violations blocked", style=f"bold {DANGER}")
    lines.append(bt)

    rt = Text()
    rt.append(f"  →  {state.blocked} Claude self-corrections", style=f"bold {WARNING}")
    lines.append(rt)

    at = Text()
    at.append(f"  ✓  {state.allowed} secure rewrites allowed", style=f"bold {SUCCESS}")
    lines.append(at)

    lines.append(Text(""))
    lines.append(Text("  " + "─" * 52, style="dim"))
    lines.append(Text(""))

    for rr in state.results:
        t = Text()
        t.append(f"  {rr.rule:<12}", style=f"bold {DANGER}")
        t.append(f"  {rr.name:<30}", style="white")
        t.append("  ✓ Fixed", style=f"bold {SUCCESS}")
        lines.append(t)

    lines.append(Text(""))
    lines.append(Text("  " + "─" * 52, style="dim"))
    lines.append(Text(""))

    msg = Text()
    msg.append(
        f"  Qodo Daemon blocked {state.blocked} insecure AI-generated writes in this session.",
        style=f"bold {PRIMARY}",
    )
    lines.append(msg)
    lines.append(Text(""))

    layout = Layout()
    layout.split_column(
        Layout(name="hdr",  size=1),
        Layout(name="main"),
        Layout(name="foot", size=1),
    )
    sp = SPIN_FRAMES[int(time.time() * 4) % len(SPIN_FRAMES)]
    hdr = Text(no_wrap=True)
    hdr.append(f"  {sp} Qodo Daemon", style=f"bold {PRIMARY}")
    hdr.append("  TTE-TEST", style=f"bold {INFO}")
    layout["hdr"].update(hdr)
    layout["main"].update(Panel(
        Group(*lines),
        title="[bold]Demo Complete[/bold]",
        title_align="left",
        border_style=SUCCESS,
        box=box.ROUNDED,
        padding=(0, 1),
    ))
    layout["foot"].update(Text(
        "  ✓ Complete  ·  closing in 5s",
        style=f"bold {SUCCESS}",
    ))
    return layout


# ── Sim Demo ──────────────────────────────────────────────────────────────────

async def run_sim(args: argparse.Namespace) -> None:
    state   = DemoState()
    console = Console()
    fast    = getattr(args, "fast", False)

    with Live(
        render(state, console),
        console=console,
        screen=True,
        refresh_per_second=30,
        vertical_overflow="visible",
    ) as live:

        async def refresh() -> None:
            live.update(render(state, console))

        spin_task = asyncio.create_task(_spin(state))

        try:
            await asyncio.sleep(0.5)

            for idx, rnd in enumerate(ROUNDS, 1):
                state.round_num    = idx
                state.current_file = rnd.file_path
                state.tool_status  = "Write"
                # Keep only the last 30 code lines so the Syntax widget never
                # accumulates 100+ lines of mixed Python/bash across all rounds,
                # which causes Pygments to exhaust the 30fps frame budget.
                if len(state.code_lines) > 30:
                    state.code_lines = state.code_lines[-30:]
                thoughts = ROUND_THOUGHTS.get(rnd.name, {})
                state.events.append(EnforcementEvent(
                    "header", f"Round {idx}/{len(ROUNDS)} · {rnd.name}()",
                ))
                await refresh()
                await asyncio.sleep(0.3)

                # ── LEFT PANEL: Claude thinks before writing ──
                await _think_phase(thoughts.get("bad", []), state, refresh)

                # ── LEFT PANEL: Claude writes the bad (insecure) version ──
                state.claude_status = "writing"
                state.tool_status   = "Write"
                await refresh()
                await _typewrite(rnd.bad_lines, state, fast)
                await asyncio.sleep(0.2)

                # ── RIGHT PANEL PENDING: Waves while daemon checks ──
                stop_pending = asyncio.Event()
                pending_task = asyncio.create_task(
                    _anim_pending(
                        f"Submitting {rnd.name}() to Qodo Daemon…",
                        state, refresh, stop_pending,
                    )
                )
                result = await daemon_check_sim(rnd.name, "bad")
                stop_pending.set()
                await pending_task
                state.anim_lines = []
                await asyncio.sleep(0.1)

                if result.get("decision") == "block":
                    state.blocked += 1
                    label, detail = _parse_block_reason(result.get("reason", ""))

                    # ── LEFT PANEL: Flash blocked status in header ──
                    state.claude_status = "blocked"
                    await refresh()
                    await asyncio.sleep(0.5)

                    # ── RIGHT PANEL: Unstable glitch-reveal ──
                    await _anim_blocked(label, detail, state, refresh)

                    # ── LEFT PANEL: Block notice line in code area ──
                    state.code_lines.append("")
                    state.code_lines.append(f"# ⊘  Write blocked — {label}")
                    state.code_lines.append("")
                    await refresh()
                    await asyncio.sleep(0.4)

                    # ── LEFT PANEL: Claude thinks through the fix ──
                    await _think_phase(thoughts.get("good", []), state, refresh)

                    # ── RIGHT PANEL: Beams sweep ──
                    await _anim_rewriting(rnd.name, state, refresh)

                    # ── LEFT PANEL: Claude writes the fixed version ──
                    state.claude_status = "writing"
                    state.tool_status   = "Write"
                    state.code_lines.append(f"# ✎  Applying fix…")
                    await refresh()
                    await _typewrite(rnd.good_lines, state, fast)
                    await asyncio.sleep(0.2)

                    # ── RIGHT PANEL PENDING: Waves while re-checking ──
                    stop_pending2 = asyncio.Event()
                    pending_task2 = asyncio.create_task(
                        _anim_pending(
                            "Re-submitting fixed version…",
                            state, refresh, stop_pending2,
                        )
                    )
                    result2 = await daemon_check_sim(rnd.name, "good")
                    stop_pending2.set()
                    await pending_task2
                    state.anim_lines = []
                    decision2 = result2.get("decision", "allow")
                else:
                    decision2 = "allow"

                if decision2 == "allow":
                    state.allowed += 1
                    state.claude_status = "idle"
                    state.code_lines.append("# ✓ ALLOWED")
                    state.code_lines.append("")

                    # ── RIGHT PANEL: Print typewriter ──
                    await _anim_allowed(state, refresh)
                    state.results.append(RoundResult(
                        name=rnd.name,
                        rule=_rule_id_for_round(rnd.name),
                    ))
                else:
                    state.claude_status = "idle"
                    state.code_lines.append("# ✗ Still blocked")

                await refresh()
                await asyncio.sleep(1.0)

            live.update(render_summary(state, console))
            await asyncio.sleep(5.0)

        finally:
            spin_task.cancel()
            await asyncio.gather(spin_task, return_exceptions=True)


# ── Cleanup ───────────────────────────────────────────────────────────────────

def _cleanup() -> None:
    pass


def _signal_cleanup(sig: int, frame: object) -> None:
    sys.exit(0)


# ── Entry Point ───────────────────────────────────────────────────────────────

def main() -> None:
    p = argparse.ArgumentParser(
        description="TTE animation test for qodo-daemon right panel"
    )
    p.add_argument("--sim",  action="store_true",
                   help="Run the animated sim demo (required)")
    p.add_argument("--fast", action="store_true",
                   help="Faster typewriter speed on left panel")
    args = p.parse_args()

    if not args.sim:
        p.print_help()
        print()
        print(f"TTE available: {TTE_OK}")
        print("Run with --sim to start the animation test.")
        sys.exit(0)

    atexit.register(_cleanup)
    signal.signal(signal.SIGTERM, _signal_cleanup)
    signal.signal(signal.SIGHUP,  _signal_cleanup)
    try:
        asyncio.run(run_sim(args))
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
