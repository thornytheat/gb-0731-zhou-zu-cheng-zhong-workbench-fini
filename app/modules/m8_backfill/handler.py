"""补传包提交与重算编排。"""
import json

from app.errors import DomainError
from app.manifest import canonical, clock, digest
from app.modules.m1_intake.service import accept
from app.modules.m2_calibration.crossing import crossings
from app.modules.m8_backfill.loader import load_backfill_rows
from app.modules.m8_backfill.service import (
    affected_passes,
    backfill_overview,
    conclusion_digest,
    diff_conclusions,
    parse_package,
    package_identity,
    recompute_pass,
)
from app.sources import Source
from app.store import (
    add_package,
    conclusion_history,
    find_package_by_hash,
    get_package,
    latest_conclusions,
    list_backfill_rows,
    list_packages,
    next_conclusion_versions,
)
from app.evidence import wire


def submit_backfill(store, source_dir, sample, upload: Source):
    """登记补传包并重算受影响车次。同内容包重跑完全幂等。"""
    sha256, size = package_identity(upload)
    package_id = digest(canonical({"sha256": sha256, "file": upload.name}).encode("utf-8"))[:16]

    # 权威来源（车次档案、证书、磅单、原始触发）始终从受信样例目录读取，
    # 上传内容只作为补传证据，不替换任何受信文件。
    base_sources = _base_sources(source_dir, sample)
    base_events = accept(base_sources["triggers"])["accepted"]
    passes = crossings(base_sources["passes"])

    with store.transaction() as db:
        existing = find_package_by_hash(db, sha256)
        if existing:
            return wire({
                "package": existing, "idempotent": True, "recomputed": [],
                "overview": _overview(db, passes, base_events, base_sources),
            })

        rows, encoding, segments, blank, raw = parse_package(package_id, upload)
        targets = affected_passes(rows)
        unknown = [pass_id for pass_id in targets if pass_id not in passes]
        if unknown:
            raise DomainError("PASS_UNKNOWN", 422, detail=unknown)

        versions = next_conclusion_versions(db, targets)
        recomputed, conclusions = [], []
        for pass_id in targets:
            outcome = recompute_pass(base_sources, base_events, rows, pass_id)
            previous = _latest_row(db, pass_id)
            new_digest = conclusion_digest(outcome["conclusion"])
            changed = previous is None or conclusion_digest(previous) != new_digest
            version = versions[pass_id]
            conclusions.append({
                "pass_id": pass_id, "version": version, "package_id": package_id,
                "published_at": clock(), "digest": new_digest,
                "row": wire(outcome["conclusion"]),
            })
            recomputed.append({
                "pass_id": pass_id,
                "version": version,
                "changed": changed,
                "changes": diff_conclusions(previous, outcome["conclusion"]) if previous and changed else [],
                "new_conclusion": previous is None,
                "axes": len(outcome["axes"]),
                "duplicates": len(outcome["duplicates"]),
                "issues": [wire(issue) for issue in outcome["issues"]],
            })

        package = {
            "package_id": package_id, "sha256": sha256, "file_name": upload.name, "bytes": size,
            "received_at": clock(), "raw_rows": raw,
            "accepted_rows": sum(row.accepted for row in rows),
            "rejected_rows": sum(not row.accepted for row in rows),
            "affected_passes": targets,
        }
        add_package(db, package, rows, conclusions)
        overview = _overview(db, passes, base_events, base_sources)

    return wire({
        "package": package, "idempotent": False, "recomputed": recomputed,
        "encoding": encoding, "segments": segments, "blank": blank, "overview": overview,
    })


def backfill_dashboard(store, source_dir, sample):
    base_sources = _base_sources(source_dir, sample)
    base_events = accept(base_sources["triggers"])["accepted"]
    passes = crossings(base_sources["passes"])
    with store.transaction() as db:
        packages = list_packages(db)
        records = list_backfill_rows(db)
        overview = _overview(db, passes, base_events, base_sources)
        history = {pass_id: conclusion_history(db, pass_id) for pass_id in passes}
    return wire({"packages": packages, "rows": records, "overview": overview, "history": history})


def package_detail(store, package_id):
    with store.transaction() as db:
        package = get_package(db, package_id)
        if package is None:
            raise DomainError("RUN_UNKNOWN", 404, detail=package_id)
        records = [dict(row) for row in db.execute(
            "SELECT * FROM backfill_rows WHERE package_id = ? ORDER BY id", (package_id,),
        ).fetchall()]
    return wire({"package": package, "rows": records})


def _base_sources(source_dir, sample):
    from app.sources import read_sources
    return read_sources(source_dir, sample)


def _latest_row(db, pass_id):
    row = db.execute(
        "SELECT row FROM conclusions WHERE pass_id = ? ORDER BY version DESC LIMIT 1", (pass_id,),
    ).fetchone()
    return json.loads(row["row"]) if row else None


def _overview(db, passes, base_events, sources):
    stored = load_backfill_rows(list_backfill_rows(db))
    overview = backfill_overview(passes, base_events, stored, latest_conclusions(db))
    overview["sources"] = {role: source.name for role, source in sources.items()}
    return overview
