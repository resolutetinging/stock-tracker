#!/usr/bin/env python3
"""台股觀察名單追蹤器：抓TWSE官方資料、比對alert規則、有異常才寄信。
數字一律來自TWSE官方API直接算出，LLM只負責把數字變化轉譯成白話一句話，
不生成任何數字、不給買賣建議（比照Renewable Tracker的既有原則）。
"""
import json
import os
import smtplib
import sys
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
WATCHLIST_PATH = os.path.join(DATA_DIR, "watchlist.json")
HISTORY_PATH = os.path.join(DATA_DIR, "stock_history.json")
ALERTS_LOG_PATH = os.path.join(DATA_DIR, "alerts_log.json")

STOCK_DAY_URL = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
BWIBBU_URL = "https://openapi.twse.com.tw/v1/exchangeReport/BWIBBU_ALL"

MAX_HISTORY_LEN = 260  # 約一年交易日，供52週高低判斷
MIN_HISTORY_FOR_52W = 60  # 累積不到這麼多筆，52週高低規則先不啟用
COOLDOWN_DAYS = 7  # 狀態持續型alert的預設冷卻天數

GROQ_MODELS = ["openai/gpt-oss-120b", "openai/gpt-oss-20b"]

TAIPEI_TZ = timezone(timedelta(hours=8))


def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        content = f.read().strip()
        if not content:
            return default
        return json.loads(content)


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def to_float(s):
    """TWSE數字欄位常是空字串(例如虧損公司沒有本益比)，轉成None讓比對時跳過"""
    if s is None:
        return None
    s = str(s).strip().replace(",", "")
    if s == "" or s == "--":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def roc_date_to_iso(date_str):
    """TWSE的Date欄位是民國年字串，例如'1150909'＝民國115年09月09日＝2026-09-09"""
    date_str = str(date_str).strip()
    if len(date_str) != 7:
        return None
    roc_year = int(date_str[:3])
    month = int(date_str[3:5])
    day = int(date_str[5:7])
    return f"{1911 + roc_year:04d}-{month:02d}-{day:02d}"


