"""หน้าเว็บ Dashboard — ตั้งค่าร้าน, สินค้า, ลูกค้า, สั่งโทร และดูผล โดยไม่ต้องแก้ไฟล์เอง.

รันด้วย `python main.py web` แล้วเปิด http://localhost:8080
ใช้ Python มาตรฐานล้วน (http.server) ไม่ต้องติดตั้ง library เพิ่ม
"""
from __future__ import annotations

import base64
import collections
import hmac
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import db as dbm
from .campaign import call_variables, place_calls, sync_results
from .config import (SECRET_KEYS, Calling, Config, ConfigError, load_config, load_secrets,
                     parse_products, save_secrets, write_json, _read_json)
from .export import OUTCOME_TH, STATUS_TH, export_all, price_order
from .phone import normalize_th
from .prompt import build_assistant_payload, build_first_message, build_system_prompt
from .vapi import VapiClient, VapiError

log = logging.getLogger("telesales")

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
MAX_UPLOAD = 50 * 1024 * 1024  # รายชื่อหลายหมื่นแถวยังเล็กกว่านี้มาก

_SHOP_FIELDS = ("name", "agent_name", "disclose_ai", "payment_methods", "shipping_note", "promotion")
_CALLING_FIELDS = tuple(Calling.__dataclass_fields__)
_ASSISTANT_FIELDS = ("model_provider", "model", "temperature", "voice_provider", "voice_id",
                     "transcriber_provider", "language", "max_duration_seconds")


class Paths:
    def __init__(self, base: str):
        env = os.environ.get
        self.config = env("CONFIG_PATH") or os.path.join(base, "config.json")
        self.products = env("PRODUCTS_PATH") or os.path.join(base, "products.json")
        self.secrets = env("SECRETS_PATH") or os.path.join(base, "secrets.json")
        self.db = env("DB_PATH") or os.path.join(base, "telesales.db")
        self.exports = os.path.join(base, "exports")


class LogBuffer(logging.Handler):
    """เก็บ log ล่าสุดไว้แสดงในหน้าเว็บ."""

    def __init__(self, size: int = 200):
        super().__init__()
        self.lines: collections.deque[str] = collections.deque(maxlen=size)
        self.setFormatter(logging.Formatter("%(asctime)s  %(message)s", datefmt="%d/%m %H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self.format(record))


class Campaign:
    """ตัวโทรอัตโนมัติที่ทำงานเบื้องหลัง: ทุก N นาที ดึงผล + โทรชุดใหม่ (เฉพาะในเวลาโทร)."""

    def __init__(self, paths: Paths):
        self.paths = paths
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.interval_mins = 5.0
        self.last_run: str = ""

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self, interval_mins: float) -> None:
        if self.running:
            return
        self.interval_mins = max(1.0, float(interval_mins))
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="campaign", daemon=True)
        self._thread.start()
        log.info("เริ่มโทรอัตโนมัติ ทุก %g นาที", self.interval_mins)

    def stop(self) -> None:
        self._stop.set()
        log.info("หยุดโทรอัตโนมัติแล้ว (สายที่โทรออกไปแล้วจะคุยจนจบ)")

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                cfg = load_config(self.paths.config, self.paths.products, self.paths.secrets)
                client = VapiClient(cfg.vapi_api_key)
                db = dbm.DB(self.paths.db)
                try:
                    sync_results(db, client, cfg.calling.max_attempts)
                    rep = place_calls(cfg, db, client, sleep=self._stop.wait)
                finally:
                    db.close()
                if rep.skipped_reason:
                    log.info(rep.skipped_reason)
                elif rep.placed or rep.errors:
                    log.info("รอบนี้โทร %d สาย, ผิดพลาด %d", len(rep.placed), len(rep.errors))
                else:
                    log.info("ไม่มีรายชื่อที่ถึงคิวโทรตอนนี้")
            except (ConfigError, VapiError) as e:
                log.error("โทรอัตโนมัติผิดพลาด: %s — หยุดทำงาน", e)
                return
            except Exception:  # noqa: BLE001 — ไม่ให้ thread ตายเงียบ ๆ
                log.exception("โทรอัตโนมัติผิดพลาด")
            self.last_run = datetime.now(timezone.utc).isoformat(timespec="seconds")
            self._stop.wait(self.interval_mins * 60)


