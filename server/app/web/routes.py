from math import ceil
from collections import Counter, defaultdict
import json
import html
import re
import os
import hashlib
from difflib import SequenceMatcher
from datetime import datetime, timedelta
import requests
from fastapi import APIRouter, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, Response, FileResponse
from fastapi.templating import Jinja2Templates
from app.db import Database
from app.utils.time_util import now_str
from app.services.image_selector import select_article_images
from pathlib import Path
from urllib.parse import quote, urlparse, urlencode

router = APIRouter()
db = Database()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))
PAGE_SIZE = 20
PAGE_SIZE_OPTIONS = [20, 50, 100, 200]
ADMIN_COOKIE = "wechat_ingest_admin"
MEDIA_CACHE_DIR = Path("/root/.openclaw/workspace/wechat_ingest_system/server/data/media_cache")
NON_SOURCE_SIGNAL_LABELS = {"长尾观察", "必选方向", "补充方向", "长尾", "观察"}
ARTICLE_PREF_STATES = {"follow", "ignore", "neutral", "unset"}
ARTICLE_PREF_PROFILE_JSON_PATH = Path('/root/.openclaw/workspace/wechat_ingest_system/server/data/article_preference_profile.json')
REPORT_STATE_JSON_PATH = Path('/root/.openclaw/workspace/wechat_ingest_system/report_state.json')
DASHBOARD_DIRECTION_OPTIONS = [
    "AI / 大模型",
    "国际局势 / 地缘政治",
    "网络安全",
    "中国军事 / 外交",
    "科技产业 / 商业",
    "具身智能 / 机器人",
    "生活健康 / 医疗",
    "消费 / 民生 / 社会风险",
    "教育 / 科学",
    "其他观察",
]
REPORT_SETTINGS_JSON_PATH = Path('/root/.openclaw/workspace/wechat_ingest_system/server/data/report_settings.json')
REPORT_DIRECTION_SIGNAL_LIMITS_DEFAULT = {
    "AI / 大模型": 8,
    "国际局势 / 地缘政治": 6,
    "网络安全": 4,
    "中国军事 / 外交": 4,
    "科技产业 / 商业": 6,
    "具身智能 / 机器人": 4,
    "生活健康 / 医疗": 4,
    "消费 / 民生 / 社会风险": 4,
    "教育 / 科学": 2,
    "其他观察": 2,
}
REPORT_DIRECTION_CAPS_DEFAULT = {
    "AI / 大模型": 16,
    "国际局势 / 地缘政治": 8,
    "网络安全": 8,
    "中国军事 / 外交": 6,
    "科技产业 / 商业": 8,
    "具身智能 / 机器人": 6,
    "生活健康 / 医疗": 6,
    "消费 / 民生 / 社会风险": 6,
    "教育 / 科学": 4,
    "其他观察": 4,
}
DEFAULT_REPORT_SETTINGS = {
    "selection": {
        "max_clusters": 60,
        "longtail_min": 3,
        "same_source_cap_default": 1,
        "same_source_cap_geo_huanqiu": 2,
        "same_source_cap_military_huanqiu": 2,
        "preferred_bonus_ai": 3,
        "preferred_bonus_core": 2,
        "preferred_bonus_longtail": 1,
        "appendix_limit_min": 3,
        "appendix_limit_max": 6,
        "multi_source_appendix_items_preferred": 5,
        "multi_source_appendix_items_default": 3,
        "single_source_appendix_items": 1,
        "github_section_items": 5,
        "direction_caps": REPORT_DIRECTION_CAPS_DEFAULT,
        "signal_limits": REPORT_DIRECTION_SIGNAL_LIMITS_DEFAULT,
    }
}


def _admin_password():
    return (os.getenv("WECHAT_ADMIN_PASSWORD") or "123567Aa!").strip()


def _admin_token():
    password = _admin_password()
    if not password:
        return ""
    secret = os.getenv("WECHAT_ADMIN_SECRET") or "wechat-ingest-admin"
    return hashlib.sha256(f"{password}|{secret}".encode("utf-8")).hexdigest()


def _is_admin_authenticated(request: Request) -> bool:
    password = _admin_password()
    if not password:
        return True
    return request.cookies.get(ADMIN_COOKIE) == _admin_token()


def _request_path(request: Request) -> str:
    path = request.url.path or "/"
    if request.url.query:
        path += f"?{request.url.query}"
    return path


def _require_admin(request: Request):
    if _is_admin_authenticated(request):
        return None
    next_url = quote(_request_path(request), safe="")
    return RedirectResponse(url=f"/admin/login?next={next_url}", status_code=303)


def _safe_next_path(next_value: str | None, default: str = "/articles"):
    raw = (next_value or "").strip()
    if not raw:
        return default
    if raw.startswith("/") and not raw.startswith("//"):
        return raw
    return default


def _deep_merge_dict(base: dict, override: dict):
    merged = dict(base or {})
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge_dict(merged[key], value)
        else:
            merged[key] = value
    return merged


def _load_report_settings():
    settings = json.loads(json.dumps(DEFAULT_REPORT_SETTINGS, ensure_ascii=False))
    if REPORT_SETTINGS_JSON_PATH.exists():
        try:
            raw = json.loads(REPORT_SETTINGS_JSON_PATH.read_text(encoding='utf-8'))
            settings = _deep_merge_dict(settings, raw)
        except Exception:
            pass
    return settings


def _save_report_settings(settings: dict):
    REPORT_SETTINGS_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = _deep_merge_dict(DEFAULT_REPORT_SETTINGS, settings or {})
    payload['updated_at'] = now_str()
    REPORT_SETTINGS_JSON_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')


def _clamp_int(raw, default: int, minimum: int = 0, maximum: int = 999):
    try:
        value = int(str(raw).strip())
    except Exception:
        value = default
    return max(minimum, min(maximum, value))


def _report_settings_view_model():
    settings = _load_report_settings()
    selection = settings.get('selection') or {}
    direction_caps = dict(REPORT_DIRECTION_CAPS_DEFAULT)
    direction_caps.update(selection.get('direction_caps') or {})
    signal_limits = dict(REPORT_DIRECTION_SIGNAL_LIMITS_DEFAULT)
    signal_limits.update(selection.get('signal_limits') or {})
    return {
        'updated_at': settings.get('updated_at') or '未保存',
        'selection': selection,
        'direction_caps': direction_caps,
        'signal_limits': signal_limits,
        'directions': DASHBOARD_DIRECTION_OPTIONS,
    }


def _recommend_direction_cap(direction: str, raw_avg: float, selected_avg: float):
    if direction == '其他观察':
        return 2
    if direction == 'AI / 大模型':
        return max(12, min(20, int(round(max(selected_avg + 4, raw_avg / 5.0)))))
    if raw_avg >= 24:
        return max(6, min(12, int(round(max(selected_avg + 2, raw_avg / 4.0)))))
    if raw_avg >= 10:
        return max(4, min(8, int(round(max(selected_avg + 1, raw_avg / 3.0)))))
    if raw_avg >= 4:
        return max(3, min(6, int(round(max(selected_avg + 1, raw_avg / 2.5)))))
    if raw_avg >= 1:
        return max(2, min(4, int(round(max(selected_avg, raw_avg + 1)))))
    return 1


def _recommend_signal_limit(direction: str, raw_avg: float, signal_avg: float, cap_value: int):
    if direction == '其他观察':
        return 1
    if direction == 'AI / 大模型':
        return max(6, min(cap_value, int(round(max(signal_avg + 2, raw_avg / 10.0)))))
    if raw_avg >= 24:
        return max(4, min(cap_value, int(round(max(signal_avg + 1, raw_avg / 8.0)))))
    if raw_avg >= 10:
        return max(3, min(cap_value, int(round(max(signal_avg + 1, raw_avg / 7.0)))))
    if raw_avg >= 4:
        return max(2, min(cap_value, int(round(max(signal_avg, raw_avg / 6.0)))))
    if raw_avg >= 1:
        return max(1, min(cap_value, int(round(max(signal_avg, 1)))))
    return 1


def _report_settings_recommendations():
    try:
        import sys
        sys.path.insert(0, '/root/.openclaw/workspace/wechat_ingest_system')
        import report_generator as rg
        from statistics import mean
    except Exception:
        return {"days": [], "rows": [], "summary": None}

    today = datetime.now().date()
    active_days = []
    for back in range(0, 7):
        day = today - timedelta(days=back)
        start = f"{day.isoformat()} 06:00:00"
        end = f"{day.isoformat()} 23:30:00"
        rows = rg.load_rows(start, end)
        if rows:
            active_days.append((day.isoformat(), start, end, rows))
        if len(active_days) >= 3:
            break

    if not active_days:
        return {"days": [], "rows": [], "summary": None}

    per_day = []
    for day, start, end, rows in active_days:
        raw_dir = defaultdict(int)
        for row in rows:
            direction = rg.classify_cluster_direction([{
                'title': row['title'],
                'content_md': row['content_md'] if 'content_md' in row.keys() else '',
                'account_name': row['account_name'] or '未知公众号',
            }])
            raw_dir[direction] += 1

        data = rg.dedupe_rows(rows)
        semantic = rg.semantic_week_dedupe(data['rows'], start)
        rows_sem = semantic['rows']
        kept_urls = {r['article_url'] for r in rows_sem}
        cluster_records = []
        for record in data.get('cluster_records') or []:
            rows_kept = [r for r in record['rows'] if r['article_url'] in kept_urls]
            if not rows_kept:
                continue
            rep = record['representative']
            if rep['article_url'] not in kept_urls:
                rep = rg.choose_cluster_representative(rows_kept)
            cluster_records.append({
                **record,
                'rows': rows_kept,
                'representative': rep,
                'direction': rg.classify_cluster_direction(rows_kept),
                'cluster_size': len(rows_kept),
                'source_diversity': len({r['account_name'] or '未知公众号' for r in rows_kept}),
                'sources': sorted({r['account_name'] or '未知公众号' for r in rows_kept}),
            })
        selected = rg.select_balanced_cluster_records(cluster_records)
        data2 = dict(data)
        data2['rows'] = rows_sem
        data2['cluster_records'] = cluster_records
        data2['selected_cluster_records'] = selected
        signals = rg.build_signal_records(data2)
        per_day.append({
            'day': day,
            'raw_total': len(rows),
            'raw_dir': dict(raw_dir),
            'selected_dir': Counter(r['direction'] for r in selected),
            'signals_dir': Counter(s['direction'] for s in signals),
        })

    rows = []
    for direction in DASHBOARD_DIRECTION_OPTIONS:
        raw_list = [item['raw_dir'].get(direction, 0) for item in per_day]
        selected_list = [item['selected_dir'].get(direction, 0) for item in per_day]
        signal_list = [item['signals_dir'].get(direction, 0) for item in per_day]
        raw_avg = mean(raw_list) if raw_list else 0.0
        selected_avg = mean(selected_list) if selected_list else 0.0
        signal_avg = mean(signal_list) if signal_list else 0.0
        recommended_cap = _recommend_direction_cap(direction, raw_avg, selected_avg)
        recommended_signal = _recommend_signal_limit(direction, raw_avg, signal_avg, recommended_cap)
        rows.append({
            'direction': direction,
            'raw_avg': round(raw_avg, 1),
            'selected_avg': round(selected_avg, 1),
            'signal_avg': round(signal_avg, 1),
            'recommended_cap': recommended_cap,
            'recommended_signal': recommended_signal,
            'raw_list': raw_list,
            'signal_list': signal_list,
        })

    return {
        'days': [item['day'] for item in per_day],
        'rows': rows,
        'summary': {
            'active_days': len(per_day),
            'avg_raw_total': round(mean([item['raw_total'] for item in per_day]), 1),
        }
    }


def _article_pref_meta(state: str | None):
    state = (state or "").strip().lower()
    if state == "follow":
        return {"state": "follow", "label": "关注", "badge": "good"}
    if state == "ignore":
        return {"state": "ignore", "label": "忽略", "badge": "bad"}
    if state == "neutral":
        return {"state": "neutral", "label": "中性", "badge": ""}
    return {"state": "unset", "label": "未设置", "badge": "warn"}


def _fetch_status_meta(status: str | None, error: str | None = None):
    status = (status or "").strip().lower()
    error = (error or "").strip()
    if status == "done":
        return {"status": status, "label": "已完成", "badge": "good"}
    if status == "failed":
        return {"status": status, "label": "失败", "badge": "bad"}
    if status == "deleted_author":
        return {"status": status, "label": "作者已删除", "badge": "warn"}
    if status == "deleted_violation":
        return {"status": status, "label": "违规不可见", "badge": "warn"}
    if status == "deleted_complaint":
        return {"status": status, "label": "投诉/侵权不可见", "badge": "warn"}
    if status == "deleted_unavailable":
        return {"status": status, "label": "内容不可查看", "badge": "warn"}
    if status == "deleted":
        label = "已删除"
        if "发布者删除" in error:
            label = "作者已删除"
        elif "违规" in error:
            label = "违规不可见"
        elif "投诉" in error or "侵权" in error:
            label = "投诉/侵权不可见"
        elif "无法查看" in error:
            label = "内容不可查看"
        return {"status": status, "label": label, "badge": "warn"}
    return {"status": "pending", "label": "处理中", "badge": "warn"}


