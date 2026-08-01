"""SwitchBot 認証パッド 時間管理システム（Flask + APScheduler）.

起動:  python app.py   →  http://127.0.0.1:5058
"""

from __future__ import annotations

import os
import re
import unicodedata
from datetime import datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request

import store
from scheduler_core import Engine, rules_overlap, weekdays_label
from switchbot_api import SwitchBotClient, SwitchBotError

load_dotenv()

TOKEN = os.getenv("SWITCHBOT_TOKEN", "")
SECRET = os.getenv("SWITCHBOT_SECRET", "")
DEVICE_ID = os.getenv("SWITCHBOT_DEVICE_ID", "")
TZ = ZoneInfo(os.getenv("APP_TZ", "Asia/Tokyo"))
HOST = os.getenv("APP_HOST", "127.0.0.1")
PORT = int(os.getenv("APP_PORT", "5058"))
TICK_SECONDS = int(os.getenv("TICK_SECONDS", "30"))

app = Flask(__name__)
store.init()
client = SwitchBotClient(TOKEN, SECRET)
engine = Engine(client, DEVICE_ID, TZ)

TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


# --------------------------------------------------------------------- 入力検証
class BadInput(ValueError):
    pass


def clean_rule(payload: dict, rule_id: int | None = None) -> dict:
    label = str(payload.get("label", "")).strip()
    passcode = normalize_code(payload.get("passcode"))
    key_type = str(payload.get("key_type", "timeLimit"))
    repeat_type = str(payload.get("repeat_type", "daily"))
    weekdays = payload.get("weekdays") or []
    on_date = str(payload.get("on_date", "")).strip()
    start_time = str(payload.get("start_time", "")).strip()
    end_time = str(payload.get("end_time", "")).strip()

    if not label:
        raise BadInput("名前を入力してください")
    if not (passcode.isdigit() and 6 <= len(passcode) <= 12):
        raise BadInput("パスコードは6〜12桁の数字にしてください")
    if key_type not in ("timeLimit", "permanent"):
        raise BadInput("種類が不正です")
    if repeat_type not in ("daily", "weekly", "date"):
        raise BadInput("繰り返し方法が不正です")
    if not TIME_RE.match(start_time) or not TIME_RE.match(end_time):
        raise BadInput("時刻は HH:MM 形式で入力してください")
    if start_time == end_time:
        raise BadInput("開始と終了が同じ時刻です")

    if isinstance(weekdays, str):
        weekdays = [x for x in weekdays.split(",") if x.strip()]
    days = sorted({int(d) for d in weekdays if str(d).isdigit() and 0 <= int(d) <= 6})
    if repeat_type == "weekly" and not days:
        raise BadInput("曜日を1つ以上選んでください")
    if repeat_type == "date":
        try:
            datetime.strptime(on_date, "%Y-%m-%d")
        except ValueError:
            raise BadInput("日付は YYYY-MM-DD 形式で入力してください") from None

    try:
        panel_minutes = int(payload.get("panel_minutes", 120))
    except (TypeError, ValueError):
        raise BadInput("パネルの有効時間が不正です") from None
    if not 1 <= panel_minutes <= 43200:
        raise BadInput("パネルの有効時間は1分〜30日の範囲で指定してください")

    try:
        lead = int(payload.get("lead_minutes", 10))
    except (TypeError, ValueError):
        raise BadInput("事前発行の分数が不正です") from None
    if not 0 <= lead <= 1440:
        raise BadInput("事前発行は0〜1440分の範囲で指定してください")

    candidate = {
        "passcode": passcode, "key_type": key_type, "repeat_type": repeat_type,
        "weekdays": ",".join(str(d) for d in days),
        "on_date": on_date if repeat_type == "date" else "",
        "start_time": start_time, "end_time": end_time, "lead_minutes": lead,
    }
    for other in store.list_rules():
        if other["id"] == rule_id or other["passcode"] != passcode:
            continue
        if rules_overlap(candidate, other):
            raise BadInput(
                f"「{other['label']}」（{other['start_time']}〜{other['end_time']}）と"
                "同じ番号で時間帯が重なっています。認証パッドは同じ番号を二重に登録できません"
            )

    return {
        "label": label[:40],
        "passcode": passcode,
        "key_type": key_type,
        "repeat_type": repeat_type,
        "weekdays": ",".join(str(d) for d in days),
        "on_date": on_date if repeat_type == "date" else "",
        "start_time": start_time,
        "end_time": end_time,
        "lead_minutes": lead,
        "enabled": 1 if payload.get("enabled", True) else 0,
        "note": str(payload.get("note", ""))[:200],
        "panel": 1 if payload.get("panel") else 0,
        "panel_minutes": panel_minutes,
    }


