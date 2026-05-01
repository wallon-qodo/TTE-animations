#!/usr/bin/env python3
"""
tte-statusbar-live.py — event-driven qodo status bar with TTE transition animations.

Animations fire ONLY when the daemon state actually changes. The state change
is the trigger. No timers, no loops — the animation is the notification.

Transition map (what event → what effect):
  watching ⬡  →  blocked ✖    SynthGrid  full bar  (violation detected)
  blocked ✖   →  watching ⬡   Wipe       full bar  (alert cleared)
  watching ⬡  →  enforcing ✔  Slide      full bar  (rule check starting)
  enforcing ✔ →  watching ⬡   Wipe       full bar  (check completed clean)
  enforcing ✔ →  blocked ✖    SynthGrid  full bar  (check completed blocked)
  token count increases        Sweep      segment   (tokens accumulated)
  review state changes         Smoke      segment   (review mode toggled)

The full bar string (~65 chars) is passed to SynthGrid/Wipe — not just the
segment label. This gives the effects enough canvas to look like a real
system-state reveal rather than a brief character flicker.

Usage:
  python3 tte-statusbar-live.py          # reads /tmp/qodo-statusline (live daemon)
  python3 tte-statusbar-live.py --sim    # simulates state transitions internally
"""

import argparse
import asyncio
import itertools
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

try:
    from rich import box
    from rich.console import Console, Group
    from rich.live import Live
    from rich.panel import Panel
    from rich.text import Text
except ImportError:
    print("Missing dependency.\nRun: pip install rich")
    sys.exit(1)

try:
    from terminaltexteffects.effects.effect_slide      import Slide
    from terminaltexteffects.effects.effect_smoke      import Smoke
    from terminaltexteffects.effects.effect_sweep      import Sweep
    from terminaltexteffects.effects.effect_synthgrid  import SynthGrid
    from terminaltexteffects.effects.effect_wipe       import Wipe
    from terminaltexteffects.effects.effect_colorshift import ColorShift
    from terminaltexteffects.effects.effect_waves      import Waves
    TTE_OK = True
except ImportError:
    TTE_OK = False

STATUSLINE_PATH = "/tmp/qodo-statusline"

# ── Color palette (exact match to statusline.c) ───────────────────────────────

C_BRAND = "color(135)"
C_BAR   = "│"
C_NACNT = "color(209)"

SHIMMER_WATCH   = ["color(75)","color(81)","color(87)","color(123)",
                   "color(87)","color(81)","color(75)","color(69)"]
SHIMMER_ENFORCE = ["color(77)","color(83)","color(119)","color(155)",
                   "color(119)","color(83)","color(77)","color(71)"]
SHIMMER_BLOCK   = ["color(203)","color(209)","color(215)","color(221)",
                   "color(215)","color(209)","color(203)","color(197)"]
SHIMMER_REVIEW  = ["color(136)","color(178)","color(214)","color(220)",
                   "color(214)","color(178)","color(136)","color(130)"]
SHIMMER_HOT     = ["color(214)","color(220)","color(226)","color(227)",
                   "color(226)","color(220)","color(214)","color(208)"]

PULSE_IDLE  = ["color(57)","color(93)","color(129)","color(183)",
               "color(129)","color(93)","color(57)","color(54)"]
PULSE_WARM  = ["color(71)","color(77)","color(83)","color(155)",
               "color(83)","color(77)","color(71)","color(65)"]
PULSE_HOT   = ["color(208)","color(214)","color(220)","color(226)",
               "color(220)","color(214)","color(208)","color(202)"]

SHIELD_CHAR = "⛨"
PROC_CHARS  = ["◐","◓","◑","◒","◐","◓","◑","◒"]
BRAILLE     = ["⣾","⣽","⣻","⢿","⡿","⣟","⣯","⣷"]


# ── Parsed daemon state ───────────────────────────────────────────────────────

