"""Work timer. Does not listen until a calendar name is set."""

from __future__ import annotations

import base64
import json
import os
import socket
import ssl
import struct
import threading
import urllib.error
import urllib.request
from copy import deepcopy
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse
from zoneinfo import ZoneInfo

STATE_PATH = Path(os.environ.get("STATE_PATH", "/data/work-timer.json"))
HA_API = os.environ.get("HA_API", "http://supervisor/core/api").rstrip("/")
HA_WS = os.environ.get("HA_WS", "ws://supervisor/core/websocket")
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
    """A routine sync must not undo a newer start or pause."""
    server_at = int(server.get("updated_at") or 0)
    client_at = int(client.get("updated_at") or 0)
    server_running = bool(server.get("running"))
    client_running = bool(client.get("running"))

    def finish(source: dict[str, Any], running: bool) -> dict[str, Any]:
        result = deepcopy(source)
        result["running"] = running
        result["events"] = list(server.get("events") or [])
        result["display_cleared_at"] = int(server.get("display_cleared_at") or 0)
        if not running:
            result["segment_start"] = None
        return result

    if server_running != client_running:
        if server_at >= client_at:
            return finish(server, server_running)
        return finish(client, client_running)
    newer = client if client_at > server_at else server
    result = deepcopy(newer)
    result["events"] = list(server.get("events") or [])
    result["display_cleared_at"] = int(server.get("display_cleared_at") or 0)
    if not (server_running and client_running):
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


def calendar_events_range(
    token: str, entity: str, zone: ZoneInfo, start: datetime, end: datetime
) -> list[dict[str, str]]:
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


def calendar_events(token: str, entity: str, zone: ZoneInfo) -> list[dict[str, str]]:
    now = datetime.now(zone)
    start = (now - timedelta(days=6)).replace(hour=0, minute=0, second=0, microsecond=0)
    end = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    return calendar_events_range(token, entity, zone, start, end)


def _ws_recv_exact(sock: socket.socket, count: int, buf: bytearray) -> bytes:
    while len(buf) < count:
        chunk = sock.recv(max(4096, count - len(buf)))
        if not chunk:
            raise RuntimeError("Home Assistant WebSocket closed unexpectedly")
        buf.extend(chunk)
    out = bytes(buf[:count])
    del buf[:count]
    return out


def _ws_send_frame(sock: socket.socket, opcode: int, payload: bytes) -> None:
    mask = os.urandom(4)
    header = bytearray([0x80 | (opcode & 0x0F)])
    length = len(payload)
    if length < 126:
        header.append(0x80 | length)
    elif length < 65536:
        header.append(0x80 | 126)
        header.extend(struct.pack("!H", length))
    else:
        header.append(0x80 | 127)
        header.extend(struct.pack("!Q", length))
    header.extend(mask)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    sock.sendall(bytes(header) + masked)


def _ws_recv_json(sock: socket.socket, buf: bytearray) -> dict[str, Any]:
    while True:
        first, second = _ws_recv_exact(sock, 2, buf)
        opcode = first & 0x0F
        masked = bool(second & 0x80)
        length = second & 0x7F
        if length == 126:
            length = struct.unpack("!H", _ws_recv_exact(sock, 2, buf))[0]
        elif length == 127:
            length = struct.unpack("!Q", _ws_recv_exact(sock, 8, buf))[0]
        mask = _ws_recv_exact(sock, 4, buf) if masked else b""
        payload = _ws_recv_exact(sock, length, buf) if length else b""
        if masked:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        if opcode == 0x9:  # ping
            _ws_send_frame(sock, 0xA, payload)
            continue
        if opcode == 0x8:  # close
            raise RuntimeError("Home Assistant WebSocket closed")
        if opcode in (0x1, 0x2):
            parsed = json.loads(payload.decode("utf-8"))
            if isinstance(parsed, dict):
                return parsed


def _ws_delete_calendar_events(token: str, entity: str, targets: list[dict[str, str]]) -> int:
    if not targets:
        return 0
    parsed = urlparse(HA_WS)
    scheme = parsed.scheme or "ws"
    host = parsed.hostname or "supervisor"
    port = parsed.port or (443 if scheme == "wss" else 80)
    ws_path = parsed.path or "/core/websocket"
    if parsed.query:
        ws_path += "?" + parsed.query
    raw_sock = socket.create_connection((host, port), timeout=15)
    sock: socket.socket = (
        ssl.create_default_context().wrap_socket(raw_sock, server_hostname=host)
        if scheme == "wss"
        else raw_sock
    )
    try:
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        host_header = f"{host}:{port}" if parsed.port else host
        req = (
            f"GET {ws_path} HTTP/1.1\r\n"
            f"Host: {host_header}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n\r\n"
        )
        sock.sendall(req.encode("ascii"))
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = sock.recv(1024)
            if not chunk:
                raise RuntimeError("WebSocket handshake failed")
            head += chunk
            if len(head) > 16384:
                raise RuntimeError("WebSocket handshake header too large")
        header_part, leftover = head.split(b"\r\n\r\n", 1)
        status_line = header_part.split(b"\r\n", 1)[0].decode("ascii", errors="replace")
        if " 101 " not in status_line:
            raise RuntimeError(f"WebSocket handshake rejected: {status_line}")
        ws_buf = bytearray(leftover)

        greeting = _ws_recv_json(sock, ws_buf)
        if greeting.get("type") == "auth_required":
            _ws_send_frame(sock, 0x1, json.dumps({"type": "auth", "access_token": token}).encode("utf-8"))
            auth = _ws_recv_json(sock, ws_buf)
            if auth.get("type") != "auth_ok":
                raise RuntimeError(f"WebSocket auth failed: {auth.get('message') or auth.get('type')}")

        deleted = 0
        for idx, item in enumerate(targets, start=1):
            msg: dict[str, Any] = {
                "id": idx,
                "type": "calendar/event/delete",
                "entity_id": entity,
                "uid": item["uid"],
            }
            if item.get("recurrence_id"):
                msg["recurrence_id"] = item["recurrence_id"]
            _ws_send_frame(sock, 0x1, json.dumps(msg).encode("utf-8"))
            while True:
                reply = _ws_recv_json(sock, ws_buf)
                if reply.get("id") == idx:
                    if not reply.get("success"):
                        err = reply.get("error")
                        detail = err.get("message") if isinstance(err, dict) else str(err)
                        raise RuntimeError(f"Could not delete event {item['uid']}: {detail}")
                    deleted += 1
                    break
        return deleted
    finally:
        try:
            _ws_send_frame(sock, 0x8, b"")
        except OSError:
            pass
        sock.close()