def fetch_json(url, retries=3, timeout=15):
    headers = {"User-Agent": "Mozilla/5.0 (personal stock watchlist tracker)"}
    last_err = None
    for attempt in range(retries):
        try:
            resp = requests.get(url, headers=headers, timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:  # noqa: BLE001 - 單純的網路重試，記下最後一次錯誤即可
            last_err = e
    raise RuntimeError(f"抓取失敗（重試{retries}次）：{url}：{last_err}")


def today_taipei_str():
    return datetime.now(TAIPEI_TZ).strftime("%Y-%m-%d")


def build_today_records(day_all, bwibbu_all, tickers):
    """從全市場資料篩出watchlist的ticker，合併成當日紀錄。回傳(records, trade_date)"""
    day_by_code = {row["Code"]: row for row in day_all if row.get("Code") in tickers}
    bwibbu_by_code = {row["Code"]: row for row in bwibbu_all if row.get("Code") in tickers}

    trade_dates = {roc_date_to_iso(row["Date"]) for row in day_by_code.values() if row.get("Date")}
    if len(trade_dates) > 1:
        raise RuntimeError(f"STOCK_DAY_ALL回傳日期不一致：{trade_dates}")
    trade_date = next(iter(trade_dates), None)

    records = {}
    for ticker in tickers:
        day_row = day_by_code.get(ticker)
        if not day_row:
            continue
        close = to_float(day_row.get("ClosingPrice"))
        change = to_float(day_row.get("Change"))
        change_pct = None
        if close is not None and change is not None and (close - change) != 0:
            prev_close = close - change
            change_pct = round(change / prev_close * 100, 2)

        bwibbu_row = bwibbu_by_code.get(ticker, {})
        records[ticker] = {
            "date": trade_date,
            "close": close,
            "change_pct": change_pct,
            "volume": to_float(day_row.get("TradeVolume")),
            "pe_ratio": to_float(bwibbu_row.get("PEratio")),
            "dividend_yield": to_float(bwibbu_row.get("DividendYield")),
            "pb_ratio": to_float(bwibbu_row.get("PBratio")),
        }
    return records, trade_date


def upsert_history(history, ticker, record):
    """同日重跑覆蓋不新增，確保冪等；只保留最近MAX_HISTORY_LEN筆"""
    series = history.setdefault(ticker, [])
    series[:] = [r for r in series if r["date"] != record["date"]]
    series.append(record)
    series.sort(key=lambda r: r["date"])
    if len(series) > MAX_HISTORY_LEN:
        del series[: len(series) - MAX_HISTORY_LEN]


def resolve_threshold(stock, key):
    override = stock.get("alert_thresholds", {}).get(key)
    if override is not None:
        return override
    return None  # 由呼叫端自行fallback到global_defaults


def check_alerts(stock, defaults, series):
    """純數學規則，不用LLM算數字。回傳這檔股票今天觸發的alert候選清單"""
    if not series:
        return []
    today = series[-1]
    alerts = []

    daily_pct_threshold = stock.get("alert_thresholds", {}).get("daily_change_pct")
    if daily_pct_threshold is None:
        daily_pct_threshold = defaults.get("daily_change_pct")
    if daily_pct_threshold is not None and today["change_pct"] is not None:
        if abs(today["change_pct"]) >= daily_pct_threshold:
            alerts.append({
                "ticker": stock["ticker"], "name": stock["name"], "rule_type": "daily_change_pct",
                "value": today["change_pct"], "threshold": daily_pct_threshold,
                "date": today["date"],
            })

    check_52w = stock.get("alert_thresholds", {}).get("check_52w_high_low")
    if check_52w is None:
        check_52w = defaults.get("check_52w_high_low")
    if check_52w and len(series) >= MIN_HISTORY_FOR_52W and today["close"] is not None:
        closes = [r["close"] for r in series if r["close"] is not None]
        if closes:
            if today["close"] >= max(closes):
                alerts.append({
                    "ticker": stock["ticker"], "name": stock["name"], "rule_type": "52w_high",
                    "value": today["close"], "date": today["date"],
                })
            elif today["close"] <= min(closes):
                alerts.append({
                    "ticker": stock["ticker"], "name": stock["name"], "rule_type": "52w_low",
                    "value": today["close"], "date": today["date"],
                })

    for key, rule_type, compare in [
        ("pe_ratio_high", "pe_ratio_high", lambda v, t: v is not None and v >= t),
        ("pe_ratio_low", "pe_ratio_low", lambda v, t: v is not None and v <= t),
        ("yield_pct_low", "yield_pct_low", lambda v, t: v is not None and v <= t),
    ]:
        threshold = stock.get("alert_thresholds", {}).get(key)
        if threshold is None:
            threshold = defaults.get(key)
        if threshold is None:
            continue
        field = "pe_ratio" if "pe_ratio" in key else "dividend_yield"
        value = today.get(field)
        if compare(value, threshold):
            alerts.append({
                "ticker": stock["ticker"], "name": stock["name"], "rule_type": rule_type,
                "value": value, "threshold": threshold, "date": today["date"],
            })

    return alerts


def filter_new_alerts(alerts, alerts_log, today_str):
    """依alerts_log過濾掉今天不用重複寄的alert，回傳真正要寄的清單並更新alerts_log"""
    to_send = []
    for alert in alerts:
        log_key = f"{alert['ticker']}:{alert['rule_type']}"
        prev = alerts_log.get(log_key)
        is_daily_event = alert["rule_type"] == "daily_change_pct"

        if prev is None:
            should_send = True
        elif is_daily_event:
            should_send = prev.get("last_sent_date") != today_str
        else:
            last_sent = prev.get("last_sent_date")
            days_since = None
            if last_sent:
                try:
                    days_since = (datetime.fromisoformat(today_str) - datetime.fromisoformat(last_sent)).days
                except ValueError:
                    days_since = None
            made_new_extreme = (
                prev.get("last_value") is not None
                and alert.get("value") is not None
                and alert["rule_type"] in ("52w_high",) and alert["value"] > prev["last_value"]
                or alert["rule_type"] in ("52w_low",) and alert["value"] < prev["last_value"]
            )
            should_send = days_since is None or days_since >= COOLDOWN_DAYS or made_new_extreme

        if should_send:
            to_send.append(alert)
            alerts_log[log_key] = {"last_sent_date": today_str, "last_value": alert.get("value")}

    return to_send


RULE_LABELS = {
    "daily_change_pct": "單日漲跌幅",
    "52w_high": "52週新高",
    "52w_low": "52週新低",
    "pe_ratio_high": "本益比偏高",
    "pe_ratio_low": "本益比偏低",
    "yield_pct_low": "殖利率偏低",
}


def fallback_template(alerts):
    """Groq呼叫失敗時的保底：純模板字串，確保email一定寄得出去"""
    lines = []
    for a in alerts:
        label = RULE_LABELS.get(a["rule_type"], a["rule_type"])
        if a["rule_type"] == "daily_change_pct":
            direction = "漲" if a["value"] > 0 else "跌"
            lines.append(f"{a['name']}（{a['ticker']}）今天收盤{direction}了{abs(a['value']):.1f}%，超過你設定的{a['threshold']}%提醒門檻。")
        elif a["rule_type"] in ("52w_high", "52w_low"):
            lines.append(f"{a['name']}（{a['ticker']}）今天收盤價{a['value']}元，創下近一年的{label}。")
        else:
            lines.append(f"{a['name']}（{a['ticker']}）目前{label}，數值為{a['value']}（門檻{a['threshold']}）。")
    return "\n".join(lines)


def call_groq_for_alerts(alerts):
    """把當次alert一次送進Groq轉譯成白話，明確禁止給買賣建議。失敗回傳None讓呼叫端fallback"""
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        return None
    try:
        from groq import Groq
    except ImportError:
        return None

    alert_lines = []
    for a in alerts:
        label = RULE_LABELS.get(a["rule_type"], a["rule_type"])
        alert_lines.append(f"- {a['name']}（{a['ticker']}）觸發「{label}」，數值{a.get('value')}"
                            + (f"，門檻{a['threshold']}" if a.get("threshold") is not None else ""))

    prompt = (
        "你是一個幫不懂投資的人把股票數字變化講成白話的小助手。以下是今天觸發的提醒項目：\n"
        + "\n".join(alert_lines)
        + "\n\n請針對每一項用一句話解釋這個數字變化「代表什麼意思」，用詞盡量白話，"
        "不要用艱澀術語堆疊。絕對禁止：不能給任何買進/賣出/加碼/減碼建議，"
        "不能預測未來走勢，不能說「這是好時機」或「應該要」這類建議語氣。"
        "只能客觀解釋數字的意義。直接輸出純文字，每項一行，不要用markdown、不要用JSON，"
        "務必使用繁體中文（台灣正體），禁止簡體字與日文漢字。"
    )

    client = Groq(api_key=api_key)
    for model in GROQ_MODELS:
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=1000,
                reasoning_effort="low",
            )
            content = resp.choices[0].message.content or ""
            content = content.strip()
            if content:
                return content
        except Exception as e:  # noqa: BLE001 - 413等API層錯誤，換下一個model重試
            print(f"  ⚠ Groq呼叫失敗（{model}）：{e}")
            continue
    return None


