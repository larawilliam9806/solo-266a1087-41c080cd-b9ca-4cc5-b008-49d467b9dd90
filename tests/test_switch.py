"""换档：历史读数整批换成新平台编号与分段。"""
from __future__ import annotations

import math

HOUR = "2026-11-21T08:00:00Z"
NEXT = "2026-11-21T09:00:00Z"
T15 = "2026-11-21T08:15:00Z"
T30 = "2026-11-21T08:30:00Z"
T45 = "2026-11-21T08:45:00Z"


def _reading(number, point, role, start, end, db):
    return {"point": point, "role": role, "start": start, "end": end, "db": db}


def _switch(point, role, start, end, segments):
    return {
        "point": point,
        "role": role,
        "start": start,
        "end": end,
        "segments": segments,
    }


def _seg(number, start, end, db):
    return {"number": number, "start": start, "end": end, "db": db}


def _seed_one_hour(client):
    client.put(
        "/readings/OLD-1",
        json=_reading("OLD-1", "P1", "primary", HOUR, NEXT, 60.0),
    )


def test_switch_replaces_segments_and_detail_in_window(client):
    _seed_one_hour(client)
    resp = client.post(
        "/readings/switch",
        json=_switch(
            "P1", "primary", HOUR, NEXT,
            [
                _seg("NEW-1", HOUR, T30, 60.0),
                _seg("NEW-2", T30, NEXT, 60.0),
            ],
        ),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["superseded"] == 1
    assert body["inserted"] == 2
    assert [r["number"] for r in body["readings"]] == ["NEW-1", "NEW-2"]

    listed = client.get("/readings?point=P1").json()
    assert [r["number"] for r in listed] == ["NEW-1", "NEW-2"]

    # 小时结果沿用原值。
    hour = client.get(f"/points/P1/hours/{HOUR}").json()
    assert abs(hour["leq_db"] - 60.0) < 1e-9
    assert hour["covered_seconds"] == 3600
    assert hour["primary_seconds"] == 3600


def test_switch_allows_resegmentation_with_identical_per_second_db(client):
    client.put(
        "/readings/OLD-1",
        json=_reading("OLD-1", "P1", "primary", HOUR, NEXT, 60.0),
    )
    resp = client.post(
        "/readings/switch",
        json=_switch(
            "P1", "primary", HOUR, NEXT,
            [
                _seg("NEW-A", HOUR, T15, 60.0),
                _seg("NEW-B", T15, T45, 60.0),
                _seg("NEW-C", T45, NEXT, 60.0),
            ],
        ),
    )
    assert resp.status_code == 200, resp.text
    assert len(client.get("/readings?point=P1").json()) == 3


def test_switch_rejects_unequal_cover_or_db(client):
    client.put(
        "/readings/OLD-1",
        json=_reading("OLD-1", "P1", "primary", HOUR, T30, 60.0),
    )
    # 覆盖秒不同：新档多覆盖了 15 分钟。
    resp = client.post(
        "/readings/switch",
        json=_switch(
            "P1", "primary", HOUR, NEXT,
            [_seg("NEW-1", HOUR, T45, 60.0)],
        ),
    )
    assert resp.status_code == 409

    # 覆盖相同但分贝不同。
    resp = client.post(
        "/readings/switch",
        json=_switch(
            "P1", "primary", HOUR, NEXT,
            [_seg("NEW-1", HOUR, T30, 59.9)],
        ),
    )
    assert resp.status_code == 409

    # 缺口不同：旧档只覆盖前半小时，新档补了中间。
    resp = client.post(
        "/readings/switch",
        json=_switch(
            "P1", "primary", HOUR, NEXT,
            [
                _seg("NEW-1", HOUR, T30, 60.0),
                _seg("NEW-2", T45, NEXT, 60.0),
            ],
        ),
    )
    assert resp.status_code == 409

    # 失败后原明细完整保留。
    listed = client.get("/readings?point=P1").json()
    assert [r["number"] for r in listed] == ["OLD-1"]


def test_switch_includes_backfilled_readings_in_window(client):
    client.put(
        "/readings/OLD-1",
        json=_reading("OLD-1", "P1", "primary", HOUR, T30, 60.0),
    )
    # 窗口内普通补报先入库，也必须参与换档核对。
    client.put(
        "/readings/OLD-2",
        json=_reading("OLD-2", "P1", "primary", T30, NEXT, 70.0),
    )
    mismatch = client.post(
        "/readings/switch",
        json=_switch(
            "P1", "primary", HOUR, NEXT,
            [
                _seg("NEW-1", HOUR, T30, 60.0),
                _seg("NEW-2", T30, NEXT, 60.0),
            ],
        ),
    )
    assert mismatch.status_code == 409

    ok = client.post(
        "/readings/switch",
        json=_switch(
            "P1", "primary", HOUR, NEXT,
            [
                _seg("NEW-1", HOUR, T30, 60.0),
                _seg("NEW-2", T30, NEXT, 70.0),
            ],
        ),
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["superseded"] == 2
    listed = client.get("/readings?point=P1").json()
    assert [r["number"] for r in listed] == ["NEW-1", "NEW-2"]


def test_switch_requires_window_containment_and_no_boundary_crossing(client):
    # 新档段越出窗口。
    client.put(
        "/readings/OLD-1",
        json=_reading("OLD-1", "P1", "primary", HOUR, NEXT, 60.0),
    )
    resp = client.post(
        "/readings/switch",
        json=_switch(
            "P1", "primary", HOUR, NEXT,
            [_seg("NEW-1", HOUR, "2026-11-21T09:00:01Z", 60.0)],
        ),
    )
    assert resp.status_code == 400

    # 已有读数跨越窗口右边界。
    resp = client.post(
        "/readings/switch",
        json=_switch(
            "P1", "primary", "2026-11-21T07:00:00Z", T30,
            [_seg("NEW-1", "2026-11-21T07:00:00Z", T30, 60.0)],
        ),
    )
    assert resp.status_code == 409
    assert [r["number"] for r in client.get("/readings?point=P1").json()] == ["OLD-1"]

    # 窗口超过 24 小时。
    resp = client.post(
        "/readings/switch",
        json=_switch(
            "P1", "primary", "2026-11-20T08:00:00Z", "2026-11-21T09:00:00Z",
            [_seg("NEW-1", "2026-11-20T08:00:00Z", "2026-11-21T09:00:00Z", 60.0)],
        ),
    )
    assert resp.status_code == 400


def test_switch_rejects_segment_overlap_duplicate_and_used_numbers(client):
    client.put(
        "/readings/OLD-1",
        json=_reading("OLD-1", "P1", "primary", HOUR, NEXT, 60.0),
    )
    # 新档内部重叠。
    resp = client.post(
        "/readings/switch",
        json=_switch(
            "P1", "primary", HOUR, NEXT,
            [
                _seg("NEW-1", HOUR, T45, 60.0),
                _seg("NEW-2", T30, NEXT, 60.0),
            ],
        ),
    )
    assert resp.status_code == 409

    # 批内编号重复。
    resp = client.post(
        "/readings/switch",
        json=_switch(
            "P1", "primary", HOUR, NEXT,
            [
                _seg("NEW-1", HOUR, T30, 60.0),
                _seg("NEW-1", T30, NEXT, 60.0),
            ],
        ),
    )
    assert resp.status_code == 409

    # 复用已有编号（即使属于其他测点）。
    client.put(
        "/readings/OTHER",
        json=_reading("OTHER", "P2", "primary", HOUR, NEXT, 50.0),
    )
    resp = client.post(
        "/readings/switch",
        json=_switch(
            "P1", "primary", HOUR, NEXT,
            [
                _seg("OTHER", HOUR, T30, 60.0),
                _seg("NEW-2", T30, NEXT, 60.0),
            ],
        ),
    )
    assert resp.status_code == 409

    # 任何失败都不改变原明细。
    assert [r["number"] for r in client.get("/readings?point=P1").json()] == ["OLD-1"]


def test_switch_old_number_retransmission_keeps_original_content(client):
    _seed_one_hour(client)
    client.post(
        "/readings/switch",
        json=_switch(
            "P1", "primary", HOUR, NEXT,
            [_seg("NEW-1", HOUR, NEXT, 60.0)],
        ),
    )

    # 旧设备按原编号原样重传：返回原登记内容，不受换档影响。
    original = client.put(
        "/readings/OLD-1",
        json=_reading("OLD-1", "P1", "primary", HOUR, NEXT, 60.0),
    )
    assert original.status_code == 200
    assert original.json()["number"] == "OLD-1"
    assert original.json()["db"] == 60.0

    # 同号改报 -> 409，且不污染现有明细。
    changed = client.put(
        "/readings/OLD-1",
        json=_reading("OLD-1", "P1", "primary", HOUR, T30, 61.0),
    )
    assert changed.status_code == 409
    assert [r["number"] for r in client.get("/readings?point=P1").json()] == ["NEW-1"]

    # 已用于旧读数的编号不能作为新编号再次换档。
    resp = client.post(
        "/readings/switch",
        json=_switch(
            "P1", "primary", HOUR, NEXT,
            [_seg("OLD-1", HOUR, NEXT, 60.0)],
        ),
    )
    assert resp.status_code == 409


def test_switch_hour_kept_then_calibration_recomputes_from_new_segments(client):
    _seed_one_hour(client)
    client.post(
        "/readings/switch",
        json=_switch(
            "P1", "primary", HOUR, NEXT,
            [_seg("NEW-1", HOUR, NEXT, 60.0)],
        ),
    )
    before = client.get(f"/points/P1/hours/{HOUR}").json()
    assert abs(before["leq_db"] - 60.0) < 1e-9

    # 后续新增校准单：按新档核算，且只重算一次。
    resp = client.put(
        "/calibrations/C1",
        json={
            "point": "P1", "role": "primary",
            "start": HOUR, "end": T30, "offset": 3.0,
        },
    )
    assert resp.status_code == 200
    after = client.get(f"/points/P1/hours/{HOUR}").json()
    expected = 10 * math.log10((1800 * 10 ** 6.3 + 1800 * 10 ** 6.0) / 3600)
    assert abs(after["leq_db"] - expected) < 1e-9

    # 修改校准单，仍以新档重算。
    client.put(
        "/calibrations/C1",
        json={
            "point": "P1", "role": "primary",
            "start": HOUR, "end": T30, "offset": -10.0,
        },
    )
    modified = client.get(f"/points/P1/hours/{HOUR}").json()
    expected = 10 * math.log10((1800 * 10 ** 5.0 + 1800 * 10 ** 6.0) / 3600)
    assert abs(modified["leq_db"] - expected) < 1e-9

    # 撤回校准单回到原始值。
    client.post("/calibrations/C1/withdraw")
    restored = client.get(f"/points/P1/hours/{HOUR}").json()
    assert abs(restored["leq_db"] - 60.0) < 1e-9


def test_switch_isolated_by_role_and_point(client):
    client.put(
        "/readings/OLD-P",
        json=_reading("OLD-P", "P1", "primary", HOUR, NEXT, 60.0),
    )
    client.put(
        "/readings/OLD-B",
        json=_reading("OLD-B", "P1", "backup", HOUR, NEXT, 55.0),
    )
    # 切换主表不应影响备表。
    resp = client.post(
        "/readings/switch",
        json=_switch(
            "P1", "primary", HOUR, NEXT,
            [_seg("NEW-P", HOUR, NEXT, 60.0)],
        ),
    )
    assert resp.status_code == 200, resp.text
    listed = client.get("/readings?point=P1").json()
    assert sorted(r["number"] for r in listed) == ["NEW-P", "OLD-B"]


def test_switch_atomic_on_late_insert_failure(client):
    client.put(
        "/readings/OLD-1",
        json=_reading("OLD-1", "P1", "primary", HOUR, NEXT, 60.0),
    )
    resp = client.post(
        "/readings/switch",
        json=_switch(
            "P1", "primary", HOUR, NEXT,
            [
                _seg("NEW-1", HOUR, T30, 60.0),
                _seg("NEW-2", T30, T45, 60.0),
                _seg("NEW-3", T45, NEXT, 60.9),
            ],
        ),
    )
    assert resp.status_code == 409
    assert [r["number"] for r in client.get("/readings?point=P1").json()] == ["OLD-1"]
    hour = client.get(f"/points/P1/hours/{HOUR}").json()
    assert abs(hour["leq_db"] - 60.0) < 1e-9
    # 原号重传仍然有效。
    assert client.put(
        "/readings/OLD-1",
        json=_reading("OLD-1", "P1", "primary", HOUR, NEXT, 60.0),
    ).status_code == 200


def test_switch_keeps_preexisting_calibration_result_then_withdraw_uses_new(client):
    client.put(
        "/readings/OLD-1",
        json=_reading("OLD-1", "P1", "primary", HOUR, NEXT, 60.0),
    )
    client.put(
        "/calibrations/C1",
        json={
            "point": "P1", "role": "primary",
            "start": HOUR, "end": T30, "offset": 3.0,
        },
    )
    before = client.get(f"/points/P1/hours/{HOUR}").json()
    expected = 10 * math.log10((1800 * 10 ** 6.3 + 1800 * 10 ** 6.0) / 3600)
    assert abs(before["leq_db"] - expected) < 1e-9

    # 换档不改小时结果，即便校准已存在。
    resp = client.post(
        "/readings/switch",
        json=_switch(
            "P1", "primary", HOUR, NEXT,
            [
                _seg("NEW-1", HOUR, T15, 60.0),
                _seg("NEW-2", T15, NEXT, 60.0),
            ],
        ),
    )
    assert resp.status_code == 200, resp.text
    kept = client.get(f"/points/P1/hours/{HOUR}").json()
    assert kept["leq_db"] == before["leq_db"]
    assert kept["covered_seconds"] == before["covered_seconds"]

    # 撤回校准单，按新档重算回 60 dB。
    client.post("/calibrations/C1/withdraw")
    restored = client.get(f"/points/P1/hours/{HOUR}").json()
    assert abs(restored["leq_db"] - 60.0) < 1e-9


def test_switch_multi_hour_window_and_out_of_order_segments(client):
    client.put(
        "/readings/OLD-1",
        json=_reading("OLD-1", "P1", "primary", HOUR, "2026-11-21T10:00:00Z", 60.0),
    )
    first = client.get(f"/points/P1/hours/{HOUR}").json()
    second = client.get("/points/P1/hours/2026-11-21T09:00:00Z").json()

    # 段按乱序提交也应接受。
    resp = client.post(
        "/readings/switch",
        json=_switch(
            "P1", "primary", HOUR, "2026-11-21T10:00:00Z",
            [
                _seg("NEW-2", NEXT, "2026-11-21T10:00:00Z", 60.0),
                _seg("NEW-1", HOUR, NEXT, 60.0),
            ],
        ),
    )
    assert resp.status_code == 200, resp.text
    assert [r["number"] for r in resp.json()["readings"]] == ["NEW-1", "NEW-2"]
    assert client.get(f"/points/P1/hours/{HOUR}").json()["leq_db"] == first["leq_db"]
    assert (
        client.get("/points/P1/hours/2026-11-21T09:00:00Z").json()["leq_db"]
        == second["leq_db"]
    )


def test_switch_allows_readings_touching_window_boundary(client):
    client.put(
        "/readings/BEFORE",
        json=_reading("BEFORE", "P1", "primary", "2026-11-21T07:59:00Z", HOUR, 50.0),
    )
    client.put(
        "/readings/INSIDE",
        json=_reading("INSIDE", "P1", "primary", HOUR, NEXT, 60.0),
    )
    client.put(
        "/readings/AFTER",
        json=_reading("AFTER", "P1", "primary", NEXT, "2026-11-21T09:01:00Z", 70.0),
    )
    resp = client.post(
        "/readings/switch",
        json=_switch(
            "P1", "primary", HOUR, NEXT,
            [_seg("NEW-1", HOUR, NEXT, 60.0)],
        ),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["superseded"] == 1
    assert sorted(
        r["number"] for r in client.get("/readings?point=P1").json()
    ) == ["AFTER", "BEFORE", "NEW-1"]


def test_switch_old_number_retransmission_with_different_identity_conflicts(client):
    _seed_one_hour(client)
    client.post(
        "/readings/switch",
        json=_switch(
            "P1", "primary", HOUR, NEXT,
            [_seg("NEW-1", HOUR, NEXT, 60.0)],
        ),
    )
    # 原号改测点 / 改角色 / 改区间 / 改分贝均为改报 -> 409。
    for changed in [
        _reading("OLD-1", "P2", "primary", HOUR, NEXT, 60.0),
        _reading("OLD-1", "P1", "backup", HOUR, NEXT, 60.0),
        _reading("OLD-1", "P1", "primary", HOUR, T30, 60.0),
        _reading("OLD-1", "P1", "primary", HOUR, NEXT, 61.0),
    ]:
        resp = client.put("/readings/OLD-1", json=changed)
        assert resp.status_code == 409
    assert [r["number"] for r in client.get("/readings?point=P1").json()] == ["NEW-1"]
