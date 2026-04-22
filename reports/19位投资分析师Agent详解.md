# AI对冲基金 19位投资分析师Agent详解

## 概览

本系统包含19个独立的分析师Agent，每个Agent基于不同的投资哲学或分析维度独立给出bullish/bearish/neutral信号，最终由Risk Manager设定风险约束、Portfolio Manager综合决策。

| 层级 | Agent数 | 信号流向 |
|------|---------|---------|
| 一、投资大师Agent | 12 | 独立评分 → Portfolio Manager |
| 二、量化分析Agent | 5 | 规则化计算 → Portfolio Manager |
| 三、情绪分析Agent | 2 | 情绪/新闻 → Portfolio Manager |
| 四、决策层 | 2 | Risk Manager → Portfolio Manager → 交易指令 |

整体架构：**19个分析师独立评分 → Risk Manager 设定风险约束 → Portfolio Manager 综合决策**

---

## 一、投资大师Agent

### 1.1 Warren Buffett（沃伦·巴菲特）

**源码文件**：[warren_buffett.py](file:///e:/qa/ai-hedge-fund/src/agents/warren_buffett.py)

**投资哲学**：巴菲特以"护城河"价值投资著称，只投资业务简单易懂、具有持久竞争优势的公司。他重视所有者盈余（Owner Earnings）而非会计利润，要求显著的安全边际，偏好低杠杆、高ROE、稳定增长的企业，并高度关注管理层是否股东友好（回购+分红）。

**评分子模块函数名**：
- `analyze_fundamentals(metrics)` — 基本面分析（ROE、D/E、营业利润率、流动比率），满分7分
- `analyze_consistency(financial_line_items)` — 盈利一致性分析，满分3分
- `analyze_moat(metrics)` — 竞争护城河分析（ROE一致性、利润率稳定性、资产效率、竞争地位），满分5分
- `analyze_pricing_power(financial_line_items, metrics)` — 定价权分析（毛利率趋势+毛利率水平），满分5分
- `analyze_book_value_growth(financial_line_items)` — 每股账面价值增长分析，满分5分
- `analyze_management_quality(financial_line_items)` — 管理层质量（回购+分红），满分2分
- `calculate_owner_earnings(financial_line_items)` — 巴菲特"所有者盈余"计算
- `calculate_intrinsic_value(financial_line_items)` — 三阶段DCF内在价值（含维护资本支出估算）

**评分维度与阈值表格**：

| 子模块 | 评分维度 | 阈值/规则 | 得分 |
|--------|---------|----------|------|
| 基本面（`analyze_fundamentals`） | ROE | > 15% | +2 |
| | D/E | < 0.5 | +2 |
| | 营业利润率 | > 15% | +2 |
| | 流动比率 | > 1.5 | +1 |
| 盈利一致性（`analyze_consistency`） | 净利润持续增长 | 每期>下一期（4期+） | +3 |
| 护城河（`analyze_moat`） | ROE一致性 | ≥80%期间ROE>15% | +2；≥60% +1 |
| | 营业利润率稳定性 | 均值>20%且近期≥早期 | +1 |
| | 资产周转效率 | 任一期>1.0 | +1 |
| | 业绩稳定性（变异系数） | 综合稳定性>0.7 | +1 |
| 定价权（`analyze_pricing_power`） | 毛利率改善 | 近期>早期+2% | +3；微升 +2；稳定±1% +1 |
| | 毛利率水平 | 均值>50% | +2；>30% +1 |
| 账面价值增长（`analyze_book_value_growth`） | 增长一致性 | ≥80%期间增长 | +3；≥60% +2；≥40% +1 |
| | CAGR | > 15% | +2；> 10% +1 |
| 管理层质量（`analyze_management_quality`） | 回购 | 股权发行/回购 < 0 | +1 |
| | 分红 | 分红现金流出 < 0 | +1 |

**内在价值模型**（`calculate_intrinsic_value`）：
- 所有者盈余 = 净利润 + 折旧 - 维护资本支出 - 营运资金变动
- 维护资本支出估算：取85%总CapEx、100%折旧、历史CapEx/收入比×收入三者的中位数
- 三阶段DCF：阶段1（5年，增长率上限8%）、阶段2（5年，阶段1的50%上限4%）、终值（2.5% GDP增速）
- 折现率：10%
- 额外15%保守折价

**信号判定规则**：
- 由 `generate_buffett_output()` 调用LLM，根据安全边际和业务质量综合判断
- Bullish：强业务 + 安全边际>0
- Bearish：差业务 或 明显高估
- Neutral：好业务但安全边际≤0，或信号混合
- 置信度区间：90-100% 卓越+低价；70-89% 好护城河+合理价；50-69% 混合；30-49% 圈外/基本面差；10-29% 差业务/高估

---

### 1.2 Charlie Munger（查理·芒格）

**源码文件**：[charlie_munger.py](file:///e:/qa/ai-hedge-fund/src/agents/charlie_munger.py)

**投资哲学**：芒格主张"以合理价格买入伟大企业"，重视护城河强度、业务可预测性和管理层质量。他使用多学科"心智模型"（Mental Models），偏好低资本开支、高ROIC的轻资产企业，对管理层"赤裸上阵"（skin in the game）和资本配置能力要求极高。

**评分子模块函数名**：
- `analyze_moat_strength(metrics, financial_line_items)` — 护城河强度分析（ROIC+定价权+资本密集度+无形资产），满分10分
- `analyze_management_quality(financial_line_items, insider_trades)` — 管理层质量（FCF/NI+D/E+现金/收入+内部人+股份趋势），满分10分
- `analyze_predictability(financial_line_items)` — 业务可预测性（营收稳定性+营业利润一致性+利润率波动+FCF可靠性），满分10分
- `calculate_munger_valuation(financial_line_items, market_cap)` — 芒格风格估值（FCF收益率+安全边际+FCF趋势），满分10分

**评分维度与阈值表格**：

| 子模块 | 权重 | 评分维度 | 阈值/规则 | 得分 |
|--------|------|---------|----------|------|
| 护城河（`analyze_moat_strength`） | 35% | ROIC一致性 | ≥80%期间>15% +3；≥50% +2；>0 +1 | 原始9→映射0-10 |
| | | 定价权（毛利率趋势） | ≥70%期间改善 +2；均值>30% +1 | |
| | | 资本密集度 | CapEx/收入<5% +2；<10% +1 | |
| | | 无形资产 | 有R&D投入 +1；有商誉/无形资产 +1 | |
| 管理层（`analyze_management_quality`） | 25% | FCF/净利润比率 | >1.1 +3；>0.9 +2；>0.7 +1 | 原始12→映射0-10 |
| | | D/E | <0.3 +3；<0.7 +2；<1.5 +1 | |
| | | 现金/收入 | 10%~25% +2；5%~10%或25%~40% +1 | |
| | | 内部人买入比率 | >70% +2；>40% +1；<10%且卖出>5次 -1 | |
| | | 股份数趋势 | 减少>5% +2；稳定±5% +1；增加>20% -1 | |
| 可预测性（`analyze_predictability`） | 25% | 营收稳定性 | 均增长>5%且波动<10% +3；正增长且波动<20% +2；仅正增长 +1 | 满分10 |
| | | 营业利润一致性 | 全部期间为正 +3；≥80% +2；≥60% +1 | |
| | | 利润率波动 | 波动<3% +2；<7% +1 | |
| | | FCF可靠性 | 全部期间正FCF +2；≥80% +1 | |
| 估值（`calculate_munger_valuation`） | 15% | FCF收益率 | >8% +4；>5% +3；>3% +1 | 满分10 |
| | | 安全边际vs合理价值 | >30% +3；>10% +2；>-10% +1 | |
| | | FCF趋势 | 近3年均>远3年均×1.2 +3；近>远 +2 | |

**信号判定规则**：
- 加权总分 = `护城河 × 0.35 + 管理层 × 0.25 + 可预测性 × 0.25 + 估值 × 0.15`，满分10分
- 总分 ≥ 7.5 → **bullish**
- 总分 ≤ 5.5 → **bearish**
- 其余 → **neutral**
- 置信度由 `compute_confidence()` 计算：质量主导（85%权重）+ 估值调整（15%权重）+ MOS修正±10pp，并按信号类型分桶
- 最终由 `generate_munger_output()` 调用LLM以芒格"心智模型"风格生成推理

---

### 1.3 Ben Graham（本杰明·格雷厄姆）

**源码文件**：[ben_graham.py](file:///e:/qa/ai-hedge-fund/src/agents/ben_graham.py)

**投资哲学**：格雷厄姆是"价值投资之父"，强调安全边际和保守分析。他要求公司有多年的盈利稳定性、强健的资产负债表（流动比率≥2、低负债），并以Graham Number和Net-Net（净流动资产价值）法评估内在价值，只在价格显著低于内在价值时买入。

**评分子模块函数名**：
- `analyze_earnings_stability(metrics, financial_line_items)` — 盈利稳定性分析（EPS正数年份+EPS增长），满分4分
- `analyze_financial_strength(financial_line_items)` — 财务强健度（流动比率+负债率+分红记录），满分5分
- `analyze_valuation_graham(financial_line_items, market_cap)` — 格雷厄姆估值（Net-Net+Graham Number+安全边际），满分7分

**评分维度与阈值表格**：

| 子模块 | 评分维度 | 阈值/规则 | 得分 |
|--------|---------|----------|------|
| 盈利稳定性（`analyze_earnings_stability`） | EPS正数年份 | 全部期间为正 +3；≥80%期间 +2 | — |
| | EPS增长 | 最新期>最早期 +1 | — |
| 财务强健度（`analyze_financial_strength`） | 流动比率 | ≥2.0 +2；≥1.5 +1 | — |
| | 负债比率（总负债/总资产） | <0.5 +2；<0.8 +1 | — |
| | 分红记录 | 多数年份有分红 +1 | — |
| 格雷厄姆估值（`analyze_valuation_graham`） | Net-Net（NCAV=流动资产-总负债） | NCAV>市值 +4；NCAV/股≥股价2/3 +2 | — |
| | Graham Number | √(22.5×EPS×BVPS) | — |
| | 安全边际vs Graham Number | >50% +3；>20% +1 | — |

**信号判定规则**：
- 总分 = 三子模块直接求和，满分16分（代码中max_possible_score=15，取三者之和）
- 总分 ≥ 0.7×满分 → **bullish**
- 总分 ≤ 0.3×满分 → **bearish**
- 其余 → **neutral**
- 最终由 `generate_graham_output()` 调用LLM以格雷厄姆"保守、分析性"风格生成推理

---

### 1.4 Bill Ackman（比尔·阿克曼）

**源码文件**：[bill_ackman.py](file:///e:/qa/ai-hedge-fund/src/agents/bill_ackman.py)

**投资哲学**：阿克曼是激进价值投资者，专注于少数高确信投资。他寻找具有持久竞争优势（品牌/护城河）的优质企业，重视自由现金流和营业利润率，关注资本纪律（杠杆合理、股东回报），并善于通过激进主义（activism）在运营改善不足的公司中推动变革以释放价值。

**评分子模块函数名**：
- `analyze_business_quality(metrics, financial_line_items)` — 业务质量（营收增长+营业利润率+FCF+ROE），满分7分
- `analyze_financial_discipline(metrics, financial_line_items)` — 财务纪律（D/E+分红+回购），满分4分
- `analyze_activism_potential(financial_line_items)` — 激进主义潜力（营收增长但利润率低），满分2分
- `analyze_valuation(financial_line_items, market_cap)` — FCF DCF估值+安全边际，满分7分

**评分维度与阈值表格**：

| 子模块 | 评分维度 | 阈值/规则 | 得分 |
|--------|---------|----------|------|
| 业务质量（`analyze_business_quality`） | 营收累计增长 | >50% +2；正增长 +1 | — |
| | 营业利润率 | >15%的期间过半 +2 | — |
| | FCF | 正FCF期间过半 +1 | — |
| | ROE | >15% +2 | — |
| 财务纪律（`analyze_financial_discipline`） | D/E | <1.0期间过半 +2；负债/资产<50%期间过半 +2 | — |
| | 分红 | 多数年份有分红 +1 | — |
| | 股份减少 | 最新<最早 +1 | — |
| 激进主义潜力（`analyze_activism_potential`） | 营收增长+低利润率 | 增长>15%且平均利润率<10% +2 | — |
| 估值（`analyze_valuation`） | FCF DCF安全边际 | >30% +3；>10% +1 | — |

**DCF估值参数**（`analyze_valuation`）：
- 增长率：6%，折现率：10%，终值倍数：15×，预测期：5年
- 安全边际 = (内在价值 - 市值) / 市值

**信号判定规则**：
- 总分 = 四子模块直接求和，满分20分
- 总分 ≥ 0.7×20=14 → **bullish**
- 总分 ≤ 0.3×20=6 → **bearish**
- 其余 → **neutral**
- 最终由 `generate_ackman_output()` 调用LLM以阿克曼"自信、分析性、有时对抗性"风格生成推理

---

### 1.5 Cathie Wood（凯瑟琳·伍德）

**源码文件**：[cathie_wood.py](file:///e:/qa/ai-hedge-fund/src/agents/cathie_wood.py)

**投资哲学**：伍德专注于颠覆性创新投资，寻找AI、机器人、基因测序、金融科技和区块链等领域的突破性公司。她看重营收加速增长、R&D投入强度、毛利率扩张和经营杠杆，愿意承受短期波动以换取长期指数级回报，使用高增长假设的DCF模型。

**评分子模块函数名**：
- `analyze_disruptive_potential(metrics, financial_line_items)` — 颠覆性潜力（营收加速+毛利率+经营杠杆+R&D强度），原始满分12→标准化5分
- `analyze_innovation_growth(metrics, financial_line_items)` — 创新驱动增长（R&D趋势+FCF+经营效率+CapEx+再投资倾向），原始满分15→标准化5分
- `analyze_cathie_wood_valuation(financial_line_items, market_cap)` — 高增长DCF估值+安全边际，满分5分

**评分维度与阈值表格**：

| 子模块 | 评分维度 | 阈值/规则 | 得分 |
|--------|---------|----------|------|
| 颠覆性潜力（`analyze_disruptive_potential`） | 营收增长加速 | 最新增速>最早增速 +2 | — |
| | 营收绝对增速 | >100% +3；>50% +2；>20% +1 | — |
| | 毛利率扩张 | 近期>早期+5% +2；微升 +1 | — |
| | 毛利率水平 | >50% +2 | — |
| | 经营杠杆 | 营收增速>费用增速 +2 | — |
| | R&D强度 | >15% +3；>8% +2；>5% +1 | — |
| 创新增长（`analyze_innovation_growth`） | R&D增长 | >50% +3；>20% +2 | — |
| | R&D强度趋势 | 递增 +2 | — |
| | FCF增长+一致性 | 增长>30%且全正 +3；≥75%正 +2；>50%正 +1 | — |
| | 营业利润率 | >15%且改善 +3；>10% +2；仅改善 +1 | — |
| | CapEx增长 | 强度>10%且增长>20% +2；强度>5% +1 | — |
| | 再投资倾向 | 分红/FCF<20% +2；<40% +1 | — |
| 估值（`analyze_cathie_wood_valuation`） | 高增长DCF安全边际 | >50% +3；>20% +1 | — |

**高增长DCF参数**（`analyze_cathie_wood_valuation`）：
- 增长率：20%，折现率：15%，终值倍数：25×，预测期：5年

**信号判定规则**：
- 总分 = 颠覆性（5）+ 创新增长（5）+ 估值（5）= 满分15分
- 总分 ≥ 0.7×15=10.5 → **bullish**
- 总分 ≤ 0.3×15=4.5 → **bearish**
- 其余 → **neutral**
- 最终由 `generate_cathie_wood_output()` 调用LLM以伍德"乐观、前瞻、信念驱动"风格生成推理

---

### 1.6 Michael Burry（迈克尔·布瑞）

**源码文件**：[michael_burry.py](file:///e:/qa/ai-hedge-fund/src/agents/michael_burry.py)

**投资哲学**：布瑞是深度价值逆向投资者，以发现市场误判著称。他聚焦于硬数字（自由现金流收益率、EV/EBIT），优先关注下行保护（避开高杠杆），寻找硬催化剂（内部人买入、回购、资产出售），并将负面新闻舆论视为逆向机会——"被憎恨的公司只要基本面扎实就是朋友"。

**评分子模块函数名**：
- `_analyze_value(metrics, line_items, market_cap)` — 深度价值分析（FCF收益率+EV/EBIT），满分6分
- `_analyze_balance_sheet(metrics, line_items)` — 资产负债表分析（D/E+净现金），满分3分
- `_analyze_insider_activity(insider_trades)` — 内部人活动分析（净买入），满分2分
- `_analyze_contrarian_sentiment(news)` — 逆向情绪分析（负面新闻=机会），满分1分

**评分维度与阈值表格**：

| 子模块 | 评分维度 | 阈值/规则 | 得分 |
|--------|---------|----------|------|
| 价值（`_analyze_value`） | FCF收益率 | ≥15% +4；≥12% +3；≥8% +2 | — |
| | EV/EBIT | <6 +2；<10 +1 | — |
| 资产负债表（`_analyze_balance_sheet`） | D/E | <0.5 +2；<1.0 +1 | — |
| | 净现金 | 现金>总债务 +1 | — |
| 内部人活动（`_analyze_insider_activity`） | 净买入 | 净买入且买入/卖出>1 +2；净买入 +1 | — |
| 逆向情绪（`_analyze_contrarian_sentiment`） | 负面新闻数 | ≥5条负面 +1（逆向机会） | — |

**信号判定规则**：
- 总分 = 四子模块直接求和，满分12分
- 总分 ≥ 0.7×12=8.4 → **bullish**
- 总分 ≤ 0.3×12=3.6 → **bearish**
- 其余 → **neutral**
- 最终由 `_generate_burry_output()` 调用LLM以布瑞"简短、数据驱动、直接"风格生成推理

---

### 1.7 Mohnish Pabrai（莫尼什·帕伯莱）

**源码文件**：[mohnish_pabrai.py](file:///e:/qa/ai-hedge-fund/src/agents/mohnish_pabrai.py)

**投资哲学**：帕伯莱是"克隆大师"，以"正面我赢，反面我也输不多"（Heads I win, tails I don't lose much）著称。他只投资极度简单、业务易懂的公司，以高自由现金流收益率和低杠杆为首要条件，追求在2-3年内低风险翻倍资本。核心理念是先保护本金，再寻求收益。

**评分子模块函数名**：
- `analyze_downside_protection(financial_line_items)` — 下行保护分析（资产负债表强健度）
- `analyze_pabrai_valuation(financial_line_items, market_cap)` — FCF收益率与轻资产偏好评估
- `analyze_double_potential(financial_line_items, market_cap)` — 2-3年翻倍潜力评估

**评分维度与阈值表格**：

| 子模块 | 权重 | 评分维度 | 阈值/规则 | 得分 |
|--------|------|---------|----------|------|
| 下行保护（`analyze_downside_protection`） | 45% | 净现金头寸 | 现金 > 债务（净现金为正） | +3 |
| | | 流动比率 | ≥ 2.0 强流动性 | +2 |
| | | | 1.2~2.0 尚可流动性 | +1 |
| | | 债务/股权比（D/E） | < 0.3 极低杠杆 | +2 |
| | | | 0.3~0.7 适中杠杆 | +1 |
| | | FCF稳定性 | 近3年均值 > 0 且改善/稳定 | +2 |
| | | | 近3年均值 > 0 但下降 | +1 |
| FCF估值（`analyze_pabrai_valuation`） | 35% | FCF收益率（FCF/市值） | > 10% 极度低估 | +4 |
| | | | 7%~10% 吸引 | +3 |
| | | | 5%~7% 合理 | +2 |
| | | | 3%~5% 边界 | +1 |
| | | 资本支出/收入（轻资产程度） | < 5% 轻资产 | +2 |
| | | | 5%~10% 中等资本支出 | +1 |
| 翻倍潜力（`analyze_double_potential`） | 20% | 营收增长趋势（近3/全期均值） | > 15% 强劲 | +2 |
| | | | 5%~15% 温和 | +1 |
| | | FCF增长趋势 | > 20% 强劲 | +3 |
| | | | 8%~20% 健康 | +2 |
| | | | 0%~8% 正增长 | +1 |
| | | FCF收益率（高收益自身驱动翻倍） | > 8% | +3 |
| | | | 5%~8% | +1 |

**信号判定规则**：
- 加权总分 = `下行保护分 × 0.45 + 估值分 × 0.35 + 翻倍潜力分 × 0.20`，满分10分
- 总分 ≥ 7.5 → **bullish**
- 总分 ≤ 4.0 → **bearish**
- 其余 → **neutral**
- 最终由 `generate_pabrai_output()` 调用LLM以帕伯莱"清单驱动"风格生成叙述性推理

---

### 1.8 Stanley Druckenmiller（斯坦利·德鲁肯米勒）

**源码文件**：[stanley_druckenmiller.py](file:///e:/qa/ai-hedge-fund/src/agents/stanley_druckenmiller.py)

**投资哲学**：德鲁肯米勒是顶级宏观投资者，专注于寻找"不对称风险回报"机会：大幅上涨潜力、有限下行风险。他高度重视增长动量和价格动量，愿意为真正的成长领导者支付溢价，同时严格控制资本损失风险，当投资逻辑改变时快速止损。

**评分子模块函数名**：
- `analyze_growth_and_momentum(financial_line_items, prices)` — 增长与价格动量分析
- `analyze_sentiment(news_items)` — 新闻情绪分析
- `analyze_insider_activity(insider_trades)` — 内部人交易活动分析
- `analyze_risk_reward(financial_line_items, prices)` — 风险回报分析
- `analyze_druckenmiller_valuation(financial_line_items, market_cap)` — 德鲁肯米勒风格估值

**评分维度与阈值表格**：

| 子模块 | 权重 | 评分维度 | 阈值/规则 | 得分（原始/9→映射0-10） |
|--------|------|---------|----------|----------------------|
| 增长与动量（`analyze_growth_and_momentum`） | 35% | 营收年化CAGR | > 8% 强劲 | +3 |
| | | | 4%~8% 温和 | +2 |
| | | | 1%~4% 微弱 | +1 |
| | | EPS年化CAGR | > 8% 强劲 | +3 |
| | | | 4%~8% 温和 | +2 |
| | | | 1%~4% 微弱 | +1 |
| | | 价格动量（期间涨幅） | > 50% 极强 | +3 |
| | | | 20%~50% 中等 | +2 |
| | | | 0%~20% 轻微正向 | +1 |
| 风险回报（`analyze_risk_reward`） | 20% | D/E（债务/股权） | < 0.3 低杠杆 | +3 |
| | | | 0.3~0.7 中等 | +2 |
| | | | 0.7~1.5 偏高 | +1 |
| | | 日收益率标准差（价格波动率） | < 1% 低波动 | +3 |
| | | | 1%~2% 中等波动 | +2 |
| | | | 2%~4% 高波动 | +1 |
| 估值（`analyze_druckenmiller_valuation`） | 20% | P/E | < 15 | +2；15~25 → +1 |
| | | P/FCF | < 15 | +2；15~25 → +1 |
| | | EV/EBIT | < 15 | +2；15~25 → +1 |
| | | EV/EBITDA | < 10 | +2；10~18 → +1 |
| 情绪（`analyze_sentiment`） | 15% | 负面新闻比例 | > 30% → 3分；> 0 → 6分；= 0 → 8分（固定值） | — |
| 内部人活动（`analyze_insider_activity`） | 10% | 买入比率（买入数/总交易数） | > 70% → 8分；40%~70% → 6分；< 40% → 4分 | — |

**信号判定规则**：
- 加权总分 = `增长动量 × 0.35 + 风险回报 × 0.20 + 估值 × 0.20 + 情绪 × 0.15 + 内部人 × 0.10`，满分10分
- 总分 ≥ 7.5 → **bullish**
- 总分 ≤ 4.5 → **bearish**
- 其余 → **neutral**
- 最终由 `generate_druckenmiller_output()` 调用LLM以德鲁肯米勒果断、动量导向风格生成推理

---

### 1.9 Nassim Taleb（纳西姆·塔勒布）

**源码文件**：[nassim_taleb.py](file:///e:/qa/ai-hedge-fund/src/agents/nassim_taleb.py)

**投资哲学**：塔勒布以"反脆弱"（Antifragile）、"黑天鹅"理论和"正向凸性"（Convexity）著称。他的投资体系核心是：避开脆弱公司（高杠杆、薄利润、收益波动大），寻找能从混乱中受益的反脆弱企业（净现金、稳定利润率、R&D期权性）。同时高度关注尾部风险、波动率机制和内部人"赤裸上阵"程度。

**评分子模块函数名**：
- `analyze_tail_risk(prices_df)` — 尾部风险分析（肥尾、偏度、尾比、最大回撤），满分8分
- `analyze_antifragility(metrics, line_items, market_cap)` — 反脆弱性分析（净现金、低杠杆、利润率稳定性、FCF一致性），满分10分
- `analyze_convexity(metrics, line_items, prices_df, market_cap)` — 凸性分析（R&D期权性、上涨/下跌捕获比、现金期权性、FCF收益率），满分10分
- `analyze_fragility(metrics, line_items)` — 脆弱性分析（Via Negativa，高分代表不脆弱），满分8分
- `analyze_skin_in_game(insider_trades)` — "赤裸上阵"分析（内部人净买入），满分4分
- `analyze_volatility_regime(prices_df)` — 波动率机制分析（低波动=危险的"火鸡问题"），满分6分
- `analyze_black_swan_sentinel(news, prices_df)` — 黑天鹅哨兵（异常新闻情绪+成交量异常），满分4分

**评分维度与阈值表格**：

| 子模块（满分） | 评分维度 | 阈值/规则 | 得分 |
|--------------|---------|----------|------|
| 尾部风险（8分） | 超额峰度 | > 5 极肥尾 | +2；2~5 中等 → +1 |
| | 偏度 | > 0.5 正偏（利多长凸性） | +2；-0.5~0.5 对称 → +1 |
| | 尾比（95%分位上涨/5%分位下跌） | > 1.2 上行非对称 | +2；0.8~1.2 均衡 → +1 |
| | 最大回撤 | > -15% 韧性 | +2；-15%~-30% 中等 → +1 |
| 反脆弱性（10分） | 净现金 & 现金/市值 | 净现金且现金 > 20%市值 | +3；净现金正 → +2；净债务但可控 → +1 |
| | D/E | < 0.3 | +2；0.3~0.7 → +1 |
| | 营业利润率稳定性（变异系数CV） | CV < 0.15 且均值 > 15% | +3；CV < 0.30 且均值 > 10% → +2 |
| | FCF一致性 | 全部期间正FCF | +2；超过半数正 → +1 |
| 凸性（10分） | R&D/收入（嵌入期权性） | > 15% | +3；8%~15% → +2；3%~8% → +1 |
| | 上涨/下跌日均值比 | > 1.3 凸性回报 | +2；1.0~1.3 微正非对称 → +1 |
| | 现金/市值（现金期权性） | > 30% | +3；15%~30% → +2；5%~15% → +1 |
| | FCF收益率 | > 10% | +2；5%~10% → +1 |
| 脆弱性（8分） | D/E（负向指标） | < 0.5 低杠杆（不脆弱） | +3；0.5~1.0 中等 → +2；1.0~2.0 偏高 → +1；> 2.0 极脆弱 → +0 |
| | 利息覆盖率 | > 10× | +2；5×~10× → +1 |
| | 盈利增长标准差 | < 0.20 稳定 | +2；0.20~0.50 中等 → +1 |
| | 净利润率 | > 15% 缓冲充足 | +1 |
| 赤裸上阵（4分） | 内部人净买入/卖出比率 | > 2.0× | +4；0.5×~2.0× → +3；正净买入 → +2；净卖出 → +0 |
| 波动率机制（6分） | 当前波动/63日均值（机制比率） | 危险低波（< 0.7，"火鸡问题"） | +0；0.7~0.9 → +1；0.9~1.3 正常 → +3；1.3~2.0 高波（机会） → +4；> 2.0 危机 → +2 |
| | 波动率之波动率 | > 2×中位数 高度不稳定 | +2；> 中位数 → +1 |
| 黑天鹅哨兵（4分） | 负面新闻比例 & 成交量异常 | 负面 > 70% 且成交量 > 2× → 0分（警报）；负面 > 50% 或成交量 > 2.5× → +1；清白 → +3 | — |
| | 逆向加成 | 负面 > 40% 但无恐慌卖出 | +1（机会） |

**信号判定规则**：
- 总分 = 七个子模块原始分求和，最大可能分为50分（满分之和）
- 由 `generate_taleb_output()` 调用LLM，传入各子模块的详情字符串
- **bullish**：反脆弱 + 凸性回报，且不脆弱
- **bearish**：脆弱（高杠杆、薄利润、盈利波动大）或无赤裸上阵
- **neutral**：混合信号或数据不足
- 置信度区间：90-100% 真正反脆弱；70-89% 低脆弱 + 期权性；50-69% 混合；30-49% 检测到脆弱；10-29% 明显脆弱或危险波动率机制

---

### 1.10 Peter Lynch（彼得·林奇）

**源码文件**：[peter_lynch.py](file:///e:/qa/ai-hedge-fund/src/agents/peter_lynch.py)

**投资哲学**：林奇是"投资你所了解的"理念倡导者，以"合理价格成长"（GARP）策略闻名。他用 **PEG 比率**（市盈率/增长率）衡量成长是否物有所值，寻找潜在"十倍股"（Ten-Baggers），偏好业务简单易懂、财务稳健（低债务、正FCF）、收益稳定增长的公司，并将新闻情绪和内部人交易作为辅助信号。

**评分子模块函数名**：
- `analyze_lynch_growth(financial_line_items)` — 营收与EPS增长分析
- `analyze_lynch_fundamentals(financial_line_items)` — 基本面分析（D/E、营业利润率、FCF）
- `analyze_lynch_valuation(financial_line_items, market_cap)` — GARP估值（PEG比率为核心）
- `analyze_sentiment(news_items)` — 新闻情绪（负面关键词检测）
- `analyze_insider_activity(insider_trades)` — 内部人交易活动

**评分维度与阈值表格**：

| 子模块 | 权重 | 评分维度 | 阈值/规则 | 得分（原始→映射0-10） |
|--------|------|---------|----------|---------------------|
| 成长（`analyze_lynch_growth`） | 30% | 营收增长（期末vs期初） | > 25% 强劲 | +3 |
| | | | 10%~25% 中等 | +2 |
| | | | 2%~10% 轻微 | +1 |
| | | EPS增长（期末vs期初） | > 25% 强劲 | +3 |
| | | | 10%~25% 中等 | +2 |
| | | | 2%~10% 轻微 | +1 |
| | | 原始满分6分，映射到0-10 | — | — |
| 估值/GARP（`analyze_lynch_valuation`） | 25% | P/E | < 15 | +2；15~25 → +1 |
| | | PEG比率（P/E ÷ EPS增长%） | < 1 极具吸引力 | +3 |
| | | | 1~2 合理 | +2 |
| | | | 2~3 偏贵 | +1 |
| | | 原始满分5分，映射到0-10 | — | — |
| 基本面（`analyze_lynch_fundamentals`） | 20% | D/E | < 0.5 低杠杆 | +2；0.5~1.0 中等 → +1 |
| | | 营业利润率 | > 20% 强 | +2；10%~20% 中等 → +1 |
| | | FCF（最新期） | > 0 正向现金流 | +2 |
| | | 原始满分6分，映射到0-10 | — | — |
| 情绪（`analyze_sentiment`） | 15% | 负面新闻比例 | > 30% → 3分；> 0 → 6分；= 0 → 8分 | — |
| 内部人活动（`analyze_insider_activity`） | 10% | 买入比率 | > 70% → 8分；40%~70% → 6分；< 40% → 4分 | — |

**信号判定规则**：
- 加权总分 = `成长 × 0.30 + 估值 × 0.25 + 基本面 × 0.20 + 情绪 × 0.15 + 内部人 × 0.10`，满分10分
- 总分 ≥ 7.5 → **bullish**
- 总分 ≤ 4.5 → **bearish**
- 其余 → **neutral**
- 最终由 `generate_lynch_output()` 调用LLM以林奇"通俗、轶事"风格生成推理，着重引用PEG比率和"十倍股"潜力

---

### 1.11 Phil Fisher（菲利普·费雪）

**源码文件**：[phil_fisher.py](file:///e:/qa/ai-hedge-fund/src/agents/phil_fisher.py)

**投资哲学**：费雪是"质量成长投资之父"，以"闲聊法"（Scuttlebutt）深度调研著称。他重视公司长期增长潜力、优质管理层、R&D投入对未来产品的驱动力，以及利润率的稳定性。愿意为卓越企业付出溢价，但仍关注估值合理性。通常以3-5年以上长期视角持有。

**评分子模块函数名**：
- `analyze_fisher_growth_quality(financial_line_items)` — 成长与质量分析（营收/EPS CAGR + R&D投入）
- `analyze_margins_stability(financial_line_items)` — 利润率稳定性分析（营业/毛利率趋势）
- `analyze_management_efficiency_leverage(financial_line_items)` — 管理效率与杠杆分析（ROE + D/E + FCF一致性）
- `analyze_fisher_valuation(financial_line_items, market_cap)` — 费雪风格估值（P/E + P/FCF）
- `analyze_insider_activity(insider_trades)` — 内部人交易分析
- `analyze_sentiment(news_items)` — 新闻情绪分析

**评分维度与阈值表格**：

| 子模块 | 权重 | 评分维度 | 阈值/规则 | 得分（原始→映射0-10） |
|--------|------|---------|----------|---------------------|
| 成长质量（`analyze_fisher_growth_quality`） | 30% | 营收年化CAGR | > 20% 极强 | +3；10%~20% → +2；3%~10% → +1 |
| | | EPS年化CAGR | > 20% 极强 | +3；10%~20% → +2；3%~10% → +1 |
| | | R&D/收入比率 | 3%~15% 健康区间 | +3；> 15% 非常高 → +2；> 0% 低但正 → +1 |
| | | 原始满分9分，映射到0-10 | — | — |
| 利润率稳定（`analyze_margins_stability`） | 25% | 营业利润率趋势 | 最新 ≥ 最早且 > 0 → 稳定/改善 | +2；最新 > 0 但下降 → +1 |
| | | 毛利率水平 | > 50% 强 | +2；30%~50% 中等 → +1 |
| | | 多年利润率标准差 | < 2% 极稳定 | +2；2%~5% 合理稳定 → +1 |
| | | 原始满分6分，映射到0-10 | — | — |
| 管理效率（`analyze_management_efficiency_leverage`） | 20% | ROE（净利润/股东权益） | > 20% 高 | +3；10%~20% 中等 → +2；0~10% 低正 → +1 |
| | | D/E | < 0.3 低 | +2；0.3~1.0 可控 → +1 |
| | | FCF一致性（多期正FCF占比） | > 80% 多数期正FCF | +1 |
| | | 原始满分6分，映射到0-10 | — | — |
| 估值（`analyze_fisher_valuation`） | 15% | P/E | < 20 较合理 | +2；20~30 略高但可接受 → +1 |
| | | P/FCF | < 20 合理 | +2；20~30 略高 → +1 |
| | | 原始满分4分，映射到0-10 | — | — |
| 内部人活动（`analyze_insider_activity`） | 5% | 买入比率 | > 70% → 8分；40%~70% → 6分；< 40% → 4分 | — |
| 情绪（`analyze_sentiment`） | 5% | 负面新闻比例 | > 30% → 3分；> 0 → 6分；= 0 → 8分 | — |

**信号判定规则**：
- 加权总分 = `成长质量 × 0.30 + 利润率稳定 × 0.25 + 管理效率 × 0.20 + 估值 × 0.15 + 内部人 × 0.05 + 情绪 × 0.05`，满分10分
- 总分 ≥ 7.5 → **bullish**
- 总分 ≤ 4.5 → **bearish**
- 其余 → **neutral**
- 最终由 `generate_fisher_output()` 调用LLM以费雪"详尽、系统化、长期导向"风格生成推理，着重讨论R&D管线和竞争优势可持续性

---

### 1.12 Rakesh Jhunjhunwala（拉凯什·琼伦瓦拉）

**源码文件**：[rakesh_jhunjhunwala.py](file:///e:/qa/ai-hedge-fund/src/agents/rakesh_jhunjhunwala.py)

**投资哲学**：被誉为"印度巴菲特"，琼伦瓦拉聚焦于印度新兴市场，以严格的安全边际（30%以上）和高质量公司筛选著称。他偏好持续复利增长的公司，关注ROE、营收/净利润CAGR、干净的资产负债表（低债务）和股东友好型管理层（回购/分红）。结合DCF计算内在价值，只有价格显著低于内在价值才买入。

**评分子模块函数名**：
- `analyze_profitability(financial_line_items)` — 盈利能力分析（ROE + 营业利润率 + EPS CAGR）
- `analyze_growth(financial_line_items)` — 营收与净利润CAGR分析
- `analyze_balance_sheet(financial_line_items)` — 资产负债表健康度（负债比率 + 流动比率）
- `analyze_cash_flow(financial_line_items)` — 现金流与分红分析
- `analyze_management_actions(financial_line_items)` — 管理层行动（回购/增发）
- `calculate_intrinsic_value(financial_line_items, market_cap)` — DCF内在价值计算
- `assess_quality_metrics(financial_line_items)` — 质量评估（0-1分，ROE/债务/增长一致性）

**评分维度与阈值表格**：

| 子模块（满分） | 评分维度 | 阈值/规则 | 得分 |
|--------------|---------|----------|------|
| 盈利能力（8分上限） | ROE（净利润/股东权益） | > 20% 优秀 | +3；15%~20% 良好 → +2；10%~15% 一般 → +1 |
| | 营业利润率 | > 20% 优秀 | +2；15%~20% 良好 → +1 |
| | EPS CAGR（近3年历史） | > 20% 高增长 | +3；15%~20% 良好 → +2；10%~15% 中等 → +1 |
| 成长（7分上限） | 营收CAGR | > 20% 优秀 | +3；15%~20% 良好 → +2；10%~15% 中等 → +1 |
| | 净利润CAGR | > 25% 极高 | +3；20%~25% 高 → +2；15%~20% 良好 → +1 |
| | 营收一致性（≥80%年份正增长） | — | +1 |
| 资产负债表（4分上限） | 负债/总资产 | < 0.5 低 | +2；0.5~0.7 中等 → +1 |
| | 流动比率 | > 2.0 优秀 | +2；1.5~2.0 良好 → +1 |
| 现金流（3分上限） | FCF | > 0 正向 | +2 |
| | 分红（现金流出为负表示已分红） | < 0 分红 | +1 |
| 管理层行动（2分上限） | 回购/增发 | < 0 回购股份 | +2；= 0 无动作 → +1；> 0 增发稀释 → +0 |

**内在价值DCF模型**（`calculate_intrinsic_value`）：
- 基础盈利：最新净利润
- 历史CAGR → 保守增长率（> 25%则上限20%，> 15%取历史×0.8，> 5%取历史×0.9）
- 折现率：质量高（≥0.8）→ 12%，质量中（≥0.6）→ 15%，质量低 → 18%
- 终值倍数：质量高→18×，中→15×，低→12×
- 5年DCF + 第5年终值，加权求和

**信号判定规则**：
- 总分 = 五个子模块直接求和，满分24分
- **优先规则（安全边际驱动）**：
  - 安全边际 = `(内在价值 - 市值) / 市值`
  - 安全边际 ≥ 30% → **bullish**
  - 安全边际 ≤ -30% → **bearish**
- **次要规则（质量分兜底）**：
  - `assess_quality_metrics()` ≥ 0.7 且总分 ≥ 14.4（60%满分） → **bullish**（合理价格的高质量公司）
  - 质量分 ≤ 0.4 或总分 ≤ 7.2（30%满分） → **bearish**
  - 其余 → **neutral**
- 置信度：`min(max(|安全边际| × 150, 20), 95)`（20%~95%区间）
- 最终由 `generate_jhunjhunwala_output()` 调用LLM以琼伦瓦拉"强调复利、质量管理、安全边际"的对话风格生成推理

---

## 二、量化分析Agent

### 2.1 Technical Analyst（技术分析师）

**源码文件**：[technicals.py](file:///e:/qa/ai-hedge-fund/src/agents/technicals.py)

**投资哲学**：纯技术面驱动，不依赖基本面或LLM推理。通过综合5种量化策略的加权集成（Weighted Ensemble），从价格动量、均值回归、波动率机制、统计套利四个维度捕捉市场信号。每种策略独立给出信号和置信度，最终加权组合输出。

**评分子模块函数名**：
- `calculate_trend_signals(prices_df)` — 趋势跟踪策略（EMA多周期 + ADX趋势强度）
- `calculate_mean_reversion_signals(prices_df)` — 均值回归策略（Z分数 + 布林带 + RSI）
- `calculate_momentum_signals(prices_df)` — 多因子动量策略（1/3/6月动量 + 成交量动量）
- `calculate_volatility_signals(prices_df)` — 波动率策略（历史波动率机制 + ATR比率）
- `calculate_stat_arb_signals(prices_df)` — 统计套利信号（Hurst指数 + 偏度/峰度）
- `weighted_signal_combination(signals, weights)` — 加权信号合成

**评分维度与阈值表格**：

| 策略 | 权重 | 评分维度 | 信号判定阈值 | 置信度计算 |
|------|------|---------|------------|----------|
| 趋势跟踪（`calculate_trend_signals`） | 25% | EMA8 vs EMA21 vs EMA55多周期排列 | 多头排列（8>21>55）→ bullish；空头排列（8<21<55）→ bearish；其余 neutral | `ADX / 100`（ADX为14期平均趋向指数） |
| 均值回归（`calculate_mean_reversion_signals`） | 20% | Z分数（价格vs50日均线，以50日标准差标准化）& 布林带位置（0-1区间） | Z < -2 且 布林带位置 < 0.2 → bullish；Z > 2 且布林带位置 > 0.8 → bearish；其余 neutral | `min(|Z| / 4, 1.0)` |
| 动量（`calculate_momentum_signals`） | 25% | 综合动量分 = 1月收益×0.4 + 3月×0.3 + 6月×0.3；成交量动量 = 当日成交量/21日均量 | 动量分 > 0.05 且成交量动量 > 1 → bullish；动量分 < -0.05 且成交量动量 > 1 → bearish；其余 neutral | `min(|动量分| × 5, 1.0)` |
| 波动率（`calculate_volatility_signals`） | 15% | 当前波动率机制比率（当前/63日均）；Z分数（波动率vs其标准差） | 机制 < 0.8 且 Z < -1 → bullish（低波动扩张机会）；机制 > 1.2 且 Z > 1 → bearish（高波动收缩机会）；其余 neutral | `min(|Z| / 3, 1.0)` |
| 统计套利（`calculate_stat_arb_signals`） | 15% | Hurst指数（< 0.5 均值回归；= 0.5 随机游走；> 0.5 趋势）；63日滚动偏度 | Hurst < 0.4 且偏度 > 1 → bullish；Hurst < 0.4 且偏度 < -1 → bearish；其余 neutral | `(0.5 - Hurst) × 2`（仅Hurst < 0.5时有效） |

**信号判定规则**（`weighted_signal_combination`）：
- 将各策略信号转为数值：bullish=+1，neutral=0，bearish=-1
- `加权分 = Σ(信号值 × 策略权重 × 该策略置信度) / Σ(策略权重 × 置信度)`
- 最终加权分 > 0.2 → **bullish**
- 最终加权分 < -0.2 → **bearish**
- 其余 → **neutral**
- 最终置信度 = `|加权分|`（0~1，乘以100后取整作为百分比输出）
- **注意**：技术分析Agent无LLM调用，全程规则化计算，输出的 reasoning 字段为结构化JSON

---

### 2.2 Fundamentals Analyst（基本面分析师）

**源码文件**：[fundamentals.py](file:///e:/qa/ai-hedge-fund/src/agents/fundamentals.py)

**职责说明**：对每只股票的基本面财务数据进行多维度分析，从盈利能力、成长性、财务健康度和估值比率四个方面生成交易信号。该Agent不依赖LLM，完全基于规则化指标阈值计算。

**核心函数**：
- `fundamentals_analyst_agent(state, agent_id)` — 主入口函数，遍历所有ticker，获取财务指标并生成信号

**评分/计算逻辑**：

| 分析维度 | 指标 | 阈值/条件 | 评分方式 |
|---------|------|----------|---------|
| 盈利能力（Profitability） | ROE > 15%, 净利润率 > 20%, 营业利润率 > 15% | 3项中达标项数 | ≥2 bullish, =0 bearish, 否则 neutral |
| 成长性（Growth） | 营收增长 > 10%, 盈利增长 > 10%, 账面价值增长 > 10% | 3项中达标项数 | ≥2 bullish, =0 bearish, 否则 neutral |
| 财务健康（Financial Health） | 流动比率 > 1.5, 负债权益比 < 0.5, FCF/每股收益 > 0.8 | 3项中达标项数 | ≥2 bullish, =0 bearish, 否则 neutral |
| 估值比率（Price Ratios） | P/E < 25, P/B < 3, P/S < 5 | 3项中超出项数 | ≥2 bearish（高估）, =0 bullish（低估）, 否则 neutral |

- **综合信号**：4个维度信号的多数投票（bullish/bearish/neutral）
- **置信度**：`max(bullish_count, bearish_count) / total_signals × 100`

**输入**：
- `state["data"]["tickers"]` — 股票代码列表
- `state["data"]["end_date"]` — 截止日期
- 通过 `get_financial_metrics()` 获取财务指标（TTM，最近10期）

**输出**：
- 每个ticker的 `signal`（bullish/bearish/neutral）、`confidence`（0-100）、`reasoning`（四个维度的详情）
- 写入 `state["data"]["analyst_signals"]["fundamentals_analyst_agent"]`

---

### 2.3 Valuation Analyst（估值分析师）

**源码文件**：[valuation.py](file:///e:/qa/ai-hedge-fund/src/agents/valuation.py)

**职责说明**：运用四种互补的估值方法论对公司进行估值，并加权汇总后与当前市值比较，判断股价是否被低估或高估。支持增强型DCF（含WACC计算和多情景分析）。

**核心函数**：
- `valuation_analyst_agent(state, agent_id)` — 主入口函数
- `calculate_owner_earnings_value(net_income, depreciation, capex, working_capital_change, ...)` — 巴菲特"所有者盈余"估值法
- `calculate_intrinsic_value(free_cash_flow, growth_rate, discount_rate, ...)` — 经典FCF DCF估值
- `calculate_ev_ebitda_value(financial_metrics)` — EV/EBITDA可比估值
- `calculate_residual_income_value(market_cap, net_income, price_to_book_ratio, ...)` — 剩余收益模型（Edwards-Bell-Ohlson）
- `calculate_wacc(market_cap, total_debt, cash, interest_coverage, ...)` — 加权平均资本成本计算
- `calculate_fcf_volatility(fcf_history)` — FCF波动率（变异系数）
- `calculate_enhanced_dcf_value(fcf_history, growth_metrics, wacc, market_cap, ...)` — 增强型三阶段DCF
- `calculate_dcf_scenarios(fcf_history, growth_metrics, wacc, market_cap, ...)` — 多情景DCF（熊市/基准/牛市）

**评分/计算逻辑**：

| 估值方法 | 权重 | 安全边际 | 核心思路 |
|---------|------|---------|---------|
| 增强型DCF | 35% | 质量因子调整（基于FCF波动率） | 三阶段增长（高增长→过渡→终值），WACC折现 |
| 所有者盈余 | 35% | 25%安全边际 | 巴菲特法：净利+折旧-资本支出-营运资金变动 |
| EV/EBITDA | 20% | 无额外安全边际 | 中位数乘数法，隐含股权价值 = EV - 净债务 |
| 剩余收益 | 10% | 20%安全边际 | 账面价值 + 剩余收益现值 + 终值 |

- **WACC计算**：CAPM估算股权成本（无风险利率4.5% + β×6%），利息覆盖率估算债务成本，股权/债务权重，25%税率税盾
- **DCF多情景**：熊市（增长×0.5, WACC×1.2）、基准、牛市（增长×1.5, WACC×0.9），概率权重 20%/60%/20%
- **加权缺口**：各方法 gap = (估值 - 市值) / 市值，加权平均后判断信号
- **信号**：加权缺口 > 15% → bullish，< -15% → bearish，否则 neutral
- **置信度**：`min(abs(weighted_gap) / 0.30 × 100, 100)`

**输入**：
- 通过 `get_financial_metrics()` 获取财务指标（8期TTM）
- 通过 `search_line_items()` 获取自由现金流、净利润、折旧摊销、资本支出、营运资金、总债务、现金等科目
- 通过 `get_market_cap()` 获取当前市值

**输出**：
- 每个ticker的 `signal`、`confidence`、`reasoning`（含各估值方法详情、DCF情景分析、WACC等）
- 写入 `state["data"]["analyst_signals"]["valuation_analyst_agent"]`

---

### 2.4 Growth Analyst（成长分析师）

**源码文件**：[growth_agent.py](file:///e:/qa/ai-hedge-fund/src/agents/growth_agent.py)

**职责说明**：专注于成长型投资视角，从历史增长趋势、成长估值、利润率扩张、内部人交易信念和财务健康度五个维度综合评估股票的成长潜力。

**核心函数**：
- `growth_analyst_agent(state, agent_id)` — 主入口函数
- `analyze_growth_trends(metrics)` — 历史增长趋势分析（营收/EPS/FCF增长 + 线性趋势）
- `analyze_valuation(metrics)` — 成长视角估值（PEG比率 + P/S比率）
- `analyze_margin_trends(metrics)` — 利润率扩张监控（毛利率/营业利润率/净利率趋势）
- `analyze_insider_conviction(trades)` — 内部人交易信念追踪
- `check_financial_health(metrics)` — 财务健康检查（D/E + 流动比率）
- `_calculate_trend(data)` — 简单线性回归计算趋势斜率

**评分/计算逻辑**：

| 维度 | 权重 | 评分规则 |
|------|------|---------|
| 历史增长（Growth） | 40% | 营收增长>20% +0.4, >10% +0.2; 加速趋势 +0.1; EPS增长>20% +0.25, >10% +0.1; FCF增长>15% +0.1; 满分1.0 |
| 成长估值（Valuation） | 25% | PEG<1 +0.5, <2 +0.25; P/S<2 +0.5, <5 +0.25; 满分1.0 |
| 利润率扩张（Margins） | 15% | 毛利率>50% +0.2, 扩张趋势 +0.2; 营业利润率>15% +0.2, 扩张趋势 +0.2; 净利率扩张 +0.2; 满分1.0 |
| 内部人信念（Insider） | 10% | 净买入比率>0.5 → 1.0, >0.1 → 0.7, ≈0 → 0.5, <0 → 0.2 |
| 财务健康（Health） | 10% | 起始1.0; D/E>1.5 -0.5, >0.8 -0.2; 流动比率<1.0 -0.5, <1.5 -0.2; 最低0.0 |

- **加权总分**：`Σ(score_i × weight_i)`，范围 0~1
- **信号**：加权总分 > 0.6 → bullish，< 0.4 → bearish，否则 neutral
- **置信度**：`abs(weighted_score - 0.5) × 2 × 100`

**输入**：
- 通过 `get_financial_metrics()` 获取12期TTM财务指标（约3年数据）
- 通过 `get_insider_trades()` 获取内部人交易记录（最近1000条）

**输出**：
- 每个ticker的 `signal`、`confidence`、`reasoning`（五个维度的详细评分和指标）
- 写入 `state["data"]["analyst_signals"]["growth_analyst_agent"]`

---

### 2.5 Aswath Damodaran（阿斯沃思·达摩达兰）

**源码文件**：[aswath_damodaran.py](file:///e:/qa/ai-hedge-fund/src/agents/aswath_damodaran.py)

**职责说明**：以纽约大学斯特恩商学院教授Aswath Damodaran的估值框架为核心，通过CAPM计算股权成本、分析营收/FCFF增长趋势与再投资效率、执行FCFF DCF得出内在价值，并以相对估值交叉验证，最终由LLM以Damodaran的分析风格输出叙述性报告。

**核心函数**：
- `aswath_damodaran_agent(state, agent_id)` — 主入口函数
- `analyze_growth_and_reinvestment(metrics, line_items)` — 增长与再投资效率分析
- `analyze_risk_profile(metrics, line_items)` — 风险画像分析（Beta、D/E、利息覆盖率）
- `calculate_intrinsic_value_dcf(metrics, line_items, risk_analysis)` — FCFF DCF内在价值计算
- `analyze_relative_valuation(metrics)` — 相对估值分析（PE vs 历史中位数）
- `estimate_cost_of_equity(beta)` — CAPM股权成本估算
- `generate_damodaran_output(ticker, analysis_data, state, agent_id)` — LLM生成Damodaran风格叙述

**评分/计算逻辑**：

| 分析模块 | 满分 | 评分规则 |
|---------|------|---------|
| 增长与再投资 | 4 | 5年营收CAGR>8% +2, >3% +1; FCFF正增长 +1; ROIC>10% +1 |
| 风险画像 | 3 | Beta<1.3 +1; D/E<1 +1; 利息覆盖率>3× +1 |
| 相对估值 | 1 | TTM P/E < 70%历史中位数 +1, >130% -1, 否则 0 |

- **FCFF DCF**：基础FCFF = 最近期自由现金流；增长率 = 5年营收CAGR（上限12%）；10年内线性衰减至终端增长率2.5%；折现率 = CAPM股权成本（无风险4% + β×5% ERP）
- **安全边际**：`(intrinsic_value - market_cap) / market_cap`
- **信号**：安全边际 ≥ 25% → bullish，≤ -25% → bearish，否则 neutral
- **最终输出**：由LLM以Damodaran的"故事→数字→价值"叙事风格生成 reasoning

**输入**：
- 通过 `get_financial_metrics()` 获取5期TTM财务指标
- 通过 `search_line_items()` 获取自由现金流、EBIT、利息支出、资本支出、折旧摊销、流通股数、净利润、总债务
- 通过 `get_market_cap()` 获取市值

**输出**：
- 每个ticker的 `signal`、`confidence`（0-100）、`reasoning`（LLM生成的Damodaran风格分析叙述）
- 写入 `state["data"]["analyst_signals"]["aswath_damodaran_agent"]`

---

## 三、情绪分析Agent

### 3.1 Sentiment Analyst（情绪分析师）

**源码文件**：[sentiment.py](file:///e:/qa/ai-hedge-fund/src/agents/sentiment.py)

**职责说明**：综合分析内部人交易行为和公司新闻情绪，以加权方式生成市场情绪信号。内部人交易数据反映公司内部人对股票的信心，新闻情绪反映市场舆论方向。

**核心函数**：
- `sentiment_analyst_agent(state, agent_id)` — 主入口函数

**评分/计算逻辑**：

| 信号来源 | 权重 | 信号提取方式 |
|---------|------|------------|
| 内部人交易（Insider Trading） | 30% | 交易股数 > 0 → bullish, < 0 → bearish |
| 公司新闻（Company News） | 70% | 新闻情绪 = positive → bullish, negative → bearish, 否则 neutral |

- **加权信号计算**：bullish_weighted = insider_bullish×0.3 + news_bullish×0.7，bearish同理
- **综合信号**：bullish_weighted > bearish_weighted → bullish，反之 bearish，相等 neutral
- **置信度**：`max(bullish_weighted, bearish_weighted) / total_weighted_signals × 100`

**输入**：
- 通过 `get_insider_trades()` 获取内部人交易记录（最近1000条）
- 通过 `get_company_news()` 获取公司新闻（最近100条）

**输出**：
- 每个ticker的 `signal`、`confidence`、`reasoning`（含内部人交易详情和新闻情绪详情的加权组合分析）
- 写入 `state["data"]["analyst_signals"]["sentiment_analyst_agent"]`

---

### 3.2 News Sentiment（新闻情绪分析师）

**源码文件**：[news_sentiment.py](file:///e:/qa/ai-hedge-fund/src/agents/news_sentiment.py)

**职责说明**：专门针对公司新闻进行深度情绪分析。对于缺失情绪标注的新闻文章，使用LLM对标题进行情绪分类（positive/negative/neutral）和置信度打分，然后汇总所有文章情绪生成交易信号。与Sentiment Analyst不同，该Agent聚焦于新闻维度的精细化分析。

**核心函数**：
- `news_sentiment_agent(state, agent_id)` — 主入口函数
- `_calculate_confidence_score(sentiment_confidences, company_news, overall_signal, bullish_signals, bearish_signals, total_signals)` — 置信度评分计算

**评分/计算逻辑**：

1. **LLM情绪分类**：取最近10篇文章，对缺失情绪标注的最多5篇调用LLM进行情绪分析
   - 输入：新闻标题 + 股票代码上下文
   - 输出：`Sentiment` 模型（sentiment: positive/negative/neutral, confidence: 0-100）

2. **信号汇总**：positive → bullish, negative → bearish, neutral → neutral，多数投票决定 overall_signal

3. **置信度计算**（`_calculate_confidence_score`）：
   - 若有LLM置信度：`0.7 × avg_llm_confidence + 0.3 × signal_proportion`（LLM置信度权重70%，信号比例权重30%）
   - 否则回退：`max(bullish, bearish) / total × 100`

**输入**：
- 通过 `get_company_news()` 获取公司新闻（最近100条）
- LLM对缺失情绪的文章标题进行分类

**输出**：
- 每个ticker的 `signal`、`confidence`、`reasoning`（含文章总数、多空文章数、LLM分类数等指标）
- 写入 `state["data"]["analyst_signals"]["news_sentiment_agent"]`

---

## 四、决策层

### 4.1 Risk Manager（风险经理）

**源码文件**：[risk_manager.py](file:///e:/qa/ai-hedge-fund/src/agents/risk_manager.py)

**职责说明**：基于波动率和相关性调整的风险控制Agent，负责计算每只股票的持仓上限。综合考虑年化波动率（反比关系）和与现有持仓的相关性（高相关则缩减额度），确保投资组合的风险敞口在可控范围内。

**核心函数**：
- `risk_management_agent(state, agent_id)` — 主入口函数
- `calculate_volatility_metrics(prices_df, lookback_days=60)` — 波动率指标计算
- `calculate_volatility_adjusted_limit(annualized_volatility)` — 波动率调整持仓上限
- `calculate_correlation_multiplier(avg_correlation)` — 相关性调整乘数

**评分/计算逻辑**：

**波动率调整持仓上限**（`calculate_volatility_adjusted_limit`）：

| 年化波动率区间 | 波动率乘数 | 实际上限 |
|--------------|-----------|---------|
| < 15%（低波动） | 1.25× | 25% |
| 15%-30%（中波动） | 1.0×→0.625× 线性递减 | 12.5%-20% |
| 30%-50%（高波动） | 0.75×→0.25× 线性递减 | 5%-15% |
| > 50%（极高波动） | 0.50× | 10% |

基准上限 = 20%，乘数范围限制在 0.25~1.25（即5%~25%）

**相关性调整乘数**（`calculate_correlation_multiplier`）：

| 平均相关性区间 | 乘数 |
|--------------|------|
| ≥ 0.80 | 0.70×（大幅缩减） |
| 0.60-0.80 | 0.85×（适度缩减） |
| 0.40-0.60 | 1.00×（中性） |
| 0.20-0.40 | 1.05×（微增） |
| < 0.20 | 1.10×（增加，分散化收益） |

- **综合持仓上限**：`base_limit × vol_multiplier × corr_multiplier`
- **剩余可用额度**：`组合总价值 × 综合上限% - 当前持仓市值`，且不超过可用现金
- **波动率指标**：日波动率、年化波动率（日波动率×√252）、波动率百分位（30日滚动对比历史）

**输入**：
- `state["data"]["portfolio"]` — 当前投资组合（现金、持仓、保证金）
- 通过 `get_prices()` 获取价格数据

**输出**：
- 每个ticker的 `remaining_position_limit`（剩余可持仓金额）、`current_price`、波动率指标、相关性指标、决策推理
- 写入 `state["data"]["analyst_signals"]["risk_management_agent"]`

---

### 4.2 Portfolio Manager（投资组合经理）

**源码文件**：[portfolio_manager.py](file:///e:/qa/ai-hedge-fund/src/agents/portfolio_manager.py)

**职责说明**：系统最终的决策层Agent，接收所有分析师信号和风险经理的持仓约束，通过LLM综合判断后生成具体的交易指令（买入/卖出/做空/平仓/持有），并指定交易数量和置信度。

**核心函数**：
- `portfolio_management_agent(state, agent_id)` — 主入口函数
- `compute_allowed_actions(tickers, current_prices, max_shares, portfolio)` — 确定性计算每只股票的可行操作及最大数量
- `_compact_signals(signals_by_ticker)` — 压缩分析师信号为{sig, conf}格式
- `generate_trading_decision(tickers, signals_by_ticker, current_prices, max_shares, portfolio, agent_id, state)` — LLM生成交易决策

**数据模型**：
- `PortfolioDecision`：action（buy/sell/short/cover/hold）、quantity、confidence、reasoning
- `PortfolioManagerOutput`：decisions字典 {ticker → PortfolioDecision}

**评分/计算逻辑**：

1. **信号收集**：从 `analyst_signals` 中提取所有非风险经理Agent的信号（排除 `risk_management_agent*`）
2. **持仓约束**：从风险经理获取 `remaining_position_limit` 和 `current_price`，计算 `max_shares = position_limit // price`
3. **可行操作计算**（`compute_allowed_actions`，确定性规则）：
   - 有多头持仓 → 可 sell
   - 有现金且价格>0 → 可 buy（上限 = min(max_shares, cash//price)）
   - 有空头持仓 → 可 cover
   - 有保证金余额 → 可 short（上限 = min(max_shares, available_margin//price)）
   - hold 始终可用
   - 仅保留数量>0的操作（hold除外），减少LLM token消耗
4. **预填充**：若某ticker仅有hold操作（无交易可能），直接预填 hold 决策，不发送给LLM
5. **LLM决策**：将压缩后的分析师信号和可行操作发送给LLM，要求选择一个操作和数量（≤最大值）
6. **合并**：预填充的hold决策 + LLM返回的决策合并为最终输出

**输入**：
- `state["data"]["analyst_signals"]` — 所有分析师信号
- `state["data"]["portfolio"]` — 当前投资组合
- `state["data"]["tickers"]` — 股票列表
- 风险经理的持仓约束数据

**输出**：
- 每个ticker的 `action`（buy/sell/short/cover/hold）、`quantity`、`confidence`、`reasoning`
- 写入 `state["data"]["current_prices"]` 和消息流

---

*文档生成时间：2026-04-21*
*AI Hedge Fund Multi-Agent System*
