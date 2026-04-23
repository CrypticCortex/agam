#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11"
# ///
"""
Session pattern analyzer -- reads work-log.md and identifies patterns,
recurring topics, project activity, and potential blind spots.

Usage:
    session_patterns.py                   Full analysis
    session_patterns.py projects          Project activity heatmap
    session_patterns.py topics            Topic frequency analysis
    session_patterns.py streaks           Active/idle streaks
    session_patterns.py gaps              Detect goal stalls (14+ days inactive)

Environment variables:
    AGAM_WORK_LOG      Path to work-log markdown file
                       (default: ~/.claude/work-log.md)
    AGAM_HOME          Directory holding AGAM.md / THISAI.md / MUGAM.md
                       (default: ~/.claude/agam)
"""

import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path


WORKLOG = Path(
    os.environ.get("AGAM_WORK_LOG", os.path.expanduser("~/.claude/work-log.md"))
)
AGAM_HOME = Path(
    os.environ.get("AGAM_HOME", os.path.expanduser("~/.claude/agam"))
)
GOALS = AGAM_HOME / "THISAI.md"


def parse_worklog():
    if not WORKLOG.exists():
        print("[FAIL] No work-log.md found")
        sys.exit(1)

    text = WORKLOG.read_text()
    entries = []
    current_date = None
    current_project = None
    current_content = []

    for line in text.splitlines():
        # Date headers: ## 2026-03-18 or ## 2026-03-18 | Project | HH:MM
        date_match = re.match(r"^##\s+(\d{4}-\d{2}-\d{2})", line)
        if date_match:
            # Save previous entry
            if current_date and current_content:
                entries.append({
                    "date": current_date,
                    "project": current_project,
                    "content": "\n".join(current_content),
                })
                current_content = []

            current_date = date_match.group(1)
            parts = line.split("|")
            current_project = parts[1].strip() if len(parts) > 1 else "unknown"
            continue

        # Time sub-headers: ### HH:MM | Project
        time_match = re.match(r"^###\s+\d{2}:\d{2}\s*\|\s*(.+)", line)
        if time_match:
            if current_content:
                entries.append({
                    "date": current_date,
                    "project": current_project,
                    "content": "\n".join(current_content),
                })
                current_content = []
            current_project = time_match.group(1).strip()
            continue

        if current_date:
            current_content.append(line)

    # Don't forget last entry
    if current_date and current_content:
        entries.append({
            "date": current_date,
            "project": current_project,
            "content": "\n".join(current_content),
        })

    return entries


def analyze_projects(entries):
    project_dates = defaultdict(set)
    project_words = defaultdict(int)

    for e in entries:
        p = e["project"] or "unknown"
        project_dates[p].add(e["date"])
        project_words[p] += len(e["content"].split())

    print("PROJECT ACTIVITY")
    print("=" * 50)
    for p, dates in sorted(project_dates.items(), key=lambda x: -len(x[1])):
        sorted_dates = sorted(dates)
        first = sorted_dates[0]
        last = sorted_dates[-1]
        print(f"\n  {p}")
        print(f"    Sessions: {len(dates)}")
        print(f"    Words logged: {project_words[p]:,}")
        print(f"    Active: {first} to {last}")


def analyze_topics(entries):
    # Extract common technical terms
    all_text = " ".join(e["content"].lower() for e in entries)
    # Find 2-3 word phrases that appear frequently
    words = re.findall(r"\b[a-z][a-z-]+\b", all_text)
    word_counts = Counter(words)

    # Filter out common English words
    stopwords = {
        "the", "and", "for", "that", "this", "with", "from", "was", "were",
        "has", "have", "had", "been", "not", "but", "are", "can", "will",
        "just", "more", "also", "into", "some", "than", "then", "when",
        "what", "which", "about", "would", "there", "their", "them",
        "other", "could", "after", "before", "should", "where", "those",
        "these", "being", "each", "made", "like", "between", "does",
        "most", "only", "over", "such", "make", "its", "way", "may",
        "said", "did", "get", "got", "very", "still", "need", "how",
    }

    technical = {
        w: c for w, c in word_counts.items()
        if c >= 2 and w not in stopwords and len(w) > 3
    }

    print("RECURRING TOPICS")
    print("=" * 50)
    for word, count in sorted(technical.items(), key=lambda x: -x[1])[:30]:
        bar = "*" * min(count, 30)
        print(f"  {word:20s} {count:3d} {bar}")


