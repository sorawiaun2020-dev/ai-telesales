#!/usr/bin/env python3
"""AI Telesales — ให้ AI เสียงผู้หญิงโทรขายให้ฐานลูกค้าเก่า.

การใช้งาน:
    python main.py web                          # เปิดหน้า Dashboard ที่ http://localhost:8080 (แนะนำ)
    python main.py import customers.csv         # นำเข้ารายชื่อ (ซ้ำได้ ไม่โทรซ้ำ)
    python main.py sync-assistant               # สร้าง/อัปเดตบทพูด AI บน Vapi (รันทุกครั้งที่แก้สินค้า)
    python main.py show-prompt                  # ดูบทพูดที่จะส่งให้ AI
    python main.py test-call 0812345678 --name สมชาย   # โทรทดสอบหาเบอร์ตัวเอง
    python main.py call --dry-run               # ดูว่ารอบนี้จะโทรหาใครบ้าง (ไม่โทรจริง)
    python main.py call                         # โทรจริง 1 รอบ (ตาม max_calls_per_run)
    python main.py sync                         # ดึงผลการโทรจาก Vapi
    python main.py run --loop --interval 5      # ดึงผล + โทรรอบใหม่ วนทุก 5 นาที
    python main.py report                       # สรุปผล
    python main.py export                       # ส่งออก orders.csv / callbacks.csv
    python main.py dnc 0812345678               # ใส่เบอร์ในรายชื่อห้ามโทร

ตั้งค่าผ่าน env (หรือกรอกในหน้า Dashboard ซึ่งจะเก็บไว้ใน secrets.json):
    VAPI_API_KEY, VAPI_PHONE_NUMBER_ID, VAPI_ASSISTANT_ID
    DASHBOARD_PASSWORD (ต้องตั้งถ้าเปิด Dashboard ให้เครื่องอื่นเข้าได้)
    CONFIG_PATH (./config.json), PRODUCTS_PATH (./products.json), DB_PATH (./telesales.db)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time

from telesales import db as dbm
from telesales.campaign import call_variables, place_calls, sync_results
from telesales.config import ConfigError, load_config, save_secrets
from telesales.export import export_all, report_text
from telesales.phone import normalize_th
from telesales.prompt import build_assistant_payload, build_first_message, build_system_prompt
from telesales.vapi import VapiClient, VapiError

HERE = os.path.dirname(os.path.abspath(__file__))

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("telesales")


def _path(env: str, default: str) -> str:
    return os.environ.get(env) or os.path.join(HERE, default)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="AI Telesales")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("web", help="เปิดหน้า Dashboard")
    p.add_argument("--host", default="127.0.0.1", help="0.0.0.0 = ให้เครื่องอื่นเข้าได้ (ต้องตั้ง DASHBOARD_PASSWORD)")
    p.add_argument("--port", type=int, default=int(os.environ.get("PORT") or 8080))
    p = sub.add_parser("import", help="นำเข้ารายชื่อลูกค้าจาก CSV")
    p.add_argument("csv")
    sub.add_parser("sync-assistant", help="สร้าง/อัปเดต assistant บน Vapi")
    sub.add_parser("show-prompt", help="แสดงบทพูดของ AI")
    p = sub.add_parser("test-call", help="โทรทดสอบหาเบอร์เดียว (ไม่นับในแคมเปญ)")
    p.add_argument("phone")
    p.add_argument("--name", default="ทดสอบ")
    p.add_argument("--last-purchase", default="ถุงปลูกผัก 12 นิ้ว 50 ใบ")
    for name in ("call", "run"):
        p = sub.add_parser(name, help="โทรรอบใหม่" if name == "call" else "ดึงผล + โทรรอบใหม่")
        p.add_argument("--dry-run", action="store_true", help="แสดงรายชื่อที่จะโทร ไม่โทรจริง")
        p.add_argument("--force", action="store_true", help="โทรแม้อยู่นอกเวลาที่ตั้งไว้")
        p.add_argument("--limit", type=int)
        if name == "run":
            p.add_argument("--loop", action="store_true")
            p.add_argument("--interval", type=float, default=5, help="นาที")
    sub.add_parser("sync", help="ดึงผลการโทรจาก Vapi")
    sub.add_parser("report", help="สรุปผล")
    p = sub.add_parser("export", help="ส่งออก CSV")
    p.add_argument("--out", default=os.path.join(HERE, "exports"))
    p = sub.add_parser("dnc", help="ใส่เบอร์ในรายชื่อห้ามโทร")
    p.add_argument("phone")
    args = ap.parse_args(argv)

    if args.cmd == "web":
        from telesales.web import serve
        serve(HERE, args.host, args.port, os.environ.get("DASHBOARD_PASSWORD", ""))
        return 0

    secrets_path = _path("SECRETS_PATH", "secrets.json")
    try:
        cfg = load_config(_path("CONFIG_PATH", "config.json"), _path("PRODUCTS_PATH", "products.json"), secrets_path)
    except ConfigError as e:
        log.error("config ผิดพลาด: %s", e)
        return 2

    if args.cmd == "show-prompt":
        print("ประโยคแรก:", build_first_message(cfg), "\n")
        print(build_system_prompt(cfg))
        return 0

    db = dbm.DB(_path("DB_PATH", "telesales.db"))
    try:
        if args.cmd == "import":
            res = db.import_csv(args.csv)
            for msg in res.invalid_rows[:20]:
                log.warning(msg)
            log.info("นำเข้าใหม่ %d, อัปเดต %d, เบอร์ผิด %d", res.added, res.updated, res.invalid)
            return 0
        if args.cmd == "report":
            print(report_text(db))
            return 0
        if args.cmd == "export":
            for name, n in export_all(cfg, db, args.out).items():
                log.info("%s: %d แถว -> %s", name, n, os.path.join(args.out, name))
            return 0
        if args.cmd == "dnc":
            phone = normalize_th(args.phone)
            if not phone or not db.set_status(phone, dbm.DNC):
                log.error("ไม่พบเบอร์ %s ในรายชื่อ", args.phone)
                return 1
            log.info("ใส่ %s ในรายชื่อห้ามโทรแล้ว", phone)
            return 0

        dry = getattr(args, "dry_run", False)
        client = None if dry else VapiClient(cfg.vapi_api_key)

        if args.cmd == "sync-assistant":
            payload = build_assistant_payload(cfg)
            if cfg.vapi_assistant_id:
                client.update_assistant(cfg.vapi_assistant_id, payload)
                log.info("อัปเดต assistant %s แล้ว", cfg.vapi_assistant_id)
            else:
                res = client.create_assistant(payload)
                save_secrets(secrets_path, {"VAPI_ASSISTANT_ID": res.get("id") or ""})
                log.info("สร้าง assistant แล้ว (id=%s) บันทึกไว้ใน secrets.json ให้แล้ว", res.get("id"))
            return 0
        if args.cmd == "test-call":
            phone = normalize_th(args.phone)
            if not phone:
                log.error("เบอร์ไม่ถูกต้อง: %s", args.phone)
                return 1
            res = client.create_call(
                assistant_id=cfg.vapi_assistant_id, phone_number_id=cfg.vapi_phone_number_id, number=phone,
                name=args.name, variables=call_variables({"name": args.name, "last_purchase": args.last_purchase,
                                                          "note": "สายทดสอบ"}),
            )
            log.info("กำลังโทรหา %s call=%s — ดูผล/ไฟล์เสียงได้ที่ dashboard.vapi.ai", phone, res.get("id"))
            return 0
        if args.cmd == "sync":
            log.info("ผลที่ได้: %s", json.dumps(sync_results(db, client, cfg.calling.max_attempts), ensure_ascii=False))
            return 0

        while True:  # call / run
            if args.cmd == "run" and not dry:
                sync_results(db, client, cfg.calling.max_attempts)
            rep = place_calls(cfg, db, client, dry_run=dry, force=args.force, limit=args.limit)
            if rep.skipped_reason:
                log.info(rep.skipped_reason)
            else:
                log.info("รอบนี้โทร %d สาย, ผิดพลาด %d", len(rep.placed), len(rep.errors))
            if not getattr(args, "loop", False):
                return 1 if rep.errors and not rep.placed else 0
            time.sleep(args.interval * 60)
    except VapiError as e:
        log.error("%s", e)
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
