"""时间工具：报告月以 ``YYYY-MM`` 表示，业务时间以分钟精度字符串表示。

所有函数都保持“来源字符串未给出”与“计算得出”之间的区别：解析失败抛出
:class:`SourceTimeError`，由上层决定是计入“缺少节点”还是中止。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import re

_MONTH = re.compile(r"^(\d{4})-(\d{2})$")
_MINUTE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})$")


class SourceTimeError(ValueError):
    """来源时间/月份字段无法解析。"""


@dataclass(frozen=True, order=True)
class Period:
    """闭区间的月份。``end=None`` 表示来源未写截止月（仍在适用）。"""

    start: tuple[int, int]
    end: tuple[int, int] | None = None

    def __str__(self) -> str:
        tail = "" if self.end is None else f"..{self.end[0]:04d}-{self.end[1]:02d}"
        return f"{self.start[0]:04d}-{self.start[1]:02d}{tail}"

    @property
    def label(self) -> str:
        return f"{self.start[0]:04d}-{self.start[1]:02d}"

    def contains(self, month: str) -> bool:
        key = month_key(month)
        if key < self.start:
            return False
        return self.end is None or key <= self.end

    def open_ended(self) -> bool:
        return self.end is None

    def overlaps(self, other: "Period") -> bool:
        """两个闭区间是否有交集；开放端点视为无穷。"""
        left = max(self.start, other.start)
        right_candidates = [p for p in (self.end, other.end) if p is not None]
        if not right_candidates:
            return True
        return left <= min(right_candidates)


def month_key(value: str) -> tuple[int, int]:
    match = _MONTH.match(value or "")
    if not match:
        raise SourceTimeError(f"无效月份: {value!r}")
    month = int(match.group(2))
    if not 1 <= month <= 12:
        raise SourceTimeError(f"月份超出范围: {value!r}")
    return int(match.group(1)), month


def parse_period(value: str) -> Period:
    """解析单个月份字符串为仅含该月的 Period。"""
    return Period(month_key(value), month_key(value))


def period_of(value: str) -> str:
    """从业务时间字符串取报告月；空/非法时抛错。"""
    return _parse_minute(value).strftime("%Y-%m")


def _parse_minute(value: str) -> datetime:
    match = _MINUTE.match(value or "")
    if not match:
        raise SourceTimeError(f"无效业务时间: {value!r}")
    try:
        return datetime(*(int(part) for part in match.groups()))
    except ValueError as exc:  # e.g. 2026-02-30T10:00
        raise SourceTimeError(f"无效业务时间: {value!r}") from exc


def parse_minute(value: str) -> datetime:
    """公开的分钟级时间解析。"""
    return _parse_minute(value)


def add_minutes(value: str, minutes: int) -> str:
    """在业务时间上增加分钟数，返回同格式字符串。"""
    return (_parse_minute(value) + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M")


def earlier(a: str, b: str) -> bool:
    return _parse_minute(a) < _parse_minute(b)


def later(a: str, b: str) -> bool:
    return _parse_minute(a) > _parse_minute(b)


def is_month(value: str) -> bool:
    try:
        month_key(value)
    except SourceTimeError:
        return False
    return True
