import asyncio, os, time, random, logging
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Optional
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import httpx

# ── Завантаження .env ────────────────────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s — %(message)s")
log = logging.getLogger("axiflow")

TG_TOKEN  = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT   = os.environ.get("TELEGRAM_CHAT_ID", "")
PORT      = int(os.environ.get("PORT", 8000))
TESTNET   = os.environ.get("TESTNET", "true").lower() == "true"
RISK_PCT  = float(os.environ.get("RISK_PERCENT", "1.5"))

BFUT = "https://fapi.binance.com"

SYMBOLS = [
    "BTCUSDT","ETHUSDT","SOLUSDT","BNBUSDT","XRPUSDT",
    "ADAUSDT","DOGEUSDT","AVAXUSDT","DOTUSDT","LINKUSDT",
    "LTCUSDT","MATICUSDT","ATOMUSDT","UNIUSDT","NEARUSDT",
]


# ══════════════════════════════════════════
#  SIGNAL
# ══════════════════════════════════════════
@dataclass
class Signal:
    symbol:     str
    decision:   str
    confidence: int
    strategy:   str
    score:      float
    entry:      float
    tp:         float
    sl:         float
    rr:         float
    lev:        int
    reasons:    list
    raw:        dict
    ts:         float = field(default_factory=time.time)

    def to_dict(self):
        return {
            "symbol": self.symbol, "decision": self.decision,
            "confidence": self.confidence, "strategy": self.strategy,
            "score": round(self.score, 2), "entry": self.entry,
            "tp": round(self.tp, 6), "sl": round(self.sl, 6),
            "rr": round(self.rr, 2), "lev": self.lev,
            "reasons": self.reasons, "raw": self.raw, "ts": self.ts,
        }


# ══════════════════════════════════════════
#  HTTP HELPER
# ══════════════════════════════════════════
async def _get(client, url, params=None):
    try:
        r = await client.get(url, params=params, timeout=8)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.debug(f"HTTP {url}: {e}")
        return None


# ══════════════════════════════════════════
#  DATA FETCHERS
# ══════════════════════════════════════════
async def fetch_ticker(client, sym):
    d = await _get(client, f"{BFUT}/fapi/v1/ticker/24hr", {"symbol": sym})
    if not d:
        return {"price": 0.0, "change": 0.0}
    return {"price": float(d["lastPrice"]), "change": float(d["priceChangePercent"])}

async def fetch_klines(client, sym, tf="5m", limit=80):
    d = await _get(client, f"{BFUT}/fapi/v1/klines",
                   {"symbol": sym, "interval": tf, "limit": limit})
    if not d:
        return _mock_candles(limit)
    return [{"o": float(x[1]), "h": float(x[2]), "l": float(x[3]),
             "c": float(x[4]), "v": float(x[5]), "t": int(x[0])} for x in d]

async def fetch_oi(client, sym):
    now  = await _get(client, f"{BFUT}/fapi/v1/openInterest", {"symbol": sym})
    hist = await _get(client, f"{BFUT}/futures/data/openInterestHist",
                      {"symbol": sym, "period": "5m", "limit": 6})
    cur  = float(now["openInterest"]) if now else random.uniform(50000, 120000)
    vals = [float(h["sumOpenInterest"]) for h in (hist or [])]
    p15  = vals[-1] if len(vals) >= 4 else cur * 0.995
    d15  = (cur - p15) / max(p15, 1) * 100
    return {"current": cur, "delta_15m": d15,
            "strength": 2 if abs(d15) > 5 else 1 if abs(d15) > 2 else 0}

async def fetch_funding(client, sym):
    d  = await _get(client, f"{BFUT}/fapi/v1/premiumIndex", {"symbol": sym})
    fr = float(d["lastFundingRate"]) if d else random.uniform(-0.003, 0.007)
    return {"rate": fr, "extreme_long": fr > 0.01, "extreme_short": fr < -0.01}

