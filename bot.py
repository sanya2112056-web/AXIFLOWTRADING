"""
AXIFLOW TRADE - Professional AI Trading Agent
Reads @OpenInterestTracker channel
Analyzes per Smart Money prompt
Always returns decision (ENTER or NO TRADE)
Executes via MEXC/Bybit/Binance futures
Pure ASCII source - no latin-1 errors
"""
import asyncio
import os
import re
import time
import logging
import httpx

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)
log = logging.getLogger("axiflow")

# ?? Config ????????????????????????????????????????????????????????????????????
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")
APP_URL   = os.environ.get("MINI_APP_URL", "")
API_URL   = os.environ.get("API_URL", "http://localhost:3000")

TG_API_ID   = int(os.environ.get("TG_API_ID", "0"))
TG_API_HASH = os.environ.get("TG_API_HASH", "")
TG_PHONE    = os.environ.get("TG_PHONE", "")

CHANNEL = "@OpenInterestTracker"
BFUT    = "https://fapi.binance.com"

# Agent state
_position_pct: float = 10.0   # % of balance per trade
_agent_on:     bool  = False   # auto-trading on/off
_cooldown:     dict  = {}      # sym -> last_signal_ts
_positions:    dict  = {}      # sym -> position info
COOLDOWN_SEC   = 600           # 10 min per symbol
MAX_POSITIONS  = 2             # max simultaneous positions


# ?? Telegram ??????????????????????????????????????????????????????????????????
async def tg(text: str):
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
        log.warning("TG error: %s", e)


# ?? HTTP ??????????????????????????????????????????????????????????????????????
async def get(url: str, params: dict = None):
    try:
        async with httpx.AsyncClient(timeout=9) as c:
            r = await c.get(url, params=params)
            r.raise_for_status()
            return r.json()
    except Exception as e:
        log.debug("GET %s: %s", url, e)
        return None


# ?? PARSE CHANNEL MESSAGE ?????????????????????????????????????????????????????
def parse_msg(text: str) -> dict | None:
    """
    Parse @OpenInterestTracker message.

    Example format:
    [pump] #APTUSDT open interest increased 8.32% in 15 mins!
    OI 5m:  +44,200 USD (+1.2%)
    OI 15m: +310,500 USD (+8.32%)
    OI 1H:  +890,000 USD (+24.1%)
    CP: 9.842
    5m:  9.798 (-0.45%)
    15m: 9.107 (-7.5%)
    FR: 0.0262%
    """
    if not text or len(text) < 5:
        return None

    # Extract ticker
    m = re.search(r"#([A-Z]{2,12}(?:USDT)?)", text.upper())
    if not m:
        return None
    raw_ticker = m.group(1)
    symbol = raw_ticker if raw_ticker.endswith("USDT") else raw_ticker + "USDT"

    # Signal type
    tl = text.lower()
    if any(w in tl for w in ["increased", "increase", "pump", "spike up", "surge"]):
        sig_type = "pump"
    elif any(w in tl for w in ["decreased", "decrease", "dump", "drop", "crash", "fall"]):
        sig_type = "dump"
    else:
        sig_type = "pump"  # default

    # OI % from message (already parsed - use as extra context)
    oi_15m = 0.0
    m15 = re.search(r"OI\s*15m[:\s]+[^(]*\(([+-]?\d+\.?\d*)%\)", text, re.IGNORECASE)
    if m15:
        oi_15m = float(m15.group(1))

    oi_1h = 0.0
    m1h = re.search(r"OI\s*1H[:\s]+[^(]*\(([+-]?\d+\.?\d*)%\)", text, re.IGNORECASE)
    if m1h:
        oi_1h = float(m1h.group(1))

    # Current price from message
    msg_price = 0.0
    mcp = re.search(r"CP[:\s]+([0-9.]+)", text, re.IGNORECASE)
    if mcp:
        msg_price = float(mcp.group(1))

    # Funding rate from message
    msg_fr = 0.0
    mfr = re.search(r"FR[:\s]+([+-]?\d+\.?\d*)%", text, re.IGNORECASE)
    if mfr:
        msg_fr = float(mfr.group(1)) / 100

    # Price change 15m
    price_15m_pct = 0.0
    mp15 = re.search(r"15m[:\s]+[0-9.]+\s*\(([+-]?\d+\.?\d*)%\)", text, re.IGNORECASE)
    if mp15:
        price_15m_pct = float(mp15.group(1))

    return {
        "symbol":        symbol,
        "sig_type":      sig_type,
        "msg_oi_15m":    oi_15m,
        "msg_oi_1h":     oi_1h,
        "msg_price":     msg_price,
        "msg_fr":        msg_fr,
        "price_15m_pct": price_15m_pct,
        "raw":           text,
    }


# ?? MARKET DATA ???????????????????????????????????????????????????????????????
async def symbol_exists(sym: str) -> bool:
    d = await get(f"{BFUT}/fapi/v1/ticker/price", {"symbol": sym})
    return bool(d and "price" in d)


async def fetch_ticker(sym: str) -> dict | None:
    d = await get(f"{BFUT}/fapi/v1/ticker/24hr", {"symbol": sym})
    if not d:
        return None
    return {
        "price":  float(d["lastPrice"]),
        "change": float(d["priceChangePercent"]),
        "volume": float(d["quoteVolume"]),
        "high":   float(d["highPrice"]),
        "low":    float(d["lowPrice"]),
    }


async def fetch_klines(sym: str, tf: str = "5m", n: int = 100) -> list:
    d = await get(f"{BFUT}/fapi/v1/klines",
                  {"symbol": sym, "interval": tf, "limit": n})
    if not d:
        return []
    return [{"o": float(x[1]), "h": float(x[2]), "l": float(x[3]),
             "c": float(x[4]), "v": float(x[5]), "t": int(x[0])} for x in d]


async def fetch_oi(sym: str) -> dict:
    now  = await get(f"{BFUT}/fapi/v1/openInterest", {"symbol": sym})
    hist = await get(f"{BFUT}/futures/data/openInterestHist",
                     {"symbol": sym, "period": "5m", "limit": 12})
    if not now:
        return {"d15": 0, "d1h": 0, "cur": 0}
    cur  = float(now["openInterest"])
    vals = [float(h["sumOpenInterest"]) for h in (hist or [])]
    p15  = vals[-3]  if len(vals) >= 3  else cur
    p1h  = vals[-12] if len(vals) >= 12 else cur
    return {
        "cur": cur,
        "d15": (cur - p15) / max(p15, 1) * 100,
        "d1h": (cur - p1h) / max(p1h, 1) * 100,
    }


