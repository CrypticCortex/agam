"""Agam interactive terminal dashboard.

Usable, not just informational. Every row supports drill-down (Enter)
and most rows support direct actions (force-sync a queue entry, edit a
rationale, run a lint fix). Auto-refreshes every 30s, manual refresh
via 'r'. All paths come from env vars with sane defaults so the same
module works in both the personal and  installs.

Env vars:
    AGAM_HOME        default ~/.claude/agam
    AGAM_KG_PATH     default ~/.claude/knowledge/graph.db
    AGAM_WORKLOG     default ~/.claude/work-log.md
    AGAM_HOOKS       default ~/.claude/hooks
    AGAM_TOOLS       default ~/.claude/tools

Bindings (top-level):
    q          quit
    r          refresh now
    ?          help
    1..7       jump to tab
    d          drain queue (sync --all in background)
    c          start claude-code container if missing

Per-tab row bindings (DataTables):
    Enter      drill-down modal
    s          force-sync (queue tab)
    D          drop queue row to archive (queue tab, with confirm)
    p          toggle project paused (graph tab, project rows only)
    a          run lint fix (lint tab) -- evaluates via /bin/sh
"""

from __future__ import annotations

import json
import math
import os
import re
import shlex
import sqlite3
import subprocess
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from rich.text import Text

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, ScrollableContainer, Vertical
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    Static,
    TabbedContent,
    TabPane,
)


# ---- paths --------------------------------------------------------------

def _env_path(var: str, default: Path) -> Path:
    v = os.environ.get(var)
    return Path(os.path.expanduser(v)) if v else default


HOME = Path.home()
AGAM = _env_path("AGAM_HOME", HOME / ".claude" / "agam")
KG_DB = _env_path("AGAM_KG_PATH", HOME / ".claude" / "knowledge" / "graph.db")
WORKLOG = _env_path("AGAM_WORKLOG", HOME / ".claude" / "work-log.md")
HOOKS_DIR = _env_path("AGAM_HOOKS", HOME / ".claude" / "hooks")
TOOLS_DIR = _env_path("AGAM_TOOLS", HOME / ".claude" / "tools")

QUEUE_PATH = AGAM / ".pending-closes.jsonl"
ARCHIVE_PATH = AGAM / ".pending-closes.archive.jsonl"
# Neutral shared data home (Cursor + OSS). The shared watchdog drains a
# file-per-session queue here; the TUI surfaces it alongside the legacy
# .pending-closes.jsonl so both agents' pending sessions are visible.
DATA_HOME = _env_path("AGAM_DATA_HOME", HOME / ".agam")
NEW_QUEUE_DIR = DATA_HOME / "queue"
PROCESSED = AGAM / ".processed-sessions.jsonl"
WLOG = AGAM / ".watchdog-log"
LINT = AGAM / ".lint-findings.md"
SUVADU = AGAM / "SUVADU.md"
THISAI = AGAM / "THISAI.md"
AGAM_MD = AGAM / "AGAM.md"
MUGAM = AGAM / "MUGAM.md"


def _find_tool(*candidates: str) -> Path | None:
    """Resolve a tool path across the personal and  install layouts.

    Personal install drops tools at ``~/.claude/tools/<dash-name>.py``.
     install drops them at ``~/.claude/tools/agam/<underscore_name>.py``.
    Try each candidate in order; return the first one that exists.
    """
    for c in candidates:
        p = TOOLS_DIR / c
        if p.exists():
            return p
        # Also try one level down ( layout)
        nested = TOOLS_DIR / "agam" / c
        if nested.exists():
            return nested
    return None


# Probe for the kg CLI under both naming conventions.
KG_CLI = _find_tool("knowledge-graph.py", "knowledge_graph.py")
WATCHDOG_MONITOR = _find_tool("watchdog-monitor.py", "watchdog_monitor.py")


# ---- helpers ------------------------------------------------------------

def _age_str(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds/60)}m"
    if seconds < 86400:
        return f"{seconds/3600:.1f}h"
    return f"{seconds/86400:.1f}d"


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    try:
        for line in path.read_text(errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except OSError:
        pass
    return out


def _bar(value: int, max_value: int, width: int = 20) -> str:
    if max_value <= 0:
        return " " * width
    blocks = "▁▂▃▄▅▆▇█"
    fraction = value / max_value
    filled_full = int(fraction * width)
    remainder = (fraction * width) - filled_full
    bar = "█" * filled_full
    if filled_full < width and remainder > 0:
        bar += blocks[min(int(remainder * len(blocks)), len(blocks) - 1)]
        bar += " " * (width - filled_full - 1)
    else:
        bar += " " * (width - filled_full)
    return bar


_CONTAINER_CACHE: dict = {"name": None, "ts": 0.0}


def _container_name() -> str | None:
    now = time.time()
    if now - _CONTAINER_CACHE["ts"] < 30:
        return _CONTAINER_CACHE["name"]
    try:
        r = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}} {{.Image}}"],
            capture_output=True, text=True, timeout=3,
        )
    except Exception:
        _CONTAINER_CACHE.update(name=None, ts=now)
        return None
    pat = re.compile(r"claude-code", re.IGNORECASE)
    for line in r.stdout.splitlines():
        if pat.search(line):
            _CONTAINER_CACHE.update(name=line.split()[0], ts=now)
            return _CONTAINER_CACHE["name"]
    _CONTAINER_CACHE.update(name=None, ts=now)
    return None


