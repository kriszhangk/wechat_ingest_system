#!/usr/bin/env python3
"""生成并发送早/晚报。"""

import json
import os
import sqlite3
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

PROJECT_DIR = Path("/root/.openclaw/workspace/wechat_ingest_system")
GENERATOR = PROJECT_DIR / "report_generator.py"
WECHAT_DRAFT_PUBLISHER = PROJECT_DIR / "wechat_draft_publisher.py"
PYTHON_BIN = PROJECT_DIR / ".venv" / "bin" / "python"
STATE_FILE = PROJECT_DIR / "report_state.json"
DB_PATH = PROJECT_DIR / "server" / "data" / "app.db"
TARGET_CHAT = os.getenv("WECHAT_INGEST_REPORT_TARGET_CHAT", "-5289939595")
WEB_BASE_URL = os.getenv("WECHAT_INGEST_WEB_BASE_URL", "http://43.167.177.15:8000").rstrip("/")
MAX_LEN = 4050
AUTO_CREATE_WECHAT_DRAFT = str(os.getenv("WECHAT_INGEST_AUTO_CREATE_WECHAT_DRAFT", "true")).strip().lower() in {"1", "true", "yes", "on"}
try:
    GENERATOR_TIMEOUT = int(str(os.getenv("WECHAT_INGEST_GENERATOR_TIMEOUT", "900")).strip())
except Exception:
    GENERATOR_TIMEOUT = 900

try:
    WECHAT_DRAFT_TIMEOUT = int(str(os.getenv("WECHAT_INGEST_WECHAT_DRAFT_TIMEOUT", "180")).strip())
except Exception:
    WECHAT_DRAFT_TIMEOUT = 180

try:
    GENERATOR_RETRY_DELAYS = [
        int(part.strip()) for part in str(os.getenv("WECHAT_INGEST_GENERATOR_RETRY_DELAYS", "15,45,90")).split(",") if part.strip()
    ]
except Exception:
    GENERATOR_RETRY_DELAYS = [15, 45, 90]

if not GENERATOR_RETRY_DELAYS:
    GENERATOR_RETRY_DELAYS = [15, 45, 90]

RETRYABLE_ERROR_MARKERS = [
    "HTTP 529",
    "overloaded_error",
    "HTTP 502",
    "HTTP 503",
    "Bad Gateway",
    "Service Unavailable",
    "timed out",
    "timeout",
]

try:
    TELEGRAM_SEND_TIMEOUT = int(str(os.getenv("WECHAT_INGEST_TELEGRAM_SEND_TIMEOUT", "90")).strip())
except Exception:
    TELEGRAM_SEND_TIMEOUT = 90

try:
    TELEGRAM_SEND_RETRIES = int(str(os.getenv("WECHAT_INGEST_TELEGRAM_SEND_RETRIES", "2")).strip())
except Exception:
    TELEGRAM_SEND_RETRIES = 2

try:
    TELEGRAM_SEND_RETRY_DELAY = float(str(os.getenv("WECHAT_INGEST_TELEGRAM_SEND_RETRY_DELAY", "3")).strip())
except Exception:
    TELEGRAM_SEND_RETRY_DELAY = 3.0


def is_retryable_generator_error(text: str) -> bool:
    raw = (text or "").lower()
    return any(marker.lower() in raw for marker in RETRYABLE_ERROR_MARKERS)


def send_alert(text: str) -> bool:
    result = subprocess.run(
        [
            "openclaw", "message", "send",
            "--channel", "telegram",
            "--target", TARGET_CHAT,
            "--message", text,
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result.returncode == 0


def run_generator(report_type: str) -> dict:
    last_error = None
    python_cmd = str(PYTHON_BIN) if PYTHON_BIN.exists() else "python3"
    for attempt in range(1, len(GENERATOR_RETRY_DELAYS) + 2):
        try:
            result = subprocess.run(
                ["timeout", "--signal=TERM", str(GENERATOR_TIMEOUT), python_cmd, str(GENERATOR), report_type],
                capture_output=True,
                text=True,
                timeout=GENERATOR_TIMEOUT + 30,
                cwd=str(PROJECT_DIR),
            )
        except subprocess.TimeoutExpired as exc:
            err_text = f"generator timeout after {GENERATOR_TIMEOUT}s"
            last_error = err_text
            if attempt <= len(GENERATOR_RETRY_DELAYS):
                time.sleep(GENERATOR_RETRY_DELAYS[attempt - 1])
                continue
            raise RuntimeError(err_text) from exc

        if result.returncode != 0:
            if result.returncode == 124:
                err_text = f"generator timeout after {GENERATOR_TIMEOUT}s (killed by timeout wrapper)"
            else:
                err_text = (result.stderr or result.stdout or "generator failed").strip()
            last_error = err_text
            if is_retryable_generator_error(err_text) and attempt <= len(GENERATOR_RETRY_DELAYS):
                time.sleep(GENERATOR_RETRY_DELAYS[attempt - 1])
                continue
            raise RuntimeError(err_text)

        data = json.loads(result.stdout)
        if str(data.get("report_text", "")).startswith("ERROR:"):
            err_text = str(data["report_text"])
            last_error = err_text
            if is_retryable_generator_error(err_text) and attempt <= len(GENERATOR_RETRY_DELAYS):
                time.sleep(GENERATOR_RETRY_DELAYS[attempt - 1])
                continue
            raise RuntimeError(err_text)
        return data

    raise RuntimeError(last_error or "generator failed")


def split_report(text: str):
    major_blocks = []
    raw = text.split("\n## ")
    for i, block in enumerate(raw):
        piece = block if i == 0 else "## " + block
        if piece.strip():
            major_blocks.append(piece.strip())

    sections = []
    current = ""
    for piece in major_blocks:
        if len(piece) <= MAX_LEN:
            candidate = piece if not current else current + "\n\n" + piece
            if len(candidate) <= MAX_LEN:
                current = candidate
            else:
                if current:
                    sections.append(current.rstrip())
                current = piece
            continue

        subparts = piece.split("\n### ")
        for j, sub in enumerate(subparts):
            subpiece = sub if j == 0 else "### " + sub
            if len(subpiece) <= MAX_LEN:
                candidate = subpiece if not current else current + "\n\n" + subpiece
                if len(candidate) <= MAX_LEN:
                    current = candidate
                else:
                    if current:
                        sections.append(current.rstrip())
                    current = subpiece
            else:
                if current:
                    sections.append(current.rstrip())
                    current = ""
                lines = subpiece.splitlines()
                chunk = ""
                for line in lines:
                    add = ("\n" if chunk else "") + line
                    if len(chunk) + len(add) > MAX_LEN:
                        if chunk:
                            sections.append(chunk.rstrip())
                        chunk = line
                    else:
                        chunk += add
                if chunk:
                    sections.append(chunk.rstrip())
    if current:
        sections.append(current.rstrip())
    return [s for s in sections if s.strip()]


def split_message(text: str):
    lines = (text or "").splitlines()
    sections = []
    current = ""
    for line in lines:
        add = line if not current else "\n" + line
        if len(current) + len(add) <= MAX_LEN:
            current += add
            continue
        if current:
            sections.append(current.rstrip())
        current = line
    if current:
        sections.append(current.rstrip())
    return [s for s in sections if s.strip()]


def _clean(text: str, limit: int = 110) -> str:
    text = re.sub(r"\*\*(.*?)\*\*", r"\1", text or "")
    text = re.sub(r"【[^】]+】", "", text)
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"\s+", " ", text).strip(" ：:-")
    if len(text) > limit:
        return text[: limit - 1].rstrip() + "…"
    return text


def _extract_direction_lines(report_text: str, max_items: int = 4):
    items = []
    in_section = False
    for raw in (report_text or "").splitlines():
        line = raw.strip()
        if line.startswith("## ") and "方向归类" in line:
            in_section = True
            continue
        if in_section and line.startswith("## "):
            break
        line = re.sub(r"^-\s*", "", line)
        if not in_section or not line.startswith("**"):
            continue
        match = re.match(r"\*\*(.*?)\*\*[：:](.*)", line)
        if not match:
            continue
        title = _clean(match.group(1), 30)
        summary = _clean(match.group(2), 96)
        items.append(f"- {title}：{summary}")
        if len(items) >= max_items:
            break
    return items


def _extract_signal_lines(report_text: str, max_items: int = 5):
    items = []
    in_section = False
    pending = None
    for raw in (report_text or "").splitlines():
        line = raw.strip()
        if line.startswith("## ") and "内容详览" in line:
            in_section = True
            continue
        if in_section and line.startswith("## ") and "链接附录" in line:
            break
        if pending and in_section:
            if line.startswith("### ") or (line.startswith("**") and "【" in line):
                items.append(pending)
                pending = None
            elif line.startswith("- "):
                detail = _clean(line, 72)
                if detail and not detail.startswith("信号源"):
                    items.append(f"- {pending}：{detail}")
                    pending = None
                    if len(items) >= max_items:
                        break
                    continue
        line = re.sub(r"^-\s*", "", line)
        if not in_section or "【" not in line or not line.startswith("**"):
            continue
        title_match = re.match(r"\*\*(.*?)\*\*", line)
        weight_match = re.search(r"【权重\s*([0-9.]+)·(\d+)源(?:·[^】]+)?】", line)
        kind_match = re.search(r"【(多源共识|单点信号)(?:·[^】]+)?】", line)
        if not title_match or (not weight_match and not kind_match):
            continue
        desc = line.split("】", 1)[1] if "】" in line else ""
        title = _clean(title_match.group(1), 28)
        desc = _clean(desc, 72)
        if weight_match:
            prefix = f"权重{weight_match.group(1)}｜{weight_match.group(2)}源｜{title}"
            if desc:
                items.append(f"- {prefix}：{desc}")
            else:
                pending = prefix
        else:
            prefix = f"{kind_match.group(1)}｜{title}"
            if desc:
                items.append(f"- {prefix}：{desc}")
            else:
                pending = prefix
        if len(items) >= max_items:
            break
    if pending and len(items) < max_items:
        items.append(f"- {pending}")
    return items


def build_brief(data: dict, report_id: int) -> str:
    report_type = data["report_type"]
    label = "早报" if report_type == "morning" else "晚报"
    date_str = datetime.now().strftime("%Y-%m-%d")
    web_url = f"{WEB_BASE_URL}/reports/{report_id}"
    direction_lines = _extract_direction_lines(data.get("report_text", ""), max_items=4)
    signal_lines = _extract_signal_lines(data.get("report_text", ""), max_items=5)

    parts = [f"🧭 AI资讯{label}简报 | {date_str}"]
    if direction_lines:
        parts.append("【重点方向】\n" + "\n".join(direction_lines))
    if signal_lines:
        parts.append("【重点信号】\n" + "\n".join(signal_lines))
    if not direction_lines and not signal_lines:
        stats = data.get("stats") or {}
        if int(stats.get("after_semantic_week") or stats.get("raw_count") or 0) <= 0:
            parts.append("【窗口情况】\n- 本时间窗暂无新增入库文章，本次仅保留空窗说明页。")
        else:
            parts.append("【窗口情况】\n- 本次未提炼出可展示的正文要点，请直接查看完整版排查。")
    parts.append(f"完整版：{web_url}")
    return "\n\n".join(parts)


def has_reportable_content(data: dict) -> bool:
    stats = data.get("stats") or {}
    return int(stats.get("after_semantic_week") or stats.get("raw_count") or 0) > 0


def build_empty_window_notice(data: dict) -> str:
    report_type = data["report_type"]
    label = "早报" if report_type == "morning" else "晚报"
    date_str = datetime.now().strftime("%Y-%m-%d")
    return "\n\n".join([
        f"🧭 AI资讯{label}空窗提醒 | {date_str}",
        "【窗口情况】\n"
        f"- 本次统计窗口：{data['window_start']} ~ {data['window_end']}\n"
        "- 该时间窗内暂无新增入库文章，因此本次不生成正式日报网页，也不创建公众号草稿。\n"
        "- 我已正常推进时间窗状态，下一次将从新的窗口继续统计。",
    ])


def send_telegram(text: str) -> tuple[bool, str]:
    last_detail = ""
    for attempt in range(1, TELEGRAM_SEND_RETRIES + 2):
        try:
            result = subprocess.run(
                [
                    "openclaw", "message", "send",
                    "--channel", "telegram",
                    "--target", TARGET_CHAT,
                    "--message", text,
                ],
                capture_output=True,
                text=True,
                timeout=TELEGRAM_SEND_TIMEOUT,
            )
        except subprocess.TimeoutExpired as exc:
            stdout_text = (exc.stdout or "")[:600]
            stderr_text = (exc.stderr or "")[:600]
            detail_parts = [f"timeout after {TELEGRAM_SEND_TIMEOUT}s"]
            if stdout_text:
                detail_parts.append(f"stdout: {stdout_text}")
            if stderr_text:
                detail_parts.append(f"stderr: {stderr_text}")
            last_detail = " | ".join(detail_parts)
        else:
            stdout_text = (result.stdout or "").strip()
            stderr_text = (result.stderr or "").strip()
            if result.returncode == 0:
                return True, stdout_text[:1000]
            detail_parts = [f"exit code {result.returncode}"]
            if stdout_text:
                detail_parts.append(f"stdout: {stdout_text[:600]}")
            if stderr_text:
                detail_parts.append(f"stderr: {stderr_text[:600]}")
            last_detail = " | ".join(detail_parts)

        if attempt <= TELEGRAM_SEND_RETRIES:
            time.sleep(TELEGRAM_SEND_RETRY_DELAY)

    return False, last_detail or "unknown telegram send failure"


def commit_state(report_type: str, window_end: str):
    state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    if report_type == "morning":
        state["last_morning_report_at"] = window_end
    else:
        state["last_evening_report_at"] = window_end
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def save_report_record(data: dict):
    conn = sqlite3.connect(DB_PATH, timeout=30)
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS reports (id INTEGER PRIMARY KEY AUTOINCREMENT, report_type TEXT NOT NULL, window_start TEXT, window_end TEXT, stats_json TEXT, report_md TEXT NOT NULL, created_at TEXT NOT NULL)"
        )
        now_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        existing = conn.execute(
            "SELECT id FROM reports WHERE report_type=? AND window_start=? ORDER BY id ASC LIMIT 1",
            (data["report_type"], data["window_start"]),
        ).fetchone()
        if existing:
            report_id = int(existing[0])
            conn.execute(
                "UPDATE reports SET window_end=?, stats_json=?, report_md=?, created_at=? WHERE id=?",
                (
                    data["window_end"],
                    json.dumps(data.get("stats") or {}, ensure_ascii=False),
                    data["report_text"],
                    now_text,
                    report_id,
                ),
            )
            conn.commit()
            return report_id, True

        cursor = conn.execute(
            "INSERT INTO reports(report_type, window_start, window_end, stats_json, report_md, created_at) VALUES(?,?,?,?,?,?)",
            (
                data["report_type"],
                data["window_start"],
                data["window_end"],
                json.dumps(data.get("stats") or {}, ensure_ascii=False),
                data["report_text"],
                now_text,
            ),
        )
        conn.commit()
        return cursor.lastrowid, False
    finally:
        conn.close()