def _apply_article_preference(article_id: int, state: str):
    state = (state or "").strip().lower()
    if state not in ARTICLE_PREF_STATES:
        raise HTTPException(status_code=400, detail="invalid preference state")
    pref_state = None if state == "unset" else state
    updated_at = now_str()
    with db.connect() as conn:
        row = conn.execute(
            "SELECT a.id, a.user_pref_state, a.user_pref_updated_at, tg.account_name, a.title FROM articles a LEFT JOIN targets tg ON a.target_id=tg.id WHERE a.id=?",
            (article_id,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="article not found")
        conn.execute(
            "UPDATE articles SET user_pref_state=?, user_pref_updated_at=? WHERE id=?",
            (pref_state, updated_at, article_id),
        )
    payload = dict(row)
    payload["user_pref_state"] = pref_state
    payload["user_pref_updated_at"] = updated_at
    payload["pref_meta"] = _article_pref_meta(pref_state)
    return payload


def _load_article_preference_profile():
    if ARTICLE_PREF_PROFILE_JSON_PATH.exists():
        try:
            return json.loads(ARTICLE_PREF_PROFILE_JSON_PATH.read_text(encoding='utf-8'))
        except Exception:
            pass
    return {
        "updated_at": "",
        "labeled_count": 0,
        "follow_count": 0,
        "ignore_count": 0,
        "neutral_count": 0,
        "ai_summary": {
            "summary": "",
            "likes": [],
            "dislikes": [],
            "neutral_observations": [],
            "selection_advice": [],
        },
        "ai_model_used": "",
        "ai_fallback_used": False,
        "ai_fallback_reason": "",
        "ai_rules": {
            "boost_directions": [],
            "suppress_directions": [],
            "boost_sources": [],
            "suppress_sources": [],
            "boost_terms": [],
            "suppress_terms": [],
        },
        "ai_rules_model_used": "",
        "ai_rules_fallback_used": False,
        "ai_rules_fallback_reason": "",
        "preferred_sources": [],
        "ignored_sources": [],
        "preferred_terms": [],
        "ignored_terms": [],
        "followed_titles": [],
        "ignored_titles": [],
        "neutral_titles": [],
        "description": "# 文章偏好画像\n\n当前还没有生成偏好画像。",
    }


def _load_report_state():
    if REPORT_STATE_JSON_PATH.exists():
        try:
            data = json.loads(REPORT_STATE_JSON_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {
                    "last_morning_report_at": data.get("last_morning_report_at"),
                    "last_evening_report_at": data.get("last_evening_report_at"),
                }
        except Exception:
            pass
    return {"last_morning_report_at": None, "last_evening_report_at": None}


def _parse_json_object(raw: str | None):
    text = (raw or "").strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _safe_int(value, default: int = 0):
    try:
        if value in (None, ""):
            return default
        return int(float(value))
    except Exception:
        return default


def _parse_selected_target_ids(raw: str | None):
    values = []
    seen = set()
    for part in (raw or "").split(","):
        target_id = _safe_int(part.strip(), 0)
        if target_id <= 0 or target_id in seen:
            continue
        seen.add(target_id)
        values.append(target_id)
    return values


def _batch_targets_where(selected_target_ids_raw: str | None, enabled_only: int | None = None):
    selected_ids = _parse_selected_target_ids(selected_target_ids_raw)
    if selected_ids:
        placeholders = ",".join("?" for _ in selected_ids)
        return f"WHERE id IN ({placeholders})", selected_ids
    if enabled_only is not None and int(enabled_only or 0) == 1:
        return "WHERE enabled=1", []
    return "", []


def _parse_dt(value: str | None):
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def _parse_form_datetime(value: str | None):
    raw = (value or "").strip()
    if not raw:
        return None
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(raw, fmt)
        except Exception:
            continue
    return None


def _time_ago_label(value: str | None):
    dt = _parse_dt(value)
    if not dt:
        return ""
    delta = datetime.now() - dt
    minutes = max(0, int(delta.total_seconds() // 60))
    if minutes < 60:
        return f"{minutes} 分钟前"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} 小时前"
    days = hours // 24
    return f"{days} 天前"


def _recent_labeled_articles(limit: int = 20):
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT a.id, a.title, a.article_url, a.user_pref_state, a.user_pref_updated_at,
                   tg.account_name
            FROM articles a
            LEFT JOIN targets tg ON a.target_id=tg.id
            WHERE a.user_pref_state IN ('follow', 'ignore', 'neutral')
            ORDER BY COALESCE(a.user_pref_updated_at, a.discovered_at) DESC, a.id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        item["pref_meta"] = _article_pref_meta(item.get("user_pref_state"))
        items.append(item)
    return items


def _profile_term_hits(title: str, terms, limit: int = 3):
    title_norm = re.sub(r"\s+", "", (title or "").lower())
    hits = []
    seen = set()
    for term in (terms or []):
        raw = str(term or "").strip()
        norm = re.sub(r"\s+", "", raw.lower())
        if len(norm) < 2 or raw in seen:
            continue
        if norm in title_norm:
            hits.append(raw)
            seen.add(raw)
            if len(hits) >= limit:
                break
    return hits


def _dashboard_pref_signal(item: dict, profile: dict, include_state: bool = True):
    score = 0.0
    reasons = []

    state = (item.get("user_pref_state") or "").strip().lower()
    if include_state:
        if state == "follow":
            score += 8.0
            reasons.append("已标关注")
        elif state == "ignore":
            score -= 8.0
            reasons.append("已标忽略")
        elif state == "neutral":
            reasons.append("已标中性")

    source = (item.get("account_name") or "未知公众号").strip() or "未知公众号"
    source_signal = float(((profile.get("source_scores") or {}).get(source)) or 0.0)
    if source_signal >= 0.5:
        score += min(4.5, source_signal * 0.9)
        reasons.append(f"偏好来源：{source}")
    elif source_signal <= -0.5:
        score += max(-4.5, source_signal * 0.9)
        reasons.append(f"低偏好来源：{source}")

    title = item.get("title") or ""
    pos_hits = _profile_term_hits(title, profile.get("preferred_terms") or [], limit=3)
    neg_hits = _profile_term_hits(title, profile.get("ignored_terms") or [], limit=3)
    if pos_hits:
        score += min(4.0, 1.25 * len(pos_hits))
        reasons.append("偏好词：" + " / ".join(pos_hits))
    if neg_hits:
        score -= min(4.0, 1.25 * len(neg_hits))
        reasons.append("低偏好词：" + " / ".join(neg_hits))

    if not pos_hits and not neg_hits:
        term_scores = profile.get("term_scores") or {}
        token_score = 0.0
        for token in _match_terms(title):
            token_score += float(term_scores.get(token) or 0.0)
        token_signal = max(-3.0, min(3.0, token_score * 0.25))
        score += token_signal
        if token_signal >= 1.0:
            reasons.append("标题词与既有偏好相近")
        elif token_signal <= -1.0:
            reasons.append("标题词与已忽略样本相近")

    return round(score, 2), reasons[:3]


def _ai_rule_term_matches(raw_term: str, title: str, content_md: str):
    term = str(raw_term or "").strip().lower()
    if len(term) < 2:
        return False
    hay = ((title or "") + "\n" + (content_md or "")).lower()
    if term in hay:
        return True
    for part in re.split(r'[/、，,；;|+]|\s+', term):
        p = part.strip().lower()
        if len(p) >= 2 and p in hay:
            return True
    return False


def _dashboard_ai_rule_signal(item: dict, profile: dict):
    rules = (profile or {}).get("ai_rules") or {}
    if not rules:
        return 0.0, []

    score = 0.0
    reasons = []
    direction = _classify_dashboard_direction(item)
    source = (item.get("account_name") or "未知公众号").strip() or "未知公众号"
    title = item.get("title") or ""
    content_md = item.get("content_md") or ""

    for rule in rules.get("boost_directions") or []:
        if rule.get("direction") == direction:
            weight = float(rule.get("weight") or 0.0)
            score += weight
            reasons.append(f"AI方向提权：{direction}")
    for rule in rules.get("suppress_directions") or []:
        if rule.get("direction") == direction:
            weight = float(rule.get("weight") or 0.0)
            score -= weight
            reasons.append(f"AI方向压低：{direction}")
    for rule in rules.get("boost_sources") or []:
        if rule.get("source") == source:
            weight = float(rule.get("weight") or 0.0)
            score += weight
            reasons.append(f"AI来源提权：{source}")
    for rule in rules.get("suppress_sources") or []:
        if rule.get("source") == source:
            weight = float(rule.get("weight") or 0.0)
            score -= weight
            reasons.append(f"AI来源压低：{source}")
    for rule in rules.get("boost_terms") or []:
        if _ai_rule_term_matches(rule.get("term") or "", title, content_md):
            weight = float(rule.get("weight") or 0.0)
            score += weight
            reasons.append(f"AI主题提权：{rule.get('term')}")
    for rule in rules.get("suppress_terms") or []:
        if _ai_rule_term_matches(rule.get("term") or "", title, content_md):
            weight = float(rule.get("weight") or 0.0)
            score -= weight
            reasons.append(f"AI主题压低：{rule.get('term')}")

    deduped = []
    seen = set()
    for reason in reasons:
        if reason in seen:
            continue
        seen.add(reason)
        deduped.append(reason)
    return round(score, 2), deduped[:4]


def _dashboard_ai_rule_impact_meta(score: float):
    if score >= 4:
        return {"label": "AI 明显提权", "badge": "good"}
    if score >= 1.2:
        return {"label": "AI 轻度提权", "badge": "good"}
    if score <= -4:
        return {"label": "AI 明显压低", "badge": "bad"}
    if score <= -1.2:
        return {"label": "AI 轻度压低", "badge": "bad"}
    return {"label": "AI 影响较弱", "badge": ""}


def _dashboard_pref_impact_meta(score: float):
    if score >= 6:
        return {"label": "明显提权", "badge": "good"}
    if score >= 2:
        return {"label": "轻度提权", "badge": "good"}
    if score <= -6:
        return {"label": "明显压低", "badge": "bad"}
    if score <= -2:
        return {"label": "轻度压低", "badge": "bad"}
    return {"label": "影响较弱", "badge": ""}


def _json_list_len(raw: str):
    try:
        data = json.loads(raw or "[]")
        return len(data) if isinstance(data, list) else 0
    except Exception:
        return 0


def _dashboard_value_signal(item: dict):
    score = 0.0
    reasons = []

    content_md = item.get("content_md") or ""
    if content_md:
        score += min(2.4, len(content_md) / 1400.0)
        reasons.append("正文已抓取")

    image_count = _json_list_len(item.get("image_urls_json") or "")
    video_count = _json_list_len(item.get("video_urls_json") or "")
    media_score = min(1.8, image_count * 0.18 + video_count * 0.45)
    if media_score > 0:
        score += media_score
        reasons.append(f"媒体较完整：图{image_count}/视频{video_count}")

    discovered_at = item.get("discovered_at") or ""
    try:
        dt = datetime.strptime(discovered_at, "%Y-%m-%d %H:%M:%S")
        hours_ago = max(0.0, (datetime.now() - dt).total_seconds() / 3600.0)
        recency = max(0.0, 2.6 - min(2.6, hours_ago / 18.0))
        score += recency
        if recency >= 1.2:
            reasons.append("时效性较高")
    except Exception:
        pass

    if len(item.get("title") or "") >= 18:
        score += 0.6

    return round(score, 2), reasons[:2]


def _latest_report_preference_hits(report: dict | None, profile: dict, limit: int = 8):
    if not report or not report.get("report_md"):
        return []
    _, _, appendix_items = _extract_source_links(report.get("report_md") or "")
    urls = []
    seen = set()
    for item in appendix_items:
        url = (item.get("url") or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        urls.append(url)
    if not urls:
        return []

    placeholders = ",".join(["?"] * len(urls))
    with db.connect() as conn:
        rows = conn.execute(
            f"""
            SELECT a.id, a.title, a.article_url, a.discovered_at, a.user_pref_state, a.user_pref_updated_at,
                   tg.account_name
            FROM articles a
            LEFT JOIN targets tg ON a.target_id=tg.id
            WHERE a.article_url IN ({placeholders})
            """,
            tuple(urls),
        ).fetchall()
    rows_by_url = {row["article_url"]: dict(row) for row in rows}

    items = []
    used = set()
    for link in appendix_items:
        url = (link.get("url") or "").strip()
        if not url or url in used:
            continue
        row = rows_by_url.get(url)
        if not row:
            continue
        used.add(url)
        pref_score, reasons = _dashboard_pref_signal(row, profile, include_state=True)
        if abs(pref_score) < 2.0:
            continue
        meta = _dashboard_pref_impact_meta(pref_score)
        item = dict(row)
        item.update({
            "pref_score": pref_score,
            "pref_reasons": reasons,
            "impact_label": meta["label"],
            "impact_badge": meta["badge"],
            "report_link_title": link.get("title") or row.get("title") or "",
        })
        items.append(item)

    items.sort(key=lambda x: (-abs(float(x.get("pref_score") or 0.0)), -(1 if x.get("user_pref_state") == "follow" else 0), x.get("title") or ""))
    return items[:limit]


def _latest_report_ai_rule_impacts(report: dict | None, profile: dict, limit: int = 8):
    if not report or not report.get("report_md"):
        return []
    _, _, appendix_items = _extract_source_links(report.get("report_md") or "")
    urls = []
    seen = set()
    for item in appendix_items:
        url = (item.get("url") or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        urls.append(url)
    if not urls:
        return []

    placeholders = ",".join(["?"] * len(urls))
    with db.connect() as conn:
        rows = conn.execute(
            f"""
            SELECT a.id, a.title, a.article_url, a.discovered_at, a.user_pref_state, a.user_pref_updated_at,
                   a.content_md, tg.account_name
            FROM articles a
            LEFT JOIN targets tg ON a.target_id=tg.id
            WHERE a.article_url IN ({placeholders})
            """,
            tuple(urls),
        ).fetchall()
    rows_by_url = {row["article_url"]: dict(row) for row in rows}

    items = []
    used = set()
    for link in appendix_items:
        url = (link.get("url") or "").strip()
        if not url or url in used:
            continue
        row = rows_by_url.get(url)
        if not row:
            continue
        used.add(url)
        ai_score, ai_reasons = _dashboard_ai_rule_signal(row, profile)
        if abs(ai_score) < 1.2:
            continue
        meta = _dashboard_ai_rule_impact_meta(ai_score)
        item = dict(row)
        item.update({
            "ai_rule_score": ai_score,
            "ai_rule_reasons": ai_reasons,
            "ai_impact_label": meta["label"],
            "ai_impact_badge": meta["badge"],
            "report_link_title": link.get("title") or row.get("title") or "",
        })
        items.append(item)
    items.sort(key=lambda x: (-abs(float(x.get("ai_rule_score") or 0.0)), x.get("title") or ""))
    return items[:limit]


def _priority_unlabeled_candidates(profile: dict, limit: int = 8):
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT a.id, a.title, a.article_url, a.discovered_at, a.fetch_status, a.content_md,
                   a.image_urls_json, a.video_urls_json, a.user_pref_state, a.user_pref_updated_at,
                   tg.account_name
            FROM articles a
            LEFT JOIN targets tg ON a.target_id=tg.id
            WHERE COALESCE(a.user_pref_state, '') = ''
              AND a.fetch_status = 'done'
              AND a.discovered_at >= datetime('now', '-7 day')
            ORDER BY a.discovered_at DESC, a.id DESC
            LIMIT 160
            """
        ).fetchall()

    items = []
    for row in rows:
        item = dict(row)
        pref_score, pref_reasons = _dashboard_pref_signal(item, profile, include_state=False)
        ai_score, ai_reasons = _dashboard_ai_rule_signal(item, profile)
        value_score, value_reasons = _dashboard_value_signal(item)
        total_score = round(abs(pref_score) * 1.55 + abs(ai_score) * 0.85 + value_score, 2)
        if abs(pref_score) < 1.4 and abs(ai_score) < 1.2 and value_score < 2.2:
            continue

        if pref_score >= 3.2:
            suggestion_label = "建议优先看"
            suggestion_badge = "good"
        elif pref_score <= -3.2:
            suggestion_label = "建议优先判忽略"
            suggestion_badge = "bad"
        else:
            suggestion_label = "建议补标"
            suggestion_badge = "warn"

        item.update({
            "candidate_score": total_score,
            "pref_score": pref_score,
            "ai_rule_score": ai_score,
            "value_score": value_score,
            "candidate_reasons": (pref_reasons + ai_reasons + value_reasons)[:4],
            "suggestion_label": suggestion_label,
            "suggestion_badge": suggestion_badge,
        })
        items.append(item)

    items.sort(key=lambda x: (-float(x.get("candidate_score") or 0.0), -abs(float(x.get("pref_score") or 0.0)), x.get("discovered_at") or ""), reverse=False)
    items = sorted(items, key=lambda x: (-float(x.get("candidate_score") or 0.0), -abs(float(x.get("pref_score") or 0.0)), x.get("discovered_at") or ""))
    return items[:limit]


def _report_article_urls(report: dict | None):
    if not report or not report.get("report_md"):
        return set()
    _, _, appendix_items = _extract_source_links(report.get("report_md") or "")
    return {
        (item.get("url") or "").strip()
        for item in appendix_items
        if (item.get("url") or "").strip()
    }


def _classify_dashboard_direction(item: dict):
    try:
        import sys
        project_root = "/root/.openclaw/workspace/wechat_ingest_system"
        if project_root not in sys.path:
            sys.path.insert(0, project_root)
        import report_generator as rg
        row = {
            "title": item.get("title") or "",
            "content_md": item.get("content_md") or "",
            "account_name": item.get("account_name") or "未知公众号",
            "article_url": item.get("article_url") or "",
            "discovered_at": item.get("discovered_at") or "",
            "user_pref_state": item.get("user_pref_state") or "",
            "user_pref_updated_at": item.get("user_pref_updated_at") or "",
        }
        direction = rg.classify_cluster_direction([row])
        return direction or "其他观察"
    except Exception:
        title = (item.get("title") or "") + "\n" + (item.get("content_md") or "")
        low = title.lower()
        if any(k in low for k in ["agent", "大模型", "kimi", "claude", "openai", "github", "模型"]):
            return "AI / 大模型"
        if any(k in low for k in ["伊朗", "以色列", "停火", "外交", "白宫"]):
            return "国际局势 / 地缘政治"
        if any(k in low for k in ["漏洞", "攻击", "渗透", "exploit", "rce"]):
            return "网络安全"
        if any(k in low for k in ["解放军", "东部战区", "舰艇", "台海", "外交部"]):
            return "中国军事 / 外交"
        if any(k in low for k in ["机器人", "具身", "人形", "机械臂", "机器狗"]):
            return "具身智能 / 机器人"
        if any(k in low for k in ["健康", "医疗", "医院", "维生素", "睡眠", "饮食", "刷牙"]):
            return "生活健康 / 医疗"
        if any(k in low for k in ["融资", "商业", "产业", "芯片", "ceo", "公司"]):
            return "科技产业 / 商业"
        if any(k in low for k in ["消费", "民生", "电商", "旅游", "抢票", "机票"]):
            return "消费 / 民生 / 社会风险"
        if any(k in low for k in ["教育", "大学", "科研", "论文", "science", "研究"]):
            return "教育 / 科学"
        return "其他观察"


def _latest_report_missed_candidates(report: dict | None, profile: dict, limit: int = 8):
    if not report:
        return []
    included_urls = _report_article_urls(report)
    window_start = (report.get("window_start") or "").strip()
    window_end = (report.get("window_end") or "").strip()

    with db.connect() as conn:
        if window_start and window_end:
            rows = conn.execute(
                """
                SELECT a.id, a.title, a.article_url, a.discovered_at, a.fetch_status, a.content_md,
                       a.image_urls_json, a.video_urls_json, a.user_pref_state, a.user_pref_updated_at,
                       tg.account_name
                FROM articles a
                LEFT JOIN targets tg ON a.target_id=tg.id
                WHERE a.discovered_at > ? AND a.discovered_at <= ?
                  AND a.fetch_status='done'
                ORDER BY a.discovered_at DESC, a.id DESC
                LIMIT 240
                """,
                (window_start, window_end),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT a.id, a.title, a.article_url, a.discovered_at, a.fetch_status, a.content_md,
                       a.image_urls_json, a.video_urls_json, a.user_pref_state, a.user_pref_updated_at,
                       tg.account_name
                FROM articles a
                LEFT JOIN targets tg ON a.target_id=tg.id
                WHERE a.discovered_at >= datetime('now', '-2 day')
                  AND a.fetch_status='done'
                ORDER BY a.discovered_at DESC, a.id DESC
                LIMIT 240
                """
            ).fetchall()

    items = []
    for row in rows:
        item = dict(row)
        if item.get("article_url") in included_urls:
            continue
        pref_score, pref_reasons = _dashboard_pref_signal(item, profile, include_state=True)
        ai_score, ai_reasons = _dashboard_ai_rule_signal(item, profile)
        value_score, value_reasons = _dashboard_value_signal(item)
        total_score = round(pref_score * 1.5 + max(0.0, ai_score) * 0.8 + value_score, 2)
        if pref_score < 1.0:
            continue
        if total_score < 4.5:
            continue
        item.update({
            "missed_score": total_score,
            "pref_score": pref_score,
            "ai_rule_score": ai_score,
            "value_score": value_score,
            "missed_reasons": (pref_reasons + ai_reasons + value_reasons)[:5],
            "direction": _classify_dashboard_direction(item),
            "missed_label": "高概率漏选" if total_score >= 8.0 else "值得复核",
            "missed_badge": "good" if total_score >= 8.0 else "warn",
        })
        items.append(item)

    items.sort(key=lambda x: (-float(x.get("missed_score") or 0.0), -float(x.get("pref_score") or 0.0), x.get("discovered_at") or ""))
    return items[:limit]


def _direction_preference_gaps(profile: dict, report: dict | None, limit: int = 6):
    included_urls = _report_article_urls(report)
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT a.id, a.title, a.article_url, a.discovered_at, a.fetch_status, a.content_md,
                   a.image_urls_json, a.video_urls_json, a.user_pref_state, a.user_pref_updated_at,
                   tg.account_name
            FROM articles a
            LEFT JOIN targets tg ON a.target_id=tg.id
            WHERE a.fetch_status='done'
              AND a.discovered_at >= datetime('now', '-7 day')
            ORDER BY a.discovered_at DESC, a.id DESC
            LIMIT 300
            """
        ).fetchall()

    by_direction = {}
    for row in rows:
        item = dict(row)
        direction = _classify_dashboard_direction(item)
        bucket = by_direction.setdefault(direction, {
            "direction": direction,
            "total": 0,
            "labeled": 0,
            "unset": 0,
            "follow": 0,
            "ignore": 0,
            "neutral": 0,
            "in_report": 0,
            "examples": [],
        })
        bucket["total"] += 1
        state = (item.get("user_pref_state") or "").strip().lower()
        if state in {"follow", "ignore", "neutral"}:
            bucket["labeled"] += 1
            bucket[state] += 1
        else:
            bucket["unset"] += 1
            pref_score, reasons = _dashboard_pref_signal(item, profile, include_state=False)
            value_score, value_reasons = _dashboard_value_signal(item)
            priority = round(abs(pref_score) * 1.6 + value_score, 2)
            if priority >= 4.0 and len(bucket["examples"]) < 3:
                bucket["examples"].append({
                    "title": item.get("title") or "",
                    "article_url": item.get("article_url") or "",
                    "priority": priority,
                    "reasons": (reasons + value_reasons)[:2],
                })
        if item.get("article_url") in included_urls:
            bucket["in_report"] += 1

    items = []
    for direction, bucket in by_direction.items():
        total = bucket["total"]
        labeled = bucket["labeled"]
        unset = bucket["unset"]
        coverage = round((labeled / max(1, total)) * 100)
        gap_score = round(unset * 1.8 + max(0, total - labeled) * 0.7 + (2.0 if bucket["follow"] == 0 and total >= 3 else 0.0), 2)
        if total < 2:
            continue
        bucket["coverage"] = coverage
        bucket["gap_score"] = gap_score
        if coverage < 35 or unset >= 3:
            bucket["gap_label"] = "优先补这个方向"
            bucket["gap_badge"] = "warn"
        elif coverage < 60:
            bucket["gap_label"] = "仍有缺口"
            bucket["gap_badge"] = ""
        else:
            bucket["gap_label"] = "覆盖尚可"
            bucket["gap_badge"] = "good"
        items.append(bucket)

    items.sort(key=lambda x: (-float(x.get("gap_score") or 0.0), -int(x.get("unset") or 0), x.get("direction") or ""))
    return items[:limit]


def _dashboard_selected_report_articles(report: dict | None, profile: dict, limit: int = 12):
    if not report or not report.get("report_md"):
        return []
    _, _, appendix_items = _extract_source_links(report.get("report_md") or "")
    urls = []
    seen = set()
    for item in appendix_items:
        url = (item.get("url") or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        urls.append(url)
    if not urls:
        return []

    placeholders = ",".join(["?"] * len(urls))
    with db.connect() as conn:
        rows = conn.execute(
            f"""
            SELECT a.id, a.title, a.article_url, a.discovered_at, a.fetch_status, a.content_md,
                   a.image_urls_json, a.video_urls_json, a.user_pref_state, a.user_pref_updated_at,
                   tg.account_name
            FROM articles a
            LEFT JOIN targets tg ON a.target_id=tg.id
            WHERE a.article_url IN ({placeholders})
            """,
            tuple(urls),
        ).fetchall()
    rows_by_url = {row["article_url"]: dict(row) for row in rows}
    items = []
    for link in appendix_items:
        url = (link.get("url") or "").strip()
        if not url:
            continue
        item = rows_by_url.get(url)
        if not item:
            continue
        pref_score, pref_reasons = _dashboard_pref_signal(item, profile, include_state=True)
        ai_score, ai_reasons = _dashboard_ai_rule_signal(item, profile)
        value_score, value_reasons = _dashboard_value_signal(item)
        total_score = round(pref_score * 1.3 + max(0.0, ai_score) * 0.7 + value_score, 2)
        item = dict(item)
        item.update({
            "direction": _classify_dashboard_direction(item),
            "ai_rule_score": ai_score,
            "selected_score": total_score,
            "selected_reasons": (pref_reasons + ai_reasons + value_reasons)[:5],
        })
        items.append(item)
    items.sort(key=lambda x: (-float(x.get("selected_score") or 0.0), x.get("title") or ""))
    return items[:limit]


def _dashboard_selection_comparisons(report: dict | None, profile: dict, limit: int = 4):
    selected = _dashboard_selected_report_articles(report, profile, limit=18)
    missed = _latest_report_missed_candidates(report, profile, limit=18)
    if not selected or not missed:
        return []

    selected_by_direction = {}
    for item in selected:
        direction = item.get("direction") or "其他观察"
        selected_by_direction.setdefault(direction, []).append(item)

    pairs = []
    used_selected = set()
    for miss in missed:
        direction = miss.get("direction") or "其他观察"
        candidates = selected_by_direction.get(direction) or []
        pick = None
        for cand in candidates:
            key = cand.get("article_url") or cand.get("id")
            if key in used_selected:
                continue
            pick = cand
            used_selected.add(key)
            break
        if not pick:
            continue
        pairs.append({
            "direction": direction,
            "selected": pick,
            "missed": miss,
        })
        if len(pairs) >= limit:
            break
    return pairs


def _direction_activity_overview(profile: dict, limit: int = 8):
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT a.id, a.title, a.article_url, a.discovered_at, a.fetch_status, a.content_md,
                   a.image_urls_json, a.video_urls_json, a.user_pref_state, a.user_pref_updated_at,
                   tg.account_name
            FROM articles a
            LEFT JOIN targets tg ON a.target_id=tg.id
            WHERE a.fetch_status='done'
              AND a.discovered_at >= datetime('now', '-7 day')
            ORDER BY a.discovered_at DESC, a.id DESC
            LIMIT 320
            """
        ).fetchall()

    buckets = {}
    now = datetime.now()
    for row in rows:
        item = dict(row)
        direction = _classify_dashboard_direction(item)
        bucket = buckets.setdefault(direction, {
            "direction": direction,
            "total": 0,
            "last_24h": 0,
            "labeled": 0,
            "unset": 0,
            "follow": 0,
            "ignore": 0,
            "neutral": 0,
            "examples": [],
        })
        bucket["total"] += 1
        state = (item.get("user_pref_state") or "").strip().lower()
        if state in {"follow", "ignore", "neutral"}:
            bucket["labeled"] += 1
            bucket[state] += 1
        else:
            bucket["unset"] += 1

        try:
            dt = datetime.strptime(item.get("discovered_at") or "", "%Y-%m-%d %H:%M:%S")
            if (now - dt).total_seconds() <= 86400:
                bucket["last_24h"] += 1
        except Exception:
            pass

        if len(bucket["examples"]) < 2:
            bucket["examples"].append(item.get("title") or "")

    items = []
    for bucket in buckets.values():
        bucket["coverage"] = round((bucket["labeled"] / max(1, bucket["total"])) * 100)
        items.append(bucket)

    items.sort(key=lambda x: (-int(x.get("total") or 0), -int(x.get("last_24h") or 0), x.get("direction") or ""))
    return items[:limit]


def _priority_today_queue(profile: dict, limit: int = 10):
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT a.id, a.title, a.article_url, a.discovered_at, a.fetch_status, a.content_md,
                   a.image_urls_json, a.video_urls_json, a.user_pref_state, a.user_pref_updated_at,
                   tg.account_name
            FROM articles a
            LEFT JOIN targets tg ON a.target_id=tg.id
            WHERE a.fetch_status='done'
              AND a.discovered_at >= datetime('now', '-1 day')
            ORDER BY a.discovered_at DESC, a.id DESC
            LIMIT 180
            """
        ).fetchall()

    items = []
    for row in rows:
        item = dict(row)
        pref_score, pref_reasons = _dashboard_pref_signal(item, profile, include_state=True)
        ai_score, ai_reasons = _dashboard_ai_rule_signal(item, profile)
        value_score, value_reasons = _dashboard_value_signal(item)
        state = (item.get("user_pref_state") or "").strip().lower()
        unlabeled_bonus = 2.2 if not state else 0.0
        total_score = round(abs(pref_score) * 1.35 + abs(ai_score) * 0.8 + value_score + unlabeled_bonus, 2)
        if total_score < 4.2:
            continue
        item["dashboard_direction"] = _classify_dashboard_direction(item)
        item["pref_signal_score"] = pref_score
        item["ai_rule_score"] = ai_score
        item["value_signal_score"] = value_score
        item["system_explain_reasons"] = (pref_reasons + ai_reasons + value_reasons)[:5]
        item["priority_score"] = total_score
        if not state:
            item["queue_label"] = "优先补标"
            item["queue_badge"] = "warn"
        elif pref_score >= 4.0:
            item["queue_label"] = "值得关注"
            item["queue_badge"] = "good"
        elif pref_score <= -4.0:
            item["queue_label"] = "可快速判忽略"
            item["queue_badge"] = "bad"
        else:
            item["queue_label"] = "建议复核"
            item["queue_badge"] = ""
        item["review_url"] = f"/articles?title={quote(item.get('title') or '', safe='')}&fetch_status=done"
        items.append(item)

    items.sort(key=lambda x: (-float(x.get("priority_score") or 0.0), x.get("discovered_at") or ""))
    return items[:limit]


def _image_proxy_url(url: str):
    raw = (url or "").strip()
    if not raw:
        return ""
    return f"/media/image-proxy?url={quote(raw, safe='')}"


def _image_cache_paths(url: str):
    key = hashlib.sha256((url or "").encode("utf-8")).hexdigest()
    return MEDIA_CACHE_DIR / f"{key}.bin", MEDIA_CACHE_DIR / f"{key}.json"


def _normalize_source_name(name: str) -> str:
    norm = re.sub(r"\s+", "", (name or "").strip().lower())
    for suffix in ["公众号", "订阅号", "服务号"]:
        norm = norm.replace(suffix, "")
    return norm


def _source_name_matches(candidate: str, expected: str) -> bool:
    left = _normalize_source_name(candidate)
    right = _normalize_source_name(expected)
    if not left or not right:
        return False
    return left == right or left.startswith(right) or right.startswith(left)


def _source_link_fallback(source_links: dict, source_name: str):
    if source_links.get(source_name):
        return source_links[source_name][0]
    for key, pairs in (source_links or {}).items():
        if _source_name_matches(key, source_name) and pairs:
            return pairs[0]
    return None


def _signal_badge_replace(text: str, source_links=None, fallback_links=None, global_links=None):
    source_links = source_links or {}
    fallback_links = fallback_links or []
    global_links = global_links or []
    def repl(match):
        weight = (match.group(1) or "").strip()
        source_count_raw = (match.group(2) or "").strip()
        weight_sources = (match.group(3) or "").strip("·")
        old_kind = match.group(4)
        old_sources = (match.group(5) or "").strip("·")
        kind = old_kind or ("多源共识" if int(source_count_raw or 0) >= 2 else "单点信号")
        sources = weight_sources or old_sources
        if sources in NON_SOURCE_SIGNAL_LABELS:
            sources = ""
        weight_val = float(weight or 0.0) if weight else 0.0
        source_names = [s.strip() for s in re.split(r"[\/、,，]", sources) if s.strip()]
        source_count = int(source_count_raw or 0) if source_count_raw else len(source_names)
        if source_count <= 0:
            source_count = 1 if kind == "单点信号" else 2
        cls = "good" if weight_val >= 0.85 or source_count >= 3 else "warn"
        badge_text = f"权重 {weight}｜{source_count}源" if weight else kind
        title = f"{badge_text}" if not sources else f"{badge_text} · 来源公众号：{sources}"
        tips = []
        seen_urls = set()
        if source_names:
            per_source_limit = 1 if kind == "单点信号" else (2 if source_count <= 3 else 1)
            total_limit = 1 if kind == "单点信号" else min(8, max(source_count, source_count + 2))
            for src in source_names:
                scoped_group = [item for item in (fallback_links or []) if _source_name_matches(item.get("source") or "", src)]
                scoped_global = [item for item in (global_links or []) if _source_name_matches(item.get("source") or "", src)]
                scoped_candidates = _merge_link_items(scoped_group, scoped_global)
                picked = _pick_signal_links(text, kind, scoped_candidates, [], limit_override=per_source_limit)
                if picked:
                    for item in picked:
                        url = item.get("url") or ""
                        if not url or url in seen_urls:
                            continue
                        seen_urls.add(url)
                        label_src = item.get("source") or src
                        tips.append(f'<a href="{html.escape(url)}" target="_blank" rel="noopener">{html.escape(label_src)}：{html.escape(item.get("title") or "")}</a>')
                        if len(tips) >= total_limit:
                            break
                else:
                    fallback_pair = _source_link_fallback(source_links, src)
                    if fallback_pair:
                        title0, url0 = fallback_pair
                        if url0 and url0 not in seen_urls:
                            seen_urls.add(url0)
                            tips.append(f'<a href="{html.escape(url0)}" target="_blank" rel="noopener">{html.escape(src)}：{html.escape(title0)}</a>')
                    else:
                        tips.append(f'<span>{html.escape(src)}</span>')
                if len(tips) >= total_limit:
                    break
            if kind == "多源共识" and len(tips) < total_limit:
                related_pool = [
                    item for item in (global_links or [])
                    if any(_source_name_matches(item.get("source") or "", src) for src in source_names)
                ]
                for item in _pick_signal_links(text, kind, related_pool, [], limit_override=total_limit):
                    url = item.get("url") or ""
                    if not url or url in seen_urls:
                        continue
                    seen_urls.add(url)
                    label_src = item.get("source") or "未知公众号"
                    tips.append(f'<a href="{html.escape(url)}" target="_blank" rel="noopener">{html.escape(label_src)}：{html.escape(item.get("title") or "")}</a>')
                    if len(tips) >= total_limit:
                        break
        else:
            picked = _pick_signal_links(text, kind, fallback_links, global_links, limit_override=source_count)
            for item in picked:
                url = item.get("url") or ""
                src = item.get("source") or "未知公众号"
                title0 = item.get("title") or ""
                if url:
                    tips.append(f'<a href="{html.escape(url)}" target="_blank" rel="noopener">{html.escape(src)}：{html.escape(title0)}</a>')
                else:
                    tips.append(f'<span>{html.escape(src)}：{html.escape(title0)}</span>')
            if not tips:
                tips.append('<span>暂无可匹配来源</span>')
        tooltip_html = "".join(f"<div>{t}</div>" for t in tips) if tips else "<div>暂无可匹配来源</div>"
        extra = f" data-tooltip-html='{html.escape(tooltip_html, quote=True)}' data-label='{html.escape(title)}'"
        return f'<span class="badge {cls} signal-badge"{extra}>{html.escape(badge_text)}</span>'
    return re.sub(r"【(?:权重\s*([0-9.]+)·(\d+)源(?:·([^】]+))?|(多源共识|单点信号)(?:·([^】]+))?)】", repl, text)


def _extract_source_links(md: str):
    mapping = {}
    in_appendix = False
    current_group = None
    grouped = {}
    all_links = []
    for raw in (md or "").splitlines():
        s = raw.strip()
        if s.startswith("## ") and "链接附录" in s:
            in_appendix = True
            continue
        if not in_appendix:
            continue
        if s.startswith("### ") or (s.startswith("**") and s.endswith("**")):
            current_group = _normalize_heading(s.replace("### ", "").strip("*"))
            grouped.setdefault(current_group, [])
            continue
        if not (s.startswith("- ") or re.match(r"^\d+[\.、]\s+", s)):
            continue
        body = s[2:] if s.startswith("- ") else re.sub(r"^\d+[\.、]\s+", "", s)
        parts = [p.strip() for p in body.split(" | ")]
        if len(parts) >= 3 and parts[2].startswith("http"):
            title, source, url = parts[:3]
            item = {"title": title, "source": source, "url": url, "reason": parts[3] if len(parts) > 3 else ""}
            all_links.append(item)
            mapping.setdefault(source, []).append((title, url))
            if current_group:
                grouped.setdefault(current_group, []).append(item)
    return mapping, grouped, all_links


def _merge_link_items(primary=None, extra=None):
    merged = []
    seen_urls = set()
    for item in list(primary or []) + list(extra or []):
        url = (item or {}).get("url") or ""
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        merged.append({
            "title": item.get("title") or "",
            "source": item.get("source") or "未知公众号",
            "url": url,
            "reason": item.get("reason") or "",
        })
    return merged


def _report_window_source_links(report: dict | None):
    if not report:
        return []
    window_start = (report.get("window_start") or "").strip()
    window_end = (report.get("window_end") or "").strip()
    created_at = (report.get("created_at") or "").strip()
    params = []
    where = []
    if window_start and window_end:
        where.append("a.discovered_at >= ? AND a.discovered_at <= ?")
        params.extend([window_start, window_end])
    elif created_at:
        where.append("a.discovered_at <= ?")
        params.append(created_at)
    else:
        return []
    sql = (
        "SELECT a.title, COALESCE(t.account_name, '未知公众号') AS source, a.article_url AS url "
        "FROM articles a LEFT JOIN targets t ON a.target_id = t.id "
        f"WHERE {' AND '.join(where)} "
        "ORDER BY a.discovered_at DESC, a.id DESC LIMIT 800"
    )
    with db.connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    items = []
    for row in rows:
        url = (row["url"] or "").strip()
        if not url:
            continue
        items.append({
            "title": (row["title"] or "").strip(),
            "source": (row["source"] or "未知公众号").strip(),
            "url": url,
            "reason": "report-window-source",
        })
    return _merge_link_items(items)


def _match_terms(text: str):
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


def _plain_signal_text(text: str):
    raw = html.unescape(text or "")
    match = re.search(r"\*\*(.*?)\*\*", raw)
    topic = match.group(1).strip() if match else ""
    plain = re.sub(r"\*\*(.*?)\*\*", r"\1", raw)
    plain = re.sub(r"【[^】]+】", "", plain)
    plain = re.sub(r"https?://\S+", "", plain)
    return topic.strip(), plain.strip()


def _title_signal_link_score(item: dict, topic: str, context: str):
    title = item.get("title") or ""
    title_norm = re.sub(r"\s+", "", title.lower())
    topic_norm = re.sub(r"\s+", "", (topic or "").lower())
    context_norm = re.sub(r"\s+", "", (context or "").lower())
    score = 0.0
    if title_norm and title_norm in context_norm:
        score += 22
    if topic_norm and title_norm:
        if topic_norm in title_norm or title_norm in topic_norm:
            score += 18
        score += SequenceMatcher(None, topic_norm[:80], title_norm[:80]).ratio() * 12
    if context_norm and title_norm:
        score += SequenceMatcher(None, context_norm[:160], title_norm[:120]).ratio() * 6
    title_terms = _match_terms(title)
    score += min(12, len(_match_terms(topic) & title_terms) * 2.5)
    score += min(8, len(_match_terms(context) & title_terms) * 1.2)
    return score


def _score_signal_link(item: dict, topic: str, context: str):
    source = item.get("source") or ""
    source_norm = re.sub(r"\s+", "", source.lower())
    context_norm = re.sub(r"\s+", "", (context or "").lower())
    score = _title_signal_link_score(item, topic, context)
    if source_norm and source_norm in context_norm:
        score += 16
    return score


def _pick_signal_links(text: str, kind: str, group_links=None, global_links=None, limit_override=None):
    topic, context = _plain_signal_text(text)
    candidates = list(group_links or [])
    fallback = list(global_links or [])

    def ranked(items):
        scored = []
        for idx, item in enumerate(items):
            scored.append((_score_signal_link(item, topic, context), idx, item))
        return sorted(scored, key=lambda x: (-x[0], x[1]))

    def choose(items):
        picked = []
        seen_urls = set()
        limit = int(limit_override or (1 if kind == "单点信号" else 3))
        best_score = items[0][0] if items else 0
        min_score = max(8, best_score * 0.58) if kind == "多源共识" else max(8, best_score * 0.45)
        for score, _, item in items:
            if score < min_score:
                continue
            if kind == "多源共识" and picked and _title_signal_link_score(item, topic, context) < 8:
                continue
            url = item.get("url")
            if not url or url in seen_urls:
                continue
            picked.append(item)
            seen_urls.add(url)
            if len(picked) >= limit:
                break
        if not picked and items:
            picked.append(items[0][2])
        return picked

    picked = choose(ranked(candidates)) if candidates else []
    if not picked and fallback:
        picked = choose(ranked(fallback))
    return picked


def _pick_source_article_link(article_title: str, source_name: str, global_links=None):
    candidates = [
        item for item in (global_links or [])
        if _source_name_matches(item.get("source") or "", source_name)
    ]
    if not candidates:
        return None
    title_norm = re.sub(r"\s+", "", (article_title or "").lower())
    scored = []
    for idx, item in enumerate(candidates):
        item_title = item.get("title") or ""
        item_title_norm = re.sub(r"\s+", "", item_title.lower())
        score = 0.0
        if title_norm and item_title_norm:
            if title_norm == item_title_norm:
                score += 40
            if title_norm in item_title_norm or item_title_norm in title_norm:
                score += 24
            score += SequenceMatcher(None, title_norm[:120], item_title_norm[:120]).ratio() * 20
            score += min(12, len(_match_terms(article_title) & _match_terms(item_title)) * 2.0)
        scored.append((score, idx, item))
    scored.sort(key=lambda x: (-x[0], x[1]))
    best_score, _, best_item = scored[0]
    if best_score < 8:
        return None
    return best_item


def _linkify_github_source_article(item_html: str, global_links=None):
    pattern = r"来源文章：(.+?)（([^（）]+)）(?=$|\s*<|\s*$)"
    def repl(match):
        article_title = (match.group(1) or "").strip()
        source_name = (match.group(2) or "").strip()
        picked = _pick_source_article_link(article_title, source_name, global_links)
        if not picked or not picked.get("url"):
            return match.group(0)
        url = html.escape(picked["url"])
        title = html.escape(article_title)
        source = html.escape(source_name)
        return f"来源文章：<a class='source-link' href='{url}' target='_blank' rel='noopener'>{title}</a>（{source}）"
    return re.sub(pattern, repl, item_html)


def _report_html(md: str, report: dict | None = None):
    source_links, grouped_links, all_links = _extract_source_links(md)
    report_window_links = _report_window_source_links(report)
    global_links = _merge_link_items(all_links, report_window_links)
    text = html.escape(md or "")
    lines = text.splitlines()
    out = []
    in_list = False
    in_appendix = False
    current_group = None
    for line in lines:
        s = line.strip()
        if not s:
            if in_list:
                out.append("</ul>")
                in_list = False
            continue
        if s.startswith("## "):
            if in_list:
                out.append("</ul>")
                in_list = False
            heading = s[3:]
            slug = _slug(heading)
            if in_appendix:
                out.append("</details>")
                in_appendix = False
            if '链接附录' in s:
                out.append('<details id="appendix" class="card" style="margin-top:18px;"><summary style="cursor:pointer;font-weight:700;">全部来源索引（点击展开）</summary>')
                in_appendix = True
                current_group = None
            else:
                current_group = heading if heading in grouped_links else None
                out.append(f"<h2 id='sec-{slug}'>{heading}</h2>")
        elif s.startswith("### "):
            if in_list:
                out.append("</ul>")
                in_list = False
            subheading = s[4:]
            normalized_subheading = _normalize_heading(subheading)
            if normalized_subheading in grouped_links:
                current_group = normalized_subheading
            out.append(f"<h3>{subheading}</h3>")
        elif s.startswith("- ") or re.match(r"^\d+[\.、]\s+", s):
            if not in_list:
                out.append('<ul class="report-list">')
                in_list = True
            item = s[2:] if s.startswith("- ") else re.sub(r"^\d+[\.、]\s+", "", s)
            item = re.sub(r"\*\*(.*?)\*\*", r"<strong>\1</strong>", item)
            if ' | http' in item or ' | https' in item:
                parts = item.split(' | ')
                if len(parts) >= 4 and parts[2].startswith('http'):
                    item = (
                        f"<a class='report-link-title source-link' href='{parts[2]}' target='_blank' rel='noopener'>{parts[0]}</a>"
                        f" <span class='muted'>| {parts[1]}</span> "
                        f"<a class='source-link raw-link' href='{parts[2]}' target='_blank' rel='noopener'>链接</a> "
                        f"<span class='badge'>{parts[3]}</span>"
                    )
            else:
                item = re.sub(r'(https?://[^\s<]+)', r"<a class='source-link' href='\1' target='_blank' rel='noopener'>\1</a>", item)
                item = _linkify_github_source_article(item, global_links)
                item = _signal_badge_replace(item, source_links, grouped_links.get(current_group, []), global_links)
            out.append(f"<li>{item}</li>")
        else:
            if in_list:
                out.append("</ul>")
                in_list = False
            para = re.sub(r"\*\*(.*?)\*\*", r"<strong>\1</strong>", s)
            para = re.sub(r'(https?://[^\s<]+)', r"<a class='source-link' href='\1' target='_blank' rel='noopener'>查看链接</a>", para)
            para = _signal_badge_replace(para, source_links, grouped_links.get(current_group, []), all_links)
            out.append(f"<p>{para}</p>")
    if in_list:
        out.append("</ul>")
    if in_appendix:
        out.append("</details>")
    return "\n".join(out)


def _report_summary(md: str):
    lines = [l.strip() for l in (md or "").splitlines() if l.strip()]
    bullets = []
    for line in lines:
        if not line.startswith("- "):
            continue
        text = re.sub(r"\*\*(.*?)\*\*", r"\1", line[2:]).strip()
        text = re.sub(r"\s+", " ", text)
        if not text or text == "---":
            continue
        bullets.append(text)
    return bullets[:4]


def _report_tldr(md: str):
    text = md or ""
    chunks = [c.strip() for c in re.split(r"\n\n+", text) if c.strip()]
    picked = []
    for c in chunks:
        if c.startswith("##") or c.startswith("###"):
            continue
        if c.strip() == "---":
            continue
        c = re.sub(r"【[^】]+】", "", c).strip()
        c = re.sub(r"\*\*(.*?)\*\*", r"\1", c)
        c = re.sub(r"(^|\n)-\s*", " ", c).strip()
        c = re.sub(r"\s+", " ", c)
        if len(c) > 120:
            c = c[:120] + "..."
        if c and c not in picked:
            picked.append(c)
        if len(picked) >= 4:
            break
    return picked


def _normalize_heading(text: str):
    text = (text or "").strip()
    text = re.sub(r'^\d+[\.、]\s*', '', text)
    return text.strip()


def _slug(text: str):
    return re.sub(r'[^a-zA-Z0-9\u4e00-\u9fff]+', '-', _normalize_heading(text)).strip('-').lower()


def _report_sections(md: str):
    items = []
    for line in (md or "").splitlines():
        s = line.strip()
        if s.startswith("## "):
            heading = s[3:]
            items.append({"title": heading, "anchor": "appendix" if "链接附录" in heading else f"sec-{_slug(heading)}"})
    return items


def _summary_html(text: str):
    s = html.escape(text or "")
    return re.sub(r'(https?://[^\s<]+)', r"<a class='source-link' href='\1' target='_blank' rel='noopener'>查看链接</a>", s)


def _line_chart(points, value_keys, width=640, height=180, padding=24):
    if not points:
        return {"width": width, "height": height, "paths": [], "dots": [], "labels": [], "y_ticks": [], "max_val": 1}
    vals = []
    for p in points:
        for k in value_keys:
            vals.append(float(p.get(k) or 0))
    max_val = max(vals) if vals else 1
    max_val = max(max_val, 1)
    step_x = (width - padding * 2) / max(1, len(points) - 1)
    def xy(i, value):
        x = padding + step_x * i
        y = height - padding - ((float(value or 0) / max_val) * (height - padding * 2))
        return x, y
    paths = []
    dots = []
    colors = ["#2563eb", "#7c3aed", "#dc2626"]
    series_labels = {"c": "文章数", "done_c": "Done", "failed_c": "Failed", "follow_c": "关注", "ignore_c": "忽略", "neutral_c": "中性"}
    for idx, key in enumerate(value_keys):
        coords = [xy(i, p.get(key) or 0) for i, p in enumerate(points)]
        path = " ".join([("M" if i == 0 else "L") + f" {x:.1f} {y:.1f}" for i, (x, y) in enumerate(coords)])
        paths.append({"key": key, "d": path, "color": colors[idx % len(colors)]})
        for i, (x, y) in enumerate(coords):
            dots.append({
                "x": x,
                "y": y,
                "color": colors[idx % len(colors)],
                "value": points[i].get(key) or 0,
                "key": key,
                "series_label": series_labels.get(key, key),
                "date_label": str(points[i].get("d", "")),
                "value_y": y - 10 if idx % 2 == 0 else y + 16,
            })
    labels = []
    for i, p in enumerate(points):
        x, _ = xy(i, 0)
        labels.append({"x": x, "text": str(p.get("d", ""))[5:]})
    y_ticks = []
    for frac in [1.0, 0.5, 0.0]:
        value = round(max_val * frac)
        y = height - padding - ((value / max_val) * (height - padding * 2)) if max_val else height - padding
        y_ticks.append({"y": y, "value": value})
    return {"width": width, "height": height, "paths": paths, "dots": dots, "labels": labels, "y_ticks": y_ticks, "max_val": max_val}


def _page_params(request: Request):
    try:
        page = int(request.query_params.get("page", "1"))
    except Exception:
        page = 1
    if page < 1:
        page = 1
    try:
        page_size = int(request.query_params.get("page_size", str(PAGE_SIZE)))
    except Exception:
        page_size = PAGE_SIZE
    if page_size not in PAGE_SIZE_OPTIONS:
        page_size = PAGE_SIZE
    return page, page_size, (page - 1) * page_size


def _pager_dict(page: int, page_size: int, total: int):
    total_pages = max(1, ceil(total / page_size)) if page_size > 0 else 1
    return {
        "page": page,
        "page_size": page_size,
        "total": total,
        "total_pages": total_pages,
        "has_prev": page > 1,
        "has_next": page < total_pages,
    }


def _targets_filter_params(request: Request):
    query = (request.query_params.get("q", "") or "").strip()
    enabled_raw = (request.query_params.get("enabled", "") or "").strip()
    enabled = enabled_raw if enabled_raw in {"", "0", "1"} else ""
    priority_min_raw = (request.query_params.get("priority_min", "") or "").strip()
    priority_max_raw = (request.query_params.get("priority_max", "") or "").strip()

    priority_min = None if priority_min_raw == "" else _safe_int(priority_min_raw, None)
    priority_max = None if priority_max_raw == "" else _safe_int(priority_max_raw, None)

    filters = {
        "q": query,
        "enabled": enabled,
        "priority_min": "" if priority_min is None else str(priority_min),
        "priority_max": "" if priority_max is None else str(priority_max),
    }

    where_clauses = []
    params = []
    if query:
        like = f"%{query}%"
        where_clauses.append(
            "(account_name LIKE ? OR keyword LIKE ? OR COALESCE(resolved_nickname,'') LIKE ? OR COALESCE(resolved_alias,'') LIKE ?)"
        )
        params.extend([like, like, like, like])
    if enabled != "":
        where_clauses.append("enabled=?")
        params.append(int(enabled))
    if priority_min is not None:
        where_clauses.append("priority>=?")
        params.append(priority_min)
    if priority_max is not None:
        where_clauses.append("priority<=?")
        params.append(priority_max)

    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
    pager_query = urlencode({
        "page_size": request.query_params.get("page_size", str(PAGE_SIZE)),
        **{k: v for k, v in filters.items() if v != ""},
    })
    active_filter_count = sum(1 for v in filters.values() if v != "")
    return filters, where_sql, params, pager_query, active_filter_count


def _human_dt_to_ts(value: str):
    value = (value or "").strip()
    if not value:
        return ""
    if value.isdigit():
        return value
    for fmt in ["%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d"]:
        try:
            return str(int(datetime.strptime(value, fmt).timestamp()))
        except Exception:
            continue
    return ""


def _dashboard_alert(level: str, title: str, message: str, action_url: str = "", action_label: str = "", impact: str = ""):
    tone = {"high": "bad", "medium": "warn", "low": ""}.get(level, "")
    level_label = {"high": "高", "medium": "中", "low": "低"}.get(level, level)
    return {
        "level": level,
        "level_label": level_label,
        "tone": tone,
        "title": title,
        "message": message,
        "action_url": action_url,
        "action_label": action_label,
        "impact": impact,
    }


def _dashboard_task_funnel(hours: int = 24, limit: int = 400):
    since_dt = datetime.now() - timedelta(hours=hours)
    since_str = since_dt.strftime("%Y-%m-%d %H:%M:%S")
    with db.connect() as conn:
        task_rows = conn.execute(
            """
            SELECT t.*, tg.account_name
            FROM tasks t
            LEFT JOIN targets tg ON t.target_id=tg.id
            WHERE t.created_at >= ?
            ORDER BY t.id DESC
            LIMIT ?
            """,
            (since_str, limit),
        ).fetchall()
        articles_done = conn.execute(
            "SELECT COUNT(*) c FROM articles WHERE discovered_at >= ? AND fetch_status='done'",
            (since_str,),
        ).fetchone()["c"]
        articles_failed = conn.execute(
            "SELECT COUNT(*) c FROM articles WHERE discovered_at >= ? AND fetch_status='failed'",
            (since_str,),
        ).fetchone()["c"]

    agg = {
        "since_label": f"最近 {hours} 小时",
        "tasks_total": len(task_rows),
        "tasks_done": 0,
        "tasks_failed": 0,
        "tasks_pending": 0,
        "tasks_with_stats": 0,
        "tasks_without_stats": 0,
        "stale_pending": 0,
        "worker_raw_count": 0,
        "worker_reported_count": 0,
        "worker_filtered_time_count": 0,
        "worker_duplicate_count": 0,
        "worker_missing_publish_count": 0,
        "server_inserted_count": 0,
        "server_existing_url_skip_count": 0,
        "server_publish_time_skip_count": 0,
        "server_missing_publish_time_skip_count": 0,
        "retry_after_refresh_count": 0,
        "invalid_session_failures": 0,
        "articles_fetch_done": _safe_int(articles_done),
        "articles_fetch_failed": _safe_int(articles_failed),
        "drop_breakdown": [],
        "stages": [],
        "notes": [],
    }

    now = datetime.now()
    for row in task_rows:
        item = dict(row)
        status = (item.get("status") or "").strip().lower()
        if status == "done":
            agg["tasks_done"] += 1
        elif status == "failed":
            agg["tasks_failed"] += 1
        else:
            agg["tasks_pending"] += 1
            created_dt = _parse_dt(item.get("created_at"))
            if created_dt and (now - created_dt).total_seconds() > 600:
                agg["stale_pending"] += 1

        if "invalid session" in str(item.get("error_message") or "").lower():
            agg["invalid_session_failures"] += 1

        stats = _parse_json_object(item.get("task_stats_json"))
        if not stats:
            agg["tasks_without_stats"] += 1
            continue
        agg["tasks_with_stats"] += 1
        agg["worker_raw_count"] += _safe_int(stats.get("raw_item_count"))
        agg["worker_reported_count"] += _safe_int(stats.get("accepted_item_count") or stats.get("reported_item_count"))
        agg["worker_filtered_time_count"] += _safe_int(stats.get("filtered_by_min_publish_time"))
        agg["worker_duplicate_count"] += _safe_int(stats.get("duplicate_count"))
        agg["worker_missing_publish_count"] += _safe_int(stats.get("skipped_missing_publish_time"))
        agg["server_inserted_count"] += _safe_int(stats.get("server_inserted_count"))
        agg["server_existing_url_skip_count"] += _safe_int(stats.get("server_existing_url_skip_count"))
        agg["server_publish_time_skip_count"] += _safe_int(stats.get("server_publish_time_skip_count"))
        agg["server_missing_publish_time_skip_count"] += _safe_int(stats.get("server_missing_publish_time_skip_count"))
        if stats.get("retry_after_refresh"):
            agg["retry_after_refresh_count"] += 1

    coverage = round((agg["tasks_with_stats"] / max(1, agg["tasks_total"])) * 100) if agg["tasks_total"] else 0
    agg["stats_coverage_pct"] = coverage
    if agg["tasks_with_stats"] == 0:
        agg["notes"].append("最近任务还没有写入详细 task_stats_json，漏斗先展示为基础运行视角。")
    elif agg["tasks_without_stats"]:
        agg["notes"].append(f"最近任务里有 {agg['tasks_without_stats']} 个没有详细 stats，漏斗统计是部分覆盖。")
    if agg["retry_after_refresh_count"]:
        agg["notes"].append(f"有 {agg['retry_after_refresh_count']} 个任务发生过异常后重刷。")
    if agg["invalid_session_failures"]:
        agg["notes"].append(f"检测到 {agg['invalid_session_failures']} 个 invalid session 失败任务。")

    agg["drop_breakdown"] = [
        {"label": "worker 时间过滤", "value": agg["worker_filtered_time_count"], "tone": "warn"},
        {"label": "worker 去重", "value": agg["worker_duplicate_count"], "tone": "warn"},
        {"label": "worker 缺发布时间", "value": agg["worker_missing_publish_count"], "tone": "warn"},
        {"label": "URL 已存在", "value": agg["server_existing_url_skip_count"], "tone": ""},
        {"label": "服务端发布时间压制", "value": agg["server_publish_time_skip_count"], "tone": "warn"},
        {"label": "服务端缺发布时间", "value": agg["server_missing_publish_time_skip_count"], "tone": "warn"},
    ]
    agg["drop_breakdown"] = [item for item in agg["drop_breakdown"] if item["value"] > 0]
    agg["stages"] = [
        {"label": "任务下发", "value": agg["tasks_total"], "sub": f"done {agg['tasks_done']} / failed {agg['tasks_failed']}"},
        {"label": "worker 原始发现", "value": agg["worker_raw_count"], "sub": f"stats 覆盖 {coverage}%"},
        {"label": "worker 上报", "value": agg["worker_reported_count"], "sub": "进入服务端判重"},
        {"label": "服务端入库", "value": agg["server_inserted_count"], "sub": "通过 URL + publish_time 边界"},
        {"label": "正文抓取完成", "value": agg["articles_fetch_done"], "sub": f"抓取失败 {agg['articles_fetch_failed']}"},
    ]
    return agg


def _dashboard_zero_insert_summary(hours: int = 24, limit: int = 6):
    since_dt = datetime.now() - timedelta(hours=hours)
    since_str = since_dt.strftime("%Y-%m-%d %H:%M:%S")
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT t.*, tg.account_name
            FROM tasks t
            LEFT JOIN targets tg ON t.target_id=tg.id
            WHERE t.status='done' AND t.created_at >= ?
            ORDER BY t.id DESC
            LIMIT 300
            """,
            (since_str,),
        ).fetchall()

    reason_counts = {
        "URL 已存在": 0,
        "发布时间压制": 0,
        "缺发布时间": 0,
        "worker 0 上报": 0,
        "无详细 stats": 0,
    }
    examples = []
    zero_insert_count = 0
    for row in rows:
        item = dict(row)
        stats = _parse_json_object(item.get("task_stats_json"))
        if not stats:
            reason_counts["无详细 stats"] += 1
            continue
        inserted = _safe_int(stats.get("server_inserted_count"))
        if inserted > 0:
            continue
        zero_insert_count += 1
        reasons = []
        if _safe_int(stats.get("server_existing_url_skip_count")) > 0:
            reason_counts["URL 已存在"] += 1
            reasons.append("URL 已存在")
        if _safe_int(stats.get("server_publish_time_skip_count")) > 0:
            reason_counts["发布时间压制"] += 1
            reasons.append("发布时间压制")
        if _safe_int(stats.get("server_missing_publish_time_skip_count")) > 0:
            reason_counts["缺发布时间"] += 1
            reasons.append("缺发布时间")
        reported = _safe_int(stats.get("accepted_item_count") or stats.get("reported_item_count"))
        if reported == 0:
            reason_counts["worker 0 上报"] += 1
            reasons.append("worker 0 上报")
        if not reasons:
            reasons.append("入库结果为 0")
        if len(examples) < limit:
            examples.append({
                "account_name": item.get("account_name") or "未知公众号",
                "task_id": item.get("task_id") or "",
                "created_at": item.get("created_at") or "",
                "reasons": reasons[:3],
                "task_url": f"/tasks?account_name={quote((item.get('account_name') or ''), safe='')}",
            })

    reason_items = [
        {"label": label, "value": value, "tone": "warn" if label != "URL 已存在" else ""}
        for label, value in reason_counts.items()
        if value > 0
    ]
    reason_items.sort(key=lambda x: (-x["value"], x["label"]))
    return {
        "since_label": f"最近 {hours} 小时",
        "done_tasks": len(rows),
        "zero_insert_count": zero_insert_count,
        "reason_items": reason_items,
        "examples": examples,
        "unknown_without_stats": reason_counts["无详细 stats"],
    }


def _dashboard_report_cadence():
    state = _load_report_state()
    with db.connect() as conn:
        morning_report = conn.execute("SELECT * FROM reports WHERE report_type='morning' ORDER BY id DESC LIMIT 1").fetchone()
        evening_report = conn.execute("SELECT * FROM reports WHERE report_type='evening' ORDER BY id DESC LIMIT 1").fetchone()

        def _window_counts(start_at: str | None):
            start = (start_at or "1970-01-01 00:00:00").strip() or "1970-01-01 00:00:00"
            row = conn.execute(
                """
                SELECT
                    COUNT(*) c,
                    SUM(CASE WHEN fetch_status='done' THEN 1 ELSE 0 END) done_c,
                    SUM(CASE WHEN COALESCE(user_pref_state, '') = '' THEN 1 ELSE 0 END) unset_c
                FROM articles
                WHERE discovered_at > ?
                """,
                (start,),
            ).fetchone()
            return {
                "candidate_count": _safe_int(row["c"]),
                "ready_count": _safe_int(row["done_c"]),
                "unset_count": _safe_int(row["unset_c"]),
            }

        morning_window = _window_counts(state.get("last_evening_report_at"))
        evening_window = _window_counts(state.get("last_morning_report_at"))

    def _type_card(report_type: str, start_key: str, state_key: str, report_row):
        state_time = state.get(state_key)
        window_counts = morning_window if report_type == "morning" else evening_window
        report_end = report_row["window_end"] if report_row else None
        state_ahead = bool(state_time and ((not report_end) or state_time > report_end))
        return {
            "report_type": report_type,
            "label": "早报" if report_type == "morning" else "晚报",
            "next_start_at": state.get(start_key) or "1970-01-01 00:00:00",
            "last_state_at": state_time or "未推进",
            "last_report_at": report_row["created_at"] if report_row else "暂无记录",
            "last_report_window_end": report_end or "",
            "state_ahead": state_ahead,
            "state_status_label": "最近一次是空窗推进" if state_ahead else "最近一次已落 report",
            "state_badge": "warn" if state_ahead else "good",
            **window_counts,
        }

    morning = _type_card("morning", "last_evening_report_at", "last_morning_report_at", dict(morning_report) if morning_report else None)
    evening = _type_card("evening", "last_morning_report_at", "last_evening_report_at", dict(evening_report) if evening_report else None)
    return {
        "morning": morning,
        "evening": evening,
        "state": state,
    }


def _dashboard_target_watchlist(days: int = 7, limit: int = 6):
    since_dt = datetime.now() - timedelta(days=days)
    since_str = since_dt.strftime("%Y-%m-%d %H:%M:%S")
    with db.connect() as conn:
        targets = conn.execute(
            "SELECT * FROM targets WHERE enabled=1 ORDER BY priority DESC, id ASC"
        ).fetchall()
        task_rows = conn.execute(
            "SELECT * FROM tasks WHERE created_at >= ? ORDER BY id DESC LIMIT 600",
            (since_str,),
        ).fetchall()

    by_target = {}
    for row in targets:
        item = dict(row)
        by_target[item["id"]] = {
            "target_id": item["id"],
            "account_name": item.get("account_name") or "未知公众号",
            "priority": _safe_int(item.get("priority"), 10),
            "check_interval_minutes": _safe_int(item.get("check_interval_minutes"), 180),
            "last_dispatched_at": item.get("last_dispatched_at") or "",
            "resolved_fakeid": item.get("resolved_fakeid") or "",
            "done_count": 0,
            "failed_count": 0,
            "pending_count": 0,
            "zero_insert_count": 0,
            "score": 0.0,
            "reasons": [],
        }

    for row in task_rows:
        item = dict(row)
        bucket = by_target.get(item.get("target_id"))
        if not bucket:
            continue
        status = (item.get("status") or "").strip().lower()
        if status == "done":
            bucket["done_count"] += 1
        elif status == "failed":
            bucket["failed_count"] += 1
        else:
            bucket["pending_count"] += 1
        stats = _parse_json_object(item.get("task_stats_json"))
        if status == "done" and stats and _safe_int(stats.get("server_inserted_count")) == 0:
            bucket["zero_insert_count"] += 1

    now = datetime.now()
    watch = []
    unresolved_count = 0
    weak_count = 0
    for bucket in by_target.values():
        if not bucket["resolved_fakeid"]:
            unresolved_count += 1
            bucket["score"] += 2.4
            bucket["reasons"].append("未解析 fakeid")
        if not bucket["last_dispatched_at"]:
            bucket["score"] += 3.8
            bucket["reasons"].append("从未下发任务")
        else:
            last_dt = _parse_dt(bucket["last_dispatched_at"])
            threshold_minutes = max(360, bucket["check_interval_minutes"] * 4)
            if last_dt and (now - last_dt).total_seconds() > threshold_minutes * 60:
                bucket["score"] += 1.4
                bucket["reasons"].append("最近调度偏慢")
        if bucket["failed_count"] >= 2:
            bucket["score"] += 2.8
            bucket["reasons"].append(f"近 {days} 天失败 {bucket['failed_count']} 次")
        if bucket["zero_insert_count"] >= 2:
            bucket["score"] += 2.0
            bucket["reasons"].append(f"近 {days} 天 0 入库 {bucket['zero_insert_count']} 次")
        if bucket["pending_count"] >= 2:
            bucket["score"] += 1.4
            bucket["reasons"].append(f"仍有 {bucket['pending_count']} 个 pending")
        if bucket["score"] <= 0:
            continue
        weak_count += 1
        bucket["health_label"] = "优先检查" if bucket["score"] >= 4.5 else "建议留意"
        bucket["health_badge"] = "warn" if bucket["score"] >= 4.5 else ""
        bucket["task_url"] = f"/tasks?account_name={quote(bucket['account_name'], safe='')}"
        bucket["target_url"] = "/targets"
        watch.append(bucket)

    watch.sort(key=lambda x: (-float(x.get("score") or 0.0), -int(x.get("priority") or 0), x.get("account_name") or ""))
    return {
        "unresolved_count": unresolved_count,
        "weak_count": weak_count,
        "rows": watch[:limit],
    }
    if value.isdigit():
        return value
    for fmt in ["%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d"]:
        try:
            return str(int(datetime.strptime(value, fmt).timestamp()))
        except Exception:
            pass
    return value


@router.get("/admin/login", response_class=HTMLResponse)
def admin_login_page(request: Request):
    next_url = (request.query_params.get("next") or "/").strip() or "/"
    return templates.TemplateResponse("admin_login.html", {
        "request": request,
        "next_url": next_url,
        "configured": bool(_admin_password()),
        "error": "",
    })


@router.post("/admin/login", response_class=HTMLResponse)
def admin_login_submit(request: Request, password: str = Form(...), next_url: str = Form("/")):
    next_url = (next_url or "/").strip() or "/"
    configured = bool(_admin_password())
    if not configured:
        return templates.TemplateResponse("admin_login.html", {
            "request": request,
            "next_url": next_url,
            "configured": False,
            "error": "尚未配置 WECHAT_ADMIN_PASSWORD，当前无法启用管理页密码保护。",
        }, status_code=400)
    if password != _admin_password():
        return templates.TemplateResponse("admin_login.html", {
            "request": request,
            "next_url": next_url,
            "configured": True,
            "error": "管理员密码错误，请重试。",
        }, status_code=401)

    response = RedirectResponse(url=next_url, status_code=303)
    response.set_cookie(
        ADMIN_COOKIE,
        _admin_token(),
        max_age=7 * 24 * 3600,
        httponly=True,
        samesite="lax",
    )
    return response


@router.get("/admin/logout")
def admin_logout():
    response = RedirectResponse(url="/admin/login", status_code=303)
    response.delete_cookie(ADMIN_COOKIE)
    return response


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    guard = _require_admin(request)
    if guard:
        return guard
    profile = _load_article_preference_profile()
    report_cadence = _dashboard_report_cadence()
    task_funnel = _dashboard_task_funnel(hours=24)
    zero_insert_summary = _dashboard_zero_insert_summary(hours=24)
    target_watchlist = _dashboard_target_watchlist(days=7)
    with db.connect() as conn:
        enabled_targets = conn.execute("SELECT COUNT(*) c FROM targets WHERE enabled=1").fetchone()["c"]
        disabled_targets = conn.execute("SELECT COUNT(*) c FROM targets WHERE enabled=0").fetchone()["c"]
        resolved_targets = conn.execute("SELECT COUNT(*) c FROM targets WHERE resolved_fakeid IS NOT NULL AND resolved_fakeid != ''").fetchone()["c"]
        never_dispatched_targets = conn.execute("SELECT COUNT(*) c FROM targets WHERE last_dispatched_at IS NULL OR last_dispatched_at = ''").fetchone()["c"]

        workers_total = conn.execute("SELECT COUNT(*) c FROM workers").fetchone()["c"]
        workers_active_5m = conn.execute("SELECT COUNT(*) c FROM workers WHERE last_seen_at >= datetime('now', '-5 minutes')").fetchone()["c"]

        tasks_pending = conn.execute("SELECT COUNT(*) c FROM tasks WHERE status='pending'").fetchone()["c"]
        tasks_done = conn.execute("SELECT COUNT(*) c FROM tasks WHERE status='done'").fetchone()["c"]
        tasks_failed = conn.execute("SELECT COUNT(*) c FROM tasks WHERE status='failed'").fetchone()["c"]
        tasks_24h = conn.execute("SELECT COUNT(*) c FROM tasks WHERE created_at >= datetime('now', '-1 day')").fetchone()["c"]
        stale_pending_tasks = conn.execute("SELECT COUNT(*) c FROM tasks WHERE status='pending' AND created_at < datetime('now', '-10 minutes')").fetchone()["c"]

        articles_total = conn.execute("SELECT COUNT(*) c FROM articles").fetchone()["c"]
        articles_done = conn.execute("SELECT COUNT(*) c FROM articles WHERE fetch_status='done'").fetchone()["c"]
        articles_pending = conn.execute("SELECT COUNT(*) c FROM articles WHERE fetch_status='pending'").fetchone()["c"]
        articles_failed = conn.execute("SELECT COUNT(*) c FROM articles WHERE fetch_status='failed'").fetchone()["c"]
        articles_deleted = conn.execute("SELECT COUNT(*) c FROM articles WHERE fetch_status LIKE 'deleted%'").fetchone()["c"]
        articles_24h = conn.execute("SELECT COUNT(*) c FROM articles WHERE discovered_at >= datetime('now', '-1 day')").fetchone()["c"]
        articles_follow = conn.execute("SELECT COUNT(*) c FROM articles WHERE user_pref_state='follow'").fetchone()["c"]
        articles_ignore = conn.execute("SELECT COUNT(*) c FROM articles WHERE user_pref_state='ignore'").fetchone()["c"]
        articles_neutral = conn.execute("SELECT COUNT(*) c FROM articles WHERE user_pref_state='neutral'").fetchone()["c"]
        articles_unset = conn.execute("SELECT COUNT(*) c FROM articles WHERE COALESCE(user_pref_state, '') = ''").fetchone()["c"]
        marked_24h = conn.execute("SELECT COUNT(*) c FROM articles WHERE user_pref_updated_at >= datetime('now', '-1 day')").fetchone()["c"]
        recent_reports = conn.execute("SELECT * FROM reports ORDER BY id DESC LIMIT 3").fetchall()
        article_trend = conn.execute("SELECT substr(discovered_at,1,10) d, COUNT(*) c FROM articles GROUP BY substr(discovered_at,1,10) ORDER BY d DESC LIMIT 7").fetchall()
        task_trend = conn.execute("SELECT substr(created_at,1,10) d, SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) done_c, SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) failed_c FROM tasks GROUP BY substr(created_at,1,10) ORDER BY d DESC LIMIT 7").fetchall()
        pref_trend = conn.execute(
            """
            SELECT substr(user_pref_updated_at,1,10) d,
                   SUM(CASE WHEN user_pref_state='follow' THEN 1 ELSE 0 END) follow_c,
                   SUM(CASE WHEN user_pref_state='ignore' THEN 1 ELSE 0 END) ignore_c,
                   SUM(CASE WHEN user_pref_state='neutral' THEN 1 ELSE 0 END) neutral_c
            FROM articles
            WHERE user_pref_updated_at IS NOT NULL AND user_pref_updated_at != ''
            GROUP BY substr(user_pref_updated_at,1,10)
            ORDER BY d DESC
            LIMIT 7
            """
        ).fetchall()
        recent_labeled_articles = conn.execute(
            """
            SELECT a.id, a.title, a.article_url, a.user_pref_state, a.user_pref_updated_at, tg.account_name
            FROM articles a
            LEFT JOIN targets tg ON a.target_id=tg.id
            WHERE a.user_pref_state IN ('follow', 'ignore', 'neutral')
            ORDER BY a.user_pref_updated_at DESC, a.id DESC
            LIMIT 6
            """
        ).fetchall()
        recent_failed_articles = conn.execute("SELECT a.id, a.title, a.fetch_status, a.fetch_error, tg.account_name FROM articles a LEFT JOIN targets tg ON a.target_id=tg.id WHERE a.fetch_status='failed' ORDER BY a.id DESC LIMIT 5").fetchall()
        recent_deleted_articles = conn.execute("SELECT a.id, a.title, a.fetch_status, a.fetch_error, tg.account_name FROM articles a LEFT JOIN targets tg ON a.target_id=tg.id WHERE a.fetch_status LIKE 'deleted%' ORDER BY a.id DESC LIMIT 5").fetchall()
        recent_failed_tasks = conn.execute("SELECT t.id, t.task_id, tg.account_name, t.error_message FROM tasks t LEFT JOIN targets tg ON t.target_id=tg.id WHERE t.status='failed' ORDER BY t.id DESC LIMIT 5").fetchall()

    recent_failed_articles = [dict(row) for row in recent_failed_articles]
    for item in recent_failed_articles:
        item["fetch_meta"] = _fetch_status_meta(item.get("fetch_status"), item.get("fetch_error"))
    recent_deleted_articles = [dict(row) for row in recent_deleted_articles]
    for item in recent_deleted_articles:
        item["fetch_meta"] = _fetch_status_meta(item.get("fetch_status"), item.get("fetch_error"))

    metrics = {
        "targets": {
            "enabled": enabled_targets,
            "disabled": disabled_targets,
            "resolved": resolved_targets,
            "unresolved": max(0, enabled_targets - resolved_targets),
            "never_dispatched": never_dispatched_targets,
        },
        "workers": {
            "total": workers_total,
            "active_5m": workers_active_5m,
        },
        "tasks": {
            "pending": tasks_pending,
            "done": tasks_done,
            "failed": tasks_failed,
            "last_24h": tasks_24h,
            "stale_pending": stale_pending_tasks,
        },
        "articles": {
            "total": articles_total,
            "done": articles_done,
            "pending": articles_pending,
            "failed": articles_failed,
            "deleted": articles_deleted,
            "last_24h": articles_24h,
            "follow": articles_follow,
            "ignore": articles_ignore,
            "neutral": articles_neutral,
            "unset": articles_unset,
            "marked_24h": marked_24h,
        },
        "preferences": {
            "profile_updated_at": profile.get("updated_at") or "未生成",
            "labeled": profile.get("labeled_count") or 0,
            "follow": profile.get("follow_count") or 0,
            "ignore": profile.get("ignore_count") or 0,
            "neutral": profile.get("neutral_count") or 0,
        }
    }

    recent_reports_enriched = []
    for r in recent_reports:
        item = dict(r)
        item["summary_bullets"] = _report_summary(r["report_md"])
        item["tldr"] = _report_tldr(r["report_md"])
        recent_reports_enriched.append(item)
    latest_report = recent_reports_enriched[0] if recent_reports_enriched else None
    latest_report_preference_hits = _latest_report_preference_hits(latest_report, profile, limit=8)
    latest_report_ai_rule_impacts = _latest_report_ai_rule_impacts(latest_report, profile, limit=8)
    latest_report_missed_candidates = _latest_report_missed_candidates(latest_report, profile, limit=8)
    priority_unlabeled_candidates = _priority_unlabeled_candidates(profile, limit=8)
    direction_preference_gaps = _direction_preference_gaps(profile, latest_report, limit=6)
    selection_comparisons = _dashboard_selection_comparisons(latest_report, profile, limit=4)
    direction_activity_overview = _direction_activity_overview(profile, limit=8)
    priority_today_queue = _priority_today_queue(profile, limit=10)
    review_queue = latest_report_missed_candidates[:6] if latest_report_missed_candidates else [
        item for item in priority_today_queue if (item.get("user_pref_state") or "").strip().lower() in {"follow", "ignore", "neutral"}
    ][:6]

    direction_quick_links = []
    for direction in direction_preference_gaps:
        name = direction.get("direction") or ""
        if not name:
            continue
        direction_quick_links.append({
            "direction": name,
            "unset_url": f"/articles?direction={quote(name, safe='')}&pref_state=unset&fetch_status=done",
            "all_url": f"/articles?direction={quote(name, safe='')}&fetch_status=done",
        })

    alerts = []
    if stale_pending_tasks > 0:
        alerts.append(_dashboard_alert(
            "high",
            "Pending 任务超过 10 分钟",
            f"当前有 {stale_pending_tasks} 个任务长时间未回报，优先排查 worker 卡住或请求异常。",
            "/tasks?status=pending",
            "看 Pending Tasks",
            f"影响 {stale_pending_tasks} 个任务",
        ))
    if zero_insert_summary.get("zero_insert_count", 0) > 0:
        alerts.append(_dashboard_alert(
            "medium",
            "Done 但 0 新增文章",
            f"最近 24 小时有 {zero_insert_summary['zero_insert_count']} 个 done 任务最终没有新文章入库，建议优先看 URL 重复、发布时间压制或 worker 0 上报。",
            "/tasks?status=done",
            "看 Done Tasks",
            f"Done 任务 {zero_insert_summary['done_tasks']} 个",
        ))
    if articles_failed > 0:
        alerts.append(_dashboard_alert(
            "medium",
            "正文抓取失败需要清理",
            f"当前有 {articles_failed} 篇文章抓取失败，建议按错误原因清理失败队列。",
            "/articles?fetch_status=failed",
            "看失败 Articles",
            f"影响 {articles_failed} 篇文章",
        ))
    if never_dispatched_targets > 0:
        alerts.append(_dashboard_alert(
            "medium",
            "存在从未下发过任务的目标源",
            f"有 {never_dispatched_targets} 个 target 从未调度，建议检查启用状态、调度间隔与 worker 供给。",
            "/targets",
            "看 Targets",
            f"影响 {never_dispatched_targets} 个 target",
        ))
    if latest_report_missed_candidates:
        alerts.append(_dashboard_alert(
            "medium",
            "日报外还有值得复核的候选",
            f"最新日报之外还有 {len(latest_report_missed_candidates)} 篇高信号候选，可能存在漏选。",
            f"/reports/{latest_report['id']}" if latest_report else "/reports",
            "对照最新日报",
            f"待复核 {len(latest_report_missed_candidates)} 篇",
        ))
    if direction_preference_gaps:
        weakest_gap = direction_preference_gaps[0]
        if weakest_gap.get("unset", 0) >= 3:
            alerts.append(_dashboard_alert(
                "low",
                "方向偏好覆盖偏弱",
                f"方向“{weakest_gap.get('direction')}”近 7 天仍有 {weakest_gap.get('unset')} 篇未标注，建议集中补标。",
                f"/articles?direction={quote(weakest_gap.get('direction') or '', safe='')}&pref_state=unset&fetch_status=done",
                "补这个方向",
                f"未标 {weakest_gap.get('unset')} 篇",
            ))
    health = {
        "worker_active_ratio": round((workers_active_5m / workers_total) * 100) if workers_total else 0,
        "task_success_ratio": round((tasks_done / max(1, tasks_done + tasks_failed)) * 100) if (tasks_done + tasks_failed) else 0,
        "article_done_ratio": round((articles_done / max(1, articles_total)) * 100) if articles_total else 0,
        "article_labeled_ratio": round(((articles_total - articles_unset) / max(1, articles_total)) * 100) if articles_total else 0,
    }

    hero_status = {
        "label": "需要关注" if alerts else "系统运行平稳",
        "badge": "warn" if alerts else "good",
        "detail": f"当前 {len(alerts)} 个告警 / 线索" if alerts else "暂无明显阻塞或异常漂移",
    }

    trend = {
        "articles": [dict(r) for r in reversed(article_trend)],
        "tasks": [dict(r) for r in reversed(task_trend)],
        "preferences": [dict(r) for r in reversed(pref_trend)],
    }
    charts = {
        "articles": _line_chart(trend["articles"], ["c"]),
        "tasks": _line_chart(trend["tasks"], ["done_c", "failed_c"]),
        "preferences": _line_chart(trend["preferences"], ["follow_c", "ignore_c", "neutral_c"]),
    }

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "metrics": metrics,
        "hero_status": hero_status,
        "alerts": alerts,
        "recent_reports": recent_reports_enriched,
        "latest_report": latest_report,
        "latest_report_preference_hits": latest_report_preference_hits,
        "latest_report_ai_rule_impacts": latest_report_ai_rule_impacts,
        "latest_report_missed_candidates": latest_report_missed_candidates,
        "selection_comparisons": selection_comparisons,
        "direction_activity_overview": direction_activity_overview,
        "priority_today_queue": priority_today_queue,
        "priority_unlabeled_candidates": priority_unlabeled_candidates,
        "review_queue": review_queue,
        "direction_preference_gaps": direction_preference_gaps,
        "direction_quick_links": direction_quick_links,
        "task_funnel": task_funnel,
        "zero_insert_summary": zero_insert_summary,
        "report_cadence": report_cadence,
        "target_watchlist": target_watchlist,
        "health": health,
        "trend": trend,
        "charts": charts,
        "profile": profile,
        "recent_labeled_articles": [dict(r) for r in recent_labeled_articles],
        "recent_failed_articles": [dict(r) for r in recent_failed_articles],
        "recent_failed_tasks": [dict(r) for r in recent_failed_tasks],
    })


