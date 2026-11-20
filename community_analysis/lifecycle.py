"""办理节点生命周期：按“形成报告时已知的节点”还原状态。

规则把两件事分开：

- **业务时间** ``at``：节点实际发生的时刻，用来比较时限；
- **记录时间** ``received_at``：分析系统得知该节点的时刻，用来决定某次报告
  能看见哪些记录。

因此晚到的更正不会改写已经发出的月报；只有用更晚的 ``as_of`` 重新生成报告，
结论才会变化。``supersedes`` 表示更正关系，被更正的旧节点在更正可见后不再
生效，但来源记录仍然保留，可在追溯中列出。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .loader import Dataset, Event
from .periods import parse_minute

CLOSE_KINDS = frozenset({"closed"})
REOPEN_KINDS = frozenset({"reopened"})


@dataclass(frozen=True)
class VisibleEvent:
    id: str
    kind: str
    at: str
    received_at: str
    source_kind: str
    supersedes: str | None
    inherited_kind: bool

    @property
    def is_close(self) -> bool:
        return self.kind == "closed"


@dataclass(frozen=True)
class ExcludedEvent:
    """未纳入本次计算的来源节点及原因。"""

    id: str
    kind: str
    at: str
    received_at: str
    reason: str  # "received_after_as_of" | "at_after_as_of" | "superseded"
    superseded_by: str | None = None


@dataclass(frozen=True)
class Lifecycle:
    status: str  # closed | open | withdrawn
    close_at: str | None
    close_event_id: str | None
    close_was_correction: bool
    visible: tuple[VisibleEvent, ...]
    excluded: tuple[ExcludedEvent, ...]
    reopened_after_close: bool
    withdrawn_after_close: bool
    first_close_at: str | None
    first_close_event_id: str | None
    note: str = ""

    @property
    def has_close(self) -> bool:
        return self.close_at is not None


def _kind_of(
    event: Event,
    by_id: dict[str, Event],
    *,
    _seen: frozenset[str] = frozenset(),
) -> str:
    """解析有效类型；``corrected`` 继承被更正节点的类型。

    若被更正节点在快照中尚不可见（或缺失），将 ``corrected`` 视为一次
    办结更正，这是资料中该类型的唯一明确含义。
    """
    if event.kind != "corrected":
        return event.kind
    if event.id in _seen:
        return "closed"
    target = by_id.get(event.supersedes or "")
    if target is None:
        return "closed"
    return _kind_of(target, by_id, _seen=_seen | {event.id})


def resolve_lifecycle(dataset: Dataset, case_id: str, as_of: str) -> Lifecycle:
    """在 ``as_of`` 快照上解析单个事项的办理状态。"""
    events = sorted(
        dataset.events_for(case_id),
        key=lambda e: (parse_minute(e.at), parse_minute(e.received_at), e.id),
    )
    by_id = {event.id: event for event in events}
    visible_raw = [
        e
        for e in events
        if _not_after(e.received_at, as_of) and _not_after(e.at, as_of)
    ]
    superseded_by = {
        e.supersedes: e.id for e in visible_raw if e.supersedes is not None
    }
    visible_ids = {e.id for e in visible_raw}
    effective = [e for e in visible_raw if e.id not in superseded_by]

    visible = tuple(
        VisibleEvent(
            id=e.id,
            kind=_kind_of(e, by_id),
            at=e.at,
            received_at=e.received_at,
            source_kind=e.kind,
            supersedes=e.supersedes,
            inherited_kind=e.kind == "corrected",
        )
        for e in effective
    )

    excluded: list[ExcludedEvent] = []
    for e in events:
        if e.id not in visible_ids:
            reason = (
                "at_after_as_of"
                if _not_after(e.received_at, as_of)
                else "received_after_as_of"
            )
            excluded.append(ExcludedEvent(e.id, e.kind, e.at, e.received_at, reason))
        elif e.id in superseded_by:
            excluded.append(
                ExcludedEvent(
                    e.id,
                    e.kind,
                    e.at,
                    e.received_at,
                    "superseded",
                    superseded_by[e.id],
                )
            )

    # 同一业务时刻的节点按 received_at、id 稳定排序。
    ordered = sorted(
        visible, key=lambda e: (parse_minute(e.at), parse_minute(e.received_at), e.id)
    )

    # 用状态机还原“当前仍然有效”的办理状态。重开、撤回会覆盖此前的办结，
    # 但不能抹去已经发生过的首次办结事实；办结后的撤回保留办结结论并显式标记。
    state = "open"
    last_close: VisibleEvent | None = None
    first_close = next((e for e in ordered if e.is_close), None)
    last_reopen_after_close: VisibleEvent | None = None
    saw_close = False
    for event in ordered:
        if event.is_close:
            state = "closed"
            last_close = event
            saw_close = True
        elif event.kind in REOPEN_KINDS:
            state = "open"
            last_close = None
            if saw_close:
                last_reopen_after_close = event
        elif event.kind == "withdrawn":
            # 办结前撤回退出总体；办结后撤回不推翻已完成的办理结论。
            if state == "closed":
                state = "withdrawn_after_close"
            elif state != "withdrawn_after_close":
                state = "withdrawn"
                last_close = None

    reopened_after_close = last_reopen_after_close is not None
    withdrawn_after_close = state == "withdrawn_after_close"
    first_close_at = first_close.at if first_close else None
    first_close_id = first_close.id if first_close else None

    if state in ("closed", "withdrawn_after_close") and last_close is not None:
        return Lifecycle(
            status="closed",
            close_at=last_close.at,
            close_event_id=last_close.id,
            close_was_correction=last_close.inherited_kind,
            visible=visible,
            excluded=tuple(excluded),
            reopened_after_close=reopened_after_close,
            withdrawn_after_close=withdrawn_after_close,
            first_close_at=first_close_at,
            first_close_event_id=first_close_id,
            note="办结后撤回，保留已办结事实并标记撤回" if withdrawn_after_close else "",
        )

    common = dict(
        close_at=None,
        close_event_id=None,
        close_was_correction=False,
        visible=visible,
        excluded=tuple(excluded),
        reopened_after_close=reopened_after_close,
        first_close_at=first_close_at,
        first_close_event_id=first_close_id,
    )
    if state == "withdrawn":
        return Lifecycle(
            status="withdrawn",
            withdrawn_after_close=False,
            note="办结前撤回，退出服务总体",
            **common,
        )
    note = "收到重开节点，尚未再次办结" if saw_close else "尚未收到办结节点"
    return Lifecycle(
        status="open",
        withdrawn_after_close=False,
        note=note,
        **common,
    )


def _not_after(timestamp: str, as_of: str) -> bool:
    return parse_minute(timestamp) <= parse_minute(as_of)


def select_close(lifecycle: Lifecycle, rule: str) -> tuple[str | None, str | None, str]:
    """按指标规则选择用于计时的办结节点。

    返回 ``(close_at, event_id, basis_note)``。
    """
    if lifecycle.status == "withdrawn" and not lifecycle.withdrawn_after_close:
        return None, None, "办结前撤回，不参与时限判断"
    if lifecycle.status == "open":
        return None, None, lifecycle.note
    if rule == "first_close":
        return (
            lifecycle.first_close_at,
            lifecycle.first_close_event_id,
            "首次办结节点（重开不改变首次办结事实）",
        )
    if rule == "latest_valid_close":
        if not lifecycle.has_close:
            return None, None, lifecycle.note
        return lifecycle.close_at, lifecycle.close_event_id, "最近一次仍然有效的办结节点"
    raise ValueError(f"未知计时规则: {rule!r}")


def event_ledger(
    dataset: Dataset, case_id: str, as_of: str
) -> list[dict[str, str]]:
    """列出某事项全部来源节点及其在快照中的可见性。"""
    lifecycle = resolve_lifecycle(dataset, case_id, as_of)
    ledger: list[dict[str, str]] = []
    visible = {e.id: e for e in lifecycle.visible}
    excluded = {e.id: e for e in lifecycle.excluded}
    for event in sorted(
        dataset.events_for(case_id),
        key=lambda e: (parse_minute(e.at), parse_minute(e.received_at), e.id),
    ):
        row = {
            "event_id": event.id,
            "source_kind": event.kind,
            "at": event.at,
            "received_at": event.received_at,
            "supersedes": event.supersedes or "",
            "status": "",
            "effective_kind": "",
            "reason": "",
        }
        if event.id in visible:
            row["status"] = "used"
            row["effective_kind"] = visible[event.id].kind
        else:
            dropped = excluded[event.id]
            row["status"] = "excluded"
            row["reason"] = dropped.reason
            if dropped.superseded_by:
                row["reason"] = f"superseded_by:{dropped.superseded_by}"
        ledger.append(row)
    return ledger
