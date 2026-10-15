# 粤芯半导体（301660）独立做T模拟

**隔离设计**：本工具不接入券商、不发实盘订单、不修改 `config/research-system.yaml`，也不触碰私有 `paper_research.db`。

## 先检查研究覆盖

```bash
python scripts/check_research_coverage.py
```

当前固定研究名单只有 300136、301297；301660 尚未加入。现有 `PaperStore` 将完整配置哈希绑定到账本，直接增加股票会导致已有账本无法打开。正式纳入需要新策略版本/迁移与保留旧账本；不能只改 YAML。

## 运行独立反T模拟

```bash
python -m unittest tests.test_yuexin_t_paper
python scripts/yuexin_t_paper.py --input data/yuexin_frozen_signals.csv --output reports/research/yuexin-t-paper.json
```

CSV 标题：`session,open,high,low,close,volume,signal`。
`signal` 只能是 `sell`、`buyback`、`hold`。每个信号须在**该交易日前冻结**，否则会出现未来函数。

初始参考持仓1500股、成本37.911元/股，默认单次300股，最大卖出未回补600股；优先反T，卖出已有持仓再择日买回。开盘价加减10bp滑点；按模拟佣金0.03%（最低5元）、过户费0.001%、卖出印花税0.05%估算，具体券商费率须复核。按日线开盘模拟，不使用当天最高/最低价倒推成交，也不保证真实开盘成交。输出每日现金、剩余股数、未回补股数、相对一直持有的净值差及最大回撤。

**局限**：初始成本只用于参考，不把券商历史已实现损益误认为当期做T损益。仅有日K无法评价盘中T点、盘口成交和同日先卖后买；若要真正验证日内做T，应接入分钟线/逐笔数据及带时间戳的事前信号。工具默认不生成交易信号，不宣称任何已实现收益。
