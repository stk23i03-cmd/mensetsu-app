import os
import uuid
import time
import shutil
import tempfile
import subprocess
from pathlib import Path
from typing import Dict, Any, List

import requests
import whisper
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

# ====== 環境設定 ======
HOST = os.getenv("BACKEND_HOST", "0.0.0.0")
PORT = int(os.getenv("BACKEND_PORT", "8001"))

# Ollama
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "gpt-oss:20b")

# SSL（開発では通常使わない）
CERT_FILE = os.getenv("SSL_CERT_FILE", str(Path(__file__).with_name("cert.pem")))
KEY_FILE = os.getenv("SSL_KEY_FILE", str(Path(__file__).with_name("key.pem")))

# オーディオ出力ディレクトリ
AUDIO_DIR = Path(os.getenv("AUDIO_DIR", Path(__file__).with_name("static") / "audio"))
AUDIO_DIR.mkdir(parents=True, exist_ok=True)

# ====== 依存（Whisper / Open JTalk） ======
# Whisper（CPU では FP32 警告が出ますが無害）
whisper_model = whisper.load_model(os.getenv("WHISPER_MODEL", "large"))

# --- Open JTalk 設定（環境変数で上書き可）---
OPENJTALK_BIN = shutil.which(os.getenv("OPENJTALK_BIN", "open_jtalk")) or "open_jtalk"
OPENJTALK_DICT = os.getenv("OPENJTALK_DICT", "/var/lib/mecab/dic/open-jtalk/naist-jdic")
# 既定は女性(めい)の標準ボイス。男性にするなら nitech に変更可。
OPENJTALK_VOICE = os.getenv("OPENJTALK_VOICE", "/usr/share/hts-voice/nitech-jp-atr503-m001/nitech_jp_atr503_m001.htsvoice")
OPENJTALK_SAMPLING = int(os.getenv("OPENJTALK_SAMPLING", "48000"))
OPENJTALK_SPEED = float(os.getenv("OPENJTALK_SPEED", "1.0"))  # 0.7~1.3 あたり
OPENJTALK_GAIN_DB = float(os.getenv("OPENJTALK_GAIN_DB", "4.0"))  # 出力ゲイン(dB)

def _assert_openjtalk_ready():
    if not shutil.which(OPENJTALK_BIN):
        raise RuntimeError(f"open_jtalk が見つかりません: {OPENJTALK_BIN}")
    if not Path(OPENJTALK_DICT).exists():
        raise RuntimeError(f"Open JTalk 辞書が見つかりません: {OPENJTALK_DICT}")
    if not Path(OPENJTALK_VOICE).exists():
        raise RuntimeError(f"HTS 音声が見つかりません: {OPENJTALK_VOICE}")

def tts_to_wav_openjtalk(text: str, out_wav_path: Path,
                         speed: float = OPENJTALK_SPEED,
                         volume_db: float = OPENJTALK_GAIN_DB,
                         sampling: int = OPENJTALK_SAMPLING) -> str:
    """
    Open JTalk で WAV を生成
    - speed: 発話速度（1.0=等速）
    - volume_db: 出力ゲイン(dB)
    - sampling: サンプリング周波数（48000 推奨）
    """
    _assert_openjtalk_ready()
    out_wav_path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as td:
        tmp_wav = Path(td) / "tmp.wav"
        cmd = [
            OPENJTALK_BIN,
            "-x", OPENJTALK_DICT,
            "-m", OPENJTALK_VOICE,
            "-r", str(speed),
            "-s", str(sampling),
            "-ow", str(tmp_wav),
        ]
        if abs(volume_db) > 1e-6:
            cmd.extend(["-g", str(volume_db)])

        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        stdout, stderr = proc.communicate(text.encode("utf-8"))
        if proc.returncode != 0:
            raise RuntimeError(
                f"open_jtalk 失敗: rc={proc.returncode}\n{stderr.decode(errors='ignore')}"
            )
        tmp_wav.replace(out_wav_path)
    return str(out_wav_path)

# ====== アプリ ======
app = FastAPI()

# CORS：開発中は * で許可（本番は適切に制限してください）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 静的ファイル配信（TTS 生成音声）
static_root = Path(__file__).with_name("static")
static_root.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_root)), name="static")

