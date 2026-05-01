#!/usr/bin/env python3
"""
tte-statusbar-test.py — exact qodo status bar emulator with per-segment TTE animations.

Replicates the statusline.c rendering faithfully:
  qodo ⛨  │  watching ⬡  │  reviewing ◎  │  {n} blocked  │  {tok} tokens

Each of the 5 segments is animated individually with one TTE effect:
  ┌─────────────────┬──────────┬─────────────────────────────────┐
  │ Segment         │ Effect   │ Trigger                         │
  ├─────────────────┼──────────┼─────────────────────────────────┤
  │ qodo ⛨ brand   │ Slide    │ startup reveal                  │
  │ watching ⬡      │ SynthGrid│ transition to blocked ✖         │
  │ reviewing ◎     │ Smoke    │ review scan fires               │
  │ {n} blocked     │ Sweep    │ block count appears             │
  │ {tok} tokens    │ Wipe     │ token count updates             │
  └─────────────────┴──────────┴─────────────────────────────────┘

Activity shimmer states (matches C binary):
  HOT  < 500ms  — amber   (◐◓◑◒ spinner + gold shimmer)
  WARM < 4000ms — green   (⣾⣽ spinner + green shimmer)
  IDLE          — purple  (⛨ shield  + blue-cyan shimmer)

Usage:
  python3 tte-statusbar-test.py
  python3 tte-statusbar-test.py --segment brand    # single segment test
  python3 tte-statusbar-test.py --segment state
  python3 tte-statusbar-test.py --segment review
  python3 tte-statusbar-test.py --segment blocked
  python3 tte-statusbar-test.py --segment tokens
"""

import argparse
import asyncio
import itertools
import math
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Optional

try:
    from rich import box
    from rich.console import Console, Group
    from rich.layout import Layout
    from rich.live import Live
    from rich.panel import Panel
    from rich.text import Text
    from rich.rule import Rule
except ImportError:
    print("Missing dependency.\nRun: pip install rich")
    sys.exit(1)

try:
    from terminaltexteffects.effects.effect_slide     import Slide
    from terminaltexteffects.effects.effect_smoke     import Smoke
    from terminaltexteffects.effects.effect_sweep     import Sweep
    from terminaltexteffects.effects.effect_synthgrid import SynthGrid
    from terminaltexteffects.effects.effect_wipe      import Wipe
    TTE_OK = True
except ImportError:
    TTE_OK = False
    print("[warn] terminaltexteffects not found — animations disabled")


# ── Exact color palette from statusline.c ─────────────────────────────────────

# Purple brand: #BD93F9 (Dracula)
C_BRAND   = "color(135)"          # pfx — purple brand
C_BOLD    = "bold"
C_RST     = ""
C_BAR     = "│"

# Shimmer palettes — 8-frame cycles (256-color, matches statusline.c arrays)
SHIMMER_WATCH   = ["color(75)", "color(81)", "color(87)", "color(123)",
                   "color(87)", "color(81)", "color(75)", "color(69)"]
SHIMMER_ENFORCE = ["color(77)", "color(83)", "color(119)", "color(155)",
                   "color(119)", "color(83)", "color(77)", "color(71)"]
SHIMMER_BLOCK   = ["color(203)", "color(209)", "color(215)", "color(221)",
                   "color(215)", "color(209)", "color(203)", "color(197)"]
SHIMMER_REVIEW  = ["color(136)", "color(178)", "color(214)", "color(220)",
                   "color(214)", "color(178)", "color(136)", "color(130)"]
SHIMMER_HOT     = ["color(214)", "color(220)", "color(226)", "color(227)",
                   "color(226)", "color(220)", "color(214)", "color(208)"]

PULSE_IDLE  = ["color(57)", "color(93)", "color(129)", "color(183)",
               "color(129)", "color(93)", "color(57)", "color(54)"]
PULSE_WARM  = ["color(71)", "color(77)", "color(83)", "color(155)",
               "color(83)", "color(77)", "color(71)", "color(65)"]
