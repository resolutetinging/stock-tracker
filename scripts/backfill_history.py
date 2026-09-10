#!/usr/bin/env python3
"""一次性歷史資料回補工具：既有的daily排程(update_stocks.py)只能從上線當天開始
逐日累積stock_history.json，完全沒有回溯能力。這支腳本改呼叫TWSE另一組「單一股票、
按月查詢」的官方歷史端點，把過去N個月的收盤價/本益比/殖利率/淨值比補進同一份
stock_history.json，讓前端一上線就能看3個月/半年趨勢。

手動執行，非排程工具：
    python3 scripts/backfill_history.py --months 6

已查證事項（2026-09-10實測，見交辦紀錄）：
(a) 虧損公司的本益比在BWIBBU可能顯示為單一個"-"字元（不是update_stocks.py既有
    to_float()原本設想的""或"--"），但float("-")一樣會丟ValueError被to_float()的
    except接住回傳None，行為已經正確，沿用既有to_float()不用改。
(b) 這兩支端點(www.twse.com.tw/rwd/zh/afterTrading/...)不需要先建立session cookie，
    純GET+User-Agent header即可直接拿到200與合法JSON，跟需要cookie的
    mis.twse.com.tw不同、也跟既有openapi.twse.com.tw不同源但一樣是直接可打。
(c) 實測連續8次請求（間隔0.3秒）皆為200無rate limit跡象，但9檔股票×N個月×2支API
    仍有一定量請求，保守起見預設請求間隔拉到1.5秒，並對429/403做重試退避，避免
    一次性大量請求被TWSE暫時封鎖。

⚠️ 與原始交辦假設不同之處（已依CLAUDE.md鐵律指出矛盾，不是自己選一邊）：
    原本認為STOCK_DAY跟BWIBBU兩支歷史端點日期格式都是"115/08/03"斜線格式。
    實測發現只有STOCK_DAY是"115/08/03"（斜線），BWIBBU卻是"115年08月03日"
    （民國年月日，中文字分隔），兩者不同，因此下面寫了兩個獨立的日期轉換函式，
    不可共用、也不可套用update_stocks.py既有處理"1150909"純數字格式的
    roc_date_to_iso()。
"""
import argparse
import os
import re
import sys
import time
from datetime import date

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from update_stocks import (  # noqa: E402
    HISTORY_PATH,
    WATCHLIST_PATH,
    load_json,
    save_json,
    to_float,
    upsert_history,
)

STOCK_DAY_URL = "https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY"
BWIBBU_URL = "https://www.twse.com.tw/rwd/zh/afterTrading/BWIBBU"

REQUEST_INTERVAL_SEC = 1.5  # 保守節流，見上方查證(c)
MAX_RETRIES = 3
RETRY_BACKOFF_SEC = 5


def roc_slash_date_to_iso(date_str):
    """STOCK_DAY歷史端點的日期格式："115/08/03"（民國年/月/日，斜線分隔）"""
    date_str = str(date_str).strip()
    parts = date_str.split("/")
    if len(parts) != 3:
        return None
    try:
        roc_year, month, day = int(parts[0]), int(parts[1]), int(parts[2])
    except ValueError:
        return None
    return f"{1911 + roc_year:04d}-{month:02d}-{day:02d}"


def roc_zh_date_to_iso(date_str):
    """BWIBBU歷史端點的日期格式："115年08月03日"（民國年月日，中文字分隔）
    跟STOCK_DAY的斜線格式不同，兩支API各自實測確認過，見檔頭說明。"""
    date_str = str(date_str).strip()
    m = re.match(r"^(\d+)年(\d+)月(\d+)日$", date_str)
    if not m:
        return None
    roc_year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
    return f"{1911 + roc_year:04d}-{month:02d}-{day:02d}"


def month_starts(n_months):
    """回傳從當月往前推n_months個月（含當月）的每月1號日期清單，當月在最前面"""
    today = date.today()
    year, month = today.year, today.month
    result = []
    for i in range(n_months):
        y, m = year, month - i
        while m <= 0:
            m += 12
            y -= 1
        result.append(date(y, m, 1))
    return result


def fetch_month_json(url, retries=MAX_RETRIES):
    """打單一TWSE歷史端點，429/403退避重試；其餘錯誤也重試，用完仍失敗就丟例外
    交給呼叫端捕捉（讓單一月份/單一股票失敗不影響其他組合繼續跑）"""
    headers = {"User-Agent": "Mozilla/5.0 (personal stock watchlist tracker - backfill)"}
    last_err = None
    for attempt in range(retries):
        try:
            resp = requests.get(url, headers=headers, timeout=15)
            if resp.status_code in (429, 403):
                wait = RETRY_BACKOFF_SEC * (attempt + 1)
                print(f"    ⚠ HTTP {resp.status_code}，等待{wait}秒後重試（第{attempt + 1}次）")
                time.sleep(wait)
                last_err = f"HTTP {resp.status_code}"
                continue
            resp.raise_for_status()
            return resp.json()
        except Exception as e:  # noqa: BLE001 - 單純重試用，記下最後一次錯誤即可
            last_err = e
            if attempt < retries - 1:
                time.sleep(RETRY_BACKOFF_SEC)
    raise RuntimeError(f"抓取失敗（重試{retries}次）：{url}：{last_err}")


