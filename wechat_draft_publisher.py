#!/usr/bin/env python3
"""生成公众号版文章并推送到微信公众号草稿箱。

默认行为：本地生成文章与 draft payload 预览，不调用微信写接口。
如需真正写入公众号草稿箱，显式加：--create-draft
"""

import argparse
import hashlib
import html
import json
import math
import os
import re
import sqlite3
import struct
import sys
import uuid
import zlib
from pathlib import Path
from urllib.parse import urlencode, urlparse, parse_qs
from urllib.request import Request, urlopen

from report_generator import call_minimax
from wechat_article_generator import (
    SYSTEM_PROMPT as WECHAT_SYSTEM_PROMPT,
    build_user_message as build_wechat_user_message,
    load_report as load_report_record,
)

PROJECT_DIR = Path("/root/.openclaw/workspace/wechat_ingest_system")
CONFIG_PATH = PROJECT_DIR / "wechat_official_account.json"
OUTPUT_DIR = PROJECT_DIR / "wechat_articles"
ASSET_DIR = PROJECT_DIR / "wechat_assets"
ARTICLE_DB_PATH = PROJECT_DIR / "server/data/app.db"
WEB_BASE_URL = os.getenv("WECHAT_INGEST_WEB_BASE_URL", "http://43.167.177.15:8000").rstrip("/")
WECHAT_API_BASE = "https://api.weixin.qq.com/cgi-bin"
SERVER_DIR = PROJECT_DIR / "server"
SOURCE_IMAGE_CACHE_DIR = ASSET_DIR / "source_image_cache"
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))
from app.services.image_selector import select_article_images

MULTI_ARTICLE_SYSTEM_PROMPT = """你是一名资深公众号主编，现在需要把一份内部 AI 日报拆成一组适合微信公众号“多图文消息”发布的稿件。

目标：生成 1 篇主图文 + 0~4 篇子图文。

要求：
1. 主图文负责“全局总览”，突出今天最重要的 4-6 个核心判断。
2. 子图文每篇聚焦一个强方向，不要硬凑弱方向。
3. 子图文优先从这些方向中选：AI / 大模型、国际局势 / 地缘政治、网络安全、中国军事 / 外交、科技产业 / 商业、具身智能 / 机器人。
4. 如果当天只有 0、1、2、3 个方向足够强，就只输出对应数量的子图文，不要硬凑到 4 篇。
5. 标题必须像公众号标题，不要像系统标题，不要太长。
6. 主图文和子图文不要大段重复；主图文讲全局判断，子图文讲对应方向的展开。
7. 每篇内容都用 Markdown，适合手机端连续阅读。
8. 如果 AI / 大模型主题材料里出现 GitHub / 开源项目，必须保留一个 `### 开源项目 / GitHub` 小节，并逐字输出完整 URL；严禁省略成 `github.com/...`。
9. 不要输出代码块，不要输出解释，只输出 JSON。

严格输出 JSON，格式：
{
  "main": {
    "title": "主图文标题",
    "markdown": "主图文Markdown正文"
  },
  "subs": [
    {
      "title": "子图文标题1",
      "topic": "AI / 大模型",
      "markdown": "子图文Markdown正文"
    }
  ]
}
"""

JSON_REPAIR_PROMPT = """你是一个 JSON 修复器。

你的任务：
1. 接收一段“本来想输出 JSON，但格式坏了”的文本
2. 在不改动字段语义的前提下，修复成**合法 JSON**
3. 不要输出任何解释、不要输出代码块，只输出修复后的 JSON 本体

目标 JSON 结构：
{
  "main": {"title": "...", "markdown": "..."},
  "subs": [{"title": "...", "topic": "...", "markdown": "..."}]
}
"""

TOPIC_PRIORITY = [
    "AI / 大模型",
    "国际局势 / 地缘政治",
    "网络安全",
    "中国军事 / 外交",
    "科技产业 / 商业",
    "具身智能 / 机器人",
]

TOPIC_DISPLAY = {
    "AI / 大模型": "AI大模型",
    "国际局势 / 地缘政治": "国际局势",
    "网络安全": "网络安全",
    "中国军事 / 外交": "中国军事外交",
    "科技产业 / 商业": "科技商业",
    "具身智能 / 机器人": "具身智能",
}

TOPIC_KEYWORDS = {
    "AI / 大模型": ["ai", "大模型", "模型", "agent", "anthropic", "claude", "cursor", "kimi", "kvcache", "openclaw", "审稿"],
    "国际局势 / 地缘政治": ["国际", "地缘", "伊朗", "以色列", "白宫", "美军", "冲突", "巴基斯坦", "运输机", "国家风险", "外交"],
    "网络安全": ["安全", "漏洞", "rce", "攻击", "chrome", "exploit", "武器化", "评测", "治理", "渗透", "防御"],
    "科技产业 / 商业": ["融资", "估值", "裁员", "投资", "商业", "产业", "游戏", "coding", "meta", "xai", "市场", "芯片"],
    "具身智能 / 机器人": ["具身", "机器人", "半马", "abot", "claw", "harness", "人形", "physical agi"],
}


def load_official_account_config():
    env_appid = (os.getenv("WECHAT_OFFICIAL_APPID") or "").strip()
    env_secret = (os.getenv("WECHAT_OFFICIAL_APPSECRET") or "").strip()
    env_name = (os.getenv("WECHAT_OFFICIAL_ACCOUNT_NAME") or "").strip()
    if env_appid and env_secret:
        return {
            "account_name": env_name or "微信公众号",
            "appid": env_appid,
            "appsecret": env_secret,
        }
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    raise SystemExit("缺少公众号配置，请设置环境变量或创建 wechat_official_account.json")


def http_json(url: str, method: str = "GET", payload=None, headers=None):
    data = None
    req_headers = {"User-Agent": "Axiom-WeChatDraftPublisher/1.0"}
    if headers:
        req_headers.update(headers)
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req_headers.setdefault("Content-Type", "application/json")
    req = Request(url, data=data, headers=req_headers, method=method)
    with urlopen(req, timeout=60) as resp:
        text = resp.read().decode("utf-8")
    return json.loads(text)


