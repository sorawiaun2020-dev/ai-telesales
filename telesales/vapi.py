"""ตัวเรียก Vapi REST API (https://docs.vapi.ai) — ใช้ urllib ไม่ต้องติดตั้ง library เพิ่ม."""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

BASE_URL = "https://api.vapi.ai"


class VapiError(Exception):
    pass


class VapiClient:
    def __init__(self, api_key: str, base_url: str = BASE_URL, timeout: float = 30):
        if not api_key:
            raise VapiError("ยังไม่ได้ตั้ง VAPI_API_KEY (ดู README ขั้นตอนที่ 1)")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _request(self, method: str, path: str, body: dict | None = None) -> dict[str, Any]:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            self.base_url + path,
            data=data,
            method=method,
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")[:500]
            raise VapiError(f"{method} {path} -> HTTP {e.code}: {detail}") from e
        except urllib.error.URLError as e:
            raise VapiError(f"{method} {path} เชื่อมต่อไม่ได้: {e.reason}") from e
        return json.loads(raw) if raw else {}

    def create_assistant(self, payload: dict) -> dict:
        return self._request("POST", "/assistant", payload)

    def update_assistant(self, assistant_id: str, payload: dict) -> dict:
        return self._request("PATCH", f"/assistant/{assistant_id}", payload)

    def create_call(self, *, assistant_id: str, phone_number_id: str, number: str,
                    name: str = "", variables: dict[str, str] | None = None) -> dict:
        body: dict[str, Any] = {
            "assistantId": assistant_id,
            "phoneNumberId": phone_number_id,
            "customer": {"number": number, **({"name": name} if name else {})},
        }
        if variables:
            body["assistantOverrides"] = {"variableValues": variables}
        return self._request("POST", "/call", body)

    def get_call(self, call_id: str) -> dict:
        return self._request("GET", f"/call/{call_id}")
