"""命令行入口：生成月报、重算比较、追溯单事项或归档。"""

from __future__ import annotations

import argparse
import csv
import io
import json
from pathlib import Path
import sys

from .archive import Archive, ArchiveError
from .compare import compare_reports
from .loader import SourceError, load_dataset
from .report import Report, build_report
from .rules import RuleError, recompute, resolve
from .trace import explain_case

DEFAULT_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="community-analysis",
        description="按可追溯口径生成网格民生服务月报（合成资料）",
    )
    parser.add_argument(
        "--data", default=str(DEFAULT_FIXTURES), help="资料目录（含四个 JSON 文件）"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    report = sub.add_parser("report", help="生成某月报告")
    report.add_argument("month")
    report.add_argument("--as-of", dest="as_of", help="报告形成时点，默认月末")
    report.add_argument("--category-cut", dest="category_cut", help="分类版本切点月")
    report.add_argument("--definition-cut", dest="definition_cut", help="指标定义切点月")
    report.add_argument(
        "--map",
        action="append",
        default=[],
        metavar="OLD=NEW",
        help="显式分类归并，可重复",
    )
    report.add_argument("--label", default="", help="本次口径的说明")
    report.add_argument("--format", choices=("json", "csv"), default="json")
    report.add_argument("--archive", help="归档文件路径")
    report.add_argument("--reason", default="", help="归档说明")
    report.add_argument("--supersedes", help="被替代的口径 basis_id")

    diff = sub.add_parser("compare", help="比较两种口径")
    diff.add_argument("month")
    diff.add_argument("--as-of", dest="as_of")
    diff.add_argument("--after-as-of", dest="after_as_of")
    diff.add_argument("--before-cut", dest="before_cut")
    diff.add_argument("--after-cut", dest="after_cut")
    diff.add_argument("--before-map", action="append", default=[])
    diff.add_argument("--after-map", action="append", default=[])
    diff.add_argument("--format", choices=("json",), default="json")

    trace = sub.add_parser("explain", help="追溯单个事项")
    trace.add_argument("case_id")
    trace.add_argument("month")
    trace.add_argument("--as-of", dest="as_of")
    trace.add_argument("--category-cut", dest="category_cut")
    trace.add_argument("--definition-cut", dest="definition_cut")
    trace.add_argument("--map", action="append", default=[])

    return parser


def _mapping(pairs: list[str]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for pair in pairs:
        if "=" not in pair:
            raise RuleError(f"--map 需要 OLD=NEW，收到 {pair!r}")
        old, new = pair.split("=", 1)
        mapping[old] = new
    return mapping


def _rules(args):
    dataset = _dataset(args)
    mapping = _mapping(getattr(args, "map", []))
    if not any(
        [
            getattr(args, "category_cut", None),
            getattr(args, "definition_cut", None),
            mapping,
            getattr(args, "label", ""),
        ]
    ):
        return resolve(dataset, args.month, as_of=getattr(args, "as_of", None))
    return recompute(
        dataset,
        args.month,
        as_of=getattr(args, "as_of", None),
        category_cut=getattr(args, "category_cut", None),
        definition_cut=getattr(args, "definition_cut", None),
        category_map=mapping,
        label=getattr(args, "label", ""),
    )


def _dataset(args):
    return load_dataset(args.data)


def _report_json(report: Report, *, indent: int = 2) -> str:
    return json.dumps(report.to_dict(), ensure_ascii=False, indent=indent)


def _report_csv(report: Report) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "case_id",
            "grid",
            "received",
            "source_category",
            "category_id",
            "category_label",
            "outcome",
            "due_minutes",
            "due_at",
            "close_at",
            "close_event_id",
            "elapsed_minutes",
            "close_basis",
            "drivers",
        ]
    )
    for item in report.observations:
        row = item.to_dict()
        writer.writerow(
            [
                row["case_id"],
                row["grid"],
                row["received"],
                row["source_category"],
                row["category_id"],
                row["category_label"],
                row["outcome"],
                "" if row["due_minutes"] is None else row["due_minutes"],
                row["due_at"] or "",
                row["close_at"] or "",
                row["close_event_id"] or "",
                "" if row["elapsed_minutes"] is None else row["elapsed_minutes"],
                row["close_basis"],
                ";".join(row["drivers"]),
            ]
        )
    buffer.write("\n")
    writer.writerow([])
    writer.writerow(
        [
            "分组",
            "键",
            "名称",
            "受理",
            "已办结",
            "按时",
            "逾期",
            "按时率分母",
            "按时率",
            "未结",
            "未结-时限内",
            "未结-已逾期",
            "缺少时限",
            "撤回",
        ]
    )
    for title, groups in (("分类", report.by_category), ("网格", report.by_grid)):
        for group in groups:
            row = group.to_dict()
            writer.writerow(
                [
                    title,
                    row["key"],
                    row["label"],
                    row["received"],
                    row["closed"],
                    row["timely"],
                    row["late"],
                    row["timely_denominator"],
                    "" if row["timely_rate"] is None else f"{row['timely_rate']:.4f}",
                    row["open_total"],
                    row["open_within_due"],
                    row["open_overdue"],
                    row["missing_due"],
                    row["withdrawn"],
                ]
            )
    return buffer.getvalue()


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        dataset = _dataset(args)
        if args.command == "report":
            rules = _rules(args)
            report = build_report(dataset, rules)
            if args.format == "csv":
                sys.stdout.write(_report_csv(report))
            else:
                sys.stdout.write(_report_json(report) + "\n")
            if args.archive:
                archive = (
                    Archive.load(args.archive)
                    if Path(args.archive).exists()
                    else Archive()
                )
                archive.publish(report, reason=args.reason, supersedes=args.supersedes)
                archive.save(args.archive)
            return 0

        if args.command == "compare":
            before_map = _mapping(args.before_map)
            after_map = _mapping(args.after_map) if args.after_map else before_map
            if args.before_cut or before_map:
                before_rules = recompute(
                    dataset,
                    args.month,
                    as_of=args.as_of,
                    category_cut=args.before_cut,
                    category_map=before_map,
                    label="before",
                )
            else:
                before_rules = resolve(dataset, args.month, as_of=args.as_of)
            if args.after_as_of or args.after_cut or after_map:
                after_rules = recompute(
                    dataset,
                    args.month,
                    as_of=args.after_as_of or before_rules.as_of,
                    category_cut=args.after_cut or args.before_cut,
                    category_map=after_map,
                    label="after",
                )
            else:
                after_rules = resolve(
                    dataset, args.month, as_of=args.after_as_of
                )
            comparison = compare_reports(
                build_report(dataset, before_rules),
                build_report(dataset, after_rules),
            )
            sys.stdout.write(
                json.dumps(comparison.to_dict(), ensure_ascii=False, indent=2) + "\n"
            )
            return 0

        if args.command == "explain":
            rules = _rules(args)
            report = build_report(dataset, rules)
            sys.stdout.write(
                json.dumps(
                    explain_case(dataset, report, args.case_id),
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n"
            )
            return 0
    except (SourceError, RuleError, ArchiveError, KeyError) as exc:
        sys.stderr.write(f"错误: {exc}\n")
        return 2
    return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
