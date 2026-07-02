#!/usr/bin/env python3
"""Attesto MCP server (LVAP fork addition).

Source of truth + actions for the Attesto phone receptionist:
  - attesto_info:      speak-ready answers from the Attesto knowledge base
  - check_demo_slots:  free demo slots (simple local calendar)
  - schedule_demo:     book a demo (JSON store, survives restarts)
  - take_message:      callback note for the team (JSON store)

Design rule (Jerome): the agent answers from SOURCES, not model memory.
Newline-delimited JSON-RPC over stdio, matching src/mcp_servers/weather_mcp_server.py.

Env:
  ATTESTO_KB_PATH   knowledge base markdown (default /app/config/attesto-kb.md)
  ATTESTO_DATA_DIR  bookings/messages store  (default /app/data/attesto)
  ATTESTO_TZ        IANA timezone            (default Europe/Amsterdam)
"""
from __future__ import annotations

import json
import os
import re
import sys
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

KB_PATH = os.getenv("ATTESTO_KB_PATH", "/app/config/attesto-kb.md")
DATA_DIR = os.getenv("ATTESTO_DATA_DIR", "/app/data/attesto")
TZ = ZoneInfo(os.getenv("ATTESTO_TZ", "Europe/Amsterdam"))

BUSINESS_START, BUSINESS_END, SLOT_MINUTES = 9, 17, 45


# ── KB ──────────────────────────────────────────────────────────────────────

def _load_kb_sections() -> dict:
    """Parse '## N. Title' sections from the KB markdown."""
    sections = {}
    try:
        text = open(KB_PATH, encoding="utf-8").read()
    except OSError:
        return sections
    for m in re.finditer(r"^## (\d+)\.\s*(.+?)$(.*?)(?=^## \d+\.|\Z)", text, re.M | re.S):
        sections[int(m.group(1))] = (m.group(2).strip(), m.group(3).strip())
    return sections


def _spoken(md: str, limit: int = 1400) -> str:
    """Markdown → speakable plain text."""
    t = re.sub(r"^>\s?", "", md, flags=re.M)          # blockquotes are the spoken scripts
    t = re.sub(r"\*\*([^*]*)\*\*|\*([^*]*)\*", lambda m: m.group(1) or m.group(2), t)
    t = re.sub(r"^\s*[-•]\s*", "", t, flags=re.M)
    t = re.sub(r"^#+.*$", "", t, flags=re.M)
    t = re.sub(r"`([^`]*)`", r"\1", t)
    t = re.sub(r"\n+", " ", t)
    t = re.sub(r"\s{2,}", " ", t).strip()
    return t[:limit]


TOPIC_SECTIONS = {
    "what_is": [2, 3],
    "pricing": [6],
    "getting_started": [7],
    "connectors": [5],
    "developers": [5],
    "use_cases": [4],
    "security_privacy": [8],
    "company": [1],
    "glossary": [11],
}


def attesto_info(topic: str) -> str:
    kb = _load_kb_sections()
    if not kb:
        return "I'm sorry, my knowledge base is unavailable right now. May I take your details for a callback?"
    nums = TOPIC_SECTIONS.get(topic) or TOPIC_SECTIONS["what_is"]
    parts = [_spoken(kb[n][1]) for n in nums if n in kb]
    return " ".join(parts)[:1600] or "I don't have that information. May I take your details so the right person calls you back?"


# ── Scheduling ──────────────────────────────────────────────────────────────

_WEEKDAYS = {
    "monday": 0, "maandag": 0, "tuesday": 1, "dinsdag": 1, "wednesday": 2,
    "woensdag": 2, "thursday": 3, "donderdag": 3, "friday": 4, "vrijdag": 4,
    "saturday": 5, "zaterdag": 5, "sunday": 6, "zondag": 6,
}


def _resolve_day(text: str, now: datetime):
    t = (text or "").strip().lower()
    if m := re.search(r"\d{4}-\d{2}-\d{2}", t):
        return datetime.strptime(m.group(0), "%Y-%m-%d").replace(tzinfo=TZ)
    if any(w in t for w in ("today", "vandaag")):
        return now
    if any(w in t for w in ("tomorrow", "morgen")) and "overmorgen" not in t:
        return now + timedelta(days=1)
    if "overmorgen" in t or "day after" in t:
        return now + timedelta(days=2)
    for name, wd in _WEEKDAYS.items():
        if name in t:
            ahead = (wd - now.weekday()) % 7 or 7
            return now + timedelta(days=ahead)
    return None


def _resolve_time(text: str):
    t = (text or "").strip().lower()
    if m := re.search(r"(\d{1,2})[:.](\d{2})", t):
        h, mn = int(m.group(1)), int(m.group(2))
    elif m := re.search(r"(\d{1,2})\s*(am|pm|uur|u\b|o'?clock)?", t):
        h, mn = int(m.group(1)), 0
    else:
        return None
    if "pm" in t and h < 12:
        h += 12
    if h < 8 and "am" not in t:  # "2 uur" on a phone means 14:00
        h += 12
    return (h, mn) if 0 <= h <= 23 and 0 <= mn <= 59 else None


def _store(name: str) -> list:
    path = os.path.join(DATA_DIR, name)
    try:
        return json.load(open(path, encoding="utf-8"))
    except (OSError, ValueError):
        return []


def _save(name: str, items: list) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    path = os.path.join(DATA_DIR, name)
    tmp = path + ".tmp"
    json.dump(items, open(tmp, "w", encoding="utf-8"), indent=1, ensure_ascii=False)
    os.replace(tmp, path)


def _spoken_slot(dt: datetime) -> str:
    return dt.strftime("%A %d %B at %H:%M")


def _free_slots(day: datetime, count: int = 3) -> list:
    """Free slots for the day, SPREAD across morning/midday/afternoon so the
    caller hears the real range (offering only 9:00-10:30 made afternoon
    availability look nonexistent — found by softphone self-test)."""
    now = datetime.now(TZ)
    booked = {b["slot"] for b in _store("bookings.json")}
    all_free, cur = [], day.replace(hour=BUSINESS_START, minute=0, second=0, microsecond=0)
    end = day.replace(hour=BUSINESS_END, minute=0, second=0, microsecond=0)
    while cur < end:
        if cur > now + timedelta(minutes=30) and cur.isoformat() not in booked and cur.weekday() < 5:
            all_free.append(cur)
        cur += timedelta(minutes=SLOT_MINUTES)
    if len(all_free) <= count:
        return all_free
    step = (len(all_free) - 1) / (count - 1)
    return [all_free[round(i * step)] for i in range(count)]


def check_demo_slots(day_text: str, time_text: str = "") -> str:
    now = datetime.now(TZ)
    day = _resolve_day(day_text, now) or (now + timedelta(days=1))
    if day.weekday() >= 5:
        day += timedelta(days=7 - day.weekday())

    # Caller asked about a specific time → answer about THAT slot first.
    hm = _resolve_time(time_text) if time_text else None
    if hm:
        want = day.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)
        booked = {b["slot"] for b in _store("bookings.json")}
        in_hours = BUSINESS_START <= want.hour < BUSINESS_END and want.weekday() < 5
        if in_hours and want > now and want.isoformat() not in booked:
            return f"Yes, {_spoken_slot(want)} is available. Shall I book it?"

    slots = _free_slots(day)
    if not slots:
        nxt = _free_slots(day + timedelta(days=1))
        if not nxt:
            return "I don't see open demo slots on that day. Shall I take your details so the team proposes a time?"
        return "That day is full. The next openings are " + ", ".join(_spoken_slot(s) for s in nxt) + ". Would one of those work?"
    prefix = "That exact time is not free. " if hm else ""
    return prefix + "Available demo slots: " + ", ".join(_spoken_slot(s) for s in slots) + ". Which one suits you?"


def schedule_demo(day_text: str, time_text: str, name: str, contact: str) -> str:
    now = datetime.now(TZ)
    day = _resolve_day(day_text, now)
    hm = _resolve_time(time_text or day_text)
    if not day or not hm:
        return "I could not work out that date and time. Could you give me the day and the time once more?"
    slot = day.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)
    if slot <= now:
        return "That time is in the past. Could you pick a moment later than now?"
    if slot.weekday() >= 5 or not (BUSINESS_START <= slot.hour < BUSINESS_END):
        return "Demos are scheduled on weekdays between nine and five. Could you pick a time in business hours?"
    bookings = _store("bookings.json")
    if any(b["slot"] == slot.isoformat() for b in bookings):
        return "That slot was just taken. " + check_demo_slots(day_text)
    if not (name or "").strip() or not (contact or "").strip():
        return "Almost there — I still need a name and an email or phone number to confirm the demo."
    bookings.append({
        "id": uuid.uuid4().hex[:8], "slot": slot.isoformat(), "name": name.strip(),
        "contact": contact.strip(), "created": now.isoformat(),
    })
    _save("bookings.json", bookings)
    return (f"Done. I have scheduled your demo for {_spoken_slot(slot)}. "
            f"You will receive a confirmation at {contact.strip()}.")


def take_message(name: str, contact: str, topic: str, category: str) -> str:
    if not (name or "").strip() or not (contact or "").strip():
        return "I still need a name and an email or phone number for the callback."
    msgs = _store("messages.json")
    msgs.append({
        "id": uuid.uuid4().hex[:8], "name": name.strip(), "contact": contact.strip(),
        "topic": (topic or "").strip(), "category": (category or "other").strip().lower(),
        "created": datetime.now(TZ).isoformat(),
    })
    _save("messages.json", msgs)
    return (f"Thank you {name.strip()}. I have noted your message and the right person "
            "will call you back within one business day.")


# ── JSON-RPC stdio loop ─────────────────────────────────────────────────────

TOOLS = [
    {"name": "attesto_info",
     "description": "Authoritative Attesto company information. ALWAYS use this instead of memory for details about the product, pricing, connectors, developers, security or use cases.",
     "inputSchema": {"type": "object", "properties": {"topic": {"type": "string", "enum": list(TOPIC_SECTIONS)}}, "required": ["topic"]}},
    {"name": "check_demo_slots",
     "description": "Check demo availability for a day. If the caller mentioned a specific time, ALWAYS pass it in 'time' — the tool then answers about that exact slot.",
     "inputSchema": {"type": "object", "properties": {"day": {"type": "string"}, "time": {"type": "string", "description": "optional specific time the caller asked about"}}, "required": ["day"]}},
    {"name": "schedule_demo",
     "description": "Book a product demo. Requires day, time, the caller's name and a contact (email or phone).",
     "inputSchema": {"type": "object", "properties": {"day": {"type": "string"}, "time": {"type": "string"}, "name": {"type": "string"}, "contact": {"type": "string"}}, "required": ["day", "time", "name", "contact"]}},
    {"name": "take_message",
     "description": "Record a callback message for the Attesto team (sales, support, auditor, investor, press or other).",
     "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}, "contact": {"type": "string"}, "topic": {"type": "string"}, "category": {"type": "string", "enum": ["sales", "support", "auditor", "investor", "press", "other"]}}, "required": ["name", "contact", "topic"]}},
]


def _send(req_id, result):
    body = json.dumps({"jsonrpc": "2.0", "id": req_id, "result": result}, ensure_ascii=False)
    sys.stdout.buffer.write((body + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()


def main() -> None:
    for raw in sys.stdin.buffer:
        line = raw.strip()
        if not line:
            continue
        try:
            msg = json.loads(line.decode("utf-8"))
            method, req_id, params = msg.get("method", ""), msg.get("id"), msg.get("params", {})
            if method == "initialize":
                _send(req_id, {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                               "serverInfo": {"name": "attesto", "version": "1.0.0"}})
            elif method == "tools/list":
                _send(req_id, {"tools": TOOLS})
            elif method == "tools/call":
                tool, args = params.get("name", ""), params.get("arguments", {}) or {}
                try:
                    if tool == "attesto_info":
                        spoken = attesto_info(args.get("topic", "what_is"))
                    elif tool == "check_demo_slots":
                        spoken = check_demo_slots(args.get("day", "tomorrow"), args.get("time", ""))
                    elif tool == "schedule_demo":
                        spoken = schedule_demo(args.get("day", ""), args.get("time", ""),
                                               args.get("name", ""), args.get("contact", ""))
                    elif tool == "take_message":
                        spoken = take_message(args.get("name", ""), args.get("contact", ""),
                                              args.get("topic", ""), args.get("category", "other"))
                    else:
                        spoken = f"Unknown tool: {tool}"
                except Exception as exc:  # noqa: BLE001 — always answer the caller
                    spoken = f"Sorry, that action failed: {exc}"
                _send(req_id, {"content": [{"type": "text", "text": spoken}], "structured": {"spoken": spoken}})
            elif method == "notifications/initialized":
                pass
            elif req_id is not None:
                _send(req_id, {})
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write(f"attesto_server error: {exc}\n")
            sys.stderr.flush()


if __name__ == "__main__":
    main()
