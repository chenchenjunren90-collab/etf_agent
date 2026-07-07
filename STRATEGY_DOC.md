# ETF 智能体 — 每日新闻驱动预测

> 每天早上更新行情、抓取 09:30 前新闻、两级筛选（关键词 + DeepSeek 语义）量化信号，调 DeepSeek 做最终决策，叠加经济日历分级风控，输出比赛要求的 `[{symbol, symbol_name, volume}]`，并复盘上一日预测收益。

---

## 1. 项目结构

```
etf_agent/
├── daily_job.py                 # 每日流程编排（唯一入口）
├── strategy.py                  # 决策编排：LM融合 + 规则风控 + 仓位分配
├── scoring.py                   # ETF 排名、LLM 态度注入、评分闸门
├── position.py                  # 市场环境评估、仓位分配、新闻调仓
├── features.py                  # 量价特征计算（趋势、量比、RSI、MACD 等）
├── indicators.py                # 技术指标（RSI、MACD、布林带）
├── pool.py                      # 交易池定义（稳健池 + 进攻池）
│
├── news_signal.py               # Stage 1: 关键词筛选 + 主题强度评分
├── news_llm_scorer.py           # Stage 2: DeepSeek 语义评分（增强）
├── news_fetcher.py              # AkShare 多源新闻抓取
├── news_store.py                # 新闻 SQLite 存储（回测用）
├── news_time_split.py           # 按上一交易日 15:00 切分新鲜/陈旧
├── news_aux_fetcher.py          # 辅助新闻源（CCTV 等）
│
├── econ_calendar.py             # 经济日历加载（最高优先级信号）
├── llm_decider.py               # DeepSeek 决策提示词 + 调用
├── llm_client.py                # DeepSeek API 客户端
│
├── theme_signal.py              # 主题信号保存/加载
├── market_data.py               # 行情多源抓取 + 新鲜度校验
├── update_local_csv.py          # 同步 data/*.csv
├── daily_pnl.py                 # 上一日开盘→收盘收益复盘
├── daily_run_guard.py           # 同日重复运行保护
├── agent_kb.py                  # 智能体对话知识库
│
├── historical_news_builder.py   # 历史新闻信号构建（回测用）
├── run_news_backtest.py         # 历史回测入口
│
├── dashboard_server.py          # 本地前端面板（8765 端口）
├── agent_server.py              # 对话智能体（8766 端口）
│
├── etf_agent_chat.py            # 聊天逻辑
│
├── data/                        # ETF 日线 CSV、新闻信号、每日输出
│   ├── daily_news_signal/       # 每日新闻信号 JSON
│   ├── daily_output/            # 比赛格式输出 + 完整记录
│   ├── llm_cache/               # LLM 响应缓存
│   └── news_backtest/           # 回测信号与报告
├── prompts/                     # LLM 提示词模板
│   └── decider_zh.md           # 决策提示词
└── .env                         # DeepSeek API 密钥配置
```

---

## 2. 数据源

| 来源 | 用途 | 备注 |
| --- | --- | --- |
| AkShare `fund_etf_hist_em` | ETF 日线 K 线 | 主要 |
| AkShare 多源新闻接口 | 盘前财经新闻抓取（东方财富、新浪、富途等） | 约 60 小时窗口 |
| AkShare `news_economic_baidu` | 经济日历事件 | 高优先级信号 |
| SQLite 历史新闻库 | 回测用历史新闻 | 东财源 3-4 月完整，5 月起仅 CCTV |
| DeepSeek Chat API | 新闻语义评分 + 最终决策 | 需配置 `.env` 中的 `DEEPSEEK_API_KEY` |

行情 CSV 数据每日由 `update_local_etfs` 同步，网络不可用时可 `--skip-price-update` 使用本地缓存。

---

## 3. 标的池

**稳健池**（10 只，日常使用，`pool.TRADING_POOL`）：

```
510300 沪深300ETF       510050 上证50ETF        510500 中证500ETF
510330 华夏沪深300ETF    159338 中证A500ETF      518880 黄金ETF
159985 豆粕ETF          510880 红利ETF          512880 证券ETF
512010 医药ETF
```

**进攻池**（3 只，宽基 5 日涨幅 ≥ 3% 时自动启用，`pool.OFFENSIVE_POOL`）：

```
159915 创业板ETF        588000 科创50ETF        159949 创业板50ETF
```

涵盖宽基、商品避险、行业三类，提供截面差异。

---

## 4. 每日流程（交易日 09:25 启动）

