"""Agam interactive terminal dashboard.

Usable, not just informational. Every row supports drill-down (Enter)
and most rows support direct actions (force-sync a queue entry, edit a
rationale, run a lint fix). Auto-refreshes every 30s, manual refresh
via 'r'. All paths come from env vars with sane defaults so the same
module works across Claude Code, Cursor, and Codex installs.

Env vars:
    AGAM_DATA_HOME   default ~/.agam
    AGAM_HOME        default AGAM_DATA_HOME
    AGAM_KG_PATH     default AGAM_DATA_HOME/knowledge/graph.db
    AGAM_WORK_LOG    default AGAM_HOME/work-log.md
    AGAM_HOOKS_DIR   default AGAM_HOME/hooks
    AGAM_TOOLS_DIR   default AGAM_HOME/tools

Bindings (top-level):
    q          quit
    r          refresh now
    ?          help
    1..7       jump to tab
    d          confirm/drain all session rows (sync --all in background)
    c          start claude-code container if missing

Per-tab row bindings (DataTables):
    Enter      drill-down modal
    s          force-sync selected session
    D          archive selected session (double-press confirmation)
    h          retry selected sealed review with Claude CLI / Haiku
    x          resolve selected review through a signed decision dialog
    P          publish a new immutable vault version (double-press confirmation)
    a          run lint fix (Health view) -- evaluates via /bin/sh
"""

from __future__ import annotations

import json
import math
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import tempfile
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from rich.text import Text

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, ScrollableContainer, Vertical
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


def _data_home(home: Path = HOME) -> Path:
    """Resolve Agam's shared, agent-neutral operational home."""
    return _env_path("AGAM_DATA_HOME", home / ".agam")


def _cursor_home() -> Path:
    """Backward-compatible alias for the former Cursor-specific resolver."""
    return _data_home()


def _identity_home(home: Path, data_home: Path | None = None) -> Path:
    """Use shared identity when present, else a pre-migration Claude identity."""
    if os.environ.get("AGAM_HOME"):
        return _env_path("AGAM_HOME", home / ".agam")
    shared = data_home or _data_home(home)
    legacy = home / ".claude" / "agam"
    shared_identity = any(
        (shared / filename).exists()
        for filename in ("AGAM.md", "THISAI.md", "config.yaml")
    )
    return shared if shared_identity or not legacy.exists() else legacy


def _knowledge_db(home: Path, data_home: Path | None = None) -> Path:
    """Prefer the shared graph, with a read-compatible legacy fallback."""
    if os.environ.get("AGAM_KG_PATH"):
        return _env_path("AGAM_KG_PATH", home / ".agam" / "knowledge" / "graph.db")
    shared = (data_home or _data_home(home)) / "knowledge" / "graph.db"
    legacy = home / ".claude" / "knowledge" / "graph.db"
    return shared if shared.exists() or not legacy.exists() else legacy


def _first_existing(default: Path, *fallbacks: Path) -> Path:
    """Return the first existing compatibility path, else the shared default."""
    for candidate in (default, *fallbacks):
        if candidate.exists():
            return candidate
    return default


DATA_HOME = _data_home()
AGAM = _identity_home(HOME, DATA_HOME)
KG_DB = _knowledge_db(HOME, DATA_HOME)
WORKLOG = _env_path(
    "AGAM_WORK_LOG",
    _env_path(
        "AGAM_WORKLOG",
        _first_existing(
            DATA_HOME / "work-log.md",
            HOME / ".claude" / "work-log.md",
            AGAM / "work-log.md",
        ),
    ),
)
HOOKS_DIR = _env_path(
    "AGAM_HOOKS_DIR",
    _env_path(
        "AGAM_HOOKS",
        _first_existing(DATA_HOME / "hooks", HOME / ".claude" / "hooks"),
    ),
)
TOOLS_DIR = _env_path(
    "AGAM_TOOLS_DIR",
    _env_path(
        "AGAM_TOOLS",
        _first_existing(DATA_HOME / "tools", HOME / ".claude" / "tools"),
    ),
)

QUEUE_PATH = AGAM / ".pending-closes.jsonl"
ARCHIVE_PATH = AGAM / ".pending-closes.archive.jsonl"
NEW_QUEUE_DIR = DATA_HOME / "queue"
SHARED_PROCESSED = DATA_HOME / "processed"
SHARED_ERRORS = DATA_HOME / "queue-errors"
SHARED_WLOG = DATA_HOME / "logs" / "watchdog.log"
SCOPES_ROOT = DATA_HOME / "knowledge" / "scopes"
REGISTRY_PATH = SCOPES_ROOT / "registry.json"
# Back-compatible aliases for callers that imported the old Cursor-era names.
CURSOR_PROCESSED = SHARED_PROCESSED
CURSOR_ERRORS = SHARED_ERRORS
CURSOR_WLOG = SHARED_WLOG
PROCESSED = AGAM / ".processed-sessions.jsonl"
WLOG = AGAM / ".watchdog-log"
LINT = AGAM / ".lint-findings.md"
SUVADU = AGAM / "SUVADU.md"
THISAI = AGAM / "THISAI.md"
AGAM_MD = AGAM / "AGAM.md"
MUGAM = AGAM / "MUGAM.md"


def _find_tool(*candidates: str) -> Path | None:
    """Resolve a tool path across legacy and shared install layouts.

    Personal install drops tools at ``~/.claude/tools/<dash-name>.py``.
    Shared installs use ``~/.agam/tools/agam/<underscore_name>.py``.
    Try each candidate in order; return the first one that exists.
    """
    for c in candidates:
        p = TOOLS_DIR / c
        if p.exists():
            return p
        # Also try the shared install's one-level-down package directory.
        nested = TOOLS_DIR / "agam" / c
        if nested.exists():
            return nested
    return None


