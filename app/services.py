"""领域服务：登记、校准、撤回与小时等效声级重算。

所有写函数都要求调用方已持有写事务（BEGIN IMMEDIATE），
原始数据变更与小时结果重算在同一事务内提交或一起回滚。
"""
from __future__ import annotations

import math
import sqlite3
import time
from dataclasses import dataclass

from .schemas import CalibrationIn, ReadingIn, Role, SwitchIn, SwitchSegmentIn
from .time_utils import SECONDS_PER_HOUR, format_epoch_second, hours_touching

# dB 转声能时的参考基准，公共因子在平均后会抵消。
_ENERGY_REF_FACTOR = 0.1

# 单次换档窗口最长 24 小时。
MAX_SWITCH_WINDOW_SECONDS = 24 * SECONDS_PER_HOUR


class ServiceError(Exception):
    """服务层业务错误基类。"""


class NotFoundError(ServiceError):
    """编号不存在。"""


class ConflictError(ServiceError):
    """编号冲突 / 区间重叠 / 状态不允许变更。"""


class ValidationError(ServiceError):
    """入参语义不合法。"""


@dataclass(frozen=True)
class Reading:
    number: str
    point: str
    role: str
    start_ts: int
    end_ts: int
    db_value: float


@dataclass(frozen=True)
class Calibration:
    number: str
    point: str
    role: str
    start_ts: int
    end_ts: int
    offset_db: float
    withdrawn: bool


# --------------------------------------------------------------------------- #
# 行序列化
# --------------------------------------------------------------------------- #
def reading_out(row: sqlite3.Row) -> dict:
    return {
        "number": row["number"],
        "point": row["point"],
        "role": Role(row["role"]),
        "start": format_epoch_second(row["start_ts"]),
        "end": format_epoch_second(row["end_ts"]),
        "db": row["db_value"],
    }


def calibration_out(row: sqlite3.Row) -> dict:
    return {
        "number": row["number"],
        "point": row["point"],
        "role": Role(row["role"]),
        "start": format_epoch_second(row["start_ts"]),
        "end": format_epoch_second(row["end_ts"]),
        "offset": row["offset_db"],
        "withdrawn": bool(row["withdrawn"]),
    }


# --------------------------------------------------------------------------- #
# 通用校验
# --------------------------------------------------------------------------- #
def _validate_interval(start_ts: int, end_ts: int) -> None:
    if start_ts >= end_ts:
        raise ValidationError("区间必须满足 start < end，且为左闭右开的整秒区间")


def _find_reading_overlap(
    conn: sqlite3.Connection,
    point: str,
    role: Role,
    start_ts: int,
    end_ts: int,
    exclude_number: str | None = None,
) -> str | None:
    """返回与给定区间重叠的已有读数编号，没有则返回 None。"""
    sql = (
        "SELECT number FROM readings "
        "WHERE point = ? AND role = ? AND superseded = 0 "
        "AND start_ts < ? AND end_ts > ?"
    )
    params: list[object] = [point, role.value, end_ts, start_ts]
    if exclude_number is not None:
        sql += " AND number != ?"
        params.append(exclude_number)
    row = conn.execute(sql, params).fetchone()
    return row["number"] if row else None


def _find_calibration_overlap(
    conn: sqlite3.Connection,
    point: str,
    role: Role,
    start_ts: int,
    end_ts: int,
    exclude_number: str | None = None,
) -> str | None:
    """返回与给定区间重叠的有效（未撤回）校准单编号。"""
    sql = (
        "SELECT number FROM calibrations "
        "WHERE point = ? AND role = ? AND withdrawn = 0 "
        "AND start_ts < ? AND end_ts > ?"
    )
    params: list[object] = [point, role.value, end_ts, start_ts]
    if exclude_number is not None:
        sql += " AND number != ?"
        params.append(exclude_number)
    row = conn.execute(sql, params).fetchone()
    return row["number"] if row else None


