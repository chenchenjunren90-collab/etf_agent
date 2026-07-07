# ETF 日内配置智能体

面向 A 股 ETF 盘前配置的「大模型语义理解 + 规则引擎风控」双层系统。  
**接手请先读本文件，再读 `STRATEGY_DOC.md` 与 `回测方案_数据源与参数优化.md`。**

---

## 1. 阅读顺序（给接手智能体 / 开发者）

| 顺序 | 文件 | 内容 |
|------|------|------|
| ① | **本 README** | 入口、目录、命令、生产参数 |
| ② | `STRATEGY_DOC.md` | 模块说明、每日流程、数据源 |
| ③ | `回测方案_数据源与参数优化.md` | 三轨回测，**禁止混用** CSDN 与东财结论 |
| ④ | `参数来源与数据分级说明.md` | 48 参数 L1/L2/L3 |
| ⑤ | `说明书附录_回测与参数说明.md` | 答辩用结论摘要 |
| ⑥ | 上级目录 `ETF智能体说明书.docx` | 各类分数计算公式 |

---

## 2. 生产参数

| 项 | 值 |
|----|-----|
| 综合评分 | 新鲜主题×25% + 陈旧×10% + 趋势×30% + 历史量价×20% − 风险×15% − 轮动惩罚 |
| 评分闸门 | **50**（大模型强信号时可降至 42） |
| 经济日仓位上限 | 1–2 条 85% / 3–5 条 75% / 6+ 条 65% |
| 极端走弱 | 约 **15%** 试探仓（非空仓） |
| 决策提示词 | `prompts/decider_zh.md` |
| 新闻切分 | 上一交易日 **15:00** 后为新鲜新闻 |

代码真源：`scoring.py`、`position.py`、`strategy.py`、`daily_job.py`。

---

## 3. 项目结构（核心）

```
etf_agent/
├── daily_job.py              # 【实盘唯一入口】每日 09:25 流程
├── strategy.py               # 决策编排：LLM 融合 + 风控 + 仓位
├── scoring.py                # 综合评分、闸门、轮动惩罚
├── position.py               # 市场环境、新闻调仓、资金分配
├── features.py / indicators.py
├── news_signal.py            # 关键词预筛
├── news_llm_scorer.py        # 语义评分
├── news_time_split.py        # 新鲜/陈旧切分
├── llm_decider.py / llm_client.py
├── econ_calendar.py
├── theme_signal.py
├── run_news_backtest.py      # 【东财全链路回测】
├── csdn_backtest.py          # 【CSDN 长窗回测】
├── warm_llm_decider_cache.py # 预热大模型决策缓存
├── check_llm_pipeline.py     # 链路自检
├── data/
│   ├── *.csv                 # ETF 日 K 线
│   ├── csdn_scores/          # CSDN 日度情绪缓存
│   ├── llm_cache/            # 大模型磁盘缓存（回测必需）
│   ├── daily_output/         # 实盘每日输出
│   └── news_backtest/        # 回测报告（已精简）
└── .env                      # DEEPSEEK_API_KEY（勿提交仓库）
```

---

## 4. 环境配置

```powershell
cd etf_agent
pip install -r requirements.txt
copy .env.example .env
# 编辑 .env 填入 DEEPSEEK_API_KEY
```

---

## 5. 常用命令

### 实盘 / 当日预测

```powershell
py -3 daily_job.py --date 2026-06-09
py -3 dashboard_server.py    # 仪表盘 :8765
py -3 agent_server.py        # 对话体 :8766
```

### 东财回测（可信窗 2026-03-02 ~ 2026-04-30）

```powershell
$env:ETF_AGENT_STRICT_DATA="1"
$env:ETF_AGENT_ALLOW_NETWORK="0"

# 规则版
py -3 run_news_backtest.py --start 2026-03-02 --end 2026-04-30 --sources all --tag rule

# 大模型版（须先预热缓存）
py -3 warm_llm_decider_cache.py --start 2026-03-02 --end 2026-04-30
py -3 run_news_backtest.py --start 2026-03-02 --end 2026-04-30 --sources all --tag llm --use-llm --llm-cache-only
py -3 check_llm_pipeline.py --backtest-window
```

### CSDN 长窗回测（权重方向，≠ 实盘收益）

```powershell
# 需上级目录 骏/ 数据；或已生成 data/csdn_scores/csdn_daily_scores.json
py -3 csdn_backtest.py
py -3 run_backtest_suite.py
```

---

## 6. 三轨回测（必读）

| 轨道 | 数据 | 用途 |
|------|------|------|
| 甲轨 | 本地 K 线 | 六因子、RSI、趋势权重 |
| 乙轨 | CSDN 2020–2023 | **权重方向**（验证 +16.37%） |
| 丙轨 | 东财 43 日 | **全链路验收**（规则 +0.61%，LLM +1.70%） |

**禁止**用乙轨数字直接代表丙轨实盘表现。

---

## 7. 外部数据依赖

| 路径 | 说明 |
|------|------|
| `../骏/` | CSDN 原始 xlsx；仅乙轨需要 |
| `data/historical_news.db` | 东财历史新闻 SQLite；丙轨需要 |
| `data/llm_cache/2026-03-*` | 大模型决策缓存；`--llm-cache-only` 需要 |

---

## 8. 已归档回测结果

| 文件 | 说明 |
|------|------|
| `data/news_backtest/backtest_suite_results.json` | 三轨汇总 |
| `data/news_backtest/track_b.json` | CSDN 权重对比 |
| `data/news_backtest/news_backtest_*_rule.json` | 丙轨规则 |
| `data/news_backtest/news_backtest_*_llm.json` | 丙轨大模型 |

---

## 9. 维护脚本

| 脚本 | 用途 |
|------|------|
| `gen_manual_docx.py` | 生成 Word 说明书 |
| `build_csdn_cache.py` | 从骏数据重建 CSDN 缓存 |
| `extend_klines.py` | 扩展 K 线 CSV |
| `cleanup_project.py` | 清理临时回测产物（可重复执行） |

---

## 10. 团队

陈骏人：工程与回测 · 虞子潼：策略与文档
