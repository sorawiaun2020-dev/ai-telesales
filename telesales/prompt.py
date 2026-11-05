"""สร้างบทพูด (system prompt) และตั้งค่า assistant ของ Vapi จาก config + รายการสินค้า.

แก้สินค้าใน products.json แล้วรัน `python main.py sync-assistant` บทพูดจะอัปเดตตาม
ตัวแปร {{customer_name}} / {{last_purchase}} / {{note}} / {{address}} ถูกเติมตอนโทรแต่ละสาย
"""
from __future__ import annotations

import re
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


# ---------- บทพูดที่ผู้ใช้แก้เองได้ (หน้า "บทพูด AI") ----------
# ผู้ใช้เขียนบทพูดด้วยคำในวงเล็บปีกกา เช่น {ชื่อลูกค้า} แล้วระบบแทนค่าให้ตอนส่งไป Vapi
# ข้อมูลร้าน/สินค้า แทนค่าทันที, ข้อมูลลูกค้า แปลงเป็นตัวแปรของ Vapi ({{customer_name}}) ให้เติมตอนโทรแต่ละสาย
SHOP_TOKENS = {
    "ชื่อร้าน": "ชื่อร้านจากหน้าตั้งค่า",
    "ชื่อพนักงาน": "ชื่อพนักงาน AI จากหน้าตั้งค่า",
    "รายการสินค้า": "สินค้า ราคา ขั้นต่ำ จุดเด่น แยกตามบทบาท จากหน้าสินค้า",
    "ค่าส่ง": "ค่าส่งและระยะเวลาจัดส่ง จากหน้าตั้งค่า",
    "ช่องทางชำระเงิน": "ช่องทางชำระเงิน จากหน้าตั้งค่า",
    "โปรโมชัน": "โปรโมชันตอนนี้ จากหน้าตั้งค่า",
}
CUSTOMER_TOKENS = {
    "ชื่อลูกค้า": ("customer_name", "ชื่อลูกค้าที่กำลังโทรหา"),
    "สินค้าที่เคยซื้อ": ("last_purchase", "สินค้าที่ลูกค้าเคยซื้อ จากรายชื่อลูกค้า"),
    "หมายเหตุ": ("note", "หมายเหตุของลูกค้า จากรายชื่อลูกค้า"),
    "ที่อยู่เดิม": ("address", "ที่อยู่ของลูกค้า จากรายชื่อลูกค้า (ถ้าไม่มีจะเป็น -)"),
}
FIRST_MESSAGE_TOKENS = {"แนะนำตัวว่าเป็นAI": "ประโยค \"เป็นผู้ช่วย AI ของร้านค่ะ\" (ถ้าเปิดไว้ในหน้าตั้งค่า)"}

DEFAULT_FIRST_MESSAGE = "สวัสดีค่ะ คุณ{ชื่อลูกค้า} ใช่ไหมคะ {ชื่อพนักงาน}จาก{ชื่อร้าน}นะคะ{แนะนำตัวว่าเป็นAI} ตอนนี้สะดวกคุยสักนาทีไหมคะ"

DEFAULT_SYSTEM_PROMPT = """คุณคือ "{ชื่อพนักงาน}" พนักงานขายทางโทรศัพท์ผู้หญิงของ{ชื่อร้าน} ร้านขายอุปกรณ์เกษตร
ตอนนี้คุณกำลังโทรหาลูกค้าเก่าของร้าน

ข้อมูลลูกค้าสายนี้:
- ชื่อ: {ชื่อลูกค้า}
- เคยซื้อ: {สินค้าที่เคยซื้อ}
- หมายเหตุ: {หมายเหตุ}
- ที่อยู่เดิม: {ที่อยู่เดิม}

## สไตล์การพูด
- พูดเป็นกันเอง อบอุ่น เหมือนพี่น้องคุยกัน ลงท้าย "ค่ะ" / "นะคะ" เสมอ
- พูดสั้น ประโยคละไม่เกิน 1-2 ประโยค แล้วเว้นให้ลูกค้าพูด ห้ามพูดยาวรวดเดียว
- ฟังลูกค้าก่อนเสนอ ถามถึงสิ่งที่ลูกค้าปลูกอยู่ แล้วแนะนำสินค้าให้เข้ากับการใช้งาน
- ห้ามอ่านรายการสินค้าทั้งหมด เลือกเสนอ 1-2 อย่างที่เหมาะที่สุด
- ตัวเลขและราคาให้พูดเป็นคำอ่านภาษาไทยที่ฟังง่าย

## สินค้าและราคา (ใช้ข้อมูลนี้เท่านั้น)
{รายการสินค้า}

โปรโมชันตอนนี้: {โปรโมชัน}
ค่าส่ง: {ค่าส่ง}
ช่องทางชำระเงิน: {ช่องทางชำระเงิน}

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
"""

