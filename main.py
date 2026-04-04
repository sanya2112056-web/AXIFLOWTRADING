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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)
log = logging.getLogger("axiflow")

TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT  = os.environ.get("TELEGRAM_CHAT_ID", "")
PORT     = int(os.environ.get("PORT", 3000))
BFUT     = "https://fapi.binance.com"

SYMBOLS = [
    "SOLUSDT", "AVAXUSDT", "NEARUSDT", "INJUSDT", "APTUSDT",
    "ARBUSDT", "OPUSDT",   "SUIUSDT",  "TIAUSDT", "DOTUSDT",
    "LINKUSDT","ATOMUSDT", "ADAUSDT",  "MATICUSDT","LTCUSDT",
    "BTCUSDT", "ETHUSDT",  "BNBUSDT",  "XRPUSDT", "DOGEUSDT",
]

SIGNAL_COOLDOWN = 900
MIN_CONFIDENCE  = 70

_last_signal: dict = {}
cache:         dict = {}
wallets:       dict = {}
manual_trades: list = []


@dataclass
class Signal:
    symbol:     str
    decision:   str
    confidence: int
    strategy:   str
    entry:      float
    tp1:        float
    tp2:        float
    tp3:        float
    sl:         float
    rr:         float
    move_pct:   float
    lev:        int
    reasons:    list
    raw:        dict
    ts:         float = field(default_factory=time.time)

    def to_dict(self):
        return {
            "symbol":     self.symbol,
            "decision":   self.decision,
            "confidence": self.confidence,
            "strategy":   self.strategy,
            "entry":      self.entry,
            "tp":         round(self.tp1, 8),
            "tp1":        round(self.tp1, 8),
            "tp2":        round(self.tp2, 8),
            "tp3":        round(self.tp3, 8),
            "sl":         round(self.sl,  8),
            "rr":         round(self.rr,  2),
            "move_pct":   round(self.move_pct, 2),
            "lev":        self.lev,
            "reasons":    self.reasons,
            "raw":        self.raw,
            "ts":         self.ts,
        }


def no_trade(sym, price, oi_d=0, fr=0, liq_r=1, ob_i=0, reason="No signal"):
    return {
        "symbol": sym, "decision": "NO TRADE", "confidence": 0,
        "strategy": "STANDARD", "entry": price,
        "tp": price, "tp1": price, "tp2": price, "tp3": price,
        "sl": price, "rr": 0, "move_pct": 0, "lev": 1,
        "reasons": [reason],
        "raw": {
            "price": price, "change": 0, "oi_15m": oi_d,
            "funding": fr, "liq_ratio": liq_r, "ob_imb": ob_i,
            "conf_long": 0, "conf_short": 0,
            "structure": "unknown", "buying_pressure": 50,
        },
        "ts": time.time(),
    }


async def _get(url, params=None):
    try:
        async with httpx.AsyncClient(timeout=9) as c:
            r = await c.get(url, params=params)
            r.raise_for_status()
            return r.json()
    except Exception as e:
        log.debug("GET %s: %s", url, e)
        return None


async def get_ticker(sym):
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


async def get_klines(sym, tf="5m", limit=150):
    d = await _get(f"{BFUT}/fapi/v1/klines",
                   {"symbol": sym, "interval": tf, "limit": limit})
    if not d:
        return []
    return [
        {"o": float(x[1]), "h": float(x[2]), "l": float(x[3]),
         "c": float(x[4]), "v": float(x[5]), "t": int(x[0])}
        for x in d
    ]


async def get_oi(sym):
    now  = await _get(f"{BFUT}/fapi/v1/openInterest", {"symbol": sym})
    hist = await _get(f"{BFUT}/futures/data/openInterestHist",
                      {"symbol": sym, "period": "5m", "limit": 36})
    if not now:
        return {"delta_15m": 0, "delta_1h": 0, "delta_4h": 0,
                "strength": 0, "current": 0, "trend": "flat"}
    cur  = float(now["openInterest"])
    vals = [float(h["sumOpenInterest"]) for h in (hist or [])]
    p15  = vals[-3]  if len(vals) >= 3  else cur
    p1h  = vals[-12] if len(vals) >= 12 else cur
    p4h  = vals[-36] if len(vals) >= 36 else cur
    d15  = (cur - p15) / max(p15, 1) * 100
    d1h  = (cur - p1h) / max(p1h, 1) * 100
    d4h  = (cur - p4h) / max(p4h, 1) * 100
    trend = "rising" if d1h > 2 else "falling" if d1h < -2 else "flat"
    return {
        "current": cur, "delta_15m": d15, "delta_1h": d1h, "delta_4h": d4h,
        "strength": 2 if abs(d15) > 5 else 1 if abs(d15) > 2 else 0,
        "trend": trend,
    }


async def get_funding(sym):
    d  = await _get(f"{BFUT}/fapi/v1/premiumIndex", {"symbol": sym})
    fr = float(d["lastFundingRate"]) if d else 0.0
    return {
        "rate":          fr,
        "extreme_long":  fr >  0.010,
        "extreme_short": fr < -0.010,
        "bullish":       fr < -0.005,
        "bearish":       fr >  0.005,
    }