# ====== セッション管理（メモリ内） ======
sessions: Dict[str, Dict[str, Any]] = {}

SYSTEM_PROMPT = (
    "あなたは『高校生向けの模擬面接官』です。目的は、進学または就職の面接練習を現実に近い形で行うことです。\n"
    "・口調は丁寧で公平、やや厳しめ。・一度に1〜2問だけ。・解説は控えめで総評は終了時。\n"
    "・特殊な記号、絵文字、全角スペースは絶対に出力しない。・日本語の標準語、敬語。\n"
    "・ユーザー情報(進学/就職, 志望分野, 志望先)を踏まえて質問する。"
)

def build_intro(user_info: Dict[str, str]) -> str:
    track = user_info.get("track", "進学")
    field = user_info.get("field", "未指定の分野")
    target = user_info.get("target", "未指定の志望先")
    if track == "進学":
        return f"面接練習を始めます。{target}（{field}）を志望とのことですね。まず志望理由を簡潔に教えてください。"
    else:
        return f"面接練習を始めます。{target}（{field}）を志望とのことですね。まず自己紹介と志望動機を1分程度でお願いします。"

# ====== 音声処理ユーティリティ ======
def transcode_to_wav(src_path: Path, dst_path: Path) -> None:
    """webm/mp4 などを ffmpeg で 16kHz mono WAV へ変換"""
    cmd = [
        "ffmpeg", "-y",
        "-i", str(src_path),
        "-ac", "1", "-ar", "16000",  # Whisper 向け
        str(dst_path),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def stt_whisper(wav_path: Path) -> str:
    """Whisper で文字起こし（日本語想定）"""
    result = whisper_model.transcribe(str(wav_path), language="ja")
    return (result.get("text") or "").strip()

# ====== Ollama 呼び出し（/api/chat → 404 時 /api/generate に自動フォールバック） ======
def chat_ollama(messages: List[Dict[str, str]]) -> str:
    """
    messages: [{"role": "system"|"user"|"assistant", "content": "..."}]
    1) まず /api/chat を叩く
    2) 404 の場合は、旧 API 互換として /api/generate にフォールバック
    """
    # まず /api/chat
    url_chat = f"{OLLAMA_URL}/api/chat"
    payload_chat = {"model": OLLAMA_MODEL, "messages": messages, "stream": False}
    r = requests.post(url_chat, json=payload_chat, timeout=120)

    if r.status_code == 404:
        # 旧 Ollama 互換: /api/generate へ
        prompt = "\n".join(f"{m['role']}: {m['content']}" for m in messages)
        url_gen = f"{OLLAMA_URL}/api/generate"
        payload_gen = {"model": OLLAMA_MODEL, "prompt": prompt, "stream": False}
        r = requests.post(url_gen, json=payload_gen, timeout=120)

    r.raise_for_status()
    data = r.json()
    # /api/chat は data.message.content, /api/generate は data.response
    content = (data.get("message", {}) or {}).get("content") or data.get("response", "")
    return (content or "").strip()

# ====== ルート ======
@app.post("/session/start")
def session_start(
    track: str = Form(..., description="進学 or 就職"),
    field: str = Form(..., description="志望分野"),
    target: str = Form(..., description="志望先(企業/学校名)"),
):
    if track not in ("進学", "就職"):
        return JSONResponse(status_code=400, content={"error": "track は『進学』か『就職』です"})

    sid = str(uuid.uuid4())
    system_with_user = (
        SYSTEM_PROMPT +
        f"\nユーザー情報: track={track}, 分野={field}, 志望先={target}。これに即した質問から開始してください。"
    )

    intro = build_intro({"track": track, "field": field, "target": target})
    sessions[sid] = {
        "messages": [
            {"role": "system", "content": system_with_user},
            {"role": "assistant", "content": intro},
        ],
        "created_at": time.time(),
        "track": track,
        "field": field,
        "target": target,
    }
    return {"session_id": sid, "first_message": intro}

@app.post("/turn")
def turn(
    session_id: str = Form(...),
    audio: UploadFile = File(..., description="WebM/Opus などの録音ファイル"),
):
    if session_id not in sessions:
        raise HTTPException(status_code=404, detail="invalid session")

    # 1) 受信 & 一時保存
    try:
        tmp_dir = Path("./_tmp")
        tmp_dir.mkdir(exist_ok=True)
        src = tmp_dir / f"{uuid.uuid4()}.webm"
        with src.open("wb") as f:
            f.write(audio.file.read())
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"upload_error: {e}")

    # 2) ffmpeg 変換
    try:
        wav = tmp_dir / (src.stem + ".wav")
        transcode_to_wav(src, wav)
    except FileNotFoundError as e:
        # 典型: ffmpeg 未インストール
        raise HTTPException(status_code=500, detail=f"ffmpeg_not_found: {e}")
    except subprocess.CalledProcessError as e:
        raise HTTPException(status_code=500, detail=f"ffmpeg_error: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"ffmpeg_unknown_error: {e}")

    # 3) Whisper STT
    try:
        user_text = stt_whisper(wav)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"whisper_error: {e}")

    # 4) LLM（Ollama）
    msgs: List[Dict[str, str]] = sessions[session_id]["messages"]
    msgs.append({"role": "user", "content": user_text})
    try:
        assistant_text = chat_ollama(msgs)
    except requests.HTTPError as e:
        # サーバの応答エラー（404, 500 など）
        body = ""
        try:
            body = e.response.text[:300]
        except Exception:
            pass
        raise HTTPException(
            status_code=502,
            detail=f"ollama_http_error: {getattr(e.response, 'status_code', '???')} {body}"
        )
    except requests.RequestException as e:
        # 接続不能など
        raise HTTPException(status_code=502, detail=f"ollama_request_error: {e}")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"ollama_error: {e}")

    msgs.append({"role": "assistant", "content": assistant_text})

    # 5) TTS（Open JTalk）— 失敗してもテキストは返す
    ts = int(time.time())
    out_wav = AUDIO_DIR / f"{session_id}-{ts}.wav"
    try:
        # 「。」と「*」を半角スペースに置換
        clean_text = assistant_text.replace("*", "").replace("！", "").replace("？", "").replace("\n", "")
        tts_to_wav_openjtalk(clean_text, out_wav)
        audio_url = f"/static/audio/{out_wav.name}"
    except Exception as e:
        audio_url = ""

    return {
        "user_text": user_text,
        "assistant_text": assistant_text,
        "audio_url": audio_url,
    }

