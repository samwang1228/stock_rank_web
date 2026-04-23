# stock_ui（暫）

這個資料夾提供：

- Web UI（Flask）：漲幅排行、K 線、接近 MA
- CLI 腳本：簡單篩選（保留）

資料來源：
- TWSE（上市）：每日收盤行情(全部)
- TPEx（上櫃）：上櫃股票行情

## 使用方式

建議使用你的 conda 環境 Python（避免 macOS 系統 Python 的 HTTPS 憑證問題）：

```bash
/Users/wangshaocheng/anaconda3/envs/stock/bin/python stock_screener.py --help
```

## Web（Flask）

啟動：

```bash
cd /Users/wangshaocheng/Desktop/python/side_project/stock_ui
/Users/wangshaocheng/anaconda3/envs/stock/bin/python app.py
```

打開：
- http://127.0.0.1:5000/gainers
- http://127.0.0.1:5000/kline
- http://127.0.0.1:5000/near-ma

資料會存到 `data/stock.db`（SQLite）。每次啟動/第一次進頁面會：
啟動後請按導覽列的「更新DB」按鈕，才會進行：
- 自動偵測從今天往回「近 60 個交易日」是否有缺資料，缺的會補抓（抓取間有延遲避免被封）
- 自動刪掉超過 60 個交易日的舊資料

### 1) 接近 20MA / 60MA

```bash
/Users/wangshaocheng/anaconda3/envs/stock/bin/python stock_screener.py near-ma --date 2026-04-23 --threshold-pct 1.0 --top 50
```

### 2) 近 N 交易日漲幅排行

```bash
/Users/wangshaocheng/anaconda3/envs/stock/bin/python stock_screener.py gainers --date 2026-04-23 --lookback-days 5 --top 50
```

## 快取

預設會把每日 JSON 存在 `.cache/`，避免每次重跑都重新下載。
若要強制重抓：加 `--no-cache`。

## 憑證問題（若你用系統 python3）

如果你用 `python3` 遇到 `CERTIFICATE_VERIFY_FAILED`，最簡單的解法是改用 conda 環境的 Python。
在你了解風險的前提下，也可以暫時關閉驗證：

```bash
STOCK_SCREENER_INSECURE=1 python3 stock_screener.py near-ma --date 2026-04-23
```
