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

十日目标控制默认为 `monitor`，只使用前瞻波动预算降低仓位，不会为了追赶目标增加风险。设置 `ETF_TEN_DAY_GOAL_MODE=fixed` 后，系统才会在固定十日窗口内启用止盈保护和回撤防守；该模式应先经过独立样本外验证。
