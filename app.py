import logging
import os
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Deque, Dict, List, Optional, Tuple

import requests
from fastapi import FastAPI


logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("btc-signal-bot")

API_BASE = "https://api.elections.kalshi.com/trade-api/v2"
DEFAULT_SERIES = "KXBTC15M"
MAX_SIGNAL_HISTORY = 50
MAX_CLOSED_PAPER_TRADES = 10
REQUEST_TIMEOUT = 10


@dataclass
class Config:
    series_ticker: str = os.getenv("SERIES_TICKER", DEFAULT_SERIES)
    poll_seconds: int = int(os.getenv("POLL_SECONDS", "10"))
    entry_min: float = float(os.getenv("ENTRY_MIN", "35"))
    entry_max: float = float(os.getenv("ENTRY_MAX", "65"))
    take_profit_pct: float = float(os.getenv("TAKE_PROFIT_PCT", "8"))
    stop_loss_pct: float = float(os.getenv("STOP_LOSS_PCT", "5"))
    min_seconds_left: int = int(os.getenv("MIN_SECONDS_LEFT", "180"))
    force_exit_seconds: int = int(os.getenv("FORCE_EXIT_SECONDS", "45"))
    max_spread: float = float(os.getenv("MAX_SPREAD", "8"))
    paper_trading_enabled: bool = True


@dataclass
class PaperPosition:
    side: str
    market_ticker: str
    entry_price: float
    entry_time: str


@dataclass
class PaperTrade:
    side: str
    market_ticker: str
    entry_price: float
    exit_price: float
    entry_time: str
    exit_time: str
    exit_reason: str
    pnl_dollars: float
    pnl_percent: float
    outcome: str


@dataclass
class PaperStats:
    total_paper_trades: int = 0
    wins: int = 0
    losses: int = 0
    cumulative_pnl_dollars: float = 0.0
    cumulative_pnl_percent: float = 0.0


@dataclass
class BotState:
    running: bool = False
    last_poll_time: Optional[str] = None
    last_error: Optional[str] = None
    current_market: Optional[str] = None
    last_signal: str = "SKIP"
    last_reason: str = "Bot has not polled yet."
    last_skip_reason: Optional[str] = None
    last_diagnostics: Dict[str, Any] = field(default_factory=dict)
    paper_position: Optional[PaperPosition] = None
    paper_stats: PaperStats = field(default_factory=PaperStats)
    closed_paper_trades: Deque[PaperTrade] = field(
        default_factory=lambda: deque(maxlen=MAX_CLOSED_PAPER_TRADES)
    )
    signals: Deque[Dict[str, Any]] = field(default_factory=lambda: deque(maxlen=MAX_SIGNAL_HISTORY))
    market_snapshot: Dict[str, Any] = field(default_factory=dict)


config = Config()
state = BotState()
state_lock = threading.Lock()
app = FastAPI(title="BTC Signal Bot", version="0.2.0")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utc_now().isoformat()


def parse_time(value: Any) -> Optional[datetime]:
    if not value or not isinstance(value, str):
        return None
    cleaned = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(cleaned)
    except ValueError:
        return None


def safe_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def fetch_json(path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    response = requests.get(f"{API_BASE}{path}", params=params, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("Kalshi API returned a non-dict payload")
    return payload


def fetch_series_markets(series_ticker: str) -> List[Dict[str, Any]]:
    payload = fetch_json("/markets", params={"series_ticker": series_ticker, "limit": 100})
    markets = payload.get("markets", [])
    return markets if isinstance(markets, list) else []


def choose_active_market(markets: List[Dict[str, Any]], min_seconds_left: int) -> Optional[Dict[str, Any]]:
    now = utc_now()
    candidates = []
    for market in markets:
        if not isinstance(market, dict):
            continue
        status = str(market.get("status") or "").lower()
        close_time = parse_time(market.get("close_time") or market.get("expiration_time"))
        if status not in {"open", "active", "initialized"} or not close_time:
            continue
        seconds_left = (close_time - now).total_seconds()
        if seconds_left < min_seconds_left:
            continue
        candidates.append((seconds_left, market))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0])
    return candidates[0][1]


