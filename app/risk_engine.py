"""
All three risk filters live here, isolated from parsing and order submission
so you can unit-test risk logic without touching Discord or Tastytrade.
"""
from __future__ import annotations
from dataclasses import dataclass

from app.config import settings
from app.signal_parser import ParsedSignal


@dataclass
class RiskDecision:
    approved: bool
    reason: str
    contracts: int = 0
    entry_limit_price: float = 0.0
    take_profit_price: float = 0.0
    stop_loss_price: float = 0.0
    slippage_pct: float = 0.0


def check_slippage(signal_price: float, live_price: float) -> tuple[bool, float]:
    """Returns (within_tolerance, slippage_pct)."""
    if signal_price <= 0:
        return False, 0.0
    slippage_pct = abs(live_price - signal_price) / signal_price * 100
    return slippage_pct <= settings.max_slippage_pct, slippage_pct


def size_for_tag(size_tag: str) -> int:
    contracts = settings.size_tag_map.get(size_tag.upper(), settings.default_contracts)
    return min(contracts, settings.max_contracts_hard_cap)


def size_for_budget(live_price: float) -> int:
    """
    Most contracts that fit at or under settings.budget_usd, never over -
    e.g. $300 budget at $0.80/share ($80/contract) buys 3 (floor(300/80)),
    not 4, even though $320 is numerically closer to $300 than $240 is.
    Still capped by the hard cap safety net, same as tag mode.
    """
    cost_per_contract = live_price * 100
    if cost_per_contract <= 0:
        return 0
    contracts = int(settings.budget_usd // cost_per_contract)
    return min(contracts, settings.max_contracts_hard_cap)


def compute_bracket_prices(entry_price: float, action_is_buy: bool) -> tuple[float, float]:
    """
    Returns (take_profit_price, stop_loss_price) for the closing leg, based
    on a % of the entry premium. Long options (BTO): TP above entry, SL below.
    Short options (STO): TP below entry, SL above. Prices rounded to cents.
    """
    tp_frac = settings.take_profit_pct / 100
    sl_frac = settings.stop_loss_pct / 100
    if action_is_buy:
        tp = entry_price * (1 + tp_frac)
        sl = entry_price * (1 - sl_frac)
    else:
        tp = entry_price * (1 - tp_frac)
        sl = entry_price * (1 + sl_frac)
    return round(max(tp, 0.01), 2), round(max(sl, 0.01), 2)


def evaluate(signal: ParsedSignal, live_price: float) -> RiskDecision:
    within_tolerance, slippage_pct = check_slippage(signal.signal_price, live_price)
    if not within_tolerance:
        return RiskDecision(
            approved=False,
            reason=f"Slippage {slippage_pct:.1f}% exceeds max {settings.max_slippage_pct}%",
            slippage_pct=slippage_pct,
        )

    if settings.sizing_mode == "budget":
        contracts = size_for_budget(live_price)
        if contracts <= 0:
            cost_per_contract = live_price * 100
            return RiskDecision(
                approved=False,
                reason=f"Budget mode: ${settings.budget_usd:.0f} budget can't afford even 1 contract at ${cost_per_contract:.2f}/contract",
                slippage_pct=slippage_pct,
            )
    else:
        contracts = size_for_tag(signal.size_tag)
        if contracts <= 0:
            return RiskDecision(approved=False, reason="Computed contract size is 0", slippage_pct=slippage_pct)

    action_is_buy = signal.action.value.startswith("Buy")
    tp_price, sl_price = compute_bracket_prices(live_price, action_is_buy)

    return RiskDecision(
        approved=True,
        reason="OK",
        contracts=contracts,
        entry_limit_price=round(live_price, 2),
        take_profit_price=tp_price,
        stop_loss_price=sl_price,
        slippage_pct=slippage_pct,
    )
