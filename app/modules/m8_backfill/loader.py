"""把持久化的补传原行还原为领域对象。"""
import json
from datetime import UTC, datetime
from decimal import Decimal

from app.evidence import SourceRow
from app.modules.m1_intake.parse import Trigger
from app.modules.m8_backfill.service import BackfillRow


def _source(data):
    return SourceRow(data["file"], data["line"], data["end_line"], tuple(data["cells"]))


def _trigger(payload, source):
    if payload is None:
        return None
    time = datetime.fromisoformat(payload["time"].replace("Z", "+00:00")).astimezone(UTC)
    return Trigger(
        payload["id"], payload["pass_id"], payload["side"], time,
        Decimal(payload["raw_kg"]), source, payload["late"],
    )


def load_backfill_rows(records):
    rows = []
    for record in records:
        source = _source(json.loads(record["source"]))
        trigger = _trigger(json.loads(record["trigger"]) if record["trigger"] else None, source)
        rows.append(BackfillRow(
            record["package_id"], source, trigger,
            bool(record["accepted"]), record["issue_code"],
        ))
    return rows