async def fetch_funding(sym: str) -> dict:
    d  = await get(f"{BFUT}/fapi/v1/premiumIndex", {"symbol": sym})
    fr = float(d["lastFundingRate"]) if d else 0
    return {
        "rate":     fr,
        "ext_long": fr >  0.010,
        "ext_short":fr < -0.010,
    }


async def fetch_liqs(sym: str) -> dict:
    d = await get(f"{BFUT}/fapi/v1/allForceOrders", {"symbol": sym, "limit": 500})
    if not d:
        return {"ratio": 1.0, "long_usd": 0, "short_usd": 0, "total": 0}
    lv = sum(float(o["origQty"]) * float(o["price"])
             for o in d if o.get("side") == "SELL")
    sv = sum(float(o["origQty"]) * float(o["price"])
             for o in d if o.get("side") == "BUY")
    return {
        "ratio":     lv / max(sv, 1),
        "long_usd":  lv,
        "short_usd": sv,
        "total":     lv + sv,
    }


async def fetch_ob(sym: str) -> dict:
    d = await get(f"{BFUT}/fapi/v1/depth", {"symbol": sym, "limit": 100})
    if not d:
        return {"imb": 0}
    bv = sum(float(b[1]) for b in d.get("bids", []))
    av = sum(float(a[1]) for a in d.get("asks", []))
    tot = bv + av
    return {"imb": (bv - av) / tot if tot else 0, "bid": bv, "ask": av}


async def fetch_ls(sym: str) -> dict:
    d = await get(f"{BFUT}/futures/data/globalLongShortAccountRatio",
                  {"symbol": sym, "period": "5m", "limit": 3})
    if not d:
        return {"ratio": 1.0}
    return {"ratio": float(d[-1].get("longShortRatio", 1))}


# ?? SMART MONEY DETECTORS ?????????????????????????????????????????????????????
def cvd(candles: list) -> dict:
    if len(candles) < 10:
        return {"div": 0, "bp": 50, "absorb": False}
    vals = []
    cum  = 0.0
    for c in candles:
        cum += c["v"] if c["c"] >= c["o"] else -c["v"]
        vals.append(cum)
    p = min(12, len(candles) - 1)
    pu = candles[-1]["c"] > candles[-p]["c"] * 1.001
    pd = candles[-1]["c"] < candles[-p]["c"] * 0.999
    cu = vals[-1] > vals[-p]
    cd = vals[-1] < vals[-p]
    div_val = 0
    if pu and cd: div_val = -1
    if pd and cu: div_val = 1
    bv  = sum(c["v"] for c in candles[-10:] if c["c"] >= c["o"])
    sv  = sum(c["v"] for c in candles[-10:] if c["c"] <  c["o"])
    bp  = bv / (bv + sv) * 100 if (bv + sv) > 0 else 50
    lc  = candles[-1]
    av  = sum(c["v"] for c in candles[-20:]) / max(len(candles[-20:]), 1)
    ab_ = sum(abs(c["c"] - c["o"]) for c in candles[-20:]) / max(len(candles[-20:]), 1)
    absorb = lc["v"] > av * 2.0 and abs(lc["c"] - lc["o"]) < ab_ * 0.4
    return {"div": div_val, "bp": bp, "absorb": bool(absorb),
            "up": bool(cu), "dn": bool(cd)}


def eq_levels(candles: list, price: float) -> dict:
    hs  = [c["h"] for c in candles[-80:]]
    ls  = [c["l"] for c in candles[-80:]]
    tol = 0.0018
    above = sorted(set(
        round(h, 8) for i, h in enumerate(hs)
        if h > price * 1.001 and
        sum(1 for hh in hs[i+1:] if abs(hh - h) / max(h, 1) < tol) >= 2
    ))
    below = sorted(set(
        round(l, 8) for i, l in enumerate(ls)
        if l < price * 0.999 and
        sum(1 for ll in ls[i+1:] if abs(ll - l) / max(l, 1) < tol) >= 2
    ), reverse=True)
    return {
        "above":  above[:4],
        "below":  below[:4],
        "hi24":   max(hs) if hs else price * 1.05,
        "lo24":   min(ls) if ls else price * 0.95,
    }


def fvg(candles: list, price: float) -> dict:
    bull = []; bear = []
    data = candles[-60:]
    for i in range(2, len(data)):
        c0 = data[i-2]; c2 = data[i]
        if c2["l"] > c0["h"]:
            sz = (c2["l"] - c0["h"]) / max(c0["h"], 1) * 100
            if sz > 0.05:
                bull.append({"top": c2["l"], "bot": c0["h"],
                             "mid": (c2["l"] + c0["h"]) / 2, "sz": sz})
        elif c2["h"] < c0["l"]:
            sz = (c0["l"] - c2["h"]) / max(c0["l"], 1) * 100
            if sz > 0.05:
                bear.append({"top": c0["l"], "bot": c2["h"],
                             "mid": (c0["l"] + c2["h"]) / 2, "sz": sz})
    nb = [f for f in bull if f["top"] <= price * 1.03]
    nb2= [f for f in bear if f["bot"] >= price * 0.97]
    return {
        "bull": nb[-1]  if nb  else None,
        "bear": nb2[-1] if nb2 else None,
    }


def ob(candles: list, price: float) -> dict:
    data = candles[-80:]
    avg  = sum(abs(c["c"] - c["o"]) for c in data) / max(len(data), 1)
    bull = []; bear = []
    for i in range(1, len(data) - 1):
        c = data[i]; n = data[i+1]
        nb = abs(n["c"] - n["o"])
        if c["c"] < c["o"] and n["c"] > n["o"] and nb > avg * 1.5:
            bull.append({"top": c["o"], "bot": c["c"],
                         "mid": (c["o"] + c["c"]) / 2})
        elif c["c"] > c["o"] and n["c"] < n["o"] and nb > avg * 1.5:
            bear.append({"top": c["c"], "bot": c["o"],
                         "mid": (c["c"] + c["o"]) / 2})
    nb_  = [z for z in bull if z["top"] <= price * 1.03]
    nb2_ = [z for z in bear if z["bot"] >= price * 0.97]
    return {
        "bull": nb_[-1]  if nb_  else None,
        "bear": nb2_[-1] if nb2_ else None,
    }