def _event_start_text(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("dateTime") or value.get("date") or "")
    return str(value or "")


def delete_day_events(
    token: str,
    entity: str,
    zone: ZoneInfo,
    date_iso: str,
    user_name: str,
    several: bool,
) -> int:
    day_dt = datetime.strptime(date_iso, "%Y-%m-%d").replace(tzinfo=zone)
    start_dt = day_dt
    end_dt = day_dt + timedelta(days=1)
    query = urlencode({"start": start_dt.isoformat(), "end": end_dt.isoformat()})
    raw = _request("GET", f"/calendars/{entity}?{query}", token)
    targets: list[dict[str, str]] = []
    if isinstance(raw, list):
        for event in raw:
            if not isinstance(event, dict):
                continue
            uid = str(event.get("uid") or "").strip()
            if not uid:
                continue
            if several and str(event.get("summary") or "") != user_name:
                continue
            start_moment = _moment(_event_start_text(event.get("start")), zone)
            if start_moment is None or start_moment.date().isoformat() != date_iso:
                continue
            targets.append(
                {
                    "uid": uid,
                    "recurrence_id": str(event.get("recurrence_id") or "").strip(),
                }
            )
    return _ws_delete_calendar_events(token, entity, targets)


def build_timetables(
    events: list[dict[str, str]],
    zone: ZoneInfo,
    dates: list[str],
    user_name: str,
    several: bool,
    active_user: dict[str, Any] | None = None,
    now_epoch: int | None = None,
) -> dict[str, dict[str, Any]]:
    by_date: dict[str, list[tuple[datetime, dict[str, Any]]]] = {d: [] for d in dates}
    for event in events:
        if several and event.get("summary") != user_name:
            continue
        start = _moment(event.get("start") or "", zone)
        end = _moment(event.get("end") or "", zone)
        if start is None or end is None or end <= start:
            continue
        key = start.date().isoformat()
        if key not in by_date:
            continue
        secs = int((end - start).total_seconds())
        by_date[key].append(
            (
                start,
                {
                    "start": start.strftime("%H:%M"),
                    "end": end.strftime("%H:%M"),
                    "seconds": secs,
                    "running": False,
                },
            )
        )
    if (
        active_user is not None
        and now_epoch is not None
        and active_user.get("running")
        and isinstance(active_user.get("segment_start"), (int, float))
    ):
        seg_start = int(active_user["segment_start"])
        seg_dt = datetime.fromtimestamp(seg_start, zone)
        key = seg_dt.date().isoformat()
        if key in by_date:
            by_date[key].append(
                (
                    seg_dt,
                    {
                        "start": seg_dt.strftime("%H:%M"),
                        "end": None,
                        "seconds": max(0, now_epoch - seg_start),
                        "running": True,
                    },
                )
            )
    result: dict[str, dict[str, Any]] = {}
    for day in dates:
        items = sorted(by_date[day], key=lambda pair: pair[0])
        intervals = [item for _, item in items]
        total_secs = sum(int(item["seconds"]) for item in intervals)
        result[day] = {
            "date": day,
            "intervals": intervals,
            "total_seconds": total_secs,
            "total": _hhmm_seconds(total_secs),
        }
    return result


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
            now = self._now()
            now_epoch = int(now.timestamp())
            today = now.date()
            last5 = [(today - timedelta(days=offset)).isoformat() for offset in range(4, -1, -1)]
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
                timetables = build_timetables(history, self.zone, last5, name, several, user, now_epoch)
                row["recent_days"] = [timetables[d] for d in last5]
                users.append(row)
            self.store.save()
            return {"users": users, "server_now": now_epoch, "today": today.isoformat()}

    def month_calendar(self, name: str, year: int, month: int) -> dict[str, Any]:
        if name not in self.users:
            raise KeyError(name)
        if not (1 <= month <= 12) or not (2000 <= year <= 2100):
            raise ValueError("invalid year or month")
        start = datetime(year, month, 1, 0, 0, 0, tzinfo=self.zone)
        if month == 12:
            end = datetime(year + 1, 1, 1, 0, 0, 0, tzinfo=self.zone)
        else:
            end = datetime(year, month + 1, 1, 0, 0, 0, tzinfo=self.zone)
        num_days = (end.date() - start.date()).days
        dates = [(start.date() + timedelta(days=i)).isoformat() for i in range(num_days)]
        with self.store.lock:
            try:
                events = calendar_events_range(self.ha_token, self.calendar_entity, self.zone, start, end)
            except (OSError, RuntimeError, json.JSONDecodeError) as err:
                print(f"calendar month was not loaded: {err}", flush=True)
                events = []
            user = self._user(name)
            now_epoch = int(self._now().timestamp())
            several = len(self.users) > 1
            timetables = build_timetables(events, self.zone, dates, name, several, user, now_epoch)
            return {
                "user": name,
                "year": year,
                "month": month,
                "days": timetables,
                "server_now": now_epoch,
            }

    def clear_day(self, name: str, date_iso: str) -> dict[str, Any]:
        if name not in self.users:
            raise KeyError(name)
        try:
            datetime.strptime(date_iso, "%Y-%m-%d")
        except ValueError as err:
            raise ValueError("invalid date") from err
        several = len(self.users) > 1
        with self.store.lock:
            deleted = delete_day_events(
                self.ha_token,
                self.calendar_entity,
                self.zone,
                date_iso,
                name,
                several,
            )
            now = self._now()
            today = now.strftime("%Y-%m-%d")
            if date_iso == today:
                user = self._user(name)
                user["running"] = False
                user["segment_start"] = None
                user["elapsed_seconds"] = 0
                user["events"] = []
                user["seen_intervals"] = []
                user["updated_at"] = int(now.timestamp())
                self.store.save()
            return {"deleted": deleted, "date": date_iso}


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
    --border: rgba(15, 23, 42, 0.09);
    --text: #0f172a;
    --text-muted: #64748b;
    --accent: #03a9f4;
    --accent-soft: rgba(3, 169, 244, 0.14);
    --green: #16a34a;
    --green-soft: rgba(22, 163, 74, 0.12);
    --red: #dc2626;
    --red-soft: rgba(220, 38, 38, 0.11);
    --amber: #d97706;
    --amber-soft: rgba(217, 119, 6, 0.14);
    --btn-bg: #eef2f6;
    --btn-hover: #e2e8f0;
    --shadow: 0 1px 2px rgba(15, 23, 42, 0.04), 0 6px 20px rgba(15, 23, 42, 0.04);
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #111418;
      --surface: #1b2028;
      --surface-subtle: #222832;
      --border: rgba(255, 255, 255, 0.09);
      --text: #f1f5f9;
      --text-muted: #94a3b8;
      --accent: #38bdf8;
      --accent-soft: rgba(56, 189, 248, 0.16);
      --green: #4ade80;
      --green-soft: rgba(74, 222, 128, 0.14);
      --red: #f87171;
      --red-soft: rgba(248, 113, 113, 0.14);
      --amber: #fbbf24;
      --amber-soft: rgba(251, 191, 36, 0.16);
      --btn-bg: #262d38;
      --btn-hover: #313a48;
      --shadow: 0 1px 2px rgba(0, 0, 0, 0.25), 0 8px 24px rgba(0, 0, 0, 0.22);
    }
  }
  html.dark {
    --bg: #111418;
    --surface: #1b2028;
    --surface-subtle: #222832;
    --border: rgba(255, 255, 255, 0.09);
    --text: #f1f5f9;
    --text-muted: #94a3b8;
    --accent: #38bdf8;
    --accent-soft: rgba(56, 189, 248, 0.16);
    --green: #4ade80;
    --green-soft: rgba(74, 222, 128, 0.14);
    --red: #f87171;
    --red-soft: rgba(248, 113, 113, 0.14);
    --amber: #fbbf24;
    --amber-soft: rgba(251, 191, 36, 0.16);
    --btn-bg: #262d38;
    --btn-hover: #313a48;
    --shadow: 0 1px 2px rgba(0, 0, 0, 0.25), 0 8px 24px rgba(0, 0, 0, 0.22);
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    padding: 24px 20px 44px;
    font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: var(--bg);
    color: var(--text);
    line-height: 1.45;
  }
  .shell {
    max-width: 1240px;
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
    max-width: 46rem;
  }
  .topbar-meta {
    display: flex;
    align-items: center;
    gap: 10px;
    font-size: 0.8rem;
    color: var(--text-muted);
  }
  .users-stack {
    display: flex;
    flex-direction: column;
    gap: 28px;
  }
  .card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 16px;
    padding: 20px 22px;
    box-shadow: var(--shadow);
    display: flex;
    flex-direction: column;
    gap: 20px;
  }
  .card-head {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: 14px;
  }
  .head-left {
    display: flex;
    align-items: center;
    gap: 12px;
    flex-wrap: wrap;
  }
  .user-name {
    margin: 0;
    font-size: 1.2rem;
    font-weight: 700;
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
    padding: 14px 18px;
    border-radius: 12px;
    background: var(--surface-subtle);
    border: 1px solid var(--border);
  }
  .timer-label {
    font-size: 0.72rem;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--text-muted);
    margin: 0 0 2px;
  }
  .time {
    margin: 0;
    font-size: 2.2rem;
    font-weight: 700;
    line-height: 1.05;
    letter-spacing: -0.02em;
    font-variant-numeric: tabular-nums;
  }
  .actions {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px;
  }
  button {
    font: inherit;
    font-size: 0.84rem;
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
  button:disabled { opacity: 0.5; cursor: default; }
  button.primary {
    background: var(--green);
    color: #ffffff;
  }
  button.primary:hover:not(:disabled) { filter: brightness(1.06); background: var(--green); }
  button.pause {
    background: var(--amber-soft);
    color: var(--amber);
    border-color: rgba(217, 119, 6, 0.28);
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
  .section-title {
    margin: 0 0 12px;
    font-size: 0.82rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--text-muted);
  }

  /* Last 5 days panels */
  .five-days {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(210px, 1fr));
    gap: 12px;
  }
  @media (max-width: 480px) {
    body { padding: 14px 10px 32px; }
    .five-days { grid-template-columns: 1fr; }
  }
  .day-panel {
    display: flex;
    flex-direction: column;
    background: var(--surface-subtle);
    border: 1px solid var(--border);
    border-radius: 12px;
    overflow: hidden;
    min-height: 210px;
  }
  .day-panel.today {
    border-color: var(--accent);
    box-shadow: 0 0 0 1px var(--accent-soft);
  }
  .day-panel-head {
    padding: 10px 12px;
    border-bottom: 1px solid var(--border);
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 6px;
    background: var(--surface);
  }
  .day-title {
    font-size: 0.88rem;
    font-weight: 700;
    margin: 0;
  }
  .day-iso {
    font-size: 0.74rem;
    color: var(--text-muted);
    font-variant-numeric: tabular-nums;
  }
  .today-pill {
    font-size: 0.68rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    padding: 2px 7px;
    border-radius: 999px;
    background: var(--accent-soft);
    color: var(--accent);
  }
  .day-panel-body {
    flex: 1;
    padding: 8px 10px;
  }
  .tt-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 0.83rem;
    font-variant-numeric: tabular-nums;
  }
  .tt-table th {
    text-align: left;
    font-size: 0.7rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: var(--text-muted);
    padding: 4px 6px 6px;
    border-bottom: 1px solid var(--border);
  }
  .tt-table th.col-start { color: var(--green); }
  .tt-table th.col-stop { color: var(--red); }
  .tt-table th.col-dur, .tt-table td.col-dur { text-align: right; color: var(--text-muted); }
  .tt-table td {
    padding: 6px 6px;
    border-bottom: 1px solid var(--border);
  }
  .tt-table tr:last-child td { border-bottom: 0; }
  .t-pill {
    display: inline-block;
    padding: 2px 7px;
    border-radius: 6px;
    font-weight: 650;
    font-size: 0.8rem;
  }
  .t-start {
    background: var(--green-soft);
    color: var(--green);
  }
  .t-stop {
    background: var(--red-soft);
    color: var(--red);
  }
  .t-active {
    background: var(--green-soft);
    color: var(--green);
    font-style: italic;
  }
  .empty-day {
    padding: 22px 8px;
    text-align: center;
    font-size: 0.8rem;
    color: var(--text-muted);
  }
  .day-panel-foot {
    padding: 9px 12px;
    border-top: 1px solid var(--border);
    background: var(--surface);
    display: flex;
    align-items: center;
    justify-content: space-between;
    font-size: 0.82rem;
  }
  .foot-label {
    color: var(--text-muted);
    font-weight: 600;
  }
  .foot-total {
    font-weight: 750;
    font-variant-numeric: tabular-nums;
  }
  .foot-total.zero {
    color: var(--text-muted);
    font-weight: 500;
  }

  /* Interactive Calendar section */
  .calendar-grid-layout {
    display: grid;
    grid-template-columns: 1.25fr 1fr;
    gap: 16px;
    align-items: start;
  }
  @media (max-width: 820px) {
    .calendar-grid-layout { grid-template-columns: 1fr; }
  }
  .cal-panel {
    background: var(--surface-subtle);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 14px 16px;
  }
  .cal-header {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    margin-bottom: 12px;
  }
  .cal-pickers {
    display: flex;
    align-items: center;
    gap: 6px;
  }
  .cal-select {
    font: inherit;
    font-size: 0.88rem;
    font-weight: 650;
    color: var(--text);
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 9px;
    padding: 6px 10px;
    cursor: pointer;
    outline: none;
  }
  .cal-select:hover, .cal-select:focus {
    border-color: var(--accent);
  }
  .cal-nav {
    display: flex;
    align-items: center;
    gap: 6px;
  }
  .cal-nav button {
    padding: 5px 10px;
    font-size: 0.8rem;
  }
  .cal-weekdays {
    display: grid;
    grid-template-columns: repeat(7, minmax(0, 1fr));
    gap: 6px;
    margin-bottom: 6px;
  }
  .cal-weekday {
    text-align: center;
    font-size: 0.72rem;
    font-weight: 700;
    text-transform: uppercase;
    color: var(--text-muted);
    padding: 4px 0;
  }
  .cal-days {
    display: grid;
    grid-template-columns: repeat(7, minmax(0, 1fr));
    gap: 6px;
  }
  .cal-cell {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    min-height: 52px;
    padding: 5px 4px;
    border-radius: 10px;
    border: 1px solid transparent;
    background: var(--surface);
    color: var(--text);
    cursor: pointer;
    gap: 3px;
  }
  .cal-cell:hover {
    border-color: var(--accent);
    background: var(--surface);
  }
  .cal-cell.blank {
    background: transparent;
    border: 0;
    cursor: default;
    pointer-events: none;
  }
  .cal-cell.is-today {
    border-color: var(--accent);
  }
  .cal-cell.selected {
    background: var(--accent-soft);
    border-color: var(--accent);
  }
  .cal-day-num {
    font-size: 0.86rem;
    font-weight: 650;
    font-variant-numeric: tabular-nums;
  }
  .cal-day-meta {
    font-size: 0.68rem;
    font-weight: 650;
    font-variant-numeric: tabular-nums;
    color: var(--green);
    line-height: 1;
  }
  .cal-day-dot {
    width: 5px;
    height: 5px;
    border-radius: 50%;
    background: transparent;
  }
  .cal-cell.has-work .cal-day-dot {
    background: var(--green);
  }
  .cal-detail .day-panel {
    min-height: 280px;
  }
  .status-banner {
    padding: 16px 18px;
    border-radius: 12px;
    background: var(--surface);
    border: 1px solid var(--border);
    color: var(--text-muted);
  }
  .inline-notice {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: 10px 14px;
    padding: 11px 14px;
    border-radius: 11px;
    font-size: 0.84rem;
    background: var(--amber-soft);
    border: 1px solid rgba(217, 119, 6, 0.3);
    color: var(--text);
  }
  .inline-notice.error {
    background: var(--red-soft);
    border-color: rgba(220, 38, 38, 0.3);
  }
  .inline-notice-actions {
    display: flex;
    align-items: center;
    gap: 8px;
  }
  .inline-notice-actions button {
    padding: 5px 11px;
    font-size: 0.8rem;
  }
