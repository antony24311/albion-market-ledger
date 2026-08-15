# Albion 市場帳本

這是一套完全在本機執行的 Albion Online 市場成交紀錄工具。它只讀取本機網路封包，不操作遊戲、不注入程式、不讀取遊戲記憶體，也不向 Albion 市場伺服器送出交易資料。

目前會記錄：

- 市場立即購入（`AuctionBuyOffer`）、指定品項出售（`AuctionSellSpecificItemRequest`）與快速出售（`QuickSellAuction*`）。
- 買單與賣單完成／到期後已成交的數量：在遊戲實際送出 `ReadMail` 時記錄。
- 封包價格會從 1/10,000 銀幣單位正規化；舊版立即購入資料會在資料庫升級時自動修正。
- 品項以官方繁體中文名稱顯示並快取在本機；手動補登可用圖片、類型與階級直接點選，不必記品項代碼。
- 主要城市以玩家常用中文名稱加英文原名顯示，市場估價會標明伺服器、城市與資料時間，也可自行切換市場。
- 每三分鐘保存一筆收支快照；同一成交可分配到多個專案，但跨專案總量不會超額。
- 製造／精煉專案會依成品、附魔與起始階級自動展開完整配方；例如 7.1 皮革從 5.1 開始時，會分別計算 5.1 皮革、6.1 粗皮與 7.1 粗皮需求。
- 物品名稱、代碼與圖片由內建目錄自動帶入；畫面分開顯示開工毛投入與保守預估淨耗用。實際材料返還會按遊戲批次規則取整。
- 專注不足時優先分配到較高階製作；基礎專注消耗會自動帶入，也可依角色專精後的遊戲顯示值逐階覆寫。
- 原料與成品價格可手動輸入，也可向 Albion Online Data Project 取得最近市場賣價；即時價格缺少時改用最近 14 個有成交日的加權均價。多筆購入會依實際分配數量與各筆淨成本計算加權平均。
- 原料轉換比較器同時比較「低一階同附魔」、「同階升附魔」及直接購買，例如 5.1→6.1、6.0→6.1 與直購 6.1。銀幣費預填官方基準並允許依製作台畫面覆寫；比較時可引用帳本成本而不扣庫存，建立專案時才依需求分配並扣除可用數量。
- 材料表會合併帳本分配與使用者輸入的儲物箱庫存，顯示 `已有／開工需備` 和尚缺數量；也能依任一材料的持有量反推成品與其他材料需求。
- 儲存專案時若未命名，會預設使用「目標成品 × 數量」；也可自動切割相符的購入紀錄，多個專案的分配總和仍不會超過原成交數量。私下交易可用總金額與數量自動換算單價，無需封包。
- 掛單成交自動套用可調整的交易稅與設定費，分別顯示成交毛額、實收／實付與專案淨利。
- 市場信件先建立待驗證紀錄，讀取內容後再區分成交、部分成交、零成交或解析失敗；非市場郵件不會進入同步清單。
- 成交可手動補記、修改、標記已售出或軟刪除；上方統計可切換近一天、近一週、近一個月、近一年及全部。
- 捕捉器與遊戲流量狀態。
- SQLite 永久保存、中文網頁統計、CSV 匯出與命令列查詢。
- API 暫時離線時，先寫入 JSONL 佇列，連線恢復後自動補送。
- 事件 ID 去重，避免同一個封包被多張網卡捕捉而重複入帳。

## 架構

```text
Albion Online (TCP/UDP 5056)
          │ 只讀封包
          ▼
albion-capture (Go + AODP Photon parser)
          │ localhost HTTP / 離線 JSONL
          ▼
albion_tracker (Python 標準函式庫)
          │
          ├── data/albion-purchases.sqlite3
          ├── http://127.0.0.1:8765 統計頁面
          └── CSV / JSON 查詢
```

捕捉器會分別快取市場的 sell offer 與 buy request。立即買賣時，它以訂單 ID 找回品項與單價並等待成功回應；失敗或解析不完整的操作不會寫成成交。訂單成交則由市場郵件確認。