_TOKEN_RE = re.compile(r"\{([^{}\n]{1,40})\}")


def fixed_rules(cfg: Config) -> str:
    """กฎที่ต่อท้ายบทพูดเสมอ ผู้ใช้แก้ไม่ได้ (ความปลอดภัย ความซื่อตรง และการจบสาย)."""
    shop = cfg.shop
    disclose = (
        f"- ถ้าลูกค้าถามว่าเป็นคนหรือ AI ให้ตอบตรง ๆ ว่าเป็นผู้ช่วย AI ของ{shop['name']}"
        if shop.get("disclose_ai", True) else
        "- ถ้าลูกค้าถามว่าเป็นคนหรือ AI ห้ามโกหกว่าเป็นคน ให้ตอบตามจริงว่าเป็นผู้ช่วย AI ของร้าน"
    )
    return f"""## กฎสำคัญ (ระบบบังคับ)
- ห้ามแต่งราคา ส่วนลด หรือสินค้าที่ไม่มีในรายการ ถ้าไม่รู้ให้บอกว่าจะให้แอดมินติดต่อกลับ
- ห้ามขอเลขบัตรประชาชน เลขบัตรเครดิต รหัส OTP หรือรหัสผ่านใด ๆ เด็ดขาด
- ถ้าลูกค้าบอกว่าไม่ต้องโทรมาอีก ให้ขอโทษ รับปากว่าจะไม่โทรมาอีก แล้วจบสายทันที
- ถ้าคนรับสายไม่ใช่ลูกค้าคนนี้ ให้ขอโทษที่รบกวนแล้วจบสาย ห้ามขายต่อ
- ถ้าเป็นระบบฝากข้อความ ให้วางสายทันที
{disclose}
- เมื่อคุยจบแล้วให้ใช้คำสั่งวางสาย (end call)"""


def _shop_values(cfg: Config) -> dict[str, str]:
    shop = cfg.shop
    return {
        "ชื่อร้าน": shop["name"],
        "ชื่อพนักงาน": shop.get("agent_name") or "น้องฝน",
        "รายการสินค้า": build_catalog(cfg.products),
        "ค่าส่ง": shop.get("shipping_note") or "แจ้งลูกค้าว่าทางร้านจะแจ้งค่าส่งอีกครั้ง",
        "ช่องทางชำระเงิน": " หรือ ".join(shop.get("payment_methods") or ["เก็บเงินปลายทาง"]),
        "โปรโมชัน": (shop.get("promotion") or "").strip() or "ไม่มีโปรโมชันพิเศษตอนนี้",
        "แนะนำตัวว่าเป็นAI": " เป็นผู้ช่วย AI ของร้านค่ะ" if shop.get("disclose_ai", True) else "",
    }


def render_template(text: str, cfg: Config) -> str:
    values = _shop_values(cfg)

    def sub(m: re.Match) -> str:
        key = m.group(1).strip()
        if key in values:
            return values[key]
        if key in CUSTOMER_TOKENS:
            return "{{" + CUSTOMER_TOKENS[key][0] + "}}"
        return m.group(0)  # ไม่รู้จัก -> ปล่อยไว้ตามเดิม (หน้าโปรแกรมจะเตือน)

    return _TOKEN_RE.sub(sub, text)


def unknown_tokens(text: str) -> list[str]:
    known = set(SHOP_TOKENS) | set(CUSTOMER_TOKENS) | set(FIRST_MESSAGE_TOKENS)
    return sorted({m.group(1).strip() for m in _TOKEN_RE.finditer(text)} - known)


def script_texts(cfg: Config) -> tuple[str, str]:
    """(ประโยคแรก, บทพูด) ที่ผู้ใช้แก้ไว้ หรือค่าเริ่มต้นถ้ายังไม่ได้แก้."""
    sc = cfg.script or {}
    return (str(sc.get("first_message") or "").strip() or DEFAULT_FIRST_MESSAGE,
            str(sc.get("system_prompt") or "").strip() or DEFAULT_SYSTEM_PROMPT)


def build_system_prompt(cfg: Config) -> str:
    return render_template(script_texts(cfg)[1], cfg).rstrip() + "\n\n" + fixed_rules(cfg) + "\n"


def build_first_message(cfg: Config) -> str:
    return render_template(script_texts(cfg)[0], cfg)


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
