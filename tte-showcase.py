#!/usr/bin/env python3
"""
tte-showcase.py — Cycles through all 37 TTE effects, naming each before playing it.

Usage:
  python3 tte-showcase.py                    # run all effects (~4s each)
  python3 tte-showcase.py --duration 6       # let each effect run longer
  python3 tte-showcase.py --fps 60           # override fps for all effects
  python3 tte-showcase.py --filter wipe      # run only effects whose name matches
"""

import argparse
import asyncio
import itertools
import re
import sys
import time
from typing import Optional

try:
    from rich.console import Console
    from rich.live import Live
    from rich.text import Text
    from rich.panel import Panel
    from rich.align import Align
except ImportError:
    print("Missing dependency.\nRun: pip install rich")
    sys.exit(1)

# ── Effect registry ───────────────────────────────────────────────────────────
# (name, module_suffix, class_name, default_fps)
# No max_frames here — duration controls the cap at runtime so every effect
# plays long enough to look complete instead of cutting off early.

EFFECTS = [
    ("Beams",           "beams",            "Beams",            30),
    ("BinaryPath",      "binarypath",       "BinaryPath",       30),
    ("Blackhole",       "blackhole",        "Blackhole",        30),
    ("BouncyBalls",     "bouncyballs",      "BouncyBalls",      30),
    ("Bubbles",         "bubbles",          "Bubbles",          30),
    ("Burn",            "burn",             "Burn",             30),
    ("ColorShift",      "colorshift",       "ColorShift",       30),
    ("Crumble",         "crumble",          "Crumble",          30),
    ("Decrypt",         "decrypt",          "Decrypt",          60),
    ("ErrorCorrect",    "errorcorrect",     "ErrorCorrect",     30),
    ("Expand",          "expand",           "Expand",           30),
    ("Fireworks",       "fireworks",        "Fireworks",        30),
    ("Highlight",       "highlight",        "Highlight",        30),
    ("LaserEtch",       "laseretch",        "LaserEtch",        30),
    ("Matrix",          "matrix",           "Matrix",           30),
    ("MiddleOut",       "middleout",        "MiddleOut",        30),
    ("OrbittingVolley", "orbittingvolley",  "OrbittingVolley",  30),
    ("Overflow",        "overflow",         "Overflow",         30),
    ("Pour",            "pour",             "Pour",             30),
    ("Print",           "print",            "Print",            30),
    ("Rain",            "rain",             "Rain",             30),
    ("RandomSequence",  "random_sequence",  "RandomSequence",   30),
    ("Rings",           "rings",            "Rings",            30),
    ("Scattered",       "scattered",        "Scattered",        30),
    ("Slice",           "slice",            "Slice",            30),
    ("Slide",           "slide",            "Slide",            30),
    ("Smoke",           "smoke",            "Smoke",            30),
    ("Spotlights",      "spotlights",       "Spotlights",       30),
    ("Spray",           "spray",            "Spray",            30),
    ("Swarm",           "swarm",            "Swarm",            30),
    ("Sweep",           "sweep",            "Sweep",            30),
    ("SynthGrid",       "synthgrid",        "SynthGrid",        30),
    ("Thunderstorm",    "thunderstorm",     "Thunderstorm",     30),
    ("Unstable",        "unstable",         "Unstable",         60),
    ("VHSTape",         "vhstape",          "VHSTape",          30),
    ("Waves",           "waves",            "Waves",            30),
    ("Wipe",            "wipe",             "Wipe",             30),
]

DEMO_TEXT = "  ◈  Qodo Daemon  —  terminaltexteffects"

# ── TTE helpers ───────────────────────────────────────────────────────────────

def _import_effect(module_suffix: str, class_name: str):
    import importlib
    mod = importlib.import_module(f"terminaltexteffects.effects.effect_{module_suffix}")
    return getattr(mod, class_name)


def _gen_frames(effect_class, text: str, max_frames: Optional[int]) -> list:
    effect = effect_class(text)
    if max_frames:
        return list(itertools.islice(effect, max_frames))
    return list(effect)


def _frame_to_text(frame: str) -> Text:
    try:
        return Text.from_ansi(frame)
    except Exception:
        clean = re.sub(r"\x1b\[[^m]*m", "", frame)
        return Text(clean)


# ── Display state ─────────────────────────────────────────────────────────────