# --------------------------------------------------------------------------- #
# 读数登记
# --------------------------------------------------------------------------- #
def register_reading(conn: sqlite3.Connection, number: str, payload: ReadingIn) -> tuple[dict, bool]:
    """登记读数。

    返回 (响应体, 是否新建)。同号原样重传视为幂等成功；同号内容不同则冲突。
    读数可补交（任意历史时刻），没有时效限制。

    「同号」覆盖换档后保留的原读数：旧设备按原编号原样重传仍返回原登记内容，
    同号改报则冲突；新编号若从未使用过则按新读数登记。
    """
    _validate_interval(payload.start, payload.end)
    existing = conn.execute(
        "SELECT * FROM readings WHERE number = ?", (number,)
    ).fetchone()
    now = int(time.time())

    if existing is not None:
        same = (
            existing["point"] == payload.point
            and existing["role"] == payload.role.value
            and existing["start_ts"] == payload.start
            and existing["end_ts"] == payload.end
            and float(existing["db_value"]) == float(payload.db)
        )
        if not same:
            raise ConflictError(
                f"读数编号 {number} 已存在且内容不同；同号只接受原样重传"
            )
        # 原样重传：返回原登记内容（换档后也不改成新档内容）。
        return reading_out(existing), False

    overlap = _find_reading_overlap(
        conn, payload.point, payload.role, payload.start, payload.end
    )
    if overlap is not None:
        raise ConflictError(
            f"与同仪器读数 {overlap} 的区间重叠，同一仪器读数区间不得重叠"
        )

    conn.execute(
        "INSERT INTO readings (number, point, role, start_ts, end_ts, db_value, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            number,
            payload.point,
            payload.role.value,
            payload.start,
            payload.end,
            payload.db,
            now,
        ),
    )
    row = conn.execute("SELECT * FROM readings WHERE number = ?", (number,)).fetchone()
    recompute_hours(
        conn, payload.point, hours_touching(payload.start, payload.end)
    )
    return reading_out(row), True


# --------------------------------------------------------------------------- #
# 换档：历史读数整批换成新平台编号与分段
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class _Segment:
    number: str
    start_ts: int
    end_ts: int
    db_value: float


def _validate_switch_window(start_ts: int, end_ts: int) -> None:
    _validate_interval(start_ts, end_ts)
    if end_ts - start_ts > MAX_SWITCH_WINDOW_SECONDS:
        raise ValidationError("换档窗口最长 24 小时（86400 秒）")


def _validate_switch_segments(
    segments: list[SwitchSegmentIn], window_start: int, window_end: int
) -> list[_Segment]:
    """校验新档各段：区间合法、落在窗口内、互不重叠、编号批内唯一。"""
    if not segments:
        raise ValidationError("新档至少需要一个段（number/start/end/db）")
    parsed: list[_Segment] = []
    seen_numbers: set[str] = set()
    for seg in segments:
        _validate_interval(seg.start, seg.end)
        if seg.start < window_start or seg.end > window_end:
            raise ValidationError(
                f"新档段 {seg.number} 必须完全落在窗口 "
                f"[{format_epoch_second(window_start)}, "
                f"{format_epoch_second(window_end)}) 内"
            )
        if seg.number in seen_numbers:
            raise ConflictError(f"新档编号 {seg.number} 在本次提交中重复")
        seen_numbers.add(seg.number)
        parsed.append(
            _Segment(
                number=seg.number,
                start_ts=seg.start,
                end_ts=seg.end,
                db_value=float(seg.db),
            )
        )

    parsed.sort(key=lambda item: item.start_ts)
    for previous, current in zip(parsed, parsed[1:]):
        if previous.end_ts > current.start_ts:
            raise ConflictError(
                f"新档段 {previous.number} 与 {current.number} 的区间重叠"
            )
    return parsed


def _assert_new_numbers_unused(conn: sqlite3.Connection, numbers: set[str]) -> None:
    """新编号必须从未用于任何读数（含换档后保留的原读数）。"""
    if not numbers:
        return
    placeholders = ", ".join("?" for _ in numbers)
    rows = conn.execute(
        f"SELECT number FROM readings WHERE number IN ({placeholders})",
        sorted(numbers),
    ).fetchall()
    if rows:
        raise ConflictError(
            f"新档编号 {rows[0]['number']} 已用于其他读数，换档编号必须是全新编号"
        )