</style>
</head>
<body>
<div class="shell">
  <header class="topbar">
    <div>
      <h1>Work Timer</h1>
      <p class="subtitle">Last 5 days of work sessions and full calendar history. Start times are shown in green and stop times in red.</p>
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
let serverToday = "";
const calState = {};
const pendingReset = {};
const pendingClearDay = {};
const cardNotices = {};

function fmtHMS(totalSec) {
  const s = Math.max(0, Math.floor(totalSec));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  return String(h).padStart(2, "0") + ":" + String(m).padStart(2, "0") + ":" + String(sec).padStart(2, "0");
}

function fmtHoursMins(totalSec) {
  const m = Math.max(0, Math.floor(totalSec / 60));
  const h = Math.floor(m / 60);
  const rem = m % 60;
  return h + "h " + String(rem).padStart(2, "0") + "m";
}

function fmtCompactHM(totalSec) {
  const m = Math.max(0, Math.floor(totalSec / 60));
  if (m <= 0) return "";
  const h = Math.floor(m / 60);
  const rem = m % 60;
  return h > 0 ? (h + "h" + (rem ? " " + rem + "m" : "")) : (rem + "m");
}

function fmtShortDate(iso) {
  const d = new Date(iso + "T00:00:00");
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
}

function fmtFullDate(iso) {
  const d = new Date(iso + "T00:00:00");
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleDateString(undefined, { weekday: "long", month: "short", day: "numeric", year: "numeric" });
}

