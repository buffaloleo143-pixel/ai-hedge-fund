"""验证 akshare_provider 修复后的数据缺失问题"""
from src.tools.api import get_financial_metrics, search_line_items

# 测试 financial_metrics
metrics = get_financial_metrics('600690', '2025-04-20', 'annual', 3)
if metrics:
    for i, m in enumerate(metrics):
        non_none = sum(1 for f in dir(m) if not f.startswith('_') and getattr(m, f, None) is not None)
        print(f'Metric {i}: {m.report_period}, fields with value: {non_none}')

# 测试 search_line_items 关键字段
print()
items = search_line_items('600690', ['free_cash_flow', 'capital_expenditure', 'ebitda', 'working_capital', 'total_debt', 'revenue', 'net_income', 'operating_cash_flow'], '2025-04-20', 'annual', 2)
if items:
    for item in items:
        print(f'Period: {item.report_period}')
        for f in ['free_cash_flow','capital_expenditure','ebitda','working_capital','total_debt','revenue','net_income','operating_cash_flow']:
            print(f'  {f}: {getattr(item, f, None)}')
else:
    print('No line items returned!')
