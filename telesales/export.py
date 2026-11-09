"""ส่งออกผลเป็นไฟล์ CSV (เปิดใน Excel ได้) — ออเดอร์, รายชื่อโทรกลับ, รายงานสรุป."""
from __future__ import annotations

import csv
import json
import os

from . import db as dbm
from .config import Config

OUTCOME_TH = {
    "ordered": "สั่งซื้อ",
    "callback": "ขอให้โทรกลับ",
    "not_interested": "ไม่สนใจ",
    "do_not_call": "ขอไม่ให้โทรอีก",
    "wrong_person": "เบอร์ผิด/ไม่ใช่ลูกค้า",
    "no_answer": "ไม่รับสาย/ไม่ได้คุย",
    "unknown": "ไม่ทราบผล",
    "รอผล": "รอผล",
}

STATUS_TH = {
    dbm.NEW: "ยังไม่โทร",
    dbm.CALLING: "กำลังโทร/รอผล",
    dbm.RETRY: "รอโทรซ้ำ",
    dbm.DONE: "จบแล้ว",
    dbm.DNC: "ห้ามโทร",
}


def price_breakdown(cfg: Config, items: list[dict]) -> dict:
    """คิดราคาจาก products.json เอง (ไม่เชื่อตัวเลขที่ AI พูด) รวมโปรคละได้.

    คืน {"lines", "total", "warnings", "revenue": {sku: ยอดของสินค้านั้น}} — สินค้าในโปรแบ่งยอดตามสัดส่วนจำนวน
    """
    lines, total, warnings = [], 0.0, []
    revenue: dict[str, float] = {}
    groups: dict[str, list[tuple[object, float]]] = {}
    for it in items or []:
        sku = str(it.get("sku") or "")
        try:
            qty = float(it.get("qty") or 0)
        except (TypeError, ValueError):
            qty = 0
        p = cfg.product(sku)
        if not p:
            warnings.append(f"ไม่รู้จักสินค้า {sku!r}")
            continue
        if qty < p.min_qty:
            warnings.append(f"{p.name} สั่ง {qty:g} ต่ำกว่าขั้นต่ำ {p.min_qty}")
        promo = cfg.promo_of(p.sku)
        if promo and qty > 0:
            groups.setdefault(promo.name, []).append((p, qty))
            continue
        sub = p.price * qty
        total += sub
        revenue[p.sku] = revenue.get(p.sku, 0.0) + sub
        lines.append(f"{p.name} x{qty:g} = {sub:,.0f}")
    for promo in cfg.promos:
        members = groups.get(promo.name)
        if not members:
            continue
        n = int(round(sum(q for _, q in members)))
        unit = max(p.price for p, _ in members)
        cost, split = promo.best_price(n, unit)
        total += cost
        for p, q in members:
            revenue[p.sku] = revenue.get(p.sku, 0.0) + cost * q / n
        picked = " + ".join(f"{q} {members[0][0].unit}" + (f" x{k}" if k > 1 else "") for q, k in split)
        lines.append(f"โปร{promo.name}: " + ", ".join(f"{p.name} x{q:g}" for p, q in members)
                     + f" (รวม {n} {members[0][0].unit}: {picked}) = {cost:,.0f}")
    if not lines:
        warnings.append("ไม่มีรายการสินค้า")
    return {"lines": lines, "total": total, "warnings": warnings, "revenue": revenue}


def price_order(cfg: Config, items: list[dict]) -> tuple[list[str], float, list[str]]:
    """คืน (บรรทัดสินค้า, ยอดรวม, คำเตือน)."""
    b = price_breakdown(cfg, items)
    return b["lines"], b["total"], b["warnings"]


