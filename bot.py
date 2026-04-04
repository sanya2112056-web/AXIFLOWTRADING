"""
AXIFLOW TRADE - Bot v2
Reads @OpenInterestTracker, analyzes pump/dump signals,
runs Smart Money analysis, auto-trades, sends all alerts.
Pure ASCII - no latin-1 issues.
"""
import asyncio
import os
import time
import logging
import re
import httpx

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("axiflow_bot")

# ?? Config ???????????????????????????????????????????????????????????????????
BOT_TOKEN       = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID         = os.environ.get("TELEGRAM_CHAT_ID", "")
APP_URL         = os.environ.get("MINI_APP_URL", "")
API_URL         = os.environ.get("API_URL", "http://localhost:3000")
TG_API_ID       = int(os.environ.get("TG_API_ID", "0"))
TG_API_HASH     = os.environ.get("TG_API_HASH", "")
TG_PHONE        = os.environ.get("TG_PHONE", "")
MONITOR_CHANNEL = "@OpenInterestTracker"

ANALYZE_COOLDOWN = 600   # 10 min per symbol
_analyzed: dict  = {}

# Global settings - changed via /setrisk command
balance_pct_setting: float = 10.0
agent_active: bool          = False

# ?? Telegram ??????????????????????????????????????????????????????????????????
async def send(text: str):
    if not BOT_TOKEN or not CHAT_ID:
        return
    safe = text.encode("ascii", errors="replace").decode("ascii")
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            await c.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                json={
                    "chat_id":                  CHAT_ID,
                    "text":                     safe,
                    "parse_mode":               "Markdown",
                    "disable_web_page_preview": True,
                },
            )
    except Exception as e:
        log.warning("TG send: %s", e)


# ?? Binance Futures data ???????????????????????????????????????????????????????
BFUT = "https://fapi.binance.com"

async def _get(url, params=None):
    try:
        async with httpx.AsyncClient(timeout=9) as c:
            r = await c.get(url, params=params)
            r.raise_for_status()
            return r.json()
    except Exception as e:
        log.debug("GET %s: %s", url, e)
        return None

async def fetch_ticker(sym):
    d = await _get(f"{BFUT}/fapi/v1/ticker/24hr", {"symbol": sym})
    if not d:
        return None
    return {
        "price":  float(d["lastPrice"]),
        "change": float(d["priceChangePercent"]),
        "volume": float(d["quoteVolume"]),
        "high":   float(d["highPrice"]),
        "low":    float(d["lowPrice"]),
    }

async def fetch_klines(sym, tf="5m", limit=100):
    d = await _get(f"{BFUT}/fapi/v1/klines",
                   {"symbol": sym, "interval": tf, "limit": limit})
    if not d:
        return []
    return [{"o": float(x[1]), "h": float(x[2]), "l": float(x[3]),
             "c": float(x[4]), "v": float(x[5])} for x in d]

async def fetch_oi(sym):
    now  = await _get(f"{BFUT}/fapi/v1/openInterest", {"symbol": sym})
    hist = await _get(f"{BFUT}/futures/data/openInterestHist",
                      {"symbol": sym, "period": "5m", "limit": 12})
    if not now:
        return {"delta_15m": 0, "delta_1h": 0}
    cur  = float(now["openInterest"])
    vals = [float(h["sumOpenInterest"]) for h in (hist or [])]
    p15  = vals[-3]  if len(vals) >= 3  else cur
    p1h  = vals[-12] if len(vals) >= 12 else cur
    return {
        "delta_15m": (cur - p15) / max(p15, 1) * 100,
        "delta_1h":  (cur - p1h) / max(p1h, 1) * 100,
        "current":   cur,
    }

async def fetch_funding(sym):
    d  = await _get(f"{BFUT}/fapi/v1/premiumIndex", {"symbol": sym})
    fr = float(d["lastFundingRate"]) if d else 0
    return {
        "rate":          fr,
        "extreme_long":  fr >  0.01,
        "extreme_short": fr < -0.01,
    }

