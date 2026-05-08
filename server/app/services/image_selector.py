import re
from typing import Dict, List


DROP_PATTERNS = [
    r"二维码",
    r"扫码",
    r"长按",
    r"关注",
    r"一键三连",
    r"点赞",
    r"转发",
    r"分享",
    r"阅读原文",
    r"赞赏",
    r"小助手",
    r"公众号",
    r"广告",
    r"推广",
    r"海报",
    r"福利",
    r"抽奖",
]


def _to_int(value, default=0):
    try:
        return int(str(value).strip())
    except Exception:
        return default


def _match_any(text: str, patterns: List[str]):
    raw = text or ""
    return [p for p in patterns if re.search(p, raw, re.I)]


def _normalize_item(item: Dict):
    data = dict(item or {})
    data["width_px"] = _to_int(data.get("width"), 0)
    data["type_norm"] = str(data.get("type") or "").lower().strip()
    data["tag_norm"] = str(data.get("tag") or "").lower().strip()
    data["source_attr_norm"] = str(data.get("source_attr") or "").lower().strip()
    data["alt_norm"] = str(data.get("alt") or "").strip()
    data["url_norm"] = str(data.get("url") or "").strip()
    return data


def _score_image(item: Dict, article_title: str = "", article_text: str = ""):
    data = _normalize_item(item)
    url = data["url_norm"]
    alt = data["alt_norm"]
    width = data["width_px"]
    img_type = data["type_norm"]
    tag = data["tag_norm"]
    source_attr = data["source_attr_norm"]
    article_ctx = f"{article_title}\n{article_text[:1200]}"
    combined_text = f"{alt}\n{url}\n{article_ctx}"

    keep_reasons = []
    drop_reasons = []
    score = 0.0

    if not url:
        drop_reasons.append("缺少图片 URL")
    if source_attr == "data-miniprogram-imageurl" or tag == "a":
        drop_reasons.append("小程序/卡片类图片")
    if width and width < 220:
        drop_reasons.append("尺寸过小")
    if img_type == "gif" or "wx_fmt=gif" in url:
        drop_reasons.append("GIF 动图默认不入选")
    if source_attr == "data-cover" or tag == "iframe":
        drop_reasons.append("更像视频封面或嵌入卡片封面")
    for matched in _match_any(combined_text, DROP_PATTERNS):
        drop_reasons.append(f"命中过滤词：{matched}")

    if width >= 1000:
        score += 2.5
        keep_reasons.append("分辨率较高")
    elif width >= 700:
        score += 1.5
        keep_reasons.append("尺寸适中")
    elif width >= 480:
        score += 0.8
        keep_reasons.append("基础尺寸可用")

    if img_type == "png":
        score += 1.2
        keep_reasons.append("PNG 更像图表/截图")
    elif img_type in {"jpeg", "jpg", "other"}:
        score += 0.6
        keep_reasons.append("常规静态图")

    if tag == "img":
        score += 0.4
        keep_reasons.append("正文图片节点")

    if width >= 900 and img_type == "png":
        score += 1.0
        keep_reasons.append("适合作为信息图/截图")
    if width >= 900 and img_type in {"jpeg", "jpg", "other"}:
        score += 0.6
        keep_reasons.append("适合作为现场图/人物图")

    if drop_reasons:
        score -= min(6, len(drop_reasons) * 1.6)

    kind = "unknown"
    if img_type == "png" and width >= 700:
        kind = "info"
    elif img_type in {"jpeg", "jpg", "other"} and width >= 700:
        kind = "scene"
    elif img_type == "gif":
        kind = "animated"

    return {
        **data,
        "score": round(score, 2),
        "kind": kind,
        "keep_reasons": keep_reasons,
        "drop_reasons": drop_reasons,
        "recommended_for_web": False,
        "recommended_for_wechat": False,
    }


def select_article_images(article: Dict, max_web: int = 3, max_wechat: int = 4):
    title = str((article or {}).get("title") or "")
    content_md = str((article or {}).get("content_md") or "")
    images = list((article or {}).get("image_urls") or [])

    scored = []
    seen = set()
    for item in images:
        data = _score_image(item, article_title=title, article_text=content_md)
        url = data.get("url_norm") or ""
        if not url or url in seen:
            continue
        seen.add(url)
        scored.append(data)

    eligible_web = [
        x for x in scored
        if not x["drop_reasons"] and x["score"] >= 2.2 and x["kind"] in {"info", "scene"}
    ]
    eligible_wechat = [
        x for x in scored
        if not x["drop_reasons"] and x["score"] >= 1.3 and x["kind"] in {"info", "scene"}
    ]
    dropped = [x for x in scored if x["drop_reasons"] or x["score"] < 1.3]

    eligible_web.sort(key=lambda x: (-x["score"], -(x.get("width_px") or 0), x.get("url_norm") or ""))
    eligible_wechat.sort(key=lambda x: (-x["score"], -(x.get("width_px") or 0), x.get("url_norm") or ""))
    dropped.sort(key=lambda x: (len(x["drop_reasons"]), x["score"]))

    keep_for_web = eligible_web[:max_web]
    keep_for_wechat = eligible_wechat[:max_wechat]

    web_urls = {x["url_norm"] for x in keep_for_web}
    wechat_urls = {x["url_norm"] for x in keep_for_wechat}
    for item in scored:
        if item["url_norm"] in web_urls:
            item["recommended_for_web"] = True
        if item["url_norm"] in wechat_urls:
            item["recommended_for_wechat"] = True

    return {
        "keep_for_web": keep_for_web,
        "keep_for_wechat": keep_for_wechat,
        "drop": dropped,
        "all": scored,
        "summary": {
            "total": len(scored),
            "web_count": len(keep_for_web),
            "wechat_count": len(keep_for_wechat),
            "drop_count": len(dropped),
        },
    }
