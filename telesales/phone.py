"""แปลงเบอร์โทรไทยทุกรูปแบบให้เป็น E.164 (+66...) ที่ระบบโทรศัพท์ต้องการ."""
from __future__ import annotations

import re


def normalize_th(raw: str | None) -> str | None:
    """'081-234-5678' / '66812345678' / '812345678' (Excel ตัด 0 หน้า) -> '+66812345678'.

    คืน None ถ้าไม่ใช่เบอร์ไทยที่โทรได้
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if s.endswith(".0"):  # Excel แปลงเบอร์เป็นตัวเลข float
        s = s[:-2]
    digits = re.sub(r"\D", "", s)
    if digits.startswith("66"):
        national = digits[2:]
    elif digits.startswith("0"):
        national = digits[1:]
    else:
        national = digits
    # มือถือ 6x/8x/9x = 9 หลัก, เบอร์บ้าน 2-7 = 8 หลัก (ไม่รวม 0 นำหน้า)
    if len(national) == 9 and national[0] in "689":
        return "+66" + national
    if len(national) == 8 and national[0] in "234567":
        return "+66" + national
    return None
