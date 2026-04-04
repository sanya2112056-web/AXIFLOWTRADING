"""
AXIFLOW TRADE - Bot v2
- Reads @OpenInterestTracker in real time via Telethon
- Extracts ticker from #SYMBOL in every message
- Runs FULL Smart Money analysis per the trading prompt
- ALWAYS sends a response (even if NO TRADE - explains why)
- Auto-trades if agent is ON
- Sends all agent actions to Telegram
- Pure ASCII - no latin-1 errors
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

# ?? Config ????????????????????????????????????????????????
BOT_TOKEN       = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID         = os.environ.get("TELEGRAM_CHAT_ID", "")
APP_URL         = os.environ.get("MINI_APP_URL", "")
API_URL         = os.environ.get("API_URL", "http://localhost:3000")
TG_API_ID       = int(os.environ.get("TG_API_ID", "0"))
TG_API_HASH     = os.environ.get("TG_API_HASH", "")
TG_PHONE        = os.environ.get("TG_PHONE", "")
MONITOR_CHANNEL = "@OpenInterestTracker"
ANALYZE_COOLDOWN = 600  # 10 min per symbol

_analyzed: dict = {}
balance_pct_setting: float = 10.0
agent_active: bool = False

# ?? Send TG message ???????????????????????????????????????
async def send(text: str):
    if not BOT_TOKEN or not CHAT_ID:
        return
    safe = text.encode("ascii", errors="replace").decode("ascii")
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            await c.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                json={
                    "chat_id": CHAT_ID,
                    "text": safe,
                    "parse_mode": "Markdown",
                    "disable_web_page_preview": True,
                },
            )
    except Exception as e:
        log.warning("TG send: %s", e)

# ?? Binance Futures data ??????????????????????????????????
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

async def check_symbol_exists(sym):
    """Check if symbol exists on Binance Futures."""
    d = await _get(f"{BFUT}/fapi/v1/exchangeInfo")
    if not d:
        return False
    symbols = [s["symbol"] for s in d.get("symbols", [])]
    return sym in symbols

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
        return {"delta_15m": 0, "delta_1h": 0, "current": 0, "trend": "flat"}
    cur  = float(now["openInterest"])
    vals = [float(h["sumOpenInterest"]) for h in (hist or [])]
    p15  = vals[-3]  if len(vals) >= 3  else cur
    p1h  = vals[-12] if len(vals) >= 12 else cur
    d15  = (cur - p15) / max(p15, 1) * 100
    d1h  = (cur - p1h) / max(p1h, 1) * 100
    return {
        "current":   cur,
        "delta_15m": d15,
        "delta_1h":  d1h,
        "trend":     "rising" if d1h > 2 else "falling" if d1h < -2 else "flat",
    }

async def fetch_funding(sym):
    d  = await _get(f"{BFUT}/fapi/v1/premiumIndex", {"symbol": sym})
    fr = float(d["lastFundingRate"]) if d else 0
    return {
        "rate":          fr,
        "extreme_long":  fr >  0.010,
        "extreme_short": fr < -0.010,
    }

async def fetch_liqs(sym):
    d = await _get(f"{BFUT}/fapi/v1/allForceOrders", {"symbol": sym, "limit": 500})
    if not d:
        return {"ratio": 1.0, "long_vol": 0, "short_vol": 0, "total": 0}
    lv = sum(float(o["origQty"]) * float(o["price"])
             for o in d if o.get("side") == "SELL")
    sv = sum(float(o["origQty"]) * float(o["price"])
             for o in d if o.get("side") == "BUY")
    r  = lv / max(sv, 1)
    return {"ratio": r, "long_vol": lv, "short_vol": sv, "total": lv + sv}

async def fetch_ob(sym):
    d = await _get(f"{BFUT}/fapi/v1/depth", {"symbol": sym, "limit": 100})
    if not d:
        return {"imbalance": 0}
    bv = sum(float(b[1]) for b in d.get("bids", []))
    av = sum(float(a[1]) for a in d.get("asks", []))
    total = bv + av
    return {"bid": bv, "ask": av, "imbalance": (bv - av) / total if total else 0}

async def fetch_ls(sym):
    d = await _get(f"{BFUT}/futures/data/globalLongShortAccountRatio",
                   {"symbol": sym, "period": "5m", "limit": 3})
    if not d:
        return {"ratio": 1.0}
    return {"ratio": float(d[-1].get("longShortRatio", 1))}

# ?? Smart Money detectors ?????????????????????????????????

def compute_cvd(candles):
    if len(candles) < 10:
        return {"divergence": 0, "buying_pressure": 50, "trend": "flat"}
    cvd_vals = []
    cum = 0.0
    for c in candles:
        cum += c["v"] if c["c"] >= c["o"] else -c["v"]
        cvd_vals.append(cum)
    p = min(10, len(candles) - 1)
    pu = candles[-1]["c"] > candles[-p]["c"] * 1.001
    pd = candles[-1]["c"] < candles[-p]["c"] * 0.999
    cu = cvd_vals[-1] > cvd_vals[-p]
    cd = cvd_vals[-1] < cvd_vals[-p]
    div = 0
    if pu and cd: div = -1   # bearish divergence
    if pd and cu: div = 1    # bullish divergence
    bv = sum(c["v"] for c in candles[-10:] if c["c"] >= c["o"])
    sv = sum(c["v"] for c in candles[-10:] if c["c"] <  c["o"])
    bp = bv / (bv + sv) * 100 if (bv + sv) > 0 else 50
    return {
        "divergence":      div,
        "buying_pressure": bp,
        "trend":           "rising" if cu else "falling" if cd else "flat",
    }

def find_liquidity(candles, price):
    """Find equal highs/lows (stop clusters) as liquidity targets."""
    hs  = [c["h"] for c in candles[-60:]]
    ls  = [c["l"] for c in candles[-60:]]
    tol = 0.002
    above = sorted(set(
        round(h, 6) for i, h in enumerate(hs)
        if h > price and sum(1 for hh in hs[i+1:] if abs(hh - h) / max(h, 1) < tol) >= 2
    ))
    below = sorted(set(
        round(l, 6) for i, l in enumerate(ls)
        if l < price and sum(1 for ll in ls[i+1:] if abs(ll - l) / max(l, 1) < tol) >= 2
    ), reverse=True)
    return {
        "above":    above[:3],
        "below":    below[:3],
        "day_high": max(hs),
        "day_low":  min(ls),
    }

def find_fvg(candles, price):
    """Fair Value Gaps - imbalance zones."""
    bull_fvg = None
    bear_fvg = None
    data = candles[-40:]
    for i in range(2, len(data)):
        c0 = data[i-2]; c2 = data[i]
        if c2["l"] > c0["h"] and c0["h"] < price:
            bull_fvg = {
                "top": c2["l"], "bot": c0["h"],
                "mid": (c2["l"] + c0["h"]) / 2,
                "size": (c2["l"] - c0["h"]) / max(c0["h"], 1) * 100,
            }
        elif c2["h"] < c0["l"] and c0["l"] > price:
            bear_fvg = {
                "top": c0["l"], "bot": c2["h"],
                "mid": (c0["l"] + c2["h"]) / 2,
                "size": (c0["l"] - c2["h"]) / max(c0["l"], 1) * 100,
            }
    return {"bull": bull_fvg, "bear": bear_fvg}

def find_ob(candles, price):
    """Order Blocks - last candle before strong move."""
    data = candles[-60:]
    avg  = sum(abs(c["c"] - c["o"]) for c in data) / max(len(data), 1)
    bull = None; bear = None
    for i in range(1, len(data) - 1):
        c = data[i]; n = data[i+1]; nb = abs(n["c"] - n["o"])
        if c["c"] < c["o"] and n["c"] > n["o"] and nb > avg * 1.5:
            if c["o"] < price:
                bull = {"top": c["o"], "bot": c["c"], "mid": (c["o"] + c["c"]) / 2}
        elif c["c"] > c["o"] and n["c"] < n["o"] and nb > avg * 1.5:
            if c["o"] > price:
                bear = {"top": c["c"], "bot": c["o"], "mid": (c["c"] + c["o"]) / 2}
    return {"bull": bull, "bear": bear}

def detect_sweep(candles):
    """Liquidity sweep - price takes stops then reverses."""
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
        "prev_high": ph,
        "prev_low":  pl,
    }

def detect_market_context(candles, oi_delta):
    """
    Determine: Continuation / Manipulation / Reversal
    Based on OI + price + CVD.
    """
    if len(candles) < 20:
        return "unknown"
    price_change = (candles[-1]["c"] - candles[-10]["c"]) / max(candles[-10]["c"], 1) * 100
    vols = [c["v"] for c in candles[-10:]]
    avg_vol = sum(vols) / len(vols) if vols else 1

    if abs(oi_delta) > 2 and abs(price_change) > 1:
        return "continuation"
    if oi_delta > 2 and abs(price_change) < 0.5:
        return "manipulation"  # OI rising but price stuck = accumulation before move
    if abs(price_change) > 3 and oi_delta < 0:
        return "reversal_likely"
    return "consolidation"

# ?? Full analysis per the trading prompt ?????????????????

async def full_analysis(sym: str, sig_type: str, channel_oi_data: dict) -> dict:
    """
    Full Smart Money analysis:
    - Collects all data
    - Applies 4-pillar confidence (OI 25% + CVD 25% + Liq 25% + Structure 25%)
    - Determines scenario (Continuation/Reversal)
    - Filters: confidence >=70%, expected move >=2%, risk <=1%
    - ALWAYS returns a result with explanation
    """
    result_base = {
        "symbol":    sym,
        "sig_type":  sig_type,
        "decision":  "NO TRADE",
        "reasons":   [],
        "filters":   [],
        "scenario":  "unknown",
        "data":      {},
    }

    # Step 1: Check symbol exists on Binance Futures
    exists = await check_symbol_exists(sym)
    if not exists:
        result_base["decision"]   = "NO TRADE"
        result_base["skip_reason"] = f"{sym} not found on Binance Futures"
        result_base["filters"].append(f"{sym} not listed on Binance Futures - cannot analyze")
        return result_base

    # Step 2: Fetch all data in parallel
    try:
        ticker, c5m, c1m, oi, funding, liqs, ob_data, ls = await asyncio.gather(
            fetch_ticker(sym),
            fetch_klines(sym, "5m", 100),
            fetch_klines(sym, "1m", 60),
            fetch_oi(sym),
            fetch_funding(sym),
            fetch_liqs(sym),
            fetch_ob(sym),
            fetch_ls(sym),
        )
    except Exception as e:
        result_base["skip_reason"] = f"Data fetch error: {e}"
        return result_base

    if not ticker or not c5m:
        result_base["skip_reason"] = f"No market data for {sym}"
        return result_base

    price  = ticker["price"]
    change = ticker["change"]
    volume = ticker["volume"]

    # Step 3: Compute all SM indicators
    cvd    = compute_cvd(c5m)
    liq    = find_liquidity(c5m, price)
    fvg    = find_fvg(c5m, price)
    ob     = find_ob(c5m, price)
    sweep  = detect_sweep(c5m)

    d15    = oi["delta_15m"]
    d1h    = oi["delta_1h"]
    fr     = funding["rate"]
    liq_r  = liqs["ratio"]
    im     = ob_data["imbalance"]
    ls_r   = ls["ratio"]
    bp     = cvd["buying_pressure"]

    # Use channel data if available (OI from channel message)
    if channel_oi_data.get("oi_15m_pct"):
        d15 = channel_oi_data["oi_15m_pct"]
    if channel_oi_data.get("oi_1h_pct"):
        d1h = channel_oi_data["oi_1h_pct"]

    context = detect_market_context(c5m, d15)

    # Step 4: Determine scenario
    # Continuation Long: pump + OI up + CVD up + short liqs
    # Continuation Short: dump + OI up + CVD down + long liqs
    # Reversal Short: strong pump + CVD divergence + sweep high
    # Reversal Long: strong dump + CVD up + sweep low
    scenario = "none"
    if sig_type == "pump":
        if d15 > 2 and cvd["trend"] == "rising" and liq_r > 1.5:
            scenario = "CONTINUATION_LONG"
        elif d15 > 3 and cvd["divergence"] == -1 and sweep["bear"]:
            scenario = "REVERSAL_SHORT"
        elif d15 > 2 and cvd["trend"] == "rising":
            scenario = "CONTINUATION_LONG"
    elif sig_type == "dump":
        if d15 > 0 and cvd["trend"] == "falling" and liq_r < 0.7:
            scenario = "CONTINUATION_SHORT"
        elif liqs["total"] > 0 and cvd["divergence"] == 1 and sweep["bull"]:
            scenario = "REVERSAL_LONG"
        elif d15 < -2 and cvd["trend"] == "falling":
            scenario = "CONTINUATION_SHORT"

    # Step 5: 4-Pillar Confidence
    reasons_long  = []; reasons_short = []
    ol = 0; os_ = 0   # OI pillar
    cl = 0; cs  = 0   # CVD pillar
    ll = 0; ls2 = 0   # Liquidity pillar
    stl = 0; sts = 0  # Structure pillar

    # --- PILLAR 1: OI (25pts) ---
    if d15 > 5:
        ol += 20; reasons_long.append(f"OI +{d15:.1f}% spike - aggressive longs entering")
    elif d15 > 2 and change > 0:
        ol += 14; reasons_long.append(f"OI +{d15:.1f}% + price up - long accumulation")
    elif d15 > 2 and change < 0:
        os_ += 14; reasons_short.append(f"OI +{d15:.1f}% + price down - shorts building")
    elif d15 < -3:
        os_ += 14; reasons_short.append(f"OI -{abs(d15):.1f}% - longs closing, bearish")
    elif d15 < -1:
        os_ += 8; reasons_short.append(f"OI -{abs(d15):.1f}% - slight OI reduction")

    if funding["extreme_short"]:
        ol  += 12; reasons_long.append(f"Funding oversold {fr*100:.4f}% - long squeeze incoming")
    elif funding["extreme_long"]:
        os_ += 12; reasons_short.append(f"Funding overbought {fr*100:.4f}% - short squeeze incoming")

    if liq_r > 3:
        ol  += 10; reasons_long.append(f"Mass long liqs x{liq_r:.1f} - bull reversal pressure")
    elif liq_r > 2:
        ol  += 6;  reasons_long.append(f"Long liqs x{liq_r:.1f} - bullish signal")
    elif liq_r < 0.33:
        os_ += 10; reasons_short.append(f"Mass short liqs x{1/max(liq_r,.001):.1f} - bear pressure")
    elif liq_r < 0.5:
        os_ += 6;  reasons_short.append(f"Short liqs ratio {liq_r:.2f} - bearish signal")

    if ls_r < 0.7:
        ol  += 5; reasons_long.append(f"L/S ratio {ls_r:.2f} - crowd short = squeeze up")
    elif ls_r > 1.4:
        os_ += 5; reasons_short.append(f"L/S ratio {ls_r:.2f} - crowd long = squeeze down")

    # --- PILLAR 2: CVD (25pts) ---
    if cvd["divergence"] == 1:
        cl += 20; reasons_long.append("CVD bullish div: price down but buying pressure UP")
    elif cvd["divergence"] == -1:
        cs += 20; reasons_short.append("CVD bearish div: price up but selling pressure UP")
    if bp > 65:
        cl += 12; reasons_long.append(f"Buy pressure {bp:.0f}% - aggressive buyers dominating")
    elif bp > 55:
        cl += 6;  reasons_long.append(f"Buy pressure {bp:.0f}% - slight long bias")
    elif bp < 35:
        cs += 12; reasons_short.append(f"Sell pressure {100-bp:.0f}% - aggressive sellers dominating")
    elif bp < 45:
        cs += 6;  reasons_short.append(f"Sell pressure {100-bp:.0f}% - slight short bias")
    if im > 0.2:
        cl += 8; reasons_long.append(f"OB strong buy imbalance {im:+.2f}")
    elif im > 0.1:
        cl += 4; reasons_long.append(f"OB buy imbalance {im:+.2f}")
    elif im < -0.2:
        cs += 8; reasons_short.append(f"OB strong sell imbalance {im:+.2f}")
    elif im < -0.1:
        cs += 4; reasons_short.append(f"OB sell imbalance {im:+.2f}")

    # --- PILLAR 3: LIQUIDITY (25pts) ---
    if sweep["bull"]:
        ll  += 20; reasons_long.append("Liquidity Sweep: stops below taken, price reversed UP")
    if sweep["bear"]:
        ls2 += 20; reasons_short.append("Liquidity Sweep: stops above taken, price reversed DOWN")
    if fvg["bull"]:
        ll  += 8; reasons_long.append(f"Bullish FVG zone: {fvg['bull']['bot']:.6f}-{fvg['bull']['top']:.6f}")
    if fvg["bear"]:
        ls2 += 8; reasons_short.append(f"Bearish FVG zone: {fvg['bear']['bot']:.6f}-{fvg['bear']['top']:.6f}")
    if ob["bull"]:
        ll  += 7; reasons_long.append(f"Bullish OB: {ob['bull']['bot']:.6f}-{ob['bull']['top']:.6f}")
    if ob["bear"]:
        ls2 += 7; reasons_short.append(f"Bearish OB: {ob['bear']['bot']:.6f}-{ob['bear']['top']:.6f}")
    if liq["above"]:
        ll  += 5; reasons_long.append(f"Liquidity pool above: {liq['above'][0]:.6f} - magnet target")
    if liq["below"]:
        ls2 += 5; reasons_short.append(f"Liquidity pool below: {liq['below'][0]:.6f} - magnet target")

    # --- PILLAR 4: STRUCTURE (25pts) ---
    pp = (price - liq["day_low"]) / max(liq["day_high"] - liq["day_low"], 0.001) * 100
    if pp < 20:
        stl += 15; reasons_long.append(f"Price near 24H low ({pp:.0f}%) - reversal zone")
    elif pp < 35:
        stl += 8;  reasons_long.append(f"Price in lower 24H range ({pp:.0f}%) - long bias")
    elif pp > 80:
        sts += 15; reasons_short.append(f"Price near 24H high ({pp:.0f}%) - reversal zone")
    elif pp > 65:
        sts += 8;  reasons_short.append(f"Price in upper 24H range ({pp:.0f}%) - short bias")

    # Scenario bonus
    if scenario == "CONTINUATION_LONG":
        stl += 12; reasons_long.append("Scenario: CONTINUATION LONG (OI+CVD+Liqs aligned)")
    elif scenario == "CONTINUATION_SHORT":
        sts += 12; reasons_short.append("Scenario: CONTINUATION SHORT (OI+CVD+Liqs aligned)")
    elif scenario == "REVERSAL_LONG":
        stl += 15; reasons_long.append("Scenario: REVERSAL LONG (post-dump, sweep + CVD div)")
    elif scenario == "REVERSAL_SHORT":
        sts += 15; reasons_short.append("Scenario: REVERSAL SHORT (post-pump, sweep + CVD div)")

    # Context
    if context == "manipulation":
        stl += 8; sts += 8
        reasons_long.append("Context: MANIPULATION - OI building, move incoming soon")

    # Anti-pump rule: don't enter after 3-5% move without pullback
    anti_pump_filter = False
    if sig_type == "pump" and change > 3 and not sweep["bear"] and not fvg["bull"]:
        anti_pump_filter = True
    if sig_type == "dump" and change < -3 and not sweep["bull"] and not fvg["bear"]:
        anti_pump_filter = True

    # Step 6: Final confidence scores
    conf_long  = int(min(25, ol) + min(25, cl) + min(25, ll) + min(25, stl))
    conf_short = int(min(25, os_) + min(25, cs) + min(25, ls2) + min(25, sts))

    # Step 7: Decision
    direction  = None
    confidence = 0
    reasons    = []

    if anti_pump_filter:
        result_base["filters"].append(
            f"ANTI-PUMP RULE: {change:.1f}% move already happened without pullback - NOT ENTERING"
        )

    if not anti_pump_filter:
        if conf_long >= 70 and conf_long > conf_short:
            direction = "LONG"; confidence = conf_long; reasons = reasons_long
        elif conf_short >= 70 and conf_short > conf_long:
            direction = "SHORT"; confidence = conf_short; reasons = reasons_short

    # Step 8: TP / SL calculation (if entering)
    entry = price; sl_val = 0; tp1 = 0; tp2 = 0; tp3 = 0
    leverage = 0; expected_move = 0; risk_pct = 0; potential_profit = 0

    if direction:
        # Entry zone
        if direction == "LONG":
            if fvg["bull"] and abs(fvg["bull"]["mid"] - price) / max(price, 1) < 0.01:
                entry = fvg["bull"]["mid"]
            elif ob["bull"] and abs(ob["bull"]["mid"] - price) / max(price, 1) < 0.01:
                entry = ob["bull"]["mid"]
        else:
            if fvg["bear"] and abs(fvg["bear"]["mid"] - price) / max(price, 1) < 0.01:
                entry = fvg["bear"]["mid"]
            elif ob["bear"] and abs(ob["bear"]["mid"] - price) / max(price, 1) < 0.01:
                entry = ob["bear"]["mid"]

        # SL - behind structure
        recent  = c5m[-20:]
        avg_rng = sum(c["h"] - c["l"] for c in recent) / max(len(recent), 1)
        atr     = avg_rng * 1.2

        if direction == "LONG":
            sl_candidates = [entry - atr * 1.5]
            if ob["bull"]:  sl_candidates.append(ob["bull"]["bot"] * 0.997)
            if fvg["bull"]: sl_candidates.append(fvg["bull"]["bot"] * 0.997)
            sl_val = max(sl_candidates)
        else:
            sl_candidates = [entry + atr * 1.5]
            if ob["bear"]:  sl_candidates.append(ob["bear"]["top"] * 1.003)
            if fvg["bear"]: sl_candidates.append(fvg["bear"]["top"] * 1.003)
            sl_val = min(sl_candidates)

        risk_pct = abs(entry - sl_val) / max(entry, 1) * 100

        # TP targets = liquidity pools
        sl_d = abs(entry - sl_val)
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
        leverage      = max(3, min(20, int(40 / max(expected_move, 0.1))))
        potential_profit = round(expected_move * leverage, 1)

        # Apply filters
        if risk_pct > 1.0:
            result_base["filters"].append(f"RISK FILTER: {risk_pct:.2f}% > 1% max - NOT ENTERING")
            direction = None
        elif expected_move < 2.0:
            result_base["filters"].append(f"MOVE FILTER: Expected {expected_move:.1f}% < 2% min - NOT ENTERING")
            direction = None

    # Step 9: Build full result
    result = {
        "symbol":           sym,
        "sig_type":         sig_type,
        "decision":         direction if direction else "NO TRADE",
        "scenario":         scenario,
        "context":          context,
        "confidence":       confidence,
        "conf_long":        conf_long,
        "conf_short":       conf_short,
        "price":            price,
        "change_24h":       change,
        "volume_24h":       volume,
        "entry":            round(entry,    8) if direction else 0,
        "sl":               round(sl_val,   8) if direction else 0,
        "tp1":              round(tp1,      8) if direction else 0,
        "tp2":              round(tp2,      8) if direction else 0,
        "tp3":              round(tp3,      8) if direction else 0,
        "expected_move":    round(expected_move, 2),
        "risk_pct":         round(risk_pct,  3),
        "leverage":         leverage,
        "potential_profit": potential_profit,
        "reasons":          reasons[:6],
        "filters":          result_base.get("filters", []),
        "data": {
            "oi_15m":          d15,
            "oi_1h":           d1h,
            "funding":         fr,
            "liq_ratio":       liq_r,
            "ob_imb":          im,
            "cvd_div":         cvd["divergence"],
            "buying_pressure": bp,
            "ls_ratio":        ls_r,
            "sweep_bull":      sweep["bull"],
            "sweep_bear":      sweep["bear"],
            "has_fvg_bull":    fvg["bull"] is not None,
            "has_fvg_bear":    fvg["bear"] is not None,
            "liq_above":       liq["above"],
            "liq_below":       liq["below"],
            "day_high":        liq["day_high"],
            "day_low":         liq["day_low"],
            "price_position":  round((price - liq["day_low"]) / max(liq["day_high"] - liq["day_low"], 0.001) * 100, 1),
        },
    }

    if "skip_reason" in result_base:
        result["skip_reason"] = result_base["skip_reason"]

    return result


def format_result(res: dict) -> str:
    """
    Format analysis result for Telegram.
    ALWAYS sends something - even NO TRADE gets full explanation.
    """
    sym    = res.get("symbol", "?")
    dec    = res.get("decision", "NO TRADE")
    price  = res.get("price", 0)
    sig    = res.get("sig_type", "").upper()
    scene  = res.get("scenario", "none")
    ctx    = res.get("context", "")
    data   = res.get("data", {})
    cl     = res.get("conf_long", 0)
    cs     = res.get("conf_short", 0)

    # Skip reason (symbol not on exchange etc)
    if res.get("skip_reason"):
        return (
            f"Analysis: `{sym}` ({sig})\n\n"
            f"SKIP: {res['skip_reason']}\n\n"
            f"No data available - cannot analyze."
        )

    # Header with market overview (always shown)
    pp    = data.get("price_position", 50)
    d15   = data.get("oi_15m", 0)
    bp    = data.get("buying_pressure", 50)
    fr    = data.get("funding", 0)
    sweep = "Bull sweep" if data.get("sweep_bull") else "Bear sweep" if data.get("sweep_bear") else "No sweep"
    liq_a = data.get("liq_above", [])
    liq_b = data.get("liq_below", [])

    header = (
        f"Analysis: `{sym}` | Signal: *{sig}*\n"
        f"Price: `${price:,.6f}` | 24H: `{res.get('change_24h', 0):+.2f}%`\n"
        f"Position in range: `{pp:.0f}%` (0=low, 100=high)\n\n"
        f"Market data:\n"
        f"OI 15m: `{d15:+.2f}%` | Scenario: `{scene}`\n"
        f"CVD: `{data.get('cvd_div', 0):+d}` | Buy pressure: `{bp:.0f}%`\n"
        f"Funding: `{fr*100:.4f}%` | L/S: `{data.get('ls_ratio', 1):.2f}`\n"
        f"Liq sweep: `{sweep}`\n"
    )

    if liq_a:
        header += f"Liq pools above: `{', '.join(f'${x:.4f}' for x in liq_a[:2])}`\n"
    if liq_b:
        header += f"Liq pools below: `{', '.join(f'${x:.4f}' for x in liq_b[:2])}`\n"

    header += (
        f"\nConfidence: LONG `{cl}%` | SHORT `{cs}%`\n"
        f"(need 70%+ to enter)\n"
    )

    if dec == "NO TRADE":
        # Explain WHY we are not entering
        filters = res.get("filters", [])
        reasons = res.get("reasons", [])

        why_not = ""
        if cl < 70 and cs < 70:
            if cl >= cs:
                why_not = f"Confidence too low: LONG {cl}% / SHORT {cs}%\nNeed 70%+ in one direction."
            else:
                why_not = f"Confidence too low: SHORT {cs}% / LONG {cl}%\nNeed 70%+ in one direction."
        elif filters:
            why_not = "\n".join(filters)
        else:
            why_not = f"No clear setup. LONG={cl}% SHORT={cs}%"

        return (
            header +
            f"\nDECISION: *NO TRADE*\n\n"
            f"Why not entering:\n{why_not}\n\n"
            f"What to watch:\n"
            + _what_to_watch(data, scene, sig)
        )

    # TRADE signal
    e1 = abs(res["tp1"] - res["entry"]) / max(res["entry"], 1) * 100
    e2 = abs(res["tp2"] - res["entry"]) / max(res["entry"], 1) * 100
    e3 = abs(res["tp3"] - res["entry"]) / max(res["entry"], 1) * 100

    reasons_txt = "\n".join(f"- {r}" for r in res.get("reasons", [])[:5])

    return (
        header +
        f"\nDECISION: *{dec}*\n\n"
        f"Entry zone: `${res['entry']:,.6f}`\n"
        f"Stop Loss: `${res['sl']:,.6f}` (risk `{res['risk_pct']:.2f}%`)\n\n"
        f"TP1: `${res['tp1']:,.6f}` (+{e1:.1f}%)\n"
        f"TP2: `${res['tp2']:,.6f}` (+{e2:.1f}%)\n"
        f"TP3: `${res['tp3']:,.6f}` (+{e3:.1f}%)\n\n"
        f"Expected Move: `{res['expected_move']}%`\n"
        f"Leverage: `{res['leverage']}x`\n"
        f"Potential Profit: `+{res['potential_profit']}%`\n\n"
        f"Confidence: `{res['confidence']}%` | Scenario: `{scene}`\n\n"
        f"Reasons:\n{reasons_txt}"
    )


def _what_to_watch(data: dict, scenario: str, sig_type: str) -> str:
    """Give actionable advice even on NO TRADE."""
    tips = []
    if scenario == "manipulation":
        tips.append("OI building but price stuck - wait for the move direction to confirm")
    if not data.get("sweep_bull") and not data.get("sweep_bear"):
        tips.append("No liquidity sweep yet - wait for stop hunt before entering")
    if not data.get("has_fvg_bull") and not data.get("has_fvg_bear"):
        tips.append("No FVG/OB zone nearby - entry zone not defined")
    liq_a = data.get("liq_above", [])
    liq_b = data.get("liq_below", [])
    if liq_a:
        tips.append(f"Liquidity above at ${liq_a[0]:.4f} - possible magnet for price")
    if liq_b:
        tips.append(f"Liquidity below at ${liq_b[0]:.4f} - possible sweep target")
    if not tips:
        tips.append("Wait for clearer confluence of OI + CVD + Sweep + FVG")
    return "\n".join(f"- {t}" for t in tips[:4])


# ?? Trade execution ???????????????????????????????????????

async def execute_trade(res: dict, bal_pct: float):
    """Execute via main server API. Sends all actions to TG."""
    if res.get("decision") == "NO TRADE":
        return
    sym  = res["symbol"]
    side = "BUY" if res["decision"] == "LONG" else "SELL"
    lev  = res["leverage"]

    try:
        async with httpx.AsyncClient(timeout=10) as c:
            wr = await c.get(f"{API_URL}/api/wallet/agent_main")
            w  = wr.json()

        if not w.get("connected"):
            await send(f"Agent cannot trade `{sym}` - no exchange connected in Mini App")
            return

        balance = float(w.get("balance", 0))
        if balance < 10:
            await send(f"Agent skipped `{sym}` - balance `${balance:.2f}` too low")
            return

        amount = max(5.0, round(balance * (bal_pct / 100), 2))

        await send(
            f"Agent opening `{side}` `{sym}`\n"
            f"Size: `${amount:.2f}` ({bal_pct}% of `${balance:.2f}`)\n"
            f"Leverage: `{lev}x`\n"
            f"Entry: `${res['entry']:,.6f}`\n"
            f"TP2: `${res['tp2']:,.6f}` | SL: `${res['sl']:,.6f}`"
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
                f"Filled at: `${t.get('price', res['entry']):,.6f}`\n"
                f"Qty: `{t.get('qty', 0)}`\n"
                f"Order ID: `{t.get('id', '-')}`\n"
                f"TP: `${res['tp2']:,.6f}` | SL: `${res['sl']:,.6f}`"
            )
        else:
            await send(
                f"Order FAILED for `{sym}`\n"
                f"Error: {result.get('error', 'Unknown error')}\n"
                f"Enter manually: {side} `{sym}` @ `${res['entry']:,.6f}`"
            )

    except Exception as e:
        log.error("execute_trade %s: %s", sym, e)
        await send(f"Trade error `{sym}`: {str(e)[:100]}")


# ?? Parse channel message ?????????????????????????????????

def parse_channel_msg(text: str):
    """
    Extract ticker and signal type from @OpenInterestTracker messages.
    Format: #RLSUSDT open interest decreased/increased X% in Y mins!
    """
    if not text:
        return None, None, {}

    text_upper = text.upper()

    # Extract #TICKER
    m = re.search(r"#([A-Z0-9]{2,15})", text_upper)
    if not m:
        return None, None, {}

    ticker = m.group(1)
    sym    = ticker if ticker.endswith("USDT") else ticker + "USDT"

    # Detect pump/dump from message
    text_lower = text.lower()
    if any(w in text_lower for w in ["decreased", "dropped", "dump", "fall", "bearish", "lost"]):
        sig_type = "dump"
    elif any(w in text_lower for w in ["increased", "surged", "pump", "rise", "bullish", "gained"]):
        sig_type = "pump"
    else:
        sig_type = "pump"  # default for OpenInterestTracker

    # Extract OI data from message if present
    channel_oi = {}
    m15 = re.search(r"OI 15m.*?([+-]?\d+\.?\d*)%", text)
    m1h = re.search(r"OI 1H.*?([+-]?\d+\.?\d*)%", text)
    if m15:
        try: channel_oi["oi_15m_pct"] = float(m15.group(1))
        except Exception: pass
    if m1h:
        try: channel_oi["oi_1h_pct"] = float(m1h.group(1))
        except Exception: pass

    return sym, sig_type, channel_oi


# ?? Telethon channel reader ???????????????????????????????

async def run_telethon():
    if not TG_API_ID or not TG_API_HASH:
        log.warning("TG_API_ID/TG_API_HASH not set")
        await send(
            "Channel monitor NOT started\n"
            "Add to Railway Variables:\n"
            "TG_API_ID = your id from my.telegram.org\n"
            "TG_API_HASH = your hash\n"
            "TG_PHONE = +380..."
        )
        return

    try:
        from telethon import TelegramClient, events

        client = TelegramClient("/tmp/axiflow_session", TG_API_ID, TG_API_HASH)
        await client.start(phone=TG_PHONE)
        log.info("Telethon started - monitoring %s", MONITOR_CHANNEL)
        await send(
            f"Channel monitor started\n"
            f"Monitoring: {MONITOR_CHANNEL}\n"
            f"Every message will be analyzed\n"
            f"Agent: {'ON' if agent_active else 'OFF'} | Risk: {balance_pct_setting}%"
        )

        @client.on(events.NewMessage(chats=MONITOR_CHANNEL))
        async def on_new_message(event):
            global agent_active, balance_pct_setting

            text = event.message.text or ""
            if not text.strip():
                return

            log.info("Channel message: %s", text[:100])

            sym, sig_type, channel_oi = parse_channel_msg(text)
            if not sym:
                log.info("No ticker found in message - skipping")
                return

            # Cooldown check
            now = time.time()
            if now - _analyzed.get(sym, 0) < ANALYZE_COOLDOWN:
                remaining = int(ANALYZE_COOLDOWN - (now - _analyzed[sym]))
                log.info("Cooldown %s: %ds remaining", sym, remaining)
                return

            _analyzed[sym] = now

            await send(
                f"Signal received from {MONITOR_CHANNEL}\n"
                f"Ticker: `{sym}` | Type: *{sig_type.upper()}*\n"
                f"Running full analysis..."
            )

            try:
                res = await full_analysis(sym, sig_type, channel_oi)
                msg = format_result(res)
                await send(msg)

                # Auto-trade if agent is active
                if res.get("decision") != "NO TRADE":
                    if agent_active:
                        await execute_trade(res, balance_pct_setting)
                    else:
                        await send(
                            f"Agent is OFF - signal not traded automatically\n"
                            f"Use /agent on to enable auto-trading\n"
                            f"Or open Mini App to trade manually"
                        )

            except Exception as e:
                log.error("Analysis error %s: %s", sym, e)
                await send(f"Analysis error for `{sym}`: {str(e)[:120]}")

        await client.run_until_disconnected()

    except ImportError:
        await send("Telethon not installed - add telethon==1.34.0 to requirements.txt")
    except Exception as e:
        log.error("Telethon: %s", e)
        await send(f"Channel monitor error: {str(e)[:150]}")


# ?? Bot commands ??????????????????????????????????????????

async def cmd_start(update, ctx):
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
    kb = [
        [InlineKeyboardButton("Open AXIFLOW TRADE", web_app=WebAppInfo(url=APP_URL))],
        [InlineKeyboardButton("How it works", callback_data="about")],
    ]
    await update.message.reply_text(
        "AXIFLOW TRADE v2\n\n"
        "Monitors @OpenInterestTracker\n"
        "Analyzes EVERY signal with full SM logic\n"
        "Always sends result - even NO TRADE\n\n"
        "Commands:\n"
        "/setrisk 20  - 20% of balance per trade\n"
        "/agent on    - enable auto-trading\n"
        "/agent off   - disable auto-trading\n"
        "/analyze SOLUSDT - analyze manually\n"
        "/status      - current settings\n",
        reply_markup=InlineKeyboardMarkup(kb)
    )

async def cmd_setrisk(update, ctx):
    global balance_pct_setting
    args = ctx.args
    if not args:
        await update.message.reply_text(f"Current: {balance_pct_setting}% per trade\nUsage: /setrisk 20")
        return
    try:
        pct = float(args[0])
        if not 1 <= pct <= 100:
            raise ValueError("must be 1-100")
        balance_pct_setting = pct
        await update.message.reply_text(f"Position size set to {pct}% per trade")
        await send(f"Position size updated: {pct}% of balance per trade")
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
        await update.message.reply_text(f"Agent ON\nRisk per trade: {balance_pct_setting}%\nWill auto-trade all valid signals")
        await send(f"Agent ACTIVATED\nAuto-trading ON\nRisk: {balance_pct_setting}% per trade")
    elif args[0].lower() == "off":
        agent_active = False
        await update.message.reply_text("Agent OFF - analysis only, no auto-trading")
        await send("Agent DEACTIVATED - monitoring only")

async def cmd_status(update, ctx):
    await update.message.reply_text(
        f"AXIFLOW Status\n\n"
        f"Agent: {'ON - auto-trading' if agent_active else 'OFF - monitor only'}\n"
        f"Position size: {balance_pct_setting}% per trade\n"
        f"Channel: {MONITOR_CHANNEL}\n"
        f"Min confidence: 70%\n"
        f"Min expected move: 2%\n"
        f"Max risk per trade: 1%\n"
        f"Cooldown per symbol: 10 min\n"
        f"Leverage formula: 40 / expected_move%\n"
        f"Always responds - even NO TRADE"
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
    try:
        res = await full_analysis(sym, "pump", {})
        await send(format_result(res))
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")

async def cmd_cb(update, ctx):
    q = update.callback_query
    await q.answer()
    if q.data == "about":
        await q.edit_message_text(
            "AXIFLOW v2 - How it works:\n\n"
            "1. Reads @OpenInterestTracker real-time\n"
            "2. Extracts #TICKER from every message\n"
            "3. Collects all market data:\n"
            "   - OI, Funding, Liquidations, L/S\n"
            "   - CVD, OB imbalance, Buy pressure\n"
            "   - Liquidity sweeps, FVG, OB zones\n"
            "   - Equal highs/lows (stop clusters)\n"
            "4. Determines scenario:\n"
            "   Continuation / Reversal / Manipulation\n"
            "5. 4-pillar confidence (OI+CVD+Liq+Structure)\n"
            "6. Filters: 70%+ conf, 2%+ move, <1% risk\n"
            "7. Calculates: Entry / SL / TP1/2/3 / Leverage\n"
            "8. ALWAYS sends result with explanation\n"
            "9. If agent ON - opens position\n\n"
            "/setrisk 20 | /agent on | /analyze SOLUSDT"
        )

async def post_init(app):
    if APP_URL:
        try:
            from telegram import MenuButtonWebApp, WebAppInfo
            await app.bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(text="AXIFLOW", web_app=WebAppInfo(url=APP_URL))
            )
        except Exception as e:
            log.warning("Menu button: %s", e)


# ?? Main ??????????????????????????????????????????????????

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