def sweep(candles: list) -> dict:
    data = candles[-30:]
    if len(data) < 8:
        return {"bull": False, "bear": False, "bull_pct": 0, "bear_pct": 0}
    recent = data[:-4]; last4 = data[-4:]
    ph  = max(c["h"] for c in recent)
    pl  = min(c["l"] for c in recent)
    lh  = max(c["h"] for c in last4)
    ll  = min(c["l"] for c in last4)
    lc  = data[-1]["c"]
    b   = bool(ll < pl and lc > pl)
    be  = bool(lh > ph and lc < ph)
    return {
        "bull":     b,
        "bear":     be,
        "bull_pct": (pl - ll) / max(pl, 1) * 100 if b  else 0,
        "bear_pct": (lh - ph) / max(ph, 1) * 100 if be else 0,
    }


def flat(candles: list) -> bool:
    if len(candles) < 15: return True
    data = candles[-15:]
    rng  = max(c["h"] for c in data) - min(c["l"] for c in data)
    return rng / max(min(c["l"] for c in data), 1) * 100 < 0.8


def atr(candles: list, n: int = 14) -> float:
    if not candles: return 0
    recent = candles[-n:] if len(candles) >= n else candles
    return sum(c["h"] - c["l"] for c in recent) / max(len(recent), 1)


# ?? CONFIDENCE SCORING ????????????????????????????????????????????????????????
def score(
    sig_type: str, ticker: dict, oi_data: dict, fund: dict,
    liqs_data: dict, ob_data: dict, ls_data: dict,
    cvd_data: dict, lvl: dict, fvg_data: dict, ob_zones: dict,
    sw: dict, msg_oi_15m: float, msg_oi_1h: float, msg_fr: float,
) -> tuple[int, int, list, list]:

    price  = ticker["price"]
    change = ticker["change"]

    # Use best available OI data (message or API)
    d15 = oi_data["d15"] if abs(oi_data["d15"]) > abs(msg_oi_15m) else msg_oi_15m
    d1h = oi_data["d1h"] if abs(oi_data["d1h"]) > abs(msg_oi_1h)  else msg_oi_1h
    fr  = fund["rate"] if fund["rate"] != 0 else msg_fr
    liq_r   = liqs_data["ratio"]
    imb     = ob_data["imb"]
    ls_ratio= ls_data["ratio"]
    bp      = cvd_data["bp"]
    div     = cvd_data["div"]

    sl = ss = 0.0
    rl = []; rs = []

    # PILLAR 1: OI (max 25pts)
    # Strong OI increase + price up = longs
    if d15 > 5:
        sl += 20; rl.append(f"OI +{d15:.1f}% spike 15m - aggressive longs")
    elif d15 > 2 and change > 0:
        sl += 14; rl.append(f"OI +{d15:.1f}% + price up - long accumulation")
    elif d15 > 2 and change < 0:
        ss += 14; rs.append(f"OI +{d15:.1f}% + price down - shorts building")
    elif d15 < -5:
        ss += 18; rs.append(f"OI -{abs(d15):.1f}% 15m - mass long exit, bearish")
    elif d15 < -2:
        ss += 10; rs.append(f"OI -{abs(d15):.1f}% - longs closing")

    if d1h > 8:
        sl += 5; rl.append(f"OI 1H +{d1h:.1f}% - sustained bullish pressure")
    elif d1h < -8:
        ss += 5; rs.append(f"OI 1H -{abs(d1h):.1f}% - sustained bearish pressure")

    # Funding
    if fund["ext_short"]:
        sl += 8; rl.append(f"Funding extreme negative {fr*100:.4f}% - long squeeze imminent")
    elif fund["ext_long"]:
        ss += 8; rs.append(f"Funding extreme positive {fr*100:.4f}% - short squeeze imminent")
    elif fr < -0.003:
        sl += 4; rl.append(f"Funding negative {fr*100:.4f}% - bullish bias")
    elif fr > 0.003:
        ss += 4; rs.append(f"Funding positive {fr*100:.4f}% - bearish bias")

    # Liquidations
    if liq_r > 3:
        sl += 8; rl.append(f"Mass long liqs x{liq_r:.1f} - bull reversal expected")
    elif liq_r > 1.8:
        sl += 4; rl.append(f"Long liqs x{liq_r:.1f} - slight bull bias")
    elif liq_r < 0.33:
        ss += 8; rs.append("Mass short liqs - bear reversal expected")
    elif liq_r < 0.55:
        ss += 4; rs.append(f"Short liqs x{1/max(liq_r,0.001):.1f} - slight bear bias")

    # L/S ratio
    if ls_ratio < 0.7:
        sl += 5; rl.append(f"L/S {ls_ratio:.2f} - crowd short = long squeeze likely")
    elif ls_ratio > 1.4:
        ss += 5; rs.append(f"L/S {ls_ratio:.2f} - crowd long = short squeeze likely")

    # Signal type from channel
    if sig_type == "pump":
        sl += 3; rl.append("Channel: OI pump alert")
    elif sig_type == "dump":
        ss += 3; rs.append("Channel: OI dump alert")

    score_l_oi = min(25, sl); score_s_oi = min(25, ss)

    # PILLAR 2: CVD / Order Flow (max 25pts)
    cl = cs = 0.0

    if div == 1:
        cl += 18; rl.append("CVD bullish divergence: price falls but buyers dominate")
    elif div == -1:
        cs += 18; rs.append("CVD bearish divergence: price rises but sellers dominate")

    if bp > 65:
        cl += 10; rl.append(f"Buy pressure {bp:.0f}% - aggressive buyers in market")
    elif bp < 35:
        cs += 10; rs.append(f"Sell pressure {100-bp:.0f}% - aggressive sellers in market")

    if cvd_data["absorb"]:
        if change >= 0:
            cl += 7; rl.append("Absorption: large vol, tiny body = smart money buying")
        else:
            cs += 7; rs.append("Absorption: large vol, tiny body = smart money selling")

    if imb > 0.20:
        cl += 7; rl.append(f"OB imbalance {imb:+.2f} - buy side dominant")
    elif imb > 0.10:
        cl += 4; rl.append(f"OB imbalance {imb:+.2f} - mild buy pressure")
    elif imb < -0.20:
        cs += 7; rs.append(f"OB imbalance {imb:+.2f} - sell side dominant")
    elif imb < -0.10:
        cs += 4; rs.append(f"OB imbalance {imb:+.2f} - mild sell pressure")

    score_l_cvd = min(25, cl); score_s_cvd = min(25, cs)

    # PILLAR 3: LIQUIDITY (max 25pts)
    ll = ls2 = 0.0

    # Sweep = most important signal in SM
    if sw["bull"]:
        ll  += 22; rl.append(f"SWEEP: stops below taken ({sw['bull_pct']:.2f}%), price returned -> LONG")
    if sw["bear"]:
        ls2 += 22; rs.append(f"SWEEP: stops above taken ({sw['bear_pct']:.2f}%), price returned -> SHORT")

    # FVG zones
    if fvg_data["bull"]:
        f = fvg_data["bull"]
        ll  += 7; rl.append(f"Bullish FVG: {f['bot']:.6f} - {f['top']:.6f} ({f['sz']:.2f}%)")
    if fvg_data["bear"]:
        f = fvg_data["bear"]
        ls2 += 7; rs.append(f"Bearish FVG: {f['bot']:.6f} - {f['top']:.6f} ({f['sz']:.2f}%)")

    # Order Blocks
    if ob_zones["bull"]:
        ll  += 6; rl.append(f"Bullish OB: {ob_zones['bull']['bot']:.6f} - {ob_zones['bull']['top']:.6f}")
    if ob_zones["bear"]:
        ls2 += 6; rs.append(f"Bearish OB: {ob_zones['bear']['bot']:.6f} - {ob_zones['bear']['top']:.6f}")

    # Equal highs/lows as targets
    if lvl["above"]:
        ll  += 5; rl.append(f"Equal highs above {lvl['above'][0]:.6f} - liquidity magnet")
    if lvl["below"]:
        ls2 += 5; rs.append(f"Equal lows below {lvl['below'][0]:.6f} - liquidity magnet")

    score_l_liq = min(25, ll); score_s_liq = min(25, ls2)

    # PILLAR 4: STRUCTURE (max 25pts)
    stl = sts = 0.0

    # Price position in 24H range
    rng = max(lvl["hi24"] - lvl["lo24"], 0.001)
    pp  = (price - lvl["lo24"]) / rng * 100

    if pp < 20:
        stl += 10; rl.append(f"Price near 24H low ({pp:.0f}%) - upside potential")
    elif pp > 80:
        sts += 10; rs.append(f"Price near 24H high ({pp:.0f}%) - downside potential")

    # Scenario detection
    if sig_type == "dump" and d15 < -3 and div == 1 and sw["bull"]:
        stl += 12; rl.append("REVERSAL LONG: dump liqs done + CVD turning up + sweep low")
    elif sig_type == "dump" and d15 < -3 and div == -1:
        sts += 12; rs.append("CONTINUATION SHORT: OI down + price down + CVD bearish")
    elif sig_type == "pump" and d15 > 3 and div == 1:
        stl += 10; rl.append("CONTINUATION LONG: OI up + price up + CVD bullish")
    elif sig_type == "pump" and d15 > 5 and div == -1 and sw["bear"]:
        sts += 12; rs.append("REVERSAL SHORT: OI spike + CVD bearish div + sweep high")
    elif abs(d15) > 3 and abs(change) < 0.5:
        stl += 4; sts += 4  # manipulation - slight bias both ways
        rl.append("MANIPULATION: OI moving but price static - explosion imminent")

    # Anti-pump: already moved hard without pullback
    if abs(change) > 5 and not sw["bull"] and not sw["bear"]:
        stl = max(0, stl - 8)
        sts = max(0, sts - 8)
        rl.append(f"Anti-pump: {change:.1f}% move without pullback - confidence reduced")
        rs.append(f"Anti-pump: {change:.1f}% move without pullback - confidence reduced")

    score_l_str = min(25, stl); score_s_str = min(25, sts)

    conf_l = int(score_l_oi + score_l_cvd + score_l_liq + score_l_str)
    conf_s = int(score_s_oi + score_s_cvd + score_s_liq + score_s_str)

    return conf_l, conf_s, rl, rs