async def get_liqs(sym):
    d = await _get(f"{BFUT}/fapi/v1/allForceOrders",
                   {"symbol": sym, "limit": 500})
    if not d:
        return {"ratio": 1.0, "long_vol": 0, "short_vol": 0,
                "strength": 0, "total": 0}
    lv = sum(float(o["origQty"]) * float(o["price"])
             for o in d if o.get("side") == "SELL")
    sv = sum(float(o["origQty"]) * float(o["price"])
             for o in d if o.get("side") == "BUY")
    r  = lv / max(sv, 1)
    return {
        "ratio": r, "long_vol": lv, "short_vol": sv, "total": lv + sv,
        "strength": 2 if r > 3 or r < 0.33 else 1 if r > 2 or r < 0.5 else 0,
    }


async def get_ob(sym):
    d = await _get(f"{BFUT}/fapi/v1/depth", {"symbol": sym, "limit": 100})
    if not d:
        return {"imbalance": 0, "strength": 0}
    bv = sum(float(b[1]) for b in d.get("bids", []))
    av = sum(float(a[1]) for a in d.get("asks", []))
    total = bv + av
    imb   = (bv - av) / total if total else 0
    return {
        "bid": bv, "ask": av, "imbalance": imb,
        "strength": 2 if abs(imb) > 0.2 else 1 if abs(imb) > 0.1 else 0,
    }


async def get_ls(sym):
    d = await _get(
        f"{BFUT}/futures/data/globalLongShortAccountRatio",
        {"symbol": sym, "period": "5m", "limit": 3}
    )
    if not d:
        return {"ratio": 1.0, "long_pct": 50.0, "short_pct": 50.0}
    last = d[-1]
    return {
        "ratio":     float(last.get("longShortRatio", 1)),
        "long_pct":  float(last.get("longAccount",    0.5)) * 100,
        "short_pct": float(last.get("shortAccount",   0.5)) * 100,
    }


def market_structure(candles):
    if len(candles) < 20:
        return {"phase": "range", "trend": "flat"}
    data = candles[-30:]
    ph, pl = [], []
    for i in range(2, len(data) - 2):
        if data[i]["h"] > data[i-1]["h"] and data[i]["h"] > data[i+1]["h"]:
            ph.append(data[i]["h"])
        if data[i]["l"] < data[i-1]["l"] and data[i]["l"] < data[i+1]["l"]:
            pl.append(data[i]["l"])
    if len(ph) >= 2 and len(pl) >= 2:
        if ph[-1] > ph[-2] and pl[-1] > pl[-2]:
            return {"phase": "uptrend",   "trend": "up"}
        if ph[-1] < ph[-2] and pl[-1] < pl[-2]:
            return {"phase": "downtrend", "trend": "down"}
    return {"phase": "range", "trend": "flat"}


def market_phase(candles, oi_delta):
    if len(candles) < 20:
        return "unknown"
    recent  = candles[-20:]
    vols    = [c["v"] for c in recent]
    hi      = max(c["h"] for c in recent)
    lo      = min(c["l"] for c in recent)
    rng_pct = (hi - lo) / max(lo, 1) * 100
    avg_vol = sum(vols) / len(vols)
    if rng_pct < 2.0 and vols[-1] < avg_vol * 0.7 and abs(oi_delta) < 3:
        return "accumulation"
    if rng_pct > 4.0:
        return "distribution" if candles[-1]["c"] < candles[-5]["c"] else "expansion"
    return "consolidation"


def is_flat(candles):
    if len(candles) < 15:
        return True
    data    = candles[-15:]
    hi      = max(c["h"] for c in data)
    lo      = min(c["l"] for c in data)
    rng_pct = (hi - lo) / max(lo, 1) * 100
    return rng_pct < 1.0


def detect_fvg(candles, lb=60):
    fvgs = []
    data = candles[-lb:]
    for i in range(2, len(data)):
        c0 = data[i-2]
        c2 = data[i]
        if c2["l"] > c0["h"]:
            sz = (c2["l"] - c0["h"]) / max(c0["h"], 1) * 100
            if sz > 0.06:
                fvgs.append({"type": "bullish",
                             "top":  c2["l"], "bot": c0["h"],
                             "mid":  (c2["l"] + c0["h"]) / 2, "size": sz})
        elif c2["h"] < c0["l"]:
            sz = (c0["l"] - c2["h"]) / max(c0["l"], 1) * 100
            if sz > 0.06:
                fvgs.append({"type": "bearish",
                             "top":  c0["l"], "bot": c2["h"],
                             "mid":  (c0["l"] + c2["h"]) / 2, "size": sz})
    return fvgs[-6:] if fvgs else []


def detect_obs(candles, lb=100):
    obs  = []
    data = candles[-lb:]
    avg  = sum(abs(c["c"] - c["o"]) for c in data) / max(len(data), 1)
    for i in range(1, len(data) - 2):
        c  = data[i]
        n  = data[i+1]
        nb = abs(n["c"] - n["o"])
        if c["c"] < c["o"] and n["c"] > n["o"] and nb > avg * 1.5:
            obs.append({"type": "bullish",
                        "top": c["o"], "bot": c["c"],
                        "mid": (c["o"] + c["c"]) / 2})
        elif c["c"] > c["o"] and n["c"] < n["o"] and nb > avg * 1.5:
            obs.append({"type": "bearish",
                        "top": c["c"], "bot": c["o"],
                        "mid": (c["c"] + c["o"]) / 2})
    return obs[-6:] if obs else []