async def fetch_liqs(sym):
    d = await _get(f"{BFUT}/fapi/v1/allForceOrders", {"symbol": sym, "limit": 500})
    if not d:
        return {"ratio": 1.0, "long_vol": 0, "short_vol": 0}
    lv = sum(float(o["origQty"]) * float(o["price"]) for o in d if o.get("side") == "SELL")
    sv = sum(float(o["origQty"]) * float(o["price"]) for o in d if o.get("side") == "BUY")
    return {"ratio": lv / max(sv, 1), "long_vol": lv, "short_vol": sv}

async def fetch_ob(sym):
    d = await _get(f"{BFUT}/fapi/v1/depth", {"symbol": sym, "limit": 100})
    if not d:
        return {"imbalance": 0}
    bv = sum(float(b[1]) for b in d.get("bids", []))
    av = sum(float(a[1]) for a in d.get("asks", []))
    total = bv + av
    return {"imbalance": (bv - av) / total if total else 0}

async def fetch_ls(sym):
    d = await _get(f"{BFUT}/futures/data/globalLongShortAccountRatio",
                   {"symbol": sym, "period": "5m", "limit": 3})
    if not d:
        return {"ratio": 1.0}
    return {"ratio": float(d[-1].get("longShortRatio", 1))}


# ?? Smart Money detectors ?????????????????????????????????????????????????????
def compute_cvd(candles):
    if len(candles) < 10:
        return {"divergence": 0, "buying_pressure": 50}
    cvd_vals = []
    cum = 0.0
    for c in candles:
        cum += c["v"] if c["c"] >= c["o"] else -c["v"]
        cvd_vals.append(cum)
    p = 10
    pu = candles[-1]["c"] > candles[-p]["c"] * 1.001
    pd = candles[-1]["c"] < candles[-p]["c"] * 0.999
    cu = cvd_vals[-1] > cvd_vals[-p]
    cd = cvd_vals[-1] < cvd_vals[-p]
    div = 0
    if pu and cd: div = -1
    if pd and cu: div = 1
    bv = sum(c["v"] for c in candles[-10:] if c["c"] >= c["o"])
    sv = sum(c["v"] for c in candles[-10:] if c["c"] <  c["o"])
    bp = bv / (bv + sv) * 100 if (bv + sv) > 0 else 50
    return {"divergence": div, "buying_pressure": bp}

def find_liquidity(candles, price):
    hs  = [c["h"] for c in candles[-60:]]
    ls  = [c["l"] for c in candles[-60:]]
    tol = 0.002
    above = sorted(set(
        round(h, 6) for i, h in enumerate(hs)
        if h > price and sum(1 for hh in hs[i+1:] if abs(hh - h) / h < tol) >= 2
    ))
    below = sorted(set(
        round(l, 6) for i, l in enumerate(ls)
        if l < price and sum(1 for ll in ls[i+1:] if abs(ll - l) / l < tol) >= 2
    ), reverse=True)
    return {
        "above":    above[:3],
        "below":    below[:3],
        "day_high": max(hs),
        "day_low":  min(ls),
    }

def find_fvg(candles, price):
    bull_fvg = None
    bear_fvg = None
    data = candles[-40:]
    for i in range(2, len(data)):
        c0 = data[i-2]; c2 = data[i]
        if c2["l"] > c0["h"]:
            if c0["h"] < price:
                bull_fvg = {"top": c2["l"], "bot": c0["h"], "mid": (c2["l"] + c0["h"]) / 2}
        elif c2["h"] < c0["l"]:
            if c0["l"] > price:
                bear_fvg = {"top": c0["l"], "bot": c2["h"], "mid": (c0["l"] + c2["h"]) / 2}
    return {"bull": bull_fvg, "bear": bear_fvg}

