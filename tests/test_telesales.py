"""ทดสอบตรรกะหลัก — รันด้วย: python -m unittest discover -s tests"""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from telesales import db as dbm  # noqa: E402
from telesales.campaign import classify, place_calls, sync_results  # noqa: E402
from telesales.config import load_config  # noqa: E402
from telesales.export import export_orders, price_order, report_text  # noqa: E402
from telesales.phone import normalize_th  # noqa: E402
from telesales.prompt import build_assistant_payload, build_system_prompt  # noqa: E402
from telesales.vapi import VapiError  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# วันอังคาร 11:00 เวลาไทย
OPEN = datetime(2026, 10, 27, 4, 0, tzinfo=timezone.utc)


def _cfg():
    return load_config(os.path.join(ROOT, "config.json"), os.path.join(ROOT, "products.json"))


class FakeVapi:
    def __init__(self, fail_on=None):
        self.calls = {}
        self.fail_on = fail_on or set()
        self.n = 0

    def create_call(self, *, assistant_id, phone_number_id, number, name="", variables=None):
        if number in self.fail_on:
            raise VapiError("HTTP 400: bad number")
        self.n += 1
        cid = f"call-{self.n}"
        self.calls[cid] = {"id": cid, "status": "queued", "number": number, "variables": variables}
        return {"id": cid}

    def get_call(self, call_id):
        return self.calls[call_id]

    def end(self, number, **fields):
        for c in self.calls.values():
            if c["number"] == number and c["status"] != "ended":
                c.update(status="ended", **fields)


class TestPhone(unittest.TestCase):
    def test_formats(self):
        for raw in ("081-234-5678", "0812345678", "66812345678", "+66 81 234 5678", "812345678", "812345678.0"):
            self.assertEqual(normalize_th(raw), "+66812345678", raw)
        self.assertEqual(normalize_th("02-123-4567"), "+6621234567")

    def test_invalid(self):
        for raw in ("", None, "12345", "0112345678", "081234567890"):
            self.assertIsNone(normalize_th(raw), raw)


class TestConfigAndPrompt(unittest.TestCase):
    def test_open_hours(self):
        cal = _cfg().calling
        self.assertTrue(cal.is_open(OPEN))
        self.assertFalse(cal.is_open(OPEN.replace(hour=13)))           # 20:00 ไทย
        self.assertFalse(cal.is_open(OPEN + timedelta(days=5)))        # วันอาทิตย์

    def test_prompt_has_catalog_and_variables(self):
        cfg = _cfg()
        prompt = build_system_prompt(cfg)
        self.assertIn("[BAG-12]", prompt)
        self.assertIn("{{customer_name}}", prompt)
        self.assertIn("ห้ามแต่งราคา", prompt)
        payload = build_assistant_payload(cfg)
        self.assertIn("{{customer_name}}", payload["firstMessage"])
        self.assertEqual(payload["voice"]["voiceId"], "th-TH-PremwadeeNeural")
        self.assertNotIn("model", payload["voice"])
        cfg.assistant.update(voice_provider="11labs", voice_id="abc", voice_model="eleven_multilingual_v2",
                             voice_language="th")
        self.assertEqual(build_assistant_payload(cfg)["voice"], {
            "provider": "11labs", "voiceId": "abc", "model": "eleven_multilingual_v2", "language": "th"})
        json.dumps(payload)  # ต้องแปลงเป็น JSON ได้
        plan = payload["startSpeakingPlan"]
        self.assertEqual(plan["transcriptionEndpointingPlan"]["onNoPunctuationSeconds"], 0.6)  # ค่าเริ่มต้น = เร็ว
        cfg.assistant["response_speed"] = "patient"
        self.assertEqual(build_assistant_payload(cfg)["startSpeakingPlan"]["waitSeconds"], 0.6)