async def fetch_liqs(client, sym):
    d = await _get(client, f"{BFUT}/fapi/v1/allForceOrders", {"symbol": sym, "limit": 100})
    if not d:
        lv, sv = random.uniform(10000, 60000), random.uniform(10000, 60000)
    else:
        lv = sum(float(o["origQty"]) * float(o["price"]) for o in d if o.get("side") == "SELL")
        sv = sum(float(o["origQty"]) * float(o["price"]) for o in d if o.get("side") == "BUY")
    r = lv / max(sv, 1)
    return {"long": lv, "short": sv, "ratio": r,
            "strength": 2 if r > 3 or r < 0.33 else 1 if r > 2 or r < 0.5 else 0}

async def fetch_ob(client, sym):
    d = await _get(client, f"{BFUT}/fapi/v1/depth", {"symbol": sym, "limit": 20})
    if not d:
        bv, av = random.uniform(500, 1500), random.uniform(500, 1500)
    else:
        bv = sum(float(b[1]) for b in d.get("bids", []))
        av = sum(float(a[1]) for a in d.get("asks", []))
    total = bv + av
    imb   = (bv - av) / total if total else 0
    return {"bid": bv, "ask": av, "imbalance": imb,
            "strength": 2 if abs(imb) > 0.2 else 1 if abs(imb) > 0.1 else 0}


# ══════════════════════════════════════════
#  INDICATORS
# ══════════════════════════════════════════
def compute_cvd(candles):
    cum = 0.0
    for c in candles[:-1]:
        cum += c["v"] if c["c"] >= c["o"] else -c["v"]
    prev = cum
    last = candles[-1]
    cum += last["v"] if last["c"] >= last["o"] else -last["v"]
    pd  = 1 if len(candles) > 1 and last["c"] > candles[-2]["c"] else -1
    cd  = 1 if cum > prev else -1
    div = 1 if cd > 0 and pd < 0 else -1 if cd < 0 and pd > 0 else 0
    return {"divergence": div}

def compute_atr(candles, period=14):
    if len(candles) < period + 1:
        return candles[-1]["h"] - candles[-1]["l"] if candles else 1
    trs = []
    for i in range(1, min(period + 1, len(candles))):
        c = candles[-i]; p = candles[-i - 1]
        trs.append(max(c["h"] - c["l"], abs(c["h"] - p["c"]), abs(c["l"] - p["c"])))
    return sum(trs) / len(trs)

def vol_ratio(candles):
    if len(candles) < 10: return 1.0
    avg = sum(c["v"] for c in candles[:-5]) / max(len(candles[:-5]), 1)
    rec = sum(c["v"] for c in candles[-5:]) / 5
    return rec / max(avg, 0.001)

def detect_amd(candles, oi_delta):
    if len(candles) < 15: return {"active": False, "confirmed": False}
    recent = candles[-15:]
    highs  = [c["h"] for c in recent]; lows = [c["l"] for c in recent]
    vols   = [c["v"] for c in recent]
    pr     = (max(highs) - min(lows)) / max(min(lows), 1) * 100
    if pr >= 2.0: return {"active": False, "confirmed": False}
    rt, rb = max(highs), min(lows); tol = pr * 0.15
    top_t  = sum(1 for h in highs if abs(h - rt) / max(rt, 1) * 100 < tol)
    bot_t  = sum(1 for l in lows  if abs(l - rb) / max(rb, 1) * 100 < tol)
    if top_t < 2 and bot_t < 2: return {"active": False, "confirmed": False}
    if not (vols[-1] < vols[0] and 0 <= oi_delta <= 3):
        return {"active": True, "confirmed": False}
    avg_v = sum(vols[:-3]) / max(len(vols[:-3]), 1)
    avg_s = sum(c["h"] - c["l"] for c in recent[:-3]) / max(len(recent[:-3]), 1)
    for c in candles[-3:]:
        bt    = c["h"] > rt * 1.002; bb = c["l"] < rb * 0.998
        back  = rb < candles[-1]["c"] < rt
        spike = c["v"] > avg_v * 1.5 or (c["h"] - c["l"]) > avg_s * 1.5
        if (bt or bb) and back and spike:
            return {"active": True, "confirmed": True,
                    "fake": "up" if bt else "down",
                    "signal": "SHORT" if bt else "LONG",
                    "fvg_top": rt, "fvg_bot": rb}
    return {"active": True, "confirmed": False}