def send_email(subject, plain_body):
    gmail_user = os.environ.get("GMAIL_USER")
    gmail_pass = os.environ.get("GMAIL_APP_PASSWORD")
    recipient = os.environ.get("NOTIFY_EMAIL")
    if not (gmail_user and gmail_pass and recipient):
        print("  ⚠ 缺少email環境變數，略過寄信")
        return

    disclaimer = (
        "\n\n---\n這個工具只是幫你追蹤自己選的股票的公開數字變化，"
        "所有內容是資料整理與白話翻譯，不構成任何投資建議，買賣決定與風險請自行判斷。"
    )
    html_lines = "".join(f"<div>• {line}</div>" for line in plain_body.split("\n") if line.strip())
    html_body = f"<div>{html_lines}</div><div style='color:#888;margin-top:16px;font-size:12px;'>{disclaimer}</div>"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = gmail_user
    msg["To"] = recipient
    msg.attach(MIMEText(plain_body + disclaimer, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(gmail_user, gmail_pass)
        server.sendmail(gmail_user, [recipient], msg.as_string())
    print("  ✓ 已寄出提醒信")


def main():
    watchlist = load_json(WATCHLIST_PATH, {"global_defaults": {}, "stocks": []})
    defaults = watchlist.get("global_defaults", {})
    active_stocks = [s for s in watchlist.get("stocks", []) if s.get("active", True)]
    if not active_stocks:
        print("watchlist沒有啟用中的股票，結束")
        return
    tickers = {s["ticker"] for s in active_stocks}

    print(f"追蹤 {len(tickers)} 檔股票：{sorted(tickers)}")
    day_all = fetch_json(STOCK_DAY_URL)
    bwibbu_all = fetch_json(BWIBBU_URL)

    records, trade_date = build_today_records(day_all, bwibbu_all, tickers)
    if not trade_date:
        print("查無有效交易日資料（可能非交易日），結束")
        return

    today_str = today_taipei_str()
    if trade_date != today_str:
        print(f"TWSE最新資料日期為{trade_date}，非今天({today_str})，可能是非交易日或資料尚未更新，略過本次")
        return

    history = load_json(HISTORY_PATH, {})
    for ticker, record in records.items():
        upsert_history(history, ticker, record)
    save_json(HISTORY_PATH, history)
    print(f"✓ stock_history.json 已更新至 {trade_date}")

    all_alerts = []
    for stock in active_stocks:
        series = history.get(stock["ticker"], [])
        all_alerts.extend(check_alerts(stock, defaults, series))

    if not all_alerts:
        print("今天沒有觸發任何alert，不寄信")
        return

    alerts_log = load_json(ALERTS_LOG_PATH, {})
    new_alerts = filter_new_alerts(all_alerts, alerts_log, today_str)
    save_json(ALERTS_LOG_PATH, alerts_log)

    if not new_alerts:
        print(f"觸發{len(all_alerts)}項alert，但都在冷卻期內，不重複寄信")
        return

    print(f"觸發{len(new_alerts)}項新alert，準備寄信")
    body = call_groq_for_alerts(new_alerts)
    if not body:
        print("  Groq轉譯失敗或無金鑰，改用模板字串")
        body = fallback_template(new_alerts)

    send_email(f"股票觀察名單提醒：{len(new_alerts)}項異動（{trade_date}）", body)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001 - 讓GitHub Actions log清楚顯示失敗原因
        print(f"執行失敗：{e}", file=sys.stderr)
        raise