def find_ob(candles, price):
    data = candles[-60:]
    avg  = sum(abs(c["c"] - c["o"]) for c in data) / max(len(data), 1)
    bull = None; bear = None
    for i in range(1, len(data) - 1):
        c = data[i]; n = data[i+1]; nb = abs(n["c"] - n["o"])
        if c["c"] < c["o"] and n["c"] > n["o"] and nb > avg * 1.5 and c["o"] < price:
            bull = {"top": c["o"], "bot": c["c"], "mid": (c["o"] + c["c"]) / 2}
        elif c["c"] > c["o"] and n["c"] < n["o"] and nb > avg * 1.5 and c["o"] > price:
            bear = {"top": c["c"], "bot": c["o"], "mid": (c["c"] + c["o"]) / 2}
    return {"bull": bull, "bear": bear}

def detect_sweep(candles):
    data = candles[-20:]
    if len(data) < 8:
        return {"bull": False, "bear": False}
    recent = data[:-3]; last3 = data[-3:]
    ph = max(c["h"] for c in recent); pl = min(c["l"] for c in recent)
    lh = max(c["h"] for c in last3);  ll = min(c["l"] for c in last3)
    lc = data[-1]["c"]
    return {
        "bull": bool(ll < pl and lc > pl),
        "bear": bool(lh > ph and lc < ph),
    }


