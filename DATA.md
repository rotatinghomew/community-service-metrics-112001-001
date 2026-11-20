# 社区诉求口径与履约分析：资料说明

用于研究社区服务事项的分类、责任时限、办理事实和月度指标口径之间的关系。资料为合成记录。

每个 JSON 文件均为 UTF-8 编码。空字符串表示来源没有给出该事实，不等同于零、否或业务终态。标识仅在本项目资料内有意义。

## `categories.json`

字段：id, label, valid_from, valid_to。共 2 条合成记录。

## `cases.json`

字段：id, grid, category, received, due_minutes。共 5 条合成记录。

## `events.json`

字段：id, case, kind, at, received_at, supersedes。共 5 条合成记录。

## `definitions.json`

字段：id, metric, version, valid_from, valid_to, rule。共 2 条合成记录。

## 关系与口径提示

事项.category 对应 categories.id；events.case 对应 cases.id；events.supersedes 为空或指向被更正事件；definitions 保存指标适用月份与版本。due_minutes 为空表示没有可用时限事实。