# ══════════════════════════════════════════
#  SCORING
# ══════════════════════════════════════════
def score_market(ticker, oi, funding, liqs, ob, cvd_data):
    s = 0.0; reasons = []
    d  = oi["delta_15m"]; up = ticker["change"] > 0
    if   d > 5:            s += 1.5; reasons.append(f"OI екстрем +{d:.1f}%")
    elif d > 2 and up:     s += 1.0; reasons.append(f"OI +{d:.1f}% + ціна ↑")
    elif d > 2 and not up: s -= 1.0; reasons.append(f"OI +{d:.1f}% + ціна ↓")
    elif d < -2 and up:    s -= 0.5; reasons.append(f"OI −{abs(d):.1f}% слабкий ріст")
    fr = funding["rate"]
    if funding["extreme_long"]:   s -= 1.0; reasons.append(f"Funding перекупленість {fr*100:.4f}%")
    elif funding["extreme_short"]:s += 1.0; reasons.append(f"Funding перепроданість {fr*100:.4f}%")
    r  = liqs["ratio"]
    if   r > 2:   s += 1.0; reasons.append(f"Лонг ліквідації x{r:.1f} → розворот ↑")
    elif r < 0.5: s -= 1.0; reasons.append(f"Шорт ліквідації x{1/max(r,.001):.1f} → розворот ↓")
    im = ob["imbalance"]
    if   im > 0.15:  s += 1.0; reasons.append(f"OB тиск покупців {im:+.2f}")
    elif im < -0.15: s -= 1.0; reasons.append(f"OB тиск продавців {im:+.2f}")
    dv = cvd_data["divergence"]
    if   dv == 1:  s += 1.0; reasons.append("CVD бичача дивергенція")
    elif dv == -1: s -= 1.0; reasons.append("CVD ведмежа дивергенція")
    return s, reasons

def calc_confidence(final, oi_s, liq_s, ob_s, amd_conf):
    return min(100, max(0, int(
        50 + abs(final) * 12 + oi_s * 5 + liq_s * 5 + ob_s * 5 + (10 if amd_conf else 0)
    )))

def calc_tp_sl(price, direction, atr_val):
    risk = atr_val * 1.5; reward = risk * 4.0
    if direction == "LONG": return price + reward, price - risk
    return price - reward, price + risk

def calc_lev(conf, final):
    if conf >= 80 and abs(final) >= 4: return 5
    if conf >= 72 and abs(final) >= 3: return 3
    if conf >= 65: return 2
    return 1