@router.get("/targets", response_class=HTMLResponse)
def targets_page(request: Request):
    guard = _require_admin(request)
    if guard:
        return guard
    page, page_size, offset = _page_params(request)
    filters, where_sql, where_params, pager_query, active_filter_count = _targets_filter_params(request)
    with db.connect() as conn:
        total_all = conn.execute("SELECT COUNT(*) c FROM targets").fetchone()["c"]
        total = conn.execute(
            f"SELECT COUNT(*) c FROM targets {where_sql}",
            where_params,
        ).fetchone()["c"]
        force_pending_total = conn.execute("SELECT COUNT(*) c FROM targets WHERE force_full_sync_once=1").fetchone()["c"]
        force_pending_enabled = conn.execute("SELECT COUNT(*) c FROM targets WHERE force_full_sync_once=1 AND enabled=1").fetchone()["c"]
        rows = conn.execute(
            f"SELECT * FROM targets {where_sql} ORDER BY priority DESC, id ASC LIMIT ? OFFSET ?",
            [*where_params, page_size, offset]
        ).fetchall()
        target_items = [dict(r) for r in rows]
        target_ids = [item["id"] for item in target_items]
        latest_task_map = {}
        if target_ids:
            placeholders = ",".join("?" for _ in target_ids)
            task_rows = conn.execute(
                f"SELECT * FROM tasks WHERE target_id IN ({placeholders}) ORDER BY target_id ASC, id DESC",
                target_ids,
            ).fetchall()
            for row in task_rows:
                item = dict(row)
                target_id = item.get("target_id")
                if target_id in latest_task_map:
                    continue
                stats = _parse_json_object(item.get("task_stats_json"))
                item["stats"] = stats
                item["inserted_count"] = _safe_int(stats.get("server_inserted_count"))
                item["limit_hit"] = bool(stats.get("server_limit_hit"))
                item["max_items_limit"] = _safe_int(stats.get("server_max_items_limit"), 50)
                error_summary = (item.get("error_message") or "").strip()
                if len(error_summary) > 120:
                    error_summary = error_summary[:117] + "..."
                item["error_summary"] = error_summary
                latest_task_map[target_id] = item
        for item in target_items:
            latest_task = latest_task_map.get(item["id"])
            item["latest_task"] = latest_task
            item["task_url"] = f"/tasks?account_name={quote((item.get('account_name') or ''), safe='')}"
    ctx = {
        "request": request,
        "targets": target_items,
        "force_pending_total": force_pending_total,
        "force_pending_enabled": force_pending_enabled,
        "page_size_options": PAGE_SIZE_OPTIONS,
        "filters": filters,
        "pager_query": pager_query,
        "active_filter_count": active_filter_count,
        "total_all": total_all,
    }
    ctx.update(_pager_dict(page, page_size, total))
    return templates.TemplateResponse("targets.html", ctx)