@dataclass
class DaemonState:
    mode:        str   = ""
    label:       str   = "watching ⬡"
    review:      str   = "reviewing ◎"
    blocks:      int   = 0
    tokens:      int   = 0
    activity_ms: int   = 9999
    hot_until:   float = 0.0

    def tok_str(self) -> str:
        return f"{self.tokens / 1000:.1f}k" if self.tokens >= 1000 else str(self.tokens)

    def bar_plain(self) -> str:
        """Plain-text representation of the full bar (used as TTE input string)."""
        parts = [f"qodo ⛨  {C_BAR}  {self.label}  {C_BAR}  {self.review}"]
        if self.blocks > 0:
            parts.append(f"  {C_BAR}  {self.blocks} blocked")
        parts.append(f"  {C_BAR}  {self.tok_str()} tokens")
        return "".join(parts)


def _parse(line: str) -> DaemonState:
    """Parse the 7-field statusline format into a DaemonState."""
    fields = line.strip().split("|")
    if len(fields) < 7:
        fields += [""] * (7 - len(fields))
    mode, lbl, rev, blk, tok, act, hot = fields[:7]
    try:
        blocks = int(blk) if blk else 0
    except ValueError:
        blocks = 0
    try:
        tok_val = float(tok.rstrip("k")) * 1000 if tok.endswith("k") else int(tok or 0)
        tokens = int(tok_val)
    except (ValueError, AttributeError):
        tokens = 0
    try:
        activity_ms = int(act) if act else 9999
    except ValueError:
        activity_ms = 9999
    try:
        hot_until = float(hot) if hot else 0.0
    except ValueError:
        hot_until = 0.0
    label  = lbl  or "watching ⬡"
    review = rev  or "reviewing ◎"
    return DaemonState(mode, label, review, blocks, tokens, activity_ms, hot_until)


def _read_statusline() -> Optional[DaemonState]:
    try:
        with open(STATUSLINE_PATH) as f:
            return _parse(f.read())
    except OSError:
        return None


# ── Transition trigger map ────────────────────────────────────────────────────
# (prev_label, curr_label) → (effect_class, scope, fps, max_frames)
# scope "bar"     → animate the full bar plain-text string
# scope "tokens"  → animate just "{tok} tokens"
# scope "review"  → animate just the review segment text

LABEL_TRANSITIONS: dict[tuple[str, str], tuple] = {
    ("watching ⬡",  "blocked ✖"):   (SynthGrid, "bar",     30, 130),
    ("watching ⬡",  "enforcing ✔"): (Slide,     "bar",     30, None),
    ("enforcing ✔", "watching ⬡"):  (Wipe,      "bar",     30, None),
    ("enforcing ✔", "blocked ✖"):   (SynthGrid, "bar",     30, 130),
    ("blocked ✖",   "watching ⬡"):  (Wipe,      "bar",     30, None),
    ("blocked ✖",   "enforcing ✔"): (Slide,     "bar",     30, None),
}


def _diff(prev: DaemonState, curr: DaemonState) -> list[tuple]:
    """Return list of (effect_class, scope, text, fps, max_frames, description) to fire."""
    events = []

    # Label transition — highest priority, full bar animation
    key = (prev.label, curr.label)
    if key in LABEL_TRANSITIONS:
        cls, scope, fps, mf = LABEL_TRANSITIONS[key]
        text = curr.bar_plain()
        desc = f"{prev.label}  →  {curr.label}"
        events.append((cls, scope, text, fps, mf, desc))

    # Token increase → Sweep on tokens segment
    elif curr.tokens > prev.tokens and curr.tokens > 0:
        text = f"{curr.tok_str()} tokens"
        events.append((Sweep, "tokens", text, 30, None,
                        f"tokens  {prev.tok_str()}  →  {curr.tok_str()}"))

    # Review toggle → Smoke on review segment
    if curr.review != prev.review:
        events.append((Smoke, "review", curr.review, 30, 90,
                        f"review  →  {curr.review}"))

    return events


# ── Render state ──────────────────────────────────────────────────────────────

@dataclass
class RenderState:
    daemon:       DaemonState = field(default_factory=DaemonState)
    # Animation overlay
    bar_frame:    Optional[str]  = None   # full-bar TTE frame (replaces whole bar)
    seg_segment:  Optional[str]  = None   # "tokens" | "review" (segment overlay)
    seg_frame:    Optional[str]  = None   # frame for that segment
    active_effect: str           = ""
    # Transition log (last N entries)
    log:          list           = field(default_factory=list)