def _tail_jsonl(path: Path, n: int = 60) -> list[dict]:
    if not path.exists():
        return []
    try:
        size = path.stat().st_size
        chunk = min(size, max(8192, n * 250))
        with path.open("rb") as f:
            f.seek(-chunk, os.SEEK_END if size > chunk else os.SEEK_SET)
            data = f.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    out = []
    for line in data.splitlines()[-n:]:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _kg_query(sql: str, params: tuple = ()) -> list:
    if not KG_DB.exists():
        return []
    conn = sqlite3.connect(str(KG_DB))
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def _read_queue() -> list[dict]:
    """Merge both queue sources: legacy .pending-closes.jsonl (personal Claude
    pipeline) + the file-per-session queue/*.json the shared watchdog drains
    (Cursor + OSS). Each entry keeps its 'agent' tag where present."""
    entries = list(_read_jsonl(QUEUE_PATH))
    if NEW_QUEUE_DIR.exists():
        for p in sorted(NEW_QUEUE_DIR.glob("*.json")):
            try:
                entries.append(json.loads(p.read_text()))
            except (json.JSONDecodeError, OSError):
                continue
    return entries


def _provenance_counts() -> list[tuple]:
    """source-agent breakdown across the graph: [(agent, count), ...]."""
    return _kg_query(
        """SELECT COALESCE(p.value, '(untagged)') AS agent, COUNT(*) c
           FROM entities e
           LEFT JOIN properties p
             ON p.entity_id = e.id AND p.key = 'source-agent'
           GROUP BY agent ORDER BY c DESC"""
    )


def _queue_state(entries: list[dict]) -> list[dict]:
    processed = {e.get("session_id") for e in _read_jsonl(PROCESSED)}
    now = time.time()
    out = []
    for e in entries:
        ts = e.get("ts", 0)
        tp = e.get("transcript_path", "")
        try:
            tmtime = os.path.getmtime(tp)
        except OSError:
            tmtime = 0
        age = now - ts
        idle = now - tmtime if tmtime else -1
        if age > 48 * 3600:
            state = "stale"
        elif tmtime == 0:
            state = "missing"
        elif idle >= 600:
            state = "ready"
        else:
            state = "waiting"
        if e.get("session_id") in processed:
            state += "*"
        out.append({**e, "_state": state, "_age": age, "_idle": idle})
    return out


# ---- panel renderers ----------------------------------------------------

def render_overview() -> Text:
    queue = _read_queue()
    queue_states = _queue_state(queue)
    queue_counts = Counter(q["_state"] for q in queue_states)

    daycap_file = AGAM / f".daycap-{time.strftime('%Y-%m-%d')}"
    try:
        daycap_n = int(daycap_file.read_text().strip()) if daycap_file.exists() else 0
    except (ValueError, OSError):
        daycap_n = 0

    container = _container_name()
    log_events = _tail_jsonl(WLOG, 80)
    last_drain = None
    for e in reversed(log_events):
        if e.get("event") in ("done", "work-log-appended", "agam-sync-done"):
            last_drain = e.get("ts")
            break
    last_drain_str = _age_str(time.time() - last_drain) + " ago" if last_drain else "(none seen)"

    ent = _kg_query("SELECT COUNT(*) FROM entities")
    rel = _kg_query("SELECT COUNT(*) FROM relationships")
    types = _kg_query("SELECT type, COUNT(*) c FROM entities GROUP BY type ORDER BY c DESC LIMIT 6")

    text = Text()

    text.append("WATCHDOG\n", style="bold yellow")
    container_color = "green" if container else "red"
    text.append("  container     ", style="grey50")
    text.append(f"{container or '(none) — press c to start'}\n", style=container_color)
    text.append("  queue depth   ", style="grey50")
    text.append(f"{len(queue)}\n", style="yellow")
    text.append("  daycap        ", style="grey50")
    daycap_color = "red" if daycap_n >= 8 else ("orange3" if daycap_n >= 6 else "green")
    text.append(f"{daycap_n}/8 ", style=daycap_color)
    text.append(_bar(daycap_n, 8, 16) + "\n", style="grey50")
    text.append("  last drain    ", style="grey50")
    text.append(f"{last_drain_str}\n\n", style="yellow")

    text.append("QUEUE BREAKDOWN\n", style="bold yellow")
    if queue_counts:
        max_n = max(queue_counts.values())
        for state, n in sorted(queue_counts.items(), key=lambda x: -x[1]):
            color = "green" if "ready" in state else ("orange3" if "stale" in state else "grey50")
            text.append(f"  {state:12}", style=color)
            text.append(_bar(n, max_n, 18) + " ", style=color)
            text.append(f"{n}\n", style="yellow")
    else:
        text.append("  empty\n", style="grey50")
    text.append("\n")

    text.append("KNOWLEDGE GRAPH\n", style="bold yellow")
    if ent:
        n_ent = ent[0][0]
        n_rel = rel[0][0]
        text.append(f"  {n_ent}", style="bold yellow")
        text.append(" entities  ", style="grey50")
        text.append(f"{n_rel}", style="bold yellow")
        text.append(" relationships\n", style="grey50")
        if types:
            max_count = types[0][1]
            for type_name, count in types:
                text.append(f"  {type_name:12} ", style="grey50")
                text.append(_bar(count, max_count, 20) + " ", style="yellow3")
                text.append(f"{count}\n", style="yellow")
    text.append("\n")

    text.append("PROVENANCE (source-agent)\n", style="bold yellow")
    prov = _provenance_counts()
    if prov:
        max_p = max(c for _, c in prov)
        for agent, count in prov:
            color = "cyan" if agent == "cursor" else ("yellow3" if agent == "claude" else "grey50")
            text.append(f"  {agent:12} ", style="grey50")
            text.append(_bar(count, max_p, 18) + " ", style=color)
            text.append(f"{count}\n", style=color)
    else:
        text.append("  (no entities)\n", style="grey50")
    text.append("\n")

    text.append("IDENTITY FILES\n", style="bold yellow")
    for name, path in [("AGAM.md", AGAM_MD), ("THISAI.md", THISAI), ("MUGAM.md", MUGAM), ("SUVADU.md", SUVADU)]:
        if path.exists():
            age_h = (time.time() - path.stat().st_mtime) / 3600
            color = "green" if age_h < 48 else ("orange3" if age_h < 168 else "red")
            lines = sum(1 for _ in path.open())
            text.append(f"  {name:12} ", style="grey50")
            text.append(f"{_age_str(time.time() - path.stat().st_mtime)} ago  ", style=color)
            text.append(f"{lines} lines\n", style="grey50")
        else:
            text.append(f"  {name:12} ", style="grey50")
            text.append("missing\n", style="red")

    return text