def extract_json_object(text: str):
    s = (text or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?", "", s).strip()
        s = re.sub(r"```$", "", s).strip()
    try:
        return json.loads(s)
    except Exception:
        pass
    m = re.search(r"\{[\s\S]*\}", s)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


def ensure_multi_article_json(raw: str):
    parsed = extract_json_object(raw)
    if isinstance(parsed, dict) and isinstance(parsed.get("main"), dict):
        return parsed
    repaired = call_minimax(JSON_REPAIR_PROMPT, raw)
    parsed = extract_json_object(repaired)
    if isinstance(parsed, dict) and isinstance(parsed.get("main"), dict):
        return parsed
    return None


def normalize_topic_name(text: str):
    s = (text or "").strip()
    s = re.sub(r'^[一二三四五六七八九十\d]+[、\.．]\s*', '', s)
    s = re.sub(r'^\*+|\*+$', '', s).strip()
    for topic in TOPIC_PRIORITY:
        if topic in s:
            return topic
    mapping = {
        "AI大模型": "AI / 大模型",
        "国际局势": "国际局势 / 地缘政治",
        "地缘政治": "国际局势 / 地缘政治",
        "网络安全": "网络安全",
        "中国军事外交": "中国军事 / 外交",
        "中国军事 / 外交": "中国军事 / 外交",
        "科技产业": "科技产业 / 商业",
        "科技商业": "科技产业 / 商业",
        "具身智能": "具身智能 / 机器人",
        "机器人": "具身智能 / 机器人",
    }
    return mapping.get(s, s)


def extract_report_sections(report_md: str):
    sections = {}
    in_section = False
    current = None
    buf = []
    for raw in (report_md or "").splitlines():
        line = raw.rstrip()
        s = line.strip()
        if s.startswith("## ") and "内容详览" in s:
            in_section = True
            continue
        if not in_section:
            continue
        if s.startswith("## ") and "链接附录" in s:
            break
        if s.startswith("### "):
            if current and buf:
                sections[current] = "\n".join(buf).strip()
            current = normalize_topic_name(s[4:])
            buf = []
            continue
        if current:
            buf.append(line)
    if current and buf:
        sections[current] = "\n".join(buf).strip()
    return sections


def extract_appendix_groups(report_md: str):
    groups = {}
    in_appendix = False
    current = None
    for raw in (report_md or "").splitlines():
        s = raw.strip()
        if s.startswith("## ") and "链接附录" in s:
            in_appendix = True
            continue
        if not in_appendix:
            continue
        if s.startswith("### "):
            current = normalize_topic_name(s[4:])
            groups.setdefault(current, [])
            continue
        if current and s.startswith("- "):
            groups.setdefault(current, []).append(s[2:].strip())
    return groups


def _collect_github_refs_from_lines(lines, fallback_label: str = "开源项目"):
    refs = []
    seen = set()
    for raw in lines:
        line = (raw or "").strip()
        if not line:
            continue
        urls = re.findall(r"https?://github\.com/[A-Za-z0-9._~:/?#\\\[\\\]@!$&'()*+,;=%\\-]+", line, flags=re.I)
        for url in urls:
            fixed_url = url.replace('\\-', '-').replace('\\_', '_').rstrip('，。；;：:!！?？）》)]*')
            fixed_url = fixed_url.rstrip('*')
            if '/...' in fixed_url or fixed_url.endswith('/..') or fixed_url.endswith('/.'):
                continue
            if fixed_url.count('/') < 4:
                continue
            if fixed_url in seen:
                continue
            seen.add(fixed_url)
            label = re.sub(r'https?://github\.com/[^\s)\]"<>]+', '', line, flags=re.I)
            label = re.sub(r'^[-*\d\.、\s]+', '', label).strip(' ：:|，,')
            refs.append({"url": fixed_url, "label": label or fallback_label})
    return refs


def extract_github_refs(report_md: str, topic: str = "AI / 大模型"):
    sections = extract_report_sections(report_md)
    appendix_groups = extract_appendix_groups(report_md)
    candidates = []
    body = sections.get(topic) or ""
    if body:
        candidates.extend(body.splitlines())
    candidates.extend(appendix_groups.get(topic, []))
    return _collect_github_refs_from_lines(candidates, fallback_label="开源项目")


def extract_github_refs_from_window(report: dict):
    window_start = report.get("window_start")
    window_end = report.get("window_end")
    if not window_start or not window_end:
        return []
    conn = sqlite3.connect(str(ARTICLE_DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT title, content_md, content_html
            FROM articles
            WHERE discovered_at > ? AND discovered_at <= ?
              AND (
                COALESCE(content_md, '') LIKE '%github.com/%'
                OR COALESCE(content_html, '') LIKE '%github.com/%'
              )
            ORDER BY id DESC
            LIMIT 120
            """,
            (window_start, window_end),
        ).fetchall()
    finally:
        conn.close()

    candidates = []
    for row in rows:
        title = (row["title"] or "").strip() or "开源项目"
        for blob in (row["content_md"] or "", row["content_html"] or ""):
            urls = re.findall(r"https?://github\.com/[A-Za-z0-9._~:/?#\\\[\\\]@!$&'()*+,;=%\\-]+", blob, flags=re.I)
            for url in urls:
                candidates.append(f"{title} | {url}")
    return _collect_github_refs_from_lines(candidates, fallback_label="开源项目")


def merge_github_refs(*groups):
    merged = []
    seen = set()
    for group in groups:
        for item in group or []:
            url = item.get("url")
            if not url or url in seen:
                continue
            seen.add(url)
            merged.append(item)
    return merged


def ensure_github_section(markdown: str, refs, topic: str):
    if not refs:
        return markdown
    topic_name = normalize_topic_name(topic or "")
    if topic_name != "AI / 大模型":
        return markdown
    if 'github.com/' in (markdown or '').lower():
        return markdown
    lines = [markdown.rstrip(), "", "### 开源项目 / GitHub"]
    for item in refs[:3]:
        lines.append(f"- {item['label']}：{item['url']}")
    return "\n".join(lines).strip() + "\n"


def topic_strength(topic: str, section_text: str, appendix_items=None):
    appendix_items = appendix_items or []
    text = section_text or ""
    content_len = len(re.sub(r'\s+', '', text))
    article_count = len(re.findall(r'^(?:-\s+)?\*\*.*?\*\*', text, flags=re.M))
    weighted_tags = re.findall(r'【权重\s*([0-9.]+)·(\d+)源(?:·[^】]+)?】', text)
    multi_source_count = text.count('【多源共识】') + sum(1 for _, count in weighted_tags if int(count) >= 2)
    single_signal_count = text.count('【单点信号】') + sum(1 for _, count in weighted_tags if int(count) == 1)
    appendix_count = len(appendix_items)
    headline_count = len(extract_topic_headlines(text, limit=6))

    score = 0.0
    score += min(article_count, 4) * 2.2
    score += min(appendix_count, 4) * 1.4
    score += min(multi_source_count, 3) * 2.6
    score += min(single_signal_count, 3) * 0.7
    score += min(headline_count, 4) * 0.9
    score += min(content_len / 180.0, 4.0)

    qualified = True
    reasons = []
    if content_len < 80:
        qualified = False
        reasons.append('内容长度不足')
    if article_count < 1 and appendix_count < 1:
        qualified = False
        reasons.append('缺少事件与附录支撑')
    if score < 5.6:
        qualified = False
        reasons.append('综合强度不足')

    return {
        'topic': topic,
        'score': round(score, 2),
        'qualified': qualified,
        'content_len': content_len,
        'article_count': article_count,
        'appendix_count': appendix_count,
        'multi_source_count': multi_source_count,
        'single_signal_count': single_signal_count,
        'headline_count': headline_count,
        'reasons': reasons,
    }


def choose_sub_topics(report_md: str, max_sub: int = 4):
    sections = extract_report_sections(report_md)
    appendix_groups = extract_appendix_groups(report_md)
    chosen = []
    scores = {}
    for topic in TOPIC_PRIORITY:
        body = (sections.get(topic) or "").strip()
        if not body:
            continue
        metrics = topic_strength(topic, body, appendix_groups.get(topic, []))
        scores[topic] = metrics
        if not metrics['qualified']:
            continue
        chosen.append(topic)
        if len(chosen) >= max_sub:
            break
    return chosen, sections, scores


def choose_multi_topics(report_md: str, max_sub: int = 4):
    chosen, sections, scores = choose_sub_topics(report_md, max_sub=max_sub + 1)
    if not chosen:
        return None, [], sections, scores
    main_topic = "AI / 大模型" if scores.get("AI / 大模型", {}).get('qualified') else chosen[0]
    sub_topics = [t for t in chosen if t != main_topic][:max_sub]
    return main_topic, sub_topics, sections, scores


def extract_topic_headlines(section_text: str, limit: int = 2):
    hits = []
    for raw in (section_text or "").splitlines():
        line = raw.strip()
        m = re.match(r"(?:-\s+)?\*\*(.*?)\*\*", line)
        if not m:
            continue
        title = m.group(1).strip()
        title = re.sub(r"【[^】]+】", "", title).strip()
        title = re.sub(r"\s+", "", title)
        title = title.strip('：:，,。.!！?？—- ')
        if len(title) < 3:
            continue
        if title not in hits:
            hits.append(title)
        if len(hits) >= limit:
            break
    return hits


def count_core_points(article_md: str):
    lines = (article_md or "").splitlines()
    in_section = False
    count = 0
    for raw in lines:
        s = raw.strip()
        if s.startswith("## ") and "今日核心判断" in s:
            in_section = True
            continue
        if in_section and s.startswith("## "):
            break
        if in_section and s.startswith("- "):
            count += 1
    return count or 5


def clean_title(title: str, max_len: int = 28):
    s = re.sub(r'\s+', '', (title or "").strip())
    s = re.sub(r'^(标题[:：]|主标题[:：])', '', s)
    s = s.replace('“', '').replace('”', '').replace('"', '').replace("'", '')
    s = s.strip('“”"\'：:，,。.!！?？—- ')
    s = s.replace("今天你需要知道的", "").replace("今日必读的", "").replace("今天发生了什么", "今日要点")
    if len(s) <= max_len:
        return s
    for sep in ["——", "：", ":", "，", ",", "、"]:
        head = s.split(sep)[0].strip('“”"\'：:，,。.!！?？—- ')
        if 6 <= len(head) <= max_len:
            return head
    return s[:max_len].rstrip('“”"\'：:，,。.!！?？—- ')


def apply_main_title_style(title: str, chosen_topics, max_len: int = 26):
    s = clean_title(title, max_len=max_len)
    if s and len(s) >= 10 and not re.fullmatch(r'今日\d+[大个]?[核心关键]*判断', s):
        return s
    displays = [TOPIC_DISPLAY.get(t, t) for t in (chosen_topics or [])[:2]]
    if len(displays) >= 2:
        s = f"{displays[0]}与{displays[1]}：今日重点判断"
    elif len(displays) == 1:
        s = f"{displays[0]}：今日重点判断"
    else:
        s = "今日重点判断"
    return clean_title(s, max_len=max_len)


def apply_sub_title_style(topic: str, title: str, max_len: int = 32):
    display = TOPIC_DISPLAY.get(topic, topic)
    body = clean_title(title, max_len=max_len)
    for prefix in [display, topic, display + '｜', topic + '｜']:
        if body.startswith(prefix):
            body = body[len(prefix):].lstrip('｜:：-— ')
            break
    reserve = len(display) + 1
    body_limit = max(8, max_len - reserve)
    body = clean_title(body, max_len=body_limit)
    return f"{display}｜{body}"[:max_len]


def extract_core_bullets(article_md: str, limit: int = 3):
    bullets = []
    in_section = False
    for raw in (article_md or "").splitlines():
        s = raw.strip()
        if s.startswith("## ") and "今日核心判断" in s:
            in_section = True
            continue
        if in_section and s.startswith("## "):
            break
        if not in_section or not s.startswith("- "):
            continue
        item = s[2:].strip()
        item = re.sub(r"【[^】]+】", "", item).strip()
        item = clean_title(item, max_len=22)
        if len(item) < 4 or item in bullets:
            continue
        bullets.append(item)
        if len(bullets) >= limit:
            break
    return bullets


def build_main_title(main_topic: str, article_md: str, section_text: str = "", model_title: str = ""):
    display = TOPIC_DISPLAY.get(main_topic, main_topic or "AI")
    candidates = []
    candidates.extend(extract_topic_headlines(section_text, limit=3))
    candidates.extend(extract_core_bullets(article_md, limit=3))
    if model_title:
        candidates.append(clean_title(model_title, max_len=24))

    seen = set()
    for candidate in candidates:
        body = clean_title(candidate, max_len=20)
        if len(body) < 4:
            continue
        key = body.lower()
        if key in seen:
            continue
        seen.add(key)
        return clean_title(f"{display}｜{body}", max_len=32)

    count = count_core_points(article_md)
    base = f"AI日报｜{display}今日{count}个判断"
    return clean_title(base, max_len=32)


def build_sub_title(topic: str, section_text: str, model_title: str = ""):
    display = TOPIC_DISPLAY.get(topic, topic)
    heads = extract_topic_headlines(section_text, limit=1)
    if heads:
        return apply_sub_title_style(topic, heads[0], max_len=32)
    generic = apply_sub_title_style(topic, model_title or f"{display}重点变化", max_len=32)
    if generic.endswith("今天最值得关注的变化") or generic.endswith("今日要点"):
        return apply_sub_title_style(topic, f"{display}重点变化", max_len=32)
    return generic


def fallback_sub_article(topic: str, section_text: str, report_url: str):
    display = TOPIC_DISPLAY.get(topic, topic)
    lines = [l.strip() for l in (section_text or "").splitlines() if l.strip()]
    paras = []
    current = []
    for line in lines:
        if re.match(r"(?:-\s+)?\*\*", line) and current:
            paras.append(" ".join(current))
            current = []
        current.append(re.sub(r"\*\*(.*?)\*\*", r"\1", line))
    if current:
        paras.append(" ".join(current))
    paras = [p for p in paras if len(re.sub(r'\s+', '', p)) >= 10][:3]
    title = f"{display}｜今天最值得关注的变化"
    body = [f"# {title}", ""]
    if paras:
        body.extend(paras)
        body.append("")
    body.extend(["## 延伸阅读", f"完整版日报：<{report_url}>"])
    return {"title": title, "topic": topic, "markdown": "\n".join(body)}


def fallback_main_article(main_topic: str, section_text: str, report_url: str, github_refs=None):
    display = TOPIC_DISPLAY.get(main_topic, main_topic or "AI大模型")
    lines = [l.strip() for l in (section_text or "").splitlines() if l.strip()]
    entries = []
    current = None
    for line in lines:
        clean = re.sub(r"\*\*(.*?)\*\*", r"\1", line)
        if clean.startswith("开源项目 / GitHub"):
            break
        if line.startswith("- **"):
            if current:
                entries.append(current)
            current = [clean.lstrip("- ").strip()]
        elif current:
            current.append(clean)
    if current:
        entries.append(current)

    paras = []
    bullet_points = []
    for block in entries[:5]:
        merged = re.sub(r"\s+", " ", " ".join(block)).strip()
        if not merged:
            continue
        title = merged.split("【", 1)[0].strip(" ：:-")
        desc = ""
        if "】" in merged:
            desc = merged.split("】", 1)[1].strip()
        elif "。" in merged:
            desc = merged.split("。", 1)[0].strip()
        if title:
            bullet_points.append(f"- {title}")
        if desc:
            paras.append(desc)

    title = build_main_title(main_topic, "")
    body = [f"# {title}", ""]
    body.append(f"今天这组内容的主轴仍然集中在{display}。从当天入选信号看，重点不是单一新闻，而是模型能力、工具链落地和开源生态同时推进。")
    body.append("")
    body.append("## 今日核心判断")
    if bullet_points:
        body.extend(bullet_points[:5])
    else:
        body.extend([
            f"- {display} 方向仍是本轮窗口内最值得持续跟踪的主线",
            "- 值得重点看多源共识而不是单条标题噪音",
            "- 开源项目与工程落地信号在继续增强",
        ])
    body.append("")
    body.append("## 重点展开")
    if paras:
        for para in paras[:4]:
            body.append(para)
            body.append("")
    else:
        body.append(section_text[:1200].strip())
        body.append("")
    if main_topic == "AI / 大模型":
        body.append("### 开源项目 / GitHub")
        refs = github_refs or []
        if refs:
            for item in refs[:3]:
                body.append(f"- {item.get('label') or '项目'}：{item.get('url') or ''}")
        else:
            body.append("- 本时间窗未抽取到适合保留的开源项目链接")
        body.append("")
    body.extend(["## 延伸阅读", f"完整版日报：<{report_url}>"])
    markdown = "\n".join([part for part in body if part is not None]).strip() + "\n"
    return {"title": title, "topic": main_topic, "markdown": markdown}


def _match_terms_local(text: str):
    text = (text or "").lower()
    terms = set()
    for chunk in re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]+", text):
        if re.fullmatch(r"[a-z0-9]+", chunk):
            if len(chunk) >= 2:
                terms.add(chunk)
            continue
        if len(chunk) <= 4:
            terms.add(chunk)
        for n in (2, 3, 4):
            if len(chunk) >= n:
                for i in range(len(chunk) - n + 1):
                    terms.add(chunk[i:i+n])
    return terms


def load_window_image_articles(report: dict):
    window_start = report.get("window_start")
    window_end = report.get("window_end")
    if not window_start or not window_end:
        return []
    conn = sqlite3.connect(str(ARTICLE_DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT a.id, a.title, a.article_url, a.content_md, a.image_urls_json, tg.account_name
            FROM articles a
            LEFT JOIN targets tg ON a.target_id = tg.id
            WHERE a.discovered_at > ? AND a.discovered_at <= ?
              AND COALESCE(a.image_urls_json, '') NOT IN ('', '[]')
            ORDER BY a.id DESC
            LIMIT 240
            """,
            (window_start, window_end),
        ).fetchall()
    finally:
        conn.close()

    articles = []
    for row in rows:
        try:
            image_urls = json.loads(row["image_urls_json"] or "[]")
        except Exception:
            image_urls = []
        if not image_urls:
            continue
        article = {
            "id": row["id"],
            "title": row["title"] or "",
            "article_url": row["article_url"] or "",
            "account_name": row["account_name"] or "",
            "content_md": row["content_md"] or "",
            "image_urls": image_urls,
        }
        article["image_selection"] = select_article_images(article, max_web=3, max_wechat=5)
        articles.append(article)
    return articles


def score_topic_article_for_images(topic: str, section_text: str, article: dict, appendix_titles=None, appendix_urls=None):
    appendix_titles = appendix_titles or []
    appendix_urls = appendix_urls or []
    title = article.get("title") or ""
    article_url = article.get("article_url") or ""
    content_md = article.get("content_md") or ""
    topic_display = TOPIC_DISPLAY.get(topic, topic)
    heads = extract_topic_headlines(section_text, limit=4)
    topic_terms = _match_terms_local("\n".join([topic, topic_display, section_text[:1000], *heads]))
    article_terms = _match_terms_local("\n".join([title, content_md[:1200]]))
    overlap = topic_terms & article_terms

    score = 0.0
    if topic_display and topic_display in title:
        score += 8.0
    if topic and topic in title:
        score += 6.0
    for head in heads:
        if head and (head in title or title in head):
            score += 10.0
            break
    if title and title in (section_text or ""):
        score += 12.0
    if article_url and article_url in appendix_urls:
        score += 18.0
    for appendix_title in appendix_titles:
        if appendix_title and (appendix_title in title or title in appendix_title):
            score += 16.0
            break
    score += min(10.0, len(overlap) * 0.7)
    score += min(3.0, len(article.get("image_selection", {}).get("keep_for_wechat", [])) * 0.8)
    return round(score, 2)


def has_direct_topic_anchor(topic: str, section_text: str, article: dict, appendix_titles=None, appendix_urls=None):
    appendix_titles = appendix_titles or []
    appendix_urls = appendix_urls or []
    title = article.get("title") or ""
    article_url = article.get("article_url") or ""
    if article_url and article_url in appendix_urls:
        return True
    for appendix_title in appendix_titles:
        if appendix_title and (appendix_title in title or title in appendix_title):
            return True
    if title and title in (section_text or ""):
        return True
    for head in extract_topic_headlines(section_text, limit=6):
        if head and (head in title or title in head):
            return True
    return False


def has_topic_keyword_anchor(topic: str, article: dict):
    keywords = TOPIC_KEYWORDS.get(topic) or []
    title_text = (article.get("title") or "").lower()
    return any(k.lower() in title_text for k in keywords)


def choose_topic_inline_images(report: dict, topic: str, section_text: str, limit: int = 3):
    articles = load_window_image_articles(report)
    if not articles:
        return []

    active_topics = [t for t in TOPIC_PRIORITY if (extract_report_sections(report.get("report_md") or "").get(t) or "").strip()]
    sections = extract_report_sections(report.get("report_md") or "")

    appendix_groups = extract_appendix_groups(report.get("report_md") or "")
    appendix_titles = []
    appendix_urls = []
    for raw in appendix_groups.get(topic, []):
        parts = [p.strip() for p in (raw or "").split(" | ")]
        if len(parts) >= 3:
            appendix_titles.append(parts[0])
            appendix_urls.append(parts[2])

    image_freq = {}
    for article in articles:
        for item in article.get("image_urls", []):
            url = (item or {}).get("url") or ""
            if url:
                image_freq[url] = image_freq.get(url, 0) + 1

    candidates = []
    for article in articles:
        article_score = score_topic_article_for_images(topic, section_text, article, appendix_titles=appendix_titles, appendix_urls=appendix_urls)
        if article_score < 2.5:
            continue
        direct_anchor = has_direct_topic_anchor(topic, section_text, article, appendix_titles=appendix_titles, appendix_urls=appendix_urls)
        keyword_anchor = has_topic_keyword_anchor(topic, article)

        if not direct_anchor and not keyword_anchor:
            continue

        topic_score_map = {}
        for other_topic in active_topics:
            other_section = sections.get(other_topic, "") or ""
            topic_score_map[other_topic] = score_topic_article_for_images(other_topic, other_section, article)
        best_topic = max(topic_score_map.items(), key=lambda kv: kv[1])[0] if topic_score_map else topic
        best_score = topic_score_map.get(best_topic, article_score)

        if not direct_anchor and best_topic != topic and best_score >= article_score + 3.0:
            continue

        for item in article.get("image_selection", {}).get("keep_for_wechat", []):
            url = item.get("url_norm") or item.get("url") or ""
            if not url:
                continue
            repeat_penalty = max(0, image_freq.get(url, 1) - 1) * 1.6
            final_score = article_score + float(item.get("score") or 0) - repeat_penalty
            caption = item.get("alt_norm") or article.get("title") or topic
            candidates.append({
                "url": url,
                "caption": caption if caption not in {"图片", "img", "image"} else (article.get("title") or topic),
                "source_title": article.get("title") or "",
                "account_name": article.get("account_name") or "",
                "score": round(final_score, 2),
                "kind": item.get("kind") or "unknown",
                "reasons": item.get("keep_reasons") or [],
                "width_px": item.get("width_px") or 0,
            })

    candidates.sort(key=lambda x: (-x["score"], -(x.get("width_px") or 0), x.get("source_title") or ""))
    chosen = []
    seen = set()
    seen_titles = set()
    for item in candidates:
        if item["url"] in seen:
            continue
        title_key = (item.get("source_title") or "").strip()
        if title_key and title_key in seen_titles:
            continue
        seen.add(item["url"])
        if title_key:
            seen_titles.add(title_key)
        chosen.append(item)
        if len(chosen) >= limit:
            break
    return chosen


def infer_image_ext(url: str, content_type: str = ""):
    parsed = urlparse(url or "")
    query = parse_qs(parsed.query or "")
    wx_fmt = (query.get("wx_fmt") or [""])[0].lower().strip()
    if wx_fmt in {"png", "jpeg", "jpg", "gif", "webp"}:
        return ".jpg" if wx_fmt == "jpeg" else f".{wx_fmt}"
    suffix = Path(parsed.path or "").suffix.lower()
    if suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp"}:
        return ".jpg" if suffix == ".jpeg" else suffix
    content_type = (content_type or "").lower()
    if "png" in content_type:
        return ".png"
    if "gif" in content_type:
        return ".gif"
    if "webp" in content_type:
        return ".webp"
    return ".jpg"


def download_remote_image(url: str, prefix: str = "img"):
    SOURCE_IMAGE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256((url or "").encode("utf-8")).hexdigest()
    for ext in (".png", ".jpg", ".gif", ".webp", ".jpeg"):
        existing = SOURCE_IMAGE_CACHE_DIR / f"{prefix}_{key}{ext}"
        if existing.exists():
            return existing
    req = Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Referer": "https://mp.weixin.qq.com/",
            "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        },
    )
    with urlopen(req, timeout=20) as resp:
        data = resp.read()
        content_type = resp.headers.get("Content-Type") or "image/jpeg"
    ext = infer_image_ext(url, content_type)
    path = SOURCE_IMAGE_CACHE_DIR / f"{prefix}_{key}{ext}"
    path.write_bytes(data)
    return path


def upload_selected_images(access_token: str, items, prefix: str):
    uploaded = []
    for idx, item in enumerate(items or [], start=1):
        try:
            local_path = download_remote_image(item.get("url") or "", prefix=f"{prefix}_{idx}")
            wechat_url, upload_resp = upload_content_image(access_token, local_path)
            uploaded.append({
                **item,
                "wechat_url": wechat_url,
                "local_path": str(local_path),
                "upload_resp": upload_resp,
            })
        except Exception:
            continue
    return uploaded


def multipart_request(url: str, file_path: Path, field_name: str = "media"):
    boundary = f"----AxiomBoundary{uuid.uuid4().hex}"
    content = file_path.read_bytes()
    mime = "image/png" if file_path.suffix.lower() == ".png" else "image/jpeg"
    body = []
    body.append(f"--{boundary}\r\n".encode())
    body.append(
        (
            f'Content-Disposition: form-data; name="{field_name}"; filename="{file_path.name}"\r\n'
            f"Content-Type: {mime}\r\n\r\n"
        ).encode()
    )
    body.append(content)
    body.append(f"\r\n--{boundary}--\r\n".encode())
    req = Request(
        url,
        data=b"".join(body),
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "User-Agent": "Axiom-WeChatDraftPublisher/1.0",
        },
        method="POST",
    )
    with urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get_stable_access_token(appid: str, appsecret: str):
    url = f"{WECHAT_API_BASE}/stable_token"
    payload = {
        "grant_type": "client_credential",
        "appid": appid,
        "secret": appsecret,
        "force_refresh": False,
    }
    data = http_json(url, method="POST", payload=payload)
    if data.get("access_token"):
        return data["access_token"], data
    raise RuntimeError(f"获取 access_token 失败：{json.dumps(data, ensure_ascii=False)}")


