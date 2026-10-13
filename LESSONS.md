# Lessons from merged work

这里保存 greeks 的合并经验。改动前先读本文件。每条先给加粗祈使规则，再给证据（SHA / 现象），结尾 `Seen N× (日期…)`。满 3 个不同复盘日迁入 [docs/code-standards.md](docs/code-standards.md) 并在此留 Moved 索引。

## Performance reports

**绩效报告按计算索引输入，避免重复扫描同一批原始数据。**

- 证据：`baab7af0` 改 `app/performance.py`；`tests/test_performance.py`。
- Seen 1× (2026-10-13)

**图表路径只解析一次时间戳并缓存坐标；用解析次数预算钉死回归。**

- 证据：`160e9329` 改 `app/static/performance.js` 预解析 `Date.parse` 并缓存 x/y；`tests/test_dashboard.cjs` 断言大规模点列解析次数线性上界。
- Seen 1× (2026-10-13)

## Theme / docs

本窗口另有 `6fee372c`（深色主题），纯样式，无缺陷类教训。

## Moved to the code standards

尚无。
