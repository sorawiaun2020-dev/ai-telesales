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
        self.app.close()
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
                if "json" in ctype:
                    return resp.status, json.loads(payload)
                return resp.status, (payload.decode("utf-8-sig") if ctype.startswith("text/") else payload)
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_page_and_state(self):
        code, html = self.req("/")
        self.assertEqual(code, 200)
        self.assertIn("AI Telesales", html)
        code, st = self.req("/api/state")
        self.assertEqual(code, 200)
        self.assertEqual(st["secrets"], {"VAPI_API_KEY": False, "VAPI_PHONE_NUMBER_ID": False, "VAPI_ASSISTANT_ID": False})
        self.assertEqual(len(st["products"]), 4)

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
            {"sku": "TRAY", "name": "ถาดเพาะกล้า", "price": 25, "unit": "ถาด", "min_qty": 1, "details": "", "role": "main"}]})
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

    def test_excel_template_import_and_add_customer(self):
        from telesales import xlsx
        code, _ = self.req("/export/customers-template.xlsx")
        self.assertEqual(code, 200)
        # ไฟล์ที่ผู้ใช้กรอกใน Excel: เบอร์ถูกเก็บเป็นตัวเลข (0 หน้าหาย) ก็ต้องนำเข้าได้
        raw = xlsx.write([["ชื่อ", "เบอร์โทร", "สินค้าที่เคยซื้อ"], ["ป้าแดง", "0898765432", "ดิน"]])
        raw = raw.replace(b't="inlineStr" s="2"><is><t xml:space="preserve">0898765432</t></is>',
                          b'><v>898765432</v>')
        code, r = self.req("/api/import", raw=raw)
        self.assertEqual((code, r["added"]), (200, 1))
        code, r = self.req("/api/customers/add", {"name": "ลุงมี", "phone": "081 111 2222"})
        self.assertEqual((code, r["added"]), (200, 1))
        self.assertEqual(self.req("/api/customers/add", {"name": "x", "phone": "12"})[0], 400)
        _, c = self.req("/api/customers")
        self.assertEqual(sorted(x["name"] for x in c["rows"]), ["ป้าแดง", "ลุงมี"])
        self.assertEqual(self.req("/api/import", raw=b"PK\x03\x04 broken")[0], 400)

    def test_test_call_is_recorded_but_not_counted(self):
        from unittest import mock

        class FakeClient:
            calls = {}

            def __init__(self, key):
                pass

            def create_call(self, **kw):
                self.calls["t1"] = {"id": "t1", "status": "ended", "endedReason": "customer-ended-call",
                                    "analysis": {"summary": "ทดสอบสั่งซื้อ", "structuredData": {
                                        "outcome": "ordered", "items": [{"sku": "BAG-12", "qty": 10}]}}}
                return {"id": "t1"}

            def get_call(self, cid):
                return self.calls[cid]

        self.req("/api/settings", {"secrets": {"VAPI_API_KEY": "k", "VAPI_PHONE_NUMBER_ID": "p",
                                               "VAPI_ASSISTANT_ID": "a"}})
        with mock.patch.object(web, "VapiClient", FakeClient):
            code, r = self.req("/api/test-call", {"phone": "0812345678", "name": "สมชาย"})
            self.assertEqual((code, r["call_id"]), (200, "t1"))
            self.assertEqual(self.req("/api/sync", {})[1]["counts"], {"ordered": 1})
        _, calls = self.req("/api/calls")
        row = calls["rows"][0]
        self.assertEqual((row["is_test"], row["customer_name"], row["outcome"]), (1, "สมชาย", "ordered"))
        self.assertEqual(row["order"]["total"], 150)
        _, st = self.req("/api/state")
        self.assertEqual(st["stats"]["outcomes"], {})      # สายทดสอบไม่นับในสถิติ
        _, c = self.req("/api/customers")
        self.assertEqual(c["total"], 0)                    # และไม่ถูกเพิ่มเป็นลูกค้า
        code, csv_out = self.req("/export/orders.csv")
        self.assertNotIn("สมชาย", csv_out)                 # ไม่อยู่ในไฟล์ออเดอร์จริง

    def test_dashboard_orders_and_exports(self):
        from telesales import db as dbm, xlsx
        db = dbm.DB(os.path.join(self.tmp, "telesales.db"))
        db.import_rows([{"name": "สมชาย", "phone": "0811111111"}, {"name": "มาลี", "phone": "0822222222"},
                        {"name": "ลุงมี", "phone": "0833333333"}])
        db.record_call_started("a", "+66811111111")
        db.record_call_result("a", status="ended", ended_reason="x", outcome="ordered", summary="ซื้อ",
                              data={"items": [{"sku": "BAG-PACK-50", "qty": 2}, {"sku": "SOIL-25", "qty": 1}],
                                    "upsell_offered": True, "address": "บ้าน"},
                              recording_url=None, transcript=None, cost=0.2, customer_status=dbm.DONE)
        db.record_call_started("b", "+66822222222")
        db.record_call_result("b", status="ended", ended_reason="x", outcome="not_interested", summary="",
                              data={"backup_offered": True}, recording_url=None, transcript=None, cost=0.1,
                              customer_status=dbm.DONE)
        db.record_call_started("c", "+66833333333")
        db.update_call_status("c", "in-progress")
        db.close()

        _, d = self.req("/api/dashboard")
        k = d["kpi"]
        self.assertEqual((k["orders"], k["revenue"], k["talked"]), (1, 1389, 2))
        self.assertEqual((k["upsell_rate"], k["backup_rate"], k["close_rate"]), (1.0, 0.0, 0.5))
        self.assertEqual(d["series"][-1]["revenue"], 1389)
        self.assertEqual([p["sku"] for p in d["top_products"]], ["BAG-PACK-50", "SOIL-25"])
        self.assertEqual([(c["name"], c["status"]) for c in d["live"]], [("ลุงมี", "in-progress")])

        _, c = self.req("/api/customers?outcome=ordered")
        self.assertEqual(c["total"], 1)
        row = c["rows"][0]
        self.assertEqual((row["name"], row["last_outcome"], row["order"]["total"]), ("สมชาย", "ordered", 1389))

        self.assertEqual(self.req("/api/order-status", {"call_id": "a", "status": "shipped"})[0], 200)
        self.assertEqual(self.req("/api/order-status", {"call_id": "b", "status": "shipped"})[0], 400)
        self.assertEqual(self.req("/api/order-status", {"call_id": "a", "status": "lost"})[0], 400)
        self.req("/api/order-status", {"call_id": "a", "status": "cancelled"})
        self.assertEqual(self.req("/api/dashboard")[1]["kpi"]["revenue"], 0)  # ยกเลิกแล้วไม่นับยอด

        code, raw = self.req("/export/customers.xlsx")
        rows = xlsx.read_rows(raw)
        self.assertEqual(code, 200)
        self.assertEqual({r["ชื่อ"]: r["ผลการโทรล่าสุด"] for r in rows}["สมชาย"], "สั่งซื้อ")
        code, raw = self.req("/export/orders.xlsx")
        self.assertEqual(xlsx.read_rows(raw)[0]["สถานะออเดอร์"], "ยกเลิก")

    def test_product_roles(self):
        code, _ = self.req("/api/products", {"products": [
            {"sku": "A", "name": "หลัก", "price": 10, "role": "main"},
            {"sku": "B", "name": "เสริม", "price": 5, "role": "upsell"},
            {"sku": "C", "name": "สำรอง", "price": 3, "role": "backup"}]})
        self.assertEqual(code, 200)
        _, p = self.req("/api/prompt")
        self.assertIn("สินค้าอัปเซล", p["system_prompt"])
        self.assertIn("[C] สำรอง", p["system_prompt"].split("สินค้าสำรอง")[1])
        self.assertEqual(self.req("/api/products", {"products": [{"sku": "A", "name": "x", "price": 1, "role": "vip"}]})[0], 400)

    def test_existing_address_is_confirmed_not_asked(self):
        from unittest import mock
        from telesales import xlsx
        raw = xlsx.write([["ชื่อ", "เบอร์โทร", "ที่อยู่"], ["ป้าแดง", "0898765432", "9 ม.1 ต.ก อ.ข จ.ค 10000"]])
        self.assertEqual(self.req("/api/import", raw=raw)[1]["added"], 1)
        # นำเข้าไฟล์ที่ไม่มีคอลัมน์ที่อยู่ซ้ำ ต้องไม่ลบที่อยู่เดิม
        self.req("/api/import", raw=xlsx.write([["ชื่อ", "เบอร์โทร"], ["ป้าแดง", "0898765432"]]))
        _, c = self.req("/api/customers")
        self.assertEqual(c["rows"][0]["address"], "9 ม.1 ต.ก อ.ข จ.ค 10000")

        sent = {}

        class FakeClient:
            def __init__(self, key):
                pass

            def create_call(self, **kw):
                sent.update(kw["variables"])
                return {"id": "t1"}

            def get_call(self, cid):
                return {"id": cid, "status": "ended", "analysis": {"structuredData": {
                    "outcome": "ordered", "use_existing_address": True, "items": [{"sku": "SOIL-25", "qty": 1}]}}}

        self.req("/api/settings", {"secrets": {"VAPI_API_KEY": "k", "VAPI_PHONE_NUMBER_ID": "p",
                                               "VAPI_ASSISTANT_ID": "a"}})
        with mock.patch.object(web, "VapiClient", FakeClient):
            self.req("/api/test-call", {"phone": "0812345678", "name": "สมชาย", "address": "1 ถ.สุข จ.ลำปาง"})
            self.req("/api/sync", {})
        self.assertEqual(sent["address"], "1 ถ.สุข จ.ลำปาง")
        _, calls = self.req("/api/calls")
        dv = calls["rows"][0]["delivery"]
        self.assertEqual((dv["address"], dv["address_source"], dv["recipient"], dv["contact_phone"]),
                         ("1 ถ.สุข จ.ลำปาง", "ที่อยู่เดิม", "สมชาย", "0812345678"))
        _, p = self.req("/api/prompt")
        self.assertIn("{{address}}", p["system_prompt"])
        self.assertIn("ห้ามขอให้ลูกค้าบอกที่อยู่ใหม่", p["system_prompt"])

    def test_script_editor(self):
        _, sc = self.req("/api/script")
        self.assertTrue(sc["is_default"])
        self.assertIn("{รายการสินค้า}", sc["system_prompt"])
        body = "คุณคือ {ชื่อพนักงาน} จาก {ชื่อร้าน} ขายของให้ {ชื่อลูกค้า}\n{รายการสินค้า}\nที่อยู่ {ที่อยู่เดิม} {อะไรนะ}"
        code, r = self.req("/api/script/preview", {"first_message": "หวัดดีครับ {ชื่อลูกค้า}", "system_prompt": body})
        self.assertEqual(code, 200)
        self.assertEqual(r["first_message"], "หวัดดีครับ {{customer_name}}")
        self.assertIn("ขายของให้ {{customer_name}}", r["system_prompt"])
        self.assertIn("[BAG-12]", r["system_prompt"])                  # รายการสินค้าถูกแทนค่า
        self.assertIn("ห้ามขอเลขบัตรประชาชน", r["system_prompt"])       # กฎที่ระบบบังคับต่อท้ายเสมอ
        self.assertTrue(any("{อะไรนะ}" in w for w in r["warnings"]))
        _, sc = self.req("/api/script")
        self.assertTrue(sc["is_default"])                               # preview ไม่บันทึก
        code, r = self.req("/api/script", {"first_message": "หวัดดีครับ {ชื่อลูกค้า}", "system_prompt": "ขาย {ชื่อร้าน}"})
        self.assertEqual(code, 200)
        self.assertTrue(any("{รายการสินค้า}" in w for w in r["warnings"]))
        _, p = self.req("/api/prompt")
        self.assertTrue(p["system_prompt"].startswith("ขาย ร้านเกษตรของเรา"))
        self.assertEqual(self.req("/api/script", {"first_message": "", "system_prompt": "x"})[0], 400)
        self.req("/api/script", {"reset": True})
        _, sc = self.req("/api/script")
        self.assertTrue(sc["is_default"])

    def test_mix_and_match_promo(self):
        from telesales import db as dbm
        prods = [{"sku": s, "name": f"ถุงปลูกผักแสนดี {n}", "price": 200, "unit": "ชุด", "role": "main", "details": d}
                 for s, n, d in (("S", "5x11", "30 ใบ"), ("M", "8x15", "15 ใบ"), ("L", "10x20", "10 ใบ"))]
        prods.append({"sku": "SOIL", "name": "ดิน", "price": 89, "unit": "กระสอบ", "role": "upsell"})
        promo = {"name": "ถุงปลูกผักแสนดี", "skus": ["S", "M", "L"], "tiers": [{"qty": 1, "price": 200}, {"qty": 3, "price": 500}]}
        code, r = self.req("/api/products", {"products": prods, "promos": [promo]})
        self.assertEqual((code, r["promos"]), (200, 1))
        _, p = self.req("/api/prompt")
        self.assertIn("3 ชุด = 500 บาท", p["system_prompt"])
        self.assertIn("4 ชุด = 700 บาท", p["system_prompt"])
        self.assertIn("(คละกันได้)", p["system_prompt"])

        db = dbm.DB(os.path.join(self.tmp, "telesales.db"))
        db.import_rows([{"name": "ป้าแดง", "phone": "0811111111"}])
        for cid, items in (("a", [{"sku": "S", "qty": 1}, {"sku": "M", "qty": 1}, {"sku": "L", "qty": 1}]),
                           ("b", [{"sku": "S", "qty": 4}, {"sku": "SOIL", "qty": 1}])):
            db.record_call_started(cid, "+66811111111")
            db.record_call_result(cid, status="ended", ended_reason="x", outcome="ordered", summary="",
                                  data={"items": items}, recording_url=None, transcript=None, cost=0,
                                  customer_status=dbm.DONE)
        db.close()
        _, calls = self.req("/api/calls?outcome=ordered")
        totals = {c["id"]: c["order"]["total"] for c in calls["rows"]}
        self.assertEqual(totals, {"a": 500, "b": 789})      # คละ 3 ขนาด = 500, 4 ชุด = 700 + ดิน 89
        _, d = self.req("/api/dashboard")
        self.assertEqual(d["kpi"]["revenue"], 1289)
        self.assertEqual(round(sum(x["revenue"] for x in d["top_products"])), 1289)

        bad = dict(promo, name="โปร 2")
        self.assertEqual(self.req("/api/products", {"products": prods, "promos": [promo, bad]})[0], 400)

    def test_password(self):
        self.app.close()
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