def fail(message: str, code: int = 400):
    store.log("WARN", f"受け付けませんでした: {message}")
    return jsonify({"ok": False, "error": message}), code


def normalize_code(value) -> str:
    """全角数字・空白・ハイフンを吸収して半角数字だけにする."""
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    return re.sub(r"[\s\-_]", "", text)


# ------------------------------------------------------------------------ 画面
@app.get("/")
def index():
    return render_template("index.html", device_id=DEVICE_ID, tz=str(TZ))


@app.get("/api/state")
def api_state():
    keys = engine.sync_keys(force=request.args.get("refresh") == "1")
    rules = []
    for rule in store.list_rules():
        r = dict(rule)
        r["weekdays_label"] = weekdays_label(rule["weekdays"])
        r["next_run"] = engine.next_run(rule) if rule["enabled"] else None
        rules.append(r)
    return jsonify(
        {
            "ok": True,
            "now": engine.now().isoformat(timespec="seconds"),
            "tz": str(TZ),
            "device_id": DEVICE_ID,
            "rules": rules,
            "issuances": store.list_issuances(),
            "keys": keys,
            "keys_synced_at": engine.keys_synced_at,
            "last_error": engine.last_error,
            "logs": store.recent_logs(60),
        }
    )


# ------------------------------------------------------------------- ルール CRUD
@app.post("/api/rules")
def api_create_rule():
    try:
        data = clean_rule(request.get_json(force=True) or {})
    except BadInput as exc:
        return fail(str(exc))
    rule_id = store.insert_rule(data)
    if data["panel"]:
        store.clear_panel_except(rule_id)
    store.log("INFO", f"ルールを追加: #{rule_id} {data['label']}")
    return jsonify({"ok": True, "id": rule_id})


@app.put("/api/rules/<int:rule_id>")
def api_update_rule(rule_id: int):
    if not store.get_rule(rule_id):
        return fail("そのルールはありません", 404)
    try:
        data = clean_rule(request.get_json(force=True) or {}, rule_id=rule_id)
    except BadInput as exc:
        return fail(str(exc))
    store.update_rule(rule_id, data)
    if data["panel"]:
        store.clear_panel_except(rule_id)
    store.log("INFO", f"ルールを更新: #{rule_id} {data['label']}")
    return jsonify({"ok": True})


@app.delete("/api/rules/<int:rule_id>")
def api_delete_rule(rule_id: int):
    if not store.get_rule(rule_id):
        return fail("そのルールはありません", 404)
    revoked = engine.revoke_rule_now(rule_id)
    store.delete_rule(rule_id)
    store.log("INFO", f"ルールを削除: #{rule_id}（有効なパスコード {revoked} 件も削除）")
    return jsonify({"ok": True, "revoked": revoked})


# --------------------------------------------------------------------- 即時操作
@app.post("/api/rules/<int:rule_id>/issue-now")
def api_issue_now(rule_id: int):
    rule = store.get_rule(rule_id)
    if not rule:
        return fail("そのルールはありません", 404)
    body = request.get_json(silent=True) or {}
    try:
        result = engine.issue_now(rule, minutes=body.get("minutes"))
    except (SwitchBotError, ValueError) as exc:
        return fail(str(exc), 502)
    return jsonify({"ok": True, **result})


