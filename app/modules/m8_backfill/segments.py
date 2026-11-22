"""观测区间合并与补传水位计算。

晚班控制器断网后，晚到的补传记录带着旧的采样时刻回到工作台。朴素做法是
"收到一条就把水位抬到该时刻"，中间没有证据的空档会被误判成已完整；一直等齐
再处理又会拖住下一班复核。这里把每个通道（车次 × 轮道）已观测到的时刻合并为
闭区间，只承认从通道期望起点开始、没有空档的前缀，水位停在第一个空档之前。
"""
from dataclasses import dataclass
from datetime import timedelta

# 缺省连续窗（毫秒）：通道没有车速档案时，按 10 m/s 走过最大轴距 12 m 兜底。
DEFAULT_GAP_MS = 1200


@dataclass(frozen=True)
class Segment:
    start: object
    end: object
    refs: tuple[str, ...]
    observed: int

    def as_wire(self):
        return {
            "start": self.start, "end": self.end, "observed": self.observed,
            "refs": list(self.refs), "kind": "confirmed" if self.refs else "gap",
        }


def contiguity_gap_ms(speed_mps, max_spacing_m):
    """按车次速度推算连续窗：走过最大合法轴距所需的毫秒数。"""
    if speed_mps is None:
        return DEFAULT_GAP_MS
    seconds = max_spacing_m / speed_mps
    return max(1, int(seconds * 1000))


def merge_observations(points, gap_ms=DEFAULT_GAP_MS, expected_start=None, expected_end=None):
    """把 (时刻, 源行引用) 序列合并为连续观测段与空档段。

    相邻观测时刻差不超过 gap_ms 时并入同一段；否则中间补一个空档段。
    空档段没有证据引用，refs 为空。期望窗口起点/终点与观测之间的缺证区间
    同样补为空档，避免把窗口边缘的缺失误判成已完整。
    """
    ordered = sorted(points, key=lambda item: (item[0], item[1]))
    if not ordered:
        return []

    observed_segments = []
    seg_start = seg_end = ordered[0][0]
    seg_refs = [ordered[0][1]]
    for time, ref in ordered[1:]:
        if time - seg_end <= timedelta(milliseconds=gap_ms):
            seg_end = time
            if ref not in seg_refs:
                seg_refs.append(ref)
        else:
            observed_segments.append(Segment(seg_start, seg_end, tuple(seg_refs), len(seg_refs)))
            seg_start = seg_end = time
            seg_refs = [ref]
    observed_segments.append(Segment(seg_start, seg_end, tuple(seg_refs), len(seg_refs)))

    result = []
    cursor = expected_start
    for segment in observed_segments:
        if cursor is not None and segment.start > cursor:
            result.append(Segment(cursor, segment.start, (), 0))
        result.append(segment)
        cursor = segment.end
    if expected_end is not None and cursor is not None and expected_end > cursor:
        result.append(Segment(cursor, expected_end, (), 0))
    return result


def channel_timeline(channel, expected_start, expected_end, points, gap_ms=DEFAULT_GAP_MS):
    """单通道时间线：观测段、空档段与完整水位。

    水位 watermark 是从期望起点开始的连续前缀末端；一旦遇到空档，水位停住，
    晚到记录落在水位之后只会补洞或形成孤岛，不会抬高水位。
    """
    segments = merge_observations(
        points, gap_ms=gap_ms, expected_start=expected_start, expected_end=expected_end,
    )
    watermark = expected_start
    prefix_open = True
    for segment in segments:
        if prefix_open and segment.refs:
            watermark = max(watermark, segment.end)
        elif not segment.refs:
            prefix_open = False
    return {
        "channel": channel,
        "expected_start": expected_start,
        "expected_end": expected_end,
        "watermark": watermark,
        "caught_up": watermark >= expected_end,
        "segments": [segment.as_wire() for segment in segments],
        "gaps": [segment.as_wire() for segment in segments if not segment.refs],
    }


def safe_resume_at(timelines):
    """可安全继续处理的起点 = 最早待补空档的左端点。

    所有通道都追平时返回 None，表示期望窗口内不存在待补时段。
    """
    candidates = [timeline["gaps"][0]["start"] for timeline in timelines if timeline["gaps"]]
    return min(candidates) if candidates else None


def coverage_summary(timelines):
    confirmed = sum(1 for timeline in timelines if timeline["caught_up"])
    waiting = len(timelines) - confirmed
    gap_count = sum(len(timeline["gaps"]) for timeline in timelines)
    observed = sum(sum(segment["observed"] for segment in timeline["segments"]) for timeline in timelines)
    return {
        "channels": len(timelines),
        "confirmed_channels": confirmed,
        "waiting_channels": waiting,
        "gap_segments": gap_count,
        "observed_points": observed,
    }
