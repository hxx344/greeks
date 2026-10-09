from datetime import datetime, timedelta
from math import isfinite

from .models import OptionInstrument, StrategyLeg, StrategyPreview
from .risk import strategy_mode as validate_mode, validate_structure


class SundayExpiryUnavailable(ValueError):
    """The strategy's expiry is not currently available in the chain."""


class StrategyUnavailable(ValueError):
    """Current quotes cannot form the requested structure; market data is still usable."""

    def __init__(self, reason_code: str, message: str, expiry: datetime):
        super().__init__(message)
        self.reason_code = reason_code
        self.expiry = expiry


def _nearest(options: list[OptionInstrument], target: float) -> OptionInstrument:
    return min(options, key=lambda item: abs(abs(item.delta) - target))


def choose_expiry(options: list[OptionInstrument], now: datetime, target_dte: int) -> datetime:
    del target_dte  # Kept for API compatibility; this strategy is calendar-based.
    expiries = sorted({item.expiry for item in options if item.expiry > now and item.expiry.weekday() == 6})
    if not expiries:
        raise SundayExpiryUnavailable("No future Sunday BTC option expiry available")
    return expiries[0]


def build_strategy(options: list[OptionInstrument], now: datetime, target_dte: int = 2, qty: float = 1, contract_multiplier: float = 1.0, fee_rate: float = 0.0003, margin_buffer_pct: float = 0.0, index_price: float = 0.0, margin_mode: str = "REGULAR_MARGIN", mm_factor: float = 0.03, max_im_factor: float = 0.10, min_im_factor: float = 0.05, liquidation_fee_rate: float = 0.002, fee_cap_pct: float = 0.07, strategy_mode: str = "iron_condor") -> StrategyPreview:
    validate_mode(strategy_mode)
    unbounded = strategy_mode == "short_strangle"
    if not isfinite(qty) or qty <= 0:
        raise ValueError("Quantity must be finite and greater than zero")
    options = [item for item in options if isfinite(item.delta) and 0 < abs(item.delta) <= 1 and isfinite(item.mark_price) and item.mark_price > 0 and isfinite(item.strike) and item.strike > 0]
    if not options:
        raise ValueError("Option chain has no usable prices and deltas")
    expiry = choose_expiry(options, now, target_dte)
    chain = [item for item in options if item.expiry == expiry]
    calls = sorted((item for item in chain if item.option_type == "Call"), key=lambda item: item.strike)
    puts = sorted((item for item in chain if item.option_type == "Put"), key=lambda item: item.strike)
    if not calls or not puts:
        raise StrategyUnavailable("missing_option_side", "Selected expiry requires both calls and puts with usable prices and deltas", expiry)
    short_call = _nearest(calls, 0.45)
    short_put = _nearest(puts, 0.45)
    if short_put.strike >= short_call.strike:
        raise StrategyUnavailable("short_strike_order", "Strategy short put strike must be below short call strike", expiry)
    legs = [
        StrategyLeg(symbol=short_call.symbol, side="Sell", option_type="Call", strike=short_call.strike, delta=short_call.delta, qty=qty, mark_price=short_call.mark_price, target_delta=0.45),
        StrategyLeg(symbol=short_put.symbol, side="Sell", option_type="Put", strike=short_put.strike, delta=short_put.delta, qty=qty, mark_price=short_put.mark_price, target_delta=0.45),
    ]
    max_loss = None
    if not unbounded:
        long_calls = [item for item in calls if item.strike > short_call.strike]
        long_puts = [item for item in puts if item.strike < short_put.strike]
        if not long_calls or not long_puts:
            raise StrategyUnavailable("missing_protective_wings", "Unable to find protective wings beyond short strikes", expiry)
        long_call = _nearest(long_calls, 0.10)
        long_put = _nearest(long_puts, 0.10)
        legs.extend([
            StrategyLeg(symbol=long_call.symbol, side="Buy", option_type="Call", strike=long_call.strike, delta=long_call.delta, qty=qty, mark_price=long_call.mark_price, target_delta=0.10),
            StrategyLeg(symbol=long_put.symbol, side="Buy", option_type="Put", strike=long_put.strike, delta=long_put.delta, qty=qty, mark_price=long_put.mark_price, target_delta=0.10),
        ])
    credit = sum((1 if leg.side == "Sell" else -1) * leg.mark_price * qty for leg in legs)
    if not unbounded:
        width = max(long_call.strike - short_call.strike, short_put.strike - long_put.strike)
        max_loss = max(0.0, width * qty * contract_multiplier - credit)
    validate_structure({leg.symbol: {**leg.model_dump(), "expiry": expiry} for leg in legs}, strategy_mode, require_expiry=True)
    if unbounded and (not isfinite(index_price) or index_price <= 0):
        raise ValueError("Short-strangle margin estimate requires a valid BTC underlying price")
    index_price = index_price or max(item.strike for item in chain)
    # Passive BBO estimate: buys at Bid1 and sells at Ask1.
    by_symbol = {item.symbol: item for item in chain}
    order_prices = {leg.symbol: (by_symbol[leg.symbol].bid if leg.side == "Buy" else by_symbol[leg.symbol].ask) or leg.mark_price for leg in legs}
    if any(not isfinite(price) or price <= 0 for price in order_prices.values()):
        raise ValueError("Option chain has invalid execution prices")
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
    if unbounded:
        estimated_margin = order_im * (1 + margin_buffer_pct)
        margin_basis = "regular_order_im"
        margin_status = "Regular option Order IM plus buffer; unbounded loss, not an estimate of portfolio margin"
    elif margin_mode == "PORTFOLIO_MARGIN":
        estimated_margin = max_loss * (1 + margin_buffer_pct)
        margin_basis = "portfolio_loss_estimate"
        margin_status = "PM stress lower-bound estimate; exact account margin is calculated by Bybit"
    else:
        estimated_margin = order_im
        margin_basis = "regular_order_im"
        margin_status = "Bybit official option Order IM formula (regular/cross)"
    if any(not isfinite(value) or value < 0 for value in (estimated_margin, fees, order_im, maintenance_margin)):
        raise ValueError("Option chain has invalid margin estimates")
    return StrategyPreview(strategy_mode=strategy_mode, unbounded_loss=unbounded, expiry=expiry, legs=legs, net_credit_usd=round(credit, 2), max_loss_usd=round(max_loss, 2) if max_loss is not None else None, max_profit_usd=round(max(0.0, credit), 2), risk_reward=None if unbounded else round(credit / max_loss, 3) if max_loss else 0, generated_at=now, source="bybit", estimated_margin_usd=round(estimated_margin, 2), estimated_trading_cost_usd=round(fees, 2), estimated_fee_rate=fee_rate, margin_buffer_pct=margin_buffer_pct, estimated_initial_margin_usd=round(order_im, 2), estimated_maintenance_margin_usd=round(maintenance_margin, 2), margin_mode=margin_mode, margin_basis=margin_basis, margin_formula_status=margin_status, fee_cap_pct=fee_cap_pct)


def build_iron_condor(*args, **kwargs) -> StrategyPreview:
    """Compatibility entry point for callers that always request four legs."""
    return build_strategy(*args, **kwargs, strategy_mode="iron_condor")


def demo_chain(now: datetime) -> list[OptionInstrument]:
    days_until_sunday = (6 - now.weekday()) % 7
    expiry = (now + timedelta(days=days_until_sunday)).replace(hour=8, minute=0, second=0, microsecond=0)
    if expiry <= now:
        expiry += timedelta(days=7)
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
