from __future__ import annotations

import json
import mimetypes
import threading
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .crafting import CraftingValidationError, build_refining_plan, build_transmutation_options, catalog
from .db import Database, ValidationError
from .localization import resolve_item_names
from .market import MarketError, fetch_market_prices, market_config


MAX_BODY_BYTES = 1_000_000


def query_bool(query: dict[str, list[str]], name: str) -> bool:
    return query.get(name, [""])[0].lower() in {"1", "true", "yes", "on"}


class TrackerServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], database: Database, web_root: Path):
        super().__init__(address, TrackerHandler)
        self.database = database
        self.web_root = web_root.resolve()


class TrackerHandler(BaseHTTPRequestHandler):
    server: TrackerServer
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[HTTP] {self.address_string()} - {fmt % args}")

    def _send_bytes(
        self,
        body: bytes,
        *,
        status: int = HTTPStatus.OK,
        content_type: str = "application/octet-stream",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data: https://render.albiononline.com; connect-src 'self'",
        )
        if headers:
            for key, value in headers.items():
                self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, value: Any, *, status: int = HTTPStatus.OK) -> None:
        body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self._send_bytes(body, status=status, content_type="application/json; charset=utf-8")

    def _read_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise ValidationError("Content-Length 無效") from error
        if length <= 0 or length > MAX_BODY_BYTES:
            raise ValidationError("請求內容為空或過大")
        try:
            value = json.loads(self.rfile.read(length))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValidationError("JSON 格式無效") from error
        if not isinstance(value, dict):
            raise ValidationError("JSON 根節點必須是物件")
        return value

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if parsed.path == "/api/health":
            self._send_json({"ok": True})
            return
        if parsed.path == "/api/summary":
            try:
                self._send_json(self.server.database.summary(query.get("period", ["all"])[0]))
            except ValidationError as error:
                self._send_json({"error": str(error)}, status=HTTPStatus.BAD_REQUEST)
            return
        if parsed.path == "/api/purchases":
            try:
                limit = int(query.get("limit", ["100"])[0])
                offset = int(query.get("offset", ["0"])[0])
            except ValueError:
                self._send_json({"error": "limit/offset 必須是整數"}, status=HTTPStatus.BAD_REQUEST)
                return
            self._send_json({"items": self.server.database.list_purchases(limit=limit, offset=offset)})
            return
        if parsed.path == "/api/transactions":
            try:
                limit = int(query.get("limit", ["100"])[0])
                offset = int(query.get("offset", ["0"])[0])
                items = self.server.database.list_transactions(
                    limit=limit,
                    offset=offset,
                    direction=query.get("direction", [None])[0],
                    transaction_kind=query.get("kind", [None])[0],
                    item_category_value=query.get("category", [None])[0],
                    include_sold=query_bool(query, "include_sold"),
                    include_deleted=query_bool(query, "include_deleted"),
                )
            except (ValueError, ValidationError) as error:
                self._send_json({"error": str(error)}, status=HTTPStatus.BAD_REQUEST)
                return
            self._send_json({"items": items})
            return
        if parsed.path == "/api/snapshots":
            try:
                limit = int(query.get("limit", ["40"])[0])
            except ValueError:
                self._send_json({"error": "limit 必須是整數"}, status=HTTPStatus.BAD_REQUEST)
                return
            self._send_json({"items": self.server.database.list_snapshots(limit=limit)})
            return
        if parsed.path == "/api/projects":
            self._send_json({"items": self.server.database.list_projects()})
            return
        if parsed.path == "/api/crafting/catalog":
            self._send_json({**catalog(), "market": market_config()})
            return
        if parsed.path == "/api/market/prices":
            item_ids: list[str] = []
            for value in query.get("ids", []):
                item_ids.extend(part.strip() for part in value.split(",") if part.strip())
            item_ids = list(dict.fromkeys(item_ids))[:40]
            cluster = query.get("server", ["east"])[0]
            location = query.get("location", ["Martlock"])[0]
            local = self.server.database.estimate_item_prices(item_ids)
            warning = None
            try:
                remote = fetch_market_prices(item_ids, cluster, location)
            except MarketError as error:
                remote = []
                warning = str(error)
            remote_by_id = {item["item_id"]: item for item in remote}
            items = []
            for item_id in item_ids:
                value = remote_by_id.get(item_id, {
                    "item_id": item_id, "city": location, "quality": 1,
                    "sell_price_min": 0, "sell_price_min_date": None,
                    "buy_price_max": 0, "buy_price_max_date": None,
                    "source": "albion_online_data",
                })
                value["local_estimate"] = local.get(item_id)
                items.append(value)
            self._send_json({"items": items, "server": cluster, "location": location, "warning": warning})
            return
        if parsed.path == "/api/ledger/prices":
            item_ids: list[str] = []
            for value in query.get("ids", []):
                item_ids.extend(part.strip() for part in value.split(",") if part.strip())
            item_ids = list(dict.fromkeys(item_ids))[:100]
            estimates = self.server.database.estimate_item_prices(item_ids)
            self._send_json({"items": [{"item_id": item_id, **estimates.get(item_id, {})} for item_id in item_ids]})
            return
        if parsed.path == "/api/settings":
            self._send_json(self.server.database.get_settings())
            return
        if parsed.path == "/api/mails":
            try:
                limit = int(query.get("limit", ["100"])[0])
            except ValueError:
                self._send_json({"error": "limit 必須是整數"}, status=HTTPStatus.BAD_REQUEST)
                return
            self._send_json({"items": self.server.database.list_market_mails(limit)})
            return
        if parsed.path == "/api/catalog":
            item_ids: list[str] = []
            for value in query.get("ids", []):
                item_ids.extend(part.strip() for part in value.split(",") if part.strip())
            item_ids = list(dict.fromkeys(item_ids))[:30]
            cached = self.server.database.catalog_names(item_ids)
            missing = [item_id for item_id in item_ids if item_id not in cached]
            resolved = resolve_item_names(missing)
            self.server.database.cache_item_names(resolved)
            # Only return verified catalog names.  Transaction responses already
            # contain a local fallback, and a fallback must not overwrite a
            # user's manually supplied Chinese name in the browser.
            names = {item_id: cached.get(item_id) or resolved.get(item_id)
                     for item_id in item_ids if cached.get(item_id) or resolved.get(item_id)}
            self._send_json({"names": names})
            return
        if parsed.path == "/api/export.csv":
            body = ("\ufeff" + self.server.database.export_csv()).encode("utf-8")
            self._send_bytes(
                body,
                content_type="text/csv; charset=utf-8",
                headers={"Content-Disposition": 'attachment; filename="albion-purchases.csv"'},
            )
            return

        relative = "index.html" if parsed.path == "/" else parsed.path.lstrip("/")
        candidate = (self.server.web_root / relative).resolve()
        if candidate != self.server.web_root and self.server.web_root not in candidate.parents:
            self._send_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)
            return
        if not candidate.is_file():
            self._send_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)
            return
        content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in {"application/javascript", "application/json"}:
            content_type += "; charset=utf-8"
        self._send_bytes(candidate.read_bytes(), content_type=content_type)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            if path == "/api/events":
                event = self._read_json()
                event_type = event.get("type")
                if event_type in {"purchase", "sale", "transaction"}:
                    inserted, row_id = self.server.database.insert_transaction(event)
                    self._send_json({"ok": True, "inserted": inserted, "id": row_id})
                elif event_type == "status":
                    self.server.database.update_status(event)
                    self._send_json({"ok": True})
                elif event_type == "capture_warning":
                    self.server.database.insert_warning(event)
                    self._send_json({"ok": True})
                elif event_type == "mail_metadata":
                    self.server.database.upsert_mail_metadata(event)
                    self._send_json({"ok": True})
                elif event_type == "mail_resolution":
                    self.server.database.resolve_market_mail(event)
                    self._send_json({"ok": True})
                else:
                    raise ValidationError("不支援的事件 type")
                return
            if path == "/api/projects":
                payload = self._read_json()
                selections = payload.get("selections")
                if not isinstance(selections, list):
                    raise ValidationError("selections 必須是陣列")
                project_id = self.server.database.create_project(payload.get("name", ""), selections, payload)
                self._send_json({"ok": True, "id": project_id}, status=HTTPStatus.CREATED)
                return
            if path == "/api/crafting/plan":
                self._send_json(build_refining_plan(self._read_json()))
                return
            if path == "/api/crafting/transmutation":
                self._send_json(build_transmutation_options(self._read_json()))
                return
            if path == "/api/transactions":
                payload = self._read_json()
                payload.setdefault("type", "transaction")
                payload.setdefault("source_event_id", f"manual:{uuid.uuid4()}")
                payload.setdefault("source", "manual_entry")
                payload.setdefault("confidence", "manual")
                inserted, row_id = self.server.database.insert_transaction(payload)
                self._send_json({"ok": True, "inserted": inserted, "id": row_id}, status=HTTPStatus.CREATED)
                return
            self._send_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)
        except (CraftingValidationError, ValidationError) as error:
            self._send_json({"error": str(error)}, status=HTTPStatus.BAD_REQUEST)
        except Exception as error:  # Keep the capture client alive; details stay local.
            print(f"[ERROR] ingest failed: {error}")
            self._send_json({"error": "伺服器無法處理事件"}, status=HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_PUT(self) -> None:  # noqa: N802
        self._update_resource()

    def do_PATCH(self) -> None:  # noqa: N802
        self._update_resource()

    def _update_resource(self) -> None:
        path = urlparse(self.path).path
        try:
            payload = self._read_json()
            transaction_prefix = "/api/transactions/"
            project_prefix = "/api/projects/"
            if path == "/api/settings":
                self._send_json({"ok": True, "settings": self.server.database.update_settings(payload)})
                return
            if path.startswith(transaction_prefix):
                transaction_id = int(path[len(transaction_prefix):])
                item = self.server.database.update_transaction(transaction_id, payload)
                self._send_json({"ok": True, "item": item})
                return
            if path.startswith(project_prefix):
                project_id = int(path[len(project_prefix):])
                item = self.server.database.update_project(project_id, payload)
                self._send_json({"ok": True, "item": item})
                return
            self._send_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)
        except (TypeError, ValueError, ValidationError) as error:
            self._send_json({"error": str(error)}, status=HTTPStatus.BAD_REQUEST)

    def do_DELETE(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            if path.startswith("/api/projects/"):
                found = self.server.database.delete_project(int(path.removeprefix("/api/projects/")))
                label = "專案"
            elif path.startswith("/api/transactions/"):
                found = self.server.database.delete_transaction(int(path.removeprefix("/api/transactions/")))
                label = "成交紀錄"
            else:
                self._send_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)
                return
            self._send_json({"ok": True} if found else {"error": f"找不到{label}"},
                            status=HTTPStatus.OK if found else HTTPStatus.NOT_FOUND)
        except (ValueError, ValidationError) as error:
            self._send_json({"error": str(error)}, status=HTTPStatus.BAD_REQUEST)


def run_server(database: Database, host: str, port: int, web_root: Path) -> None:
    server = TrackerServer((host, port), database, web_root)
    print(f"Albion 市場帳本服務已啟動：http://{host}:{port}")
    print(f"SQLite：{database.path}")
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\n正在關閉…")
    finally:
        server.shutdown()
        server.server_close()


def serve_in_thread(database: Database, host: str, port: int, web_root: Path) -> tuple[TrackerServer, threading.Thread]:
    server = TrackerServer((host, port), database, web_root)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread
