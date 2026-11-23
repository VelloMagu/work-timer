"""Work timer. Does not listen until a calendar name is set."""

from __future__ import annotations

import json
import os
import threading
import urllib.error
import urllib.request
from copy import deepcopy
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

STATE_PATH = Path(os.environ.get("STATE_PATH", "/data/work-timer.json"))
HA_API = os.environ.get("HA_API", "http://supervisor/core/api").rstrip("/")
MAX_EVENTS = 6


def load_options() -> dict[str, Any]:
    path = Path(os.environ.get("OPTIONS_PATH", "/data/options.json"))
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise SystemExit("Add-on options must be a JSON object")
    return raw


def blank_user(day: str) -> dict[str, Any]:
    return {
        "running": False,
        "elapsed_seconds": 0,
        "segment_start": None,
        "events": [],
        "intervals": [],
        "updated_at": 0,
        "day": day,
    }


def hhmm(moment: datetime) -> str:
    return moment.strftime("%H:%M")


def elapsed_label(user: dict[str, Any], now_epoch: int) -> str:
    total = int(user.get("elapsed_seconds") or 0)
    start = user.get("segment_start")
    if user.get("running") and isinstance(start, (int, float)):
        total += max(0, now_epoch - int(start))
    minutes = max(0, total) // 60
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def public_user(user: dict[str, Any], now_epoch: int) -> dict[str, Any]:
    return {
        "running": bool(user.get("running")),
        "elapsed_seconds": int(user.get("elapsed_seconds") or 0),
        "segment_start": user.get("segment_start"),
        "events": list(user.get("events") or []),
        "updated_at": int(user.get("updated_at") or 0),
        "day": user.get("day") or "",
        "elapsed": elapsed_label(user, now_epoch),
        "display_cleared_at": int(user.get("display_cleared_at") or 0),
    }


def _trim(user: dict[str, Any]) -> None:
    events = user.setdefault("events", [])
    if len(events) > MAX_EVENTS:
        user["events"] = events[-MAX_EVENTS:]


def add_event(user: dict[str, Any], kind: str, when: str) -> None:
    user.setdefault("events", []).append({"t": when, "kind": kind})
    _trim(user)


def roll_day(user: dict[str, Any], today: str, midnight_epoch: int) -> tuple[int, int] | None:
    """Move a user onto today. A running timer continues and yesterday is closed at midnight."""
    if user.get("day") == today:
        return None
    closed = None
    if user.get("running") and isinstance(user.get("segment_start"), (int, float)):
        closed = (int(user["segment_start"]), midnight_epoch)
        user["segment_start"] = midnight_epoch
        user["elapsed_seconds"] = 0
        user["events"] = [{"t": "00:00", "kind": "start"}]
        user["intervals"] = []
    else:
        user["running"] = False
        user["segment_start"] = None
        user["elapsed_seconds"] = 0
        user["events"] = []
        user["intervals"] = []
    user["day"] = today
    user["updated_at"] = midnight_epoch
    return closed


def start(user: dict[str, Any], now_epoch: int, when: str) -> None:
    if user.get("running"):
        return
    user["running"] = True
    user["segment_start"] = now_epoch
    user["updated_at"] = now_epoch
    add_event(user, "start", when)


def pause(user: dict[str, Any], now_epoch: int, when: str) -> tuple[int, int] | None:
    if not user.get("running") or not isinstance(user.get("segment_start"), (int, float)):
        return None
    began = int(user["segment_start"])
    user["elapsed_seconds"] = int(user.get("elapsed_seconds") or 0) + max(0, now_epoch - began)
    user["running"] = False
    user["segment_start"] = None
    user["updated_at"] = now_epoch
    add_event(user, "pause", when)
    return (began, now_epoch)


def reset(user: dict[str, Any], now_epoch: int, when: str) -> dict[str, Any]:
    """Stop and zero the timer. Stored lines and the calendar stay."""
    if not user.get("running") and int(user.get("elapsed_seconds") or 0) <= 0:
        return {"interval": None}
    interval = pause(user, now_epoch, when) if user.get("running") else None
    user["elapsed_seconds"] = 0
    user["running"] = False
    user["segment_start"] = None
    user["updated_at"] = now_epoch
    return {"interval": interval}


def clear_addon_events(user: dict[str, Any], now_epoch: int) -> None:
    """Drop this user's lines in the add-on. The calendar and the running timer stay."""
    user["events"] = []
    user["updated_at"] = now_epoch


def mark_display_cleared(user: dict[str, Any], now_epoch: int) -> None:
    """Ask the panel to drop its on-screen lines. Stored lines and the calendar stay."""
    user["display_cleared_at"] = now_epoch
    user["updated_at"] = now_epoch


def apply_command(user: dict[str, Any], command: str, now_epoch: int, when: str) -> dict[str, Any]:
    if command == "start":
        start(user, now_epoch, when)
        return {"interval": None}
    if command == "pause":
        return {"interval": pause(user, now_epoch, when)}
    if command == "reset":
        return reset(user, now_epoch, when)
    if command == "clear":
        mark_display_cleared(user, now_epoch)
        return {"interval": None}
    if command == "clear_addon":
        clear_addon_events(user, now_epoch)
        return {"interval": None}
    raise ValueError(f"unknown command {command}")


def merge_users(server: dict[str, Any], client: dict[str, Any]) -> dict[str, Any]:
    """Keep a running timer. A sync from a stopped side is not a pause."""
    server_at = int(server.get("updated_at") or 0)
    client_at = int(client.get("updated_at") or 0)
    server_running = bool(server.get("running"))
    client_running = bool(client.get("running"))
    if server_running != client_running:
        result = deepcopy(server if server_running else client)
        result["running"] = True
        result["events"] = list(server.get("events") or [])
        result["display_cleared_at"] = int(server.get("display_cleared_at") or 0)
        if server_running:
            result["segment_start"] = server.get("segment_start")
            result["elapsed_seconds"] = int(server.get("elapsed_seconds") or 0)
        return result
    newer = client if client_at > server_at else server
    result = deepcopy(newer)
    result["events"] = list(server.get("events") or [])
    result["display_cleared_at"] = int(server.get("display_cleared_at") or 0)
    both_running = server_running and client_running
    if not both_running:
        return result
    starts = [
        int(item["segment_start"])
        for item in (server, client)
        if isinstance(item.get("segment_start"), (int, float))
    ]
    result["running"] = True
    if starts:
        result["segment_start"] = min(starts)
    result["elapsed_seconds"] = int(newer.get("elapsed_seconds") or 0)
    result["events"] = list(server.get("events") or [])
    result["updated_at"] = max(server_at, client_at)
    return result


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.Lock()
        self.users: dict[str, dict[str, Any]] = {}
        if path.is_file():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                raw = {}
            if isinstance(raw, dict):
                for name, user in raw.items():
                    if isinstance(user, dict):
                        self.users[str(name)] = user

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.users), encoding="utf-8")
        temporary.replace(self.path)


def _request(
    method: str,
    path: str,
    token: str,
    body: dict[str, Any] | None = None,
    timeout: int = 30,
    return_response: bool = False,
) -> Any:
    data = None if body is None else json.dumps(body).encode()
    url = HA_API + path
    if return_response:
        url += "&return_response" if "?" in url else "?return_response"
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode()
    except urllib.error.HTTPError as err:
        detail = err.read().decode(errors="replace")
        raise RuntimeError(f"Home Assistant {err.code}: {detail[:300]}") from err
    if not raw:
        return None
    return json.loads(raw)


def timezone_name(token: str) -> str:
    try:
        config = _request("GET", "/config", token)
    except (OSError, RuntimeError, json.JSONDecodeError):
        return "UTC"
    name = config.get("time_zone") if isinstance(config, dict) else None
    return str(name or "UTC")


def slug(name: str) -> str:
    cleaned = "".join(ch.lower() if ch.isalnum() else "_" for ch in name).strip("_")
    while "__" in cleaned:
        cleaned = cleaned.replace("__", "_")
    return cleaned or "work"


def ensure_calendar(token: str, calendar_name: str) -> str:
    states = _request("GET", "/states", token)
    wanted = calendar_name.casefold()
    if isinstance(states, list):
        for state in states:
            if not isinstance(state, dict):
                continue
            entity = str(state.get("entity_id") or "")
            attrs = state.get("attributes") if isinstance(state.get("attributes"), dict) else {}
            friendly = str(attrs.get("friendly_name") or "")
            if entity.startswith("calendar.") and friendly.casefold() == wanted:
                return entity
    flow = _request(
        "POST",
        "/config/config_entries/flow",
        token,
        {"handler": "local_calendar", "show_advanced_options": True},
    )
    if not isinstance(flow, dict) or not flow.get("flow_id"):
        raise RuntimeError("Home Assistant did not start a calendar setup")
    created = _request(
        "POST",
        f"/config/config_entries/flow/{flow['flow_id']}",
        token,
        {"calendar_name": calendar_name},
    )
    if isinstance(created, dict) and created.get("type") not in (None, "create_entry"):
        raise RuntimeError(f"Calendar setup did not finish: {created.get('type')}")
    entity = f"calendar.{slug(calendar_name)}"
    states = _request("GET", "/states", token)
    if isinstance(states, list):
        for state in states:
            if isinstance(state, dict) and state.get("entity_id") == entity:
                return entity
    return entity


def create_interval(token: str, entity: str, user: str, several_users: bool, start_epoch: int, end_epoch: int, zone: ZoneInfo) -> None:
    start = datetime.fromtimestamp(start_epoch, zone).strftime("%Y-%m-%d %H:%M:%S")
    end = datetime.fromtimestamp(max(end_epoch, start_epoch + 60), zone).strftime("%Y-%m-%d %H:%M:%S")
    summary = user if several_users else "Work"
    _request(
        "POST",
        "/services/calendar/create_event",
        token,
        {
            "entity_id": entity,
            "summary": summary,
            "start_date_time": start,
            "end_date_time": end,
        },
    )