def _fi() -> int:
    return int(time.time() * 10) % 8


def _act(d: DaemonState):
    fi = _fi()
    now = time.time()
    is_hot  = d.activity_ms < 500 or (d.hot_until > 0 and d.hot_until > now)
    is_warm = not is_hot and d.activity_ms < 4000
    return ("hot" if is_hot else "warm" if is_warm else "idle"), fi


def _frame_to_text(frame: str, prefix: str = "") -> Text:
    try:
        return Text.from_ansi(prefix + frame)
    except Exception:
        clean = re.sub(r"\x1b\[[^m]*m", "", frame)
        return Text(prefix + clean)


def _render_bar(rs: RenderState) -> Text:
    d  = rs.daemon
    fi = _fi()
    act, _ = _act(d)

    # If a full-bar animation is playing, show that
    if rs.bar_frame is not None:
        return _frame_to_text(rs.bar_frame, "  ")

    if act == "hot":
        sp, pulse_c = PROC_CHARS[fi], PULSE_HOT[fi]
    elif act == "warm":
        sp, pulse_c = BRAILLE[fi], PULSE_WARM[fi]
    else:
        sp, pulse_c = SHIELD_CHAR, PULSE_IDLE[fi]

    if "enforc" in d.label:
        shimmer = SHIMMER_ENFORCE[fi]
    elif "block" in d.label:
        shimmer = SHIMMER_BLOCK[fi]
    elif "watch" in d.label:
        shimmer = (SHIMMER_HOT[fi] if act == "hot"
                   else SHIMMER_ENFORCE[fi] if act == "warm"
                   else SHIMMER_WATCH[fi])
    else:
        shimmer = C_BRAND

    rev_c = (SHIMMER_HOT[fi] if act == "hot"
             else SHIMMER_REVIEW[fi] if act == "warm"
             else "color(241)")

    bar = Text(no_wrap=True, overflow="ellipsis")

    # Brand segment — animated by ColorShift idle heartbeat
    if rs.seg_segment == "brand" and rs.seg_frame is not None:
        bar.append("  ")
        bar.append_text(_frame_to_text(rs.seg_frame))
    else:
        bar.append("  qodo ", style=f"bold {C_BRAND}")
        bar.append(sp, style=f"bold {pulse_c}")

    bar.append(f"  {C_BAR}  ", style=C_BRAND)
    bar.append(d.label, style=shimmer)
    bar.append(f"  {C_BAR}  ", style=C_BRAND)

    # Review segment — may be animated
    if rs.seg_segment == "review" and rs.seg_frame is not None:
        bar.append_text(_frame_to_text(rs.seg_frame))
    else:
        bar.append(d.review, style=rev_c)

    if d.blocks > 0:
        bar.append(f"  {C_BAR}  ", style=C_BRAND)
        bar.append(f"{d.blocks} blocked", style=C_NACNT)

    bar.append(f"  {C_BAR}  ", style=C_BRAND)

    # Tokens segment — may be animated
    if rs.seg_segment == "tokens" and rs.seg_frame is not None:
        bar.append_text(_frame_to_text(rs.seg_frame))
    else:
        bar.append(f"{d.tok_str()} tokens", style="dim")

    return bar


def _render_log(rs: RenderState) -> Text:
    t = Text()
    for ts, desc, effect, scope in rs.log[-6:]:
        t.append(f"  {ts}  ", style="dim")
        t.append(f"{desc}", style="color(147)")
        t.append(f"  →  {effect}", style="bold color(69)")
        t.append(f"  [{scope}]\n", style="dim color(69)")
    if not rs.log:
        t.append("  waiting for state change…\n", style="dim")
    return t