def _switch_old_segments(
    conn: sqlite3.Connection, point: str, role: Role, window_start: int, window_end: int
) -> list[sqlite3.Row]:
    """取窗口内该仪器的有效读数；任何跨越窗口边界的读数都拒绝换档。"""
    rows = conn.execute(
        "SELECT * FROM readings WHERE point = ? AND role = ? AND superseded = 0 "
        "AND start_ts < ? AND end_ts > ? ORDER BY start_ts",
        (point, role.value, window_end, window_start),
    ).fetchall()
    for row in rows:
        if row["start_ts"] < window_start or row["end_ts"] > window_end:
            raise ConflictError(
                f"已有读数 {row['number']} 跨越换档窗口边界，不能整批切换"
            )
    return list(rows)


def _coverage_matches(
    old_rows: list[sqlite3.Row], new_segments: list[_Segment]
) -> bool:
    """逐秒比较旧档与新档：覆盖的秒必须完全相同，且每秒原始分贝相同。

    两边的区间各自互不重叠；按所有端点切分后逐片比较即可。
    """
    old = [(r["start_ts"], r["end_ts"], float(r["db_value"])) for r in old_rows]
    new = [(s.start_ts, s.end_ts, s.db_value) for s in new_segments]
    points: set[int] = set()
    for segments in (old, new):
        for start, end, _ in segments:
            points.add(start)
            points.add(end)
    if not points:
        return True

    ordered = sorted(points)
    old_index = new_index = 0
    for slice_start, slice_end in zip(ordered, ordered[1:]):
        while old_index < len(old) and old[old_index][1] <= slice_start:
            old_index += 1
        while new_index < len(new) and new[new_index][1] <= slice_start:
            new_index += 1
        old_db = (
            old[old_index][2]
            if old_index < len(old) and old[old_index][0] <= slice_start
            else None
        )
        new_db = (
            new[new_index][2]
            if new_index < len(new) and new[new_index][0] <= slice_start
            else None
        )
        if (old_db is None) != (new_db is None):
            return False
        if old_db is not None and not math.isclose(
            old_db, new_db, rel_tol=0.0, abs_tol=1e-9
        ):
            return False
    return True


def switch_readings(conn: sqlite3.Connection, payload: SwitchIn) -> dict:
    """把某测点某仪器的窗口内历史读数整批换成新平台编号与分段。

    校验全部通过后才落地：原读数标记为已被替换（仍保留用于原号重传），
    新档整批插入。小时结果沿用原值，不在换档时重算；后续校准变更会基于
    新档重算。任何校验或写入失败都由外层事务整体回滚。
    """
    window_start = payload.start
    window_end = payload.end
    if not payload.point:
        raise ValidationError("必须指定换档测点 point")
    _validate_switch_window(window_start, window_end)
    role = Role(payload.role)
    new_segments = _validate_switch_segments(
        payload.segments, window_start, window_end
    )
    _assert_new_numbers_unused(conn, {seg.number for seg in new_segments})
    for seg in payload.segments:
        if seg.point is not None and seg.point != payload.point:
            raise ValidationError(f"新档段 {seg.number} 的测点与换档测点不一致")
        if seg.role is not None and seg.role != role:
            raise ValidationError(f"新档段 {seg.number} 的主备角色与换档角色不一致")

    old_rows = _switch_old_segments(
        conn, payload.point, role, window_start, window_end
    )
    if not old_rows:
        raise ConflictError(
            "换档窗口内没有可切换的历史读数，请确认测点、主备角色与窗口"
        )
    if not _coverage_matches(old_rows, new_segments):
        raise ConflictError(
            "新旧档覆盖的秒或每秒原始分贝不一致，不能整批切换"
        )

    now = int(time.time())
    for row in old_rows:
        conn.execute(
            "UPDATE readings SET superseded = 1, superseded_at = ? WHERE id = ?",
            (now, row["id"]),
        )
    for seg in new_segments:
        conn.execute(
            "INSERT INTO readings (number, point, role, start_ts, end_ts, db_value, "
            "created_at, superseded) VALUES (?, ?, ?, ?, ?, ?, ?, 0)",
            (
                seg.number,
                payload.point,
                role.value,
                seg.start_ts,
                seg.end_ts,
                seg.db_value,
                now,
            ),
        )
    conn.execute(
        "INSERT INTO reading_switch_batches (point, role, window_start, window_end, "
        "superseded_count, inserted_count, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            payload.point,
            role.value,
            window_start,
            window_end,
            len(old_rows),
            len(new_segments),
            now,
        ),
    )

    # 小时结果沿用原值：不调用 recompute_hours。新旧档逐秒原始分贝相同，
    # 因此原结果在任何口径下都一致；后续校准变更时会按新档重算。
    ordered_rows: list[sqlite3.Row] = []
    ordered_input = list(new_segments)
    if ordered_input:
        rows = conn.execute(
            "SELECT * FROM readings WHERE number IN "
            f"({', '.join('?' for _ in ordered_input)})",
            [seg.number for seg in ordered_input],
        ).fetchall()
        by_number = {row["number"]: row for row in rows}
        ordered_rows = [by_number[seg.number] for seg in ordered_input]

    return {
        "point": payload.point,
        "role": role,
        "start": format_epoch_second(window_start),
        "end": format_epoch_second(window_end),
        "superseded": len(old_rows),
        "inserted": len(new_segments),
        "readings": [reading_out(row) for row in ordered_rows],
    }


