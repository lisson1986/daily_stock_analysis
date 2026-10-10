# 完整股票研究闭环（个人部署、opt-in）

入口：`python scripts/run_research.py <mode>`。此扩展只生成研究报告和模拟账本，不连接证券交易账户、不构造通知服务。既有Web/API/真实持仓接口和原始报告格式不变。

## 已接通的代码路径

- 复用全市场snapshot、规则评分、技术指标、基本面聚合、搜索服务与GeminiAnalyzer模型路由；监测最多100支，固定包含STOCK_LIST之外的专用watchlist（默认300136/301297），深度分析最多10支。
- 新闻在进入模型前按海外域名准入过滤；缺日期、过期、未来日期保留披露，但不计为有效新催化。国内原始行情/财务字段允许，国内投资分析不注入。
- 复用模型的公开`generate_text`入口生成海外事件→A股行业传导摘要，最多额外1次请求；模型/搜索失败出缺失记录，不强制买入。模型重试、fallback和补完整报告可能增加实际调用数，尚未实现货币金额硬预算。
- `PaperStore.apply`同一事务保存冻结输入、订单、成交、净值及报告；同交易日重跑返回原报告。研究账本用独立表，避免更改用户持仓服务的事件契约。
- `restore`验证hash/SQLite完整性；`checkpoint`通过SQLite backup API保存包含WAL的一致性快照。工作流将全部checkpoint一次提交到私有状态仓库，fast-forward push失败则不发布本次报告。
- 周复盘分别运行原生5/10日建议评估、前瞻模拟指标、冻结信号回放。`backtest`另支持用户提供PIT历史数据的价格规则基线；未伪造多年AI新闻回测，也不自动下载不存在的PIT股票池。
- 原生建议评估增加向后兼容参数`refill_missing_daily=True`（原默认行为不变）；本闭环显式设为false，只使用保存的原始价日线，缺失未来窗口标为insufficient_data，避免自动补取复权价后与原始价混算。

## GitHub启动步骤（手机可操作）

1. 将本变更合入个人代码仓库的main。现有定时任务保持原样，直到显式启用。
2. 在GitHub创建一个**私有**状态仓库，勾选添加README，使默认分支存在。不要将已含真实持仓的数据库放进公开代码仓库。
3. 创建仅授权该私有仓库的fine-grained token，权限`Contents: Read and write`（元信息只读）；在个人代码仓库的Settings → Secrets and variables → Actions：

| 位置 | 名称 | 值 |
|---|---|---|
| Repository variable | `RESEARCH_STATE_REPO` | 第2步的`owner/private-repo` |
| Repository secret | `RESEARCH_STATE_TOKEN` | 第3步token，在安全页面填写 |
| Repository variable（最后才设置） | `RESEARCH_SYSTEM_ENABLED` | `true` |

4. 复用已有DeepSeek/兼容模型路由。新工作流显式映射`DEEPSEEK_API_KEY`、`OPENAI_API_KEY/BASE_URL/MODEL`、`LITELLM_MODEL/CONFIG`、Gemini、Anthropic。若已有配置使用其他channel路由，启用前补同等映射；不要只因Secret存在宣称调用成功。
5. 搜索服务择一：复用`TAVILY_API_KEYS`、`BRAVE_API_KEYS`或`SERPAPI_API_KEYS`。搜索无有效海外证据时允许空仓。没有搜索凭据也能结算与记录缺失，但不算完整消息研究验收。
6. Actions → **完整研究与模拟交易** → Run workflow → `initialize`。这一步只创建20万元模拟账户与一致性备份，不产生订单；已有checkpoint拒绝重新初始化。
7. 在真实交易日16:00后手动选`close`。核验报告里的交易日、两只固定关注股、有效日线数量、模型结果、来源URL、模拟订单和状态提交。首次不会凭空生成过去收益；次日撮合才可能买入。
8. 通过验收后设置`RESEARCH_SYSTEM_ENABLED=true`。新工作流取代旧00工作流的schedule；旧手动入口仍可用。两条共享`stock-analysis`并发组，避免同时运行。旧手动分析不参与新私有状态账本。

当前连接若没有创建私有仓库、Secrets/Variables或dispatch接口，需要在GitHub安全页面完成这些设置；仓库push权限不代表上述接口均可操作。密钥不放入聊天、不提交`.env`。

## 调度

| 北京时间 | GitHub cron（UTC） | 行为 |
|---|---|---|
| 交易日07:30 | `30 23 * * 0-4` | 海外增量研究；保存已有订单摘要，不改写冻结订单 |
| 交易日18:00 | `0 10 * * 1-5` | 先补结算遗漏日、当日撮合/研究、冻结次日订单、持久化 |
| 周六10:00 | `0 2 * * 6` | 建议评估、模拟复盘与冻结输入回放 |

Actions可能延迟，不保证盘中定点运行。`exchange_calendars.XSHG`范围不足、缺失或异常时拒绝交易；非交易日跳过close。周期中断后`catch-up`用此前已冻结的订单逐日补结算，不补造遗漏日的新AI信号；遇到无法验证数据就停止。

## 模拟成交纪律

参数全部在`config/research-system.yaml`。修改参数/来源域名/股票范围后要求新独立账户，不能用新规则改写旧收益。

