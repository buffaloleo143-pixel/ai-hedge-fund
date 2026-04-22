---
name: stock-analysis-report
description: 运行AI多因子投资分析并生成结构化MD报告。当用户要求分析某只A股股票、生成投资分析报告、或使用"/stock-analysis-report"命令时触发。支持A股（6位数字代码）和美股（字母代码）。
---

# 股票多因子分析报告生成

## 概述

本技能执行完整的AI多Agent投资分析流程，并将结果整理为结构化的Markdown投资分析报告。

## 工作流程

### Step 1：运行端到端分析

在项目根目录执行分析命令：

```powershell
cd e:\qa\ai-hedge-fund; poetry run python src/main.py --tickers {TICKER} --analysts-all --model qwen3-coder-plus
```

- `{TICKER}` 替换为目标股票代码（如 `600519`、`600690`、`AAPL`）
- 命令会运行19个投资大师Agent对目标股票进行独立分析
- 记录完整输出，包括每个Agent的信号、信心度、关键数据点和最终交易决策

### Step 2：读取Agent源码提取分析框架

读取以下Agent源码文件，提取每个Agent的**评分维度、阈值、权重和信号规则**：

| Agent文件 | 角色 | 关注重点 |
|-----------|------|----------|
| `src/agents/bill_ackman.py` | 激进价值 | 护城河、FCF、资本纪律、激进主义潜力 |
| `src/agents/cathie_wood.py` | 成长创新 | 颠覆性创新、R&D、高增长DCF |
| `src/agents/mohnish_pabrai.py` | 防守价值 | 下行保护、FCF yield、翻倍潜力 |
| `src/agents/stanley_druckenmiller.py` | 动量宏观 | 增长动量、风险回报、内部人 |
| `src/agents/michael_burry.py` | 深度价值 | FCF yield、EV/EBIT、资产负债表 |
| `src/agents/warren_buffett.py` | 护城河增长 | 护城河、管理、三阶段DCF |
| `src/agents/charlie_munger.py` | 护城河可预测 | ROIC、业务可预测性、轻资产 |
| `src/agents/nassim_taleb.py` | 反脆弱 | 尾部风险、凸性、杠杆脆弱性 |
| `src/agents/ben_graham.py` | 古典价值 | Graham Number、Net-Net、流动比率 |
| `src/agents/technicals.py` | 技术面 | EMA/RSI/MACD/布林带/动量 |
| `src/agents/risk_manager.py` | 风险管理 | 波动率调整仓位、相关性 |
| `src/agents/portfolio_manager.py` | 最终决策 | 信号综合、风险约束、仓位决策 |
| `src/agents/fundamentals.py` | 基本面 | 财务指标综合评估 |
| `src/agents/valuation.py` | 估值 | DCF/可比估值 |
| `src/agents/growth_agent.py` | 成长 | 营收/利润增长趋势 |
| `src/agents/sentiment.py` | 情绪 | 市场情绪综合 |
| `src/agents/news_sentiment.py` | 新闻 | 新闻情绪分析 |

提取要点：
- 每个Agent的**评分函数名**和**各评分项的阈值**（如 `return_on_equity > 0.15` 得2分）
- **信号判定规则**（如 `total_score >= 0.7 * max_score → bullish`）
- **权重分配**（如 护城河35%、管理25%、可预测性25%、估值15%）

### Step 3：生成MD报告

将报告保存到 `reports/{TICKER}_{公司名称}_投资分析报告.md`。

## 报告模板

```markdown
# {公司名称}（{TICKER}）AI 多因子投资分析报告

## 一、分析概述

| 项目 | 内容 |
|------|------|
| 分析标的 | {公司名称}（{TICKER}） |
| 分析日期 | {YYYY-MM-DD} |
| 使用模型 | qwen3-coder-plus |
| 分析师Agent数量 | 19 位 |
| 最终决策 | **{ACTION}，信心度 {CONFIDENCE}%** |
| 多空统计 | 看多 {N} / 看空 {N} / 中性 {N} |

本报告基于 AI 对冲基金多 Agent 协同分析框架生成...

---

## 二、核心财务数据

| 指标 | 数值 | 评价 |
|------|------|------|
| 市盈率（P/E） | {value} | {评价} |
| 净资产收益率（ROE） | {value} | {评价} |
| 自由现金流收益率（FCF Yield） | {value} | {评价} |
| ... | ... | ... |

---

## 三、各投资大师 Agent 分析详解

### 3.N {大师名}（{中文名}）— {SIGNAL}，信心度 {CONF}%

**投资哲学**：
{从源码prompt中提取的投资理念}

**代码中的评分维度与阈值**（`src/agents/{agent}.py`）：
{列出所有评分子模块、各项阈值、最高分}

**对{公司名称}的判断**：
{结合实际数据说明该Agent如何打分、为何给出该信号}

---

## 四、技术面分析

{Technical Analyst的五策略加权框架、各策略权重和信号合成逻辑}

---

## 五、情绪面与新闻分析

**5.1 内部人交易信号**
{各Agent的内部人评分模块对比表}

**5.2 新闻情绪分析**
{新闻关键词匹配逻辑和结论}

---

## 六、风险评估

{Risk Manager的波动率调整和相关性调整逻辑}

---

## 七、Portfolio Manager 决策逻辑

{信号收集→风险约束→LLM综合决策的完整流程}

---

## 八、投资建议总结

### 8.1 多空因素对比

| 因素 | 具体数据 | 支撑Agent |
|------|---------|----------|
| {看多因素} | {数据} | {Agent} |

| 因素 | 具体数据 | 担忧Agent |
|------|---------|----------|
| {看空因素} | {数据} | {Agent} |

### 8.2 适合投资者类型

| 投资者类型 | 适配度 | 说明 |
|-----------|-------|------|
| 价值投资者 | ★★★★☆ | {说明} |
| ... | ... | ... |

### 8.3 关键监控指标

| 指标 | 当前值 | 监控阈值 | 来源Agent |
|------|-------|---------|----------|
| {指标} | {值} | {阈值说明} | {Agent} |

### 8.4 结论

{综合多空分析的最终结论}

---

*报告生成时间：{YYYY-MM-DD}*
*分析模型：qwen3-coder-plus*
*框架版本：AI Hedge Fund Multi-Agent System*
*免责声明：本报告由AI系统自动生成，仅供研究参考，不构成投资建议。*
```

## 报告撰写要求

1. **全部中文**撰写
2. 每个Agent必须引用源码中**实际的评分函数名、阈值条件**，不可凭空编造
3. 用 `**加粗**` 标注该股票在各阈值上的达标/未达标情况
4. 报告总长度至少 **300行** Markdown
5. 表格格式清晰，数据对齐

## 参考示例

完整报告示例见 [600690_海尔智家_投资分析报告.md](../../reports/600690_海尔智家_投资分析报告.md)

## 注意事项

- A股代码为6位纯数字（如600519、600690）
- 运行分析时间较长（约3-5分钟），需要后台执行并等待完成
- 如果某些Agent报告数据缺失，在报告中如实记录
- PowerShell中使用分号 `;` 分隔命令，不能用 `&&`
