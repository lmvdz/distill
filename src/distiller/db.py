"""The work queue and card store, in one SQLite file.

A video moves `new -> leased -> dropped | carded | failed`. A worker takes a lease; if it dies,
the lease expires and the video goes back to the pool. Every write is one short transaction, so
many workers share the file safely under WAL.
"""

import json
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from .discovery import (
    FINISHED_STATUSES,
    SourceStats,
    card_depth,
    is_paused,
    refill_multiplier,
    smoothed_yield,
    video_value,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
  id TEXT PRIMARY KEY,
  url TEXT NOT NULL,
  site TEXT NOT NULL,
  title TEXT,
  duration REAL,
  seed TEXT,
  status TEXT NOT NULL DEFAULT 'new',
  attempts INTEGER NOT NULL DEFAULT 0,
  lease_until REAL,
  error TEXT,
  screen TEXT,
  upload_date TEXT,
  channel TEXT,
  view_count INTEGER,
  priority REAL,
  created REAL NOT NULL,
  updated REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS videos_status ON videos(status);
CREATE TABLE IF NOT EXISTS cards (
  id INTEGER PRIMARY KEY,
  video_id TEXT NOT NULL REFERENCES videos(id),
  tool TEXT NOT NULL,
  topic TEXT NOT NULL,
  technique TEXT NOT NULL,
  body TEXT NOT NULL,
  created REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS cards_group ON cards(tool, topic, technique);
CREATE TABLE IF NOT EXISTS memo (
  video_id TEXT NOT NULL,
  key TEXT NOT NULL,
  value TEXT NOT NULL,
  PRIMARY KEY (video_id, key)
);
CREATE TABLE IF NOT EXISTS compositions (
  id INTEGER PRIMARY KEY,
  video_id TEXT NOT NULL REFERENCES videos(id),
  kind TEXT NOT NULL,
  body TEXT NOT NULL,
  created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS auto_seeds (
  query TEXT PRIMARY KEY,
  video_id TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'search',
  created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS workflow_cards (
  id INTEGER PRIMARY KEY,
  video_id TEXT NOT NULL REFERENCES videos(id),
  goal TEXT NOT NULL,
  body TEXT NOT NULL,
  created REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS workflow_cards_video ON workflow_cards(video_id);
CREATE TABLE IF NOT EXISTS merges (
  group_key TEXT PRIMARY KEY,
  cards_hash TEXT NOT NULL,
  markdown TEXT NOT NULL,
  updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS audits (
  card_id INTEGER PRIMARY KEY REFERENCES cards(id),
  card_hash TEXT NOT NULL,
  model TEXT NOT NULL,
  body TEXT NOT NULL,
  updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS styles (
  source_id TEXT PRIMARY KEY,
  author TEXT,
  author_url TEXT,
  home TEXT,
  credit TEXT NOT NULL,
  markdown TEXT NOT NULL,
  screenshots TEXT NOT NULL,
  updated REAL NOT NULL
);
"""


@lru_cache(maxsize=16)
def _compiled(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.IGNORECASE)


def _regexp(pattern: str, value: str | None) -> bool:
    return value is not None and _compiled(pattern).search(value) is not None


# `--only <regex>`: a video is selected when its seed, channel or url matches.
ONLY_SQL = "(seed REGEXP ? OR channel REGEXP ? OR url REGEXP ?)"


def _only(only: str | None) -> tuple[str, tuple[str, ...]]:
    """An `AND ...` clause (and its arguments) restricting a videos query to `--only`."""
    return (f" AND {ONLY_SQL}", (only, only, only)) if only else ("", ())


@dataclass(frozen=True)
class Video:
    id: str
    url: str
    site: str
    title: str | None
    duration: float | None
    attempts: int
    seed: str | None = None
    mode: str = "technique"  # set by the runner from the video's seed; not stored


class Queue:
    def __init__(self, path: Path | str) -> None:
        if isinstance(path, Path):
            path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.create_function("REGEXP", 2, _regexp, deterministic=True)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA busy_timeout=5000")
        self._db.executescript(SCHEMA)
        cols = {r["name"] for r in self._db.execute("PRAGMA table_info(videos)")}
        if "upload_date" not in cols:
            self._db.execute("ALTER TABLE videos ADD COLUMN upload_date TEXT")
        for col, decl in (("channel", "TEXT"), ("view_count", "INTEGER"), ("priority", "REAL")):
            if col not in cols:
                self._db.execute(f"ALTER TABLE videos ADD COLUMN {col} {decl}")
        auto_cols = {r["name"] for r in self._db.execute("PRAGMA table_info(auto_seeds)")}
        if "kind" not in auto_cols:
            self._db.execute(
                "ALTER TABLE auto_seeds ADD COLUMN kind TEXT NOT NULL DEFAULT 'search'"
            )
        self._db.execute("CREATE INDEX IF NOT EXISTS videos_priority ON videos(priority)")
        self._lock = threading.Lock()

    def _write(self, sql: str, args: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._db.execute(sql, args)

    def enqueue(
        self,
        *,
        site: str,
        site_id: str,
        url: str,
        title: str | None = None,
        duration: float | None = None,
        seed: str | None = None,
        channel: str | None = None,
        view_count: int | None = None,
        priority: float | None = None,
    ) -> bool:
        """Adds a video unless it is already known. Returns whether it was new."""
        now = time.time()
        cur = self._write(
            "INSERT OR IGNORE INTO videos (id, url, site, title, duration, seed, channel,"
            " view_count, priority, created, updated) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                f"{site}:{site_id}",
                url,
                site,
                title,
                duration,
                seed,
                channel,
                view_count,
                priority,
                now,
                now,
            ),
        )
        return cur.rowcount == 1

    def lease(
        self, lease_s: float, now: float | None = None, only: str | None = None
    ) -> Video | None:
        """Takes the highest-priority free video: new, or leased with an expired lease. Ties go
        to the oldest, so equally-scored videos still drain in order. A studied source's `web`
        rows are never fetched or downloaded, so they never wait in this pool at all — this
        filter is belt and braces, since they are inserted straight into `carded`. `only` is a
        regex: only videos whose seed, channel or url match it are leased."""
        now = time.time() if now is None else now
        extra, args = _only(only)
        row = self._write(
            "UPDATE videos SET status='leased', lease_until=?, attempts=attempts+1, updated=?"
            " WHERE id = (SELECT id FROM videos WHERE site != 'web' AND (status='new'"
            f"   OR (status='leased' AND lease_until < ?)){extra}"
            "   ORDER BY COALESCE(priority, 0) DESC, created LIMIT 1)"
            " RETURNING id, url, site, title, duration, attempts, seed",
            (now + lease_s, now, now, *args),
        ).fetchone()
        return Video(**dict(row)) if row else None

    def upsert_web_video(self, *, id: str, url: str, title: str | None, seed: str) -> None:
        """A studied source's project, stored straight as a `carded` row: no lease, no download,
        ever. Re-import refreshes it in place, so `distill import` stays idempotent."""
        now = time.time()
        self._write(
            "INSERT INTO videos (id, url, site, title, duration, seed, status, attempts,"
            " created, updated) VALUES (?, ?, 'web', ?, NULL, ?, 'carded', 0, ?, ?)"
            " ON CONFLICT(id) DO UPDATE SET url=excluded.url, title=excluded.title,"
            " seed=excluded.seed, status='carded', updated=excluded.updated",
            (id, url, title, seed, now, now),
        )

    def finish(
        self,
        video_id: str,
        status: str,
        *,
        screen: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        self._write(
            "UPDATE videos SET status=?, screen=COALESCE(?, screen), error=?, lease_until=NULL,"
            " updated=? WHERE id=?",
            (status, json.dumps(screen) if screen else None, error, time.time(), video_id),
        )

    def retry_or_fail(self, video: Video, error: str, max_attempts: int) -> str:
        status = "failed" if video.attempts >= max_attempts else "new"
        self.finish(video.id, status, error=error[:2000])
        return status

    def renew(self, video_id: str, lease_s: float) -> None:
        """Extends a lease still held, so a long video is not handed to a second worker."""
        self._write(
            "UPDATE videos SET lease_until=? WHERE id=? AND status='leased'",
            (time.time() + lease_s, video_id),
        )

    def memo(self, video_id: str, key: str) -> Any:
        """A model answer already obtained for this video, or None. Retries replay these
        instead of asking again, so a failure late in a video costs only the missing calls."""
        with self._lock:
            row = self._db.execute(
                "SELECT value FROM memo WHERE video_id=? AND key=?", (video_id, key)
            ).fetchone()
        return json.loads(row["value"]) if row else None

    def remember(self, video_id: str, key: str, value: Any) -> None:
        self._write(
            "INSERT OR REPLACE INTO memo (video_id, key, value) VALUES (?, ?, ?)",
            (video_id, key, json.dumps(value)),
        )

    def release(self, video_id: str) -> None:
        self._write(
            "UPDATE videos SET status='new', attempts=MAX(attempts-1, 0), lease_until=NULL,"
            " updated=? WHERE id=? AND status='leased'",
            (time.time(), video_id),
        )

    def requeue_for_new_forms(self, only: str | None = None) -> list[str]:
        """After the forms change: drops cards in the old shape and queues their videos again,
        with any video the old screen dropped or studied as a reference. Age drops stand: the
        age rule did not change with the forms. A studied source's `web` rows never watched
        anything and have no forms version to outgrow, so they are left alone. `only` limits it
        to videos matching that regex."""
        extra, args = _only(only)
        with self._lock:
            ids = [
                r["id"]
                for r in self._db.execute(
                    "SELECT id FROM videos WHERE site != 'web' AND (status IN"
                    " ('carded', 'referenced')"
                    " OR (status='dropped' AND COALESCE(error, '') NOT LIKE 'published %'))"
                    f"{extra}",
                    args,
                )
            ]
        self._write("DELETE FROM cards WHERE body NOT LIKE '%\"produces\"%'")
        for video_id in ids:
            self._write("DELETE FROM compositions WHERE video_id=?", (video_id,))
            self._write(
                "UPDATE videos SET status='new', attempts=0, error=NULL, lease_until=NULL,"
                " updated=? WHERE id=?",
                (time.time(), video_id),
            )
        return ids

    def requeue_failed(self, only: str | None = None) -> int:
        extra, args = _only(only)
        cur = self._write(
            "UPDATE videos SET status='new', attempts=0, error=NULL, lease_until=NULL, updated=?"
            f" WHERE status IN ('failed', 'leased') AND site != 'web'{extra}",
            (time.time(), *args),
        )
        return cur.rowcount

    def clear_cards(self, video_id: str) -> None:
        self._write("DELETE FROM cards WHERE video_id=?", (video_id,))

    def replace_studied_cards(
        self, video_id: str, url: str, title: str, cards: list[tuple]
    ) -> None:
        """Atomic source replacement: a crash leaves either old or fully verified new cards."""
        now = time.time()
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                self._replace_studied(video_id, url, title, cards, now)
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            self._db.execute("COMMIT")

    def _replace_studied(
        self, video_id: str, url: str, title: str, cards: list[tuple], now: float
    ) -> None:
        self._db.execute(
            "INSERT INTO videos (id,url,site,title,seed,status,created,updated)"
            " VALUES (?,?,'web',?,?,'carded',?,?) ON CONFLICT(id) DO UPDATE SET"
            " url=excluded.url,title=excluded.title,status='carded',updated=excluded.updated",
            (video_id, url, title, "studied:research", now, now),
        )
        self._db.execute("DELETE FROM cards WHERE video_id=?", (video_id,))
        self._db.executemany(
            "INSERT INTO cards (video_id,tool,topic,technique,body,created) VALUES (?,?,?,?,?,?)",
            [
                (video_id, tool, topic, name, json.dumps(body), now)
                for tool, topic, name, body in cards
            ],
        )

    def add_card(
        self, video_id: str, tool: str, topic: str, technique: str, body: dict[str, Any]
    ) -> None:
        self._write(
            "INSERT INTO cards (video_id, tool, topic, technique, body, created)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (video_id, tool, topic, technique, json.dumps(body), time.time()),
        )

    def clear_workflow_cards(self, video_id: str) -> None:
        self._write("DELETE FROM workflow_cards WHERE video_id=?", (video_id,))

    def add_workflow_card(self, video_id: str, goal: str, body: dict[str, Any]) -> None:
        self._write(
            "INSERT INTO workflow_cards (video_id, goal, body, created) VALUES (?, ?, ?, ?)",
            (video_id, goal, json.dumps(body), time.time()),
        )

    def workflow_cards(self) -> list[dict[str, Any]]:
        """Workflow cards with the video each came from, oldest first."""
        with self._lock:
            rows = self._db.execute(
                "SELECT c.id, c.video_id, c.goal, c.body, v.url, v.title, v.channel, v.upload_date"
                " FROM workflow_cards c JOIN videos v ON v.id = c.video_id ORDER BY c.id"
            ).fetchall()
        return [{**dict(r), "body": json.loads(r["body"])} for r in rows]

    def set_upload_date(self, video_id: str, date: str) -> None:
        self._write("UPDATE videos SET upload_date=? WHERE id=?", (date, video_id))

    def set_priority(self, video_id: str, priority: float) -> None:
        self._write("UPDATE videos SET priority=? WHERE id=?", (priority, video_id))

    def set_priorities(self, scored: list[tuple[str, float]]) -> None:
        """Many priorities in one transaction, not one commit per row."""
        with self._lock:
            self._db.execute("BEGIN")
            try:
                self._db.executemany(
                    "UPDATE videos SET priority=? WHERE id=?", [(p, i) for i, p in scored]
                )
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            self._db.execute("COMMIT")

    def carded_videos(self) -> list[dict[str, Any]]:
        """Carded videos with enough to weigh a link scan against their source's yield. A
        studied source's `web` rows are not videos and have no description to scan for links."""
        with self._lock:
            rows = self._db.execute(
                "SELECT id, url, site, channel, seed FROM videos"
                " WHERE status='carded' AND site != 'web'"
            ).fetchall()
        return [dict(r) for r in rows]

    def videos_missing_priority(self) -> list[dict[str, Any]]:
        """`new` rows queued before scoring existed (or a migrated database), for backfill."""
        return self._waiting("AND priority IS NULL")

    def waiting_videos(self) -> list[dict[str, Any]]:
        """Every `new` row, to rescore as its source's record changes."""
        return self._waiting("")

    def _waiting(self, extra: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT id, site, title, duration, seed, channel FROM videos"
                f" WHERE status='new' AND site != 'web' {extra}"
            ).fetchall()
        return [dict(r) for r in rows]

    def _group_stats(self, group_col: str) -> dict[str, SourceStats]:
        """Watched, carded and yielded value per distinct value of `seed` or `channel`. A
        studied source's `web` rows were never watched or discovered, so they are excluded: a
        studied source is not a video source with a track record to score."""
        with self._lock:
            videos = self._db.execute(
                f"SELECT id, {group_col} AS key, status FROM videos"
                f" WHERE {group_col} IS NOT NULL AND site != 'web'"
            ).fetchall()
            cards = self._db.execute("SELECT video_id, body FROM cards").fetchall()
        key_of = {v["id"]: v["key"] for v in videos}
        watched: dict[str, int] = {}
        carded: dict[str, int] = {}
        for v in videos:
            if v["status"] in FINISHED_STATUSES:
                watched[v["key"]] = watched.get(v["key"], 0) + 1
            if v["status"] == "carded":
                carded[v["key"]] = carded.get(v["key"], 0) + 1
        depths: dict[str, list[int]] = {}
        for c in cards:
            if c["video_id"] in key_of:
                depths.setdefault(c["video_id"], []).append(card_depth(json.loads(c["body"])))
        yielded: dict[str, float] = {}
        for video_id, ds in depths.items():
            key = key_of[video_id]
            yielded[key] = yielded.get(key, 0.0) + video_value(ds)
        keys = set(watched) | set(carded) | set(yielded)
        return {
            k: SourceStats(
                watched=watched.get(k, 0), carded=carded.get(k, 0), value=yielded.get(k, 0.0)
            )
            for k in keys
        }

    def seed_stats(self) -> dict[str, SourceStats]:
        return self._group_stats("seed")

    def channel_stats(self) -> dict[str, SourceStats]:
        return self._group_stats("channel")

    def sources(self) -> list[dict[str, Any]]:
        """One row per seed and per channel with any history, sorted by yield descending, for
        `distill sources`."""
        out = []
        for kind, stats_map in (("seed", self.seed_stats()), ("channel", self.channel_stats())):
            for key, st in stats_map.items():
                y = smoothed_yield(st)
                out.append(
                    {
                        "kind": kind,
                        "source": key,
                        "watched": st.watched,
                        "carded": st.carded,
                        "value": round(st.value, 2),
                        "yield": round(y, 3),
                        "multiplier": round(refill_multiplier(y), 2),
                        "paused": is_paused(st),
                    }
                )
        return sorted(out, key=lambda r: r["yield"], reverse=True)

    def add_composition(self, video_id: str, kind: str, body: dict[str, Any]) -> None:
        self._write(
            "INSERT INTO compositions (video_id, kind, body, created) VALUES (?, ?, ?, ?)",
            (video_id, kind, json.dumps(body), time.time()),
        )

    def clear_compositions(self, video_id: str) -> None:
        self._write("DELETE FROM compositions WHERE video_id=?", (video_id,))

    def compositions(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT c.id, c.video_id, c.kind, c.body, v.url, v.upload_date"
                " FROM compositions c JOIN videos v ON v.id = c.video_id ORDER BY c.id"
            ).fetchall()
        return [{**dict(r), "body": json.loads(r["body"])} for r in rows]

    def add_auto_seed(self, query: str, video_id: str, kind: str = "search") -> bool:
        """`kind` is 'search' (a query, turned into a YouTube search URL), 'channel' (a
        snowballed channel's uploads listing) or 'link' (a URL mentioned in a carded video's
        description) — `query` is already a full listing URL for the latter two."""
        cur = self._write(
            "INSERT OR IGNORE INTO auto_seeds (query, video_id, kind, created) VALUES (?, ?, ?, ?)",
            (query, video_id, kind, time.time()),
        )
        return cur.rowcount == 1

    def auto_seeds(self) -> list[dict[str, str]]:
        with self._lock:
            rows = self._db.execute("SELECT query, kind, video_id FROM auto_seeds").fetchall()
        return [dict(r) for r in rows]

    def technique_names(self) -> list[dict[str, Any]]:
        """One row per distinct technique carded so far, for matching references against."""
        with self._lock:
            rows = self._db.execute("SELECT technique, body FROM cards ORDER BY id").fetchall()
        seen: dict[str, dict[str, Any]] = {}
        for r in rows:
            body = json.loads(r["body"])
            if "produces" in body:
                seen.setdefault(
                    r["technique"], {"name": r["technique"], "produces": body["produces"]}
                )
        return list(seen.values())

    def update_card(self, card_id: int, body: dict[str, Any]) -> None:
        self._write("UPDATE cards SET body=? WHERE id=?", (json.dumps(body), card_id))

    def cards(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT c.id, c.video_id, c.tool, c.topic, c.technique, c.body, v.url,"
                " v.upload_date"
                " FROM cards c JOIN videos v ON v.id = c.video_id ORDER BY c.id"
            ).fetchall()
        return [{**dict(r), "body": json.loads(r["body"])} for r in rows]

    def merged(self, group_key: str) -> tuple[str, str] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT cards_hash, markdown FROM merges WHERE group_key=?", (group_key,)
            ).fetchone()
        return (row["cards_hash"], row["markdown"]) if row else None

    def save_merge(self, group_key: str, cards_hash: str, markdown: str) -> None:
        self._write(
            "INSERT INTO merges (group_key, cards_hash, markdown, updated) VALUES (?, ?, ?, ?)"
            " ON CONFLICT(group_key) DO UPDATE SET cards_hash=excluded.cards_hash,"
            " markdown=excluded.markdown, updated=excluded.updated",
            (group_key, cards_hash, markdown, time.time()),
        )

    def audits(self) -> dict[int, dict[str, Any]]:
        """Card id -> its latest audit: the card hash and model it was made with, and checks."""
        with self._lock:
            rows = self._db.execute("SELECT card_id, card_hash, model, body FROM audits").fetchall()
        return {
            r["card_id"]: {
                "card_hash": r["card_hash"],
                "model": r["model"],
                **json.loads(r["body"]),
            }
            for r in rows
        }

    def save_audit(self, card_id: int, card_hash: str, model: str, body: str) -> None:
        self._write(
            "INSERT INTO audits (card_id, card_hash, model, body, updated) VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(card_id) DO UPDATE SET card_hash=excluded.card_hash,"
            " model=excluded.model, body=excluded.body, updated=excluded.updated",
            (card_id, card_hash, model, body, time.time()),
        )

    def save_style(
        self,
        source_id: str,
        *,
        author: str | None,
        author_url: str | None,
        home: str | None,
        credit: str,
        markdown: str,
        screenshots: list[str],
    ) -> None:
        """A studied source's style guide, replaced whole on re-import."""
        self._write(
            "INSERT INTO styles (source_id, author, author_url, home, credit, markdown,"
            " screenshots, updated) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(source_id) DO UPDATE SET author=excluded.author,"
            " author_url=excluded.author_url, home=excluded.home, credit=excluded.credit,"
            " markdown=excluded.markdown, screenshots=excluded.screenshots,"
            " updated=excluded.updated",
            (
                source_id,
                author,
                author_url,
                home,
                credit,
                markdown,
                json.dumps(screenshots),
                time.time(),
            ),
        )

    def styles(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM styles ORDER BY source_id").fetchall()
        return [{**dict(r), "screenshots": json.loads(r["screenshots"])} for r in rows]

    def counts(self) -> dict[str, int]:
        with self._lock:
            rows = self._db.execute("SELECT status, COUNT(*) n FROM videos GROUP BY status")
            out = {r["status"]: r["n"] for r in rows}
            out["cards"] = self._db.execute("SELECT COUNT(*) FROM cards").fetchone()[0]
            out["workflow_cards"] = self._db.execute(
                "SELECT COUNT(*) FROM workflow_cards"
            ).fetchone()[0]
        return out

    def finished_since(self, since: float) -> int:
        with self._lock:
            return self._db.execute(
                "SELECT COUNT(*) FROM videos WHERE site!='web'"
                " AND status IN ('dropped','carded') AND updated>=?",
                (since,),
            ).fetchone()[0]

    def pending(self, only: str | None = None) -> int:
        extra, args = _only(only)
        with self._lock:
            return self._db.execute(
                f"SELECT COUNT(*) FROM videos WHERE status IN ('new','leased'){extra}", args
            ).fetchone()[0]
