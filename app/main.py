"""FastAPI 应用与路由。"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Query, status
from sqlite3 import IntegrityError, OperationalError

from . import services
from .config import MAX_HOUR_RANGE
from .database import connect, init_db, write_transaction
from .schemas import (
    CalibrationIn,
    CalibrationOut,
    HourlyRangeOut,
    HourlyResultOut,
    MessageOut,
    ReadingIn,
    ReadingOut,
    SwitchIn,
    SwitchResultOut,
)
from .time_utils import (
    SECONDS_PER_HOUR,
    TimeFormatError,
    format_hour_start,
    parse_hour_param,
)

@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="噪声小时等效声级监测 API",
    version="1.0.0",
    description=(
        "监测员按测点登记读数与校准单，服务按 UTC 小时计算等效声级 Leq，"
        "并统计主表 / 备表各计入的秒数。"
    ),
    lifespan=lifespan,
)


def get_conn() -> Iterator:
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


def _service_error(exc: Exception) -> HTTPException:
    if isinstance(exc, services.NotFoundError):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    if isinstance(exc, services.ConflictError):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    if isinstance(exc, (services.ValidationError, TimeFormatError)):
        return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    raise exc


def _hour_out(result: dict) -> HourlyResultOut:
    return HourlyResultOut(
        point=result["point"],
        hour=format_hour_start(result["hour_start"]),
        leq_db=result["leq_db"],
        covered_seconds=result["covered_seconds"],
        primary_seconds=result["primary_seconds"],
        backup_seconds=result["backup_seconds"],
    )


# --------------------------------------------------------------------------- #
# 健康检查
# --------------------------------------------------------------------------- #
@app.get("/health", response_model=MessageOut, tags=["系统"])
def health() -> dict[str, str]:
    return {"detail": "ok"}


# --------------------------------------------------------------------------- #
# 读数
# --------------------------------------------------------------------------- #
@app.put(
    "/readings/{number}",
    response_model=ReadingOut,
    status_code=status.HTTP_200_OK,
    tags=["读数"],
    responses={
        409: {"model": MessageOut, "description": "同号内容不同，或同仪器区间重叠"},
        400: {"model": MessageOut, "description": "区间 / 时间格式不合法"},
    },
)
def put_reading(
    number: str, payload: ReadingIn, conn=Depends(get_conn)
) -> ReadingOut:
    try:
        with write_transaction(conn):
            body, created = services.register_reading(conn, number, payload)
        return ReadingOut(**body)
    except (services.ServiceError, TimeFormatError) as exc:
        raise _service_error(exc)
    except IntegrityError as exc:
        # 并发下同号插入竞态：写事务已回滚，原明细与重传记录保持不变。
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"编号冲突，写入已回滚: {exc}",
        )
    except OperationalError as exc:
        # 锁等待超时等：事务已回滚，原始数据与小时结果保持不变。
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"数据库暂时不可写，请重试: {exc}",
        )


@app.get("/readings", response_model=list[ReadingOut], tags=["读数"])
def get_readings(
    point: str | None = Query(default=None, description="按测点过滤"),
    conn=Depends(get_conn),
) -> list[ReadingOut]:
    return [ReadingOut(**row) for row in services.list_readings(conn, point)]


_SWITCH_RESPONSES = {
    409: {
        "model": MessageOut,
        "description": "编号重复/已用、区间重叠、跨界读数或新旧档不一致",
    },
    400: {"model": MessageOut, "description": "窗口或时间段不合法"},
}


def _switch(payload: SwitchIn, conn) -> SwitchResultOut:
    try:
        with write_transaction(conn):
            body = services.switch_readings(conn, payload)
        return SwitchResultOut(**body)
    except (services.ServiceError, TimeFormatError) as exc:
        raise _service_error(exc)
    except IntegrityError as exc:
        # 并发下同号插入竞态：写事务已回滚，原明细与重传记录保持不变。
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"编号冲突，写入已回滚: {exc}",
        )
    except OperationalError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"数据库暂时不可写，请重试: {exc}",
        )


@app.post(
    "/readings/switch",
    response_model=SwitchResultOut,
    status_code=status.HTTP_200_OK,
    tags=["读数"],
    responses=_SWITCH_RESPONSES,
)
@app.post(
    "/readings/switchover",
    response_model=SwitchResultOut,
    status_code=status.HTTP_200_OK,
    tags=["读数"],
    include_in_schema=False,
    responses=_SWITCH_RESPONSES,
)
@app.post(
    "/readings/replace",
    response_model=SwitchResultOut,
    status_code=status.HTTP_200_OK,
    tags=["读数"],
    include_in_schema=False,
    responses=_SWITCH_RESPONSES,
)
@app.post(
    "/readings/migrate",
    response_model=SwitchResultOut,
    status_code=status.HTTP_200_OK,
    tags=["读数"],
    include_in_schema=False,
    responses=_SWITCH_RESPONSES,
)
@app.post(
    "/readings/renumber",
    response_model=SwitchResultOut,
    status_code=status.HTTP_200_OK,
    tags=["读数"],
    include_in_schema=False,
    responses=_SWITCH_RESPONSES,
)
@app.post(
    "/switchovers",
    response_model=SwitchResultOut,
    status_code=status.HTTP_200_OK,
    tags=["读数"],
    include_in_schema=False,
    responses=_SWITCH_RESPONSES,
)
def readings_switch(
    payload: SwitchIn, conn=Depends(get_conn)
) -> SwitchResultOut:
    """换档：把某测点某仪器窗口内的历史读数整批换成新平台编号与分段。"""
    return _switch(payload, conn)


@app.post(
    "/points/{point}/readings/switch",
    response_model=SwitchResultOut,
    status_code=status.HTTP_200_OK,
    tags=["读数"],
    include_in_schema=False,
    responses=_SWITCH_RESPONSES,
)
def post_point_readings_switch(
    point: str, payload: SwitchIn, conn=Depends(get_conn)
) -> SwitchResultOut:
    """换档（测点取自路径）：请求体中的 point 可省略，若填写必须一致。"""
    if payload.point is not None and payload.point != point:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"路径测点 {point} 与请求体测点 {payload.point} 不一致",
        )
    payload = payload.model_copy(update={"point": point})
    return _switch(payload, conn)


# --------------------------------------------------------------------------- #
# 校准单
# --------------------------------------------------------------------------- #
@app.put(
    "/calibrations/{number}",
    response_model=CalibrationOut,
    tags=["校准单"],
    responses={
        409: {"model": MessageOut, "description": "区间重叠，或校准单已撤回"},
        400: {"model": MessageOut, "description": "区间 / 时间格式不合法"},
    },
)
def put_calibration(
    number: str, payload: CalibrationIn, conn=Depends(get_conn)
) -> CalibrationOut:
    try:
        with write_transaction(conn):
            body, _created = services.upsert_calibration(conn, number, payload)
        return CalibrationOut(**body)
    except (services.ServiceError, TimeFormatError) as exc:
        raise _service_error(exc)
    except OperationalError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"数据库暂时不可写，请重试: {exc}",
        )


@app.post(
    "/calibrations/{number}/withdraw",
    response_model=CalibrationOut,
    tags=["校准单"],
    responses={404: {"model": MessageOut, "description": "校准单不存在"}},
)
def post_withdraw_calibration(
    number: str, conn=Depends(get_conn)
) -> CalibrationOut:
    try:
        with write_transaction(conn):
            body = services.withdraw_calibration(conn, number)
        return CalibrationOut(**body)
    except (services.ServiceError, TimeFormatError) as exc:
        raise _service_error(exc)
    except OperationalError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"数据库暂时不可写，请重试: {exc}",
        )


@app.get("/calibrations", response_model=list[CalibrationOut], tags=["校准单"])
def get_calibrations(
    point: str | None = Query(default=None, description="按测点过滤"),
    include_withdrawn: bool = Query(default=True, description="是否包含已撤回单据"),
    conn=Depends(get_conn),
) -> list[CalibrationOut]:
    rows = services.list_calibrations(conn, point)
    if not include_withdrawn:
        rows = [r for r in rows if not r["withdrawn"]]
    return [CalibrationOut(**r) for r in rows]


# --------------------------------------------------------------------------- #
# 小时结果查询
# --------------------------------------------------------------------------- #
@app.get(
    "/points/{point}/hours/{hour}",
    response_model=HourlyResultOut,
    tags=["小时结果"],
    responses={
        404: {"model": MessageOut, "description": "该小时无覆盖，无数据"},
        400: {"model": MessageOut, "description": "hour 不是 UTC 整点"},
    },
)
def get_hour(
    point: str,
    hour: str,
    conn=Depends(get_conn),
) -> HourlyResultOut:
    try:
        hour_start = parse_hour_param(hour)
    except TimeFormatError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    result = services.get_hour_result(conn, point, hour_start)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"测点 {point} 在 {format_hour_start(hour_start)} 无覆盖，无数据",
        )
    return _hour_out(result)


@app.get(
    "/points/{point}/hours",
    response_model=HourlyRangeOut,
    tags=["小时结果"],
    responses={400: {"model": MessageOut, "description": "时间参数不合法"}},
)
def get_hour_range(
    point: str,
    start_hour: str = Query(..., alias="start", description="起始 UTC 整点（含）"),
    end_hour: str = Query(..., alias="end", description="结束 UTC 整点（不含）"),
    conn=Depends(get_conn),
) -> HourlyRangeOut:
    try:
        start = parse_hour_param(start_hour)
        end = parse_hour_param(end_hour)
    except TimeFormatError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    if end <= start:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="end 必须晚于 start，区间为左闭右开",
        )
    span = (end - start) // SECONDS_PER_HOUR
    if span > MAX_HOUR_RANGE:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"查询区间最多 {MAX_HOUR_RANGE} 个小时",
        )
    rows = services.list_hour_results(conn, point, start, end)
    return HourlyRangeOut(
        point=point,
        start_hour=format_hour_start(start),
        end_hour=format_hour_start(end),
        hours=[_hour_out(r) for r in rows],
    )