PULSE_HOT   = ["color(208)", "color(214)", "color(220)", "color(226)",
               "color(220)", "color(214)", "color(208)", "color(202)"]

# Spinners — matches statusline.c arrays
SHIELD_CHAR = "⛨"
PROC_CHARS  = ["◐", "◓", "◑", "◒", "◐", "◓", "◑", "◒"]
BRAILLE     = ["⣾", "⣽", "⣻", "⢿", "⡿", "⣟", "⣯", "⣷"]

C_NACNT = "color(209)"  # block count accent (salmon)


# ── State ─────────────────────────────────────────────────────────────────────

@dataclass
class StatusState:
    label:       str   = "watching ⬡"    # watching ⬡ | blocked ✖ | enforcing ✔
    review:      str   = "reviewing ◎"   # reviewing ◎ | review off
    blocks:      int   = 0
    tokens:      int   = 0
    mode:        str   = ""              # observe | "" (enforce)
    activity_ms: int   = 9999           # ms since last tool call

    # Animation channel: which segment slot is playing TTE frames
    anim_segment: Optional[str] = None   # "brand"|"state"|"review"|"blocked"|"tokens"
    anim_frame:   Optional[str] = None   # raw TTE frame string (SGR encoded)
    anim_label:   str           = ""     # effect name for display

    # Current demo phase description
    phase_label: str = "Idle"
    spin_t:      float = field(default_factory=time.time)


def _frame_idx(state: StatusState) -> int:
    """8-frame index at ~10fps, matches statusline.c clock approach."""
    return int(time.time() * 10) % 8


def _activity(state: StatusState):
    """Returns ('hot'|'warm'|'idle', frame_index)."""
    fi = _frame_idx(state)
    if state.activity_ms < 500:
        return "hot", fi
    elif state.activity_ms < 4000:
        return "warm", fi
    return "idle", fi


# ── Status bar renderer ───────────────────────────────────────────────────────

def _seg_text(raw_frame: Optional[str], fallback: str) -> Text:
    """Return TTE-animated text if frame available, else static fallback."""
    if raw_frame is not None:
        try:
            return Text.from_ansi(raw_frame)
        except Exception:
            clean = re.sub(r"\x1b\[[^m]*m", "", raw_frame)
            return Text(clean)
    return Text(fallback)


def _status_bar(state: StatusState) -> Text:
    """Render the full status bar as a rich Text, exactly matching statusline.c output."""
    act, fi = _activity(state)

    # ── Spinner / shield ─────────────────────────────────────────────────────
    if act == "hot":
        sp_char  = PROC_CHARS[fi]
        pulse_c  = PULSE_HOT[fi]
    elif act == "warm":
        sp_char  = BRAILLE[fi]
        pulse_c  = PULSE_WARM[fi]
    else:
        sp_char  = SHIELD_CHAR
        pulse_c  = PULSE_IDLE[fi]

    # ── Label shimmer ─────────────────────────────────────────────────────────
    if "enforc" in state.label:
        shimmer = SHIMMER_ENFORCE[fi]
    elif "block" in state.label:
        shimmer = SHIMMER_BLOCK[fi]
    elif "watch" in state.label:
        shimmer = SHIMMER_HOT[fi] if act == "hot" else (
                  SHIMMER_ENFORCE[fi] if act == "warm" else SHIMMER_WATCH[fi])
    else:
        shimmer = C_BRAND

    # ── Review shimmer ────────────────────────────────────────────────────────
    if "off" in state.review:
        rev_c = "color(241)"
    elif act == "hot":
        rev_c = SHIMMER_HOT[fi]
    elif act == "warm":
        rev_c = SHIMMER_REVIEW[fi]
    else:
        rev_c = "color(241)"

    bar = Text(no_wrap=True, overflow="ellipsis")

    # ── Segment 1: brand ─────────────────────────────────────────────────────
    if state.anim_segment == "brand" and state.anim_frame is not None:
        bar.append_text(_seg_text(state.anim_frame, f"qodo {sp_char}"))
    else:
        bar.append("qodo ", style=f"bold {C_BRAND}")
        bar.append(sp_char, style=f"bold {pulse_c}")

    bar.append(f" {C_BAR} ", style=C_BRAND)

    # ── Segment 2: state label ────────────────────────────────────────────────
    if state.anim_segment == "state" and state.anim_frame is not None:
        bar.append_text(_seg_text(state.anim_frame, state.label))
    else:
        bar.append(state.label, style=shimmer)

    bar.append(f" {C_BAR} ", style=C_BRAND)

    # ── Segment 3: review ─────────────────────────────────────────────────────
    if state.anim_segment == "review" and state.anim_frame is not None:
        bar.append_text(_seg_text(state.anim_frame, state.review))
    else:
        bar.append(state.review, style=rev_c)

    # ── Segment 4: block count (only when > 0) ────────────────────────────────
    if state.blocks > 0 or (state.anim_segment == "blocked" and state.anim_frame is not None):
        bar.append(f" {C_BAR} ", style=C_BRAND)
        if state.anim_segment == "blocked" and state.anim_frame is not None:
            bar.append_text(_seg_text(state.anim_frame, f"{state.blocks} blocked"))
        else:
            bar.append(f"{state.blocks} blocked", style=C_NACNT)

    # ── Segment 5: tokens ─────────────────────────────────────────────────────
    bar.append(f" {C_BAR} ", style=C_BRAND)
    tok_str = (
        f"{state.tokens / 1000:.1f}k" if state.tokens >= 1000
        else str(state.tokens)
    )
    if state.anim_segment == "tokens" and state.anim_frame is not None:
        bar.append_text(_seg_text(state.anim_frame, f"{tok_str} tokens"))
    else:
        bar.append(f"{tok_str} tokens", style="dim")

    return bar


