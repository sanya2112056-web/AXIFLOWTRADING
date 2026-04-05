"""
AXIFLOW TRADE - Main Server
FastAPI server for Mini App and exchange API.
Does NOT scan symbols independently - that is bot.py's job.
Pure ASCII - no latin-1 errors.
"""
import asyncio, os, time, logging
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Optional
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import httpx

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("axiflow_server")

TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT  = os.environ.get("TELEGRAM_CHAT_ID", "")
PORT     = int(os.environ.get("PORT", 3000))
BFUT     = "https://fapi.binance.com"

wallets:       dict = {}
manual_trades: list = []


async def _get(url, params=None):
    try:
        async with httpx.AsyncClient(timeout=9) as c:
            r = await c.get(url, params=params)
            r.raise_for_status()
            return r.json()
    except Exception as e:
        log.debug("GET %s: %s", url, e)
        return None


async def notify(text: str):
    if not TG_TOKEN or not TG_CHAT:
        return
    safe = text.encode("ascii", errors="replace").decode("ascii")
    try:
        async with httpx.AsyncClient(timeout=6) as c:
            await c.post(
                f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                json={"chat_id": TG_CHAT, "text": safe,
                      "parse_mode": "Markdown",
                      "disable_web_page_preview": True},
            )
    except Exception as e:
        log.warning("TG: %s", e)


# ?? Exchange client ???????????????????????????????????????????????????????????
class ExchangeClient:
    def __init__(self, exchange: str, api_key: str, api_secret: str, testnet: bool = False):
        self.exchange   = exchange.lower().strip()
        self.api_key    = api_key.strip()
        self.api_secret = api_secret.strip()
        self.testnet    = testnet
        self._ex        = None
        self.connected  = False
        self.error      = ""
        if self.api_key and self.api_secret:
            self._connect()

    def _connect(self):
        try:
            import ccxt
            if self.exchange == "mexc":
                cfg = {"apiKey": self.api_key, "secret": self.api_secret,
                       "enableRateLimit": True, "options": {"defaultType": "swap"}}
                self._ex = ccxt.mexc(cfg)
            elif self.exchange == "bybit":
                cfg = {"apiKey": self.api_key, "secret": self.api_secret,
                       "enableRateLimit": True, "options": {"defaultType": "future"}}
                self._ex = ccxt.bybit(cfg)
            elif self.exchange == "binance":
                cfg = {"apiKey": self.api_key, "secret": self.api_secret,
                       "enableRateLimit": True, "options": {"defaultType": "future"}}
                self._ex = ccxt.binanceusdm(cfg)
            else:
                self.error = f"Unsupported: {self.exchange}"; return

            if self.testnet:
                try: self._ex.set_sandbox_mode(True)
                except Exception: pass

            self.connected = True
            log.info("%s connected testnet=%s", self.exchange.upper(), self.testnet)
        except Exception as e:
            self.error = str(e)
            log.error("Exchange %s: %s", self.exchange, e)

    async def test_connection(self):
        if not self._ex: return False, self.error
        try:
            if self.exchange == "mexc":
                bal = await asyncio.to_thread(self._ex.fetch_balance, {"type": "swap"})
            else:
                bal = await asyncio.to_thread(self._ex.fetch_balance)
            usdt = 0.0
            for k in ["USDT", "usdt"]:
                if k in bal and bal[k].get("free") is not None:
                    usdt = float(bal[k]["free"]); break
            if usdt == 0.0 and "total" in bal:
                usdt = float(bal["total"].get("USDT", 0))
            return True, usdt
        except Exception as e:
            return False, str(e)

    async def get_balance(self) -> float:
        if not self._ex: return 0.0
        try:
            if self.exchange == "mexc":
                bal = await asyncio.to_thread(self._ex.fetch_balance, {"type": "swap"})
            else:
                bal = await asyncio.to_thread(self._ex.fetch_balance)
            usdt = 0.0
            for k in ["USDT", "usdt"]:
                if k in bal and bal[k].get("free") is not None:
                    usdt = float(bal[k]["free"]); break
            if usdt == 0.0 and "total" in bal:
                usdt = float(bal["total"].get("USDT", 0))
            return usdt
        except Exception:
            return 0.0

    async def place_order(self, symbol: str, side: str, usdt: float,
                          leverage: int, tp: float, sl: float) -> dict:
        if not self._ex: return {"error": "Not connected"}
        try:
            sf = symbol.replace("USDT", "/USDT:USDT")
            try: await asyncio.to_thread(self._ex.set_leverage, leverage, sf)
            except Exception: pass
            ticker = await asyncio.to_thread(self._ex.fetch_ticker, sf)
            qty    = round(usdt / ticker["last"], 6)
            order  = await asyncio.to_thread(
                self._ex.create_market_order, sf, side.lower(), qty
            )
            cs = "sell" if side.upper() == "BUY" else "buy"
            try:
                if tp:
                    await asyncio.to_thread(
                        self._ex.create_order, sf, "take_profit_market", cs, qty, tp,
                        {"stopPrice": tp, "closePosition": True, "reduceOnly": True},
                    )
                if sl:
                    await asyncio.to_thread(
                        self._ex.create_order, sf, "stop_market", cs, qty, sl,
                        {"stopPrice": sl, "closePosition": True, "reduceOnly": True},
                    )
            except Exception as e:
                log.warning("TP/SL set error: %s", e)

            return {
                "id":     order["id"],
                "status": "filled",
                "symbol": symbol,
                "side":   side,
                "amount": usdt,
                "price":  ticker["last"],
                "qty":    qty,
            }
        except Exception as e:
            return {"error": str(e)}