def fetch_orderbook(market_ticker: str) -> Dict[str, Any]:
    payload = fetch_json(f"/markets/{market_ticker}/orderbook")
    orderbook = payload.get("orderbook", payload)
    return orderbook if isinstance(orderbook, dict) else {}


def extract_price_quantity(level: Any) -> Tuple[Optional[float], Optional[float]]:
    if isinstance(level, dict):
        price = safe_float(level.get("price") or level.get("yes_price") or level.get("no_price"))
        quantity = safe_float(level.get("quantity") or level.get("qty") or level.get("size") or level.get("count"))
        return price, quantity
    if isinstance(level, (list, tuple)):
        if not level:
            return None, None
        price = safe_float(level[0])
        quantity = safe_float(level[1]) if len(level) > 1 else None
        return price, quantity
    return None, None


def summarize_levels(levels: Any, limit: int = 3) -> List[Dict[str, Optional[float]]]:
    if not isinstance(levels, list):
        return []
    summary: List[Dict[str, Optional[float]]] = []
    for level in levels[:limit]:
        price, quantity = extract_price_quantity(level)
        if price is None and quantity is None:
            continue
        summary.append({"price": price, "quantity": quantity})
    return summary


def best_price(levels: Any, pick: str) -> Optional[float]:
    if not isinstance(levels, list):
        return None
    prices: List[float] = []
    for level in levels:
        price, _quantity = extract_price_quantity(level)
        if price is not None:
            prices.append(price)
    if not prices:
        return None
    return max(prices) if pick == "max" else min(prices)


def build_orderbook_diagnostics(orderbook: Dict[str, Any]) -> Dict[str, Any]:
    yes_levels = orderbook.get("yes") or orderbook.get("yes_bids")
    no_levels = orderbook.get("no") or orderbook.get("no_bids")
    yes_asks = orderbook.get("yes_asks")
    no_asks = orderbook.get("no_asks")

    return {
        "has_orderbook": bool(orderbook),
        "available_keys": sorted(orderbook.keys()),
        "yes_side_present": yes_levels is not None or yes_asks is not None,
        "no_side_present": no_levels is not None or no_asks is not None,
        "yes_bid_count": len(yes_levels) if isinstance(yes_levels, list) else 0,
        "no_bid_count": len(no_levels) if isinstance(no_levels, list) else 0,
        "yes_ask_count": len(yes_asks) if isinstance(yes_asks, list) else 0,
        "no_ask_count": len(no_asks) if isinstance(no_asks, list) else 0,
        "top_of_book": {
            "yes_bids": summarize_levels(yes_levels),
            "no_bids": summarize_levels(no_levels),
            "yes_asks": summarize_levels(yes_asks),
            "no_asks": summarize_levels(no_asks),
        },
    }