def _anim_zone(state: StatusState) -> Text:
    """Large animation preview area — shows TTE frames for the active segment."""
    if state.anim_segment is None or state.anim_frame is None:
        t = Text("\n  ○  idle", style="dim")
        return t
    try:
        t = Text.from_ansi("  " + state.anim_frame)
    except Exception:
        clean = re.sub(r"\x1b\[[^m]*m", "", state.anim_frame)
        t = Text("  " + clean)
    return t


SEGMENT_EFFECTS = {
    "brand":   ("Slide",     "qodo ⛨",                  "startup brand reveal"),
    "state":   ("SynthGrid", "watching ⬡  →  blocked ✖", "violation detected"),
    "review":  ("Smoke",     "reviewing ◎",              "code review scan fires"),
    "blocked": ("Sweep",     "3 blocked",                "block count appears"),
    "tokens":  ("Wipe",      "206 tokens",               "token count updates"),
}

SEG_ORDER = ["brand", "state", "review", "blocked", "tokens"]

SEGMENT_LABELS = {
    "brand":   "qodo ⛨",
    "state":   "state label",
    "review":  "review label",
    "blocked": "block count",
    "tokens":  "token count",
}


def _legend(active_seg: Optional[str]) -> Text:
    t = Text(no_wrap=True)
    for seg in SEG_ORDER:
        effect, _, _ = SEGMENT_EFFECTS[seg]
        label = SEGMENT_LABELS[seg]
        if seg == active_seg:
            t.append(f"  ▶ {label}", style="bold color(147)")
            t.append(f" [{effect}]", style="bold color(69)")
        else:
            t.append(f"  · {label}", style="dim")
            t.append(f" [{effect}]", style="dim color(69)")
    return t


def _render(state: StatusState, console: Console) -> Panel:
    bar = _status_bar(state)

    sep_top = Text("  " + "─" * 60, style="dim color(237)")

    # Phase label
    phase = Text(no_wrap=True)
    phase.append("  phase  ", style="dim")
    phase.append(state.phase_label, style="bold color(147)")
    if state.anim_label:
        phase.append(f"  →  {state.anim_label}", style="bold color(69)")

    sep_mid = Text("  " + "─" * 60, style="dim color(237)")

    # Animation zone
    anim = _anim_zone(state)

    sep_bot = Text("  " + "─" * 60, style="dim color(237)")

    # Legend
    legend = _legend(state.anim_segment)

    content = Group(
        Text(""),
        bar,
        sep_top,
        phase,
        sep_mid,
        anim,
        Text(""),
        sep_bot,
        legend,
        Text(""),
    )

    return Panel(
        content,
        title="[bold color(135)]qodo daemon  —  status bar animation test[/bold color(135)]",
        title_align="left",
        border_style="color(57)",
        box=box.ROUNDED,
        padding=(0, 1),
    )


