from datetime import datetime
from math import isfinite


def strategy_mode(value="iron_condor") -> str:
    if value not in {"iron_condor", "short_strangle"}:
        raise ValueError("Unknown strategy mode")
    return value


def leg_count(mode="iron_condor") -> int:
    return 2 if strategy_mode(mode) == "short_strangle" else 4


def validate_structure(legs: dict[str, dict], mode="iron_condor", *, require_expiry=False) -> dict:
    """Validate the complete original structure, never a set of residual fills."""
    mode = strategy_mode(mode)
    if not isinstance(legs, dict) or any(not isinstance(leg, dict) for leg in legs.values()):
        raise ValueError("Invalid strategy leg structure")
    roles = {(leg.get("option_type"), leg.get("side")): leg for leg in legs.values()}
    expected = {("Call", "Sell"), ("Put", "Sell")}
    if mode == "iron_condor":
        expected |= {("Call", "Buy"), ("Put", "Buy")}
    if len(legs) != leg_count(mode) or set(roles) != expected:
        raise ValueError("Risk check requires four balanced legs" if mode == "iron_condor" else "Risk check requires exactly two short Call/Put legs")
    for leg in legs.values():
        if any(not isinstance(leg.get(key), (int, float)) or isinstance(leg[key], bool)
               or not isfinite(leg[key]) or leg[key] <= 0 for key in ("qty", "strike")):
            raise ValueError("Invalid quantity or strike")
    if len({leg["qty"] for leg in legs.values()}) != 1:
        raise ValueError("Risk check requires equal leg quantities")
    if mode == "iron_condor" and not roles[("Put", "Buy")]["strike"] < roles[("Put", "Sell")]["strike"] < roles[("Call", "Sell")]["strike"] < roles[("Call", "Buy")]["strike"]:
        raise ValueError("Risk check requires ordered condor strikes")
    if roles[("Put", "Sell")]["strike"] >= roles[("Call", "Sell")]["strike"]:
        raise ValueError("Short put strike must be below short call strike")
    if require_expiry or any("expiry" in leg for leg in legs.values()):
        try:
            expiries = [leg["expiry"] if isinstance(leg["expiry"], datetime) else datetime.fromisoformat(leg["expiry"]) for leg in legs.values()]
        except (KeyError, ValueError, TypeError) as exc:
            raise ValueError("Risk check requires a known common Sunday expiry") from exc
        if len(set(expiries)) != 1 or any(expiry.tzinfo is None or expiry.weekday() != 6 for expiry in expiries):
            raise ValueError("Risk check requires one common Sunday expiry")
    return roles


def maximum_loss(legs: dict[str, dict]) -> float:
    """Expiry loss for a balanced condor at conservative execution prices."""
    roles = validate_structure(legs)
    for leg in legs.values():
        if not isfinite(leg["price_bound"]) or leg["price_bound"] <= 0:
            raise ValueError("Invalid execution price")
    qty = next(iter(legs.values()))["qty"]
    width = max(roles[("Call", "Buy")]["strike"] - roles[("Call", "Sell")]["strike"],
                roles[("Put", "Sell")]["strike"] - roles[("Put", "Buy")]["strike"])
    credit = sum((1 if leg["side"] == "Sell" else -1) * leg["qty"] * leg["price_bound"] for leg in legs.values())
    loss = max(0.0, width * qty - credit)
    if not isfinite(loss):
        raise ValueError("Nonfinite combination risk")
    return loss


def short_order_margin(leg: dict, context: dict, price: float) -> float:
    """Regular option Order IM at one bounded sell price, including fees."""
    fields = ("index_price", "fee_rate", "fee_cap_pct", "mm_factor", "max_im_factor", "min_im_factor", "liquidation_fee_rate", "margin_buffer_pct")
    if any(not isinstance(context.get(key), (int, float)) or isinstance(context[key], bool)
           or not isfinite(context[key]) or context[key] < 0 for key in fields) or context["index_price"] <= 0:
        raise ValueError("Short-strangle budget requires a valid underlying price and margin parameters")
    limits = {"fee_rate": 0.1, "fee_cap_pct": 1, "mm_factor": 1, "max_im_factor": 1,
              "min_im_factor": 1, "liquidation_fee_rate": 1, "margin_buffer_pct": 2}
    if any(context[key] > limit for key, limit in limits.items()):
        raise ValueError("Short-strangle margin parameters are outside supported bounds")
    if not isfinite(price) or price <= 0 or not isfinite(leg.get("mark_price", float("nan"))) or leg["mark_price"] <= 0:
        raise ValueError("Short-strangle budget requires valid mark and execution prices")
    index, mark, qty = context["index_price"], leg["mark_price"], leg["qty"]
    otm = max(0.0, leg["strike"] - index) if leg["option_type"] == "Call" else max(0.0, index - leg["strike"])
    position_mm = (max(context["mm_factor"] * index, context["mm_factor"] * mark) + mark + context["liquidation_fee_rate"] * index) * qty
    order_im_prime = (max(context["max_im_factor"] * index - otm, context["min_im_factor"] * index) + max(price, mark)) * qty
    fee = min(context["fee_rate"] * index, context["fee_cap_pct"] * price) * qty
    value = max(order_im_prime, position_mm) + fee - price * qty
    if not isfinite(value) or value < 0:
        raise ValueError("Nonfinite short-strangle margin requirement")
    return value


def reserve_short_margin(legs: dict[str, dict], context: dict) -> tuple[dict[str, dict], float]:
    """Reserve each leg's worst observed IM; favorable prices never free it."""
    validate_structure(legs, "short_strangle", require_expiry=True)
    proposed = {symbol: dict(leg) for symbol, leg in legs.items()}
    for leg in proposed.values():
        price = leg["price_bound"]
        floor, ceiling = leg.get("price_floor", price), leg.get("price_ceiling", price)
        if any(not isfinite(value) or value <= 0 for value in (price, floor, ceiling)) or not floor <= price <= ceiling:
            raise ValueError("Invalid short-strangle execution price bounds")
        old = leg.get("margin_bound", 0.0)
        if not isfinite(old) or old < 0:
            raise ValueError("Invalid reserved margin")
        # IM is piecewise decreasing then increasing in a short order's price.
        # Both endpoints cover its worst value over every previously used price.
        leg["margin_bound"] = max(old, short_order_margin(leg, context, floor), short_order_margin(leg, context, ceiling))
        leg.update(price_floor=floor, price_ceiling=ceiling)
    total = sum(leg["margin_bound"] for leg in proposed.values()) * (1 + context["margin_buffer_pct"])
    if not isfinite(total):
        raise ValueError("Nonfinite short-strangle margin budget")
    return proposed, total
