#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
 APEX-QUANT-OMNI SOVEREIGN v75.2 (ULTIMATE FORENSIC EDITION)
 Institutional-Grade XAUUSD Hybrid Quantitative Signal Engine
================================================================================
"""
from __future__ import annotations

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
matplotlib.rcParams["figure.autolayout"] = True
matplotlib.rcParams["axes.grid"] = True
matplotlib.rcParams["grid.alpha"] = 0.25

import argparse
import io
import json
import logging
import math
import os
import re
import sqlite3
import sys
import time
import traceback
import warnings
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html import unescape
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
from urllib.parse import quote

import numpy as np
import pandas as pd
import requests
import yfinance as yf

# Mute noisy warnings from yfinance/pandas to keep GH Actions logs clean
warnings.filterwarnings("ignore")

# ==============================================================================
# 1. CONFIGURATION
# ==============================================================================
VERSION = "75.2"
SYSTEM_NAME = "APEX-QUANT-OMNI SOVEREIGN"

DB_PATH = os.environ.get("DB_PATH", "trading_signals.db")
CHART_PATH = os.environ.get("CHART_PATH", "backtest_performance.png")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()

ACCOUNT_BALANCE = float(os.environ.get("ACCOUNT_BALANCE", "10000") or 10000)
RISK_PERCENT = float(os.environ.get("RISK_PERCENT", "1.0") or 1.0)
CONTRACT_SIZE = float(os.environ.get("CONTRACT_SIZE", "100") or 100)

POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "240") or 240)
NOTIFY_NEUTRAL = os.environ.get("NOTIFY_NEUTRAL", "0").strip().lower() in ("1", "true", "yes")
FORCE_NOTIFY = os.environ.get("FORCE_NOTIFY", "0").strip().lower() in ("1", "true", "yes")

EPS = 1e-8
HTTP_TIMEOUT = 20
MIN_CONFLUENCE = 30.0

Z_WINDOW_DEFAULT = 50
ATR_PERIOD_DEFAULT = 14
ATR_RISK_MULT = 1.5
TP_R_MULTIPLES = (1.5, 2.5, 4.0)

GRID_ATR_PERIODS = (10, 14, 20)
GRID_Z_THRESHOLDS = (1.5, 2.0, 2.5)

UA_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "application/json,text/plain,*/*",
}

# ==============================================================================
# 2. LOGGING
# ==============================================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)sZ | %(levelname)-8s | %(name)-12s | %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("APEX-OMNI")

# ==============================================================================
# 3. UTILITIES
# ==============================================================================
def utcnow() -> datetime:
    return datetime.now(timezone.utc)

def iso_now() -> str:
    return utcnow().replace(microsecond=0).isoformat().replace("+00:00", "Z")

def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))

def safe_float(value: Any, fallback: float = 0.0) -> float:
    try:
        out = float(value)
        if math.isnan(out) or math.isinf(out):
            return fallback
        return out
    except (TypeError, ValueError):
        return fallback

def split_message(text: str, limit: int = 3800) -> List[str]:
    if len(text) <= limit:
        return [text]
    chunks, current = [], ""
    for line in text.split("\n"):
        candidate = (current + "\n" + line) if current else line
        if len(candidate) > limit:
            if current: chunks.append(current)
            current = line[:limit]
        else:
            current = candidate
    if current: chunks.append(current)
    return chunks

def strip_html(text: str) -> str:
    return unescape(re.sub(r"<[^>]+>", "", text))

# ==============================================================================
# 4. SQLITE PERSISTENCE LAYER
# ==============================================================================
DDL_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS signals (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, symbol TEXT NOT NULL,
        decision TEXT NOT NULL, entry REAL, sl REAL, tp1 REAL, tp2 REAL, tp3 REAL,
        confluence REAL, lots REAL, risk_usd REAL, atr REAL, zscore REAL,
        smc_state TEXT, source TEXT, payload TEXT
    )
    """,
    "CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, level TEXT NOT NULL, event TEXT NOT NULL, metrics TEXT)",
    "CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT)",
    """
    CREATE TABLE IF NOT EXISTS open_positions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, signal_id INTEGER, direction TEXT,
        entry REAL, sl REAL, tp1 REAL, tp2 REAL, tp3 REAL, atr REAL,
        tp1_hit INTEGER DEFAULT 0, last_ts TEXT, status TEXT DEFAULT 'OPEN', updated_ts TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS optimization_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, atr_period INTEGER, z_threshold REAL,
        trades INTEGER, win_rate REAL, profit_factor REAL, max_drawdown REAL, equity_curve TEXT
    )
    """,
)