# ?? Main analysis ?????????????????????????????????????????????????????????????
async def analyze(sym: str, sig_type: str) -> dict:
    log.info("Analyzing %s (%s)", sym, sig_type)

    ticker, c5m, oi, funding, liqs, ob_data, ls = await asyncio.gather(
        fetch_ticker(sym),
        fetch_klines(sym, "5m", 100),
        fetch_oi(sym),
        fetch_funding(sym),
        fetch_liqs(sym),
        fetch_ob(sym),
        fetch_ls(sym),
    )

    if not ticker:
        return {"decision": "NO TRADE", "symbol": sym,
                "reason": "No market data - symbol may not exist on Binance Futures"}

    price  = ticker["price"]
    change = ticker["change"]

    cvd   = compute_cvd(c5m)
    liq   = find_liquidity(c5m, price)
    fvg   = find_fvg(c5m, price)
    ob    = find_ob(c5m, price)
    sweep = detect_sweep(c5m)

    d15   = oi["delta_15m"]
    fr    = funding["rate"]
    liq_r = liqs["ratio"]
    im    = ob_data["imbalance"]
    ls_r  = ls["ratio"]
    bp    = cvd["buying_pressure"]

    sl_arr = []; ss_arr = []; reasons = []

    # PILLAR 1: OI (25pts)
    ol = 0; os_ = 0
    if d15 > 5:
        ol += 20; reasons.append(f"OI +{d15:.1f}% spike - aggressive longs entering")
    elif d15 > 2 and change > 0:
        ol += 14; reasons.append(f"OI +{d15:.1f}% + price up - long accumulation")
    elif d15 > 2 and change < 0:
        os_ += 14; reasons.append(f"OI +{d15:.1f}% + price down - shorts building")
    elif d15 < -3:
        os_ += 10; reasons.append(f"OI -{abs(d15):.1f}% - longs closing")
    if funding["extreme_short"]:
        ol  += 12; reasons.append(f"Funding oversold {fr*100:.4f}% - long squeeze coming")
    elif funding["extreme_long"]:
        os_ += 12; reasons.append(f"Funding overbought {fr*100:.4f}% - short squeeze")
    if liq_r > 3:
        ol  += 10; reasons.append(f"Mass long liqs x{liq_r:.1f} - bull reversal")
    elif liq_r < 0.33:
        os_ += 10; reasons.append("Mass short liqs - bear reversal")
    sl_arr.append(min(25, ol)); ss_arr.append(min(25, os_))

    # PILLAR 2: CVD (25pts)
    cl = 0; cs = 0
    if cvd["divergence"] == 1:
        cl += 18; reasons.append("CVD bullish div: price down but buyers dominate")
    elif cvd["divergence"] == -1:
        cs += 18; reasons.append("CVD bearish div: price up but sellers dominate")
    if bp > 65:
        cl += 12; reasons.append(f"Buy pressure {bp:.0f}% - aggressive buyers")
    elif bp < 35:
        cs += 12; reasons.append(f"Sell pressure {100-bp:.0f}% - aggressive sellers")
    if im > 0.2:
        cl += 8; reasons.append(f"OB buy imbalance {im:+.2f}")
    elif im < -0.2:
        cs += 8; reasons.append(f"OB sell imbalance {im:+.2f}")
    if ls_r < 0.7:
        cl += 6; reasons.append(f"L/S {ls_r:.2f} - crowd short = squeeze up")
    elif ls_r > 1.4:
        cs += 6; reasons.append(f"L/S {ls_r:.2f} - crowd long = squeeze down")
    sl_arr.append(min(25, cl)); ss_arr.append(min(25, cs))

    # PILLAR 3: Liquidity (25pts)
    ll = 0; ls2 = 0
    if sweep["bull"]:
        ll  += 18; reasons.append("Liquidity Sweep: stops below taken then reversed - LONG")
    if sweep["bear"]:
        ls2 += 18; reasons.append("Liquidity Sweep: stops above taken then reversed - SHORT")
    if fvg["bull"]:
        ll  += 8;  reasons.append(f"Bullish FVG: {fvg['bull']['bot']:.4f}-{fvg['bull']['top']:.4f}")
    if fvg["bear"]:
        ls2 += 8;  reasons.append(f"Bearish FVG: {fvg['bear']['bot']:.4f}-{fvg['bear']['top']:.4f}")
    if ob["bull"]:
        ll  += 7;  reasons.append(f"Bullish OB: {ob['bull']['bot']:.4f}-{ob['bull']['top']:.4f}")
    if ob["bear"]:
        ls2 += 7;  reasons.append(f"Bearish OB: {ob['bear']['bot']:.4f}-{ob['bear']['top']:.4f}")
    if liq["above"]:
        ll  += 5;  reasons.append(f"Liq cluster above {liq['above'][0]:.4f} - magnet target")
    if liq["below"]:
        ls2 += 5;  reasons.append(f"Liq cluster below {liq['below'][0]:.4f} - magnet target")
    sl_arr.append(min(25, ll)); ss_arr.append(min(25, ls2))

    # PILLAR 4: Structure (25pts)
    stl = 0; sts = 0
    if sig_type == "pump":
        stl += 8; reasons.append("Channel: pump signal - checking continuation/reversal")
    elif sig_type == "dump":
        sts += 8; reasons.append("Channel: dump signal - checking continuation/reversal")
    pp = (price - liq["day_low"]) / max(liq["day_high"] - liq["day_low"], 0.001) * 100
    if pp < 20:
        stl += 12; reasons.append(f"Price near 24H low ({pp:.0f}%) - reversal zone")
    elif pp > 80:
        sts += 12; reasons.append(f"Price near 24H high ({pp:.0f}%) - reversal zone")
    if change > 3 and sweep["bear"]:
        sts += 10; reasons.append("Post-pump reversal setup: sweep high after move")
    elif change < -3 and sweep["bull"]:
        stl += 10; reasons.append("Post-dump reversal: sweep low after move")
    if change > 5 and sig_type == "pump" and not sweep["bear"]:
        stl += 8; reasons.append(f"Momentum +{change:.1f}% continuation")
    elif change < -5 and sig_type == "dump" and not sweep["bull"]:
        sts += 8; reasons.append(f"Momentum {change:.1f}% continuation")
    sl_arr.append(min(25, stl)); ss_arr.append(min(25, sts))

    conf_long  = sum(sl_arr)
    conf_short = sum(ss_arr)

    # Decision
    if conf_long >= 70 and conf_long > conf_short:
        direction = "LONG"; confidence = conf_long
    elif conf_short >= 70 and conf_short > conf_long:
        direction = "SHORT"; confidence = conf_short
    else:
        return {
            "decision":   "NO TRADE",
            "symbol":     sym,
            "price":      price,
            "conf_long":  conf_long,
            "conf_short": conf_short,
            "reason":     f"Conf LONG={conf_long}% SHORT={conf_short}% below 70%",
            "reasons":    reasons[:4],
        }

    # Entry zone
    entry = price
    if direction == "LONG":
        if fvg["bull"] and abs(fvg["bull"]["mid"] - price) / price < 0.005:
            entry = fvg["bull"]["mid"]
        elif ob["bull"] and abs(ob["bull"]["mid"] - price) / price < 0.005:
            entry = ob["bull"]["mid"]
    else:
        if fvg["bear"] and abs(fvg["bear"]["mid"] - price) / price < 0.005:
            entry = fvg["bear"]["mid"]
        elif ob["bear"] and abs(ob["bear"]["mid"] - price) / price < 0.005:
            entry = ob["bear"]["mid"]

    # SL
    recent  = c5m[-20:]
    avg_rng = sum(c["h"] - c["l"] for c in recent) / max(len(recent), 1)
    atr     = avg_rng * 1.2

    if direction == "LONG":
        sl_c = [entry - atr * 1.5]
        if ob["bull"]:   sl_c.append(ob["bull"]["bot"] * 0.997)
        if fvg["bull"]:  sl_c.append(fvg["bull"]["bot"] * 0.997)
        sl = max(sl_c)
    else:
        sl_c = [entry + atr * 1.5]
        if ob["bear"]:   sl_c.append(ob["bear"]["top"] * 1.003)
        if fvg["bear"]:  sl_c.append(fvg["bear"]["top"] * 1.003)
        sl = min(sl_c)

    risk_pct = abs(entry - sl) / max(entry, 1) * 100

    # Filter: risk > 1%
    if risk_pct > 1.0:
        return {
            "decision": "NO TRADE", "symbol": sym, "price": price,
            "reason":   f"Risk {risk_pct:.2f}% exceeds 1% max",
            "reasons":  reasons[:3],
        }

    # TP targets
    sl_d = abs(entry - sl)
    if direction == "LONG":
        tp1 = entry + sl_d * 1.5
        tp2 = entry + sl_d * 2.5
        tp3 = liq["above"][0] * 0.999 if liq["above"] else entry + sl_d * 4.0
        tp3 = max(tp3, entry + sl_d * 3.0)
    else:
        tp1 = entry - sl_d * 1.5
        tp2 = entry - sl_d * 2.5
        tp3 = liq["below"][0] * 1.001 if liq["below"] else entry - sl_d * 4.0
        tp3 = min(tp3, entry - sl_d * 3.0)

    expected_move = abs(tp2 - entry) / max(entry, 1) * 100

    # Filter: move < 2%
    if expected_move < 2.0:
        return {
            "decision": "NO TRADE", "symbol": sym, "price": price,
            "reason":   f"Expected move {expected_move:.1f}% below 2% min",
            "reasons":  reasons[:3],
        }

    # Leverage = 40 / expected_move, min 3x max 20x
    leverage = max(3, min(20, int(40 / max(expected_move, 1))))
    potential_profit = round(expected_move * leverage, 1)

    return {
        "decision":        direction,
        "symbol":          sym,
        "price":           price,
        "entry":           round(entry,    8),
        "sl":              round(sl,       8),
        "tp1":             round(tp1,      8),
        "tp2":             round(tp2,      8),
        "tp3":             round(tp3,      8),
        "expected_move":   round(expected_move, 2),
        "risk_pct":        round(risk_pct,  3),
        "leverage":        leverage,
        "potential_profit":potential_profit,
        "confidence":      confidence,
        "conf_long":       conf_long,
        "conf_short":      conf_short,
        "reasons":         [r for r in reasons][:6],
        "signal_type":     sig_type,
    }