def create_wechat_draft(report_id: int):
    if not AUTO_CREATE_WECHAT_DRAFT:
        return None
    if not WECHAT_DRAFT_PUBLISHER.exists():
        raise RuntimeError(f"未找到公众号草稿脚本：{WECHAT_DRAFT_PUBLISHER}")

    cmd = [
        "python3",
        str(WECHAT_DRAFT_PUBLISHER),
        str(report_id),
        "--multi",
        "--create-draft",
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=WECHAT_DRAFT_TIMEOUT,
            cwd=str(PROJECT_DIR),
        )
    except subprocess.TimeoutExpired as exc:
        stdout_text = (exc.stdout or "")[:800]
        stderr_text = (exc.stderr or "")[:800]
        detail_parts = [f"公众号草稿创建超时，超过 {WECHAT_DRAFT_TIMEOUT}s"]
        if stdout_text:
            detail_parts.append(f"stdout: {stdout_text}")
        if stderr_text:
            detail_parts.append(f"stderr: {stderr_text}")
        raise RuntimeError(" | ".join(detail_parts)) from exc

    if result.returncode != 0:
        err_text = (result.stderr or result.stdout or "公众号草稿创建失败").strip()
        raise RuntimeError(err_text[:1600])
    try:
        return json.loads(result.stdout)
    except Exception as exc:
        raise RuntimeError(f"公众号草稿返回结果无法解析：{exc}\n{result.stdout[:800]}") from exc