def render_activity() -> Text:
    proc = _read_jsonl(PROCESSED)
    today = datetime.now().date()
    counts: dict = defaultdict(int)
    for e in proc:
        ts = e.get("processed_mtime") or e.get("ts") or 0
        if not ts:
            continue
        try:
            d = datetime.fromtimestamp(ts).date()
        except (OSError, ValueError):
            continue
        if (today - d).days <= 90:
            counts[d] += 1

    days_30 = [today - timedelta(days=29 - i) for i in range(30)]
    days_90 = [today - timedelta(days=89 - i) for i in range(90)]
    max_n = max(counts.values()) if counts else 1
    blocks = " ░▒▓█"

    text = Text()
    text.append("LAST 30 DAYS\n", style="bold yellow")
    text.append("  ")
    for d in days_30:
        c = counts.get(d, 0)
        if c == 0:
            text.append(blocks[0])
        else:
            level = min(int((c / max_n) * 4) + 1, 4)
            text.append(blocks[level], style="yellow")
    text.append("\n")
    text.append("  " + days_30[0].strftime("%m-%d") + " " * 18 + days_30[-1].strftime("%m-%d") + "\n", style="grey50")

    total_30 = sum(counts.get(d, 0) for d in days_30)
    streak = 0
    for d in reversed(days_30):
        if counts.get(d, 0) > 0:
            streak += 1
        else:
            break
    text.append(f"\n  {total_30}", style="yellow")
    text.append(" sessions / 30d  ", style="grey50")
    text.append(f"{streak}", style="green" if streak >= 3 else "orange3")
    text.append(" day streak  ", style="grey50")
    text.append(f"max {max_n}\n\n", style="grey50")

    text.append("LAST 90 DAYS (week heatmap)\n", style="bold yellow")
    week_buckets = defaultdict(lambda: [0] * 7)
    for d in days_90:
        week_idx = (today - d).days // 7
        dow = d.weekday()
        week_buckets[week_idx][dow] += counts.get(d, 0)
    weeks = sorted(week_buckets.keys(), reverse=True)
    for dow in range(7):
        text.append(f"  {['M','T','W','T','F','S','S'][dow]} ", style="grey50")
        for w in reversed(weeks):
            c = week_buckets[w][dow]
            if c == 0:
                text.append(" ")
            else:
                level = min(int((c / max_n) * 4) + 1, 4)
                text.append(blocks[level], style="yellow")
        text.append("\n")

    return text


def render_lint() -> Text:
    text = Text()
    if not LINT.exists():
        text.append("no lint run yet.", style="grey50")
        return text
    raw = LINT.read_text().strip()
    for line in raw.splitlines():
        if line.startswith("##"):
            text.append(line + "\n", style="bold yellow")
        elif line.strip().startswith("fix:"):
            text.append("  ")
            text.append(line.strip(), style="green")
            text.append("\n")
        elif re.match(r"^\d+\.", line.strip()):
            text.append(line + "\n", style="orange3")
        else:
            text.append(line + "\n", style="grey50")
    text.append("\n[dim]press a on a finding to apply its fix command[/dim]")
    return text


