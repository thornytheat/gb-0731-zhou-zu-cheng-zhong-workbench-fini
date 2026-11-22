from datetime import UTC, datetime, timedelta

from app.modules.m8_backfill.segments import (
    channel_timeline,
    merge_observations,
    safe_resume_at,
)


def t(ms):
    return datetime(2026, 11, 20, 12, 0, 0, tzinfo=UTC) + timedelta(milliseconds=ms)


def test_merged_points_become_one_confirmed_segment_within_gap():
    segments = merge_observations([(t(100), "f:1"), (t(108), "f:2"), (t(112), "f:3")], gap_ms=12)
    assert len(segments) == 1
    assert segments[0].start == t(100)
    assert segments[0].end == t(112)
    assert segments[0].observed == 3


def test_late_arrival_does_not_lift_watermark_across_gap():
    # 已观测 100ms 与 400ms，窗口起点到 100ms 即有空档；水位停在期望起点，
    # 晚到的 400ms 记录不能让水位越过任何空档。
    timeline = channel_timeline("0007/L", t(0), t(900), [(t(100), "f:1"), (t(400), "f:9")], gap_ms=12)
    assert timeline["watermark"] == t(0)
    assert timeline["caught_up"] is False
    kinds = [segment["kind"] for segment in timeline["segments"]]
    assert kinds == ["gap", "confirmed", "gap", "confirmed", "gap"]
    middle = next(segment for segment in timeline["segments"] if segment["start"] == t(100) and segment["kind"] == "gap")
    assert middle["end"] == t(400)


def test_watermark_advances_when_prefix_is_covered():
    # 观测从期望起点开始且内部连续，水位推进到前缀观测段末端。
    timeline = channel_timeline(
        "0007/L", t(0), t(900), [(t(0), "f:0"), (t(8), "f:1"), (t(400), "f:9")], gap_ms=12,
    )
    assert timeline["watermark"] == t(8)
    assert timeline["gaps"][0]["start"] == t(8)
    assert timeline["gaps"][0]["end"] == t(400)


def test_filling_the_gap_advances_watermark_to_prefix_end():
    points = [(t(0), "f:0"), (t(100), "f:1"), (t(108), "f:2"), (t(400), "f:9")]
    before = channel_timeline("0007/L", t(0), t(900), points, gap_ms=12)
    assert before["watermark"] == t(0)
    points.append((t(12), "bf:1"))
    after = channel_timeline("0007/L", t(0), t(900), points, gap_ms=12)
    # 起点处空档补齐到 t(12)，但 t(12)→t(100) 仍有 88ms 空档，水位停在 t(12)。
    assert after["watermark"] == t(12)
    assert after["gaps"][0]["start"] == t(12)
    # 再用一串间距不超过连续窗的补传观测把 12ms→100ms 桥接起来。
    for bridge_ms in (24, 36, 48, 60, 72, 84, 96):
        points.append((t(bridge_ms), f"bf:{bridge_ms}"))
    bridged = channel_timeline("0007/L", t(0), t(900), points, gap_ms=12)
    assert bridged["watermark"] == t(108)
    assert bridged["gaps"][0]["start"] == t(108)


def test_safe_resume_is_earliest_gap_left_edge_across_channels():
    # 两通道都在期望起点之后才出现首个观测，最早空档左边缘都是窗口起点。
    first = channel_timeline("a/L", t(0), t(900), [(t(300), "f:1")], gap_ms=12)
    second = channel_timeline("b/R", t(0), t(900), [(t(150), "f:2")], gap_ms=12)
    assert safe_resume_at([first, second]) == t(0)
    # 起点已覆盖但存在内部空档时，安全起点等于该内部空档左边缘。
    internal = channel_timeline("b/R", t(0), t(900), [(t(0), "f:2"), (t(300), "f:3")], gap_ms=12)
    assert internal["gaps"][0]["start"] == t(0)
    assert safe_resume_at([internal]) == t(0)

def test_safe_resume_none_when_every_channel_caught_up():
    # 观测覆盖整个期望窗口且内部间隔不超过连续窗。
    timeline = channel_timeline("c/L", t(0), t(10), [(t(0), "f:3"), (t(10), "f:4")], gap_ms=12)
    assert timeline["gaps"] == []
    assert timeline["caught_up"] is True
    assert safe_resume_at([timeline]) is None


def test_empty_channel_whole_window_is_waiting():
    timeline = channel_timeline("x/R", t(0), t(900), [])
    assert timeline["segments"] == []
    assert timeline["watermark"] == t(0)
    assert timeline["caught_up"] is False