計算依據：[Albion Online Wiki：Resource return rate](https://wiki.albiononline.com/wiki/Resource_return_rate)、[Crafting Focus](https://wiki.albiononline.com/wiki/Crafting_Focus)、[Transmuting](https://wiki.albiononline.com/wiki/Transmuting)，配方代碼取自目前遊戲資料的 [ao-bin-dumps](https://github.com/ao-data/ao-bin-dumps)。市場價格端點依照 [Albion Online Data Project API](https://github.com/ao-data/albion-data-website/blob/master/api-info/api-info.md)。

## 下載

可以在 GitHub 按 **Code → Download ZIP**，解壓縮後進入資料夾；或使用 Git：

```bash
git clone https://github.com/antony24311/albion-market-ledger.git
cd albion-market-ledger
```

程式後端只使用 Python 標準函式庫，不需要執行 `pip install`。Windows 與 macOS 已附捕捉器；Linux 需在本機建置。

## Windows 10／11

1. 安裝 [Python 3.10 以上](https://www.python.org/downloads/windows/)，安裝時勾選 **Add Python to PATH**。
2. 安裝 [Npcap](https://npcap.com/windows-10)，勾選 **WinPcap API-compatible Mode**。
3. 在專案資料夾空白處按 Shift＋滑鼠右鍵，選擇「在終端機中開啟」，執行：


```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\start-windows.ps1
```

腳本會啟動統計頁面，並要求系統管理員權限來讀取本機網路封包。看到瀏覽器開啟 `http://127.0.0.1:8765` 後即可啟動 Albion。

若 Windows SmartScreen 阻擋捕捉器，可檢查 `bin\SHA256SUMS.txt` 後選擇「其他資訊 → 仍要執行」。若想自行重建，先安裝 Go 1.24 以上，再執行：

```powershell
.\scripts\build-windows.ps1
```

## macOS（Apple Silicon／Intel）

1. 確認 `python3 --version` 為 3.10 以上；若沒有，可從 [python.org](https://www.python.org/downloads/macos/) 安裝。
2. 在「終端機」進入專案資料夾後執行：

```bash
chmod +x scripts/start-macos.sh bin/albion-capture-macos-*
./scripts/start-macos.sh
```

啟動腳本會自動選擇 Apple Silicon (`arm64`) 或 Intel (`amd64`) 版本。macOS 會要求管理員密碼，因為開啟 BPF 網路介面需要權限；接著自行開啟 <http://127.0.0.1:8765>。

若 macOS 顯示檔案來自未識別的開發者，可在「系統設定 → 隱私權與安全性」允許，或自行安裝 Go 1.24 以上後重建：

```bash
./scripts/build-macos.sh
```

## Linux

Linux 沒有附預先建置的捕捉器。先安裝 Python 3.10 以上、Go 1.24 以上、C 編譯器與 libpcap headers。Debian／Ubuntu 可執行：

```bash
sudo apt update
sudo apt install python3 build-essential libpcap-dev
# Go 版本若低於 1.24，請改用 https://go.dev/dl/ 的官方版本。
```

接著在專案資料夾執行：

```bash
chmod +x scripts/build-linux.sh scripts/start-linux.sh
./scripts/build-linux.sh
./scripts/start-linux.sh
```

輸入 sudo 密碼後，開啟 <http://127.0.0.1:8765>。Fedora/RHEL 的 libpcap 開發套件名稱為 `libpcap-devel`，Arch Linux 為 `libpcap`。

所有平台的帳本都儲存在 `data/albion-purchases.sqlite3`。更新程式前可先複製這個檔案備份；GitHub 不會上傳此本機資料。

## 使用統計與資料

統計頁面：<http://127.0.0.1:8765>

SQLite 檔案預設在 `data/albion-purchases.sqlite3`。統一帳本資料表 `transactions` 的主要欄位如下：

| 欄位 | 意義 |
|---|---|
| `traded_at` | 成交捕捉時間（UTC） |
| `direction` | `buy` 或 `sell` |
| `transaction_kind` | `instant` 或 `order` |
| `item_id` | Albion 內部品項 ID |
| `quantity` | 成交數量 |
| `unit_price` | 每件銀幣價格 |
| `total_price` | 成交毛額；信箱成交保留遊戲回報的精確總額，可能因單價四捨五入而不完全等於數量乘單價 |
| `sales_tax` / `setup_fee` | 交易稅與掛單設定費 |
| `net_total` | 扣／加費用後的實收或實付 |
| `mail_id` | 用於市場信件交叉驗證的信件 ID |
| `order_id` | 市場訂單或 mail ID |
| `location_id` | 市場地點 |
| `character_name` | 當時角色 |
| `quality_level` / `enchantment_level` | 品質與附魔 |
| `source` | `auction_buy_offer`、`auction_sell_request`、`quick_sell`、`buy_order_mail`、`sell_order_mail` 或 `manual_entry` |
| `confidence` | `confirmed`、`inferred` 或 `manual` |

常用命令：

```bash
# 最近 20 筆
python3 -m albion_tracker list --limit 20

# 統計 JSON
python3 -m albion_tracker summary --pretty

# 匯出 Excel 可開啟的 UTF-8 CSV
python3 -m albion_tracker export exports/purchases.csv

# 手動補登：品項、數量、單價（可加 --direction sell --kind order）
python3 -m albion_tracker add T4_BAG 3 1250 --location 3005

# 手動匯入離線佇列
python3 -m albion_tracker import-events data/capture-spool.jsonl
```

也可以直接用 SQLite：

```sql
SELECT item_id,
       SUM(quantity) AS quantity,
       SUM(total_price) AS spent
FROM transactions
WHERE direction = 'buy'
GROUP BY item_id
ORDER BY spent DESC;
```

## 準確度與限制

- 立即購買必須先收到同一張市場 sell offer 清單。正常從市場畫面購買時會自然滿足；若清單封包解析失敗，該筆會顯示警告而不入帳。
- 買單與賣單成交是透過遊戲郵件確認。下線期間完成的掛單會在下次登入、載入郵件資訊並讀取該封郵件後補記；若遊戲從未傳送該郵件內容，被動捕捉器無法得知價格，可在網頁用「手動補記」補齊。
- `GetMailInfos` 只包含郵件 ID、類型、地點與時間；品項、數量和成交價格只存在 `ReadMail` 內容。工具不會主動向遊戲偽造讀信請求，因此同步中心會列出真正需要開啟的市場郵件並排除一般信件。
- 買單「到期但部分成交」的單價由剩餘退款反推，因此標為 `inferred`。
- 不會從封包自動追蹤玩家間交易、NPC 商店、製作／修理費或金幣市場；玩家間交易與儲物箱庫存可手動補記。
- 首次遇到新品項時會向 Albion Game Info 查詢繁體中文名稱並只在本機快取；離線時先使用內建中文類型名稱或品項 ID。
- Albion 更新可能改變操作碼或封包格式。目前預設值包含 offers `81`、requests `82`、buy `83`、舊 sell `88`、指定品項 sell `315`、mail infos `174`、read mail `176`、quick-sell query/action `484/485`。捕捉器所有代碼都可用參數覆寫，例如 `-sell-specific-op 315`。
- 若頁面顯示「市場資料已加密」，工具不會嘗試從遊戲記憶體取金鑰或繞過加密；這是刻意的安全／合規邊界。

## 故障排除

列出網路介面：

```bash
# macOS Apple Silicon；Intel 請改用 albion-capture-macos-amd64
bin/albion-capture-macos-arm64 -list-devices
```

只監聽指定介面：

```bash
bin/albion-capture-macos-arm64 -devices "en0"
```

Windows 若出現 `couldn't load wpcap.dll`，請重新安裝 Npcap 並啟用 WinPcap 相容模式。若捕捉器在線但一直顯示等待遊戲，確認它有系統管理員／root 權限，並檢查 Albion 是否仍使用 port 5056。

## 測試

```bash
python3 -m unittest discover -v
go test ./...
go vet ./...
```

## 安全與授權提醒

本工具只做被動讀取。SBI 過去的公開說明將 AODP 這類「只讀網路封包、沒有遊戲內 overlay、沒有即時 PvE/PvP 優勢」與作弊工具區分；但遊戲規則與官方立場可能改變，使用前仍應自行查閱最新規則。參考：[SBI 第三方工具與網路流量說明](https://forum.albiononline.com/index.php/Thread/124819-Regarding-3rd-Party-Software-and-Network-Traffic-aka-do-not-cheat-Update-16-45-U/)、[AODP client](https://west.albion-online-data.com/client)。

程式採 MIT License。Photon Protocol18 解析器透過 Go module 使用 [ao-data/albiondata-client](https://github.com/ao-data/albiondata-client)（MIT）；封包捕捉使用 gopacket（BSD）。
