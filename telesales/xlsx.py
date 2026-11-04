"""อ่าน/เขียนไฟล์ Excel (.xlsx) แบบง่าย ๆ ด้วย Python มาตรฐาน (ไฟล์ .xlsx คือ zip ของ XML).

รองรับเฉพาะสิ่งที่ใช้กับรายชื่อลูกค้า: ชีตแรก, ข้อความ, ตัวเลข
"""
from __future__ import annotations

import io
import re
import zipfile
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
       "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}
_REL_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"


class XlsxError(Exception):
    pass


def is_xlsx(raw: bytes) -> bool:
    return raw[:2] == b"PK"


def _col_index(ref: str) -> int:
    """'A1' -> 0, 'C7' -> 2, 'AA3' -> 26."""
    letters = re.match(r"[A-Z]+", ref or "")
    n = 0
    for ch in (letters.group(0) if letters else "A"):
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def _number_text(v: str) -> str:
    # Excel เก็บเบอร์ที่พิมพ์เป็นตัวเลขเป็น "812345678" หรือ "812345678.0" / "8.12345678E8"
    try:
        f = float(v)
    except ValueError:
        return v
    return str(int(f)) if f.is_integer() else v


def read_rows(raw: bytes) -> list[dict[str, str]]:
    """คืนแถวของชีตแรกเป็น dict โดยใช้แถวแรกเป็นหัวคอลัมน์."""
    try:
        z = zipfile.ZipFile(io.BytesIO(raw))
        shared: list[str] = []
        if "xl/sharedStrings.xml" in z.namelist():
            for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall("m:si", _NS):
                shared.append("".join(t.text or "" for t in si.iter(f"{{{_NS['m']}}}t")))
        sheet_path = _first_sheet_path(z)
        root = ET.fromstring(z.read(sheet_path))
    except (zipfile.BadZipFile, KeyError, ET.ParseError) as e:
        raise XlsxError("เปิดไฟล์ Excel ไม่ได้ — ลองบันทึกเป็น .xlsx ใหม่อีกครั้ง") from e

    table: list[list[str]] = []
    for row in root.iter(f"{{{_NS['m']}}}row"):
        cells: dict[int, str] = {}
        for c in row.findall("m:c", _NS):
            t = c.get("t")
            if t == "inlineStr":
                val = "".join(x.text or "" for x in c.iter(f"{{{_NS['m']}}}t"))
            else:
                v = c.find("m:v", _NS)
                val = v.text if v is not None and v.text is not None else ""
                if t == "s" and val:
                    val = shared[int(val)]
                elif t not in ("str", "s", "b", "e"):
                    val = _number_text(val)
            cells[_col_index(c.get("r", ""))] = val.strip()
        if cells:
            table.append([cells.get(i, "") for i in range(max(cells) + 1)])
    if not table:
        return []
    header = table[0]
    return [{header[i]: (r[i] if i < len(r) else "") for i in range(len(header)) if header[i]}
            for r in table[1:] if any(x for x in r)]


def _first_sheet_path(z: zipfile.ZipFile) -> str:
    wb = ET.fromstring(z.read("xl/workbook.xml"))
    sheet = wb.find("m:sheets/m:sheet", _NS)
    rid = sheet.get(f"{{{_NS['r']}}}id") if sheet is not None else None
    if rid and "xl/_rels/workbook.xml.rels" in z.namelist():
        for rel in ET.fromstring(z.read("xl/_rels/workbook.xml.rels")).iter(f"{_REL_NS}Relationship"):
            if rel.get("Id") == rid:
                target = rel.get("Target", "").lstrip("/")
                return target if target.startswith("xl/") else "xl/" + target
    return "xl/worksheets/sheet1.xml"


def write(rows: list[list[str]], widths: list[int] | None = None, sheet_name: str = "ลูกค้า") -> bytes:
    """สร้างไฟล์ .xlsx จากตาราง (ทุกช่องเก็บเป็นข้อความ เพื่อให้เบอร์ 0 นำหน้าไม่หาย)."""
    def col(i: int) -> str:
        s = ""
        i += 1
        while i:
            i, r = divmod(i - 1, 26)
            s = chr(65 + r) + s
        return s

    sheet_rows = []
    for ri, row in enumerate(rows, start=1):
        style = ' s="1"' if ri == 1 else ' s="2"'
        cells = "".join(
            f'<c r="{col(ci)}{ri}" t="inlineStr"{style}><is><t xml:space="preserve">{escape(str(v))}</t></is></c>'
            for ci, v in enumerate(row))
        sheet_rows.append(f'<row r="{ri}">{cells}</row>')
    cols = "".join(f'<col min="{i + 1}" max="{i + 1}" width="{w}" customWidth="1"/>'
                   for i, w in enumerate(widths or []))
    files = {
        "[Content_Types].xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
            '</Types>'),
        "_rels/.rels": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            '</Relationships>'),
        "xl/workbook.xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<workbook xmlns="{_NS["m"]}" xmlns:r="{_NS["r"]}">'
            f'<sheets><sheet name="{escape(sheet_name)}" sheetId="1" r:id="rId1"/></sheets></workbook>'),
        "xl/_rels/workbook.xml.rels": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
            '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
            '</Relationships>'),
        "xl/styles.xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<styleSheet xmlns="{_NS["m"]}">'
            '<fonts count="2"><font><sz val="12"/><name val="Tahoma"/><family val="2"/></font>'
            '<font><b/><sz val="12"/><name val="Tahoma"/><family val="2"/></font></fonts>'
            '<fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill>'
            '<fill><patternFill patternType="solid"><fgColor rgb="FFE5F2E9"/><bgColor indexed="64"/></patternFill></fill></fills>'
            '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
            '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
            '<cellXfs count="3"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
            '<xf numFmtId="49" fontId="1" fillId="2" borderId="0" xfId="0" applyNumberFormat="1" applyFont="1" applyFill="1"/>'
            '<xf numFmtId="49" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/></cellXfs>'
            '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
            '</styleSheet>'),
        "xl/worksheets/sheet1.xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<worksheet xmlns="{_NS["m"]}">'
            '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>'
            + (f"<cols>{cols}</cols>" if cols else "")
            + f'<sheetData>{"".join(sheet_rows)}</sheetData></worksheet>'),
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, content in files.items():
            z.writestr(name, content)
    return buf.getvalue()


def customer_template() -> bytes:
    return write([
        ["ชื่อ", "เบอร์โทร", "สินค้าที่เคยซื้อ", "หมายเหตุ"],
        ["สมชาย ใจดี", "081-234-5678", "ถุงปลูกผัก 12 นิ้ว 50 ใบ", "ปลูกผักขายที่ตลาด"],
        ["มาลี สวนสวย", "089-876-5432", "ดินผสม 5 กระสอบ", ""],
    ], widths=[24, 18, 32, 32])