def detect_sweep(candles, lb=60):
    data = candles[-lb:]
    if len(data) < 10:
        return {"bull_sweep": False, "bear_sweep": False,
                "sweep_pct_bull": 0, "sweep_pct_bear": 0}
    recent = data[:-4]
    last4  = data[-4:]
    ph  = max(c["h"] for c in recent)
    pl  = min(c["l"] for c in recent)
    lh  = max(c["h"] for c in last4)
    ll  = min(c["l"] for c in last4)
    lc  = data[-1]["c"]
    bs  = ll < pl and lc > pl
    be  = lh > ph and lc < ph
    return {
        "bull_sweep":      bool(bs),
        "bear_sweep":      bool(be),
        "sweep_pct_bull":  (pl - ll) / max(pl, 1) * 100 if bs else 0,
        "sweep_pct_bear":  (lh - ph) / max(ph, 1) * 100 if be else 0,
        "prev_high":       ph,
        "prev_low":        pl,
    }


def detect_cvd(candles):
    if len(candles) < 20:
        return {"divergence": 0, "absorption": False, "buying_pressure": 50.0}
    cvd_vals = []
    cum      = 0.0
    for c in candles:
        cum += c["v"] if c["c"] >= c["o"] else -c["v"]
        cvd_vals.append(cum)
    period  = 12
    p_start = candles[-period]["c"]
    p_end   = candles[-1]["c"]
    cvd_s   = cvd_vals[-period]
    cvd_e   = cvd_vals[-1]
    price_up = p_end > p_start * 1.001
    price_dn = p_end < p_start * 0.999
    cvd_up   = cvd_e > cvd_s
    cvd_dn   = cvd_e < cvd_s
    div = 0
    if price_up and cvd_dn:
        div = -1
    if price_dn and cvd_up:
        div = 1
    last   = candles[-1]
    avg_v  = sum(c["v"] for c in candles[-20:]) / 20
    avg_b  = sum(abs(c["c"] - c["o"]) for c in candles[-20:]) / 20
    absorb = last["v"] > avg_v * 2.0 and abs(last["c"] - last["o"]) < avg_b * 0.4
    bv = sum(c["v"] for c in candles[-10:] if c["c"] >= c["o"])
    sv = sum(c["v"] for c in candles[-10:] if c["c"] <  c["o"])
    bp = bv / (bv + sv) * 100 if (bv + sv) > 0 else 50.0
    return {
        "divergence":      div,
        "absorption":      bool(absorb),
        "buying_pressure": bp,
        "cvd_trend":       "rising" if cvd_up else "falling" if cvd_dn else "flat",
    }


def liq_clusters(candles):
    if len(candles) < 30:
        return {"clusters_above": [], "clusters_below": []}
    tol = 0.0015
    hs  = [c["h"] for c in candles[-60:]]
    ls  = [c["l"] for c in candles[-60:]]
    ca  = list(set(
        round(h, 6) for i, h in enumerate(hs)
        if sum(1 for hh in hs[i+1:] if abs(hh - h) / max(h, 1) < tol) >= 2
    ))[:4]
    cb  = list(set(
        round(l, 6) for i, l in enumerate(ls)
        if sum(1 for ll in ls[i+1:] if abs(ll - l) / max(l, 1) < tol) >= 2
    ))[:4]
    return {"clusters_above": ca, "clusters_below": cb}


def vol_ratio(candles):
    if len(candles) < 20:
        return 1.0
    avg = sum(c["v"] for c in candles[-20:-5]) / 15
    rec = sum(c["v"] for c in candles[-5:])    / 5
    return rec / max(avg, 0.001)


def vol_spike(candles):
    if len(candles) < 20:
        return False
    avg = sum(c["v"] for c in candles[-20:-1]) / 19
    return candles[-1]["v"] > avg * 2.5