# ══════════════════════════════════════════
#  BUILD SIGNAL
# ══════════════════════════════════════════
def build_signal(sym, ticker, oi, funding, liqs, ob, cvd_data, amd, candles, vr):
    price = ticker["price"]
    base, reasons = score_market(ticker, oi, funding, liqs, ob, cvd_data)
    amd_score = 0.0; strategy = "STANDARD"
    if amd.get("confirmed"):
        amd_score = 3.5 if amd["signal"] == "LONG" else -3.5
        strategy  = "AMD_FVG"
        reasons.append(f"⚡ AMD: фейк {amd['fake'].upper()} → {amd['signal']}")
        reasons.append(f"FVG зона ${amd['fvg_bot']:.2f}–${amd['fvg_top']:.2f}")
    final = base + amd_score
    raw   = {"price": price, "change": ticker["change"], "oi": oi["delta_15m"],
             "funding": funding["rate"], "liq": liqs["ratio"], "ob": ob["imbalance"],
             "amd": amd.get("confirmed", False), "vol": vr}
    if vr < 0.7 and not amd.get("confirmed"):
        return Signal(sym, "NO TRADE", 0, strategy, final, price, price, price, 0, 1,
                      [f"Об'єм низький {vr:.0%}"], raw)
    if abs(final) < 1.5 and not amd.get("confirmed"):
        return Signal(sym, "NO TRADE", 0, strategy, final, price, price, price, 0, 1,
                      [f"Score {final:.1f} нижче мінімуму"], raw)
    if   final >= 2:  decision = "LONG"
    elif final <= -2: decision = "SHORT"
    else:             decision = "NO TRADE"
    conf    = calc_confidence(final, oi["strength"], liqs["strength"], ob["strength"], amd.get("confirmed", False))
    atr_val = compute_atr(candles)
    tp, sl  = calc_tp_sl(price, decision, atr_val) if decision != "NO TRADE" else (price, price)
    rr      = abs(tp - price) / max(abs(price - sl), 0.0001) if decision != "NO TRADE" else 0
    lev     = calc_lev(conf, final) if decision != "NO TRADE" else 1
    return Signal(sym, decision, conf, strategy, final, price, tp, sl, rr, lev,
                  reasons or ["Без причин"], raw)


# ══════════════════════════════════════════
#  ENGINE
# ══════════════════════════════════════════
class Engine:
    def __init__(self):
        self.cache:   dict[str, Signal] = {}
        self.history: dict[str, list]   = {}
        self._prev:   dict[str, float]  = {}

    async def analyze(self, sym: str) -> Signal:
        try:
            async with httpx.AsyncClient() as c:
                ticker, candles, oi, funding, liqs, ob = await asyncio.gather(
                    fetch_ticker(c, sym), fetch_klines(c, sym, "5m", 80),
                    fetch_oi(c, sym), fetch_funding(c, sym),
                    fetch_liqs(c, sym), fetch_ob(c, sym),
                )
            cvd_data = compute_cvd(candles)
            amd      = detect_amd(candles, oi["delta_15m"])
            vr       = vol_ratio(candles)
            sig      = build_signal(sym, ticker, oi, funding, liqs, ob, cvd_data, amd, candles, vr)
            self.cache[sym] = sig
            self.history.setdefault(sym, []).append(sig)
            if len(self.history[sym]) > 50:
                self.history[sym] = self.history[sym][-50:]
            log.info(f"[{sym}] {sig.decision} conf={sig.confidence}% score={sig.score:.1f}")
            return sig
        except Exception as e:
            log.error(f"Engine {sym}: {e}")
            return Signal(sym, "NO TRADE", 0, "STANDARD", 0, 0, 0, 0, 0, 1, ["API помилка"], {"price": 0})

    async def analyze_all(self, syms=None) -> dict:
        syms    = syms or SYMBOLS
        results = await asyncio.gather(*[self.analyze(s) for s in syms], return_exceptions=True)
        return {s: (r if isinstance(r, Signal) else
                    Signal(s, "NO TRADE", 0, "STANDARD", 0, 0, 0, 0, 0, 1, ["Error"], {"price": 0}))
                for s, r in zip(syms, results)}

    def is_new(self, sym: str) -> bool:
        sig = self.cache.get(sym)
        if not sig or sig.decision == "NO TRADE":
            self._prev[sym] = 0; return False
        prev = self._prev.get(sym, 0); curr = sig.score
        fired = (prev == 0 and abs(curr) >= 2) or (abs(curr - prev) >= 1.5)
        self._prev[sym] = curr
        return fired

    def get(self, sym): return self.cache.get(sym)


