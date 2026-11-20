"""口径比较：解释同一批事项在不同规则下的差异来源。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .report import CaseObservation, Report


@dataclass(frozen=True)
class FieldChange:
    case_id: str
    field: str
    before: Any
    after: Any


@dataclass(frozen=True)
class CaseChange:
    case_id: str
    outcome_before: str
    outcome_after: str
    category_before: str
    category_after: str
    drivers: tuple[str, ...]
    fields: tuple[FieldChange, ...]

    @property
    def outcome_changed(self) -> bool:
        return self.outcome_before != self.outcome_after

    @property
    def category_changed(self) -> bool:
        return self.category_before != self.category_after

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "outcome_before": self.outcome_before,
            "outcome_after": self.outcome_after,
            "category_before": self.category_before,
            "category_after": self.category_after,
            "drivers": list(self.drivers),
            "fields": [
                {
                    "field": change.field,
                    "before": change.before,
                    "after": change.after,
                }
                for change in self.fields
            ],
        }


@dataclass
class Comparison:
    before: Report
    after: Report
    changes: list[CaseChange]
    only_before: list[str]
    only_after: list[str]
    totals_before: dict[str, Any]
    totals_after: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "before_basis": self.before.basis_id,
            "after_basis": self.after.basis_id,
            "totals_before": self.totals_before,
            "totals_after": self.totals_after,
            "changes": [change.to_dict() for change in self.changes],
            "only_before": self.only_before,
            "only_after": self.only_after,
        }


_TRACKED = (
    ("category", "category"),
    ("due_minutes", "due_minutes"),
    ("due_at", "due_at"),
    ("close_at", "close_at"),
    ("close_event_id", "close_event_id"),
    ("elapsed_minutes", "elapsed_minutes"),
    ("close_basis", "close_basis"),
)


def _value(item: CaseObservation, attr: str):
    if attr == "category":
        resolved = item.category
        return {
            "id": resolved.resolved_id,
            "label": resolved.label,
            "valid_from": resolved.valid_from,
            "valid_to": resolved.valid_to,
            "note": resolved.note,
        }
    return getattr(item, attr)


def compare_reports(before: Report, after: Report) -> Comparison:
    """逐事项比较两份报告，列出判定、分类和计时依据的变化。"""
    before_map = {item.case_id: item for item in before.observations}
    after_map = {item.case_id: item for item in after.observations}
    changes: list[CaseChange] = []
    for case_id in sorted(set(before_map) & set(after_map)):
        old = before_map[case_id]
        new = after_map[case_id]
        fields = tuple(
            FieldChange(case_id, name, _value(old, attr), _value(new, attr))
            for name, attr in _TRACKED
            if _value(old, attr) != _value(new, attr)
        )
        if (
            old.outcome == new.outcome
            and old.category.resolved_id == new.category.resolved_id
            and not fields
        ):
            continue
        changes.append(
            CaseChange(
                case_id=case_id,
                outcome_before=old.outcome,
                outcome_after=new.outcome,
                category_before=old.category.resolved_id,
                category_after=new.category.resolved_id,
                drivers=_explain(
                    old,
                    new,
                    fields,
                    as_of_changed=before.rules.as_of != after.rules.as_of,
                    definition_cut_changed=(
                        before.rules.definition_cut != after.rules.definition_cut
                    ),
                    metric_changed=before.rules.metric != after.rules.metric,
                ),
                fields=fields,
            )
        )
    return Comparison(
        before=before,
        after=after,
        changes=changes,
        only_before=sorted(set(before_map) - set(after_map)),
        only_after=sorted(set(after_map) - set(before_map)),
        totals_before=before.totals.to_dict(),
        totals_after=after.totals.to_dict(),
    )


def _explain(
    old: CaseObservation,
    new: CaseObservation,
    fields: tuple[FieldChange, ...],
    *,
    as_of_changed: bool,
    definition_cut_changed: bool,
    metric_changed: bool,
) -> tuple[str, ...]:
    reasons: list[str] = []
    if old.category.case_category != new.category.case_category:
        reasons.append("case_category_changed")
    if old.category.resolved_id != new.category.resolved_id:
        if new.category.mapped or old.category.mapped:
            reasons.append("category_merge")
        else:
            reasons.append("category_version")
    elif any(change.field == "category" for change in fields):
        # 同一分类 id 在两种口径下取到不同版本（标签或有效区间变化）。
        reasons.append("category_version")
    if old.close_event_id != new.close_event_id:
        if new.close_at and _is_correction(new, new.close_event_id):
            reasons.append("close_correction")
        elif new.lifecycle.reopened_after_close or old.lifecycle.reopened_after_close:
            reasons.append("reopened")
        elif new.lifecycle.withdrawn_after_close or old.lifecycle.withdrawn_after_close:
            reasons.append("withdrawn_after_close")
        elif as_of_changed:
            reasons.append("late_arriving_event")
        else:
            reasons.append("close_event_changed")
    if old.close_basis != new.close_basis and (
        definition_cut_changed or metric_changed
    ):
        reasons.append("timing_rule")
    if old.outcome != new.outcome:
        if as_of_changed and old.lifecycle.status != new.lifecycle.status:
            reasons.append("visibility_changed")
        reasons.append("status_changed")
    if old.due_minutes != new.due_minutes:
        reasons.append("due_rule_changed")
    # 报告时点不同导致“未结已逾期”的累计时长变化，即使结论未翻转也需说明。
    if (
        as_of_changed
        and old.outcome == new.outcome
        and old.outcome in ("open_within_due", "open_overdue")
    ):
        reasons.append("observation_window_changed")
    return tuple(dict.fromkeys(reasons))


def _is_correction(observation: CaseObservation, event_id: str) -> bool:
    for event in observation.lifecycle.visible:
        if event.id == event_id:
            return event.inherited_kind or event.source_kind == "corrected"
    return False