@router.post("/targets/create")
def create_target(request: Request, account_name: str = Form(...), keyword: str = Form(...), check_interval_minutes: int = Form(180), priority: int = Form(10), enabled: int = Form(1)):
    guard = _require_admin(request)
    if guard:
        return guard
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO targets(account_name,keyword,enabled,priority,check_interval_minutes,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
            (account_name, keyword, enabled, priority, check_interval_minutes, now_str(), now_str())
        )
    return RedirectResponse(url="/targets", status_code=303)


@router.post("/targets/batch_create")
def batch_create_targets(
    request: Request,
    batch_text: str = Form(...),
    check_interval_minutes: int = Form(180),
    priority: int = Form(10),
    enabled: int = Form(1),
):
    guard = _require_admin(request)
    if guard:
        return guard
    raw_lines = [line.strip() for line in batch_text.splitlines() if line.strip()]
    seen = set()
    lines = []
    for line in raw_lines:
        if line in seen:
            continue
        seen.add(line)
        lines.append(line)

    with db.connect() as conn:
        existing_names = {r[0] for r in conn.execute("SELECT account_name FROM targets").fetchall()}
        existing_keywords = {r[0] for r in conn.execute("SELECT keyword FROM targets").fetchall()}
        for line in lines:
            account_name = line
            keyword = line
            if account_name in existing_names or keyword in existing_keywords:
                continue
            conn.execute(
                "INSERT INTO targets(account_name,keyword,enabled,priority,check_interval_minutes,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                (account_name, keyword, enabled, priority, check_interval_minutes, now_str(), now_str())
            )
            existing_names.add(account_name)
            existing_keywords.add(keyword)
    return RedirectResponse(url="/targets", status_code=303)