def _render(rs: RenderState, console: Console) -> Panel:
    bar = _render_bar(rs)

    effect_hint = Text(no_wrap=True)
    if rs.active_effect:
        effect_hint.append(f"  ▶ {rs.active_effect}", style="bold color(69)")
    elif rs.seg_segment == "brand":
        effect_hint.append("  ◈ ColorShift  [idle heartbeat]", style="dim color(135)")
    elif rs.bar_frame is not None:
        effect_hint.append("  ◈ Waves  [warm activity]", style="dim color(77)")
    else:
        effect_hint.append("  ○ idle — waiting for transition", style="dim")

    sep = Text("  " + "─" * 62, style="dim color(237)")

    log_hdr = Text("  transition log", style="dim")
    log     = _render_log(rs)

    content = Group(
        Text(""),
        bar,
        Text(""),
        sep,
        effect_hint,
        sep,
        log_hdr,
        Text(""),
        log,
    )

    border = "color(197)" if "block" in rs.daemon.label else C_BRAND
    return Panel(
        content,
        title="[bold color(135)]qodo  ⛨  status bar[/bold color(135)]",
        title_align="left",
        border_style=border,
        box=box.ROUNDED,
        padding=(0, 1),
    )


# ── TTE animation engine ──────────────────────────────────────────────────────

def _gen_frames(cls, text: str, max_frames: Optional[int]) -> list:
    eff = cls(text)
    if max_frames:
        return list(itertools.islice(eff, max_frames))
    return list(eff)


async def _play_transition(
    cls, scope: str, text: str, fps: int, max_frames: Optional[int],
    effect_name: str, desc: str,
    rs: RenderState, refresh,
) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    rs.active_effect = f"{effect_name} [{scope}]"
    rs.log.append((ts, desc, effect_name, scope))
    refresh()

    try:
        frames = await asyncio.to_thread(_gen_frames, cls, text, max_frames)
    except Exception:
        rs.active_effect = ""
        refresh()
        return

    delay = 1.0 / fps
    try:
        for frame in frames:
            if scope == "bar":
                rs.bar_frame   = frame
                rs.seg_frame   = None
                rs.seg_segment = None
            else:
                rs.bar_frame   = None
                rs.seg_segment = scope
                rs.seg_frame   = frame
            refresh()
            await asyncio.sleep(delay)
    finally:
        rs.bar_frame   = None
        rs.seg_frame   = None
        rs.seg_segment = None
        rs.active_effect = ""
        refresh()


# ── Continuous alive animations ──────────────────────────────────────────────
#
# Three layers — each fills a different purpose:
#
#  1. _shimmer_tick     — 10fps palette refresh, no TTE.
#                         Directly mirrors statusline.c's clock-based frame index.
#                         Makes the ⛨ pulse and label shimmer cycle visually.
#
#  2. _anim_idle_loop   — ColorShift on "qodo ⛨" while IDLE (>4s since last tool).
#                         Subtle breathing effect — "daemon is alive, nothing happening."
#                         Replays every ~8s. Cancelled the moment a transition fires.
#
#  3. _anim_warm_loop   — Waves on the full bar while WARM (500ms–4s since tool call).
#                         More energetic — "daemon just processed something."
#                         Cancelled when activity_ms crosses back to IDLE or goes HOT.
#
# Priority: transition animation > warm loop > idle loop > shimmer tick.
# Any higher-priority animation cancels lower-priority ones cleanly.


async def _shimmer_tick(rs: RenderState, refresh, stop: asyncio.Event) -> None:
    """10fps tick — cycles the palette frame index so shimmer colors animate."""
    while not stop.is_set():
        refresh()
        await asyncio.sleep(0.1)


async def _anim_idle_loop(rs: RenderState, refresh, stop: asyncio.Event) -> None:
    """ColorShift on 'qodo ⛨' — subtle heartbeat while daemon is idle."""
    brand_text = "qodo ⛨"
    try:
        while not stop.is_set():
            # Only play when genuinely idle and no other animation is active
            if rs.bar_frame is None and rs.seg_frame is None and not rs.active_effect:
                try:
                    frames = await asyncio.to_thread(
                        _gen_frames, ColorShift, brand_text, 60
                    )
                except Exception:
                    await asyncio.sleep(2.0)
                    continue
                for frame in frames:
                    if stop.is_set() or rs.bar_frame is not None or rs.active_effect:
                        break
                    rs.seg_segment = "brand"
                    rs.seg_frame   = frame
                    refresh()
                    await asyncio.sleep(1 / 30)
                rs.seg_segment = None
                rs.seg_frame   = None
                refresh()
            # Rest between heartbeats
            await asyncio.sleep(6.0)
    except asyncio.CancelledError:
        rs.seg_segment = None
        rs.seg_frame   = None
        raise