def calendar_events(token: str, entity: str, zone: ZoneInfo) -> list[dict[str, str]]:
    now = datetime.now(zone)
    start = (now - timedelta(days=6)).replace(hour=0, minute=0, second=0, microsecond=0)
    end = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    listed = _request(
        "POST",
        "/services/calendar/get_events",
        token,
        {
            "entity_id": entity,
            "start_date_time": start.strftime("%Y-%m-%d %H:%M:%S"),
            "end_date_time": end.strftime("%Y-%m-%d %H:%M:%S"),
        },
        return_response=True,
    )
    found: list[dict[str, Any]] = []
    if isinstance(listed, dict):
        service = listed.get("service_response") if isinstance(listed.get("service_response"), dict) else listed
        bucket = service.get(entity) if isinstance(service, dict) else None
        if isinstance(bucket, dict):
            found = [item for item in bucket.get("events") or [] if isinstance(item, dict)]
    rows = []
    for event in found:
        rows.append(
            {
                "summary": str(event.get("summary") or ""),
                "start": str(event.get("start") or ""),
                "end": str(event.get("end") or ""),
            }
        )
    rows.sort(key=lambda item: item["start"], reverse=True)
    return rows


def _moment(value: str, zone: ZoneInfo) -> datetime | None:
    text = value.strip().replace("Z", "+00:00")
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=zone)
    return moment.astimezone(zone)


def _hhmm_seconds(total: int) -> str:
    minutes = max(0, total) // 60
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def daily_totals(events: list[dict[str, str]], zone: ZoneInfo, today_seconds: int, user_name: str, several: bool) -> list[dict[str, str]]:
    now = datetime.now(zone)
    today = now.date()
    rows = []
    for offset in range(6, -1, -1):
        day = today - timedelta(days=offset)
        rows.append({"date": day.isoformat(), "seconds": 0})
    by_date = {row["date"]: row for row in rows}
    for event in events:
        if several and event.get("summary") != user_name:
            continue
        start = _moment(event.get("start") or "", zone)
        end = _moment(event.get("end") or "", zone)
        if start is None or end is None or end <= start:
            continue
        key = start.date().isoformat()
        if key == today.isoformat() or key not in by_date:
            continue
        by_date[key]["seconds"] += int((end - start).total_seconds())
    by_date[today.isoformat()]["seconds"] = max(0, today_seconds)
    for row in rows:
        row["total"] = _hhmm_seconds(int(row["seconds"]))
        del row["seconds"]
    return rows


class TimerApp:
    def __init__(self, options: dict[str, Any], calendar_entity: str, zone: ZoneInfo, ha_token: str) -> None:
        self.options = options
        self.calendar_entity = calendar_entity
        self.zone = zone
        self.ha_token = ha_token
        self.users = {str(name).strip() for name in options.get("users") or [] if str(name).strip()}
        if not self.users:
            self.users = {"panel"}
        self.store = Store(STATE_PATH)
        self.token = str(options.get("token") or "").strip()

    def _now(self) -> datetime:
        return datetime.now(self.zone)

    def _user(self, name: str) -> dict[str, Any]:
        today = self._now().strftime("%Y-%m-%d")
        user = self.store.users.get(name)
        if user is None:
            user = blank_user(today)
            self.store.users[name] = user
        midnight = datetime.strptime(today, "%Y-%m-%d").replace(tzinfo=self.zone)
        closed = roll_day(user, today, int(midnight.timestamp()))
        if closed is not None:
            self._write_interval(name, closed[0], closed[1])
            self.store.save()
        return user

    def _write_interval(self, name: str, start_epoch: int, end_epoch: int) -> None:
        try:
            create_interval(
                self.ha_token,
                self.calendar_entity,
                name,
                len(self.users) > 1,
                start_epoch,
                end_epoch,
                self.zone,
            )
        except (OSError, RuntimeError) as err:
            print(f"calendar event was not written: {err}", flush=True)

    def handle(self, name: str, payload: dict[str, Any]) -> dict[str, Any]:
        if name not in self.users:
            raise KeyError(name)
        with self.store.lock:
            user = self._user(name)
            now = self._now()
            now_epoch = int(now.timestamp())
            client = payload.get("state") if isinstance(payload.get("state"), dict) else None
            command = str(payload.get("command") or "").strip()
            result: dict[str, Any] = {"interval": None}
            if command:
                result = apply_command(user, command, now_epoch, hhmm(now))
            elif isinstance(client, dict) and "running" in client:
                client = dict(client)
                client["day"] = user.get("day")
                merged = merge_users(user, client)
                self.store.users[name] = merged
                user = merged
                closed = payload.get("closed_interval")
                if isinstance(closed, list) and len(closed) == 2:
                    result = {"interval": (int(closed[0]), int(closed[1]))}
            interval = result.get("interval")
            seen = user.setdefault("seen_intervals", [])
            if isinstance(interval, tuple):
                key = f"{interval[0]}:{interval[1]}"
                if key not in seen:
                    seen.append(key)
                    self._write_interval(name, interval[0], interval[1])
            self.store.save()
            return public_user(user, now_epoch)

    def snapshot(self) -> dict[str, Any]:
        with self.store.lock:
            try:
                history = calendar_events(self.ha_token, self.calendar_entity, self.zone)
            except (OSError, RuntimeError, json.JSONDecodeError) as err:
                print(f"calendar events were not loaded: {err}", flush=True)
                history = []
            several = len(self.users) > 1
            now_epoch = int(self._now().timestamp())
            users = []
            for name in sorted(self.users):
                user = self._user(name)
                row = public_user(user, now_epoch)
                row["name"] = name
                if several:
                    visible = [item for item in history if item["summary"] == name]
                else:
                    visible = history
                row["history"] = visible
                total = int(user.get("elapsed_seconds") or 0)
                start = user.get("segment_start")
                if user.get("running") and isinstance(start, (int, float)):
                    total += max(0, now_epoch - int(start))
                row["days"] = daily_totals(history, self.zone, total, name, several)
                users.append(row)
            self.store.save()
            return {"users": users, "server_now": now_epoch}


PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Work Timer</title>
<style>
  :root {
    color-scheme: light dark;
    --bg: #f3f5f8;
    --surface: #ffffff;
    --surface-subtle: #f8fafc;
    --border: rgba(15, 23, 42, 0.08);
    --text: #0f172a;
    --text-muted: #64748b;
    --accent: #03a9f4;
    --accent-soft: rgba(3, 169, 244, 0.12);
    --green: #10b981;
    --green-soft: rgba(16, 185, 129, 0.14);
    --amber: #f59e0b;
    --amber-soft: rgba(245, 158, 11, 0.15);
    --red: #ef4444;
    --red-soft: rgba(239, 68, 68, 0.12);
    --btn-bg: #eef2f6;
    --btn-hover: #e2e8f0;
    --bar-track: #e9eef4;
    --shadow: 0 1px 2px rgba(15, 23, 42, 0.04), 0 6px 20px rgba(15, 23, 42, 0.04);
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #111418;
      --surface: #1b2028;
      --surface-subtle: #222832;
      --border: rgba(255, 255, 255, 0.08);
      --text: #f1f5f9;
      --text-muted: #94a3b8;
      --accent: #38bdf8;
      --accent-soft: rgba(56, 189, 248, 0.15);
      --green: #34d399;
      --green-soft: rgba(52, 211, 153, 0.16);
      --amber: #fbbf24;
      --amber-soft: rgba(251, 191, 36, 0.16);
      --red: #f87171;
      --red-soft: rgba(248, 113, 113, 0.16);
      --btn-bg: #262d38;
      --btn-hover: #313a48;
      --bar-track: #262e3a;
      --shadow: 0 1px 2px rgba(0, 0, 0, 0.25), 0 8px 24px rgba(0, 0, 0, 0.22);
    }
  }
  html.dark {
    --bg: #111418;
    --surface: #1b2028;
    --surface-subtle: #222832;
    --border: rgba(255, 255, 255, 0.08);
    --text: #f1f5f9;
    --text-muted: #94a3b8;
    --accent: #38bdf8;
    --accent-soft: rgba(56, 189, 248, 0.15);
    --green: #34d399;
    --green-soft: rgba(52, 211, 153, 0.16);
    --amber: #fbbf24;
    --amber-soft: rgba(251, 191, 36, 0.16);
    --red: #f87171;
    --red-soft: rgba(248, 113, 113, 0.16);
    --btn-bg: #262d38;
    --btn-hover: #313a48;
    --bar-track: #262e3a;
    --shadow: 0 1px 2px rgba(0, 0, 0, 0.25), 0 8px 24px rgba(0, 0, 0, 0.22);
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    padding: 24px 20px 40px;
    font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: var(--bg);
    color: var(--text);
    line-height: 1.45;
  }
  .shell {
    max-width: 1120px;
    margin: 0 auto;
  }
  .topbar {
    display: flex;
    flex-wrap: wrap;
    align-items: flex-start;
    justify-content: space-between;
    gap: 12px 20px;
    margin-bottom: 22px;
  }
  .topbar h1 {
    margin: 0;
    font-size: 1.5rem;
    font-weight: 700;
    letter-spacing: -0.02em;
  }
  .subtitle {
    margin: 4px 0 0;
    font-size: 0.875rem;
    color: var(--text-muted);
    max-width: 44rem;
  }
  .topbar-meta {
    display: flex;
    align-items: center;
    gap: 10px;
    font-size: 0.8rem;
    color: var(--text-muted);
  }
  .grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(360px, 1fr));
    gap: 20px;
  }
  .grid.single {
    grid-template-columns: minmax(0, 860px);
    justify-content: center;
  }
  .card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 16px;
    padding: 20px 22px;
    box-shadow: var(--shadow);
    display: flex;
    flex-direction: column;
    gap: 18px;
  }
  .card-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
  }
  .user-name {
    margin: 0;
    font-size: 1.125rem;
    font-weight: 650;
    letter-spacing: -0.01em;
    text-transform: capitalize;
  }
  .badge {
    display: inline-flex;
    align-items: center;
    gap: 7px;
    padding: 4px 10px;
    border-radius: 999px;
    font-size: 0.78rem;
    font-weight: 600;
    background: var(--btn-bg);
    color: var(--text-muted);
  }
  .badge.running {
    background: var(--green-soft);
    color: var(--green);
  }
  .dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: currentColor;
  }
  .badge.running .dot {
    animation: pulse 1.8s ease-in-out infinite;
  }
  @keyframes pulse {
    0%, 100% { opacity: 1; transform: scale(1); }
    50% { opacity: 0.45; transform: scale(0.85); }
  }
  .hero {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: 16px;
    padding: 16px 18px;
    border-radius: 12px;
    background: var(--surface-subtle);
    border: 1px solid var(--border);
  }
  .timer-label {
    font-size: 0.75rem;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--text-muted);
    margin: 0 0 2px;
  }
  .time {
    margin: 0;
    font-size: 2.4rem;
    font-weight: 700;
    line-height: 1.05;
    letter-spacing: -0.02em;
    font-variant-numeric: tabular-nums;
  }
  .actions {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
  }
  button {
    font: inherit;
    font-size: 0.85rem;
    font-weight: 600;
    border: 1px solid transparent;
    border-radius: 10px;
    padding: 8px 14px;
    cursor: pointer;
    background: var(--btn-bg);
    color: var(--text);
    transition: background 0.15s ease, transform 0.08s ease, opacity 0.15s ease;
  }
  button:hover:not(:disabled) { background: var(--btn-hover); }
  button:active:not(:disabled) { transform: translateY(1px); }
  button:disabled { opacity: 0.55; cursor: default; }
  button.primary {
    background: var(--accent);
    color: #ffffff;
  }
  button.primary:hover:not(:disabled) { filter: brightness(1.06); background: var(--accent); }
  button.pause {
    background: var(--amber-soft);
    color: var(--amber);
    border-color: rgba(245, 158, 11, 0.28);
  }
  button.pause:hover:not(:disabled) { filter: brightness(1.05); background: var(--amber-soft); }
  button.danger {
    background: var(--red-soft);
    color: var(--red);
  }
  button.danger:hover:not(:disabled) { filter: brightness(1.06); background: var(--red-soft); }
  button.ghost {
    background: transparent;
    border-color: var(--border);
    color: var(--text-muted);
    font-size: 0.78rem;
    padding: 6px 10px;
  }
  button.ghost:hover:not(:disabled) {
    background: var(--btn-bg);
    color: var(--text);
  }
  .sections {
    display: grid;
    grid-template-columns: 1.35fr 1fr;
    gap: 18px;
  }
  @media (max-width: 680px) {
    body { padding: 16px 12px 32px; }
    .grid { grid-template-columns: 1fr; }
    .sections { grid-template-columns: 1fr; }
    .time { font-size: 2rem; }
  }
  .panel {
    display: flex;
    flex-direction: column;
    gap: 10px;
  }
  .section-head {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 8px;
  }
  .section-head h3 {
    margin: 0;
    font-size: 0.82rem;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: var(--text-muted);
  }
  .week-total {
    font-size: 0.82rem;
    font-weight: 650;
    font-variant-numeric: tabular-nums;
    color: var(--text);
  }
  .bars {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .bar-row {
    display: grid;
    grid-template-columns: 62px 1fr 48px;
    align-items: center;
    gap: 10px;
    font-size: 0.84rem;
  }
  .bar-day {
    color: var(--text-muted);
    font-weight: 500;
    white-space: nowrap;
  }
  .bar-row.today .bar-day {
    color: var(--text);
    font-weight: 700;
  }
  .bar-track {
    height: 10px;
    border-radius: 999px;
    background: var(--bar-track);
    overflow: hidden;
  }
  .bar-fill {
    height: 100%;
    border-radius: 999px;
    background: var(--accent);
    opacity: 0.72;
    transition: width 0.25s ease;
  }
  .bar-row.today .bar-fill {
    opacity: 1;
    background: var(--green);
  }
  .bar-val {
    text-align: right;
    font-variant-numeric: tabular-nums;
    font-weight: 600;
    color: var(--text);
  }
  .bar-val.zero {
    color: var(--text-muted);
    font-weight: 400;
  }
  .events {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .event-item {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 7px 10px;
    border-radius: 9px;
    background: var(--surface-subtle);
    border: 1px solid var(--border);
    font-size: 0.84rem;
  }
  .event-time {
    font-variant-numeric: tabular-nums;
    font-weight: 650;
  }
  .event-kind {
    font-size: 0.74rem;
    font-weight: 650;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    padding: 2px 8px;
    border-radius: 999px;
    background: var(--btn-bg);
    color: var(--text-muted);
  }
  .event-kind.start {
    background: var(--green-soft);
    color: var(--green);
  }
  .event-kind.pause {
    background: var(--amber-soft);
    color: var(--amber);
  }
  .empty {
    font-size: 0.84rem;
    color: var(--text-muted);
    padding: 12px;
    border-radius: 10px;
    background: var(--surface-subtle);
    border: 1px dashed var(--border);
    text-align: center;
  }
  .card-foot {
    display: flex;
    flex-wrap: wrap;
    justify-content: flex-end;
    gap: 8px;
    padding-top: 12px;
    border-top: 1px solid var(--border);
  }
  .status-banner {
    padding: 16px 18px;
    border-radius: 12px;
    background: var(--surface);
    border: 1px solid var(--border);
    color: var(--text-muted);
  }
</style>
</head>
<body>
<div class="shell">
  <header class="topbar">
    <div>
      <h1>Work Timer</h1>
      <p class="subtitle">Today's lines stay in the add-on while completed intervals are saved to your Home Assistant calendar. Clearing here only drops add-on or panel lines.</p>
    </div>
    <div class="topbar-meta">
      <span id="synced">Syncing…</span>
      <button id="refreshBtn" class="ghost" type="button">Refresh</button>
    </div>
  </header>
  <main id="users" class="status-banner">Loading…</main>
</div>
<script>
let currentUsers = [];
let serverOffsetSec = 0;

function parseHHMM(text) {
  const parts = String(text || "00:00").split(":");
  if (parts.length !== 2) return 0;
  return (parseInt(parts[0], 10) || 0) * 3600 + (parseInt(parts[1], 10) || 0) * 60;
}

function fmtHMS(totalSec) {
  const s = Math.max(0, Math.floor(totalSec));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  return String(h).padStart(2, "0") + ":" + String(m).padStart(2, "0") + ":" + String(sec).padStart(2, "0");
}

function fmtHM(totalSec) {
  const m = Math.max(0, Math.floor(totalSec / 60));
  return String(Math.floor(m / 60)).padStart(2, "0") + ":" + String(m % 60).padStart(2, "0");
}

function fmtDayLabel(iso, isToday) {
  if (isToday) return "Today";
  const d = new Date(iso + "T00:00:00");
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleDateString(undefined, { weekday: "short", day: "numeric" });
}

function liveSeconds(user) {
  let total = Number(user.elapsed_seconds || 0);
  if (user.running && typeof user.segment_start === "number") {
    const nowSec = Math.floor(Date.now() / 1000) + serverOffsetSec;
    total += Math.max(0, nowSec - user.segment_start);
  }
  return total;
}

async function sendCommand(userName, command, button) {
  if (command === "reset" && !window.confirm("Reset today's timer for " + userName + " to 00:00? Completed calendar entries will be kept.")) {
    return;
  }
  if (button) button.disabled = true;
  try {
    await fetch("api/command", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({user: userName, command})
    });
  } finally {
    await load();
  }
}

function render(users) {
  const root = document.getElementById("users");
  root.className = users.length <= 1 ? "grid single" : "grid";
  root.innerHTML = "";
  if (!users.length) {
    root.className = "status-banner";
    root.textContent = "No users configured.";
    return;
  }

  for (const user of users) {
    const card = document.createElement("article");
    card.className = "card";
    card.dataset.user = user.name;

    // Header
    const head = document.createElement("div");
    head.className = "card-head";
    const title = document.createElement("h2");
    title.className = "user-name";
    title.textContent = user.name;
    const badge = document.createElement("span");
    badge.className = "badge" + (user.running ? " running" : "");
    const dot = document.createElement("span");
    dot.className = "dot";
    badge.append(dot, document.createTextNode(user.running ? "Running" : "Stopped"));
    head.append(title, badge);

    // Hero timer & primary controls
    const hero = document.createElement("div");
    hero.className = "hero";
    const timerBox = document.createElement("div");
    const timerLabel = document.createElement("p");
    timerLabel.className = "timer-label";
    timerLabel.textContent = "Today's total";
    const timeEl = document.createElement("p");
    timeEl.className = "time";
    timeEl.dataset.timer = user.name;
    const secNow = liveSeconds(user);
    timeEl.textContent = fmtHMS(secNow);
    timerBox.append(timerLabel, timeEl);

    const actions = document.createElement("div");
    actions.className = "actions";
    const toggleBtn = document.createElement("button");
    toggleBtn.type = "button";
    toggleBtn.className = user.running ? "pause" : "primary";
    toggleBtn.textContent = user.running ? "Pause" : "Start";
    toggleBtn.addEventListener("click", () => sendCommand(user.name, user.running ? "pause" : "start", toggleBtn));

    const resetBtn = document.createElement("button");
    resetBtn.type = "button";
    resetBtn.className = "danger";
    resetBtn.textContent = "Reset";
    resetBtn.disabled = !user.running && secNow <= 0;
    resetBtn.addEventListener("click", () => sendCommand(user.name, "reset", resetBtn));

    actions.append(toggleBtn, resetBtn);
    hero.append(timerBox, actions);

    // Two-column body: 7-day bars + today's events
    const sections = document.createElement("div");
    sections.className = "sections";

    // Left: 7-day chart
    const weekPanel = document.createElement("section");
    weekPanel.className = "panel";
    const days = user.days || [];
    const daySecs = days.map((d, idx) => idx === days.length - 1 ? secNow : parseHHMM(d.total));
    const maxSec = Math.max(3600, ...daySecs);
    const weekSum = daySecs.reduce((acc, v) => acc + v, 0);

    const weekHead = document.createElement("div");
    weekHead.className = "section-head";
    const weekTitle = document.createElement("h3");
    weekTitle.textContent = "Last 7 days";
    const weekTotal = document.createElement("span");
    weekTotal.className = "week-total";
    weekTotal.textContent = "Total " + fmtHM(weekSum);
    weekHead.append(weekTitle, weekTotal);

    const bars = document.createElement("div");
    bars.className = "bars";
    if (!days.length) {
      const empty = document.createElement("div");
      empty.className = "empty";
      empty.textContent = "No calendar entries";
      bars.appendChild(empty);
    } else {
      days.forEach((day, idx) => {
        const isToday = idx === days.length - 1;
        const valSec = daySecs[idx];
        const row = document.createElement("div");
        row.className = "bar-row" + (isToday ? " today" : "");
        row.title = day.date + " — " + fmtHM(valSec);

        const dayLbl = document.createElement("span");
        dayLbl.className = "bar-day";
        dayLbl.textContent = fmtDayLabel(day.date, isToday);

        const track = document.createElement("div");
        track.className = "bar-track";
        const fill = document.createElement("div");
        fill.className = "bar-fill";
        fill.style.width = (valSec > 0 ? Math.max(4, Math.round((valSec / maxSec) * 100)) : 0) + "%";
        track.appendChild(fill);

        const valLbl = document.createElement("span");
        valLbl.className = "bar-val" + (valSec <= 0 ? " zero" : "");
        valLbl.textContent = fmtHM(valSec);

        row.append(dayLbl, track, valLbl);
        bars.appendChild(row);
      });
    }
    weekPanel.append(weekHead, bars);

    // Right: Today's activity log
    const todayPanel = document.createElement("section");
    todayPanel.className = "panel";
    const todayHead = document.createElement("div");
    todayHead.className = "section-head";
    const todayTitle = document.createElement("h3");
    todayTitle.textContent = "Today's activity";
    todayHead.appendChild(todayTitle);

    const events = user.events || [];
    if (!events.length) {
      const empty = document.createElement("div");
      empty.className = "empty";
      empty.textContent = "No timestamps yet";
      todayPanel.append(todayHead, empty);
    } else {
      const list = document.createElement("ul");
      list.className = "events";
      for (const ev of [...events].reverse()) {
        const item = document.createElement("li");
        item.className = "event-item";
        const t = document.createElement("span");
        t.className = "event-time";
        t.textContent = ev.t || "--:--";
        const k = document.createElement("span");
        const kind = String(ev.kind || "").toLowerCase();
        k.className = "event-kind " + (kind === "start" || kind === "pause" ? kind : "");
        k.textContent = ev.kind || "";
        item.append(t, k);
        list.appendChild(item);
      }
      todayPanel.append(todayHead, list);
    }

    sections.append(weekPanel, todayPanel);

    // Footer utility buttons
    const foot = document.createElement("div");
    foot.className = "card-foot";
    const clearPanelBtn = document.createElement("button");
    clearPanelBtn.type = "button";
    clearPanelBtn.className = "ghost";
    clearPanelBtn.textContent = "Clear panel screen";
    clearPanelBtn.addEventListener("click", () => sendCommand(user.name, "clear", clearPanelBtn));

    const clearAddonBtn = document.createElement("button");
    clearAddonBtn.type = "button";
    clearAddonBtn.className = "ghost";
    clearAddonBtn.textContent = "Clear add-on timestamps";
    clearAddonBtn.addEventListener("click", () => sendCommand(user.name, "clear_addon", clearAddonBtn));

    foot.append(clearPanelBtn, clearAddonBtn);
    card.append(head, hero, sections, foot);
    root.appendChild(card);
  }
}

function tick() {
  for (const user of currentUsers) {
    if (!user.running) continue;
    const el = document.querySelector('[data-timer="' + CSS.escape(user.name) + '"]');
    if (el) el.textContent = fmtHMS(liveSeconds(user));
  }
}

async function load() {
  const root = document.getElementById("users");
  const synced = document.getElementById("synced");
  let data;
  try {
    const response = await fetch("api/view");
    data = await response.json();
  } catch (err) {
    root.className = "status-banner";
    root.textContent = "The timer could not be loaded.";
    return;
  }
  if (!data || data.error) {
    root.className = "status-banner";
    root.textContent = (data && data.error) || "The timer could not be loaded.";
    return;
  }
  if (typeof data.server_now === "number") {
    serverOffsetSec = data.server_now - Math.floor(Date.now() / 1000);
  }
  currentUsers = data.users || [];
  render(currentUsers);
  synced.textContent = "Updated " + new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

document.getElementById("refreshBtn").addEventListener("click", load);
load();
setInterval(load, 15000);
setInterval(tick, 1000);

function followTheme() {
  try {
    const parent = window.parent.document.documentElement;
    const style = window.parent.getComputedStyle(parent);
    const dark = parent.classList.contains("dark") || style.colorScheme === "dark";
    document.documentElement.classList.toggle("dark", dark);
  } catch (err) {}
}
followTheme();
setInterval(followTheme, 2000);
</script>
</body>
</html>
"""


def make_handler(app: TimerApp, token: str) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "work-timer/0.1"

        def log_message(self, fmt: str, *args: Any) -> None:
            print(f"{self.address_string()} {fmt % args}", flush=True)

        def _send(self, status: int, payload: dict[str, Any]) -> None:
            raw = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _send_html(self, page: str) -> None:
            raw = page.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _ingress(self) -> bool:
            return bool(self.headers.get("X-Ingress-Path"))

        def _authorized(self) -> bool:
            if not token:
                return False
            header = self.headers.get("Authorization", "")
            return header == f"Bearer {token}"

        def _read_json(self) -> dict[str, Any] | None:
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length).decode() or "{}")
            except (ValueError, json.JSONDecodeError):
                return None
            return payload if isinstance(payload, dict) else None

        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path.rstrip("/") or "/"
            if path in ("/", "/index.html"):
                if not (self._ingress() or self._authorized()):
                    self._send(401, {"error": "unauthorized"})
                    return
                self._send_html(PAGE)
                return
            if path == "/api/view":
                if not (self._ingress() or self._authorized()):
                    self._send(401, {"error": "unauthorized"})
                    return
                try:
                    self._send(200, app.snapshot())
                except (OSError, RuntimeError) as err:
                    self._send(502, {"error": str(err)})
                return
            if path != "/v1/timer":
                self._send(404, {"error": "not found"})
                return
            if not self._authorized():
                self._send(401, {"error": "unauthorized"})
                return
            query = parse_qs(urlparse(self.path).query)
            name = (query.get("user") or ["panel"])[0]
            try:
                state = app.handle(name, {})
            except KeyError:
                self._send(404, {"error": f"no user {name}"})
                return
            self._send(200, state)

        def do_POST(self) -> None:  # noqa: N802
            path = urlparse(self.path).path.rstrip("/")
            if path in ("/api/clear-addon", "/api/command"):
                if not (self._ingress() or self._authorized()):
                    self._send(401, {"error": "unauthorized"})
                    return
                payload = self._read_json()
                if payload is None:
                    self._send(400, {"error": "invalid json"})
                    return
                name = str(payload.get("user") or "panel")
                command = "clear_addon" if path == "/api/clear-addon" else str(payload.get("command") or "").strip()
                try:
                    state = app.handle(name, {"command": command})
                except KeyError:
                    self._send(404, {"error": f"no user {name}"})
                    return
                except ValueError as err:
                    self._send(400, {"error": str(err)})
                    return
                self._send(200, state)
                return
            if not self._authorized():
                self._send(401, {"error": "unauthorized"})
                return
            if path != "/v1/timer":
                self._send(404, {"error": "not found"})
                return
            payload = self._read_json()
            if payload is None:
                self._send(400, {"error": "invalid json"})
                return
            name = str(payload.get("user") or "panel")
            try:
                state = app.handle(name, payload)
            except KeyError:
                self._send(404, {"error": f"no user {name}"})
                return
            except ValueError as err:
                self._send(400, {"error": str(err)})
                return
            self._send(200, state)

    return Handler


def main() -> None:
    options = load_options()
    calendar_name = str(options.get("calendar_name") or "").strip()
    if not calendar_name:
        raise SystemExit("Set calendar_name in the add-on options. The work timer will not listen until it is set.")
    ha_token = os.environ.get("SUPERVISOR_TOKEN", "").strip()
    if not ha_token:
        raise SystemExit("Home Assistant API token is missing. Enable homeassistant_api on the add-on.")
    zone = ZoneInfo(timezone_name(ha_token))
    entity = ensure_calendar(ha_token, calendar_name)
    print(f"Using calendar {entity}", flush=True)
    port = int(options.get("port") or 8097)
    app = TimerApp(options, entity, zone, ha_token)
    server = ThreadingHTTPServer(("0.0.0.0", port), make_handler(app, str(options.get("token") or "").strip()))
    print(f"Listening on port {port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