class App:
    def __init__(self, base: str, password: str = ""):
        self.paths = Paths(base)
        self.password = password
        self.logs = LogBuffer()
        log.addHandler(self.logs)
        self.campaign = Campaign(self.paths)
        self._file_lock = threading.Lock()

    # ---------- helpers ----------
    def cfg(self) -> Config:
        return load_config(self.paths.config, self.paths.products, self.paths.secrets)

    def db(self) -> dbm.DB:
        return dbm.DB(self.paths.db)

    def client(self) -> VapiClient:
        return VapiClient(self.cfg().vapi_api_key)

    # ---------- API ----------
    def state(self) -> dict[str, Any]:
        raw = _read_json(self.paths.config)
        secrets = load_secrets(self.paths.secrets)
        try:
            cfg = self.cfg()
            open_now, cfg_error = cfg.calling.is_open(datetime.now(timezone.utc)), ""
            secret_set = {
                "VAPI_API_KEY": bool(cfg.vapi_api_key),
                "VAPI_PHONE_NUMBER_ID": bool(cfg.vapi_phone_number_id),
                "VAPI_ASSISTANT_ID": bool(cfg.vapi_assistant_id),
            }
        except ConfigError as e:
            open_now, cfg_error = False, str(e)
            secret_set = {k: bool(os.environ.get(k) or secrets.get(k)) for k in SECRET_KEYS}
        db = self.db()
        try:
            stats = {"status": db.status_counts(), "outcomes": db.outcome_counts(), "cost": db.total_cost()}
        finally:
            db.close()
        return {
            "shop": raw.get("shop") or {},
            "calling": raw.get("calling") or {},
            "assistant": raw.get("assistant") or {},
            "products": _read_json(self.paths.products).get("products") or [],
            "secrets": secret_set,
            "stats": stats,
            "labels": {"status": STATUS_TH, "outcome": OUTCOME_TH},
            "campaign": {"running": self.campaign.running, "interval_mins": self.campaign.interval_mins,
                         "last_run": self.campaign.last_run},
            "open_now": open_now,
            "config_error": cfg_error,
        }

    def save_settings(self, body: dict) -> dict:
        with self._file_lock:
            raw = _read_json(self.paths.config)
            for section, fields in (("shop", _SHOP_FIELDS), ("calling", _CALLING_FIELDS),
                                    ("assistant", _ASSISTANT_FIELDS)):
                if isinstance(body.get(section), dict):
                    cur = dict(raw.get(section) or {})
                    cur.update({k: v for k, v in body[section].items() if k in fields})
                    raw[section] = cur
            cal_raw = raw.get("calling") or {}
            for k in ("max_calls_per_run", "max_attempts"):
                if k in cal_raw:
                    cal_raw[k] = max(1, int(cal_raw[k]))
            for k in ("seconds_between_calls", "retry_after_hours"):
                if k in cal_raw:
                    cal_raw[k] = max(0.0, float(cal_raw[k]))
            if "days" in cal_raw:
                cal_raw["days"] = sorted({int(d) for d in cal_raw["days"] if 0 <= int(d) <= 6})
            shop = raw.get("shop") or {}
            if not str(shop.get("name") or "").strip():
                raise ConfigError("กรุณาใส่ชื่อร้าน")
            cal = Calling(**(raw.get("calling") or {}))
            if not cal.days:
                raise ConfigError("ต้องเลือกวันที่โทรได้อย่างน้อย 1 วัน")
            if cal.start >= cal.end:
                raise ConfigError("เวลาเริ่มโทรต้องก่อนเวลาเลิกโทร")
            tmp = self.paths.config + ".check"
            write_json(tmp, raw)
            try:
                load_config(tmp, self.paths.products)  # ตรวจทั้งไฟล์ก่อนบันทึกจริง
            finally:
                os.remove(tmp)
            write_json(self.paths.config, raw)
        if isinstance(body.get("secrets"), dict):
            save_secrets(self.paths.secrets, body["secrets"])
        log.info("บันทึกการตั้งค่าแล้ว")
        return {"ok": True}

    def save_products(self, body: dict) -> dict:
        items = body.get("products")
        if not isinstance(items, list) or not items:
            raise ConfigError("ต้องมีสินค้าอย่างน้อย 1 รายการ")
        products = parse_products(items)
        with self._file_lock:
            raw = _read_json(self.paths.products)
            raw["products"] = [p.__dict__ for p in products]
            write_json(self.paths.products, raw)
        log.info("บันทึกสินค้า %d รายการแล้ว", len(products))
        return {"ok": True, "count": len(products)}

    def sync_assistant(self) -> dict:
        cfg = self.cfg()
        client = VapiClient(cfg.vapi_api_key)
        payload = build_assistant_payload(cfg)
        if cfg.vapi_assistant_id:
            client.update_assistant(cfg.vapi_assistant_id, payload)
            log.info("อัปเดตบทพูด AI บน Vapi แล้ว")
            return {"ok": True, "created": False}
        res = client.create_assistant(payload)
        if not res.get("id"):
            raise VapiError("Vapi ไม่ส่ง assistant id กลับมา")
        save_secrets(self.paths.secrets, {"VAPI_ASSISTANT_ID": res["id"]})
        log.info("สร้าง AI บน Vapi แล้ว (บันทึก Assistant ID ให้อัตโนมัติ)")
        return {"ok": True, "created": True}

    def prompt(self) -> dict:
        cfg = self.cfg()
        return {"first_message": build_first_message(cfg), "system_prompt": build_system_prompt(cfg)}

    def import_customers(self, raw: bytes) -> dict:
        db = self.db()
        try:
            res = db.import_csv_bytes(raw)
        finally:
            db.close()
        log.info("นำเข้ารายชื่อ: ใหม่ %d, อัปเดต %d, เบอร์ผิด %d", res.added, res.updated, res.invalid)
        return {"added": res.added, "updated": res.updated, "invalid": res.invalid,
                "invalid_rows": res.invalid_rows[:50]}

    def customers(self, q: str, status: str, page: int) -> dict:
        db = self.db()
        try:
            rows, total = db.list_customers(q, status, 50, (page - 1) * 50)
        finally:
            db.close()
        return {"total": total, "rows": [dict(r) for r in rows]}

    def set_dnc(self, body: dict) -> dict:
        phone = normalize_th(body.get("phone"))
        status = dbm.DNC if body.get("dnc", True) else dbm.RETRY
        db = self.db()
        try:
            ok = bool(phone) and db.set_status(phone, status)
        finally:
            db.close()
        if not ok:
            raise ConfigError("ไม่พบเบอร์นี้ในรายชื่อ")
        return {"ok": True}

    def calls(self, outcome: str, page: int) -> dict:
        cfg = self.cfg()
        db = self.db()
        try:
            rows, total = db.list_calls(outcome, 50, (page - 1) * 50)
        finally:
            db.close()
        out = []
        for r in rows:
            d = dict(r)
            data = json.loads(d.pop("data_json") or "{}")
            d["data"] = data
            if d["outcome"] == "ordered":
                lines, total_baht, warnings = price_order(cfg, data.get("items") or [])
                d["order"] = {"lines": lines, "total": total_baht, "warnings": warnings}
            out.append(d)
        return {"total": total, "rows": out}

    def preview(self) -> dict:
        cfg = self.cfg()
        db = self.db()
        try:
            rows = db.due_customers(cfg.calling.max_calls_per_run, cfg.calling.max_attempts,
                                    cfg.calling.retry_after_hours)
        finally:
            db.close()
        return {"rows": [dict(r) for r in rows], "open_now": cfg.calling.is_open(datetime.now(timezone.utc))}

    def test_call(self, body: dict) -> dict:
        cfg = self.cfg()
        phone = normalize_th(body.get("phone"))
        if not phone:
            raise ConfigError("เบอร์โทรไม่ถูกต้อง")
        if not cfg.vapi_assistant_id:
            raise ConfigError("ยังไม่ได้สร้าง AI — กด 'อัปเดตบทพูดให้ AI' ในแท็บสินค้าก่อน")
        if not cfg.vapi_phone_number_id:
            raise ConfigError("ยังไม่ได้ใส่ Phone Number ID ในแท็บตั้งค่า")
        name = str(body.get("name") or "ทดสอบ")
        res = VapiClient(cfg.vapi_api_key).create_call(
            assistant_id=cfg.vapi_assistant_id, phone_number_id=cfg.vapi_phone_number_id, number=phone,
            name=name, variables=call_variables({"name": name, "last_purchase": body.get("last_purchase") or "",
                                                 "note": "สายทดสอบ"}),
        )
        log.info("โทรทดสอบหา %s แล้ว", phone)
        return {"ok": True, "call_id": res.get("id")}

    def call_now(self, body: dict) -> dict:
        cfg = self.cfg()
        db = self.db()
        try:
            client = VapiClient(cfg.vapi_api_key)
            sync_results(db, client, cfg.calling.max_attempts)
            limit = int(body.get("limit") or cfg.calling.max_calls_per_run)
            rep = place_calls(cfg, db, client, force=bool(body.get("force")), limit=max(1, min(limit, 500)))
        finally:
            db.close()
        if rep.skipped_reason:
            raise ConfigError(rep.skipped_reason)
        log.info("โทรออก %d สาย, ผิดพลาด %d", len(rep.placed), len(rep.errors))
        return {"placed": len(rep.placed), "errors": rep.errors[:20]}

    def sync(self) -> dict:
        cfg = self.cfg()
        db = self.db()
        try:
            counts = sync_results(db, VapiClient(cfg.vapi_api_key), cfg.calling.max_attempts)
        finally:
            db.close()
        return {"counts": counts}

    def start_campaign(self, body: dict) -> dict:
        cfg = self.cfg()
        missing = [label for label, v in (("API Key", cfg.vapi_api_key), ("Phone Number ID", cfg.vapi_phone_number_id),
                                          ("AI (กด 'อัปเดตบทพูดให้ AI' ก่อน)", cfg.vapi_assistant_id)) if not v]
        if missing:
            raise ConfigError("ยังขาด: " + ", ".join(missing))
        self.campaign.start(float(body.get("interval_mins") or 5))
        return {"ok": True}

    def stop_campaign(self) -> dict:
        self.campaign.stop()
        return {"ok": True}

    def open_folder(self) -> dict:
        folder = os.path.dirname(os.path.abspath(self.paths.config))
        if not hasattr(os, "startfile"):  # มีเฉพาะ Windows
            return {"ok": False, "path": folder}
        os.startfile(folder)  # noqa: S606 — เปิด File Explorer ที่โฟลเดอร์ข้อมูล
        return {"ok": True, "path": folder}

    def export_file(self, name: str) -> str:
        cfg = self.cfg()
        db = self.db()
        try:
            export_all(cfg, db, self.paths.exports)
        finally:
            db.close()
        return os.path.join(self.paths.exports, name)


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = "Telesales"

        def log_message(self, fmt, *args):  # ไม่ต้องพิมพ์ทุก request
            pass

        # ---------- auth ----------
        def _authorized(self) -> bool:
            if not app.password:
                return True
            header = self.headers.get("Authorization") or ""
            if header.startswith("Basic "):
                try:
                    _, _, pw = base64.b64decode(header[6:]).decode("utf-8").partition(":")
                except (ValueError, UnicodeDecodeError):
                    return False
                return hmac.compare_digest(pw.encode(), app.password.encode())
            return False

        def _deny(self):
            self.send_response(HTTPStatus.UNAUTHORIZED)
            self.send_header("WWW-Authenticate", 'Basic realm="AI Telesales"')
            self.send_header("Content-Length", "0")
            self.end_headers()

        # ---------- responses ----------
        def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, data: Any, status: int = 200):
            self._send(status, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def _body(self) -> bytes:
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_UPLOAD:
                raise ConfigError("ไฟล์ใหญ่เกินไป")
            return self.rfile.read(n) if n else b""

        def _json_body(self) -> dict:
            raw = self._body()
            try:
                data = json.loads(raw.decode("utf-8")) if raw else {}
            except (ValueError, UnicodeDecodeError) as e:
                raise ConfigError("ข้อมูลที่ส่งมาไม่ถูกต้อง") from e
            if not isinstance(data, dict):
                raise ConfigError("ข้อมูลที่ส่งมาไม่ถูกต้อง")
            return data

        def _handle(self, method: str):
            if not self._authorized():
                return self._deny()
            url = urlparse(self.path)
            qs = {k: v[0] for k, v in parse_qs(url.query).items()}
            page = max(1, int(qs.get("page") or 1)) if str(qs.get("page") or "1").isdigit() else 1
            route = (method, url.path)
            try:
                if route == ("GET", "/"):
                    with open(os.path.join(STATIC_DIR, "index.html"), "rb") as f:
                        return self._send(200, f.read(), "text/html; charset=utf-8")
                if method == "GET" and url.path in ("/export/orders.csv", "/export/callbacks.csv"):
                    name = url.path.rsplit("/", 1)[1]
                    with open(app.export_file(name), "rb") as f:
                        stamp = datetime.now().strftime("%Y%m%d-%H%M")
                        return self._send(200, f.read(), "text/csv; charset=utf-8", {
                            "Content-Disposition": f'attachment; filename="{name[:-4]}-{stamp}.csv"'})
                handlers = {
                    ("GET", "/api/state"): lambda: app.state(),
                    ("GET", "/api/prompt"): lambda: app.prompt(),
                    ("GET", "/api/customers"): lambda: app.customers(qs.get("q", ""), qs.get("status", ""), page),
                    ("GET", "/api/calls"): lambda: app.calls(qs.get("outcome", ""), page),
                    ("GET", "/api/preview"): lambda: app.preview(),
                    ("GET", "/api/logs"): lambda: {"lines": list(app.logs.lines)},
                    ("POST", "/api/settings"): lambda: app.save_settings(self._json_body()),
                    ("POST", "/api/products"): lambda: app.save_products(self._json_body()),
                    ("POST", "/api/sync-assistant"): lambda: app.sync_assistant(),
                    ("POST", "/api/import"): lambda: app.import_customers(self._body()),
                    ("POST", "/api/dnc"): lambda: app.set_dnc(self._json_body()),
                    ("POST", "/api/test-call"): lambda: app.test_call(self._json_body()),
                    ("POST", "/api/call-now"): lambda: app.call_now(self._json_body()),
                    ("POST", "/api/sync"): lambda: app.sync(),
                    ("POST", "/api/open-folder"): lambda: app.open_folder(),
                    ("POST", "/api/campaign/start"): lambda: app.start_campaign(self._json_body()),
                    ("POST", "/api/campaign/stop"): lambda: app.stop_campaign(),
                }
                fn = handlers.get(route)
                if not fn:
                    return self._json({"error": "ไม่พบหน้านี้"}, 404)
                return self._json(fn())
            except (ConfigError, VapiError) as e:
                return self._json({"error": str(e)}, 400)
            except (TypeError, ValueError) as e:
                return self._json({"error": f"ข้อมูลไม่ถูกต้อง: {e}"}, 400)
            except Exception as e:  # noqa: BLE001
                log.exception("dashboard error")
                return self._json({"error": f"เกิดข้อผิดพลาด: {e}"}, 500)

        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

    return Handler


def create_server(base: str, host: str = "127.0.0.1", port: int = 8080,
                  password: str = "") -> tuple[ThreadingHTTPServer, App]:
    """สร้างเซิร์ฟเวอร์ (ยังไม่เริ่มรับ request). port=0 = ให้ระบบเลือก port ว่างให้."""
    app = App(base, password)
    return ThreadingHTTPServer((host, port), make_handler(app)), app


def serve(base: str, host: str = "127.0.0.1", port: int = 8080, password: str = "") -> None:
    if host not in ("127.0.0.1", "localhost") and not password:
        raise SystemExit("เปิดให้เครื่องอื่นเข้าได้ต้องตั้งรหัสผ่าน: DASHBOARD_PASSWORD=... python main.py web --host 0.0.0.0")
    httpd, app = create_server(base, host, port, password)
    shown = "localhost" if host in ("127.0.0.1", "0.0.0.0") else host
    log.info("เปิด Dashboard แล้ว: http://%s:%d  (กด Ctrl+C เพื่อปิด)", shown, port)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.campaign.stop()
        httpd.server_close()
        time.sleep(0.1)
