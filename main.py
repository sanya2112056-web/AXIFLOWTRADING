import asyncio
import os
import time
import random
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import httpx

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("axiflow")

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT  = os.environ.get("TELEGRAM_CHAT_ID", "")

SYMBOLS = [
    "BTCUSDT","ETHUSDT","SOLUSDT","BNBUSDT","XRPUSDT",
    "ADAUSDT","DOGEUSDT","AVAXUSDT","DOTUSDT","LINKUSDT",
]

# Global cache
cache = {}


async def fetch(url, params=None):
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            r = await c.get(url, params=params)
            r.raise_for_status()
            return r.json()
    except Exception as e:
        log.debug(f"fetch {url}: {e}")
        return None


async def analyze_symbol(sym):
    base = "https://fapi.binance.com"
    try:
        ticker = await fetch(f"{base}/fapi/v1/ticker/24hr", {"symbol": sym})
        oi     = await fetch(f"{base}/fapi/v1/openInterest", {"symbol": sym})
        fr     = await fetch(f"{base}/fapi/v1/premiumIndex", {"symbol": sym})

        price  = float(ticker["lastPrice"]) if ticker else 0
        change = float(ticker["priceChangePercent"]) if ticker else 0
        oi_val = float(oi["openInterest"]) if oi else 0
        fr_val = float(fr["lastFundingRate"]) if fr else 0

        # Simple scoring
        score = 0.0
        reasons = []

        if change > 2:
            score += 1.0
            reasons.append(f"Ціна ↑ {change:.1f}%")
        elif change < -2:
            score -= 1.0
            reasons.append(f"Ціна ↓ {change:.1f}%")

        if fr_val > 0.01:
            score -= 1.0
            reasons.append(f"Funding перекупленість {fr_val*100:.4f}%")
        elif fr_val < -0.01:
            score += 1.0
            reasons.append(f"Funding перепроданість {fr_val*100:.4f}%")

        if score >= 1.5:
            decision = "LONG"
            conf = min(90, 50 + int(abs(score) * 15))
        elif score <= -1.5:
            decision = "SHORT"
            conf = min(90, 50 + int(abs(score) * 15))
        else:
            decision = "NO TRADE"
            conf = 0

        atr = price * 0.015
        if decision == "LONG":
            tp = price + atr * 4
            sl = price - atr
        elif decision == "SHORT":
            tp = price - atr * 4
            sl = price + atr
        else:
            tp = sl = price

        result = {
            "symbol": sym,
            "decision": decision,
            "confidence": conf,
            "strategy": "STANDARD",
            "score": round(score, 2),
            "entry": price,
            "tp": round(tp, 6),
            "sl": round(sl, 6),
            "rr": 4.0 if decision != "NO TRADE" else 0,
            "lev": 2 if conf >= 70 else 1,
            "reasons": reasons or ["Недостатньо сигналів"],
            "raw": {
                "price": price,
                "change": change,
                "oi": oi_val,
                "funding": fr_val,
                "liq": 1.0,
                "ob": 0.0,
            },
            "ts": time.time(),
        }

        cache[sym] = result

        if decision != "NO TRADE":
            log.info(f"[{sym}] {decision} conf={conf}% score={score:.1f}")
            if decision != "NO TRADE" and conf >= 70:
                await notify(
                    f"{'🟢' if decision=='LONG' else '🔴'} *{decision}* `{sym}`\n\n"
                    f"💲 Вхід: `${price:,.4f}`\n"
                    f"🎯 TP: `${tp:,.4f}`\n"
                    f"🛑 SL: `${sl:,.4f}`\n"
                    f"📊 Конф.: `{conf}%`\n"
                    f"📐 RR: `1:4`\n\n"
                    + "\n".join(f"• {r}" for r in reasons)
                )

        return result
    except Exception as e:
        log.error(f"analyze {sym}: {e}")
        return {"symbol": sym, "decision": "NO TRADE", "confidence": 0,
                "score": 0, "entry": 0, "tp": 0, "sl": 0, "rr": 0, "lev": 1,
                "reasons": ["Помилка"], "raw": {"price": 0}, "ts": time.time(),
                "strategy": "STANDARD"}