# ══════════════════════════════════════════
#  EXCHANGE CLIENT
# ══════════════════════════════════════════
class ExchangeClient:
    def __init__(self, bybit_key="", bybit_secret="",
                 binance_key="", binance_secret="", testnet=True):
        self.testnet  = testnet
        self.demo     = not bybit_key and not binance_key
        self._bybit   = None
        self._binance = None
        if bybit_key:   self._init("bybit",   bybit_key,   bybit_secret)
        if binance_key: self._init("binance", binance_key, binance_secret)

    def _init(self, name, key, secret):
        try:
            import ccxt
            if name == "bybit":
                ex = ccxt.bybit({"apiKey": key, "secret": secret, "enableRateLimit": True})
                if self.testnet: ex.set_sandbox_mode(True)
                self._bybit = ex
            else:
                ex = ccxt.binanceusdm({"apiKey": key, "secret": secret, "enableRateLimit": True,
                                       "options": {"defaultType": "future"}})
                if self.testnet: ex.set_sandbox_mode(True)
                self._binance = ex
            log.info(f"✅ {name.upper()} підключено testnet={self.testnet}")
        except Exception as e:
            log.error(f"{name} init: {e}")

    def _ex(self): return self._bybit or self._binance

    async def get_balance(self) -> float:
        if self.demo: return 10000.0
        try:
            bal = await asyncio.to_thread(self._ex().fetch_balance)
            return float(bal.get("USDT", {}).get("free", 0))
        except Exception as e:
            log.error(f"Balance: {e}"); return 0.0

    async def place_order(self, symbol, side, usdt, leverage, tp, sl):
        if self.demo:
            return {"id": f"DEMO_{int(time.time())}", "status": "filled",
                    "symbol": symbol, "side": side, "amount": usdt, "demo": True}
        try:
            ex      = self._ex()
            sym_fmt = symbol.replace("USDT", "/USDT:USDT")
            try: await asyncio.to_thread(ex.set_leverage, leverage, sym_fmt)
            except: pass
            ticker  = await asyncio.to_thread(ex.fetch_ticker, sym_fmt)
            qty     = round(usdt / ticker["last"], 6)
            order   = await asyncio.to_thread(ex.create_market_order, sym_fmt, side.lower(), qty)
            cs      = "sell" if side == "BUY" else "buy"
            try:
                await asyncio.to_thread(ex.create_order, sym_fmt,
                    "take_profit_market", cs, qty, tp,
                    {"stopPrice": tp, "closePosition": True, "reduceOnly": True})
                await asyncio.to_thread(ex.create_order, sym_fmt,
                    "stop_market", cs, qty, sl,
                    {"stopPrice": sl, "closePosition": True, "reduceOnly": True})
            except Exception as e:
                log.warning(f"TP/SL: {e}")
            return {"id": order["id"], "status": "filled", "symbol": symbol,
                    "side": side, "amount": usdt, "demo": False}
        except Exception as e:
            log.error(f"Order {symbol}: {e}")
            return {"error": str(e)}


# ══════════════════════════════════════════
#  TRADING AGENT
# ══════════════════════════════════════════
@dataclass
class Trade:
    id: str; symbol: str; side: str
    entry: float; tp: float; sl: float
    amount: float; leverage: int
    opened: float = field(default_factory=time.time)
    pnl: float = 0.0; status: str = "open"


