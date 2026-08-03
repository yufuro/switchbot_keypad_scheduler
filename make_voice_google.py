"""Google Cloud Text-to-Speech で操作パネルの読み上げ音声を作る.

準備（一度だけ）
  1. Google Cloud のプロジェクトで「Cloud Text-to-Speech API」を有効にする
  2. 「APIとサービス > 認証情報」で API キーを作る
     作ったキーを開き、「APIの制限」で "Cloud Text-to-Speech API" を選んで保存すること。
     ここが空だと 403 API_KEY_SERVICE_BLOCKED になります。
  3. export GOOGLE_TTS_API_KEY=作ったキー

  API キーを使わず gcloud の認証で通すこともできます。
    gcloud auth login && gcloud config set project プロジェクトID
  （GOOGLE_TTS_API_KEY を設定しなければ、自動でこちらを使います）

使い方
  python make_voice_google.py --list                      # 日本語の声を一覧
  python make_voice_google.py                             # 既定の声で7ファイル作成
  python make_voice_google.py --voice ja-JP-Chirp3-HD-Aoede --rate 0.95
  python make_voice_google.py --text on="パスコード、有効です"   # 文言だけ差し替え

作られたファイルは audio/custom/ に入り、アプリを再起動すると自動で使われます
（say コマンドより優先されます）。合計60文字ほどなので、無料枠に収まる範囲です。
"""

from __future__ import annotations

import argparse
import base64
import os
import subprocess
import sys
from pathlib import Path

import requests

from phrases import PHRASES

ENDPOINT = "https://texttospeech.googleapis.com/v1"
OUT_DIR = Path(__file__).with_name("audio") / "custom"
DEFAULT_VOICE = "ja-JP-Chirp3-HD-Aoede"


def _gcloud(*args: str) -> str:
    try:
        out = subprocess.run(["gcloud", *args], capture_output=True, timeout=30, check=True)
        return out.stdout.decode().strip()
    except (subprocess.SubprocessError, OSError):
        return ""


def auth() -> tuple[dict, dict]:
    """API キーがあればそれを使い、無ければ gcloud のアクセストークンを使う."""
    key = os.getenv("GOOGLE_TTS_API_KEY", "").strip()
    if key:
        return {"key": key}, {}
    token = os.getenv("GOOGLE_TTS_ACCESS_TOKEN", "").strip() or _gcloud("auth", "print-access-token")
    if token:
        headers = {"Authorization": f"Bearer {token}"}
        project = (os.getenv("GOOGLE_CLOUD_PROJECT", "").strip()
                   or _gcloud("config", "get-value", "project"))
        if project and project != "(unset)":
            headers["x-goog-user-project"] = project
        return {}, headers
    sys.exit(
        "認証情報がありません。次のどちらかを用意してください。\n"
        "  ・API キー:   export GOOGLE_TTS_API_KEY=...\n"
        "  ・gcloud:     gcloud auth login && gcloud auth application-default login"
    )


def explain_error(status: int, payload: dict) -> str:
    error = payload.get("error", {})
    message = error.get("message", "")
    reasons = {d.get("reason", "") for d in error.get("details", []) if isinstance(d, dict)}
    blocked = "API_KEY_SERVICE_BLOCKED" in reasons or "are blocked" in message
    disabled = "SERVICE_DISABLED" in reasons or "has not been used in project" in message
    lines = [f"生成に失敗しました（{status}）: {message or payload}"]
    if blocked:
        lines += [
            "",
            "APIキーの「APIの制限」に Text-to-Speech が入っていません。次の手順で直ります。",
            "  1. Google Cloud コンソール > APIとサービス > 認証情報",
            "  2. 使っている APIキーをクリック",
            "  3. 「APIの制限」で『キーを制限』を選び、一覧から",
            "     「Cloud Text-to-Speech API」にチェックを入れて保存",
            "     （とりあえず試すなら『キーを制限しない』でも動きます）",
            "  4. 反映に数分かかることがあります",
            "",
            "APIキーを使わない方法もあります:",
            "  gcloud auth login && gcloud config set project プロジェクトID",
            "  unset GOOGLE_TTS_API_KEY   # これでアクセストークン認証に切り替わります",
        ]
    elif disabled:
        lines += [
            "",
            "プロジェクトで API が有効になっていません。",
            "  gcloud services enable texttospeech.googleapis.com",
            "  もしくはコンソールの「APIとサービス > ライブラリ」で",
            "  「Cloud Text-to-Speech API」を有効にしてください。",
        ]
    return "\n".join(lines)


def list_voices() -> None:
    params, headers = auth()
    res = requests.get(
        f"{ENDPOINT}/voices", params={**params, "languageCode": "ja-JP"},
        headers=headers, timeout=30,
    )
    if res.status_code >= 400:
        sys.exit(explain_error(res.status_code, res.json() if res.content else {}))
    rows = res.json().get("voices", [])
    rows.sort(key=lambda v: v.get("name", ""))
    for voice in rows:
        gender = {"FEMALE": "女性", "MALE": "男性"}.get(voice.get("ssmlGender", ""), "-")
        print(f"  {voice.get('name'):32} {gender}")
    print(f"\n  {len(rows)} 件。名前をそのまま --voice に指定してください。")


def synthesize(text: str, voice: str, rate: float, fmt: str) -> bytes:
    body = {
        "input": {"text": text},
        "voice": {"languageCode": "-".join(voice.split("-")[:2]), "name": voice},
        "audioConfig": {"audioEncoding": fmt.upper(), "speakingRate": rate},
    }
    params, headers = auth()
    res = requests.post(
        f"{ENDPOINT}/text:synthesize", params=params, headers=headers, json=body, timeout=60
    )
    if res.status_code >= 400:
        sys.exit(explain_error(res.status_code, res.json() if res.content else {}))
    return base64.b64decode(res.json()["audioContent"])


def main() -> None:
    parser = argparse.ArgumentParser(description="操作パネルの読み上げ音声を作る")
    parser.add_argument("--voice", default=DEFAULT_VOICE, help=f"声の名前（既定 {DEFAULT_VOICE}）")
    parser.add_argument("--rate", type=float, default=1.0, help="話す速さ 0.25〜2.0（既定 1.0）")
    parser.add_argument("--format", default="mp3", choices=["mp3", "linear16", "ogg_opus"],
                        help="音声形式（既定 mp3）")
    parser.add_argument("--text", action="append", default=[], metavar="KEY=文言",
                        help="文言を差し替える（例 --text on=パスコードが使えます）")
    parser.add_argument("--list", action="store_true", help="日本語の声を一覧表示して終了")
    args = parser.parse_args()

    if args.list:
        list_voices()
        return

    phrases = dict(PHRASES)
    for item in args.text:
        key, _, value = item.partition("=")
        if key not in phrases:
            sys.exit(f"知らないキーです: {key}（使えるのは {', '.join(phrases)}）")
        phrases[key] = value

    ext = {"mp3": ".mp3", "linear16": ".wav", "ogg_opus": ".ogg"}[args.format]
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for key, text in phrases.items():
        audio = synthesize(text, args.voice, args.rate, args.format)
        for old in OUT_DIR.glob(f"{key}.*"):        # 形式を変えたときの重複を防ぐ
            old.unlink()
        path = OUT_DIR / f"{key}{ext}"
        path.write_bytes(audio)
        print(f"  {path.name:16} {len(audio):>7,} bytes  「{text}」")

    print(f"\n{len(phrases)} 件を {OUT_DIR} に作りました。アプリを再起動すると使われます。")


if __name__ == "__main__":
    main()