```
① 复盘上一日预测收益（开盘价 → 收盘价）
     ↓
② 更新 ETF 历史行情 CSV（可 --skip-price-update 跳过）
     ↓
③ 抓取 09:30 前盘前新闻（约 60 小时财经资讯窗口）
     ↓
④ 新闻时间切割：按上一交易日 15:00 分「新鲜」和「陈旧」
     ↓
⑤ 两级新闻筛选：
   Stage 1 — 关键词规则筛选（news_signal.py）
   Stage 2 — DeepSeek 语义评分增强（news_llm_scorer.py）
     新鲜、陈旧两路独立处理，生成 fresh_theme_scores / stale_theme_scores
     ↓
⑥ 加载经济日历（econ_calendar.py，信号优先级最高）
     ↓
⑦ 数据质量检查：新闻不足或日历为空则终止/降级
     ↓
⑧ 调用 DeepSeek 做最终决策（llm_decider.py）
     输入：经济日历 → 盘后新鲜新闻 → 全量新闻摘要 → ETF 量价表
     输出：市场状态、每只 ETF 态度分、仓位建议、中文总结
     ↓
⑨ 规则层合并（strategy.py）：
   - 宽基市场环境 → 基准仓位
   - 新闻置信度 → 仓位调整
   - 大模型建议仓位取较低值（只能更保守）
   - 经济日历分级上限（1-2高影响≤85%，3-5≤75%，6+≤65%）
   - 评分闸门（最高分 < 50 → 强制空仓；LLM 强信号可降至 42）
   - 选前 1-3 只，按权重分配资金
     ↓
⑩ 输出：competition submit.json + 完整审计 full.json
```

---

## 5. 新闻筛选标准

### 5.1 关键词筛选（Stage 1 — `news_signal.py`）

单条新闻须通过三层判断：

| 层级 | 要求 | 处理 |
| --- | --- | --- |
| 实质事件 | 必须有可量化利好/利空（政策落地、补贴、降息降准、融资、订单、中标、业绩预增等） | 强信号 |
| 明确赛道 | 新闻文本必须能直接映射到 ETF 赛道 | 否则拒绝 |
| 价格位置 | 高位接盘风险或利好未确认 → 大幅降权 | 乘数打折 |

排除规则：
- 空泛趋势表达（"前景广阔""有望受益"等）无实质动作 → 弱信号
- 大涨后报道利好 → `late_news_after_rally`，乘数 0.35
- 利好但趋势下跌 → `good_news_in_downtrend`，乘数 0.45
- 有利好但无法锁定 ETF 赛道 → 拒绝

### 5.2 语义评分（Stage 2 — `news_llm_scorer.py`）

对通过关键词筛选的文章，调用 DeepSeek 进行语义理解：
- 重新评估相关性（< 0.3 直接丢弃）
- 判断情绪方向（正面/负面）
- 映射到具体 ETF 代码
- 防幻觉：仅允许候选池内的 ETF 代码，单篇文章最多影响 3 只 ETF

失败时自动降级到关键词评分，不阻塞流程。

---

## 6. 排序与打分

`scoring.rank_etfs_short_race` 对每只 ETF 计算：

```
新鲜主题分 = 截断(50 + 新鲜原始分 × 50, 0, 100)    ← 经量价确认可打折
陈旧主题分 = 截断(50 + 陈旧原始分 × 50, 0, 100)
历史量价分 = 六因子技术分

综合评分 = 新鲜主题分 × 25%
         + 陈旧主题分 × 10%
         + 趋势分       × 30%
         + 历史量价分   × 20%
         − 风险扣分     × 15%
         − 轮动惩罚
```

| 权重 | 含义 | 来源 |
|------|------|------|
| 25% | 新鲜主题分 | 盘后新闻（关键词 + 大模型语义） |
| 10% | 陈旧主题分 | 盘中及更早新闻 |
| 30% | 短期趋势分 | K 线量价特征 |
| 20% | 历史量价分 | 六因子技术分 |
| −15% | 风险扣分 | 过热、波动异常 |
| −(可变) | 轮动惩罚 | 连续持有同一 ETF |

### 大模型态度注入（`_inject_llm_views_into_signals`）

DeepSeek 对每只 ETF 输出的态度分（−1 ~ +1）会**覆盖**该 ETF 的 `fresh_theme_scores`，然后用同一套公式重新排名。大模型不直接点名买哪只，只改变「主题」这一项输入。

---

## 7. 市场环境与仓位控制

### 7.1 市场环境评估（`evaluate_market_regime`）

看三只代表性宽基 ETF（510300 沪深300 / 159915 创业板 / 588000 科创50）的 5 日 + 3 日综合涨跌：

| 宽基均值 `ret_5d + 0.5×ret_3d` | 基准仓位 |
| --- | --- |
| ≤ −5.0% | 0%（空仓） |
| ≤ −3.0% | 15% |
| ≤ −1.0% | 40% |
| ≥ +3.0% | 90% |
| ≥ +1.0% | 85% |
| 中性 | 70% |

### 7.2 新闻调仓（`adjust_invest_ratio_by_news`）

根据 `auto_news` 汇总统计微调：无新闻 ×0.55、低置信 ×0.62、无主线 ×0.65、无催化 ×0.62；强情绪 ±8%∼±15%。

### 7.3 大模型建议（只能收紧）

大模型的 `position_ratio_hint` 与规则仓位**取较低者**，大模型只能让仓位更保守。