async def _anim_warm_loop(rs: RenderState, refresh, stop: asyncio.Event) -> None:
    """Waves on the full bar while daemon is WARM — shows recent activity."""
    try:
        while not stop.is_set():
            act, _ = _act(rs.daemon)
            if act == "warm" and rs.bar_frame is None and not rs.active_effect:
                bar_text = rs.daemon.bar_plain()
                try:
                    frames = await asyncio.to_thread(_gen_frames, Waves, bar_text, 80)
                except Exception:
                    await asyncio.sleep(1.0)
                    continue
                for frame in frames:
                    if stop.is_set() or rs.active_effect:
                        break
                    act2, _ = _act(rs.daemon)
                    if act2 != "warm":
                        break
                    rs.bar_frame = frame
                    refresh()
                    await asyncio.sleep(1 / 30)
                rs.bar_frame = None
                refresh()
            await asyncio.sleep(0.3)
    except asyncio.CancelledError:
        rs.bar_frame = None
        raise


class AliveAnimator:
    """Manages the three-layer alive animation stack with clean task lifecycle."""

    def __init__(self, rs: RenderState, refresh):
        self._rs      = rs
        self._refresh = refresh
        self._shimmer_stop = asyncio.Event()
        self._idle_stop    = asyncio.Event()
        self._warm_stop    = asyncio.Event()
        self._shimmer_task: Optional[asyncio.Task] = None
        self._idle_task:    Optional[asyncio.Task] = None
        self._warm_task:    Optional[asyncio.Task] = None

    def start(self):
        self._shimmer_stop.clear()
        self._idle_stop.clear()
        self._warm_stop.clear()
        self._shimmer_task = asyncio.create_task(
            _shimmer_tick(self._rs, self._refresh, self._shimmer_stop)
        )
        self._idle_task = asyncio.create_task(
            _anim_idle_loop(self._rs, self._refresh, self._idle_stop)
        )
        self._warm_task = asyncio.create_task(
            _anim_warm_loop(self._rs, self._refresh, self._warm_stop)
        )

    def pause_tte(self):
        """Pause TTE loops before a transition animation plays."""
        self._idle_stop.set()
        self._warm_stop.set()
        if self._idle_task and not self._idle_task.done():
            self._idle_task.cancel()
        if self._warm_task and not self._warm_task.done():
            self._warm_task.cancel()

    def resume_tte(self):
        """Resume TTE loops after a transition animation completes."""
        self._idle_stop.clear()
        self._warm_stop.clear()
        self._idle_task = asyncio.create_task(
            _anim_idle_loop(self._rs, self._refresh, self._idle_stop)
        )
        self._warm_task = asyncio.create_task(
            _anim_warm_loop(self._rs, self._refresh, self._warm_stop)
        )

    async def stop(self):
        self._shimmer_stop.set()
        self._idle_stop.set()
        self._warm_stop.set()
        tasks = [t for t in [self._shimmer_task, self._idle_task, self._warm_task]
                 if t and not t.done()]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


# ── Poller ────────────────────────────────────────────────────────────────────

async def _poll_live(rs: RenderState, refresh) -> None:
    """Poll /tmp/qodo-statusline every 200ms; fire animations on field changes."""
    prev      = rs.daemon
    anim_task: Optional[asyncio.Task] = None
    alive     = AliveAnimator(rs, refresh)
    alive.start()

    try:
        while True:
            await asyncio.sleep(0.2)
            curr = _read_statusline()
            if curr is None:
                continue

            rs.daemon.activity_ms = curr.activity_ms
            rs.daemon.hot_until   = curr.hot_until

            events = _diff(prev, curr)

            if events and (anim_task is None or anim_task.done()):
                rs.daemon = curr
                cls, scope, text, fps, mf, desc = events[0]
                alive.pause_tte()
                anim_task = asyncio.create_task(
                    _play_transition(cls, scope, text, fps, mf,
                                     cls.__name__, desc, rs, refresh)
                )
                # Resume alive loops once transition finishes
                async def _resume_after(task):
                    await asyncio.gather(task, return_exceptions=True)
                    alive.resume_tte()
                asyncio.create_task(_resume_after(anim_task))
            elif not events:
                rs.daemon = curr

            prev = curr
    finally:
        await alive.stop()


