"""Durable, leased research jobs and persistent feed/budget bookkeeping."""

import json
import sqlite3
import time
import unicodedata
import uuid
from pathlib import Path

from .frontier import arxiv_id, canonical_url


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.executescript(
            "CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, body TEXT, priority REAL,"
            " status TEXT DEFAULT 'new', attempts INTEGER DEFAULT 0, retry_at REAL DEFAULT 0,"
            " lease_until REAL, token TEXT, error TEXT, result TEXT, created REAL, updated REAL);"
            "CREATE TABLE IF NOT EXISTS attempts (id TEXT, started REAL);"
            "CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, body TEXT);"
            "CREATE TABLE IF NOT EXISTS aliases (key TEXT PRIMARY KEY, id TEXT);"
        )

    def close(self) -> None:
        self.db.close()

    def put(self, item: dict) -> bool:
        now = time.time()
        with self.db:
            keys = [
                "title:"
                + "".join(
                    c
                    for c in unicodedata.normalize("NFKC", item["title"]).casefold()
                    if c.isalnum()
                ),
                *[canonical_url(s["url"]) for s in item["sources"]],
            ]
            owner = self._owner(keys, item)
            item = {**item, "id": owner}
            cur = self.db.execute(
                "INSERT OR IGNORE INTO jobs (id,body,priority,created,updated) VALUES (?,?,?,?,?)",
                (item["id"], json.dumps(item), item["priority"], now, now),
            )
            # Rediscovery merges resources without resetting finished work.
            if not cur.rowcount:
                old = self.db.execute("SELECT body FROM jobs WHERE id=?", (item["id"],)).fetchone()
                body = json.loads(old[0])
                body["sources"] = list(
                    {s["url"]: s for s in [*body["sources"], *item["sources"]]}.values()
                )
                if item.get("authors") and not body.get("authors"):
                    body["authors"] = item["authors"]
                if item.get("published") and not body.get("published"):
                    body["published"] = item["published"]
                changed_version = bool(
                    item.get("version")
                    and body.get("version")
                    and item["version"] != body["version"]
                )
                if item.get("version"):
                    body["version"] = item["version"]
                self.db.execute(
                    "UPDATE jobs SET body=?,priority=MAX(priority,?),status=CASE WHEN ?"
                    " AND status!='leased' THEN 'new' ELSE status END,retry_at=CASE WHEN ?"
                    " THEN 0 ELSE retry_at END WHERE id=?",
                    (
                        json.dumps(body),
                        item["priority"],
                        changed_version,
                        changed_version,
                        item["id"],
                    ),
                )
            self.db.executemany(
                "INSERT OR IGNORE INTO aliases VALUES (?,?)", [(k, owner) for k in keys]
            )
        return bool(cur.rowcount)

    def _owner(self, keys: list[str], item: dict) -> str:
        incoming = {ident[0] for s in item["sources"] if (ident := arxiv_id(s["url"]))}
        for key in keys[1:] + keys[:1]:
            row = self.db.execute(
                "SELECT j.id,j.body FROM aliases a JOIN jobs j ON a.id=j.id WHERE a.key=?", (key,)
            ).fetchone()
            if not row:
                continue
            prior = json.loads(row["body"])
            existing = {ident[0] for s in prior["sources"] if (ident := arxiv_id(s["url"]))}
            if incoming and existing and incoming.isdisjoint(existing):
                continue
            return row["id"]
        return item["id"]

    def claim(self, daily_cap: int, lease_s: int = 3600, now: float | None = None) -> dict | None:
        now = time.time() if now is None else now
        token = uuid.uuid4().hex
        self.db.execute("BEGIN IMMEDIATE")
        try:
            started = self.db.execute(
                "SELECT COUNT(*) FROM attempts WHERE started>=?", (now - 86400,)
            ).fetchone()[0]
            row = None
            if started < daily_cap:
                row = self.db.execute(
                    "UPDATE jobs SET status='leased', token=?,lease_until=?,attempts=attempts+1,"
                    " updated=? WHERE id=(SELECT id FROM jobs WHERE"
                    " (status IN ('new','watch') AND retry_at<=?)"
                    " OR (status='leased' AND lease_until<?)"
                    " ORDER BY priority DESC,created LIMIT 1) RETURNING *",
                    (token, now + lease_s, now, now, now),
                ).fetchone()
            if row:
                self.db.execute("INSERT INTO attempts VALUES (?,?)", (row["id"], now))
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise
        return {**dict(row), "body": json.loads(row["body"])} if row else None

    def renew(self, job: dict, lease_s: int = 3600) -> bool:
        with self.db:
            return bool(
                self.db.execute(
                    "UPDATE jobs SET lease_until=? WHERE id=? AND token=? AND status='leased'",
                    (time.time() + lease_s, job["id"], job["token"]),
                ).rowcount
            )

    def finish(self, job: dict, status: str, result: dict) -> bool:
        with self.db:
            return bool(
                self.db.execute(
                    "UPDATE jobs SET status=?,result=?,error=NULL,lease_until=NULL,updated=?,"
                    " retry_at=?,attempts=0"
                    " WHERE id=? AND token=? AND status='leased'",
                    (
                        status,
                        json.dumps(result),
                        time.time(),
                        time.time() + 7 * 86400,
                        job["id"],
                        job["token"],
                    ),
                ).rowcount
            )

    def fail(self, job: dict, error: str, max_attempts: int = 3, busy: bool = False) -> None:
        attempts = job["attempts"] - int(busy)
        now = time.time()
        with self.db:
            self.db.execute(
                "UPDATE jobs SET status=?,attempts=?,error=?,lease_until=NULL,retry_at=?,updated=?"
                " WHERE id=? AND token=? AND status='leased'",
                (
                    "failed" if attempts >= max_attempts else "new",
                    attempts,
                    error[:2000],
                    now + min(86400, 300 * 2 ** min(attempts, 8)),
                    now,
                    job["id"],
                    job["token"],
                ),
            )

    def get(self, key: str, default=None):
        row = self.db.execute("SELECT body FROM state WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key: str, value) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO state VALUES (?,?)", (key, json.dumps(value)))

    def retry(self) -> int:
        with self.db:
            return self.db.execute(
                "UPDATE jobs SET status='new',attempts=0,retry_at=0,error=NULL WHERE"
                " status IN ('failed','watch')"
            ).rowcount

    def report(self) -> dict:
        counts = dict(self.db.execute("SELECT status,COUNT(*) FROM jobs GROUP BY status"))
        recent = [
            {**dict(r), "result": summary_result(json.loads(r["result"])) if r["result"] else None}
            for r in self.db.execute(
                "SELECT id,status,attempts,error,result,updated FROM jobs"
                " ORDER BY updated DESC LIMIT 15"
            )
        ]
        return {"counts": counts, "recent": recent}


def summary_result(result: dict) -> dict:
    return {
        k: result[k] for k in ("reason", "capture", "source_errors", "curation") if k in result
    } | {"cards": len(result.get("cards", []))}