@router.post("/targets/update/{target_id}")
def update_target(request: Request, target_id: int, account_name: str = Form(...), keyword: str = Form(...), check_interval_minutes: int = Form(180), priority: int = Form(10), enabled: int = Form(1)):
    guard = _require_admin(request)
    if guard:
        return guard
    with db.connect() as conn:
        conn.execute(
            "UPDATE targets SET account_name=?, keyword=?, enabled=?, priority=?, check_interval_minutes=?, updated_at=? WHERE id=?",
            (account_name, keyword, enabled, priority, check_interval_minutes, now_str(), target_id)
        )
    return RedirectResponse(url="/targets", status_code=303)


@router.post("/targets/force_rerun/{target_id}")
def force_rerun_target(request: Request, target_id: int):
    guard = _require_admin(request)
    if guard:
        return guard
    now = now_str()
    with db.connect() as conn:
        conn.execute(
            "UPDATE targets SET force_full_sync_once=1, last_dispatched_at=NULL, updated_at=? WHERE id=?",
            (now, target_id),
        )
    return RedirectResponse(url="/targets", status_code=303)


@router.post("/targets/batch_force_rerun")
def batch_force_rerun_targets(request: Request, enabled_only: int = Form(1), selected_target_ids: str = Form("")):
    guard = _require_admin(request)
    if guard:
        return guard
    where_sql, where_params = _batch_targets_where(selected_target_ids, enabled_only)
    now = now_str()
    with db.connect() as conn:
        conn.execute(
            f"UPDATE targets SET force_full_sync_once=1, last_dispatched_at=NULL, updated_at=? {where_sql}",
            [now, *where_params],
        )
    return RedirectResponse(url="/targets", status_code=303)