class TestCampaign(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = dbm.DB(os.path.join(self.tmp.name, "t.db"))
        self.cfg = _cfg()
        self.cfg.vapi_assistant_id = "asst"
        self.cfg.vapi_phone_number_id = "num"
        self.cfg.calling.seconds_between_calls = 0
        self.db.import_rows([
            {"ชื่อ": "สมชาย", "เบอร์โทร": "0811111111", "สินค้าที่เคยซื้อ": "ถุงปลูก"},
            {"ชื่อ": "มาลี", "เบอร์โทร": "0822222222"},
            {"ชื่อ": "เสีย", "เบอร์โทร": "123"},
        ], now=OPEN)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_import_dedupes_and_keeps_status(self):
        self.db.set_status("+66811111111", dbm.DNC)
        res = self.db.import_rows([{"name": "สมชาย ใหม่", "phone": "081-111-1111"}], now=OPEN)
        self.assertEqual((res.added, res.updated), (0, 1))
        self.assertEqual(self.db.customer("+66811111111")["status"], dbm.DNC)

    def test_closed_hours_skip(self):
        rep = place_calls(self.cfg, self.db, FakeVapi(), now=OPEN.replace(hour=14))
        self.assertTrue(rep.skipped_reason)
        self.assertEqual(rep.placed, [])

    def test_full_cycle(self):
        api = FakeVapi()
        rep = place_calls(self.cfg, self.db, api, now=OPEN)
        self.assertEqual(len(rep.placed), 2)
        self.assertEqual(api.calls["call-1"]["variables"]["customer_name"], "สมชาย")
        # ระหว่างรอผล จะไม่ถูกโทรซ้ำ
        self.assertEqual(place_calls(self.cfg, self.db, api, now=OPEN).placed, [])

        api.end("+66811111111", endedReason="assistant-ended-call", analysis={
            "summary": "สั่งถุง", "structuredData": {
                "outcome": "ordered", "items": [{"sku": "BAG-PACK-50", "qty": 2}], "address": "1 ม.2 ต.ก อ.ข จ.ค 10000"}})
        api.end("+66822222222", endedReason="customer-did-not-answer")
        counts = sync_results(self.db, api, self.cfg.calling.max_attempts)
        self.assertEqual(counts, {"ordered": 1, "no_answer": 1})
        self.assertEqual(self.db.customer("+66811111111")["status"], dbm.DONE)
        self.assertEqual(self.db.customer("+66822222222")["status"], dbm.RETRY)

        # โทรซ้ำได้หลัง retry_after_hours เท่านั้น
        self.assertEqual(place_calls(self.cfg, self.db, api, now=OPEN + timedelta(hours=1)).placed, [])
        later = OPEN + timedelta(days=1)
        self.assertEqual(place_calls(self.cfg, self.db, api, now=later).placed, ["+66822222222"])

        out = os.path.join(self.tmp.name, "orders.csv")
        self.assertEqual(export_orders(self.cfg, self.db, out), 1)
        with open(out, encoding="utf-8-sig") as f:
            self.assertIn("1300", f.read())
        self.assertIn("สั่งซื้อ: 1", report_text(self.db))

    def test_do_not_call_and_max_attempts(self):
        api = FakeVapi()
        self.cfg.calling.max_attempts = 1
        place_calls(self.cfg, self.db, api, now=OPEN)
        api.end("+66811111111", analysis={"structuredData": {"outcome": "do_not_call"}})
        api.end("+66822222222", endedReason="customer-busy")
        sync_results(self.db, api, self.cfg.calling.max_attempts)
        self.assertEqual(self.db.customer("+66811111111")["status"], dbm.DNC)
        self.assertEqual(self.db.customer("+66822222222")["status"], dbm.DONE)
        self.assertEqual(place_calls(self.cfg, self.db, api, now=OPEN + timedelta(days=3)).placed, [])

    def test_call_error_does_not_count_attempt(self):
        api = FakeVapi(fail_on={"+66811111111"})
        rep = place_calls(self.cfg, self.db, api, now=OPEN)
        self.assertEqual(len(rep.errors), 1)
        self.assertEqual(self.db.customer("+66811111111")["attempts"], 0)


class TestXlsx(unittest.TestCase):
    def test_roundtrip_and_migration(self):
        from telesales import xlsx
        rows = xlsx.read_rows(xlsx.customer_template())
        self.assertEqual(rows[0]["ชื่อ"], "สมชาย ใจดี")
        self.assertEqual(rows[0]["เบอร์โทร"], "081-234-5678")
        # ฐานข้อมูลจากโปรแกรมรุ่นก่อน (ไม่มีคอลัมน์ is_test) ต้องเปิดได้และข้อมูลไม่หาย
        import sqlite3
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "old.db")
            con = sqlite3.connect(path)
            con.executescript("CREATE TABLE calls (id TEXT PRIMARY KEY, phone TEXT NOT NULL, created_at TEXT NOT NULL,"
                              " status TEXT NOT NULL DEFAULT 'queued', ended_reason TEXT, outcome TEXT, summary TEXT,"
                              " data_json TEXT, recording_url TEXT, transcript TEXT, cost REAL);"
                              "INSERT INTO calls (id, phone, created_at, outcome) VALUES ('c', '+66811111111', 'x', 'ordered');")
            con.commit()
            con.close()
            db = dbm.DB(path)
            self.assertEqual(db.outcome_counts(), {"ordered": 1})
            db.close()


class TestVapiClient(unittest.TestCase):
    def test_sends_user_agent(self):
        # Cloudflare ของ Vapi ตอบ 403 (error 1010) ถ้าใช้ User-Agent ค่าเริ่มต้นของ urllib
        from unittest import mock
        from telesales import vapi

        seen = {}

        class Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b'{"id": "a1"}'

        def fake_urlopen(req, timeout):
            seen.update({k.lower(): v for k, v in req.header_items()})
            return Resp()

        with mock.patch.object(vapi.urllib.request, "urlopen", fake_urlopen):
            self.assertEqual(vapi.VapiClient("k").create_assistant({})["id"], "a1")
        self.assertNotIn("python-urllib", seen["user-agent"].lower())
        self.assertEqual(seen["authorization"], "Bearer k")


class TestClassifyAndPricing(unittest.TestCase):
    def test_classify(self):
        self.assertEqual(classify({"endedReason": "voicemail"})[0], "no_answer")
        self.assertEqual(classify({"analysis": {"structuredData": {"outcome": "no_conversation"}}})[0], "no_answer")
        self.assertEqual(classify({"endedReason": "customer-ended-call"})[0], "unknown")

    def test_price_uses_catalog(self):
        cfg = _cfg()
        lines, total, warnings = price_order(cfg, [{"sku": "bag-12", "qty": 5}, {"sku": "XXX", "qty": 1}])
        self.assertEqual(total, 75)
        self.assertEqual(len(lines), 1)
        self.assertEqual(len(warnings), 2)  # ต่ำกว่าขั้นต่ำ + ไม่รู้จัก sku


if __name__ == "__main__":
    unittest.main()
