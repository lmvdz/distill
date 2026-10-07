"""Distill's additive registry. Legacy queue/catalogs are never rewritten here."""

import json
import sqlite3
import time
import uuid
from pathlib import Path

from .directives import COMPILE, Directive, canonical, identity
from .library import Capture, ChangeBatch

SCHEMA = """
CREATE TABLE IF NOT EXISTS directives (
 key TEXT PRIMARY KEY, version TEXT NOT NULL, catalog TEXT NOT NULL UNIQUE,
 state TEXT NOT NULL DEFAULT 'active', last_started REAL NOT NULL DEFAULT 0,
 next_run REAL NOT NULL DEFAULT 0, lease_until REAL, token TEXT, error TEXT);
CREATE TABLE IF NOT EXISTS directive_versions (
 id TEXT PRIMARY KEY, key TEXT NOT NULL, body TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS compilations (version TEXT PRIMARY KEY, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sources (id TEXT PRIMARY KEY, url TEXT NOT NULL UNIQUE);
CREATE TABLE IF NOT EXISTS observations (
 id TEXT PRIMARY KEY, source TEXT NOT NULL REFERENCES sources(id),
 body TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS item_revisions (
 id TEXT PRIMARY KEY, library TEXT NOT NULL, external_id TEXT NOT NULL,
 upstream_revision TEXT NOT NULL, source TEXT NOT NULL REFERENCES sources(id),
 observation TEXT REFERENCES observations(id), body TEXT NOT NULL,
 UNIQUE(library, external_id, upstream_revision));
CREATE TABLE IF NOT EXISTS library_items (
 library TEXT NOT NULL, external_id TEXT NOT NULL, revision TEXT NOT NULL
 REFERENCES item_revisions(id), removed INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(library,external_id));
CREATE TABLE IF NOT EXISTS adapters (library TEXT PRIMARY KEY, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS adapter_configs (library TEXT PRIMARY KEY, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS source_checks (source TEXT PRIMARY KEY, checked REAL NOT NULL);
CREATE TABLE IF NOT EXISTS source_leases (
 source TEXT PRIMARY KEY, token TEXT NOT NULL, lease_until REAL NOT NULL);
CREATE TABLE IF NOT EXISTS legacy_entities (
 id TEXT PRIMARY KEY, source TEXT NOT NULL REFERENCES sources(id), kind TEXT NOT NULL,
 body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS legacy_aliases (
 origin TEXT NOT NULL, kind TEXT NOT NULL, row_id INTEGER NOT NULL, entity TEXT NOT NULL,
 body TEXT NOT NULL, PRIMARY KEY(origin,kind,row_id));
CREATE TABLE IF NOT EXISTS responses (id TEXT PRIMARY KEY, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS call_attempts (
 directive TEXT NOT NULL, started REAL NOT NULL);
CREATE TABLE IF NOT EXISTS run_attempts (
 directive TEXT NOT NULL, fingerprint TEXT NOT NULL, started REAL NOT NULL,
 status TEXT NOT NULL, error TEXT, manifest TEXT);
CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, body TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS upstream_runs (id TEXT PRIMARY KEY, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS publications (
 catalog TEXT NOT NULL, version TEXT NOT NULL REFERENCES runs(id),
 created REAL NOT NULL, PRIMARY KEY(catalog,version));
CREATE TABLE IF NOT EXISTS catalog_heads (
 catalog TEXT PRIMARY KEY, version TEXT NOT NULL REFERENCES runs(id));
CREATE TABLE IF NOT EXISTS memberships (
 catalog TEXT NOT NULL, version TEXT NOT NULL, entity TEXT NOT NULL,
 body TEXT NOT NULL, PRIMARY KEY(catalog,version,entity));
CREATE TABLE IF NOT EXISTS relationships (
 id TEXT NOT NULL, catalog TEXT NOT NULL, version TEXT NOT NULL,
 body TEXT NOT NULL, PRIMARY KEY(id,catalog,version));
"""