function fmtMonthYear(year, month) {
  const d = new Date(year, month - 1, 1);
  return d.toLocaleDateString(undefined, { month: "long", year: "numeric" });
}

function liveSeconds(user) {
  let total = Number(user.elapsed_seconds || 0);
  if (user.running && typeof user.segment_start === "number") {
    const nowSec = Math.floor(Date.now() / 1000) + serverOffsetSec;
    total += Math.max(0, nowSec - user.segment_start);
  }
  return total;
}

function liveRunningSegmentSeconds(user) {
  if (!user.running || typeof user.segment_start !== "number") return 0;
  const nowSec = Math.floor(Date.now() / 1000) + serverOffsetSec;
  return Math.max(0, nowSec - user.segment_start);
}

function effectiveDayTotalSeconds(dayData, user) {
  let total = 0;
  for (const iv of (dayData && dayData.intervals) || []) {
    total += iv.running && user ? liveRunningSegmentSeconds(user) : Number(iv.seconds || 0);
  }
  return total;
}

function getCalState(userName) {
  if (!calState[userName]) {
    const base = serverToday ? new Date(serverToday + "T00:00:00") : new Date();
    calState[userName] = {
      year: base.getFullYear(),
      month: base.getMonth() + 1,
      selectedDate: serverToday || base.toISOString().slice(0, 10),
      days: null,
      loading: false,
      loadedKey: ""
    };
  }
  return calState[userName];
}