- 本金20万元是模拟默认值，独立于真实资金与持仓。每天最多冻结1支，最多5支，单股20%、总仓位80%；单笔计划风险1%，100股向下取整。
- 收盘信号下一交易日开盘估算成交；最高买入价为信号收盘价×1.02，超限/不可成交取消；不会事后选择5天最高价卖出。
- 开盘买单只用上次已结算现金、风险与仓位，不使用当天未来卖出的资金/未来收盘回撤。这一限制避免回测资金时间穿越。
- T+1；买入日计第1个交易日，第5个交易日收盘退出。初始止损5%、止盈10%，涨幅6%后以此前最高收盘价下方4%作为下一交易日起的跟踪止损。
- 跳空止损按可交易开盘价减滑点；同日止损/止盈都触发时先止损。止盈限价不按低于限价成交。滑点单边10bp；日成交量参与率最多1%，可能部分成交。
- 验证证券简称、上市时间、分红与配股事件后，普通主板/创业板按相应涨跌幅限制保守判断；触及日涨停不买、触及日跌停不卖。ST、未知元信息、上市不足60日不参与。
- 停牌只有在对应交易日的停复牌列表匹配后才按此前收盘价记账，标记价格陈旧、成交量为0且不能买卖。没有证据就不补造停牌K线。
- 公司行为目前**暂停结算并要求核对调整**，没有自动分红/配股记账。因此不能冒称全类型公司行为自动处理。缺少当前原始成交价、元信息或公司行为日历时也暂停。
- 回撤8%暂停新增订单，12%安排可卖仓位退出；受停牌、跌停、数据缺失影响，不能保证实际损失止于阈值。

## 费用与数据边界

默认佣金0.03%、最低5元，卖出税率0.05%、过户费用0.001%，均为**明确的实验假设**，不是对当前法规/你的券商费率的核定。历史规则回测同样使用该账户固定费用假设；若需跨税费制度变更的真实历史收益，应提供日期化费率并扩展规则后新建账户，不将固定费率结果宣传为真实交易成本。

成交价只用未复权数据：腾讯raw-day优先、AkShare `adjust=''`兜底，保留source/errors。不将DSA默认前复权价格直接拿来撮合。指标目前同样使用原始价；有公司行为的历史窗口可能扭曲趋势，报告已披露；这是第一版限制，未来可增加复权指标与原始撮合价双轨。

沪深300使用可得日线价格指数作为参考，单列起止日与缺失。不是分红再投资基准；首次/末次日期不同于账户完整区间时不宣称精确同区间超额收益。尚未接中证500基准与长期年化/统计显著性计算。

财务为原始聚合字段，不代表取得完整年报/季报全文。其余非深度股目前为行情和技术监测，未逐股下载完整财务。不能证明某股从未被任何券商推荐。100股只是目标上限，缺失数据不凑数。

## 本地与测试命令

```bash
python scripts/run_research.py initialize
python scripts/run_research.py close
python scripts/run_research.py checkpoint
python scripts/run_research.py restore
python scripts/run_research.py replay
python scripts/run_research.py weekly
python -m pytest tests/research_system -q
```

先安全配置`.env`与有效模型/搜索Key；本地使用自己的持久磁盘不必填GitHub状态token。`RESEARCH_CONFIG_PATH`、`RESEARCH_PAPER_DB`、`RESEARCH_STATE_DIR`和`DATABASE_PATH`可覆盖本地路径。initialize只允许一次；备份先于任何文件迁移。restore时不得有其他进程使用目标SQLite，恢复到临时目录后检查再迁移。

离线演练：`close --fixture packet.json`，packet结构为`{session, previous_session, bars, candidate, research}`；bar必须带`date/open/high/low/close/volume/adjustment='none'/buyable/sellable`，candidate为`{code,close}`或null。fixture收益必须标记为合成测试，不能当真实股票收益。

历史价格规则基线：用独立`--paper-db`先initialize，再`backtest --input dataset.json`。dataset包含`schema_version=1`、`point_in_time_universe=true`、`source`、`fee_assumption`、`previous_session`与sessions数组；每个session包含session/previous_session/bars。PIT声明来自数据供应者，程序不替供应者认证真实性。基线规则是20日均线之上按过去20日涨幅选择一支，用相同撮合/风控；不读取未来新闻、不声称AI历史成绩。

## 验证与回滚

离线测试覆盖实际CLI首次初始化→两日冻结/撮合→重复执行→一致性备份→删除本地db→恢复→回放，也覆盖真实DSA存储桥接（外部行情/模型以fixture替代）、止损/止盈冲突、T+1、费用、部分退出、5天退出、停牌限制、公司行为暂停、配置变更拒绝与校验失败。原生回测/持仓服务回归测试独立执行。

在线行情、模型额度、私有状态仓库权限、GitHub定时及真实推送尚须部署环境验收；本扩展不启用推送。报告格式为新增Markdown/JSON，既有仪表盘未改；无法提供线上截图时，以测试生成的Markdown/JSON artifact作为可视证据，明确标记fixture。

关闭：把`RESEARCH_SYSTEM_ENABLED=false`，旧18:00任务恢复；保留私有状态仓库与token直到导出备份。不删除模拟账户、不重置历史。版本回滚前检查config_hash与schema兼容，不强制覆盖远端状态。

私有状态仓库的git历史可恢复完整checkpoint；Actions artifacts保留30天仅用于报告。仓库体积、token到期和依赖/交易日历更新需要维护；不承诺永久零费用或无限存储。英文文档本次未新增：这是个人中文部署扩展，未更改已有双语公共产品文档。