### 7.4 经济日历硬顶

高重要度经济事件日按条数分级限制总仓位：1-2 条 ≤ 85%、3-5 条 ≤ 75%、6+ 条 ≤ 65%。

### 7.5 评分闸门

排名第一的 ETF 综合评分 < 45 分 → **强制空仓**。LLM 对至少一只 ETF 给出 |score| ≥ 0.5 时，闸门可动态降至 42 分（`SCORE_GATE_MODE=dynamic`）。

---

## 8. 持仓分配

### 持仓数（`short_race_max_positions`）

| 新闻状态 | 持仓数 |
| --- | --- |
| 信号强（confidence ≥ 0.26 且 max_abs ≥ 0.17） | 3 只 |
| 信号中（confidence ≥ 0.17 且 max_abs ≥ 0.095） | 2 只 |
| 信号弱/无新闻 | 1 只 |

### 权重模板

```
BASE_WEIGHTS = [0.45, 0.30, 0.25]
```

分数差距 ≥ 8 分时，第一名多分 8 个百分点。**单只 ETF 硬限不超过总资金 30%。**

股数 = 总资金 × 仓位比例 × 权重 ÷ 价格，向下取 100 股整数倍。

---

## 9. P&L 结算

```
open  = 当日开盘价
close = 当日收盘价
pnl_per_pick = volume × (close - open)
day_pnl = Σ pnl_per_pick
```

按比赛规则：开盘买入、收盘卖出、日终清仓。

---

## 10. 运行方式

### 每日预测

```bat
start_auto.bat
```

或命令行：

```bash
py -3 daily_job.py --date 2026-06-04 --capital 500000 --cutoff 09:30
```

- `--date` 默认当天日期
- `--capital` 默认 500000
- `--cutoff` 默认 09:30（新闻截止时间）
- `--skip-price-update` 跳过行情同步（使用本地缓存）
- `--force` 覆盖已有预测

### 历史回测

```bash
py -3 run_news_backtest.py --start 2026-03-01 --end 2026-04-30 --use-llm
```

### 仪表盘

```bat
start_dashboard.bat
```

浏览器 → `http://127.0.0.1:8765`

### 对话智能体

```bat
start_agent.bat
```

浏览器 → `http://127.0.0.1:8766`

---

## 11. 输出文件

- `data/{code}.csv` — ETF 日线 K 线
- `data/daily_news_signal/YYYY-MM-DD.json` — 当日新闻筛选结果
- `data/daily_output/YYYY-MM-DD_submit.json` — 比赛格式输出（`[{symbol, symbol_name, volume}]`）
- `data/daily_output/YYYY-MM-DD_full.json` — 完整审计记录（含新闻、LLM 原文、各分项得分、触发规则）
- `data/daily_output/YYYY-MM-DD_error.json` — 数据质量不达标时的错误报告
- `data/daily_output/last_pnl_report.txt` — 上一日收益摘要

---

## 12. 关键参数表

| 参数 | 位置 | 默认值 |
| --- | --- | --- |
| 资金 | CLI `--capital` | 500,000 CNY |
| `SCORE_GATE` | `scoring.py` | 45（动态可降至 42） |
| `MAX_SINGLE_WEIGHT` | `scoring.py` | 0.30 |
| `RACE_MAX_POSITIONS` | `scoring.py` | 3 |
| `RACE_BASE_WEIGHTS` | `scoring.py` | [0.45, 0.30, 0.25] |
| `ECON_TIER1_CAP` | `scoring.py` | 0.85（1-2 条高影响） |
| `ECON_TIER2_CAP` | `scoring.py` | 0.75（3-5 条高影响） |
| `ECON_TIER3_CAP` | `scoring.py` | 0.65（6+ 条高影响） |
| `ROTATION_PENALTY_RATE` | `scoring.py` | 2.0 |
| `ROTATION_PENALTY_CAP` | `scoring.py` | 8.0 |
| `STRONG_NEWS_THRESHOLD` | `news_signal.py` | 0.35 |
| `WEAK_NEWS_THRESHOLD` | `news_signal.py` | 0.12 |
| SCORE_GATE_MODE | 环境变量 | dynamic |
| FORCE_POSITION_CAP | 环境变量 | 空（数据质量降级时 set） |

---

## 13. 注意事项

- **大模型可选**：未配置 `DEEPSEEK_API_KEY` 时自动退回纯规则版，流程不中断。
- **历史量价分**仅由六因子技术分构成（无 LSTM 模块）。
- **不依赖 Tushare**：完全基于 AkShare 免费数据源。
- **回测与实盘新闻链路一致**：两阶段（关键词 + LLM 语义）均参与，结果可比。
- **决策严格使用 09:30 前可见数据**，禁止当日 K 线和未来信息。
- **同日期重复运行**：`--force` 可覆盖；不加强制时跳过已有预测。
- **周末不生成预测**，仅复盘上一交易日。
