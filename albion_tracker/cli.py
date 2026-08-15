from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from uuid import uuid4

from .db import Database, ValidationError, utc_now
from .server import run_server


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = PROJECT_ROOT / "data" / "albion-purchases.sqlite3"
DEFAULT_WEB = PROJECT_ROOT / "web"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Albion Online 本機市場成交紀錄與統計")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="SQLite 資料庫路徑")
    commands = parser.add_subparsers(dest="command", required=True)

    serve = commands.add_parser("serve", help="啟動事件 API 與統計頁面")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--web-root", type=Path, default=DEFAULT_WEB)

    listing = commands.add_parser("list", help="列出最近成交")
    listing.add_argument("--limit", type=int, default=20)

    summary = commands.add_parser("summary", help="輸出統計 JSON")
    summary.add_argument("--pretty", action="store_true")

    export = commands.add_parser("export", help="匯出 CSV")
    export.add_argument("path", type=Path)

    manual = commands.add_parser("add", help="手動補登一筆成交")
    manual.add_argument("item_id")
    manual.add_argument("quantity", type=int)
    manual.add_argument("unit_price", type=int)
    manual.add_argument("--item-name")
    manual.add_argument("--location")
    manual.add_argument("--character")
    manual.add_argument("--direction", choices=("buy", "sell"), default="buy")
    manual.add_argument("--kind", choices=("instant", "order"), default="instant")

    ingest = commands.add_parser("import-events", help="匯入捕捉器離線佇列 JSONL")
    ingest.add_argument("path", type=Path)
    return parser


def run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    database = Database(args.db)

    if args.command == "serve":
        run_server(database, args.host, args.port, args.web_root)
        return 0

    if args.command == "list":
        print(json.dumps(database.list_transactions(limit=args.limit), ensure_ascii=False, indent=2))
        return 0

    if args.command == "summary":
        print(json.dumps(database.summary(), ensure_ascii=False, indent=2 if args.pretty else None))
        return 0

    if args.command == "export":
        args.path.parent.mkdir(parents=True, exist_ok=True)
        args.path.write_text("\ufeff" + database.export_csv(), encoding="utf-8", newline="")
        print(f"已匯出：{args.path.resolve()}")
        return 0

    if args.command == "add":
        inserted, row_id = database.insert_transaction(
            {
                "type": "purchase",
                "source_event_id": f"manual:{uuid4()}",
                "purchased_at": utc_now(),
                "item_id": args.item_id,
                "item_name": args.item_name,
                "quantity": args.quantity,
                "unit_price": args.unit_price,
                "location_id": args.location,
                "character_name": args.character,
                "source": "manual",
                "confidence": "manual",
                "direction": args.direction,
                "transaction_kind": args.kind,
            }
        )
        print(f"已新增 #{row_id}" if inserted else f"已存在 #{row_id}")
        return 0

    if args.command == "import-events":
        inserted = duplicates = ignored = 0
        for line_number, line in enumerate(args.path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
                if event.get("type") in {"purchase", "sale", "transaction"}:
                    created, _ = database.insert_transaction(event)
                    inserted += int(created)
                    duplicates += int(not created)
                elif event.get("type") == "capture_warning":
                    database.insert_warning(event)
                else:
                    ignored += 1
            except (json.JSONDecodeError, ValidationError) as error:
                print(f"第 {line_number} 行略過：{error}", file=sys.stderr)
                ignored += 1
        print(f"新增 {inserted}、重複 {duplicates}、略過 {ignored}")
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(run())
