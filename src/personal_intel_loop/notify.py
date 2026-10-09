"""可配置的推送通道。没有任何内置账号；不配置就不推送。

- channel="command": 运行环境变量 `PIL_NOTIFY_CMD` 指定的命令（经 shell），消息正文走 stdin，
  标题放在环境变量 `PIL_NOTIFY_SUBJECT` 里。可以接任何你自己的推送脚本（ntfy、Bark、
  Telegram bot、osascript 通知等）。
- channel="email": SMTP。`PIL_SMTP_HOST` / `PIL_SMTP_PORT`(缺省 587, STARTTLS) / `PIL_SMTP_USER` /
  `PIL_SMTP_PASSWORD`（或 `PIL_SMTP_PASSWORD_FILE`）/ `PIL_NOTIFY_EMAIL_FROM` / `PIL_NOTIFY_EMAIL_TO`。
- channel="none": 什么都不做。

失败返回 False 而不是抛异常，调用方自己决定是否重试。
"""
from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

CHANNELS = ("command", "email", "none")


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def send_command(text: str, *, subject: str = "") -> bool:
    cmd = _env("PIL_NOTIFY_CMD")
    if not cmd:
        logger.warning("PIL_NOTIFY_CMD not set; notification skipped")
        return False
    env = dict(os.environ, PIL_NOTIFY_SUBJECT=subject)
    try:
        completed = subprocess.run(cmd, shell=True, input=text, text=True, env=env, timeout=60,
                                   capture_output=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning("notify command failed: %s", exc)
        return False
    if completed.returncode != 0:
        logger.warning("notify command exit %s: %s", completed.returncode, (completed.stderr or "")[-200:])
        return False
    return True


def send_email(subject: str, body: str) -> bool:
    import smtplib
    from email.message import EmailMessage

    host = _env("PIL_SMTP_HOST")
    to_addr = _env("PIL_NOTIFY_EMAIL_TO")
    if not host or not to_addr:
        logger.warning("PIL_SMTP_HOST / PIL_NOTIFY_EMAIL_TO not set; email skipped")
        return False
    user = _env("PIL_SMTP_USER")
    password = _env("PIL_SMTP_PASSWORD")
    pw_file = _env("PIL_SMTP_PASSWORD_FILE")
    if not password and pw_file:
        try:
            password = Path(pw_file).expanduser().read_text("utf-8").strip()
        except OSError as exc:
            logger.warning("PIL_SMTP_PASSWORD_FILE unreadable: %s", exc)
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = _env("PIL_NOTIFY_EMAIL_FROM") or user or to_addr
    msg["To"] = to_addr
    msg.set_content(body)
    try:
        with smtplib.SMTP(host, int(_env("PIL_SMTP_PORT", "587")), timeout=30) as smtp:
            smtp.starttls()
            if user:
                smtp.login(user, password)
            smtp.send_message(msg)
    except Exception as exc:  # noqa: BLE001
        logger.warning("email send failed: %s", exc)
        return False
    return True


def send(text: str, channel: str, *, subject: str = "personal-intel-loop") -> bool:
    if channel == "none":
        return True
    if channel == "command":
        return send_command(text, subject=subject)
    if channel == "email":
        return send_email(subject, text)
    raise ValueError(f"unsupported channel: {channel!r}")
