"""ตรรกะหลัก: เลือกคนที่จะโทร -> สั่งโทร -> ดึงผล -> ตัดสินว่าจบ / โทรซ้ำ / ห้ามโทร."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from . import db as dbm
from .config import Config
from .vapi import VapiClient, VapiError

log = logging.getLogger("telesales")

# endedReason ของ Vapi ที่แปลว่า "ไม่ได้คุยกับลูกค้า" -> โทรใหม่รอบหน้า
_NO_ANSWER_HINTS = ("did-not-answer", "no-answer", "busy", "voicemail", "failed", "silence", "error", "unreachable")

# ผลของสาย -> สถานะลูกค้า
_OUTCOME_STATUS = {
    "ordered": dbm.DONE,
    "not_interested": dbm.DONE,
    "wrong_person": dbm.DONE,
    "do_not_call": dbm.DNC,
    "callback": dbm.RETRY,
    "no_answer": dbm.RETRY,
    "no_conversation": dbm.RETRY,
    "unknown": dbm.RETRY,
}


@dataclass
class RunReport:
    placed: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    skipped_reason: str = ""


def call_variables(customer) -> dict[str, str]:
    return {
        "customer_name": customer["name"] or "ลูกค้า",
        "last_purchase": customer["last_purchase"] or "ไม่มีข้อมูล",
        "note": customer["note"] or "-",
    }


def place_calls(cfg: Config, db: dbm.DB, client: VapiClient | None, *, dry_run: bool = False,
                force: bool = False, limit: int | None = None, now: datetime | None = None,
                sleep=time.sleep) -> RunReport:
    report = RunReport()
    now = now or datetime.now(timezone.utc)
    if not force and not cfg.calling.is_open(now):
        report.skipped_reason = (
            f"นอกเวลาโทร ({cfg.calling.start}-{cfg.calling.end} วัน {cfg.calling.days}) — ใช้ --force ถ้าจะโทรจริง ๆ"
        )
        return report
    if not dry_run:
        if not cfg.vapi_assistant_id:
            raise VapiError("ยังไม่มี VAPI_ASSISTANT_ID — รัน `python main.py sync-assistant` ก่อน")
        if not cfg.vapi_phone_number_id:
            raise VapiError("ยังไม่ได้ตั้ง VAPI_PHONE_NUMBER_ID (เบอร์โทรออกใน Vapi)")

    due = db.due_customers(limit or cfg.calling.max_calls_per_run, cfg.calling.max_attempts,
                           cfg.calling.retry_after_hours, now)
    for i, c in enumerate(due):
        if dry_run:
            log.info("[จำลอง] จะโทรหา %s (%s) ครั้งที่ %d", c["name"], c["phone"], c["attempts"] + 1)
            report.placed.append(c["phone"])
            continue
        if i and cfg.calling.seconds_between_calls:
            sleep(cfg.calling.seconds_between_calls)
        try:
            res = client.create_call(
                assistant_id=cfg.vapi_assistant_id, phone_number_id=cfg.vapi_phone_number_id,
                number=c["phone"], name=c["name"], variables=call_variables(c),
            )
        except VapiError as e:
            log.error("โทรหา %s ไม่สำเร็จ: %s", c["phone"], e)
            report.errors.append(f"{c['phone']}: {e}")
            if "429" in str(e) or "concurrency" in str(e).lower():
                break  # สายเต็มโควตา — รอบหน้าค่อยโทรต่อ
            continue
        call_id = res.get("id")
        if not call_id:
            report.errors.append(f"{c['phone']}: Vapi ไม่ส่ง call id กลับมา")
            continue
        db.record_call_started(call_id, c["phone"], now)
        report.placed.append(c["phone"])
        log.info("โทรหา %s (%s) แล้ว call=%s", c["name"], c["phone"], call_id)
    return report


def classify(call: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """ดูผลจาก Vapi แล้วคืน (outcome, structured_data)."""
    analysis = call.get("analysis") or {}
    data = analysis.get("structuredData") or {}
    if not isinstance(data, dict):
        data = {}
    outcome = str(data.get("outcome") or "").strip()
    if outcome == "no_conversation":
        return "no_answer", data
    if outcome in _OUTCOME_STATUS:
        return outcome, data
    reason = str(call.get("endedReason") or "").lower()
    if any(h in reason for h in _NO_ANSWER_HINTS):
        return "no_answer", data
    return "unknown", data


def _transcript(call: dict[str, Any]) -> str | None:
    return (call.get("artifact") or {}).get("transcript") or call.get("transcript")


def _recording(call: dict[str, Any]) -> str | None:
    return (call.get("artifact") or {}).get("recordingUrl") or call.get("recordingUrl")


def sync_results(db: dbm.DB, client: VapiClient, max_attempts: int) -> dict[str, int]:
    """ดึงผลของทุกสายที่ยังไม่จบ. คืนจำนวนแยกตาม outcome."""
    counts: dict[str, int] = {}
    for row in db.pending_calls():
        try:
            call = client.get_call(row["id"])
        except VapiError as e:
            log.warning("ดึงผล call %s ไม่ได้: %s", row["id"], e)
            continue
        status = call.get("status") or "unknown"
        if status != "ended":
            continue
        outcome, data = classify(call)
        customer_status = _OUTCOME_STATUS[outcome]
        cust = db.customer(row["phone"])
        if customer_status == dbm.RETRY and cust and cust["attempts"] >= max_attempts:
            customer_status = dbm.DONE  # โทรครบจำนวนครั้งแล้ว
        db.record_call_result(
            row["id"], status="ended", ended_reason=call.get("endedReason"), outcome=outcome,
            summary=(call.get("analysis") or {}).get("summary"), data=data or None,
            recording_url=_recording(call), transcript=_transcript(call),
            cost=call.get("cost"), customer_status=customer_status,
        )
        counts[outcome] = counts.get(outcome, 0) + 1
        log.info("ผล %s: %s", row["phone"], outcome)
    return counts