# --------------------------------------------------------------------------- #
# 校准单登记 / 修改 / 撤回
# --------------------------------------------------------------------------- #
def upsert_calibration(
    conn: sqlite3.Connection, number: str, payload: CalibrationIn
) -> tuple[dict, bool]:
    """登记新校准单，或修改已有校准单的起止时刻与偏移。

    已撤回的编号不可复活，需使用新编号。
    """
    _validate_interval(payload.start, payload.end)
    existing = conn.execute(
        "SELECT * FROM calibrations WHERE number = ?", (number,)
    ).fetchone()
    now = int(time.time())

    if existing is not None:
        if existing["withdrawn"]:
            raise ConflictError(f"校准单 {number} 已撤回，不能修改或复活，请使用新编号")
        if payload.point != existing["point"] or payload.role.value != existing["role"]:
            raise ConflictError(
                f"校准单 {number} 的测点与主备角色不可修改，只能修改起止时刻和偏移"
            )

        overlap = _find_calibration_overlap(
            conn,
            payload.point,
            payload.role,
            payload.start,
            payload.end,
            exclude_number=number,
        )
        if overlap is not None:
            raise ConflictError(
                f"与有效校准单 {overlap} 的区间重叠，同一仪器校准区间不得重叠"
            )

        conn.execute(
            "UPDATE calibrations SET point = ?, role = ?, start_ts = ?, end_ts = ?, "
            "offset_db = ?, updated_at = ? WHERE number = ?",
            (
                payload.point,
                payload.role.value,
                payload.start,
                payload.end,
                payload.offset,
                now,
                number,
            ),
        )
        affected = set(hours_touching(existing["start_ts"], existing["end_ts"]))
        affected.update(hours_touching(payload.start, payload.end))
        recompute_hours(conn, payload.point, sorted(affected))
        row = conn.execute(
            "SELECT * FROM calibrations WHERE number = ?", (number,)
        ).fetchone()
        return calibration_out(row), False

    overlap = _find_calibration_overlap(
        conn, payload.point, payload.role, payload.start, payload.end
    )
    if overlap is not None:
        raise ConflictError(
            f"与有效校准单 {overlap} 的区间重叠，同一仪器校准区间不得重叠"
        )

    conn.execute(
        "INSERT INTO calibrations (number, point, role, start_ts, end_ts, offset_db, "
        "withdrawn, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)",
        (
            number,
            payload.point,
            payload.role.value,
            payload.start,
            payload.end,
            payload.offset,
            now,
            now,
        ),
    )
    row = conn.execute(
        "SELECT * FROM calibrations WHERE number = ?", (number,)
    ).fetchone()
    recompute_hours(conn, payload.point, hours_touching(payload.start, payload.end))
    return calibration_out(row), True


