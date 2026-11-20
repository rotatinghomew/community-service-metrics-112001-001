"""追溯：把单个事项的结论还原到来源记录、分类版本和计时依据。"""

from __future__ import annotations

from typing import Any

from .lifecycle import event_ledger
from .loader import Dataset
from .report import Report


def explain_case(dataset: Dataset, report: Report, case_id: str) -> dict[str, Any]:
    """返回某事项在报告口径下的完整证据链。"""
    observation = report.observation(case_id)
    if observation is None:
        excluded = next(
            (item for item in report.excluded_cases if item["case_id"] == case_id),
            None,
        )
        if excluded is None:
            raise KeyError(f"报告中不包含事项 {case_id!r}")
        return {
            "case_id": case_id,
            "included": False,
            "reason": excluded["reason"],
            "received": excluded["received"],
            "basis": report.basis_id,
            "source_digest": report.source_digest,
        }

    category = observation.category
    source = dataset.category(category.case_category)
    return {
        "case_id": case_id,
        "included": True,
        "basis": report.basis_id,
        "source_digest": report.source_digest,
        "report": {
            "month": report.month,
            "as_of": report.rules.as_of,
            "category_cut": report.rules.category_cut,
            "definition_cut": report.rules.definition_cut,
            "definition": {
                "id": report.definition_id,
                "version": report.definition_version,
                "rule": report.definition_rule,
            },
        },
        "case_source": {
            "grid": observation.grid,
            "received": observation.received,
            "category": category.case_category,
            "due_minutes": observation.due_minutes,
        },
        "category_version": {
            "source_id": category.case_category,
            "source_label": source.label if source else "",
            "resolved_id": category.resolved_id,
            "resolved_label": category.label,
            "valid_from": category.valid_from,
            "valid_to": category.valid_to,
            "renamed": category.renamed,
            "mapped": category.mapped,
            "note": category.note,
        },
        "timing": {
            "due_at": observation.due_at,
            "close_at": observation.close_at,
            "close_event_id": observation.close_event_id,
            "elapsed_minutes": observation.elapsed_minutes,
            "basis": observation.close_basis,
        },
        "outcome": observation.outcome,
        "drivers": list(observation.drivers),
        "events": event_ledger(dataset, case_id, report.rules.as_of),
    }


def explain_driver(dataset: Dataset, report: Report, driver: str) -> list[str]:
    """列出在某报告中触发指定驱动因素的事项。"""
    return sorted(
        item.case_id
        for item in report.observations
        if driver in item.drivers
    )