def parse_stock_day(day_data, ticker, label):
    """回傳{iso_date: {close, change_pct, volume}}；stat非OK視為當月無資料，回傳空dict"""
    if day_data.get("stat") != "OK":
        print(f"    ⚠ STOCK_DAY無資料：{label}（{day_data.get('stat')}）")
        return {}
    result = {}
    for row in day_data.get("data", []):
        iso = roc_slash_date_to_iso(row[0])
        if not iso:
            continue
        close = to_float(row[6])
        change = to_float(row[7])
        change_pct = None
        if close is not None and change is not None and (close - change) != 0:
            change_pct = round(change / (close - change) * 100, 2)
        result[iso] = {
            "close": close,
            "change_pct": change_pct,
            "volume": to_float(row[1]),
        }
    return result


def parse_bwibbu(bwibbu_data, ticker, label):
    """回傳{iso_date: {pe_ratio, dividend_yield, pb_ratio}}；stat非OK視為當月無資料"""
    if bwibbu_data.get("stat") != "OK":
        print(f"    ⚠ BWIBBU無資料：{label}（{bwibbu_data.get('stat')}）")
        return {}
    result = {}
    for row in bwibbu_data.get("data", []):
        iso = roc_zh_date_to_iso(row[0])
        if not iso:
            continue
        result[iso] = {
            "dividend_yield": to_float(row[1]),
            "pe_ratio": to_float(row[3]),
            "pb_ratio": to_float(row[4]),
        }
    return result


def build_month_records(ticker, month_date):
    """抓單一ticker單一月份的STOCK_DAY+BWIBBU，合併成跟upsert_history()一致的record清單"""
    yyyymmdd = month_date.strftime("%Y%m01")
    label = f"{ticker} {month_date.strftime('%Y-%m')}"

    day_url = f"{STOCK_DAY_URL}?date={yyyymmdd}&stockNo={ticker}&response=json"
    day_data = fetch_month_json(day_url)
    time.sleep(REQUEST_INTERVAL_SEC)

    bwibbu_url = f"{BWIBBU_URL}?date={yyyymmdd}&stockNo={ticker}&response=json"
    bwibbu_data = fetch_month_json(bwibbu_url)
    time.sleep(REQUEST_INTERVAL_SEC)

    day_by_date = parse_stock_day(day_data, ticker, label)
    bwibbu_by_date = parse_bwibbu(bwibbu_data, ticker, label)

    records = []
    for iso, day_rec in day_by_date.items():
        bwibbu_rec = bwibbu_by_date.get(iso, {})
        records.append({
            "date": iso,
            "close": day_rec["close"],
            "change_pct": day_rec["change_pct"],
            "volume": day_rec["volume"],
            "pe_ratio": bwibbu_rec.get("pe_ratio"),
            "dividend_yield": bwibbu_rec.get("dividend_yield"),
            "pb_ratio": bwibbu_rec.get("pb_ratio"),
        })
    return records


def main():
    parser = argparse.ArgumentParser(description="回補stock_history.json過去N個月的歷史資料")
    parser.add_argument("--months", type=int, default=6, help="往前回補幾個月（含當月），預設6")
    args = parser.parse_args()

    watchlist = load_json(WATCHLIST_PATH, {"stocks": []})
    tickers = [s["ticker"] for s in watchlist.get("stocks", []) if s.get("active", True)]
    if not tickers:
        print("watchlist沒有啟用中的股票，結束")
        return

    months = month_starts(args.months)
    print(f"回補範圍：{len(tickers)}檔股票 × {len(months)}個月"
          f"（{months[-1].strftime('%Y-%m')} ~ {months[0].strftime('%Y-%m')}）")
    print(f"股票清單：{tickers}")

    history = load_json(HISTORY_PATH, {})
    success_count = 0
    fail_count = 0
    fail_details = []

    for ticker in tickers:
        for month_date in months:
            label = f"{ticker} {month_date.strftime('%Y-%m')}"
            try:
                records = build_month_records(ticker, month_date)
                for rec in records:
                    upsert_history(history, ticker, rec)
                success_count += 1
                print(f"  ✓ {label}：{len(records)}筆交易日")
            except Exception as e:  # noqa: BLE001 - 單一組合失敗不能讓整個回補中止
                fail_count += 1
                fail_details.append(f"{label}：{e}")
                print(f"  ✗ {label} 失敗：{e}")
                continue
        # 每跑完一檔股票的所有月份就存一次檔：全部54組合要跑數分鐘，
        # 中途網路異常或撞到未知節流限制時，已完成的進度不會整批遺失
        save_json(HISTORY_PATH, history)

    save_json(HISTORY_PATH, history)

    print(f"\n回補完成：成功 {success_count} 筆（ticker×月份組合）/ 失敗 {fail_count} 筆")
    if fail_details:
        print("失敗清單：")
        for d in fail_details:
            print(f"  - {d}")


if __name__ == "__main__":
    main()