def estimate_prices(orderbook: Dict[str, Any]) -> Dict[str, Any]:
    diagnostics = build_orderbook_diagnostics(orderbook)

    if not orderbook:
        diagnostics["skip_reason"] = "missing orderbook"
        return {"valid": False, "diagnostics": diagnostics}

    yes_bids = orderbook.get("yes") or orderbook.get("yes_bids")
    no_bids = orderbook.get("no") or orderbook.get("no_bids")

    if yes_bids is None and orderbook.get("yes_asks") is None:
        diagnostics["skip_reason"] = "missing yes side"
        return {"valid": False, "diagnostics": diagnostics}

    if no_bids is None and orderbook.get("no_asks") is None:
        diagnostics["skip_reason"] = "missing no side"
        return {"valid": False, "diagnostics": diagnostics}

    yes_ask_direct = best_price(orderbook.get("yes_asks"), "min")
    no_ask_direct = best_price(orderbook.get("no_asks"), "min")
    best_yes_bid = best_price(yes_bids, "max")
    best_no_bid = best_price(no_bids, "max")

    if yes_ask_direct is None and no_ask_direct is None and best_yes_bid is None and best_no_bid is None:
        diagnostics["skip_reason"] = "no usable bid levels"
        return {"valid": False, "diagnostics": diagnostics}

    yes_buy = yes_ask_direct
    if yes_buy is None and best_no_bid is not None:
        yes_buy = 100 - best_no_bid

    no_buy = no_ask_direct
    if no_buy is None and best_yes_bid is not None:
        no_buy = 100 - best_yes_bid

    spread = None
    if yes_buy is not None and no_buy is not None:
        spread = abs((yes_buy + no_buy) - 100)

    diagnostics.update(
        {
            "best_yes_bid": best_yes_bid,
            "best_no_bid": best_no_bid,
            "yes_buy": yes_buy,
            "no_buy": no_buy,
            "synthetic_spread": spread,
        }
    )

    if yes_buy is None or no_buy is None:
        diagnostics["skip_reason"] = "no usable bid levels"
        return {"valid": False, "diagnostics": diagnostics}

    if yes_buy < 0 or yes_buy > 100 or no_buy < 0 or no_buy > 100:
        diagnostics["skip_reason"] = "synthetic price invalid"
        return {"valid": False, "diagnostics": diagnostics}

    return {
        "valid": True,
        "yes_buy": yes_buy,
        "no_buy": no_buy,
        "best_yes_bid": best_yes_bid,
        "best_no_bid": best_no_bid,
        "synthetic_spread": spread,
        "diagnostics": diagnostics,
    }


def pct_change(entry_price: float, current_price: float) -> float:
    return ((current_price - entry_price) / entry_price) * 100


def decide_signal(market: Dict[str, Any], pricing: Dict[str, Any]) -> Dict[str, Any]:
    close_time = parse_time(market.get("close_time") or market.get("expiration_time"))
    seconds_left = (close_time - utc_now()).total_seconds() if close_time else 0
    diagnostics = dict(pricing.get("diagnostics") or {})
    yes_buy = pricing.get("yes_buy")
    no_buy = pricing.get("no_buy")
    spread = pricing.get("synthetic_spread")

    if not pricing.get("valid"):
        return {
            "action": "SKIP",
            "reason": diagnostics.get("skip_reason", "orderbook parsing failed"),
            "diagnostics": diagnostics,
        }

    if spread is None:
        diagnostics["skip_reason"] = "synthetic price invalid"
        return {"action": "SKIP", "reason": "synthetic price invalid", "diagnostics": diagnostics}

    if spread > config.max_spread:
        diagnostics["skip_reason"] = "spread too wide"
        return {
            "action": "SKIP",
            "reason": f"spread too wide ({spread:.2f} > {config.max_spread:.2f})",
            "diagnostics": diagnostics,
        }

    with state_lock:
        current_position = state.paper_position

    if current_position:
        current_price = yes_buy if current_position.side == "YES" else no_buy
        pnl_pct = pct_change(current_position.entry_price, current_price)
        diagnostics["open_position_pnl_percent"] = round(pnl_pct, 4)
        if pnl_pct >= config.take_profit_pct:
            return {
                "action": "EXIT",
                "reason": f"Take profit hit at {pnl_pct:.2f}%.",
                "price": current_price,
                "diagnostics": diagnostics,
            }
        if pnl_pct <= -config.stop_loss_pct:
            return {
                "action": "EXIT",
                "reason": f"Stop loss hit at {pnl_pct:.2f}%.",
                "price": current_price,
                "diagnostics": diagnostics,
            }
        if seconds_left <= config.force_exit_seconds:
            return {
                "action": "EXIT",
                "reason": f"Force exit with {seconds_left:.0f}s left.",
                "price": current_price,
                "diagnostics": diagnostics,
            }
        return {
            "action": "HOLD",
            "reason": f"Paper position open with {pnl_pct:.2f}% unrealized P/L.",
            "price": current_price,
            "diagnostics": diagnostics,
        }

    if seconds_left < config.min_seconds_left:
        diagnostics["skip_reason"] = "too close to market close"
        return {
            "action": "SKIP",
            "reason": f"too close to market close ({seconds_left:.0f}s left)",
            "diagnostics": diagnostics,
        }

    if config.entry_min <= yes_buy <= config.entry_max:
        if yes_buy < no_buy:
            return {
                "action": "BUY_YES",
                "reason": f"YES price {yes_buy:.2f} is in range.",
                "price": yes_buy,
                "diagnostics": diagnostics,
            }
        if no_buy < yes_buy:
            return {
                "action": "BUY_NO",
                "reason": f"NO price {no_buy:.2f} is more attractive in range.",
                "price": no_buy,
                "diagnostics": diagnostics,
            }

    diagnostics["skip_reason"] = "no entry in range"
    return {"action": "SKIP", "reason": "no entry in range", "diagnostics": diagnostics}