def score_all(ticker, oi, funding, liqs, ob_data, ls_ratio,
              cvd5, cvd15, sw5, sw15, fvg5, fvg15, ob5, ob15,
              liq_cl, struct, phase, price, vr, has_spike):

    dl, ds = 0.0, 0.0
    fl, fs = 0.0, 0.0
    ll, ls = 0.0, 0.0
    sl, ss = 0.0, 0.0
    rl, rs = [], []

    d15   = oi["delta_15m"]
    d1h   = oi["delta_1h"]
    fr    = funding["rate"]
    liq_r = liqs["ratio"]
    im    = ob_data["imbalance"]
    bp    = cvd5["buying_pressure"]
    lsr   = ls_ratio.get("ratio", 1.0)
    chg   = ticker["change"]

    # PILLAR 1: DERIVATIVES (max 25pts)
    if d15 > 5:
        dl += 8; rl.append(f"OI +{d15:.1f}% aggressive - strong longs entering")
    elif d15 > 2 and chg > 0:
        dl += 5; rl.append(f"OI +{d15:.1f}% + price up - long accumulation")
    elif d15 > 2 and chg < 0:
        ds += 5; rs.append(f"OI +{d15:.1f}% + price down - short pressure")
    elif d15 < -3:
        ds += 4; rs.append(f"OI -{abs(d15):.1f}% - longs closing")

    if d1h > 5:
        dl += 4; rl.append(f"OI 1H +{d1h:.1f}% - strong bullish trend")
    elif d1h < -5:
        ds += 4; rs.append(f"OI 1H -{abs(d1h):.1f}% - strong bearish trend")

    if funding["extreme_short"]:
        dl += 8; rl.append(f"Funding oversold {fr*100:.4f}% - long squeeze coming")
    elif funding["bullish"]:
        dl += 4; rl.append(f"Funding {fr*100:.4f}% - bullish bias")
    elif funding["extreme_long"]:
        ds += 8; rs.append(f"Funding overbought {fr*100:.4f}% - short squeeze coming")
    elif funding["bearish"]:
        ds += 4; rs.append(f"Funding {fr*100:.4f}% - bearish bias")

    if liq_r > 3:
        dl += 8; rl.append(f"Mass long liquidations x{liq_r:.1f} - bull reversal")
    elif liq_r > 2:
        dl += 5; rl.append(f"Long liquidations x{liq_r:.1f} - bullish signal")
    elif liq_r < 0.33:
        ds += 8; rs.append(f"Mass short liquidations - bear reversal")
    elif liq_r < 0.5:
        ds += 5; rs.append(f"Short liquidations - bearish signal")

    if lsr < 0.7:
        dl += 5; rl.append(f"L/S ratio {lsr:.2f} - crowd short = squeeze up possible")
    elif lsr > 1.4:
        ds += 5; rs.append(f"L/S ratio {lsr:.2f} - crowd long = squeeze down possible")

    if has_spike and chg > 3:
        dl += 4; rl.append("Volume spike + price up - pump signal")
    elif has_spike and chg < -3:
        ds += 4; rs.append("Volume spike + price down - dump signal")

    # PILLAR 2: ORDER FLOW (max 25pts)
    if cvd5["divergence"] == 1:
        fl += 8; rl.append("CVD bullish divergence: price down but buyers dominate")
    elif cvd5["divergence"] == -1:
        fs += 8; rs.append("CVD bearish divergence: price up but sellers dominate")

    if cvd15["divergence"] == 1:
        fl += 5; rl.append("CVD 15M bullish - confirms long")
    elif cvd15["divergence"] == -1:
        fs += 5; rs.append("CVD 15M bearish - confirms short")

    if bp > 65:
        fl += 7; rl.append(f"Buy pressure {bp:.0f}% - aggressive buyers in market")
    elif bp < 35:
        fs += 7; rs.append(f"Sell pressure {100-bp:.0f}% - aggressive sellers in market")

    if cvd5["absorption"]:
        if chg >= 0:
            fl += 6; rl.append("Absorption: high vol no move = smart money buying")
        else:
            fs += 6; rs.append("Absorption: high vol no move = smart money selling")

    if im > 0.25:
        fl += 7; rl.append(f"OB strong buy pressure {im:+.2f}")
    elif im > 0.12:
        fl += 4; rl.append(f"OB buy pressure {im:+.2f}")
    elif im < -0.25:
        fs += 7; rs.append(f"OB strong sell pressure {im:+.2f}")
    elif im < -0.12:
        fs += 4; rs.append(f"OB sell pressure {im:+.2f}")

    # PILLAR 3: LIQUIDITY (max 25pts)
    if sw5["bull_sweep"]:
        ll += 12
        rl.append(f"Liquidity Sweep: stops below taken ({sw5['sweep_pct_bull']:.2f}%) -> LONG")
    if sw5["bear_sweep"]:
        ls += 12
        rs.append(f"Liquidity Sweep: stops above taken ({sw5['sweep_pct_bear']:.2f}%) -> SHORT")
    if sw15["bull_sweep"]:
        ll += 6; rl.append("15M sweep below - confirms long bias")
    if sw15["bear_sweep"]:
        ls += 6; rs.append("15M sweep above - confirms short bias")

    for fvg in (fvg5 + fvg15)[-6:]:
        iz = fvg["bot"] <= price <= fvg["top"]
        if fvg["type"] == "bullish" and (iz or price < fvg["mid"] * 1.005):
            ll += 5; rl.append(f"Bullish FVG: {fvg['bot']:.4f}-{fvg['top']:.4f} ({fvg['size']:.2f}%)")
        elif fvg["type"] == "bearish" and (iz or price > fvg["mid"] * 0.995):
            ls += 5; rs.append(f"Bearish FVG: {fvg['bot']:.4f}-{fvg['top']:.4f} ({fvg['size']:.2f}%)")

    for ob_z in (ob5 + ob15)[-6:]:
        if ob_z["type"] == "bullish" and ob_z["bot"] <= price <= ob_z["top"] * 1.01:
            ll += 5; rl.append(f"Bullish OB: {ob_z['bot']:.4f}-{ob_z['top']:.4f}")
        elif ob_z["type"] == "bearish" and ob_z["bot"] * 0.99 <= price <= ob_z["top"]:
            ls += 5; rs.append(f"Bearish OB: {ob_z['bot']:.4f}-{ob_z['top']:.4f}")

    for cl in liq_cl.get("clusters_above", []):
        if abs(cl - price) / max(price, 1) < 0.03:
            ll += 3; rl.append(f"Liq cluster above {cl:.4f} - magnet target")

    for cl in liq_cl.get("clusters_below", []):
        if abs(cl - price) / max(price, 1) < 0.03:
            ls += 3; rs.append(f"Liq cluster below {cl:.4f} - magnet target")

    # PILLAR 4: STRUCTURE (max 25pts)
    if struct["phase"] == "uptrend":
        sl += 12; rl.append("Structure 15M: HH/HL uptrend confirmed")
    elif struct["phase"] == "downtrend":
        ss += 12; rs.append("Structure 15M: LH/LL downtrend confirmed")
    elif struct["phase"] == "range":
        if sw5["bull_sweep"]:   sl += 6
        elif sw5["bear_sweep"]: ss += 6

    if phase == "accumulation":
        sl += 8; rl.append("Phase: accumulation - expecting move up")
    elif phase == "distribution":
        ss += 8; rs.append("Phase: distribution - expecting move down")
    elif phase == "expansion":
        sl += 5; rl.append("Phase: expansion upward")

    pp = (price - ticker["low"]) / max(ticker["high"] - ticker["low"], 0.001) * 100
    if pp < 25:
        sl += 8; rl.append(f"Price near 24H low ({pp:.0f}%) - upside potential")
    elif pp > 75:
        ss += 8; rs.append(f"Price near 24H high ({pp:.0f}%) - downside potential")

    conf_long  = int(min(25, dl) + min(25, fl) + min(25, ll) + min(25, sl))
    conf_short = int(min(25, ds) + min(25, fs) + min(25, ls) + min(25, ss))
    return conf_long, conf_short, rl, rs


