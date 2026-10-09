#!/usr/bin/env python3
"""没有字幕的 YouTube 视频：yt-dlp 下音频 → 本机 ASR 转文字，打印到 stdout（最后一行是 JSON）。

由 paper_ai 以子进程调用（需要环境变量 PIL_ASR_ENABLE=1）。可选依赖：
- `yt-dlp` 在 PATH 里（或用 PIL_YTDLP 指定路径）
- `mlx-qwen3-asr`（Apple Silicon 上的 Qwen3-ASR），模型可用 PIL_ASR_MODEL 覆盖

用法：youtube_asr.py <video_id> [--lang zh|en] [--max-minutes 150]
退出码：0 成功；2 超长跳过；1 失败。临时音频放系统临时目录，用完即清。
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

DEFAULT_ASR_MODEL = "Qwen/Qwen3-ASR-1.7B"


def transcribe_audio(audio_path: str, *, language: str, context: str = "") -> str:
    import mlx_qwen3_asr  # 可选依赖

    result = mlx_qwen3_asr.transcribe(
        str(audio_path),
        model=os.environ.get("PIL_ASR_MODEL") or DEFAULT_ASR_MODEL,
        language="zh" if language.lower().startswith("zh") else language,
        context=context,
    )
    return getattr(result, "text", None) or str(result)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("video_id")
    ap.add_argument("--lang", default="zh")
    ap.add_argument("--max-minutes", type=int, default=150)
    a = ap.parse_args()
    ytdlp = os.environ.get("PIL_YTDLP") or shutil.which("yt-dlp")
    if not ytdlp:
        print("yt-dlp not found", file=sys.stderr)
        return 1
    url = f"https://www.youtube.com/watch?v={a.video_id}"
    meta = subprocess.run([ytdlp, "--skip-download", "--print", "%(duration)s|%(title)s", url],
                          capture_output=True, text=True, timeout=120)
    try:
        duration = float(meta.stdout.strip().split("|", 1)[0])
    except ValueError:
        duration = 0
    if duration and duration > a.max_minutes * 60:
        print(f"skip: {duration/60:.0f} min > {a.max_minutes}", file=sys.stderr)
        return 2
    with tempfile.TemporaryDirectory(prefix="pil_yt_asr_") as tmp:
        out = Path(tmp) / "audio.%(ext)s"
        r = subprocess.run([ytdlp, "-f", "bestaudio[ext=m4a]/bestaudio", "-x", "--audio-format", "m4a",
                            "-o", str(out), url], capture_output=True, text=True, timeout=1800)
        files = list(Path(tmp).glob("audio.*"))
        if r.returncode != 0 or not files:
            print(f"download failed: {r.stderr[-300:]}", file=sys.stderr)
            return 1
        title = meta.stdout.strip().split("|", 1)[-1]
        try:
            text = transcribe_audio(str(files[0]), language=a.lang, context=title)
        except Exception as exc:  # noqa: BLE001
            print(f"asr failed: {exc}", file=sys.stderr)
            return 1
    print(json.dumps({"text": text, "duration_s": duration}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
