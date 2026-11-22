"""补传工作台领域服务。

设计约束（对应需求）：

1. 补传记录保留原行——补传文件按物理行登记到 backfill_rows，主流水不做就地
   覆盖；重算时把补传行作为追加证据与原始流水合并后整体重放 M1–M6。
2. 受影响车次从去抖到轴组重新计算——重算粒度是整个车次，而不是单条触发，
   保证去抖锚点、对侧配对和轴距分类不会因局部替换而失真。
3. 已对外发出的车次结论不能被静默改写——每个车次的对外结论按版本固化在
   conclusions 表里；重算只写新版本，旧版保持不变，并产出结构化差异。
4. 同一补传包重跑幂等——以包内容 SHA-256 为幂等键，重复提交只登记一次
   接收事件，不新增水位、不重复处理任何行。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.config import BACKFILL_MAX_SPACING_M
from app.errors import DomainError
from app.evidence import wire
from app.manifest import canonical, digest
from app.modules.m1_intake.parse import HEADER as TRIGGER_HEADER
from app.modules.m1_intake.parse import parse_trigger
from app.modules.m1_intake.reader import read_table
from app.modules.m2_calibration.service import calibrate
from app.modules.m3_debounce.service import debounce
from app.modules.m4_axles.service import reconstruct
from app.modules.m6_reconcile.service import reconcile
from app.modules.m8_backfill.segments import (
    channel_timeline,
    contiguity_gap_ms,
    coverage_summary,
    safe_resume_at,
)

if TYPE_CHECKING:
    from app.evidence import SourceRow

# 对外发布的结论字段；差异只比较这些字段，证据引用变化单独列出。
CONCLUSION_FIELDS = ("status", "codes", "wheel_kg", "difference_kg", "axle_ids")


@dataclass(frozen=True)
class BackfillRow:
    package_id: str
    source: SourceRow
    trigger: object
    accepted: bool
    issue_code: str = ""


def parse_package(package_id, source):
    """解析补传包，保留每条原行；坏行隔离但不影响同行其他记录。"""
    table = read_table(source, TRIGGER_HEADER)
    rows, highwater = [], {}
    for row in table.rows:
        if len(row.cells) != len(TRIGGER_HEADER):
            rows.append(BackfillRow(package_id, row, None, False, "BAD_COLUMNS"))
            continue
        try:
            trigger = parse_trigger(row, table.columns(row), highwater)
            rows.append(BackfillRow(package_id, row, trigger, True))
        except DomainError as exc:
            rows.append(BackfillRow(package_id, row, None, False, exc.code))
    return rows, table.encoding, table.segments, table.blank, len(table.rows)


def package_identity(source):
    content = source.content
    return digest(content), len(content)


def _trigger_points(triggers):
    return {(event.source.file, event.source.line, event.source.end_line, event.id) for event in triggers}


def merge_triggers(base_events, backfill_rows):
    """合并原始流水与已接受补传行，保留全部原行。

    同一记录号的补传行进入 M3 身份判定：等值重送归 REPLAY_ID，不等值归
    ID_CONFLICT，规则与普通重送完全一致，不在合并阶段做任何静默覆盖。
    """
    merged = list(base_events)
    seen_sources = _trigger_points(base_events)
    for row in backfill_rows:
        if row.accepted and (
            row.trigger.source.file, row.trigger.source.line, row.trigger.source.end_line, row.trigger.id,
        ) not in seen_sources:
            merged.append(row.trigger)
            seen_sources.add((
                row.trigger.source.file, row.trigger.source.line,
                row.trigger.source.end_line, row.trigger.id,
            ))
    return merged


def recompute_pass(sources, base_events, backfill_rows, pass_id):
    """对单个车次从标定、去抖到轴组、勾稽整体重算。"""
    merged = merge_triggers(
        [event for event in base_events if event.pass_id == pass_id],
        [row for row in backfill_rows if row.accepted and row.trigger.pass_id == pass_id],
    )
    cal = calibrate(merged, sources)
    pulse = debounce(merged, cal["wheels"])
    axles = reconstruct(pulse["kept"], cal["passes"])
    isolated = sorted(cal["issues"] + pulse["issues"] + axles["issues"], key=lambda issue: issue.source.line)
    ledger = reconcile(
        sources["tickets"], cal["passes"], axles["axes"], isolated,
        sorted(pulse["duplicates"], key=lambda issue: issue.source.line), len(merged),
    )
    row = next(item for item in ledger["rows"] if item["pass_id"] == pass_id)
    return {
        "pass_id": pass_id,
        "raw": len(merged),
        "kept": pulse["kept"],
        "duplicates": pulse["duplicates"],
        "issues": isolated,
        "axes": axles["axes"],
        "conclusion": row,
    }


def conclusion_digest(row):
    payload = {field: wire(row[field]) for field in CONCLUSION_FIELDS}
    return digest(canonical(payload).encode("utf-8"))


def diff_conclusions(previous, current):
    """对比两个已发布/新算结论，只返回发生变化的字段。

    入参可能是带 Decimal/datetime 的领域行，也可能是已 wire 的归档行；
    统一序列化后再比较，保证差异判断不受表示形式影响。
    """
    changes = []
    for field in CONCLUSION_FIELDS:
        before = wire(previous[field])
        after = wire(current[field])
        if before != after:
            changes.append({"field": field, "before": before, "after": after})
    return changes


def build_timelines(passes, base_events, backfill_rows):
    """按车次 × 轮道组装水位时间线。

    期望边界取该通道的观测包络（首条与末条观测时刻）：传感器只在车辆压磅时
    才有脉冲，入口/出口窗口内没有轮道事件是常态，用整窗做边界会让边缘空档
    淹没真正的内部缺证。包络内出现空档才是待补；水位停在包络内第一个空档前。
    没有任何观测的通道回退到车次窗口，整段标记待补。
    """
    accepted = [row.trigger for row in backfill_rows if row.accepted]
    timelines = []
    for pass_id, crossing in passes.items():
        base = [event for event in base_events if event.pass_id == pass_id]
        late = [event for event in accepted if event.pass_id == pass_id]
        for side in ("L", "R"):
            base_points = [(event.time, event.source.ref) for event in base if event.side == side]
            late_points = [(event.time, event.source.ref) for event in late if event.side == side]
            points = sorted(base_points + late_points, key=lambda item: (item[0], item[1]))
            envelope = base_points or late_points
            if envelope:
                expected_start = min(item[0] for item in envelope)
                expected_end = max(item[0] for item in envelope)
            else:
                expected_start, expected_end = crossing.start, crossing.end
            gap_ms = contiguity_gap_ms(crossing.speed_mps, BACKFILL_MAX_SPACING_M)
            timeline = channel_timeline(
                f"{pass_id}/{side}", expected_start, expected_end, points, gap_ms=gap_ms,
            )
            timeline["contiguity_gap_ms"] = gap_ms
            timelines.append(timeline)
    return sorted(timelines, key=lambda item: item["channel"])


def backfill_overview(passes, base_events, backfill_rows, conclusions):
    timelines = build_timelines(passes, base_events, backfill_rows)
    resume_at = safe_resume_at(timelines)
    waiting_passes = sorted({
        timeline["channel"].split("/")[0]
        for timeline in timelines if timeline["gaps"]
    })
    published = {item["pass_id"]: item for item in conclusions}
    pass_states = []
    for pass_id in passes:
        timeline_pair = [item for item in timelines if item["channel"].startswith(pass_id + "/")]
        state = "confirmed" if all(item["caught_up"] for item in timeline_pair) else "waiting"
        published_row = published.get(pass_id)
        pass_states.append({
            "pass_id": pass_id,
            "state": state,
            "watermarks": {item["channel"].split("/")[1]: item["watermark"] for item in timeline_pair},
            "gaps": sum(len(item["gaps"]) for item in timeline_pair),
            "published_version": published_row["version"] if published_row else None,
            "published_status": published_row["row"]["status"] if published_row else None,
        })
    return {
        "summary": coverage_summary(timelines),
        "safe_resume_at": resume_at,
        "waiting_passes": waiting_passes,
        "pass_states": pass_states,
        "timelines": timelines,
    }


def affected_passes(backfill_rows):
    return sorted({row.trigger.pass_id for row in backfill_rows if row.accepted})