def calc_levels(price, direction, candles, fvg5, fvg15, ob5, liq_cl, ticker):
    recent  = candles[-20:]
    avg_rng = sum(c["h"] - c["l"] for c in recent) / max(len(recent), 1)
    atr     = avg_rng * 1.3

    if direction == "LONG":
        sl_cands = [price - atr * 1.5]
        for ob_z in ob5:
            if ob_z["type"] == "bullish" and ob_z["bot"] < price:
                sl_cands.append(ob_z["bot"] * 0.997)
        for fvg in fvg5 + fvg15:
            if fvg["type"] == "bullish" and fvg["bot"] < price:
                sl_cands.append(fvg["bot"] * 0.997)
        sl  = max(sl_cands)
        sl_d = price - sl
        tp1  = price + sl_d * 1.5
        tp2  = price + sl_d * 2.5
        tp3  = price + sl_d * 4.0
        for cl in sorted(liq_cl.get("clusters_above", [])):
            if cl > price * 1.01:
                tp3 = cl * 0.999
                break
        if ticker["high"] > price * 1.02:
            tp3 = max(tp3, ticker["high"] * 0.999)
    else:
        sl_cands = [price + atr * 1.5]
        for ob_z in ob5:
            if ob_z["type"] == "bearish" and ob_z["top"] > price:
                sl_cands.append(ob_z["top"] * 1.003)
        for fvg in fvg5 + fvg15:
            if fvg["type"] == "bearish" and fvg["top"] > price:
                sl_cands.append(fvg["top"] * 1.003)
        sl   = min(sl_cands)
        sl_d = sl - price
        tp1  = price - sl_d * 1.5
        tp2  = price - sl_d * 2.5
        tp3  = price - sl_d * 4.0
        for cl in sorted(liq_cl.get("clusters_below", []), reverse=True):
            if cl < price * 0.99:
                tp3 = cl * 1.002
                break
        if ticker["low"] < price * 0.98:
            tp3 = min(tp3, ticker["low"] * 1.002)

    rr       = abs(tp2 - price) / max(abs(sl - price), 0.0001)
    move_pct = abs(tp2 - price) / max(price, 1) * 100
    return tp1, tp2, tp3, sl, rr, move_pct


