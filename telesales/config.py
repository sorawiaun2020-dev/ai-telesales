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
class Promo:
    """โปรคละได้ เช่น "ถุงปลูกผักแสนดี" 1 ชุด 200 / 3 ชุด 500 คละขนาดได้."""
    name: str
    skus: list[str]
    tiers: list[tuple[int, float]]  # (จำนวนชุด, ราคารวม) เรียงจากน้อยไปมาก

    def best_price(self, qty: int, unit_price: float) -> tuple[float, list[tuple[int, int]]]:
        """ราคาที่ถูกที่สุดสำหรับ qty ชุด (คละได้) และวิธีแบ่ง [(ขนาดขั้น, จำนวนครั้ง)].

        ใช้ราคาต่อหน่วยของสินค้าเมื่อไม่มีขั้น 1 ชุด เช่น ขั้น 3 ชุด อย่างเดียว แล้วสั่ง 4 = 500 + 200
        """
        steps = {1: unit_price}
        for q, price in self.tiers:
            steps[q] = min(price, steps.get(q, price))
        best = [0.0] + [float("inf")] * qty
        pick = [0] * (qty + 1)
        for n in range(1, qty + 1):
            for q, price in steps.items():
                if q <= n and best[n - q] + price < best[n]:
                    best[n], pick[n] = best[n - q] + price, q
        used: dict[int, int] = {}
        n = qty
        while n > 0:
            used[pick[n]] = used.get(pick[n], 0) + 1
            n -= pick[n]
        return best[qty], sorted(used.items(), reverse=True)


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
    script: dict[str, Any] = field(default_factory=dict)  # บทพูดที่ผู้ใช้แก้เอง (ว่าง = ใช้ค่าเริ่มต้น)
    promos: list[Promo] = field(default_factory=list)

    def product(self, sku: str) -> Product | None:
        for p in self.products:
            if p.sku.lower() == str(sku).strip().lower():
                return p
        return None

    def promo_of(self, sku: str) -> Promo | None:
        key = str(sku).strip().lower()
        for pr in self.promos:
            if key in (s.lower() for s in pr.skus):
                return pr
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


def load_promos(path: str, products: list[Product]) -> list[Promo]:
    return parse_promos(_read_json(path).get("promos") or [], products)


def parse_promos(raw: list[dict[str, Any]], products: list[Product]) -> list[Promo]:
    known = {p.sku.lower(): p.sku for p in products}
    promos: list[Promo] = []
    taken: dict[str, str] = {}
    for i, r in enumerate(raw):
        name = str(r.get("name") or "").strip()
        if not name:
            raise ConfigError(f"โปรลำดับที่ {i + 1} ต้องมีชื่อโปร")
        skus = []
        for s in r.get("skus") or []:
            k = str(s).strip().lower()
            if k not in known:
                continue  # สินค้าถูกลบไปแล้ว — ข้าม
            if k in taken:
                raise ConfigError(f"สินค้า {known[k]} อยู่ทั้งโปร \"{taken[k]}\" และ \"{name}\" — สินค้า 1 ตัวอยู่ได้โปรเดียว")
            taken[k] = name
            skus.append(known[k])
        if not skus:
            raise ConfigError(f"โปร \"{name}\" ยังไม่ได้เลือกสินค้า")
        tiers = []
        for t in r.get("tiers") or []:
            try:
                q, price = int(t.get("qty")), float(t.get("price"))
            except (TypeError, ValueError, AttributeError) as e:
                raise ConfigError(f"โปร \"{name}\": จำนวนและราคาต้องเป็นตัวเลข") from e
            if q < 1 or price < 0:
                raise ConfigError(f"โปร \"{name}\": จำนวนต้องอย่างน้อย 1 และราคาต้องไม่ติดลบ")
            tiers.append((q, price))
        if not tiers:
            raise ConfigError(f"โปร \"{name}\" ต้องมีราคาอย่างน้อย 1 ขั้น")
        promos.append(Promo(name=name, skus=skus, tiers=sorted(dict(tiers).items())))
    return promos


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
    products = load_products(products_path)
    return Config(
        shop=shop,
        calling=calling,
        vapi_api_key=resolve_env(vapi.get("api_key", "env:VAPI_API_KEY"), secrets),
        vapi_phone_number_id=resolve_env(vapi.get("phone_number_id", "env:VAPI_PHONE_NUMBER_ID"), secrets),
        vapi_assistant_id=resolve_env(vapi.get("assistant_id", "env:VAPI_ASSISTANT_ID"), secrets),
        assistant=dict(raw.get("assistant") or {}),
        products=products,
        script=dict(raw.get("script") or {}),
        promos=load_promos(products_path, products),
    )
