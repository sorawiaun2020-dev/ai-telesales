"""ฐานข้อมูล SQLite เก็บรายชื่อลูกค้า สถานะการโทร และผลแต่ละสาย (รองรับหลายหมื่นรายชื่อ)."""
from __future__ import annotations

import csv
import io
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from . import xlsx
from .phone import normalize_th

# สถานะลูกค้า
NEW = "new"          # ยังไม่เคยโทร
CALLING = "calling"  # โทรออกไปแล้ว รอผลจาก Vapi
RETRY = "retry"      # ไม่รับสาย / ขอให้โทรกลับ -> โทรใหม่รอบหน้า
DONE = "done"        # จบแล้ว (สั่งซื้อ / ไม่สนใจ / โทรครบจำนวนครั้ง)
DNC = "dnc"          # ขอไม่ให้โทรมาอีก — จะไม่โทรหาอีกเลย

SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
    phone         TEXT PRIMARY KEY,
    name          TEXT NOT NULL DEFAULT '',
    last_purchase TEXT NOT NULL DEFAULT '',
    note          TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL DEFAULT 'new',
    attempts      INTEGER NOT NULL DEFAULT 0,
    last_call_at  TEXT,
    imported_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_customers_status ON customers(status);
CREATE TABLE IF NOT EXISTS calls (
    id            TEXT PRIMARY KEY,
    phone         TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'queued',
    ended_reason  TEXT,
    outcome       TEXT,
    summary       TEXT,
    data_json     TEXT,
    recording_url TEXT,
    transcript    TEXT,
    cost          REAL,
    is_test       INTEGER NOT NULL DEFAULT 0,  -- สายทดสอบ: ไม่นับในสถิติ/ไฟล์ออเดอร์ และไม่เปลี่ยนสถานะลูกค้า
    name          TEXT NOT NULL DEFAULT '',    -- ชื่อที่ให้ AI เรียก (ใช้กับสายทดสอบที่ไม่มีในรายชื่อลูกค้า)
    order_status  TEXT NOT NULL DEFAULT ''     -- ออเดอร์: '' = รอยืนยัน, confirmed, shipped, cancelled
);
CREATE INDEX IF NOT EXISTS idx_calls_phone ON calls(phone);
CREATE INDEX IF NOT EXISTS idx_calls_status ON calls(status);
"""

# ชื่อคอลัมน์ที่รองรับในไฟล์ CSV (ไทย/อังกฤษ)
_COLS = {
    "name": ("name", "ชื่อ", "ชื่อลูกค้า", "ชื่อ-นามสกุล"),
    "phone": ("phone", "tel", "mobile", "เบอร์", "เบอร์โทร", "เบอร์โทรศัพท์", "โทรศัพท์"),
    "last_purchase": ("last_purchase", "สินค้าที่เคยซื้อ", "เคยซื้อ", "สินค้า"),
    "note": ("note", "หมายเหตุ"),
}


def _now_iso(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(timespec="seconds")


@dataclass
class ImportResult:
    added: int = 0
    updated: int = 0
    invalid: int = 0
    invalid_rows: list[str] | None = None


class DB:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """เพิ่มคอลัมน์ใหม่ให้ฐานข้อมูลที่สร้างจากโปรแกรมรุ่นก่อน (ข้อมูลเดิมไม่หาย)."""
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(calls)")}
        with self.conn:
            if "is_test" not in cols:
                self.conn.execute("ALTER TABLE calls ADD COLUMN is_test INTEGER NOT NULL DEFAULT 0")
            if "name" not in cols:
                self.conn.execute("ALTER TABLE calls ADD COLUMN name TEXT NOT NULL DEFAULT ''")
            if "order_status" not in cols:
                self.conn.execute("ALTER TABLE calls ADD COLUMN order_status TEXT NOT NULL DEFAULT ''")

    def close(self) -> None:
        self.conn.close()

    # ---------- นำเข้ารายชื่อ ----------
    def import_csv(self, path: str, now: datetime | None = None) -> ImportResult:
        with open(path, "rb") as f:
            return self.import_file_bytes(f.read(), now)

    def import_file_bytes(self, raw: bytes, now: datetime | None = None) -> ImportResult:
        """รับได้ทั้งไฟล์ Excel (.xlsx) และ CSV."""
        if xlsx.is_xlsx(raw):
            return self.import_rows(xlsx.read_rows(raw), now)
        return self.import_csv_bytes(raw, now)

    def import_csv_bytes(self, raw: bytes, now: datetime | None = None) -> ImportResult:
        # Excel บน Windows ไทย "Save as CSV" แบบธรรมดาจะได้ไฟล์ TIS-620 (cp874) ไม่ใช่ UTF-8
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("cp874", errors="replace")
        return self.import_rows(csv.DictReader(io.StringIO(text, newline="")), now)

    def import_rows(self, rows: Iterable[dict[str, Any]], now: datetime | None = None) -> ImportResult:
        res = ImportResult(invalid_rows=[])
        ts = _now_iso(now)
        with self.conn:
            for line_no, row in enumerate(rows, start=2):
                rec = _map_row(row)
                phone = normalize_th(rec["phone"])
                if not phone:
                    res.invalid += 1
                    res.invalid_rows.append(f"แถว {line_no}: เบอร์ไม่ถูกต้อง {rec['phone']!r}")
                    continue
                cur = self.conn.execute(
                    "UPDATE customers SET name = ?, last_purchase = ?, note = ? WHERE phone = ?",
                    (rec["name"], rec["last_purchase"], rec["note"], phone),
                )
                if cur.rowcount:
                    res.updated += 1  # อัปเดตข้อมูลแต่ไม่รีเซ็ตสถานะ (กันโทรซ้ำ / โทรหาคนที่ขอไม่ให้โทร)
                    continue
                self.conn.execute(
                    "INSERT INTO customers (phone, name, last_purchase, note, imported_at) VALUES (?, ?, ?, ?, ?)",
                    (phone, rec["name"], rec["last_purchase"], rec["note"], ts),
                )
                res.added += 1
        return res

    # ---------- เลือกคนที่จะโทร ----------
    def due_customers(self, limit: int, max_attempts: int, retry_after_hours: float,
                      now: datetime | None = None) -> list[sqlite3.Row]:
        cutoff = _now_iso((now or datetime.now(timezone.utc)) - timedelta(hours=retry_after_hours))
        return self.conn.execute(
            """SELECT * FROM customers
               WHERE status IN (?, ?) AND attempts < ?
                 AND (last_call_at IS NULL OR last_call_at <= ?)
               ORDER BY attempts ASC, (status = ?) DESC, imported_at ASC, phone ASC
               LIMIT ?""",
            (NEW, RETRY, max_attempts, cutoff, NEW, limit),
        ).fetchall()

    def customer(self, phone: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM customers WHERE phone = ?", (phone,)).fetchone()

    # ---------- บันทึกการโทร ----------
    def record_call_started(self, call_id: str, phone: str, now: datetime | None = None, *,
                            test: bool = False, name: str = "") -> None:
        ts = _now_iso(now)
        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO calls (id, phone, created_at, is_test, name) VALUES (?, ?, ?, ?, ?)",
                (call_id, phone, ts, int(test), name),
            )
            if test:
                return
            self.conn.execute(
                "UPDATE customers SET status = ?, attempts = attempts + 1, last_call_at = ? WHERE phone = ?",
                (CALLING, ts, phone),
            )

    def update_call_status(self, call_id: str, status: str) -> None:
        """สถานะระหว่างสาย (queued / ringing / in-progress) — ใช้แสดง "กำลังคุย" แบบสด."""
        with self.conn:
            self.conn.execute("UPDATE calls SET status = ? WHERE id = ? AND status != 'ended'", (status, call_id))

    def live_calls(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT calls.*, COALESCE(NULLIF(calls.name, ''), customers.name) AS customer_name FROM calls
               LEFT JOIN customers ON customers.phone = calls.phone
               WHERE calls.status != 'ended' ORDER BY calls.created_at DESC LIMIT 50"""
        ).fetchall()

    def set_order_status(self, call_id: str, status: str) -> bool:
        with self.conn:
            return self.conn.execute(
                "UPDATE calls SET order_status = ? WHERE id = ? AND outcome = 'ordered'", (status, call_id)
            ).rowcount > 0

    def report_calls(self) -> list[sqlite3.Row]:
        """ทุกสายจริง (ไม่รวมสายทดสอบ) สำหรับคำนวณสถิติและกราฟ."""
        return self.conn.execute(
            "SELECT id, created_at, outcome, data_json, cost, order_status FROM calls WHERE is_test = 0"
        ).fetchall()

    def pending_calls(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM calls WHERE status != 'ended' ORDER BY created_at").fetchall()

    def record_call_result(self, call_id: str, *, status: str, ended_reason: str | None,
                           outcome: str | None, summary: str | None, data: dict | None,
                           recording_url: str | None, transcript: str | None, cost: float | None,
                           customer_status: str | None) -> None:
        with self.conn:
            self.conn.execute(
                """UPDATE calls SET status = ?, ended_reason = ?, outcome = ?, summary = ?, data_json = ?,
                       recording_url = ?, transcript = ?, cost = ? WHERE id = ?""",
                (status, ended_reason, outcome, summary,
                 json.dumps(data, ensure_ascii=False) if data else None,
                 recording_url, transcript, cost, call_id),
            )
            if customer_status:
                row = self.conn.execute("SELECT phone, is_test FROM calls WHERE id = ?", (call_id,)).fetchone()
                if row and not row["is_test"]:
                    # DNC ทับทุกอย่าง ไม่ถูกเปลี่ยนกลับ
                    self.conn.execute(
                        "UPDATE customers SET status = ? WHERE phone = ? AND status != ?",
                        (customer_status, row["phone"], DNC),
                    )

    def add_customer(self, name: str, phone: str, last_purchase: str = "", note: str = "",
                     now: datetime | None = None) -> ImportResult:
        """เพิ่มลูกค้าทีละคน (จากหน้าโปรแกรม)."""
        return self.import_rows([{"name": name, "phone": phone, "last_purchase": last_purchase, "note": note}], now)

    def set_status(self, phone: str, status: str) -> bool:
        with self.conn:
            return self.conn.execute(
                "UPDATE customers SET status = ? WHERE phone = ?", (status, phone)
            ).rowcount > 0

    def list_customers(self, q: str = "", status: str = "", limit: int = 50,
                       offset: int = 0, outcome: str = "") -> tuple[list[sqlite3.Row], int]:
        where, args = [], []
        if outcome:  # กรองตามผลของสายล่าสุด
            where.append("""phone IN (SELECT k.phone FROM calls k WHERE k.is_test = 0 AND k.outcome = ?
                              AND k.created_at = (SELECT MAX(created_at) FROM calls
                                                  WHERE phone = k.phone AND is_test = 0))""")
            args.append(outcome)
        if q:
            where.append("(name LIKE ? OR phone LIKE ?)")
            digits = "".join(ch for ch in q if ch.isdigit()).lstrip("0")
            args += [f"%{q}%", f"%{digits or q}%"]
        if status:
            where.append("status = ?")
            args.append(status)
        cond = ("WHERE " + " AND ".join(where)) if where else ""
        total = self.conn.execute(f"SELECT COUNT(*) FROM customers {cond}", args).fetchone()[0]
        # แนบผลของสายล่าสุด (ไม่รวมสายทดสอบ) ให้แต่ละคน — ใช้แสดงผล/ยอดขายในตารางลูกค้า
        rows = self.conn.execute(
            f"""SELECT c.*, k.id AS call_id, k.outcome AS last_outcome, k.summary AS last_summary,
                       k.data_json AS last_data, k.status AS last_call_status, k.order_status AS order_status,
                       k.recording_url AS recording_url
                FROM (SELECT * FROM customers {cond}
                      ORDER BY COALESCE(last_call_at, '') DESC, imported_at DESC, phone LIMIT ? OFFSET ?) c
                LEFT JOIN calls k ON k.id = (SELECT id FROM calls WHERE phone = c.phone AND is_test = 0
                                             ORDER BY created_at DESC LIMIT 1)
                ORDER BY COALESCE(c.last_call_at, '') DESC, c.imported_at DESC, c.phone""",
            args + [limit, offset],
        ).fetchall()
        return rows, total

    def all_customers_with_last_call(self) -> list[sqlite3.Row]:
        rows, _ = self.list_customers(limit=-1)
        return rows

    def list_calls(self, outcome: str = "", limit: int = 50, offset: int = 0) -> tuple[list[sqlite3.Row], int]:
        cond, args = ("WHERE calls.outcome = ?", [outcome]) if outcome else ("", [])
        total = self.conn.execute(f"SELECT COUNT(*) FROM calls {cond}", args).fetchone()[0]
        rows = self.conn.execute(
            f"""SELECT calls.*, COALESCE(NULLIF(calls.name, ''), customers.name) AS customer_name FROM calls
                LEFT JOIN customers ON customers.phone = calls.phone {cond}
                ORDER BY calls.created_at DESC LIMIT ? OFFSET ?""",
            args + [limit, offset],
        ).fetchall()
        return rows, total

    # ---------- รายงาน ----------
    def status_counts(self) -> dict[str, int]:
        rows = self.conn.execute("SELECT status, COUNT(*) AS n FROM customers GROUP BY status").fetchall()
        return {r["status"]: r["n"] for r in rows}

    def outcome_counts(self) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT COALESCE(outcome, 'รอผล') AS o, COUNT(*) AS n FROM calls WHERE is_test = 0 GROUP BY o"
        ).fetchall()
        return {r["o"]: r["n"] for r in rows}

    def total_cost(self) -> float:
        return float(self.conn.execute("SELECT COALESCE(SUM(cost), 0) FROM calls").fetchone()[0])

    def calls_with_outcome(self, outcome: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT calls.*, customers.name AS customer_name FROM calls
               LEFT JOIN customers ON customers.phone = calls.phone
               WHERE calls.outcome = ? AND calls.is_test = 0 ORDER BY calls.created_at""",
            (outcome,),
        ).fetchall()


def _map_row(row: dict[str, Any]) -> dict[str, str]:
    norm = {str(k or "").strip().lower(): ("" if v is None else str(v).strip()) for k, v in row.items()}
    out = {}
    for field, aliases in _COLS.items():
        out[field] = next((norm[a.lower()] for a in aliases if norm.get(a.lower())), "")
    return out
