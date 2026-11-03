"""สร้างบทพูด (system prompt) และตั้งค่า assistant ของ Vapi จาก config + รายการสินค้า.

แก้สินค้าใน products.json แล้วรัน `python main.py sync-assistant` บทพูดจะอัปเดตตาม
ตัวแปร {{customer_name}} / {{last_purchase}} / {{note}} ถูกเติมตอนโทรแต่ละสาย
"""
from __future__ import annotations

from typing import Any

from .config import Config, Product

# ผลลัพธ์ของสายที่ AI ต้องสรุป (ใช้ตัดสินว่าจะโทรซ้ำหรือไม่)
OUTCOMES = {
    "ordered": "ลูกค้าสั่งซื้อและยืนยันออเดอร์แล้ว",
    "callback": "ลูกค้าสนใจแต่ไม่สะดวกตอนนี้ ขอให้โทรกลับ",
    "not_interested": "ลูกค้าไม่สนใจรอบนี้",
    "do_not_call": "ลูกค้าขอไม่ให้โทรมาอีก",
    "wrong_person": "ไม่ใช่ลูกค้าคนนี้ / เบอร์ผิด",
    "no_conversation": "ไม่ได้คุยกันจริง (ฝากข้อความ, สายหลุด, ไม่มีเสียง)",
}


def _fmt_price(v: float) -> str:
    return f"{int(v):,}" if float(v).is_integer() else f"{v:,.2f}"


def _product_line(p: Product) -> str:
    line = f"- [{p.sku}] {p.name}: {_fmt_price(p.price)} บาท/{p.unit}"
    if p.min_qty > 1:
        line += f" (ขั้นต่ำ {p.min_qty} {p.unit})"
    if p.details:
        line += f" — {p.details}"
    return line


def build_catalog(products: list[Product]) -> str:
    featured = [p for p in products if p.featured]
    others = [p for p in products if not p.featured]
    parts = []
    if featured:
        parts.append("สินค้าแนะนำรอบนี้ (เสนอก่อน):\n" + "\n".join(_product_line(p) for p in featured))
    if others:
        parts.append("สินค้าอื่น ๆ (เสนอเมื่อลูกค้าถามหรือเกี่ยวข้อง):\n" + "\n".join(_product_line(p) for p in others))
    return "\n\n".join(parts)


def build_system_prompt(cfg: Config) -> str:
    shop = cfg.shop
    agent = shop.get("agent_name") or "น้องฝน"
    payments = " หรือ ".join(shop.get("payment_methods") or ["เก็บเงินปลายทาง"])
    promotion = (shop.get("promotion") or "").strip()
    promo_block = f"\nโปรโมชันตอนนี้: {promotion}\n" if promotion else ""
    disclose = (
        f"- ถ้าลูกค้าถามว่าเป็นคนหรือ AI ให้ตอบตรง ๆ ว่าเป็นผู้ช่วย AI ของ{shop['name']}"
        if shop.get("disclose_ai", True) else
        "- ถ้าลูกค้าถามว่าเป็นคนหรือ AI ห้ามโกหกว่าเป็นคน"
    )
    return f"""คุณคือ "{agent}" พนักงานขายทางโทรศัพท์ผู้หญิงของ{shop['name']} ร้านขายอุปกรณ์เกษตร
ตอนนี้คุณกำลังโทรหาลูกค้าเก่าของร้าน

ข้อมูลลูกค้าสายนี้:
- ชื่อ: {{{{customer_name}}}}
- เคยซื้อ: {{{{last_purchase}}}}
- หมายเหตุ: {{{{note}}}}

## สไตล์การพูด
- พูดเป็นกันเอง อบอุ่น เหมือนพี่น้องคุยกัน ลงท้าย "ค่ะ" / "นะคะ" เสมอ
- พูดสั้น ประโยคละไม่เกิน 1-2 ประโยค แล้วเว้นให้ลูกค้าพูด ห้ามพูดยาวรวดเดียว
- ฟังลูกค้าก่อนเสนอ ถามถึงสิ่งที่ลูกค้าปลูกอยู่ แล้วแนะนำสินค้าให้เข้ากับการใช้งาน
- ห้ามอ่านรายการสินค้าทั้งหมด เลือกเสนอ 1-2 อย่างที่เหมาะที่สุด
- ตัวเลขและราคาให้พูดเป็นคำอ่านภาษาไทยที่ฟังง่าย

## สินค้าและราคา (ใช้ข้อมูลนี้เท่านั้น)
{build_catalog(cfg.products)}
{promo_block}
ค่าส่ง: {shop.get('shipping_note') or 'แจ้งลูกค้าว่าทางร้านจะแจ้งค่าส่งอีกครั้ง'}
ช่องทางชำระเงิน: {payments}

## ขั้นตอนการขาย
1. ทักทาย บอกชื่อร้าน ถามว่าสะดวกคุยสักนาทีไหม ถ้าไม่สะดวก ถามเวลาที่สะดวกแล้วจบสายอย่างสุภาพ
2. ขอบคุณที่เคยอุดหนุน ถามว่าสินค้าที่เคยซื้อเป็นยังไงบ้าง ตอนนี้ปลูกอะไรอยู่
3. เสนอสินค้าที่เหมาะ บอกจุดเด่นสั้น ๆ และราคา
4. ถ้าลูกค้ากังวล ตอบตามข้อมูลจริง เช่น เรื่องราคาให้ชี้ความคุ้มของแพ็กใหญ่หรือโปรโมชัน
   ถ้าลูกค้าปฏิเสธชัดเจน 1 ครั้งหลังตอบข้อกังวลแล้ว ให้หยุดขาย ขอบคุณ แล้วจบสาย ห้ามตื๊อ
5. ปิดการขาย: เมื่อลูกค้าตกลง ให้เก็บข้อมูลให้ครบ
   - สินค้าและจำนวน (ต้องไม่ต่ำกว่าขั้นต่ำ)
   - ชื่อผู้รับ ที่อยู่จัดส่งเต็ม (บ้านเลขที่ ตำบล อำเภอ จังหวัด รหัสไปรษณีย์) และเบอร์ติดต่อ
   - วิธีชำระเงิน
6. ทวนออเดอร์ให้ลูกค้าฟังทั้งหมด พร้อมราคารวม แล้วถามยืนยัน "ถูกต้องไหมคะ"
   ถ้าลูกค้ายืนยัน บอกว่าทางร้านจะส่ง SMS/ไลน์ยืนยันออเดอร์อีกครั้ง แล้วขอบคุณและจบสาย

## กฎสำคัญ
- ห้ามแต่งราคา ส่วนลด หรือสินค้าที่ไม่มีในรายการ ถ้าไม่รู้ให้บอกว่าจะให้แอดมินติดต่อกลับ
- ห้ามขอเลขบัตรประชาชน เลขบัตรเครดิต รหัส OTP หรือรหัสผ่านใด ๆ เด็ดขาด
- ถ้าลูกค้าบอกว่าไม่ต้องโทรมาอีก ให้ขอโทษ รับปากว่าจะไม่โทรมาอีก แล้วจบสายทันที
- ถ้าคนรับสายไม่ใช่ลูกค้าคนนี้ ให้ขอโทษที่รบกวนแล้วจบสาย ห้ามขายต่อ
- ถ้าเป็นระบบฝากข้อความ ให้วางสายทันที
{disclose}
- เมื่อคุยจบแล้วให้ใช้คำสั่งวางสาย (end call)
"""