def png_chunk(tag: bytes, data: bytes):
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)


def create_default_cover(path: Path, width: int = 900, height: int = 383):
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    for y in range(height):
        ratio = y / max(1, height - 1)
        r = int(20 + 25 * ratio)
        g = int(72 + 40 * ratio)
        b = int(170 + 45 * ratio)
        row = bytearray([0])
        for _ in range(width):
            row.extend((r, g, b))
        rows.append(bytes(row))
    raw = b"".join(rows)
    png = b"\x89PNG\r\n\x1a\n"
    png += png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    png += png_chunk(b"IDAT", zlib.compress(raw, 9))
    png += png_chunk(b"IEND", b"")
    path.write_bytes(png)
    return path


def create_body_illustration(path: Path, kind: str = "lead", width: int = 900, height: int = 520):
    path.parent.mkdir(parents=True, exist_ok=True)
    palettes = {
        "lead": ((37, 99, 235), (124, 58, 237), (224, 242, 254)),
        "event": ((15, 118, 110), (14, 165, 233), (236, 253, 245)),
        "signal": ((217, 119, 6), (239, 68, 68), (255, 247, 237)),
    }
    c1, c2, c3 = palettes.get(kind, palettes["lead"])
    rows = []
    for y in range(height):
        row = bytearray([0])
        yr = y / max(1, height - 1)
        for x in range(width):
            xr = x / max(1, width - 1)
            wave = (math.sin(x / 48.0) + math.cos(y / 36.0) + math.sin((x + y) / 72.0)) / 3.0
            glow = max(0.0, 1.0 - ((xr - 0.72) ** 2 + (yr - 0.28) ** 2) * 3.6)
            blend = 0.58 * xr + 0.42 * yr
            r = int(c1[0] * (1 - blend) + c2[0] * blend + wave * 22 + glow * c3[0] * 0.35)
            g = int(c1[1] * (1 - blend) + c2[1] * blend + wave * 18 + glow * c3[1] * 0.35)
            b = int(c1[2] * (1 - blend) + c2[2] * blend + wave * 28 + glow * c3[2] * 0.35)
            r = max(0, min(255, r))
            g = max(0, min(255, g))
            b = max(0, min(255, b))
            row.extend((r, g, b))
        rows.append(bytes(row))
    raw = b"".join(rows)
    png = b"\x89PNG\r\n\x1a\n"
    png += png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    png += png_chunk(b"IDAT", zlib.compress(raw, 9))
    png += png_chunk(b"IEND", b"")
    path.write_bytes(png)
    return path