@router.post("/targets/batch_cancel_force_rerun")
def batch_cancel_force_rerun_targets(request: Request, enabled_only: int = Form(1), selected_target_ids: str = Form("")):
    guard = _require_admin(request)
    if guard:
        return guard
    where_sql, where_params = _batch_targets_where(selected_target_ids, enabled_only)
    now = now_str()
    with db.connect() as conn:
        conn.execute(
            f"UPDATE targets SET force_full_sync_once=0, updated_at=? {where_sql}",
            [now, *where_params],
        )
    return RedirectResponse(url="/targets", status_code=303)


@router.post("/targets/batch_update_dispatch_time")
def batch_update_target_dispatch_time(
    request: Request,
    mode: str = Form("clear"),
    dispatch_at: str = Form(""),
    enabled_only: int = Form(1),
    selected_target_ids: str = Form(""),
):
    guard = _require_admin(request)
    if guard:
        return guard

    mode = (mode or "clear").strip().lower()
    dispatch_value = None
    if mode == "set":
        dt = _parse_form_datetime(dispatch_at)
        if not dt:
            raise HTTPException(status_code=400, detail="invalid dispatch_at")
        dispatch_value = dt.strftime("%Y-%m-%d %H:%M:%S")
    elif mode != "clear":
        raise HTTPException(status_code=400, detail="invalid mode")

    where_sql, where_params = _batch_targets_where(selected_target_ids, enabled_only)
    now = now_str()
    with db.connect() as conn:
        conn.execute(
            f"UPDATE targets SET last_dispatched_at=?, updated_at=? {where_sql}",
            [dispatch_value, now, *where_params],
        )
    return RedirectResponse(url="/targets", status_code=303)