async function sendCommand(userName, command, button) {
  if (button) button.disabled = true;
  delete cardNotices[userName];
  if (command === "reset") {
    pendingReset[userName] = false;
  }
  try {
    const resp = await fetch("api/command", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({user: userName, command})
    });
    if (!resp.ok) {
      const errData = await resp.json().catch(() => ({}));
      cardNotices[userName] = (errData && errData.error) || "Command failed.";
    }
  } catch (err) {
    cardNotices[userName] = "Could not reach the add-on.";
  } finally {
    const st = getCalState(userName);
    st.loadedKey = "";
    await load();
  }
}

async function clearDayData(userName, dateIso, button) {
  if (button) button.disabled = true;
  delete cardNotices[userName];
  delete pendingClearDay[userName];
  try {
    const resp = await fetch("api/clear-day", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({user: userName, date: dateIso})
    });
    if (!resp.ok) {
      const errData = await resp.json().catch(() => ({}));
      cardNotices[userName] = (errData && errData.error) || "Could not clear day data.";
    }
  } catch (err) {
    cardNotices[userName] = "Could not reach the add-on.";
  } finally {
    const st = getCalState(userName);
    st.loadedKey = "";
    await load();
  }
}

async function loadCalendarMonth(userName, force) {
  const st = getCalState(userName);
  const key = st.year + "-" + st.month;
  if (!force && st.loadedKey === key && st.days) {
    renderCalendarArea(userName);
    return;
  }
  st.loading = true;
  renderCalendarArea(userName);
  try {
    const url = "api/calendar?user=" + encodeURIComponent(userName) + "&year=" + st.year + "&month=" + st.month;
    const resp = await fetch(url);
    const data = await resp.json();
    if (data && data.days) {
      st.days = data.days;
      st.loadedKey = key;
    }
  } catch (err) {
    // Keep existing view if offline
  } finally {
    st.loading = false;
    renderCalendarArea(userName);
  }
}

