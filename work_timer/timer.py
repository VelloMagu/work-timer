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


def apply_command(user: dict[str, Any], command: str, now_epoch: int, when: str) -> dict[str, Any]:
    if command == "start":
        start(user, now_epoch, when)
        return {"interval": None}
    if command == "pause":
        return {"interval": pause(user, now_epoch, when)}
    if command == "reset":
        return reset(user, now_epoch, when)
    if command == "clear":
        return {"interval": None}
    if command == "clear_addon":
        clear_addon_events(user, now_epoch)
        return {"interval": None}
    raise ValueError(f"unknown command {command}")


def merge_users(server: dict[str, Any], client: dict[str, Any]) -> dict[str, Any]:
    """Keep one open segment. A restart is not a pause. The newer command snapshot wins otherwise."""
    server_at = int(server.get("updated_at") or 0)
    client_at = int(client.get("updated_at") or 0)
    newer = client if client_at > server_at else server
    result = deepcopy(newer)
    result["events"] = list(server.get("events") or [])
    both_running = bool(server.get("running")) and bool(client.get("running"))
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


def _request(method: str, path: str, token: str, body: dict[str, Any] | None = None, timeout: int = 30) -> Any:
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(
        HA_API + path,
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
            history = calendar_events(self.ha_token, self.calendar_entity, self.zone)
            several = len(self.users) > 1
            now_epoch = int(self._now().timestamp())
            users = []
            for name in sorted(self.users):
                user = self._user(name)
                row = public_user(user, now_epoch)
                row["name"] = name
                if several:
                    row["history"] = [item for item in history if item["summary"] == name]
                else:
                    row["history"] = history
                users.append(row)
            self.store.save()
            return {"users": users}


PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Work Timer</title>
<style>
  body { font-family: sans-serif; margin: 24px; color: #1f2933; background: #f7f7f5; }
  h1 { font-size: 1.4rem; }
  article { background: white; border-radius: 12px; padding: 16px 18px; margin: 0 0 16px; }
  .time { font-size: 2rem; margin: 0; }
  .state { margin: 0 0 8px; }
  ul { padding-left: 18px; }
  button { margin-top: 8px; }
  p.note { max-width: 40rem; }
</style>
</head>
<body>
<h1>Work Timer</h1>
<p class="note">Today's lines are kept by the add-on. Completed time stays on the calendar. Clearing here removes only the add-on lines. Remove calendar entries in the calendar itself.</p>
<div id="users">Loading…</div>
<script>
async function load() {
  const response = await fetch("api/view");
  const data = await response.json();
  const root = document.getElementById("users");
  root.innerHTML = "";
  for (const user of data.users || []) {
    const card = document.createElement("article");
    const title = document.createElement("h2");
    title.textContent = user.name;
    const state = document.createElement("p");
    state.className = "state";
    state.textContent = user.running ? "Running" : "Stopped";
    const time = document.createElement("p");
    time.className = "time";
    time.textContent = user.elapsed || "00:00";
    const today = document.createElement("h3");
    today.textContent = "Today";
    const lines = document.createElement("ul");
    for (const event of user.events || []) {
      const item = document.createElement("li");
      item.textContent = (event.t || "") + " " + (event.kind || "");
      lines.appendChild(item);
    }
    if (!lines.childElementCount) {
      const item = document.createElement("li");
      item.textContent = "No lines";
      lines.appendChild(item);
    }
    const week = document.createElement("h3");
    week.textContent = "Last 7 days";
    const history = document.createElement("ul");
    for (const event of user.history || []) {
      const item = document.createElement("li");
      item.textContent = (event.summary ? event.summary + " " : "") + (event.start || "") + " – " + (event.end || "");
      history.appendChild(item);
    }
    if (!history.childElementCount) {
      const item = document.createElement("li");
      item.textContent = "No calendar entries";
      history.appendChild(item);
    }
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = "Clear add-on timestamps";
    button.addEventListener("click", async () => {
      await fetch("api/clear-addon", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({user: user.name})
      });
      load();
    });
    card.append(title, state, time, today, lines, week, history, button);
    root.appendChild(card);
  }
}
load();
setInterval(load, 15000);
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
            if path == "/api/clear-addon":
                if not (self._ingress() or self._authorized()):
                    self._send(401, {"error": "unauthorized"})
                    return
                payload = self._read_json()
                if payload is None:
                    self._send(400, {"error": "invalid json"})
                    return
                name = str(payload.get("user") or "panel")
                try:
                    state = app.handle(name, {"command": "clear_addon"})
                except KeyError:
                    self._send(404, {"error": f"no user {name}"})
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
