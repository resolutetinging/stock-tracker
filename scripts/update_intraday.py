#!/usr/bin/env python3
"""台股觀察名單追蹤器：盤中價格參考（best-effort，非官方即時報價）。

這是獨立於 update_stocks.py 的第二條管線。update_stocks.py 抓的是 TWSE 官方
OpenAPI 的「盤後」全市場快照，每天收盤後才有一次資料；使用者若想在收盤前
先看到接近即時的參考價，官方 OpenAPI 沒有這個資料，只能改抓 TWSE 自家網頁
（mis.twse.com.tw）使用的內部 AJAX 端點。

重要限制（誠實記在這裡，不要當成穩定公開API使用）：
- 這支端點不是 TWSE 正式文件化公開的 API，是網頁 www.twse.com.tw/zh/trading/
  即時報價頁面自己在用的內部端點，欄位語意沒有官方文件，隨時可能改版。
- 呼叫前必須先 GET https://mis.twse.com.tw/stock/index 建立 session cookie，
  否則 getStockInfo.jsp 會回傳空結果或失敗。
- 社群反查（見下方查證來源）指出高頻連續呼叫會被暫時封鎖，但每15-30分鐘查
  一次不會有問題，所以本腳本設計成排程每15分鐘跑一次，不在單次執行內重試
  太多次。
- 這是 best-effort 功能：任何一步失敗（連線逾時、cookie失敗、JSON格式跟預期
  不符）都只印 log 然後正常結束（exit 0），不能讓 GitHub Actions 因為這條
  邊車管線失敗而發警示——那是既有盤後管線 update_stocks.py 的職責，兩者互不
  影響。

## 欄位語意查證紀錄（2026-09-10）

查證來源：
1. 知名開源專案 twstock 原始碼 realtime.py
   (https://github.com/mlouielu/twstock/blob/master/twstock/realtime.py)
   ——其中明確把 z/tv/v/o/h/l/c/tlong/b/g/a/f/n/nf 等欄位對應到英文意義。
2. 對同一支 API 實際發送請求（2026-09-10 台北時間上午，交易日盤中）交叉核對
   twstock 的對應是否吻合真實回傳，並確認 twstock 原始碼「沒有」處理的欄位
   （例如昨收 y）在真實回傳裡長什麼樣子。

確認的欄位對照表（只列本腳本會用到的）：
| key    | 意義                     | 查證依據                                   |
|--------|--------------------------|---------------------------------------------|
| c      | 股票代號                 | twstock原始碼 result["info"]["code"]=data["c"] |
| n/nf   | 簡稱/全名                | twstock原始碼有對應但本腳本用watchlist自己的name，不採用 |
| z      | 最新成交價               | twstock原始碼 latest_trade_price=data.get("z")；實測確認 |
| y      | 昨收盤價                 | 實測回傳值（2465.0000）與台積電當時已知昨收吻合，twstock未列此欄但欄位命名與同類文件慣例一致 |
| o      | 開盤價                   | twstock原始碼 open=data.get("o")            |
| h      | 當日最高價               | twstock原始碼 high=data.get("h")            |
| l      | 當日最低價               | twstock原始碼 low=data.get("l")             |
| v      | 累積成交量（張）         | twstock原始碼 accumulate_trade_volume=data.get("v") |
| tlong  | 時間戳（epoch毫秒）      | twstock原始碼 timestamp=int(data["tlong"])/1000 |
| d, t   | 資料日期(YYYYMMDD)/時間(HH:MM:SS) | 實測回傳觀察，與tlong換算結果一致       |

**未使用、語意未查證清楚的欄位**（不使用，避免瞎猜）：pz、ps、bp、m%、mt、ip、i、it、
p、s、u（漲停價，推測但未逐一查證，不用）、w（跌停價，同上，不用）。

實測也證實：`z`（最新成交價）在盤中查詢當下常常是字串 `"-"`（不是每一tick都有新
成交、或查詢當下剛好卡在兩筆成交之間），即使累積成交量 `v` 同時在增加。這代表
「查無最新成交價」是正常會發生的情況，不是錯誤，必須容錯處理成 null，不能假設
z 一定有值。
"""
import json
import os
import sys
from datetime import datetime, timezone, timedelta

import requests

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
WATCHLIST_PATH = os.path.join(DATA_DIR, "watchlist.json")
INTRADAY_PATH = os.path.join(DATA_DIR, "intraday.json")

MIS_INDEX_URL = "https://mis.twse.com.tw/stock/index"
MIS_API_URL = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp"

TAIPEI_TZ = timezone(timedelta(hours=8))

DATA_SOURCE_NOTE = (
    "盤中參考資料來源：TWSE網站內部AJAX端點（非正式公開API，欄位語意由社群反查與"
    "實測確認，非TWSE官方文件保證）。這份資料是查詢當下的快照，僅供收盤前參考，"
    "不是券商等級的即時報價，正式數字以收盤後 stock_history.json 的官方資料為準。"
)


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