async def analyze_symbol(sym: str):
    try:
        (ticker, c5m, c15m, oi, funding,
         liqs, ob_data, ls) = await asyncio.gather(
            get_ticker(sym),
            get_klines(sym, "5m",  150),
            get_klines(sym, "15m", 80),
            get_oi(sym),
            get_funding(sym),
            get_liqs(sym),
            get_ob(sym),
            get_ls(sym),
        )

        if not ticker or len(c5m) < 30 or len(c15m) < 10:
            return None

        price = ticker["price"]

        # Pre-filters
        flat = is_flat(c15m)
        sw_check = detect_sweep(c5m, 60)
        if flat and not sw_check["bull_sweep"] and not sw_check["bear_sweep"]:
            r = no_trade(sym, price, oi["delta_15m"], funding["rate"],
                         liqs["ratio"], ob_data["imbalance"],
                         "Flat market - skip")
            cache[sym] = r
            return r

        vr       = vol_ratio(c5m)
        has_spike = vol_spike(c5m)
        if vr < 0.35 and not has_spike:
            r = no_trade(sym, price, oi["delta_15m"], funding["rate"],
                         liqs["ratio"], ob_data["imbalance"],
                         "Low volume - no liquidity")
            cache[sym] = r
            return r

        struct = market_structure(c15m)
        phase  = market_phase(c5m, oi["delta_15m"])
        fvg5   = detect_fvg(c5m,  60)
        fvg15  = detect_fvg(c15m, 40)
        ob5    = detect_obs(c5m,  100)
        ob15   = detect_obs(c15m, 60)
        sw5    = detect_sweep(c5m,  60)
        sw15   = detect_sweep(c15m, 40)
        cvd5   = detect_cvd(c5m)
        cvd15  = detect_cvd(c15m)
        liq_cl = liq_clusters(c5m)

        conf_long, conf_short, rl, rs = score_all(
            ticker, oi, funding, liqs, ob_data, ls,
            cvd5, cvd15, sw5, sw15, fvg5, fvg15, ob5, ob15,
            liq_cl, struct, phase, price, vr, has_spike,
        )

        if conf_long >= MIN_CONFIDENCE and conf_long > conf_short:
            direction  = "LONG"
            confidence = conf_long
            reasons    = rl[:7]
        elif conf_short >= MIN_CONFIDENCE and conf_short > conf_long:
            direction  = "SHORT"
            confidence = conf_short
            reasons    = rs[:7]
        else:
            r = no_trade(sym, price, oi["delta_15m"], funding["rate"],
                         liqs["ratio"], ob_data["imbalance"],
                         f"Conf LONG={conf_long}% SHORT={conf_short}% below {MIN_CONFIDENCE}%")
            cache[sym] = r
            return r

        tp1, tp2, tp3, sl, rr, move_pct = calc_levels(
            price, direction, c5m, fvg5, fvg15, ob5, liq_cl, ticker
        )

        if rr < 1.5:
            r = no_trade(sym, price, oi["delta_15m"], funding["rate"],
                         liqs["ratio"], ob_data["imbalance"],
                         f"RR {rr:.1f} < 1:1.5 - skip")
            cache[sym] = r
            return r

        lev = (10 if confidence >= 90 else
               7  if confidence >= 85 else
               5  if confidence >= 80 else 3)

        sp = []
        if sw5["bull_sweep"] or sw5["bear_sweep"]: sp.append("SWEEP")
        if any(f["bot"] <= price <= f["top"] for f in fvg5 + fvg15): sp.append("FVG")
        if any(o["bot"] <= price <= o["top"] * 1.01 for o in ob5 + ob15): sp.append("OB")
        if cvd5["divergence"] != 0: sp.append("CVD")
        strategy = "+".join(sp) if sp else "SMART_MONEY"

        raw = {
            "price":            price,
            "change":           ticker["change"],
            "oi_15m":           oi["delta_15m"],
            "oi_1h":            oi["delta_1h"],
            "funding":          funding["rate"],
            "liq_ratio":        liqs["ratio"],
            "ob_imb":           ob_data["imbalance"],
            "cvd_div":          cvd5["divergence"],
            "buying_pressure":  cvd5["buying_pressure"],
            "bull_sweep":       sw5["bull_sweep"],
            "bear_sweep":       sw5["bear_sweep"],
            "fvg_count":        len(fvg5 + fvg15),
            "ob_count":         len(ob5  + ob15),
            "structure":        struct["phase"],
            "phase":            phase,
            "vol_ratio":        vr,
            "ls_ratio":         ls.get("ratio", 1.0),
            "conf_long":        conf_long,
            "conf_short":       conf_short,
        }

        prev          = cache.get(sym, {})
        prev_decision = prev.get("decision", "NO TRADE")
        cooldown_ok   = (time.time() - _last_signal.get(sym, 0)) > SIGNAL_COOLDOWN

        sig = Signal(sym, direction, confidence, strategy,
                     price, tp1, tp2, tp3, sl, rr, move_pct, lev, reasons, raw)
        cache[sym] = sig.to_dict()

        log.info("[%s] %s conf=%d%% RR=%.1f move=%.1f%% %s",
                 sym, direction, confidence, rr, move_pct, strategy)

        if (prev_decision != direction or cooldown_ok) and cooldown_ok:
            _last_signal[sym] = time.time()
            await _tg_signal(sig)

        return cache[sym]

    except Exception as e:
        log.error("analyze %s: %s", sym, e)
        return None


async def notify(text: str):
    if not TG_TOKEN or not TG_CHAT:
        return
    try:
        # Encode as ASCII - replace any non-ASCII with '?'
        safe = text.encode("ascii", errors="replace").decode("ascii")
        async with httpx.AsyncClient(timeout=6) as c:
            await c.post(
                f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                json={
                    "chat_id":                  TG_CHAT,
                    "text":                     safe,
                    "parse_mode":               "Markdown",
                    "disable_web_page_preview": True,
                },
            )
    except Exception as e:
        log.warning("TG notify: %s", e)


async def _tg_signal(sig: Signal):
    e1 = abs(sig.tp1 - sig.entry) / max(sig.entry, 1) * 100
    e2 = abs(sig.tp2 - sig.entry) / max(sig.entry, 1) * 100
    e3 = abs(sig.tp3 - sig.entry) / max(sig.entry, 1) * 100
    direction_label = "LONG" if sig.decision == "LONG" else "SHORT"
    text = (
        f"*{direction_label}* `{sig.symbol}`\n\n"
        f"Entry: `${sig.entry:,.6f}`\n"
        f"TP1: `${sig.tp1:,.6f}` (+{e1:.1f}%)\n"
        f"TP2: `${sig.tp2:,.6f}` (+{e2:.1f}%)\n"
        f"TP3: `${sig.tp3:,.6f}` (+{e3:.1f}%)\n"
        f"SL: `${sig.sl:,.6f}`\n"
        f"RR: `1:{sig.rr:.1f}` | Lev: `{sig.lev}x`\n"
        f"Confidence: `{sig.confidence}%`\n"
        f"Strategy: `{sig.strategy}`\n\n"
        + "\n".join(f"- {r}" for r in sig.reasons[:5])
    )
    await notify(text)


