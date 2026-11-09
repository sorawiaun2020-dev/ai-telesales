"""ตัวเลขสำหรับหน้าภาพรวม (ยอดขาย, อัตราปิดการขาย, อัปเซล, สินค้าขายดี, กราฟรายวัน) และไฟล์ Excel."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from . import db as dbm
from . import xlsx
from .config import Config
from .export import OUTCOME_TH, STATUS_TH, delivery, price_breakdown, price_order

ORDER_STATUS_TH = {"": "รอยืนยัน", "confirmed": "ยืนยันแล้ว", "shipped": "ส่งแล้ว", "cancelled": "ยกเลิก"}
_NOT_TALKED = ("no_answer", "unknown", None)


def _local_date(iso: str, cfg: Config) -> str:
    try:
        return datetime.fromisoformat(iso).astimezone(cfg.calling.tz).date().isoformat()
    except (TypeError, ValueError):
        return ""


def order_of(cfg: Config, data: dict) -> dict[str, Any]:
    """ราคา/สินค้าของออเดอร์ คิดจากราคาใน products.json เสมอ (ไม่เชื่อตัวเลขที่ AI พูด)."""
    items = data.get("items") or []
    lines, total, warnings = price_order(cfg, items)
    found = (cfg.product(str(i.get("sku") or "")) for i in items)
    roles = {p.role for p in found if p}
    return {"lines": lines, "total": total, "warnings": warnings,
            "has_upsell": "upsell" in roles, "has_backup": "backup" in roles,
            "revenue": price_breakdown(cfg, items)["revenue"]}


def dashboard(cfg: Config, db: dbm.DB, days: int = 14, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    today = now.astimezone(cfg.calling.tz).date()
    series = {(today - timedelta(days=i)).isoformat(): {"calls": 0, "orders": 0, "revenue": 0.0}
              for i in range(days - 1, -1, -1)}
    k = {"calls": 0, "talked": 0, "orders": 0, "revenue": 0.0, "today_orders": 0, "today_revenue": 0.0,
         "today_calls": 0, "cost": 0.0, "upsell_offered": 0, "upsell_won": 0, "backup_offered": 0,
         "backup_won": 0, "pending_orders": 0}
    outcomes: dict[str, int] = {}
    products: dict[str, dict[str, Any]] = {}

    for r in db.report_calls():
        k["cost"] += float(r["cost"] or 0)
        if not r["outcome"]:
            continue  # ยังไม่จบสาย
        day = _local_date(r["created_at"], cfg)
        k["calls"] += 1
        outcomes[r["outcome"]] = outcomes.get(r["outcome"], 0) + 1
        if day in series:
            series[day]["calls"] += 1
        if day == today.isoformat():
            k["today_calls"] += 1
        if r["outcome"] not in _NOT_TALKED:
            k["talked"] += 1
        data = json.loads(r["data_json"] or "{}")
        if data.get("upsell_offered"):
            k["upsell_offered"] += 1
        if data.get("backup_offered"):
            k["backup_offered"] += 1
        if r["outcome"] != "ordered" or r["order_status"] == "cancelled":
            continue
        o = order_of(cfg, data)
        k["orders"] += 1
        k["revenue"] += o["total"]
        k["pending_orders"] += r["order_status"] == ""
        k["upsell_won"] += bool(data.get("upsell_offered") and o["has_upsell"])
        k["backup_won"] += bool(data.get("backup_offered") and o["has_backup"])
        if day in series:
            series[day]["orders"] += 1
            series[day]["revenue"] += o["total"]
        if day == today.isoformat():
            k["today_orders"] += 1
            k["today_revenue"] += o["total"]
        for it in data.get("items") or []:
            p = cfg.product(str(it.get("sku") or ""))
            if not p:
                continue
            try:
                qty = float(it.get("qty") or 0)
            except (TypeError, ValueError):
                qty = 0
            row = products.setdefault(p.sku, {"sku": p.sku, "name": p.name, "unit": p.unit, "role": p.role,
                                              "qty": 0.0, "revenue": 0.0})
            row["qty"] += qty
            row["revenue"] += o["revenue"].get(p.sku, 0.0)

    def rate(a: int, b: int) -> float | None:
        return round(a / b, 4) if b else None

    k.update(
        close_rate=rate(k["orders"], k["talked"]),
        answer_rate=rate(k["talked"], k["calls"]),
        upsell_rate=rate(k["upsell_won"], k["upsell_offered"]),
        backup_rate=rate(k["backup_won"], k["backup_offered"]),
        avg_order=round(k["revenue"] / k["orders"], 2) if k["orders"] else None,
        cost_per_order=round(k["cost"] / k["orders"], 2) if k["orders"] else None,
    )
    return {
        "kpi": k,
        "series": [{"date": d, **v} for d, v in series.items()],
        "outcomes": outcomes,
        "top_products": sorted(products.values(), key=lambda x: -x["revenue"])[:8],
        "customers": db.status_counts(),
    }


def customer_row(cfg: Config, r) -> dict[str, Any]:
    """แถวลูกค้า + ผลการโทรล่าสุด (สำหรับตารางในโปรแกรมและไฟล์ Excel)."""
    d = dict(r)
    data = json.loads(d.pop("last_data", None) or "{}")
    d["order"] = order_of(cfg, data) if d.get("last_outcome") == "ordered" else None
    d["callback_time"] = data.get("callback_time") or ""
    d["customer_notes"] = data.get("customer_notes") or ""
    return d


def customers_xlsx(cfg: Config, db: dbm.DB) -> bytes:
    rows = [["ชื่อ", "เบอร์โทร", "ที่อยู่", "สถานะ", "ผลการโทรล่าสุด", "สินค้าที่สั่ง", "ยอดเงิน (บาท)", "สถานะออเดอร์",
             "โทรแล้ว (ครั้ง)", "โทรล่าสุด", "เวลาที่สะดวกให้โทรกลับ", "สรุปสาย", "เคยซื้อ", "หมายเหตุ"]]
    for r in db.all_customers_with_last_call():
        c = customer_row(cfg, r)
        o = c["order"]
        rows.append([
            c["name"], "0" + c["phone"][3:] if c["phone"].startswith("+66") else c["phone"], c.get("address") or "",
            STATUS_TH.get(c["status"], c["status"]),
            OUTCOME_TH.get(c["last_outcome"] or "", "") if c["last_outcome"] else "",
            " | ".join(o["lines"]) if o else "", f"{o['total']:.0f}" if o else "",
            ORDER_STATUS_TH.get(c["order_status"] or "", "") if o else "",
            str(c["attempts"]), _fmt_time(c["last_call_at"], cfg), c["callback_time"],
            c["last_summary"] or "", c["last_purchase"], c["note"],
        ])
    return xlsx.write(rows, widths=[22, 14, 40, 14, 18, 40, 14, 14, 12, 18, 22, 60, 28, 28])


def orders_xlsx(cfg: Config, db: dbm.DB) -> bytes:
    rows = [["วันที่โทร", "ชื่อลูกค้า", "เบอร์", "สินค้า", "ยอดรวม (บาท)", "อัปเซล", "สถานะออเดอร์", "ชื่อผู้รับ",
             "ที่อยู่", "เบอร์ติดต่อ", "ชำระเงิน", "ต้องตรวจสอบ", "สรุปสาย", "ไฟล์เสียง"]]
    for r in db.calls_with_outcome("ordered"):
        data = json.loads(r["data_json"] or "{}")
        o = order_of(cfg, data)
        dv = delivery(data, r)
        warnings = list(o["warnings"]) + ([] if dv["address"] else ["ไม่มีที่อยู่"])
        rows.append([
            _fmt_time(r["created_at"], cfg), r["customer_name"] or "", "0" + r["phone"][3:],
            " | ".join(o["lines"]), f"{o['total']:.0f}", "ใช่" if o["has_upsell"] else "",
            ORDER_STATUS_TH.get(r["order_status"] or "", ""), dv["recipient"],
            dv["address"], dv["contact_phone"], data.get("payment_method", ""),
            "; ".join(warnings), r["summary"] or "", r["recording_url"] or "",
        ])
    return xlsx.write(rows, widths=[18, 22, 14, 44, 14, 10, 14, 22, 50, 16, 18, 30, 60, 40], sheet_name="ออเดอร์")


def _fmt_time(iso: str | None, cfg: Config) -> str:
    if not iso:
        return ""
    try:
        return datetime.fromisoformat(iso).astimezone(cfg.calling.tz).strftime("%d/%m/%Y %H:%M")
    except ValueError:
        return iso
