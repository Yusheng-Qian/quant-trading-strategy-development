# quant-trading-strategy-development
Developed and backtested quantitative trading strategies using Codex and portfolio performance metrics.
# Quant Trading Strategy Development

This project develops and backtests quantitative trading strategies using historical financial market data.

## Overview

The goal of this project is to evaluate whether rule-based trading signals can outperform a passive buy-and-hold strategy.

The project focuses on:

- signal generation
- strategy backtesting
- portfolio return analysis
- risk-adjusted performance evaluation

## Strategies

The strategies explored include:

- Moving Average Crossover
- RSI-based Mean Reversion

## Evaluation Metrics

Strategy performance is evaluated using:

- cumulative return
- annualized return
- volatility
- Sharpe ratio
- maximum drawdown

## Tools

- Python
- Pandas
- NumPy
- Matplotlib


# 9.0 Test

只保留交易策略系统最小运行链路:

- `data.market_data`
- `engine.feature_collector`
- `engine.meta_decision`
- `engine.decision_engine`
- `core.strategy`
- `core.grid_executor`
- `core.paper_trader` / `core.binance_trader`
- `risk.*`

已默认关闭:

- Dashboard / Web
- AI 训练 / Qwen / DailyLearner
- 新闻分析
- 资金费率套利
- 数据库与附加调度器

常用命令:

```bash
bin/run_strategy_test.sh --mode paper --init-only
bin/run_strategy_test.sh --mode paper --once
bin/run_strategy_test.sh --mode paper --loop-delay 60
bin/run_strategy_test.sh --mode live --init-only
```

`--mode live --init-only` 只做只读 API 预热，不会清单或改杠杆。