# ?? Agent state for Mini App ??????????????????????????????????????????????????
@dataclass
class AgentStats:
    running:     bool  = False
    exchange:    str   = ""
    connected:   bool  = False
    risk_pct:    float = 10.0
    open_count:  int   = 0
    closed_count:int   = 0
    win_rate:    float = 0.0
    total_pnl:   float = 0.0
    scans:       int   = 0


agent_stats = AgentStats()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await asyncio.sleep(3)
    await notify(
        "AXIFLOW Server started\n"
        "Mini App ready\n"
        "Bot handles trading signals"
    )
    yield


app = FastAPI(title="AXIFLOW v3", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
os.makedirs(static_dir, exist_ok=True)
app.mount("/static", StaticFiles(directory=static_dir, html=True), name="static")


@app.get("/")
async def root():
    return {"status": "ok", "service": "AXIFLOW v3"}


@app.get("/api/market/{symbol}")
async def market(symbol: str):
    sym = symbol.upper()
    ticker = await _get(f"{BFUT}/fapi/v1/ticker/24hr",    {"symbol": sym})
    oi     = await _get(f"{BFUT}/fapi/v1/openInterest",   {"symbol": sym})
    fund   = await _get(f"{BFUT}/fapi/v1/premiumIndex",   {"symbol": sym})
    liqs   = await _get(f"{BFUT}/fapi/v1/allForceOrders", {"symbol": sym, "limit": 100})
    ob     = await _get(f"{BFUT}/fapi/v1/depth",          {"symbol": sym, "limit": 50})

    liq_ratio = 1.0
    if liqs:
        lv = sum(float(o["origQty"]) * float(o["price"]) for o in liqs if o.get("side") == "SELL")
        sv = sum(float(o["origQty"]) * float(o["price"]) for o in liqs if o.get("side") == "BUY")
        liq_ratio = lv / max(sv, 1)

    ob_imb = 0.0
    if ob:
        bv = sum(float(b[1]) for b in ob.get("bids", []))
        av = sum(float(a[1]) for a in ob.get("asks", []))
        tot = bv + av
        ob_imb = (bv - av) / tot if tot else 0

    return {
        "symbol":    sym,
        "price":     float(ticker["lastPrice"])        if ticker else 0,
        "change":    float(ticker["priceChangePercent"]) if ticker else 0,
        "volume":    float(ticker["quoteVolume"])      if ticker else 0,
        "high":      float(ticker["highPrice"])        if ticker else 0,
        "low":       float(ticker["lowPrice"])         if ticker else 0,
        "oi":        float(oi["openInterest"])         if oi     else 0,
        "funding":   float(fund["lastFundingRate"])    if fund   else 0,
        "liq_ratio": liq_ratio,
        "ob_imb":    ob_imb,
        "ts":        time.time(),
    }


@app.get("/api/klines/{symbol}")
async def klines(symbol: str, interval: str = "5m", limit: int = 100):
    sym  = symbol.upper()
    data = await _get(f"{BFUT}/fapi/v1/klines",
                      {"symbol": sym, "interval": interval, "limit": limit})
    candles = []
    if data:
        candles = [{"o":float(x[1]),"h":float(x[2]),"l":float(x[3]),
                    "c":float(x[4]),"v":float(x[5]),"t":int(x[0])} for x in data]
    return {"symbol": sym, "interval": interval, "candles": candles}


# ?? Pydantic models ???????????????????????????????????????????????????????????
class WalletReq(BaseModel):
    user_id:    str
    exchange:   str  = ""
    api_key:    str  = ""
    api_secret: str  = ""
    testnet:    bool = False


class AgentReq(BaseModel):
    user_id:  str
    action:   str
    risk_pct: float = 10.0
    min_conf: int   = 70
    max_open: int   = 2


class TradeReq(BaseModel):
    user_id:  str
    symbol:   str
    side:     str
    amount:   float
    leverage: int = 1


# ?? Routes ????????????????????????????????????????????????????????????????????
@app.post("/api/wallet")
async def connect_wallet(req: WalletReq):
    if not req.api_key or not req.api_secret:
        return {"success": False, "error": "Enter API keys"}
    if not req.exchange:
        return {"success": False, "error": "Select exchange"}

    ex = ExchangeClient(req.exchange, req.api_key, req.api_secret, req.testnet)
    if not ex.connected:
        return {"success": False, "error": ex.error or "Connection failed"}

    ok, result = await ex.test_connection()
    if not ok:
        return {"success": False, "error": str(result)}

    wallets[req.user_id] = {
        "exchange":  req.exchange,
        "balance":   float(result),
        "testnet":   req.testnet,
        "connected": True,
        "client":    ex,
    }

    agent_stats.exchange  = req.exchange
    agent_stats.connected = True

    return {"success": True, "balance": float(result), "exchange": req.exchange}


@app.get("/api/wallet/{user_id}")
async def get_wallet(user_id: str):
    w = wallets.get(user_id, {})
    if not w:
        return {"connected": False}
    if "client" in w:
        try:
            w["balance"] = await w["client"].get_balance()
        except Exception:
            pass
    return {k: v for k, v in w.items() if k != "client"}


@app.post("/api/agent")
async def control_agent(req: AgentReq):
    w  = wallets.get(req.user_id)
    ex = w["client"] if w and "client" in w else None

    if req.action == "start":
        if not ex:
            return {"success": False, "msg": "Connect API keys first"}
        agent_stats.running  = True
        agent_stats.risk_pct = req.risk_pct
        return {"success": True, "msg": "Agent started - waiting for channel signals"}

    if req.action == "stop":
        agent_stats.running = False
        return {"success": True, "msg": "Agent stopped"}

    if req.action == "status":
        return {
            "running":       agent_stats.running,
            "exchange":      agent_stats.exchange,
            "connected":     agent_stats.connected,
            "scans":         agent_stats.scans,
            "open_count":    agent_stats.open_count,
            "closed_count":  agent_stats.closed_count,
            "win_rate":      agent_stats.win_rate,
            "total_pnl":     agent_stats.total_pnl,
            "open_trades":   [],
            "closed_trades": [],
        }

    return {"success": False, "msg": "Unknown action"}


@app.post("/api/trade")
async def manual_trade(req: TradeReq):
    w  = wallets.get(req.user_id)
    ex = w["client"] if w and "client" in w else None
    if not ex:
        return {"success": False, "error": "Connect API keys first"}

    order = await ex.place_order(
        req.symbol.upper(), req.side,
        req.amount, req.leverage,
        tp=0, sl=0,
    )
    if "error" in order:
        return {"success": False, "error": order["error"]}

    manual_trades.append({**order, "ts": time.time()})
    return {"success": True, "trade": order}


@app.get("/api/trades/{user_id}")
async def get_trades(user_id: str):
    return {"trades": manual_trades[-30:]}