# ?? ENTRY / SL / TP CALCULATION ???????????????????????????????????????????????
def calc_levels(
    direction: str, price: float, atr_val: float,
    fvg_data: dict, ob_zones: dict, lvl: dict, ticker: dict
) -> dict:

    if direction == "LONG":
        # Entry: price or nearest FVG/OB mid if close enough
        entry = price
        if fvg_data["bull"] and abs(fvg_data["bull"]["mid"] - price) / price < 0.01:
            entry = fvg_data["bull"]["mid"]
        elif ob_zones["bull"] and abs(ob_zones["bull"]["mid"] - price) / price < 0.01:
            entry = ob_zones["bull"]["mid"]

        # SL: below nearest structure
        sl_c = [entry - atr_val * 1.4]
        if ob_zones["bull"]:  sl_c.append(ob_zones["bull"]["bot"] * 0.997)
        if fvg_data["bull"]:  sl_c.append(fvg_data["bull"]["bot"] * 0.997)
        sl   = max(sl_c)
        sl_d = max(entry - sl, atr_val * 0.5)

        # TP: next liquidity pools (equal highs, 24H high)
        tp1 = entry + sl_d * 1.5
        tp2 = entry + sl_d * 2.5
        if lvl["above"]:
            tp3 = max(lvl["above"][0] * 0.999, entry + sl_d * 3.5)
        else:
            tp3 = entry + sl_d * 4.0
        # Cap at 24H high if reasonable
        if ticker["high"] > entry * 1.02:
            tp3 = max(tp3, ticker["high"] * 0.999)

    else:  # SHORT
        entry = price
        if fvg_data["bear"] and abs(fvg_data["bear"]["mid"] - price) / price < 0.01:
            entry = fvg_data["bear"]["mid"]
        elif ob_zones["bear"] and abs(ob_zones["bear"]["mid"] - price) / price < 0.01:
            entry = ob_zones["bear"]["mid"]

        sl_c = [entry + atr_val * 1.4]
        if ob_zones["bear"]:  sl_c.append(ob_zones["bear"]["top"] * 1.003)
        if fvg_data["bear"]:  sl_c.append(fvg_data["bear"]["top"] * 1.003)
        sl   = min(sl_c)
        sl_d = max(sl - entry, atr_val * 0.5)

        tp1 = entry - sl_d * 1.5
        tp2 = entry - sl_d * 2.5
        if lvl["below"]:
            tp3 = min(lvl["below"][0] * 1.001, entry - sl_d * 3.5)
        else:
            tp3 = entry - sl_d * 4.0
        if ticker["low"] < entry * 0.98:
            tp3 = min(tp3, ticker["low"] * 1.001)

    risk_pct      = abs(entry - sl)  / max(entry, 1) * 100
    expected_move = abs(tp2 - entry) / max(entry, 1) * 100
    rr            = abs(tp2 - entry) / max(abs(sl - entry), 0.0001)
    leverage      = max(3, min(20, int(40 / max(expected_move, 1))))
    pot_profit    = round(expected_move * leverage, 1)

    return {
        "entry":    round(entry, 8),
        "sl":       round(sl,    8),
        "tp1":      round(tp1,   8),
        "tp2":      round(tp2,   8),
        "tp3":      round(tp3,   8),
        "risk_pct": round(risk_pct, 3),
        "exp_move": round(expected_move, 2),
        "leverage": leverage,
        "pot_prof": pot_profit,
        "rr":       round(rr, 2),
    }


