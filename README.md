# ETF 投资智能体

面向普通用户与 ETF 投资比赛的智能交互系统。用户可以通过对话补充资金规模、风险偏好和投资期限，获取当日 ETF 配置建议，并继续追问持仓理由、相关新闻及新闻对 ETF 市场的潜在影响。

## 在线体验

- 智能体交互前端：<http://chinaglass.shop/etf-agent/chat/>
- 比赛建议看板：<http://chinaglass.shop/etf-agent/>
- 当日比赛格式接口：<http://chinaglass.shop/etf-agent/api/today_advice>

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