def analyze_streaks(entries):
    if not entries:
        print("No entries.")
        return

    dates = sorted(set(e["date"] for e in entries))
    date_objs = [datetime.strptime(d, "%Y-%m-%d") for d in dates]

    print("ACTIVITY STREAKS")
    print("=" * 50)
    print(f"  Total active days: {len(dates)}")
    print(f"  First entry: {dates[0]}")
    print(f"  Last entry: {dates[-1]}")

    if len(date_objs) >= 2:
        span = (date_objs[-1] - date_objs[0]).days + 1
        pct = len(dates) / span * 100 if span > 0 else 0
        print(f"  Span: {span} days ({pct:.0f}% active)")

    # Find longest streak
    if len(date_objs) >= 2:
        max_streak = 1
        current_streak = 1
        for i in range(1, len(date_objs)):
            if (date_objs[i] - date_objs[i-1]).days == 1:
                current_streak += 1
                max_streak = max(max_streak, current_streak)
            else:
                current_streak = 1
        print(f"  Longest streak: {max_streak} consecutive days")

    # Find longest gap
    if len(date_objs) >= 2:
        max_gap = 0
        gap_start = ""
        for i in range(1, len(date_objs)):
            gap = (date_objs[i] - date_objs[i-1]).days
            if gap > max_gap:
                max_gap = gap
                gap_start = dates[i-1]
        if max_gap > 1:
            print(f"  Longest gap: {max_gap} days (after {gap_start})")


def detect_gaps(entries):
    """Cross-reference GOALS.md with work-log to find stalled goals."""
    print("GOAL ACTIVITY GAPS")
    print("=" * 50)

    if not GOALS.exists():
        print("  No THISAI.md found at", GOALS)
        return

    goals_text = GOALS.read_text()
    # Extract goal names (### headers under ## Active Goals in THISAI.md)
    goals = re.findall(r"^###\s+(.+)", goals_text, re.MULTILINE)

    if not goals:
        print("  No goals found in THISAI.md")
        return

    all_text = " ".join(e["content"].lower() + " " + (e["project"] or "").lower() for e in entries)
    recent_text = " ".join(
        e["content"].lower() + " " + (e["project"] or "").lower()
        for e in entries
        if e["date"] >= (datetime.now() - timedelta(days=14)).strftime("%Y-%m-%d")
    )

    for goal in goals:
        goal_lower = goal.lower()
        # Simple keyword presence check
        keywords = [w for w in re.findall(r"\b\w+\b", goal_lower) if len(w) > 3]
        in_recent = any(k in recent_text for k in keywords)
        in_all = any(k in all_text for k in keywords)

        if not in_recent and in_all:
            print(f"  [STALLED] {goal} -- appeared in older logs but not in last 14 days")
        elif not in_recent and not in_all:
            print(f"  [NO SIGNAL] {goal} -- never appeared in work logs")
        else:
            print(f"  [ACTIVE] {goal}")


def full_analysis():
    entries = parse_worklog()
    if not entries:
        print("No entries found in work-log.md")
        return

    print(f"Analyzed {len(entries)} work-log entries\n")
    analyze_projects(entries)
    print()
    analyze_streaks(entries)
    print()
    detect_gaps(entries)
    print()
    analyze_topics(entries)


def main():
    if len(sys.argv) < 2:
        full_analysis()
    elif sys.argv[1] == "projects":
        analyze_projects(parse_worklog())
    elif sys.argv[1] == "topics":
        analyze_topics(parse_worklog())
    elif sys.argv[1] == "streaks":
        analyze_streaks(parse_worklog())
    elif sys.argv[1] == "gaps":
        detect_gaps(parse_worklog())
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
