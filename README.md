# 社区诉求口径与履约分析

用于研究社区服务事项的分类、责任时限、办理事实和月度指标口径之间的关系。资料为合成记录，不包含真实居民信息。

`fixtures/` 保存 UTF-8 合成业务资料，字段、关系和时间含义见 `DATA.md`。项目使用 Python 3.11 标准库，无第三方依赖。

## 快速开始

```bash
# 资料结构检查 + 分析逻辑测试
python3 -m unittest discover -s tests -v

# 按 2026-03 当时有效口径生成月报（JSON）
python3 -m community_analysis report 2026-03

# 表格视图（逐事项明细 + 分类/网格汇总）
python3 -m community_analysis report 2026-04 --format csv

# 用 4 月分类口径重算 3 月，并列出哪些事项/字段改变
python3 -m community_analysis compare 2026-03 --after-cut 2026-04

# 追溯单个事项：来源记录、分类版本、计时依据、节点可见性
python3 -m community_analysis explain R-101 2026-03

# 发布月报并归档；更正只能形成新版本，不覆盖旧结论
python3 -m community_analysis report 2026-03 --archive reports/archive.json \
    --reason "3 月月报"
python3 -m community_analysis report 2026-03 --as-of 2026-04-30T23:59 \
    --archive reports/archive.json --reason "补录后再看 3 月" \
    --supersedes "2026-03|as_of=2026-03-31T23:59|cat=2026-03|def=2026-03|metric=timely_close|rule=first_close@1"
```

## 口径与判定

一次报告由 `RuleSet` 固定：

| 维度 | 含义 | 默认 |
| --- | --- | --- |
| `report_month` | 统计归属月，按受理时间纳入事项 | 必填 |
| `as_of` | 报告形成时点，只看见此前**收到**的节点 | 报告月末 23:59 |
| `category_cut` | 分类目录版本切点 | 报告月 |
| `definition_cut` | 指标定义（计时规则）版本切点 | 报告月 |
| `category_map` | 显式分类归并，如 `C-OLD=C-NEW` | 空 |

原始月报用“当时有效”口径；重算必须显式给出 `--category-cut`、`--definition-cut`、`--as-of` 或 `--map`，两份报告的 `basis_id` 不同，可并存。

逐事项判定（`outcome`）：

- `timely` / `late`：已办结且有时限事实，按所选规则的办结节点计时；
- `open_within_due` / `open_overdue`：未结，分别表示仍在时限内、确实已逾期；
- `missing_due`：来源没有责任时限，不进入按时率分母；
- `withdrawn`：办结前撤回，退出总体。

按时率 = `timely / (timely + late)`，分母始终与分子一起输出；`received`、`closed`、`open_total`、`missing_due` 等分开列示，避免把“缺少节点”混同为“逾期”。

## 时间与更正语义

- 节点有业务时间 `at` 和记录时间 `received_at`。月报只使用 `as_of` 之前**收到**的节点，因此晚到的补录或更正不会改写已发布结论；要看新事实必须用更晚的 `as_of` 重算。
- `events.supersedes` 表示办结后更正：旧节点在更正可见后不再生效，但来源行保留，`explain` 会列出 `superseded_by`。`corrected` 继承被更正节点的办结含义。
- 重开/撤回会改变“当前有效状态”，但不抹去首次办结事实；`first_close` 与 `latest_valid_close` 两种规则分别对应两种口径。
- 分类改名：来源 `cases.category` 仍是受理时记录的 id。报告按 `category_cut` 找当时有效的版本；找不到同 id 且没有同名后继时，保留来源 id 并标记 `category_unresolved`，不把旧事项算成新分类。
- 归档以 `(month, basis_id)` 为键。同口径内容变化会被拒绝（返回 `ArchiveError`），必须用新 `basis_id` 重算并显式 `supersedes`。

## 代码结构

| 模块 | 职责 |
| --- | --- |
| `loader.py` | 读取/校验四个来源表，保留空字符串语义，维护 `supersedes` 链 |
| `periods.py` | 月份区间与分钟级业务时间 |
| `rules.py` | 报告口径、分类版本解析、指标定义版本解析 |
| `lifecycle.py` | 按 `as_of` 还原节点生命周期、选择计时节点 |
| `report.py` | 逐事项判定与分类/网格聚合（含分母） |
| `compare.py` | 两种口径的逐事项差异及原因 |
| `trace.py` | 单事项证据链 |
| `archive.py` | 不可覆盖的报告归档 |
| `cli.py` | 命令行入口 |

`tests/test_fixtures.py` 只校验资料结构；`tests/test_analysis.py` 覆盖按时/逾期判定、缺少时限、改派、撤回、办结后更正、晚到记录、分类改名与归并、口径切换和归档防覆盖。
