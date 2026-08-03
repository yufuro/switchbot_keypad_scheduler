"""スケジュール判定と発行／削除の実行エンジン.

30秒ごとに tick() が呼ばれ、次の判定だけを行います（状態はすべて SQLite にあるので
アプリを再起動しても取りこぼしません）。

    1. 有効なルールについて「今この瞬間、有効であるべき窓」を計算する
    2. まだ発行していなければ createKey を送る（開始 lead_minutes 前から）
    3. 終了時刻を過ぎた発行済みパスコードに deleteKey を送る
    4. 認証パッドの keyList を同期し、パスコード ID と実際の状態を回収する
"""

from __future__ import annotations

import re
import threading
import time
from datetime import date, datetime, time as dtime, timedelta
from typing import Any

import store
from switchbot_api import SwitchBotClient, SwitchBotError

CREATE_TIMEOUT = 420      # createKey がこの秒数たっても反映されなければエラー扱い
DELETE_RETRY = 180        # deleteKey の再送間隔（秒）
CREATE_RETRY = 180        # 発行に失敗したときの再試行間隔（秒）
SYNC_INTERVAL = 60        # keyList を取り直す最短間隔（秒）
PANEL_LOOKAHEAD = 120     # パネル操作時に「直近のスケジュール」とみなす範囲（分）
WEEKDAY_LABELS = ("月", "火", "水", "木", "金", "土", "日")