# ?? MAIN ANALYSIS ?????????????????????????????????????????????????????????????
async def analyze(parsed: dict) -> dict:
    sym      = parsed["symbol"]
    sig_type = parsed["sig_type"]

    # 1. Check symbol exists on Binance Futures
    if not await symbol_exists(sym):
        return {
            "decision": "NO TRADE",
            "symbol":   sym,
            "reason":   f"{sym} not listed on Binance Futures",
            "no_symbol":True,
        }

    # 2. Fetch all data in parallel
    results = await asyncio.gather(
        fetch_ticker(sym),
        fetch_klines(sym, "5m",  100),
        fetch_klines(sym, "15m", 60),
        fetch_oi(sym),
        fetch_funding(sym),
        fetch_liqs(sym),
        fetch_ob(sym),
        fetch_ls(sym),
        return_exceptions=True,
    )

    ticker = results[0]; c5m = results[1]; c15m = results[2]
    oi_d   = results[3]; fund = results[4]; liqs_ = results[5]
    ob_d   = results[6]; ls_  = results[7]

    # Safe defaults on any fetch failure
    if not ticker or isinstance(ticker, Exception):
        return {"decision":"NO TRADE","symbol":sym,"reason":"Cannot fetch market data from Binance Futures"}

    def safe(v, default):
        return default if (v is None or isinstance(v, Exception)) else v

    c5m   = safe(c5m,   [])
    c15m  = safe(c15m,  [])
    oi_d  = safe(oi_d,  {"d15": parsed["msg_oi_15m"], "d1h": parsed["msg_oi_1h"], "cur": 0})
    fund  = safe(fund,  {"rate": parsed["msg_fr"], "ext_long": False, "ext_short": False})
    liqs_ = safe(liqs_, {"ratio": 1.0, "long_usd": 0, "short_usd": 0, "total": 0})
    ob_d  = safe(ob_d,  {"imb": 0})
    ls_   = safe(ls_,   {"ratio": 1.0})

    price = ticker["price"]

    # 3. SM Detectors
    cvd_d  = cvd(c5m)    if c5m  else {"div": 0, "bp": 50, "absorb": False, "up": False, "dn": False}
    lvl_d  = eq_levels(c5m, price) if c5m else {"above": [], "below": [], "hi24": price*1.05, "lo24": price*0.95}
    fvg_d  = fvg(c5m, price)       if c5m else {"bull": None, "bear": None}
    ob_z   = ob(c5m, price)         if c5m else {"bull": None, "bear": None}
    sw_d   = sweep(c5m)             if c5m else {"bull": False, "bear": False, "bull_pct": 0, "bear_pct": 0}
    is_flt = flat(c5m)              if c5m else True
    atr_v  = atr(c5m)               if c5m else price * 0.005

    # 4. Flat market filter
    if is_flt and not sw_d["bull"] and not sw_d["bear"]:
        return {
            "decision":   "NO TRADE",
            "symbol":     sym,
            "price":      price,
            "reason":     "Market is flat - no directional setup",
            "conf_l":     0,
            "conf_s":     0,
            "oi_d":       oi_d,
            "fund":       fund,
            "liqs":       liqs_,
            "cvd_d":      cvd_d,
            "sw_d":       sw_d,
        }

    # 5. Score
    conf_l, conf_s, rl, rs = score(
        sig_type, ticker, oi_d, fund, liqs_, ob_d, ls_,
        cvd_d, lvl_d, fvg_d, ob_z, sw_d,
        parsed["msg_oi_15m"], parsed["msg_oi_1h"], parsed["msg_fr"],
    )

    MIN_CONF = 70

    if conf_l >= MIN_CONF and conf_l > conf_s:
        direction = "LONG"; confidence = conf_l; reasons = rl
    elif conf_s >= MIN_CONF and conf_s > conf_l:
        direction = "SHORT"; confidence = conf_s; reasons = rs
    else:
        # NO TRADE - always explain why
        why = f"Confidence: LONG={conf_l}% SHORT={conf_s}% (need {MIN_CONF}%)"
        if abs(ticker["change"]) > 5 and not sw_d["bull"] and not sw_d["bear"]:
            why = f"Anti-pump rule: {ticker['change']:.1f}% move without pullback"
        elif is_flt:
            why = "Flat market - no clear direction"
        elif not fvg_d["bull"] and not fvg_d["bear"] and not ob_z["bull"] and not ob_z["bear"]:
            why = "No entry zone (FVG/OB) - no structure to trade from"

        return {
            "decision": "NO TRADE",
            "symbol":   sym,
            "price":    price,
            "reason":   why,
            "conf_l":   conf_l,
            "conf_s":   conf_s,
            "reasons_l":rl[:3],
            "reasons_s":rs[:3],
            "oi_d":     oi_d,
            "fund":     fund,
            "liqs":     liqs_,
            "cvd_d":    cvd_d,
            "sw_d":     sw_d,
            "volume":   ticker["volume"],
        }

    # 6. Calculate levels
    lvls = calc_levels(direction, price, atr_v, fvg_d, ob_z, lvl_d, ticker)

    # 7. Filters
    if lvls["risk_pct"] > 1.0:
        return {
            "decision": "NO TRADE",
            "symbol":   sym,
            "price":    price,
            "reason":   f"Risk {lvls['risk_pct']:.2f}% > 1% max (SL too wide for current volatility)",
            "conf_l":   conf_l,
            "conf_s":   conf_s,
            "reasons_l":rl[:2],
            "reasons_s":rs[:2],
        }

    if lvls["exp_move"] < 2.0:
        return {
            "decision": "NO TRADE",
            "symbol":   sym,
            "price":    price,
            "reason":   f"Expected move {lvls['exp_move']:.1f}% < 2% minimum",
            "conf_l":   conf_l,
            "conf_s":   conf_s,
            "reasons_l":rl[:2],
            "reasons_s":rs[:2],
        }

    return {
        "decision":   direction,
        "symbol":     sym,
        "price":      price,
        "confidence": confidence,
        "conf_l":     conf_l,
        "conf_s":     conf_s,
        "reasons":    reasons[:6],
        "sig_type":   sig_type,
        **lvls,
        "oi_d":       oi_d,
        "fund":       fund,
        "liqs":       liqs_,
        "cvd_d":      cvd_d,
        "sw_d":       sw_d,
        "volume":     ticker["volume"],
    }