# ── TTE helpers ───────────────────────────────────────────────────────────────

def _gen_frames(cls, text: str, max_frames: Optional[int]) -> list:
    eff = cls(text)
    if max_frames:
        return list(itertools.islice(eff, max_frames))
    return list(eff)


async def _play(
    cls,
    text: str,
    segment: str,
    effect_name: str,
    state: StatusState,
    refresh,
    fps: int = 30,
    max_frames: Optional[int] = None,
) -> None:
    state.anim_segment = segment
    state.anim_label   = effect_name
    try:
        frames = await asyncio.to_thread(_gen_frames, cls, text, max_frames)
    except Exception as exc:
        state.anim_frame = None
        state.anim_segment = None
        state.anim_label = ""
        refresh()
        return
    delay = 1.0 / fps
    try:
        for frame in frames:
            state.anim_frame = frame
            refresh()
            await asyncio.sleep(delay)
    finally:
        state.anim_frame   = None
        state.anim_segment = None
        state.anim_label   = ""
        refresh()


# ── Demo sequence ─────────────────────────────────────────────────────────────

async def run_segment(seg: str, state: StatusState, refresh) -> None:
    effect_name, text, _ = SEGMENT_EFFECTS[seg]
    cls_map = {
        "Slide": Slide, "Smoke": Smoke, "Sweep": Sweep,
        "SynthGrid": SynthGrid, "Wipe": Wipe,
    }
    cls = cls_map[effect_name]

    fps_map = {"Slide": 30, "Smoke": 30, "Sweep": 30, "SynthGrid": 30, "Wipe": 30}
    max_map = {"Slide": None, "Smoke": 90, "Sweep": None, "SynthGrid": 110, "Wipe": None}

    await _play(cls, text, seg, effect_name, state, refresh,
                fps=fps_map[effect_name], max_frames=max_map[effect_name])


async def run_all(only_seg: Optional[str]) -> None:
    state   = StatusState()
    console = Console()

    with Live(_render(state, console), console=console,
              refresh_per_second=30, screen=False) as live:
        refresh = lambda: live.update(_render(state, console))

        segs = [only_seg] if only_seg else SEG_ORDER
        await asyncio.sleep(0.6)

        for seg in segs:
            _, _, trigger_desc = SEGMENT_EFFECTS[seg]
            state.phase_label = trigger_desc

            # Set up state transitions before animating
            if seg == "state":
                state.activity_ms = 100    # HOT — make it amber
            elif seg == "blocked":
                state.blocks      = 3
                state.activity_ms = 300
            elif seg == "tokens":
                state.tokens      = 206
                state.activity_ms = 600    # WARM
            elif seg == "review":
                state.activity_ms = 800

            refresh()
            await asyncio.sleep(0.5)

            await run_segment(seg, state, refresh)

            # Post-animation state update
            if seg == "state":
                state.label       = "blocked ✖"
                state.activity_ms = 2000
            elif seg == "review":
                state.activity_ms = 9999   # back to IDLE

            refresh()
            await asyncio.sleep(0.8)

        # Hold final state
        state.phase_label = "complete"
        refresh()
        await asyncio.sleep(1.5)


def main() -> None:
    parser = argparse.ArgumentParser(description="qodo status bar TTE animation test")
    parser.add_argument(
        "--segment", choices=SEG_ORDER,
        help="test a single segment animation (brand/state/review/blocked/tokens)"
    )
    args = parser.parse_args()

    if not TTE_OK:
        print("Install terminaltexteffects first:  pip install terminaltexteffects")
        sys.exit(1)

    asyncio.run(run_all(args.segment))


if __name__ == "__main__":
    main()