def withdraw_calibration(conn: sqlite3.Connection, number: str) -> dict:
    """撤回校准单（软删除）。撤回后其偏移不再参与任何小时计算。"""
    existing = conn.execute(
        "SELECT * FROM calibrations WHERE number = ?", (number,)
    ).fetchone()
    if existing is None:
        raise NotFoundError(f"校准单 {number} 不存在")
    if existing["withdrawn"]:
        # 幂等：重复撤回直接返回当前状态。
        return calibration_out(existing)

    now = int(time.time())
    conn.execute(
        "UPDATE calibrations SET withdrawn = 1, updated_at = ? WHERE number = ?",
        (now, number),
    )
    row = conn.execute(
        "SELECT * FROM calibrations WHERE number = ?", (number,)
    ).fetchone()
    recompute_hours(
        conn, existing["point"], hours_touching(existing["start_ts"], existing["end_ts"])
    )
    return calibration_out(row)


# --------------------------------------------------------------------------- #
# 小时等效声级计算
# --------------------------------------------------------------------------- #
def _active_calibrations(
    conn: sqlite3.Connection, point: str, role: str
) -> list[Calibration]:
    rows = conn.execute(
        "SELECT * FROM calibrations WHERE point = ? AND role = ? AND withdrawn = 0 "
        "ORDER BY start_ts",
        (point, role),
    ).fetchall()
    return [
        Calibration(
            number=r["number"],
            point=r["point"],
            role=r["role"],
            start_ts=r["start_ts"],
            end_ts=r["end_ts"],
            offset_db=r["offset_db"],
            withdrawn=False,
        )
        for r in rows
    ]


def _adjust_reading(
    reading: Reading, calibrations: list[Calibration]
) -> list[tuple[int, int, float]]:
    """把一条读数按校准区间切成若干 (起点, 终点, 叠加偏移后的分贝) 片段。

    校准单同仪器互不重叠，因此任一秒最多命中一个偏移；未命中校准的片段偏移为 0。
    """
    segments: list[tuple[int, int, float]] = []
    cursor = reading.start_ts
    for cal in calibrations:
        if cal.end_ts <= cursor:
            continue
        if cal.start_ts >= reading.end_ts:
            break
        overlap_start = max(cursor, cal.start_ts)
        overlap_end = min(reading.end_ts, cal.end_ts)
        if overlap_start < overlap_end:
            if cursor < overlap_start:
                segments.append((cursor, overlap_start, reading.db_value))
            segments.append(
                (overlap_start, overlap_end, reading.db_value + cal.offset_db)
            )
            cursor = overlap_end
    if cursor < reading.end_ts:
        segments.append((cursor, reading.end_ts, reading.db_value))
    return segments


def _compute_hour(
    conn: sqlite3.Connection, point: str, hour_start: int
) -> dict | None:
    """计算单个测点单个 UTC 小时的结果；无任何覆盖返回 None。"""
    hour_end = hour_start + SECONDS_PER_HOUR
    rows = conn.execute(
        "SELECT * FROM readings WHERE point = ? AND superseded = 0 "
        "AND start_ts < ? AND end_ts > ? ORDER BY start_ts, role",
        (point, hour_end, hour_start),
    ).fetchall()
    if not rows:
        return None

    calibrations = {
        Role.PRIMARY.value: _active_calibrations(conn, point, Role.PRIMARY.value),
        Role.BACKUP.value: _active_calibrations(conn, point, Role.BACKUP.value),
    }

    # chosen_energy[秒在小时内的偏移] = 该秒计入的声能；-1 表示尚未覆盖。
    chosen_energy: list[float] = [-1.0] * SECONDS_PER_HOUR
    chosen_role: list[int] = [0] * SECONDS_PER_HOUR  # 1=primary, 2=backup

    for row in rows:
        reading = Reading(
            number=row["number"],
            point=row["point"],
            role=row["role"],
            start_ts=row["start_ts"],
            end_ts=row["end_ts"],
            db_value=row["db_value"],
        )
        role_rank = 1 if reading.role == Role.PRIMARY.value else 2
        for seg_start, seg_end, adjusted_db in _adjust_reading(
            reading, calibrations[reading.role]
        ):
            lo = max(seg_start, hour_start)
            hi = min(seg_end, hour_end)
            energy = 10.0 ** (_ENERGY_REF_FACTOR * adjusted_db)
            for sec in range(lo - hour_start, hi - hour_start):
                # 主表优先：主表已覆盖的秒不再被备表替换；备表只补缺口。
                if chosen_energy[sec] < 0:
                    chosen_energy[sec] = energy
                    chosen_role[sec] = role_rank
                elif chosen_role[sec] > role_rank:
                    chosen_energy[sec] = energy
                    chosen_role[sec] = role_rank

    covered = [e for e in chosen_energy if e >= 0.0]
    if not covered:
        return None

    primary_seconds = sum(1 for r in chosen_role if r == 1)
    backup_seconds = sum(1 for r in chosen_role if r == 2)
    mean_energy = sum(covered) / len(covered)
    leq_db = 10.0 * math.log10(mean_energy)

    return {
        "point": point,
        "hour_start": hour_start,
        "leq_db": leq_db,
        "covered_seconds": len(covered),
        "primary_seconds": primary_seconds,
        "backup_seconds": backup_seconds,
    }


