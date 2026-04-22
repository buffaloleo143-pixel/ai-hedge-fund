这19个Agent是项目中的**分析师层**，每个都独立给出看多/看空/中性信号，最终由 Portfolio Manager 综合决策。具体如下：

### 投资大师Agent（12个）

| # | Agent | 源码文件 | 投资风格 |
|---|-------|---------|---------|
| 1 | Bill Ackman | [bill_ackman.py](file:///e:/qa/ai-hedge-fund/src/agents/bill_ackman.py) | 激进价值投资 |
| 2 | Cathie Wood | [cathie_wood.py](file:///e:/qa/ai-hedge-fund/src/agents/cathie_wood.py) | 颠覆性创新成长 |
| 3 | Warren Buffett | [warren_buffett.py](file:///e:/qa/ai-hedge-fund/src/agents/warren_buffett.py) | 护城河价值投资 |
| 4 | Charlie Munger | [charlie_munger.py](file:///e:/qa/ai-hedge-fund/src/agents/charlie_munger.py) | 合理价格买伟大企业 |
| 5 | Ben Graham | [ben_graham.py](file:///e:/qa/ai-hedge-fund/src/agents/ben_graham.py) | 古典安全边际价值 |
| 6 | Michael Burry | [michael_burry.py](file:///e:/qa/ai-hedge-fund/src/agents/michael_burry.py) | 深度逆向价值 |
| 7 | Mohnish Pabrai | [mohnish_pabrai.py](file:///e:/qa/ai-hedge-fund/src/agents/mohnish_pabrai.py) | 防守型低风险价值 |
| 8 | Stanley Druckenmiller | [stanley_druckenmiller.py](file:///e:/qa/ai-hedge-fund/src/agents/stanley_druckenmiller.py) | 宏观动量 |
| 9 | Nassim Taleb | [nassim_taleb.py](file:///e:/qa/ai-hedge-fund/src/agents/nassim_taleb.py) | 反脆弱/尾部风险 |
| 10 | Peter Lynch | [peter_lynch.py](file:///e:/qa/ai-hedge-fund/src/agents/peter_lynch.py) | PEG成长价值 |
| 11 | Phil Fisher | [phil_fisher.py](file:///e:/qa/ai-hedge-fund/src/agents/phil_fisher.py) | 质量成长投资 |
| 12 | Rakesh Jhunjhunwala | [rakesh_jhunjhunwala.py](file:///e:/qa/ai-hedge-fund/src/agents/rakesh_jhunjhunwala.py) | 印度巴菲特/新兴市场价值 |

### 量化分析Agent（5个）

| # | Agent | 源码文件 | 分析维度 |
|---|-------|---------|---------|
| 13 | Technical Analyst | [technicals.py](file:///e:/qa/ai-hedge-fund/src/agents/technicals.py) | 技术面（EMA/RSI/MACD/布林带） |
| 14 | Fundamentals Analyst | [fundamentals.py](file:///e:/qa/ai-hedge-fund/src/agents/fundamentals.py) | 基本面财务指标 |
| 15 | Valuation Analyst | [valuation.py](file:///e:/qa/ai-hedge-fund/src/agents/valuation.py) | DCF/可比估值 |
| 16 | Growth Analyst | [growth_agent.py](file:///e:/qa/ai-hedge-fund/src/agents/growth_agent.py) | 营收/利润增长趋势 |
| 17 | Aswath Damodaran | [aswath_damodaran.py](file:///e:/qa/ai-hedge-fund/src/agents/aswath_damodaran.py) | 学术估值框架 |

### 情绪分析Agent（2个）

| # | Agent | 源码文件 | 分析维度 |
|---|-------|---------|---------|
| 18 | Sentiment Analyst | [sentiment.py](file:///e:/qa/ai-hedge-fund/src/agents/sentiment.py) | 内部人交易情绪 |
| 19 | News Sentiment | [news_sentiment.py](file:///e:/qa/ai-hedge-fund/src/agents/news_sentiment.py) | 新闻舆情 |

### 决策层（不计入19个分析师）

| Agent | 源码文件 | 角色 |
|-------|---------|------|
| Risk Manager | [risk_manager.py](file:///e:/qa/ai-hedge-fund/src/agents/risk_manager.py) | 波动率/相关性风险约束 |
| Portfolio Manager | [portfolio_manager.py](file:///e:/qa/ai-hedge-fund/src/agents/portfolio_manager.py) | 综合19个信号做最终交易决策 |

所以整体架构是：**19个分析师独立评分 → Risk Manager 设定风险约束 → Portfolio Manager 综合决策**。