def render_next_actions() -> Text:
    text = Text()
    suggestions = []
    daycap_file = AGAM / f".daycap-{time.strftime('%Y-%m-%d')}"
    if daycap_file.exists():
        try:
            n = int(daycap_file.read_text().strip())
            if n >= 8:
                suggestions.append(("daycap exhausted", "press d to drain (sync --all)"))
        except (ValueError, OSError):
            pass
    if _container_name() is None:
        suggestions.append(("no claude-code container", "press c to start"))
    queue = _read_queue()
    if len(queue) > 25:
        suggestions.append((f"queue depth high ({len(queue)})", "switch to queue tab to inspect"))
    if LINT.exists():
        body = LINT.read_text()
        if "fix:" in body:
            suggestions.append(("lint findings open", "switch to lint tab"))
    if not suggestions:
        text.append("nothing pressing.", style="green")
        return text
    for label, cmd in suggestions[:4]:
        text.append(f"  {label:28}", style="orange3")
        text.append(cmd + "\n", style="yellow")
    return text


# ---- detail modal -------------------------------------------------------

class DetailScreen(ModalScreen):
    """Modal showing details for a selected row. Esc closes."""

    BINDINGS = [Binding("escape", "dismiss", "close"), Binding("q", "dismiss", "close")]

    def __init__(self, title: str, body: Text | str, **kwargs):
        super().__init__(**kwargs)
        self._title = title
        self._body = body

    def compose(self) -> ComposeResult:
        yield Container(
            Label(f"  {self._title}  ", id="modal-title"),
            ScrollableContainer(Static(self._body, id="modal-body")),
            Label("  [dim]press esc or q to close[/dim]  ", id="modal-foot"),
            id="modal-box",
        )


# ---- animated brain bar -------------------------------------------------