class Registry:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

    def close(self):
        self.db.close()

    def save_directive(self, directive: Directive) -> str:
        body = canonical(directive.model_dump())
        with self.db:
            prior = self.db.execute(
                "SELECT catalog FROM directives WHERE key=?", (directive.key,)
            ).fetchone()
            if prior and prior[0] != directive.catalog:
                raise ValueError("a directive's catalog identity cannot change on revision")
            owner = self.db.execute(
                "SELECT key FROM directives WHERE catalog=?", (directive.catalog,)
            ).fetchone()
            if owner and owner[0] != directive.key:
                raise ValueError("catalog is owned by another directive")
            self.db.execute(
                "INSERT OR IGNORE INTO directive_versions VALUES (?,?,?,?)",
                (directive.version, directive.key, body, time.time()),
            )
            self.db.execute(
                "INSERT INTO directives (key,version,catalog) VALUES (?,?,?)"
                " ON CONFLICT(key) DO UPDATE SET version=excluded.version,next_run=0,error=NULL",
                (directive.key, directive.version, directive.catalog),
            )
        return directive.version

    def directive(self, key: str, version: str | None = None) -> Directive:
        if version:
            row = self.db.execute(
                "SELECT body FROM directive_versions WHERE key=? AND id=?", (key, version)
            ).fetchone()
        else:
            row = self.db.execute(
                "SELECT v.body FROM directives d JOIN directive_versions v ON d.version=v.id"
                " WHERE d.key=?",
                (key,),
            ).fetchone()
        if not row:
            raise ValueError(f"unknown directive/version: {key}")
        return Directive.model_validate_json(row[0])

    def record_compilation(self, directive: Directive, data: dict):
        from .engine import dependencies

        request = {
            "stage": "compile",
            "prompt": COMPILE,
            "schema": Directive.model_json_schema(),
            "model": directive.model,
            "data": data,
            "dependencies": dependencies(),
        }
        trace = {
            "key": identity("response", request),
            "request": request,
            "response": directive.model_dump(),
        }
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO compilations VALUES (?,?)",
                (directive.version, canonical(trace)),
            )

    def compilation(self, directive: Directive) -> dict:
        row = self.db.execute(
            "SELECT body FROM compilations WHERE version=?", (directive.version,)
        ).fetchone()
        return json.loads(row[0]) if row else {"origin": "validated declarative JSON"}

    def set_state(self, key: str, state: str):
        if state not in {"active", "paused", "retired"}:
            raise ValueError("invalid directive state")
        with self.db:
            cur = self.db.execute(
                "UPDATE directives SET state=?,next_run=0 WHERE key=?", (state, key)
            )
            if not cur.rowcount:
                raise ValueError(f"unknown directive: {key}")

    def observe(self, source: str, capture: Capture) -> str:
        body = {"source": source, **capture.model_dump()}
        ident = identity("observation", body)
        inserted = self.db.execute(
            "INSERT OR IGNORE INTO observations VALUES (?,?,?,?)",
            (ident, source, canonical(body), time.time()),
        )
        if inserted.rowcount:
            libraries = self.db.execute(
                "SELECT DISTINCT library FROM item_revisions"
                " WHERE source=? AND observation IS NULL",
                (source,),
            ).fetchall()
            for library in libraries:
                self.invalidate_library(library[0])
        return ident

    def import_batch(self, batch: ChangeBatch) -> dict:
        """Atomic import; reused upstream revisions cannot silently change their content."""
        added, changed = 0, False
        with self.db:
            for item in batch.items:
                source = identity("source", item.url)
                self.db.execute("INSERT OR IGNORE INTO sources VALUES (?,?)", (source, item.url))
                observation = self.observe(source, item.capture) if item.capture else None
                body = item.model_dump()
                ident = identity("item", [batch.library, item.external_id, body])
                old = self.db.execute(
                    "SELECT id FROM item_revisions WHERE library=? AND external_id=?"
                    " AND upstream_revision=?",
                    (batch.library, item.external_id, item.revision),
                ).fetchone()
                if old and old[0] != ident:
                    raise ValueError(
                        f"upstream revision reused with changed content: {item.external_id}"
                    )
                cur = self.db.execute(
                    "INSERT OR IGNORE INTO item_revisions VALUES (?,?,?,?,?,?,?)",
                    (
                        ident,
                        batch.library,
                        item.external_id,
                        item.revision,
                        source,
                        observation,
                        canonical(body),
                    ),
                )
                added += cur.rowcount
                current = self.db.execute(
                    "SELECT revision,removed FROM library_items WHERE library=? AND external_id=?",
                    (batch.library, item.external_id),
                ).fetchone()
                changed = changed or not current or tuple(current) != (ident, int(item.removed))
                self.db.execute(
                    "INSERT INTO library_items VALUES (?,?,?,?) ON CONFLICT(library,external_id)"
                    " DO UPDATE SET revision=excluded.revision,removed=excluded.removed",
                    (batch.library, item.external_id, ident, item.removed),
                )
            if batch.full_snapshot:
                present = {i.external_id for i in batch.items}
                for row in self.db.execute(
                    "SELECT external_id FROM library_items WHERE library=?", (batch.library,)
                ).fetchall():
                    if row[0] not in present:
                        withdrawn = self.db.execute(
                            "UPDATE library_items SET removed=1 WHERE library=? AND external_id=?"
                            " AND removed=0",
                            (batch.library, row[0]),
                        )
                        changed = changed or bool(withdrawn.rowcount)
            self.db.execute(
                "INSERT OR REPLACE INTO adapters VALUES (?,?)",
                (
                    batch.library,
                    canonical({"version": batch.adapter_version, "cursor": batch.cursor}),
                ),
            )
            if changed:
                self.invalidate_library(batch.library)
        return {"library": batch.library, "new_revisions": added, "cursor": batch.cursor}

    def invalidate_library(self, library: str):
        for row in self.db.execute("SELECT key FROM directives").fetchall():
            directive = self.directive(row[0])
            if self.uses_library(directive, library):
                self.db.execute("UPDATE directives SET next_run=0 WHERE key=?", (directive.key,))

    @staticmethod
    def uses_library(directive: Directive, library: str) -> bool:
        if library.startswith("discovery:"):
            return (
                library == f"discovery:{directive.key}" or library in directive.selection.libraries
            )
        return not directive.selection.libraries or library in directive.selection.libraries

    def configure_adapter(self, library: str, body: dict):
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO adapter_configs VALUES (?,?)", (library, canonical(body))
            )

    def acquire_source(self, source: str) -> str | None:
        now, token = time.time(), uuid.uuid4().hex
        with self.db:
            row = self.db.execute(
                "INSERT INTO source_leases VALUES (?,?,?)"
                " ON CONFLICT(source) DO UPDATE SET token=excluded.token,"
                " lease_until=excluded.lease_until WHERE source_leases.lease_until<?"
                " RETURNING token",
                (source, token, now + 300, now),
            ).fetchone()
        return row[0] if row else None

    def release_source(self, source: str, token: str):
        with self.db:
            self.db.execute("DELETE FROM source_leases WHERE source=? AND token=?", (source, token))

    def cursor(self, library: str) -> str | None:
        row = self.db.execute("SELECT body FROM adapters WHERE library=?", (library,)).fetchone()
        return json.loads(row[0])["cursor"] if row else None

    def inventory(self) -> list[dict]:
        out = []
        for row in self.db.execute("SELECT library FROM adapters ORDER BY library").fetchall():
            items = [
                json.loads(r[0])
                for r in self.db.execute(
                    "SELECT r.body FROM library_items i JOIN item_revisions r ON i.revision=r.id"
                    " WHERE i.library=? AND i.removed=0 ORDER BY i.external_id",
                    (row[0],),
                )
            ]
            out.append(
                {
                    "library": row[0],
                    "items": len(items),
                    "collections": sorted({c for i in items for c in i["collections"]})[:1000],
                    "tags": sorted({t["value"] for i in items for t in i["tags"]})[:1000],
                }
            )
        return out

    def candidates(self, directive: Directive) -> list[dict]:
        rows = self.db.execute(
            "SELECT r.*,i.removed,s.url FROM library_items i JOIN item_revisions r"
            " ON i.revision=r.id JOIN sources s ON r.source=s.id"
            " ORDER BY r.source,r.library,r.external_id"
        ).fetchall()
        out = []
        for row in rows:
            if not self.uses_library(directive, row["library"]):
                continue
            observation = row["observation"]
            # URL-only items reuse the most recently preserved shared observation.
            if not observation:
                latest = self.db.execute(
                    "SELECT id FROM observations WHERE source=?"
                    " AND json_extract(body,'$.modality')!='catalog'"
                    " ORDER BY created DESC,id LIMIT 1",
                    (row["source"],),
                ).fetchone()
                observation = latest[0] if latest else None
            observed = self.db.execute(
                "SELECT body FROM observations WHERE id=?", (observation,)
            ).fetchone()
            out.append(
                {
                    "item_revision": row["id"],
                    "library": row["library"],
                    "source": row["source"],
                    "url": row["url"],
                    "metadata": {
                        **json.loads(row["body"]),
                        "capture": None,
                        "removed": bool(row["removed"]),
                    },
                    "observation_id": observation,
                    "observation": json.loads(observed[0]) if observed else None,
                }
            )
        return out

    def response(self, key: str) -> dict | None:
        row = self.db.execute("SELECT body FROM responses WHERE id=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def save_response(self, key: str, response: dict):
        with self.db:
            old = self.response(key)
            if old is not None and old != response:
                raise ValueError("saved model response is immutable")
            self.db.execute(
                "INSERT OR IGNORE INTO responses VALUES (?,?)", (key, canonical(response))
            )

    def upstream(self, run_id: str) -> dict:
        row = self.db.execute(
            "SELECT body FROM runs WHERE id=? UNION ALL"
            " SELECT body FROM upstream_runs WHERE id=? LIMIT 1",
            (run_id, run_id),
        ).fetchone()
        if not row:
            raise ValueError(f"missing upstream manifest: {run_id}")
        return json.loads(row[0])

    def reserve_call(self, directive: Directive, shared_cap: int, now: float | None = None):
        now = time.time() if now is None else now
        self.db.execute("BEGIN IMMEDIATE")
        try:
            total = self.db.execute(
                "SELECT COUNT(*) FROM call_attempts WHERE started>?", (now - 86400,)
            ).fetchone()[0]
            own = self.db.execute(
                "SELECT COUNT(*) FROM call_attempts WHERE started>? AND directive=?",
                (now - 86400, directive.key),
            ).fetchone()[0]
            if total >= shared_cap or own >= directive.limits.daily_model_calls:
                raise ValueError("persisted rolling daily model-call budget exhausted")
            self.db.execute("INSERT INTO call_attempts VALUES (?,?)", (directive.key, now))
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def claim(self, key: str, *, force: bool = False, lease_seconds: int = 3600) -> str:
        now, token = time.time(), uuid.uuid4().hex
        with self.db:
            row = self.db.execute(
                "UPDATE directives SET token=?,lease_until=?,last_started=? WHERE key=?"
                " AND state='active' AND (lease_until IS NULL OR lease_until<?)"
                " AND (? OR next_run<=?) RETURNING key",
                (token, now + lease_seconds, now, key, now, force, now),
            ).fetchone()
        if not row:
            raise ValueError(f"directive paused, retired, busy or not yet due: {key}")
        return token

    def renew(self, key: str, token: str):
        with self.db:
            row = self.db.execute(
                "UPDATE directives SET lease_until=? WHERE key=? AND token=?"
                " AND state='active' RETURNING key",
                (time.time() + 3600, key, token),
            ).fetchone()
        if not row:
            raise ValueError("directive lease lost or directive paused/retired")

    def finish(self, directive: Directive, token: str, error: str | None):
        wait = directive.limits.retry_seconds if error else directive.limits.refresh_seconds
        with self.db:
            self.db.execute(
                "UPDATE directives SET lease_until=NULL,token=NULL,"
                "next_run=CASE WHEN version=? THEN ? ELSE 0 END,"
                "error=CASE WHEN version=? THEN ? ELSE NULL END"
                " WHERE key=? AND token=?",
                (
                    directive.version,
                    time.time() + wait,
                    directive.version,
                    error,
                    directive.key,
                    token,
                ),
            )

    def attempt(self, directive: Directive, fingerprint: str) -> int:
        failures = self.db.execute(
            "SELECT COUNT(*) FROM run_attempts WHERE directive=? AND fingerprint=?"
            " AND status IN ('failed','running')",
            (directive.key, fingerprint),
        ).fetchone()[0]
        if failures >= directive.limits.max_attempts:
            raise ValueError("attempt limit reached for these inputs; revise inputs or use retry")
        with self.db:
            cur = self.db.execute(
                "INSERT INTO run_attempts VALUES (?,?,?,'running',NULL,NULL)",
                (directive.key, fingerprint, time.time()),
            )
        return cur.lastrowid

    def end_attempt(self, ident: int, status: str, error: str | None, manifest: dict | None):
        with self.db:
            self.db.execute(
                "UPDATE run_attempts SET status=?,error=?,manifest=? WHERE rowid=?",
                (status, error, canonical(manifest) if manifest else None, ident),
            )

    def retry(self, key: str):
        with self.db:
            self.db.execute(
                "UPDATE run_attempts SET status='reset' WHERE directive=?"
                " AND status IN ('failed','running')",
                (key,),
            )
            self.db.execute("UPDATE directives SET next_run=0,error=NULL WHERE key=?", (key,))

    def publish(self, manifest: dict, token: str):
        directive = Directive.model_validate(manifest["inputs"]["directive"])
        key, catalog, version = directive.key, directive.catalog, manifest["id"]
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            active = self.db.execute(
                "SELECT version,state,token FROM directives WHERE key=?", (key,)
            ).fetchone()
            if not active or tuple(active) != (directive.version, "active", token):
                raise ValueError("directive changed or lease lost; publication withheld")
            if self.candidates(directive) != manifest["inputs"]["candidates"]:
                raise ValueError(
                    "source or library metadata changed during run; publication withheld"
                )
            self.db.execute(
                "INSERT OR IGNORE INTO runs VALUES (?,?,?)",
                (version, canonical(manifest), time.time()),
            )
            self.db.execute(
                "INSERT OR IGNORE INTO publications VALUES (?,?,?)", (catalog, version, time.time())
            )
            for membership in manifest["semantic"]["memberships"]:
                self.db.execute(
                    "INSERT OR IGNORE INTO memberships VALUES (?,?,?,?)",
                    (catalog, version, membership["entity"], canonical(membership)),
                )
            for edge in manifest["semantic"]["relationships"]:
                self.db.execute(
                    "INSERT OR IGNORE INTO relationships VALUES (?,?,?,?)",
                    (edge["id"], catalog, version, canonical(edge)),
                )
            self.db.execute("INSERT OR REPLACE INTO catalog_heads VALUES (?,?)", (catalog, version))

    def manifest(self, version: str) -> dict:
        row = self.db.execute("SELECT body FROM runs WHERE id=?", (version,)).fetchone()
        if not row:
            raise ValueError(f"unknown run: {version}")
        return json.loads(row[0])

    def head(self, catalog: str) -> dict | None:
        row = self.db.execute(
            "SELECT version FROM catalog_heads WHERE catalog=?", (catalog,)
        ).fetchone()
        return self.manifest(row[0]) if row else None

    def due(self) -> list[str]:
        now = time.time()
        return [
            r[0]
            for r in self.db.execute(
                "SELECT key FROM directives WHERE state='active' AND next_run<=?"
                " AND (lease_until IS NULL OR lease_until<?) ORDER BY last_started,key",
                (now, now),
            )
        ]

    def report(self) -> dict:
        return {
            "directives": [
                dict(r)
                for r in self.db.execute(
                    "SELECT key,version,catalog,state,last_started,next_run,lease_until,error"
                    " FROM directives"
                    " ORDER BY key"
                )
            ],
            "catalogs": [
                dict(r) for r in self.db.execute("SELECT * FROM catalog_heads ORDER BY catalog")
            ],
            "counts": {
                table: self.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("sources", "observations", "item_revisions", "runs")
            },
            "attempts": [
                dict(r)
                for r in self.db.execute(
                    "SELECT directive,fingerprint,started,status,error FROM run_attempts"
                    " ORDER BY rowid DESC LIMIT 25"
                )
            ],
        }