# ?? FORMAT OUTPUT ?????????????????????????????????????????????????????????????
def fmt(res: dict) -> str:
    sym  = res["symbol"]
    dec  = res["decision"]
    p    = res.get("price", 0)

    if res.get("no_symbol"):
        return (
            f"TICKER: `{sym}`\n"
            f"ENTRY: NO\n\n"
            f"Reason:\n"
            f"- Symbol not found on Binance Futures\n"
            f"- May be listed on other exchanges only\n"
            f"- Cannot analyze without market data"
        )

    if dec == "NO TRADE":
        cl = res.get("conf_l", 0); cs = res.get("conf_s", 0)
        oi15 = res.get("oi_d", {}).get("d15", 0)
        fr   = res.get("fund",  {}).get("rate", 0) * 100
        vol  = res.get("volume", 0)
        cvd_ = res.get("cvd_d", {})
        sw_  = res.get("sw_d",  {})
        liqs_= res.get("liqs",  {})

        lines = [
            f"TICKER: `{sym}`",
            f"Price: `${p:,.6f}`",
            f"ENTRY: NO",
            "",
            f"Reason:",
            f"- {res.get('reason', '-')}",
            "",
            f"Market snapshot:",
            f"- OI 15m: {oi15:+.2f}%",
            f"- Funding: {fr:.4f}%",
            f"- Buy pressure: {cvd_.get('bp', 50):.0f}%",
            f"- CVD div: {'bullish' if cvd_.get('div')==1 else 'bearish' if cvd_.get('div')==-1 else 'none'}",
            f"- Sweep: {'bull' if sw_.get('bull') else 'bear' if sw_.get('bear') else 'none'}",
            f"- Liq ratio: {liqs_.get('ratio', 1):.2f}x",
            f"- Volume 24H: ${vol:,.0f}",
            f"- Conf LONG={cl}% SHORT={cs}%",
        ]

        if res.get("reasons_l"):
            lines.append("")
            lines.append("Bull signals found:")
            lines += [f"  + {r}" for r in res["reasons_l"]]

        if res.get("reasons_s"):
            lines.append("")
            lines.append("Bear signals found:")
            lines += [f"  - {r}" for r in res["reasons_s"]]

        return "\n".join(lines)

    # ENTER
    e1 = abs(res["tp1"] - res["entry"]) / max(res["entry"], 1) * 100
    e2 = abs(res["tp2"] - res["entry"]) / max(res["entry"], 1) * 100
    e3 = abs(res["tp3"] - res["entry"]) / max(res["entry"], 1) * 100
    oi_ = res["oi_d"]; fr_ = res["fund"]["rate"] * 100

    lines = [
        f"TICKER: `{sym}`",
        f"ENTRY: YES",
        "",
        f"Type: *{dec}*",
        "",
        f"Entry: `${res['entry']:,.6f}`",
        f"Stop Loss: `${res['sl']:,.6f}` (risk {res['risk_pct']:.2f}%)",
        "",
        f"Take Profit:",
        f"- TP1: `${res['tp1']:,.6f}` (+{e1:.1f}%)",
        f"- TP2: `${res['tp2']:,.6f}` (+{e2:.1f}%)",
        f"- TP3: `${res['tp3']:,.6f}` (+{e3:.1f}%)",
        "",
        f"Expected Move: `{res['exp_move']}%`",
        f"Leverage: `{res['leverage']}x`",
        f"Potential Profit: `+{res['pot_prof']}%`",
        "",
        f"Confidence: `{res['confidence']}%`",
        f"(LONG {res['conf_l']}% / SHORT {res['conf_s']}%)",
        "",
        f"Reason:",
    ]
    lines += [f"- {r}" for r in res.get("reasons", [])[:6]]

    lines += [
        "",
        f"OI 15m: {oi_['d15']:+.2f}% | 1H: {oi_['d1h']:+.2f}%",
        f"Funding: {fr_:.4f}%",
        f"Liq ratio: {res['liqs']['ratio']:.2f}x",
        f"Buy pressure: {res['cvd_d']['bp']:.0f}%",
    ]

    return "\n".join(lines)


