import asyncio, os, time, logging, math
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Optional
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
import httpx
from pydantic import BaseModel
 
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass
 
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s — %(message)s")
log = logging.getLogger("axiflow")
 
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT  = os.environ.get("TELEGRAM_CHAT_ID", "")
PORT     = int(os.environ.get("PORT", 3000))
BFUT     = "https://fapi.binance.com"
 
SYMBOLS = [
    "BTCUSDT","ETHUSDT","SOLUSDT","BNBUSDT","XRPUSDT",
    "ADAUSDT","DOGEUSDT","AVAXUSDT","DOTUSDT","LINKUSDT",
    "LTCUSDT","MATICUSDT","ATOMUSDT","NEARUSDT","APTUSDT",
    "ARBUSDT","OPUSDT","INJUSDT","SUIUSDT","TIAUSDT",
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
    entry:      float
    tp:         float
    sl:         float
    rr:         float
    move_pct:   float
    lev:        int
    reasons:    list
    structure:  dict   # raw market structure data
    ts:         float = field(default_factory=time.time)
 
    def to_dict(self):
        return {
            "symbol":    self.symbol,
            "decision":  self.decision,
            "confidence":self.confidence,
            "strategy":  self.strategy,
            "entry":     self.entry,
            "tp":        round(self.tp, 6),
            "sl":        round(self.sl, 6),
            "rr":        round(self.rr, 2),
            "move_pct":  round(self.move_pct, 2),
            "lev":       self.lev,
            "reasons":   self.reasons,
            "raw":       self.structure,
            "ts":        self.ts,
        }
 
 
# ══════════════════════════════════════════
#  HTTP
# ══════════════════════════════════════════
async def _get(url, params=None):
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            r = await c.get(url, params=params)
            r.raise_for_status()
            return r.json()
    except Exception as e:
        log.debug(f"GET {url}: {e}")
        return None
 
 
# ══════════════════════════════════════════
#  DATA FETCHERS
# ══════════════════════════════════════════
async def get_ticker(sym):
    d = await _get(f"{BFUT}/fapi/v1/ticker/24hr", {"symbol": sym})
    if not d: return None
    return {
        "price":  float(d["lastPrice"]),
        "change": float(d["priceChangePercent"]),
        "volume": float(d["quoteVolume"]),
        "high":   float(d["highPrice"]),
        "low":    float(d["lowPrice"]),
    }
 
async def get_klines(sym, tf="5m", limit=150):
    d = await _get(f"{BFUT}/fapi/v1/klines", {"symbol": sym, "interval": tf, "limit": limit})
    if not d: return []
    return [{"o":float(x[1]),"h":float(x[2]),"l":float(x[3]),
             "c":float(x[4]),"v":float(x[5]),"t":int(x[0])} for x in d]
 
async def get_oi(sym):
    now  = await _get(f"{BFUT}/fapi/v1/openInterest", {"symbol": sym})
    hist = await _get(f"{BFUT}/futures/data/openInterestHist",
                      {"symbol": sym, "period": "5m", "limit": 24})
    if not now: return {"delta_15m": 0, "delta_1h": 0, "delta_4h": 0,
                        "strength": 0, "current": 0, "trend": "flat"}
    cur  = float(now["openInterest"])
    vals = [float(h["sumOpenInterest"]) for h in (hist or [])]
    p15  = vals[-3]  if len(vals) >= 3  else cur
    p1h  = vals[-12] if len(vals) >= 12 else cur
    p4h  = vals[-24] if len(vals) >= 24 else cur
    d15  = (cur - p15) / max(p15, 1) * 100
    d1h  = (cur - p1h) / max(p1h, 1) * 100
    d4h  = (cur - p4h) / max(p4h, 1) * 100
    trend = "rising" if d1h > 2 else "falling" if d1h < -2 else "flat"
    return {"current": cur, "delta_15m": d15, "delta_1h": d1h, "delta_4h": d4h,
            "strength": 2 if abs(d15) > 5 else 1 if abs(d15) > 2 else 0,
            "trend": trend}
 
async def get_funding(sym):
    d = await _get(f"{BFUT}/fapi/v1/premiumIndex", {"symbol": sym})
    fr = float(d["lastFundingRate"]) if d else 0
    return {"rate": fr, "extreme_long": fr > 0.01, "extreme_short": fr < -0.01,
            "bullish": fr < -0.005, "bearish": fr > 0.005}
 
async def get_liqs(sym):
    d = await _get(f"{BFUT}/fapi/v1/allForceOrders", {"symbol": sym, "limit": 500})
    if not d: return {"ratio": 1.0, "long_vol": 0, "short_vol": 0,
                      "strength": 0, "total": 0, "dominant": "none"}
    lv = sum(float(o["origQty"]) * float(o["price"]) for o in d if o.get("side") == "SELL")
    sv = sum(float(o["origQty"]) * float(o["price"]) for o in d if o.get("side") == "BUY")
    total = lv + sv
    r = lv / max(sv, 1)
    dominant = "longs" if r > 2 else "shorts" if r < 0.5 else "balanced"
    return {"ratio": r, "long_vol": lv, "short_vol": sv, "total": total,
            "strength": 2 if r > 3 or r < 0.33 else 1 if r > 2 or r < 0.5 else 0,
            "dominant": dominant}
 
async def get_ob(sym, depth=100):
    d = await _get(f"{BFUT}/fapi/v1/depth", {"symbol": sym, "limit": depth})
    if not d: return {"imbalance": 0, "strength": 0, "bid_wall": 0, "ask_wall": 0}
    bids = d.get("bids", [])
    asks = d.get("asks", [])
    bv = sum(float(b[1]) for b in bids)
    av = sum(float(a[1]) for a in asks)
    total = bv + av
    imb = (bv - av) / total if total else 0
    # Find walls (large orders)
    bid_wall = max((float(b[1]) for b in bids), default=0)
    ask_wall = max((float(a[1]) for a in asks), default=0)
    return {"bid": bv, "ask": av, "imbalance": imb,
            "strength": 2 if abs(imb) > 0.2 else 1 if abs(imb) > 0.1 else 0,
            "bid_wall": bid_wall, "ask_wall": ask_wall}
 
 
# ══════════════════════════════════════════
#  SMART MONEY STRUCTURE ANALYSIS
# ══════════════════════════════════════════
 
def detect_fvg(candles, lookback=30):
    """
    Fair Value Gap — imbalance between candles.
    FVG UP: candle[i].low > candle[i-2].high
    FVG DOWN: candle[i].high < candle[i-2].low
    Returns most recent FVGs within price range
    """
    fvgs = []
    data = candles[-lookback:]
    for i in range(2, len(data)):
        c0 = data[i-2]; c2 = data[i]
        # Bullish FVG
        if c2["l"] > c0["h"]:
            fvgs.append({
                "type": "bullish",
                "top":  c2["l"],
                "bot":  c0["h"],
                "mid":  (c2["l"] + c0["h"]) / 2,
                "size": (c2["l"] - c0["h"]) / c0["h"] * 100,
                "idx":  i,
            })
        # Bearish FVG
        elif c2["h"] < c0["l"]:
            fvgs.append({
                "type": "bearish",
                "top":  c0["l"],
                "bot":  c2["h"],
                "mid":  (c0["l"] + c2["h"]) / 2,
                "size": (c0["l"] - c2["h"]) / c0["l"] * 100,
                "idx":  i,
            })
    # Return only significant FVGs (size > 0.1%)
    sig = [f for f in fvgs if f["size"] > 0.1]
    return sig[-5:] if sig else []
 
 
def detect_order_blocks(candles, lookback=50):
    """
    Order Block — last bearish candle before strong bullish move (bull OB)
    or last bullish candle before strong bearish move (bear OB)
    """
    obs = []
    data = candles[-lookback:]
    avg_size = sum(abs(c["c"] - c["o"]) for c in data) / max(len(data), 1)
 
    for i in range(1, len(data) - 2):
        c = data[i]
        next_c = data[i + 1]
        body = abs(c["c"] - c["o"])
        next_body = abs(next_c["c"] - next_c["o"])
 
        # Bull OB: bearish candle followed by strong bullish move
        if c["c"] < c["o"] and next_c["c"] > next_c["o"] and next_body > avg_size * 1.5:
            obs.append({
                "type":  "bullish",
                "top":   c["o"],
                "bot":   c["c"],
                "mid":   (c["o"] + c["c"]) / 2,
                "idx":   i,
            })
        # Bear OB: bullish candle followed by strong bearish move
        elif c["c"] > c["o"] and next_c["c"] < next_c["o"] and next_body > avg_size * 1.5:
            obs.append({
                "type":  "bearish",
                "top":   c["c"],
                "bot":   c["o"],
                "mid":   (c["c"] + c["o"]) / 2,
                "idx":   i,
            })
    return obs[-5:] if obs else []
 
 
def detect_liquidity_sweep(candles, lookback=50):
    """
    Liquidity Sweep — price breaks recent high/low then reverses back.
    = stop hunt / wick that took out liquidity
    """
    data = candles[-lookback:]
    if len(data) < 10: return {"bull_sweep": False, "bear_sweep": False}
 
    recent = data[:-3]
    last3  = data[-3:]
 
    prev_high = max(c["h"] for c in recent)
    prev_low  = min(c["l"] for c in recent)
 
    last_high = max(c["h"] for c in last3)
    last_low  = min(c["l"] for c in last3)
    last_close = data[-1]["c"]
 
    # Bull sweep: swept below prev low but closed back above
    bull_sweep = last_low < prev_low and last_close > prev_low
 
    # Bear sweep: swept above prev high but closed back below
    bear_sweep = last_high > prev_high and last_close < prev_high
 
    sweep_pct_bull = (prev_low - last_low) / prev_low * 100 if bull_sweep else 0
    sweep_pct_bear = (last_high - prev_high) / prev_high * 100 if bear_sweep else 0
 
    return {
        "bull_sweep":      bull_sweep,
        "bear_sweep":      bear_sweep,
        "sweep_pct_bull":  sweep_pct_bull,
        "sweep_pct_bear":  sweep_pct_bear,
        "prev_high":       prev_high,
        "prev_low":        prev_low,
    }
 
 
def detect_cvd_divergence(candles):
    """
    CVD (Cumulative Volume Delta) divergence:
    Price going up but CVD going down = bearish
    Price going down but CVD going up = bullish
    Also detects absorption (large volume at key level with no price move)
    """
    if len(candles) < 20: return {"divergence": 0, "absorption": False}
 
    # Compute CVD
    cvd_vals = []
    cum = 0.0
    for c in candles:
        delta = c["v"] if c["c"] >= c["o"] else -c["v"]
        cum += delta
        cvd_vals.append(cum)
 
    # Compare recent price trend vs CVD trend
    period = 10
    price_start = candles[-period]["c"]
    price_end   = candles[-1]["c"]
    cvd_start   = cvd_vals[-period]
    cvd_end     = cvd_vals[-1]
 
    price_up = price_end > price_start * 1.001
    price_dn = price_end < price_start * 0.999
    cvd_up   = cvd_end > cvd_start * 1.001 if cvd_start != 0 else cvd_end > cvd_start
    cvd_dn   = cvd_end < cvd_start * 0.999 if cvd_start != 0 else cvd_end < cvd_start
 
    divergence = 0
    if price_up and cvd_dn: divergence = -1  # bearish divergence
    if price_dn and cvd_up: divergence = 1   # bullish divergence
 
    # Absorption: big volume but small candle body (smart money absorbing)
    last = candles[-1]
    avg_vol  = sum(c["v"] for c in candles[-20:]) / 20
    avg_body = sum(abs(c["c"] - c["o"]) for c in candles[-20:]) / 20
    body_now = abs(last["c"] - last["o"])
    absorption = last["v"] > avg_vol * 2 and body_now < avg_body * 0.5
 
    return {
        "divergence":  divergence,
        "absorption":  absorption,
        "cvd_current": cvd_end,
        "cvd_trend":   "rising" if cvd_up else "falling" if cvd_dn else "flat",
        "price_trend": "rising" if price_up else "falling" if price_dn else "flat",
    }
 
 
def detect_liq_clusters(candles):
    """
    Liquidation clusters — zones with many stops.
    Equal highs/lows = stop cluster (liquidity pool).
    """
    if len(candles) < 30: return {"clusters_above": [], "clusters_below": []}
    tol = 0.002  # 0.2% tolerance
 
    highs = [c["h"] for c in candles[-50:]]
    lows  = [c["l"] for c in candles[-50:]]
 
    # Find equal highs (stops above)
    clusters_above = []
    for i, h in enumerate(highs):
        similar = [hh for hh in highs[i+1:] if abs(hh - h) / h < tol]
        if len(similar) >= 2:
            clusters_above.append(round(h, 4))
 
    # Find equal lows (stops below)
    clusters_below = []
    for i, l in enumerate(lows):
        similar = [ll for ll in lows[i+1:] if abs(ll - l) / l < tol]
        if len(similar) >= 2:
            clusters_below.append(round(l, 4))
 
    return {
        "clusters_above": list(set(clusters_above))[:3],
        "clusters_below": list(set(clusters_below))[:3],
    }
 
 
def detect_amd(candles_5m, oi_delta):
    """AMD — Accumulation / Manipulation / Distribution"""
    if len(candles_5m) < 20: return {"confirmed": False}
    recent = candles_5m[-20:]
    highs  = [c["h"] for c in recent]
    lows   = [c["l"] for c in recent]
    vols   = [c["v"] for c in recent]
    pr     = (max(highs) - min(lows)) / max(min(lows), 1) * 100
    if pr >= 3.0: return {"confirmed": False}
    rt, rb  = max(highs), min(lows)
    tol     = pr * 0.12
    top_t   = sum(1 for h in highs if abs(h - rt) / max(rt, 1) * 100 < tol)
    bot_t   = sum(1 for l in lows  if abs(l - rb) / max(rb, 1) * 100 < tol)
    if top_t < 2 and bot_t < 2: return {"confirmed": False}
    if not (vols[-1] < vols[0] and 0 <= oi_delta <= 5): return {"active": True, "confirmed": False}
    avg_v   = sum(vols[:-3]) / max(len(vols[:-3]), 1)
    avg_s   = sum(c["h"] - c["l"] for c in recent[:-3]) / max(len(recent[:-3]), 1)
    for c in candles_5m[-5:]:
        bt    = c["h"] > rt * 1.002
        bb    = c["l"] < rb * 0.998
        back  = rb < candles_5m[-1]["c"] < rt
        spike = c["v"] > avg_v * 1.3 or (c["h"] - c["l"]) > avg_s * 1.3
        if (bt or bb) and back and spike:
            return {
                "confirmed":  True,
                "fake":       "up" if bt else "down",
                "signal":     "SHORT" if bt else "LONG",
                "fvg_top":    rt,
                "fvg_bot":    rb,
                "range_pct":  round(pr, 2),
            }
    return {"active": True, "confirmed": False}
 
 
def vol_ratio(candles):
    if len(candles) < 20: return 1.0
    avg = sum(c["v"] for c in candles[-20:-5]) / 15
    rec = sum(c["v"] for c in candles[-5:]) / 5
    return rec / max(avg, 0.001)
 
 
# ══════════════════════════════════════════
#  SMART MONEY SCORING
# ══════════════════════════════════════════
def sm_score(ticker, oi, funding, liqs, ob,
             cvd_5m, fvg_5m, obs_5m, sweep_5m, liq_clusters,
             cvd_1h, sweep_1h, amd, vr):
    score = 0.0
    reasons = []
    confluence = []   # list of aligned signals
 
    price = ticker["price"]
    change = ticker["change"]
 
    # ── 1. LIQUIDITY SWEEP ─────────────────────────────
    if sweep_5m["bull_sweep"]:
        score += 2.0
        pct = sweep_5m["sweep_pct_bull"]
        reasons.append(f"💧 Bull Liquidity Sweep: взяли стопи нижче, ціна повернулась ({pct:.2f}%)")
        confluence.append("bull_sweep")
    if sweep_5m["bear_sweep"]:
        score -= 2.0
        pct = sweep_5m["sweep_pct_bear"]
        reasons.append(f"💧 Bear Liquidity Sweep: взяли стопи вище, ціна впала ({pct:.2f}%)")
        confluence.append("bear_sweep")
 
    # 1h sweep confirmation
    if sweep_1h["bull_sweep"]:
        score += 1.0; reasons.append("💧 Bull Sweep підтверджений на 1H структурі")
    if sweep_1h["bear_sweep"]:
        score -= 1.0; reasons.append("💧 Bear Sweep підтверджений на 1H структурі")
 
    # ── 2. FAIR VALUE GAP ──────────────────────────────
    recent_fvgs = fvg_5m[-3:] if fvg_5m else []
    for fvg in recent_fvgs:
        if fvg["type"] == "bullish" and fvg["bot"] <= price <= fvg["top"]:
            score += 1.5
            reasons.append(f"📐 Ціна в Bullish FVG зоні: ${fvg['bot']:.4f}–${fvg['top']:.4f} ({fvg['size']:.2f}%)")
            confluence.append("fvg_bull")
        elif fvg["type"] == "bearish" and fvg["bot"] <= price <= fvg["top"]:
            score -= 1.5
            reasons.append(f"📐 Ціна в Bearish FVG зоні: ${fvg['bot']:.4f}–${fvg['top']:.4f} ({fvg['size']:.2f}%)")
            confluence.append("fvg_bear")
 
    # ── 3. ORDER BLOCKS ────────────────────────────────
    for ob_zone in (obs_5m or [])[-3:]:
        if ob_zone["type"] == "bullish" and ob_zone["bot"] <= price <= ob_zone["top"] * 1.005:
            score += 1.5
            reasons.append(f"🟩 Bullish Order Block: ${ob_zone['bot']:.4f}–${ob_zone['top']:.4f}")
            confluence.append("ob_bull")
        elif ob_zone["type"] == "bearish" and ob_zone["bot"] * 0.995 <= price <= ob_zone["top"]:
            score -= 1.5
            reasons.append(f"🟥 Bearish Order Block: ${ob_zone['bot']:.4f}–${ob_zone['top']:.4f}")
            confluence.append("ob_bear")
 
    # ── 4. CVD DIVERGENCE ──────────────────────────────
    if cvd_5m["divergence"] == 1:
        score += 1.5
        reasons.append("📊 Bullish CVD дивергенція: ціна ↓ але об'єм купівлі ↑")
        confluence.append("cvd_bull")
    elif cvd_5m["divergence"] == -1:
        score -= 1.5
        reasons.append("📊 Bearish CVD дивергенція: ціна ↑ але об'єм продажу ↑")
        confluence.append("cvd_bear")
    if cvd_5m["absorption"]:
        # Absorption is bullish if price didn't fall (smart money buying)
        if change > 0:
            score += 0.8; reasons.append("🧲 Absorption detected: великий об'єм без руху = накопичення")
        else:
            score -= 0.8; reasons.append("🧲 Absorption detected: великий об'єм без руху = дистрибуція")
 
    # ── 5. OPEN INTEREST ───────────────────────────────
    d15 = oi["delta_15m"]
    d1h = oi["delta_1h"]
    price_up = change > 0
 
    if d15 > 5:
        score += 1.5; reasons.append(f"📈 OI екстрем +{d15:.1f}% за 15хв — агресивне відкриття")
    elif d15 > 2 and price_up:
        score += 1.0; reasons.append(f"📈 OI +{d15:.1f}% + ціна ↑ — накопичення лонгів")
    elif d15 > 2 and not price_up:
        score -= 1.0; reasons.append(f"📈 OI +{d15:.1f}% + ціна ↓ — накопичення шортів")
    elif d15 < -3:
        score -= 0.8; reasons.append(f"📉 OI −{abs(d15):.1f}% — закриття позицій")
 
    if d1h > 5:
        score += 0.5; reasons.append(f"📈 OI +{d1h:.1f}% за 1H — сильний тренд")
 
    # ── 6. FUNDING RATE ────────────────────────────────
    fr = funding["rate"]
    if funding["extreme_long"]:
        score -= 1.5; reasons.append(f"💸 Funding перекупленість {fr*100:.4f}% — SHORT тиск")
    elif funding["extreme_short"]:
        score += 1.5; reasons.append(f"💸 Funding перепроданість {fr*100:.4f}% — LONG тиск")
    elif funding["bearish"]:
        score -= 0.5; reasons.append(f"💸 Funding {fr*100:.4f}% — помірний ведмежий тиск")
    elif funding["bullish"]:
        score += 0.5; reasons.append(f"💸 Funding {fr*100:.4f}% — помірний бичачий тиск")
 
    # ── 7. LIQUIDATIONS ────────────────────────────────
    r = liqs["ratio"]
    if r > 3:
        score += 1.5; reasons.append(f"💥 Масові лонг-ліквідації x{r:.1f} → бичачий розворот")
        confluence.append("liq_bull")
    elif r > 2:
        score += 1.0; reasons.append(f"💥 Лонг-ліквідації x{r:.1f} → бичачий сигнал")
    elif r < 0.33:
        score -= 1.5; reasons.append(f"💥 Масові шорт-ліквідації x{1/max(r,.001):.1f} → ведмежий розворот")
        confluence.append("liq_bear")
    elif r < 0.5:
        score -= 1.0; reasons.append(f"💥 Шорт-ліквідації x{1/max(r,.001):.1f} → ведмежий сигнал")
 
    # ── 8. ORDERBOOK IMBALANCE ─────────────────────────
    im = ob["imbalance"]
    if im > 0.25:
        score += 1.2; reasons.append(f"📖 OB сильний тиск покупців {im:+.2f}")
    elif im > 0.12:
        score += 0.6; reasons.append(f"📖 OB тиск покупців {im:+.2f}")
    elif im < -0.25:
        score -= 1.2; reasons.append(f"📖 OB сильний тиск продавців {im:+.2f}")
    elif im < -0.12:
        score -= 0.6; reasons.append(f"📖 OB тиск продавців {im:+.2f}")
 
    # ── 9. LIQUIDITY CLUSTERS ─────────────────────────
    clusters_above = liq_clusters.get("clusters_above", [])
    clusters_below = liq_clusters.get("clusters_below", [])
    if clusters_above:
        # Price near cluster above = target for sweep up
        nearest = min(clusters_above, key=lambda x: abs(x - price))
        if abs(nearest - price) / price < 0.02:
            score += 0.5; reasons.append(f"🎯 Liquidity cluster above: ${nearest:.4f} — ймовірний таргет")
    if clusters_below:
        nearest = min(clusters_below, key=lambda x: abs(x - price))
        if abs(nearest - price) / price < 0.02:
            score -= 0.5; reasons.append(f"🎯 Liquidity cluster below: ${nearest:.4f} — ймовірний таргет")
 
    # ── 10. AMD OVERRIDE ──────────────────────────────
    amd_active = False
    if amd.get("confirmed"):
        amd_score = 4.0 if amd["signal"] == "LONG" else -4.0
        score += amd_score
        amd_active = True
        reasons.insert(0, f"⚡ AMD підтверджений: фейк {amd['fake'].upper()} → {amd['signal']} | FVG ${amd['fvg_bot']:.2f}–${amd['fvg_top']:.2f}")
 
    # ── Volume filter ─────────────────────────────────
    if vr < 0.4:
        reasons.append(f"⚠️ Дуже низький об'єм {vr:.0%} — обережно")
        score *= 0.6
 
    # ── Confluence bonus ──────────────────────────────
    bull_conf = sum(1 for c in confluence if "bull" in c or "sweep" == "bull_sweep")
    bear_conf = sum(1 for c in confluence if "bear" in c)
    if bull_conf >= 3:
        score += 0.8; reasons.append(f"🔗 Confluence: {bull_conf} бичачих сигналів збіглись")
    if bear_conf >= 3:
        score -= 0.8; reasons.append(f"🔗 Confluence: {bear_conf} ведмежих сигналів збіглись")
 
    return score, reasons, amd_active
 
 
# ══════════════════════════════════════════
#  DYNAMIC TP/SL (structure-based)
# ══════════════════════════════════════════
def calc_tp_sl(price, direction, candles, fvgs, obs, liq_clusters, ticker):
    """
    SL: behind nearest OB or FVG
    TP: next FVG fill or liquidity cluster
    Minimum RR: 1:3
    """
    high24 = ticker["high"]
    low24  = ticker["low"]
 
    # Find ATR-equivalent via recent range
    recent = candles[-20:]
    avg_range = sum(c["h"] - c["l"] for c in recent) / len(recent)
    atr = avg_range * 1.2
 
    if direction == "LONG":
        # SL: below nearest bullish OB or 1.5x ATR
        sl_candidates = [price - atr * 1.5]
        for ob_z in (obs or []):
            if ob_z["type"] == "bullish" and ob_z["bot"] < price:
                sl_candidates.append(ob_z["bot"] * 0.998)
        for fvg in (fvgs or []):
            if fvg["type"] == "bullish" and fvg["bot"] < price:
                sl_candidates.append(fvg["bot"] * 0.998)
        sl = max(sl_candidates)  # tightest SL above the lowest
 
        # TP: next bearish FVG fill or liquidity cluster above
        tp_candidates = [price + atr * 4]  # minimum RR 1:4
        # Next bearish FVG above price
        for fvg in sorted((f for f in (fvgs or []) if f["type"] == "bearish" and f["bot"] > price), key=lambda x: x["bot"]):
            tp_candidates.append(fvg["mid"] * 0.998)
            break
        # Liquidity clusters above
        for cl in sorted((c for c in liq_clusters.get("clusters_above", []) if c > price)):
            tp_candidates.append(cl * 0.999)
            break
        # 24h high
        if high24 > price * 1.02:
            tp_candidates.append(high24 * 0.999)
        tp = max(p for p in tp_candidates if p > price + atr * 3)
        if tp is None or tp <= price:
            tp = price + atr * 4
 
    else:  # SHORT
        # SL: above nearest bearish OB or 1.5x ATR
        sl_candidates = [price + atr * 1.5]
        for ob_z in (obs or []):
            if ob_z["type"] == "bearish" and ob_z["top"] > price:
                sl_candidates.append(ob_z["top"] * 1.002)
        for fvg in (fvgs or []):
            if fvg["type"] == "bearish" and fvg["top"] > price:
                sl_candidates.append(fvg["top"] * 1.002)
        sl = min(sl_candidates)
 
        # TP: next bullish FVG fill or liquidity cluster below
        tp_candidates = [price - atr * 4]
        for fvg in sorted((f for f in (fvgs or []) if f["type"] == "bullish" and f["top"] < price), key=lambda x: -x["top"]):
            tp_candidates.append(fvg["mid"] * 1.002)
            break
        for cl in sorted((c for c in liq_clusters.get("clusters_below", []) if c < price), reverse=True):
            tp_candidates.append(cl * 1.001)
            break
        if low24 < price * 0.98:
            tp_candidates.append(low24 * 1.001)
        tp = min(p for p in tp_candidates if p < price - atr * 3)
        if tp is None or tp >= price:
            tp = price - atr * 4
 
    sl_dist = abs(price - sl)
    tp_dist = abs(price - tp)
    rr = tp_dist / max(sl_dist, 0.0001)
    move_pct = tp_dist / price * 100
 
    return tp, sl, rr, move_pct
 
 
# ══════════════════════════════════════════
#  MAIN ANALYSIS
# ══════════════════════════════════════════
async def analyze_symbol(sym):
    try:
        ticker, c5m, c1h, oi, funding, liqs, ob_data = await asyncio.gather(
            get_ticker(sym),
            get_klines(sym, "5m", 150),
            get_klines(sym, "1h",  50),
            get_oi(sym),
            get_funding(sym),
            get_liqs(sym),
            get_ob(sym, 100),
        )
 
        if not ticker or len(c5m) < 30:
            return None
 
        price = ticker["price"]
 
        # ── Smart Money Structure ──
        fvg_5m       = detect_fvg(c5m, 50)
        obs_5m       = detect_order_blocks(c5m, 80)
        sweep_5m     = detect_liquidity_sweep(c5m, 50)
        cvd_5m       = detect_cvd_divergence(c5m)
        liq_clusters = detect_liq_clusters(c5m)
        amd          = detect_amd(c5m, oi["delta_15m"])
        sweep_1h     = detect_liquidity_sweep(c1h, 30)
        cvd_1h       = detect_cvd_divergence(c1h)
        vr           = vol_ratio(c5m)
 
        # ── Score ──
        score, reasons, amd_active = sm_score(
            ticker, oi, funding, liqs, ob_data,
            cvd_5m, fvg_5m, obs_5m, sweep_5m, liq_clusters,
            cvd_1h, sweep_1h, amd, vr
        )
 
        # ── Decision ──
        if   score >= 3.0:  direction = "LONG"
        elif score <= -3.0: direction = "SHORT"
        else:               direction = "NO TRADE"
 
        structure = {
            "price":      price,
            "change":     ticker["change"],
            "score":      round(score, 2),
            "oi_15m":     oi["delta_15m"],
            "oi_1h":      oi["delta_1h"],
            "funding":    funding["rate"],
            "liq_ratio":  liqs["ratio"],
            "ob_imb":     ob_data["imbalance"],
            "cvd_div":    cvd_5m["divergence"],
            "fvg_count":  len(fvg_5m),
            "ob_count":   len(obs_5m),
            "bull_sweep": sweep_5m["bull_sweep"],
            "bear_sweep": sweep_5m["bear_sweep"],
            "amd":        amd.get("confirmed", False),
            "vol_ratio":  vr,
        }
 
        if direction == "NO TRADE":
            result = Signal(
                sym, "NO TRADE", 0, "STANDARD",
                price, price, price, 0, 0, 1,
                [f"Score {score:.1f} | Нема чіткої структури"] if not reasons else reasons[:3],
                structure
            ).to_dict()
            cache[sym] = result
            return result
 
        # ── TP/SL ──
        tp, sl, rr, move_pct = calc_tp_sl(price, direction, c5m, fvg_5m, obs_5m, liq_clusters, ticker)
 
        # Filter: min RR 1:3, min move 2%
        if rr < 3.0:
            cache[sym] = Signal(
                sym, "NO TRADE", 0, "STANDARD",
                price, price, price, 0, 0, 1,
                [f"RR {rr:.1f}:1 < 1:3 — пропускаємо"], structure
            ).to_dict()
            return cache[sym]
 
        if move_pct < 2.0:
            cache[sym] = Signal(
                sym, "NO TRADE", 0, "STANDARD",
                price, price, price, 0, 0, 1,
                [f"Рух {move_pct:.1f}% < 2% — нецікаво"], structure
            ).to_dict()
            return cache[sym]
 
        # Confidence
        base_conf = 40 + abs(score) * 7
        if amd_active:   base_conf += 20
        if rr >= 5:      base_conf += 10
        if rr >= 4:      base_conf += 5
        if move_pct >= 5:base_conf += 5
        if sweep_5m["bull_sweep"] or sweep_5m["bear_sweep"]: base_conf += 8
        if len(fvg_5m) > 0: base_conf += 5
        conf = min(95, max(35, int(base_conf)))
 
        # Leverage
        lev = 5 if conf >= 85 else 3 if conf >= 75 else 2 if conf >= 65 else 1
 
        strategy = "AMD_FVG" if amd_active else (
            "SWEEP" if (sweep_5m["bull_sweep"] or sweep_5m["bear_sweep"]) else
            "FVG" if fvg_5m else "OB" if obs_5m else "STANDARD"
        )
 
        prev = cache.get(sym, {})
        is_new = prev.get("decision") != direction
 
        sig = Signal(sym, direction, conf, strategy, price, tp, sl,
                     rr, move_pct, lev, reasons[:6], structure)
        cache[sym] = sig.to_dict()
 
        log.info(f"[{sym}] {direction} conf={conf}% RR={rr:.1f} move={move_pct:.1f}% strat={strategy}")
 
        if is_new and direction != "NO TRADE" and conf >= 60:
            await notify(
                f"{'🟢' if direction=='LONG' else '🔴'} *{direction}* `{sym}`\n\n"
                f"💲 Entry: `${price:,.4f}`\n"
                f"🎯 TP: `${tp:,.4f}` (+{move_pct:.1f}%)\n"
                f"🛑 SL: `${sl:,.4f}`\n"
                f"📐 RR: `1:{rr:.1f}`\n"
                f"⚡ Lev: `{lev}x` | Conf: `{conf}%`\n"
                f"🧠 `{strategy}`\n\n"
                + "\n".join(f"• {r}" for r in reasons[:4])
            )
 
        return cache[sym]
 
    except Exception as e:
        log.error(f"analyze {sym}: {e}")
        return None
 
 
# ══════════════════════════════════════════
#  EXCHANGE CLIENT
# ══════════════════════════════════════════
class ExchangeClient:
    def __init__(self, exchange="", api_key="", api_secret="", testnet=False):
        self.exchange   = exchange.lower()
        self.api_key    = api_key
        self.api_secret = api_secret
        self.testnet    = testnet
        self._ex        = None
        self.connected  = False
        self.error      = ""
        if api_key and api_secret:
            self._connect()
 
    def _connect(self):
        try:
            import ccxt
            cfg = {"apiKey": self.api_key, "secret": self.api_secret,
                   "enableRateLimit": True, "options": {"defaultType": "future"}}
            ex_map = {"bybit": ccxt.bybit, "binance": ccxt.binanceusdm, "mexc": ccxt.mexc}
            if self.exchange not in ex_map:
                self.error = f"Unsupported exchange: {self.exchange}"; return
            self._ex = ex_map[self.exchange](cfg)
            if self.testnet:
                try: self._ex.set_sandbox_mode(True)
                except: pass
            self.connected = True
        except Exception as e:
            self.error = str(e)
 
    async def test_connection(self):
        if not self._ex: return False, self.error
        try:
            bal = await asyncio.to_thread(self._ex.fetch_balance)
            return True, float(bal.get("USDT", {}).get("free", 0))
        except Exception as e:
            return False, str(e)
 
    async def get_balance(self):
        if not self._ex: return 0.0
        try:
            bal = await asyncio.to_thread(self._ex.fetch_balance)
            return float(bal.get("USDT", {}).get("free", 0))
        except: return 0.0
 
    async def place_order(self, symbol, side, usdt, leverage, tp, sl):
        if not self._ex: return {"error": "Not connected"}
        try:
            sym_fmt = symbol.replace("USDT", "/USDT:USDT")
            try: await asyncio.to_thread(self._ex.set_leverage, leverage, sym_fmt)
            except: pass
            ticker = await asyncio.to_thread(self._ex.fetch_ticker, sym_fmt)
            qty    = round(usdt / ticker["last"], 6)
            order  = await asyncio.to_thread(self._ex.create_market_order, sym_fmt, side.lower(), qty)
            cs     = "sell" if side.upper() == "BUY" else "buy"
            try:
                await asyncio.to_thread(self._ex.create_order, sym_fmt, "take_profit_market", cs, qty, tp,
                                        {"stopPrice": tp, "closePosition": True, "reduceOnly": True})
                await asyncio.to_thread(self._ex.create_order, sym_fmt, "stop_market", cs, qty, sl,
                                        {"stopPrice": sl, "closePosition": True, "reduceOnly": True})
            except Exception as e:
                log.warning(f"TP/SL: {e}")
            return {"id": order["id"], "status": "filled", "symbol": symbol,
                    "side": side, "amount": usdt, "price": ticker["last"], "qty": qty}
        except Exception as e:
            return {"error": str(e)}
 
 
# ══════════════════════════════════════════
#  AGENT
# ══════════════════════════════════════════
@dataclass
class OpenTrade:
    id: str; symbol: str; side: str
    entry: float; tp: float; sl: float
    amount: float; leverage: int; exchange: str
    opened: float = field(default_factory=time.time)
    pnl: float = 0.0; status: str = "open"
 
 
class Agent:
    def __init__(self):
        self.running   = False
        self.exchange: Optional[ExchangeClient] = None
        self.risk_pct  = 1.5
        self.min_conf  = 65
        self.max_open  = 3
        self.trades:   list[OpenTrade] = []
        self.closed:   list[OpenTrade] = []
        self.total_pnl = 0.0
        self.scans     = 0
 
    def configure(self, exchange, risk_pct=1.5, min_conf=65, max_open=3):
        self.exchange = exchange
        self.risk_pct = risk_pct
        self.min_conf = min_conf
        self.max_open = max_open
 
    async def start(self):
        self.running = True
        await notify(
            f"🟢 *AXIFLOW Agent активний*\n\n"
            f"🏦 {self.exchange.exchange.upper()}\n"
            f"📊 Smart Money: Sweeps · FVG · OB · CVD · AMD\n"
            f"⚡ Конф. ≥ {self.min_conf}% | Ризик {self.risk_pct}%\n"
            f"📐 Мін. RR 1:3 | Рух ≥ 2%"
        )
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
        if not self.exchange or not self.exchange.connected: return
        open_syms = {t.symbol for t in self.trades if t.status == "open"}
        if len(open_syms) >= self.max_open: return
        for sym in SYMBOLS:
            if sym in open_syms: continue
            sig = cache.get(sym)
            if not sig or sig.get("decision") == "NO TRADE": continue
            if sig.get("confidence", 0) < self.min_conf: continue
            if sig.get("rr", 0) < 3.0: continue
            await self._open(sig)
            if len({t.symbol for t in self.trades if t.status == "open"}) >= self.max_open: break
 
    async def _open(self, sig):
        bal      = await self.exchange.get_balance()
        pos_size = bal * (self.risk_pct / 100) * sig["lev"]
        if pos_size < 5: return
        side  = "BUY" if sig["decision"] == "LONG" else "SELL"
        order = await self.exchange.place_order(sig["symbol"], side, pos_size,
                                                sig["lev"], sig["tp"], sig["sl"])
        if "error" in order:
            await notify(f"❌ Order error `{sig['symbol']}`:\n{order['error']}"); return
        t = OpenTrade(id=order["id"], symbol=sig["symbol"], side=side,
                      entry=sig["entry"], tp=sig["tp"], sl=sig["sl"],
                      amount=pos_size, leverage=sig["lev"], exchange=self.exchange.exchange)
        self.trades.append(t)
        await notify(
            f"{'🟢' if side=='BUY' else '🔴'} *{sig['decision']} відкрито*\n\n"
            f"📌 `{sig['symbol']}`\n"
            f"💲 `${sig['entry']:,.4f}`\n"
            f"🎯 TP `${sig['tp']:,.4f}` (+{sig['move_pct']:.1f}%)\n"
            f"🛑 SL `${sig['sl']:,.4f}`\n"
            f"📐 RR `1:{sig['rr']:.1f}` | ⚡ `{sig['lev']}x`\n"
            f"💰 `${pos_size:.0f}` | `{sig['confidence']}%`\n"
            f"🧠 `{sig['strategy']}`"
        )
 
    async def _monitor(self):
        for t in [t for t in self.trades if t.status == "open"]:
            sig = cache.get(t.symbol, {})
            price = sig.get("raw", {}).get("price", t.entry)
            if not price: continue
            if t.side == "BUY":
                pnl_pct = (price - t.entry) / t.entry * 100 * t.leverage
                hit_tp  = price >= t.tp; hit_sl = price <= t.sl
            else:
                pnl_pct = (t.entry - price) / t.entry * 100 * t.leverage
                hit_tp  = price <= t.tp; hit_sl = price >= t.sl
            t.pnl = t.amount * pnl_pct / 100
            if hit_tp:
                t.status = "tp"; self.total_pnl += t.pnl; self.closed.append(t)
                await notify(f"✅ *TP Hit!*\n`{t.symbol}` +${t.pnl:.2f} (+{pnl_pct:.1f}%)\n📈 Total: `${self.total_pnl:.2f}`")
            elif hit_sl:
                t.status = "sl"; self.total_pnl += t.pnl; self.closed.append(t)
                await notify(f"🛑 *SL Hit*\n`{t.symbol}` ${t.pnl:.2f} ({pnl_pct:.1f}%)\n📉 Total: `${self.total_pnl:.2f}`")
 
    def stats(self):
        open_t = [t for t in self.trades if t.status == "open"]
        wins   = [t for t in self.closed if t.pnl > 0]
        return {
            "running":      self.running,
            "scans":        self.scans,
            "open_count":   len(open_t),
            "closed_count": len(self.closed),
            "win_rate":     round(len(wins)/max(len(self.closed),1)*100,1),
            "total_pnl":    round(self.total_pnl, 2),
            "exchange":     self.exchange.exchange if self.exchange else "",
            "connected":    self.exchange.connected if self.exchange else False,
            "open_trades":  [{"id":t.id,"symbol":t.symbol,"side":t.side,"entry":t.entry,
                               "tp":t.tp,"sl":t.sl,"pnl":round(t.pnl,2),"lev":t.leverage,
                               "amount":round(t.amount,2),"exchange":t.exchange} for t in open_t],
            "closed_trades":[{"symbol":t.symbol,"side":t.side,"pnl":round(t.pnl,2),
                               "status":t.status} for t in self.closed[-10:]],
        }
 
 
# ══════════════════════════════════════════
#  TELEGRAM
# ══════════════════════════════════════════
async def notify(text):
    if not TG_TOKEN or not TG_CHAT: return
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            await c.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                         json={"chat_id": TG_CHAT, "text": text,
                               "parse_mode": "Markdown", "disable_web_page_preview": True})
    except Exception as e:
        log.warning(f"TG: {e}")
 
 
