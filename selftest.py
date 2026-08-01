"""オフライン自己テスト（SwitchBot には接続しません）.

    python selftest.py

偽のクラウドを相手に、発行 → ID 回収 → 期限到来 → 削除、までの流れを検証します。
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import store

store.DB_PATH = os.path.join(tempfile.mkdtemp(), "selftest.sqlite3")
store.init()

import scheduler_core  # noqa: E402
from scheduler_core import Engine  # noqa: E402

TZ = ZoneInfo("Asia/Tokyo")


class FakeCloud:
    """createKey / deleteKey が数十秒遅れて反映される SwitchBot クラウドの模擬."""

    def __init__(self):
        self.keys: list[dict] = []
        self.queue: list[tuple[str, dict]] = []
        self.next_id = 1
        self.calls: list[str] = []

    def create_key(self, device_id, name, password, key_type="timeLimit",
                   start_time=None, end_time=None):
        self.calls.append(f"create:{name}")
        self.queue.append(("create", {"name": name, "type": key_type,
                                      "password": password, "status": "normal"}))
        return "CMD-FAKE"

    def delete_key(self, device_id, key_id):
        self.calls.append(f"delete:{key_id}")
        self.queue.append(("delete", {"id": int(key_id)}))
        return "CMD-FAKE"

    def get_key_list(self, device_id):
        for kind, payload in self.queue:          # 反映（ポーリングのたびに確定させる）
            if kind == "create":
                payload["id"] = self.next_id
                self.next_id += 1
                self.keys.append(payload)
            else:
                self.keys = [k for k in self.keys if k["id"] != payload["id"]]
        self.queue.clear()
        return [dict(k) for k in self.keys]

    def decrypt_password(self, password, iv):
        return None


def run():
    cloud = FakeCloud()
    engine = Engine(cloud, "TESTDEVICE", TZ)

    base = datetime(2026, 8, 1, 8, 45, tzinfo=TZ)   # 土曜
    engine.now = lambda: base                        # type: ignore[method-assign]

    rule_id = store.insert_rule({
        "label": "朝稽古", "passcode": "246810", "key_type": "timeLimit",
        "repeat_type": "weekly", "weekdays": "5,6", "on_date": "",
        "start_time": "09:00", "end_time": "11:30", "lead_minutes": 10,
        "enabled": 1, "note": "",
    })
    rule = store.get_rule(rule_id)
    assert rule and rule["label"] == "朝稽古"

    # 開始10分前より前 → 何もしない
    engine.now = lambda: base - timedelta(minutes=10)   # 08:35
    engine.tick()
    assert not store.list_issuances(("creating", "active")), "早すぎる発行が起きた"

    # 08:50（開始10分前）→ createKey
    engine.now = lambda: base.replace(hour=8, minute=50)
    engine.tick()
    active = store.list_issuances(("creating", "active"))
    assert len(active) == 1, active
    assert any(c.startswith("create:") for c in cloud.calls)
    print("発行:", active[0]["key_name"], active[0]["state"])

    # 同じ時刻でもう一度 tick しても二重発行しない
    engine.tick()
    assert len(store.list_issuances(("creating", "active"))) == 1, "二重発行した"

    # keyList 同期で ID が付き active になる
    engine.sync_keys(force=True)
    row = store.list_issuances(("active",))[0]
    assert row["key_id"] == 1, row
    print("ID 回収:", row["key_id"], row["state"])

    # 期間中は削除されない
    engine.now = lambda: base.replace(hour=10, minute=0)
    engine.tick()
    assert store.list_issuances(("active",)), "期間中に消えた"

    # 終了時刻を過ぎたら deleteKey
    engine.now = lambda: base.replace(hour=11, minute=31)
    engine.keys_synced_at = 0
    engine.tick()
    assert f"delete:1" in cloud.calls, cloud.calls
    engine.keys_synced_at = 0
    engine.sync_keys(force=True)
    row = store.list_issuances()[0]
    assert row["state"] == "deleted", row
    print("削除:", row["key_name"], row["state"])

    # 日曜（曜日一致）は発行、月曜は発行しない
    engine.now = lambda: datetime(2026, 8, 2, 8, 55, tzinfo=TZ)
    engine.tick()
    assert len(store.list_issuances(("creating", "active"))) == 1, "日曜に発行されていない"
    engine.now = lambda: datetime(2026, 8, 3, 8, 55, tzinfo=TZ)
    before = len(store.list_issuances())
    engine.tick()
    assert len(store.list_issuances()) == before, "月曜に発行された"
    print("曜日判定 OK")

    # 日跨ぎ 22:00→06:00
    night = store.insert_rule({
        "label": "夜間", "passcode": "987654", "key_type": "permanent",
        "repeat_type": "daily", "weekdays": "", "on_date": "",
        "start_time": "22:00", "end_time": "06:00", "lead_minutes": 5,
        "enabled": 1, "note": "",
    })
    engine.now = lambda: datetime(2026, 8, 5, 2, 0, tzinfo=TZ)   # 前日22時開始の窓の中
    engine.tick()
    got = [i for i in store.list_issuances(("creating", "active")) if i["rule_id"] == night]
    assert got and got[0]["occurrence"] == "2026-08-04", got
    start = datetime.fromtimestamp(got[0]["valid_from"], TZ)
    end = datetime.fromtimestamp(got[0]["valid_to"], TZ)
    assert (start.hour, end.hour, end.day) == (22, 6, 5), (start, end)
    print("日跨ぎ OK:", start, "→", end)

    # 次回予定の計算
    nxt = engine.next_run(store.get_rule(rule_id))
    print("次回予定:", nxt)
    assert nxt and nxt["phase"] in ("発行待ち", "有効期間中")

    print("\nすべて通りました。API 呼び出し履歴:", cloud.calls)


if __name__ == "__main__":
    run()
