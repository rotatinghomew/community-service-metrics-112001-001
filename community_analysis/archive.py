"""报告归档：结论不可被后续更正悄悄覆盖。

归档以 ``(month, basis_id)`` 为键保存不可变快照。重新生成同一口径只允许
产生内容完全相同的报告；同一月份若要用新口径或新 ``as_of`` 重算，必须使用
不同的 basis，并且必须显式说明这是重算版本，原版本保留。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from .report import Report


class ArchiveError(ValueError):
    """归档操作违反不可覆盖原则。"""


@dataclass(frozen=True)
class ArchivedReport:
    month: str
    basis_id: str
    origin: str
    supersedes_basis: str | None
    reason: str
    archived_at: str
    payload: dict[str, Any]


@dataclass
class Archive:
    entries: dict[str, ArchivedReport] = field(default_factory=dict)

    def publish(
        self,
        report: Report,
        *,
        reason: str = "",
        supersedes: str | None = None,
    ) -> ArchivedReport:
        """归档一份报告；相同 basis 内容不同则拒绝。"""
        key = _key(report.month, report.basis_id)
        payload = report.to_dict()
        existing = self.entries.get(key)
        if existing is not None:
            # 来源摘要可以随补录变化；只要同一口径下的结论不变，就视为同一份
            # 已发布报告。结论有任何变化都必须改用新口径/as_of 重算。
            if _conclusions(existing.payload) != _conclusions(payload):
                raise ArchiveError(
                    f"{report.month} 的口径 {report.basis_id} 已发布且内容不同；"
                    "请使用新的口径/as_of 重算，不要覆盖原结论"
                )
            return existing
        if supersedes is not None:
            if _key(report.month, supersedes) not in self.entries:
                raise ArchiveError(f"要替代的口径 {supersedes!r} 不在归档中")
        entry = ArchivedReport(
            month=report.month,
            basis_id=report.basis_id,
            origin=report.rules.origin,
            supersedes_basis=supersedes,
            reason=reason,
            archived_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            payload=payload,
        )
        self.entries[key] = entry
        return entry

    def get(self, month: str, basis_id: str) -> ArchivedReport | None:
        return self.entries.get(_key(month, basis_id))

    def history(self, month: str) -> list[ArchivedReport]:
        return sorted(
            (entry for entry in self.entries.values() if entry.month == month),
            key=lambda entry: entry.archived_at,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "entries": {
                key: {
                    "month": entry.month,
                    "basis_id": entry.basis_id,
                    "origin": entry.origin,
                    "supersedes_basis": entry.supersedes_basis,
                    "reason": entry.reason,
                    "archived_at": entry.archived_at,
                    "payload": entry.payload,
                }
                for key, entry in sorted(self.entries.items())
            }
        }

    def save(self, path: str | Path) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    @classmethod
    def load(cls, path: str | Path) -> "Archive":
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        entries: dict[str, ArchivedReport] = {}
        for key, item in raw.get("entries", {}).items():
            entries[key] = ArchivedReport(
                month=item["month"],
                basis_id=item["basis_id"],
                origin=item["origin"],
                supersedes_basis=item.get("supersedes_basis"),
                reason=item.get("reason", ""),
                archived_at=item["archived_at"],
                payload=item["payload"],
            )
        return cls(entries=entries)


def _key(month: str, basis_id: str) -> str:
    return f"{month}::{basis_id}"


def _conclusions(payload: dict[str, Any]) -> dict[str, Any]:
    """归档冲突判断只看结论，不看来源摘要等追溯元数据。"""
    return {
        "totals": payload["totals"],
        "by_category": payload["by_category"],
        "by_grid": payload["by_grid"],
        "cases": payload["cases"],
        "excluded_cases": payload["excluded_cases"],
    }