def db_connect(path: str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn

def init_db(conn: sqlite3.Connection) -> None:
    for ddl in DDL_STATEMENTS:
        conn.execute(ddl)
    conn.commit()

def set_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT INTO state(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (str(key), str(value)))
    conn.commit()

def get_state(conn: sqlite3.Connection, key: str, default: Optional[str] = None) -> Optional[str]:
    row = conn.execute("SELECT value FROM state WHERE key = ?", (str(key),)).fetchone()
    return row["value"] if row else default

def forensic_audit_log(conn: sqlite3.Connection, event: str, metrics: Optional[Dict[str, Any]] = None, level: str = "INFO") -> None:
    metrics = metrics or {}
    payload = json.dumps(metrics, default=str, ensure_ascii=False)
    banner = "=" * 78
    log.log(getattr(logging, level.upper(), logging.INFO), "\n%s\n[FORENSIC] %s :: %s\n%s\n%s", banner, iso_now(), event, payload, banner)
    try:
        conn.execute("INSERT INTO audit(ts, level, event, metrics) VALUES(?, ?, ?, ?)", (iso_now(), level.upper(), str(event), payload))
        conn.commit()
    except sqlite3.Error as exc:
        log.error("Audit persistence failure: %s", exc)

def persist_signal(conn: sqlite3.Connection, sig: Dict[str, Any]) -> int:
    cur = conn.execute("""
        INSERT INTO signals (ts, symbol, decision, entry, sl, tp1, tp2, tp3, confluence, lots, risk_usd, atr, zscore, smc_state, source, payload)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        sig["timestamp"], sig["symbol"], sig["decision"], float(sig["entry"]), float(sig["sl"]),
        float(sig["tp1"]), float(sig["tp2"]), float(sig["tp3"]), float(sig["confluence"]),
        float(sig["lots"]), float(sig["risk_usd"]), float(sig["atr"]), float(sig["zscore"]),
        str(sig["smc_state"]), str(sig["source"]), json.dumps(sig, default=str, ensure_ascii=False)
    ))
    conn.commit()
    return int(cur.lastrowid)

def persist_optimization(conn: sqlite3.Connection, row: Dict[str, Any]) -> None:
    conn.execute("""
        INSERT INTO optimization_runs (ts, atr_period, z_threshold, trades, win_rate, profit_factor, max_drawdown, equity_curve)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        iso_now(), int(row["atr_period"]), float(row["z_threshold"]),
        int(row["trades"]), float(row["win_rate"]), float(row["profit_factor"]),
        float(row["max_drawdown"]), json.dumps(row.get("curve", [])[:400], default=str)
    ))
    conn.commit()

# ==============================================================================
# 5. HTTP DELIVERY & WEBHOOKS
# ==============================================================================
def http_post_backoff(url: str, *, json_payload: Optional[Dict] = None, data: Optional[Dict] = None, headers: Optional[Dict] = None, max_retries: int = 3, base_delay: float = 2.0, label: str = "webhook") -> Optional[requests.Response]:
    delays = [base_delay * (2 ** i) for i in range(max_retries)]
    attempt = 0
    last_error = None
    while attempt <= max_retries:
        try:
            resp = requests.post(url, json=json_payload, data=data, headers=headers, timeout=HTTP_TIMEOUT)
            if 200 <= resp.status_code < 300:
                log.info("[%s] delivered HTTP %s (attempt %d)", label, resp.status_code, attempt + 1)
                return resp
            if resp.status_code == 429:
                wait = delays[attempt] if attempt < len(delays) else delays[-1]
                try: wait = max(wait, float(resp.headers.get("Retry-After", "")))
                except: pass
                log.warning("[%s] HTTP 429 -> sleeping %.1fs", label, wait)
                time.sleep(wait)
                attempt += 1
                continue
            if 500 <= resp.status_code < 600:
                last_error = RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
            else:
                log.error("[%s] non-retryable HTTP %s: %s", label, resp.status_code, resp.text[:300])
                return resp
        except requests.RequestException as exc:
            last_error = exc
            log.warning("[%s] attempt %d transport failure: %s", label, attempt + 1, exc)
        if attempt < max_retries:
            wait = delays[attempt]
            log.info("[%s] backoff -> sleeping %.1fs", label, wait)
            time.sleep(wait)
        attempt += 1
    log.error("[%s] retries exhausted. Last: %s", label, last_error)
    return None

def tg_api(method: str) -> str:
    return f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"

def send_telegram_html(text: str, chat_id: Optional[str] = None) -> bool:
    if not TELEGRAM_BOT_TOKEN: return False
    target = chat_id or TELEGRAM_CHAT_ID
    if not target: return False
    ok = True
    for chunk in split_message(text, 3800):
        resp = http_post_backoff(tg_api("sendMessage"), json_payload={"chat_id": target, "text": chunk, "parse_mode": "HTML", "disable_web_page_preview": True}, label="telegram")
        if resp is None or resp.status_code >= 300: ok = False
    return ok

def send_discord(text: str) -> bool:
    if not DISCORD_WEBHOOK_URL: return False
    plain = strip_html(text)
    ok = True
    for chunk in split_message(plain, 1900):
        resp = http_post_backoff(DISCORD_WEBHOOK_URL, json_payload={"content": chunk, "username": f"{SYSTEM_NAME} v{VERSION}"}, label="discord")
        if resp is None or resp.status_code >= 300: ok = False
    return ok

def send_emergency_alert(error_text: str) -> None:
    body = f"🚨 <b>{SYSTEM_NAME} v{VERSION} — CRITICAL FAILURE</b> 🚨\n━━━━━━━━━━━━━━━━━━━━━━\n🕒 <b>UTC:</b> {iso_now()}\n🧨 <b>Traceback:</b>\n<pre>{error_text[-1200:]}</pre>"
    try: send_telegram_html(body)
    except: pass
    try: send_discord(body)
    except: pass

# ==============================================================================
# 6. DATA PIPELINE
# ==============================================================================
_last_yahoo_throttle: float = 0.0

def _yahoo_soft_backoff() -> None:
    global _last_yahoo_throttle
    if _last_yahoo_throttle > 0:
        elapsed = time.time() - _last_yahoo_throttle
        if elapsed < 4.0: time.sleep(4.0 - elapsed)

def _finalize_ohlcv(df: Optional[pd.DataFrame], min_rows: int = 60) -> Optional[pd.DataFrame]:
    if df is None or not isinstance(df, pd.DataFrame) or df.empty: return None
    df = df.copy()
    if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.get_level_values(0)
    df.columns = [str(c).strip().lower().replace(" ", "_").replace("-", "_") for c in df.columns]
    
    for variant in ("adj_close", "adjclose", "adjusted_close"):
        if variant in df.columns:
            if "close" in df.columns: df = df.drop(columns=[variant])
            else: df = df.rename(columns={variant: "close"})
                
    if "price" in df.columns and "close" not in df.columns: df = df.rename(columns={"price": "close"})
    if df.columns.duplicated().any(): df = df.loc[:, ~df.columns.duplicated(keep="first")]
    for col in ("open", "high", "low", "close"):
        if col not in df.columns: return None
    if "volume" not in df.columns: df["volume"] = 0.0
    df = df[["open", "high", "low", "close", "volume"]].copy()
    df = df.apply(pd.to_numeric, errors="coerce").dropna(subset=["open", "high", "low", "close"])
    if df.empty: return None
    if not isinstance(df.index, pd.DatetimeIndex):
        try: df.index = pd.to_datetime(df.index, utc=True)
        except: return None
    if df.index.tz is None: df.index = df.index.tz_localize("UTC")
    else: df.index = df.index.tz_convert("UTC")
    df = df[~df.index.duplicated(keep="last")].sort_index()
    df = df[df["high"] >= df["low"]]
    if not df.columns.is_unique or len(df) < min_rows: return None
    return df

