"""SwitchBot OpenAPI v1.1 クライアント（認証パッド／Keypad Touch のパスコード操作用）.

参考: https://github.com/OpenWonderLabs/SwitchBotAPI

重要な仕様（実装がこれに依存しています）
--------------------------------------------------
* createKey / deleteKey は「非同期」コマンドです。HTTP レスポンスは
  {"statusCode":100,"body":{"commandId":"CMD..."},"message":"success"} だけを返し、
  実際に成功したかどうかは分かりません（数十秒〜1分ほど遅れて確定します）。
* createKey のレスポンスにはパスコード ID が含まれません。deleteKey には ID が必須です。
  そのため本実装では GET /v1.1/devices が返す keyList（id / name / status / password）を
  ポーリングして、name をキーに ID を回収します。name はデバイス内で一意である必要があります。
* keyList の password は secret key を鍵とした AES-128-CBC で暗号化されています。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
import uuid
from typing import Any

import requests
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

HOST = "https://api.switch-bot.com"
VALID_KEY_TYPES = ("permanent", "timeLimit", "disposable", "urgent")


class SwitchBotError(RuntimeError):
    """API がエラーを返した、または通信に失敗した."""


class SwitchBotClient:
    def __init__(self, token: str, secret: str, timeout: float = 20.0):
        if not token or not secret:
            raise ValueError("token と secret を設定してください（.env を確認）")
        self.token = token.strip()
        self.secret = secret.strip()
        self.timeout = timeout

    # ------------------------------------------------------------------ 認証
    def _headers(self) -> dict[str, str]:
        t = str(int(time.time() * 1000))
        nonce = str(uuid.uuid4())
        payload = f"{self.token}{t}{nonce}".encode()
        sign = base64.b64encode(
            hmac.new(self.secret.encode(), msg=payload, digestmod=hashlib.sha256).digest()
        ).decode()
        return {
            "Authorization": self.token,
            "sign": sign,
            "t": t,
            "nonce": nonce,
            "Content-Type": "application/json; charset=utf8",
        }

    def _request(self, method: str, path: str, json_body: dict | None = None) -> dict[str, Any]:
        url = f"{HOST}{path}"
        try:
            res = requests.request(
                method, url, headers=self._headers(), json=json_body, timeout=self.timeout
            )
        except requests.RequestException as exc:
            raise SwitchBotError(f"通信エラー: {exc}") from exc

        if res.status_code == 401:
            raise SwitchBotError("401 Unauthorized: token / secret が違うか、1日1万回の上限を超えています")
        if res.status_code >= 400:
            raise SwitchBotError(f"HTTP {res.status_code}: {res.text[:200]}")

        try:
            data = res.json()
        except ValueError as exc:
            raise SwitchBotError(f"JSON ではない応答: {res.text[:200]}") from exc

        if data.get("statusCode") != 100:
            raise SwitchBotError(
                f"statusCode={data.get('statusCode')} message={data.get('message')}"
            )
        return data

    # ---------------------------------------------------------------- デバイス
    def get_devices(self) -> list[dict[str, Any]]:
        body = self._request("GET", "/v1.1/devices").get("body", {})
        return body.get("deviceList", []) or []

    def get_device(self, device_id: str) -> dict[str, Any]:
        target = device_id.strip().upper()
        for dev in self.get_devices():
            if str(dev.get("deviceId", "")).strip().upper() == target:
                return dev
        raise SwitchBotError(
            f"deviceId={device_id} が見つかりません。SwitchBot アプリでクラウドサービスを"
            "有効にしているか、Device ID を確認してください"
        )

    def get_key_list(self, device_id: str) -> list[dict[str, Any]]:
        """認証パッドに登録されているパスコード一覧（id / name / type / status など）."""
        dev = self.get_device(device_id)
        keys = dev.get("keyList") or []
        if isinstance(keys, dict):  # 念のため（仕様上は配列）
            keys = list(keys.values())
        return keys

    # ---------------------------------------------------------------- コマンド
    def _command(self, device_id: str, command: str, parameter: Any) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/v1.1/devices/{device_id}/commands",
            {"commandType": "command", "command": command, "parameter": parameter},
        )

    def create_key(
        self,
        device_id: str,
        name: str,
        password: str,
        key_type: str = "timeLimit",
        start_time: int | None = None,
        end_time: int | None = None,
    ) -> str:
        """パスコードを作成し commandId を返す（成否は後から keyList で確認する）."""
        if key_type not in VALID_KEY_TYPES:
            raise ValueError(f"type は {VALID_KEY_TYPES} のいずれか")
        if not password.isdigit() or not (6 <= len(password) <= 12):
            raise ValueError("パスコードは6〜12桁の数字です")
        param: dict[str, Any] = {"name": name, "type": key_type, "password": password}
        if key_type in ("timeLimit", "disposable"):
            if start_time is None or end_time is None:
                raise ValueError(f"{key_type} は startTime / endTime が必須です")
            param["startTime"] = int(start_time)  # 10桁（秒）
            param["endTime"] = int(end_time)
        res = self._command(device_id, "createKey", param)
        return (res.get("body") or {}).get("commandId", "")

    def delete_key(self, device_id: str, key_id: int) -> str:
        res = self._command(device_id, "deleteKey", {"id": int(key_id)})
        return (res.get("body") or {}).get("commandId", "")

    # ------------------------------------------------------------ 復号（表示用）
    def decrypt_password(self, password_b64: str, iv: str) -> str | None:
        """keyList の password を復号する。失敗したら None."""
        if not password_b64 or not iv:
            return None
        ciphertexts = [c for c in (_b64(password_b64), _hexb(password_b64)) if c]
        ivs = [v for v in (_b64(iv), _hexb(iv), iv.encode()) if v and len(v) == 16]
        keys = [k for k in (_hexb(self.secret), self.secret.encode()[:16]) if k and len(k) == 16]
        for key in keys:
            for iv_bytes in ivs:
                for ct in ciphertexts:
                    if len(ct) % 16:
                        continue
                    plain = _aes_cbc_decrypt(key, iv_bytes, ct)
                    if plain and plain.isdigit() and 6 <= len(plain) <= 12:
                        return plain
        return None


def _b64(s: str) -> bytes | None:
    try:
        return base64.b64decode(s, validate=True)
    except Exception:
        return None


def _hexb(s: str) -> bytes | None:
    try:
        return bytes.fromhex(s)
    except Exception:
        return None


def _aes_cbc_decrypt(key: bytes, iv: bytes, ct: bytes) -> str | None:
    try:
        dec = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
        raw = dec.update(ct) + dec.finalize()
        pad = raw[-1]
        if 1 <= pad <= 16 and raw[-pad:] == bytes([pad]) * pad:
            raw = raw[:-pad]
        return raw.decode("ascii", errors="ignore").strip()
    except Exception:
        return None