class ShowcaseState:
    def __init__(self):
        self.effect_name: str = ""
        self.effect_index: int = 0
        self.effect_total: int = len(EFFECTS)
        self.frame_line: Optional[Text] = None
        self.phase: str = "name"   # "name" | "anim" | "done"


def _build_display(state: ShowcaseState) -> Panel:
    progress = f"{state.effect_index}/{state.effect_total}"

    header = Text(no_wrap=True)
    header.append("  TTE Showcase  ", style="bold white on color(235)")
    header.append(f"  {progress}  ", style="dim color(245)")

    name_line = Text(justify="center")
    if state.phase == "name":
        name_line.append(f"\n  {state.effect_name}  \n", style="bold color(147) on color(236)")
    elif state.phase == "anim":
        name_line.append(f"\n  {state.effect_name}  \n", style="bold color(135)")
    else:
        name_line.append("\n  Done  \n", style="bold green")

    anim_line = Text(justify="left", no_wrap=True)
    if state.frame_line is not None:
        anim_line = state.frame_line

    from rich.console import Group
    body = Group(name_line, Text(""), anim_line, Text(""))

    return Panel(
        body,
        title=header,
        border_style="color(239)",
        padding=(0, 1),
    )


# ── Main loop ─────────────────────────────────────────────────────────────────

async def showcase(
    filter_str: Optional[str],
    fps_override: Optional[int],
    duration_secs: float,
    pause_secs: float,
) -> None:
    effects_to_run = [
        e for e in EFFECTS
        if filter_str is None or filter_str.lower() in e[0].lower()
    ]

    if not effects_to_run:
        print(f"No effects matched '{filter_str}'.")
        sys.exit(1)

    # Pre-import all effect classes upfront to surface ImportErrors early
    loaded = []
    failed = []
    for name, mod_suffix, cls_name, fps in effects_to_run:
        try:
            cls = _import_effect(mod_suffix, cls_name)
            effective_fps = fps_override or fps
            # Cap at duration_secs worth of frames; effects with fewer frames
            # than the cap will play fully and stop naturally.
            max_f = int(effective_fps * duration_secs)
            loaded.append((name, cls, effective_fps, max_f))
        except Exception as exc:
            failed.append((name, str(exc)))

    if failed:
        print("Could not import the following effects:")
        for n, err in failed:
            print(f"  {n}: {err}")
        if not loaded:
            sys.exit(1)

    state = ShowcaseState()
    state.effect_total = len(loaded)

    console = Console()

    with Live(_build_display(state), console=console, refresh_per_second=60, screen=False) as live:
        refresh = lambda: live.update(_build_display(state))

        for idx, (name, cls, fps, max_f) in enumerate(loaded, start=1):
            state.effect_index = idx
            state.effect_name = name
            state.frame_line = None
            state.phase = "name"
            refresh()

            # Hold the name label for a moment before animating
            await asyncio.sleep(pause_secs)

            # Pre-generate frames off the event loop
            state.phase = "anim"
            refresh()
            try:
                frames = await asyncio.to_thread(_gen_frames, cls, DEMO_TEXT, max_f)
            except Exception as exc:
                state.frame_line = Text(f"  [error: {exc}]", style="red")
                refresh()
                await asyncio.sleep(1.5)
                continue

            frame_delay = 1.0 / fps
            for frame in frames:
                state.frame_line = _frame_to_text(frame)
                refresh()
                await asyncio.sleep(frame_delay)

            # Brief hold at final frame before moving on
            await asyncio.sleep(0.6)
            state.frame_line = None

        state.phase = "done"
        state.effect_name = ""
        refresh()
        await asyncio.sleep(1.2)


def main():
    parser = argparse.ArgumentParser(description="TTE effect showcase")
    parser.add_argument("--filter",   metavar="NAME", help="only run effects whose name contains this string (case-insensitive)")
    parser.add_argument("--fps",      type=int,       help="override fps for all effects")
    parser.add_argument("--duration", type=float, default=4.0, help="max seconds per animation (default: 4.0); effects shorter than this play fully")
    parser.add_argument("--pause",    type=float, default=0.8,  help="seconds to show name before animating (default: 0.8)")
    args = parser.parse_args()

    asyncio.run(showcase(args.filter, args.fps, args.duration, args.pause))


if __name__ == "__main__":
    main()