# ══════════════════════════════════════════
#  GLOBALS
# ══════════════════════════════════════════
cache:         dict  = {}
agent:         Agent = Agent()
wallets:       dict  = {}
manual_trades: list  = []
 
 
async def scan_loop():
    log.info("Smart Money scanner started")
    while True:
        try:
            for sym in SYMBOLS:
                await analyze_symbol(sym)
                await asyncio.sleep(1)
        except Exception as e:
            log.error(f"Scan: {e}")
        await asyncio.sleep(20)
 
 
@asynccontextmanager
async def lifespan(app: FastAPI):
    asyncio.create_task(scan_loop())
    yield
 
 
# ══════════════════════════════════════════
#  FASTAPI
# ══════════════════════════════════════════
app = FastAPI(title="AXIFLOW v3", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
 
static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
os.makedirs(static_dir, exist_ok=True)
app.mount("/static", StaticFiles(directory=static_dir, html=True), name="static")
 
 
@app.get("/")
async def root():
    return {"status": "ok", "service": "AXIFLOW v3 — Pure Smart Money"}
 
@app.get("/api/signals")
async def signals():
    return {"signals": cache, "ts": time.time()}
 
@app.get("/api/signal/{symbol}")
async def signal(symbol: str, fresh: bool = False):
    sym = symbol.upper()
    if fresh or sym not in cache:
        await analyze_symbol(sym)
    return cache.get(sym, {"symbol": sym, "decision": "NO TRADE"})
 
@app.get("/api/market/{symbol}")
async def market(symbol: str):
    sym = symbol.upper()
    ticker, oi, fr, lq, ob = await asyncio.gather(
        get_ticker(sym), get_oi(sym), get_funding(sym), get_liqs(sym), get_ob(sym)
    )
    return {"symbol": sym, "ticker": ticker, "oi": oi,
            "funding": fr, "liquidations": lq, "orderbook": ob, "ts": time.time()}
 
@app.get("/api/klines/{symbol}")
async def klines(symbol: str, interval: str = "5m", limit: int = 100):
    sym = symbol.upper()
    data = await get_klines(sym, interval, limit)
    return {"symbol": sym, "interval": interval, "candles": data}
 
 
class WalletReq(BaseModel):
    user_id: str; exchange: str = ""; api_key: str = ""
    api_secret: str = ""; testnet: bool = False
 
class AgentReq(BaseModel):
    user_id: str; action: str
    risk_pct: float = 1.5; min_conf: int = 65; max_open: int = 3
 
class TradeReq(BaseModel):
    user_id: str; symbol: str; side: str; amount: float; leverage: int = 1
 
 
@app.post("/api/wallet")
async def connect_wallet(req: WalletReq):
    if not req.api_key or not req.api_secret:
        return {"success": False, "error": "Введіть API ключі"}
    if not req.exchange:
        return {"success": False, "error": "Виберіть біржу"}
    ex = ExchangeClient(req.exchange, req.api_key, req.api_secret, req.testnet)
    if not ex.connected:
        return {"success": False, "error": ex.error or "Помилка підключення"}
    ok, result = await ex.test_connection()
    if not ok:
        return {"success": False, "error": str(result)}
    wallets[req.user_id] = {"exchange": req.exchange, "balance": float(result),
                             "testnet": req.testnet, "connected": True, "client": ex}
    return {"success": True, "balance": float(result), "exchange": req.exchange}
 
@app.get("/api/wallet/{user_id}")
async def get_wallet(user_id: str):
    w = wallets.get(user_id, {})
    if not w: return {"connected": False}
    if "client" in w:
        try: w["balance"] = await w["client"].get_balance()
        except: pass
    return {k: v for k, v in w.items() if k != "client"}
 
@app.post("/api/agent")
async def control_agent(req: AgentReq):
    w  = wallets.get(req.user_id)
    ex = w["client"] if w and "client" in w else None
    if req.action == "start":
        if agent.running: return {"success": False, "msg": "Агент вже запущений"}
        if not ex: return {"success": False, "msg": "Підключіть API ключі"}
        agent.configure(ex, req.risk_pct, req.min_conf, req.max_open)
        asyncio.create_task(agent.start())
        return {"success": True, "msg": "Агент запущений"}
    elif req.action == "stop":
        agent.stop(); return {"success": True, "msg": "Агент зупинений"}
    elif req.action == "status":
        return agent.stats()
    return {"success": False}
 
@app.post("/api/trade")
async def manual_trade(req: TradeReq):
    w = wallets.get(req.user_id)
    ex = w["client"] if w and "client" in w else None
    if not ex: return {"success": False, "error": "Підключіть API"}
    sig = cache.get(req.symbol.upper(), {})
    order = await ex.place_order(req.symbol.upper(), req.side, req.amount,
                                  req.leverage, sig.get("tp", 0), sig.get("sl", 0))
    if "error" in order: return {"success": False, "error": order["error"]}
    manual_trades.append({**order, "ts": time.time()})
    return {"success": True, "trade": order}
 
@app.get("/api/trades/{user_id}")
async def get_trades(user_id: str):
    return {"trades": manual_trades[-30:]}