@router.post("/targets/batch_update_interval")
def batch_update_target_interval(
    request: Request,
    check_interval_minutes: int = Form(...),
    enabled_only: int = Form(1),
    selected_target_ids: str = Form(""),
):
    guard = _require_admin(request)
    if guard:
        return guard

    interval = _safe_int(check_interval_minutes, 0)
    if interval <= 0:
        raise HTTPException(status_code=400, detail="invalid check_interval_minutes")

    where_sql, where_params = _batch_targets_where(selected_target_ids, enabled_only)
    now = now_str()
    with db.connect() as conn:
        conn.execute(
            f"UPDATE targets SET check_interval_minutes=?, updated_at=? {where_sql}",
            [interval, now, *where_params],
        )
    return RedirectResponse(url="/targets", status_code=303)


@router.post("/targets/batch_update_priority")
def batch_update_target_priority(
    request: Request,
    priority: int = Form(...),
    enabled_only: int = Form(1),
    selected_target_ids: str = Form(""),
):
    guard = _require_admin(request)
    if guard:
        return guard

    priority_value = _safe_int(priority, -1)
    if priority_value < 0:
        raise HTTPException(status_code=400, detail="invalid priority")

    where_sql, where_params = _batch_targets_where(selected_target_ids, enabled_only)
    now = now_str()
    with db.connect() as conn:
        conn.execute(
            f"UPDATE targets SET priority=?, updated_at=? {where_sql}",
            [priority_value, now, *where_params],
        )
    return RedirectResponse(url="/targets", status_code=303)


@router.post("/targets/batch_update_enabled")
def batch_update_target_enabled(
    request: Request,
    enabled: int = Form(...),
    selected_target_ids: str = Form(""),
):
    guard = _require_admin(request)
    if guard:
        return guard

    enabled_value = 1 if _safe_int(enabled, 0) == 1 else 0
    where_sql, where_params = _batch_targets_where(selected_target_ids, None)
    now = now_str()
    with db.connect() as conn:
        conn.execute(
            f"UPDATE targets SET enabled=?, updated_at=? {where_sql}",
            [enabled_value, now, *where_params],
        )
    return RedirectResponse(url="/targets", status_code=303)


@router.post("/targets/delete/{target_id}")
def delete_target(request: Request, target_id: int):
    guard = _require_admin(request)
    if guard:
        return guard
    with db.connect() as conn:
        conn.execute("DELETE FROM targets WHERE id=?", (target_id,))
    return RedirectResponse(url="/targets", status_code=303)


@router.get("/workers", response_class=HTMLResponse)
def workers_page(request: Request):
    guard = _require_admin(request)
    if guard:
        return guard
    page, page_size, offset = _page_params(request)
    with db.connect() as conn:
        total = conn.execute("SELECT COUNT(*) c FROM workers").fetchone()["c"]
        rows = conn.execute(
            "SELECT * FROM workers ORDER BY last_seen_at DESC LIMIT ? OFFSET ?",
            (page_size, offset)
        ).fetchall()
    ctx = {"request": request, "workers": rows}
    ctx.update(_pager_dict(page, page_size, total))
    return templates.TemplateResponse("workers.html", ctx)


@router.get("/tasks", response_class=HTMLResponse)
def tasks_page(request: Request):
    guard = _require_admin(request)
    if guard:
        return guard
    page, page_size, offset = _page_params(request)
    status = (request.query_params.get("status") or "").strip()
    account_name = (request.query_params.get("account_name") or "").strip()
    worker_id = (request.query_params.get("worker_id") or "").strip()

    where = []
    params = []
    if status:
        where.append("t.status = ?")
        params.append(status)
    if account_name:
        where.append("tg.account_name LIKE ?")
        params.append(f"%{account_name}%")
    if worker_id:
        where.append("t.assigned_worker_id LIKE ?")
        params.append(f"%{worker_id}%")

    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    with db.connect() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) c FROM tasks t LEFT JOIN targets tg ON t.target_id=tg.id {where_sql}",
            params,
        ).fetchone()["c"]
        rows = conn.execute(
            f"SELECT t.*, tg.account_name FROM tasks t LEFT JOIN targets tg ON t.target_id=tg.id {where_sql} ORDER BY t.id DESC LIMIT ? OFFSET ?",
            params + [page_size, offset]
        ).fetchall()
    task_items = []
    for row in rows:
        item = dict(row)
        stats = {}
        raw_stats = (item.get("task_stats_json") or "").strip()
        if raw_stats:
            try:
                stats = json.loads(raw_stats)
            except Exception:
                stats = {}
        lines = []
        worker_bits = []
        if stats.get("params_source"):
            worker_bits.append(f"参数={stats.get('params_source')}")
        if stats.get("retry_after_refresh"):
            worker_bits.append("异常后重刷")
        if item.get("force_full_sync"):
            worker_bits.append("强制重跑")
        if worker_bits:
            lines.append(" / ".join(worker_bits))
        flow_bits = []
        if any(k in stats for k in ("raw_item_count", "accepted_item_count", "filtered_by_min_publish_time", "duplicate_count")):
            flow_bits.append(f"worker 原始{int(stats.get('raw_item_count') or 0)}")
            flow_bits.append(f"上报{int(stats.get('accepted_item_count') or stats.get('reported_item_count') or 0)}")
            filtered_count = int(stats.get('filtered_by_min_publish_time') or 0)
            if filtered_count:
                flow_bits.append(f"时间过滤{filtered_count}")
            duplicate_count = int(stats.get('duplicate_count') or 0)
            if duplicate_count:
                flow_bits.append(f"去重{duplicate_count}")
            missing_publish_count = int(stats.get('skipped_missing_publish_time') or 0)
            if missing_publish_count:
                flow_bits.append(f"缺发布时间{missing_publish_count}")
        if flow_bits:
            lines.append(" / ".join(flow_bits))
        server_bits = []
        if any(k in stats for k in ("server_inserted_count", "server_existing_url_skip_count", "server_publish_time_skip_count", "server_missing_publish_time_skip_count")):
            server_bits.append(f"入库{int(stats.get('server_inserted_count') or 0)}")
            existing_count = int(stats.get('server_existing_url_skip_count') or 0)
            if existing_count:
                server_bits.append(f"已存在{existing_count}")
            publish_skip = int(stats.get('server_publish_time_skip_count') or 0)
            if publish_skip:
                server_bits.append(f"服务端时间压制{publish_skip}")
            server_missing_publish = int(stats.get('server_missing_publish_time_skip_count') or 0)
            if server_missing_publish:
                server_bits.append(f"服务端缺发布时间{server_missing_publish}")
            if stats.get('server_limit_hit'):
                server_bits.append(f"上限{int(stats.get('server_max_items_limit') or 50)}")
        if server_bits:
            lines.append(" / ".join(server_bits))
        if stats.get("server_force_full_sync"):
            lines.append("本次任务跳过历史发布时间基线，仅保留 URL 去重")
        item["task_stats"] = stats
        item["task_stats_lines"] = lines
        task_items.append(item)
    query = {"status": status, "account_name": account_name, "worker_id": worker_id}
    ctx = {"request": request, "tasks": task_items, "query": query}
    ctx.update(_pager_dict(page, page_size, total))
    return templates.TemplateResponse("tasks.html", ctx)