def upload_content_image(access_token: str, image_path: Path):
    url = f"{WECHAT_API_BASE}/media/uploadimg?{urlencode({'access_token': access_token})}"
    data = multipart_request(url, image_path)
    image_url = data.get("url")
    if image_url:
        return image_url, data
    raise RuntimeError(f"上传正文图片失败：{json.dumps(data, ensure_ascii=False)}")


def markdown_inline(text: str):
    s = html.escape(text)
    s = re.sub(r"\*\*(.*?)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"\[(.*?)\]\((https?://[^)]+)\)", r'<a href="\2">\1</a>', s)
    s = re.sub(r'&lt;(https?://[^&]+)&gt;', r'<a href="\1">\1</a>', s)
    return s


def render_paragraph(text: str, *, lead: bool = False, muted: bool = False):
    style = "margin:0 0 16px; font-size:16px; line-height:1.9; color:#1f2937; letter-spacing:0.2px;"
    if lead:
        style = "margin:0 0 18px; font-size:17px; line-height:1.95; color:#111827; letter-spacing:0.2px;"
    if muted:
        style = "margin:0 0 14px; font-size:14px; line-height:1.85; color:#6b7280;"
    return f'<p style="{style}">{markdown_inline(text)}</p>'


def render_h2(title: str):
    return (
        '<section style="margin:34px 0 16px;">'
        f'<div style="display:inline-block;padding:6px 14px;border-radius:999px;background:#eef4ff;color:#2563eb;'
        f'font-size:12px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;">{markdown_inline(title)}</div>'
        '</section>'
    )


def render_h3(title: str):
    return (
        '<div style="margin:26px 0 12px;padding:14px 16px;border-radius:16px;'
        'background:linear-gradient(135deg,#ffffff 0%,#f8fbff 100%);border:1px solid #dbe7ff;box-shadow:0 8px 20px rgba(37,99,235,.06);">'
        f'<div style="font-size:18px;font-weight:800;line-height:1.55;color:#0f172a;">{markdown_inline(title)}</div>'
        '</div>'
    )


def render_core_list(items):
    blocks = []
    for item in items:
        blocks.append(
            '<div style="margin:0 0 12px;padding:14px 16px;border-radius:14px;background:#f8fafc;border:1px solid #e2e8f0;">'
            f'<div style="font-size:15px;line-height:1.85;color:#111827;">{markdown_inline(item)}</div>'
            '</div>'
        )
    return '<div style="margin:14px 0 8px;">' + ''.join(blocks) + '</div>'


def render_signal_list(items, title: str):
    blocks = []
    tone = '#059669' if '多源' in title else '#d97706'
    bg = '#ecfdf5' if '多源' in title else '#fff7ed'
    border = '#a7f3d0' if '多源' in title else '#fed7aa'
    for item in items:
        blocks.append(
            f'<div style="margin:0 0 12px;padding:14px 16px;border-radius:14px;background:{bg};border:1px solid {border};">'
            f'<div style="font-size:15px;line-height:1.85;color:#111827;">{markdown_inline(item)}</div>'
            '</div>'
        )
    return '<div style="margin:8px 0 4px;">' + ''.join(blocks) + '</div>'


def render_footer_link(text: str):
    return (
        '<div style="margin:30px 0 6px;padding:16px 18px;border-radius:16px;background:#f8fafc;border:1px solid #e2e8f0;">'
        '<div style="font-size:12px;font-weight:700;letter-spacing:.08em;color:#6b7280;text-transform:uppercase;margin-bottom:8px;">延伸阅读</div>'
        f'<div style="font-size:15px;line-height:1.8;color:#111827;">{markdown_inline(text)}</div>'
        '</div>'
    )


def render_image_block(url: str, caption: str = ""):
    caption_html = (
        f'<div style="margin-top:8px;font-size:12px;line-height:1.7;color:#94a3b8;text-align:center;">{markdown_inline(caption)}</div>'
        if caption else ''
    )
    return (
        '<div style="margin:20px 0 26px;">'
        f'<img src="{html.escape(url)}" style="display:block;width:100%;border-radius:18px;border:1px solid #e5e7eb;" />'
        f'{caption_html}'
        '</div>'
    )


def render_selected_image_gallery(items, limit: int = 2):
    blocks = []
    for item in (items or [])[:limit]:
        url = item.get("wechat_url") or item.get("url") or ""
        if not url:
            continue
        caption = item.get("caption") or item.get("source_title") or "相关原文图"
        blocks.append(render_image_block(url, caption))
    return "".join(blocks)


def markdown_to_html(md: str, image_urls=None):
    image_urls = image_urls or {}
    selected_images = list(image_urls.get('selected') or [])
    lines = (md or "").splitlines()
    out = [
        '<section style="font-family:-apple-system,BlinkMacSystemFont,Segoe UI,PingFang SC,Hiragino Sans GB,Microsoft YaHei,sans-serif;'
        'font-size:16px;color:#111827;line-height:1.9;">'
    ]
    current_h2 = ""
    current_h3 = ""
    preface = []
    list_buffer = []
    list_mode = None
    started_sections = False
    event_image_inserted = False
    selected_images_inserted = False

    def flush_list():
        nonlocal list_buffer, list_mode
        if not list_buffer:
            return
        if list_mode == 'core':
            out.append(render_core_list(list_buffer))
        elif list_mode == 'signal':
            out.append(render_signal_list(list_buffer, current_h3 or current_h2))
        else:
            blocks = ''.join(
                f'<li style="margin:0 0 10px;color:#1f2937;line-height:1.85;">{markdown_inline(item)}</li>'
                for item in list_buffer
            )
            out.append(f'<ul style="padding-left:22px;margin:10px 0 18px;">{blocks}</ul>')
        list_buffer = []
        list_mode = None

    for raw in lines:
        line = raw.strip()
        if not line:
            flush_list()
            continue
        if line.startswith('# '):
            continue
        if line.startswith('## '):
            flush_list()
            title = line[3:].strip()
            current_h2 = title
            current_h3 = ''
            if not started_sections:
                started_sections = True
                if preface:
                    lead_html = ''.join(render_paragraph(p, lead=(i == 0)) for i, p in enumerate(preface))
                    out.append(
                        '<div style="margin:8px 0 30px;padding:20px 18px;border-radius:18px;'
                        'background:linear-gradient(135deg,#f8fbff 0%,#ffffff 100%);border:1px solid #dbe7ff;box-shadow:0 10px 24px rgba(37,99,235,.06);">'
                        '<div style="font-size:12px;font-weight:700;letter-spacing:.08em;color:#2563eb;text-transform:uppercase;margin-bottom:10px;">导语</div>'
                        f'{lead_html}'
                        '</div>'
                    )
                    if image_urls.get('lead'):
                        out.append(render_image_block(image_urls['lead'], '今日主线视觉图'))
                    if selected_images and not selected_images_inserted:
                        out.append(render_selected_image_gallery(selected_images, limit=2))
                        selected_images_inserted = True
                    preface = []
            if title == '重点事件详解' and image_urls.get('event') and not event_image_inserted:
                out.append(render_image_block(image_urls['event'], '重点事件视觉图'))
                event_image_inserted = True
            out.append(render_h2(title))
            if started_sections and not preface and selected_images and not selected_images_inserted:
                out.append(render_selected_image_gallery(selected_images, limit=2))
                selected_images_inserted = True
            continue
        if line.startswith('### '):
            flush_list()
            current_h3 = line[4:].strip()
            out.append(render_h3(current_h3))
            continue
        if line == '---':
            flush_list()
            if not started_sections:
                continue
            out.append('<div style="height:1px;background:linear-gradient(90deg,rgba(37,99,235,0),rgba(37,99,235,.26),rgba(37,99,235,0));margin:26px 0;"></div>')
            continue
        if line.startswith('- '):
            item = line[2:].strip()
            if current_h2 == '今日核心判断':
                list_mode = 'core'
            elif current_h2 == '今日信号分级':
                list_mode = 'signal'
            else:
                list_mode = 'normal'
            list_buffer.append(item)
            continue

        flush_list()
        if line.startswith('完整版日报：') or line.startswith('延伸阅读：'):
            out.append(render_footer_link(line))
            continue
        if not started_sections:
            preface.append(line)
            continue
        if current_h2 == '结语':
            out.append(
                '<div style="margin:18px 0 8px;padding:18px 18px;border-radius:16px;background:#f8fafc;border:1px solid #e5e7eb;">'
                f'{render_paragraph(line)}'
                '</div>'
            )
            continue
        muted = current_h2 == '重点方向' and not current_h3
        out.append(render_paragraph(line, muted=muted))

    flush_list()
    if preface:
        lead_html = ''.join(render_paragraph(p, lead=(i == 0)) for i, p in enumerate(preface))
        out.append(
            '<div style="margin:8px 0 30px;padding:20px 18px;border-radius:18px;'
            'background:linear-gradient(135deg,#f8fbff 0%,#ffffff 100%);border:1px solid #dbe7ff;">'
            '<div style="font-size:12px;font-weight:700;letter-spacing:.08em;color:#2563eb;text-transform:uppercase;margin-bottom:10px;">导语</div>'
            f'{lead_html}'
            '</div>'
        )
        if image_urls.get('lead'):
            out.append(render_image_block(image_urls['lead'], '今日主线视觉图'))
        if selected_images and not selected_images_inserted:
            out.append(render_selected_image_gallery(selected_images, limit=2))
    out.append('</section>')
    return '\n'.join(out)


def extract_title(article_md: str):
    for line in (article_md or "").splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return "AI日报"


def extract_digest(article_md: str, limit: int = 120):
    skip_titles = {"导语", "今日核心判断", "重点方向", "今日重点方向", "重点事件详解", "今日信号分级", "结语", "延伸阅读", "多源共识", "单点信号"}
    for line in (article_md or "").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if s in skip_titles or (s.startswith("## ") or s.startswith("### ")):
            continue
        s = re.sub(r"\*\*(.*?)\*\*", r"\1", s)
        s = re.sub(r"\[(.*?)\]\((https?://[^)]+)\)", r"\1", s)
        s = re.sub(r"\s+", " ", s).strip("-• ")
        if len(s) < 8:
            continue
        return s[:limit]
    return "AI日报精选摘要"


def build_draft_article(article_md: str, report_url: str, thumb_media_id: str, account_name: str, image_urls=None, title_override: str = "", digest_override: str = ""):
    title = (title_override or extract_title(article_md)).strip()
    digest = (digest_override or extract_digest(article_md, 120)).strip()
    content = markdown_to_html(article_md, image_urls=image_urls)
    return {
        "title": title[:64],
        "author": account_name[:16],
        "digest": digest[:120],
        "content": content,
        "content_source_url": report_url[:1024],
        "thumb_media_id": thumb_media_id,
        "need_open_comment": 0,
        "only_fans_can_comment": 0,
    }


def generate_wechat_article(report_id: int):
    report = load_report_record(report_id)
    prompt = build_wechat_user_message(report)
    article_md = call_minimax(WECHAT_SYSTEM_PROMPT, prompt)
    if str(article_md).startswith("ERROR:"):
        raise RuntimeError(article_md)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    title = extract_title(article_md)
    safe = re.sub(r"[^a-zA-Z0-9\u4e00-\u9fff_-]+", "_", title)[:80]
    save_path = OUTPUT_DIR / f"{report_id}_{safe}.md"
    save_path.write_text(article_md, encoding="utf-8")
    return report, article_md, save_path


def generate_multi_wechat_articles(report_id: int, max_sub: int = 4):
    report = load_report_record(report_id)
    report_url = f"{WEB_BASE_URL}/reports/{report_id}"
    main_topic, sub_topics, sections, topic_scores = choose_multi_topics(report.get("report_md") or "", max_sub=max_sub)
    github_refs = merge_github_refs(
        extract_github_refs(report.get("report_md") or "", topic="AI / 大模型"),
        extract_github_refs_from_window(report),
    )
    topic_material = []
    all_topics = ([main_topic] if main_topic else []) + sub_topics
    for topic in all_topics:
        topic_material.append(f"## {topic}\n{sections.get(topic, '')}")
    prompt = f"""请把下面这份内部日报拆成多图文公众号稿件。

规则：
- 生成 1 篇主图文 + 1~{max_sub} 篇子图文
- 主图文必须严格聚焦主题：{main_topic or 'AI / 大模型'}
- 主图文不要写总览，不要混入其他主题，只围绕这个主题展开
- 子图文必须严格按以下主题顺序生成，且只从这些主题里选：{', '.join(sub_topics) if sub_topics else '国际局势 / 地缘政治, 网络安全, 科技产业 / 商业'}
- 子图文与主图文必须是不同主题，彼此之间也不能重复主题
- 主图文标题控制在 26 字内，子图文标题控制在 24 字内，避免口水化和过长
- 子图文每篇聚焦一个强方向，不要硬凑弱方向，不要改变主题顺序
- 延伸阅读里保留完整版链接：{report_url}
- 如果以下给出了 GitHub / 开源项目候选地址，AI / 大模型相关稿件必须优先保留其中 1-3 个完整 URL，不要丢掉

原始日报全文：

{report['report_md']}

AI / 大模型候选 GitHub / 开源项目地址：

{chr(10).join([f'- {item["label"]} | {item["url"]}' for item in github_refs]) if github_refs else '- 无'}

优先参考的子图文方向材料：

{'\n\n'.join(topic_material)}
"""
    raw = call_minimax(MULTI_ARTICLE_SYSTEM_PROMPT, prompt)
    parsed = ensure_multi_article_json(raw)
    used_fallback_main = False
    if not isinstance(parsed, dict) or not isinstance(parsed.get("main"), dict):
        parsed = {"main": fallback_main_article(main_topic or "AI / 大模型", sections.get(main_topic or "AI / 大模型", ""), report_url, github_refs=github_refs), "subs": []}
        used_fallback_main = True

    parsed["main"]["markdown"] = ensure_github_section(parsed["main"].get("markdown") or "", github_refs, main_topic or "")
    parsed["main"]["title"] = build_main_title(
        main_topic or "AI / 大模型",
        parsed["main"].get("markdown") or "",
        sections.get(main_topic or "AI / 大模型", ""),
        parsed["main"].get("title") or "",
    )

    raw_subs = parsed.get("subs") or []
    topic_to_sub = {}
    for item in raw_subs:
        if not isinstance(item, dict) or not item.get("markdown"):
            continue
        topic = normalize_topic_name(item.get("topic") or "")
        if topic and topic not in topic_to_sub:
            item["topic"] = topic
            topic_to_sub[topic] = item

    subs = []
    for topic in sub_topics:
        item = topic_to_sub.get(topic)
        if not item:
            item = fallback_sub_article(topic, sections.get(topic, ""), report_url)
        item["markdown"] = ensure_github_section(item.get("markdown") or "", github_refs, topic)
        item["title"] = build_sub_title(topic, sections.get(topic, ""), item.get("title") or TOPIC_DISPLAY.get(topic, topic))
        subs.append(item)
    subs = subs[:max_sub]

    selected_images = {
        "main": choose_topic_inline_images(report, main_topic or "AI / 大模型", sections.get(main_topic or "AI / 大模型", ""), limit=3),
        "subs": {},
    }
    for topic in sub_topics:
        selected_images["subs"][topic] = choose_topic_inline_images(report, topic, sections.get(topic, ""), limit=2)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    main_title = parsed["main"].get("title") or f"report_{report_id}_main"
    main_safe = re.sub(r"[^a-zA-Z0-9\u4e00-\u9fff_-]+", "_", main_title)[:80]
    main_path = OUTPUT_DIR / f"{report_id}_main_{main_safe}.md"
    main_path.write_text(parsed["main"].get("markdown") or "", encoding="utf-8")

    sub_paths = []
    for idx, item in enumerate(subs, start=1):
        sub_title = item.get("title") or f"sub_{idx}"
        sub_safe = re.sub(r"[^a-zA-Z0-9\u4e00-\u9fff_-]+", "_", sub_title)[:80]
        sub_path = OUTPUT_DIR / f"{report_id}_sub{idx}_{sub_safe}.md"
        sub_path.write_text(item.get("markdown") or "", encoding="utf-8")
        sub_paths.append(str(sub_path))

    return {
        "report": report,
        "report_url": report_url,
        "main": parsed["main"],
        "subs": subs,
        "used_fallback_main": used_fallback_main,
        "main_topic": main_topic,
        "sub_topics": sub_topics,
        "topic_scores": topic_scores,
        "selected_images": selected_images,
        "main_saved_path": str(main_path),
        "sub_saved_paths": sub_paths,
    }


def upload_cover(access_token: str, cover_path: Path):
    url = f"{WECHAT_API_BASE}/material/add_material?{urlencode({'access_token': access_token, 'type': 'image'})}"
    data = multipart_request(url, cover_path)
    media_id = data.get("media_id")
    if media_id:
        return media_id, data
    raise RuntimeError(f"上传封面失败：{json.dumps(data, ensure_ascii=False)}")


def create_draft(access_token: str, article_payload):
    url = f"{WECHAT_API_BASE}/draft/add?{urlencode({'access_token': access_token})}"
    if isinstance(article_payload, dict) and "articles" in article_payload:
        payload = article_payload
    else:
        payload = {"articles": [article_payload]}
    data = http_json(url, method="POST", payload=payload)
    if data.get("media_id"):
        return data["media_id"], data
    raise RuntimeError(f"创建草稿失败：{json.dumps(data, ensure_ascii=False)}")


def main():
    parser = argparse.ArgumentParser(description="生成公众号版日报并可选推送到微信公众号草稿箱")
    parser.add_argument("report_id", nargs="?", type=int, default=None, help="日报 ID，默认取最新一份")
    parser.add_argument("--cover", dest="cover_path", help="自定义封面图片路径")
    parser.add_argument("--check-token", action="store_true", help="只校验 access_token 获取，不创建草稿")
    parser.add_argument("--create-draft", action="store_true", help="真正调用微信草稿箱接口创建草稿")
    parser.add_argument("--multi", action="store_true", help="生成多图文草稿：1篇主图文 + 0~4篇子图文")
    args = parser.parse_args()

    cfg = load_official_account_config()

    report = load_report_record(args.report_id)
    report_id = report["id"]
    report_url = f"{WEB_BASE_URL}/reports/{report_id}"

    if args.multi:
        package = generate_multi_wechat_articles(report_id, max_sub=4)
        main_md = package["main"].get("markdown") or ""
        main_title = package["main"].get("title") or "AI日报"
        subs = package["subs"]
        selected_images = package.get("selected_images") or {"main": [], "subs": {}}
        result = {
            "account_name": cfg.get("account_name"),
            "appid": cfg.get("appid"),
            "report_id": report_id,
            "report_url": report_url,
            "multi": True,
            "main_topic": package.get("main_topic"),
            "sub_topics": package.get("sub_topics") or [],
            "topic_scores": package.get("topic_scores") or {},
            "main_saved_path": package["main_saved_path"],
            "sub_saved_paths": package["sub_saved_paths"],
            "selected_source_images_preview": {
                "main": selected_images.get("main") or [],
                "subs": selected_images.get("subs") or {},
            },
            "create_draft": args.create_draft,
        }
    else:
        report, article_md, saved_path = generate_wechat_article(report_id)
        result = {
            "account_name": cfg.get("account_name"),
            "appid": cfg.get("appid"),
            "report_id": report_id,
            "report_url": report_url,
            "saved_article_path": str(saved_path),
            "create_draft": args.create_draft,
            "multi": False,
        }

    cover_path = Path(args.cover_path) if args.cover_path else (ASSET_DIR / f"report_{report_id}_cover.png")
    if not cover_path.exists():
        create_default_cover(cover_path)
    result["cover_path"] = str(cover_path)

    lead_image_path = ASSET_DIR / f"report_{report_id}_lead.png"
    event_image_path = ASSET_DIR / f"report_{report_id}_event.png"
    if not lead_image_path.exists():
        create_body_illustration(lead_image_path, kind="lead")
    if not event_image_path.exists():
        create_body_illustration(event_image_path, kind="event")
    result["body_image_paths"] = {
        "lead": str(lead_image_path),
        "event": str(event_image_path),
    }

    if args.multi:
        result["draft_preview"] = {
            "main": {
                "title": main_title,
                "digest": extract_digest(main_md),
                "author": cfg.get("account_name"),
            },
            "subs": [
                {
                    "title": (item.get("title") or "")[:40],
                    "topic": item.get("topic") or "",
                    "digest": extract_digest(item.get("markdown") or "", 100),
                }
                for item in subs
            ],
        }
    else:
        title = extract_title(article_md)
        digest = extract_digest(article_md)
        result["draft_preview"] = {
            "title": title,
            "digest": digest,
            "author": cfg.get("account_name"),
        }

    if not args.check_token and not args.create_draft:
        result["status"] = "dry_run_only"
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    access_token, token_resp = get_stable_access_token(cfg["appid"], cfg["appsecret"])
    result["token_ok"] = True
    result["token_hint"] = {k: v for k, v in token_resp.items() if k != "access_token"}

    if args.check_token and not args.create_draft:
        result["status"] = "token_checked"
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    thumb_media_id, cover_resp = upload_cover(access_token, cover_path)
    result["thumb_media_id"] = thumb_media_id
    result["cover_upload"] = cover_resp

    lead_image_url, lead_upload = upload_content_image(access_token, lead_image_path)
    event_image_url, event_upload = upload_content_image(access_token, event_image_path)
    result["body_image_urls"] = {"lead": lead_image_url, "event": event_image_url}
    result["body_image_uploads"] = {"lead": lead_upload, "event": event_upload}

    if args.multi:
        selected_images = package.get("selected_images") or {"main": [], "subs": {}}
        uploaded_selected_images = {
            "main": upload_selected_images(access_token, selected_images.get("main") or [], prefix=f"report_{report_id}_main_src"),
            "subs": {},
        }
        for topic, items in (selected_images.get("subs") or {}).items():
            safe_topic = re.sub(r"[^a-zA-Z0-9\u4e00-\u9fff_-]+", "_", topic)[:32]
            uploaded_selected_images["subs"][topic] = upload_selected_images(access_token, items or [], prefix=f"report_{report_id}_{safe_topic}")
        result["selected_source_images_uploaded"] = uploaded_selected_images

        sub_thumb_media_ids = []
        for idx, _ in enumerate(subs, start=1):
            sub_cover_path = ASSET_DIR / f"report_{report_id}_sub{idx}.png"
            if not sub_cover_path.exists():
                create_body_illustration(sub_cover_path, kind="signal" if idx % 2 else "event", width=900, height=520)
            sub_media_id, _ = upload_cover(access_token, sub_cover_path)
            sub_thumb_media_ids.append(sub_media_id)

        articles_payload = [
            build_draft_article(
                main_md,
                report_url,
                thumb_media_id,
                cfg.get("account_name") or "公众号",
                image_urls={"lead": lead_image_url, "event": event_image_url, "selected": uploaded_selected_images.get("main") or []},
                title_override=main_title,
                digest_override=extract_digest(main_md, 120),
            )
        ]
        for idx, item in enumerate(subs):
            sub_md = item.get("markdown") or ""
            topic = item.get("topic") or ""
            articles_payload.append(
                build_draft_article(
                    sub_md,
                    report_url,
                    sub_thumb_media_ids[idx],
                    cfg.get("account_name") or "公众号",
                    image_urls={"selected": uploaded_selected_images.get("subs", {}).get(topic, [])},
                    title_override=item.get("title") or "",
                    digest_override=extract_digest(sub_md, 100),
                )
            )
        media_id, draft_resp = create_draft(access_token, {"articles": articles_payload})
        result["sub_thumb_media_ids"] = sub_thumb_media_ids
    else:
        article_payload = build_draft_article(
            article_md,
            report_url,
            thumb_media_id,
            cfg.get("account_name") or "公众号",
            image_urls={"lead": lead_image_url, "event": event_image_url},
        )
        media_id, draft_resp = create_draft(access_token, article_payload)
    result["draft_media_id"] = media_id
    result["draft_response"] = draft_resp
    result["status"] = "draft_created"
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