# ── Simulation mode ───────────────────────────────────────────────────────────

SIM_STEPS = [
    # (delay, state_line)  — written to a temp file the poller reads
    (1.5,  "|watching ⬡|reviewing ◎||0|9999|0"),
    (1.0,  "|watching ⬡|reviewing ◎||0|80|0"),      # HOT: tool fired
    (1.2,  "|blocked ✖|reviewing ◎|1|206|145|0"),    # BLOCKED → SynthGrid
    (3.5,  "|watching ⬡|reviewing ◎|1|206|9999|0"),  # cleared → Wipe
    (1.5,  "|watching ⬡|reviewing ◎|1|206|60|0"),    # HOT again
    (1.2,  "|blocked ✖|reviewing ◎|2|412|110|0"),    # second block → SynthGrid
    (3.5,  "|watching ⬡|reviewing ◎|2|412|9999|0"),  # cleared → Wipe
    (1.5,  "|watching ⬡|review off|2|412|9999|0"),   # review toggled → Smoke
    (2.0,  "|watching ⬡|reviewing ◎|2|1.2k|9999|0"), # tokens jump → Sweep
    (2.0,  "|watching ⬡|reviewing ◎|2|1.2k|9999|0"), # hold
]


async def _poll_sim(rs: RenderState, refresh) -> None:
    """Simulate state transitions; drives the same animation engine as live mode."""
    prev  = rs.daemon
    alive = AliveAnimator(rs, refresh)
    alive.start()

    # Let the idle heartbeat play briefly before the first transition
    await asyncio.sleep(1.5)

    anim_task: Optional[asyncio.Task] = None
    try:
        for delay, line in SIM_STEPS:
            await asyncio.sleep(delay)
            curr = _parse(line)
            events = _diff(prev, curr)

            rs.daemon.activity_ms = curr.activity_ms
            rs.daemon.hot_until   = curr.hot_until

            if events and (anim_task is None or anim_task.done()):
                rs.daemon = curr
                cls, scope, text, fps, mf, desc = events[0]
                alive.pause_tte()
                anim_task = asyncio.create_task(
                    _play_transition(cls, scope, text, fps, mf,
                                     cls.__name__, desc, rs, refresh)
                )
                async def _resume_after(task):
                    await asyncio.gather(task, return_exceptions=True)
                    alive.resume_tte()
                asyncio.create_task(_resume_after(anim_task))
            else:
                rs.daemon = curr

            prev = curr

        if anim_task and not anim_task.done():
            await anim_task
        await asyncio.sleep(2.0)
    finally:
        await alive.stop()


# ── Entry point ───────────────────────────────────────────────────────────────

async def run(sim: bool) -> None:
    if not TTE_OK:
        print("Install terminaltexteffects first:  pip install terminaltexteffects")
        sys.exit(1)

    rs      = RenderState()
    console = Console()

    # Seed with current state or offline placeholder
    initial = _read_statusline()
    if initial:
        rs.daemon = initial
    else:
        rs.daemon = DaemonState(label="offline ⚠", review="reviewing ◎")

    with Live(_render(rs, console), console=console,
              refresh_per_second=15, screen=False) as live:
        refresh = lambda: live.update(_render(rs, console))

        if sim:
            await _poll_sim(rs, refresh)
        else:
            await _poll_live(rs, refresh)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="qodo status bar — event-driven TTE transition animations"
    )
    parser.add_argument(
        "--sim", action="store_true",
        help="simulate state transitions (no daemon needed)"
    )
    args = parser.parse_args()
    asyncio.run(run(args.sim))


if __name__ == "__main__":
    main()