def to_float_or_none(s):
    """mis.twse欄位常見'-'（尚無資料）或空字串，一律轉None讓前端顯示查無資料"""
    if s is None:
        return None
    s = str(s).strip()
    if s in ("", "-", "--"):
        return None
    s = s.replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None


def build_mis_key(ticker, market):
    """market目前只支援tse（上市）。watchlist未來若混入otc（上櫃），
    在watchlist.json該檔股票加一個"market":"otc"欄位即可，本函式已支援。
    """
    prefix = "otc" if market == "otc" else "tse"
    return f"{prefix}_{ticker}.tw"


def fetch_cookie_session(timeout=10):
    """建立session並打一次mis.twse.com.tw/stock/index取得必要cookie。
    失敗就丟例外，由呼叫端統一當成best-effort失敗處理。
    """
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0 (personal stock watchlist tracker)"})
    resp = session.get(MIS_INDEX_URL, timeout=timeout)
    resp.raise_for_status()
    return session


def fetch_intraday_raw(session, mis_keys, timeout=10):
    ex_ch = "|".join(mis_keys)
    resp = session.get(MIS_API_URL, params={"ex_ch": ex_ch, "json": "1", "delay": "0"}, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def parse_msg_array(raw_json, valid_tickers):
    """把mis.twse回傳的msgArray轉成 {ticker: record}。
    比對方式：mis.twse回傳的"c"欄位就是純股票代號（實測confirmed，例如"2330"），
    直接拿來跟watchlist的ticker字串比對即可，不需要組合"key"欄位（key格式是
    "tse_2330.tw_20260910"這種含日期後綴的字串，比對起來反而脆弱）。
    對任何缺欄位/型別不符的情況一律容錯成None，不讓單一股票的髒資料
    讓整批解析中斷。
    """
    records = {}
    msg_array = raw_json.get("msgArray") if isinstance(raw_json, dict) else None
    if not isinstance(msg_array, list):
        return records

    for row in msg_array:
        if not isinstance(row, dict):
            continue
        ticker = row.get("c")
        if ticker not in valid_tickers:
            continue

        last_price = to_float_or_none(row.get("z"))
        prev_close = to_float_or_none(row.get("y"))
        change_pct = None
        if last_price is not None and prev_close not in (None, 0):
            change_pct = round((last_price - prev_close) / prev_close * 100, 2)

        data_date = row.get("d")
        data_time = row.get("t")  # 只用已查證的t欄位，"%"欄位語意未查證不採用
        tlong = row.get("tlong")
        timestamp_iso = None
        if tlong:
            try:
                ts_seconds = int(tlong) / 1000
                timestamp_iso = datetime.fromtimestamp(ts_seconds, TAIPEI_TZ).isoformat()
            except (ValueError, TypeError, OverflowError):
                timestamp_iso = None

        records[ticker] = {
            "last_price": last_price,
            "prev_close": prev_close,
            "change_pct": change_pct,
            "day_high": to_float_or_none(row.get("h")),
            "day_low": to_float_or_none(row.get("l")),
            "day_open": to_float_or_none(row.get("o")),
            "accumulated_volume": to_float_or_none(row.get("v")),
            "data_date": data_date,
            "data_time": data_time,
            "timestamp": timestamp_iso,
        }
    return records


def build_ticker_keys(active_stocks):
    """回傳 (mis_keys list, valid_tickers set)。
    未指定market一律當tse（目前watchlist全部是上市股票）；若未來watchlist
    混入上櫃股票，在該檔股票物件加上"market":"otc"即可，不需要改這支腳本。
    """
    mis_keys = []
    valid_tickers = set()
    for stock in active_stocks:
        ticker = stock["ticker"]
        market = stock.get("market", "tse")
        if market not in ("tse", "otc"):
            market = "tse"
        mis_keys.append(build_mis_key(ticker, market))
        valid_tickers.add(ticker)
    return mis_keys, valid_tickers


def run():
    watchlist = load_json(WATCHLIST_PATH, {"stocks": []})
    active_stocks = [s for s in watchlist.get("stocks", []) if s.get("active", True)]
    if not active_stocks:
        print("watchlist沒有啟用中的股票，略過盤中更新")
        return

    mis_keys, valid_tickers = build_ticker_keys(active_stocks)
    print(f"查詢盤中資料：{mis_keys}")

    session = fetch_cookie_session()
    raw = fetch_intraday_raw(session, mis_keys)
    records = parse_msg_array(raw, valid_tickers)

    if not records:
        print("盤中API沒有回傳任何可用資料（可能非交易時間或格式改版），略過寫檔")
        return

    output = {
        "generated_at": datetime.now(TAIPEI_TZ).isoformat(),
        "source_note": DATA_SOURCE_NOTE,
        "stocks": records,
    }
    save_json(INTRADAY_PATH, output)
    print(f"✓ intraday.json 已更新，{len(records)}檔股票")


def main():
    try:
        run()
    except Exception as e:  # noqa: BLE001 - best-effort邊車功能，任何失敗都不影響既有盤後管線
        print(f"盤中更新失敗（已忽略，不影響既有排程）：{e}", file=sys.stderr)


if __name__ == "__main__":
    main()
    sys.exit(0)