class Engine:
    def __init__(self, client: SwitchBotClient, device_id: str, tzinfo):
        self.client = client
        self.device_id = device_id
        self.tz = tzinfo
        self.lock = threading.RLock()
        self.keys_cache: list[dict[str, Any]] = []
        self.keys_synced_at: float = 0.0
        self.last_error: str = ""
        self.last_tick_at: float = 0.0
        self._empty_reads: int = 0

    # ------------------------------------------------------------------ 時刻計算
    def now(self) -> datetime:
        return datetime.now(self.tz)

    def _window(self, rule: dict, start_date: date) -> tuple[datetime, datetime]:
        sh, sm = (int(x) for x in rule["start_time"].split(":"))
        eh, em = (int(x) for x in rule["end_time"].split(":"))
        start = datetime.combine(start_date, dtime(sh, sm), tzinfo=self.tz)
        end = datetime.combine(start_date, dtime(eh, em), tzinfo=self.tz)
        if end <= start:                      # 22:00→06:00 のような日跨ぎ
            end += timedelta(days=1)
        return start, end

    def _applies_on(self, rule: dict, d: date) -> bool:
        rt = rule["repeat_type"]
        if rt == "daily":
            return True
        if rt == "weekly":
            days = {int(x) for x in str(rule["weekdays"]).split(",") if x.strip().isdigit()}
            return d.weekday() in days
        if rt == "date":
            return str(rule["on_date"]) == d.isoformat()
        return False

    def candidate_windows(self, rule: dict, now: datetime) -> list[tuple[str, datetime, datetime]]:
        """今日と昨日（日跨ぎ対策）と明日（前倒し発行対策）の窓を返す."""
        out = []
        for delta in (-1, 0, 1):
            d = (now + timedelta(days=delta)).date()
            if not self._applies_on(rule, d):
                continue
            start, end = self._window(rule, d)
            out.append((d.isoformat(), start, end))
        return out

    def next_run(self, rule: dict) -> dict[str, Any] | None:
        """UI 表示用に、次に発行／削除される予定を返す."""
        now = self.now()
        lead = timedelta(minutes=int(rule["lead_minutes"] or 0))
        for delta in range(0, 400):
            d = (now + timedelta(days=delta)).date()
            if not self._applies_on(rule, d):
                continue
            start, end = self._window(rule, d)
            if now < start - lead:
                return {"phase": "発行待ち", "at": (start - lead).isoformat(), "start": start.isoformat(), "end": end.isoformat()}
            if now < end:
                return {"phase": "有効期間中", "at": end.isoformat(), "start": start.isoformat(), "end": end.isoformat()}
        return None

    # -------------------------------------------------------------- keyList 同期
    def sync_keys(self, force: bool = False) -> list[dict[str, Any]]:
        with self.lock:
            if not force and (time.time() - self.keys_synced_at) < SYNC_INTERVAL:
                return self.keys_cache
            try:
                keys = self.client.get_key_list(self.device_id)
                self.last_error = ""
            except SwitchBotError as exc:
                self.last_error = str(exc)
                store.log("ERROR", f"keyList 取得に失敗: {exc}")
                return self.keys_cache

            for k in keys:
                if k.get("password") and k.get("iv"):
                    k["plain"] = self.client.decrypt_password(k["password"], k["iv"])
            self.keys_cache = keys
            self.keys_synced_at = time.time()
            self._reconcile(keys)
            return keys

    def _reconcile(self, keys: list[dict[str, Any]]) -> None:
        by_name = {str(k.get("name")): k for k in keys}
        now = int(time.time())
        pending = store.list_issuances(("creating", "active", "deleting"))
        # 一覧が空の応答は2回続いてから信じる（一時的な不整合で全件消したことにしないため）
        self._empty_reads = 0 if keys else self._empty_reads + 1
        trust_absence = bool(keys) or self._empty_reads >= 2
        for iss in pending:
            found = by_name.get(iss["key_name"])
            if not found and not trust_absence:
                continue
            if found:
                key_id = found.get("id")
                if iss["state"] == "creating":
                    store.update_issuance(iss["id"], state="active", key_id=key_id, detail="")
                    store.log("INFO", f"発行を確認: {iss['key_name']} (id={key_id})")
                elif iss["state"] == "active" and iss["key_id"] is None:
                    store.update_issuance(iss["id"], key_id=key_id)
            else:
                if iss["state"] == "deleting":
                    store.update_issuance(iss["id"], state="deleted", detail="")
                    store.log("INFO", f"削除を確認: {iss['key_name']}")
                elif iss["state"] == "active":
                    store.update_issuance(
                        iss["id"], state="deleted", detail="端末側で消えていました"
                    )
                elif iss["state"] == "creating" and now - iss["created_at"] > CREATE_TIMEOUT:
                    store.update_issuance(
                        iss["id"], state="error",
                        detail="createKey が反映されませんでした（SwitchBot 側でタイムアウト）",
                    )
                    store.log("ERROR", f"発行失敗: {iss['key_name']}")

    # ------------------------------------------------------------------- 発行
    def _create(
        self,
        *,
        rule: dict | None,
        occurrence: str,
        passcode: str,
        key_type: str,
        start: datetime | None,
        end: datetime | None,
        label: str,
    ) -> dict[str, Any]:
        key_name = _key_name(rule["id"] if rule else None, occurrence, label, start)
        base, dup = key_name, 1
        while store.query("SELECT id FROM issuances WHERE key_name=?", (key_name,)):
            dup += 1
            key_name = f"{base}-{dup}"
        valid_from = int(start.timestamp()) if start else None
        valid_to = int(end.timestamp()) if end else None
        issuance_id = store.insert_issuance(
            {
                "rule_id": rule["id"] if rule else None,
                "occurrence": occurrence,
                "key_name": key_name,
                "passcode": passcode,
                "key_type": key_type,
                "valid_from": valid_from,
                "valid_to": valid_to,
                "key_id": None,
                "state": "creating",
                "detail": "",
            }
        )
        try:
            self.client.create_key(
                self.device_id,
                name=key_name,
                password=passcode,
                key_type=key_type,
                start_time=valid_from if key_type == "timeLimit" else None,
                end_time=valid_to if key_type == "timeLimit" else None,
            )
            store.log("INFO", f"createKey 送信: {key_name} / {key_type} / {passcode}")
        except (SwitchBotError, ValueError) as exc:
            store.update_issuance(issuance_id, state="error", detail=str(exc))
            store.log("ERROR", f"createKey 失敗: {key_name}: {exc}")
            raise
        self.keys_synced_at = 0.0   # 次の tick で確認しにいく
        return {"issuance_id": issuance_id, "key_name": key_name}

    def nearest_window(
        self, passcode: str, now: datetime, lookahead: int = PANEL_LOOKAHEAD
    ) -> tuple[dict, str, datetime, datetime] | None:
        """同じ番号の「いま開いている窓」または「これから lookahead 分以内に始まる窓」."""
        upper = now + timedelta(minutes=lookahead)
        best: tuple[dict, str, datetime, datetime] | None = None
        for rule in store.list_rules():
            if not rule["enabled"] or rule["passcode"] != passcode:
                continue
            for occurrence, start, end in self.candidate_windows(rule, now):
                if end <= now or start > upper:
                    continue
                if best is None or start < best[2]:
                    best = (rule, occurrence, start, end)
        return best

    def activate(
        self, passcode: str, label: str, fallback_minutes: int
    ) -> dict[str, Any]:
        """番号を今すぐ有効にする.

        直近（既定2時間以内）に同じ番号のスケジュールがあれば、その回の**開始時刻を
        前倒しした**ものとして発行します。終了時刻は元のスケジュールのままです。
        こうすると発行がひとつで済むので、パネルとスケジュールの切り替えで
        番号が一瞬使えなくなる時間が生まれません。
        直近のスケジュールがなければ fallback_minutes だけ有効な番号を出します。
        """
        with self.lock:
            live = [
                i for i in store.list_issuances(("creating", "active"))
                if i["passcode"] == passcode
            ]
            if live:                            # すでに使える状態なので何もしない
                head = max(live, key=lambda i: i["valid_to"] or 0)
                return {"already": True, "key_name": head["key_name"],
                        "valid_to": head["valid_to"]}
            if self.passcode_busy(passcode):    # 削除の反映待ち
                return {"busy": True}

            now = self.now()
            target = self.nearest_window(passcode, now)
            if target:
                owner, occurrence, start, end = target
                previous = store.find_issuance(owner["id"], occurrence)
                if previous:                    # 同じ回の古い記録は外して作り直す
                    store.update_issuance(
                        previous["id"], occurrence="", detail="パネル操作で発行し直しました"
                    )
                result = self._create(
                    rule=owner,
                    occurrence=occurrence,
                    passcode=passcode,
                    key_type=owner["key_type"],
                    start=now - timedelta(minutes=1),
                    end=end,
                    label=owner["label"],
                )
                result["schedule"] = owner["label"]
                result["scheduled_start"] = start.strftime("%H:%M")
                result["started_early"] = start > now
            else:
                end = now + timedelta(minutes=int(fallback_minutes))
                result = self._create(
                    rule=None,
                    occurrence="",
                    passcode=passcode,
                    key_type="timeLimit",
                    start=now - timedelta(minutes=1),
                    end=end,
                    label=label,
                )
                result["schedule"] = ""
                result["scheduled_start"] = ""
                result["started_early"] = False
            result["aligned_until"] = end.strftime("%H:%M")
            result["valid_to"] = int(end.timestamp())
            return result

    def issue_now(self, rule: dict, minutes: int | None = None) -> dict[str, Any]:
        """管理画面の「今すぐ発行」。パネルと同じ扱いにする."""
        fallback = int(minutes or rule["panel_minutes"] or 120)
        return self.activate(rule["passcode"], rule["label"], fallback)

    def issue_adhoc(self, label: str, passcode: str, minutes: int) -> dict[str, Any]:
        return self.activate(passcode, label or "manual", int(minutes))

    # ------------------------------------------------------------------- 削除
    def _delete_issuance(self, iss: dict, reason: str, retry: bool = False) -> bool:
        rows = store.query("SELECT * FROM issuances WHERE id=?", (iss["id"],))
        if not rows:
            return False
        iss = rows[0]                                  # 直前に他の処理が触った可能性がある
        if iss["state"] in ("deleted", "error"):
            return False
        if iss["state"] == "deleting" and not retry:
            return False
        if iss["key_id"] is None:
            self.sync_keys(force=True)
            iss = (store.query("SELECT * FROM issuances WHERE id=?", (iss["id"],)) or [iss])[0]
        if iss["key_id"] is None:
            if int(time.time()) - iss["created_at"] > CREATE_TIMEOUT:
                store.update_issuance(
                    iss["id"], state="error", detail="パスコード ID が取得できず削除できません"
                )
                store.log("ERROR", f"削除不能: {iss['key_name']}（ID 不明）")
            return False
        try:
            self.client.delete_key(self.device_id, int(iss["key_id"]))
        except SwitchBotError as exc:
            store.update_issuance(iss["id"], detail=f"deleteKey 失敗: {exc}")
            store.log("ERROR", f"deleteKey 失敗: {iss['key_name']}: {exc}")
            return False
        store.update_issuance(iss["id"], state="deleting", detail=reason)
        store.log("INFO", f"deleteKey 送信: {iss['key_name']} (id={iss['key_id']}) / {reason}")
        self.keys_synced_at = 0.0
        return True

    def sweep_same_passcode(
        self, passcode: str, exclude_id: int | None, reason: str, reissue: bool = True
    ) -> int:
        """同じ番号で有効になっている発行を、まとめて削除する.

        別のルールや操作パネルから同じ番号が登録されていると、片方だけ消しても
        ドアは開いたままになります。番号単位で必ず道連れにします。
        まだ自分の時間帯が終わっていないルールは、削除が反映されたあと自動で登録し直します。
        """
        now_ts = int(self.now().timestamp())
        count = 0
        for other in store.list_issuances(("creating", "active")):
            if other["id"] == exclude_id or other["passcode"] != passcode:
                continue
            if not self._delete_issuance(other, reason):
                continue
            count += 1
            store.log("INFO", f"同じ番号 {passcode} を同時に削除: {other['key_name']}")
            if reissue and other["rule_id"] and other["valid_to"] and other["valid_to"] > now_ts:
                # 時間帯がまだ残っているので、あとで登録し直せるようにしておく
                store.update_issuance(
                    other["id"], occurrence="", detail=f"{reason}（時間帯が残るため再登録します）"
                )
        return count

    def passcode_busy(self, passcode: str, exclude_id: int | None = None) -> bool:
        """その番号がまだ端末に載っている（あるいは登録・削除の途中）か."""
        for iss in store.list_issuances(("creating", "active", "deleting")):
            if iss["passcode"] == passcode and iss["id"] != exclude_id:
                return True
        return False

    def revoke_passcode_now(self, passcode: str, reason: str = "手動で無効化") -> int:
        """手で無効にしたものは、その回のスケジュールが残っていても復活させない."""
        with self.lock:
            return self.sweep_same_passcode(passcode, None, reason, reissue=False)

    def revoke_rule_now(self, rule_id: int) -> int:
        """このルールの番号で有効になっているものを、発行元を問わずすべて消す."""
        rule = store.get_rule(rule_id) or {}
        with self.lock:
            count = 0
            for iss in store.list_issuances(("creating", "active")):
                if iss["rule_id"] == rule_id and self._delete_issuance(iss, "手動で無効化"):
                    count += 1
            if rule.get("passcode"):
                count += self.sweep_same_passcode(
                    rule["passcode"], None, "手動で無効化", reissue=False
                )
            return count

    def revoke_issuance(self, issuance_id: int) -> bool:
        rows = store.query("SELECT * FROM issuances WHERE id=?", (issuance_id,))
        if not rows:
            return False
        with self.lock:
            return self._delete_issuance(rows[0], "手動で無効化")

    def delete_key_by_id(self, key_id: int) -> None:
        with self.lock:
            self.client.delete_key(self.device_id, int(key_id))
            store.log("INFO", f"deleteKey 送信（一覧から直接）: id={key_id}")
            self.keys_synced_at = 0.0

    # ------------------------------------------------------------- 操作パネル
    def panel_status(self, rule: dict | None) -> dict[str, Any]:
        """操作パネル用の状態。番号単位で見るので、どこから発行されたかは問いません."""
        if not rule:
            return {"configured": False, "on": False}
        live = [
            i for i in store.list_issuances(("creating", "active", "deleting"))
            if i["passcode"] == rule["passcode"]
        ]
        live.sort(key=lambda i: i["valid_to"] or 0, reverse=True)
        head = live[0] if live else None
        return {
            "configured": True,
            "rule_id": rule["id"],
            "label": rule["label"],
            "passcode": rule["passcode"],
            "panel_minutes": rule["panel_minutes"],
            "on": bool(head and head["state"] in ("creating", "active")),
            "state": head["state"] if head else "off",
            "valid_to": head["valid_to"] if head else None,
            "key_name": head["key_name"] if head else "",
            "count": len(live),
            "now": int(self.now().timestamp()),
        }

    def panel_on(self, rule: dict) -> dict[str, Any]:
        result = self.activate(
            rule["passcode"], rule["label"], int(rule["panel_minutes"] or 120)
        )
        if result.get("already"):
            store.log("INFO", f"操作パネル: {rule['label']} はすでに有効")
        elif result.get("busy"):
            store.log("INFO", f"操作パネル: {rule['label']} は解除の反映待ち")
        elif result.get("schedule"):
            how = "前倒しで開始" if result.get("started_early") else "開始"
            store.log(
                "INFO",
                f"操作パネル: スケジュール「{result['schedule']}」"
                f"（本来 {result['scheduled_start']} 開始）を{how} / "
                f"終了は予定どおり {result['aligned_until']}",
            )
        else:
            store.log(
                "INFO",
                f"操作パネル: 有効化 {rule['label']} / "
                f"直近のスケジュールなし → {result['aligned_until']} まで",
            )
        return result

    def panel_off(self, rule: dict) -> int:
        store.log("INFO", f"操作パネル: 無効化 {rule['label']}")
        return self.revoke_passcode_now(rule["passcode"], "操作パネルで無効化")

    # -------------------------------------------------------------------- tick
    def tick(self) -> None:
        with self.lock:
            self.last_tick_at = time.time()
            now = self.now()

            # 1. 発行すべきものを発行
            for rule in store.list_rules():
                if not rule["enabled"]:
                    continue
                lead = timedelta(minutes=int(rule["lead_minutes"] or 0))
                for occurrence, start, end in self.candidate_windows(rule, now):
                    if not (start - lead <= now < end):
                        continue
                    existing = store.find_issuance(rule["id"], occurrence)
                    if existing:
                        # 回線が切れていたなどで失敗した回は、時間帯が残っていれば作り直す
                        stale = int(time.time()) - existing["updated_at"] > CREATE_RETRY
                        if existing["state"] == "error" and stale:
                            store.update_issuance(
                                existing["id"], occurrence="",
                                detail=f"{existing['detail']} / 再試行しました",
                            )
                            store.log("INFO", f"発行を再試行します: {rule['label']}")
                        else:
                            continue
                    if self.passcode_busy(rule["passcode"]):
                        continue          # 同じ番号が端末に残っている間は登録しない
                    try:
                        self._create(
                            rule=rule,
                            occurrence=occurrence,
                            passcode=rule["passcode"],
                            key_type=rule["key_type"],
                            start=start,
                            end=end,
                            label=rule["label"],
                        )
                    except Exception:      # noqa: BLE001 - ログ済み、次の tick で再試行
                        pass

            # 2. 期限切れを削除（同じ番号の他の発行も道連れにする）
            for iss in store.list_issuances(("creating", "active", "deleting")):
                if not iss["valid_to"]:
                    continue
                if now.timestamp() < iss["valid_to"]:
                    continue
                if iss["state"] == "deleting":
                    if int(time.time()) - iss["updated_at"] > DELETE_RETRY:
                        self._delete_issuance(iss, "削除を再送", retry=True)
                    continue
                if self._delete_issuance(iss, "終了時刻"):
                    self.sweep_same_passcode(
                        iss["passcode"], iss["id"], "同じ番号のため同時に削除"
                    )

            # 3. 端末の状態を同期
            self.sync_keys()


