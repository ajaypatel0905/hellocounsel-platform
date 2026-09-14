"""SQLite persistence. One writer, JSON columns, no ORM. Enough for a working slice."""
from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import asdict, fields
from datetime import datetime
from typing import Any, Iterable

from .models import (Call, Contact, Engagement, EngagementStatus, Event,
                     Intervention, InterventionKind, Matter, Run, Trigger, Wakeup)

SCHEMA = """
CREATE TABLE IF NOT EXISTS matters (id TEXT PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS contacts (id TEXT PRIMARY KEY, matter_id TEXT, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS engagements (
  id TEXT PRIMARY KEY, matter_id TEXT, contact_id TEXT, status TEXT, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events (
  id TEXT PRIMARY KEY, engagement_id TEXT, ts TEXT, type TEXT, data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_events_eng ON events(engagement_id, ts);
CREATE INDEX IF NOT EXISTS ix_events_type ON events(type, ts);
CREATE TABLE IF NOT EXISTS wakeups (
  id TEXT PRIMARY KEY, engagement_id TEXT, due_at TEXT, consumed_at TEXT, data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_wakeups_due ON wakeups(consumed_at, due_at);
CREATE TABLE IF NOT EXISTS interventions (
  id TEXT PRIMARY KEY, engagement_id TEXT, status TEXT, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, engagement_id TEXT, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS calls (id TEXT PRIMARY KEY, engagement_id TEXT, provider_sid TEXT, data TEXT NOT NULL);
"""

DT_FIELDS = {"created_at", "updated_at", "completed_at", "next_wake_at", "ts", "due_at",
             "consumed_at", "resolved_at", "started_at", "finished_at", "ended_at"}


def _dump(obj) -> str:
    d = asdict(obj)
    for k, v in d.items():
        if isinstance(v, datetime):
            d[k] = v.isoformat()
    return json.dumps(d, default=str)


def _load(cls, raw: str):
    d = json.loads(raw)
    for f in fields(cls):
        if f.name in DT_FIELDS and d.get(f.name):
            d[f.name] = datetime.fromisoformat(d[f.name])
    if cls is Engagement:
        d["status"] = EngagementStatus(d["status"])
    if cls in (Wakeup, Run):
        d["trigger"] = Trigger(d["trigger"])
    if cls is Intervention:
        d["kind"] = InterventionKind(d["kind"])
    return cls(**d)


