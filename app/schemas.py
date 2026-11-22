"""Pydantic 请求 / 响应模型。"""
from __future__ import annotations

from enum import Enum
from typing import Annotated

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from .time_utils import parse_epoch_second


class Role(str, Enum):
    """仪器主备角色。"""

    PRIMARY = "primary"
    BACKUP = "backup"


class IntervalIn(BaseModel):
    """读数 / 校准单登记的共同字段。"""

    model_config = ConfigDict(extra="forbid")

    point: Annotated[str, Field(min_length=1, max_length=128, description="测点编号")]
    role: Role = Field(description="主表 primary / 备表 backup")
    start: Annotated[object, Field(description="区间起点，UTC 整秒（闭）")]
    end: Annotated[object, Field(description="区间终点，UTC 整秒（开）")]

    @field_validator("start", "end")
    @classmethod
    def _validate_time(cls, value: object) -> int:
        # 校验阶段即转成纪元秒，服务层只处理整数。
        return parse_epoch_second(value)


class ReadingIn(IntervalIn):
    """读数登记请求；区间内声级恒定。"""

    db: Annotated[float, Field(description="区间内恒定分贝值 dB(A)")]


class CalibrationIn(IntervalIn):
    """校准单登记 / 修改请求。"""

    offset: Annotated[float, Field(description="校准偏移 dB，叠加到读数分贝上")]


class SwitchSegmentIn(BaseModel):
    """换档新档中的一段：新编号、左闭右开区间、原始分贝。"""

    # 兼容不同报送平台可能附带的额外字段（如测点 / 角色回显），不参与换档判断。
    model_config = ConfigDict(extra="ignore")

    number: Annotated[str, Field(min_length=1, max_length=128, description="新平台编号")]
    point: Annotated[str | None, Field(default=None, description="可选的测点回显，必须一致")]
    role: Annotated[Role | None, Field(default=None, description="可选的角色回显，必须一致")]
    start: Annotated[object, Field(description="段起点，UTC 整秒（闭）")]
    end: Annotated[object, Field(description="段终点，UTC 整秒（开）")]
    db: Annotated[
        float,
        Field(
            description="段内恒定原始分贝值 dB(A)",
            validation_alias=AliasChoices(
                "db", "db_value", "raw_db", "raw_db_value", "level",
                "db_a", "decibel", "decibels",
            ),
        ),
    ]

    @field_validator("start", "end")
    @classmethod
    def _validate_time(cls, value: object) -> int:
        return parse_epoch_second(value)


class SwitchIn(BaseModel):
    """换档请求：一个测点 + 主/备角色，一个窗口与整批新档。

    窗口既可平铺为 ``start`` / ``end``，也可写成 ``window: {start, end}``；
    新档列表字段接受 ``segments`` 或 ``readings``。
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    point: Annotated[
        str | None,
        Field(default=None, min_length=1, max_length=128, description="测点编号"),
    ]
    role: Role = Field(description="主表 primary / 备表 backup")
    start: Annotated[object | None, Field(default=None, description="窗口起点，UTC 整秒（闭）")]
    end: Annotated[object | None, Field(default=None, description="窗口终点，UTC 整秒（开）")]
    window_start: Annotated[object | None, Field(default=None, alias="windowStart")]
    window_end: Annotated[object | None, Field(default=None, alias="windowEnd")]
    window: Annotated[dict | None, Field(default=None, description="窗口 {start, end}")]
    segments: Annotated[
        list[SwitchSegmentIn] | None,
        Field(
            default=None,
            description="整批新档，各段互不重叠",
            validation_alias=AliasChoices(
                "segments", "readings", "items", "segments_new", "new_readings"
            ),
        ),
    ]
    readings: Annotated[
        list[SwitchSegmentIn] | None,
        Field(default=None, description="segments 的别名"),
    ]

    @field_validator("start", "end", "window_start", "window_end")
    @classmethod
    def _validate_time(cls, value: object) -> int | None:
        return None if value is None else parse_epoch_second(value)

    @field_validator("window")
    @classmethod
    def _validate_window(cls, value: dict | None) -> dict | None:
        if value is None:
            return None
        if not isinstance(value, dict) or "start" not in value or "end" not in value:
            raise ValueError("window 必须是包含 start 与 end 的对象")
        return {
            "start": parse_epoch_second(value["start"]),
            "end": parse_epoch_second(value["end"]),
        }

    @model_validator(mode="after")
    def _normalize(self) -> "SwitchIn":
        candidates: list[tuple[object, object]] = []
        if self.start is not None and self.end is not None:
            candidates.append((self.start, self.end))
        if self.window_start is not None and self.window_end is not None:
            candidates.append((self.window_start, self.window_end))
        if self.window is not None:
            candidates.append((self.window["start"], self.window["end"]))
        if not candidates:
            raise ValueError("必须提供换档窗口：start/end 或 window")
        start, end = candidates[0]
        for other in candidates[1:]:
            if other != (start, end):
                raise ValueError("同时提供了多组不一致的窗口")
        self.start = start
        self.end = end

        segment_lists = [s for s in (self.segments, self.readings) if s is not None]
        if not segment_lists:
            raise ValueError("必须提供新档各段：segments")
        first = segment_lists[0]
        for other in segment_lists[1:]:
            if [s.model_dump() for s in other] != [s.model_dump() for s in first]:
                raise ValueError("segments 与 readings 的内容不一致")
        self.segments = first
        return self


class ReadingOut(BaseModel):
    number: str
    point: str
    role: Role
    start: str
    end: str
    db: float


class SwitchResultOut(BaseModel):
    point: str
    role: Role
    start: str
    end: str
    superseded: int
    inserted: int
    readings: list[ReadingOut]


class CalibrationOut(BaseModel):
    number: str
    point: str
    role: Role
    start: str
    end: str
    offset: float
    withdrawn: bool


class HourlyResultOut(BaseModel):
    point: str
    hour: str
    leq_db: float
    covered_seconds: int
    primary_seconds: int
    backup_seconds: int


class HourlyRangeOut(BaseModel):
    point: str
    start_hour: str
    end_hour: str
    hours: list[HourlyResultOut]


class MessageOut(BaseModel):
    detail: str
