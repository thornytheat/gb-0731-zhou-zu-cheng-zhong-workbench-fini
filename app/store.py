import json
import sqlite3
from contextlib import closing, contextmanager

from app.errors import DomainError
from app.evidence import wire
from app.manifest import canonical

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    manifest TEXT NOT NULL,
    results TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(id),
    time TEXT NOT NULL,
    level TEXT NOT NULL,
    code TEXT NOT NULL,
    message TEXT NOT NULL,
    source_time TEXT
);
CREATE INDEX IF NOT EXISTS run_history ON runs(created_at DESC, id DESC);
CREATE TABLE IF NOT EXISTS backfill_packages (
    package_id TEXT PRIMARY KEY,
    sha256 TEXT NOT NULL UNIQUE,
    file_name TEXT NOT NULL,
    bytes INTEGER NOT NULL,
    received_at TEXT NOT NULL,
    raw_rows INTEGER NOT NULL,
    accepted_rows INTEGER NOT NULL,
    rejected_rows INTEGER NOT NULL,
    affected_passes TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS backfill_rows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    package_id TEXT NOT NULL REFERENCES backfill_packages(package_id),
    pass_id TEXT NOT NULL,
    record_id TEXT NOT NULL,
    accepted INTEGER NOT NULL,
    issue_code TEXT NOT NULL,
    source TEXT NOT NULL,
    trigger TEXT
);
CREATE INDEX IF NOT EXISTS backfill_pass ON backfill_rows(pass_id);
CREATE TABLE IF NOT EXISTS conclusions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pass_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    package_id TEXT,
    published_at TEXT NOT NULL,
    digest TEXT NOT NULL,
    row TEXT NOT NULL,
    UNIQUE(pass_id, version)
);
CREATE INDEX IF NOT EXISTS conclusion_pass ON conclusions(pass_id, version DESC);
"""


class Store:
    def __init__(self, path):
        self.path = path

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        return db

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db:
            db.execute("PRAGMA journal_mode = WAL")
            db.executescript(SCHEMA)

    @contextmanager
    def transaction(self):
        try:
            with closing(self.connect()) as db, db:
                db.execute("BEGIN IMMEDIATE")
                yield db
        except sqlite3.OperationalError as exc:
            if (getattr(exc, "sqlite_errorcode", 0) & 255) in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                raise DomainError("STORE_BUSY", 503) from exc
            raise

    def read(self, run_id):
        with closing(self.connect()) as db:
            result = find(db, run_id)
        if result is None:
            raise DomainError("RUN_UNKNOWN", 404)
        return result

    def history(self):
        with closing(self.connect()) as db:
            rows = db.execute("SELECT manifest FROM runs ORDER BY created_at DESC, id DESC LIMIT 50")
            return [json.loads(row["manifest"]) for row in rows]

    def events(self, after, limit):
        with closing(self.connect()) as db:
            rows = db.execute("SELECT * FROM events WHERE seq > ? ORDER BY seq LIMIT ?", (after, limit))
            return [dict(row) for row in rows]


def find(db, run_id):
    row = db.execute("SELECT manifest, results FROM runs WHERE id = ?", (run_id,)).fetchone()
    return (json.loads(row["manifest"]), json.loads(row["results"])) if row else None


def add_run(db, manifest, results):
    db.execute("INSERT INTO runs VALUES (?, ?, ?, ?)", (
        manifest["run_id"], manifest["created_at"], canonical(manifest), canonical(results),
    ))


def add_events(db, run_id, events):
    db.executemany("""INSERT INTO events (run_id,time,level,code,message,source_time)
                    VALUES (?, ?, ?, ?, ?, ?)""", ((run_id, *event) for event in events))


def find_package_by_hash(db, sha256):
    row = db.execute("SELECT * FROM backfill_packages WHERE sha256 = ?", (sha256,)).fetchone()
    return dict(row) if row else None


def add_package(db, package, rows, conclusions):
    db.execute("""INSERT INTO backfill_packages
                (package_id,sha256,file_name,bytes,received_at,raw_rows,accepted_rows,
                 rejected_rows,affected_passes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""", (
        package["package_id"], package["sha256"], package["file_name"], package["bytes"],
        package["received_at"], package["raw_rows"], package["accepted_rows"],
        package["rejected_rows"], canonical(package["affected_passes"]),
    ))
    db.executemany("""INSERT INTO backfill_rows
                    (package_id,pass_id,record_id,accepted,issue_code,source,trigger)
                    VALUES (?, ?, ?, ?, ?, ?, ?)""", (
        (
            row.package_id,
            row.trigger.pass_id if row.trigger else (row.source.cells[1] if len(row.source.cells) > 1 else ""),
            row.trigger.id if row.trigger else (row.source.cells[0] if row.source.cells else ""),
            int(row.accepted), row.issue_code, canonical(wire(row.source)),
            canonical(wire(row.trigger)) if row.trigger else None,
        )
        for row in rows
    ))
    db.executemany("""INSERT INTO conclusions
                    (pass_id,version,package_id,published_at,digest,row)
                    VALUES (?, ?, ?, ?, ?, ?)""", (
        (
            item["pass_id"], item["version"], item.get("package_id"),
            item["published_at"], item["digest"], canonical(item["row"]),
        )
        for item in conclusions
    ))


def list_packages(db):
    rows = db.execute("SELECT * FROM backfill_packages ORDER BY received_at, package_id").fetchall()
    return [dict(row) for row in rows]


def get_package(db, package_id):
    row = db.execute("SELECT * FROM backfill_packages WHERE package_id = ?", (package_id,)).fetchone()
    return dict(row) if row else None


def list_backfill_rows(db):
    rows = db.execute("SELECT * FROM backfill_rows ORDER BY id").fetchall()
    return [dict(row) for row in rows]


def latest_conclusions(db):
    rows = db.execute("""SELECT c.* FROM conclusions c
                       JOIN (SELECT pass_id, MAX(version) AS version FROM conclusions GROUP BY pass_id) latest
                       ON latest.pass_id = c.pass_id AND latest.version = c.version
                       ORDER BY c.pass_id""").fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["row"] = json.loads(item["row"])
        result.append(item)
    return result


def conclusion_history(db, pass_id):
    rows = db.execute("SELECT * FROM conclusions WHERE pass_id = ? ORDER BY version", (pass_id,)).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["row"] = json.loads(item["row"])
        result.append(item)
    return result


def next_conclusion_versions(db, pass_ids):
    if not pass_ids:
        return {}
    placeholders = ",".join("?" for _ in pass_ids)
    rows = db.execute(
        f"SELECT pass_id, MAX(version) AS version FROM conclusions WHERE pass_id IN ({placeholders}) GROUP BY pass_id",
        tuple(pass_ids),
    ).fetchall()
    versions = {row["pass_id"]: row["version"] for row in rows}
    return {pass_id: versions.get(pass_id, 0) + 1 for pass_id in pass_ids}
