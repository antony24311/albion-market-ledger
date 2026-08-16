from __future__ import annotations

import csv
import io
import json
import math
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from .crafting import CraftingValidationError, build_refining_plan
from .localization import fallback_item_name, item_category, location_name

SCHEMA_VERSION = 10
DIRECTIONS = {"buy", "sell"}
KINDS = {"instant", "order"}
STATUSES = {"active", "sold"}
PERIODS = {"day": 1, "week": 7, "month": 30, "year": 365}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_datetime(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def _snapshot_bounds(value: str) -> tuple[str, str]:
    stamp = _parse_datetime(value)
    start = stamp.replace(second=0, microsecond=0, minute=(stamp.minute // 3) * 3)
    end = start + timedelta(minutes=3)
    return tuple(x.isoformat(timespec="seconds").replace("+00:00", "Z") for x in (start, end))


class ValidationError(ValueError):
    pass


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=10000")
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _add_column(db: sqlite3.Connection, table: str, definition: str) -> None:
        if definition.split()[0] not in {r["name"] for r in db.execute(f"PRAGMA table_info({table})")}:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")

    def initialize(self) -> None:
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS purchases(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,source_event_id TEXT NOT NULL UNIQUE,
                    purchased_at TEXT NOT NULL,captured_at TEXT NOT NULL,item_id TEXT NOT NULL,item_name TEXT,
                    quantity INTEGER NOT NULL,unit_price INTEGER NOT NULL,total_price INTEGER NOT NULL,
                    order_id TEXT,location_id TEXT,character_name TEXT,game_server TEXT,quality_level INTEGER,
                    enchantment_level INTEGER,source TEXT NOT NULL,confidence TEXT NOT NULL DEFAULT 'confirmed',raw_event TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS transactions(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,source_event_id TEXT NOT NULL UNIQUE,
                    traded_at TEXT NOT NULL,captured_at TEXT NOT NULL,
                    direction TEXT NOT NULL CHECK(direction IN('buy','sell')),
                    transaction_kind TEXT NOT NULL CHECK(transaction_kind IN('instant','order')),
                    item_id TEXT NOT NULL,item_name TEXT,item_category TEXT NOT NULL,
                    quantity INTEGER NOT NULL CHECK(quantity>0),unit_price INTEGER NOT NULL CHECK(unit_price>=0),
                    total_price INTEGER NOT NULL CHECK(total_price>=0),order_id TEXT,location_id TEXT,
                    character_name TEXT,game_server TEXT,quality_level INTEGER,enchantment_level INTEGER,
                    source TEXT NOT NULL,confidence TEXT NOT NULL DEFAULT 'confirmed',raw_event TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',notes TEXT,updated_at TEXT,deleted_at TEXT,
                    sales_tax_rate REAL NOT NULL DEFAULT 0,setup_fee_rate REAL NOT NULL DEFAULT 0,
                    sales_tax INTEGER NOT NULL DEFAULT 0,setup_fee INTEGER NOT NULL DEFAULT 0,
                    net_total INTEGER NOT NULL DEFAULT 0,mail_id INTEGER);
                CREATE INDEX IF NOT EXISTS idx_transactions_traded_at ON transactions(traded_at DESC);
                CREATE INDEX IF NOT EXISTS idx_transactions_item ON transactions(item_id,traded_at DESC);
                CREATE INDEX IF NOT EXISTS idx_transactions_type ON transactions(direction,transaction_kind,traded_at DESC);
                CREATE TABLE IF NOT EXISTS cost_snapshots(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,period_start TEXT NOT NULL UNIQUE,period_end TEXT NOT NULL,
                    updated_at TEXT NOT NULL,transaction_count INTEGER NOT NULL DEFAULT 0,
                    buy_quantity INTEGER NOT NULL DEFAULT 0,sell_quantity INTEGER NOT NULL DEFAULT 0,
                    spent INTEGER NOT NULL DEFAULT 0,revenue INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS cost_snapshot_items(
                    snapshot_id INTEGER NOT NULL REFERENCES cost_snapshots(id) ON DELETE CASCADE,item_id TEXT NOT NULL,
                    direction TEXT NOT NULL,transaction_kind TEXT NOT NULL,quantity INTEGER NOT NULL DEFAULT 0,
                    total_price INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(snapshot_id,item_id,direction,transaction_kind));
                CREATE TABLE IF NOT EXISTS projects(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT,
                    project_type TEXT NOT NULL DEFAULT 'trade',input_item_id TEXT,output_item_id TEXT,output_item_name TEXT,
                    return_rate REAL NOT NULL DEFAULT 36.7,focus_return_rate REAL NOT NULL DEFAULT 53.9,
                    use_focus INTEGER NOT NULL DEFAULT 0,material_per_unit REAL NOT NULL DEFAULT 1,
                    output_per_craft REAL NOT NULL DEFAULT 1,extra_cost INTEGER NOT NULL DEFAULT 0,
                    sale_fee_rate REAL NOT NULL DEFAULT 0,target_sale_price INTEGER NOT NULL DEFAULT 0,notes TEXT,
                    target_output INTEGER NOT NULL DEFAULT 0,available_focus INTEGER NOT NULL DEFAULT 0,
                    focus_cost_per_craft INTEGER NOT NULL DEFAULT 0,
                    planning_mode TEXT NOT NULL DEFAULT 'p95',planner_state TEXT);
                CREATE TABLE IF NOT EXISTS project_items(
                    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                    transaction_id INTEGER NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
                    selected_quantity INTEGER NOT NULL CHECK(selected_quantity>0),PRIMARY KEY(project_id,transaction_id));
                CREATE INDEX IF NOT EXISTS idx_project_items_transaction ON project_items(transaction_id);
                CREATE TABLE IF NOT EXISTS project_materials(
                    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                    item_id TEXT NOT NULL,item_name TEXT,quantity_per_craft REAL NOT NULL CHECK(quantity_per_craft>0),
                    PRIMARY KEY(project_id,item_id));
                CREATE TABLE IF NOT EXISTS market_mails(
                    mail_id INTEGER PRIMARY KEY,mail_type TEXT NOT NULL,received_raw INTEGER NOT NULL DEFAULT 0,
                    received_at TEXT,location_id TEXT,content TEXT,state TEXT NOT NULL DEFAULT 'pending',
                    transaction_id INTEGER REFERENCES transactions(id),captured_at TEXT NOT NULL,updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS app_settings(key TEXT PRIMARY KEY,value TEXT NOT NULL,updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS item_catalog(item_id TEXT PRIMARY KEY,name_zh_tw TEXT NOT NULL,updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS capture_status(
                    client_id TEXT PRIMARY KEY,updated_at TEXT NOT NULL,state TEXT NOT NULL,last_packet_at TEXT,
                    packets_seen INTEGER NOT NULL DEFAULT 0,location_id TEXT,character_name TEXT,message TEXT,version TEXT);
                CREATE TABLE IF NOT EXISTS capture_warnings(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,source_event_id TEXT UNIQUE,captured_at TEXT NOT NULL,
                    code TEXT NOT NULL,message TEXT NOT NULL,raw_event TEXT NOT NULL);
            """)
            for col in ("status TEXT NOT NULL DEFAULT 'active'", "notes TEXT", "updated_at TEXT", "deleted_at TEXT",
                        "sales_tax_rate REAL NOT NULL DEFAULT 0", "setup_fee_rate REAL NOT NULL DEFAULT 0",
                        "sales_tax INTEGER NOT NULL DEFAULT 0", "setup_fee INTEGER NOT NULL DEFAULT 0",
                        "net_total INTEGER NOT NULL DEFAULT 0", "mail_id INTEGER"):
                self._add_column(db, "transactions", col)
            project_cols = (
                "updated_at TEXT", "project_type TEXT NOT NULL DEFAULT 'trade'", "input_item_id TEXT",
                "output_item_id TEXT", "output_item_name TEXT", "return_rate REAL NOT NULL DEFAULT 36.7",
                "focus_return_rate REAL NOT NULL DEFAULT 53.9", "use_focus INTEGER NOT NULL DEFAULT 0",
                "material_per_unit REAL NOT NULL DEFAULT 1", "output_per_craft REAL NOT NULL DEFAULT 1",
                "extra_cost INTEGER NOT NULL DEFAULT 0", "sale_fee_rate REAL NOT NULL DEFAULT 0",
                "target_sale_price INTEGER NOT NULL DEFAULT 0", "notes TEXT",
                "target_output INTEGER NOT NULL DEFAULT 0", "available_focus INTEGER NOT NULL DEFAULT 0",
                "focus_cost_per_craft INTEGER NOT NULL DEFAULT 0", "planning_mode TEXT NOT NULL DEFAULT 'p95'",
                "planner_state TEXT")
            for col in project_cols:
                self._add_column(db, "projects", col)
            self._migrate_purchases(db)
            db.execute("UPDATE transactions SET net_total=total_price WHERE net_total=0 AND total_price>0")
            db.execute("UPDATE market_mails SET state='completed' WHERE state='verified'")
            for key,value in (("market_tax_rate","4"),("setup_fee_rate","2.5")):
                db.execute("INSERT OR IGNORE INTO app_settings VALUES(?,?,?)",(key,value,utc_now()))
            self._repair_legacy_allocations(db)
            self._rebuild_snapshots(db)
            db.execute("INSERT OR REPLACE INTO metadata VALUES('schema_version',?)", (str(SCHEMA_VERSION),))

    @staticmethod
    def _required_text(value: dict[str, Any], field: str) -> str:
        result = value.get(field)
        if not isinstance(result, str) or not result.strip():
            raise ValidationError(f"{field} 必須是非空字串")
        return result.strip()

    @staticmethod
    def _optional_text(value: dict[str, Any], field: str) -> str | None:
        result = value.get(field)
        return None if result is None or str(result).strip() == "" else str(result).strip()

    @staticmethod
    def _integer(value: dict[str, Any], field: str, minimum: int = 0) -> int:
        try:
            if isinstance(value.get(field), bool):
                raise ValueError
            result = int(value.get(field))
        except (TypeError, ValueError) as error:
            raise ValidationError(f"{field} 必須是整數") from error
        if result < minimum:
            raise ValidationError(f"{field} 不可小於 {minimum}")
        return result

    @staticmethod
    def _number(value: dict[str, Any], field: str, minimum: float = 0) -> float:
        try:
            if isinstance(value.get(field), bool):
                raise ValueError
            result = float(value.get(field))
        except (TypeError, ValueError) as error:
            raise ValidationError(f"{field} 必須是數字") from error
        if not math.isfinite(result) or result < minimum:
            raise ValidationError(f"{field} 不可小於 {minimum}")
        return result

    def _migrate_purchases(self, db: sqlite3.Connection) -> None:
        rows = db.execute("""SELECT p.* FROM purchases p LEFT JOIN transactions t
                             ON t.source_event_id=p.source_event_id WHERE t.id IS NULL ORDER BY p.id""").fetchall()
        for row in rows:
            v = dict(row)
            price = int(v["unit_price"]) // (10_000 if v["source"] == "auction_buy_offer" else 1)
            kind = "order" if v["source"] == "buy_order_mail" else "instant"
            db.execute("""INSERT OR IGNORE INTO transactions(
                source_event_id,traded_at,captured_at,direction,transaction_kind,item_id,item_name,item_category,
                quantity,unit_price,total_price,order_id,location_id,character_name,game_server,quality_level,
                enchantment_level,source,confidence,raw_event,status,updated_at)
                VALUES(?,?,?,'buy',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'active',?)""",
                (v["source_event_id"],v["purchased_at"],v["captured_at"],kind,v["item_id"],v["item_name"],
                 item_category(v["item_id"]),v["quantity"],price,price*int(v["quantity"]),v["order_id"],
                 v["location_id"],v["character_name"],v["game_server"],v["quality_level"],v["enchantment_level"],
                 v["source"],v["confidence"],v["raw_event"],utc_now()))

    @staticmethod
    def _repair_legacy_allocations(db: sqlite3.Connection) -> None:
        """Keep older project allocations first and trim v4-era over-allocation."""
        transactions = db.execute("""SELECT t.id,t.quantity FROM transactions t JOIN project_items pi
            ON pi.transaction_id=t.id GROUP BY t.id HAVING SUM(pi.selected_quantity)>t.quantity""").fetchall()
        for transaction in transactions:
            remaining = int(transaction["quantity"])
            rows = db.execute("""SELECT pi.project_id,pi.selected_quantity FROM project_items pi
                JOIN projects p ON p.id=pi.project_id WHERE pi.transaction_id=?
                ORDER BY p.created_at,p.id""", (transaction["id"],)).fetchall()
            for row in rows:
                kept = min(int(row["selected_quantity"]), remaining)
                if kept:
                    db.execute("""UPDATE project_items SET selected_quantity=?
                        WHERE project_id=? AND transaction_id=?""", (kept,row["project_id"],transaction["id"]))
                else:
                    db.execute("DELETE FROM project_items WHERE project_id=? AND transaction_id=?",
                               (row["project_id"],transaction["id"]))
                remaining -= kept

    def insert_transaction(self, event: dict[str, Any]) -> tuple[bool, int]:
        event_id = self._required_text(event, "source_event_id")
        item_id = self._required_text(event, "item_id")
        direction = self._optional_text(event, "direction") or ("sell" if event.get("type") == "sale" else "buy")
        source = self._optional_text(event, "source") or "capture"
        kind = self._optional_text(event, "transaction_kind") or (
            "order" if "order_mail" in source or "finished_auction" in source else "instant")
        if direction not in DIRECTIONS or kind not in KINDS:
            raise ValidationError("成交方向或類型無效")
        quantity = self._integer(event, "quantity", 1)
        price = self._integer(event, "unit_price")
        if event.get("type") == "purchase" and source == "auction_buy_offer":
            price //= 10_000
        calculated_total = quantity * price
        supplied = event.get("total_price")
        legacy_total = (event.get("type") == "purchase" and source == "auction_buy_offer"
                        and supplied is not None and int(supplied)//10_000 == calculated_total)
        from_mail = bool(event.get("mail_id")) or "order_mail" in source or "auction_mail" in source
        manual_total = source in {"manual_entry", "private_trade", "storage_inventory"}
        if supplied is not None and (from_mail or manual_total):
            total = self._integer(event, "total_price")
        else:
            total = calculated_total
        if supplied is not None and not from_mail and not manual_total and not legacy_total and self._integer(event, "total_price") != total:
            raise ValidationError("total_price 必須等於 quantity × unit_price")
        settings = self.get_settings()
        # Albion charges a higher tax for an immediate sale to an existing buy
        # order.  The configurable market_tax_rate is for sell orders; using it
        # for both paths made an instant sale at 8% appear as a 4% transaction.
        tax_rate = self._number(event, "sales_tax_rate") if event.get("sales_tax_rate") is not None else (
            8.0 if direction == "sell" and kind == "instant" else
            (settings["market_tax_rate"] if direction == "sell" else 0))
        setup_rate = self._number(event, "setup_fee_rate") if event.get("setup_fee_rate") is not None else (
            settings["setup_fee_rate"] if kind == "order" else 0)
        if tax_rate >= 100 or setup_rate >= 100:
            raise ValidationError("稅率與設定費率必須小於 100%")
        sales_tax = round(total * tax_rate / 100) if direction == "sell" else 0
        setup_fee = round(total * setup_rate / 100) if kind == "order" else 0
        net_total = total - sales_tax - setup_fee if direction == "sell" else total + setup_fee
        mail_id = self._integer(event, "mail_id", 1) if event.get("mail_id") else None
        traded_at = (self._optional_text(event,"traded_at") or self._optional_text(event,"purchased_at")
                     or self._optional_text(event,"sold_at") or utc_now())
        try:
            _parse_datetime(traded_at)
        except (TypeError, ValueError) as error:
            raise ValidationError("traded_at 必須是有效日期時間") from error
        status = self._optional_text(event, "status") or "active"
        if status not in STATUSES:
            raise ValidationError("status 必須是 active 或 sold")
        values = (event_id,traded_at,self._optional_text(event,"captured_at") or utc_now(),direction,kind,item_id,
                  self._optional_text(event,"item_name"),self._optional_text(event,"item_category") or item_category(item_id),
                  quantity,price,total,self._optional_text(event,"order_id"),self._optional_text(event,"location_id"),
                  self._optional_text(event,"character_name"),self._optional_text(event,"game_server"),
                  event.get("quality_level"),event.get("enchantment_level"),source,
                  self._optional_text(event,"confidence") or "confirmed",json.dumps(event,ensure_ascii=False,separators=(",",":")),
                  status,self._optional_text(event,"notes"),utc_now(),tax_rate,setup_rate,sales_tax,setup_fee,net_total,mail_id)
        with self.connect() as db:
            cursor = db.execute("""INSERT OR IGNORE INTO transactions(
                source_event_id,traded_at,captured_at,direction,transaction_kind,item_id,item_name,item_category,
                quantity,unit_price,total_price,order_id,location_id,character_name,game_server,quality_level,
                enchantment_level,source,confidence,raw_event,status,notes,updated_at,sales_tax_rate,setup_fee_rate,
                sales_tax,setup_fee,net_total,mail_id)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", values)
            if not cursor.rowcount:
                return False, int(db.execute("SELECT id FROM transactions WHERE source_event_id=?",(event_id,)).fetchone()[0])
            row_id = int(cursor.lastrowid)
            if mail_id:
                now = utc_now()
                mail_type = self._optional_text(event, "mail_type") or (
                    "SELLORDER_FINISHED" if direction == "sell" else "BUYORDER_FINISHED")
                mail_state = self._optional_text(event, "mail_state") or "completed"
                db.execute("""INSERT INTO market_mails(mail_id,mail_type,location_id,content,state,transaction_id,captured_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(mail_id) DO UPDATE SET
                    mail_type=excluded.mail_type,location_id=COALESCE(excluded.location_id,market_mails.location_id),
                    content=excluded.content,state=excluded.state,transaction_id=excluded.transaction_id,updated_at=excluded.updated_at""",
                    (mail_id,mail_type,self._optional_text(event,"location_id"),self._optional_text(event,"mail_content")
                     or str(event.get("raw_params") or ""),mail_state,row_id,now,now))
            self._add_to_snapshot(db,row_id,traded_at)
            return True,row_id

    def insert_purchase(self, event: dict[str, Any]) -> tuple[bool, int]:
        value = dict(event)
        value.setdefault("direction","buy")
        return self.insert_transaction(value)

    @staticmethod
    def _add_to_snapshot(db: sqlite3.Connection, transaction_id: int, traded_at: str) -> None:
        t = db.execute("SELECT * FROM transactions WHERE id=?",(transaction_id,)).fetchone()
        if not t or t["deleted_at"]:
            return
        start,end = _snapshot_bounds(traded_at)
        buy = t["quantity"] if t["direction"] == "buy" else 0
        sell = t["quantity"] if t["direction"] == "sell" else 0
        effective_total = t["net_total"] if t["net_total"] is not None else t["total_price"]
        spent = effective_total if t["direction"] == "buy" else 0
        revenue = t["total_price"] if t["direction"] == "sell" else 0
        db.execute("""INSERT INTO cost_snapshots(period_start,period_end,updated_at,transaction_count,buy_quantity,sell_quantity,spent,revenue)
            VALUES(?,?,?,1,?,?,?,?) ON CONFLICT(period_start) DO UPDATE SET updated_at=excluded.updated_at,
            transaction_count=transaction_count+1,buy_quantity=buy_quantity+excluded.buy_quantity,
            sell_quantity=sell_quantity+excluded.sell_quantity,spent=spent+excluded.spent,revenue=revenue+excluded.revenue""",
            (start,end,utc_now(),buy,sell,spent,revenue))
        snapshot_id = db.execute("SELECT id FROM cost_snapshots WHERE period_start=?",(start,)).fetchone()[0]
        db.execute("""INSERT INTO cost_snapshot_items(snapshot_id,item_id,direction,transaction_kind,quantity,total_price)
            VALUES(?,?,?,?,?,?) ON CONFLICT(snapshot_id,item_id,direction,transaction_kind) DO UPDATE SET
            quantity=quantity+excluded.quantity,total_price=total_price+excluded.total_price""",
            (snapshot_id,t["item_id"],t["direction"],t["transaction_kind"],t["quantity"],
             t["total_price"] if t["direction"] == "sell" else effective_total))

    def _rebuild_snapshots(self, db: sqlite3.Connection) -> None:
        db.execute("DELETE FROM cost_snapshot_items")
        db.execute("DELETE FROM cost_snapshots")
        for row in db.execute("SELECT id,traded_at FROM transactions WHERE deleted_at IS NULL ORDER BY id"):
            self._add_to_snapshot(db,int(row["id"]),row["traded_at"])

    def update_status(self, event: dict[str, Any]) -> None:
        values=(self._required_text(event,"client_id"),self._optional_text(event,"captured_at") or utc_now(),
                self._required_text(event,"state"),self._optional_text(event,"last_packet_at"),
                self._integer(event,"packets_seen"),self._optional_text(event,"location_id"),
                self._optional_text(event,"character_name"),self._optional_text(event,"message"),self._optional_text(event,"version"))
        with self.connect() as db:
            db.execute("""INSERT INTO capture_status VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(client_id) DO UPDATE SET
                updated_at=excluded.updated_at,state=excluded.state,last_packet_at=excluded.last_packet_at,
                packets_seen=excluded.packets_seen,location_id=excluded.location_id,character_name=excluded.character_name,
                message=excluded.message,version=excluded.version""",values)

    def insert_warning(self, event: dict[str, Any]) -> None:
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO capture_warnings(source_event_id,captured_at,code,message,raw_event) VALUES(?,?,?,?,?)",
                (self._optional_text(event,"source_event_id"),self._optional_text(event,"captured_at") or utc_now(),
                 self._required_text(event,"code"),self._required_text(event,"message"),json.dumps(event,ensure_ascii=False)))

    def get_settings(self) -> dict[str, float]:
        with self.connect() as db:
            rows = db.execute("SELECT key,value FROM app_settings").fetchall()
        values = {row["key"]: float(row["value"]) for row in rows}
        return {"market_tax_rate": values.get("market_tax_rate", 4.0),
                "setup_fee_rate": values.get("setup_fee_rate", 2.5)}

    def update_settings(self, values: dict[str, Any]) -> dict[str, float]:
        current = self.get_settings()
        for key in ("market_tax_rate", "setup_fee_rate"):
            if key in values:
                rate = self._number(values, key)
                if rate >= 100:
                    raise ValidationError("稅率與設定費率必須小於 100%")
                current[key] = rate
        with self.connect() as db:
            db.executemany("""INSERT INTO app_settings(key,value,updated_at) VALUES(?,?,?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at""",
                [(key,str(value),utc_now()) for key,value in current.items()])
        return current

    def upsert_mail_metadata(self, event: dict[str, Any]) -> None:
        mail_id = self._integer(event, "mail_id", 1)
        mail_type = self._required_text(event, "mail_type")
        received_raw = self._integer(event, "mail_received") if event.get("mail_received") else 0
        received_at = None
        if received_raw > 621355968000000000:
            received_at = datetime.fromtimestamp((received_raw-621355968000000000)/10_000_000,
                                                timezone.utc).isoformat().replace("+00:00","Z")
        elif received_raw > 1_000_000_000:
            divisor = 1000 if received_raw > 1_000_000_000_000 else 1
            received_at = datetime.fromtimestamp(received_raw/divisor, timezone.utc).isoformat().replace("+00:00","Z")
        now = self._optional_text(event,"captured_at") or utc_now()
        with self.connect() as db:
            db.execute("""INSERT INTO market_mails(mail_id,mail_type,received_raw,received_at,location_id,captured_at,updated_at)
                VALUES(?,?,?,?,?,?,?) ON CONFLICT(mail_id) DO UPDATE SET mail_type=excluded.mail_type,
                received_raw=excluded.received_raw,received_at=COALESCE(excluded.received_at,market_mails.received_at),
                location_id=excluded.location_id,updated_at=excluded.updated_at""",
                (mail_id,mail_type,received_raw,received_at,self._optional_text(event,"location_id"),now,now))

    def list_market_mails(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(r"""SELECT m.*,t.direction,t.item_id,COALESCE(c.name_zh_tw,t.item_name) item_name,
                t.quantity,t.total_price,t.net_total,t.sales_tax,t.setup_fee FROM market_mails m
                LEFT JOIN transactions t ON t.id=m.transaction_id LEFT JOIN item_catalog c ON c.item_id=t.item_id
                WHERE m.mail_type LIKE 'MARKETPLACE\_%' ESCAPE '\\' OR m.mail_type LIKE 'BLACKMARKET\_%' ESCAPE '\\'
                ORDER BY COALESCE(m.received_at,m.captured_at) DESC LIMIT ?""",(min(max(int(limit),1),500),)).fetchall()
        result=[]
        for row in rows:
            value=dict(row); value["location_name"]=location_name(value.get("location_id")); result.append(value)
        return result

    def resolve_market_mail(self, event: dict[str, Any]) -> None:
        mail_id = self._integer(event, "mail_id", 1)
        state = self._required_text(event, "mail_state")
        if state not in {"no_trade", "parse_error", "ignored"}:
            raise ValidationError("mail_state 必須是 no_trade、parse_error 或 ignored")
        now = self._optional_text(event,"captured_at") or utc_now()
        mail_type = self._optional_text(event,"mail_type") or "UNKNOWN"
        content = self._optional_text(event,"raw_params")
        message = self._optional_text(event,"message")
        with self.connect() as db:
            db.execute("""INSERT INTO market_mails(mail_id,mail_type,location_id,content,state,captured_at,updated_at)
                VALUES(?,?,?,?,?,?,?) ON CONFLICT(mail_id) DO UPDATE SET
                mail_type=excluded.mail_type,location_id=COALESCE(excluded.location_id,market_mails.location_id),
                content=excluded.content,state=excluded.state,updated_at=excluded.updated_at""",
                (mail_id,mail_type,self._optional_text(event,"location_id"),content or message,state,now,now))

    @staticmethod
    def _enrich(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        value=dict(row)
        value["item_name"]=value.get("item_name") or fallback_item_name(value["item_id"])
        value["location_name"]=location_name(value.get("location_id"))
        return value

    def list_transactions(self, *, limit: int=100, offset: int=0, direction: str|None=None,
                          transaction_kind: str|None=None,item_category_value: str|None=None,
                          include_sold: bool=False,include_deleted: bool=False) -> list[dict[str,Any]]:
        clauses=[]; params=[]
        if not include_deleted: clauses.append("t.deleted_at IS NULL")
        if not include_sold: clauses.append("t.status!='sold'")
        if direction:
            if direction not in DIRECTIONS: raise ValidationError("direction 必須是 buy 或 sell")
            clauses.append("t.direction=?"); params.append(direction)
        if transaction_kind:
            if transaction_kind not in KINDS: raise ValidationError("transaction_kind 必須是 instant 或 order")
            clauses.append("t.transaction_kind=?"); params.append(transaction_kind)
        if item_category_value: clauses.append("t.item_category=?"); params.append(item_category_value)
        where="WHERE "+" AND ".join(clauses) if clauses else ""
        params.extend((min(max(int(limit),1),1000),max(int(offset),0)))
        with self.connect() as db:
            rows=db.execute(f"""SELECT t.id,t.source_event_id,t.traded_at,t.traded_at AS purchased_at,t.direction,
                t.transaction_kind,t.item_id,COALESCE(c.name_zh_tw,t.item_name) AS item_name,t.item_category,
                t.quantity,t.unit_price,t.total_price,t.order_id,t.location_id,t.character_name,t.game_server,
                t.quality_level,t.enchantment_level,t.source,t.confidence,t.status,t.notes,t.updated_at,t.deleted_at,
                t.sales_tax_rate,t.setup_fee_rate,t.sales_tax,t.setup_fee,t.net_total,t.mail_id,
                COALESCE((SELECT SUM(selected_quantity) FROM project_items pi WHERE pi.transaction_id=t.id),0) allocated_quantity
                FROM transactions t LEFT JOIN item_catalog c ON c.item_id=t.item_id {where}
                ORDER BY t.traded_at DESC,t.id DESC LIMIT ? OFFSET ?""",params).fetchall()
        result=[self._enrich(row) for row in rows]
        for row in result: row["available_quantity"]=max(0,row["quantity"]-row["allocated_quantity"])
        return result

    def list_purchases(self, *, limit: int=100,offset: int=0) -> list[dict[str,Any]]:
        return self.list_transactions(limit=limit,offset=offset,direction="buy")

    def update_transaction(self, transaction_id: int, changes: dict[str,Any]) -> dict[str,Any]:
        allowed={"traded_at","direction","transaction_kind","item_id","item_name","item_category","quantity","unit_price","total_price",
                 "location_id","status","notes","sales_tax_rate","setup_fee_rate"}
        if not allowed.intersection(changes): raise ValidationError("沒有可修改的欄位")
        with self.connect() as db:
            row=db.execute("SELECT * FROM transactions WHERE id=? AND deleted_at IS NULL",(int(transaction_id),)).fetchone()
            if not row: raise ValidationError("找不到成交紀錄")
            value=dict(row); value.update({k:v for k,v in changes.items() if k in allowed})
            if value["direction"] not in DIRECTIONS or value["transaction_kind"] not in KINDS or value["status"] not in STATUSES:
                raise ValidationError("成交方向、類型或狀態無效")
            quantity=self._integer(value,"quantity",1); price=self._integer(value,"unit_price")
            allocated=db.execute("SELECT COALESCE(SUM(selected_quantity),0) FROM project_items WHERE transaction_id=?",(transaction_id,)).fetchone()[0]
            if quantity<allocated: raise ValidationError(f"此成交已有 {allocated} 件分配到專案，數量不可低於該值")
            try: _parse_datetime(str(value["traded_at"]))
            except (TypeError,ValueError) as error: raise ValidationError("成交時間格式無效") from error
            item_id=str(value["item_id"]).strip()
            if not item_id: raise ValidationError("品項代碼不可空白")
            tax_rate=self._number(value,"sales_tax_rate"); setup_rate=self._number(value,"setup_fee_rate")
            if tax_rate>=100 or setup_rate>=100: raise ValidationError("稅率與設定費率必須小於 100%")
            calculated_total=quantity*price
            total=self._integer(value,"total_price") if value.get("source") in {"manual_entry","private_trade","storage_inventory"} and "total_price" in changes else calculated_total
            sales_tax=round(total*tax_rate/100) if value["direction"]=="sell" else 0
            setup_fee=round(total*setup_rate/100) if value["transaction_kind"]=="order" else 0
            net_total=total-sales_tax-setup_fee if value["direction"]=="sell" else total+setup_fee
            db.execute("""UPDATE transactions SET traded_at=?,direction=?,transaction_kind=?,item_id=?,item_name=?,
                item_category=?,quantity=?,unit_price=?,total_price=?,location_id=?,status=?,notes=?,sales_tax_rate=?,
                setup_fee_rate=?,sales_tax=?,setup_fee=?,net_total=?,updated_at=? WHERE id=?""",
                (value["traded_at"],value["direction"],value["transaction_kind"],item_id,self._optional_text(value,"item_name"),
                 self._optional_text(value,"item_category") or item_category(item_id),quantity,price,total,
                 self._optional_text(value,"location_id"),value["status"],self._optional_text(value,"notes"),tax_rate,
                 setup_rate,sales_tax,setup_fee,net_total,utc_now(),transaction_id))
            self._rebuild_snapshots(db)
        return next(x for x in self.list_transactions(limit=1000,include_sold=True) if x["id"]==int(transaction_id))

    def delete_transaction(self, transaction_id: int) -> bool:
        with self.connect() as db:
            allocated=db.execute("SELECT COALESCE(SUM(selected_quantity),0) FROM project_items WHERE transaction_id=?",(transaction_id,)).fetchone()[0]
            if allocated: raise ValidationError("此成交仍在專案中，請先修改或刪除該專案")
            cursor=db.execute("UPDATE transactions SET deleted_at=?,updated_at=? WHERE id=? AND deleted_at IS NULL",(utc_now(),utc_now(),transaction_id))
            if cursor.rowcount: self._rebuild_snapshots(db)
            return cursor.rowcount>0

    def list_snapshots(self, *, limit: int=40) -> list[dict[str,Any]]:
        with self.connect() as db:
            rows=db.execute("""SELECT *,revenue-spent AS balance FROM cost_snapshots
                               ORDER BY period_start DESC LIMIT ?""",(min(max(int(limit),1),500),)).fetchall()
            result=[]
            for row in rows:
                value=dict(row)
                details=db.execute("""SELECT si.item_id,COALESCE(c.name_zh_tw,t.item_name) item_name,si.direction,
                    si.transaction_kind,si.quantity,si.total_price FROM cost_snapshot_items si
                    LEFT JOIN item_catalog c ON c.item_id=si.item_id LEFT JOIN transactions t ON t.id=(SELECT id FROM transactions
                    WHERE item_id=si.item_id ORDER BY id DESC LIMIT 1) WHERE si.snapshot_id=? ORDER BY si.total_price DESC""",(row["id"],)).fetchall()
                value["items"]=[self._enrich(x) for x in details]; result.append(value)
        return result

    @staticmethod
    def _scope(period: str) -> tuple[str,list[Any],str]:
        if period=="all": return "deleted_at IS NULL",[],"day"
        if period not in PERIODS: raise ValidationError("period 必須是 day、week、month、year 或 all")
        start=datetime.now(timezone.utc)-timedelta(days=PERIODS[period])
        bucket="hour" if period=="day" else ("month" if period=="year" else "day")
        return "deleted_at IS NULL AND traded_at>=?",[start.isoformat(timespec="seconds").replace("+00:00","Z")],bucket

    def summary(self,period: str="all") -> dict[str,Any]:
        scope,params,bucket=self._scope(period)
        bucket_sql={"hour":"substr(traded_at,1,13)","month":"substr(traded_at,1,7)","day":"substr(traded_at,1,10)"}[bucket]
        with self.connect() as db:
            totals=db.execute(f"""SELECT COUNT(*) transactions,
                COALESCE(SUM(CASE WHEN direction='buy' THEN quantity ELSE 0 END),0) quantity,
                COALESCE(SUM(CASE WHEN direction='buy' THEN quantity ELSE 0 END),0) bought_quantity,
                COALESCE(SUM(CASE WHEN direction='sell' THEN quantity ELSE 0 END),0) sold_quantity,
                COALESCE(SUM(CASE WHEN direction='buy' THEN net_total ELSE 0 END),0) spent,
                COALESCE(SUM(CASE WHEN direction='sell' THEN total_price ELSE 0 END),0) revenue,
                COALESCE(SUM(CASE WHEN direction='sell' THEN net_total ELSE 0 END),0) net_revenue,
                COALESCE(SUM(CASE WHEN direction='sell' THEN net_total ELSE -net_total END),0) balance,
                COALESCE(SUM(CASE WHEN direction='buy' THEN total_price ELSE 0 END),0) gross_spent,
                COALESCE(SUM(CASE WHEN direction='sell' THEN total_price ELSE 0 END),0) gross_revenue,
                COALESCE(SUM(sales_tax),0) sales_tax,COALESCE(SUM(setup_fee),0) setup_fee,
                COALESCE(CAST(SUM(CASE WHEN direction='buy' THEN net_total ELSE 0 END) AS REAL)/
                NULLIF(SUM(CASE WHEN direction='buy' THEN quantity ELSE 0 END),0),0) average_unit_price
                FROM transactions WHERE {scope}""",params).fetchone()
            daily=db.execute(f"""SELECT {bucket_sql} day,COUNT(*) transactions,
                SUM(CASE WHEN direction='buy' THEN quantity ELSE 0 END) bought_quantity,
                SUM(CASE WHEN direction='sell' THEN quantity ELSE 0 END) sold_quantity,
                SUM(CASE WHEN direction='buy' THEN net_total ELSE 0 END) spent,
                SUM(CASE WHEN direction='sell' THEN total_price ELSE 0 END) revenue,
                SUM(CASE WHEN direction='sell' THEN net_total ELSE 0 END) net_revenue
                FROM transactions WHERE {scope} GROUP BY {bucket_sql} ORDER BY day DESC LIMIT 366""",params).fetchall()
            top=db.execute(f"""SELECT t.item_id,COALESCE(c.name_zh_tw,MAX(t.item_name)) item_name,
                SUM(CASE WHEN direction='buy' THEN quantity ELSE 0 END) bought_quantity,
                SUM(CASE WHEN direction='sell' THEN quantity ELSE 0 END) sold_quantity,
                SUM(CASE WHEN direction='buy' THEN net_total ELSE 0 END) spent,
                SUM(CASE WHEN direction='sell' THEN total_price ELSE 0 END) revenue,
                SUM(CASE WHEN direction='sell' THEN net_total ELSE 0 END) net_revenue
                FROM transactions t LEFT JOIN item_catalog c ON c.item_id=t.item_id WHERE {scope}
                GROUP BY t.item_id ORDER BY spent+revenue DESC LIMIT 10""",params).fetchall()
            statuses=db.execute("SELECT * FROM capture_status ORDER BY updated_at DESC").fetchall()
            warnings=db.execute("SELECT captured_at,code,message FROM capture_warnings ORDER BY id DESC LIMIT 5").fetchall()
        now=datetime.now(timezone.utc); captures=[]
        for row in statuses:
            value=dict(row); value["location_name"]=location_name(value.get("location_id"))
            try: value["online"]=(now-_parse_datetime(value["updated_at"])).total_seconds()<15
            except (TypeError,ValueError): value["online"]=False
            captures.append(value)
        return {"period":period,"bucket":bucket,"totals":dict(totals),"daily":[dict(x) for x in reversed(daily)],
                "top_items":[self._enrich(x) for x in top],"capture":captures,"warnings":[dict(x) for x in warnings]}

    def cache_item_names(self,names: dict[str,str]) -> None:
        if not names: return
        with self.connect() as db:
            db.executemany("""INSERT INTO item_catalog VALUES(?,?,?) ON CONFLICT(item_id) DO UPDATE SET
                               name_zh_tw=excluded.name_zh_tw,updated_at=excluded.updated_at""",[(k,v,utc_now()) for k,v in names.items()])

    def catalog_names(self,item_ids: list[str]) -> dict[str,str]:
        ids=list(dict.fromkeys(item_ids))[:100]
        if not ids: return {}
        with self.connect() as db:
            rows=db.execute(f"SELECT item_id,name_zh_tw FROM item_catalog WHERE item_id IN ({','.join('?' for _ in ids)})",ids).fetchall()
        return {r["item_id"]:r["name_zh_tw"] for r in rows}

    def estimate_item_prices(self, item_ids: list[str]) -> dict[str, dict[str, Any]]:
        ids = list(dict.fromkeys(item_ids))[:100]
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        with self.connect() as db:
            rows = db.execute(f"""SELECT item_id,
                COALESCE(ROUND(SUM(CASE WHEN direction='buy' THEN net_total ELSE 0 END)*1.0/
                    NULLIF(SUM(CASE WHEN direction='buy' THEN quantity ELSE 0 END),0)),0) buy_unit_price,
                COALESCE(ROUND(SUM(CASE WHEN direction='sell' THEN net_total ELSE 0 END)*1.0/
                    NULLIF(SUM(CASE WHEN direction='sell' THEN quantity ELSE 0 END),0)),0) sell_unit_price,
                SUM(CASE WHEN direction='buy' THEN quantity ELSE 0 END) buy_quantity,
                SUM(CASE WHEN direction='sell' THEN quantity ELSE 0 END) sell_quantity,
                MAX(traded_at) updated_at
                FROM transactions WHERE deleted_at IS NULL AND item_id IN ({placeholders})
                GROUP BY item_id""", ids).fetchall()
        return {row["item_id"]: dict(row) for row in rows}

    def _project_values(self,payload: dict[str,Any],existing: dict[str,Any]|None=None) -> dict[str,Any]:
        value=dict(existing or {}); value.update(payload)
        name=str(value.get("name","")).strip()
        if not name: raise ValidationError("專案名稱不可空白")
        kind=str(value.get("project_type") or "trade")
        if kind not in {"trade","manufacturing"}: raise ValidationError("專案類型無效")
        return_rate=self._number(value,"return_rate"); focus_rate=self._number(value,"focus_return_rate")
        fee=self._number(value,"sale_fee_rate")
        planning_mode=str(value.get("planning_mode") or "p95")
        if planning_mode not in {"expected","p95","p99","guaranteed"}:
            raise ValidationError("估算模式必須是 expected、p95、p99 或 guaranteed")
        if return_rate>=100 or focus_rate>=100 or fee>=100: raise ValidationError("回報率與出售費率必須小於 100%")
        planner_state=value.get("planner_state")
        if isinstance(planner_state,str) and planner_state:
            try: planner_state=json.loads(planner_state)
            except json.JSONDecodeError as error: raise ValidationError("製造規劃資料格式無效") from error
        if planner_state is not None and not isinstance(planner_state,dict):
            raise ValidationError("製造規劃資料格式無效")
        encoded_planner=json.dumps(planner_state,ensure_ascii=False,separators=(",",":")) if planner_state else None
        if encoded_planner and len(encoded_planner)>100_000: raise ValidationError("製造規劃資料過大")
        return {"name":name[:100],"project_type":kind,"input_item_id":self._optional_text(value,"input_item_id"),
                "output_item_id":self._optional_text(value,"output_item_id"),"output_item_name":self._optional_text(value,"output_item_name"),
                "return_rate":return_rate,"focus_return_rate":focus_rate,"use_focus":1 if value.get("use_focus") else 0,
                "material_per_unit":self._number(value,"material_per_unit",0.000001),
                "output_per_craft":self._number(value,"output_per_craft",0.000001),"extra_cost":self._integer(value,"extra_cost"),
                "sale_fee_rate":fee,"target_sale_price":self._integer(value,"target_sale_price"),"notes":self._optional_text(value,"notes"),
                "target_output":self._integer(value,"target_output"),"available_focus":self._integer(value,"available_focus"),
                "focus_cost_per_craft":self._integer(value,"focus_cost_per_craft"),"planning_mode":planning_mode,
                "planner_state":encoded_planner}

    @staticmethod
    def _materials(values: Any) -> list[dict[str, Any]]:
        if values is None:
            return []
        if not isinstance(values,list) or len(values)>20:
            raise ValidationError("materials 必須是最多 20 筆的陣列")
        result=[]; seen=set()
        for raw in values:
            try:
                item_id=str(raw.get("item_id") or "").strip()
                name=str(raw.get("item_name") or "").strip() or None
                quantity=float(raw.get("quantity_per_craft"))
            except (AttributeError,TypeError,ValueError) as error:
                raise ValidationError("原材料資料格式無效") from error
            if not item_id or item_id in seen or not math.isfinite(quantity) or quantity<=0:
                raise ValidationError("原材料代碼不可空白或重複，單次用量必須大於 0")
            seen.add(item_id); result.append({"item_id":item_id,"item_name":name,"quantity_per_craft":quantity})
        return result

    @staticmethod
    def _selections(values: list[dict[str,Any]]) -> list[tuple[int,int]]:
        if not isinstance(values,list) or len(values)>500: raise ValidationError("selections 必須是陣列")
        result=[]; seen=set()
        for value in values:
            try: tid=int(value.get("transaction_id")); qty=int(value.get("quantity"))
            except (AttributeError,TypeError,ValueError) as error: raise ValidationError("專案訂單與數量必須是整數") from error
            if tid<=0 or qty<=0 or tid in seen: raise ValidationError("專案選取資料無效或重複")
            seen.add(tid); result.append((tid,qty))
        return result

    @staticmethod
    def _validate_allocations(db: sqlite3.Connection,values: list[tuple[int,int]],exclude: int|None=None) -> None:
        if not values: return
        ids=[x[0] for x in values]
        rows=db.execute(f"SELECT id,quantity FROM transactions WHERE deleted_at IS NULL AND id IN ({','.join('?' for _ in ids)})",ids).fetchall()
        limits={int(x["id"]):int(x["quantity"]) for x in rows}
        if len(limits)!=len(values): raise ValidationError("部分成交紀錄不存在")
        for tid,qty in values:
            if exclude is None:
                allocated=db.execute("SELECT COALESCE(SUM(selected_quantity),0) FROM project_items WHERE transaction_id=?",(tid,)).fetchone()[0]
            else:
                allocated=db.execute("SELECT COALESCE(SUM(selected_quantity),0) FROM project_items WHERE transaction_id=? AND project_id!=?",(tid,exclude)).fetchone()[0]
            available=limits[tid]-allocated
            if qty>available: raise ValidationError(f"成交 #{tid} 只剩 {available} 件可分配，無法再分配 {qty} 件")

    def create_project(self,name: str,selections: list[dict[str,Any]],settings: dict[str,Any]|None=None) -> int:
        defaults={"project_type":"trade","return_rate":36.7,"focus_return_rate":53.9,"material_per_unit":1,
                  "output_per_craft":1,"extra_cost":0,"sale_fee_rate":0,"target_sale_price":0,
                  "target_output":0,"available_focus":0,"focus_cost_per_craft":0,"planning_mode":"p95"}
        defaults.update(settings or {}); defaults["name"]=name
        value=self._project_values(defaults); selected=self._selections(selections); materials=self._materials(defaults.get("materials"))
        if not selected and value["project_type"]!="manufacturing": raise ValidationError("一般交易專案請至少選擇一筆成交紀錄")
        with self.connect() as db:
            self._validate_allocations(db,selected)
            cursor=db.execute("""INSERT INTO projects(name,created_at,updated_at,project_type,input_item_id,output_item_id,
                output_item_name,return_rate,focus_return_rate,use_focus,material_per_unit,output_per_craft,extra_cost,
                sale_fee_rate,target_sale_price,notes,target_output,available_focus,focus_cost_per_craft,planning_mode,planner_state)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (value["name"],utc_now(),utc_now(),value["project_type"],value["input_item_id"],value["output_item_id"],
                 value["output_item_name"],value["return_rate"],value["focus_return_rate"],value["use_focus"],
                 value["material_per_unit"],value["output_per_craft"],value["extra_cost"],value["sale_fee_rate"],
                 value["target_sale_price"],value["notes"],value["target_output"],value["available_focus"],
                 value["focus_cost_per_craft"],value["planning_mode"],value["planner_state"]))
            project_id=int(cursor.lastrowid)
            db.executemany("INSERT INTO project_items VALUES(?,?,?)",[(project_id,*x) for x in selected])
            db.executemany("INSERT INTO project_materials VALUES(?,?,?,?)",
                           [(project_id,x["item_id"],x["item_name"],x["quantity_per_craft"]) for x in materials])
        return project_id

    def update_project(self,project_id: int,payload: dict[str,Any]) -> dict[str,Any]:
        with self.connect() as db:
            row=db.execute("SELECT * FROM projects WHERE id=?",(project_id,)).fetchone()
            if not row: raise ValidationError("找不到專案")
            value=self._project_values(payload,dict(row))
            if "selections" in payload:
                selected=self._selections(payload["selections"]); self._validate_allocations(db,selected,project_id)
                db.execute("DELETE FROM project_items WHERE project_id=?",(project_id,))
                db.executemany("INSERT INTO project_items VALUES(?,?,?)",[(project_id,*x) for x in selected])
            if "materials" in payload:
                materials=self._materials(payload["materials"])
                db.execute("DELETE FROM project_materials WHERE project_id=?",(project_id,))
                db.executemany("INSERT INTO project_materials VALUES(?,?,?,?)",
                               [(project_id,x["item_id"],x["item_name"],x["quantity_per_craft"]) for x in materials])
            db.execute("""UPDATE projects SET name=?,updated_at=?,project_type=?,input_item_id=?,output_item_id=?,output_item_name=?,
                return_rate=?,focus_return_rate=?,use_focus=?,material_per_unit=?,output_per_craft=?,extra_cost=?,sale_fee_rate=?,
                target_sale_price=?,notes=?,target_output=?,available_focus=?,focus_cost_per_craft=?,planning_mode=?,planner_state=? WHERE id=?""",
                (value["name"],utc_now(),value["project_type"],value["input_item_id"],value["output_item_id"],
                 value["output_item_name"],value["return_rate"],value["focus_return_rate"],value["use_focus"],
                 value["material_per_unit"],value["output_per_craft"],value["extra_cost"],value["sale_fee_rate"],
                 value["target_sale_price"],value["notes"],value["target_output"],value["available_focus"],
                 value["focus_cost_per_craft"],value["planning_mode"],value["planner_state"],project_id))
        return next(x for x in self.list_projects() if x["id"]==project_id)

    def list_projects(self) -> list[dict[str,Any]]:
        with self.connect() as db:
            projects=db.execute("SELECT * FROM projects ORDER BY created_at DESC,id DESC").fetchall(); result=[]
            for project in projects:
                value=dict(project)
                try: planner_state=json.loads(value.get("planner_state") or "{}")
                except json.JSONDecodeError: planner_state={}
                value["planner_state"]=planner_state
                rows=db.execute("""SELECT t.id transaction_id,t.direction,t.transaction_kind,t.item_id,
                    COALESCE(c.name_zh_tw,t.item_name) item_name,t.item_category,pi.selected_quantity quantity,t.unit_price,
                    ROUND(t.total_price*1.0*pi.selected_quantity/t.quantity) total_price,
                    ROUND(t.net_total*1.0*pi.selected_quantity/t.quantity) net_total,
                    ROUND(t.sales_tax*1.0*pi.selected_quantity/t.quantity) sales_tax,
                    ROUND(t.setup_fee*1.0*pi.selected_quantity/t.quantity) setup_fee,
                    t.status,t.location_id FROM project_items pi
                    JOIN transactions t ON t.id=pi.transaction_id LEFT JOIN item_catalog c ON c.item_id=t.item_id
                    WHERE pi.project_id=? ORDER BY t.traded_at,t.id""",(project["id"],)).fetchall()
                items=[self._enrich(x) for x in rows]
                spent=sum(x["net_total"] for x in items if x["direction"]=="buy")
                revenue=sum(x["total_price"] for x in items if x["direction"]=="sell")
                transaction_fees=sum(x["sales_tax"]+x["setup_fee"] for x in items if x["direction"]=="sell")
                configured_fee=float(value["sale_fee_rate"] or 0)
                fee=round(revenue*configured_fee/100) if configured_fee else transaction_fees
                net_revenue=revenue-fee
                costs=spent+int(value["extra_cost"] or 0)
                input_qty=sum(x["quantity"] for x in items if x["direction"]=="buy" and (not value["input_item_id"] or x["item_id"]==value["input_item_id"]))
                sold_qty=sum(x["quantity"] for x in items if x["direction"]=="sell" and (not value["output_item_id"] or x["item_id"]==value["output_item_id"]))
                rate=float(value["focus_return_rate"] if value["use_focus"] else value["return_rate"]); expected=0
                if value["project_type"]=="manufacturing" and input_qty:
                    expected=math.floor(input_qty/float(value["material_per_unit"])*float(value["output_per_craft"])/(1-rate/100))
                net=net_revenue-costs; denominator=expected*(1-float(value["sale_fee_rate"] or 0)/100)
                projected=round(expected*int(value["target_sale_price"] or 0)*(1-float(value["sale_fee_rate"] or 0)/100))
                materials=[dict(x) for x in db.execute("""SELECT item_id,item_name,quantity_per_craft
                    FROM project_materials WHERE project_id=? ORDER BY rowid""",(project["id"],)).fetchall()]
                if not materials and value.get("input_item_id"):
                    materials=[{"item_id":value["input_item_id"],"item_name":None,
                                "quantity_per_craft":float(value["material_per_unit"])}]
                crafts=math.ceil(int(value["target_output"] or 0)/float(value["output_per_craft"])) if value["target_output"] else 0
                focus_cost=int(value["focus_cost_per_craft"] or 0); available_focus=int(value["available_focus"] or 0)
                if value["use_focus"] and focus_cost>0:
                    focus_crafts=min(crafts,available_focus//focus_cost)
                else:
                    focus_crafts=0
                normal_crafts=crafts-focus_crafts
                allocated_by_item={}
                for item in items:
                    if item["direction"]=="buy":
                        allocated_by_item[item["item_id"]]=allocated_by_item.get(item["item_id"],0)+item["quantity"]
                required_materials=[]
                for material in materials:
                    per_craft=float(material["quantity_per_craft"])
                    focus_units=per_craft*focus_crafts; normal_units=per_craft*normal_crafts
                    focus_probability=float(value["focus_return_rate"])/100
                    normal_probability=float(value["return_rate"])/100
                    expected_consumption=(focus_units*(1-focus_probability)+normal_units*(1-normal_probability)) if crafts else 0
                    variance=(focus_units*focus_probability*(1-focus_probability)+
                              normal_units*normal_probability*(1-normal_probability)) if crafts else 0
                    guaranteed=math.ceil(focus_units+normal_units)
                    z={"expected":0,"p95":1.645,"p99":2.326}.get(value["planning_mode"])
                    required=guaranteed if z is None else min(guaranteed,math.ceil(expected_consumption+z*math.sqrt(variance)))
                    allocated=allocated_by_item.get(material["item_id"],0)
                    required_materials.append({**material,"required_quantity":required,"allocated_quantity":allocated,
                                               "shortage":max(required-allocated,0),
                                               "expected_quantity":math.ceil(expected_consumption),
                                               "p95_quantity":min(guaranteed,math.ceil(expected_consumption+1.645*math.sqrt(variance))),
                                               "p99_quantity":min(guaranteed,math.ceil(expected_consumption+2.326*math.sqrt(variance))),
                                               "guaranteed_quantity":guaranteed})
                production_plan=None
                if value["project_type"]=="manufacturing" and planner_state:
                    try: production_plan=build_refining_plan(planner_state)
                    except CraftingValidationError: production_plan=None
                if production_plan:
                    raw_prices=planner_state.get("prices") or {}
                    prices=raw_prices if isinstance(raw_prices,dict) else {}
                    raw_inventory=planner_state.get("inventory") or {}
                    inventory=raw_inventory if isinstance(raw_inventory,dict) else {}
                    planned_materials=[]; planned_material_cost=0
                    for material in production_plan["materials"]:
                        try: unit_price=max(0,int(float(prices.get(material["item_id"],0) or 0)))
                        except (TypeError,ValueError): unit_price=0
                        total=unit_price*int(material["required_quantity"])
                        planned_material_cost+=total
                        allocated=allocated_by_item.get(material["item_id"],0)
                        try: manual_inventory=max(0,int(float(inventory.get(material["item_id"],0) or 0)))
                        except (TypeError,ValueError): manual_inventory=0
                        ready_required=int(material["gross_quantity"])
                        total_available=allocated+manual_inventory
                        planned_materials.append({**material,"unit_price":unit_price,"total_cost":total,
                            "allocated_quantity":allocated,"inventory_quantity":manual_inventory,
                            "total_available_quantity":total_available,"ready_required_quantity":ready_required,
                            "shortage":max(ready_required-total_available,0)})
                    planned_station_cost=int(production_plan["total_station_cost"])
                    planned_total_cost=planned_material_cost+planned_station_cost+int(value["extra_cost"] or 0)
                    projected=round(int(value["target_output"] or 0)*int(value["target_sale_price"] or 0)*
                                    (1-float(value["sale_fee_rate"] or 0)/100))
                    planned_output=int(production_plan["quantity"])
                    planned_denominator=planned_output*(1-float(value["sale_fee_rate"] or 0)/100)
                    required_materials=planned_materials
                    crafts=sum(int(step["crafts"]) for step in production_plan["steps"])
                    focus_crafts=sum(int(step["focus_crafts"]) for step in production_plan["steps"])
                    normal_crafts=sum(int(step["normal_crafts"]) for step in production_plan["steps"])
                    value.update({"production_plan":production_plan,"planned_material_cost":planned_material_cost,
                        "planned_station_cost":planned_station_cost,"planned_total_cost":planned_total_cost,
                        "projected_revenue":projected,"projected_profit":projected-planned_total_cost,
                        "projected_roi":(projected-planned_total_cost)/planned_total_cost*100 if planned_total_cost else 0,
                        "expected_output":planned_output,
                        "break_even_unit_price":planned_total_cost/planned_denominator if planned_denominator else 0,
                        "crafts_required":crafts,"focus_crafts":focus_crafts,"normal_crafts":normal_crafts,
                        "focus_required":production_plan["focus_used"],"focus_used":production_plan["focus_used"],
                        "focus_shortage":0})
                value.update({"selection_count":len(items),"spent":spent,"revenue":revenue,"fees":fee,"net_revenue":net_revenue,
                    "balance":net,"net_profit":net,"roi":net/costs*100 if costs else 0,"input_quantity":input_qty,
                    "expected_output":value.get("expected_output",expected),"sold_output_quantity":sold_qty,
                    "unsold_output_quantity":max(value.get("expected_output",expected)-sold_qty,0),
                    "active_return_rate":rate,"break_even_unit_price":value.get("break_even_unit_price",costs/denominator if denominator else 0),
                    "projected_revenue":value.get("projected_revenue",projected),
                    "projected_profit":value.get("projected_profit",projected-costs),"items":items,"materials":materials,
                    "crafts_required":value.get("crafts_required",crafts),"focus_crafts":value.get("focus_crafts",focus_crafts),
                    "normal_crafts":value.get("normal_crafts",normal_crafts),
                    "focus_required":value.get("focus_required",crafts*focus_cost),
                    "focus_used":value.get("focus_used",focus_crafts*focus_cost),
                    "focus_shortage":value.get("focus_shortage",max(crafts*focus_cost-available_focus,0) if value["use_focus"] else 0),
                    "required_materials":required_materials})
                result.append(value)
        return result

    def delete_project(self,project_id: int) -> bool:
        with self.connect() as db: return db.execute("DELETE FROM projects WHERE id=?",(project_id,)).rowcount>0

    def export_csv(self) -> str:
        fields=["traded_at","direction","transaction_kind","item_id","item_name","item_category","quantity","unit_price",
                "total_price","sales_tax_rate","setup_fee_rate","sales_tax","setup_fee","net_total","mail_id","order_id",
                "location_id","character_name","game_server","quality_level","enchantment_level","source","confidence",
                "status","notes","deleted_at"]
        with self.connect() as db:
            rows=db.execute("""SELECT t.traded_at,t.direction,t.transaction_kind,t.item_id,COALESCE(c.name_zh_tw,t.item_name) item_name,
                t.item_category,t.quantity,t.unit_price,t.total_price,t.order_id,t.location_id,t.character_name,t.game_server,
                t.sales_tax_rate,t.setup_fee_rate,t.sales_tax,t.setup_fee,t.net_total,t.mail_id,t.quality_level,
                t.enchantment_level,t.source,t.confidence,t.status,t.notes,t.deleted_at FROM transactions t
                LEFT JOIN item_catalog c ON c.item_id=t.item_id ORDER BY t.traded_at,t.id""").fetchall()
        output=io.StringIO(newline=""); writer=csv.DictWriter(output,fieldnames=fields); writer.writeheader(); writer.writerows(dict(x) for x in rows)
        return output.getvalue()
