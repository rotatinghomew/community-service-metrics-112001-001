"""网格民生服务分析：分类口径、办理节点与月度按时率的可追溯计算。

设计要点：
- 受理记录、事件、分类目录和指标定义都是带时间边界的来源事实；
- 计算某个报告月的原结果时，只使用该月当时有效的口径与记录；
- 也支持显式选择另一套口径（month/as-of/reporting rules）重算可比范围；
- 更正通过 supersedes 链表达，不覆盖已发布结论；
- 空字符串表示“来源未给出该事实”，不是零、否或终态。
"""

from .loader import Dataset, load_dataset
from .periods import Period, parse_period, period_of
from .rules import RuleError, RuleSet, recompute as recompute_rules, resolve as resolve_rules
from .report import CaseObservation, GroupStats, Report, build_report
from .archive import Archive, ArchiveError, ArchivedReport
from .compare import Comparison, compare_reports
from .trace import explain_case

__all__ = [
    "Dataset",
    "load_dataset",
    "Period",
    "parse_period",
    "period_of",
    "RuleSet",
    "resolve_rules",
    "recompute_rules",
    "CaseObservation",
    "GroupStats",
    "Report",
    "build_report",
    "Archive",
    "ArchiveError",
    "ArchivedReport",
    "Comparison",
    "compare_reports",
    "explain_case",
]
