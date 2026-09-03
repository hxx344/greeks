from datetime import datetime, timedelta

from .models import OptionInstrument, StrategyLeg, StrategyPreview


def _nearest(options: list[OptionInstrument], target: float) -> OptionInstrument:
    return min(options, key=lambda item: abs(abs(item.delta) - target))


def choose_expiry(options: list[OptionInstrument], now: datetime, target_dte: int) -> datetime:
    target = now + timedelta(days=target_dte)
    expiries = sorted({item.expiry for item in options if item.expiry > now})
    if not expiries:
        raise ValueError("No future BTC option expiry available")
    return min(expiries, key=lambda expiry: abs((expiry - target).total_seconds()))


def build_iron_condor(options: list[OptionInstrument], now: datetime, target_dte: int = 2, qty: float = 1, contract_multiplier: float = 1.0, fee_rate: float = 0.0003, margin_buffer_pct: float = 0.0, index_price: float = 0.0, margin_mode: str = "REGULAR_MARGIN", mm_factor: float = 0.03, max_im_factor: float = 0.10, min_im_factor: float = 0.05, liquidation_fee_rate: float = 0.002, fee_cap_pct: float = 0.07) -> StrategyPreview:
    if not options:
        raise ValueError("Option chain is empty")
    expiry = choose_expiry(options, now, target_dte)
    chain = [item for item in options if item.expiry == expiry]
    calls = sorted((item for item in chain if item.option_type == "Call"), key=lambda item: item.strike)
    puts = sorted((item for item in chain if item.option_type == "Put"), key=lambda item: item.strike)
    short_call = _nearest(calls, 0.45)
    short_put = _nearest(puts, 0.45)
    long_calls = [item for item in calls if item.strike > short_call.strike]
    long_puts = [item for item in puts if item.strike < short_put.strike]
    if not long_calls or not long_puts:
        raise ValueError("Unable to find protective wings beyond short strikes")
    long_call = _nearest(long_calls, 0.20)
    long_put = _nearest(long_puts, 0.20)
    legs = [
        StrategyLeg(symbol=short_call.symbol, side="Sell", option_type="Call", strike=short_call.strike, delta=short_call.delta, qty=qty, mark_price=short_call.mark_price, target_delta=0.45),
        StrategyLeg(symbol=short_put.symbol, side="Sell", option_type="Put", strike=short_put.strike, delta=short_put.delta, qty=qty, mark_price=short_put.mark_price, target_delta=0.45),
        StrategyLeg(symbol=long_call.symbol, side="Buy", option_type="Call", strike=long_call.strike, delta=long_call.delta, qty=qty, mark_price=long_call.mark_price, target_delta=0.20),
        StrategyLeg(symbol=long_put.symbol, side="Buy", option_type="Put", strike=long_put.strike, delta=long_put.delta, qty=qty, mark_price=long_put.mark_price, target_delta=0.20),
    ]
    credit = (short_call.mark_price + short_put.mark_price - long_call.mark_price - long_put.mark_price) * qty
    width = max(short_call.strike - long_put.strike, long_call.strike - short_call.strike)
    max_loss = max(0.0, width * qty * contract_multiplier - credit)
    index_price = index_price or max(item.strike for item in chain)
    # Passive BBO estimate: buys at Bid1 and sells at Ask1.
    order_prices = {short_call.symbol: short_call.ask or short_call.mark_price, short_put.symbol: short_put.ask or short_put.mark_price, long_call.symbol: long_call.bid or long_call.mark_price, long_put.symbol: long_put.bid or long_put.mark_price}
    fees = 0.0
    order_im = 0.0
    maintenance_margin = 0.0
    for leg in legs:
        option = next(item for item in chain if item.symbol == leg.symbol)
        order_price = max(0.0, order_prices[leg.symbol])
        fee = min(fee_rate * index_price, fee_cap_pct * order_price) * qty
        leg.estimated_fee_usd = round(fee, 8)
        leg.fee_cap_usd = round(fee_cap_pct * order_price * qty, 8)
        leg.fee_basis_price = round(order_price, 8)
        fees += fee
        if leg.side == "Buy":
            order_im += order_price * qty + fee
            continue
        otm = max(0.0, option.strike - index_price) if leg.option_type == "Call" else max(0.0, index_price - option.strike)
        position_mm = (max(mm_factor * index_price, mm_factor * option.mark_price) + option.mark_price + liquidation_fee_rate * index_price) * qty
        order_im_prime = (max(max_im_factor * index_price - otm, min_im_factor * index_price) + max(order_price, option.mark_price)) * qty
        order_im += max(order_im_prime, position_mm) + fee - order_price * qty
        maintenance_margin += position_mm
    if margin_mode == "PORTFOLIO_MARGIN":
        estimated_margin = max_loss * (1 + margin_buffer_pct)
        margin_status = "PM stress lower-bound estimate; exact account margin is calculated by Bybit"
    else:
        estimated_margin = order_im
        margin_status = "Bybit official option Order IM formula (regular/cross)"
    return StrategyPreview(expiry=expiry, legs=legs, net_credit_usd=round(credit, 2), max_loss_usd=round(max_loss, 2), max_profit_usd=round(max(0.0, credit), 2), risk_reward=round(credit / max_loss, 3) if max_loss else 0, generated_at=now, source="bybit", estimated_margin_usd=round(estimated_margin, 2), estimated_trading_cost_usd=round(fees, 2), estimated_fee_rate=fee_rate, margin_buffer_pct=margin_buffer_pct, estimated_initial_margin_usd=round(order_im, 2), estimated_maintenance_margin_usd=round(maintenance_margin, 2), margin_mode=margin_mode, margin_formula_status=margin_status, fee_cap_pct=fee_cap_pct)


def demo_chain(now: datetime) -> list[OptionInstrument]:
    expiry = (now + timedelta(days=2)).replace(hour=8, minute=0, second=0, microsecond=0)
    spot = 100000.0
    result: list[OptionInstrument] = []
    for offset in range(-18000, 20001, 5000):
        strike = spot + offset
        distance = abs(offset) / 10000
        call_delta = max(0.05, min(0.9, 0.55 - offset / 70000))
        put_delta = max(0.05, min(0.9, 0.55 + offset / 70000))
        for kind, delta, prefix in (("Call", call_delta, "C"), ("Put", put_delta, "P")):
            mark = round(220 - distance * 30, 2)
            symbol = f"BTC-{expiry:%d%b%y}-{int(strike)}-{prefix}"
            result.append(OptionInstrument(symbol=symbol, expiry=expiry, strike=strike, option_type=kind, delta=round(delta, 4), mark_price=mark, bid=mark - 2, ask=mark + 2, iv=0.62, volume=120))
    return result