class BrainBar(Static):
    """Top-right panel: an animated ASCII brain. The gyri (~/folds) shimmer like
    neural firing, the core pulses, and each wired agent feeds a pulse into the
    brain. Counts refresh every 10s; animation ticks every 0.5s.

    NOTE: we drive content via ``update()`` -- do NOT override ``_render`` (that
    is Textual's internal method and must return a Visual, not a rich Text)."""

    _W = 24            # brain pixel width
    _H = 14            # brain pixel height (half-block packed -> 7 cell rows)
    _BG = "#0d1120"    # panel background (empty pixels blend into this)

    def on_mount(self) -> None:
        self._frame = 0
        self._agents: list[str] = []
        self._total = 0
        self._prov: dict = {}
        self._refresh_stats()
        self.set_interval(0.4, self._animate)
        self.set_interval(10.0, self._refresh_stats)

    def _refresh_stats(self) -> None:
        home = Path.home()
        agents = []
        if (home / ".claude" / "settings.json").exists() or (home / ".claude" / "hooks").exists():
            agents.append("claude")
        if (home / ".cursor" / "hooks.json").exists():
            agents.append("cursor")
        self._agents = agents
        ent = _kg_query("SELECT COUNT(*) FROM entities")
        self._total = ent[0][0] if ent else 0
        self._prov = {a: c for a, c in _provenance_counts()}
        self.update(self._build())

    def _animate(self) -> None:
        self._frame += 1
        self.update(self._build())

    def _mask(self) -> list[list[bool]]:
        """Procedural brain silhouette: two hemispheres, central fissure, gyri
        grooves that slowly migrate (neural firing). Returns H rows of W bools."""
        W, H = self._W, self._H
        phase = self._frame * 0.12
        out = []
        for py in range(H):
            ny = (py + 0.5) / H * 2 - 1
            row = []
            for px in range(W):
                nx = (px + 0.5) / W * 2 - 1
                ax = abs(nx)
                env = 0.90 + 0.07 * math.sin(ax * 10.0) + (0.05 * math.sin(ax * 16.0) if ny < 0 else 0)
                inside = (nx * nx) / 0.9604 + (ny * ny) / 0.64 < env
                if ny > 0.68:
                    inside = inside and ax < 0.45          # flat base / stem
                if inside and ax > 0.10 and abs(math.sin(ax * 7.5 + ny * 3.2 + phase)) < 0.17:
                    inside = False                          # sulci grooves
                if ax < 0.045 and ny < 0.66:
                    inside = False                          # central fissure
                row.append(inside)
            out.append(row)
        return out

    def _px_hex(self, px: int, py: int, pulse: float) -> str:
        nx = (px + 0.5) / self._W * 2 - 1
        ny = (py + 0.5) / self._H * 2 - 1
        d = min(1.0, math.hypot(nx, ny / 0.85) / 1.05)      # 0 core -> 1 rim
        core = (255, 45, 200)                                # magenta core
        rim = (60, 110, 235)                                 # blue-cyan rim
        r = core[0] + (rim[0] - core[0]) * d
        g = core[1] + (rim[1] - core[1]) * d
        b = core[2] + (rim[2] - core[2]) * d
        bright = 0.55 + 0.45 * pulse * (1.0 - 0.45 * d)       # core breathes brightest
        return f"#{int(r*bright):02x}{int(g*bright):02x}{int(b*bright):02x}"

    def _agent_row(self, idx: int):
        """Synapse packet feeding the brain, for the cell row `idx`."""
        spec = None
        if idx == 2 and "claude" in self._agents:
            spec = ("claude ", "yellow3", "green")
        elif idx == 4 and "cursor" in self._agents:
            spec = ("cursor ", "cyan", "cyan")
        if not spec:
            return None
        label, label_style, pulse_style = spec
        track = ["\u00b7"] * 5
        track[self._frame % 5] = "\u25cf"      # ● packet travels in
        seg = Text()
        seg.append(label, style=label_style)
        seg.append("".join(track) + "\u25b8 ", style=pulse_style)  # ▸
        return seg

    def _build(self) -> Text:
        m = self._mask()
        pulse = 0.5 + 0.5 * math.sin(self._frame * 0.4)
        out = Text(justify="right")
        for r in range(self._H // 2):
            line = Text()
            ag = self._agent_row(r)
            if ag is not None:
                line.append_text(ag)
            top_row, bot_row = m[2 * r], m[2 * r + 1]
            for x in range(self._W):
                top, bot = top_row[x], bot_row[x]
                if not top and not bot:
                    line.append(" ")
                    continue
                fg = self._px_hex(x, 2 * r + 1, pulse) if bot else self._BG
                bgc = self._px_hex(x, 2 * r, pulse) if top else self._BG
                line.append("\u2584", style=f"{fg} on {bgc}")   # ▄ lower half block
            out.append_text(line)
            out.append("\n")

        minds = len(self._agents)
        cn = self._prov.get("claude", 0)
        un = self._prov.get("cursor", 0)
        out.append(f"{minds} mind{'s' if minds != 1 else ''} \u00b7 {self._total} memories  ", style="grey50")
        out.append(f"claude {cn}", style="yellow3")
        out.append(" / ", style="grey50")
        out.append(f"cursor {un}", style="cyan")
        return out


# ---- the App ------------------------------------------------------------

class AgamApp(App):
    """Interactive Agam dashboard."""

    CSS = """
    Screen { background: #080b14; }
    Header { background: #0d1120; color: #e8a849; }
    #brain-bar {
        dock: top;
        height: 8;
        padding: 0 2;
        background: #0d1120;
        color: #e8a849;
        content-align: right top;
    }
    Footer { background: #0d1120; color: #7a7a8a; }
    TabbedContent { background: #080b14; }
    TabPane { padding: 1 2; }
    .pane-static { padding: 1 2; }
    DataTable { background: #080b14; }
    DataTable > .datatable--header { background: #0d1120; color: #e8a849; }
    DataTable > .datatable--cursor { background: #2a2310; }
    #modal-box {
        background: #0d1120;
        border: thick #e8a849;
        padding: 1 2;
        width: 90%;
        height: 80%;
        margin: 2 4;
    }
    #modal-title { color: #e8a849; text-style: bold; padding-bottom: 1; }
    #modal-foot { color: #7a7a8a; padding-top: 1; }
    """

    BINDINGS = [
        Binding("q", "quit", "quit"),
        Binding("r", "manual_refresh", "refresh"),
        Binding("?", "toggle_help", "help"),
        Binding("1", "jump('overview')", "overview"),
        Binding("2", "jump('queue')", "queue"),
        Binding("3", "jump('graph')", "graph"),
        Binding("4", "jump('lessons')", "lessons"),
        Binding("5", "jump('worklog')", "worklog"),
        Binding("6", "jump('activity')", "activity"),
        Binding("7", "jump('lint')", "lint"),
        Binding("d", "drain", "drain queue"),
        Binding("c", "start_container", "start container"),
        Binding("enter", "row_detail", "drill"),
        Binding("s", "sync_row", "sync row"),
        Binding("a", "apply_fix", "apply fix"),
        Binding("p", "toggle_paused", "toggle paused"),
    ]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield BrainBar(id="brain-bar")
        with TabbedContent(initial="overview", id="tabs"):
            with TabPane("overview", id="overview"):
                yield ScrollableContainer(
                    Static(render_overview(), id="overview-stats"),
                    Label("next actions:", classes="pane-static"),
                    Static(render_next_actions(), id="overview-next"),
                )
            with TabPane("queue", id="queue"):
                yield DataTable(id="queue-table", cursor_type="row", zebra_stripes=True)
            with TabPane("graph", id="graph"):
                yield Vertical(
                    Input(placeholder="filter entities...", id="graph-filter"),
                    DataTable(id="graph-table", cursor_type="row", zebra_stripes=True),
                )
            with TabPane("lessons", id="lessons"):
                yield DataTable(id="lessons-table", cursor_type="row", zebra_stripes=True)
            with TabPane("worklog", id="worklog"):
                yield ScrollableContainer(Static(id="worklog-static"))
            with TabPane("activity", id="activity"):
                yield Static(render_activity(), id="activity-static", classes="pane-static")
            with TabPane("lint", id="lint"):
                yield Static(render_lint(), id="lint-static", classes="pane-static")
        yield Footer()

    def on_mount(self) -> None:
        self.title = "agam"
        self.sub_title = "personal knowledge OS"
        self._populate_queue()
        self._populate_graph()
        self._populate_lessons()
        self._populate_worklog()
        self.set_interval(30.0, self._auto_refresh)

    # ---- generic actions ----

    def action_jump(self, name: str) -> None:
        try:
            self.query_one("#tabs", TabbedContent).active = name
        except Exception:
            pass

    def _refresh(self) -> None:
        self.query_one("#overview-stats", Static).update(render_overview())
        self.query_one("#overview-next", Static).update(render_next_actions())
        self.query_one("#activity-static", Static).update(render_activity())
        self.query_one("#lint-static", Static).update(render_lint())
        self._populate_queue()
        self._populate_lessons()
        self._populate_worklog()

    def _auto_refresh(self) -> None:
        self._refresh()

    def action_manual_refresh(self) -> None:
        self._refresh()
        self.notify("refreshed", timeout=1)

    def action_toggle_help(self) -> None:
        self.notify(
            "q quit  r refresh  d drain  c start container  / filter  enter drill\n"
            "1..7 tabs   row actions: s sync  D drop  p pause  a apply fix",
            severity="information", timeout=8,
        )

    def action_drain(self) -> None:
        if WATCHDOG_MONITOR is None:
            self.notify(
                f"watchdog-monitor not found in {TOOLS_DIR} or {TOOLS_DIR}/agam",
                severity="error",
            )
            return
        try:
            subprocess.Popen(
                [str(WATCHDOG_MONITOR), "sync", "--all"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            self.notify("drain started in background", timeout=3)
        except Exception as e:
            self.notify(f"drain failed: {e}", severity="error")

    def action_start_container(self) -> None:
        # Start any stopped container with claude-code image
        try:
            r = subprocess.run(
                ["docker", "ps", "-a", "--format", "{{.Names}} {{.Image}} {{.Status}}"],
                capture_output=True, text=True, timeout=4,
            )
        except Exception as e:
            self.notify(f"docker ps failed: {e}", severity="error")
            return
        pat = re.compile(r"claude-code", re.IGNORECASE)
        target = None
        for line in r.stdout.splitlines():
            if pat.search(line) and "Exited" in line:
                target = line.split()[0]
                break
        if not target:
            running = _container_name()
            if running:
                self.notify(f"already running: {running}", timeout=3)
            else:
                self.notify("no claude-code container found (running or stopped)", severity="warning")
            return
        try:
            r = subprocess.run(["docker", "start", target], capture_output=True, text=True, timeout=15)
            if r.returncode == 0:
                _CONTAINER_CACHE["ts"] = 0  # invalidate cache
                self.notify(f"started {target}", timeout=3)
                self._refresh()
            else:
                self.notify(f"start failed: {r.stderr[:120]}", severity="error")
        except Exception as e:
            self.notify(f"start error: {e}", severity="error")

    # ---- row-level actions: dispatch by current tab ----

    def _active_tab(self) -> str:
        try:
            return self.query_one("#tabs", TabbedContent).active
        except Exception:
            return ""

    def action_row_detail(self) -> None:
        tab = self._active_tab()
        if tab == "queue":
            self._queue_row_detail()
        elif tab == "graph":
            self._graph_row_detail()
        elif tab == "lessons":
            self._lessons_row_detail()

    def action_sync_row(self) -> None:
        if self._active_tab() != "queue":
            return
        idx = self._queue_cursor_idx()
        if idx is None:
            return
        if WATCHDOG_MONITOR is None:
            self.notify("watchdog-monitor not installed", severity="error")
            return
        self.notify(f"force-syncing queue row {idx}...", timeout=4)
        try:
            subprocess.Popen(
                [str(WATCHDOG_MONITOR), "sync", str(idx)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except Exception as e:
            self.notify(f"sync failed: {e}", severity="error")

    def action_toggle_paused(self) -> None:
        if self._active_tab() != "graph":
            return
        row = self._graph_cursor_row()
        if not row:
            return
        name, etype, _, _ = row
        if etype != "project":
            self.notify(f"toggle-paused only valid on project rows ({etype})", severity="warning")
            return
        # Look up current status
        cur = _kg_query("""
            SELECT p.value FROM properties p JOIN entities e ON p.entity_id = e.id
            WHERE e.name = ? AND p.key = 'status'
        """, (name,))
        new_status = "paused" if not cur or cur[0][0] != "paused" else "active"
        if KG_CLI is None:
            self.notify("knowledge-graph CLI not installed", severity="error")
            return
        try:
            r = subprocess.run(
                [str(KG_CLI), "prop", name, "status", new_status],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode == 0:
                self.notify(f"{name}.status = {new_status}", timeout=3)
                self._populate_graph()
            else:
                self.notify(f"kg prop failed: {r.stderr[:100]}", severity="error")
        except Exception as e:
            self.notify(f"prop error: {e}", severity="error")

    def action_apply_fix(self) -> None:
        if self._active_tab() != "lint":
            return
        if not LINT.exists():
            return
        # Apply the FIRST fix command in the lint findings file. Multi-fix
        # selection is a v2 feature; this gives the operator a one-key
        # button for the most-actionable item.
        body = LINT.read_text()
        for line in body.splitlines():
            if line.strip().startswith("fix:"):
                cmd = line.strip().removeprefix("fix:").strip()
                self.notify(f"running: {cmd[:60]}", timeout=4)
                try:
                    # Run via /bin/sh so 'kg prop ...' resolves through PATH
                    r = subprocess.run(
                        ["/bin/sh", "-c", cmd],
                        capture_output=True, text=True, timeout=15,
                    )
                    if r.returncode == 0:
                        self.notify("fix applied", timeout=3)
                        self._refresh()
                    else:
                        self.notify(f"fix failed rc={r.returncode}: {r.stderr[:100]}", severity="error")
                except Exception as e:
                    self.notify(f"fix error: {e}", severity="error")
                return
        self.notify("no fix line found", severity="warning")

    # ---- detail openers ----

    def _queue_cursor_idx(self) -> int | None:
        try:
            tbl = self.query_one("#queue-table", DataTable)
            if tbl.cursor_row is None:
                return None
            return int(tbl.cursor_row)
        except Exception:
            return None

    def _queue_row_detail(self) -> None:
        idx = self._queue_cursor_idx()
        rows = _queue_state(_read_queue())
        if idx is None or idx >= len(rows):
            return
        e = rows[idx]
        body = Text()
        body.append("session_id     ", style="grey50"); body.append(f"{e.get('session_id','?')}\n", style="yellow")
        body.append("agent          ", style="grey50"); body.append(f"{e.get('agent','?')}\n", style="yellow")
        body.append("transcript     ", style="grey50"); body.append(f"{e.get('transcript_path','?')}\n")
        body.append("cwd            ", style="grey50"); body.append(f"{e.get('cwd','?')}\n")
        body.append("context        ", style="grey50"); body.append(f"{e.get('context','?')}\n")
        body.append("state          ", style="grey50"); body.append(f"{e['_state']}\n")
        body.append("age            ", style="grey50"); body.append(f"{_age_str(e['_age'])}\n")
        body.append("idle           ", style="grey50"); body.append(f"{_age_str(e['_idle']) if e['_idle']>=0 else '?'}\n\n")
        try:
            sz = os.path.getsize(e.get("transcript_path", "")) / 1024 / 1024
            body.append(f"transcript size: {sz:.2f} MB\n", style="grey50")
        except OSError:
            body.append("transcript size: unreadable\n", style="red")
        body.append("\nactions: s = force-sync   esc = close", style="green")
        self.push_screen(DetailScreen(f"queue row {idx}", body))

    def _graph_cursor_row(self) -> tuple | None:
        try:
            tbl = self.query_one("#graph-table", DataTable)
            if tbl.cursor_row is None:
                return None
            r = tbl.get_row_at(tbl.cursor_row)
            # Plain text from styled cells
            return tuple(getattr(c, "plain", str(c)) for c in r)
        except Exception:
            return None

    def _graph_row_detail(self) -> None:
        row = self._graph_cursor_row()
        if not row:
            return
        name = row[0]
        rows = _kg_query("SELECT id, type, description FROM entities WHERE name = ?", (name,))
        if not rows:
            self.notify(f"entity not found: {name}", severity="warning")
            return
        eid, etype, desc = rows[0]
        rels = _kg_query("""
            SELECT 'out' AS d, r.relation, e2.name, e2.type FROM relationships r
            JOIN entities e2 ON r.target_id = e2.id WHERE r.source_id = ?
            UNION ALL
            SELECT 'in' AS d, r.relation, e1.name, e1.type FROM relationships r
            JOIN entities e1 ON r.source_id = e1.id WHERE r.target_id = ?
        """, (eid, eid))
        props = _kg_query("SELECT key, value FROM properties WHERE entity_id = ?", (eid,))
        body = Text()
        body.append(f"{name}", style="bold yellow")
        body.append(f"  [{etype}]\n\n", style="grey50")
        body.append(desc or "(no description)\n", style="white")
        body.append("\n\n")
        if rels:
            body.append("relationships:\n", style="bold")
            for d, rel, other, otype in rels:
                arrow = "->" if d == "out" else "<-"
                body.append(f"  {arrow} [{rel}] {other} ({otype})\n", style="grey50")
        if props:
            body.append("\nproperties:\n", style="bold")
            for k, v in props:
                body.append(f"  {k}: ", style="yellow3")
                body.append(f"{v}\n", style="grey50")
        body.append("\nactions: ", style="grey50")
        if etype == "project":
            body.append("p = toggle paused  ", style="green")
        body.append("esc = close", style="green")
        self.push_screen(DetailScreen(name, body))

    def _lessons_row_detail(self) -> None:
        try:
            tbl = self.query_one("#lessons-table", DataTable)
            if tbl.cursor_row is None:
                return
            r = tbl.get_row_at(tbl.cursor_row)
            name = getattr(r[0], "plain", str(r[0]))
        except Exception:
            return
        rows = _kg_query("""
            SELECT e.id, e.description FROM entities e
            WHERE e.type = 'lesson' AND e.name = ?
        """, (name,))
        if not rows:
            return
        eid, desc = rows[0]
        triggers = _kg_query(
            "SELECT key, value FROM properties WHERE entity_id = ?", (eid,),
        )
        body = Text()
        body.append(name, style="bold yellow")
        body.append("\n\n")
        body.append(desc or "(no body)\n", style="white")
        if triggers:
            body.append("\n\ntriggers:\n", style="bold")
            for k, v in triggers:
                body.append(f"  {k}: ", style="yellow3")
                body.append(f"{v}\n", style="grey50")
        body.append("\nesc = close", style="green")
        self.push_screen(DetailScreen(name, body))

    # ---- table populators ----

    def _populate_queue(self) -> None:
        table = self.query_one("#queue-table", DataTable)
        table.clear(columns=True)
        table.add_columns("idx", "state", "sid", "agent", "ctx", "age", "idle", "path")
        rows = _queue_state(_read_queue())
        for i, e in enumerate(rows):
            sid = (e.get("session_id") or "?")[:8]
            agent = (e.get("agent") or "?")[:8]
            ctx = (e.get("context") or "?")[:10]
            tp = e.get("transcript_path", "?")
            short_path = "/".join(tp.split("/")[-2:])
            state = e["_state"]
            color = (
                "green" if "ready" in state else
                "orange3" if "stale" in state else
                "yellow" if "waiting" in state else "grey50"
            )
            agent_color = "cyan" if agent == "cursor" else ("yellow3" if agent == "claude" else "grey50")
            table.add_row(
                str(i),
                Text(state, style=color),
                sid,
                Text(agent, style=agent_color),
                ctx,
                _age_str(e["_age"]),
                _age_str(e["_idle"]) if e["_idle"] >= 0 else "?",
                short_path,
            )

    def _populate_graph(self, filter_str: str = "") -> None:
        table = self.query_one("#graph-table", DataTable)
        table.clear(columns=True)
        table.add_columns("name", "type", "description", "updated")
        if filter_str:
            rows = _kg_query(
                """SELECT name, type, description, datetime(updated, 'unixepoch')
                   FROM entities WHERE name LIKE ? OR description LIKE ?
                   ORDER BY updated DESC LIMIT 200""",
                (f"%{filter_str}%", f"%{filter_str}%"),
            )
        else:
            rows = _kg_query(
                """SELECT name, type, description, datetime(updated, 'unixepoch')
                   FROM entities ORDER BY updated DESC LIMIT 200"""
            )
        for name, type_name, desc, updated in rows:
            short_desc = (desc or "").splitlines()[0][:60] if desc else ""
            short_updated = (updated or "?").split(" ")[0]
            table.add_row(
                Text(name[:30], style="yellow"),
                Text(type_name or "?", style="grey50"),
                short_desc,
                Text(short_updated, style="grey50"),
            )

    def _populate_lessons(self) -> None:
        table = self.query_one("#lessons-table", DataTable)
        table.clear(columns=True)
        table.add_columns("name", "agent", "armed", "description")
        rows = _kg_query("""
            SELECT e.id, e.name, e.description FROM entities e
            WHERE e.type = 'lesson' ORDER BY e.updated DESC
        """)
        for eid, name, desc in rows:
            triggers = _kg_query(
                "SELECT key FROM properties WHERE entity_id = ? AND key IN ('trigger_commands','trigger_errors','armed')",
                (eid,),
            )
            agent_row = _kg_query(
                "SELECT value FROM properties WHERE entity_id = ? AND key = 'source-agent'",
                (eid,),
            )
            agent = agent_row[0][0] if agent_row else "?"
            agent_color = "cyan" if agent == "cursor" else ("yellow3" if agent == "claude" else "grey50")
            armed = "yes" if triggers else "—"
            short_desc = (desc or "").splitlines()[0][:80] if desc else ""
            table.add_row(
                Text(name[:30], style="yellow"),
                Text(agent, style=agent_color),
                Text(armed, style="green" if armed == "yes" else "grey50"),
                short_desc,
            )

    def _populate_worklog(self) -> None:
        static = self.query_one("#worklog-static", Static)
        if not SUVADU.exists():
            static.update(Text("(no SUVADU.md)", style="grey50"))
            return
        text = SUVADU.read_text(errors="replace")
        row_re = re.compile(r"^(\d{4}-\d{2}-\d{2}) \| ([^|]+) \| (.+)$", re.M)
        rows = list(row_re.finditer(text))
        if not rows:
            static.update(Text("(no entries)", style="grey50"))
            return
        recent = rows[-40:]
        grouped: dict = {}
        for m in recent:
            grouped.setdefault(m.group(1), []).append((m.group(2).strip(), m.group(3).strip()))
        out = Text()
        for date in sorted(grouped.keys(), reverse=True)[:7]:
            entries = grouped[date]
            out.append(f"{date}", style="bold yellow")
            out.append(f"   {len(entries)} update{'s' if len(entries) != 1 else ''}\n", style="grey50")
            for file_name, desc in entries:
                short = desc[:160] + (" ..." if len(desc) > 160 else "")
                out.append(f"  {file_name:18} ", style="yellow3")
                out.append(short + "\n", style="grey50")
            out.append("\n")
        static.update(out)

    # ---- input handlers ----

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "graph-filter":
            self._populate_graph(event.value.strip())


def main() -> int:
    AgamApp().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
