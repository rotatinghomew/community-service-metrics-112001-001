"""分析核心的单元测试。

测试用临时目录构造最小资料集，不依赖 ``fixtures/`` 的具体条数，便于覆盖
改派、撤回、更正、晚到记录和口径切换等边界。
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
import unittest

from community_analysis.archive import Archive, ArchiveError
from community_analysis.compare import compare_reports
from community_analysis.loader import SourceError, load_dataset
from community_analysis.report import build_report
from community_analysis.rules import RuleError, recompute, resolve
from community_analysis.trace import explain_case


def write_dataset(
    root: Path,
    *,
    cases,
    events,
    categories=None,
    definitions=None,
) -> Path:
    categories = categories or [
        {
            "id": "C-1",
            "label": "设施维护",
            "valid_from": "2025-01",
            "valid_to": "2026-03",
        },
        {
            "id": "C-2",
            "label": "公共空间设施",
            "valid_from": "2026-04",
            "valid_to": "",
        },
    ]
    definitions = definitions or [
        {
            "id": "M-1",
            "metric": "timely_close",
            "version": "1",
            "valid_from": "2025-01",
            "valid_to": "2026-03",
            "rule": "first_close",
        },
        {
            "id": "M-2",
            "metric": "timely_close",
            "version": "2",
            "valid_from": "2026-04",
            "valid_to": "",
            "rule": "latest_valid_close",
        },
    ]
    directory = root / "data"
    directory.mkdir()
    for name, rows in (
        ("cases", cases),
        ("events", events),
        ("categories", categories),
        ("definitions", definitions),
    ):
        (directory / f"{name}.json").write_text(
            json.dumps(rows, ensure_ascii=False), encoding="utf-8"
        )
    return directory


def case(case_id="R-1", **overrides):
    row = {
        "id": case_id,
        "grid": "G-A",
        "category": "C-1",
        "received": "2026-03-01T09:00",
        "due_minutes": 1440,
    }
    row.update(overrides)
    return row


def event(event_id, kind, at, *, case_id="R-1", received_at=None, supersedes=""):
    return {
        "id": event_id,
        "case": case_id,
        "kind": kind,
        "at": at,
        "received_at": received_at or at,
        "supersedes": supersedes,
    }


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def dataset(self, **kwargs):
        return load_dataset(write_dataset(self.root, **kwargs))


class OutcomeTests(Base):
    def test_timely_and_late_with_explicit_denominator(self):
        dataset = self.dataset(
            cases=[case("R-1"), case("R-2")],
            events=[
                event("E-1", "closed", "2026-03-02T08:00", case_id="R-1"),
                event("E-2", "closed", "2026-03-03T09:00", case_id="R-2"),
            ],
        )
        report = build_report(dataset, resolve(dataset, "2026-03"))
        self.assertEqual(report.totals.timely, 1)
        self.assertEqual(report.totals.late, 1)
        self.assertEqual(report.totals.timely_denominator, 2)
        self.assertEqual(report.totals.timely_rate, 0.5)

    def test_open_within_due_is_not_late_but_open_overdue_is(self):
        # 报告月末为 2026-03-31T23:59；1440 分钟时限在 03-02 09:00 到期。
        dataset = self.dataset(
            cases=[case("R-1"), case("R-2", received="2026-03-31T10:00")],
            events=[],
        )
        report = build_report(dataset, resolve(dataset, "2026-03"))
        first = report.observation("R-1")
        second = report.observation("R-2")
        self.assertEqual(first.outcome, "open_overdue")
        self.assertEqual(second.outcome, "open_within_due")
        self.assertEqual(report.totals.open_overdue, 1)
        self.assertEqual(report.totals.open_within_due, 1)
        self.assertEqual(report.totals.open_total, 2)
        self.assertEqual(report.totals.timely_denominator, 0)
        self.assertIsNone(report.totals.timely_rate)

    def test_missing_due_is_not_counted_as_late_or_timely(self):
        dataset = self.dataset(
            cases=[case("R-1", due_minutes="")],
            events=[event("E-1", "closed", "2026-03-05T09:00")],
        )
        report = build_report(dataset, resolve(dataset, "2026-03"))
        self.assertEqual(report.observation("R-1").outcome, "missing_due")
        self.assertEqual(report.totals.missing_due, 1)
        self.assertEqual(report.totals.timely_denominator, 0)

    def test_event_received_after_as_of_cannot_change_past_report(self):
        dataset = self.dataset(
            cases=[case("R-1")],
            events=[
                event(
                    "E-1",
                    "closed",
                    "2026-03-02T08:00",
                    received_at="2026-04-05T10:00",
                )
            ],
        )
        march = build_report(dataset, resolve(dataset, "2026-03"))
        april = build_report(
            dataset,
            recompute(dataset, "2026-03", as_of="2026-04-30T23:59"),
        )
        self.assertEqual(march.observation("R-1").outcome, "open_overdue")
        self.assertEqual(april.observation("R-1").outcome, "timely")
        self.assertEqual(march.observation("R-1").close_event_id, None)
        self.assertEqual(april.observation("R-1").close_event_id, "E-1")


class LifecycleTests(Base):
    def test_reopen_then_reclose_respects_selected_rule(self):
        dataset = self.dataset(
            cases=[case("R-1", received="2026-03-01T09:00", due_minutes=1440)],
            events=[
                event("E-1", "closed", "2026-03-02T08:00"),
                event("E-2", "reopened", "2026-03-04T08:00"),
                event("E-3", "closed", "2026-03-10T08:00"),
            ],
        )
        first = build_report(dataset, resolve(dataset, "2026-03"))
        # 3 月口径 first_close：首次办结早于时限，按时。
        self.assertEqual(first.observation("R-1").outcome, "timely")
        self.assertEqual(first.observation("R-1").close_event_id, "E-1")

    def test_latest_valid_close_uses_most_recent_close(self):
        dataset = self.dataset(
            cases=[case("R-1", received="2026-04-01T00:00", due_minutes=1440)],
            events=[
                event("E-1", "closed", "2026-04-01T10:00"),
                event("E-2", "reopened", "2026-04-02T08:00"),
                event("E-3", "closed", "2026-04-03T08:00"),
            ],
        )
        report = build_report(dataset, resolve(dataset, "2026-04"))
        self.assertEqual(report.observation("R-1").close_event_id, "E-3")
        self.assertEqual(report.observation("R-1").outcome, "late")
        self.assertTrue(report.observation("R-1").lifecycle.reopened_after_close)

    def test_latest_valid_close_open_after_reopen_is_not_closed(self):
        dataset = self.dataset(
            cases=[case("R-1", received="2026-04-01T00:00", due_minutes=1440)],
            events=[
                event("E-1", "closed", "2026-04-01T10:00"),
                event("E-2", "reopened", "2026-04-02T08:00"),
            ],
        )
        report = build_report(dataset, resolve(dataset, "2026-04"))
        item = report.observation("R-1")
        self.assertEqual(item.outcome, "open_overdue")
        self.assertIsNone(item.close_at)
        self.assertTrue(item.lifecycle.reopened_after_close)

    def test_withdrawal_before_close_leaves_population(self):
        dataset = self.dataset(
            cases=[case("R-1")],
            events=[event("E-1", "withdrawn", "2026-03-02T08:00")],
        )
        report = build_report(dataset, resolve(dataset, "2026-03"))
        item = report.observation("R-1")
        self.assertEqual(item.outcome, "withdrawn")
        self.assertEqual(report.totals.withdrawn, 1)
        self.assertEqual(report.totals.timely_denominator, 0)
        self.assertEqual(report.totals.open_total, 0)

    def test_withdrawal_after_close_keeps_closed_fact(self):
        dataset = self.dataset(
            cases=[case("R-1", due_minutes=1440)],
            events=[
                event("E-1", "closed", "2026-03-02T08:00"),
                event("E-2", "withdrawn", "2026-03-03T08:00"),
            ],
        )
        report = build_report(dataset, resolve(dataset, "2026-03"))
        item = report.observation("R-1")
        self.assertEqual(item.outcome, "timely")
        self.assertEqual(item.close_event_id, "E-1")
        self.assertTrue(item.lifecycle.withdrawn_after_close)
        self.assertIn("withdrawn_after_close", item.drivers)

    def test_reassignment_does_not_change_close_timing(self):
        dataset = self.dataset(
            cases=[case("R-1", due_minutes=1440)],
            events=[
                event("E-1", "assigned", "2026-03-01T09:30"),
                event("E-2", "reassigned", "2026-03-01T12:00"),
                event("E-3", "closed", "2026-03-02T08:00"),
            ],
        )
        report = build_report(dataset, resolve(dataset, "2026-03"))
        self.assertEqual(report.observation("R-1").close_event_id, "E-3")
        self.assertEqual(report.observation("R-1").outcome, "timely")

    def test_correction_supersedes_but_keeps_audit_trail(self):
        dataset = self.dataset(
            cases=[case("R-1", due_minutes=1440)],
            events=[
                event("E-1", "closed", "2026-03-02T08:00"),
                event(
                    "E-2",
                    "corrected",
                    "2026-03-04T08:00",
                    supersedes="E-1",
                    received_at="2026-03-04T09:00",
                ),
            ],
        )
        report = build_report(dataset, resolve(dataset, "2026-03"))
        item = report.observation("R-1")
        self.assertEqual(item.close_event_id, "E-2")
        self.assertEqual(item.outcome, "late")
        self.assertIn("close_correction", item.drivers)
        ledger = explain_case(dataset, report, "R-1")["events"]
        superseded = next(row for row in ledger if row["event_id"] == "E-1")
        self.assertEqual(superseded["status"], "excluded")
        self.assertEqual(superseded["reason"], "superseded_by:E-2")

    def test_correction_received_later_does_not_rewrite_published_result(self):
        dataset = self.dataset(
            cases=[case("R-1", due_minutes=1440)],
            events=[
                event("E-1", "closed", "2026-03-02T08:00"),
                event(
                    "E-2",
                    "corrected",
                    "2026-03-04T08:00",
                    supersedes="E-1",
                    received_at="2026-04-10T09:00",
                ),
            ],
        )
        march = build_report(dataset, resolve(dataset, "2026-03"))
        reopened = build_report(
            dataset, recompute(dataset, "2026-03", as_of="2026-04-30T23:59")
        )
        self.assertEqual(march.observation("R-1").close_event_id, "E-1")
        self.assertEqual(march.observation("R-1").outcome, "timely")
        self.assertEqual(reopened.observation("R-1").close_event_id, "E-2")
        self.assertEqual(reopened.observation("R-1").outcome, "late")


class CategoryTests(Base):
    def test_category_rename_does_not_create_new_category_bucket(self):
        dataset = self.dataset(
            cases=[case("R-1", category="C-1", received="2026-03-01T09:00")],
            events=[event("E-1", "closed", "2026-03-02T08:00")],
        )
        # 分类改名后来源 id 仍是旧 id；4 月口径下无法仅凭 id 认定它属于哪个
        # 新分类时，报告保留来源版本并显式标记，不悄悄算成新事项。
        rules = recompute(
            dataset, "2026-03", category_cut="2026-04", label="rename-only"
        )
        report = build_report(dataset, rules)
        item = report.observation("R-1")
        self.assertEqual(item.category.resolved_id, "C-1")
        self.assertIn("category_unresolved", item.drivers)
        self.assertEqual(len(report.by_category), 1)

    def test_same_label_successor_is_used_for_renamed_category(self):
        categories = [
            {"id": "C-1", "label": "设施维护", "valid_from": "2025-01", "valid_to": "2026-03"},
            {"id": "C-9", "label": "设施维护", "valid_from": "2026-04", "valid_to": ""},
        ]
        dataset = self.dataset(
            cases=[case("R-1", category="C-1", received="2026-03-01T09:00")],
            events=[event("E-1", "closed", "2026-03-02T08:00")],
            categories=categories,
        )
        report = build_report(
            dataset, recompute(dataset, "2026-03", category_cut="2026-04")
        )
        item = report.observation("R-1")
        self.assertEqual(item.category.resolved_id, "C-9")
        self.assertTrue(item.category.renamed)
        self.assertFalse(item.category.mapped)
        self.assertIn("category_version", item.drivers)

    def test_explicit_merge_is_recorded_and_changes_basis(self):
        dataset = self.dataset(
            cases=[case("R-1", category="C-1", received="2026-04-02T09:00")],
            events=[event("E-1", "closed", "2026-04-03T08:00")],
        )
        base = build_report(dataset, resolve(dataset, "2026-04"))
        merged = build_report(
            dataset,
            recompute(dataset, "2026-04", category_map={"C-1": "C-2"}),
        )
        self.assertNotEqual(base.basis_id, merged.basis_id)
        self.assertTrue(merged.observation("R-1").category.mapped)
        self.assertIn("C-1->C-2", merged.basis_id)

    def test_same_source_id_stays_same_category_across_months(self):
        dataset = self.dataset(
            cases=[
                case("R-1", category="C-1", received="2026-03-01T09:00"),
                case("R-2", category="C-2", received="2026-04-01T09:00"),
            ],
            events=[
                event("E-1", "closed", "2026-03-02T08:00", case_id="R-1"),
                event("E-2", "closed", "2026-04-02T08:00", case_id="R-2"),
            ],
        )
        march = build_report(dataset, resolve(dataset, "2026-03"))
        april = build_report(dataset, resolve(dataset, "2026-04"))
        self.assertEqual(march.by_category[0].key, "C-1")
        self.assertEqual(april.by_category[0].key, "C-2")


class RuleSelectionTests(Base):
    def test_report_month_uses_definition_effective_that_month(self):
        dataset = self.dataset(
            cases=[case("R-1", received="2026-04-01T09:00")],
            events=[event("E-1", "closed", "2026-04-02T08:00")],
        )
        march = build_report(dataset, resolve(dataset, "2026-03"))
        april = build_report(dataset, resolve(dataset, "2026-04"))
        self.assertEqual(march.definition_rule, "first_close")
        self.assertEqual(april.definition_rule, "latest_valid_close")
        self.assertTrue(march.definition_version == "1")
        self.assertEqual(april.definition_version, "2")
        self.assertEqual(march.totals.received, 0)
        self.assertEqual(april.totals.received, 1)

    def test_definition_cut_selects_older_rule_for_recompute(self):
        dataset = self.dataset(
            cases=[case("R-1", received="2026-04-01T00:00", due_minutes=1440)],
            events=[
                event("E-1", "closed", "2026-04-01T10:00"),
                event("E-2", "reopened", "2026-04-02T08:00"),
                event("E-3", "closed", "2026-04-03T08:00"),
            ],
        )
        old_rule = build_report(
            dataset,
            recompute(dataset, "2026-04", definition_cut="2026-03", label="v1"),
        )
        new_rule = build_report(dataset, resolve(dataset, "2026-04"))
        self.assertEqual(old_rule.definition_rule, "first_close")
        self.assertEqual(old_rule.observation("R-1").outcome, "timely")
        self.assertEqual(new_rule.definition_rule, "latest_valid_close")
        self.assertEqual(new_rule.observation("R-1").outcome, "late")
        comparison = compare_reports(old_rule, new_rule)
        self.assertEqual(comparison.changes[0].case_id, "R-1")
        self.assertIn("timing_rule", comparison.changes[0].drivers)
        self.assertTrue(comparison.changes[0].outcome_changed)

    def test_unknown_definition_period_is_an_error(self):
        dataset = self.dataset(cases=[case()], events=[])
        with self.assertRaises(RuleError):
            build_report(dataset, recompute(dataset, "2024-12"))


class ComparisonTests(Base):
    def test_category_version_change_appears_even_when_outcome_is_same(self):
        categories = [
            {"id": "C-1", "label": "设施维护", "valid_from": "2025-01", "valid_to": "2026-03"},
            {"id": "C-9", "label": "设施维护", "valid_from": "2026-04", "valid_to": ""},
        ]
        dataset = self.dataset(
            cases=[case("R-1", category="C-1", received="2026-03-01T09:00")],
            events=[event("E-1", "closed", "2026-03-02T08:00")],
            categories=categories,
        )
        before = build_report(dataset, resolve(dataset, "2026-03"))
        after = build_report(
            dataset, recompute(dataset, "2026-03", category_cut="2026-04")
        )
        comparison = compare_reports(before, after)
        self.assertEqual(len(comparison.changes), 1)
        change = comparison.changes[0]
        self.assertFalse(change.outcome_changed)
        self.assertTrue(change.category_changed)
        self.assertIn("category_version", change.drivers)
        self.assertEqual(change.category_before, "C-1")
        self.assertEqual(change.category_after, "C-9")


class ArchiveTests(Base):
    def test_archive_rejects_overwrite_and_keeps_versions(self):
        dataset = self.dataset(
            cases=[case("R-1")],
            events=[event("E-1", "closed", "2026-03-02T08:00")],
        )
        archive = Archive()
        first = build_report(dataset, resolve(dataset, "2026-03"))
        entry = archive.publish(first, reason="初次发布")
        self.assertEqual(entry.basis_id, first.basis_id)
        archive.publish(first, reason="重复发布同口径")  # 内容一致允许
        changed = build_report(
            dataset, recompute(dataset, "2026-03", as_of="2026-03-05T00:00")
        )
        if changed.basis_id == first.basis_id:
            self.skipTest("as_of 未改变口径标识")
        with self.assertRaises(ArchiveError):
            archive.publish(_same_basis_different_payload(first))
        archive.publish(changed, reason="补充晚到记录", supersedes=first.basis_id)
        self.assertEqual(len(archive.history("2026-03")), 2)
        self.assertEqual(archive.history("2026-03")[0].payload, first.to_dict())

    def test_archive_round_trip(self):
        dataset = self.dataset(
            cases=[case("R-1")],
            events=[event("E-1", "closed", "2026-03-02T08:00")],
        )
        report = build_report(dataset, resolve(dataset, "2026-03"))
        archive = Archive()
        archive.publish(report)
        path = self.root / "archive.json"
        archive.save(path)
        loaded = Archive.load(path)
        self.assertEqual(loaded.get("2026-03", report.basis_id).payload, report.to_dict())


def _same_basis_different_payload(report):
    """构造同 basis、不同内容的报告对象，验证归档拒绝覆盖。"""

    class Tampered:
        month = report.month
        rules = report.rules
        basis_id = report.basis_id

        def to_dict(self):
            payload = report.to_dict()
            payload["totals"]["timely"] += 1
            return payload

    return Tampered()


class ValidationTests(Base):
    def test_unknown_category_and_dangling_supersedes_are_rejected(self):
        with self.assertRaises(SourceError):
            self.dataset(cases=[case(category="C-404")], events=[])
        self.tearDown()
        self.setUp()
        with self.assertRaises(SourceError):
            self.dataset(
                cases=[case()],
                events=[event("E-1", "corrected", "2026-03-02T08:00", supersedes="E-9")],
            )

    def test_negative_due_minutes_rejected(self):
        with self.assertRaises(SourceError):
            self.dataset(cases=[case(due_minutes=-1)], events=[])

    def test_supersedes_non_close_event_rejected(self):
        with self.assertRaises(SourceError):
            self.dataset(
                cases=[case()],
                events=[
                    event("E-1", "assigned", "2026-03-01T10:00"),
                    event(
                        "E-2",
                        "corrected",
                        "2026-03-02T10:00",
                        supersedes="E-1",
                    ),
                ],
            )

    def test_duplicate_correction_of_same_close_rejected(self):
        with self.assertRaises(SourceError):
            self.dataset(
                cases=[case()],
                events=[
                    event("E-1", "closed", "2026-03-02T08:00"),
                    event(
                        "E-2", "corrected", "2026-03-03T08:00", supersedes="E-1"
                    ),
                    event(
                        "E-3", "corrected", "2026-03-04T08:00", supersedes="E-1"
                    ),
                ],
            )

    def test_explain_excluded_case(self):
        dataset = self.dataset(
            cases=[case("R-1", received="2026-04-02T09:00")],
            events=[],
        )
        report = build_report(
            dataset, recompute(dataset, "2026-04", as_of="2026-04-01T23:59")
        )
        info = explain_case(dataset, report, "R-1")
        self.assertFalse(info["included"])
        self.assertEqual(info["reason"], "received_after_as_of")


class CliTests(unittest.TestCase):
    def test_report_and_explain_from_fixtures(self):
        from contextlib import redirect_stdout
        from io import StringIO
        from community_analysis.cli import main

        fixtures = Path(__file__).resolve().parents[1] / "fixtures"
        buffer = StringIO()
        with redirect_stdout(buffer):
            self.assertEqual(main(["--data", str(fixtures), "report", "2026-04"]), 0)
        payload = json.loads(buffer.getvalue())
        self.assertEqual(payload["definition"]["version"], "2")
        self.assertEqual(payload["totals"]["missing_due"], 1)
        self.assertIn("source_digest", payload)

        buffer = StringIO()
        with redirect_stdout(buffer):
            self.assertEqual(
                main(["--data", str(fixtures), "explain", "R-101", "2026-03"]), 0
            )
        explained = json.loads(buffer.getvalue())
        self.assertEqual(explained["category_version"]["resolved_id"], "C-OLD")
        self.assertEqual(explained["timing"]["close_event_id"], "E-2")


if __name__ == "__main__":
    unittest.main()