function buildDayPanel(dayData, user, options) {
  const opts = options || {};
  const iso = dayData.date;
  const isToday = iso === serverToday;
  const panel = document.createElement("div");
  panel.className = "day-panel" + (isToday ? " today" : "");

  // Date header on top
  const head = document.createElement("div");
  head.className = "day-panel-head";
  const headText = document.createElement("div");
  const title = document.createElement("div");
  title.className = "day-title";
  title.textContent = opts.fullDate ? fmtFullDate(iso) : fmtShortDate(iso);
  const sub = document.createElement("div");
  sub.className = "day-iso";
  sub.textContent = iso;
  headText.append(title, sub);
  head.appendChild(headText);
  if (isToday) {
    const pill = document.createElement("span");
    pill.className = "today-pill";
    pill.textContent = "Today";
    head.appendChild(pill);
  }

  // Timetable underneath
  const body = document.createElement("div");
  body.className = "day-panel-body";
  const intervals = dayData.intervals || [];
  if (!intervals.length) {
    const empty = document.createElement("div");
    empty.className = "empty-day";
    empty.textContent = "No sessions";
    body.appendChild(empty);
  } else {
    const table = document.createElement("table");
    table.className = "tt-table";
    const thead = document.createElement("thead");
    const hrow = document.createElement("tr");
    const thStart = document.createElement("th");
    thStart.className = "col-start";
    thStart.textContent = "Start";
    const thStop = document.createElement("th");
    thStop.className = "col-stop";
    thStop.textContent = "Stop";
    hrow.append(thStart, thStop);
    if (opts.showDuration) {
      const thDur = document.createElement("th");
      thDur.className = "col-dur";
      thDur.textContent = "Duration";
      hrow.appendChild(thDur);
    }
    thead.appendChild(hrow);

    const tbody = document.createElement("tbody");
    for (const iv of intervals) {
      const tr = document.createElement("tr");
      const tdStart = document.createElement("td");
      const spStart = document.createElement("span");
      spStart.className = "t-pill t-start";
      spStart.textContent = iv.start || "--:--";
      tdStart.appendChild(spStart);

      const tdStop = document.createElement("td");
      const spStop = document.createElement("span");
      if (iv.running) {
        spStop.className = "t-pill t-active";
        spStop.textContent = "Running";
      } else {
        spStop.className = "t-pill t-stop";
        spStop.textContent = iv.end || "--:--";
      }
      tdStop.appendChild(spStop);
      tr.append(tdStart, tdStop);

      if (opts.showDuration) {
        const tdDur = document.createElement("td");
        tdDur.className = "col-dur";
        const secs = iv.running && user ? liveRunningSegmentSeconds(user) : Number(iv.seconds || 0);
        tdDur.textContent = fmtHoursMins(secs);
        tr.appendChild(tdDur);
      }
      tbody.appendChild(tr);
    }
    table.append(thead, tbody);
    body.appendChild(table);
  }

  // Total hours & minutes footer underneath timetable
  const foot = document.createElement("div");
  foot.className = "day-panel-foot";
  const footLbl = document.createElement("span");
  footLbl.className = "foot-label";
  footLbl.textContent = "Total";
  const totalSec = effectiveDayTotalSeconds(dayData, user);
  const footRight = document.createElement("div");
  footRight.className = "actions";
  const footVal = document.createElement("span");
  footVal.className = "foot-total" + (totalSec <= 0 ? " zero" : "");
  footVal.textContent = fmtHoursMins(totalSec);
  if (isToday && user) {
    footVal.dataset.dayTotal = user.name;
  }
  footRight.appendChild(footVal);

  const canClear = Boolean(
    opts.allowClear &&
    user &&
    (intervals.length > 0 || (isToday && (user.running || Number(user.elapsed_seconds || 0) > 0)))
  );
  if (canClear && pendingClearDay[user.name] !== iso) {
    const clearDayBtn = document.createElement("button");
    clearDayBtn.type = "button";
    clearDayBtn.className = "danger";
    clearDayBtn.style.padding = "4px 10px";
    clearDayBtn.style.fontSize = "0.78rem";
    clearDayBtn.textContent = "Clear day";
    clearDayBtn.addEventListener("click", () => {
      pendingClearDay[user.name] = iso;
      renderCalendarArea(user.name);
    });
    footRight.appendChild(clearDayBtn);
  }
  foot.append(footLbl, footRight);

  panel.append(head, body);

  if (opts.allowClear && user && pendingClearDay[user.name] === iso) {
    const confirmBar = document.createElement("div");
    confirmBar.className = "inline-notice";
    confirmBar.style.margin = "0 10px 10px";
    const msg = document.createElement("span");
    msg.textContent = "Clear all data for " + iso + "? Calendar entries for this day will be deleted.";
    const btns = document.createElement("div");
    btns.className = "inline-notice-actions";
    const yesBtn = document.createElement("button");
    yesBtn.type = "button";
    yesBtn.className = "danger";
    yesBtn.textContent = "Confirm clear";
    yesBtn.addEventListener("click", () => clearDayData(user.name, iso, yesBtn));
    const noBtn = document.createElement("button");
    noBtn.type = "button";
    noBtn.className = "ghost";
    noBtn.textContent = "Cancel";
    noBtn.addEventListener("click", () => {
      delete pendingClearDay[user.name];
      renderCalendarArea(user.name);
    });
    btns.append(yesBtn, noBtn);
    confirmBar.append(msg, btns);
    panel.appendChild(confirmBar);
  }

  panel.appendChild(foot);
  return panel;
}