def fmt_signal(res: dict) -> str:
    sym = res.get("symbol", "?")
    dec = res.get("decision", "NO TRADE")
    price = res.get("price", 0)

    if dec == "NO TRADE":
        return (
            f"ANALYSIS: {sym} @ ${price:,.4f}\n\n"
            f"DECISION: NO TRADE\n"
            f"Reason: {res.get('reason', '-')}\n"
            f"LONG={res.get('conf_long',0)}% / SHORT={res.get('conf_short',0)}%"
        )

    return (
        f"*{dec}* `{sym}`\n\n"
        f"Entry: `${res['entry']:,.6f}`\n"
        f"SL: `${res['sl']:,.6f}` (risk {res['risk_pct']:.2f}%)\n\n"
        f"TP1: `${res['tp1']:,.6f}`\n"
        f"TP2: `${res['tp2']:,.6f}`\n"
        f"TP3: `${res['tp3']:,.6f}`\n\n"
        f"Expected Move: `{res['expected_move']}%`\n"
        f"Leverage: `{res['leverage']}x`\n"
        f"Potential Profit: `+{res['potential_profit']}%`\n\n"
        f"Confidence: `{res['confidence']}%`\n\n"
        f"Reasons:\n"
        + "\n".join(f"- {r}" for r in res.get("reasons", [])[:5])
    )


