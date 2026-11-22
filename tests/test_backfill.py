import io
from concurrent.futures import ThreadPoolExecutor

from app.config import ROOT


def upload(client, name, sample="dirty"):
    path = ROOT / "sample_data" / name
    with path.open("rb") as handle:
        return client.post(
            f"/api/backfill/packages?sample={sample}",
            files={"file": (name, handle.read(), "text/csv")},
            data={"sample": sample},
        )


def test_overview_starts_empty_and_lists_waiting_gaps(client):
    response = client.get("/api/backfill/overview")
    assert response.status_code == 200
    overview = response.json()["overview"]
    # 14 个通道（7 车次 × 左右），原始流水没有覆盖任何通道的期望窗口起点。
    assert overview["summary"]["channels"] == 14
    assert overview["safe_resume_at"] is not None
    assert overview["waiting_passes"]
    assert {state["pass_id"] for state in overview["pass_states"]} == {
        "0007", "0012", "0019", "0023", "0028", "0031", "0040",
    }


def test_submit_package_keeps_original_rows_and_recomputes_affected_passes(client):
    response = upload(client, "backfill-pack-a.csv")
    assert response.status_code == 201
    payload = response.json()
    assert payload["idempotent"] is False
    package = payload["package"]
    assert package["raw_rows"] == 5
    assert package["accepted_rows"] == 5
    assert package["rejected_rows"] == 0
    assert package["affected_passes"] == ["0019", "0023", "0028"]

    # 原行完整保留，且可以按包追溯到每条物理行。
    detail = client.get(f"/api/backfill/packages/{package['package_id']}").json()
    assert len(detail["rows"]) == 5
    first = detail["rows"][0]
    assert first["record_id"] == "0019-1L"
    assert first["accepted"] == 1
    assert "backfill-pack-a.csv:2" in first["source"]

    # 受影响车次都产生了第一版结论；等值重送的 0019 不产生静默覆盖。
    recomputed = {item["pass_id"]: item for item in payload["recomputed"]}
    assert set(recomputed) == {"0019", "0023", "0028"}
    assert all(item["new_conclusion"] for item in recomputed.values())
    assert all(item["version"] == 1 for item in recomputed.values())


def test_published_conclusion_is_versioned_not_silently_rewritten(client):
    upload(client, "backfill-pack-a.csv")
    history_before = client.get("/api/backfill/overview").json()["history"]
    version_one = history_before["0023"][0]["row"]

    # 第二个包给 0040 补轮、给 0023 重发已收记录；0023 结论不应被覆盖。
    second = upload(client, "backfill-pack-b.csv")
    assert second.status_code == 201
    history_after = client.get("/api/backfill/overview").json()["history"]
    assert history_after["0023"][0]["row"] == version_one

    # 0040 首次产生结论，版本为 1；坏行被隔离但不影响同包其他行。
    package_b = second.json()["package"]
    assert package_b["accepted_rows"] == 2
    assert package_b["rejected_rows"] == 1
    detail_b = client.get(f"/api/backfill/packages/{package_b['package_id']}").json()
    bad_row = next(row for row in detail_b["rows"] if not row["accepted"])
    assert bad_row["issue_code"] == "MISSING_VALUE"
    assert bad_row["record_id"] == "0028-5L"


def test_identical_package_rerun_is_idempotent(client):
    first = upload(client, "backfill-pack-a.csv").json()
    first_id = first["package"]["package_id"]
    before = client.get("/api/backfill/overview").json()

    second = upload(client, "backfill-pack-a.csv")
    assert second.status_code == 201
    payload = second.json()
    assert payload["idempotent"] is True
    assert payload["package"]["package_id"] == first_id
    assert payload["recomputed"] == []

    after = client.get("/api/backfill/overview").json()
    # 包数量、补传行数、水位、结论版本全部保持不变。
    assert len(after["packages"]) == len(before["packages"]) == 1
    assert len(after["rows"]) == len(before["rows"])
    assert after["overview"] == before["overview"]