function renderCalendarArea(userName) {
  const container = document.querySelector('[data-cal-area="' + CSS.escape(userName) + '"]');
  if (!container) return;
  const user = currentUsers.find((u) => u.name === userName);
  const st = getCalState(userName);

  container.innerHTML = "";

  // Left: Month calendar
  const calPanel = document.createElement("div");
  calPanel.className = "cal-panel";

  const calHead = document.createElement("div");
  calHead.className = "cal-header";

  const pickers = document.createElement("div");
  pickers.className = "cal-pickers";

  const monthSelect = document.createElement("select");
  monthSelect.className = "cal-select";
  monthSelect.setAttribute("aria-label", "Month");
  for (let m = 1; m <= 12; m++) {
    const opt = document.createElement("option");
    opt.value = String(m);
    opt.textContent = new Date(2000, m - 1, 1).toLocaleDateString(undefined, { month: "long" });
    if (m === st.month) opt.selected = true;
    monthSelect.appendChild(opt);
  }
  monthSelect.addEventListener("change", () => {
    st.month = parseInt(monthSelect.value, 10);
    loadCalendarMonth(userName, true);
  });

  const yearSelect = document.createElement("select");
  yearSelect.className = "cal-select";
  yearSelect.setAttribute("aria-label", "Year");
  const curYear = serverToday ? parseInt(serverToday.slice(0, 4), 10) : new Date().getFullYear();
  const minYear = Math.min(curYear - 5, st.year - 1);
  const maxYear = Math.max(curYear + 1, st.year + 1);
  for (let y = maxYear; y >= minYear; y--) {
    const opt = document.createElement("option");
    opt.value = String(y);
    opt.textContent = String(y);
    if (y === st.year) opt.selected = true;
    yearSelect.appendChild(opt);
  }
  yearSelect.addEventListener("change", () => {
    st.year = parseInt(yearSelect.value, 10);
    loadCalendarMonth(userName, true);
  });

  pickers.append(monthSelect, yearSelect);

  const nav = document.createElement("div");
  nav.className = "cal-nav";
  const prevBtn = document.createElement("button");
  prevBtn.type = "button";
  prevBtn.className = "ghost";
  prevBtn.textContent = "‹";
  prevBtn.title = "Previous month";
  prevBtn.addEventListener("click", () => {
    st.month -= 1;
    if (st.month < 1) { st.month = 12; st.year -= 1; }
    loadCalendarMonth(userName, true);
  });

  const todayBtn = document.createElement("button");
  todayBtn.type = "button";
  todayBtn.className = "ghost";
  todayBtn.textContent = "Today";
  todayBtn.addEventListener("click", () => {
    const base = serverToday ? new Date(serverToday + "T00:00:00") : new Date();
    st.year = base.getFullYear();
    st.month = base.getMonth() + 1;
    st.selectedDate = serverToday || base.toISOString().slice(0, 10);
    loadCalendarMonth(userName, true);
  });

  const nextBtn = document.createElement("button");
  nextBtn.type = "button";
  nextBtn.className = "ghost";
  nextBtn.textContent = "›";
  nextBtn.title = "Next month";
  nextBtn.addEventListener("click", () => {
    st.month += 1;
    if (st.month > 12) { st.month = 1; st.year += 1; }
    loadCalendarMonth(userName, true);
  });

  nav.append(prevBtn, todayBtn, nextBtn);
  calHead.append(pickers, nav);

  const weekdays = document.createElement("div");
  weekdays.className = "cal-weekdays";
  for (const wd of ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]) {
    const el = document.createElement("div");
    el.className = "cal-weekday";
    el.textContent = wd;
    weekdays.appendChild(el);
  }

  const daysGrid = document.createElement("div");
  daysGrid.className = "cal-days";
  const firstDay = new Date(st.year, st.month - 1, 1);
  const startOffset = (firstDay.getDay() + 6) % 7; // Monday = 0
  const daysInMonth = new Date(st.year, st.month, 0).getDate();

  for (let i = 0; i < startOffset; i++) {
    const blank = document.createElement("div");
    blank.className = "cal-cell blank";
    daysGrid.appendChild(blank);
  }

  for (let dayNum = 1; dayNum <= daysInMonth; dayNum++) {
    const iso = st.year + "-" + String(st.month).padStart(2, "0") + "-" + String(dayNum).padStart(2, "0");
    const dayObj = st.days && st.days[iso] ? st.days[iso] : { date: iso, intervals: [], total_seconds: 0 };
    const totalSec = effectiveDayTotalSeconds(dayObj, user);
    const hasWork = (dayObj.intervals && dayObj.intervals.length > 0) || totalSec > 0;

    const cell = document.createElement("button");
    cell.type = "button";
    cell.className = "cal-cell"
      + (iso === serverToday ? " is-today" : "")
      + (iso === st.selectedDate ? " selected" : "")
      + (hasWork ? " has-work" : "");
    cell.addEventListener("click", () => {
      st.selectedDate = iso;
      delete pendingClearDay[userName];
      renderCalendarArea(userName);
    });

    const num = document.createElement("span");
    num.className = "cal-day-num";
    num.textContent = String(dayNum);

    if (hasWork) {
      const meta = document.createElement("span");
      meta.className = "cal-day-meta";
      meta.textContent = fmtCompactHM(totalSec) || "•";
      cell.append(num, meta);
    } else {
      const dot = document.createElement("span");
      dot.className = "cal-day-dot";
      cell.append(num, dot);
    }
    daysGrid.appendChild(cell);
  }

  calPanel.append(calHead, weekdays, daysGrid);

  // Right: Selected day timetable
  const detailWrap = document.createElement("div");
  detailWrap.className = "cal-detail";
  const selectedIso = st.selectedDate;
  let selectedDay = (st.days && st.days[selectedIso]) || null;
  if (!selectedDay && user && user.recent_days) {
    selectedDay = user.recent_days.find((d) => d.date === selectedIso) || null;
  }
  if (!selectedDay) {
    selectedDay = { date: selectedIso, intervals: [], total_seconds: 0 };
  }
  detailWrap.appendChild(buildDayPanel(selectedDay, user, { fullDate: true, showDuration: true, allowClear: true }));

  container.append(calPanel, detailWrap);
}

