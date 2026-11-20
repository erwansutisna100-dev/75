#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
 APEX-QUANT-OMNI — HYPERION APEX EDITION (v76.0)
 Self-Correcting High-Precision Dual-Mode (SCALP & SWING) XAUUSD Signal Engine
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
from datetime import datetime, timedelta, timezone
from html import unescape
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
from urllib.parse import quote

import numpy as np
import pandas as pd
import requests
import yfinance as yf

warnings.filterwarnings("ignore")
logging.getLogger("yfinance").setLevel(logging.CRITICAL)

# ==============================================================================
# 1. CONFIGURATION & CONSTANTS
# ==============================================================================
VERSION = "76.0-HYPERION"
SYSTEM_NAME = "APEX-QUANT-OMNI"
SYMBOL = "XAUUSD"

DB_PATH = os.environ.get("DB_PATH", "trading_signals.db")
CHART_PATH = os.environ.get("CHART_PATH", "backtest_performance.png")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()

ACCOUNT_BALANCE = float(os.environ.get("ACCOUNT_BALANCE", "10000.0") or 10000.0)
RISK_PERCENT = float(os.environ.get("RISK_PERCENT", "1.0") or 1.0)
CONTRACT_SIZE = float(os.environ.get("CONTRACT_SIZE", "100.0") or 100.0)
PIP_SIZE = float(os.environ.get("PIP_SIZE", "0.1") or 0.1)
LOT_STEP = 0.01
MIN_LOT = 0.01
MAX_LOT = float(os.environ.get("MAX_LOT", "50.0") or 50.0)

POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "180") or 180)
NOTIFY_NO_SIGNAL = os.environ.get("NOTIFY_NO_SIGNAL", "0").strip().lower() in ("1", "true", "yes")
FORCE_NOTIFY = os.environ.get("FORCE_NOTIFY", "0").strip().lower() in ("1", "true", "yes")
ALLOW_ASIAN_SCALP = os.environ.get("ALLOW_ASIAN_SCALP", "0").strip().lower() in ("1", "true", "yes")

EPS = 1e-8
HTTP_TIMEOUT = 20
CIRCUIT_MAX_FAILURES = 3
CIRCUIT_COOLDOWN_MINUTES = 30

GRID_ATR_PERIODS = (10, 14, 20)
GRID_Z_THRESHOLDS = (1.5, 2.0, 2.5)
MIN_BACKTEST_TRADES = 6