# Probe for the kg CLI under both naming conventions.
KG_CLI = _find_tool("knowledge-graph.py", "knowledge_graph.py")
WATCHDOG_MONITOR = _find_tool("watchdog-monitor.py", "watchdog_monitor.py")
_PACKAGE_WATCHDOG = Path(__file__).parent / "hooks" / "agam_watchdog.sh"
WATCHDOG_SHELL = _PACKAGE_WATCHDOG if _PACKAGE_WATCHDOG.exists() else None


def _vault_catalog():
    from agam.vaults import VaultCatalog

    return VaultCatalog(SCOPES_ROOT, agent="codex")


def _review_queue():
    from agam.review_queue import ReviewQueue

    return ReviewQueue.discover(DATA_HOME)


# ---- helpers ------------------------------------------------------------

_AGENT_STYLES = {
    "claude": "yellow3",
    "cursor": "cyan",
    "codex": "#10a37f",
}


def _agent_style(agent: str) -> str:
    """Consistent provenance color for every supported agent."""
    return _AGENT_STYLES.get(agent, "grey50")


def _file_mentions(path: Path, markers: tuple[str, ...]) -> bool:
    try:
        body = path.read_text(encoding="utf-8")
    except OSError:
        return False
    return any(marker in body for marker in markers)


def _wired_agents(home: Path = HOME) -> list[str]:
    """Return agents whose Agam-specific wiring is installed.

    Config-directory presence alone is not enough: the animation represents a
    live wire to the shared brain, not merely an installed editor or CLI.
    """
    specs = (
        (
            "claude",
            home / ".claude" / "settings.json",
            (
                home / ".claude" / "hooks" / "graph_recall.py",
                home / ".claude" / "hooks" / "session_close.py",
                home / ".claude" / "hooks" / "graph-recall.py",
                home / ".claude" / "hooks" / "session-close-hook.py",
            ),
        ),
        (
            "cursor",
            home / ".cursor" / "hooks.json",
            (
                home / ".cursor" / "hooks" / "cursor_stop.py",
                home / ".cursor" / "hooks" / "cursor_session_end.py",
            ),
        ),
        (
            "codex",
            home / ".codex" / "hooks.json",
            (
                home / ".codex" / "hooks" / "agam" / "graph_recall.py",
                home / ".codex" / "hooks" / "agam" / "codex_stop.py",
            ),
        ),
    )
    wired: list[str] = []
    for name, config, owned_files in specs:
        markers = tuple(str(path) for path in owned_files)
        if any(path.exists() for path in owned_files) or _file_mentions(config, markers):
            wired.append(name)
    return wired

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
    (Claude, Cursor, and Codex). Each entry keeps its agent provenance."""
    entries = [
        {**entry, "_queue_source": "legacy", "_queue_index": index}
        for index, entry in enumerate(_read_jsonl(QUEUE_PATH))
    ]
    if NEW_QUEUE_DIR.exists():
        for p in sorted(NEW_QUEUE_DIR.glob("*.json")):
            try:
                payload = json.loads(p.read_text())
                if not isinstance(payload, dict):
                    continue
                entries.append(
                    {
                        **payload,
                        "_queue_source": "file",
                        "_queue_file": str(p),
                    }
                )
            except (json.JSONDecodeError, OSError, TypeError):
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

def _last_drain_ts() -> float | None:
    """Most recent successful drain across BOTH pipelines: the personal jsonl
    watchdog-log and the shared text watchdog.log."""
    best = None
    for e in reversed(_tail_jsonl(WLOG, 120)):
        if e.get("event") in ("done", "work-log-appended", "agam-sync-done"):
            best = e.get("ts")
            break
    if SHARED_WLOG.exists():
        try:
            for line in reversed(SHARED_WLOG.read_text(errors="replace").splitlines()):
                if line.startswith("[") and (" ok " in line or "drain-done" in line):
                    iso = line[1:line.index("]")].replace("Z", "+00:00")
                    ts = datetime.fromisoformat(iso).timestamp()
                    best = ts if best is None else max(best, ts)
                    break
        except (ValueError, OSError):
            pass
    return best


def _draining() -> bool:
    """A drain is in flight if either pipeline holds its single-flight lock."""
    return (AGAM / ".watchdog.lock.d").exists() or (DATA_HOME / ".watchdog.lock").exists()


def _error_count() -> int:
    n = 0
    for d in {SHARED_ERRORS, AGAM / "queue-errors"}:
        if d.exists() and d.is_dir():
            n += sum(1 for _ in d.glob("*.json"))
    return n


def _daily_cap() -> int:
    """The watchdog's MAX_PER_DAY. Honors AGAM_MAX_PER_DAY, else parses the
    personal watchdog script, else a conservative default."""
    env = os.environ.get("AGAM_MAX_PER_DAY")
    if env and env.isdigit():
        return int(env)
    wd = HOOKS_DIR / "agam-watchdog.sh"
    if wd.exists():
        try:
            m = re.search(r"^MAX_PER_DAY=(\d+)", wd.read_text(), re.M)
            if m:
                return int(m.group(1))
        except OSError:
            pass
    return 8


def _today_growth() -> int:
    rows = _kg_query(
        "SELECT COUNT(*) FROM entities WHERE created LIKE ?",
        (time.strftime("%Y-%m-%d") + "%",),
    )
    return rows[0][0] if rows else 0


def _invoker() -> tuple[str, str]:
    """(label, color): can the watchdog drain right now?"""
    if _container_name():
        return ("container", "green")
    if any(shutil.which(cli) for cli in ("claude", "cursor-agent", "codex")):
        return ("host", "green")
    return ("none", "red")


def render_overview() -> Text:
    # --- gather the vital signs (cheap) ---
    states = _queue_state(_read_queue())
    # _queue_state appends "*" to the state for sessions already in
    # .processed-sessions.jsonl -- those are done, never count them.
    pending = [s for s in states if not s["_state"].endswith("*")]
    # Actionable = will actually be drained (ready/waiting). Stale/missing are
    # cruft (>48h old, or transcript gone) -- counting them as "queue" is the
    # noise we're killing. Surface them separately as prunable.
    actionable = [s for s in pending if "ready" in s["_state"] or "waiting" in s["_state"]]
    stale_n = len(pending) - len(actionable)
    qn = len(actionable)
    ready = sum(1 for s in actionable if "ready" in s["_state"])
    waiting = sum(1 for s in actionable if "waiting" in s["_state"])
    oldest = max((s["_age"] for s in actionable), default=0)

    daycap_file = AGAM / f".daycap-{time.strftime('%Y-%m-%d')}"
    try:
        daycap_n = int(daycap_file.read_text().strip()) if daycap_file.exists() else 0
    except (ValueError, OSError):
        daycap_n = 0
    cap = _daily_cap()
    cap_left = max(0, cap - daycap_n)

    last = _last_drain_ts()
    last_age = (time.time() - last) if last else None
    errors = _error_count()
    inv_label, inv_color = _invoker()

    try:
        vaults = _vault_catalog().summaries()
    except Exception:
        vaults = ()
    counts = {item.scope: item.entities for item in vaults}
    portable_total = sum(
        item.entities for item in vaults if item.readable
    )
    active_version = vaults[0].version if vaults else None

    t = Text()

    def row(label: str, value: str, color: str) -> None:
        t.append(f"  {label:13}", style="grey50")
        t.append(f"{value}\n", style=color)

    t.append("PIPELINE\n", style="bold yellow")
    row(
        "queue",
        f"{qn} active" + (f"   ({ready} ready, {waiting} waiting)" if qn else ""),
        "green" if qn == 0 else ("red" if qn > 25 else "yellow"),
    )
    if stale_n:
        row("stale", f"{stale_n}  (prunable -- Sessions, D)", "orange3")
    row("in-flight", "draining now" if _draining() else "idle",
        "green" if _draining() else "grey50")
    row("daily cap", f"{cap_left}/{cap} left",
        "red" if cap_left == 0 else ("orange3" if cap_left <= 2 else "green"))
    if last_age is None:
        row("last drain", "(none seen)", "orange3")
    else:
        row("last drain", f"{_age_str(last_age)} ago",
            "green" if last_age < 7200 else ("orange3" if last_age < 86400 else "red"))
    if qn:
        oc = "green" if oldest < 3600 else ("orange3" if oldest < 21600 else "red")
        row("oldest wait", _age_str(oldest), oc)
    else:
        row("oldest wait", "-", "grey50")
    row("errors", str(errors), "green" if errors == 0 else "red")
    row("invoker", inv_label, inv_color)

    t.append("\nBRAIN\n", style="bold yellow")
    t.append(f"  {portable_total}", style="bold magenta")
    t.append(" Codex-readable memories\n  ", style="grey50")
    readable = tuple(
        (item.name or item.scope, item.entities)
        for item in vaults
        if item.readable
    )
    for index, (name, count) in enumerate(readable[:2]):
        if index:
            t.append(" \u00b7 ", style="grey50")
        t.append(
            f"{name[:18]} {count}",
            style="yellow3" if index == 0 else "cyan",
        )
    if active_version:
        t.append(f"\n  active {active_version[:20]}", style="grey50")
    t.append("\n")
    return t


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
    if _invoker()[0] == "none":
        suggestions.append(
            ("no healthy enrichment CLI", "start a container or install an agent CLI")
        )
    queue = _read_queue()
    if len(queue) > 25:
        suggestions.append((f"queue depth high ({len(queue)})", "switch to Sessions to inspect"))
    if LINT.exists():
        body = LINT.read_text()
        if "fix:" in body:
            suggestions.append(("lint findings open", "switch to Health"))
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


class VaultEditScreen(ModalScreen[tuple[str, str] | None]):
    """Compact add/rename dialog for a user-defined vault."""

    BINDINGS = [Binding("escape", "cancel", "cancel")]

    def __init__(
        self,
        title: str,
        *,
        name: str = "",
        hint: str = "",
        include_hint: bool = True,
    ) -> None:
        super().__init__()
        self._title = title
        self._name = name
        self._hint = hint
        self._include_hint = include_hint

    def compose(self) -> ComposeResult:
        children = [
            Label(f"  {self._title}  ", id="modal-title"),
            Input(value=self._name, placeholder="Vault name", id="vault-name-input"),
        ]
        if self._include_hint:
            children.append(
                Input(
                    value=self._hint,
                    placeholder="Optional routing hint",
                    id="vault-hint-input",
                )
            )
        children.extend(
            [
                Button("Save", id="vault-save", variant="primary"),
                Button("Cancel", id="vault-cancel"),
                Label("  [dim]esc cancels[/dim]  ", id="modal-foot"),
            ]
        )
        yield Container(*children, id="modal-box")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "vault-cancel":
            self.dismiss(None)
            return
        if event.button.id != "vault-save":
            return
        name = self.query_one("#vault-name-input", Input).value.strip()
        if not name:
            self.notify("name is required", severity="error")
            return
        hint = ""
        if self._include_hint:
            hint = self.query_one("#vault-hint-input", Input).value.strip()
        self.dismiss((name, hint))

    def action_cancel(self) -> None:
        self.dismiss(None)


class VaultSetupScreen(ModalScreen[tuple[str, str] | None]):
    """First-run naming for the two protected portable roles."""

    BINDINGS = [Binding("escape", "cancel", "cancel")]

    def compose(self) -> ComposeResult:
        yield Container(
            Label("  Name your portable vaults  ", id="modal-title"),
            Label("These two vaults stay available for reusable agent context."),
            Input(value="Guidance", id="setup-guidance", placeholder="First vault name"),
            Input(value="Solutions", id="setup-solutions", placeholder="Second vault name"),
            Button("Create vaults", id="setup-save", variant="primary"),
            Button("Cancel", id="setup-cancel"),
            Label("  [dim]Names can be changed later[/dim]  ", id="modal-foot"),
            id="modal-box",
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "setup-cancel":
            self.dismiss(None)
            return
        if event.button.id != "setup-save":
            return
        first = self.query_one("#setup-guidance", Input).value.strip()
        second = self.query_one("#setup-solutions", Input).value.strip()
        if not first or not second:
            self.notify("both names are required", severity="error")
            return
        self.dismiss((first, second))

    def action_cancel(self) -> None:
        self.dismiss(None)


class ReviewDecisionScreen(ModalScreen[str | None]):
    """Explicit vault selection for one already-reviewed item."""

    BINDINGS = [Binding("escape", "cancel", "cancel"), Binding("q", "cancel", "cancel")]

    def __init__(self, vaults) -> None:
        super().__init__()
        self._vaults = tuple(vaults)
        self._routes = {
            f"route-{index}": vault.scope
            for index, vault in enumerate(self._vaults)
        }

    def compose(self) -> ComposeResult:
        children = [
            Label("  Resolve sealed review  ", id="modal-title"),
            Label("Choose the destination deliberately. The decision is integrity-signed."),
        ]
        children.extend(
            Button(
                f"{vault.name or vault.scope} · {vault.access}",
                id=f"route-{index}",
                variant="primary" if index == 0 else "default",
            )
            for index, vault in enumerate(self._vaults)
            if vault.state == "active"
        )
        children.append(Label("  [dim]esc cancels[/dim]  ", id="modal-foot"))
        yield Container(*children, id="modal-box")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        route = self._routes.get(event.button.id or "")
        if route is not None:
            self.dismiss(route)

    def action_cancel(self) -> None:
        self.dismiss(None)


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
        self._vault_counts: dict[str, int] = {}
        self._refresh_stats()
        self.set_interval(0.4, self._animate)
        self.set_interval(10.0, self._refresh_stats)

    def _refresh_stats(self) -> None:
        self._agents = _wired_agents(Path.home())
        try:
            summaries = _vault_catalog().summaries()
        except Exception:
            summaries = ()
        self._vault_counts = {
            (item.name or item.scope): item.entities
            for item in summaries
            if item.readable
        }
        self._total = sum(self._vault_counts.values())
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
        specs = {
            1: ("claude", "claude ", "green"),
            3: ("cursor", "cursor ", "cyan"),
            5: ("codex", "codex  ", "bright_green"),
        }
        item = specs.get(idx)
        spec = None
        if item is not None and item[0] in self._agents:
            name, label, pulse_style = item
            spec = (label, _agent_style(name), pulse_style)
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
        out.append(f"{minds} mind{'s' if minds != 1 else ''} \u00b7 {self._total} memories  ", style="grey50")
        for index, (name, count) in enumerate(tuple(self._vault_counts.items())[:2]):
            if index:
                out.append(" / ", style="grey50")
            out.append(f"{name[:18]} {count}", style="yellow3" if index == 0 else "cyan")
        return out


# ---- the App ------------------------------------------------------------

class AgamApp(App):
    """Keyboard-first operator console for Agam's vaults and queues."""

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
    #vault-layout { height: 1fr; }
    #vault-left {
        width: 40;
        min-width: 36;
        border-right: solid #293042;
        padding-right: 1;
    }
    #vault-main { width: 1fr; padding-left: 1; }
    #vault-filter { margin-bottom: 1; }
    #vault-hint, #review-hint { height: 2; color: #7a7a8a; }
    #vault-detail {
        height: 7;
        border-top: solid #293042;
        padding: 1 2 0 2;
        color: #b8bec9;
    }
    DataTable { background: #080b14; }
    DataTable > .datatable--header { background: #0d1120; color: #e8a849; }
    DataTable > .datatable--cursor { background: #2a2310; }
    DataTable:focus { border: tall #e8a849; }
    Input:focus { border: tall #5da9e9; }
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
        Binding("2", "jump('vaults')", "vaults"),
        Binding("3", "jump('sessions')", "sessions"),
        Binding("4", "jump('reviews')", "reviews"),
        Binding("5", "jump('worklog')", "worklog"),
        Binding("6", "jump('activity')", "activity"),
        Binding("7", "jump('health')", "health"),
        Binding("d", "drain", "drain queue"),
        Binding("c", "start_container", "start container"),
        Binding("enter", "row_detail", "drill"),
        Binding("s", "sync_row", "sync row"),
        Binding("h", "retry_review", "retry review"),
        Binding("x", "resolve_review", "resolve review"),
        Binding("P", "publish_reviews", "publish vaults"),
        Binding("D", "archive_session", "archive session"),
        Binding("n", "add_vault", "new vault"),
        Binding("e", "rename_vault", "rename vault"),
        Binding("A", "archive_vault", "archive/restore vault"),
        Binding("w", "toggle_vault_access", "toggle Codex access"),
        Binding("a", "apply_fix", "apply fix"),
    ]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield BrainBar(id="brain-bar")
        with TabbedContent(initial="overview", id="tabs"):
            with TabPane("Overview", id="overview"):
                yield ScrollableContainer(
                    Static(render_overview(), id="overview-stats"),
                    Label("next actions:", classes="pane-static"),
                    Static(render_next_actions(), id="overview-next"),
                )
            with TabPane("Vaults", id="vaults"):
                with Horizontal(id="vault-layout"):
                    with Vertical(id="vault-left"):
                        yield Static(
                            "VAULTS  [dim]Enter select · n add · e rename · A archive/restore · w Codex access[/dim]",
                            id="vault-hint",
                        )
                        yield DataTable(
                            id="vault-rail", cursor_type="row", zebra_stripes=True
                        )
                    with Vertical(id="vault-main"):
                        yield Input(
                            placeholder="filter selected vault...",
                            id="vault-filter",
                        )
                        yield DataTable(
                            id="vault-table", cursor_type="row", zebra_stripes=True
                        )
                        yield Static(
                            "Select a row to inspect its portable context.",
                            id="vault-detail",
                        )
            with TabPane("Sessions", id="sessions"):
                yield DataTable(id="queue-table", cursor_type="row", zebra_stripes=True)
            with TabPane("Reviews", id="reviews"):
                yield Static(
                    "SEALED REVIEW  [dim]Enter reveals · h Haiku retry · x resolve · P publish[/dim]",
                    id="review-hint",
                )
                yield DataTable(id="review-table", cursor_type="row", zebra_stripes=True)
            with TabPane("Worklog", id="worklog"):
                yield ScrollableContainer(Static(id="worklog-static"))
            with TabPane("Activity", id="activity"):
                yield Static(render_activity(), id="activity-static", classes="pane-static")
            with TabPane("Health", id="health"):
                yield Static(render_lint(), id="lint-static", classes="pane-static")
        yield Footer()

    def on_mount(self) -> None:
        self.title = "agam"
        self.sub_title = "vault operator"
        self._selected_vault = ""
        self._vault_summaries = ()
        self._vault_entities = ()
        self._review_items = ()
        self._populate_queue()
        self._populate_vaults()
        self._populate_reviews()
        self._populate_worklog()
        self.set_interval(30.0, self._auto_refresh)
        if not REGISTRY_PATH.exists():
            self.call_after_refresh(
                lambda: self.push_screen(VaultSetupScreen(), self._on_vault_setup)
            )

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
        self._populate_vaults(
            self.query_one("#vault-filter", Input).value.strip()
        )
        self._populate_reviews()
        self._populate_worklog()

    def _auto_refresh(self) -> None:
        self._refresh()

    def action_manual_refresh(self) -> None:
        self._refresh()
        self.notify("refreshed", timeout=1)

    def action_toggle_help(self) -> None:
        self.notify(
            "q quit  r refresh  1..7 views  / filter  enter open/select\n"
            "vaults: n add · e rename · A archive/restore · w Codex access\n"
            "sessions: s sync one · d drain all · D archive   reviews: h retry · x resolve · P publish",
            severity="information", timeout=8,
        )

    def _selected_vault_summary(self):
        for summary in self._vault_summaries:
            if summary.scope == self._selected_vault:
                return summary
        return None

    def _on_vault_setup(self, names: tuple[str, str] | None) -> None:
        if names is None:
            return
        try:
            from agam.vault_registry import initialize_registry

            initialize_registry(
                REGISTRY_PATH,
                guidance_name=names[0],
                solutions_name=names[1],
                agents=("claude", "cursor", "codex"),
            )
        except Exception:
            self.notify("vault setup failed", severity="error")
            return
        self._populate_vaults()
        self.notify("portable vaults created", timeout=3)

    def action_add_vault(self) -> None:
        if self._active_tab() != "vaults":
            return
        self.push_screen(
            VaultEditScreen("Add custom vault"), self._on_add_vault
        )

    def _on_add_vault(self, value: tuple[str, str] | None) -> None:
        if value is None:
            return
        try:
            from agam.vault_registry import add_vault

            record = add_vault(REGISTRY_PATH, name=value[0], routing_hint=value[1])
        except Exception:
            self.notify("vault add failed", severity="error")
            return
        self._selected_vault = record.id
        self._populate_vaults()
        self.notify(f"added {record.name}; restricted by default", timeout=4)

    def action_rename_vault(self) -> None:
        if self._active_tab() != "vaults":
            return
        selected = self._selected_vault_summary()
        if selected is None:
            return
        self.push_screen(
            VaultEditScreen(
                "Rename vault",
                name=selected.name or selected.scope,
                include_hint=False,
            ),
            self._on_rename_vault,
        )

    def _on_rename_vault(self, value: tuple[str, str] | None) -> None:
        if value is None:
            return
        try:
            from agam.vault_registry import rename_vault

            record = rename_vault(REGISTRY_PATH, self._selected_vault, value[0])
        except Exception:
            self.notify("vault rename failed", severity="error")
            return
        self._populate_vaults()
        self.notify(f"renamed vault to {record.name}", timeout=3)

    def action_archive_vault(self) -> None:
        if self._active_tab() != "vaults":
            return
        selected = self._selected_vault_summary()
        if selected is None:
            return
        if selected.state == "archived":
            try:
                from agam.vault_registry import restore_vault

                record = restore_vault(REGISTRY_PATH, selected.scope)
            except Exception:
                self.notify("vault restore failed", severity="error")
                return
            self._populate_vaults()
            self.notify(f"restored {record.name}", timeout=3)
            return
        if selected.role != "custom":
            self.notify("protected portable vaults cannot be archived", severity="warning")
            return
        now = time.monotonic()
        pending = getattr(self, "_confirm_vault_archive", None)
        if pending != selected.scope or now > getattr(
            self, "_confirm_vault_archive_until", 0.0
        ):
            self._confirm_vault_archive = selected.scope
            self._confirm_vault_archive_until = now + 5.0
            self.notify(
                f"archive {selected.name}? data is retained; press A again",
                severity="warning",
                timeout=5,
            )
            return
        try:
            from agam.vault_registry import archive_vault

            record = archive_vault(REGISTRY_PATH, selected.scope)
        except Exception:
            self.notify("vault archive failed", severity="error")
            return
        self._confirm_vault_archive = None
        self._populate_vaults()
        self.notify(f"archived {record.name}; all data retained", timeout=4)

    def action_toggle_vault_access(self) -> None:
        if self._active_tab() != "vaults":
            return
        selected = self._selected_vault_summary()
        if selected is None or selected.state != "active":
            return
        try:
            from agam.vault_registry import load_registry, set_agent_access

            registry = load_registry(REGISTRY_PATH)
            current = list(registry.agents.get("codex", ()))
            if selected.scope in current:
                current.remove(selected.scope)
                enabled = False
            else:
                current.append(selected.scope)
                enabled = True
            set_agent_access(REGISTRY_PATH, "codex", current)
        except Exception:
            self.notify("access update failed", severity="error")
            return
        try:
            from agam.agents import CodexAgent

            CodexAgent().install(HOME)
            wiring = "Codex wiring refreshed"
        except Exception:
            wiring = "run `agam wire codex` to refresh wiring"
        self._populate_vaults()
        state = "enabled" if enabled else "disabled"
        self.notify(f"Codex access {state}; {wiring}", timeout=5)

    def action_drain(self) -> None:
        if self._active_tab() != "sessions":
            return
        entries = _read_queue()
        now = time.monotonic()
        if now > getattr(self, "_confirm_drain_until", 0.0):
            count = len(entries)
            self._confirm_drain_until = now + 5.0
            self.notify(
                f"drain all {count} session rows? press d again within 5s",
                severity="warning",
                timeout=5,
            )
            return
        self._confirm_drain_until = 0.0
        try:
            launched = 0
            if any(item.get("_queue_source") == "legacy" for item in entries):
                if WATCHDOG_MONITOR is None:
                    raise RuntimeError("watchdog_monitor_unavailable")
                subprocess.Popen(
                    [str(WATCHDOG_MONITOR), "sync", "--all"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                launched += 1
            if any(item.get("_queue_source") == "file" for item in entries):
                if WATCHDOG_SHELL is None:
                    raise RuntimeError("watchdog_shell_unavailable")
                environment = os.environ.copy()
                environment.update(
                    {"AGAM_HOME": str(DATA_HOME), "AGAM_DATA_HOME": str(DATA_HOME)}
                )
                environment.pop("AGAM_QUEUE_FILE", None)
                subprocess.Popen(
                    ["/bin/bash", str(WATCHDOG_SHELL)],
                    env=environment,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                launched += 1
            self.notify(
                "drain started in background" if launched else "session queue is empty",
                timeout=3,
            )
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
        if tab == "sessions":
            self._queue_row_detail()
        elif tab == "vaults":
            if getattr(self.focused, "id", None) == "vault-rail":
                self._select_vault_row()
            else:
                self._vault_row_detail()
        elif tab == "reviews":
            self._review_row_detail()

    def action_sync_row(self) -> None:
        if self._active_tab() != "sessions":
            return
        idx = self._queue_cursor_idx()
        if idx is None:
            return
        rows = _queue_state(_read_queue())
        if idx >= len(rows):
            return
        self.notify(f"force-syncing queue row {idx}...", timeout=4)
        try:
            self._start_queue_sync(rows[idx], idx)
        except Exception as e:
            self.notify(f"sync failed: {e}", severity="error")

    def _start_queue_sync(self, entry: dict, display_index: int) -> None:
        if entry.get("_queue_source") == "file":
            if WATCHDOG_SHELL is None:
                raise RuntimeError("watchdog_shell_unavailable")
            selected = Path(entry["_queue_file"]).resolve(strict=False)
            queue_root = NEW_QUEUE_DIR.resolve(strict=False)
            if selected.parent != queue_root or selected.suffix != ".json":
                raise ValueError("invalid_queue_entry")
            environment = os.environ.copy()
            environment.update(
                {
                    "AGAM_HOME": str(DATA_HOME),
                    "AGAM_DATA_HOME": str(DATA_HOME),
                    "AGAM_QUEUE_FILE": selected.name,
                }
            )
            subprocess.Popen(
                ["/bin/bash", str(WATCHDOG_SHELL)],
                env=environment,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return
        if WATCHDOG_MONITOR is None:
            raise RuntimeError("watchdog_monitor_unavailable")
        source_index = entry.get("_queue_index", display_index)
        subprocess.Popen(
            [str(WATCHDOG_MONITOR), "sync", str(source_index)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def action_archive_session(self) -> None:
        if self._active_tab() != "sessions":
            return
        idx = self._queue_cursor_idx()
        rows = _queue_state(_read_queue())
        if idx is None or idx >= len(rows):
            return
        now = time.monotonic()
        pending = getattr(self, "_confirm_archive", None)
        if not pending or pending[0] != idx or now > pending[1]:
            self._confirm_archive = (idx, now + 5.0)
            self.notify(
                f"archive session row {idx}? press D again within 5s",
                severity="warning",
                timeout=5,
            )
            return
        self._confirm_archive = None
        try:
            self._archive_queue_entry(rows[idx])
        except Exception:
            self.notify("archive failed", severity="error")
            return
        self._populate_queue()
        self.notify(f"archived session row {idx}", timeout=3)

    def action_retry_review(self) -> None:
        if self._active_tab() != "reviews":
            return
        opaque_id = self._selected_review_id()
        if opaque_id is None or getattr(self, "_review_busy", False):
            return
        self._review_busy = True
        self.notify("retrying selected item with local Claude CLI / Haiku…", timeout=8)
        self.run_worker(
            lambda: self._retry_review_worker(opaque_id),
            thread=True,
            exclusive=True,
            group="sealed-review",
        )

    def action_resolve_review(self) -> None:
        if self._active_tab() != "reviews" or self._selected_review_id() is None:
            return
        try:
            vaults = tuple(
                item for item in _vault_catalog().summaries() if item.state == "active"
            )
        except Exception:
            self.notify("vault registry unavailable", severity="error")
            return
        self.push_screen(ReviewDecisionScreen(vaults), self._on_review_route)

    def action_publish_reviews(self) -> None:
        if self._active_tab() != "reviews" or getattr(self, "_review_queue_instance", None) is None:
            return
        now = time.monotonic()
        if now > getattr(self, "_confirm_publish_until", 0.0):
            unresolved = len(self._review_items)
            self._confirm_publish_until = now + 5.0
            self.notify(
                f"publish a new immutable vault version? {unresolved} unresolved rows stay omitted; press P again",
                severity="warning",
                timeout=5,
            )
            return
        self._confirm_publish_until = 0.0
        if getattr(self, "_review_busy", False):
            return
        self._review_busy = True
        self.notify("building and validating new vault version…", timeout=8)
        self.run_worker(
            self._publish_review_worker,
            thread=True,
            exclusive=True,
            group="sealed-review",
        )

    def _on_review_route(self, route: str | None) -> None:
        opaque_id = self._selected_review_id()
        if route is None or opaque_id is None or getattr(self, "_review_busy", False):
            return
        self._review_busy = True
        self.notify("sealing review decision…", timeout=6)
        self.run_worker(
            lambda: self._resolve_review_worker(opaque_id, route),
            thread=True,
            exclusive=True,
            group="sealed-review",
        )

    def _review_finished(self, message: str, *, error: bool = False) -> None:
        self._review_busy = False
        self._populate_reviews()
        self.notify(
            message,
            severity="error" if error else "information",
            timeout=5,
        )

    def _retry_review_worker(self, opaque_id: str) -> None:
        try:
            from agam.knowledge_classifier import ClaudeCliRunner

            runner_dir = DATA_HOME / "knowledge" / "sealed" / "staging" / ".classifier-runner"
            runner_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            runner = ClaudeCliRunner(working_directory=runner_dir, model="haiku")
            summary = self._review_queue_instance.retry(opaque_id, runner)
        except Exception:
            self.call_from_thread(
                self._review_finished, "Haiku retry failed; item remains sealed", error=True
            )
            return
        if summary.failed:
            self.call_from_thread(
                self._review_finished,
                "Haiku retry failed; item remains sealed",
                error=True,
            )
        elif summary.review:
            self.call_from_thread(
                self._review_finished,
                "Haiku remains uncertain; review item kept",
            )
        else:
            self.call_from_thread(self._review_finished, "Haiku resolved the review item")

    def _resolve_review_worker(
        self, opaque_id: str, route: str
    ) -> None:
        try:
            self._review_queue_instance.resolve(opaque_id, vault_id=route)
        except Exception:
            self.call_from_thread(
                self._review_finished,
                "review decision failed integrity checks",
                error=True,
            )
            return
        self.call_from_thread(self._review_finished, "review decision sealed")

    def _publish_review_worker(self) -> None:
        try:
            from agam import installer

            schema = installer._find_resource("knowledge/graph-schema.sql")
            summary = self._review_queue_instance.publish(SCOPES_ROOT, schema)
        except Exception:
            self.call_from_thread(
                self._review_finished,
                "vault publication failed; active version was not changed",
                error=True,
            )
            return
        self.call_from_thread(self._populate_vaults)
        self.call_from_thread(
            self._review_finished, f"published vault version {summary.version}"
        )

    def _archive_queue_entry(self, entry: dict) -> None:
        payload = {key: value for key, value in entry.items() if not key.startswith("_")}
        if entry.get("_queue_source") == "file":
            selected = Path(entry["_queue_file"]).resolve(strict=True)
            root = NEW_QUEUE_DIR.resolve(strict=True)
            if selected.parent != root or selected.suffix != ".json":
                raise ValueError("invalid_queue_entry")
            ARCHIVE_PATH.parent.mkdir(parents=True, exist_ok=True)
            with ARCHIVE_PATH.open("a", encoding="utf-8") as archive:
                archive.write(json.dumps(payload, sort_keys=True) + "\n")
            selected.unlink()
            return

        index = entry.get("_queue_index")
        rows = _read_jsonl(QUEUE_PATH)
        if not isinstance(index, int) or not 0 <= index < len(rows):
            raise ValueError("invalid_queue_entry")
        ARCHIVE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with ARCHIVE_PATH.open("a", encoding="utf-8") as archive:
            archive.write(json.dumps(payload, sort_keys=True) + "\n")
        del rows[index]
        QUEUE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=QUEUE_PATH.parent, delete=False
        ) as handle:
            for row in rows:
                handle.write(json.dumps(row, sort_keys=True) + "\n")
            temp_path = Path(handle.name)
        os.replace(temp_path, QUEUE_PATH)

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
        if self._active_tab() != "health":
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

    def _select_vault_row(self) -> None:
        try:
            table = self.query_one("#vault-rail", DataTable)
            if table.cursor_row is None:
                return
            summary = self._vault_summaries[table.cursor_row]
        except (IndexError, AttributeError):
            return
        self._selected_vault = summary.scope
        self.query_one("#vault-filter", Input).value = ""
        self._populate_vault_entities()
        if summary.readable:
            self.query_one("#vault-table", DataTable).focus()
        self.notify(f"selected {summary.name or summary.scope}", timeout=2)

    def _vault_row_detail(self) -> None:
        try:
            table = self.query_one("#vault-table", DataTable)
            if table.cursor_row is None:
                return
            entity = self._vault_entities[table.cursor_row]
        except (IndexError, AttributeError):
            return
        body = Text()
        body.append(entity.name, style="bold yellow")
        body.append(f"  [{entity.kind}]\n", style="grey50")
        body.append(f"vault: {self._selected_vault}\n\n", style="cyan")
        body.append(entity.description, style="white")
        body.append("\n\nread-only · verified active store", style="green")
        self.push_screen(DetailScreen(entity.name, body))

    def _update_vault_detail(self, index: int) -> None:
        if not 0 <= index < len(self._vault_entities):
            return
        entity = self._vault_entities[index]
        detail = Text()
        detail.append(f"{self._selected_vault.upper()}  ", style="bold cyan")
        detail.append(entity.name, style="bold yellow")
        detail.append(f"  [{entity.kind}]\n", style="grey50")
        detail.append(
            re.sub(
                r"^\[VAULT:vault_[0-9a-f]{24}\]\s*", "", entity.description
            ),
            style="white",
        )
        self.query_one("#vault-detail", Static).update(detail)

    def _selected_review_id(self) -> str | None:
        try:
            table = self.query_one("#review-table", DataTable)
            if table.cursor_row is None:
                return None
            return self._review_items[table.cursor_row].opaque_id
        except (IndexError, AttributeError):
            return None

    def _review_row_detail(self) -> None:
        opaque_id = self._selected_review_id()
        if opaque_id is None or getattr(self, "_review_queue_instance", None) is None:
            return
        try:
            detail = self._review_queue_instance.detail(opaque_id)
        except Exception:
            self.notify("review detail failed integrity checks", severity="error")
            return
        body = Text()
        body.append(detail.name, style="bold yellow")
        body.append(f"  [{detail.kind}]\n", style="grey50")
        body.append(f"opaque id: {detail.opaque_id}\n", style="grey50")
        body.append(f"reason: {detail.reason_code}\n\n", style="orange3")
        body.append(detail.description, style="white")
        body.append("\n\nh = retry with Haiku · x = resolve · esc = close", style="green")
        self.push_screen(DetailScreen("sealed review", body))

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

    def _populate_vaults(self, query: str = "") -> None:
        rail = self.query_one("#vault-rail", DataTable)
        rail.clear(columns=True)
        rail.add_columns("vault", "access", "state", "items")
        try:
            catalog = _vault_catalog()
            summaries = catalog.summaries()
        except Exception:
            self._vault_summaries = ()
            self._vault_entities = ()
            rail.add_row("unavailable", "—", "error", "—")
            table = self.query_one("#vault-table", DataTable)
            table.clear(columns=True)
            table.add_columns("name", "type", "description", "updated")
            table.add_row("No verified active manifest", "", "run agam doctor", "")
            return
        self._vault_catalog_instance = catalog
        self._vault_summaries = summaries
        for item in summaries:
            if item.state == "archived":
                state = Text("archived", style="grey50")
                label_style = "grey50"
            elif item.readable:
                state = Text("readable", style="green")
                label_style = "yellow" if item.role == "guidance" else "cyan"
            else:
                state = Text("restricted", style="red")
                label_style = "grey50"
            rail.add_row(
                Text(item.name or item.scope, style=label_style),
                item.access,
                state,
                str(item.entities),
            )
        if summaries:
            self.query_one("#vault-hint", Static).update(
                f"VAULTS  [dim]active {summaries[0].version[:18]} · Enter selects[/dim]"
            )
        available = {item.scope for item in summaries}
        if self._selected_vault not in available:
            self._selected_vault = summaries[0].scope if summaries else ""
        self._populate_vault_entities(query)

    def _populate_vault_entities(self, query: str = "") -> None:
        table = self.query_one("#vault-table", DataTable)
        table.clear(columns=True)
        table.add_columns("name", "type", "description", "updated")
        catalog = getattr(self, "_vault_catalog_instance", None)
        if catalog is None:
            return
        try:
            rows = catalog.entities(self._selected_vault, query=query)
        except Exception:
            self._vault_entities = ()
            table.add_row("Vault unavailable", "", "verification failed closed", "")
            return
        self._vault_entities = rows
        if not rows:
            selected = self._selected_vault_summary()
            name = selected.name if selected is not None else self._selected_vault
            table.add_row("No matches", "", f"{name} is empty for this filter", "")
            self.query_one("#vault-detail", Static).update(
                f"No {name} entities match this filter."
            )
            return
        for entity in rows:
            table.add_row(
                Text(entity.name[:36], style="yellow"),
                Text(entity.kind, style="grey50"),
                re.sub(
                    r"^\[VAULT:vault_[0-9a-f]{24}\]\s*", "", entity.description
                )[:72],
                (entity.updated or "?").split("T")[0],
            )
        self._update_vault_detail(0)

    def _populate_reviews(self) -> None:
        table = self.query_one("#review-table", DataTable)
        table.clear(columns=True)
        table.add_columns("idx", "state", "opaque id", "reason", "classified")
        try:
            queue = getattr(self, "_review_queue_instance", None) or _review_queue()
            if queue is None:
                raise RuntimeError("review_queue_unavailable")
            items = queue.items()
        except Exception:
            self._review_queue_instance = None
            self._review_items = ()
            table.add_row("—", "unavailable", "—", "no sealed review run", "—")
            return
        self._review_queue_instance = queue
        self._review_items = items
        if not items:
            table.add_row("—", "clear", "—", "no unresolved reviews", "—")
            return
        for index, item in enumerate(items):
            state = "failed" if item.failed else "review"
            table.add_row(
                str(index),
                Text(state, style="red" if item.failed else "orange3"),
                item.opaque_id,
                item.reason_code,
                item.classified_at[:19],
            )

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
            agent_color = _agent_style(agent)
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
            agent_color = _agent_style(agent)
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

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id == "vault-table":
            row = event.data_table.cursor_row
            if row is not None:
                self._update_vault_detail(row)

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "vault-filter":
            self._populate_vault_entities(event.value.strip())


def main() -> int:
    AgamApp().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