def _key_name(rule_id: int | None, occurrence: str, label: str, start: datetime | None) -> str:
    """認証パッド上の名前（端末内で一意・ASCII 短めが安全）."""
    slug = re.sub(r"[^0-9A-Za-z]+", "", label)[:6]
    if rule_id:
        stamp = occurrence.replace("-", "")
        hhmm = start.strftime("%H%M") if start else "0000"
        return f"R{rule_id}{slug}-{stamp}-{hhmm}"
    return f"M{slug}-{datetime.now().strftime('%m%d%H%M%S')}"


def weekdays_label(csv: str) -> str:
    days = [int(x) for x in str(csv).split(",") if x.strip().isdigit()]
    return "".join(WEEKDAY_LABELS[d] for d in sorted(days)) or "-"


# --------------------------------------------------- 同じ番号どうしの時間帯の衝突判定
WEEK = 7 * 1440


def _hhmm(value: str) -> int:
    h, m = (int(x) for x in str(value).split(":"))
    return h * 60 + m


def _rule_days(rule: dict) -> list[int]:
    rt = rule.get("repeat_type")
    if rt == "daily":
        return list(range(7))
    if rt == "weekly":
        return sorted({int(x) for x in str(rule.get("weekdays", "")).split(",") if x.strip().isdigit()})
    if rt == "date" and rule.get("on_date"):
        try:
            return [date.fromisoformat(str(rule["on_date"])).weekday()]
        except ValueError:
            return []
    return []


def week_segments(rule: dict) -> list[tuple[int, int]]:
    """1週間（0〜10080分）の円環上で、その番号が端末に載っている区間."""
    start = _hhmm(rule["start_time"])
    end = _hhmm(rule["end_time"])
    length = (end - start) % 1440 or 1440
    # 常時有効は登録した瞬間から使えるので、事前登録の分だけ前に伸ばす
    lead = int(rule.get("lead_minutes") or 0) if rule.get("key_type") == "permanent" else 0
    segments: list[tuple[int, int]] = []
    for d in _rule_days(rule):
        begin = (d * 1440 + start - lead) % WEEK
        finish = begin + length + lead
        if finish <= WEEK:
            segments.append((begin, finish))
        else:
            segments.append((begin, WEEK))
            segments.append((0, finish - WEEK))
    return segments


def rules_overlap(a: dict, b: dict) -> bool:
    if a.get("repeat_type") == "date" and b.get("repeat_type") == "date":
        if str(a.get("on_date")) != str(b.get("on_date")):
            return False
    for a0, a1 in week_segments(a):
        for b0, b1 in week_segments(b):
            if a0 < b1 and b0 < a1:
                return True
    return False