function render(users) {
  const root = document.getElementById("users");
  root.className = "users-stack";
  root.innerHTML = "";
  if (!users.length) {
    root.className = "status-banner";
    root.textContent = "No users configured.";
    return;
  }

  for (const user of users) {
    const card = document.createElement("article");
    card.className = "card";

    // Header
    const head = document.createElement("div");
    head.className = "card-head";
    const headLeft = document.createElement("div");
    headLeft.className = "head-left";
    const title = document.createElement("h2");
    title.className = "user-name";
    title.textContent = user.name;
    const badge = document.createElement("span");
    badge.className = "badge" + (user.running ? " running" : "");
    const dot = document.createElement("span");
    dot.className = "dot";
    badge.append(dot, document.createTextNode(user.running ? "Running" : "Stopped"));
    headLeft.append(title, badge);

    const headTools = document.createElement("div");
    headTools.className = "actions";
    const clearPanelBtn = document.createElement("button");
    clearPanelBtn.type = "button";
    clearPanelBtn.className = "ghost";
    clearPanelBtn.textContent = "Clear panel screen";
    clearPanelBtn.addEventListener("click", () => sendCommand(user.name, "clear", clearPanelBtn));
    headTools.append(clearPanelBtn);
    head.append(headLeft, headTools);

    // Live timer + controls
    const hero = document.createElement("div");
    hero.className = "hero";
    const timerBox = document.createElement("div");
    const timerLabel = document.createElement("p");
    timerLabel.className = "timer-label";
    timerLabel.textContent = "Today's timer";
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
    toggleBtn.textContent = user.running ? "Stop / Pause" : "Start";
    toggleBtn.addEventListener("click", () => sendCommand(user.name, user.running ? "pause" : "start", toggleBtn));

    const resetBtn = document.createElement("button");
    resetBtn.type = "button";
    resetBtn.className = "danger";
    resetBtn.textContent = "Reset";
    resetBtn.disabled = !user.running && secNow <= 0;
    resetBtn.addEventListener("click", () => {
      pendingReset[user.name] = true;
      render(currentUsers);
    });
    actions.append(toggleBtn, resetBtn);
    hero.append(timerBox, actions);

    card.append(head, hero);

    if (pendingReset[user.name]) {
      const warn = document.createElement("div");
      warn.className = "inline-notice";
      const msg = document.createElement("span");
      msg.textContent = "Reset today's timer to 00:00? Completed calendar entries will be kept.";
      const warnBtns = document.createElement("div");
      warnBtns.className = "inline-notice-actions";
      const confirmBtn = document.createElement("button");
      confirmBtn.type = "button";
      confirmBtn.className = "danger";
      confirmBtn.textContent = "Confirm reset";
      confirmBtn.addEventListener("click", () => sendCommand(user.name, "reset", confirmBtn));
      const cancelBtn = document.createElement("button");
      cancelBtn.type = "button";
      cancelBtn.className = "ghost";
      cancelBtn.textContent = "Cancel";
      cancelBtn.addEventListener("click", () => {
        pendingReset[user.name] = false;
        render(currentUsers);
      });
      warnBtns.append(confirmBtn, cancelBtn);
      warn.append(msg, warnBtns);
      card.appendChild(warn);
    }

    if (cardNotices[user.name]) {
      const errBar = document.createElement("div");
      errBar.className = "inline-notice error";
      const errMsg = document.createElement("span");
      errMsg.textContent = cardNotices[user.name];
      const dismissBtn = document.createElement("button");
      dismissBtn.type = "button";
      dismissBtn.className = "ghost";
      dismissBtn.textContent = "Dismiss";
      dismissBtn.addEventListener("click", () => {
        delete cardNotices[user.name];
        render(currentUsers);
      });
      errBar.append(errMsg, dismissBtn);
      card.appendChild(errBar);
    }

    // Last 5 days section: except today, show only days with timestamp data.
    // If no days in the last 5 have any timestamp data, hide the section completely.
    const recentDays = user.recent_days || [];
    const hasAnyData = recentDays.some((d) => d.intervals && d.intervals.length > 0);
    const visibleDays = hasAnyData
      ? recentDays.filter((d) => d.date === serverToday || (d.intervals && d.intervals.length > 0))
      : [];

    if (visibleDays.length) {
      const fiveSection = document.createElement("section");
      const fiveHeading = document.createElement("h3");
      fiveHeading.className = "section-title";
      fiveHeading.textContent = "Last 5 days";
      const fiveGrid = document.createElement("div");
      fiveGrid.className = "five-days";
      for (const dayData of visibleDays) {
        fiveGrid.appendChild(buildDayPanel(dayData, user, { fullDate: false, showDuration: false }));
      }
      fiveSection.append(fiveHeading, fiveGrid);
      card.appendChild(fiveSection);
    }

    // Calendar section underneath
    const calSection = document.createElement("section");
    const calHeading = document.createElement("h3");
    calHeading.className = "section-title";
    calHeading.textContent = "Calendar";
    const calArea = document.createElement("div");
    calArea.className = "calendar-grid-layout";
    calArea.dataset.calArea = user.name;
    calSection.append(calHeading, calArea);

    card.appendChild(calSection);
    root.appendChild(card);

    loadCalendarMonth(user.name, false);
  }
}

function tick() {
  for (const user of currentUsers) {
    if (!user.running) continue;
    const el = document.querySelector('[data-timer="' + CSS.escape(user.name) + '"]');
    if (el) el.textContent = fmtHMS(liveSeconds(user));
    const todayData = (user.recent_days || []).find((d) => d.date === serverToday);
    if (todayData) {
      const liveTot = fmtHoursMins(effectiveDayTotalSeconds(todayData, user));
      document.querySelectorAll('[data-day-total="' + CSS.escape(user.name) + '"]').forEach((node) => {
        node.textContent = liveTot;
      });
    }
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
  if (data.today) {
    serverToday = data.today;
  }
  currentUsers = data.users || [];
  render(currentUsers);
  synced.textContent = "Updated " + new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

document.getElementById("refreshBtn").addEventListener("click", () => {
  for (const key of Object.keys(calState)) {
    calState[key].loadedKey = "";
  }
  load();
});
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
            if path == "/api/calendar":
                if not (self._ingress() or self._authorized()):
                    self._send(401, {"error": "unauthorized"})
                    return
                query = parse_qs(urlparse(self.path).query)
                name = (query.get("user") or ["panel"])[0]
                now = app._now()
                try:
                    year = int((query.get("year") or [str(now.year)])[0])
                    month = int((query.get("month") or [str(now.month)])[0])
                except ValueError:
                    self._send(400, {"error": "invalid year or month"})
                    return
                try:
                    self._send(200, app.month_calendar(name, year, month))
                except KeyError:
                    self._send(404, {"error": f"no user {name}"})
                except ValueError as err:
                    self._send(400, {"error": str(err)})
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
            if path == "/api/clear-day":
                if not (self._ingress() or self._authorized()):
                    self._send(401, {"error": "unauthorized"})
                    return
                payload = self._read_json()
                if payload is None:
                    self._send(400, {"error": "invalid json"})
                    return
                name = str(payload.get("user") or "panel")
                date_iso = str(payload.get("date") or "").strip()
                try:
                    result = app.clear_day(name, date_iso)
                except KeyError:
                    self._send(404, {"error": f"no user {name}"})
                    return
                except ValueError as err:
                    self._send(400, {"error": str(err)})
                    return
                except (OSError, RuntimeError) as err:
                    self._send(502, {"error": str(err)})
                    return
                self._send(200, result)
                return
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