def source_yfinance(symbol: str, interval: str, period: str) -> Optional[pd.DataFrame]:
    _yahoo_soft_backoff()
    try:
        raw = yf.download(symbol, interval=interval, period=period, progress=False, auto_adjust=True, threads=False)
    except Exception as exc:
        if "404" in str(exc) or "delisted" in str(exc).lower(): raise RuntimeError(f"symbol {symbol} delisted")
        raise
    return _finalize_ohlcv(raw)

def source_yahoo_chart_api(symbol: str, interval: str, range_: str) -> Optional[pd.DataFrame]:
    global _last_yahoo_throttle
    _yahoo_soft_backoff()
    last_error = None
    for host in ("https://query1.finance.yahoo.com", "https://query2.finance.yahoo.com"):
        try:
            url = f"{host}/v8/finance/chart/{quote(symbol, safe='')}?interval={interval}&range={range_}&includePrePost=false"
            resp = requests.get(url, headers=UA_HEADERS, timeout=HTTP_TIMEOUT)
            if resp.status_code == 429:
                _last_yahoo_throttle = time.time()
                raise RuntimeError("HTTP 429")
            resp.raise_for_status()
            result = resp.json()["chart"]["result"][0]
            ts = result.get("timestamp") or []
            q = result["indicators"]["quote"][0]
            frame = pd.DataFrame({"open": q.get("open"), "high": q.get("high"), "low": q.get("low"), "close": q.get("close"), "volume": q.get("volume") or [0]*len(ts)}, index=pd.to_datetime(ts, unit="s", utc=True))
            cleaned = _finalize_ohlcv(frame)
            if cleaned is not None: return cleaned
            raise RuntimeError("insufficient rows")
        except Exception as exc:
            if "429" in str(exc): _last_yahoo_throttle = time.time()
            last_error = exc
            log.warning("Yahoo host %s failed: %s", host, exc)
    if last_error: raise last_error
    return None

