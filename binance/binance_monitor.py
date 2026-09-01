#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Binance 异动监控 - GitHub Actions 定时运行，异动推送到 Telegram。

检测项（均可通过环境变量配置）:
  1. 价格异动: 相对上次检查的涨跌幅超过阈值 -> 提醒
  2. 24h 涨跌幅异常: 超过阈值 -> 提醒（防重复）
  3. 合约资金费率异常: |费率| 超过阈值 -> 提醒（需 Key 有合约权限，否则自动跳过）

状态保存在 state.json（由 workflow 用 Actions cache 持久化），
每次运行读取上次状态、检测异动、写回新状态。

运行环境: GitHub Actions (海外 runner, 直连币安/Telegram, 无需代理)
仅用 Python 标准库, 零第三方依赖。

注意: GitHub 托管 runner 位于美国, api.binance.com 返回 HTTP 451 (地区限制),
因此现货行情改用币安官方公开数据镜像 data-api.binance.vision (无地区限制)。
合约资金费率接口 (fapi.binance.com) 同样被限, 脚本自动跳过该检测项。
"""

import hashlib
import hmac
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta

CN_TZ = timezone(timedelta(hours=8))
SPOT_BASE = "https://data-api.binance.vision"  # 币安官方公开数据镜像（规避地区限制）
FUTURES_BASE = "https://fapi.binance.com"

STATE_FILE = os.environ.get("STATE_FILE", "state.json")


def now_str():
    return datetime.now(CN_TZ).strftime("%Y-%m-%d %H:%M:%S")


def env_float(name, default):
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return float(default)


# ---------------- 币安 API（零依赖） ----------------
def api_get(base, path, params=None, signed=False, api_key="", api_secret=""):
    url = base + path
    params = dict(params or {})
    if signed:
        params["timestamp"] = int(time.time() * 1000)
        query = urllib.parse.urlencode(params)
        sig = hmac.new(api_secret.encode(), query.encode(), hashlib.sha256).hexdigest()
        url = url + "?" + query + "&signature=" + sig
        req = urllib.request.Request(url, headers={"X-MBX-APIKEY": api_key})
    else:
        if params:
            url = url + "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"User-Agent": "binance-monitor"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode())


def get_ticker(symbol):
    """现货 24h 统计。"""
    return api_get(SPOT_BASE, "/api/v3/ticker/24hr", {"symbol": symbol})


def get_funding(symbol):
    """合约最新资金费率。"""
    try:
        rows = api_get(FUTURES_BASE, "/fapi/v1/fundingRate", {"symbol": symbol, "limit": 1})
        return float(rows[0]["fundingRate"]) if rows else None
    except Exception:
        return None  # 无合约权限或异常时跳过


# ---------------- Telegram 推送 ----------------
def send_telegram(text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        print(f"!! Telegram 未配置，无法推送:\n{text}")
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode()
    req = urllib.request.Request(url, data=data)
    with urllib.request.urlopen(req, timeout=20) as resp:
        result = json.loads(resp.read().decode())
        return result.get("ok", False)


# ---------------- 主逻辑 ----------------
def main():
    symbols = [s.strip().upper() for s in
               os.environ.get("MONITOR_SYMBOLS", "BTCUSDT,ETHUSDT").split(",") if s.strip()]
    price_alert_pct = env_float("PRICE_ALERT_PCT", 2.0)      # 相对上次检查
    day_alert_pct = env_float("DAY_ALERT_PCT", 5.0)          # 24h 涨跌幅
    funding_alert_pct = env_float("FUNDING_ALERT_PCT", 0.05)  # 资金费率绝对值

    state = {}
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                state = json.load(f)
        except Exception as e:
            print(f"!! 状态文件读取失败({e})，按首次运行处理")

    alerts = []
    now_ts = int(time.time())

    for symbol in symbols:
        try:
            t = get_ticker(symbol)
        except Exception as e:
            print(f"!! {symbol} 行情获取失败: {e}")
            continue

        price = float(t["lastPrice"])
        day_pct = float(t["priceChangePercent"])
        prev = state.get(symbol, {})

        # --- 检测1: 相对上次检查的价格异动 ---
        last_price = prev.get("last_price")
        if last_price:
            chg = (price - last_price) / last_price * 100
            if abs(chg) >= price_alert_pct:
                direction = "上涨" if chg > 0 else "下跌"
                alerts.append(
                    f"⚡ <b>{symbol} 价格异动</b>\n"
                    f"现价: {price:,.4f}\n"
                    f"较上次检查: {chg:+.2f}% ({direction})\n"
                    f"24h涨跌: {day_pct:+.2f}%\n"
                    f"时间: {now_str()} (UTC+8)"
                )

        # --- 检测2: 24h 涨跌幅异常（防重复，变化>1%才再报） ---
        last_day_pct = prev.get("last_day_pct")
        last_day_alert_pct = prev.get("last_day_alert_pct")
        if abs(day_pct) >= day_alert_pct:
            if last_day_alert_pct is None or abs(day_pct - last_day_alert_pct) >= 1.0:
                alerts.append(
                    f"🔥 <b>{symbol} 24h异动</b>\n"
                    f"现价: {price:,.4f}\n"
                    f"24h涨跌: {day_pct:+.2f}%\n"
                    f"时间: {now_str()} (UTC+8)"
                )
                state[symbol]["last_day_alert_pct"] = day_pct
        elif last_day_alert_pct is not None:
            state[symbol]["last_day_alert_pct"] = None  # 回落后重置

        # --- 检测3: 合约资金费率异常 ---
        if funding_alert_pct > 0:
            funding = get_funding(symbol)
            if funding is not None:
                last_funding_alert = prev.get("last_funding_alert")
                if abs(funding) * 100 >= funding_alert_pct:
                    if last_funding_alert is None or abs(funding - last_funding_alert) >= 0.01:
                        side = "多头过热(多方付费)" if funding > 0 else "空头过热(空方付费)"
                        alerts.append(
                            f"💰 <b>{symbol} 资金费率异常</b>\n"
                            f"费率: {funding*100:+.4f}% ({side})\n"
                            f"现价: {price:,.4f}\n"
                            f"时间: {now_str()} (UTC+8)"
                        )
                        state[symbol]["last_funding_alert"] = funding
                elif last_funding_alert is not None:
                    state[symbol]["last_funding_alert"] = None

        # --- 更新状态 ---
        state[symbol] = {
            **state.get(symbol, {}),
            "last_price": price,
            "last_day_pct": day_pct,
            "last_check": now_ts,
        }

    # 写回状态
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

    # 推送
    if alerts:
        msg = "\n\n".join(alerts)
        print(msg)
        ok = send_telegram(msg)
        print(f"Telegram 推送: {'成功' if ok else '失败'}")
    else:
        print(f"[{now_str()}] 无异动，共监控 {len(symbols)} 个交易对")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"!! 运行异常: {e}")
        sys.exit(1)