@app.post("/session/end")
def session_end(session_id: str = Form(...)):
    if session_id not in sessions:
        return JSONResponse(status_code=404, content={"error": "invalid session"})

    # 必要ならサマリー生成（Ollama）など
    msgs = sessions[session_id]["messages"].copy()
    summary_prompt = (
        "上記は模擬面接のログです。以下を日本語で簡潔にまとめてください：\n"
        "- 良かった点\n- 改善点\n- 次回までの宿題（志望動機・自己PRの改善例を3-5文）\n"
    )
    msgs.append({"role": "user", "content": summary_prompt})
    try:
        summary = chat_ollama(msgs)
    except Exception:
        summary = "（サマリー生成に失敗しました。テキストログを参考に振り返ってください）"

    # セッションをクリーンアップ（任意）
    # del sessions[session_id]

    return {"summary": summary}

# 任意：ヘルスチェック（簡易）
@app.get("/health")
def health():
    info = {
        "ollama_url": OLLAMA_URL,
        "ollama_model": OLLAMA_MODEL,
        "audio_dir_exists": AUDIO_DIR.exists(),
    }
    # ffmpeg チェック
    try:
        subprocess.run(["ffmpeg", "-version"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        info["ffmpeg"] = "ok"
    except Exception as e:
        info["ffmpeg"] = f"error: {e}"

    # Open JTalk チェック（存在だけ）
    try:
        _assert_openjtalk_ready()
        info["open_jtalk"] = "ok"
    except Exception as e:
        info["open_jtalk"] = f"error: {e}"

    return info

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host=HOST, port=PORT,
        # 開発中は HTTP で十分。HTTPS を使うなら下を有効化し、証明書を信頼させてください。
        ssl_certfile=CERT_FILE, ssl_keyfile=KEY_FILE,
        reload=False,
    )