class ExchangeClient:
    def __init__(self, exchange: str = "", api_key: str = "",
                 api_secret: str = "", testnet: bool = False):
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
            cfg = {
                "apiKey":          self.api_key,
                "secret":          self.api_secret,
                "enableRateLimit": True,
                "options":         {"defaultType": "future"},
            }
            ex_map = {
                "bybit":   ccxt.bybit,
                "binance": ccxt.binanceusdm,
                "mexc":    ccxt.mexc,
            }
            if self.exchange not in ex_map:
                self.error = f"Unsupported: {self.exchange}"
                return
            self._ex = ex_map[self.exchange](cfg)
            if self.testnet:
                try:
                    self._ex.set_sandbox_mode(True)
                except Exception:
                    pass
            self.connected = True
            log.info("%s connected testnet=%s", self.exchange.upper(), self.testnet)
        except Exception as e:
            self.error = str(e)
            log.error("Exchange %s: %s", self.exchange, e)

    async def test_connection(self):
        if not self._ex:
            return False, self.error
        try:
            bal = await asyncio.to_thread(self._ex.fetch_balance)
            return True, float(bal.get("USDT", {}).get("free", 0))
        except Exception as e:
            return False, str(e)

    async def get_balance(self) -> float:
        if not self._ex:
            return 0.0
        try:
            bal = await asyncio.to_thread(self._ex.fetch_balance)
            return float(bal.get("USDT", {}).get("free", 0))
        except Exception:
            return 0.0

    async def place_order(self, symbol: str, side: str, usdt: float,
                          leverage: int, tp: float, sl: float) -> dict:
        if not self._ex:
            return {"error": "Not connected"}
        try:
            sym_fmt = symbol.replace("USDT", "/USDT:USDT")
            try:
                await asyncio.to_thread(self._ex.set_leverage, leverage, sym_fmt)
            except Exception:
                pass
            ticker = await asyncio.to_thread(self._ex.fetch_ticker, sym_fmt)
            qty    = round(usdt / ticker["last"], 6)
            order  = await asyncio.to_thread(
                self._ex.create_market_order, sym_fmt, side.lower(), qty
            )
            cs = "sell" if side.upper() == "BUY" else "buy"
            try:
                await asyncio.to_thread(
                    self._ex.create_order, sym_fmt,
                    "take_profit_market", cs, qty, tp,
                    {"stopPrice": tp, "closePosition": True, "reduceOnly": True},
                )
                await asyncio.to_thread(
                    self._ex.create_order, sym_fmt,
                    "stop_market", cs, qty, sl,
                    {"stopPrice": sl, "closePosition": True, "reduceOnly": True},
                )
            except Exception as e:
                log.warning("TP/SL not set: %s", e)
            return {
                "id": order["id"], "status": "filled",
                "symbol": symbol, "side": side,
                "amount": usdt, "price": ticker["last"], "qty": qty,
            }
        except Exception as e:
            return {"error": str(e)}


@dataclass
class OpenTrade:
    id:       str
    symbol:   str
    side:     str
    entry:    float
    tp:       float
    sl:       float
    amount:   float
    leverage: int
    exchange: str
    opened:   float = field(default_factory=time.time)
    pnl:      float = 0.0
    status:   str   = "open"


class Agent:
    def __init__(self):
        self.running   = False
        self.exchange: Optional[ExchangeClient] = None
        self.risk_pct  = 1.5
        self.min_conf  = MIN_CONFIDENCE
        self.max_open  = 3
        self.trades:   list[OpenTrade] = []
        self.closed:   list[OpenTrade] = []
        self.total_pnl = 0.0
        self.scans     = 0

    def configure(self, exchange, risk_pct=1.5, min_conf=70, max_open=3):
        self.exchange  = exchange
        self.risk_pct  = risk_pct
        self.min_conf  = min_conf
        self.max_open  = max_open

    async def start(self):
        self.running = True
        await notify(
            f"AXIFLOW Agent started\n"
            f"Exchange: {self.exchange.exchange.upper()}\n"
            f"Smart Money: Sweeps FVG OB CVD\n"
            f"Min confidence: {self.min_conf}%\n"
            f"Risk: {self.risk_pct}% per trade\n"
            f"Cooldown: 15 min per symbol"
        )
        while self.running:
            try:
                await self._scan()
                await self._monitor()
                self.scans += 1
            except Exception as e:
                log.error("Agent loop: %s", e)
            await asyncio.sleep(30)

    def stop(self):
        self.running = False

    async def _scan(self):
        if not self.exchange or not self.exchange.connected:
            return
        open_syms = {t.symbol for t in self.trades if t.status == "open"}
        if len(open_syms) >= self.max_open:
            return
        for sym in SYMBOLS:
            if sym in open_syms:
                continue
            sig = cache.get(sym)
            if not sig or sig.get("decision") == "NO TRADE":
                continue
            if sig.get("confidence", 0) < self.min_conf:
                continue
            if sig.get("rr", 0) < 1.5:
                continue
            if time.time() - _last_signal.get(sym, 0) > SIGNAL_COOLDOWN / 2:
                await self._open(sig)
            if len({t.symbol for t in self.trades if t.status == "open"}) >= self.max_open:
                break

    async def _open(self, sig: dict):
        bal      = await self.exchange.get_balance()
        pos_size = bal * (self.risk_pct / 100) * sig["lev"]
        if pos_size < 5:
            return
        side  = "BUY" if sig["decision"] == "LONG" else "SELL"
        order = await self.exchange.place_order(
            sig["symbol"], side, pos_size, sig["lev"],
            sig["tp"], sig["sl"],
        )
        if "error" in order:
            await notify(f"Order error {sig['symbol']}: {order['error']}")
            return
        t = OpenTrade(
            id=order["id"], symbol=sig["symbol"], side=side,
            entry=sig["entry"], tp=sig["tp"], sl=sig["sl"],
            amount=pos_size, leverage=sig["lev"],
            exchange=self.exchange.exchange,
        )
        self.trades.append(t)
        await notify(
            f"{'LONG' if side == 'BUY' else 'SHORT'} opened {sig['symbol']}\n"
            f"Entry: ${sig['entry']:,.4f}\n"
            f"TP: ${sig['tp']:,.4f}\n"
            f"SL: ${sig['sl']:,.4f}\n"
            f"RR: 1:{sig['rr']:.1f} | {sig['lev']}x | ${pos_size:.0f}\n"
            f"Conf: {sig['confidence']}% | {sig['strategy']}"
        )

    async def _monitor(self):
        for t in [x for x in self.trades if x.status == "open"]:
            sig   = cache.get(t.symbol, {})
            price = sig.get("raw", {}).get("price", t.entry)
            if not price:
                continue
            if t.side == "BUY":
                pnl_pct = (price - t.entry) / max(t.entry, 1) * 100 * t.leverage
                hit_tp  = price >= t.tp
                hit_sl  = price <= t.sl
            else:
                pnl_pct = (t.entry - price) / max(t.entry, 1) * 100 * t.leverage
                hit_tp  = price <= t.tp
                hit_sl  = price >= t.sl
            t.pnl = t.amount * pnl_pct / 100
            if hit_tp:
                t.status = "tp"
                self.total_pnl += t.pnl
                self.closed.append(t)
                await notify(
                    f"TP Hit {t.symbol} +${t.pnl:.2f} (+{pnl_pct:.1f}%)\n"
                    f"Total: ${self.total_pnl:.2f}"
                )
            elif hit_sl:
                t.status = "sl"
                self.total_pnl += t.pnl
                self.closed.append(t)
                await notify(
                    f"SL {t.symbol} ${t.pnl:.2f} ({pnl_pct:.1f}%)\n"
                    f"Total: ${self.total_pnl:.2f}"
                )

    def stats(self) -> dict:
        ot   = [t for t in self.trades if t.status == "open"]
        wins = [t for t in self.closed if t.pnl > 0]
        return {
            "running":       self.running,
            "scans":         self.scans,
            "open_count":    len(ot),
            "closed_count":  len(self.closed),
            "win_rate":      round(len(wins) / max(len(self.closed), 1) * 100, 1),
            "total_pnl":     round(self.total_pnl, 2),
            "exchange":      self.exchange.exchange if self.exchange else "",
            "connected":     self.exchange.connected if self.exchange else False,
            "open_trades": [
                {
                    "id":       t.id,
                    "symbol":   t.symbol,
                    "side":     t.side,
                    "entry":    t.entry,
                    "tp":       t.tp,
                    "sl":       t.sl,
                    "pnl":      round(t.pnl, 2),
                    "lev":      t.leverage,
                    "amount":   round(t.amount, 2),
                    "exchange": t.exchange,
                }
                for t in ot
            ],
            "closed_trades": [
                {
                    "symbol": t.symbol,
                    "side":   t.side,
                    "pnl":    round(t.pnl, 2),
                    "status": t.status,
                }
                for t in self.closed[-10:]
            ],
        }


