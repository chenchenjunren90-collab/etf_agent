# ETF 投资智能体

面向普通用户与 ETF 投资比赛的智能交互系统。用户可以通过对话补充资金规模、风险偏好和投资期限，获取当日 ETF 配置建议，并继续追问持仓理由、相关新闻及新闻对 ETF 市场的潜在影响。

## 在线体验

- 外链地址：<http://39.105.104.230/etf-agent/chat/>
- 演示链接：<http://39.105.104.230/etf-agent/chat/>
- 文档链接：<http://39.105.104.230/etf-agent/chat/docs>
- 代码仓库：<https://github.com/chenchenjunren90-collab/etf_agent>

## 扫码体验

![ETF 投资智能体二维码](assets/etf-agent-qr.png)

## 主要能力

- 通过选择题和填空题收集资金、风险偏好、投资期限等必要信息
- 基于行情、技术指标、宏观事件和新闻信号生成 ETF 建议
- 解释投资理由以及新闻对相关 ETF 的可能影响
- 对非 ETF 个股或无关话题进行边界提示，引导用户回到 ETF 投资
- 按比赛要求生成 `symbol`、`symbol_name`、`volume` 格式的当日建议
- 在非交易日、行情陈旧或数据异常时阻止不可靠输出

## 本地运行

安装依赖并配置本地 `.env` 后，可分别启动智能体和看板：

```powershell
python etf_agent_chat.py
python dashboard_server.py --no-browser
```

运行核心检查：

```powershell
python _recheck_bugs.py
python _test_agent_chat.py
python _test_decision_integrity.py
python _test_competition_isolation.py
python _test_personalization.py
```

## 重要说明

本项目只提供 ETF 研究与决策辅助，不覆盖个股投资建议。输出基于历史行情、公开资讯和模型判断，不承诺收益，也不能替代持牌机构或专业投资顾问的意见。市场有风险，实际投资决策应由用户独立作出。

## 盈利能力评估

项目把“真实历史成绩”和“当前策略历史模拟”严格分开：

```powershell
python _evaluate_profitability.py --target 0.005 --cost-bps 5
python _backtest_full_pipeline.py --start 2026-03-02 --end 2026-07-10 --cache-only
```

每次正式比赛决策会写入不可覆盖的内容哈希快照，记录策略版本、Git 提交、新闻时间、LLM 元数据与最终输出。评估报告同时显示十日 `0.5%` 达标率、非重叠窗口、沪深 300 基准、成本后收益、最大回撤和后 30% 留出区间。

当前策略采用“候选排序 + 独立盈利证据层”的两阶段决策。短赛评分先按正向权重总和归一化，排序模型只负责提出候选；独立证据层再检查成本后的相似历史收益、新闻与 ETF 的直接映射、单日急涨、缩量末端突破和同一 ETF 连续追入风险。价格与历史分还必须独立达到准入线，新闻只能确认或否决候选，不能单独把弱价格候选抬进交易。高置信机会仓位上限为 12%，证据较弱但已校准为正优势的机会仓位上限为 8%；样本不足时最多使用 5% 试探仓位，逐日前推校准为负或存在入场风险时保持空仓。该机制希望在约 11–12 个交易日内获得少量有效出手机会，但不会为了凑次数强制交易，也不能保证固定期限收益。

完整回测采用严格时点约束：价格特征只能看到决策日前已经收盘的 K 线；新闻必须存在有效发布时间，发布时间和抓取时间均不得晚于当日决策截止时点；模型校准每一天只使用更早日期已经完成结算的样本。回测不会写回新闻归档或 LLM 调试文件，避免前一次运行污染后一次结果。报告同时输出滚动 12 日交易次数、成本后收益、最大回撤和后 30% 留出区间。

新闻映射优先使用标题、摘要与结构化关键词。只有核心字段无法映射且正文仅指向一个 ETF 时才使用全文，避免行业综述中顺带出现的词语污染其他 ETF。LLM 默认处于 `audit` 模式，可提供解释和风险提示，但不能直接覆盖量化分数。

目标控制默认为 `monitor`，只记录窗口进度，**不会改变仓位**。设置 `ETF_TEN_DAY_GOAL_MODE=risk_cap` 才启用前瞻波动率仓位上限；设置为 `fixed`/`enforce` 才启用达标保护和回撤防守。固定模式必须同时显式配置 `ETF_GOAL_START_DATE`，窗口长度可通过 `ETF_GOAL_WINDOW_DAYS` 调整；缺少开始日时固定控制拒绝生效。

线上运行模式应在项目目录的 `.env` 中显式配置，例如 `ETF_TEN_DAY_GOAL_MODE=risk_cap`、`ETF_LLM_THEME_MODE=audit` 和 `ETF_ALLOW_LLM_SCORE_CONTROL=0`；`.env` 不提交到 GitHub。只有研究环境明确设置 `ETF_ALLOW_LLM_SCORE_CONTROL=1` 时，旧版 `blend`/`override` 才会生效。

当日盈亏默认在上海时间 16:15 后才允许结算，并要求当日成交量不低于近 20 日中位数的 5%，避免早盘残留的半截 K 线被误当成收盘数据。可通过 `ETF_SETTLEMENT_READY_TIME` 和 `ETF_SETTLEMENT_MIN_VOLUME_RATIO` 调整。