def recompute_hours(
    conn: sqlite3.Connection, point: str, hour_starts: list[int] | range
) -> None:
    """重算指定测点受影响的小时，并以新结果替换旧结果（无覆盖则删除旧行）。"""
    now = int(time.time())
    for hour_start in hour_starts:
        conn.execute(
            "DELETE FROM hourly_results WHERE point = ? AND hour_start = ?",
            (point, hour_start),
        )
        result = _compute_hour(conn, point, hour_start)
        if result is not None:
            conn.execute(
                "INSERT INTO hourly_results (point, hour_start, leq_db, covered_seconds, "
                "primary_seconds, backup_seconds, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    result["point"],
                    result["hour_start"],
                    result["leq_db"],
                    result["covered_seconds"],
                    result["primary_seconds"],
                    result["backup_seconds"],
                    now,
                ),
            )


# --------------------------------------------------------------------------- #
# 查询
# --------------------------------------------------------------------------- #
def get_hour_result(
    conn: sqlite3.Connection, point: str, hour_start: int
) -> dict | None:
    row = conn.execute(
        "SELECT * FROM hourly_results WHERE point = ? AND hour_start = ?",
        (point, hour_start),
    ).fetchone()
    if row is None:
        return None
    return {
        "point": row["point"],
        "hour_start": row["hour_start"],
        "leq_db": row["leq_db"],
        "covered_seconds": row["covered_seconds"],
        "primary_seconds": row["primary_seconds"],
        "backup_seconds": row["backup_seconds"],
    }


def list_hour_results(
    conn: sqlite3.Connection, point: str, start_hour: int, end_hour: int
) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM hourly_results WHERE point = ? AND hour_start >= ? AND hour_start < ? "
        "ORDER BY hour_start",
        (point, start_hour, end_hour),
    ).fetchall()
    return [
        {
            "point": r["point"],
            "hour_start": r["hour_start"],
            "leq_db": r["leq_db"],
            "covered_seconds": r["covered_seconds"],
            "primary_seconds": r["primary_seconds"],
            "backup_seconds": r["backup_seconds"],
        }
        for r in rows
    ]


def list_readings(conn: sqlite3.Connection, point: str | None) -> list[dict]:
    """明细查询：只列当前有效读数，换档窗口内仅剩新档。"""
    if point is None:
        rows = conn.execute(
            "SELECT * FROM readings WHERE superseded = 0 ORDER BY point, start_ts"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM readings WHERE point = ? AND superseded = 0 "
            "ORDER BY start_ts",
            (point,),
        ).fetchall()
    return [reading_out(r) for r in rows]


def list_calibrations(conn: sqlite3.Connection, point: str | None) -> list[dict]:
    if point is None:
        rows = conn.execute(
            "SELECT * FROM calibrations ORDER BY point, start_ts"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM calibrations WHERE point = ? ORDER BY start_ts", (point,)
        ).fetchall()
    return [calibration_out(r) for r in rows]
