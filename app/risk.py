from math import isfinite


def maximum_loss(legs: dict[str, dict]) -> float:
    """Expiry loss for a balanced condor at conservative execution prices."""
    roles = {(leg["option_type"], leg["side"]): leg for leg in legs.values()}
    if len(legs) != 4 or set(roles) != {("Call", "Buy"), ("Call", "Sell"), ("Put", "Buy"), ("Put", "Sell")}:
        raise ValueError("Risk check requires four balanced legs")
    for leg in legs.values():
        if any(not isfinite(leg[key]) or leg[key] <= 0 for key in ("qty", "strike", "price_bound")):
            raise ValueError("Invalid quantity, strike or execution price")
    if len({leg["qty"] for leg in legs.values()}) != 1:
        raise ValueError("Risk check requires equal leg quantities")
    if not roles[("Put", "Buy")]["strike"] < roles[("Put", "Sell")]["strike"] < roles[("Call", "Sell")]["strike"] < roles[("Call", "Buy")]["strike"]:
        raise ValueError("Risk check requires ordered condor strikes")
    qty = next(iter(legs.values()))["qty"]
    width = max(roles[("Call", "Buy")]["strike"] - roles[("Call", "Sell")]["strike"],
                roles[("Put", "Sell")]["strike"] - roles[("Put", "Buy")]["strike"])
    credit = sum((1 if leg["side"] == "Sell" else -1) * leg["qty"] * leg["price_bound"] for leg in legs.values())
    loss = max(0.0, width * qty - credit)
    if not isfinite(loss):
        raise ValueError("Nonfinite combination risk")
    return loss