def paper_trade_summary(trade: PaperTrade) -> Dict[str, Any]:
    return asdict(trade)


def current_open_position_summary() -> Optional[Dict[str, Any]]:
    if not state.paper_position:
        return None
    return asdict(state.paper_position)


def current_paper_stats_summary() -> Dict[str, Any]:
    return {
        **asdict(state.paper_stats),
        "open_paper_position": current_open_position_summary(),
    }


def record_signal(action: str, reason: str, market_ticker: str, pricing: Dict[str, Any], diagnostics: Optional[Dict[str, Any]] = None) -> None:
    signal = {
        "time": iso_now(),
        "action": action,
        "reason": reason,
        "market_ticker": market_ticker,
        "yes_buy": pricing.get("yes_buy"),
        "no_buy": pricing.get("no_buy"),
        "synthetic_spread": pricing.get("synthetic_spread"),
        "diagnostics": diagnostics or {},
    }
    with state_lock:
        state.last_signal = action
        state.last_reason = reason
        state.last_skip_reason = reason if action == "SKIP" else None
        state.last_diagnostics = diagnostics or {}
        state.signals.appendleft(signal)
    logger.info("%s %s %s diagnostics=%s", action, market_ticker, reason, diagnostics or {})


def close_paper_position(position: PaperPosition, exit_price: float, exit_reason: str) -> PaperTrade:
    pnl_dollars = exit_price - position.entry_price
    pnl_percent = pct_change(position.entry_price, exit_price)
    outcome = "win" if pnl_dollars > 0 else "loss" if pnl_dollars < 0 else "flat"
    return PaperTrade(
        side=position.side,
        market_ticker=position.market_ticker,
        entry_price=position.entry_price,
        exit_price=exit_price,
        entry_time=position.entry_time,
        exit_time=iso_now(),
        exit_reason=exit_reason,
        pnl_dollars=round(pnl_dollars, 4),
        pnl_percent=round(pnl_percent, 4),
        outcome=outcome,
    )


def apply_signal(signal: Dict[str, Any], market_ticker: str, pricing: Dict[str, Any]) -> None:
    action = signal["action"]
    reason = signal["reason"]
    diagnostics = signal.get("diagnostics") or pricing.get("diagnostics") or {}

    with state_lock:
        if action == "BUY_YES":
            state.paper_position = PaperPosition(
                side="YES",
                market_ticker=market_ticker,
                entry_price=float(signal.get("price") or pricing.get("yes_buy") or 0),
                entry_time=iso_now(),
            )
        elif action == "BUY_NO":
            state.paper_position = PaperPosition(
                side="NO",
                market_ticker=market_ticker,
                entry_price=float(signal.get("price") or pricing.get("no_buy") or 0),
                entry_time=iso_now(),
            )
        elif action == "EXIT" and state.paper_position:
            trade = close_paper_position(
                position=state.paper_position,
                exit_price=float(signal.get("price") or 0),
                exit_reason=reason,
            )
            state.paper_stats.total_paper_trades += 1
            if trade.outcome == "win":
                state.paper_stats.wins += 1
            elif trade.outcome == "loss":
                state.paper_stats.losses += 1
            state.paper_stats.cumulative_pnl_dollars = round(
                state.paper_stats.cumulative_pnl_dollars + trade.pnl_dollars,
                4,
            )
            state.paper_stats.cumulative_pnl_percent = round(
                state.paper_stats.cumulative_pnl_percent + trade.pnl_percent,
                4,
            )
            state.closed_paper_trades.appendleft(trade)
            state.paper_position = None

    record_signal(action, reason, market_ticker, pricing, diagnostics)


