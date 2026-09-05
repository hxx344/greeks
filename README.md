# BTC Options Iron Condor

一个带可视化面板的 Bybit BTC 期权 Iron Condor 系统。默认是 `dry-run`：会读取 Bybit 主网公开行情、计算选腿并记录模拟订单，但不会发送真实订单。`BYBIT_TESTNET` 只控制私有仓位和下单 API。

## 启动

```powershell
py -m venv .venv
.\\.venv\\Scripts\\Activate.ps1
pip install -e .
Copy-Item .env.example .env
uvicorn app.main:app --reload
```

打开 http://127.0.0.1:8000 。

## 实盘安全开关

仅当同时满足以下条件时才会调用 Bybit 私有下单 API：

* `.env` 中 `LIVE_TRADING=true`
* `BYBIT_API_KEY` 和 `BYBIT_API_SECRET` 已设置
* 请求体中的 `confirm_live=true`
* `BYBIT_TESTNET=false`（建议先使用测试网）

默认风险上限为 `MAX_RISK_USD=2500`。策略采用固定周历：仅允许 UTC 周五实盘开仓，四条腿必须属于同一个 UTC 周日到期日；`TARGET_DTE_DAYS` 仅为旧配置兼容项，不再用于滚动选择“开仓后两天”的合约。
实盘执行使用 Limit + BBO 跟随：Buy 挂 Bid1（买一），Sell 挂 Ask1（卖一），默认每 1 秒读取最新报价并在 BBO 变化时改单，600 秒未完成则撤单。默认不使用市价兜底。Limit BBO 是程序侧跟单，不是交易所原子组合订单，四腿可能不同步成交。

前端数量选择会实时请求 `/api/strategy/preview?quantity=...` 重算净权利金、Bybit 官方 Regular/Cross 期权 Order IM、短期权 Maintenance MM 和交易成本。手续费使用 Bybit 公式 `min(fee_rate × index_price, 7% × option_price) × qty`。若设置 `MARGIN_MODE=PORTFOLIO_MARGIN`，页面显示的是基于四腿压力损失的下界估算；Portfolio Margin 的精确账户级结果仍由 Bybit 风险引擎根据全账户仓位和动态压力参数决定。

## Bybit 铁鹰组合下单说明

Bybit 官方 V5 提供的是单腿期权下单接口 `/v5/order/create`，以及支持现货/永续/期货组合的 Spread Trading 接口 `/v5/spread/order/create`。当前 Spread Instruments 不包含期权铁鹰组合，因此本系统会对四条期权腿分别下单，并使用唯一 `orderLinkId`、数量/价差/风险校验；不发送成交后的反向回滚，部分成交时提示人工核对 Bybit 仓位。不能把截图中的网页策略组合直接映射成一个官方铁鹰 `spread symbol`。

Bybit 非 VIP 期权基础费率：Taker 0.03%、Maker 0.02%；单笔交易手续费不超过期权成交价的 7%。BTC/ETH 到期交割费为 0.015%，强平费为 0.2%。最终成交以 `/v5/execution/list` 返回的 `execFee`、`feeRate` 和 `feeCurrency` 为准。平仓仅允许关闭本系统记录的最多四个策略 symbol，并按本次策略记录的数量限制平仓；未记录的实盘仓位会被拒绝处理。