# ?? Trade execution ????????????????????????????????????????????????????????????
async def execute_trade(res: dict, bal_pct: float):
    sym  = res["symbol"]
    side = "BUY" if res["decision"] == "LONG" else "SELL"
    lev  = res["leverage"]
    tp   = res["tp2"]
    sl   = res["sl"]

    try:
        async with httpx.AsyncClient(timeout=10) as c:
            wr = await c.get(f"{API_URL}/api/wallet/agent_main")
            w  = wr.json()

        if not w.get("connected"):
            await send(f"Agent cannot trade `{sym}` - no exchange connected in Mini App API tab")
            return

        balance = float(w.get("balance", 0))
        if balance < 10:
            await send(f"Agent skipped `{sym}` - balance ${balance:.2f} too low")
            return

        amount = round(balance * (bal_pct / 100), 2)
        amount = max(5.0, amount)

        await send(
            f"Agent opening `{side}` `{sym}`\n"
            f"Size: `${amount:.2f}` ({bal_pct}% of `${balance:.2f}`)\n"
            f"Leverage: `{lev}x`\n"
            f"Entry: `${res['entry']:,.4f}`\n"
            f"TP2: `${tp:,.4f}` | SL: `${sl:,.4f}`"
        )

        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(f"{API_URL}/api/trade", json={
                "user_id":  "agent_main",
                "symbol":   sym,
                "side":     side,
                "amount":   amount,
                "leverage": lev,
            })
            result = r.json()

        if result.get("success"):
            t = result.get("trade", {})
            await send(
                f"Position opened\n"
                f"`{side}` `{sym}`\n"
                f"Filled: `${t.get('price', res['entry']):,.4f}`\n"
                f"Qty: `{t.get('qty', 0)}`\n"
                f"ID: `{t.get('id', '-')}`"
            )
        else:
            await send(
                f"Order failed `{sym}`\n"
                f"Error: {result.get('error', 'Unknown')}\n"
                f"Manual: {side} `{sym}` @ `${res['entry']:,.4f}`"
            )

    except Exception as e:
        log.error("execute_trade %s: %s", sym, e)
        await send(f"Trade error `{sym}`: {str(e)[:100]}")


# ?? Parse channel message ??????????????????????????????????????????????????????
def parse_msg(text: str):
    if not text:
        return None, None
    t = text.upper()
    patterns = [
        r"#([A-Z]{2,10}(?:USDT)?)",
        r"\$([A-Z]{2,10})",
        r"\b([A-Z]{2,8})USDT\b",
    ]
    sym = None
    for p in patterns:
        m = re.search(p, t)
        if m:
            tk  = m.group(1)
            sym = tk if tk.endswith("USDT") else tk + "USDT"
            break

    if not sym:
        return None, None

    tl = text.lower()
    sig_type = "pump"
    for w in ["dump", "short", "sell", "drop", "crash", "bear", "fall"]:
        if w in tl:
            sig_type = "dump"
            break

    return sym, sig_type