class Store:
    def __init__(self, path: str = ":memory:"):
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL") if path != ":memory:" else None
        self._lock = threading.RLock()
        with self._lock:
            self._conn.executescript(SCHEMA)

    # ---- generic helpers -------------------------------------------------
    def _one(self, sql: str, args: Iterable = ()) -> sqlite3.Row | None:
        with self._lock:
            cur = self._conn.execute(sql, tuple(args))
            return cur.fetchone()

    def _all(self, sql: str, args: Iterable = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, tuple(args)).fetchall()

    def _exec(self, sql: str, args: Iterable = ()) -> None:
        with self._lock:
            self._conn.execute(sql, tuple(args))

    # ---- matters / contacts ---------------------------------------------
    def put_matter(self, m: Matter) -> Matter:
        self._exec("INSERT OR REPLACE INTO matters VALUES (?,?)", (m.id, _dump(m)))
        return m

    def get_matter(self, id: str) -> Matter | None:
        r = self._one("SELECT data FROM matters WHERE id=?", (id,))
        return _load(Matter, r[0]) if r else None

    def list_matters(self) -> list[Matter]:
        return sorted((_load(Matter, r[0]) for r in self._all("SELECT data FROM matters")), key=lambda m: m.client_name)

    def put_contact(self, c: Contact) -> Contact:
        self._exec("INSERT OR REPLACE INTO contacts VALUES (?,?,?)", (c.id, c.matter_id, _dump(c)))
        return c

    def get_contact(self, id: str) -> Contact | None:
        r = self._one("SELECT data FROM contacts WHERE id=?", (id,))
        return _load(Contact, r[0]) if r else None

    def list_contacts(self, matter_id: str | None = None) -> list[Contact]:
        if matter_id:
            rows = self._all("SELECT data FROM contacts WHERE matter_id=? ORDER BY id", (matter_id,))
        else:
            rows = self._all("SELECT data FROM contacts ORDER BY id")
        return [_load(Contact, r[0]) for r in rows]

    def find_contact_by_address(self, address: str) -> list[Contact]:
        a = address.strip().lower()
        return [c for c in self.list_contacts() if a in (c.phone.lower(), c.email.lower())]

    # ---- engagements -----------------------------------------------------
    def put_engagement(self, e: Engagement) -> Engagement:
        self._exec("INSERT OR REPLACE INTO engagements VALUES (?,?,?,?,?)",
                   (e.id, e.matter_id, e.contact_id, e.status.value, _dump(e)))
        return e

    def get_engagement(self, id: str) -> Engagement | None:
        r = self._one("SELECT data FROM engagements WHERE id=?", (id,))
        return _load(Engagement, r[0]) if r else None

    def list_engagements(self, matter_id: str | None = None, contact_id: str | None = None,
                         status: Iterable[EngagementStatus] | None = None) -> list[Engagement]:
        sql, args = "SELECT data FROM engagements WHERE 1=1", []
        if matter_id:
            sql += " AND matter_id=?"; args.append(matter_id)
        if contact_id:
            sql += " AND contact_id=?"; args.append(contact_id)
        if status:
            st = [s.value for s in status]
            sql += f" AND status IN ({','.join('?' * len(st))})"; args += st
        return [_load(Engagement, r[0]) for r in self._all(sql + " ORDER BY id", args)]

    # ---- events ----------------------------------------------------------
    def add_event(self, ev: Event) -> Event:
        self._exec("INSERT INTO events VALUES (?,?,?,?,?)",
                   (ev.id, ev.engagement_id, ev.ts.isoformat(), ev.type, _dump(ev)))
        return ev

    def list_events(self, engagement_id: str, limit: int | None = None) -> list[Event]:
        sql = "SELECT data FROM events WHERE engagement_id=? ORDER BY ts, rowid"
        rows = self._all(sql, (engagement_id,))
        evs = [_load(Event, r[0]) for r in rows]
        return evs[-limit:] if limit else evs

    def list_events_by_type(self, type: str, limit: int = 100) -> list[Event]:
        rows = self._all("SELECT data FROM events WHERE type=? ORDER BY ts DESC, rowid DESC LIMIT ?",
                         (type, limit))
        return [_load(Event, r[0]) for r in rows]

    # ---- wakeups ---------------------------------------------------------
    def add_wakeup(self, w: Wakeup) -> Wakeup:
        self._exec("INSERT INTO wakeups VALUES (?,?,?,?,?)",
                   (w.id, w.engagement_id, w.due_at.isoformat(), None, _dump(w)))
        return w

    def claim_due_wakeups(self, now: datetime, limit: int = 50) -> list[Wakeup]:
        """Atomically mark due wakeups consumed and return them. A retry never runs one twice."""
        with self._lock:
            rows = self._all(
                "SELECT id, data FROM wakeups WHERE consumed_at IS NULL AND due_at<=? ORDER BY due_at LIMIT ?",
                (now.isoformat(), limit))
            out = []
            for id_, data in rows:
                self._exec("UPDATE wakeups SET consumed_at=? WHERE id=?", (now.isoformat(), id_))
                w = _load(Wakeup, data); w.consumed_at = now
                self._exec("UPDATE wakeups SET data=? WHERE id=?", (_dump(w), id_))
                out.append(w)
            return out

    def cancel_pending_wakeups(self, engagement_id: str, now: datetime) -> int:
        with self._lock:
            rows = self._all("SELECT id, data FROM wakeups WHERE engagement_id=? AND consumed_at IS NULL",
                             (engagement_id,))
            for id_, data in rows:
                w = _load(Wakeup, data); w.consumed_at = now; w.payload["cancelled"] = True
                self._exec("UPDATE wakeups SET consumed_at=?, data=? WHERE id=?",
                           (now.isoformat(), _dump(w), id_))
            return len(rows)

    def next_pending_wakeup(self, engagement_id: str) -> Wakeup | None:
        r = self._one("SELECT data FROM wakeups WHERE engagement_id=? AND consumed_at IS NULL ORDER BY due_at LIMIT 1",
                      (engagement_id,))
        return _load(Wakeup, r[0]) if r else None

    def count_pending_wakeups(self) -> int:
        return self._one("SELECT COUNT(*) FROM wakeups WHERE consumed_at IS NULL")[0]

    # ---- interventions ---------------------------------------------------
    def put_intervention(self, i: Intervention) -> Intervention:
        self._exec("INSERT OR REPLACE INTO interventions VALUES (?,?,?,?)",
                   (i.id, i.engagement_id, i.status, _dump(i)))
        return i

    def get_intervention(self, id: str) -> Intervention | None:
        r = self._one("SELECT data FROM interventions WHERE id=?", (id,))
        return _load(Intervention, r[0]) if r else None

    def list_interventions(self, status: str | None = None, engagement_id: str | None = None) -> list[Intervention]:
        sql, args = "SELECT data FROM interventions WHERE 1=1", []
        if status:
            sql += " AND status=?"; args.append(status)
        if engagement_id:
            sql += " AND engagement_id=?"; args.append(engagement_id)
        return [_load(Intervention, r[0]) for r in self._all(sql + " ORDER BY rowid", args)]

    # ---- runs / calls ----------------------------------------------------
    def put_run(self, r: Run) -> Run:
        self._exec("INSERT OR REPLACE INTO runs VALUES (?,?,?)", (r.id, r.engagement_id, _dump(r)))
        return r

    def list_runs(self, engagement_id: str) -> list[Run]:
        return [_load(Run, r[0]) for r in self._all("SELECT data FROM runs WHERE engagement_id=? ORDER BY rowid", (engagement_id,))]

    def put_call(self, c: Call) -> Call:
        self._exec("INSERT OR REPLACE INTO calls VALUES (?,?,?,?)", (c.id, c.engagement_id, c.provider_sid, _dump(c)))
        return c

    def get_call(self, id: str) -> Call | None:
        r = self._one("SELECT data FROM calls WHERE id=?", (id,))
        return _load(Call, r[0]) if r else None

    def get_call_by_sid(self, sid: str) -> Call | None:
        r = self._one("SELECT data FROM calls WHERE provider_sid=?", (sid,))
        return _load(Call, r[0]) if r else None

    def list_calls(self, engagement_id: str) -> list[Call]:
        return [_load(Call, r[0]) for r in self._all("SELECT data FROM calls WHERE engagement_id=? ORDER BY rowid", (engagement_id,))]
