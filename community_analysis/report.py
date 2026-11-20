"""月度报告：逐事项判定、分类/网格聚合与可追溯分母。

一次报告固定一套 :class:`~community_analysis.rules.RuleSet`。判定结果不写回
来源资料，而是保存为带口径标记的 :class:`Report`，因此后续补录、改派、撤回或
更正只能产生新报告，不能改写旧报告。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from .lifecycle import Lifecycle, resolve_lifecycle, select_close
from .loader import Case, Dataset
from .periods import add_minutes, is_month, parse_minute, period_of
from .rules import (
    CategoryResolution,
    RuleError,
    RuleSet,
    category_at,
    definition_at,
)

# 判定分类
TIMELY = "timely"
LATE = "late"
OPEN_WITHIN_DUE = "open_within_due"      # 未结，尚在时限内（缺少办结节点）
OPEN_OVERDUE = "open_overdue"            # 未结，已过时限（确实逾期）
MISSING_DUE = "missing_due"              # 缺少责任时限事实，无法判断
WITHDRAWN = "withdrawn"                  # 办结前撤回，退出总体
NOT_YET_RECEIVED = "not_yet_received"    # 报告时点尚未收到该受理记录

CLOSED_OUTCOMES = frozenset({TIMELY, LATE})
OPEN_OUTCOMES = frozenset({OPEN_WITHIN_DUE, OPEN_OVERDUE})


@dataclass(frozen=True)
class CaseObservation:
    """单个事项在一次报告口径下的完整判定。"""

    case_id: str
    grid: str
    received: str
    category: CategoryResolution
    outcome: str
    due_minutes: int | None
    due_at: str | None
    close_at: str | None
    close_event_id: str | None
    close_basis: str
    elapsed_minutes: int | None
    lifecycle: Lifecycle
    drivers: tuple[str, ...] = field(default_factory=tuple)

    @property
    def judged(self) -> bool:
        return self.outcome not in (MISSING_DUE, NOT_YET_RECEIVED, WITHDRAWN)

    @property
    def is_closed(self) -> bool:
        return self.outcome in CLOSED_OUTCOMES

    @property
    def is_open(self) -> bool:
        return self.outcome in OPEN_OUTCOMES

    @property
    def is_timely(self) -> bool:
        return self.outcome == TIMELY

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "grid": self.grid,
            "received": self.received,
            "source_category": self.category.case_category,
            "category_id": self.category.resolved_id,
            "category_label": self.category.label,
            "category_valid_from": self.category.valid_from,
            "category_valid_to": self.category.valid_to,
            "category_renamed": self.category.renamed,
            "category_mapped": self.category.mapped,
            "category_note": self.category.note,
            "outcome": self.outcome,
            "due_minutes": self.due_minutes,
            "due_at": self.due_at,
            "close_at": self.close_at,
            "close_event_id": self.close_event_id,
            "close_basis": self.close_basis,
            "elapsed_minutes": self.elapsed_minutes,
            "drivers": list(self.drivers),
        }


@dataclass(frozen=True)
class GroupStats:
    key: str
    label: str
    received: int = 0
    closed: int = 0  # 办理状态为已办结（含缺少时限事实者），不等于按时率分母
    timely: int = 0
    late: int = 0
    open_total: int = 0
    open_within_due: int = 0
    open_overdue: int = 0
    missing_due: int = 0
    withdrawn: int = 0
    withdrawn_after_close: int = 0  # closed 的子集，用于单独提示

    @property
    def timely_denominator(self) -> int:
        """按时率分母：已办结且具备时限事实的事项。"""
        return self.timely + self.late

    @property
    def timely_rate(self) -> float | None:
        denominator = self.timely_denominator
        return None if denominator == 0 else self.timely / denominator

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "received": self.received,
            "closed": self.closed,
            "timely": self.timely,
            "late": self.late,
            "timely_denominator": self.timely_denominator,
            "timely_rate": self.timely_rate,
            "open_total": self.open_total,
            "open_within_due": self.open_within_due,
            "open_overdue": self.open_overdue,
            "missing_due": self.missing_due,
            "withdrawn": self.withdrawn,
            "withdrawn_after_close": self.withdrawn_after_close,
        }


@dataclass
class Report:
    month: str
    rules: RuleSet
    definition_id: str
    definition_rule: str
    definition_version: str
    observations: list[CaseObservation]
    excluded_cases: list[dict[str, str]]
    by_category: list[GroupStats]
    by_grid: list[GroupStats]
    totals: GroupStats
    source_digest: str = ""

    @property
    def basis_id(self) -> str:
        """稳定的口径标识，便于把报告与来源版本关联。"""
        parts = [
            self.month,
            f"as_of={self.rules.as_of}",
            f"cat={self.rules.category_cut}",
            f"def={self.rules.definition_cut}",
            f"metric={self.rules.metric}",
            f"rule={self.definition_rule}@{self.definition_version}",
        ]
        if self.rules.category_map:
            mapping = ",".join(
                f"{old}->{new}" for old, new in sorted(self.rules.category_map.items())
            )
            parts.append(f"map={mapping}")
        return "|".join(parts)

    def observation(self, case_id: str) -> CaseObservation | None:
        return next(
            (item for item in self.observations if item.case_id == case_id), None
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "month": self.month,
            "basis_id": self.basis_id,
            "source_digest": self.source_digest,
            "origin": self.rules.origin,
            "label": self.rules.label,
            "as_of": self.rules.as_of,
            "category_cut": self.rules.category_cut,
            "definition_cut": self.rules.definition_cut,
            "metric": self.rules.metric,
            "definition": {
                "id": self.definition_id,
                "version": self.definition_version,
                "rule": self.definition_rule,
            },
            "category_map": dict(sorted(self.rules.category_map.items())),
            "totals": self.totals.to_dict(),
            "by_category": [group.to_dict() for group in self.by_category],
            "by_grid": [group.to_dict() for group in self.by_grid],
            "cases": [item.to_dict() for item in self.observations],
            "excluded_cases": list(self.excluded_cases),
        }


def build_report(dataset: Dataset, rules: RuleSet) -> Report:
    """按给定口径生成报告；不修改来源资料。"""
    if not is_month(rules.report_month):
        raise RuleError(f"report_month: 需要 YYYY-MM，收到 {rules.report_month!r}")
    definition = definition_at(dataset, rules.metric, rules.definition_cut)

    observations: list[CaseObservation] = []
    excluded: list[dict[str, str]] = []
    for case in sorted(dataset.cases, key=lambda c: (c.received, c.id)):
        received_month = period_of(case.received)
        if received_month != rules.report_month:
            continue
        if parse_minute(case.received) > parse_minute(rules.as_of):
            excluded.append(
                {
                    "case_id": case.id,
                    "reason": "received_after_as_of",
                    "received": case.received,
                }
            )
            continue
        observations.append(
            _observe(dataset, case, rules, definition.rule, definition.id)
        )

    by_category = _group(
        observations,
        key=lambda item: item.category.resolved_id,
        label=lambda item: item.category.label,
        order=_category_order(dataset),
    )
    by_grid = _group(
        observations,
        key=lambda item: item.grid,
        label=lambda item: "",
        order=lambda stats: stats.key,
    )
    totals = _stats(
        "__all__",
        "全部",
        observations,
    )
    return Report(
        month=rules.report_month,
        rules=rules,
        definition_id=definition.id,
        definition_rule=definition.rule,
        definition_version=definition.version,
        observations=observations,
        excluded_cases=excluded,
        by_category=by_category,
        by_grid=by_grid,
        totals=totals,
        source_digest=dataset.source_digest(),
    )


def _observe(
    dataset: Dataset,
    case: Case,
    rules: RuleSet,
    rule: str,
    definition_id: str,
) -> CaseObservation:
    category = category_at(
        dataset, case.category, cut=rules.category_cut, mapping=rules.category_map
    )
    lifecycle = resolve_lifecycle(dataset, case.id, rules.as_of)
    close_at, close_event_id, close_basis = select_close(lifecycle, rule)
    due_at = (
        add_minutes(case.received, case.due_minutes)
        if case.due_minutes is not None
        else None
    )

    drivers: list[str] = []
    if category.renamed:
        drivers.append("category_version")
    if category.mapped:
        drivers.append("category_merge")
    if category.note:
        drivers.append("category_unresolved")
    if close_at and close_event_id:
        close_event = next(
            (e for e in lifecycle.visible if e.id == close_event_id), None
        )
        if close_event is not None and close_event.inherited_kind:
            drivers.append("close_correction")
    if lifecycle.reopened_after_close:
        drivers.append("reopened")
    if lifecycle.withdrawn_after_close:
        drivers.append("withdrawn_after_close")
    if any(e.reason == "superseded" for e in lifecycle.excluded):
        drivers.append("superseded_event")
    if any(
        e.reason in ("received_after_as_of", "at_after_as_of")
        for e in lifecycle.excluded
    ):
        drivers.append("late_arriving_event")
    if case.due_minutes is None:
        drivers.append("missing_due")
    drivers.append(f"definition:{definition_id}:{rule}")

    elapsed: int | None = None
    if lifecycle.status == "withdrawn":
        # 撤回事项不进入按时率分母；若办结后撤回，办结事实仍保留在追溯中。
        outcome = WITHDRAWN
        close_basis = lifecycle.note
    elif case.due_minutes is None:
        outcome = MISSING_DUE
        close_basis = close_basis + "；缺少责任时限事实"
    elif close_at is not None:
        elapsed = _minutes_between(case.received, close_at)
        outcome = TIMELY if elapsed <= case.due_minutes else LATE
    else:
        # 未结：用报告时点与时限比较，区分“缺少节点”与“确实逾期”。
        elapsed = _minutes_between(case.received, rules.as_of)
        outcome = (
            OPEN_OVERDUE if elapsed > case.due_minutes else OPEN_WITHIN_DUE
        )

    return CaseObservation(
        case_id=case.id,
        grid=case.grid,
        received=case.received,
        category=category,
        outcome=outcome,
        due_minutes=case.due_minutes,
        due_at=due_at,
        close_at=close_at,
        close_event_id=close_event_id,
        close_basis=close_basis,
        elapsed_minutes=elapsed,
        lifecycle=lifecycle,
        drivers=tuple(dict.fromkeys(drivers)),
    )


def _minutes_between(start: str, end: str) -> int:
    delta = parse_minute(end) - parse_minute(start)
    return int(delta.total_seconds() // 60)


def _stats(key: str, label: str, items: Iterable[CaseObservation]) -> GroupStats:
    received = closed = timely = late = 0
    open_total = open_within = open_overdue = missing_due = withdrawn = 0
    withdrawn_after_close = 0
    for item in items:
        received += 1
        # “已办结”按办理状态统计，不因缺少时限事实而被抹掉。
        if item.lifecycle.status == "closed":
            closed += 1
        if item.lifecycle.withdrawn_after_close:
            withdrawn_after_close += 1
        if item.outcome == TIMELY:
            timely += 1
        elif item.outcome == LATE:
            late += 1
        elif item.outcome == OPEN_WITHIN_DUE:
            open_total += 1
            open_within += 1
        elif item.outcome == OPEN_OVERDUE:
            open_total += 1
            open_overdue += 1
        elif item.outcome == MISSING_DUE:
            missing_due += 1
            if item.lifecycle.status == "open":
                open_total += 1
        elif item.outcome == WITHDRAWN:
            withdrawn += 1
    return GroupStats(
        key=key,
        label=label,
        received=received,
        closed=closed,
        timely=timely,
        late=late,
        open_total=open_total,
        open_within_due=open_within,
        open_overdue=open_overdue,
        missing_due=missing_due,
        withdrawn=withdrawn,
        withdrawn_after_close=withdrawn_after_close,
    )


def _group(
    items: list[CaseObservation],
    *,
    key,
    label,
    order,
) -> list[GroupStats]:
    buckets: dict[str, list[CaseObservation]] = {}
    labels: dict[str, str] = {}
    for item in items:
        bucket_key = key(item)
        buckets.setdefault(bucket_key, []).append(item)
        labels.setdefault(bucket_key, label(item))
    stats = [_stats(k, labels[k], rows) for k, rows in buckets.items()]
    return sorted(stats, key=order)


def _category_order(dataset: Dataset):
    def sorter(stats: GroupStats) -> tuple[Any, ...]:
        category = dataset.category(stats.key)
        if category is None:
            return (1, stats.key)
        return (0, category.period.start, stats.key)

    return sorter
