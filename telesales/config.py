"""โหลด config.json + products.json และแปลงค่า "env:NAME" เป็นค่าจาก environment."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, time
from typing import Any
from zoneinfo import ZoneInfo

_ENV_RE = re.compile(r"^env:(?P<name>[A-Za-z_][A-Za-z0-9_]*)$")


class ConfigError(Exception):
    """ข้อผิดพลาดของไฟล์ config / products."""


# ชื่อ key ที่กรอกได้จากหน้า dashboard (เก็บใน secrets.json ซึ่งไม่ขึ้น Git)
SECRET_KEYS = ("VAPI_API_KEY", "VAPI_PHONE_NUMBER_ID", "VAPI_ASSISTANT_ID")


def resolve_env(value: Any, secrets: dict[str, str] | None = None) -> str:
    """"env:NAME" -> ค่าของ env NAME หรือจาก secrets.json (ว่างถ้าไม่ได้ตั้ง), ค่าอื่นคืนตามเดิม."""
    if not isinstance(value, str):
        return "" if value is None else str(value)
    m = _ENV_RE.match(value.strip())
    if m:
        name = m.group("name")
        return (os.environ.get(name) or (secrets or {}).get(name) or "").strip()
    return value.strip()


def load_secrets(path: str) -> dict[str, str]:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return {k: str(v) for k, v in data.items() if k in SECRET_KEYS and v}


def save_secrets(path: str, updates: dict[str, str]) -> None:
    """อัปเดตเฉพาะ key ที่ส่งมาและไม่ว่าง (ค่าว่าง = คงค่าเดิม)."""
    data = load_secrets(path)
    for k, v in updates.items():
        if k in SECRET_KEYS and str(v or "").strip():
            data[k] = str(v).strip()
    write_json(path, data)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def write_json(path: str, data: Any) -> None:
    """เขียนไฟล์แบบ atomic (เขียนไฟล์ชั่วคราวก่อนแล้วค่อยแทนที่) กันไฟล์เสียถ้าเครื่องดับกลางทาง."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)


@dataclass
class Product:
    sku: str
    name: str
    price: float
    unit: str = "ชิ้น"
    min_qty: int = 1
    details: str = ""
    role: str = "main"  # main = สินค้าหลัก, upsell = เสนอเพิ่มเมื่อซื้อ, backup = เสนอแทนเมื่อไม่สนใจ, other = ตอบเมื่อถูกถาม

    @property
    def featured(self) -> bool:  # ไฟล์รุ่นก่อนใช้ featured=true แทน role=main
        return self.role == "main"


@dataclass
class Calling:
    timezone: str = "Asia/Bangkok"
    days: list[int] = field(default_factory=lambda: [0, 1, 2, 3, 4, 5])  # 0 = จันทร์
    start: str = "09:00"
    end: str = "19:00"
    max_calls_per_run: int = 10
    seconds_between_calls: float = 3
    max_attempts: int = 3
    retry_after_hours: float = 24

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def is_open(self, now: datetime) -> bool:
        """ตอนนี้อยู่ในช่วงเวลาที่อนุญาตให้โทรหรือไม่."""
        local = now.astimezone(self.tz)
        if local.weekday() not in self.days:
            return False
        return _parse_hhmm(self.start) <= local.time() < _parse_hhmm(self.end)


@dataclass
class Config:
    shop: dict[str, Any]
    calling: Calling
    vapi_api_key: str
    vapi_phone_number_id: str
    vapi_assistant_id: str
    assistant: dict[str, Any]
    products: list[Product]

    def product(self, sku: str) -> Product | None:
        for p in self.products:
            if p.sku.lower() == str(sku).strip().lower():
                return p
        return None


def _parse_hhmm(s: str) -> time:
    try:
        h, m = str(s).split(":")
        return time(int(h), int(m))
    except ValueError as e:
        raise ConfigError(f"เวลาไม่ถูกต้อง: {s!r} (ต้องเป็น HH:MM)") from e


def _read_json(path: str) -> dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError as e:
        raise ConfigError(f"ไม่พบไฟล์ {path}") from e
    except json.JSONDecodeError as e:
        raise ConfigError(f"{path} ไม่ใช่ JSON ที่ถูกต้อง: {e}") from e


def load_products(path: str) -> list[Product]:
    products = parse_products(_read_json(path).get("products") or [])
    if not products:
        raise ConfigError(f"{path} ยังไม่มีสินค้า")
    return products


PRODUCT_ROLES = ("main", "upsell", "backup", "other")


def _product_role(p: dict[str, Any]) -> str:
    role = str(p.get("role") or "").strip().lower()
    if not role:  # products.json จากโปรแกรมรุ่นก่อน
        return "main" if p.get("featured") else "other"
    if role not in PRODUCT_ROLES:
        raise ConfigError(f"บทบาทสินค้าไม่ถูกต้อง: {role!r}")
    return role


def parse_products(raw: list[dict[str, Any]]) -> list[Product]:
    """ตรวจสอบรายการสินค้า (ใช้ทั้งตอนโหลดไฟล์และตอนบันทึกจาก dashboard)."""
    products: list[Product] = []
    seen: set[str] = set()
    for i, p in enumerate(raw):
        try:
            prod = Product(
                sku=str(p["sku"]).strip(),
                name=str(p["name"]).strip(),
                price=float(p["price"]),
                unit=str(p.get("unit") or "ชิ้น"),
                min_qty=int(p.get("min_qty") or 1),
                details=str(p.get("details") or ""),
                role=_product_role(p),
            )
        except (KeyError, TypeError, ValueError) as e:
            raise ConfigError(f"สินค้าลำดับที่ {i + 1} ต้องมี sku, name, price: {e}") from e
        if not prod.sku or not prod.name:
            raise ConfigError(f"สินค้าลำดับที่ {i + 1} ต้องมีรหัสและชื่อสินค้า")
        if prod.price < 0 or prod.min_qty < 1:
            raise ConfigError(f"{prod.name}: ราคาต้องไม่ติดลบ และขั้นต่ำต้องอย่างน้อย 1")
        if prod.sku.lower() in seen:
            raise ConfigError(f"รหัสสินค้าซ้ำ: {prod.sku}")
        seen.add(prod.sku.lower())
        products.append(prod)
    return products


def load_config(config_path: str, products_path: str, secrets_path: str | None = None) -> Config:
    raw = _read_json(config_path)
    secrets = load_secrets(secrets_path) if secrets_path else {}
    shop = dict(raw.get("shop") or {})
    if not shop.get("name"):
        raise ConfigError("ต้องตั้ง shop.name (ชื่อร้าน) ใน config.json")
    calling = Calling(**(raw.get("calling") or {}))
    _parse_hhmm(calling.start)
    _parse_hhmm(calling.end)
    vapi = raw.get("vapi") or {}
    return Config(
        shop=shop,
        calling=calling,
        vapi_api_key=resolve_env(vapi.get("api_key", "env:VAPI_API_KEY"), secrets),
        vapi_phone_number_id=resolve_env(vapi.get("phone_number_id", "env:VAPI_PHONE_NUMBER_ID"), secrets),
        vapi_assistant_id=resolve_env(vapi.get("assistant_id", "env:VAPI_ASSISTANT_ID"), secrets),
        assistant=dict(raw.get("assistant") or {}),
        products=load_products(products_path),
    )