# ?? Telethon listener ??????????????????????????????????????????????????????????
async def run_telethon():
    if not TG_API_ID or not TG_API_HASH:
        log.warning("TG_API_ID / TG_API_HASH not set - channel monitor disabled")
        await send(
            "Channel monitor NOT started\n"
            "Add to Railway variables:\n"
            "TG_API_ID = your api_id from my.telegram.org\n"
            "TG_API_HASH = your api_hash\n"
            "TG_PHONE = your phone +380..."
        )
        return

    try:
        from telethon import TelegramClient, events

        client = TelegramClient("/tmp/axiflow_session", TG_API_ID, TG_API_HASH)
        await client.start(phone=TG_PHONE)
        log.info("Telethon started - listening %s", MONITOR_CHANNEL)
        await send(
            f"Channel monitor started\n"
            f"Listening: {MONITOR_CHANNEL}\n"
            f"Analyzing every pump/dump signal\n"
            f"Risk per trade: {balance_pct_setting}%"
        )

        @client.on(events.NewMessage(chats=MONITOR_CHANNEL))
        async def on_msg(event):
            global agent_active, balance_pct_setting

            text = event.message.text or ""
            if not text.strip():
                return

            log.info("Channel: %s", text[:80])

            sym, sig_type = parse_msg(text)
            if not sym:
                return

            now = time.time()
            if now - _analyzed.get(sym, 0) < ANALYZE_COOLDOWN:
                remaining = int(ANALYZE_COOLDOWN - (now - _analyzed[sym]))
                log.info("Cooldown %s: %ds left", sym, remaining)
                return

            _analyzed[sym] = now

            await send(
                f"Signal from {MONITOR_CHANNEL}\n"
                f"Symbol: `{sym}` ({sig_type.upper()})\n"
                f"Analyzing..."
            )

            try:
                res = await analyze(sym, sig_type)
                await send(fmt_signal(res))

                if res.get("decision") != "NO TRADE":
                    if agent_active:
                        await execute_trade(res, balance_pct_setting)
                    else:
                        await send(
                            f"Agent is OFF - not trading `{sym}`\n"
                            f"Start agent in Mini App to auto-trade"
                        )

            except Exception as e:
                log.error("Analysis %s: %s", sym, e)
                await send(f"Analysis error `{sym}`: {str(e)[:100]}")

        await client.run_until_disconnected()

    except ImportError:
        await send("Telethon not installed - add to requirements.txt")
    except Exception as e:
        log.error("Telethon: %s", e)
        await send(f"Channel monitor error: {str(e)[:120]}")


# ?? Bot commands ???????????????????????????????????????????????????????????????
async def cmd_start(update, ctx):
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
    kb = [
        [InlineKeyboardButton("Open AXIFLOW TRADE", web_app=WebAppInfo(url=APP_URL))],
        [InlineKeyboardButton("How it works", callback_data="about"),
         InlineKeyboardButton("API setup",    callback_data="api")],
    ]
    await update.message.reply_text(
        "AXIFLOW TRADE v2\n\n"
        "Commands:\n"
        "/setrisk 20 - set % of balance per trade\n"
        "/status     - show current settings\n"
        "/agent on   - enable auto-trading\n"
        "/agent off  - disable auto-trading\n"
        "/analyze SOLUSDT - analyze manually\n\n"
        "Open app:",
        reply_markup=InlineKeyboardMarkup(kb)
    )

async def cmd_setrisk(update, ctx):
    global balance_pct_setting
    args = ctx.args
    if not args:
        await update.message.reply_text(
            f"Current: {balance_pct_setting}% per trade\n"
            f"Usage: /setrisk 20"
        )
        return
    try:
        pct = float(args[0])
        if not (1 <= pct <= 100):
            raise ValueError("must be 1-100")
        balance_pct_setting = pct
        await update.message.reply_text(f"Risk set to {pct}% per trade")
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")