def source_stooq_daily(symbol: str = "xauusd") -> Optional[pd.DataFrame]:
    resp = requests.get(f"https://stooq.com/q/d/l/?s={symbol}&i=d", headers=UA_HEADERS, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    text = (resp.text or "").lstrip("\ufeff").strip()
    if not text or "Date" not in text[:400]: raise RuntimeError("stooq invalid payload")
    raw = pd.read_csv(io.StringIO(text), sep=None, engine="python")
    raw.columns = [str(c).strip().lower() for c in raw.columns]
    if "date" not in raw.columns or "close" not in raw.columns: raise RuntimeError("stooq schema mismatch")
    raw["date"] = pd.to_datetime(raw["date"], utc=True, errors="coerce")
    return _finalize_ohlcv(raw.dropna(subset=["date"]).set_index("date"), min_rows=60)

DATA_SOURCES = (
    ("yfinance-GC=F-15m", lambda: source_yfinance("GC=F", "15m", "5d"), False),
    ("yfinance-PAXG-USD-15m", lambda: source_yfinance("PAXG-USD", "15m", "5d"), False),
    ("yfinance-GLD-15m", lambda: source_yfinance("GLD", "15m", "5d"), False),
    ("yahoo-chart-GC=F-15m", lambda: source_yahoo_chart_api("GC=F", "15m", "5d"), False),
    ("yahoo-chart-PAXG-USD-15m", lambda: source_yahoo_chart_api("PAXG-USD", "15m", "5d"), False),
    ("yfinance-GC=F-60m", lambda: source_yfinance("GC=F", "60m", "1mo"), False),
    ("stooq-XAUUSD-1d", lambda: source_stooq_daily("xauusd"), True),
)

def fetch_market_data() -> Tuple[pd.DataFrame, str, bool]:
    errors = []
    for name, loader, degraded in DATA_SOURCES:
        started = time.time()
        try:
            df = loader()
            if df is None or len(df) < 60: raise RuntimeError("insufficient rows")
            log.info("Feed OK -> %s | rows=%d | latency=%.2fs", name, len(df), time.time() - started)
            return df, name, degraded
        except Exception as exc:
            errors.append(f"{name}: {type(exc).__name__}")
            log.warning("Feed FAILED -> %s :: %s", name, exc)
    raise RuntimeError("ALL DATA SOURCES EXHAUSTED :: " + " || ".join(errors))

# ==============================================================================
# 7. SESSION GOVERNOR & INDICATORS
# ==============================================================================
def session_governor(moment: Optional[datetime] = None) -> Tuple[bool, List[str]]:
    moment = moment or utcnow()
    weekday, hour = moment.weekday(), moment.hour + moment.minute / 60.0
    reasons = []
    if weekday == 4 and hour >= 20.5: reasons.append("Friday close")
    if weekday == 5: reasons.append("Saturday closed")
    if weekday == 6 and hour < 22.0: reasons.append("Sunday pre-open")
    if 21.0 <= hour < 22.0: reasons.append("Daily rollover")
    if 0.0 <= hour < 1.0: reasons.append("Asian open thin-liquidity")
    return (len(reasons) == 0, reasons)

def atr_series(df: pd.DataFrame, period: int = ATR_PERIOD_DEFAULT) -> pd.Series:
    high, low, close = df["high"].astype(float), df["low"].astype(float), df["close"].astype(float)
    if not (isinstance(high, pd.Series) and isinstance(low, pd.Series) and isinstance(close, pd.Series)):
        raise ValueError("OHLC columns must be 1-D Series")
    tr = pd.concat([(high - low), (high - close.shift(1)).abs(), (low - close.shift(1)).abs()], axis=1).max(axis=1)
    atr = tr.rolling(period, min_periods=period).mean()
    mean_val = atr.mean()
    if pd.isna(mean_val) or math.isinf(mean_val): mean_val = 0.0
    return atr.fillna(mean_val).fillna(0.0) + EPS

def zscore_series(close: pd.Series, window: int = Z_WINDOW_DEFAULT) -> pd.Series:
    if not isinstance(close, pd.Series): raise ValueError("close must be Series")
    mean = close.rolling(window, min_periods=window).mean()
    std = close.rolling(window, min_periods=window).std(ddof=0)
    return ((close - mean) / (std + EPS)).clip(-10.0, 10.0).fillna(0.0)

def ema_series(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()

def resample_htf(df: pd.DataFrame, rule: str = "1h") -> pd.DataFrame:
    ohlc = pd.DataFrame({
        "open": df["open"].resample(rule).first(), "high": df["high"].resample(rule).max(),
        "low": df["low"].resample(rule).min(), "close": df["close"].resample(rule).last(),
        "volume": df["volume"].resample(rule).sum()
    })
    return ohlc.dropna(subset=["open", "high", "low", "close"])

# ==============================================================================
# 8. 5-PILLAR ENGINE
# ==============================================================================
@dataclass
class PillarResult:
    name: str
    score: float
    bias: str = "NEUTRAL"
    detail: Dict[str, Any] = field(default_factory=dict)
    def __post_init__(self):
        if self.score > 5.0: self.bias = "BULLISH"
        elif self.score < -5.0: self.bias = "BEARISH"

def _assert_series(df: pd.DataFrame, *cols: str) -> None:
    for col in cols:
        if col not in df.columns or not isinstance(df[col], pd.Series):
            raise ValueError(f"Column '{col}' missing or not a Series.")

def pillar_smc(df: pd.DataFrame, lookback: int = 3) -> PillarResult:
    _assert_series(df, "high", "low", "close")
    highs, lows, closes = df["high"].to_numpy(dtype=float), df["low"].to_numpy(dtype=float), df["close"].to_numpy(dtype=float)
    n = len(closes)
    if n < (2 * lookback + 5): return PillarResult("SMC", 0.0, detail={"error": "insufficient bars"})
    sh, sl = [], []
    for i in range(lookback, n - lookback):
        if highs[i] >= highs[i-lookback:i+lookback+1].max(): sh.append((i, float(highs[i])))
        if lows[i] <= lows[i-lookback:i+lookback+1].min(): sl.append((i, float(lows[i])))
    if not sh or not sl: return PillarResult("SMC", 0.0, detail={"error": "no swings"})
    last_sh, last_sl = sh[-1][1], sl[-1][1]
    prev_sh = sh[-2][1] if len(sh) > 1 else last_sh
    prev_sl = sl[-2][1] if len(sl) > 1 else last_sl
    atr_v = safe_float(atr_series(df).iloc[-1], EPS)
    price = float(closes[-1])
    score, detail = 0.0, {"structure": "RANGE", "event": "NONE"}
    if price > last_sh:
        score = 55.0 + 45.0 * clamp((price - last_sh) / (atr_v + EPS), 0, 1)
        detail.update({"structure": "BULLISH", "event": "BOS_BULLISH" if last_sh >= prev_sh else "ChOCh_BULLISH"})
    elif price < last_sl:
        score = -(55.0 + 45.0 * clamp((last_sl - price) / (atr_v + EPS), 0, 1))
        detail.update({"structure": "BEARISH", "event": "BOS_BEARISH" if last_sl <= prev_sl else "ChOCh_BEARISH"})
    else:
        pos = clamp((price - last_sl) / max(last_sh - last_sl, EPS), 0, 1)
        score = (pos - 0.5) * 60.0
        detail.update({"event": "RANGE_ROTATION", "range_position": round(pos, 4)})
    return PillarResult("SMC", clamp(score, -100, 100), detail=detail)

def pillar_order_flow(df: pd.DataFrame, window: int = 20) -> PillarResult:
    _assert_series(df, "high", "low", "open", "close", "volume")
    tail = df.tail(window)
    if len(tail) < 5: return PillarResult("ORDER_FLOW", 0.0)
    high, low, o, c = tail["high"].to_numpy(float), tail["low"].to_numpy(float), tail["open"].to_numpy(float), tail["close"].to_numpy(float)
    vol = tail["volume"].to_numpy(float)
    if vol.sum() <= 0: vol = np.ones_like(vol)
    bar_range = (high - low) + EPS
    body_ratio = np.clip(np.abs(c - o) / bar_range, 0, 1)
    pressure = (np.sign(c - o) * body_ratio * vol).sum() / (vol.sum() + EPS) * 60.0
    rejection = (((np.minimum(c, o) - low) - (high - np.maximum(c, o))) / bar_range * vol).sum() / (vol.sum() + EPS) * 40.0
    return PillarResult("ORDER_FLOW", clamp(pressure + rejection, -100, 100))

def pillar_zscore(df: pd.DataFrame, window: int = Z_WINDOW_DEFAULT) -> PillarResult:
    if len(df) < window + 5: return PillarResult("ZSCORE", 0.0)
    z_last = safe_float(zscore_series(df["close"].astype(float), window).iloc[-1], 0.0)
    return PillarResult("ZSCORE", clamp(-z_last * 35.0, -100, 100), detail={"zscore": round(z_last, 4)})

def pillar_mtf(df: pd.DataFrame, fast: int = 20, slow: int = 50) -> PillarResult:
    htf = resample_htf(df, "1h")
    if len(htf) < slow + 2: return PillarResult("MTF", 0.0)
    f_ema, s_ema = safe_float(ema_series(htf["close"], fast).iloc[-1]), safe_float(ema_series(htf["close"], slow).iloc[-1])
    htf_c = safe_float(htf["close"].iloc[-1])
    spread = (f_ema - s_ema) / (safe_float(atr_series(htf).iloc[-1], EPS) + EPS)
    m_bull, m_bear = f_ema > s_ema and htf_c > s_ema, f_ema < s_ema and htf_c < s_ema
    l_bull = safe_float(ema_series(df["close"], fast).iloc[-1]) > safe_float(ema_series(df["close"], slow).iloc[-1])
    l_bear = not l_bull
    score = clamp(spread * 40.0, -70, 70)
    if m_bull and l_bull: score += 30
    elif m_bear and l_bear: score -= 30
    return PillarResult("MTF", clamp(score, -100, 100))

def pillar_atr_regime(df: pd.DataFrame) -> PillarResult:
    atr_full = atr_series(df)
    atr_last = safe_float(atr_full.iloc[-1], EPS)
    perc = float((atr_full.tail(120).dropna() <= atr_last).mean()) if len(atr_full) >= 20 else 0.5
    q = 0.60 if perc < 0.15 else (0.70 if perc > 0.95 else 1.00)
    return PillarResult("ATR_REGIME", 0.0, detail={"quality_multiplier": q})

def compute_confluence(pillars: Sequence[PillarResult]) -> Tuple[float, float, float]:
    weights = {"SMC": 0.30, "ORDER_FLOW": 0.20, "ZSCORE": 0.30, "MTF": 0.20}
    signed = sum(p.score * weights[p.name] for p in pillars if p.name in weights)
    quality = next((p.detail.get("quality_multiplier", 1.0) for p in pillars if p.name == "ATR_REGIME"), 1.0)
    return clamp(signed, -100, 100), quality, clamp(abs(signed) * quality, 0, 100)

# ==============================================================================
# 9. RISK & POSITION SIZING
# ==============================================================================
def build_risk_levels(entry: float, direction: str, atr: float) -> Dict[str, float]:
    dist = max(atr * ATR_RISK_MULT, EPS)
    sl = entry - dist if direction == "BUY" else entry + dist
    sign = 1 if direction == "BUY" else -1
    tps = [round(entry + sign * dist * r, 2) for r in TP_R_MULTIPLES]
    return {"sl": round(sl, 2), "tp1": tps[0], "tp2": tps[1], "tp3": tps[2], "stop_distance": dist}

def position_size(balance: float, risk_pct: float, stop_dist: float) -> Tuple[float, float]:
    risk_amt = max(balance, 0) * max(risk_pct, 0) / 100.0
    lots = max(0.01, min(100.0, math.floor((risk_amt / max(stop_dist * CONTRACT_SIZE, EPS)) * 100) / 100))
    return round(lots, 2), round(lots * stop_dist * CONTRACT_SIZE, 2)

def analyze_market(df: pd.DataFrame, source: str, degraded: bool) -> Dict[str, Any]:
    if df is None or len(df) < 30 or not df.columns.is_unique:
        raise RuntimeError("Invalid market data")
    z_win = int(clamp(len(df) // 6, 20, Z_WINDOW_DEFAULT))
    pillars = [pillar_smc(df), pillar_order_flow(df), pillar_zscore(df, z_win), pillar_mtf(df), pillar_atr_regime(df)]
    signed, quality, conf = compute_confluence(pillars)
    decision = "NEUTRAL" if conf < MIN_CONFLUENCE else ("BUY" if signed > 0 else "SELL")
    entry = safe_float(df["close"].iloc[-1])
    atr = safe_float(atr_series(df).iloc[-1], EPS)
    levels = build_risk_levels(entry, decision if decision != "NEUTRAL" else "BUY", atr)
    lots, risk_usd = position_size(ACCOUNT_BALANCE, RISK_PERCENT, levels["stop_distance"])
    return {
        "timestamp": iso_now(), "symbol": "XAUUSD", "source": source, "degraded_feed": degraded,
        "entry": round(entry, 2), "decision": decision, "signed_confluence": round(signed, 2),
        "confluence": round(conf, 2), "atr": round(atr, 5), "sl": levels["sl"], "tp1": levels["tp1"],
        "tp2": levels["tp2"], "tp3": levels["tp3"], "stop_distance": round(levels["stop_distance"], 5),
        "lots": lots, "risk_usd": risk_usd, "risk_percent": RISK_PERCENT,
        "zscore": round(safe_float(zscore_series(df["close"], z_win).iloc[-1]), 4), "z_window": z_win,
        "smc_state": next(p.detail.get("event", "UNKNOWN") for p in pillars if p.name == "SMC"),
        "pillars": [{"name": p.name, "score": round(p.score, 2), "bias": p.bias, "detail": p.detail} for p in pillars]
    }

# ==============================================================================
# 10. BACKTEST & GRID SEARCH
# ==============================================================================
def backtest_params(df: pd.DataFrame, atr_period: int, z_threshold: float) -> Dict[str, Any]:
    empty = {"atr_period": atr_period, "z_threshold": z_threshold, "trades": 0, "wins": 0, "win_rate": 0.0, "profit_factor": 0.0, "max_drawdown": 0.0, "curve": [], "curve_labels": []}
    if df is None or not df.columns.is_unique or len(df) < max(Z_WINDOW_DEFAULT, atr_period) + 30: return empty
    
    work = pd.DataFrame({c: df[c].astype(float) for c in ("open", "high", "low", "close")})
    work["atr"] = atr_series(work, atr_period)
    work["z"] = zscore_series(work["close"], Z_WINDOW_DEFAULT)
    work = work.dropna()
    if len(work) < 30: return empty
    
    c, h, l, a, z = work["close"].values, work["high"].values, work["low"].values, work["atr"].values, work["z"].values
    risk_usd = ACCOUNT_BALANCE * RISK_PERCENT / 100.0
    equity, peak, max_dd = float(ACCOUNT_BALANCE), float(ACCOUNT_BALANCE), 0.0
    curve, labels, trades, wins, losses, gp, gl, pos = [equity], [work.index[0].isoformat()], 0, 0, 0, 0.0, 0.0, None

    for i in range(len(work)):
        if pos is None:
            if z[i] <= -z_threshold: pos = {"dir": "BUY", "e": c[i], "sl": c[i]-a[i]*1.5, "sd": a[i]*1.5, "tp": [c[i]+a[i]*1.5*r for r in (1.5,2.5,4.0)], "h": [False,False,False], "rem": 1.0, "r": 0.0}
            elif z[i] >= z_threshold: pos = {"dir": "SELL", "e": c[i], "sl": c[i]+a[i]*1.5, "sd": a[i]*1.5, "tp": [c[i]-a[i]*1.5*r for r in (1.5,2.5,4.0)], "h": [False,False,False], "rem": 1.0, "r": 0.0}
            continue
        stop_hit = False
        for idx, frac in ((0, 0.5), (1, 0.3), (2, 0.2)):
            if not pos["h"][idx] and ((pos["dir"]=="BUY" and h[i]>=pos["tp"][idx]) or (pos["dir"]=="SELL" and l[i]<=pos["tp"][idx])):
                pos["h"][idx] = True
                pos["r"] += frac * (abs(pos["tp"][idx]-pos["e"]) / pos["sd"])
                pos["rem"] -= frac
                if idx == 0: pos["sl"] = pos["e"]
        if (pos["dir"]=="BUY" and l[i]<=pos["sl"]) or (pos["dir"]=="SELL" and h[i]>=pos["sl"]):
            pos["r"] += pos["rem"] * ((pos["sl"]-pos["e"])/pos["sd"] if pos["dir"]=="BUY" else (pos["e"]-pos["sl"])/pos["sd"])
            pos["rem"] = 0.0
        if pos["rem"] <= 1e-9:
            pnl = risk_usd * pos["r"]
            equity += pnl
            peak = max(peak, equity)
            max_dd = max(max_dd, (peak-equity)/peak*100 if peak > 0 else 0)
            curve.append(round(equity,2)); labels.append(work.index[i].isoformat())
            trades += 1
            if pnl >= 0: wins += 1; gp += pnl
            else: losses += 1; gl += abs(pnl)
            pos = None

    return {"atr_period": atr_period, "z_threshold": z_threshold, "trades": trades, "wins": wins, "win_rate": round(wins/trades*100,2) if trades else 0.0, "profit_factor": round(gp/gl,4) if gl>0 else (99.0 if gp>0 else 0.0), "max_drawdown": round(max_dd,2), "curve": curve, "curve_labels": labels}

def grid_search_optimize(df: pd.DataFrame, conn: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
    results = []
    for a in GRID_ATR_PERIODS:
        for z in GRID_Z_THRESHOLDS:
            try:
                res = backtest_params(df, a, z)
                results.append(res)
                if conn: persist_optimization(conn, res)
            except Exception as exc:
                log.warning("Grid ATR=%s Z=%s failed: %s", a, z, exc)
    if not results: return {"atr_period": 14, "z_threshold": 2.0, "profit_factor": 0.0, "win_rate": 0.0, "max_drawdown": 0.0, "trades": 0, "curve": [], "curve_labels": [], "grid": []}
    results.sort(key=lambda x: (x["profit_factor"], x["trades"]), reverse=True)
    best = dict(results[0])
    best["grid"] = [{"atr_period": r["atr_period"], "z_threshold": r["z_threshold"], "trades": r["trades"], "profit_factor": r["profit_factor"], "win_rate": r["win_rate"], "max_drawdown": r["max_drawdown"]} for r in results]
    return best

# ==============================================================================
# 11. TRAILING STOP ENGINE
# ==============================================================================
class TrailingStopEngine:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def register(self, signal_id: int, sig: Dict[str, Any]) -> None:
        try:
            self.conn.execute("""
                INSERT INTO open_positions (signal_id, direction, entry, sl, tp1, tp2, tp3, atr, tp1_hit, last_ts, status, updated_ts)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, 'OPEN', ?)
            """, (signal_id, sig["decision"], sig["entry"], sig["sl"], sig["tp1"], sig["tp2"], sig["tp3"], sig["atr"], iso_now(), iso_now()))
            self.conn.commit()
        except sqlite3.Error as exc: log.error("Trailing registration failed: %s", exc)

    def refresh(self, df: pd.DataFrame) -> List[Dict[str, Any]]:
        try: rows = self.conn.execute("SELECT * FROM open_positions WHERE status = 'OPEN'").fetchall()
        except sqlite3.Error: return []
        updates = []
        for row in rows:
            r = dict(row)
            d, e, sl, tp1, tp2, tp3, atr, hit = r["direction"], r["entry"], r["sl"], r["tp1"], r["tp2"], r["tp3"], max(r["atr"], EPS), bool(r["tp1_hit"])
            try:
                last_ts = pd.Timestamp(r["last_ts"]).tz_convert("UTC") if r["last_ts"] else None
                if last_ts and last_ts.tzinfo is None: last_ts = last_ts.tz_localize("UTC")
            except: last_ts = None
            bars = df[df.index > last_ts] if last_ts is not None else df.tail(1)
            status, last_bar = "OPEN", last_ts
            for ts, b in bars.iterrows():
                hi, lo, c, last_bar = b["high"], b["low"], b["close"], ts
                if d == "BUY":
                    if lo <= sl: status = "STOPPED"; break
                    if not hit and hi >= tp1: hit = True; sl = max(sl, e)
                    if hi >= tp2 and hit: sl = max(sl, tp1)
                    if hi >= tp3 and hit: sl = max(sl, tp2)
                    if hit: sl = max(sl, c - 0.75 * atr)
                else:
                    if hi >= sl: status = "STOPPED"; break
                    if not hit and lo <= tp1: hit = True; sl = min(sl, e)
                    if lo <= tp2 and hit: sl = min(sl, tp1)
                    if lo <= tp3 and hit: sl = min(sl, tp2)
                    if hit: sl = min(sl, c + 0.75 * atr)
            self.conn.execute("UPDATE open_positions SET sl=?, tp1_hit=?, last_ts=?, status=?, updated_ts=? WHERE id=?", (sl, int(hit), last_bar.isoformat() if last_bar else iso_now(), status, iso_now(), r["id"]))
            updates.append({"id": r["id"], "direction": d, "sl": round(sl,2), "status": status, "trail_mode": "BREAKEVEN_LOCKED" if hit and abs(sl-e)<EPS else ("ATR_TRAILING" if hit else "FIXED_ATR_STOP")})
        self.conn.commit()
        return updates

# ==============================================================================
# 12. CHART GENERATION
# ==============================================================================
def plot_backtest(df: pd.DataFrame, opt: Dict[str, Any], path: str = CHART_PATH) -> bool:
    fig = None
    try:
        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(13, 8.5))
        curve, labels = list(opt.get("curve", [])), list(opt.get("curve_labels", []))
        if len(curve) >= 2:
            x = pd.to_datetime(labels, utc=True, errors="coerce") if len(labels) == len(curve) else np.arange(len(curve))
            ax1.plot(x, curve, color="#0a7d32", lw=1.8); ax1.fill_between(x, curve, min(curve), color="#0a7d32", alpha=0.12)
            ax1.set_title(f"Equity — ATR({opt.get('atr_period')}) / Z({opt.get('z_threshold')}) | PF: {opt.get('profit_factor')} | WR: {opt.get('win_rate')}%", fontweight="bold")
        else: ax1.text(0.5, 0.5, "No trades", ha="center", va="center", transform=ax1.transAxes)
        tail = df.tail(320)
        ax2.plot(tail.index, tail["close"], color="#1f3b73", lw=1.4, label="Close")
        ax2.plot(tail.index, ema_series(tail["close"], 20), color="#d97706", lw=1.1, label="EMA 20")
        ax2.plot(tail.index, ema_series(tail["close"], 50), color="#b91c1c", lw=1.1, label="EMA 50")
        ax2.set_title("Price Structure", fontweight="bold"); ax2.legend(loc="upper left", fontsize=8)
        fig.tight_layout(); fig.savefig(path, dpi=120, bbox_inches="tight")
        return True
    except Exception as exc:
        log.exception("Chart failed: %s", exc); return False
    finally:
        if fig: plt.close(fig)

# ==============================================================================
# 13. NOTIFICATIONS & COMMANDS
# ==============================================================================
def build_alert(a: Dict[str, Any], opt: Dict[str, Any], sess_ok: bool, sess_reasons: list, trail: list) -> str:
    d = a["decision"]
    lbl = "VERIFIED BUY" if d == "BUY" else ("VERIFIED SELL" if d == "SELL" else "NEUTRAL / STAND ASIDE")
    tm = next((t.get("trail_mode", "FIXED_ATR_STOP") for t in trail if t.get("status") == "OPEN"), "Armed @ TP1")
    sl = "OPEN" if sess_ok else "SUPPRESSED — " + "; ".join(sess_reasons)
    return (
        f"🛡️ <b>{SYSTEM_NAME} v{VERSION}</b> 🛡️\n━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🌟 <b>Asset:</b> XAUUSD (SPOT GOLD)\n📊 <b>Decision:</b> <b>{lbl}</b>\n"
        f"🎯 <b>Entry:</b> {a['entry']}\n🛑 <b>SL:</b> {a['sl']}\n"
        f"📈 <b>TP1 (1.5R):</b> {a['tp1']}\n📈 <b>TP2 (2.5R):</b> {a['tp2']}\n🚀 <b>TP3 (4.0R):</b> {a['tp3']}\n"
        f"🔄 <b>Trail:</b> {tm}\n━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⚙️ <b>Grid Search:</b> ATR({opt.get('atr_period')}) / Z({opt.get('z_threshold')})\n"
        f"💰 <b>Sizing:</b> {a['lots']:.2f} Lots | Risk: ${a['risk_usd']:.2f}\n"
        f"🔬 <b>Confluence:</b> {a['confluence']:.1f}/100 | SMC: {a['smc_state']}\n"
        f"🗓️ <b>Session:</b> {sl}\n📡 <b>Feed:</b> {a['source']}\n🕒 <b>UTC:</b> {a['timestamp']}"
    )

def build_check_reply(a: Dict[str, Any], sess_ok: bool, reasons: list, opt: Dict[str, Any]) -> str:
    d = a["decision"]
    lbl = "VERIFIED BUY" if d == "BUY" else ("VERIFIED SELL" if d == "SELL" else "NEUTRAL")
    p_lines = "\n".join(f"  • <i>{p['name']}:</i> {p['score']:+.1f} ({p['bias']})" for p in a["pillars"])
    return (
        f"🔎 <b>LIVE /CHECK</b>\n━━━━━━━━━━━━━━━━━━━━━━\n🌟 <b>Asset:</b> XAUUSD\n"
        f"📊 <b>Decision:</b> <b>{lbl}</b>\n🎯 <b>Entry:</b> {a['entry']}\n"
        f"🛑 <b>SL:</b> {a['sl']} | <b>TP1:</b> {a['tp1']} | <b>TP2:</b> {a['tp2']}\n"
        f"🔬 <b>Confluence:</b> {a['confluence']:.1f}/100 | SMC: {a['smc_state']}\n"
        f"🧩 <b>Pillars:</b>\n{p_lines}\n"
        f"⚙️ <b>Optimal:</b> ATR({opt.get('atr_period')}) / Z({opt.get('z_threshold')}) | PF: {opt.get('profit_factor')}\n"
        f"🗓️ <b>Session:</b> {'OPEN' if sess_ok else 'SUPPRESSED'}\n📡 <b>Feed:</b> {a['source']}"
    )

def tg_get_updates(offset: int, timeout: int = 10) -> list:
    if not TELEGRAM_BOT_TOKEN: return []
    try:
        r = requests.get(tg_api("getUpdates"), params={"offset": offset, "timeout": timeout, "allowed_updates": json.dumps(["message"])}, timeout=HTTP_TIMEOUT+timeout)
        return r.json().get("result", []) if r.json().get("ok") else []
    except: return []

def handle_update(conn: sqlite3.Connection, update: dict) -> None:
    msg = update.get("message") or {}
    txt = (msg.get("text") or "").strip()
    chat_id = str(msg.get("chat", {}).get("id", ""))
    if not txt or not chat_id: return
    if TELEGRAM_CHAT_ID and chat_id != str(TELEGRAM_CHAT_ID): return
    
    cmd = txt.split()[0].split("@")[0].lower()
    if cmd in ("/start", "/help"): send_telegram_html(f"🛡️ <b>{SYSTEM_NAME} v{VERSION}</b>\n/check — Instant status\n/status — System health\n/signal — Latest signal", chat_id)
    elif cmd == "/status":
        try: cnt = conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
        except: cnt = 0
        send_telegram_html(f"🩺 <b>STATUS</b>\nDB Signals: {cnt}\nSession: {'OPEN' if session_governor()[0] else 'SUPPRESSED'}\nUTC: {iso_now()}", chat_id)
    elif cmd == "/signal":
        row = conn.execute("SELECT * FROM signals ORDER BY id DESC LIMIT 1").fetchone()
        send_telegram_html(f"📜 <b>LATEST</b>\n{row['ts']} | {row['decision']} @ {row['entry']}" if row else "📭 Empty", chat_id)
    elif cmd == "/check":
        send_telegram_html("⏳ <i>Running analysis...</i>", chat_id)
        try:
            df, src, deg = fetch_market_data()
            a = analyze_market(df, src, deg)
            opt = grid_search_optimize(df, None)
            send_telegram_html(build_check_reply(a, session_governor()[0], session_governor()[1], opt), chat_id)
        except Exception as e: send_telegram_html(f"❌ Error: {e}", chat_id)

def run_listener(conn: sqlite3.Connection, budget: int = POLL_SECONDS) -> int:
    if not TELEGRAM_BOT_TOKEN: return 0
    deadline = time.time() + max(budget, 0)
    offset = int(get_state(conn, "tg_offset", "0") or 0)
    processed = 0
    while time.time() < deadline:
        updates = tg_get_updates(offset)
        if not updates: time.sleep(1.0); continue
        for u in updates:
            if "update_id" in u: offset = u["update_id"] + 1; set_state(conn, "tg_offset", str(offset))
            try: handle_update(conn, u); processed += 1
            except: pass
    return processed

# ==============================================================================
# 14. CORE EXECUTION
# ==============================================================================
def run_cycle(conn: sqlite3.Connection, notify: bool = True, listen_budget: int = POLL_SECONDS) -> Dict[str, Any]:
    cycle = {"status": "OK"}
    df, src, deg = fetch_market_data()
    opt = grid_search_optimize(df, conn)
    analysis = analyze_market(df, src, deg)
    sess_ok, sess_rsn = session_governor()
    
    trail_eng = TrailingStopEngine(conn)
    try: trail_up = trail_eng.refresh(df)
    except: trail_up = []

    sig_id = None
    if analysis["decision"] in ("BUY", "SELL"):
        try:
            sig_id = persist_signal(conn, analysis)
            trail_eng.register(sig_id, analysis)
        except: pass

    chart_ok = plot_backtest(df, opt, CHART_PATH)
    cycle.update({"decision": analysis["decision"], "confluence": analysis["confluence"], "signal_id": sig_id, "session_ok": sess_ok, "chart": chart_ok})

    if notify and (analysis["decision"] in ("BUY", "SELL") or NOTIFY_NEUTRAL) and (sess_ok or FORCE_NOTIFY):
        alert = build_alert(analysis, opt, sess_ok, sess_rsn, trail_up)
        send_telegram_html(alert); send_discord(alert)
        forensic_audit_log(conn, "WEBHOOK_DISPATCH", {"decision": analysis["decision"]})
    
    if listen_budget > 0: run_listener(conn, listen_budget)
    forensic_audit_log(conn, "CYCLE_COMPLETE", cycle)
    return cycle

def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["once", "check", "listen"], default="once")
    parser.add_argument("--listen-seconds", type=int, default=POLL_SECONDS)
    parser.add_argument("--no-notify", action="store_true")
    args = parser.parse_args(argv)

    conn = db_connect(); init_db(conn)
    try:
        if args.mode == "listen": run_listener(conn, args.listen_seconds); return 0
        if args.mode == "check":
            df, src, deg = fetch_market_data()
            print(build_check_reply(analyze_market(df, src, deg), session_governor()[0], session_governor()[1], grid_search_optimize(df)))
            return 0
        run_cycle(conn, not args.no_notify, args.listen_seconds); return 0
    except Exception as exc:
        tb = traceback.format_exc(); log.error("FATAL: %s", exc); send_emergency_alert(tb); return 1
    finally: conn.close()

if __name__ == "__main__":
    sys.exit(main())