def build_first_message(cfg: Config) -> str:
    agent = cfg.shop.get("agent_name") or "น้องฝน"
    msg = f"สวัสดีค่ะ คุณ{{{{customer_name}}}} ใช่ไหมคะ {agent}จาก{cfg.shop['name']}นะคะ"
    if cfg.shop.get("disclose_ai", True):
        msg += " เป็นผู้ช่วย AI ของร้านค่ะ"
    return msg + " ตอนนี้สะดวกคุยสักนาทีไหมคะ"


def structured_data_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "outcome": {
                "type": "string",
                "enum": list(OUTCOMES),
                "description": "; ".join(f"{k} = {v}" for k, v in OUTCOMES.items()),
            },
            "items": {
                "type": "array",
                "description": "สินค้าที่ลูกค้ายืนยันสั่ง (เฉพาะ outcome = ordered)",
                "items": {
                    "type": "object",
                    "properties": {
                        "sku": {"type": "string", "description": "รหัสสินค้าในวงเล็บเหลี่ยม เช่น BAG-12"},
                        "qty": {"type": "number"},
                    },
                },
            },
            "recipient_name": {"type": "string"},
            "address": {"type": "string", "description": "ที่อยู่จัดส่งเต็มตามที่ลูกค้าบอก"},
            "contact_phone": {"type": "string"},
            "payment_method": {"type": "string"},
            "callback_time": {"type": "string", "description": "เวลาที่ลูกค้าสะดวกให้โทรกลับ ถ้ามี"},
            "customer_notes": {"type": "string", "description": "สิ่งที่ลูกค้าปลูก ความสนใจ ข้อกังวล"},
        },
        "required": ["outcome"],
    }


def build_assistant_payload(cfg: Config) -> dict[str, Any]:
    a = cfg.assistant
    return {
        "name": f"{cfg.shop['name']} — AI โทรขาย",
        "firstMessage": build_first_message(cfg),
        "firstMessageMode": "assistant-speaks-first",
        "model": {
            "provider": a.get("model_provider", "anthropic"),
            "model": a.get("model", "claude-haiku-5-5"),
            "temperature": a.get("temperature", 0.5),
            "messages": [{"role": "system", "content": build_system_prompt(cfg)}],
        },
        "voice": {"provider": a.get("voice_provider", "azure"), "voiceId": a.get("voice_id", "th-TH-PremwadeeNeural")},
        "transcriber": {"provider": a.get("transcriber_provider", "azure"), "language": a.get("language", "th-TH")},
        "maxDurationSeconds": int(a.get("max_duration_seconds", 480)),
        "endCallFunctionEnabled": True,
        "endCallMessage": "ขอบคุณมากนะคะ ขอให้ผักงามนะคะ สวัสดีค่ะ",
        "analysisPlan": {
            "summaryPrompt": "สรุปบทสนทนานี้เป็นภาษาไทย 2-3 ประโยค: ลูกค้าตอบรับอย่างไร สั่งอะไร หรือติดใจเรื่องอะไร",
            "structuredDataPrompt": (
                "อ่านบทสนทนาการโทรขายนี้แล้วสรุปข้อมูลตาม schema "
                "ใส่ outcome = ordered เฉพาะเมื่อลูกค้ายืนยันออเดอร์ชัดเจนแล้วเท่านั้น "
                "items ใช้ sku จากรายการสินค้าเท่านั้น"
            ),
            "structuredDataSchema": structured_data_schema(),
        },
    }