class Agent:
    def __init__(self, engine: Engine, exchange: ExchangeClient,
                 risk_pct=1.5, min_conf=70, max_open=3):
        self.engine   = engine
        self.ex       = exchange
        self.risk_pct = risk_pct
        self.min_conf = min_conf
        self.max_open = max_open
        self.running  = False
        self.trades:  list[Trade] = []
        self.closed:  list[Trade] = []
        self.total_pnl = 0.0
        self.scans    = 0

    async def start(self):
        self.running = True
        log.info("Agent started")
        await _tg(f"🟢 *AXIFLOW Agent запущено*\n\n"
                  f"📊 Сканую {len(SYMBOLS)} пар безперервно\n"
                  f"⚡ Мін. конфіденційність: {self.min_conf}%\n"
                  f"💰 Ризик: {self.risk_pct}% на угоду\n"
                  f"📐 RR = 1:4\n"
                  f"🏦 {'Demo' if self.ex.demo else 'Live'} режим")
        while self.running:
            try:
                await self._scan()
                await self._monitor()
                self.scans += 1
            except Exception as e:
                log.error(f"Agent: {e}")
            await asyncio.sleep(30)

    def stop(self): self.running = False

    async def _scan(self):
        for sym in SYMBOLS:
            if any(t.symbol == sym and t.status == "open" for t in self.trades): continue
            sig = await self.engine.analyze(sym)
            if sig.decision != "NO TRADE" and sig.confidence >= self.min_conf:
                if self.engine.is_new(sym):
                    await self._trade(sig)

    async def _trade(self, sig: Signal):
        if sum(1 for t in self.trades if t.status == "open") >= self.max_open: return
        bal      = await self.ex.get_balance()
        pos_size = bal * (self.risk_pct / 100) * sig.lev
        order    = await self.ex.place_order(
            sig.symbol, "BUY" if sig.decision == "LONG" else "SELL",
            pos_size, sig.lev, sig.tp, sig.sl
        )
        if "error" in order:
            await _tg(f"❌ Помилка ордера `{sig.symbol}`:\n{order['error']}"); return
        t = Trade(id=order["id"], symbol=sig.symbol,
                  side="BUY" if sig.decision == "LONG" else "SELL",
                  entry=sig.entry, tp=sig.tp, sl=sig.sl,
                  amount=pos_size, leverage=sig.lev)
        self.trades.append(t)
        demo = " _(demo)_" if order.get("demo") else ""
        await _tg(
            f"{'🟢' if sig.decision=='LONG' else '🔴'} *{sig.decision} відкрито{demo}*\n\n"
            f"📌 `{sig.symbol}`\n"
            f"💲 Вхід: `${sig.entry:,.4f}`\n"
            f"🎯 Take Profit: `${sig.tp:,.4f}`\n"
            f"🛑 Stop Loss: `${sig.sl:,.4f}`\n"
            f"📐 RR: `1:{sig.rr:.1f}`\n"
            f"⚡ Плече: `{sig.lev}x`\n"
            f"💰 Позиція: `${pos_size:.0f}`\n"
            f"🧠 Стратегія: `{sig.strategy}`\n"
            f"📊 Конф.: `{sig.confidence}%`\n\n"
            + "\n".join(f"• {r}" for r in sig.reasons[:4])
        )

    async def _monitor(self):
        open_t = [t for t in self.trades if t.status == "open"]
        if not open_t: return
        async with httpx.AsyncClient() as c:
            for t in open_t:
                try:
                    ticker = await fetch_ticker(c, t.symbol)
                    price  = ticker["price"]
                    if t.side == "BUY":
                        pnl_pct = (price - t.entry) / t.entry * 100 * t.leverage
                        hit_tp  = price >= t.tp; hit_sl = price <= t.sl
                    else:
                        pnl_pct = (t.entry - price) / t.entry * 100 * t.leverage
                        hit_tp  = price <= t.tp; hit_sl = price >= t.sl
                    t.pnl = t.amount * pnl_pct / 100
                    if hit_tp:
                        t.status = "tp"; self.total_pnl += t.pnl; self.closed.append(t)
                        await _tg(f"✅ *TAKE PROFIT!*\n📌 `{t.symbol}`\n💰 PnL: `+${t.pnl:.2f}` (+{pnl_pct:.1f}%)\n📈 Загальний: `${self.total_pnl:.2f}`")
                    elif hit_sl:
                        t.status = "sl"; self.total_pnl += t.pnl; self.closed.append(t)
                        await _tg(f"🛑 *STOP LOSS*\n📌 `{t.symbol}`\n💸 PnL: `${t.pnl:.2f}` ({pnl_pct:.1f}%)\n📉 Загальний: `${self.total_pnl:.2f}`")
                except Exception as e:
                    log.warning(f"Monitor {t.symbol}: {e}")

    def stats(self):
        open_t = [t for t in self.trades if t.status == "open"]
        wins   = [t for t in self.closed if t.pnl > 0]
        return {
            "running":      self.running, "scans": self.scans,
            "open_count":   len(open_t),
            "closed_count": len(self.closed),
            "win_rate":     round(len(wins) / max(len(self.closed), 1) * 100, 1),
            "total_pnl":    round(self.total_pnl, 2),
            "open_trades":  [{"id": t.id, "symbol": t.symbol, "side": t.side,
                               "entry": t.entry, "tp": t.tp, "sl": t.sl,
                               "pnl": round(t.pnl, 2), "lev": t.leverage,
                               "amount": round(t.amount, 2)} for t in open_t],
            "closed_trades": [{"symbol": t.symbol, "side": t.side,
                                "pnl": round(t.pnl, 2), "status": t.status}
                               for t in self.closed[-10:]],
        }