def poll_once() -> None:
    markets = fetch_series_markets(config.series_ticker)
    market = choose_active_market(markets, config.min_seconds_left)

    if not market:
        diagnostics = {"skip_reason": "too close to market close", "market_selection": "no eligible open market"}
        with state_lock:
            state.current_market = None
            state.market_snapshot = {}
        record_signal("SKIP", "No open market matched the time requirements.", "none", {}, diagnostics)
        return

    market_ticker = str(market.get("ticker") or "unknown")
    orderbook = fetch_orderbook(market_ticker)
    pricing = estimate_prices(orderbook)
    signal = decide_signal(market, pricing)
    diagnostics = signal.get("diagnostics") or pricing.get("diagnostics") or {}

    with state_lock:
        state.current_market = market_ticker
        state.market_snapshot = {
            "market_ticker": market_ticker,
            "close_time": market.get("close_time") or market.get("expiration_time"),
            "yes_buy": pricing.get("yes_buy"),
            "no_buy": pricing.get("no_buy"),
            "synthetic_spread": pricing.get("synthetic_spread"),
            "orderbook_diagnostics": diagnostics,
        }

    apply_signal(signal, market_ticker, pricing)


def bot_loop() -> None:
    logger.info("Starting bot loop for %s", config.series_ticker)
    while True:
        with state_lock:
            state.running = True
            state.last_poll_time = iso_now()
        try:
            poll_once()
            with state_lock:
                state.last_error = None
        except Exception as exc:  # Defensive so the thread stays alive in production.
            logger.exception("Polling failed")
            with state_lock:
                state.last_error = str(exc)
                state.last_signal = "SKIP"
                state.last_reason = f"Polling error: {exc}"
                state.last_skip_reason = f"Polling error: {exc}"
                state.last_diagnostics = {"skip_reason": "polling error"}
        time.sleep(max(2, config.poll_seconds))


@app.on_event("startup")
def startup_event() -> None:
    worker = threading.Thread(target=bot_loop, name="signal-bot", daemon=True)
    worker.start()


@app.get("/")
def home() -> Dict[str, Any]:
    with state_lock:
        return {
            "name": "btc-signal-bot",
            "status": "running" if state.running else "starting",
            "series_ticker": config.series_ticker,
            "last_signal": state.last_signal,
            "last_reason": state.last_reason,
            "current_market": state.current_market,
            "paper_trading_enabled": config.paper_trading_enabled,
        }


@app.get("/status")
def status() -> Dict[str, Any]:
    with state_lock:
        return {
            "config": asdict(config),
            "running": state.running,
            "last_poll_time": state.last_poll_time,
            "last_error": state.last_error,
            "current_market": state.current_market,
            "last_signal": state.last_signal,
            "last_reason": state.last_reason,
            "last_skip_reason": state.last_skip_reason,
            "last_diagnostics": state.last_diagnostics,
            "paper_trading_enabled": config.paper_trading_enabled,
            "paper_stats": current_paper_stats_summary(),
            "open_paper_position": current_open_position_summary(),
            "recent_closed_paper_trades": [paper_trade_summary(trade) for trade in state.closed_paper_trades],
            "market_snapshot": state.market_snapshot,
            "recent_signals": list(state.signals),
        }


@app.get("/paper")
def paper() -> Dict[str, Any]:
    with state_lock:
        return {
            "paper_trading_enabled": config.paper_trading_enabled,
            "paper_stats": current_paper_stats_summary(),
            "open_paper_position": current_open_position_summary(),
            "recent_closed_paper_trades": [paper_trade_summary(trade) for trade in state.closed_paper_trades],
        }