async def notify(text):
    if not TG_TOKEN or not TG_CHAT:
        return
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            await c.post(
                f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                json={"chat_id": TG_CHAT, "text": text,
                      "parse_mode": "Markdown",
                      "disable_web_page_preview": True}
            )
    except Exception as e:
        log.warning(f"TG: {e}")


async def scan_loop():
    await notify("🟢 *AXIFLOW запущено*\n\nСканую ринок безперервно...")
    while True:
        try:
            for sym in SYMBOLS:
                await analyze_symbol(sym)
                await asyncio.sleep(2)
        except Exception as e:
            log.error(f"scan loop: {e}")
        await asyncio.sleep(30)


@asynccontextmanager
async def lifespan(app: FastAPI):
    asyncio.create_task(scan_loop())
    yield


app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Static files
static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
os.makedirs(static_dir, exist_ok=True)
app.mount("/static", StaticFiles(directory=static_dir, html=True), name="static")


@app.get("/")
async def root():
    return {"status": "ok", "service": "AXIFLOW"}


@app.get("/api/signals")
async def signals():
    return {"signals": cache, "ts": time.time()}


@app.get("/api/signal/{symbol}")
async def signal(symbol: str):
    sym = symbol.upper()
    if sym not in cache:
        result = await analyze_symbol(sym)
        return result
    return cache[sym]


@app.get("/api/market/{symbol}")
async def market(symbol: str):
    sym = symbol.upper()
    base = "https://fapi.binance.com"
    ticker = await fetch(f"{base}/fapi/v1/ticker/24hr", {"symbol": sym})
    oi     = await fetch(f"{base}/fapi/v1/openInterest", {"symbol": sym})
    fr     = await fetch(f"{base}/fapi/v1/premiumIndex", {"symbol": sym})
    return {
        "symbol": sym,
        "price":  float(ticker["lastPrice"]) if ticker else 0,
        "change": float(ticker["priceChangePercent"]) if ticker else 0,
        "oi":     {"delta_15m": 0, "current": float(oi["openInterest"]) if oi else 0, "strength": 0},
        "funding":{"rate": float(fr["lastFundingRate"]) if fr else 0},
        "liquidations": {"ratio": 1.0, "strength": 0},
        "orderbook":    {"imbalance": 0.0, "strength": 0},
        "ts": time.time(),
    }


@app.get("/api/klines/{symbol}")
async def klines(symbol: str, interval: str = "5m", limit: int = 80):
    sym = symbol.upper()
    data = await fetch(
        "https://fapi.binance.com/fapi/v1/klines",
        {"symbol": sym, "interval": interval, "limit": limit}
    )
    if not data:
        return {"symbol": sym, "interval": interval, "candles": []}
    candles = [{"o": float(x[1]), "h": float(x[2]), "l": float(x[3]),
                "c": float(x[4]), "v": float(x[5]), "t": int(x[0])} for x in data]
    return {"symbol": sym, "interval": interval, "candles": candles}


wallets = {}
trades  = []


@app.post("/api/wallet")
async def wallet(data: dict):
    uid = data.get("user_id", "default")
    bal = data.get("balance", 10000.0)
    wallets[uid] = {"balance": bal, "connected": True,
                    "demo": not data.get("bybit_key"), "exchange": data.get("exchange", "demo")}
    return {"success": True, "balance": bal, "demo": wallets[uid]["demo"]}


@app.get("/api/wallet/{user_id}")
async def get_wallet(user_id: str):
    return wallets.get(user_id, {"connected": False})


@app.post("/api/agent")
async def agent(data: dict):
    action = data.get("action")
    if action == "status":
        return {"running": True, "scans": len(cache), "open_count": 0,
                "win_rate": 0, "total_pnl": 0, "open_trades": [], "closed_trades": []}
    if action == "start":
        await notify("🟢 *AXIFLOW Agent запущено*\nСканую ринок...")
        return {"success": True, "msg": "Агент активний"}
    if action == "stop":
        return {"success": True, "msg": "Агент зупинений"}
    return {"success": False}


@app.post("/api/trade")
async def trade(data: dict):
    t = {**data, "id": f"T{int(time.time())}", "ts": time.time(), "demo": True}
    trades.append(t)
    return {"success": True, "trade": t}


@app.get("/api/trades/{user_id}")
async def get_trades(user_id: str):
    return {"trades": trades[-20:]}
