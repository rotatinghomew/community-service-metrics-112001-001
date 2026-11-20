"""读取并校验合成资料。

来源文件按 :mod:`DATA.md` 的字段定义读取；空字符串保留为“来源未给出”，
不会被转成 ``None`` 或默认值。校验失败抛出带文件/记录定位的
:class:`SourceError`，避免静默丢弃行。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
from pathlib import Path
import json
from typing import Any, Iterable

from .periods import Period, SourceTimeError, is_month, parse_minute, parse_period


class SourceError(ValueError):
    """来源资料结构或取值不合法。"""


_REQUIRED = {
    "categories": ("id", "label", "valid_from", "valid_to"),
    "cases": ("id", "grid", "category", "received", "due_minutes"),
    "events": ("id", "case", "kind", "at", "received_at", "supersedes"),
    "definitions": (
        "id",
        "metric",
        "version",
        "valid_from",
        "valid_to",
        "rule",
    ),
}

# 来源中允许以整数给出的字段；其余字段必须是字符串，空串表示未给出。
_INT_FIELDS = {"due_minutes"}

_EVENT_KINDS = {
    "accepted",   # 受理
    "assigned",   # 分派
    "reassigned", # 改派
    "closed",     # 办结
    "reopened",   # 撤回/重开
    "corrected",  # 办结后更正
    "withdrawn",  # 撤回
}

# 会把之前的办结状态重新打开或退出的节点。
REOPENING_KINDS = frozenset({"reopened", "withdrawn"})


@dataclass(frozen=True)
class Category:
    id: str
    label: str
    valid_from: str
    valid_to: str
    period: Period


@dataclass(frozen=True)
class Case:
    id: str
    grid: str
    category: str
    received: str
    due_minutes: int | None


@dataclass(frozen=True)
class Event:
    id: str
    case: str
    kind: str
    at: str
    received_at: str
    supersedes: str | None


@dataclass(frozen=True)
class Definition:
    id: str
    metric: str
    version: str
    valid_from: str
    valid_to: str
    rule: str
    period: Period


@dataclass
class Dataset:
    categories: list[Category] = field(default_factory=list)
    cases: list[Case] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)
    definitions: list[Definition] = field(default_factory=list)

    def category(self, category_id: str) -> Category | None:
        return next((c for c in self.categories if c.id == category_id), None)

    def case(self, case_id: str) -> Case | None:
        return next((c for c in self.cases if c.id == case_id), None)

    def events_for(self, case_id: str) -> list[Event]:
        return [e for e in self.events if e.case == case_id]

    def source_digest(self) -> str:
        """来源资料内容的稳定摘要，用于报告追溯时确认依据版本。"""
        payload = {
            "cases": [asdict(row) for row in self.cases],
            "events": [asdict(row) for row in self.events],
            "categories": [
                {k: v for k, v in asdict(row).items() if k != "period"}
                for row in self.categories
            ],
            "definitions": [
                {k: v for k, v in asdict(row).items() if k != "period"}
                for row in self.definitions
            ],
        }
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()[:16]


def load_dataset(directory: str | Path) -> Dataset:
    base = Path(directory)
    categories = [
        _category(row, where)
        for row, where in _rows(base / "categories.json", "categories")
    ]
    cases = [
        _case(row, where) for row, where in _rows(base / "cases.json", "cases")
    ]
    events = [
        _event(row, where) for row, where in _rows(base / "events.json", "events")
    ]
    definitions = [
        _definition(row, where)
        for row, where in _rows(base / "definitions.json", "definitions")
    ]
    dataset = Dataset(categories, cases, events, definitions)
    _validate_references(dataset)
    return dataset


def _rows(path: Path, table: str) -> Iterable[tuple[dict[str, Any], str]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SourceError(f"{path.name}: 缺少资料文件") from exc
    except json.JSONDecodeError as exc:
        raise SourceError(f"{path.name}: JSON 解析失败: {exc}") from exc
    if not isinstance(raw, list):
        raise SourceError(f"{path.name}: 需要记录数组")
    if not raw:
        # 节点/受理表可以暂时为空（资料逐步补齐）；其他表为空通常是配置错误。
        if table in ("events", "cases"):
            return
        raise SourceError(f"{path.name}: 需要非空记录数组")
    for index, row in enumerate(raw, start=1):
        where = f"{path.name}[{index}]"
        if not isinstance(row, dict):
            raise SourceError(f"{where}: 记录必须是对象")
        _check_fields(row, _REQUIRED[table], where)
        yield row, where


def _check_fields(row: dict[str, Any], required: tuple[str, ...], where: str) -> None:
    missing = [name for name in required if name not in row]
    if missing:
        raise SourceError(f"{where}: 缺少字段 {', '.join(missing)}")
    extra = sorted(set(row) - set(required))
    if extra:
        raise SourceError(f"{where}: 未知字段 {', '.join(extra)}")
    for name in required:
        value = row[name]
        if name in _INT_FIELDS:
            if not isinstance(value, str) and not isinstance(value, int):
                raise SourceError(f"{where}.{name}: 需要整数或空字符串")
            continue
        if not isinstance(value, str):
            raise SourceError(f"{where}.{name}: 必须是字符串（空字符串表示未给出）")


def _category(row: dict[str, Any], where: str) -> Category:
    if not row["id"]:
        raise SourceError(f"{where}.id: 不能为空")
    period = _period(row["valid_from"], row["valid_to"], where)
    return Category(row["id"], row["label"], row["valid_from"], row["valid_to"], period)


def _case(row: dict[str, Any], where: str) -> Case:
    if not row["id"]:
        raise SourceError(f"{where}.id: 不能为空")
    if not row["received"]:
        raise SourceError(f"{where}.received: 受理记录必须有受理时间")
    try:
        parse_minute(row["received"])
    except SourceTimeError as exc:
        raise SourceError(f"{where}.received: {exc}") from exc
    due = _optional_int(row["due_minutes"], f"{where}.due_minutes")
    return Case(row["id"], row["grid"], row["category"], row["received"], due)


def _event(row: dict[str, Any], where: str) -> Event:
    if not row["id"]:
        raise SourceError(f"{where}.id: 不能为空")
    if row["kind"] not in _EVENT_KINDS:
        raise SourceError(f"{where}.kind: 未知节点类型 {row['kind']!r}")
    if not row["case"]:
        raise SourceError(f"{where}.case: 节点必须指向事项")
    for name in ("at", "received_at"):
        try:
            parse_minute(row[name])
        except SourceTimeError as exc:
            raise SourceError(f"{where}.{name}: {exc}") from exc
    return Event(
        row["id"],
        row["case"],
        row["kind"],
        row["at"],
        row["received_at"],
        row["supersedes"] or None,
    )


def _definition(row: dict[str, Any], where: str) -> Definition:
    if not row["id"]:
        raise SourceError(f"{where}.id: 不能为空")
    period = _period(row["valid_from"], row["valid_to"], where)
    return Definition(
        row["id"],
        row["metric"],
        row["version"],
        row["valid_from"],
        row["valid_to"],
        row["rule"],
        period,
    )


def _period(valid_from: str, valid_to: str, where: str) -> Period:
    if not valid_from:
        raise SourceError(f"{where}.valid_from: 不能为空")
    try:
        start = parse_period(valid_from).start
        end = parse_period(valid_to).start if valid_to else None
    except SourceTimeError as exc:
        raise SourceError(f"{where}.valid_*: {exc}") from exc
    if end is not None and end < start:
        raise SourceError(f"{where}.valid_to: 早于 valid_from")
    return Period(start, end)


def _optional_int(value: str | int, where: str) -> int | None:
    if value == "":
        return None
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise SourceError(f"{where}: 需要整数或空字符串") from exc
    if number < 0:
        raise SourceError(f"{where}: 不能为负")
    return number


def _validate_references(dataset: Dataset) -> None:
    case_ids = [case.id for case in dataset.cases]
    if len(set(case_ids)) != len(case_ids):
        raise SourceError("cases.json: 事项 id 重复")
    event_ids = [event.id for event in dataset.events]
    if len(set(event_ids)) != len(event_ids):
        raise SourceError("events.json: 节点 id 重复")
    category_ids = {category.id for category in dataset.categories}
    if len(category_ids) != len(dataset.categories):
        raise SourceError("categories.json: 分类 id 重复")
    for case in dataset.cases:
        if case.category not in category_ids:
            raise SourceError(
                f"cases.json[{case.id}].category: 未找到分类 {case.category!r}"
            )
    known = set(case_ids)
    event_by_id = {event.id: event for event in dataset.events}
    for event in dataset.events:
        if event.case not in known:
            raise SourceError(f"events.json[{event.id}].case: 未找到事项 {event.case!r}")
        if event.supersedes is not None:
            if event.kind != "corrected":
                raise SourceError(
                    f"events.json[{event.id}].supersedes: 只有 corrected 节点可以更正既有节点"
                )
            target = event_by_id.get(event.supersedes)
            if target is None:
                raise SourceError(
                    f"events.json[{event.id}].supersedes: 未找到节点 {event.supersedes!r}"
                )
            if target.case != event.case:
                raise SourceError(
                    f"events.json[{event.id}].supersedes: 跨事项更正 {event.supersedes!r}"
                )
            if target.id == event.id:
                raise SourceError(f"events.json[{event.id}].supersedes: 不能指向自身")
            if target.kind not in ("closed", "corrected"):
                raise SourceError(
                    f"events.json[{event.id}].supersedes: 只能更正办结节点，"
                    f"{target.id} 是 {target.kind!r}"
                )
    # 更正链不能成环，且同一节点最多被一条更正经替代。
    corrected_targets: dict[str, str] = {}
    for event in dataset.events:
        seen = {event.id}
        cursor = event
        while cursor.supersedes is not None:
            if cursor.supersedes in seen:
                raise SourceError(f"events.json[{event.id}]: supersedes 链成环")
            seen.add(cursor.supersedes)
            cursor = event_by_id[cursor.supersedes]
        if event.supersedes is not None:
            previous = corrected_targets.get(event.supersedes)
            if previous is not None:
                raise SourceError(
                    f"events.json[{event.id}].supersedes: {event.supersedes} "
                    f"已被 {previous} 更正，不能重复更正"
                )
            corrected_targets[event.supersedes] = event.id


def definition_month(definition: Definition) -> str:
    return definition.period.label


def month_in(value: str, period: Period) -> bool:
    return is_month(value) and period.contains(value)
