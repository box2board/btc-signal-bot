import logging
import os
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Deque, Dict, List, Optional

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


@dataclass
class Position:
    side: str
    market_ticker: str
    entry_price: float
    entry_time: str


@dataclass
class BotState:
    running: bool = False
    last_poll_time: Optional[str] = None
    last_error: Optional[str] = None
    current_market: Optional[str] = None
    last_signal: str = "SKIP"
    last_reason: str = "Bot has not polled yet."
    position: Optional[Position] = None
    signals: Deque[Dict[str, Any]] = field(default_factory=lambda: deque(maxlen=MAX_SIGNAL_HISTORY))
    market_snapshot: Dict[str, Any] = field(default_factory=dict)


config = Config()
state = BotState()
state_lock = threading.Lock()
app = FastAPI(title="BTC Signal Bot", version="0.1.0")


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


def best_price(levels: Any, pick: str) -> Optional[float]:
    if not isinstance(levels, list):
        return None
    prices: List[float] = []
    for level in levels:
        if isinstance(level, dict):
            price = safe_float(level.get("price"))
        elif isinstance(level, (list, tuple)) and level:
            price = safe_float(level[0])
        else:
            price = None
        if price is not None:
            prices.append(price)
    if not prices:
        return None
    return max(prices) if pick == "max" else min(prices)


def estimate_prices(orderbook: Dict[str, Any]) -> Dict[str, Optional[float]]:
    yes_bids = orderbook.get("yes", []) or orderbook.get("yes_bids", [])
    no_bids = orderbook.get("no", []) or orderbook.get("no_bids", [])
    yes_ask_direct = best_price(orderbook.get("yes_asks"), "min")
    no_ask_direct = best_price(orderbook.get("no_asks"), "min")
    best_no_bid = best_price(no_bids, "max")
    best_yes_bid = best_price(yes_bids, "max")

    yes_buy = yes_ask_direct
    if yes_buy is None and best_no_bid is not None:
        yes_buy = 100 - best_no_bid

    no_buy = no_ask_direct
    if no_buy is None and best_yes_bid is not None:
        no_buy = 100 - best_yes_bid

    spread = None
    if yes_buy is not None and no_buy is not None:
        spread = abs((yes_buy + no_buy) - 100)

    return {
        "yes_buy": yes_buy,
        "no_buy": no_buy,
        "best_yes_bid": best_yes_bid,
        "best_no_bid": best_no_bid,
        "synthetic_spread": spread,
    }


def pct_change(entry_price: float, current_price: float, side: str) -> float:
    if side == "YES":
        return ((current_price - entry_price) / entry_price) * 100
    return ((current_price - entry_price) / entry_price) * 100


def decide_signal(market: Dict[str, Any], pricing: Dict[str, Optional[float]]) -> Dict[str, Any]:
    ticker = str(market.get("ticker") or "unknown")
    close_time = parse_time(market.get("close_time") or market.get("expiration_time"))
    seconds_left = (close_time - utc_now()).total_seconds() if close_time else 0
    yes_buy = pricing.get("yes_buy")
    no_buy = pricing.get("no_buy")
    spread = pricing.get("synthetic_spread")

    if yes_buy is None or no_buy is None:
        return {"action": "SKIP", "reason": "Orderbook did not provide usable yes/no prices."}

    if spread is None or spread > config.max_spread:
        return {"action": "SKIP", "reason": f"Synthetic spread {spread} is above MAX_SPREAD."}

    with state_lock:
        current_position = state.position

    if current_position:
        current_price = yes_buy if current_position.side == "YES" else no_buy
        pnl_pct = pct_change(current_position.entry_price, current_price, current_position.side)
        if pnl_pct >= config.take_profit_pct:
            return {"action": "EXIT", "reason": f"Take profit hit at {pnl_pct:.2f}%.", "price": current_price}
        if pnl_pct <= -config.stop_loss_pct:
            return {"action": "EXIT", "reason": f"Stop loss hit at {pnl_pct:.2f}%.", "price": current_price}
        if seconds_left <= config.force_exit_seconds:
            return {"action": "EXIT", "reason": f"Force exit with {seconds_left:.0f}s left.", "price": current_price}
        return {"action": "HOLD", "reason": f"Position open with {pnl_pct:.2f}% unrealized P/L.", "price": current_price}

    if seconds_left < config.min_seconds_left:
        return {"action": "SKIP", "reason": f"Only {seconds_left:.0f}s left before close."}

    if config.entry_min <= yes_buy <= config.entry_max:
        if yes_buy < no_buy:
            return {"action": "BUY_YES", "reason": f"YES price {yes_buy:.2f} is in range.", "price": yes_buy}
        if no_buy < yes_buy:
            return {"action": "BUY_NO", "reason": f"NO price {no_buy:.2f} is more attractive in range.", "price": no_buy}

    return {"action": "SKIP", "reason": "No entry matched the configured range."}


def record_signal(action: str, reason: str, market_ticker: str, pricing: Dict[str, Optional[float]]) -> None:
    signal = {
        "time": iso_now(),
        "action": action,
        "reason": reason,
        "market_ticker": market_ticker,
        "yes_buy": pricing.get("yes_buy"),
        "no_buy": pricing.get("no_buy"),
        "synthetic_spread": pricing.get("synthetic_spread"),
    }
    with state_lock:
        state.last_signal = action
        state.last_reason = reason
        state.signals.appendleft(signal)
    logger.info("%s %s %s", action, market_ticker, reason)


def apply_signal(signal: Dict[str, Any], market_ticker: str, pricing: Dict[str, Optional[float]]) -> None:
    action = signal["action"]
    reason = signal["reason"]

    with state_lock:
        if action == "BUY_YES":
            state.position = Position(
                side="YES",
                market_ticker=market_ticker,
                entry_price=float(signal.get("price") or pricing.get("yes_buy") or 0),
                entry_time=iso_now(),
            )
        elif action == "BUY_NO":
            state.position = Position(
                side="NO",
                market_ticker=market_ticker,
                entry_price=float(signal.get("price") or pricing.get("no_buy") or 0),
                entry_time=iso_now(),
            )
        elif action == "EXIT":
            state.position = None

    record_signal(action, reason, market_ticker, pricing)


def poll_once() -> None:
    markets = fetch_series_markets(config.series_ticker)
    market = choose_active_market(markets, config.min_seconds_left)

    if not market:
        with state_lock:
            state.current_market = None
            state.market_snapshot = {}
        record_signal("SKIP", "No open market matched the time requirements.", "none", {})
        return

    market_ticker = str(market.get("ticker") or "unknown")
    orderbook = fetch_orderbook(market_ticker)
    pricing = estimate_prices(orderbook)
    signal = decide_signal(market, pricing)

    with state_lock:
        state.current_market = market_ticker
        state.market_snapshot = {
            "market_ticker": market_ticker,
            "close_time": market.get("close_time") or market.get("expiration_time"),
            "yes_buy": pricing.get("yes_buy"),
            "no_buy": pricing.get("no_buy"),
            "synthetic_spread": pricing.get("synthetic_spread"),
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
            "position": asdict(state.position) if state.position else None,
            "market_snapshot": state.market_snapshot,
            "recent_signals": list(state.signals),
        }
