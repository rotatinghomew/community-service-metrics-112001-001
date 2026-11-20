"""报告口径：分类目录版本、责任时限与指标定义。

一个 :class:`RuleSet` 就是“某次报告明确选择的口径”：

- ``report_month``：统计归属月，决定纳入哪些受理记录；
- ``as_of``：报告形成时能看见的节点快照；
- ``category_cut``：把事项映射到分类时采用的版本日期（默认为报告月）；
- ``definition_cut``：指标定义（计时规则）的版本日期；
- ``category_map``：可选的显式分类归并（旧名 -> 新名），用于重算可比范围。

原始月报使用“当时有效”的版本；重算可以显式传入不同的切点或归并，
但两者的结果分别保存，互不覆盖。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

from .loader import Category, Dataset, Definition
from .periods import SourceTimeError, is_month, month_key, parse_minute


class RuleError(ValueError):
    """口径选择不合法。"""


@dataclass(frozen=True)
class RuleSet:
    report_month: str
    as_of: str
    category_cut: str
    definition_cut: str
    metric: str = "timely_close"
    category_map: Mapping[str, str] = field(default_factory=dict)
    label: str = ""
    origin: str = "as_effective"  # as_effective | recomputed

    def __post_init__(self) -> None:
        for name in ("report_month", "category_cut", "definition_cut"):
            value = getattr(self, name)
            if not is_month(value):
                raise RuleError(f"{name}: 需要 YYYY-MM，收到 {value!r}")
        try:
            parse_minute(self.as_of)
        except SourceTimeError as exc:
            raise RuleError(f"as_of: {exc}") from exc
        for old, new in self.category_map.items():
            if not old or not new:
                raise RuleError("category_map: 键和值都不能为空")


def resolve(dataset: Dataset, month: str, *, as_of: str | None = None) -> RuleSet:
    """构造“报告月当时有效口径”的规则集。

    ``as_of`` 缺省为报告月最后一天 23:59，代表月报形成时点；可以用更晚的
    ``as_of`` 重建同一月份、观察后来补录/更正带来的变化。
    """
    if not is_month(month):
        raise RuleError(f"month: 需要 YYYY-MM，收到 {month!r}")
    year, mon = month_key(month)
    if as_of is None:
        as_of = _month_end(year, mon)
    return RuleSet(
        report_month=month,
        as_of=as_of,
        category_cut=month,
        definition_cut=month,
    )


def recompute(
    dataset: Dataset,
    month: str,
    *,
    as_of: str | None = None,
    category_cut: str | None = None,
    definition_cut: str | None = None,
    category_map: Mapping[str, str] | None = None,
    metric: str = "timely_close",
    label: str = "",
) -> RuleSet:
    """以明确选择的新口径重算可比范围。"""
    base = resolve(dataset, month, as_of=as_of)
    return RuleSet(
        report_month=month,
        as_of=base.as_of,
        category_cut=category_cut or base.category_cut,
        definition_cut=definition_cut or base.definition_cut,
        metric=metric,
        category_map=dict(category_map or {}),
        label=label,
        origin="recomputed",
    )


def _month_end(year: int, month: int) -> str:
    if month == 12:
        nxt = (year + 1, 1)
    else:
        nxt = (year, month + 1)
    from datetime import datetime, timedelta

    last = datetime(nxt[0], nxt[1], 1) - timedelta(days=1)
    return f"{last.year:04d}-{last.month:02d}-{last.day:02d}T23:59"


@dataclass(frozen=True)
class CategoryResolution:
    """一个事项在指定口径下映射到的分类。"""

    case_category: str          # 来源记录里的分类 id
    resolved_id: str            # 归并后的分析分类 id
    label: str                  # 归并后分类在当时目录中的名称
    valid_from: str
    valid_to: str
    renamed: bool               # 来源 id 与生效分类 id 不同
    mapped: bool                # 是否经过显式归并
    note: str = ""


def category_at(
    dataset: Dataset,
    category_id: str,
    *,
    cut: str,
    mapping: Mapping[str, str] | None = None,
) -> CategoryResolution:
    """在 ``cut`` 月把来源分类解析为当时有效的分类版本。

    资料的 ``cases.category`` 是受理当时记录的 id；分类改名后旧 id 仍指向
    同一业务类别。这里按来源 id 或其后继 id 在目录中查找在 ``cut`` 月有效的
    版本，找不到时保留来源 id 并标记 ``note``，不静默丢弃或算作新事项。
    """
    mapping = mapping or {}
    source = dataset.category(category_id)
    target_id = mapping.get(category_id, category_id)
    target = dataset.category(target_id)
    if target is None:
        return CategoryResolution(
            case_category=category_id,
            resolved_id=category_id,
            label=source.label if source else "",
            valid_from=source.valid_from if source else "",
            valid_to=source.valid_to if source else "",
            renamed=False,
            mapped=target_id != category_id,
            note="目录中未找到该分类在所选口径下的版本",
        )
    effective = _effective_category(dataset, target, cut)
    if effective is None:
        # 显式归并的目标在切点月尚未生效：仍按归并目标归组，但保留来源版本
        # 的标签并明确提示，避免把“归并”误读成“该分类当时已存在”。
        base = target if target_id != category_id else dataset.category(category_id)
        return CategoryResolution(
            case_category=category_id,
            resolved_id=target_id,
            label=target.label,
            valid_from=base.valid_from if base else target.valid_from,
            valid_to=base.valid_to if base else target.valid_to,
            renamed=True,
            mapped=target_id != category_id,
            note=f"分类 {target_id} 在 {cut} 无效，沿用来源版本",
        )
    return CategoryResolution(
        case_category=category_id,
        resolved_id=effective.id,
        label=effective.label,
        valid_from=effective.valid_from,
        valid_to=effective.valid_to,
        renamed=effective.id != category_id,
        mapped=target_id != category_id,
    )


def _effective_category(
    dataset: Dataset, category: Category, cut: str
) -> Category | None:
    """返回 ``cut`` 月有效的目录条目。

    先看该 id 自身是否覆盖 ``cut``；否则在目录中寻找同一 label、且在 ``cut``
    有效的后继版本（改名不改类别）。找不到时返回 ``None``，由调用方标记为
    “口径下无法解析”，不把旧记录硬套成新分类。
    """
    if category.period.contains(cut):
        return category
    for other in dataset.categories:
        if other.id == category.id:
            continue
        if other.label == category.label and other.period.contains(cut):
            return other
    return None


def definition_at(dataset: Dataset, metric: str, cut: str) -> Definition:
    """返回在 ``cut`` 月有效的指标定义；多个时取 valid_from 最晚者。"""
    candidates = [
        definition
        for definition in dataset.definitions
        if definition.metric == metric and definition.period.contains(cut)
    ]
    if not candidates:
        raise RuleError(f"指标 {metric!r} 在 {cut} 没有有效定义")
    return max(candidates, key=lambda d: (d.period.start, d.version))