def test_backfill_advances_watermark_and_exposes_safe_resume_point(client):
    first = upload(client, "backfill-pack-a.csv").json()["overview"]
    second = upload(client, "backfill-pack-b.csv").json()["overview"]

    # 补传后观测点增加，已确认通道数单调不减；安全起点始终是最早空档左边缘。
    assert second["summary"]["observed_points"] >= first["summary"]["observed_points"]
    assert second["summary"]["confirmed_channels"] >= first["summary"]["confirmed_channels"]
    assert second["safe_resume_at"] is not None
    # 0040 的失效证书与越窗触发形成真实内部空档，补传包无法覆盖，仍待补。
    waiting = next(
        item for item in second["pass_states"] if item["pass_id"] == "0040"
    )
    assert waiting["state"] == "waiting"
    timeline = next(item for item in second["timelines"] if item["channel"] == "0040/L")
    assert any(segment["kind"] == "gap" for segment in timeline["segments"])
    # 被补传覆盖的 0023 通道追平，没有残留空档。
    filled = next(item for item in second["timelines"] if item["channel"] == "0023/L")
    assert filled["caught_up"] is True


def test_unknown_pass_in_package_is_rejected_and_rolled_back(client):
    bogus = io.BytesIO(
        "记录号,车次,轮道,采样时间,载荷,单位,质态,备注\n"
        "9999-1L,9999,L,2026-11-20T20:09:00.100+08:00,1990,kg,OK,\n".encode("utf-8"),
    )
    response = client.post(
        "/api/backfill/packages",
        files={"file": ("bogus.csv", bogus, "text/csv")},
        data={"sample": "dirty"},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "PASS_UNKNOWN"
    # 整包回滚：没有留下任何包或补传行。
    overview = client.get("/api/backfill/overview").json()
    assert overview["packages"] == []
    assert overview["rows"] == []


def test_invalid_sample_and_oversized_package_are_rejected(client):
    reply = client.get("/api/backfill/overview?sample=absent")
    assert reply.status_code == 404
    big = io.BytesIO(b"x" * (1024 * 1024 + 1))
    response = client.post(
        "/api/backfill/packages",
        files={"file": ("big.csv", big, "text/csv")},
        data={"sample": "dirty"},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "SOURCE_TOO_LARGE"


def test_filling_internal_gap_advances_watermark_and_moves_safe_resume(client):
    before = client.get("/api/backfill/overview").json()["overview"]
    channel_before = next(item for item in before["timelines"] if item["channel"] == "0040/L")
    assert channel_before["watermark"].startswith("2026-11-20T12:06:00.000")
    assert channel_before["gaps"]

    fill = io.BytesIO((
        "记录号,车次,轮道,采样时间,载荷,单位,质态,备注\n"
        "0040-1Lb,0040,L,2026-11-20T20:06:00.100+08:00,1990,kg,OK,补洞\n"
    ).encode("utf-8"))
    response = client.post(
        "/api/backfill/packages",
        files={"file": ("fill.csv", fill, "text/csv")},
        data={"sample": "dirty"},
    )
    assert response.status_code == 201
    after = client.get("/api/backfill/overview").json()["overview"]
    channel_after = next(item for item in after["timelines"] if item["channel"] == "0040/L")
    # 水位只推进到连续前缀末端，剩余空档同步收缩。
    assert channel_after["watermark"].startswith("2026-11-20T12:06:00.100")
    assert channel_after["gaps"][0]["start"].startswith("2026-11-20T12:06:00.100")
    # 安全起点不会越过任何仍存在的空档。
    assert after["safe_resume_at"] <= channel_after["gaps"][0]["start"]


def test_concurrent_submission_of_same_package_processes_once(client):
    content = (ROOT / "sample_data" / "backfill-pack-a.csv").read_bytes()

    def post(_):
        return client.post(
            "/api/backfill/packages?sample=dirty",
            files={"file": ("backfill-pack-a.csv", content, "text/csv")},
            data={"sample": "dirty"},
        )

    with ThreadPoolExecutor(max_workers=4) as workers:
        results = list(workers.map(post, range(4)))
    created = sum(response.status_code == 201 for response in results)
    assert created == 4  # 幂等重提同样返回 201，仅其中一次真正落库
    payloads = [response.json() for response in results]
    assert len({payload["package"]["package_id"] for payload in payloads}) == 1
    assert sum(payload["idempotent"] for payload in payloads) == 3
    dashboard = client.get("/api/backfill/overview").json()
    assert len(dashboard["packages"]) == 1
    assert len(dashboard["rows"]) == 5
    # 结论版本没有因并发重提而重复。
    assert all(len(versions) == 1 for versions in dashboard["history"].values() if versions)