async def cmd_agent(update, ctx):
    global agent_active
    args = ctx.args
    if not args:
        await update.message.reply_text(f"Agent: {'ON' if agent_active else 'OFF'}\n/agent on\n/agent off")
        return
    if args[0].lower() == "on":
        agent_active = True
        await update.message.reply_text(
            f"Agent ON\n"
            f"Risk per trade: {balance_pct_setting}%\n"
            f"Will auto-trade all valid signals"
        )
        await send(f"Agent activated\nRisk: {balance_pct_setting}% per trade")
    elif args[0].lower() == "off":
        agent_active = False
        await update.message.reply_text("Agent OFF - signals only, no trading")
        await send("Agent deactivated - monitoring only")

async def cmd_status(update, ctx):
    await update.message.reply_text(
        f"AXIFLOW Status\n\n"
        f"Agent: {'ON - auto-trading' if agent_active else 'OFF - signals only'}\n"
        f"Risk per trade: {balance_pct_setting}%\n"
        f"Channel: {MONITOR_CHANNEL}\n"
        f"Min confidence: 70%\n"
        f"Min expected move: 2%\n"
        f"Max risk per trade: 1%\n"
        f"Cooldown per symbol: 10 min\n"
        f"Leverage formula: 40 / move%"
    )

async def cmd_analyze(update, ctx):
    args = ctx.args
    if not args:
        await update.message.reply_text("Usage: /analyze SOLUSDT")
        return
    sym = args[0].upper()
    if not sym.endswith("USDT"):
        sym += "USDT"
    await update.message.reply_text(f"Analyzing {sym}...")
    res = await analyze(sym, "pump")
    await send(fmt_signal(res))

async def cmd_cb(update, ctx):
    q = update.callback_query
    await q.answer()
    if q.data == "about":
        await q.edit_message_text(
            "How AXIFLOW v2 works:\n\n"
            "1. Reads @OpenInterestTracker in real time\n"
            "2. Extracts ticker from message\n"
            "3. Full Smart Money analysis:\n"
            "   Pillar 1: OI + Funding + Liquidations (25pts)\n"
            "   Pillar 2: CVD + OB + L/S ratio (25pts)\n"
            "   Pillar 3: Sweeps + FVG + OB zones (25pts)\n"
            "   Pillar 4: Structure + position (25pts)\n"
            "4. Filters: conf 70%+, move 2%+, risk < 1%\n"
            "5. Leverage = 40 / expected move (3-20x)\n"
            "6. Sends signal here\n"
            "7. If agent ON - opens position\n\n"
            "Commands: /agent on/off | /setrisk 20"
        )
    elif q.data == "api":
        await q.edit_message_text(
            "API Key Setup:\n\n"
            "Bybit: bybit.com > API Management\n"
            "Enable Read + Trade. Never Withdraw.\n\n"
            "Binance: binance.com > API Management\n"
            "Enable Futures. Disable Withdrawals.\n\n"
            "Set keys in Mini App API tab."
        )


async def post_init(application):
    if APP_URL:
        try:
            from telegram import MenuButtonWebApp, WebAppInfo
            await application.bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(
                    text="AXIFLOW",
                    web_app=WebAppInfo(url=APP_URL)
                )
            )
        except Exception as e:
            log.warning("Menu button: %s", e)


# ?? Entry ??????????????????????????????????????????????????????????????????????
def main():
    if not BOT_TOKEN:
        log.error("TELEGRAM_BOT_TOKEN not set")
        return

    from telegram.ext import Application, CommandHandler, CallbackQueryHandler

    log.info("AXIFLOW Bot v2 starting...")

    tg_app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    tg_app.add_handler(CommandHandler("start",   cmd_start))
    tg_app.add_handler(CommandHandler("setrisk", cmd_setrisk))
    tg_app.add_handler(CommandHandler("agent",   cmd_agent))
    tg_app.add_handler(CommandHandler("status",  cmd_status))
    tg_app.add_handler(CommandHandler("analyze", cmd_analyze))
    tg_app.add_handler(CallbackQueryHandler(cmd_cb))

    async def run_all():
        async with tg_app:
            await tg_app.start()
            await asyncio.gather(
                tg_app.updater.start_polling(),
                run_telethon(),
            )
            await tg_app.updater.stop()
            await tg_app.stop()

    asyncio.run(run_all())


if __name__ == "__main__":
    main()