# ══════════════════════════════════════════
#  TELEGRAM NOTIFY
# ══════════════════════════════════════════
async def _tg(text: str):
    if not TG_TOKEN or not TG_CHAT:
        log.info(f"[TG] {text[:80]}")
        return
    try:
        async with httpx.AsyncClient() as c:
            await c.post(
                f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                json={"chat_id": TG_CHAT, "text": text,
                      "parse_mode": "Markdown", "disable_web_page_preview": True},
                timeout=5,
            )
    except Exception as e:
        log.warning(f"TG: {e}")


# ══════════════════════════════════════════
#  GLOBALS
# ══════════════════════════════════════════
engine  = Engine()
agent:  Optional[Agent] = None
agent_task = None
wallets: dict = {}
manual_trades: list = []


# ══════════════════════════════════════════
#  FASTAPI
# ══════════════════════════════════════════
@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info(f"AXIFLOW starting on port {PORT}")
    asyncio.create_task(engine.analyze_all(SYMBOLS))
    asyncio.create_task(_refresh_loop())
    yield

async def _refresh_loop():
    while True:
        try: await engine.analyze_all(SYMBOLS)
        except Exception as e: log.error(f"Refresh: {e}")
        await asyncio.sleep(60)

app = FastAPI(title="AXIFLOW", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

static_dir = os.path.join(os.path.dirname(__file__), "static")
os.makedirs(static_dir, exist_ok=True)
app.mount("/static", StaticFiles(directory=static_dir, html=True), name="static")


# ── Models ────────────────────────────────────────────────
class WalletReq(BaseModel):
    user_id:        str
    exchange:       str   = "demo"
    bybit_key:      str   = ""
    bybit_secret:   str   = ""
    binance_key:    str   = ""
    binance_secret: str   = ""
    testnet:        bool  = True
    balance:        float = 10000.0

class AgentReq(BaseModel):
    user_id:  str
    action:   str
    risk_pct: float = 1.5
    min_conf: int   = 70
    max_open: int   = 3

class TradeReq(BaseModel):
    user_id:  str
    symbol:   str
    side:     str
    amount:   float
    leverage: int = 1


# ── Routes ─────────────────────────────────────────────────
@app.get("/")
async def root():
    return {"status": "ok", "service": "AXIFLOW"}

@app.get("/api/signals")
async def all_signals():
    return {"signals": {s: sig.to_dict() for s, sig in engine.cache.items()},
            "ts": time.time()}

@app.get("/api/signal/{symbol}")
async def one_signal(symbol: str, fresh: bool = False):
    sym = symbol.upper()
    sig = await engine.analyze(sym) if fresh or not engine.get(sym) else engine.get(sym)
    return sig.to_dict()

@app.get("/api/market/{symbol}")
async def market_data(symbol: str):
    sym = symbol.upper()
    async with httpx.AsyncClient() as c:
        t, oi, fr, lq, ob = await asyncio.gather(
            fetch_ticker(c, sym), fetch_oi(c, sym),
            fetch_funding(c, sym), fetch_liqs(c, sym), fetch_ob(c, sym)
        )
    return {"symbol": sym, "price": t["price"], "change": t["change"],
            "oi": oi, "funding": fr, "liquidations": lq, "orderbook": ob,
            "ts": time.time()}

@app.get("/api/klines/{symbol}")
async def klines(symbol: str, interval: str = "5m", limit: int = 80):
    sym = symbol.upper()
    async with httpx.AsyncClient() as c:
        candles = await fetch_klines(c, sym, interval, limit)
    return {"symbol": sym, "interval": interval, "candles": candles}

@app.post("/api/wallet")
async def save_wallet(req: WalletReq):
    ex  = ExchangeClient(req.bybit_key, req.bybit_secret,
                         req.binance_key, req.binance_secret, req.testnet)
    bal = await ex.get_balance()
    wallets[req.user_id] = {
        "exchange": req.exchange, "balance": bal or req.balance,
        "testnet": req.testnet, "demo": ex.demo, "connected": True, "client": ex
    }
    return {"success": True, "balance": bal or req.balance, "demo": ex.demo}

@app.get("/api/wallet/{user_id}")
async def get_wallet(user_id: str):
    w = wallets.get(user_id, {})
    return {k: v for k, v in w.items() if k != "client"} if w else {"connected": False}

@app.post("/api/agent")
async def control_agent(req: AgentReq):
    global agent, agent_task
    w  = wallets.get(req.user_id)
    ex = w["client"] if w and "client" in w else ExchangeClient()

    if req.action == "start":
        if agent and agent.running:
            return {"success": False, "msg": "Агент вже запущений"}
        agent      = Agent(engine, ex, req.risk_pct, req.min_conf, req.max_open)
        agent_task = asyncio.create_task(agent.start())
        return {"success": True, "msg": "Агент запущений"}

    elif req.action == "stop":
        if agent: agent.stop()
        return {"success": True, "msg": "Агент зупинений"}

    elif req.action == "status":
        if not agent:
            return {"running": False, "scans": 0, "open_count": 0,
                    "win_rate": 0, "total_pnl": 0, "open_trades": [], "closed_trades": []}
        return agent.stats()

    return {"success": False, "msg": "Unknown action"}

@app.post("/api/trade")
async def manual_trade(req: TradeReq):
    w     = wallets.get(req.user_id)
    ex    = w["client"] if w and "client" in w else ExchangeClient()
    sig   = engine.get(req.symbol.upper())
    tp    = sig.tp if sig else 0
    sl    = sig.sl if sig else 0
    order = await ex.place_order(req.symbol.upper(), req.side, req.amount, req.leverage, tp, sl)
    t     = {"id": order.get("id", "?"), "symbol": req.symbol, "side": req.side,
             "amount": req.amount, "leverage": req.leverage, "tp": tp, "sl": sl,
             "ts": time.time(), "demo": order.get("demo", True)}
    manual_trades.append(t)
    return {"success": True, "trade": t}

@app.get("/api/trades/{user_id}")
async def get_trades(user_id: str):
    return {"trades": manual_trades[-30:]}


# ══════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════
def _mock_candles(n=80):
    p = 67000.0; out = []
    for _ in range(n):
        o = p; c = o + random.uniform(-200, 200)
        out.append({"o": o, "h": max(o, c) + random.uniform(0, 80),
                    "l": min(o, c) - random.uniform(0, 80), "c": c,
                    "v": random.uniform(100, 800)})
        p = c
    return out


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=PORT, reload=False)