def alert_report_failure(report_type: str, stage: str, error_text: str):
    label = "早报" if report_type == "morning" else "晚报"
    short_error = re.sub(r"\s+", " ", (error_text or "")).strip()
    if len(short_error) > 500:
        short_error = short_error[:497] + "..."
    msg = (
        f"⚠️ AI资讯{label}生成失败\n"
        f"阶段：{stage}\n"
        f"时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"原因：{short_error}\n"
        "本次未发送 Telegram 简报，也未提交 state。"
    )
    send_alert(msg)


def alert_fallback_used(report_type: str, data: dict):
    label = "早报" if report_type == "morning" else "晚报"
    model_used = data.get("model_used") or "unknown"
    reason = re.sub(r"\s+", " ", str(data.get("fallback_reason") or "")).strip()
    if len(reason) > 280:
        reason = reason[:277] + "..."
    msg = (
        f"ℹ️ AI资讯{label}本次使用了备用模型生成\n"
        f"实际生成模型：{model_used}\n"
        f"触发原因：{reason or 'MiniMax 超时或暂时不可用'}"
    )
    send_alert(msg)


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ("morning", "evening"):
        print("用法: python3 send_report.py [morning|evening]")
        sys.exit(1)

    report_type = sys.argv[1]
    label = "早报" if report_type == "morning" else "晚报"

    try:
        data = run_generator(report_type)
    except Exception as exc:
        alert_report_failure(report_type, "生成日报", str(exc))
        raise

    if not has_reportable_content(data):
        empty_text = build_empty_window_notice(data)
        sections = split_message(empty_text)
        sent_all = True
        send_errors = []
        for idx, section in enumerate(sections, start=1):
            ok, detail = send_telegram(section)
            sent_all = sent_all and ok
            if not ok:
                send_errors.append(f"第{idx}段失败: {detail}")
            time.sleep(0.6)

        if not sent_all:
            detail = " ; ".join(send_errors)[:1500] if send_errors else "部分消息发送失败"
            alert_report_failure(report_type, "发送空窗提醒", detail)
            raise RuntimeError(f"空窗提醒发送失败，未提交 state: {detail}")

        commit_state(report_type, data["window_end"])
        if data.get("fallback_used"):
            alert_fallback_used(report_type, data)
        print(f"[{label}] 本时间窗无新增内容，已发送空窗提醒，未生成 report 记录，window_end={data['window_end']}")
        return

    report_id, reused_existing_report = save_report_record(data)
    brief_text = build_brief(data, report_id)
    sections = split_message(brief_text)

    sent_all = True
    send_errors = []
    for idx, section in enumerate(sections, start=1):
        ok, detail = send_telegram(section)
        sent_all = sent_all and ok
        if not ok:
            send_errors.append(f"第{idx}段失败: {detail}")
        time.sleep(0.6)

    if not sent_all:
        detail = " ; ".join(send_errors)[:1500] if send_errors else "部分消息发送失败"
        alert_report_failure(report_type, "发送 Telegram", detail)
        raise RuntimeError(f"部分消息发送失败，未提交 state: {detail}")

    commit_state(report_type, data["window_end"])
    if data.get("fallback_used"):
        alert_fallback_used(report_type, data)

    wechat_draft_result = None
    try:
        wechat_draft_result = create_wechat_draft(report_id)
    except Exception as exc:
        err_text = f"[{label}] Telegram 与网页已成功，但公众号草稿创建失败：{exc}"
        print(err_text, file=sys.stderr)
        send_alert(err_text)

    record_note = "（复用同窗口已有 report 记录）" if reused_existing_report else ""
    if wechat_draft_result and wechat_draft_result.get("draft_media_id"):
        print(
            f"[{label}] 发送成功，report_id={report_id}{record_note}，draft_media_id={wechat_draft_result.get('draft_media_id')}，window_end={data['window_end']}"
        )
    else:
        print(f"[{label}] 发送成功，report_id={report_id}{record_note}，window_end={data['window_end']}")


if __name__ == "__main__":
    main()