UA_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json,text/plain,*/*",
}


@dataclass(frozen=True)
class ModeProfile:
    name: str
    min_confluence: float
    atr_period: int
    atr_risk_mult: float
    tp_r_multiples: Tuple[float, float, float]
    trail_atr_mult: float
    z_window: int
    smc_lookback: int
    of_window: int
    er_period: int
    min_er: float
    max_chop: float
    min_adx: float
    rsi_period: int
    rsi_buy_bounds: Tuple[float, float]
    rsi_sell_bounds: Tuple[float, float]
    cooldown_minutes: int
    max_stale_minutes: int
    htf_rule_mid: str
    htf_rule_macro: str
    require_prime_session: bool


SCALP_PROFILE = ModeProfile(
    name="SCALP",
    min_confluence=float(os.environ.get("MIN_CONFLUENCE_SCALP", "95.0") or 95.0),
    atr_period=14,
    atr_risk_mult=1.25,
    tp_r_multiples=(1.2, 2.0, 3.0),
    trail_atr_mult=0.50,
    z_window=30,
    smc_lookback=3,
    of_window=12,
    er_period=14,
    min_er=0.28,
    max_chop=60.0,
    min_adx=20.0,
    rsi_period=14,
    rsi_buy_bounds=(48.0, 72.0),
    rsi_sell_bounds=(28.0, 52.0),
    cooldown_minutes=45,
    max_stale_minutes=60,
    htf_rule_mid="15min",
    htf_rule_macro="1h",
    require_prime_session=True,
)

SWING_PROFILE = ModeProfile(
    name="SWING",
    min_confluence=float(os.environ.get("MIN_CONFLUENCE_SWING", "95.0") or 95.0),
    atr_period=14,
    atr_risk_mult=1.85,
    tp_r_multiples=(1.5, 2.5, 4.0),
    trail_atr_mult=0.85,
    z_window=50,
    smc_lookback=5,
    of_window=16,
    er_period=20,
    min_er=0.25,
    max_chop=61.8,
    min_adx=22.0,
    rsi_period=14,
    rsi_buy_bounds=(45.0, 74.0),
    rsi_sell_bounds=(26.0, 55.0),
    cooldown_minutes=240,
    max_stale_minutes=240,
    htf_rule_mid="1h",
    htf_rule_macro="4h",
    require_prime_session=False,
)

MODE_PROFILES: Dict[str, ModeProfile] = {
    "SCALP": SCALP_PROFILE,
    "SWING": SWING_PROFILE,
}

# ==============================================================================
# 2. LOGGING
# ==============================================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)sZ | %(levelname)-8s | %(name)-11s | %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("APEX-QUANT")

# ==============================================================================
# 3. UTILITIES
# ==============================================================================
def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utcnow().replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_iso_utc(ts_str: Optional[str]) -> Optional[datetime]:
    if not ts_str:
        return None
    try:
        cleaned = str(ts_str).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(cleaned)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


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
    chunks: List[str] = []
    current = ""
    for line in text.split("\n"):
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            if current:
                chunks.append(current)
            current = line[:limit]
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def strip_html(text: str) -> str:
    return unescape(re.sub(r"<[^>]+>", "", text))


def authorized_chat_ids() -> List[str]:
    if not TELEGRAM_CHAT_ID:
        return []
    return [c.strip() for c in TELEGRAM_CHAT_ID.split(",") if c.strip()]


# ==============================================================================
# 4. SQLITE PERSISTENCE, MIGRATION & CIRCUIT BREAKER
# ==============================================================================
DDL_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS signals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        mode TEXT NOT NULL DEFAULT 'SWING',
        symbol TEXT NOT NULL,
        decision TEXT NOT NULL,
        entry REAL,
        sl REAL,
        tp1 REAL,
        tp2 REAL,
        tp3 REAL,
        confluence REAL,
        lots REAL,
        stop_pips REAL,
        risk_usd REAL,
        atr REAL,
        zscore REAL,
        er REAL,
        adx REAL,
        regime TEXT,
        smc_state TEXT,
        bar_ts TEXT,
        source TEXT,
        payload TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS audit (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        level TEXT NOT NULL,
        event TEXT NOT NULL,
        metrics TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS state (
        key TEXT PRIMARY KEY,
        value TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS open_positions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        signal_id INTEGER,
        mode TEXT NOT NULL DEFAULT 'SWING',
        direction TEXT,
        entry REAL,
        sl REAL,
        tp1 REAL,
        tp2 REAL,
        tp3 REAL,
        atr REAL,
        trail_mult REAL DEFAULT 0.75,
        tp1_hit INTEGER DEFAULT 0,
        last_ts TEXT,
        status TEXT DEFAULT 'OPEN',
        updated_ts TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS optimization_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT,
        mode TEXT DEFAULT 'SWING',
        atr_period INTEGER,
        z_threshold REAL,
        trades INTEGER,
        win_rate REAL,
        profit_factor REAL,
        max_drawdown REAL,
        robust_score REAL,
        equity_curve TEXT
    )
    """,
)

SCHEMA_UPGRADES = (
    ("signals", "mode", "TEXT NOT NULL DEFAULT 'SWING'"),
    ("signals", "stop_pips", "REAL DEFAULT 0.0"),
    ("signals", "er", "REAL DEFAULT 0.0"),
    ("signals", "adx", "REAL DEFAULT 0.0"),
    ("signals", "regime", "TEXT DEFAULT 'UNKNOWN'"),
    ("signals", "bar_ts", "TEXT DEFAULT ''"),
    ("open_positions", "mode", "TEXT NOT NULL DEFAULT 'SWING'"),
    ("open_positions", "trail_mult", "REAL DEFAULT 0.75"),
    ("optimization_runs", "mode", "TEXT DEFAULT 'SWING'"),
    ("optimization_runs", "robust_score", "REAL DEFAULT 0.0"),
)


def db_connect(path: str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, col_def: str) -> None:
    cols = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_def}")


def init_db(conn: sqlite3.Connection) -> None:
    for ddl in DDL_STATEMENTS:
        conn.execute(ddl)
    for table, col, col_def in SCHEMA_UPGRADES:
        _ensure_column(conn, table, col, col_def)
    conn.commit()


def set_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO state(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (str(key), str(value)),
    )
    conn.commit()


def get_state(conn: sqlite3.Connection, key: str, default: Optional[str] = None) -> Optional[str]:
    row = conn.execute("SELECT value FROM state WHERE key = ?", (str(key),)).fetchone()
    return row["value"] if row else default


def forensic_audit_log(
    conn: Optional[sqlite3.Connection],
    event: str,
    metrics: Optional[Dict[str, Any]] = None,
    level: str = "INFO",
) -> None:
    metrics = metrics or {}
    payload = json.dumps(metrics, default=str, ensure_ascii=False)
    banner = "=" * 78
    log.log(
        getattr(logging, level.upper(), logging.INFO),
        "\n%s\n[FORENSIC] %s :: %s\n%s\n%s",
        banner,
        iso_now(),
        event,
        payload,
        banner,
    )
    if conn is not None:
        try:
            conn.execute(
                "INSERT INTO audit(ts, level, event, metrics) VALUES(?, ?, ?, ?)",
                (iso_now(), level.upper(), str(event), payload),
            )
            conn.commit()
        except sqlite3.Error as exc:
            log.error("Audit persistence failure: %s", exc)


def persist_signal(conn: sqlite3.Connection, sig: Dict[str, Any]) -> int:
    cur = conn.execute(
        """
        INSERT INTO signals (
            ts, mode, symbol, decision, entry, sl, tp1, tp2, tp3,
            confluence, lots, stop_pips, risk_usd, atr, zscore, er, adx,
            regime, smc_state, bar_ts, source, payload
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            sig["timestamp"],
            sig["mode"],
            sig["symbol"],
            sig["decision"],
            float(sig["entry"]),
            float(sig["sl"]),
            float(sig["tp1"]),
            float(sig["tp2"]),
            float(sig["tp3"]),
            float(sig["confluence"]),
            float(sig["lots"]),
            float(sig["stop_pips"]),
            float(sig["risk_usd"]),
            float(sig["atr"]),
            float(sig["zscore"]),
            float(sig["er"]),
            float(sig["adx"]),
            str(sig["regime"]),
            str(sig["smc_state"]),
            str(sig["bar_ts"]),
            str(sig["source"]),
            json.dumps(sig, default=str, ensure_ascii=False),
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def persist_optimization(conn: sqlite3.Connection, mode: str, row: Dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO optimization_runs (
            ts, mode, atr_period, z_threshold, trades, win_rate,
            profit_factor, max_drawdown, robust_score, equity_curve
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            iso_now(),
            mode,
            int(row["atr_period"]),
            float(row["z_threshold"]),
            int(row["trades"]),
            float(row["win_rate"]),
            float(row["profit_factor"]),
            float(row["max_drawdown"]),
            float(row.get("robust_score", 0.0)),
            json.dumps(row.get("curve", [])[:400], default=str),
        ),
    )
    conn.commit()


def is_duplicate_or_cooldown(
    conn: sqlite3.Connection,
    sig: Dict[str, Any],
    profile: ModeProfile,
) -> Tuple[bool, str]:
    mode = sig["mode"]
    decision = sig["decision"]
    bar_ts = sig.get("bar_ts", "")
    row = conn.execute(
        "SELECT id, ts, decision, entry, atr, bar_ts FROM signals "
        "WHERE mode = ? ORDER BY id DESC LIMIT 1",
        (mode,),
    ).fetchone()
    if row is None:
        return False, "NO_PRIOR_SIGNAL"

    if bar_ts and row["bar_ts"] == bar_ts and row["decision"] == decision:
        return True, f"SAME_BAR_DEDUP ({bar_ts})"

    last_dt = parse_iso_utc(row["ts"])
    if last_dt is not None:
        elapsed_min = (utcnow() - last_dt).total_seconds() / 60.0
        if elapsed_min < profile.cooldown_minutes and row["decision"] == decision:
            price_shift = abs(float(sig["entry"]) - float(row["entry"] or sig["entry"]))
            min_shift = max(float(sig["atr"]), EPS) * 1.2
            if price_shift < min_shift:
                return True, (
                    f"COOLDOWN_ACTIVE ({elapsed_min:.1f}m < {profile.cooldown_minutes}m, "
                    f"shift={price_shift:.2f} < {min_shift:.2f})"
                )

    open_same = conn.execute(
        "SELECT id FROM open_positions WHERE mode = ? AND direction = ? AND status = 'OPEN' LIMIT 1",
        (mode, decision),
    ).fetchone()
    if open_same is not None:
        return True, f"OPEN_POSITION_ACTIVE (pos_id={open_same['id']})"

    return False, "CLEAR"


def check_circuit_breaker(conn: sqlite3.Connection) -> Tuple[bool, Optional[str]]:
    open_until_raw = get_state(conn, "circuit_open_until")
    open_until = parse_iso_utc(open_until_raw)
    if open_until is not None and utcnow() < open_until:
        return True, open_until.isoformat()
    return False, None


def record_circuit_failure(conn: sqlite3.Connection, reason: str) -> int:
    fails = int(get_state(conn, "circuit_failures", "0") or 0) + 1
    set_state(conn, "circuit_failures", str(fails))
    set_state(conn, "last_error", reason[:500])
    if fails >= CIRCUIT_MAX_FAILURES:
        until = (utcnow() + timedelta(minutes=CIRCUIT_COOLDOWN_MINUTES)).replace(microsecond=0)
        set_state(conn, "circuit_open_until", until.isoformat().replace("+00:00", "Z"))
        forensic_audit_log(
            conn,
            "CIRCUIT_BREAKER_TRIPPED",
            {"failures": fails, "open_until": until.isoformat(), "reason": reason[:300]},
            level="ERROR",
        )
    return fails


def reset_circuit_breaker(conn: sqlite3.Connection) -> None:
    set_state(conn, "circuit_failures", "0")
    set_state(conn, "circuit_open_until", "")


# ==============================================================================
# 5. HTTP DELIVERY & WEBHOOKS (EXPONENTIAL BACKOFF 2s, 4s, 8s)
# ==============================================================================
def http_post_backoff(
    url: str,
    *,
    json_payload: Optional[Dict[str, Any]] = None,
    data: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
    max_retries: int = 3,
    base_delay: float = 2.0,
    label: str = "webhook",
) -> Optional[requests.Response]:
    delays = [base_delay * (2 ** i) for i in range(max_retries)]
    last_error: Optional[Exception] = None
    for attempt in range(max_retries + 1):
        try:
            resp = requests.post(
                url,
                json=json_payload,
                data=data,
                headers=headers,
                timeout=HTTP_TIMEOUT,
            )
            if 200 <= resp.status_code < 300:
                log.info("[%s] HTTP %s delivered (attempt %d)", label, resp.status_code, attempt + 1)
                return resp
            if resp.status_code == 429:
                wait = delays[min(attempt, len(delays) - 1)]
                try:
                    retry_after = float(resp.headers.get("Retry-After", "0") or 0)
                    wait = max(wait, retry_after)
                except ValueError:
                    pass
                log.warning("[%s] HTTP 429 rate-limited -> backoff %.1fs", label, wait)
                if attempt < max_retries:
                    time.sleep(wait)
                    continue
                return resp
            if 500 <= resp.status_code < 600:
                last_error = RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
            else:
                log.error("[%s] non-retryable HTTP %s: %s", label, resp.status_code, resp.text[:300])
                return resp
        except requests.RequestException as exc:
            last_error = exc
            log.warning("[%s] attempt %d transport error: %s", label, attempt + 1, exc)

        if attempt < max_retries:
            wait = delays[attempt]
            log.info("[%s] retrying in %.1fs...", label, wait)
            time.sleep(wait)

    log.error("[%s] retries exhausted. Last error: %s", label, last_error)
    return None


def tg_api(method: str) -> str:
    return f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"


def send_telegram_html(text: str, chat_id: Optional[str] = None) -> bool:
    if not TELEGRAM_BOT_TOKEN:
        return False
    targets = [chat_id] if chat_id else authorized_chat_ids()
    if not targets:
        return False
    ok = True
    for target in targets:
        for chunk in split_message(text, 3800):
            resp = http_post_backoff(
                tg_api("sendMessage"),
                json_payload={
                    "chat_id": target,
                    "text": chunk,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
                label="telegram",
            )
            if resp is None or resp.status_code >= 300:
                ok = False
    return ok


def send_discord(text: str) -> bool:
    if not DISCORD_WEBHOOK_URL:
        return False
    plain = strip_html(text)
    ok = True
    for chunk in split_message(plain, 1900):
        resp = http_post_backoff(
            DISCORD_WEBHOOK_URL,
            json_payload={"content": chunk, "username": f"{SYSTEM_NAME} {VERSION}"},
            label="discord",
        )
        if resp is None or resp.status_code >= 300:
            ok = False
    return ok


def send_emergency_alert(error_text: str) -> None:
    snippet = error_text.strip()[-900:]
    body = (
        f"🚨 <b>{SYSTEM_NAME} | SYSTEM ALERT</b>\n"
        f"🕒 <code>{iso_now()}</code>\n"
        f"<pre>{snippet}</pre>"
    )
    try:
        send_telegram_html(body)
    except Exception:
        pass
    try:
        send_discord(body)
    except Exception:
        pass


# ==============================================================================
# 6. SPOT GOLD MULTI-SOURCE DATA PIPELINE & SANITY VALIDATION
# ==============================================================================
_last_yahoo_throttle: float = 0.0


def _yahoo_soft_backoff() -> None:
    global _last_yahoo_throttle
    if _last_yahoo_throttle > 0:
        elapsed = time.time() - _last_yahoo_throttle
        if elapsed < 3.5:
            time.sleep(3.5 - elapsed)


def _finalize_ohlcv(df: Optional[pd.DataFrame], min_rows: int = 80) -> Optional[pd.DataFrame]:
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return None
    df = df.copy()

    if isinstance(df.columns, pd.MultiIndex):
        lvl0 = [str(c).strip().lower() for c in df.columns.get_level_values(0)]
        lvl1 = [str(c).strip().lower() for c in df.columns.get_level_values(1)]
        if any(k in lvl0 for k in ("open", "high", "low", "close")):
            df.columns = df.columns.get_level_values(0)
        elif any(k in lvl1 for k in ("open", "high", "low", "close")):
            df.columns = df.columns.get_level_values(1)
        else:
            df.columns = df.columns.get_level_values(0)

    df.columns = [str(c).strip().lower().replace(" ", "_").replace("-", "_") for c in df.columns]

    for variant in ("adj_close", "adjclose", "adjusted_close"):
        if variant in df.columns:
            if "close" in df.columns:
                df = df.drop(columns=[variant])
            else:
                df = df.rename(columns={variant: "close"})

    if "price" in df.columns and "close" not in df.columns:
        df = df.rename(columns={"price": "close"})

    if df.columns.duplicated().any():
        df = df.loc[:, ~df.columns.duplicated(keep="first")]

    for col in ("open", "high", "low", "close"):
        if col not in df.columns:
            return None

    if "volume" not in df.columns:
        df["volume"] = 0.0

    df = df[["open", "high", "low", "close", "volume"]].copy()
    df = df.apply(pd.to_numeric, errors="coerce")
    df["volume"] = df["volume"].fillna(0.0).clip(lower=0.0)
    df = df.dropna(subset=["open", "high", "low", "close"])
    if df.empty:
        return None

    df = df[(df["open"] > 100.0) & (df["high"] > 100.0) & (df["low"] > 100.0) & (df["close"] > 100.0)]
    if df.empty:
        return None

    if not isinstance(df.index, pd.DatetimeIndex):
        try:
            df.index = pd.to_datetime(df.index, utc=True)
        except Exception:
            return None
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    else:
        df.index = df.index.tz_convert("UTC")

    df = df[~df.index.duplicated(keep="last")].sort_index()
    df["high"] = np.maximum(df["high"], np.maximum(df["open"], df["close"]))
    df["low"] = np.minimum(df["low"], np.minimum(df["open"], df["close"]))
    df = df[df["high"] >= df["low"]]

    if not df.columns.is_unique or len(df) < min_rows:
        return None
    return df


def source_yfinance(symbol: str, interval: str, period: str, min_rows: int = 80) -> Optional[pd.DataFrame]:
    _yahoo_soft_backoff()
    raw = yf.download(
        symbol,
        interval=interval,
        period=period,
        progress=False,
        auto_adjust=True,
        threads=False,
    )
    return _finalize_ohlcv(raw, min_rows=min_rows)


def source_yahoo_chart_api(
    symbol: str,
    interval: str,
    range_: str,
    min_rows: int = 80,
) -> Optional[pd.DataFrame]:
    global _last_yahoo_throttle
    _yahoo_soft_backoff()
    last_error: Optional[Exception] = None
    for host in ("https://query1.finance.yahoo.com", "https://query2.finance.yahoo.com"):
        try:
            url = (
                f"{host}/v8/finance/chart/{quote(symbol, safe='')}"
                f"?interval={interval}&range={range_}&includePrePost=false"
            )
            resp = requests.get(url, headers=UA_HEADERS, timeout=HTTP_TIMEOUT)
            if resp.status_code == 429:
                _last_yahoo_throttle = time.time()
                raise RuntimeError("HTTP 429")
            resp.raise_for_status()
            payload = resp.json()
            result = payload["chart"]["result"][0]
            ts = result.get("timestamp") or []
            q = result["indicators"]["quote"][0]
            frame = pd.DataFrame(
                {
                    "open": q.get("open"),
                    "high": q.get("high"),
                    "low": q.get("low"),
                    "close": q.get("close"),
                    "volume": q.get("volume") or [0.0] * len(ts),
                },
                index=pd.to_datetime(ts, unit="s", utc=True),
            )
            cleaned = _finalize_ohlcv(frame, min_rows=min_rows)
            if cleaned is not None:
                return cleaned
            raise RuntimeError("insufficient cleaned rows")
        except Exception as exc:
            if "429" in str(exc):
                _last_yahoo_throttle = time.time()
            last_error = exc
            log.warning("Yahoo chart host %s (%s %s) failed: %s", host, symbol, interval, exc)
    if last_error is not None:
        raise last_error
    return None


def source_stooq_spot(symbol: str = "xauusd", interval: str = "d", min_rows: int = 80) -> Optional[pd.DataFrame]:
    url = f"https://stooq.com/q/d/l/?s={symbol}&i={interval}"
    resp = requests.get(url, headers=UA_HEADERS, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    text = (resp.text or "").lstrip("\ufeff").strip()
    if not text or "Date" not in text[:400]:
        raise RuntimeError("stooq invalid CSV payload")
    raw = pd.read_csv(io.StringIO(text), sep=None, engine="python")
    raw.columns = [str(c).strip().lower() for c in raw.columns]
    if "date" not in raw.columns or "close" not in raw.columns:
        raise RuntimeError("stooq schema mismatch")
    if "time" in raw.columns:
        dt_str = raw["date"].astype(str) + " " + raw["time"].astype(str)
        raw["dt"] = pd.to_datetime(dt_str, utc=True, errors="coerce")
    else:
        raw["dt"] = pd.to_datetime(raw["date"], utc=True, errors="coerce")
    raw = raw.dropna(subset=["dt"]).set_index("dt")
    return _finalize_ohlcv(raw, min_rows=min_rows)


def source_kraken_ohlc(pair: str = "PAXGUSD", interval: int = 5, min_rows: int = 80) -> Optional[pd.DataFrame]:
    url = f"https://api.kraken.com/0/public/OHLC?pair={quote(pair, safe='')}&interval={interval}"
    resp = requests.get(url, headers=UA_HEADERS, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("error"):
        raise RuntimeError(f"kraken error: {payload['error']}")
    result = payload.get("result") or {}
    key = next((k for k in result.keys() if k != "last"), None)
    if not key:
        raise RuntimeError("kraken empty result")
    rows = result[key]
    frame = pd.DataFrame(
        rows,
        columns=["time", "open", "high", "low", "close", "vwap", "volume", "count"],
    )
    frame["dt"] = pd.to_datetime(pd.to_numeric(frame["time"], errors="coerce"), unit="s", utc=True)
    frame = frame.dropna(subset=["dt"]).set_index("dt")
    return _finalize_ohlcv(frame[["open", "high", "low", "close", "volume"]], min_rows=min_rows)


def fetch_live_spot_xauusd() -> Optional[Tuple[float, float, str]]:
    """Fetch live Spot XAU/USD mid price and spread from public institutional feeds."""
    try:
        resp = requests.get(
            "https://forex-data-feed.swissquote.com/public-quotes/bboquotes/instrument/XAU/USD",
            headers=UA_HEADERS,
            timeout=8,
        )
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list) and data:
            profiles = data[0].get("spreadProfilePrices") or []
            if profiles:
                q = profiles[0]
                bid = safe_float(q.get("bid"), 0.0)
                ask = safe_float(q.get("ask"), 0.0)
                if bid > 100.0 and ask >= bid:
                    return round((bid + ask) * 0.5, 2), round(ask - bid, 3), "swissquote-XAUUSD"
    except Exception:
        pass

    try:
        resp = requests.get(
            "https://data-asg.goldprice.org/dbXRates/USD",
            headers=UA_HEADERS,
            timeout=8,
        )
        resp.raise_for_status()
        items = resp.json().get("items") or []
        if items:
            px = safe_float(items[0].get("xauPrice"), 0.0)
            if px > 100.0:
                return round(px, 2), 0.30, "goldprice-XAUUSD"
    except Exception:
        pass
    return None


def _align_to_spot_basis(df: pd.DataFrame, source_name: str) -> Tuple[pd.DataFrame, str, Optional[float]]:
    if "stooq-XAUUSD" in source_name or not session_governor("SWING")[0]:
        return df, source_name, None
    spot_info = fetch_live_spot_xauusd()
    if spot_info is None:
        return df, source_name, None
    spot_px, live_spread, spot_src = spot_info
    last_close = safe_float(df["close"].iloc[-1], 0.0)
    if last_close <= 100.0:
        return df, source_name, live_spread
    basis = spot_px - last_close
    if abs(basis) / last_close <= 0.02:
        aligned = df.copy()
        for col in ("open", "high", "low", "close"):
            aligned[col] = aligned[col] + basis
        log.info(
            "Spot Basis Calibrated: %s -> %s (spot=%.2f, basis=%+.2f, spread=%.2f)",
            source_name,
            spot_src,
            spot_px,
            basis,
            live_spread,
        )
        return aligned, f"{source_name}+{spot_src}", live_spread
    return df, source_name, live_spread


# Multi-source Spot Gold failover (Kraken Spot + Yahoo PAXG-USD / XAUT-USD + Stooq Spot; NO GC=F futures)
SCALP_SOURCES: Tuple[Tuple[str, Callable[[], Optional[pd.DataFrame]], bool], ...] = (
    ("kraken-PAXGUSD-5m", lambda: source_kraken_ohlc("PAXGUSD", 5, 100), False),
    ("yfinance-PAXG-USD-5m", lambda: source_yfinance("PAXG-USD", "5m", "5d", 100), False),
    ("yahoo-chart-PAXG-USD-5m", lambda: source_yahoo_chart_api("PAXG-USD", "5m", "5d", 100), False),
    ("kraken-PAXGUSD-15m", lambda: source_kraken_ohlc("PAXGUSD", 15, 100), False),
    ("yfinance-XAUT-USD-5m", lambda: source_yfinance("XAUT-USD", "5m", "5d", 100), True),
    ("yfinance-PAXG-USD-15m", lambda: source_yfinance("PAXG-USD", "15m", "1mo", 100), True),
)

SWING_SOURCES: Tuple[Tuple[str, Callable[[], Optional[pd.DataFrame]], bool], ...] = (
    ("kraken-PAXGUSD-1h", lambda: source_kraken_ohlc("PAXGUSD", 60, 120), False),
    ("yfinance-PAXG-USD-1h", lambda: source_yfinance("PAXG-USD", "1h", "3mo", 120), False),
    ("yahoo-chart-PAXG-USD-1h", lambda: source_yahoo_chart_api("PAXG-USD", "1h", "3mo", 120), False),
    ("yfinance-XAUT-USD-1h", lambda: source_yfinance("XAUT-USD", "1h", "3mo", 120), True),
    ("stooq-XAUUSD-1d", lambda: source_stooq_spot("xauusd", "d", 100), True),
)


def fetch_market_data(mode: str = "SWING") -> Tuple[pd.DataFrame, str, bool]:
    sources = SCALP_SOURCES if mode.upper() == "SCALP" else SWING_SOURCES
    errors: List[str] = []
    for name, loader, degraded in sources:
        started = time.time()
        try:
            df = loader()
            if df is None or len(df) < 80:
                raise RuntimeError("insufficient rows")
            df, final_name, _ = _align_to_spot_basis(df, name)
            log.info(
                "Feed OK [%s] -> %s | rows=%d | last=%s | latency=%.2fs",
                mode,
                final_name,
                len(df),
                df.index[-1].isoformat(),
                time.time() - started,
            )
            return df, final_name, degraded
        except Exception as exc:
            errors.append(f"{name}:{type(exc).__name__}")
            log.warning("Feed FAIL [%s] -> %s :: %s", mode, name, exc)
    raise RuntimeError(f"ALL {mode} DATA SOURCES EXHAUSTED :: " + " | ".join(errors))


def validate_market_health(
    df: pd.DataFrame,
    profile: ModeProfile,
    session_open: bool,
) -> Tuple[bool, List[str], Dict[str, Any]]:
    reasons: List[str] = []
    last_ts = df.index[-1].to_pydatetime()
    age_min = (utcnow() - last_ts).total_seconds() / 60.0

    is_daily_feed = (df.index.to_series().diff().dropna().median().total_seconds() >= 43200)
    stale_limit = 2880 if is_daily_feed else profile.max_stale_minutes
    if session_open and age_min > stale_limit:
        reasons.append(f"STALE_FEED ({age_min:.1f}m > {stale_limit}m)")
    if df["close"].tail(6).nunique() <= 1:
        reasons.append("FLATLINE_PRICE_FEED")

    closes = df["close"].astype(float)
    log_ret = np.log(closes / closes.shift(1)).dropna()
    if len(log_ret) >= 20:
        med_ret = float(log_ret.median())
        mad = float((log_ret - med_ret).abs().median())
        robust_sigma = max(1.4826 * mad, 1e-5)
        last_ret = abs(float(log_ret.iloc[-1]))
        if last_ret > max(7.0 * robust_sigma, 0.03):
            reasons.append(f"ANOMALY_SPIKE (ret={last_ret:.4f}, 7sig={7.0*robust_sigma:.4f})")

    pct_range = ((df["high"] - df["low"]) / (df["close"] + EPS)).astype(float)
    med_pct_range = max(float(pct_range.tail(100).median()), 1e-5)
    last_pct_range = float(pct_range.iloc[-1])
    if last_pct_range > max(6.5 * med_pct_range, 0.035):
        reasons.append(f"CANDLE_RANGE_SPIKE ({last_pct_range:.4f})")

    atr = atr_series(df, profile.atr_period)
    atr_now = safe_float(atr.iloc[-1], EPS)
    atr_base = max(safe_float(atr.tail(60).median(), atr_now), EPS)
    vol_ratio = atr_now / atr_base
    tr_last = float(
        max(
            df["high"].iloc[-1] - df["low"].iloc[-1],
            abs(df["high"].iloc[-1] - df["close"].iloc[-2]),
            abs(df["low"].iloc[-1] - df["close"].iloc[-2]),
        )
    )
    if vol_ratio > 3.0 or tr_last > 3.5 * atr_now:
        reasons.append(f"VOLATILITY_SHOCK (ratio={vol_ratio:.2f}x, TR/ATR={tr_last/atr_now:.2f}x)")

    meta = {
        "age_min": round(age_min, 2),
        "vol_ratio": round(vol_ratio, 3),
        "last_bar_ts": last_ts.isoformat(),
    }
    return (len(reasons) == 0), reasons, meta


# ==============================================================================
# 7. SESSION GOVERNOR & VECTORIZED QUANTITATIVE INDICATORS
# ==============================================================================
def session_governor(
    mode: str = "SWING",
    moment: Optional[datetime] = None,
) -> Tuple[bool, List[str]]:
    moment = moment or utcnow()
    weekday = moment.weekday()
    hour = moment.hour + moment.minute / 60.0
    reasons: List[str] = []

    if weekday == 4 and hour >= 20.5:
        reasons.append("Friday market close")
    elif weekday == 5:
        reasons.append("Saturday market closed")
    elif weekday == 6 and hour < 22.0:
        reasons.append("Sunday pre-open")
    elif 21.0 <= hour < 22.0:
        reasons.append("Daily rollover 21:00-22:00 UTC")
    elif 0.0 <= hour < 1.0:
        reasons.append("Thin liquidity midnight open")

    if mode.upper() == "SCALP" and not ALLOW_ASIAN_SCALP and len(reasons) == 0:
        if 1.0 <= hour < 6.0:
            reasons.append("Scalp pre-London thin session (01:00-06:00 UTC)")

    return (len(reasons) == 0), reasons


def atr_series(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    close = df["close"].astype(float)
    tr = pd.concat(
        [
            high - low,
            (high - close.shift(1)).abs(),
            (low - close.shift(1)).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr = tr.ewm(alpha=1.0 / max(period, 1), adjust=False, min_periods=min(period, len(df))).mean()
    fallback = safe_float(atr.mean(), 1.0)
    return atr.fillna(fallback) + EPS


def zscore_series(close: pd.Series, window: int = 50) -> pd.Series:
    mean = close.rolling(window, min_periods=min(window, len(close))).mean()
    std = close.rolling(window, min_periods=min(window, len(close))).std(ddof=0)
    return ((close - mean) / (std + EPS)).clip(-10.0, 10.0).fillna(0.0)


def ema_series(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def rsi_series(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / (avg_loss + EPS)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return rsi.fillna(50.0).clip(0.0, 100.0)


def efficiency_ratio_series(close: pd.Series, period: int = 14) -> pd.Series:
    net_change = (close - close.shift(period)).abs()
    volatility = close.diff().abs().rolling(period, min_periods=period).sum()
    return (net_change / (volatility + EPS)).clip(0.0, 1.0).fillna(0.0)


def choppiness_index_series(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    close = df["close"].astype(float)
    tr = pd.concat(
        [high - low, (high - close.shift(1)).abs(), (low - close.shift(1)).abs()],
        axis=1,
    ).max(axis=1)
    tr_sum = tr.rolling(period, min_periods=period).sum()
    hh = high.rolling(period, min_periods=period).max()
    ll = low.rolling(period, min_periods=period).min()
    ratio = tr_sum / (hh - ll + EPS)
    chop = 100.0 * np.log10(np.maximum(ratio, 1.0)) / math.log10(max(period, 2))
    return chop.fillna(50.0).clip(0.0, 100.0)


def adx_di_series(df: pd.DataFrame, period: int = 14) -> Tuple[pd.Series, pd.Series, pd.Series]:
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    close = df["close"].astype(float)

    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = pd.Series(np.where((up_move > down_move) & (up_move > 0), up_move, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((down_move > up_move) & (down_move > 0), down_move, 0.0), index=df.index)

    tr = pd.concat(
        [high - low, (high - close.shift(1)).abs(), (low - close.shift(1)).abs()],
        axis=1,
    ).max(axis=1)
    atr = tr.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean() + EPS
    plus_di = 100.0 * (plus_dm.ewm(alpha=1.0 / period, adjust=False).mean() / atr)
    minus_di = 100.0 * (minus_dm.ewm(alpha=1.0 / period, adjust=False).mean() / atr)
    dx = 100.0 * ((plus_di - minus_di).abs() / (plus_di + minus_di + EPS))
    adx = dx.ewm(alpha=1.0 / period, adjust=False).mean().fillna(0.0).clip(0.0, 100.0)
    return adx, plus_di.fillna(0.0), minus_di.fillna(0.0)


def resample_htf(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    if len(df) < 10:
        return df.copy()
    ohlc = pd.DataFrame(
        {
            "open": df["open"].resample(rule).first(),
            "high": df["high"].resample(rule).max(),
            "low": df["low"].resample(rule).min(),
            "close": df["close"].resample(rule).last(),
            "volume": df["volume"].resample(rule).sum(),
        }
    )
    return ohlc.dropna(subset=["open", "high", "low", "close"])


# ==============================================================================
# 8. 5-PILLAR QUANTITATIVE ENGINE + AI AUGMENTATION & >95% STRICT FILTER
# ==============================================================================
@dataclass
class PillarResult:
    name: str
    score: float  # [-100, +100]
    bias: str = "NEUTRAL"
    detail: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.score >= 15.0:
            self.bias = "BULLISH"
        elif self.score <= -15.0:
            self.bias = "BEARISH"
        else:
            self.bias = "NEUTRAL"


def pillar_smc(df: pd.DataFrame, lookback: int = 3) -> PillarResult:
    highs = df["high"].to_numpy(dtype=float)
    lows = df["low"].to_numpy(dtype=float)
    closes = df["close"].to_numpy(dtype=float)
    n = len(closes)
    if n < (2 * lookback + 10):
        return PillarResult("SMC", 0.0, detail={"event": "INSUFFICIENT_BARS"})

    sh: List[Tuple[int, float]] = []
    sl: List[Tuple[int, float]] = []
    for i in range(lookback, n - lookback):
        if highs[i] >= highs[i - lookback : i + lookback + 1].max():
            sh.append((i, float(highs[i])))
        if lows[i] <= lows[i - lookback : i + lookback + 1].min():
            sl.append((i, float(lows[i])))

    if len(sh) < 2 or len(sl) < 2:
        return PillarResult("SMC", 0.0, detail={"event": "NO_SWINGS"})

    last_sh, prev_sh = sh[-1][1], sh[-2][1]
    last_sl, prev_sl = sl[-1][1], sl[-2][1]
    atr_v = max(safe_float(atr_series(df).iloc[-1], 1.0), EPS)
    price = float(closes[-1])
    prev_close = float(closes[-2])

    hh = last_sh > prev_sh
    hl = last_sl > prev_sl
    lh = last_sh < prev_sh
    ll = last_sl < prev_sl

    if price > last_sh and prev_close <= last_sh:
        event = "BOS_BULL" if hl else "CHOCH_BULL"
        score = 96.0 + 4.0 * clamp((price - last_sh) / atr_v, 0.0, 1.0)
    elif price < last_sl and prev_close >= last_sl:
        event = "BOS_BEAR" if lh else "CHOCH_BEAR"
        score = -(96.0 + 4.0 * clamp((last_sl - price) / atr_v, 0.0, 1.0))
    elif price > last_sh:
        event = "BOS_CONT_BULL" if hh or hl else "CHOCH_CONT_BULL"
        score = 95.0 + 5.0 * clamp((price - last_sh) / atr_v, 0.0, 1.0)
    elif price < last_sl:
        event = "BOS_CONT_BEAR" if ll or lh else "CHOCH_CONT_BEAR"
        score = -(95.0 + 5.0 * clamp((last_sl - price) / atr_v, 0.0, 1.0))
    elif hh and hl and price > last_sl:
        pos = clamp((price - last_sl) / max(last_sh - last_sl, EPS), 0.0, 1.0)
        event = "HL_DISCOUNT_BULL"
        score = 95.0 if pos <= 0.65 else 75.0
    elif ll and lh and price < last_sh:
        pos = clamp((last_sh - price) / max(last_sh - last_sl, EPS), 0.0, 1.0)
        event = "LH_PREMIUM_BEAR"
        score = -95.0 if pos <= 0.65 else -75.0
    else:
        pos = clamp((price - last_sl) / max(last_sh - last_sl, EPS), 0.0, 1.0)
        event = "RANGE_CONSOLIDATION"
        score = (pos - 0.5) * 40.0

    return PillarResult(
        "SMC",
        clamp(score, -100.0, 100.0),
        detail={
            "event": event,
            "last_sh": round(last_sh, 2),
            "last_sl": round(last_sl, 2),
            "hh_hl": bool(hh and hl),
            "ll_lh": bool(ll and lh),
        },
    )


def pillar_order_flow(df: pd.DataFrame, window: int = 12) -> PillarResult:
    tail = df.tail(window)
    if len(tail) < 5:
        return PillarResult("ORDER_FLOW", 0.0, detail={"wick_trap": "NONE"})

    high = tail["high"].to_numpy(dtype=float)
    low = tail["low"].to_numpy(dtype=float)
    o = tail["open"].to_numpy(dtype=float)
    c = tail["close"].to_numpy(dtype=float)
    vol = tail["volume"].to_numpy(dtype=float)

    bar_range = (high - low) + EPS
    vol_mode = "REAL_VOLUME"
    if float(vol.sum()) <= 0.0:
        med_rng = max(float(np.median(bar_range)), EPS)
        vol = np.clip(bar_range / med_rng, 0.25, 3.0)
        vol_mode = "SYNTHETIC_RANGE"

    recency = np.linspace(0.6, 1.4, len(tail))
    w = vol * recency
    w_sum = float(w.sum()) + EPS

    body_ratio = np.clip(np.abs(c - o) / bar_range, 0.0, 1.0)
    clv = (2.0 * c - high - low) / bar_range
    dir_sign = np.sign(c - o)

    body_pressure = float((dir_sign * body_ratio * w).sum() / w_sum)
    clv_pressure = float((clv * w).sum() / w_sum)

    last_rng = float(bar_range[-1])
    upper_wick = float((high[-1] - max(o[-1], c[-1])) / last_rng)
    lower_wick = float((min(o[-1], c[-1]) - low[-1]) / last_rng)
    wick_trap = "NONE"
    if upper_wick > 0.45 and c[-1] <= o[-1]:
        wick_trap = "BULL_TRAP_UPPER_WICK"
    elif lower_wick > 0.45 and c[-1] >= o[-1]:
        wick_trap = "BEAR_TRAP_LOWER_WICK"

    raw = 0.60 * body_pressure + 0.40 * clv_pressure
    score = clamp(math.copysign(min(100.0, abs(raw) * 165.0), raw), -100.0, 100.0)
    return PillarResult(
        "ORDER_FLOW",
        score,
        detail={
            "vol_mode": vol_mode,
            "upper_wick": round(upper_wick, 3),
            "lower_wick": round(lower_wick, 3),
            "wick_trap": wick_trap,
        },
    )


def pillar_zscore(df: pd.DataFrame, window: int, trend_dir: str) -> PillarResult:
    if len(df) < window + 5:
        return PillarResult("ZSCORE", 0.0, detail={"zscore": 0.0, "zone": "INSUFFICIENT"})

    zs = zscore_series(df["close"].astype(float), window)
    z_now = safe_float(zs.iloc[-1], 0.0)
    z_prev = safe_float(zs.iloc[-2], z_now)
    z_slope = z_now - z_prev

    score = 0.0
    zone = "NEUTRAL"
    overextended = False

    if trend_dir == "BUY":
        if -2.4 <= z_now <= -0.25:
            score = 96.0 + 4.0 * clamp(abs(z_now) / 2.4, 0.0, 1.0)
            zone = "OPTIMAL_DISCOUNT_PULLBACK"
        elif -0.25 < z_now <= 1.35 and z_slope >= -0.05:
            score = 95.0
            zone = "HEALTHY_CONTINUATION"
        elif z_now > 2.1:
            score = -60.0
            zone = "OVERBOUGHT_CHASE_RISK"
            overextended = True
        elif z_now < -2.6:
            score = -40.0
            zone = "CAPITULATION_FALLING_KNIFE"
            overextended = True
        else:
            score = 65.0
            zone = "TRANSITION"
    elif trend_dir == "SELL":
        if 0.25 <= z_now <= 2.4:
            score = -(96.0 + 4.0 * clamp(abs(z_now) / 2.4, 0.0, 1.0))
            zone = "OPTIMAL_PREMIUM_PULLBACK"
        elif -1.35 <= z_now < 0.25 and z_slope <= 0.05:
            score = -95.0
            zone = "HEALTHY_CONTINUATION"
        elif z_now < -2.1:
            score = 60.0
            zone = "OVERSOLD_CHASE_RISK"
            overextended = True
        elif z_now > 2.6:
            score = 40.0
            zone = "BLOWOFF_TOP_RISK"
            overextended = True
        else:
            score = -65.0
            zone = "TRANSITION"
    else:
        score = clamp(-z_now * 25.0, -50.0, 50.0)
        zone = "NO_TREND_BIAS"

    return PillarResult(
        "ZSCORE",
        clamp(score, -100.0, 100.0),
        detail={
            "zscore": round(z_now, 4),
            "z_slope": round(z_slope, 4),
            "zone": zone,
            "overextended": overextended,
        },
    )


def _tf_trend_bias(frame: pd.DataFrame, fast: int = 20, slow: int = 50) -> int:
    if len(frame) < slow + 3:
        fast_adj = max(5, len(frame) // 4)
        slow_adj = max(12, len(frame) // 2)
        if len(frame) < slow_adj + 2:
            return 0
        f_ema = ema_series(frame["close"], fast_adj)
        s_ema = ema_series(frame["close"], slow_adj)
    else:
        f_ema = ema_series(frame["close"], fast)
        s_ema = ema_series(frame["close"], slow)

    c_now = safe_float(frame["close"].iloc[-1])
    f_now = safe_float(f_ema.iloc[-1])
    s_now = safe_float(s_ema.iloc[-1])
    s_prev = safe_float(s_ema.iloc[-2])

    if f_now > s_now and c_now > s_now and s_now >= s_prev:
        return 1
    if f_now < s_now and c_now < s_now and s_now <= s_prev:
        return -1
    return 0


def pillar_mtf(df: pd.DataFrame, profile: ModeProfile) -> PillarResult:
    mid_df = resample_htf(df, profile.htf_rule_mid)
    macro_df = resample_htf(df, profile.htf_rule_macro)

    b_ltf = _tf_trend_bias(df)
    b_mid = _tf_trend_bias(mid_df) if len(mid_df) >= 25 else b_ltf
    b_macro = _tf_trend_bias(macro_df) if len(macro_df) >= 20 else b_mid

    total = b_ltf + b_mid + b_macro
    if total == 3:
        score = 100.0
        harmony = "FULL_BULL_HARMONY"
    elif total == -3:
        score = -100.0
        harmony = "FULL_BEAR_HARMONY"
    elif total == 2:
        score = 65.0
        harmony = "PARTIAL_BULL"
    elif total == -2:
        score = -65.0
        harmony = "PARTIAL_BEAR"
    else:
        score = float(total * 20.0)
        harmony = "MIXED_CONFLICT"

    return PillarResult(
        "MTF",
        score,
        detail={"ltf": b_ltf, "mid": b_mid, "macro": b_macro, "harmony": harmony},
    )


def pillar_regime_augment(
    df: pd.DataFrame,
    profile: ModeProfile,
    candidate_dir: str,
) -> PillarResult:
    atr_full = atr_series(df, profile.atr_period)
    atr_now = safe_float(atr_full.iloc[-1], 1.0)
    atr_tail = atr_full.tail(120).dropna()
    atr_pct = float((atr_tail <= atr_now).mean()) if len(atr_tail) >= 20 else 0.5

    er = safe_float(efficiency_ratio_series(df["close"], profile.er_period).iloc[-1], 0.0)
    chop = safe_float(choppiness_index_series(df, profile.er_period).iloc[-1], 50.0)
    adx_s, pdi_s, mdi_s = adx_di_series(df, 14)
    adx = safe_float(adx_s.iloc[-1], 0.0)
    pdi = safe_float(pdi_s.iloc[-1], 0.0)
    mdi = safe_float(mdi_s.iloc[-1], 0.0)
    rsi = safe_float(rsi_series(df["close"], profile.rsi_period).iloc[-1], 50.0)

    is_choppy = (er < profile.min_er) or (chop > profile.max_chop) or (adx < profile.min_adx)
    atr_healthy = 0.10 <= atr_pct <= 0.985

    di_aligned = (candidate_dir == "BUY" and pdi > mdi) or (candidate_dir == "SELL" and mdi > pdi)
    if candidate_dir == "BUY":
        rsi_ok = profile.rsi_buy_bounds[0] <= rsi <= profile.rsi_buy_bounds[1]
    elif candidate_dir == "SELL":
        rsi_ok = profile.rsi_sell_bounds[0] <= rsi <= profile.rsi_sell_bounds[1]
    else:
        rsi_ok = False

    sign = 1.0 if candidate_dir == "BUY" else (-1.0 if candidate_dir == "SELL" else 0.0)
    if is_choppy:
        regime_label = "CHOPPY"
        score = 0.0
    elif not atr_healthy:
        regime_label = "EXTREME_VOL"
        score = sign * 35.0
    elif di_aligned and rsi_ok:
        regime_label = "TREND_PRIME"
        score = sign * 100.0
    else:
        regime_label = "WEAK_MOMENTUM"
        score = sign * 55.0

    return PillarResult(
        "REGIME_QUANT",
        score,
        detail={
            "regime": regime_label,
            "atr_pct": round(atr_pct, 3),
            "er": round(er, 3),
            "chop": round(chop, 2),
            "adx": round(adx, 2),
            "plus_di": round(pdi, 2),
            "minus_di": round(mdi, 2),
            "rsi": round(rsi, 2),
            "di_aligned": di_aligned,
            "rsi_ok": rsi_ok,
            "atr_healthy": atr_healthy,
        },
    )


def build_risk_levels(
    entry: float,
    direction: str,
    atr: float,
    profile: ModeProfile,
) -> Dict[str, float]:
    min_sl_dist = max(PIP_SIZE * 5.0, 0.5)
    dist = max(atr * profile.atr_risk_mult, min_sl_dist)
    sign = 1.0 if direction == "BUY" else -1.0
    sl = entry - sign * dist
    tps = [round(entry + sign * dist * r, 2) for r in profile.tp_r_multiples]
    return {
        "sl": round(sl, 2),
        "tp1": tps[0],
        "tp2": tps[1],
        "tp3": tps[2],
        "stop_distance": dist,
        "stop_pips": round(dist / max(PIP_SIZE, EPS), 1),
    }


def position_size(
    balance: float,
    risk_pct: float,
    stop_dist: float,
) -> Tuple[float, float]:
    risk_target = max(balance, 0.0) * max(risk_pct, 0.0) / 100.0
    stop_pips = max(stop_dist / max(PIP_SIZE, EPS), EPS)
    pip_value_per_lot = CONTRACT_SIZE * PIP_SIZE
    risk_per_lot = max(stop_pips * pip_value_per_lot, EPS)

    raw_lots = risk_target / risk_per_lot
    stepped = math.floor(raw_lots / LOT_STEP) * LOT_STEP
    lots = round(clamp(stepped, MIN_LOT, MAX_LOT), 2)
    actual_risk = round(lots * risk_per_lot, 2)
    return lots, actual_risk


def analyze_mode(
    df: pd.DataFrame,
    source: str,
    degraded: bool,
    profile: ModeProfile,
    session_open: bool,
    session_reasons: List[str],
) -> Dict[str, Any]:
    health_ok, health_reasons, health_meta = validate_market_health(df, profile, session_open)

    p_smc = pillar_smc(df, profile.smc_lookback)
    p_of = pillar_order_flow(df, profile.of_window)
    p_mtf = pillar_mtf(df, profile)

    prelim_vote = 0.45 * p_smc.score + 0.35 * p_mtf.score + 0.20 * p_of.score
    candidate_dir = "BUY" if prelim_vote >= 25.0 else ("SELL" if prelim_vote <= -25.0 else "NEUTRAL")

    p_z = pillar_zscore(df, profile.z_window, candidate_dir)
    p_reg = pillar_regime_augment(df, profile, candidate_dir)

    pillars = [p_smc, p_of, p_z, p_mtf, p_reg]
    weights = {
        "SMC": 0.25,
        "ORDER_FLOW": 0.20,
        "ZSCORE": 0.15,
        "MTF": 0.25,
        "REGIME_QUANT": 0.15,
    }

    reject_reasons: List[str] = []
    if not session_open and not FORCE_NOTIFY:
        reject_reasons.extend(session_reasons)
    if not health_ok:
        reject_reasons.extend(health_reasons)

    reg_d = p_reg.detail
    if reg_d["regime"] == "CHOPPY":
        reject_reasons.append(
            f"CHOPPY_REGIME (ER={reg_d['er']}, CHOP={reg_d['chop']}, ADX={reg_d['adx']})"
        )
    if not reg_d["atr_healthy"]:
        reject_reasons.append(f"ATR_OUT_OF_BAND (pct={reg_d['atr_pct']})")

    sign = 1.0 if candidate_dir == "BUY" else (-1.0 if candidate_dir == "SELL" else 0.0)
    if candidate_dir == "NEUTRAL":
        reject_reasons.append("NO_DIRECTIONAL_CONSENSUS")
        confluence = 0.0
    else:
        aligned_score = 0.0
        for p in pillars:
            directional = p.score * sign
            if directional < 0:
                reject_reasons.append(f"PILLAR_CONFLICT ({p.name}={p.score:+.1f})")
            aligned_score += clamp(directional, 0.0, 100.0) * weights[p.name]
        confluence = round(clamp(aligned_score, 0.0, 100.0), 2)

        if p_mtf.detail["harmony"] not in ("FULL_BULL_HARMONY", "FULL_BEAR_HARMONY"):
            reject_reasons.append(f"MTF_DISCORD ({p_mtf.detail['harmony']})")
        if p_z.detail.get("overextended"):
            reject_reasons.append(f"ZSCORE_VETO ({p_z.detail['zone']})")
        if (candidate_dir == "BUY" and p_of.detail.get("wick_trap") == "BULL_TRAP_UPPER_WICK") or (
            candidate_dir == "SELL" and p_of.detail.get("wick_trap") == "BEAR_TRAP_LOWER_WICK"
        ):
            reject_reasons.append(f"WICK_TRAP_VETO ({p_of.detail['wick_trap']})")
        if not reg_d["di_aligned"]:
            reject_reasons.append("DI_SPREAD_MISMATCH")
        if not reg_d["rsi_ok"]:
            reject_reasons.append(f"RSI_GUARD ({reg_d['rsi']})")
        if confluence < profile.min_confluence:
            reject_reasons.append(f"CONFLUENCE {confluence:.1f} < {profile.min_confluence:.1f}")

    decision = candidate_dir if (len(reject_reasons) == 0 and candidate_dir in ("BUY", "SELL")) else "NO_SIGNAL"

    entry = round(safe_float(df["close"].iloc[-1]), 2)
    atr_val = safe_float(atr_series(df, profile.atr_period).iloc[-1], 1.0)
    risk_dir = candidate_dir if candidate_dir in ("BUY", "SELL") else "BUY"
    levels = build_risk_levels(entry, risk_dir, atr_val, profile)
    lots, risk_usd = position_size(ACCOUNT_BALANCE, RISK_PERCENT, levels["stop_distance"])

    return {
        "timestamp": iso_now(),
        "bar_ts": health_meta["last_bar_ts"],
        "mode": profile.name,
        "symbol": SYMBOL,
        "source": source,
        "degraded_feed": degraded,
        "decision": decision,
        "candidate_bias": candidate_dir,
        "entry": entry,
        "sl": levels["sl"],
        "tp1": levels["tp1"],
        "tp2": levels["tp2"],
        "tp3": levels["tp3"],
        "stop_distance": round(levels["stop_distance"], 4),
        "stop_pips": levels["stop_pips"],
        "lots": lots,
        "risk_usd": risk_usd,
        "risk_percent": RISK_PERCENT,
        "confluence": confluence,
        "atr": round(atr_val, 4),
        "zscore": p_z.detail.get("zscore", 0.0),
        "er": reg_d["er"],
        "adx": reg_d["adx"],
        "rsi": reg_d["rsi"],
        "chop": reg_d["chop"],
        "regime": reg_d["regime"],
        "smc_state": p_smc.detail.get("event", "UNKNOWN"),
        "trail_mult": profile.trail_atr_mult,
        "reject_reasons": reject_reasons,
        "health": health_meta,
        "pillars": [
            {"name": p.name, "score": round(p.score, 2), "bias": p.bias, "detail": p.detail}
            for p in pillars
        ],
    }


# ==============================================================================
# 9. REALISTIC SPREAD/SLIPPAGE BACKTEST & ANTI-OVERFITTING GRID SEARCH
# ==============================================================================
def estimate_market_friction(df: pd.DataFrame) -> Tuple[float, float]:
    bar_range = (df["high"] - df["low"]).astype(float).tail(100)
    med_range = safe_float(bar_range.median(), 2.0)
    spread = clamp(med_range * 0.08, 0.25, 0.85)
    atr_med = safe_float(atr_series(df, 14).tail(100).median(), 2.0)
    slippage = clamp(atr_med * 0.04, 0.05, 0.50)
    return round(spread, 3), round(slippage, 3)


def backtest_params(
    df: pd.DataFrame,
    atr_period: int,
    z_threshold: float,
    profile: ModeProfile,
) -> Dict[str, Any]:
    empty = {
        "atr_period": atr_period,
        "z_threshold": z_threshold,
        "trades": 0,
        "wins": 0,
        "win_rate": 0.0,
        "profit_factor": 0.0,
        "max_drawdown": 0.0,
        "robust_score": 0.0,
        "spread": 0.0,
        "slippage": 0.0,
        "curve": [],
        "curve_labels": [],
    }
    min_req = max(profile.z_window, atr_period, 50) + 30
    if df is None or len(df) < min_req:
        return empty

    spread, slippage = estimate_market_friction(df)
    half_spread = spread * 0.5
    work = pd.DataFrame(
        {
            "open": df["open"].astype(float),
            "high": df["high"].astype(float),
            "low": df["low"].astype(float),
            "close": df["close"].astype(float),
        }
    )
    work["atr"] = atr_series(work, atr_period)
    work["z"] = zscore_series(work["close"], profile.z_window)
    work["ema_f"] = ema_series(work["close"], 20)
    work["ema_s"] = ema_series(work["close"], 50)
    work["er"] = efficiency_ratio_series(work["close"], profile.er_period)
    work = work.dropna()
    if len(work) < 30:
        return empty

    c = work["close"].to_numpy(dtype=float)
    h = work["high"].to_numpy(dtype=float)
    l = work["low"].to_numpy(dtype=float)
    a = work["atr"].to_numpy(dtype=float)
    z = work["z"].to_numpy(dtype=float)
    ef = work["ema_f"].to_numpy(dtype=float)
    es = work["ema_s"].to_numpy(dtype=float)
    er = work["er"].to_numpy(dtype=float)

    risk_usd = ACCOUNT_BALANCE * RISK_PERCENT / 100.0
    equity = float(ACCOUNT_BALANCE)
    peak = equity
    max_dd = 0.0
    curve: List[float] = [round(equity, 2)]
    labels: List[str] = [work.index[0].isoformat()]
    trades = wins = 0
    gp = gl = 0.0
    r_returns: List[float] = []
    pos: Optional[Dict[str, Any]] = None
    tp_r = profile.tp_r_multiples

    for i in range(50, len(work)):
        if pos is None:
            if er[i] < profile.min_er:
                continue
            bull_pullback = (ef[i] > es[i]) and (-z_threshold <= z[i] <= -0.35)
            bear_pullback = (ef[i] < es[i]) and (0.35 <= z[i] <= z_threshold)
            if bull_pullback:
                entry_px = c[i] + half_spread + slippage
                sd = max(a[i] * profile.atr_risk_mult, 0.5)
                pos = {
                    "dir": "BUY",
                    "e": entry_px,
                    "sl": entry_px - sd,
                    "sd": sd,
                    "tp": [entry_px + sd * r for r in tp_r],
                    "h": [False, False, False],
                    "rem": 1.0,
                    "r": 0.0,
                }
            elif bear_pullback:
                entry_px = c[i] - half_spread - slippage
                sd = max(a[i] * profile.atr_risk_mult, 0.5)
                pos = {
                    "dir": "SELL",
                    "e": entry_px,
                    "sl": entry_px + sd,
                    "sd": sd,
                    "tp": [entry_px - sd * r for r in tp_r],
                    "h": [False, False, False],
                    "rem": 1.0,
                    "r": 0.0,
                }
            continue

        if pos["dir"] == "BUY":
            if l[i] - half_spread <= pos["sl"]:
                exit_px = pos["sl"] - slippage
                pos["r"] += pos["rem"] * ((exit_px - pos["e"]) / pos["sd"])
                pos["rem"] = 0.0
            else:
                for idx, frac in ((0, 0.5), (1, 0.3), (2, 0.2)):
                    if not pos["h"][idx] and h[i] - half_spread >= pos["tp"][idx]:
                        pos["h"][idx] = True
                        pos["r"] += frac * ((pos["tp"][idx] - pos["e"]) / pos["sd"])
                        pos["rem"] -= frac
                        if idx == 0:
                            pos["sl"] = max(pos["sl"], pos["e"])
                if pos["h"][0] and pos["rem"] > 0:
                    pos["sl"] = max(pos["sl"], c[i] - profile.trail_atr_mult * a[i])
        else:
            if h[i] + half_spread >= pos["sl"]:
                exit_px = pos["sl"] + slippage
                pos["r"] += pos["rem"] * ((pos["e"] - exit_px) / pos["sd"])
                pos["rem"] = 0.0
            else:
                for idx, frac in ((0, 0.5), (1, 0.3), (2, 0.2)):
                    if not pos["h"][idx] and l[i] + half_spread <= pos["tp"][idx]:
                        pos["h"][idx] = True
                        pos["r"] += frac * ((pos["e"] - pos["tp"][idx]) / pos["sd"])
                        pos["rem"] -= frac
                        if idx == 0:
                            pos["sl"] = min(pos["sl"], pos["e"])
                if pos["h"][0] and pos["rem"] > 0:
                    pos["sl"] = min(pos["sl"], c[i] + profile.trail_atr_mult * a[i])

        if pos["rem"] <= 1e-9:
            pnl = risk_usd * pos["r"]
            equity += pnl
            peak = max(peak, equity)
            max_dd = max(max_dd, (peak - equity) / peak * 100.0 if peak > 0 else 0.0)
            curve.append(round(equity, 2))
            labels.append(work.index[i].isoformat())
            r_returns.append(pos["r"])
            trades += 1
            if pnl >= 0:
                wins += 1
                gp += pnl
            else:
                gl += abs(pnl)
            pos = None

    win_rate = round(wins / trades * 100.0, 2) if trades else 0.0
    pf = round(gp / gl, 3) if gl > 0 else (3.5 if gp > 0 else 0.0)
    consistency = 1.0 / (1.0 + float(np.std(r_returns)) if len(r_returns) >= 2 else 1.5)
    sample_factor = 1.0 if trades >= MIN_BACKTEST_TRADES else (0.20 * trades / MIN_BACKTEST_TRADES)
    robust = (min(pf, 4.0) * (win_rate / 100.0) * math.sqrt(max(trades, 0)) * consistency * sample_factor) / (
        1.0 + max_dd / 8.0
    )

    return {
        "atr_period": atr_period,
        "z_threshold": z_threshold,
        "trades": trades,
        "wins": wins,
        "win_rate": win_rate,
        "profit_factor": pf,
        "max_drawdown": round(max_dd, 2),
        "robust_score": round(robust, 4),
        "spread": spread,
        "slippage": slippage,
        "curve": curve,
        "curve_labels": labels,
    }


def grid_search_optimize(
    df: pd.DataFrame,
    profile: ModeProfile,
    conn: Optional[sqlite3.Connection] = None,
) -> Dict[str, Any]:
    results: List[Dict[str, Any]] = []
    for atr_p in GRID_ATR_PERIODS:
        for z_thr in GRID_Z_THRESHOLDS:
            try:
                res = backtest_params(df, atr_p, z_thr, profile)
                results.append(res)
                if conn is not None:
                    persist_optimization(conn, profile.name, res)
            except Exception as exc:
                log.warning("Grid [%s] ATR=%s Z=%s error: %s", profile.name, atr_p, z_thr, exc)

    if not results:
        return {
            "atr_period": profile.atr_period,
            "z_threshold": 2.0,
            "trades": 0,
            "win_rate": 0.0,
            "profit_factor": 0.0,
            "max_drawdown": 0.0,
            "robust_score": 0.0,
            "spread": 0.0,
            "slippage": 0.0,
            "fallback_used": True,
            "curve": [],
            "curve_labels": [],
            "grid": [],
        }

    qualified = [r for r in results if r["trades"] >= MIN_BACKTEST_TRADES and r["profit_factor"] >= 1.0]
    pool = qualified if qualified else results
    pool.sort(key=lambda r: (r["robust_score"], r["profit_factor"], -r["max_drawdown"]), reverse=True)
    best = dict(pool[0])
    best["fallback_used"] = len(qualified) == 0
    best["grid"] = [
        {
            "atr_period": r["atr_period"],
            "z_threshold": r["z_threshold"],
            "trades": r["trades"],
            "win_rate": r["win_rate"],
            "profit_factor": r["profit_factor"],
            "max_drawdown": r["max_drawdown"],
            "robust_score": r["robust_score"],
        }
        for r in results
    ]
    return best


# ==============================================================================
# 10. AUTO-TRAILING STOP ENGINE (PER-MODE CALIBRATED)
# ==============================================================================
class TrailingStopEngine:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def register(self, signal_id: int, sig: Dict[str, Any]) -> None:
        try:
            self.conn.execute(
                """
                INSERT INTO open_positions (
                    signal_id, mode, direction, entry, sl, tp1, tp2, tp3,
                    atr, trail_mult, tp1_hit, last_ts, status, updated_ts
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, 'OPEN', ?)
                """,
                (
                    signal_id,
                    sig["mode"],
                    sig["decision"],
                    sig["entry"],
                    sig["sl"],
                    sig["tp1"],
                    sig["tp2"],
                    sig["tp3"],
                    sig["atr"],
                    sig["trail_mult"],
                    sig["bar_ts"],
                    iso_now(),
                ),
            )
            self.conn.commit()
        except sqlite3.Error as exc:
            log.error("Trailing position registration failed: %s", exc)

    def refresh(self, df: pd.DataFrame, mode: str) -> List[Dict[str, Any]]:
        try:
            rows = self.conn.execute(
                "SELECT * FROM open_positions WHERE status = 'OPEN' AND mode = ?",
                (mode,),
            ).fetchall()
        except sqlite3.Error:
            return []

        updates: List[Dict[str, Any]] = []
        for row in rows:
            r = dict(row)
            d = r["direction"]
            e = float(r["entry"])
            sl = float(r["sl"])
            tp1 = float(r["tp1"])
            tp2 = float(r["tp2"])
            tp3 = float(r["tp3"])
            atr = max(float(r["atr"] or 1.0), EPS)
            trail_mult = max(float(r.get("trail_mult") or 0.75), 0.25)
            hit = bool(r["tp1_hit"])

            last_ts: Optional[pd.Timestamp] = None
            if r["last_ts"]:
                try:
                    last_ts = pd.Timestamp(r["last_ts"])
                    if last_ts.tzinfo is None:
                        last_ts = last_ts.tz_localize("UTC")
                    else:
                        last_ts = last_ts.tz_convert("UTC")
                except Exception:
                    last_ts = None

            bars = df[df.index > last_ts] if last_ts is not None else df.tail(1)
            status = "OPEN"
            last_bar = last_ts

            for ts, b in bars.iterrows():
                hi, lo, c = float(b["high"]), float(b["low"]), float(b["close"])
                last_bar = ts
                if d == "BUY":
                    if lo <= sl:
                        status = "STOPPED"
                        break
                    if not hit and hi >= tp1:
                        hit = True
                        sl = max(sl, e)
                    if hit and hi >= tp2:
                        sl = max(sl, tp1)
                    if hit and hi >= tp3:
                        sl = max(sl, tp2)
                        status = "TP3_COMPLETED"
                        break
                    if hit:
                        sl = max(sl, c - trail_mult * atr)
                else:
                    if hi >= sl:
                        status = "STOPPED"
                        break
                    if not hit and lo <= tp1:
                        hit = True
                        sl = min(sl, e)
                    if hit and lo <= tp2:
                        sl = min(sl, tp1)
                    if hit and lo <= tp3:
                        sl = min(sl, tp2)
                        status = "TP3_COMPLETED"
                        break
                    if hit:
                        sl = min(sl, c + trail_mult * atr)

            last_bar_iso = last_bar.isoformat() if last_bar is not None else iso_now()
            self.conn.execute(
                """
                UPDATE open_positions
                SET sl = ?, tp1_hit = ?, last_ts = ?, status = ?, updated_ts = ?
                WHERE id = ?
                """,
                (round(sl, 2), int(hit), last_bar_iso, status, iso_now(), r["id"]),
            )
            trail_mode = (
                "BREAKEVEN_LOCKED"
                if hit and abs(sl - e) < 0.05
                else ("ATR_TRAILING" if hit else "ARMED_AT_TP1")
            )
            updates.append(
                {
                    "id": r["id"],
                    "mode": mode,
                    "direction": d,
                    "sl": round(sl, 2),
                    "status": status,
                    "trail_mode": trail_mode,
                }
            )
        self.conn.commit()
        return updates


# ==============================================================================
# 11. BACKTEST PERFORMANCE CHART (HEADLESS Agg)
# ==============================================================================
def plot_backtest(df: pd.DataFrame, opt: Dict[str, Any], mode: str, path: str = CHART_PATH) -> bool:
    fig = None
    try:
        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 8))
        curve = list(opt.get("curve", []))
        labels = list(opt.get("curve_labels", []))
        if len(curve) >= 2:
            x = (
                pd.to_datetime(labels, utc=True, errors="coerce")
                if len(labels) == len(curve)
                else np.arange(len(curve))
            )
            ax1.plot(x, curve, color="#0f766e", lw=1.8)
            ax1.fill_between(x, curve, min(curve), color="#0f766e", alpha=0.12)
            ax1.set_title(
                f"XAUUSD {mode} Equity | ATR({opt.get('atr_period')}) Z({opt.get('z_threshold')}) "
                f"| Trades: {opt.get('trades')} | WR: {opt.get('win_rate')}% | PF: {opt.get('profit_factor')} "
                f"| DD: {opt.get('max_drawdown')}%",
                fontweight="bold",
                fontsize=10,
            )
        else:
            ax1.text(
                0.5,
                0.5,
                f"Insufficient Closed Backtest Trades ({opt.get('trades', 0)})",
                ha="center",
                va="center",
                transform=ax1.transAxes,
            )
            ax1.set_title(f"XAUUSD {mode} Equity Simulation", fontweight="bold", fontsize=10)

        tail = df.tail(240)
        ax2.plot(tail.index, tail["close"], color="#1e293b", lw=1.4, label="Spot Close")
        ax2.plot(tail.index, ema_series(tail["close"], 20), color="#d97706", lw=1.1, label="EMA 20")
        ax2.plot(tail.index, ema_series(tail["close"], 50), color="#dc2626", lw=1.1, label="EMA 50")
        ax2.set_title(f"XAUUSD Spot Structure ({mode})", fontweight="bold", fontsize=10)
        ax2.legend(loc="upper left", fontsize=8)

        fig.tight_layout()
        fig.savefig(path, dpi=120, bbox_inches="tight")
        return True
    except Exception as exc:
        log.exception("Chart rendering failed: %s", exc)
        return False
    finally:
        if fig is not None:
            plt.close(fig)


# ==============================================================================
# 12. CONCISE TELEGRAM ALERTS & INTERACTIVE COMMANDS
# ==============================================================================
def build_concise_signal_alert(sig: Dict[str, Any]) -> str:
    hhmm = utcnow().strftime("%H:%M UTC")
    return (
        f"⚡ <b>XAUUSD | {sig['mode']} | {sig['decision']}</b>\n"
        f"🎯 Entry: <code>{sig['entry']:.2f}</code> | 🛑 SL: <code>{sig['sl']:.2f}</code>\n"
        f"✅ TP1: <code>{sig['tp1']:.2f}</code> | TP2: <code>{sig['tp2']:.2f}</code> | TP3: <code>{sig['tp3']:.2f}</code>\n"
        f"📐 Lot: <code>{sig['lots']:.2f}</code> | Skor: <code>{sig['confluence']:.1f}/100</code> | Regime: <code>{sig['regime']}</code>\n"
        f"🔄 Trail: Aktif @ TP1 (BE) | 🕒 <code>{hhmm}</code>\n"
        f"📊 ER: <code>{sig['er']:.2f}</code> | ADX: <code>{sig['adx']:.1f}</code> | Z: <code>{sig['zscore']:+.2f}</code> | SL: <code>{sig['stop_pips']:.1f} pips</code>"
    )


def build_concise_check_reply(results: List[Dict[str, Any]], opts: Dict[str, Dict[str, Any]]) -> str:
    hhmm = utcnow().strftime("%H:%M UTC")
    blocks: List[str] = []
    for sig in results:
        mode = sig["mode"]
        opt = opts.get(mode, {})
        if sig["decision"] in ("BUY", "SELL"):
            blocks.append(build_concise_signal_alert(sig))
        else:
            top_reason = sig["reject_reasons"][0] if sig["reject_reasons"] else "Confluence < 95"
            blocks.append(
                f"⏸ <b>XAUUSD | {mode} | NO_SIGNAL</b> (Bias: {sig['candidate_bias']})\n"
                f"💵 Spot: <code>{sig['entry']:.2f}</code> | Skor: <code>{sig['confluence']:.1f}/100</code> | Regime: <code>{sig['regime']}</code>\n"
                f"📊 ER: <code>{sig['er']:.2f}</code> | ADX: <code>{sig['adx']:.1f}</code> | Z: <code>{sig['zscore']:+.2f}</code> | PF: <code>{opt.get('profit_factor', 0)}</code>\n"
                f"🛡 Filter: <code>{top_reason}</code>"
            )
    return "\n\n".join(blocks) + f"\n\n🕒 <code>{hhmm}</code>"


def tg_get_updates(offset: int, timeout: int = 8) -> List[Dict[str, Any]]:
    if not TELEGRAM_BOT_TOKEN:
        return []
    try:
        r = requests.get(
            tg_api("getUpdates"),
            params={
                "offset": offset,
                "timeout": timeout,
                "allowed_updates": json.dumps(["message"]),
            },
            timeout=HTTP_TIMEOUT + timeout,
        )
        data = r.json()
        return data.get("result", []) if data.get("ok") else []
    except Exception:
        return []


def evaluate_modes(
    conn: sqlite3.Connection,
    modes: Sequence[str],
    persist: bool = True,
) -> Tuple[List[Dict[str, Any]], Dict[str, Dict[str, Any]], Optional[pd.DataFrame], str]:
    results: List[Dict[str, Any]] = []
    opts: Dict[str, Dict[str, Any]] = {}
    chart_df: Optional[pd.DataFrame] = None
    chart_mode = modes[-1]

    trail_eng = TrailingStopEngine(conn)
    for mode in modes:
        profile = MODE_PROFILES[mode]
        df, src, deg = fetch_market_data(mode)
        chart_df = df
        chart_mode = mode

        opt = grid_search_optimize(df, profile, conn if persist else None)
        opts[mode] = opt

        sess_ok, sess_rsn = session_governor(mode)
        trail_eng.refresh(df, mode)
        sig = analyze_mode(df, src, deg, profile, sess_ok, sess_rsn)

        if persist and sig["decision"] in ("BUY", "SELL"):
            is_dup, dup_reason = is_duplicate_or_cooldown(conn, sig, profile)
            sig["suppressed_reason"] = dup_reason if is_dup else ""
            if not is_dup:
                sig_id = persist_signal(conn, sig)
                trail_eng.register(sig_id, sig)
                sig["signal_id"] = sig_id
            else:
                log.info("[%s] Signal suppressed by cooldown/dedup: %s", mode, dup_reason)
        else:
            sig["suppressed_reason"] = ""

        results.append(sig)

    return results, opts, chart_df, chart_mode


def handle_update(conn: sqlite3.Connection, update: Dict[str, Any]) -> None:
    msg = update.get("message") or {}
    txt = (msg.get("text") or "").strip()
    chat_id = str(msg.get("chat", {}).get("id", "")).strip()
    if not txt or not chat_id:
        return

    allowed = authorized_chat_ids()
    if allowed and chat_id not in allowed:
        log.warning("Unauthorized chat_id=%s ignored", chat_id)
        return

    parts = txt.split()
    cmd = parts[0].split("@")[0].lower()
    arg = parts[1].lower() if len(parts) > 1 else ""

    if cmd in ("/start", "/help"):
        send_telegram_html(
            f"⚡ <b>{SYSTEM_NAME} {VERSION}</b>\n"
            f"/check — Cek SCALP & SWING\n"
            f"/check scalp — Analisa SCALP\n"
            f"/check swing — Analisa SWING\n"
            f"/status — Health & posisi aktif\n"
            f"/signal — Sinyal terverifikasi terakhir",
            chat_id,
        )
    elif cmd == "/status" or (cmd == "/check" and arg == "status"):
        total_sig = conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
        open_pos = conn.execute("SELECT COUNT(*) FROM open_positions WHERE status='OPEN'").fetchone()[0]
        cb_open, cb_until = check_circuit_breaker(conn)
        s_scalp, _ = session_governor("SCALP")
        s_swing, _ = session_governor("SWING")
        send_telegram_html(
            f"🩺 <b>SYSTEM STATUS</b>\n"
            f"Signals DB: <code>{total_sig}</code> | Open Trail: <code>{open_pos}</code>\n"
            f"Sesi SCALP: <code>{'OPEN' if s_scalp else 'CLOSED'}</code> | SWING: <code>{'OPEN' if s_swing else 'CLOSED'}</code>\n"
            f"Circuit: <code>{'OPEN ' + str(cb_until) if cb_open else 'HEALTHY'}</code>\n"
            f"🕒 <code>{iso_now()}</code>",
            chat_id,
        )
    elif cmd == "/signal":
        rows = conn.execute(
            "SELECT mode, decision, entry, sl, tp1, confluence, ts FROM signals ORDER BY id DESC LIMIT 2"
        ).fetchall()
        if not rows:
            send_telegram_html("📭 Belum ada sinyal tersimpan.", chat_id)
        else:
            lines = [
                f"• <b>{r['mode']} {r['decision']}</b> @ <code>{r['entry']:.2f}</code> "
                f"(SL <code>{r['sl']:.2f}</code> | TP1 <code>{r['tp1']:.2f}</code> | {r['confluence']:.0f}/100) [{r['ts']}]"
                for r in rows
            ]
            send_telegram_html("📜 <b>LATEST SIGNALS</b>\n" + "\n".join(lines), chat_id)
    elif cmd == "/check":
        target_modes = ["SCALP"] if arg == "scalp" else (["SWING"] if arg == "swing" else ["SCALP", "SWING"])
        try:
            results, opts, _, _ = evaluate_modes(conn, target_modes, persist=False)
            send_telegram_html(build_concise_check_reply(results, opts), chat_id)
        except Exception as exc:
            send_telegram_html(f"❌ <b>/check gagal:</b> <code>{str(exc)[:180]}</code>", chat_id)


def run_listener(conn: sqlite3.Connection, budget: int = POLL_SECONDS) -> int:
    if not TELEGRAM_BOT_TOKEN:
        return 0
    deadline = time.time() + max(budget, 0)
    offset = int(get_state(conn, "tg_offset", "0") or 0)
    processed = 0
    while time.time() < deadline:
        remaining = int(max(1, min(8, deadline - time.time())))
        updates = tg_get_updates(offset, timeout=remaining)
        if not updates:
            time.sleep(1.0)
            continue
        for u in updates:
            if "update_id" in u:
                offset = int(u["update_id"]) + 1
                set_state(conn, "tg_offset", str(offset))
            try:
                handle_update(conn, u)
                processed += 1
            except Exception as exc:
                log.warning("Telegram command error: %s", exc)
    return processed


# ==============================================================================
# 13. CORE EXECUTION CYCLE
# ==============================================================================
def run_cycle(
    conn: sqlite3.Connection,
    notify: bool = True,
    listen_budget: int = POLL_SECONDS,
    modes: Sequence[str] = ("SCALP", "SWING"),
) -> Dict[str, Any]:
    cb_open, cb_until = check_circuit_breaker(conn)
    if cb_open:
        forensic_audit_log(conn, "CIRCUIT_OPEN_SKIP", {"open_until": cb_until}, level="WARNING")
        if listen_budget > 0:
            run_listener(conn, min(listen_budget, 30))
        return {"status": "CIRCUIT_OPEN", "open_until": cb_until}

    results, opts, chart_df, chart_mode = evaluate_modes(conn, modes, persist=True)
    reset_circuit_breaker(conn)

    chart_ok = False
    if chart_df is not None:
        chart_ok = plot_backtest(chart_df, opts.get(chart_mode, {}), chart_mode, CHART_PATH)

    dispatched = 0
    for sig in results:
        is_verified = sig["decision"] in ("BUY", "SELL")
        not_suppressed = not sig.get("suppressed_reason")
        if notify and ((is_verified and not_suppressed) or NOTIFY_NO_SIGNAL):
            msg = (
                build_concise_signal_alert(sig)
                if is_verified
                else build_concise_check_reply([sig], opts)
            )
            send_telegram_html(msg)
            send_discord(msg)
            dispatched += 1
            forensic_audit_log(
                conn,
                "SIGNAL_DISPATCHED",
                {
                    "mode": sig["mode"],
                    "decision": sig["decision"],
                    "confluence": sig["confluence"],
                    "entry": sig["entry"],
                    "lots": sig["lots"],
                },
            )

    if listen_budget > 0:
        run_listener(conn, listen_budget)

    summary = {
        "status": "OK",
        "chart": chart_ok,
        "dispatched": dispatched,
        "modes": {
            s["mode"]: {
                "decision": s["decision"],
                "confluence": s["confluence"],
                "regime": s["regime"],
                "suppressed": s.get("suppressed_reason", ""),
                "rejects": s["reject_reasons"][:2],
            }
            for s in results
        },
    }
    forensic_audit_log(conn, "CYCLE_COMPLETE", summary)
    return summary


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=f"{SYSTEM_NAME} {VERSION} XAUUSD Engine")
    parser.add_argument("--mode", choices=["once", "check", "listen"], default="once")
    parser.add_argument("--strategy", choices=["all", "scalp", "swing"], default="all")
    parser.add_argument("--listen-seconds", type=int, default=POLL_SECONDS)
    parser.add_argument("--no-notify", action="store_true")
    args = parser.parse_args(argv)

    target_modes = (
        ["SCALP"]
        if args.strategy == "scalp"
        else (["SWING"] if args.strategy == "swing" else ["SCALP", "SWING"])
    )

    conn = db_connect()
    init_db(conn)
    try:
        if args.mode == "listen":
            run_listener(conn, args.listen_seconds)
            return 0
        if args.mode == "check":
            results, opts, chart_df, chart_mode = evaluate_modes(conn, target_modes, persist=False)
            if chart_df is not None:
                plot_backtest(chart_df, opts.get(chart_mode, {}), chart_mode, CHART_PATH)
            print(strip_html(build_concise_check_reply(results, opts)))
            return 0

        run_cycle(conn, notify=not args.no_notify, listen_budget=args.listen_seconds, modes=target_modes)
        return 0
    except Exception as exc:
        tb = traceback.format_exc()
        log.error("FATAL CYCLE FAILURE: %s", exc)
        record_circuit_failure(conn, str(exc))
        send_emergency_alert(tb)
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