agent = Agent()


async def scan_loop():
    log.info("AXIFLOW v4 Smart Money scanner started")
    while True:
        try:
            for sym in SYMBOLS:
                await analyze_symbol(sym)
                await asyncio.sleep(2)
        except Exception as e:
            log.error("Scan loop: %s", e)
        await asyncio.sleep(20)


@asynccontextmanager
async def lifespan(app: FastAPI):
    asyncio.create_task(scan_loop())
    yield


app = FastAPI(title="AXIFLOW v4", lifespan=lifespan)
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
    return {"status": "ok", "service": "AXIFLOW v4"}


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
        get_ticker(sym), get_oi(sym),
        get_funding(sym), get_liqs(sym), get_ob(sym),
    )
    return {
        "symbol": sym, "ticker": ticker, "oi": oi,
        "funding": fr, "liquidations": lq, "orderbook": ob,
        "ts": time.time(),
    }


@app.get("/api/klines/{symbol}")
async def klines(symbol: str, interval: str = "5m", limit: int = 100):
    sym  = symbol.upper()
    data = await get_klines(sym, interval, limit)
    return {"symbol": sym, "interval": interval, "candles": data}


class WalletReq(BaseModel):
    user_id:    str
    exchange:   str  = ""
    api_key:    str  = ""
    api_secret: str  = ""
    testnet:    bool = False


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
        "exchange": req.exchange,
        "balance":  float(result),
        "testnet":  req.testnet,
        "connected": True,
        "client":   ex,
    }
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
        if agent.running:
            return {"success": False, "msg": "Already running"}
        if not ex:
            return {"success": False, "msg": "Connect API keys first"}
        agent.configure(ex, req.risk_pct, req.min_conf, req.max_open)
        asyncio.create_task(agent.start())
        return {"success": True, "msg": "Agent started"}
    if req.action == "stop":
        agent.stop()
        return {"success": True, "msg": "Agent stopped"}
    if req.action == "status":
        return agent.stats()
    return {"success": False}


@app.post("/api/trade")
async def manual_trade(req: TradeReq):
    w  = wallets.get(req.user_id)
    ex = w["client"] if w and "client" in w else None
    if not ex:
        return {"success": False, "error": "Connect API keys"}
    sig   = cache.get(req.symbol.upper(), {})
    order = await ex.place_order(
        req.symbol.upper(), req.side, req.amount,
        req.leverage, sig.get("tp", 0), sig.get("sl", 0),
    )
    if "error" in order:
        return {"success": False, "error": order["error"]}
    manual_trades.append({**order, "ts": time.time()})
    return {"success": True, "trade": order}


@app.get("/api/trades/{user_id}")
async def get_trades(user_id: str):
    return {"trades": manual_trades[-30:]}
