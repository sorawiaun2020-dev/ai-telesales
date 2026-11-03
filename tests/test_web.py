"""ทดสอบหน้า Dashboard ผ่าน HTTP จริง — รันด้วย: python -m unittest discover -s tests"""
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from telesales import web  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


class TestDashboard(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        for f in ("config.json", "products.json"):
            shutil.copy(os.path.join(ROOT, f), self.tmp)
        self._env = {k: os.environ.pop(k, None) for k in
                     ("CONFIG_PATH", "PRODUCTS_PATH", "SECRETS_PATH", "DB_PATH", "VAPI_API_KEY")}
        self.start()

    def start(self, password=""):
        self.app = web.App(self.tmp, password)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), web.make_handler(self.app))
        self.base = f"http://127.0.0.1:{self.httpd.server_port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        web.log.removeHandler(self.app.logs)
        shutil.rmtree(self.tmp)
        for k, v in self._env.items():
            if v is not None:
                os.environ[k] = v

    def req(self, path, body=None, raw=None, headers=None):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        r = urllib.request.Request(self.base + path, data=data, method="POST" if data is not None else "GET",
                                   headers=headers or {})
        try:
            with urllib.request.urlopen(r) as resp:
                ctype = resp.headers.get("Content-Type", "")
                payload = resp.read()
                return resp.status, (json.loads(payload) if "json" in ctype else payload.decode("utf-8-sig"))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_page_and_state(self):
        code, html = self.req("/")
        self.assertEqual(code, 200)
        self.assertIn("AI Telesales", html)
        code, st = self.req("/api/state")
        self.assertEqual(code, 200)
        self.assertEqual(st["secrets"], {"VAPI_API_KEY": False, "VAPI_PHONE_NUMBER_ID": False, "VAPI_ASSISTANT_ID": False})
        self.assertEqual(len(st["products"]), 3)

    def test_save_settings_and_secrets(self):
        code, _ = self.req("/api/settings", {
            "shop": {"name": "สวนผักป้าแดง", "payment_methods": ["โอน"], "hacker": "x"},
            "calling": {"days": [0, 1, "2"], "start": "08:30", "end": "18:00", "max_calls_per_run": "25"},
            "secrets": {"VAPI_API_KEY": "sk-123", "VAPI_PHONE_NUMBER_ID": ""},
        })
        self.assertEqual(code, 200)
        cfg = _load(os.path.join(self.tmp, "config.json"))
        self.assertEqual(cfg["shop"]["name"], "สวนผักป้าแดง")
        self.assertNotIn("hacker", cfg["shop"])
        self.assertEqual(cfg["calling"]["max_calls_per_run"], 25)
        self.assertEqual(cfg["calling"]["days"], [0, 1, 2])
        secrets = _load(os.path.join(self.tmp, "secrets.json"))
        self.assertEqual(secrets, {"VAPI_API_KEY": "sk-123"})
        _, st = self.req("/api/state")
        self.assertTrue(st["secrets"]["VAPI_API_KEY"])
        self.assertNotIn("sk-123", json.dumps(st))  # ไม่ส่ง key กลับไปที่หน้าเว็บ

    def test_settings_validation(self):
        code, r = self.req("/api/settings", {"shop": {"name": ""}})
        self.assertEqual(code, 400)
        code, r = self.req("/api/settings", {"calling": {"start": "19:00", "end": "09:00"}})
        self.assertEqual(code, 400)
        self.assertIn("เวลา", r["error"])

    def test_products(self):
        code, r = self.req("/api/products", {"products": [
            {"sku": "TRAY", "name": "ถาดเพาะกล้า", "price": 25, "unit": "ถาด", "min_qty": 1, "details": "", "featured": True}]})
        self.assertEqual((code, r["count"]), (200, 1))
        _, p = self.req("/api/prompt")
        self.assertIn("[TRAY] ถาดเพาะกล้า", p["system_prompt"])
        self.assertNotIn("BAG-12", p["system_prompt"])
        code, r = self.req("/api/products", {"products": [{"sku": "A", "name": "x", "price": 1},
                                                           {"sku": "a", "name": "y", "price": 2}]})
        self.assertEqual(code, 400)
        self.assertIn("ซ้ำ", r["error"])

    def test_import_thai_excel_csv_and_dnc(self):
        csv_text = "ชื่อ,เบอร์โทร,สินค้าที่เคยซื้อ\r\nสมชาย,812345678,ถุงปลูก\r\nเสีย,12\r\n"
        code, r = self.req("/api/import", raw=csv_text.encode("cp874"))
        self.assertEqual(code, 200)
        self.assertEqual((r["added"], r["invalid"]), (1, 1))
        _, c = self.req("/api/customers?q=0812345678")
        self.assertEqual(c["rows"][0]["name"], "สมชาย")
        self.assertEqual(self.req("/api/dnc", {"phone": "081-234-5678"})[0], 200)
        _, c = self.req("/api/customers?status=dnc")
        self.assertEqual(c["total"], 1)
        code, csv_out = self.req("/export/orders.csv")
        self.assertEqual(code, 200)
        self.assertIn("ยอดรวม", csv_out)

    def test_actions_need_vapi_key(self):
        code, r = self.req("/api/campaign/start", {})
        self.assertEqual(code, 400)
        code, r = self.req("/api/test-call", {"phone": "0812345678"})
        self.assertEqual(code, 400)

    def test_password(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        web.log.removeHandler(self.app.logs)
        self.start(password="pw")
        self.assertEqual(self.req("/api/state")[0], 401)
        import base64
        auth = {"Authorization": "Basic " + base64.b64encode(b"admin:pw").decode()}
        self.assertEqual(self.req("/api/state", headers=auth)[0], 200)


if __name__ == "__main__":
    unittest.main()