@app.post("/api/rules/<int:rule_id>/revoke-now")
def api_revoke_now(rule_id: int):
    if not store.get_rule(rule_id):
        return fail("そのルールはありません", 404)
    return jsonify({"ok": True, "revoked": engine.revoke_rule_now(rule_id)})


@app.post("/api/issue-adhoc")
def api_issue_adhoc():
    body = request.get_json(silent=True) or {}
    passcode = normalize_code(body.get("passcode"))
    if not (passcode.isdigit() and 6 <= len(passcode) <= 12):
        return fail(
            f"パスコードは6〜12桁の数字にしてください（受け取った値「{passcode}」{len(passcode)}文字）"
        )
    try:
        minutes = int(body.get("minutes", 60))
    except (TypeError, ValueError):
        return fail("有効時間が不正です")
    if not 1 <= minutes <= 43200:
        return fail("有効時間は1分〜30日の範囲で指定してください")
    try:
        result = engine.issue_adhoc(str(body.get("label", "manual")), passcode, minutes)
    except (SwitchBotError, ValueError) as exc:
        return fail(str(exc), 502)
    return jsonify({"ok": True, **result})


@app.post("/api/issuances/<int:issuance_id>/revoke")
def api_revoke_issuance(issuance_id: int):
    ok = engine.revoke_issuance(issuance_id)
    return jsonify({"ok": ok, "error": "" if ok else "パスコード ID が未確定です。少し待って再試行してください"})


@app.post("/api/keys/<int:key_id>/delete")
def api_delete_key(key_id: int):
    try:
        engine.delete_key_by_id(key_id)
    except SwitchBotError as exc:
        return fail(str(exc), 502)
    return jsonify({"ok": True})


@app.post("/api/tick")
def api_tick():
    engine.tick()
    return jsonify({"ok": True})


# ---------------------------------------------------------------- 操作パネル
@app.get("/panel")
def panel_page():
    return render_template("panel.html", tz=str(TZ))


@app.get("/api/panel")
def api_panel_status():
    engine.sync_keys(force=request.args.get("refresh") == "1")
    return jsonify({"ok": True, **engine.panel_status(store.get_panel_rule())})


@app.post("/api/panel/on")
def api_panel_on():
    rule = store.get_panel_rule()
    if not rule:
        return fail("操作パネルで使う番号が決まっていません。管理画面で1件指定してください", 409)
    try:
        result = engine.panel_on(rule)
    except (SwitchBotError, ValueError) as exc:
        return fail(str(exc), 502)
    return jsonify({"ok": True, **result, **engine.panel_status(store.get_panel_rule())})


@app.post("/api/panel/off")
def api_panel_off():
    rule = store.get_panel_rule()
    if not rule:
        return fail("操作パネルで使う番号が決まっていません。管理画面で1件指定してください", 409)
    revoked = engine.panel_off(rule)
    return jsonify({"ok": True, "revoked": revoked, **engine.panel_status(rule)})


# ------------------------------------------------------------------------ 起動
def start_scheduler() -> BackgroundScheduler:
    sched = BackgroundScheduler(timezone=TZ)
    sched.add_job(
        engine.tick,
        "interval",
        seconds=TICK_SECONDS,
        id="tick",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=120,
    )
    sched.start()
    return sched


if __name__ == "__main__":
    missing = [
        n for n, v in (
            ("SWITCHBOT_TOKEN", TOKEN),
            ("SWITCHBOT_SECRET", SECRET),
            ("SWITCHBOT_DEVICE_ID", DEVICE_ID),
        ) if not v
    ]
    if missing:
        raise SystemExit(f".env に {', '.join(missing)} を設定してください")

    store.log("INFO", f"起動しました device={DEVICE_ID} tz={TZ} tick={TICK_SECONDS}s")
    scheduler = start_scheduler()
    try:
        app.run(host=HOST, port=PORT, debug=False, use_reloader=False, threaded=True)
    finally:
        scheduler.shutdown(wait=False)