@router.get("/media/image-proxy")
def media_image_proxy(request: Request, url: str):
    guard = _require_admin(request)
    if guard:
        return guard
    raw = (url or "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail="missing url")
    parsed = urlparse(raw)
    host = (parsed.hostname or "").lower()
    allowed_hosts = {
        "mmbiz.qpic.cn",
        "mmbiz.qlogo.cn",
        "wx.qlogo.cn",
        "res.wx.qq.com",
    }
    if parsed.scheme not in {"http", "https"}:
        raise HTTPException(status_code=400, detail="invalid url scheme")
    if host not in allowed_hosts and not host.endswith(".qpic.cn") and not host.endswith(".qlogo.cn"):
        raise HTTPException(status_code=400, detail="host not allowed")

    cache_path, meta_path = _image_cache_paths(raw)
    if cache_path.exists() and meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            media_type = (meta.get("content_type") or "image/jpeg").strip() or "image/jpeg"
            if not media_type.startswith("image/"):
                media_type = "image/jpeg"
            return FileResponse(
                path=str(cache_path),
                media_type=media_type,
                headers={
                    "Cache-Control": "public, max-age=604800",
                    "X-Image-Cache": "HIT",
                },
            )
        except Exception:
            pass

    try:
        resp = requests.get(
            raw,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                "Referer": "https://mp.weixin.qq.com/",
                "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
            },
            timeout=20,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"image fetch failed: {exc}")
    media_type = (resp.headers.get("Content-Type") or "image/jpeg").split(";")[0].strip() or "image/jpeg"
    if not media_type.startswith("image/"):
        media_type = "image/jpeg"

    try:
        MEDIA_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache_path.write_bytes(resp.content)
        meta_path.write_text(json.dumps({"content_type": media_type, "url": raw}, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass

    return Response(
        content=resp.content,
        media_type=media_type,
        headers={
            "Cache-Control": "public, max-age=604800",
            "X-Image-Cache": "MISS",
        },
    )


@router.get("/articles/{article_id}/md", response_class=HTMLResponse)
def article_md_page(article_id: int, request: Request):
    guard = _require_admin(request)
    if guard:
        return guard
    with db.connect() as conn:
        row = conn.execute(
            "SELECT a.*, tg.account_name FROM articles a LEFT JOIN targets tg ON a.target_id=tg.id WHERE a.id=?",
            (article_id,)
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="article not found")
    article = dict(row)
    article["fetch_meta"] = _fetch_status_meta(article.get("fetch_status"), article.get("fetch_error"))
    try:
        article["image_urls"] = json.loads(article.get("image_urls_json") or "[]")
    except Exception:
        article["image_urls"] = []
    try:
        article["video_urls"] = json.loads(article.get("video_urls_json") or "[]")
    except Exception:
        article["video_urls"] = []
    for item in article["image_urls"]:
        item["proxy_url"] = _image_proxy_url(item.get("url") or "")
    for item in article["video_urls"]:
        if item.get("cover_url"):
            item["cover_proxy_url"] = _image_proxy_url(item.get("cover_url") or "")
    selection = select_article_images(article)
    for group_name in ("keep_for_web", "keep_for_wechat", "drop", "all"):
        for item in selection.get(group_name, []):
            item["proxy_url"] = _image_proxy_url(item.get("url") or "")
    article["image_selection"] = selection
    return templates.TemplateResponse("article_md.html", {"request": request, "article": article})


@router.get("/reports", response_class=HTMLResponse)
def reports_page(request: Request):
    guard = _require_admin(request)
    if guard:
        return guard
    page, page_size, offset = _page_params(request)
    report_type = (request.query_params.get("report_type") or "").strip()
    report_date = (request.query_params.get("report_date") or "").strip()
    where_parts = []
    params = []
    if report_type:
        where_parts.append("report_type=?")
        params.append(report_type)
    if report_date:
        where_parts.append("substr(created_at,1,10)=?")
        params.append(report_date)
    where = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""
    with db.connect() as conn:
        total = conn.execute(f"SELECT COUNT(*) c FROM reports {where}", params).fetchone()["c"]
        rows = conn.execute(
            f"SELECT * FROM reports {where} ORDER BY id DESC LIMIT ? OFFSET ?",
            params + [page_size, offset]
        ).fetchall()
    enriched = []
    for r in rows:
        item = dict(r)
        item["summary_bullets"] = _report_summary(r["report_md"])
        enriched.append(item)
    ctx = {"request": request, "reports": enriched, "report_type": report_type, "report_date": report_date}
    ctx.update(_pager_dict(page, page_size, total))
    return templates.TemplateResponse("reports.html", ctx)


@router.get("/reports/{report_id}", response_class=HTMLResponse)
def report_detail_page(report_id: int, request: Request):
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM reports WHERE id=?", (report_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="report not found")
    profile = _load_article_preference_profile()
    report = dict(row)
    report["report_html"] = _report_html(report["report_md"], report)
    report["summary_bullets"] = _report_summary(report["report_md"])
    report["summary_bullets_html"] = [_summary_html(x) for x in report["summary_bullets"]]
    report["tldr"] = _report_tldr(report["report_md"])
    report["sections"] = _report_sections(report["report_md"])
    report["hero_points"] = report["summary_bullets"][:3]
    _, grouped_links, _ = _extract_source_links(report["report_md"])
    report["source_groups"] = [{"title": k, "anchor": f"sec-{_slug(k)}", "items": v} for k, v in grouped_links.items()]
    report["preference_hits"] = _latest_report_preference_hits(report, profile, limit=8)
    report["ai_rule_impacts"] = _latest_report_ai_rule_impacts(report, profile, limit=8)
    return templates.TemplateResponse("report_detail.html", {"request": request, "report": report})


@router.get("/articles", response_class=HTMLResponse)
def articles_page(request: Request):
    guard = _require_admin(request)
    if guard:
        return guard
    profile = _load_article_preference_profile()
    page, page_size, offset = _page_params(request)
    account_name = (request.query_params.get("account_name") or "").strip()
    title = (request.query_params.get("title") or "").strip()
    direction = (request.query_params.get("direction") or "").strip()
    system_hint = (request.query_params.get("system_hint") or "").strip().lower()
    publish_time_from = (request.query_params.get("publish_time_from") or "").strip()
    publish_time_to = (request.query_params.get("publish_time_to") or "").strip()
    fetch_status = (request.query_params.get("fetch_status") or "").strip()
    pref_state = (request.query_params.get("pref_state") or "").strip().lower()
    media_filter = (request.query_params.get("media_filter") or "").strip()
    sort_by = (request.query_params.get("sort_by") or "latest").strip()
    min_images_raw = (request.query_params.get("min_images") or "").strip()
    min_videos_raw = (request.query_params.get("min_videos") or "").strip()

    where = []
    params = []

    if account_name:
        where.append("tg.account_name LIKE ?")
        params.append(f"%{account_name}%")
    if title:
        where.append("a.title LIKE ?")
        params.append(f"%{title}%")
    publish_time_from_ts = _human_dt_to_ts(publish_time_from)
    publish_time_to_ts = _human_dt_to_ts(publish_time_to)
    if publish_time_from_ts:
        where.append("a.publish_time >= ?")
        params.append(publish_time_from_ts)
    if publish_time_to_ts:
        where.append("a.publish_time <= ?")
        params.append(publish_time_to_ts)
    if fetch_status:
        if fetch_status == "deleted":
            where.append("(a.fetch_status = 'deleted' OR a.fetch_status LIKE 'deleted_%')")
        else:
            where.append("a.fetch_status = ?")
            params.append(fetch_status)
    if pref_state == "unset":
        where.append("COALESCE(a.user_pref_state, '') = ''")
    elif pref_state in {"follow", "ignore", "neutral"}:
        where.append("a.user_pref_state = ?")
        params.append(pref_state)
    if media_filter == "has_image":
        where.append("COALESCE(a.image_urls_json, '') NOT IN ('', '[]')")
    elif media_filter == "has_video":
        where.append("COALESCE(a.video_urls_json, '') NOT IN ('', '[]')")
    elif media_filter == "no_media":
        where.append("COALESCE(a.image_urls_json, '') IN ('', '[]') AND COALESCE(a.video_urls_json, '') IN ('', '[]')")

    where_sql = f"WHERE {' AND '.join(where)}" if where else ""

    try:
        min_images = max(0, int(min_images_raw)) if min_images_raw else 0
    except Exception:
        min_images = 0
    try:
        min_videos = max(0, int(min_videos_raw)) if min_videos_raw else 0
    except Exception:
        min_videos = 0

    with db.connect() as conn:
        rows = conn.execute(
            f"SELECT a.*, tg.account_name FROM articles a LEFT JOIN targets tg ON a.target_id=tg.id {where_sql} ORDER BY a.id DESC",
            params,
        ).fetchall()

    articles = []
    for row in rows:
        item = dict(row)
        item["fetch_meta"] = _fetch_status_meta(item.get("fetch_status"), item.get("fetch_error"))
        item["pref_meta"] = _article_pref_meta(item.get("user_pref_state"))
        try:
            item["image_count"] = len(json.loads(item.get("image_urls_json") or "[]"))
        except Exception:
            item["image_count"] = 0
        try:
            item["video_count"] = len(json.loads(item.get("video_urls_json") or "[]"))
        except Exception:
            item["video_count"] = 0
        item_direction = _classify_dashboard_direction(item)
        item["dashboard_direction"] = item_direction
        pref_signal_score, pref_signal_reasons = _dashboard_pref_signal(item, profile=profile, include_state=False)
        ai_rule_score, ai_rule_reasons = _dashboard_ai_rule_signal(item, profile=profile)
        value_signal_score, value_signal_reasons = _dashboard_value_signal(item)
        item["pref_signal_score"] = pref_signal_score
        item["ai_rule_score"] = ai_rule_score
        item["value_signal_score"] = value_signal_score
        item["ai_rule_reasons"] = ai_rule_reasons
        item["system_explain_reasons"] = (pref_signal_reasons + ai_rule_reasons + value_signal_reasons)[:6]
        if pref_signal_score >= 3.5:
            item["system_hint_label"] = "系统倾向关注"
            item["system_hint_badge"] = "good"
        elif pref_signal_score <= -3.5:
            item["system_hint_label"] = "系统倾向忽略"
            item["system_hint_badge"] = "bad"
        elif value_signal_score >= 2.2:
            item["system_hint_label"] = "值得补标"
            item["system_hint_badge"] = "warn"
        else:
            item["system_hint_label"] = "信号一般"
            item["system_hint_badge"] = ""
        if item["image_count"] < min_images:
            continue
        if item["video_count"] < min_videos:
            continue
        if system_hint == "follow" and item["system_hint_label"] != "系统倾向关注":
            continue
        if system_hint == "ignore" and item["system_hint_label"] != "系统倾向忽略":
            continue
        if system_hint == "review" and item["system_hint_label"] != "值得补标":
            continue
        if system_hint == "weak" and item["system_hint_label"] != "信号一般":
            continue
        if direction:
            if item_direction != direction:
                continue
        articles.append(item)

    if sort_by == "images_desc":
        articles.sort(key=lambda x: (-x.get("image_count", 0), -x.get("id", 0)))
    elif sort_by == "videos_desc":
        articles.sort(key=lambda x: (-x.get("video_count", 0), -x.get("id", 0)))
    elif sort_by == "media_desc":
        articles.sort(key=lambda x: (-(x.get("image_count", 0) + x.get("video_count", 0)), -x.get("image_count", 0), -x.get("video_count", 0), -x.get("id", 0)))
    else:
        articles.sort(key=lambda x: -x.get("id", 0))

    total = len(articles)
    articles = articles[offset: offset + page_size]

    query = {
        "account_name": account_name,
        "title": title,
        "direction": direction,
        "system_hint": system_hint,
        "publish_time_from": publish_time_from,
        "publish_time_to": publish_time_to,
        "fetch_status": fetch_status,
        "pref_state": pref_state,
        "media_filter": media_filter,
        "sort_by": sort_by,
        "min_images": min_images_raw,
        "min_videos": min_videos_raw,
    }
    ctx = {"request": request, "articles": articles, "query": query, "page_size": page_size, "direction_options": DASHBOARD_DIRECTION_OPTIONS}
    ctx.update(_pager_dict(page, page_size, total))
    return templates.TemplateResponse("articles.html", ctx)


@router.post("/articles/{article_id}/preference")
def set_article_preference(article_id: int, request: Request, state: str = Form(...), next: str = Form("/articles")):
    guard = _require_admin(request)
    if guard:
        return guard
    _apply_article_preference(article_id, state)
    return RedirectResponse(url=_safe_next_path(next, "/articles"), status_code=303)


@router.post("/articles/{article_id}/preference-json")
def set_article_preference_json(article_id: int, request: Request, state: str = Form(...)):
    guard = _require_admin(request)
    if guard:
        raise HTTPException(status_code=401, detail="unauthorized")
    item = _apply_article_preference(article_id, state)
    return {
        "ok": True,
        "article": {
            "id": item["id"],
            "title": item.get("title") or "",
            "account_name": item.get("account_name") or "",
            "user_pref_state": item.get("user_pref_state"),
            "user_pref_updated_at": item.get("user_pref_updated_at") or "",
            "pref_meta": item.get("pref_meta") or _article_pref_meta(item.get("user_pref_state")),
        },
    }


@router.get("/article-preferences", response_class=HTMLResponse)
def article_preferences_page(request: Request):
    guard = _require_admin(request)
    if guard:
        return guard
    profile = _load_article_preference_profile()
    recent = _recent_labeled_articles(limit=24)
    ctx = {
        "request": request,
        "profile": profile,
        "recent_articles": recent,
    }
    return templates.TemplateResponse("article_preferences.html", ctx)


@router.post("/article-preferences/refresh")
def refresh_article_preferences(request: Request, next: str = Form("/article-preferences")):
    guard = _require_admin(request)
    if guard:
        return guard
    try:
        import sys
        sys.path.insert(0, '/root/.openclaw/workspace/wechat_ingest_system')
        import report_generator as rg
        rg.refresh_article_preference_profile()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"refresh article preference profile failed: {e}")
    return RedirectResponse(url=_safe_next_path(next, "/article-preferences"), status_code=303)


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, saved: str | None = None, reset: str | None = None):
    guard = _require_admin(request)
    if guard:
        return guard
    ctx = {
        "request": request,
        "settings": _report_settings_view_model(),
        "recommendations": _report_settings_recommendations(),
        "saved": saved == '1',
        "reset": reset == '1',
    }
    return templates.TemplateResponse("settings.html", ctx)


@router.post("/settings/save")
async def save_settings(request: Request):
    guard = _require_admin(request)
    if guard:
        return guard
    form = await request.form()
    action = (form.get('action') or 'save').strip().lower()
    if action == 'reset':
        _save_report_settings(DEFAULT_REPORT_SETTINGS)
        return RedirectResponse(url='/settings?reset=1', status_code=303)

    direction_caps = {}
    signal_limits = {}
    for direction in DASHBOARD_DIRECTION_OPTIONS:
        direction_caps[direction] = _clamp_int(form.get(f'direction_cap__{direction}'), REPORT_DIRECTION_CAPS_DEFAULT.get(direction, 2), 0, 200)
        signal_limits[direction] = _clamp_int(form.get(f'signal_limit__{direction}'), REPORT_DIRECTION_SIGNAL_LIMITS_DEFAULT.get(direction, 1), 0, 200)

    payload = {
        'selection': {
            'max_clusters': _clamp_int(form.get('max_clusters'), 60, 1, 200),
            'longtail_min': _clamp_int(form.get('longtail_min'), 3, 0, 50),
            'same_source_cap_default': _clamp_int(form.get('same_source_cap_default'), 1, 1, 20),
            'same_source_cap_geo_huanqiu': _clamp_int(form.get('same_source_cap_geo_huanqiu'), 2, 1, 20),
            'same_source_cap_military_huanqiu': _clamp_int(form.get('same_source_cap_military_huanqiu'), 2, 1, 20),
            'preferred_bonus_ai': _clamp_int(form.get('preferred_bonus_ai'), 3, 0, 20),
            'preferred_bonus_core': _clamp_int(form.get('preferred_bonus_core'), 2, 0, 20),
            'preferred_bonus_longtail': _clamp_int(form.get('preferred_bonus_longtail'), 1, 0, 20),
            'appendix_limit_min': _clamp_int(form.get('appendix_limit_min'), 3, 1, 20),
            'appendix_limit_max': _clamp_int(form.get('appendix_limit_max'), 6, 1, 20),
            'multi_source_appendix_items_preferred': _clamp_int(form.get('multi_source_appendix_items_preferred'), 5, 1, 20),
            'multi_source_appendix_items_default': _clamp_int(form.get('multi_source_appendix_items_default'), 3, 1, 20),
            'single_source_appendix_items': _clamp_int(form.get('single_source_appendix_items'), 1, 1, 20),
            'github_section_items': _clamp_int(form.get('github_section_items'), 5, 1, 20),
            'direction_caps': direction_caps,
            'signal_limits': signal_limits,
        }
    }
    if payload['selection']['appendix_limit_max'] < payload['selection']['appendix_limit_min']:
        payload['selection']['appendix_limit_max'] = payload['selection']['appendix_limit_min']
    _save_report_settings(payload)
    return RedirectResponse(url='/settings?saved=1', status_code=303)
