#!/usr/bin/env python3
"""ตัวเปิดโปรแกรมแบบ Desktop (ใช้สร้างไฟล์ AI-Telesales.exe สำหรับ Windows).

ดับเบิลคลิกแล้วเปิดหน้าต่างโปรแกรมทันที ไม่ต้องลง Python หรือพิมพ์คำสั่ง
ข้อมูลทั้งหมด (ตั้งค่า สินค้า รายชื่อลูกค้า ผลการโทร ไฟล์ออเดอร์) เก็บที่ Documents\\AI-Telesales
ปิดหน้าต่าง = ปิดโปรแกรม (และหยุดโทรอัตโนมัติ)
"""
from __future__ import annotations

import logging
import os
import shutil
import sys
import threading
import webbrowser

APP_NAME = "AI Telesales"


def bundle_dir() -> str:
    """โฟลเดอร์ไฟล์ของโปรแกรม (ใน .exe คือโฟลเดอร์ชั่วคราวที่ PyInstaller แตกไฟล์ไว้)."""
    return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))


def data_dir() -> str:
    """โฟลเดอร์เก็บข้อมูลของผู้ใช้ — ต้องเขียนได้และไม่หายเมื่ออัปเดตโปรแกรม."""
    path = os.environ.get("TELESALES_HOME") or os.path.join(os.path.expanduser("~"), "Documents", "AI-Telesales")
    os.makedirs(path, exist_ok=True)
    # ครั้งแรก: คัดลอกตั้งค่าและสินค้าตัวอย่างมาให้แก้ (ครั้งถัดไปไม่ทับของเดิม)
    for name in ("config.json", "products.json", "customers.example.csv"):
        dst = os.path.join(path, name)
        if not os.path.exists(dst):
            shutil.copy(os.path.join(bundle_dir(), name), dst)
    return path


def setup_logging(home: str) -> None:
    # โปรแกรมแบบมีหน้าต่างไม่มี console จึงเขียน log ลงไฟล์แทน
    handler = logging.FileHandler(os.path.join(home, "program.log"), encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-7s  %(message)s", "%Y-%m-%d %H:%M:%S"))
    logger = logging.getLogger("telesales")
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    logger.propagate = False


def start_server(home: str):
    from telesales.web import create_server

    try:
        httpd, app = create_server(home, "127.0.0.1", 8765)
    except OSError:  # port ถูกใช้อยู่ (เช่นเปิดโปรแกรมซ้อน) -> ให้ระบบเลือก port ว่าง
        httpd, app = create_server(home, "127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, name="web", daemon=True).start()
    return httpd, app, f"http://127.0.0.1:{httpd.server_port}/"


def open_window(url: str, home: str) -> None:
    """เปิดหน้าต่างโปรแกรม (Edge WebView2 ที่มีในทุก Windows 10/11) — ปิดหน้าต่างแล้วฟังก์ชันนี้จะคืนค่า."""
    try:
        import webview

        webview.settings["ALLOW_DOWNLOADS"] = True
        webview.create_window(APP_NAME, url, width=1200, height=850, min_size=(380, 500))
        webview.start(private_mode=False, storage_path=os.path.join(home, ".webview"))
        return
    except Exception:  # noqa: BLE001 — ไม่มี WebView2 ฯลฯ -> ใช้เบราว์เซอร์ปกติแทน
        logging.getLogger("telesales").exception("เปิดหน้าต่างโปรแกรมไม่ได้ ใช้เบราว์เซอร์แทน")
    fallback_window(url, home)


def fallback_window(url: str, home: str) -> None:
    """เปิดในเบราว์เซอร์ + หน้าต่างเล็กไว้กดเปิดซ้ำ / ปิดโปรแกรม."""
    import tkinter as tk

    webbrowser.open(url)
    root = tk.Tk()
    root.title(APP_NAME)
    root.geometry("380x170")
    tk.Label(root, text="โปรแกรมกำลังทำงาน\nหน้าจอเปิดอยู่ในเบราว์เซอร์", font=("Leelawadee UI", 12)).pack(pady=12)
    tk.Button(root, text="เปิดหน้าจออีกครั้ง", command=lambda: webbrowser.open(url)).pack(fill="x", padx=40)
    tk.Button(root, text="เปิดโฟลเดอร์ข้อมูล", command=lambda: os.startfile(home)).pack(fill="x", padx=40, pady=4)
    tk.Button(root, text="ปิดโปรแกรม", command=root.destroy).pack(fill="x", padx=40)
    root.mainloop()


def selftest(url: str) -> int:
    """ใช้ตอนสร้าง .exe บน GitHub: เช็กว่าโปรแกรมเปิดได้และหน้าเว็บตอบจริง (ไม่เปิดหน้าต่าง)."""
    import json
    import urllib.request

    with urllib.request.urlopen(url, timeout=10) as r:
        assert b"AI Telesales" in r.read()
    with urllib.request.urlopen(url + "api/state", timeout=10) as r:
        assert json.load(r)["products"]
    import webview  # noqa: F401 — ต้องถูกรวมไว้ใน .exe
    return 0


def main() -> None:
    home = data_dir()
    setup_logging(home)
    httpd, app, url = start_server(home)
    if "--selftest" in sys.argv:
        try:
            code = selftest(url)
        except Exception:  # noqa: BLE001
            logging.getLogger("telesales").exception("selftest ไม่ผ่าน")
            code = 1
        httpd.shutdown()
        sys.exit(code)
    logging.getLogger("telesales").info("เปิดโปรแกรมแล้ว ข้อมูลอยู่ที่ %s", home)
    try:
        open_window(url, home)
    finally:
        app.campaign.stop()
        httpd.shutdown()


if __name__ == "__main__":
    main()
