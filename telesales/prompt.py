"""สร้างบทพูด (system prompt) และตั้งค่า assistant ของ Vapi จาก config + รายการสินค้า.

แก้สินค้าใน products.json แล้วรัน `python main.py sync-assistant` บทพูดจะอัปเดตตาม
ตัวแปร {{customer_name}} / {{last_purchase}} / {{note}} / {{address}} ถูกเติมตอนโทรแต่ละสาย
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
    groups = (
        ("main", "สินค้าหลัก (เสนอก่อนเสมอ)"),
        ("upsell", "สินค้าอัปเซล (เสนอเพิ่ม 1 ครั้ง หลังลูกค้าตกลงซื้อสินค้าหลักแล้ว)"),
        ("backup", "สินค้าสำรอง (เสนอแทน 1 ครั้ง เมื่อลูกค้าไม่สนใจสินค้าหลัก)"),
        ("other", "สินค้าอื่น ๆ (ตอบเมื่อลูกค้าถามเท่านั้น)"),
    )
    parts = []
    for role, title in groups:
        items = [p for p in products if p.role == role]
        if items:
            parts.append(f"{title}:\n" + "\n".join(_product_line(p) for p in items))
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
- ที่อยู่เดิม: {{{{address}}}}

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
3. เสนอ "สินค้าหลัก" ที่เหมาะกับลูกค้า บอกจุดเด่นสั้น ๆ และราคา
4. ถ้าลูกค้ากังวล ตอบตามข้อมูลจริง เช่น เรื่องราคาให้ชี้ความคุ้มของแพ็กใหญ่หรือโปรโมชัน
5. ถ้าลูกค้าไม่สนใจสินค้าหลัก และมี "สินค้าสำรอง" ให้เสนอสินค้าสำรองที่เหมาะที่สุด 1 ครั้ง
   (เช่น "ถ้ายังไม่ต้องการ... ตอนนี้มี... ราคา... สนใจไหมคะ")
   ถ้าลูกค้ายังปฏิเสธ ให้หยุดขาย ขอบคุณ แล้วจบสาย ห้ามตื๊อ
6. ถ้าลูกค้าตกลงซื้อ และมี "สินค้าอัปเซล" ให้เสนอสินค้าอัปเซลที่เข้ากับสิ่งที่ลูกค้าซื้อ 1 ครั้ง ก่อนสรุปออเดอร์
   (เช่น "รับ...เพิ่มด้วยไหมคะ ใช้คู่กันแล้ว...") ถ้าลูกค้าไม่รับก็ไปขั้นต่อไปทันที
7. ปิดการขาย: เก็บข้อมูลให้ครบ
   - สินค้าและจำนวน (ต้องไม่ต่ำกว่าขั้นต่ำ)
   - ที่อยู่จัดส่ง:
     * ถ้ามี "ที่อยู่เดิม" (ไม่ใช่ -) ห้ามขอให้ลูกค้าบอกที่อยู่ใหม่ ให้ทวนที่อยู่เดิมแบบย่อแล้วถามยืนยัน
       เช่น "ส่งที่อยู่เดิม บ้านเลขที่... อำเภอ... จังหวัด... เหมือนเดิมใช่ไหมคะ"
       ถ้าลูกค้าบอกว่าใช่ ใช้ที่อยู่เดิมได้เลย ไม่ต้องถามอะไรเพิ่ม
       ถามที่อยู่ใหม่เฉพาะเมื่อลูกค้าบอกว่าย้ายหรือขอส่งที่อื่น
     * ถ้าไม่มีที่อยู่เดิม ให้ถามที่อยู่เต็ม (บ้านเลขที่ ตำบล อำเภอ จังหวัด รหัสไปรษณีย์)
   - ชื่อผู้รับและเบอร์ติดต่อ: ใช้ชื่อลูกค้าและเบอร์ที่โทรอยู่ ถามเฉพาะเมื่อลูกค้าบอกว่าจะให้คนอื่นรับ
   - วิธีชำระเงิน
8. ทวนออเดอร์ให้ลูกค้าฟังทั้งหมด พร้อมราคารวม แล้วถามยืนยัน "ถูกต้องไหมคะ"
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
            "recipient_name": {"type": "string", "description": "ชื่อผู้รับ เฉพาะเมื่อไม่ใช่ลูกค้าเอง"},
            "use_existing_address": {"type": "boolean",
                                     "description": "true ถ้าลูกค้ายืนยันให้ส่งที่อยู่เดิมที่ทวนให้ฟัง"},
            "address": {"type": "string",
                        "description": "ที่อยู่ใหม่เต็มตามที่ลูกค้าบอก เฉพาะเมื่อไม่ใช้ที่อยู่เดิม (ถ้าใช้ที่อยู่เดิมให้เว้นว่าง)"},
            "contact_phone": {"type": "string"},
            "payment_method": {"type": "string"},
            "callback_time": {"type": "string", "description": "เวลาที่ลูกค้าสะดวกให้โทรกลับ ถ้ามี"},
            "upsell_offered": {"type": "boolean", "description": "ได้เสนอสินค้าอัปเซลในสายนี้หรือไม่"},
            "backup_offered": {"type": "boolean", "description": "ได้เสนอสินค้าสำรองในสายนี้หรือไม่"},
            "customer_notes": {"type": "string", "description": "สิ่งที่ลูกค้าปลูก ความสนใจ ข้อกังวล"},
        },
        "required": ["outcome"],
    }


# AI รอให้ลูกค้าเงียบนานแค่ไหนก่อนตอบ (วินาที) — ภาษาไทยไม่มีเครื่องหมายจบประโยค
# Vapi จึงใช้ onNoPunctuationSeconds เป็นหลัก ซึ่งค่าเริ่มต้นคือ 1.5 วินาที (ช้า)
RESPONSE_SPEEDS = {
    "fast": {"waitSeconds": 0.2, "onPunctuationSeconds": 0.1, "onNoPunctuationSeconds": 0.6, "onNumberSeconds": 0.5},
    "normal": {"waitSeconds": 0.4, "onPunctuationSeconds": 0.1, "onNoPunctuationSeconds": 1.0, "onNumberSeconds": 0.6},
    "patient": {"waitSeconds": 0.6, "onPunctuationSeconds": 0.3, "onNoPunctuationSeconds": 1.5, "onNumberSeconds": 0.8},
}


def build_start_speaking_plan(speed: str) -> dict[str, Any]:
    s = RESPONSE_SPEEDS.get(speed) or RESPONSE_SPEEDS["fast"]
    return {
        "waitSeconds": s["waitSeconds"],
        "transcriptionEndpointingPlan": {k: s[k] for k in ("onPunctuationSeconds", "onNoPunctuationSeconds",
                                                             "onNumberSeconds")},
    }


def build_voice(a: dict[str, Any]) -> dict[str, Any]:
    voice: dict[str, Any] = {"provider": a.get("voice_provider") or "azure",
                             "voiceId": a.get("voice_id") or "th-TH-PremwadeeNeural"}
    # รุ่นของเสียง (เช่น ElevenLabs) — รุ่นที่ Vapi เลือกให้เองอาจพูดไทยไม่ได้ จึงให้ระบุเองได้
    if str(a.get("voice_model") or "").strip():
        voice["model"] = str(a["voice_model"]).strip()
    if str(a.get("voice_language") or "").strip():
        voice["language"] = str(a["voice_language"]).strip()
    return voice


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
        "voice": build_voice(a),
        "transcriber": {"provider": a.get("transcriber_provider", "azure"), "language": a.get("language", "th-TH")},
        "maxDurationSeconds": int(a.get("max_duration_seconds", 480)),
        "startSpeakingPlan": build_start_speaking_plan(str(a.get("response_speed") or "fast")),
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