# ?? TRADE EXECUTION ???????????????????????????????????????????????????????????
async def execute(res: dict, pct: float):
    sym  = res["symbol"]
    side = "BUY" if res["decision"] == "LONG" else "SELL"
    lev  = res["leverage"]
    tp   = res["tp2"]
    sl   = res["sl"]

    # Check position limit
    open_pos = [s for s, p in _positions.items() if p.get("status") == "open"]
    if len(open_pos) >= MAX_POSITIONS:
        await tg(
            f"Agent skipped `{sym}`\n"
            f"Reason: max {MAX_POSITIONS} simultaneous positions reached\n"
            f"Open: {', '.join(open_pos)}"
        )
        return

    # Check no duplicate
    if sym in _positions and _positions[sym].get("status") == "open":
        await tg(f"Agent skipped `{sym}` - position already open")
        return

    try:
        # Get balance from server
        async with httpx.AsyncClient(timeout=10) as c:
            wr = await c.get(f"{API_URL}/api/wallet/agent_main")
            w  = wr.json()

        if not w.get("connected"):
            await tg(
                f"Agent cannot trade `{sym}`\n"
                f"Exchange not connected\n"
                f"Connect API keys in Mini App"
            )
            return

        balance = float(w.get("balance", 0))
        if balance < 10:
            await tg(f"Agent skipped `{sym}` - balance ${balance:.2f} < $10 minimum")
            return

        amount = round(balance * (pct / 100), 2)
        amount = max(5.0, amount)

        await tg(
            f"Agent opening position\n"
            f"`{side}` `{sym}`\n"
            f"Size: `${amount:.2f}` ({pct}% of `${balance:.2f}`)\n"
            f"Leverage: `{lev}x`\n"
            f"Entry: `${res['entry']:,.6f}`\n"
            f"SL: `${sl:,.6f}` | TP2: `${tp:,.6f}`\n"
            f"Expected: +{res['pot_prof']}% | Risk: {res['risk_pct']:.2f}%"
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
            _positions[sym] = {
                "status":  "open",
                "side":    side,
                "entry":   t.get("price", res["entry"]),
                "tp":      tp,
                "sl":      sl,
                "amount":  amount,
                "leverage":lev,
                "opened":  time.time(),
            }
            await tg(
                f"Position opened\n"
                f"`{side}` `{sym}`\n"
                f"Filled at: `${t.get('price', res['entry']):,.6f}`\n"
                f"Qty: `{t.get('qty', 0)}`\n"
                f"SL: `${sl:,.6f}` | TP2: `${tp:,.6f}`\n"
                f"ID: `{t.get('id', '-')}`"
            )
        else:
            err = result.get("error", "Unknown error")
            await tg(
                f"Order FAILED `{sym}`\n"
                f"Error: {err}\n"
                f"Manual: {side} `{sym}` @ `${res['entry']:,.6f}`"
            )

    except Exception as e:
        log.error("execute %s: %s", sym, e)
        await tg(f"Trade error `{sym}`: {str(e)[:120]}")


# ?? TELETHON CHANNEL LISTENER ?????????????????????????????????????????????????
async def run_channel():
    if not TG_API_ID or not TG_API_HASH:
        await tg(
            f"Channel monitor NOT started\n"
            f"Add to Railway Variables:\n"
            f"TG_API_ID   = (from my.telegram.org)\n"
            f"TG_API_HASH = (from my.telegram.org)\n"
            f"TG_PHONE    = +380yourphone"
        )
        return

    try:
        from telethon import TelegramClient, events

        client = TelegramClient("/tmp/axiflow", TG_API_ID, TG_API_HASH)
        await client.start(phone=TG_PHONE)
        log.info("Telethon ready - listening %s", CHANNEL)

        await tg(
            f"AXIFLOW Bot v3 started\n"
            f"Reading: {CHANNEL}\n"
            f"Agent: {'ON' if _agent_on else 'OFF'}\n"
            f"Position size: {_position_pct}% per trade\n"
            f"Max positions: {MAX_POSITIONS}\n"
            f"Min confidence: 70%\n"
            f"Every signal = full analysis sent here"
        )

        @client.on(events.NewMessage(chats=CHANNEL))
        async def on_msg(event):
            global _agent_on, _position_pct

            text = event.message.text or ""
            if not text.strip():
                return

            log.info("Channel: %s", text[:80])

            parsed = parse_msg(text)
            if not parsed:
                return

            sym = parsed["symbol"]

            # Cooldown
            now = time.time()
            if now - _cooldown.get(sym, 0) < COOLDOWN_SEC:
                left = int(COOLDOWN_SEC - (now - _cooldown[sym]))
                log.info("Cooldown %s: %ds", sym, left)
                return
            _cooldown[sym] = now

            # Notify start
            await tg(
                f"Signal from {CHANNEL}\n"
                f"Symbol: `{sym}` ({parsed['sig_type'].upper()})\n"
                f"OI 15m: {parsed['msg_oi_15m']:+.2f}%\n"
                f"OI 1H: {parsed['msg_oi_1h']:+.2f}%\n"
                f"Analyzing..."
            )

            try:
                res = await analyze(parsed)
                await tg(fmt(res))

                if res["decision"] not in ("NO TRADE",) and not res.get("no_symbol"):
                    if _agent_on:
                        await execute(res, _position_pct)
                    else:
                        await tg(
                            f"Agent is OFF\n"
                            f"Signal not traded: `{sym}`\n"
                            f"Use /agent on to enable auto-trading"
                        )

            except Exception as e:
                log.error("Analysis %s: %s", sym, e)
                await tg(f"Analysis error `{sym}`: {str(e)[:100]}")

        await client.run_until_disconnected()

    except ImportError:
        await tg("ERROR: telethon not installed - add telethon==1.34.0 to requirements.txt")
    except Exception as e:
        log.error("Telethon: %s", e)
        await tg(f"Channel error: {str(e)[:120]}")


# ?? BOT COMMANDS ??????????????????????????????????????????????????????????????
async def cmd_start(update, ctx):
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
    kb = [
        [InlineKeyboardButton("Open AXIFLOW TRADE", web_app=WebAppInfo(url=APP_URL))],
        [InlineKeyboardButton("How it works", callback_data="about"),
         InlineKeyboardButton("API setup",    callback_data="api")],
    ]
    await update.message.reply_text(
        "AXIFLOW TRADE v3\n\n"
        f"Agent: {'ON' if _agent_on else 'OFF'}\n"
        f"Position: {_position_pct}% per trade\n\n"
        "Commands:\n"
        "/setrisk 20  - position size % per trade\n"
        "/agent on    - enable auto-trading\n"
        "/agent off   - disable auto-trading\n"
        "/status      - show current settings\n"
        "/analyze APTUSDT - manual analysis\n",
        reply_markup=InlineKeyboardMarkup(kb),
    )


async def cmd_setrisk(update, ctx):
    global _position_pct
    args = ctx.args
    if not args:
        await update.message.reply_text(
            f"Current position size: {_position_pct}% per trade\n\n"
            f"Usage: /setrisk 20\n"
            f"This means each position uses 20% of your balance\n"
            f"Example: $1000 balance x 20% = $200 per trade"
        )
        return
    try:
        pct = float(args[0])
        if not (1 <= pct <= 100):
            raise ValueError("must be between 1 and 100")
        _position_pct = pct
        await update.message.reply_text(
            f"Position size set to {pct}% per trade\n"
            f"Each trade will use {pct}% of your available balance"
        )
        await tg(f"Position size updated: {pct}% per trade")
    except Exception as e:
        await update.message.reply_text(f"Error: {e}\nUsage: /setrisk 20")


async def cmd_agent(update, ctx):
    global _agent_on
    args = ctx.args
    if not args:
        st = "ON - auto-trading" if _agent_on else "OFF - signals only"
        await update.message.reply_text(
            f"Agent: {st}\n"
            f"Position: {_position_pct}% per trade\n"
            f"Max positions: {MAX_POSITIONS}\n\n"
            f"/agent on  - enable\n"
            f"/agent off - disable"
        )
        return
    cmd = args[0].lower()
    if cmd == "on":
        _agent_on = True
        await update.message.reply_text(
            f"Agent ON\n"
            f"Will auto-trade signals with confidence >= 70%\n"
            f"Position: {_position_pct}% per trade\n"
            f"Max {MAX_POSITIONS} simultaneous positions\n\n"
            f"Use /setrisk N to change position size"
        )
        await tg(f"Agent activated - position: {_position_pct}% per trade")
    elif cmd == "off":
        _agent_on = False
        await update.message.reply_text("Agent OFF - signals only, no auto-trading")
        await tg("Agent deactivated")
    else:
        await update.message.reply_text("Usage: /agent on OR /agent off")


async def cmd_status(update, ctx):
    open_pos = [s for s, p in _positions.items() if p.get("status") == "open"]
    await update.message.reply_text(
        f"AXIFLOW Status\n\n"
        f"Agent: {'ON - trading' if _agent_on else 'OFF - signals only'}\n"
        f"Position size: {_position_pct}% per trade\n"
        f"Open positions: {len(open_pos)}/{MAX_POSITIONS}\n"
        f"{', '.join(open_pos) if open_pos else 'none'}\n\n"
        f"Channel: {CHANNEL}\n"
        f"Min confidence: 70%\n"
        f"Min expected move: 2%\n"
        f"Max risk per trade: 1%\n"
        f"Cooldown per symbol: 10 min\n"
        f"Leverage: 40 / expected move (3x-20x)"
    )


async def cmd_analyze(update, ctx):
    args = ctx.args
    if not args:
        await update.message.reply_text("Usage: /analyze APTUSDT")
        return
    sym = args[0].upper()
    if not sym.endswith("USDT"):
        sym += "USDT"
    await update.message.reply_text(f"Analyzing {sym}...")
    parsed = {
        "symbol": sym, "sig_type": "pump",
        "msg_oi_15m": 0, "msg_oi_1h": 0,
        "msg_price": 0,  "msg_fr": 0,
        "price_15m_pct": 0, "raw": "",
    }
    res = await analyze(parsed)
    await tg(fmt(res))


async def cmd_positions(update, ctx):
    open_pos = {s: p for s, p in _positions.items() if p.get("status") == "open"}
    if not open_pos:
        await update.message.reply_text("No open positions")
        return
    lines = ["Open positions:\n"]
    for sym, p in open_pos.items():
        dur = int((time.time() - p.get("opened", time.time())) / 60)
        lines.append(
            f"{sym}: {p['side']} ${p['amount']:.0f} {p['leverage']}x\n"
            f"  Entry: ${p['entry']:.6f} | SL: ${p['sl']:.6f} | TP: ${p['tp']:.6f}\n"
            f"  Open: {dur} min ago"
        )
    await update.message.reply_text("\n".join(lines))


async def cmd_cb(update, ctx):
    q = update.callback_query
    await q.answer()
    if q.data == "about":
        await q.edit_message_text(
            "AXIFLOW v3 - how it works:\n\n"
            f"1. Reads {CHANNEL} in real time\n"
            "2. Parses ticker + OI data from message\n"
            "3. Fetches from Binance Futures:\n"
            "   OI trend, Funding, Liquidations, L/S ratio\n"
            "   CVD, Order Book, Equal highs/lows\n"
            "   FVG zones, Order Blocks, Sweeps\n"
            "4. Scores 4 pillars x25pts:\n"
            "   OI + CVD + Liquidity + Structure\n"
            "5. ALWAYS sends analysis (ENTER or NO)\n"
            "6. Filters: conf>=70%, move>=2%, risk<1%\n"
            "7. Leverage = 40 / expected move\n"
            "8. If agent ON and conf >= 70% - trades\n\n"
            "/agent on to start | /setrisk 20 to set size"
        )
    elif q.data == "api":
        await q.edit_message_text(
            "API Key Setup:\n\n"
            "Bybit: bybit.com > API Management\n"
            "Enable Read + Trade. Never Withdraw.\n\n"
            "Binance: binance.com > API Management\n"
            "Enable Futures. Disable Withdrawals.\n\n"
            "Open Mini App > API tab > paste keys > connect\n"
            "Then /agent on to start trading."
        )


async def post_init(application):
    if APP_URL:
        try:
            from telegram import MenuButtonWebApp, WebAppInfo
            await application.bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(
                    text="AXIFLOW",
                    web_app=WebAppInfo(url=APP_URL),
                )
            )
        except Exception as e:
            log.warning("Menu button: %s", e)


# ?? ENTRY ?????????????????????????????????????????????????????????????????????
def main():
    if not BOT_TOKEN:
        log.error("TELEGRAM_BOT_TOKEN not set")
        return

    from telegram.ext import (
        Application, CommandHandler, CallbackQueryHandler
    )

    log.info("AXIFLOW Bot v3 starting")

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start",     cmd_start))
    app.add_handler(CommandHandler("setrisk",   cmd_setrisk))
    app.add_handler(CommandHandler("agent",     cmd_agent))
    app.add_handler(CommandHandler("status",    cmd_status))
    app.add_handler(CommandHandler("analyze",   cmd_analyze))
    app.add_handler(CommandHandler("positions", cmd_positions))
    app.add_handler(CallbackQueryHandler(cmd_cb))

    async def run_all():
        async with app:
            await app.start()
            await asyncio.gather(
                app.updater.start_polling(),
                run_channel(),
            )
            await app.updater.stop()
            await app.stop()

    asyncio.run(run_all())


if __name__ == "__main__":
    main()