def delivery(data: dict, row) -> dict[str, str]:
    """ผู้รับ/ที่อยู่/เบอร์ของออเดอร์: ที่อยู่ใหม่ที่ลูกค้าบอก หรือที่อยู่เดิมเมื่อลูกค้ายืนยัน "ส่งที่เดิม"."""
    def col(k: str) -> str:
        try:
            return str(row[k] or "")
        except (KeyError, IndexError):
            return ""
    new_addr = str(data.get("address") or "").strip()
    old_addr = (col("address") or col("customer_address")).strip()  # สายจากโปรแกรมรุ่นก่อนไม่มีสำเนาที่อยู่
    if new_addr:
        address, source = new_addr, "ที่อยู่ใหม่"
    elif old_addr and data.get("use_existing_address") is not False:
        address, source = old_addr, "ที่อยู่เดิม"
    else:
        address, source = "", ""
    phone = col("phone")
    return {
        "recipient": str(data.get("recipient_name") or "").strip() or col("customer_name"),
        "address": address,
        "address_source": source,
        "contact_phone": str(data.get("contact_phone") or "").strip() or ("0" + phone[3:] if phone.startswith("+66") else phone),
    }


def export_orders(cfg: Config, db: dbm.DB, path: str) -> int:
    rows = db.calls_with_outcome("ordered")
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["วันที่โทร", "ชื่อลูกค้า", "เบอร์", "สินค้า", "ยอดรวม(บาท)", "ชื่อผู้รับ", "ที่อยู่",
                    "เบอร์ติดต่อ", "ชำระเงิน", "ต้องตรวจสอบ", "สรุปสาย", "ไฟล์เสียง"])
        for r in rows:
            data = json.loads(r["data_json"] or "{}")
            lines, total, warnings = price_order(cfg, data.get("items") or [])
            dv = delivery(data, r)
            if not dv["address"]:
                warnings.append("ไม่มีที่อยู่")
            w.writerow([r["created_at"], r["customer_name"], r["phone"], " | ".join(lines), f"{total:.0f}",
                        dv["recipient"], dv["address"], dv["contact_phone"],
                        data.get("payment_method", ""), "; ".join(warnings), r["summary"] or "",
                        r["recording_url"] or ""])
    return len(rows)


def export_callbacks(db: dbm.DB, path: str) -> int:
    rows = db.calls_with_outcome("callback")
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["วันที่โทร", "ชื่อลูกค้า", "เบอร์", "เวลาที่สะดวก", "ความสนใจ", "สรุปสาย", "ไฟล์เสียง"])
        for r in rows:
            data = json.loads(r["data_json"] or "{}")
            w.writerow([r["created_at"], r["customer_name"], r["phone"], data.get("callback_time", ""),
                        data.get("customer_notes", ""), r["summary"] or "", r["recording_url"] or ""])
    return len(rows)


def report_text(db: dbm.DB) -> str:
    status = db.status_counts()
    outcomes = db.outcome_counts()
    total = sum(status.values())
    lines = [f"ลูกค้าทั้งหมด {total:,} คน"]
    lines += [f"  {STATUS_TH.get(k, k)}: {v:,}" for k, v in sorted(status.items())]
    calls = sum(outcomes.values())
    lines.append(f"โทรไปแล้ว {calls:,} สาย")
    lines += [f"  {OUTCOME_TH.get(k, k)}: {v:,}" for k, v in sorted(outcomes.items(), key=lambda kv: -kv[1])]
    talked = calls - outcomes.get("no_answer", 0) - outcomes.get("รอผล", 0)
    if talked > 0:
        lines.append(f"อัตราปิดการขาย (จากสายที่ได้คุย): {outcomes.get('ordered', 0) / talked:.1%}")
    lines.append(f"ค่าใช้จ่าย Vapi รวม: ${db.total_cost():,.2f}")
    return "\n".join(lines)


def export_all(cfg: Config, db: dbm.DB, out_dir: str) -> dict[str, int]:
    os.makedirs(out_dir, exist_ok=True)
    return {
        "orders.csv": export_orders(cfg, db, os.path.join(out_dir, "orders.csv")),
        "callbacks.csv": export_callbacks(db, os.path.join(out_dir, "callbacks.csv")),
    }
