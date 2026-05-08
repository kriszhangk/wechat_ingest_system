#!/usr/bin/env python3
"""Morning/Evening report generator for WeChat article digest.
Calls MiniMax-M2.7 via Anthropic-compatible API to produce intel-style reports.

Output format (user-chosen):
  1. Direction/topic categories
  2. Content per category
  3. Links appendix at the end
"""

import json
import multiprocessing
import math
import os
import re
import signal
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path

import urllib.request
import urllib.error

try:
    import anthropic
except Exception:
    anthropic = None

# ── Config ──────────────────────────────────────────────────────────────────

DB_PATH = "/root/.openclaw/workspace/wechat_ingest_system/server/data/app.db"
STATE_PATH = Path("/root/.openclaw/workspace/wechat_ingest_system/report_state.json")

MINIMAX_BASE_URL = os.getenv("MINIMAX_BASE_URL", "https://api.minimaxi.com/anthropic")
MINIMAX_API_KEY = os.getenv("MINIMAX_API_KEY")
MINIMAX_MODEL = os.getenv("MINIMAX_MODEL", "MiniMax-M2.7")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
MINIMAX_CALL_TIMEOUT = int(os.getenv("MINIMAX_CALL_TIMEOUT", "600"))
OPENCLAW_CONFIG_PATH = Path("/root/.openclaw/openclaw.json")

SYSTEM_PROMPT = """你是一名资深科技情报分析师，负责生成公众号文章的早/晚报摘要。

输出格式严格如下（Markdown）：

## 📌 方向归类

优先从这些稳定方向里选 3-6 个：
- AI / 大模型
- 网络安全
- 国际局势 / 地缘政治
- 中国军事 / 外交
- 科技产业 / 商业
- 具身智能 / 机器人
- 生活健康 / 医疗
- 消费 / 民生 / 社会风险

可以按“5 个主要聚类 + 1~3 个长尾观察”来组织，最重要的放前面，但不要把高质量长尾内容全部丢掉。

每个方向用一句话概括趋势或关键信号。
方向默认优先顺序：
1. AI / 大模型
2. 国际局势 / 地缘政治
3. 网络安全
4. 中国军事 / 外交
5. 科技产业 / 商业
6. 具身智能 / 机器人
7. 消费 / 民生 / 社会风险
如果某高优先级方向当天没有足够内容，可以跳过；否则尽量遵循这个顺序。

---

## 📝 内容详览

按方向逐个展开：
- 每个方向下列出相关文章要点（2-4 句话，突出核心信息和判断）
- 标注来源公众号
- 如果 AI / 大模型方向中出现 GitHub、开源项目、开源框架、开源工具、代码仓库、ModelScope / GitHub / 开源社区相关内容，必须单独增加一个小节：`开源项目 / GitHub`，列出项目名称、项目做什么、为什么值得看、项目地址
- 同一事件被多号报道的，合并描述并显式标注【多源共识】
- 只有单一来源、尚未形成共识的，显式标注【单点信号】
- 对所有信号标签，必须把来源公众号直接写进标签里，格式固定为：`【单点信号·来源公众号】` 或 `【多源共识·来源A/来源B】`；禁止只写裸的 `【单点信号】` / `【多源共识】`
- 每一条 `单点信号` / `多源共识` 只允许承载 **一个主事件 / 一个主判断 / 一组高度同质的同一事件报道**；禁止在同一条里混入两个以上不相干事件
- 同一方向下，默认每条信号对应一个独立条目；如果需要写第二个事件，就另起一条，不要把多个事件并在一个条目里
- 同一方向下，`单点信号` 默认不要连续重复使用同一个来源超过 1 次；若该方向确实几乎只有同一来源可用，也要尽量合并成更少条目，避免视觉上重复刷屏
- 优先用稳定方向名，不要临时发明过多新方向
- 不同方向写作密度不同：AI/大模型、国际局势/地缘政治、中国军事/外交 可展开更多；科技产业/商业、具身智能/机器人 居中；消费/民生/社会风险 更压缩，只保留真正值得提醒的点
- 如果用户消息中明确列出了“必须覆盖”的长尾方向，这些方向必须在最终 Markdown 的“方向归类”和“内容详览”中逐一出现，不能省略，尤其不能漏掉 `生活健康 / 医疗`
- 生活健康 / 医疗、消费 / 民生 / 社会风险、教育 / 科学 这类长尾方向不要被全部清空；如果当天有高质量条目，至少保留 1-3 条“长尾观察”或补充方向
- 生活健康 / 医疗 只保留真正对生活健康有帮助的内容：优先睡眠、饮食、运动、口腔、补充剂、慢病预防、儿童/老人健康、就医建议等可执行知识；不要把“平台透明度”“医生是不是真人”“争议热议”“导购软文”这类虚泛内容当成健康主条目

---

## 🔗 链接附录

按方向分组，列出每个方向最值得看的 Top 3-6 条链接，格式：
- 标题 | 公众号 | 链接 | 推荐理由
同一方向下如果多个公众号在讲同一件事，要做来源去重：
- 默认只保留最强 1-2 条代表链接
- 优先保留信息量更高、标题更具体、来源更可靠的链接
- 每条链接后附一个很短的推荐理由标签，例如：主信源 / 代表性报道 / 争议点来源 / 背景补充 / 深读入口

规则：
- 用中文输出
- 不说废话，高信息密度
- 不要加任何"希望对你有帮助"之类的客套话
- 不要重复罗列，同方向内容合并叙述
- 链接必须放在最后的附录区，正文中不放链接
- 链接附录不要把所有文章都贴出来，只保留每个方向最关键的 Top 链接
- AI / 大模型中涉及 GitHub / 开源项目时，在正文的 `开源项目 / GitHub` 小节中直接列出项目地址，不要只放到附录里
- GitHub / 开源项目地址必须逐字输出完整 URL，严禁写成 `https://github.com/...`、`github.com/...` 或任何省略形式。
- 如果你无法确认完整 GitHub 地址，就宁可不写这个项目地址，也不要输出省略号假链接。
- 对军事、地缘政治、国家机构、军队主体相关表述，必须严格以源材料中的明确实体为准；如果原文写的是以色列国防部/以色列国防军，就不能写成解放军；如果原文写的是伊朗军方，就不能写成解放军；不得擅自替换国家、军队、机构主体。"""

SEMANTIC_DEDUPE_PROMPT = """你是一个严格的标题去重判定器。你的任务是判断“当前窗口标题”是否与“过去一周标题”语义重复。

判定标准：
1. 如果两个标题描述的是同一事件、同一新闻、同一项目发布、同一公司同一轮融资、同一产品发布、同一政策事件，即使措辞不同，也视为重复。
2. 如果只是同一大方向但讲的是不同事件、不同角度、不同主体、不同时间点，不算重复。
3. 你要偏保守：只有高度确定是同一事件/主题时，才判定重复。

输出要求：
- 只输出 JSON
- 格式：{"duplicates": [{"current_id": "c1", "matched_history": "历史标题", "reason": "一句话理由"}]}
- 如果没有重复，输出：{"duplicates": []}
"""

GITHUB_REPAIR_PROMPT = """你是一个日报修订助手。

任务：修复下方日报中所有 GitHub / 开源项目地址不完整的问题。

规则：
1. 只修复 GitHub / 开源项目相关链接，不要改写其他内容结构和措辞。
2. 只能从提供的“候选完整 GitHub 地址列表”里选用。
3. 严禁输出 `https://github.com/...`、`github.com/...`、省略号假链接、截断链接。
4. 如果某一处无法确定对应哪个完整地址，就删除该处错误的省略链接，不要编造。
5. 输出完整修复后的 Markdown 正文，不要解释。
"""

ENTITY_REPAIR_PROMPT = """你是一个日报事实修订助手。

任务：修复下方日报中的国家 / 军队 / 机构实体误指代问题。

规则：
1. 只能依据提供的“候选源证据”修复实体，不要编造新事实。
2. 标题模糊时，必须以源证据中的明确主体为准，例如：以色列国防部 / 以色列国防军 / 伊朗总统 / 东部战区 / 解放军 / 美军。
3. 严禁把不同主体互相替换，例如把以军写成解放军、把伊朗军方写成解放军、把东部战区写成伊朗军队等。
4. 只修复事实性实体误指代，不要重写整体结构，不要大改措辞。
5. 输出完整修复后的 Markdown 正文，不要解释。
"""

PREFERENCE_PROFILE_SUMMARY_PROMPT = """你是一名用户内容偏好分析助手。

你的任务是根据用户对文章打的 `关注 / 忽略 / 中性` 标记，总结出一份真正可用的偏好画像。

输出要求：
1. 只输出 JSON，不要输出 Markdown，不要解释。
2. JSON 格式固定为：
{
  "summary": "一段 60-140 字中文总结，概括这个人的核心内容偏好",
  "likes": ["更偏好的内容类型1", "更偏好的内容类型2", "..."],
  "dislikes": ["更常忽略的内容类型1", "更常忽略的内容类型2", "..."],
  "neutral_observations": ["中性观察1", "中性观察2"],
  "selection_advice": ["后续选稿建议1", "后续选稿建议2", "后续选稿建议3"]
}
3. 结论必须以提供的标记样本、来源倾向、主题词倾向为依据，不要编造用户不存在的兴趣。
4. 不要只复述统计项，要提炼成更抽象、更人能读懂的偏好判断。
5. 输出简洁、具体、可用于后续日报选稿。
"""

PREFERENCE_PROFILE_RULES_PROMPT = """你是一名内容推荐策略助手。

你的任务是根据用户的文章标记样本和已有偏好总结，提炼出可直接用于日报选稿打分的结构化规则。

只输出 JSON，格式固定为：
{
  "boost_directions": [{"direction": "AI / 大模型", "weight": 0.0, "reason": "..."}],
  "suppress_directions": [{"direction": "消费 / 民生 / 社会风险", "weight": 0.0, "reason": "..."}],
  "boost_sources": [{"source": "机器之心", "weight": 0.0, "reason": "..."}],
  "suppress_sources": [{"source": "知乎日报", "weight": 0.0, "reason": "..."}],
  "boost_terms": [{"term": "开源", "weight": 0.0, "reason": "..."}],
  "suppress_terms": [{"term": "招聘", "weight": 0.0, "reason": "..."}]
}

规则：
1. weight 取值 0.5 到 3.0，表示修正强度，不要输出负数。
2. direction 必须使用系统已有稳定方向名。
3. source / term 必须来自给定样本、统计或总结，不要编造新词。
4. 每类最多输出 4 条，宁缺毋滥。
5. 目标是辅助日报选稿，不是重做整套规则，因此保持克制，优先高置信结论。
"""

ENTITY_KEYWORDS = [
    "以色列国防军", "以色列国防部", "以色列", "以军",
    "伊朗革命卫队", "伊朗武装部队", "伊朗陆军", "伊朗总统", "伊朗", "伊军",
    "中国人民解放军", "解放军东部战区", "东部战区", "解放军",
    "美军", "美方", "白宫",
    "俄军", "俄罗斯",
    "法国士兵", "法国", "马克龙",
    "日本自卫队", "日本舰艇", "日本",
]

LAST_PROVIDER_USED = None
LAST_FALLBACK_USED = False
LAST_FALLBACK_REASON = None
CURRENT_ARTICLE_PREFERENCE_PROFILE = None

ARTICLE_PREF_PROFILE_JSON_PATH = Path('/root/.openclaw/workspace/wechat_ingest_system/server/data/article_preference_profile.json')
ARTICLE_PREF_PROFILE_MD_PATH = Path('/root/.openclaw/workspace/wechat_ingest_system/server/data/article_preference_profile.md')

PREFERENCE_TOKEN_STOPWORDS = {
    '最新', '今天', '一个', '为什么', '如何', '什么', '真的', '来了', '官方', '重磅', '发布', '我们', '他们',
    '全球', '中国', '美国', '行业', '公司', '平台', '系统', '问题', '事件', '报道', '观察', '要闻', '精选',
}

STABLE_DIRECTIONS = [
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

CORE_DIRECTIONS = {
    "AI / 大模型",
    "国际局势 / 地缘政治",
    "网络安全",
    "中国军事 / 外交",
    "科技产业 / 商业",
    "具身智能 / 机器人",
}

LONGTAIL_DIRECTIONS = {
    "生活健康 / 医疗",
    "消费 / 民生 / 社会风险",
    "教育 / 科学",
    "其他观察",
}

DIRECTION_PRIORITY = {
    "AI / 大模型": 9.0,
    "国际局势 / 地缘政治": 8.0,
    "网络安全": 7.0,
    "中国军事 / 外交": 6.5,
    "科技产业 / 商业": 6.0,
    "具身智能 / 机器人": 5.5,
    "生活健康 / 医疗": 4.6,
    "消费 / 民生 / 社会风险": 4.3,
    "教育 / 科学": 4.0,
    "其他观察": 3.2,
}

DIRECTION_KEYWORDS = {
    "AI / 大模型": ["ai", "大模型", "模型", "agent", "claude", "openai", "grok", "kimi", "qwen", "deepseek", "llm", "开源", "github", "codex", "cursor", "推理模型", "文生图", "nucleus-image", "mythos", "iclr", "oral", "siggraph", "aigc", "论文", "长程记忆", "conference", "modelscope", "huggingface", "anigen", "aiscientist"],
    "国际局势 / 地缘政治": ["伊朗", "以色列", "美国", "白宫", "霍尔木兹", "外交", "地缘", "谈判", "制裁", "油价", "停火", "局势", "海峡", "欧盟", "出口管制", "实体清单", "关税", "商务部"],
    "网络安全": ["漏洞", "rce", "攻击", "渗透", "chrome", "武器化", "勒索", "防御", "应急", "入侵", "exploit", "drozer", "oauth", "令牌", "供应链"],
    "中国军事 / 外交": ["东部战区", "解放军", "中国海警", "舰艇", "军演", "国防部", "过航", "台海", "外交部", "演训"],
    "科技产业 / 商业": ["融资", "估值", "裁员", "商业", "产业", "市场", "腾讯", "阿里", "字节", "利润", "收入", "广告", "芯片", "公司", "ceo", "并购"],
    "具身智能 / 机器人": ["机器人", "具身", "人形", "半马", "机器狗", "机械臂", "physical agi", "abot", "claw", "harness", "抓取成功率"],
    "生活健康 / 医疗": ["健康", "医疗", "医院", "癌症", "减肥", "饮食", "睡眠", "刷牙", "药", "医生", "疾病", "养生", "患者", "维生素", "结节", "高尿酸", "甲状腺", "肺结节", "死亡风险"],
    "消费 / 民生 / 社会风险": ["消费", "民生", "房价", "育儿", "宠物", "校园", "事故", "社保", "就业", "电商", "旅游", "社会", "12306", "航班", "机票", "抢票", "外卖", "快递"],
    "教育 / 科学": ["教育", "大学", "科研", "论文", "science", "实验", "发现", "研究", "学者", "期刊"],
}

SOURCE_DIRECTION_HINTS = {
    "AI寒武纪": {"AI / 大模型": 7.0},
    "机器之心": {"AI / 大模型": 6.0},
    "量子位": {"AI / 大模型": 4.5, "具身智能 / 机器人": 3.5, "科技产业 / 商业": 2.0},
    "新智元": {"AI / 大模型": 4.8, "科技产业 / 商业": 2.5},
    "PaperWeekly": {"AI / 大模型": 6.0, "教育 / 科学": 2.0},
    "魔搭ModelScope社区": {"AI / 大模型": 7.0},
    "黑白之道": {"网络安全": 7.0},
    "FreeBuf": {"网络安全": 7.0},
    "看雪学苑": {"网络安全": 7.0},
    "丁香医生": {"生活健康 / 医疗": 8.0},
    "环球科学": {"生活健康 / 医疗": 4.0, "教育 / 科学": 4.0},
    "刘润": {"科技产业 / 商业": 3.5},
    "哔哩哔哩": {"其他观察": 2.0},
    "36氪": {"科技产业 / 商业": 6.5},
    "全球风口": {"科技产业 / 商业": 5.0},
    "APPSO": {"具身智能 / 机器人": 4.0, "科技产业 / 商业": 3.0, "消费 / 民生 / 社会风险": 2.0},
}

AI_HARD_ANCHORS = ["iclr", "siggraph", "oral", "anigen", "aiscientist", "modelscope", "openc", "openai", "kimi", "claude", "deepseek", "huggingface", "github", "开源", "长程记忆", "aigc"]
MILITARY_HARD_ANCHORS = ["东部战区", "解放军", "舰艇编队", "横当水道", "美菲联合军演", "外交部回应", "黑海舰队", "登陆舰", "演训"]
GEO_HARD_ANCHORS = ["伊朗", "霍尔木兹", "海峡", "阿曼湾", "特朗普", "以色列", "白宫", "货船", "战争赔偿", "朝鲜", "日本", "巴基斯坦", "埃及", "土耳其", "约旦", "卡塔尔", "沙特", "阿联酋"]
SECURITY_HARD_ANCHORS = ["漏洞", "渗透", "exp", "exploit", "drozer", "chrome", "oauth", "令牌", "供应链", "武器化", "入侵", "rce"]

REPORT_DIRECTION_SIGNAL_LIMITS = {
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

HEALTH_PRACTICAL_KEYWORDS = [
    "刷牙", "口腔", "牙", "睡眠", "助眠", "饮食", "运动", "减脂", "减重", "减肥",
    "维生素", "vd", "d3", "补充", "补钙", "高尿酸", "血压", "血糖", "胆固醇", "体检",
    "预防", "慢病", "习惯", "久坐", "步行", "跑步", "游泳", "饮水", "作息", "防晒",
    "儿童", "婴幼儿", "孕妇", "老年人", "咳嗽", "感冒", "就医", "用药", "药物", "营养",
]

HEALTH_LOW_SIGNAL_KEYWORDS = [
    "在线问诊", "平台", "真人吗", "真人", "透明度", "回应", "热议", "争议", "辟谣", "澄清",
    "带货", "种草", "选购", "点击", "购买", "链接", "广告", "推广", "直播", "客服", "科普号",
    "你最关心的问题", "是真的吗", "平台审核", "资质认证",
]

HEALTH_DIRECTION_ANCHORS = [
    "健康", "医疗", "医院", "疾病", "减肥", "刷牙", "睡眠", "饮食", "药", "医生", "患者", "癌症", "维生素",
    "高尿酸", "甲状腺", "肺结节", "口腔", "慢病", "养生", "红疹", "皮肤", "就医", "用药", "营养", "寿命", "衰老", "感染", "病毒", "hpv",
]

GEO_POLICY_ANCHORS = [
    "欧盟", "出口管制", "实体清单", "商务部", "制裁", "关税", "停火", "外交部", "白宫", "中东", "海峡", "以色列", "伊朗",
    "外长", "清真寺", "使馆", "伊斯兰堡", "马斯喀特", "莫斯科", "朝鲜", "日本", "巴基斯坦", "埃及", "土耳其", "约旦", "卡塔尔", "沙特", "阿联酋",
]

DIRECTION_CAPS = {
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

REPORT_SETTINGS_JSON_PATH = Path('/root/.openclaw/workspace/wechat_ingest_system/server/data/report_settings.json')
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
        "direction_caps": DIRECTION_CAPS,
        "signal_limits": REPORT_DIRECTION_SIGNAL_LIMITS,
    }
}
CURRENT_REPORT_SETTINGS = None


def _deep_merge_dict(base: dict, override: dict):
    merged = dict(base or {})
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge_dict(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_report_settings(reload: bool = False):
    global CURRENT_REPORT_SETTINGS
    if CURRENT_REPORT_SETTINGS is not None and not reload:
        return CURRENT_REPORT_SETTINGS
    settings = json.loads(json.dumps(DEFAULT_REPORT_SETTINGS, ensure_ascii=False))
    if REPORT_SETTINGS_JSON_PATH.exists():
        try:
            raw = json.loads(REPORT_SETTINGS_JSON_PATH.read_text(encoding='utf-8'))
            settings = _deep_merge_dict(settings, raw)
        except Exception:
            pass
    CURRENT_REPORT_SETTINGS = settings
    return CURRENT_REPORT_SETTINGS


def report_selection_settings():
    return (load_report_settings().get('selection') or {})


def report_direction_caps():
    caps = dict(DIRECTION_CAPS)
    caps.update((report_selection_settings().get('direction_caps') or {}))
    return caps


def report_signal_limits():
    limits = dict(REPORT_DIRECTION_SIGNAL_LIMITS)
    limits.update((report_selection_settings().get('signal_limits') or {}))
    return limits


def report_setting_int(key: str, default: int):
    try:
        return int(report_selection_settings().get(key, default))
    except Exception:
        return default


def report_same_source_cap(direction: str, source: str):
    default_cap = max(1, report_setting_int('same_source_cap_default', 1))
    if source == '环球网' and direction == '国际局势 / 地缘政治':
        return max(default_cap, report_setting_int('same_source_cap_geo_huanqiu', 2))
    if source == '环球网' and direction == '中国军事 / 外交':
        return max(default_cap, report_setting_int('same_source_cap_military_huanqiu', 2))
    return default_cap


def report_preferred_direction_bonus(direction: str):
    if direction == 'AI / 大模型':
        return max(0, report_setting_int('preferred_bonus_ai', 3))
    if direction in CORE_DIRECTIONS:
        return max(0, report_setting_int('preferred_bonus_core', 2))
    return max(0, report_setting_int('preferred_bonus_longtail', 1))

TOPIC_FOCUS_GENERIC_STOPWORDS = {
    "api", "model", "models", "flash", "pro", "preview", "release", "github", "agent",
    "openai", "claude", "kimi", "qwen", "deepseek", "ai", "aigc", "llm", "v4", "v5",
    "发布", "开源", "模型", "版本", "能力", "进展", "架构", "生态", "适配", "观察",
}

TOPIC_FOCUS_PATTERNS = [
    r"(DeepSeek(?:[-\s]?V?\d+(?:\.\d+)?)?)",
    r"(GPT[-\s]?\d+(?:\.\d+)?)",
    r"(Claude\s?\d+(?:\.\d+)?)",
    r"(Qwen[-\s]?[A-Za-z0-9_.-]+)",
    r"(Kimi[-\s]?[A-Za-z0-9_.-]+)",
    r"(SimpleTES)",
    r"(JiuwenClaw)",
    r"(HappyHorse)",
    r"(vllm-mlu)",
    r"(ModelScope)",
    r"(HuggingFace)",
]
# ── State ───────────────────────────────────────────────────────────────────

def load_state():
    if not STATE_PATH.exists():
        return {"last_morning_report_at": None, "last_evening_report_at": None}
    return json.loads(STATE_PATH.read_text(encoding="utf-8"))


def save_state(state):
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ── Dedup ───────────────────────────────────────────────────────────────────

def normalize_title(title: str):
    if not title:
        return ""
    s = title.lower().strip()
    for ch in [" ", "\t", "\n", "\r", "，", ",", "。", ".", "！", "!", "？", "?",
               "：", ":", "；", ";", "-", "_", "|", "/", "\\", "（", "）", "(", ")",
               "【", "】", "[", "]", """, """, '"', "'", "《", "》"]:
        s = s.replace(ch, "")
    return s


def title_tokens(title: str):
    s = normalize_title(title)
    if not s:
        return set()
    tokens = set()
    # mixed Chinese/Latin loose tokenization
    for i in range(0, max(0, len(s) - 1)):
        if ord(s[i]) > 127 and ord(s[i + 1]) > 127:
            tokens.add(s[i:i+2])
    latin = []
    buf = []
    for ch in s:
        if ch.isascii() and ch.isalnum():
            buf.append(ch)
        else:
            if buf:
                latin.append(''.join(buf))
                buf = []
    if buf:
        latin.append(''.join(buf))
    for t in latin:
        if len(t) >= 3:
            tokens.add(t)
    return tokens


def row_similarity(a, b):
    ta = title_tokens(a["title"])
    tb = title_tokens(b["title"])
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    union = len(ta | tb)
    return inter / union if union else 0.0


def topic_key(row):
    title = normalize_title(row["title"])
    if not title:
        return row["article_url"]
    return title[:18]


# ── Data ────────────────────────────────────────────────────────────────────

def load_rows(window_start, window_end):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT a.id, a.title, a.article_url, a.discovered_at, a.target_id,
               tg.account_name, a.content_md, a.content_html,
               a.user_pref_state, a.user_pref_updated_at
        FROM articles a
        LEFT JOIN targets tg ON a.target_id = tg.id
        WHERE a.discovered_at > ? AND a.discovered_at <= ?
        ORDER BY a.discovered_at DESC, a.id DESC
        """,
        (window_start, window_end),
    ).fetchall()
    conn.close()
    return rows


def extract_github_urls(text: str):
    urls = []
    seen = set()
    raw = text or ""
    for match in re.findall(r'https?://github\.com/[^\s)\]"<>]+', raw):
        url = normalize_github_project_url(match)
        if not url:
            continue
        if url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def normalize_github_project_url(url: str):
    raw = (url or '').strip()
    if not raw:
        return ''
    raw = raw.replace('\\/', '/').replace('\\-', '-').replace('\\_', '_').replace('\\.', '.')
    raw = raw.rstrip('，。；;：:!！?？》）)]*')
    raw = raw.rstrip('*')
    raw = raw.split('?', 1)[0].split('#', 1)[0]
    if '/...' in raw or raw.endswith('/..') or raw.endswith('/.'):
        return ''
    m = re.match(r'^https?://github\.com/([^/\s]+)/([^/\s]+)(?:/.*)?$', raw)
    if not m:
        return ''
    owner, repo = m.group(1).strip(), m.group(2).strip()
    if not owner or not repo:
        return ''
    if repo.endswith('.git'):
        repo = repo[:-4]
    if not repo or repo.lower() in {'issues', 'pull', 'pulls', 'actions', 'wiki', 'projects', 'releases'}:
        return ''
    return f'https://github.com/{owner}/{repo}'


def github_project_label(url: str):
    normalized = normalize_github_project_url(url)
    if not normalized:
        return 'GitHub 项目'
    m = re.match(r'^https?://github\.com/([^/\s]+)/([^/\s]+)$', normalized)
    if not m:
        return 'GitHub 项目'
    owner, repo = m.group(1), m.group(2)
    return f'{owner}/{repo}'


def github_title_from_context(before: str, after: str, url: str):
    project_label = github_project_label(url)
    reject_titles = {'代码链接', '代码仓库', '项目地址', '项目主页', '开源地址', 'github', 'github链接'}
    candidates = []
    for match in re.finditer(r'\*\*([^*\n]{4,80})\*\*', before[-420:]):
        title = clean_inline_text(match.group(1), limit=60, ellipsis=False)
        if not title:
            continue
        if '开源地址' in title or title == project_label:
            continue
        if title.lower() in reject_titles:
            continue
        candidates.append(title)
    return candidates[-1] if candidates else ''


def github_intro_from_context(before: str, after: str, title: str = '', project_label: str = ''):
    def _pick_sentence(block: str, reverse: bool = False):
        sentences = split_sentences(block)
        if reverse:
            sentences = list(reversed(sentences))
        for sent in sentences:
            cleaned = clean_inline_text(sent, limit=120, ellipsis=False)
            if not cleaned:
                continue
            cleaned = re.sub(r'^[A-Za-z0-9 _=&?./:+\\-]*from=appmsg\)?', '', cleaned).lstrip('：:，,。.-— ')
            cleaned = re.sub(r'^[A-Za-z]{1,6}\s*fmt=\w+&from=appmsg\)?', '', cleaned).lstrip('：:，,。.-— ')
            cleaned = cleaned.replace('\\+', '+').strip()
            if title and cleaned.startswith(title):
                cleaned = cleaned[len(title):].lstrip('：:，,。.-— ')
            bad_tokens = ['开源地址', '项目地址', '点击', '关注', '阅读原文']
            if any(tok in cleaned for tok in bad_tokens):
                continue
            if cleaned.startswith(('代码链接', '代码仓库')):
                continue
            if len(cleaned) >= 14:
                return cleaned
        return ''

    before_raw = re.sub(r'!\[[^\]]*\]\([^)]*\)', ' ', before or '')
    before_raw = re.sub(r'```+', ' ', before_raw)
    before_raw = re.sub(r'`+', ' ', before_raw)
    picked = _pick_sentence(before_raw, reverse=True)
    if picked:
        return picked

    after_raw = after or ''
    for _ in range(3):
        after_raw = re.sub(r'^\s*[`\-•*]+', ' ', after_raw)
        after_raw = re.sub(r'^\s*0?\d{1,2}\s+', ' ', after_raw)
        after_raw = re.sub(r'^\s*\*\*([^*\n]{2,80})\*\*\s*', '', after_raw)
    if title:
        after_raw = after_raw.replace(title, ' ', 1)
    after_raw = re.sub(r'!\[[^\]]*\]\([^)]*\)', ' ', after_raw)
    after_raw = re.sub(r'```+', ' ', after_raw)
    after_raw = re.sub(r'`+', ' ', after_raw)
    picked = _pick_sentence(after_raw, reverse=False)
    if picked:
        return picked

    fallback_blocks = [before[-180:], after[:220]]
    for block in fallback_blocks:
        cleaned = clean_inline_text(block, limit=120, ellipsis=False)
        if not cleaned:
            continue
        if title and cleaned.startswith(title):
            cleaned = cleaned[len(title):].lstrip('：:，,。.-— ')
        if len(cleaned) >= 14 and '开源地址' not in cleaned:
            return cleaned

    if title:
        return f'{title}，值得进一步查看实现细节。'
    if project_label:
        return f'{project_label} 相关开源项目。'
    return '相关开源项目。'


def github_intro_quality_bad(text: str):
    raw = clean_inline_text(text or '', limit=160, ellipsis=False)
    if len(raw) < 14:
        return True
    bad_markers = ['代码仓库', '代码链接', '项目主页', 'from=appmsg', 'wx fmt', 'http://', 'https://']
    if any(marker in raw for marker in bad_markers):
        return True
    if re.fullmatch(r'[A-Za-z0-9/._:+\- ]+', raw):
        return True
    chinese_chars = len(re.findall(r'[\u4e00-\u9fff]', raw))
    if chinese_chars == 0 and len(raw) < 30:
        return True
    return False


def github_intro_for_row(row, project_label: str = ''):
    title = _safe_row_text(row, 'title', '')
    raw_title = (title or '').replace('\n', ' ').strip()
    for sep in ['：', ':']:
        if sep in raw_title:
            after = clean_inline_text(raw_title.split(sep, 1)[1], limit=90)
            if len(after) >= 10:
                return after

    summary = clean_inline_text(best_summary_for_row(row), limit=160, ellipsis=False)
    if summary and not summary_is_generic(summary):
        return summary

    content_md = _safe_row_text(row, 'content_md', '')
    snippet = clean_inline_text(extract_source_snippet(content_md, limit=180), limit=160, ellipsis=False)
    if snippet and not summary_is_generic(snippet):
        bad_prefixes = ['导读', '编辑', '新智元报道', '机器之心发布', '量子位', '公众号']
        if not any(snippet.startswith(x) for x in bad_prefixes):
            return snippet

    direction = classify_cluster_direction([{
        'title': title,
        'content_md': content_md,
        'account_name': _safe_row_text(row, 'account_name', '未知公众号'),
    }])
    fallback = clean_inline_text(fallback_summary_from_title(title, direction), limit=160, ellipsis=False)
    if fallback:
        return fallback
    if project_label:
        return f'{project_label} 相关开源项目。'
    return '相关开源项目。'


def compute_source_quality_stats(data):
    total_counts = Counter()
    total_urls = set()
    for record in data.get('cluster_records') or []:
        for row in record.get('rows') or []:
            url = row['article_url']
            if not url or url in total_urls:
                continue
            total_urls.add(url)
            source = row['account_name'] or '未知公众号'
            total_counts[source] += 1

    selected_counts = Counter()
    selected_rep_counts = Counter()
    selected_urls = set()
    for record in data.get('selected_cluster_records') or []:
        rep = record.get('representative') or {}
        rep_source = _safe_row_text(rep, 'account_name', '未知公众号') or '未知公众号'
        selected_rep_counts[rep_source] += 1
        for row in record.get('rows') or []:
            url = row['article_url']
            if not url or url in selected_urls:
                continue
            selected_urls.add(url)
            source = row['account_name'] or '未知公众号'
            selected_counts[source] += 1

    stats = {}
    for source, total in total_counts.items():
        selected = selected_counts.get(source, 0)
        rep_selected = selected_rep_counts.get(source, 0)
        ratio = (selected / total) if total else 0.0
        stats[source] = {
            'total': total,
            'selected': selected,
            'rep_selected': rep_selected,
            'ratio': round(ratio, 4),
            'score': ratio * 100.0 + rep_selected * 6.0 + selected * 0.35,
        }
    return stats


def sort_group_records_by_source_quality(group, source_quality_stats=None):
    source_quality_stats = source_quality_stats or {}
    return sorted(
        group,
        key=lambda item: (
            -(source_quality_stats.get(item['representative']['account_name'] or '未知公众号', {}).get('score', 0.0)),
            -item['score'],
            -item['cluster_size'],
            -item['source_diversity'],
            clean_signal_title(item['representative']['title'] or '', limit=90),
        ),
    )


def signal_group_priority_score(group, direction: str, source_quality_stats=None):
    source_quality_stats = source_quality_stats or {}
    lead = lead_record_for_group(group)
    sources = []
    seen_sources = set()
    for item in group:
        src = item['representative']['account_name'] or '未知公众号'
        if src not in seen_sources:
            seen_sources.add(src)
            sources.append(src)
    article_count = sum(max(1, int(item.get('cluster_size') or 1)) for item in group)
    source_count = len(sources)
    source_quality_bonus = sum(source_quality_stats.get(src, {}).get('score', 0.0) for src in sources[:4]) / max(1, min(4, len(sources)))
    group_bonus = source_count * (3.2 if direction == 'AI / 大模型' else 2.2) + min(8.0, max(0, article_count - 1) * 0.4)
    return lead['score'] + group_bonus + source_quality_bonus * 0.22


def signal_group_key(group):
    return tuple(sorted((item.get('cluster_id') or '') for item in group))


def github_row_relevant(row):
    title = _safe_row_text(row, 'title', '')
    content_md = _safe_row_text(row, 'content_md', '')
    content_html = _safe_row_text(row, 'content_html', '')
    source = _safe_row_text(row, 'account_name', '')
    raw = '\n'.join(filter(None, [title, content_md[:600], content_html[:600], source])).lower()
    strong_ai_markers = [
        'agent', 'llm', 'deepseek', 'gpt', 'qwen', 'kimi', '模型', '大模型', '开源模型', '多模态',
        '机器人', '具身', 'benchmark', '论文', 'iclr', 'cvpr', '推理', '模型评测', '团队协同'
    ]
    soft_ai_markers = [' ai ', 'ai工具', 'ai agent', 'ai工作流']
    utility_noise = ['密码', '书签', '历史记录', '浏览器', '黑客', '取证', '渗透', '抓包']
    if any(k in raw for k in utility_noise) and not any(k in raw for k in strong_ai_markers + soft_ai_markers):
        return False
    if any(k in raw for k in strong_ai_markers + soft_ai_markers):
        return True
    return source in {'机器之心', '量子位', '新智元', 'DeepSource', '老刘说NLP', 'JackCui'}


def collect_github_context(rows):
    items = []
    seen = set()
    url_pattern = re.compile(r'https?://github\.com/[^\s)\]"<>]+')
    for row in rows:
        content_md = row['content_md'] if 'content_md' in row.keys() else ''
        content_html = row['content_html'] if 'content_html' in row.keys() else ''
        combined = '\n'.join(filter(None, [content_md or '', content_html or '']))
        matches = list(url_pattern.finditer(combined or ''))
        if not matches:
            continue
        for idx, match in enumerate(matches):
            url = normalize_github_project_url(match.group(0))
            if not url or url in seen:
                continue
            prev_end = matches[idx - 1].end() if idx > 0 else 0
            next_start = matches[idx + 1].start() if idx + 1 < len(matches) else len(combined)
            before = combined[max(prev_end, match.start() - 240):match.start()]
            after = combined[match.end():min(next_start, match.end() + 900)]
            project_label = github_project_label(url)
            title_hint = github_title_from_context(before, after, url)
            intro_hint = github_intro_from_context(before, after, title_hint, project_label)
            intro = intro_hint
            if github_intro_quality_bad(intro):
                intro = github_intro_for_row(row, project_label)
            seen.add(url)
            items.append({
                'title': row['title'],
                'project_label': project_label,
                'title_hint': title_hint,
                'intro': intro,
                'account_name': row['account_name'] or '未知公众号',
                'url': url,
            })
    return items


def extract_source_snippet(text: str, limit: int = 140):
    raw = re.sub(r'\s+', ' ', (text or '')).strip()
    if not raw:
        return ''
    for sep in ['。', '！', '？', '\n']:
        idx = raw.find(sep)
        if 0 < idx <= limit:
            return raw[:idx + 1]
    return raw[:limit]


def extract_entity_anchors(text: str):
    found = []
    raw = text or ''
    for kw in ENTITY_KEYWORDS:
        if kw in raw and kw not in found:
            found.append(kw)
    return found[:6]


def title_has_entity_anchor(title: str):
    raw = title or ''
    return any(kw in raw for kw in ENTITY_KEYWORDS)


def normalize_focus_term(term: str) -> str:
    raw = clean_inline_text(term or '', limit=60)
    if not raw:
        return ""
    raw = raw.replace('_', '-').strip(' ：:，,。；;|-—·')
    raw = re.sub(r'\s+', ' ', raw)
    return raw


def focus_term_key(term: str) -> str:
    raw = normalize_focus_term(term)
    if not raw:
        return ""
    return normalize_title(raw)


def extract_focus_terms(text: str):
    raw = text or ''
    found = []
    seen = set()

    def add(term: str):
        display = normalize_focus_term(term)
        key = focus_term_key(display)
        if not key or key in seen:
            return
        if key in TOPIC_FOCUS_GENERIC_STOPWORDS:
            return
        if len(key) < 4 and not any(ord(ch) > 127 for ch in display):
            return
        seen.add(key)
        found.append(display)

    for kw in ENTITY_KEYWORDS:
        if kw in raw:
            add(kw)

    for pattern in TOPIC_FOCUS_PATTERNS:
        for match in re.findall(pattern, raw, flags=re.IGNORECASE):
            add(match)

    for token in re.findall(r'\b([A-Za-z][A-Za-z0-9_.-]{2,})\b', raw):
        lower = token.lower()
        if lower in TOPIC_FOCUS_GENERIC_STOPWORDS:
            continue
        if len(token) > 24:
            continue
        if any(ch.isdigit() for ch in token) or any(ch.isupper() for ch in token[1:]) or lower in {
            'deepseek', 'simpletes', 'jiuwenclaw', 'happyhorse', 'modelscope', 'huggingface', 'vllm'
        }:
            add(token)

    return found[:8]


def preferred_directions(profile=None):
    profile = profile or CURRENT_ARTICLE_PREFERENCE_PROFILE or {}
    rules = profile.get('ai_rules') or {}
    output = set()
    for item in rules.get('boost_directions') or []:
        direction = item.get('direction')
        try:
            weight = float(item.get('weight') or 0.0)
        except Exception:
            weight = 0.0
        if direction in STABLE_DIRECTIONS and weight >= 1.0:
            output.add(direction)
    return output


def direction_signal_limit(direction: str) -> int:
    limit = report_signal_limits().get(direction, 1)
    if direction in preferred_directions():
        limit += report_preferred_direction_bonus(direction)
    return min(report_direction_caps().get(direction, limit), limit)


def direction_appendix_limit(direction: str) -> int:
    min_limit = max(1, report_setting_int('appendix_limit_min', 3))
    max_limit = max(min_limit, report_setting_int('appendix_limit_max', 6))
    return max(min_limit, min(max_limit, direction_signal_limit(direction)))


def collect_entity_context(rows):
    items = []
    for row in rows:
        content_md = row['content_md'] if 'content_md' in row.keys() else ''
        snippet = extract_source_snippet(content_md)
        anchors = extract_entity_anchors('\n'.join([row['title'] or '', content_md or '']))
        if not anchors:
            continue
        ambiguous_title = not title_has_entity_anchor(row['title'])
        if not ambiguous_title and not any(x in anchors for x in ['解放军', '东部战区', '以色列', '以军', '伊朗', '伊军', '美军', '白宫']):
            continue
        items.append({
            'title': row['title'],
            'account_name': row['account_name'] or '未知公众号',
            'url': row['article_url'],
            'snippet': snippet,
            'anchors': anchors,
            'ambiguous_title': ambiguous_title,
        })
    return items


def _pref_terms_from_title(title: str):
    terms = []
    for t in title_tokens(title or ''):
        if len(t) < 2:
            continue
        if t in PREFERENCE_TOKEN_STOPWORDS:
            continue
        terms.append(t)
    return terms


def _extract_json_object(text: str):
    raw = (text or '').strip()
    if not raw:
        return None
    raw = re.sub(r'^```(?:json)?\s*', '', raw)
    raw = re.sub(r'\s*```$', '', raw)
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except Exception:
        pass
    start = raw.find('{')
    end = raw.rfind('}')
    if start >= 0 and end > start:
        try:
            data = json.loads(raw[start:end + 1])
            return data if isinstance(data, dict) else None
        except Exception:
            return None
    return None


def validate_preference_profile_summary_output(text: str):
    data = _extract_json_object(text)
    if not data:
        return 'not valid json object'
    for key in ['summary', 'likes', 'dislikes', 'neutral_observations', 'selection_advice']:
        if key not in data:
            return f'missing key: {key}'
    if not isinstance(data.get('summary'), str) or len(data.get('summary', '').strip()) < 12:
        return 'summary too short'
    for key in ['likes', 'dislikes', 'neutral_observations', 'selection_advice']:
        if not isinstance(data.get(key), list):
            return f'{key} is not a list'
    return None


def validate_preference_profile_rules_output(text: str):
    data = _extract_json_object(text)
    if not data:
        return 'not valid json object'
    for key in ['boost_directions', 'suppress_directions', 'boost_sources', 'suppress_sources', 'boost_terms', 'suppress_terms']:
        if key not in data:
            return f'missing key: {key}'
        if not isinstance(data.get(key), list):
            return f'{key} is not a list'
    return None


def _clean_rule_items(items, key_name: str, allowed_values=None, max_items: int = 4):
    cleaned = []
    seen = set()
    for item in items or []:
        if not isinstance(item, dict):
            continue
        key = str(item.get(key_name) or '').strip()
        if not key:
            continue
        if allowed_values and key not in allowed_values:
            continue
        if key in seen:
            continue
        seen.add(key)
        try:
            weight = float(item.get('weight') or 0.0)
        except Exception:
            weight = 0.0
        weight = max(0.5, min(3.0, weight))
        reason = str(item.get('reason') or '').strip()
        cleaned.append({key_name: key, 'weight': round(weight, 2), 'reason': reason})
        if len(cleaned) >= max_items:
            break
    return cleaned


def _infer_direction_signal_from_titles(titles):
    counts = defaultdict(int)
    for title in titles or []:
        pseudo = {'title': title, 'content_md': '', 'account_name': '未知公众号'}
        direction = classify_cluster_direction([pseudo])
        if direction and direction != '其他观察':
            counts[direction] += 1
    return counts


def fallback_preference_profile_rules(profile_seed: dict):
    follow_dirs = _infer_direction_signal_from_titles(profile_seed.get('followed_titles') or [])
    ignore_dirs = _infer_direction_signal_from_titles(profile_seed.get('ignored_titles') or [])
    boost_directions = [
        {'direction': k, 'weight': min(3.0, 1.0 + v * 0.4), 'reason': 'follow 样本中该方向出现频率较高'}
        for k, v in sorted(follow_dirs.items(), key=lambda kv: (-kv[1], kv[0]))[:4]
    ]
    suppress_directions = [
        {'direction': k, 'weight': min(3.0, 1.0 + v * 0.4), 'reason': 'ignore 样本中该方向出现频率较高'}
        for k, v in sorted(ignore_dirs.items(), key=lambda kv: (-kv[1], kv[0]))[:4]
        if k not in {x['direction'] for x in boost_directions}
    ]
    boost_sources = [
        {'source': x, 'weight': 2.2 - i * 0.25, 'reason': '规则统计显示该来源长期正向'}
        for i, x in enumerate((profile_seed.get('preferred_sources') or [])[:4])
    ]
    suppress_sources = [
        {'source': x, 'weight': 2.0 - i * 0.2, 'reason': '规则统计显示该来源长期负向'}
        for i, x in enumerate((profile_seed.get('ignored_sources') or [])[:4])
    ]
    boost_terms = [
        {'term': x, 'weight': 1.6 - i * 0.12, 'reason': '规则统计显示该主题词更常出现在关注样本中'}
        for i, x in enumerate((profile_seed.get('preferred_terms') or [])[:6])
    ]
    suppress_terms = [
        {'term': x, 'weight': 1.5 - i * 0.1, 'reason': '规则统计显示该主题词更常出现在忽略样本中'}
        for i, x in enumerate((profile_seed.get('ignored_terms') or [])[:6])
    ]
    return {
        'boost_directions': boost_directions,
        'suppress_directions': suppress_directions,
        'boost_sources': boost_sources,
        'suppress_sources': suppress_sources,
        'boost_terms': boost_terms,
        'suppress_terms': suppress_terms,
        'model_used': 'rules-derived',
        'fallback_used': False,
        'fallback_reason': 'deterministic fallback',
    }


def fallback_preference_profile_summary(preferred_sources, ignored_sources, preferred_terms, ignored_terms, followed_titles, ignored_titles):
    likes = []
    dislikes = []
    advice = []
    if preferred_sources:
        likes.append(f"更容易关注 {', '.join(preferred_sources[:4])} 这类来源的内容")
    if preferred_terms:
        likes.append(f"高频正向兴趣集中在 {', '.join(preferred_terms[:6])} 等主题")
    if ignored_sources:
        dislikes.append(f"对 {', '.join(ignored_sources[:4])} 这类来源整体兴趣偏低")
    if ignored_terms:
        dislikes.append(f"对 {', '.join(ignored_terms[:6])} 这类选题更容易直接忽略")
    if preferred_terms:
        advice.append(f"日报选稿可优先提高 {', '.join(preferred_terms[:4])} 相关内容的曝光")
    if ignored_terms:
        advice.append(f"对 {', '.join(ignored_terms[:4])} 这类低信号内容应降低权重")
    advice.append('对边界模糊题材，优先结合人工 follow/ignore 样本继续校准')
    summary = '当前画像以规则统计为主，已能看出较稳定的来源偏好与主题偏好，但抽象层面的兴趣总结仍偏保守。'
    if likes:
        summary = likes[0] + '。'
    return {
        'summary': summary,
        'likes': likes[:4],
        'dislikes': dislikes[:4],
        'neutral_observations': [
            '中性标记更多表示“已看过但暂不明显加减分”，不应等同于不感兴趣。'
        ],
        'selection_advice': advice[:4],
    }


def generate_ai_preference_profile_summary(profile_seed: dict):
    labeled_count = int(profile_seed.get('labeled_count') or 0)
    if labeled_count < 12:
        return {
            **fallback_preference_profile_summary(
                profile_seed.get('preferred_sources') or [],
                profile_seed.get('ignored_sources') or [],
                profile_seed.get('preferred_terms') or [],
                profile_seed.get('ignored_terms') or [],
                profile_seed.get('followed_titles') or [],
                profile_seed.get('ignored_titles') or [],
            ),
            'model_used': 'rules-only',
            'fallback_used': False,
            'fallback_reason': 'not enough labeled samples',
        }

    lines = [
        f"已标注文章：{profile_seed.get('labeled_count') or 0}",
        f"关注：{profile_seed.get('follow_count') or 0}",
        f"忽略：{profile_seed.get('ignore_count') or 0}",
        f"中性：{profile_seed.get('neutral_count') or 0}",
        '',
        f"更常关注的来源：{', '.join((profile_seed.get('preferred_sources') or [])[:8]) or '无'}",
        f"更常忽略的来源：{', '.join((profile_seed.get('ignored_sources') or [])[:8]) or '无'}",
        f"更偏好的主题词：{', '.join((profile_seed.get('preferred_terms') or [])[:12]) or '无'}",
        f"更常忽略的主题词：{', '.join((profile_seed.get('ignored_terms') or [])[:12]) or '无'}",
        '',
        '近期关注样本：',
    ]
    for item in (profile_seed.get('followed_titles') or [])[:10]:
        lines.append(f'- {item}')
    lines.append('')
    lines.append('近期忽略样本：')
    for item in (profile_seed.get('ignored_titles') or [])[:10]:
        lines.append(f'- {item}')
    lines.append('')
    lines.append('近期中性样本：')
    for item in (profile_seed.get('neutral_titles') or [])[:10]:
        lines.append(f'- {item}')

    raw = call_model_with_fallback(
        PREFERENCE_PROFILE_SUMMARY_PROMPT,
        '\n'.join(lines),
        max_retries=1,
        validate_fn=validate_preference_profile_summary_output,
        invalid_prefix='invalid preference profile summary output',
    )
    payload = _extract_json_object(raw) if isinstance(raw, str) and not raw.startswith('ERROR:') else None
    if not payload:
        payload = fallback_preference_profile_summary(
            profile_seed.get('preferred_sources') or [],
            profile_seed.get('ignored_sources') or [],
            profile_seed.get('preferred_terms') or [],
            profile_seed.get('ignored_terms') or [],
            profile_seed.get('followed_titles') or [],
            profile_seed.get('ignored_titles') or [],
        )
    payload['likes'] = [str(x).strip() for x in (payload.get('likes') or []) if str(x).strip()][:4]
    payload['dislikes'] = [str(x).strip() for x in (payload.get('dislikes') or []) if str(x).strip()][:4]
    payload['neutral_observations'] = [str(x).strip() for x in (payload.get('neutral_observations') or []) if str(x).strip()][:3]
    payload['selection_advice'] = [str(x).strip() for x in (payload.get('selection_advice') or []) if str(x).strip()][:4]
    payload['summary'] = str(payload.get('summary') or '').strip() or '当前偏好画像尚未生成稳定总结。'
    payload['model_used'] = LAST_PROVIDER_USED or 'unknown'
    payload['fallback_used'] = bool(LAST_FALLBACK_USED)
    payload['fallback_reason'] = LAST_FALLBACK_REASON or ''
    return payload


def generate_ai_preference_profile_rules(profile_seed: dict, ai_summary: dict):
    if int(profile_seed.get('labeled_count') or 0) < 12:
        return fallback_preference_profile_rules(profile_seed)

    lines = [
        '统计摘要：',
        f"- 已标注文章：{profile_seed.get('labeled_count') or 0}",
        f"- 关注：{profile_seed.get('follow_count') or 0}",
        f"- 忽略：{profile_seed.get('ignore_count') or 0}",
        f"- 中性：{profile_seed.get('neutral_count') or 0}",
        f"- 更常关注的来源：{', '.join((profile_seed.get('preferred_sources') or [])[:8]) or '无'}",
        f"- 更常忽略的来源：{', '.join((profile_seed.get('ignored_sources') or [])[:8]) or '无'}",
        f"- 更偏好的主题词：{', '.join((profile_seed.get('preferred_terms') or [])[:12]) or '无'}",
        f"- 更常忽略的主题词：{', '.join((profile_seed.get('ignored_terms') or [])[:12]) or '无'}",
        '',
        'AI 偏好总结：',
        f"- 总结：{ai_summary.get('summary') or ''}",
    ]
    for key, label in [('likes', '更可能持续关注'), ('dislikes', '更可能忽略'), ('selection_advice', '后续选稿建议')]:
        values = ai_summary.get(key) or []
        if values:
            lines.append(f"- {label}：{'；'.join(values[:4])}")
    lines.extend(['', '近期关注样本：'])
    for item in (profile_seed.get('followed_titles') or [])[:8]:
        lines.append(f'- {item}')
    lines.extend(['', '近期忽略样本：'])
    for item in (profile_seed.get('ignored_titles') or [])[:8]:
        lines.append(f'- {item}')

    raw = call_model_with_fallback(
        PREFERENCE_PROFILE_RULES_PROMPT,
        '\n'.join(lines),
        max_retries=1,
        validate_fn=validate_preference_profile_rules_output,
        invalid_prefix='invalid preference profile rules output',
    )
    payload = _extract_json_object(raw) if isinstance(raw, str) and not raw.startswith('ERROR:') else None
    if not payload:
        payload = fallback_preference_profile_rules(profile_seed)

    cleaned = {
        'boost_directions': _clean_rule_items(payload.get('boost_directions'), 'direction', allowed_values=set(STABLE_DIRECTIONS) - {'其他观察'}, max_items=4),
        'suppress_directions': _clean_rule_items(payload.get('suppress_directions'), 'direction', allowed_values=set(STABLE_DIRECTIONS) - {'其他观察'}, max_items=4),
        'boost_sources': _clean_rule_items(payload.get('boost_sources'), 'source', max_items=4),
        'suppress_sources': _clean_rule_items(payload.get('suppress_sources'), 'source', max_items=4),
        'boost_terms': _clean_rule_items(payload.get('boost_terms'), 'term', max_items=6),
        'suppress_terms': _clean_rule_items(payload.get('suppress_terms'), 'term', max_items=6),
        'model_used': LAST_PROVIDER_USED or payload.get('model_used') or 'unknown',
        'fallback_used': bool(LAST_FALLBACK_USED) or bool(payload.get('fallback_used')),
        'fallback_reason': LAST_FALLBACK_REASON or payload.get('fallback_reason') or '',
    }
    return cleaned


def refresh_article_preference_profile():
    global CURRENT_ARTICLE_PREFERENCE_PROFILE
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    agg = conn.execute(
        """
        SELECT COUNT(*) AS labeled_count,
               MAX(COALESCE(user_pref_updated_at, discovered_at)) AS last_labeled_at
        FROM articles
        WHERE user_pref_state IN ('follow', 'ignore', 'neutral')
        """
    ).fetchone()
    rows = conn.execute(
        """
        SELECT a.id, a.title, a.article_url, a.user_pref_state, a.user_pref_updated_at,
               tg.account_name
        FROM articles a
        LEFT JOIN targets tg ON a.target_id = tg.id
        WHERE a.user_pref_state IN ('follow', 'ignore', 'neutral')
        ORDER BY COALESCE(a.user_pref_updated_at, a.discovered_at) DESC, a.id DESC
        """
    ).fetchall()
    conn.close()

    source_scores = defaultdict(float)
    term_scores = defaultdict(float)
    followed_titles = []
    ignored_titles = []
    neutral_titles = []
    follow_count = 0
    ignore_count = 0
    neutral_count = 0
    for row in rows:
        state = (row['user_pref_state'] or '').strip().lower()
        if state not in {'follow', 'ignore', 'neutral'}:
            continue
        weight = 1.0 if state == 'follow' else (-1.0 if state == 'ignore' else 0.0)
        if state == 'follow':
            follow_count += 1
            if len(followed_titles) < 8:
                followed_titles.append(clean_signal_title(row['title'] or '', 90))
        elif state == 'ignore':
            ignore_count += 1
            if len(ignored_titles) < 8:
                ignored_titles.append(clean_signal_title(row['title'] or '', 90))
        else:
            neutral_count += 1
            if len(neutral_titles) < 8:
                neutral_titles.append(clean_signal_title(row['title'] or '', 90))
        src = row['account_name'] or '未知公众号'
        source_scores[src] += 3.0 * weight
        for term in _pref_terms_from_title(row['title'] or ''):
            term_scores[term] += 1.0 * weight

    preferred_sources = [k for k, v in sorted(source_scores.items(), key=lambda kv: (-kv[1], kv[0])) if v > 0][:8]
    ignored_sources = [k for k, v in sorted(source_scores.items(), key=lambda kv: (kv[1], kv[0])) if v < 0][:8]
    neutral_sources = [k for k, v in sorted(source_scores.items(), key=lambda kv: (abs(kv[1]), kv[0])) if v == 0][:8]
    preferred_terms = [k for k, v in sorted(term_scores.items(), key=lambda kv: (-kv[1], kv[0])) if v > 0][:16]
    ignored_terms = [k for k, v in sorted(term_scores.items(), key=lambda kv: (kv[1], kv[0])) if v < 0][:16]
    neutral_terms = [k for k, v in sorted(term_scores.items(), key=lambda kv: (abs(kv[1]), kv[0])) if v == 0][:16]

    profile_seed = {
        'labeled_count': len(rows),
        'follow_count': follow_count,
        'ignore_count': ignore_count,
        'neutral_count': neutral_count,
        'preferred_sources': preferred_sources,
        'ignored_sources': ignored_sources,
        'preferred_terms': preferred_terms,
        'ignored_terms': ignored_terms,
        'followed_titles': followed_titles,
        'ignored_titles': ignored_titles,
        'neutral_titles': neutral_titles,
    }
    ai_summary = generate_ai_preference_profile_summary(profile_seed)
    ai_rules = generate_ai_preference_profile_rules(profile_seed, ai_summary)

    lines = [
        '# 文章偏好画像',
        '',
        '## AI 总结',
        f"- {ai_summary.get('summary') or '暂无 AI 总结'}",
        '',
    ]
    if ai_summary.get('likes'):
        lines.append('### 更可能持续关注')
        lines.extend([f"- {x}" for x in ai_summary['likes'][:4]])
        lines.append('')
    if ai_summary.get('dislikes'):
        lines.append('### 更可能忽略')
        lines.extend([f"- {x}" for x in ai_summary['dislikes'][:4]])
        lines.append('')
    if ai_summary.get('selection_advice'):
        lines.append('### 选稿建议')
        lines.extend([f"- {x}" for x in ai_summary['selection_advice'][:4]])
        lines.append('')

    if any((ai_rules.get(key) or []) for key in ['boost_directions', 'suppress_directions', 'boost_sources', 'suppress_sources', 'boost_terms', 'suppress_terms']):
        lines.append('### AI 结构化规则')
        for key, label, item_key in [
            ('boost_directions', '方向提权', 'direction'),
            ('suppress_directions', '方向压低', 'direction'),
            ('boost_sources', '来源提权', 'source'),
            ('suppress_sources', '来源压低', 'source'),
            ('boost_terms', '主题词提权', 'term'),
            ('suppress_terms', '主题词压低', 'term'),
        ]:
            items = ai_rules.get(key) or []
            if not items:
                continue
            lines.append(f"- {label}：" + '；'.join([f"{x.get(item_key)}(+{x.get('weight')})" for x in items]))
        lines.append('')

    lines.extend([
        '## 规则统计',
        '',
        f'- 已标注文章：{len(rows)} 篇',
        f'- 关注：{follow_count} 篇',
        f'- 忽略：{ignore_count} 篇',
        f'- 中性：{neutral_count} 篇',
        '',
    ])
    if preferred_sources:
        lines.append(f"- 更常关注的来源：{', '.join(preferred_sources)}")
    if ignored_sources:
        lines.append(f"- 更常忽略的来源：{', '.join(ignored_sources)}")
    if neutral_sources:
        lines.append(f"- 中性来源：{', '.join(neutral_sources[:10])}")
    if preferred_terms:
        lines.append(f"- 更偏好的主题词：{', '.join(preferred_terms[:10])}")
    if ignored_terms:
        lines.append(f"- 更常忽略的主题词：{', '.join(ignored_terms[:10])}")
    if neutral_terms:
        lines.append(f"- 中性主题词：{', '.join(neutral_terms[:10])}")
    if followed_titles:
        lines.extend(['', '## 近期关注样本'])
        lines.extend([f"- {x}" for x in followed_titles[:5]])
    if ignored_titles:
        lines.extend(['', '## 近期忽略样本'])
        lines.extend([f"- {x}" for x in ignored_titles[:5]])
    if neutral_titles:
        lines.extend(['', '## 近期中性样本'])
        lines.extend([f"- {x}" for x in neutral_titles[:5]])

    profile = {
        'updated_at': now_str(),
        'last_labeled_at': agg['last_labeled_at'] if agg else '',
        'labeled_count': len(rows),
        'follow_count': follow_count,
        'ignore_count': ignore_count,
        'neutral_count': neutral_count,
        'preferred_sources': preferred_sources,
        'ignored_sources': ignored_sources,
        'neutral_sources': neutral_sources,
        'preferred_terms': preferred_terms,
        'ignored_terms': ignored_terms,
        'neutral_terms': neutral_terms,
        'followed_titles': followed_titles,
        'ignored_titles': ignored_titles,
        'neutral_titles': neutral_titles,
        'ai_summary': ai_summary,
        'ai_model_used': ai_summary.get('model_used') or '',
        'ai_fallback_used': bool(ai_summary.get('fallback_used')),
        'ai_fallback_reason': ai_summary.get('fallback_reason') or '',
        'ai_rules': ai_rules,
        'ai_rules_model_used': ai_rules.get('model_used') or '',
        'ai_rules_fallback_used': bool(ai_rules.get('fallback_used')),
        'ai_rules_fallback_reason': ai_rules.get('fallback_reason') or '',
        'description': '\n'.join(lines).strip(),
        'source_scores': dict(source_scores),
        'term_scores': dict(term_scores),
    }
    ARTICLE_PREF_PROFILE_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    ARTICLE_PREF_PROFILE_JSON_PATH.write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding='utf-8')
    ARTICLE_PREF_PROFILE_MD_PATH.write_text(profile['description'] + '\n', encoding='utf-8')
    CURRENT_ARTICLE_PREFERENCE_PROFILE = profile
    return profile


def ensure_article_preference_profile():
    global CURRENT_ARTICLE_PREFERENCE_PROFILE
    existing = None
    if ARTICLE_PREF_PROFILE_JSON_PATH.exists():
        try:
            existing = json.loads(ARTICLE_PREF_PROFILE_JSON_PATH.read_text(encoding='utf-8'))
        except Exception:
            existing = None

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    agg = conn.execute(
        """
        SELECT COUNT(*) AS labeled_count,
               MAX(COALESCE(user_pref_updated_at, discovered_at)) AS last_labeled_at
        FROM articles
        WHERE user_pref_state IN ('follow', 'ignore', 'neutral')
        """
    ).fetchone()
    conn.close()

    labeled_count = int((agg['labeled_count'] if agg and agg['labeled_count'] is not None else 0) or 0)
    last_labeled_at = (agg['last_labeled_at'] if agg else '') or ''
    today = now_str()[:10]

    if existing:
        CURRENT_ARTICLE_PREFERENCE_PROFILE = existing
        updated_day = (existing.get('updated_at') or '')[:10]
        existing_last_labeled = (existing.get('last_labeled_at') or '')
        existing_labeled_count = int(existing.get('labeled_count') or 0)
        has_new_labels = (last_labeled_at != existing_last_labeled) or (labeled_count != existing_labeled_count)
        if updated_day == today or not has_new_labels:
            return existing

    return refresh_article_preference_profile()


def article_preference_score(row):
    profile = CURRENT_ARTICLE_PREFERENCE_PROFILE or {}
    score = 0.0
    state = (row['user_pref_state'] if 'user_pref_state' in row.keys() else '') or ''
    state = state.strip().lower()
    if state == 'follow':
        score += 12.0
    elif state == 'ignore':
        score -= 12.0
    source = row['account_name'] if 'account_name' in row.keys() else ''
    source = source or '未知公众号'
    score += max(-6.0, min(6.0, (profile.get('source_scores') or {}).get(source, 0.0) * 1.4))
    term_scores = profile.get('term_scores') or {}
    title = row['title'] if 'title' in row.keys() else ''
    token_score = 0.0
    for term in _pref_terms_from_title(title or ''):
        token_score += term_scores.get(term, 0.0)
    score += max(-6.0, min(6.0, token_score * 0.8))
    score += ai_rule_preference_score(row, profile=profile)
    return score


def ai_rule_preference_score(row, profile=None):
    profile = profile or CURRENT_ARTICLE_PREFERENCE_PROFILE or {}
    rules = profile.get('ai_rules') or {}
    if not rules:
        return 0.0
    title = (row['title'] if 'title' in row.keys() else '') or ''
    content_md = (row['content_md'] if 'content_md' in row.keys() else '') or ''
    source = (row['account_name'] if 'account_name' in row.keys() else '') or '未知公众号'
    raw = f"{title}\n{content_md}".lower()
    tokens = set(_pref_terms_from_title(title or ''))
    direction = classify_cluster_direction([{
        'title': title,
        'content_md': content_md,
        'account_name': source,
    }])

    score = 0.0
    for item in rules.get('boost_directions') or []:
        if item.get('direction') == direction:
            score += float(item.get('weight') or 0.0)
    for item in rules.get('suppress_directions') or []:
        if item.get('direction') == direction:
            score -= float(item.get('weight') or 0.0)
    for item in rules.get('boost_sources') or []:
        if item.get('source') == source:
            score += float(item.get('weight') or 0.0)
    for item in rules.get('suppress_sources') or []:
        if item.get('source') == source:
            score -= float(item.get('weight') or 0.0)
    for item in rules.get('boost_terms') or []:
        term = str(item.get('term') or '').strip().lower()
        if _ai_rule_term_matches(term, raw, tokens):
            score += float(item.get('weight') or 0.0)
    for item in rules.get('suppress_terms') or []:
        term = str(item.get('term') or '').strip().lower()
        if _ai_rule_term_matches(term, raw, tokens):
            score -= float(item.get('weight') or 0.0)

    return max(-8.0, min(8.0, score))


def _ai_rule_term_matches(term: str, raw: str, tokens: set):
    term = (term or '').strip().lower()
    if not term:
        return False
    if term in raw or term in tokens:
        return True
    variants = [x.strip().lower() for x in re.split(r'[/、，,；;|+]|\s+', term) if x.strip()]
    for variant in variants:
        if len(variant) < 2:
            continue
        if variant in raw or variant in tokens:
            return True
    return False


def record_preference_score(record):
    rows = record.get('rows') or []
    if not rows:
        return 0.0
    row_scores = [article_preference_score(r) for r in rows]
    if not row_scores:
        return 0.0
    return max(row_scores)


def repair_entity_attribution_if_needed(report_text: str, entity_items):
    if not entity_items:
        return report_text
    lines = [
        '候选源证据（标题模糊时必须以这些证据中的明确主体为准）：'
    ]
    for item in entity_items[:60]:
        ambiguous = '是' if item.get('ambiguous_title') else '否'
        lines.append(
            f"- 标题：{item['title']} | 来源：{item['account_name']} | 链接：{item['url']} | 标题是否歧义：{ambiguous} | 明确主体：{', '.join(item['anchors'])} | 原文首句：{item['snippet']}"
        )
    lines.append('')
    lines.append('待修复日报正文：')
    lines.append(report_text)
    fixed = call_model_with_fallback(
        ENTITY_REPAIR_PROMPT,
        '\n'.join(lines),
        max_retries=1,
        validate_fn=validate_repair_text_output,
        invalid_prefix="invalid entity repair output",
    )
    if isinstance(fixed, str) and fixed.strip() and not fixed.startswith('ERROR:'):
        return fixed
    return report_text


def repair_github_links_if_needed(report_text: str, github_items):
    if 'github.com/...' not in report_text and 'https://github.com/...' not in report_text:
        return report_text
    if not github_items:
        return report_text.replace('https://github.com/...', '').replace('github.com/...', '')
    lines = ["候选完整 GitHub 地址列表："]
    for item in github_items[:80]:
        lines.append(f"- {item['title']} | {item['account_name']} | {item['url']}")
    lines.append("")
    lines.append("待修复日报正文：")
    lines.append(report_text)
    fixed = call_model_with_fallback(
        GITHUB_REPAIR_PROMPT,
        '\n'.join(lines),
        max_retries=1,
        validate_fn=validate_repair_text_output,
        invalid_prefix="invalid github repair output",
    )
    if isinstance(fixed, str) and 'github.com/...' not in fixed and 'https://github.com/...' not in fixed and fixed.strip():
        return fixed
    return report_text


def parse_appendix_groups(report_text: str):
    groups = {}
    in_appendix = False
    current_group = None
    for raw in (report_text or "").splitlines():
        s = raw.strip()
        if s.startswith("## ") and "链接附录" in s:
            in_appendix = True
            continue
        if not in_appendix:
            continue
        if s.startswith("**") and s.endswith("**"):
            current_group = s.strip("*").strip()
            groups.setdefault(current_group, [])
            continue
        if not s.startswith("- ") or not current_group:
            continue
        body = s[2:]
        parts = [p.strip() for p in body.split(" | ")]
        if len(parts) >= 3 and parts[2].startswith("http"):
            groups[current_group].append({
                "title": parts[0],
                "source": parts[1],
                "url": parts[2],
                "reason": parts[3] if len(parts) > 3 else "",
            })
    return groups


def parse_report_sections(report_text: str):
    sections = {}
    current = None
    current_lines = []
    for raw in (report_text or "").splitlines():
        s = raw.rstrip("\n")
        stripped = s.strip()
        if stripped.startswith("### "):
            if current:
                sections[current] = "\n".join(current_lines).strip()
            current = re.sub(r'^\d+[\.、]\s*', '', stripped[4:]).strip()
            current_lines = []
            continue
        if stripped.startswith("## ") and current:
            sections[current] = "\n".join(current_lines).strip()
            current = None
            current_lines = []
        if current is not None:
            current_lines.append(s)
    if current:
        sections[current] = "\n".join(current_lines).strip()
    return sections


def build_section_source_candidates(selected_cluster_records):
    mapping = defaultdict(list)
    seen = set()
    for record in selected_cluster_records or []:
        direction = record.get("direction") or ""
        for row in record.get("rows") or []:
            article_url = row["article_url"] if "article_url" in row.keys() else ""
            account_name = row["account_name"] if "account_name" in row.keys() else "未知公众号"
            title = row["title"] if "title" in row.keys() else ""
            key = (direction, article_url, account_name, title)
            if key in seen:
                continue
            seen.add(key)
            mapping[direction].append({
                "title": title or "",
                "source": account_name or "未知公众号",
                "url": article_url or "",
                "reason": "",
            })
    return dict(mapping)


def _match_terms_for_source(text: str):
    raw = (text or "").lower()
    raw = re.sub(r'https?://\S+', ' ', raw)
    terms = set()
    for chunk in re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]+", raw):
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


def score_signal_source_item(signal_title: str, signal_line: str, item: dict):
    title = item.get("title") or ""
    score = 0.0
    st = _match_terms_for_source(signal_title)
    sl = _match_terms_for_source(signal_line)
    it = _match_terms_for_source(title)
    score += min(18, len(st & it) * 3.5)
    score += min(12, len(sl & it) * 1.8)
    signal_norm = re.sub(r'\s+', '', signal_title.lower())
    title_norm = re.sub(r'\s+', '', title.lower())
    if signal_norm and title_norm:
        if signal_norm in title_norm or title_norm in signal_norm:
            score += 15
        score += SequenceMatcher(None, signal_norm[:100], title_norm[:100]).ratio() * 10
    return score


def infer_signal_sources(kind: str, signal_title: str, signal_line: str, appendix_items, fallback_items=None):
    pool = list(appendix_items or []) + [x for x in (fallback_items or []) if x not in (appendix_items or [])]
    ranked = sorted(
        pool,
        key=lambda item: score_signal_source_item(signal_title, signal_line, item),
        reverse=True,
    )
    ranked = [item for item in ranked if score_signal_source_item(signal_title, signal_line, item) >= 6]
    if not ranked and pool:
        ranked = pool[:3]
    if not ranked:
        return ""
    if kind == "多源共识":
        picked = []
        seen = set()
        for item in ranked:
            src = (item.get("source") or "").strip()
            if not src or src in seen:
                continue
            seen.add(src)
            picked.append(src)
            if len(picked) >= 2:
                break
        return "/".join(picked)
    return (ranked[0].get("source") or "").strip()


def repair_signal_sources_if_needed(report_text: str, selected_cluster_records=None):
    if not report_text or "【单点信号" not in report_text and "【多源共识" not in report_text:
        return report_text
    appendix_groups = parse_appendix_groups(report_text)
    section_candidates = build_section_source_candidates(selected_cluster_records)
    if not appendix_groups and not section_candidates:
        return report_text

    current_section = None
    out_lines = []
    for raw in (report_text or "").splitlines():
        line = raw
        stripped = line.strip()
        if stripped.startswith("### "):
            current_section = re.sub(r'^\d+[\.、]\s*', '', stripped[4:]).strip()
        if current_section and stripped.startswith("**") and ("【单点信号" in stripped or "【多源共识" in stripped):
            title_match = re.match(r"\*\*(.*?)\*\*", stripped)
            signal_title = title_match.group(1).strip() if title_match else stripped
            items = appendix_groups.get(current_section) or []
            fallback_items = section_candidates.get(current_section) or []
            kind = "多源共识" if "【多源共识" in stripped else "单点信号"
            inferred = infer_signal_sources(kind, signal_title, stripped, items, fallback_items=fallback_items)
            if inferred:
                line = re.sub(rf"【{kind}(?:·[^】]+)?】", f"【{kind}·{inferred}】", line)
        out_lines.append(line)
    return "\n".join(out_lines)


def load_recent_history_rows(window_start, days: int = 7):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT a.id, a.title, a.article_url, a.discovered_at, a.target_id,
               tg.account_name
        FROM articles a
        LEFT JOIN targets tg ON a.target_id = tg.id
        WHERE a.discovered_at > datetime(?, ?)
          AND a.discovered_at <= ?
        ORDER BY a.discovered_at DESC, a.id DESC
        """,
        (window_start, f"-{days} days", window_start),
    ).fetchall()
    conn.close()
    return rows


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


def validate_semantic_dedupe_output(text: str):
    parsed = extract_json_object(text)
    if not isinstance(parsed, dict):
        return "semantic dedupe output is not JSON"
    if "duplicates" not in parsed:
        return "semantic dedupe output is missing duplicates field"
    if not isinstance(parsed.get("duplicates"), list):
        return "semantic dedupe duplicates field is not a list"
    return None


def validate_repair_text_output(text: str):
    if not isinstance(text, str):
        return "repair output is not text"
    if not text.strip():
        return "repair output is empty"
    return None


def history_candidates(row, history_rows, top_k: int = 3):
    current_norm = normalize_title(row["title"])
    current_tokens = title_tokens(row["title"])
    scored = []
    for h in history_rows:
        hist_norm = normalize_title(h["title"])
        hist_tokens = title_tokens(h["title"])
        overlap = len(current_tokens & hist_tokens)
        score = row_similarity(row, h)
        if current_norm and hist_norm and (current_norm[:10] in hist_norm or hist_norm[:10] in current_norm):
            score += 0.35
        if overlap >= 2:
            score += min(0.28, overlap * 0.06)
        if score < 0.18:
            continue
        scored.append((score, h))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [item for _, item in scored[:top_k]]


def _title_prefix(text: str):
    raw = clean_signal_title(text or '', limit=90)
    parts = re.split(r'[：:｜|;；,，。!?！？]', raw)
    first = (parts[0] or '').strip()
    return first


def deterministic_semantic_duplicate(row, cand):
    row_title = row["title"] or ''
    cand_title = cand["title"] or ''
    row_norm = normalize_title(row_title)
    cand_norm = normalize_title(cand_title)
    sim = row_similarity(row, cand)
    ratio = SequenceMatcher(None, row_norm[:120], cand_norm[:120]).ratio() if row_norm and cand_norm else 0.0
    row_tokens = title_tokens(row_title)
    cand_tokens = title_tokens(cand_title)
    overlap = len(row_tokens & cand_tokens)
    row_prefix = _title_prefix(row_title)
    cand_prefix = _title_prefix(cand_title)
    row_anchors = set(extract_entity_anchors(row_title))
    cand_anchors = set(extract_entity_anchors(cand_title))
    row_source = row["account_name"] if "account_name" in row.keys() else ''
    cand_source = cand["account_name"] if "account_name" in cand.keys() else ''
    same_source = (row_source or '') == (cand_source or '')

    if row_norm and cand_norm and row_norm == cand_norm:
        return True, 1.0, "normalized titles identical"
    if row_norm and cand_norm and (row_norm in cand_norm or cand_norm in row_norm) and min(len(row_norm), len(cand_norm)) >= 12:
        return True, 0.95, "one normalized title contains the other"
    if row_prefix and cand_prefix and row_prefix == cand_prefix and len(row_prefix) >= 10:
        return True, 0.88, "same leading title prefix"
    if sim >= 0.72 or ratio >= 0.92:
        return True, max(sim, ratio), "very high title similarity"
    if same_source and (sim >= 0.52 or ratio >= 0.84):
        return True, max(sim, ratio), "same source and high similarity"
    if row_anchors and cand_anchors and row_anchors & cand_anchors and (sim >= 0.42 or ratio >= 0.8):
        return True, max(sim, ratio), f"same entity anchors: {'/'.join(sorted(row_anchors & cand_anchors))}"
    if overlap >= 4 and ratio >= 0.78:
        return True, ratio, "high token overlap with similar title"
    return False, max(sim, ratio), ""


def semantic_week_dedupe(rows, window_start):
    history_rows = load_recent_history_rows(window_start, days=7)
    if not rows or not history_rows:
        return {"rows": rows, "history_count": len(history_rows), "semantic_removed": 0, "decisions": []}

    duplicates = {}
    decisions = []
    for idx, row in enumerate(rows, start=1):
        candidates = history_candidates(row, history_rows, top_k=5)
        if not candidates:
            continue
        best = None
        for cand in candidates:
            is_dup, confidence, reason = deterministic_semantic_duplicate(row, cand)
            if not is_dup:
                continue
            if not best or confidence > best[1]:
                best = (cand, confidence, reason)
        if best:
            cand, confidence, reason = best
            current_id = f"c{idx}"
            duplicates[current_id] = True
            decisions.append({
                "current_id": current_id,
                "matched_history": cand['title'],
                "reason": f"{reason}; confidence={confidence:.2f}",
            })

    kept = []
    for idx, row in enumerate(rows, start=1):
        if duplicates.get(f"c{idx}"):
            continue
        kept.append(row)

    return {
        "rows": kept,
        "history_count": len(history_rows),
        "semantic_removed": len(rows) - len(kept),
        "decisions": decisions,
    }


def cluster_combined_text(cluster):
    parts = []
    for row in cluster[:6]:
        parts.append(_safe_text(row.get("title") if hasattr(row, 'get') else row["title"]))
        content_md = (row["content_md"] or "") if 'content_md' in row.keys() else ''
        if content_md:
            parts.append(extract_source_snippet(content_md, limit=100))
    return "\n".join([p for p in parts if p])


def _safe_text(value, default: str = ""):
    if value is None:
        return default
    try:
        return str(value)
    except Exception:
        return default


def _safe_row_text(row, key: str, default: str = ""):
    try:
        if hasattr(row, 'keys') and key in row.keys():
            return _safe_text(row[key], default)
        if hasattr(row, 'get'):
            return _safe_text(row.get(key), default)
    except Exception:
        return default
    return default


def _safe_row_datetime_text(row, key: str) -> str:
    value = _safe_row_text(row, key, "")
    return value.strip()


def health_usefulness_score(text: str):
    raw = (text or "").lower()
    score = 0.0
    for kw in HEALTH_PRACTICAL_KEYWORDS:
        if kw.lower() in raw:
            score += 1.2
    for kw in HEALTH_LOW_SIGNAL_KEYWORDS:
        if kw.lower() in raw:
            score -= 1.5
    if any(x in raw for x in ["每天", "多久", "几岁", "多少", "怎么做", "建议", "安全", "风险", "推荐"]):
        score += 0.8
    if any(x in raw for x in ["点击", "下单", "好物", "巨好吃", "竟然不知道"]):
        score -= 1.8
    return score


def is_low_value_health_cluster(cluster):
    text = cluster_combined_text(cluster)
    score = health_usefulness_score(text)
    raw = (text or "").lower()
    return score < 1.2 and any(kw.lower() in raw for kw in HEALTH_LOW_SIGNAL_KEYWORDS)


def has_health_direction_anchor(text: str):
    raw = (text or "").lower()
    return any(kw.lower() in raw for kw in HEALTH_DIRECTION_ANCHORS)


def has_geo_policy_anchor(text: str):
    raw = (text or "").lower()
    return any(kw.lower() in raw for kw in GEO_POLICY_ANCHORS)


def classify_cluster_direction(cluster):
    title_text = "\n".join([_safe_row_text(r, "title") for r in cluster[:6]]).lower()
    text = cluster_combined_text(cluster).lower()
    sources = [(_safe_row_text(r, "account_name") or "未知公众号") for r in cluster[:6]]
    has_health_anchor = has_health_direction_anchor(title_text) or has_health_direction_anchor(text)
    has_geo_anchor = has_geo_policy_anchor(title_text) or has_geo_policy_anchor(text)

    if any(anchor.lower() in title_text for anchor in AI_HARD_ANCHORS):
        return "AI / 大模型"
    if any(anchor.lower() in title_text for anchor in MILITARY_HARD_ANCHORS):
        return "中国军事 / 外交"
    if any(anchor.lower() in title_text for anchor in GEO_HARD_ANCHORS) or has_geo_anchor:
        return "国际局势 / 地缘政治"

    best_direction = "其他观察"
    best_score = 0.0
    for direction in STABLE_DIRECTIONS:
        if direction == "其他观察":
            continue
        score = 0.0
        for source in sources:
            score += SOURCE_DIRECTION_HINTS.get(source, {}).get(direction, 0.0)
        for kw in DIRECTION_KEYWORDS.get(direction, []):
            low = kw.lower()
            if low in title_text:
                score += 2.2
            elif low in text:
                score += 0.7
        if direction == "AI / 大模型" and any(k in text for k in ["github", "开源", "model", "agent"]):
            score += 0.8
        if direction == "生活健康 / 医疗":
            if not has_health_anchor:
                score -= 5.0
            score += max(-2.0, min(4.0, health_usefulness_score(text)))
            if has_health_anchor:
                score += 1.0
            if has_geo_anchor:
                score -= 3.0
        if direction == "网络安全" and not any(anchor.lower() in title_text for anchor in SECURITY_HARD_ANCHORS):
            score -= 4.0
        if direction == "消费 / 民生 / 社会风险" and not any(k in title_text for k in ["12306", "航班", "机票", "电商", "消费", "民生", "社保", "就业", "旅游", "抢票"]):
            score -= 3.2
        if direction == "科技产业 / 商业" and any(k in title_text for k in ["b站热门精选", "作业", "发呆", "残疾鹦鹉"]):
            score -= 4.0
        if direction == "国际局势 / 地缘政治" and any(src in SOURCE_DIRECTION_HINTS and SOURCE_DIRECTION_HINTS[src].get("AI / 大模型", 0) >= 4 for src in sources):
            score -= 3.5
        if direction == "国际局势 / 地缘政治" and has_geo_anchor:
            score += 1.4
        if direction == "中国军事 / 外交" and any(src in SOURCE_DIRECTION_HINTS and SOURCE_DIRECTION_HINTS[src].get("AI / 大模型", 0) >= 4 for src in sources):
            score -= 2.5
        if score > best_score:
            best_score = score
            best_direction = direction
    if best_direction == "生活健康 / 医疗" and is_low_value_health_cluster(cluster):
        return "其他观察"
    return best_direction


def row_quality_score(row):
    title = _safe_row_text(row, "title")
    content_md = (_safe_row_text(row, "content_md")) if hasattr(row, 'keys') and 'content_md' in row.keys() else ''
    score = 0.0
    score += min(2.0, len(title) / 18.0)
    if content_md:
        score += 1.0
    if extract_github_urls(content_md):
        score += 1.5
    if extract_entity_anchors("\n".join([title, content_md])):
        score += 1.2
    if any(ch.isdigit() for ch in title):
        score += 0.4
    if any(kw.lower() in (title + "\n" + content_md).lower() for kw in HEALTH_PRACTICAL_KEYWORDS):
        score += 0.9
    if any(kw.lower() in (title + "\n" + content_md).lower() for kw in HEALTH_LOW_SIGNAL_KEYWORDS):
        score -= 1.0
    score += article_preference_score(row)
    return score


def choose_cluster_representative(cluster):
    ranked = sorted(
        cluster,
        key=lambda r: (
            row_quality_score(r),
            len(_safe_row_text(r, "title")),
            _safe_row_datetime_text(r, "discovered_at"),
        ),
        reverse=True,
    )
    return ranked[0]


def build_cluster_records(clusters):
    records = []
    for i, cluster in enumerate(clusters, start=1):
        try:
            rep = choose_cluster_representative(cluster)
            direction = classify_cluster_direction(cluster)
            sources = sorted({(_safe_row_text(r, 'account_name') or '未知公众号') for r in cluster})
            source_diversity = len(sources)
            cluster_size = len(cluster)
            recency_bonus = 0.0
            try:
                latest_candidates = [_safe_row_datetime_text(r, 'discovered_at') for r in cluster]
                latest_candidates = [x for x in latest_candidates if x]
                latest = max(latest_candidates) if latest_candidates else ""
                if latest:
                    dt = datetime.strptime(latest, "%Y-%m-%d %H:%M:%S")
                    hours_ago = max(0.0, (datetime.now() - dt).total_seconds() / 3600.0)
                    recency_bonus = max(0.0, 2.5 - min(2.5, hours_ago / 4.0))
            except Exception:
                recency_bonus = 0.0
            score = (
                DIRECTION_PRIORITY.get(direction, 3.0)
                + math.log1p(cluster_size) * 2.1
                + math.log1p(source_diversity) * 1.6
                + row_quality_score(rep)
                + recency_bonus
            )
            pref_score = record_preference_score({"rows": cluster, "representative": rep})
            score += max(-10.0, min(12.0, pref_score))
            if direction == "生活健康 / 医疗":
                score += max(-2.5, min(5.0, health_usefulness_score(cluster_combined_text(cluster))))
            records.append({
                "cluster_id": f"cluster_{i}",
                "direction": direction,
                "score": round(score, 2),
                "preference_score": round(pref_score, 2),
                "cluster_size": cluster_size,
                "source_diversity": source_diversity,
                "sources": sources,
                "representative": rep,
                "rows": cluster,
            })
        except Exception as e:
            print(f"跳过异常 cluster_{i}: {e}", file=sys.stderr)
    return records


def select_balanced_cluster_records(cluster_records, max_clusters: int | None = None, longtail_min: int | None = None):
    max_clusters = max_clusters if max_clusters is not None else report_setting_int('max_clusters', 60)
    longtail_min = longtail_min if longtail_min is not None else report_setting_int('longtail_min', 3)
    core_records = [r for r in cluster_records if r["direction"] in CORE_DIRECTIONS]
    longtail_records = [r for r in cluster_records if r["direction"] in LONGTAIL_DIRECTIONS]

    core_records.sort(key=lambda r: (-r["score"], -r["cluster_size"], -r["source_diversity"]))
    longtail_records.sort(key=lambda r: (-r["score"], -r["cluster_size"], -r["source_diversity"]))

    selected = []
    selected_ids = set()
    direction_counts = defaultdict(int)

    def can_take(record):
        return direction_counts[record["direction"]] < report_direction_caps().get(record["direction"], 2)

    def take(record):
        selected.append(record)
        selected_ids.add(record["cluster_id"])
        direction_counts[record["direction"]] += 1

    for direction in STABLE_DIRECTIONS:
        if direction not in CORE_DIRECTIONS:
            continue
        bucket = [r for r in core_records if r["direction"] == direction]
        if bucket and can_take(bucket[0]):
            take(bucket[0])

    reserve = min(longtail_min, len(longtail_records))
    main_slots = max(0, max_clusters - reserve)

    for record in core_records:
        if len(selected) >= main_slots:
            break
        if record["cluster_id"] in selected_ids:
            continue
        if not can_take(record):
            continue
        take(record)

    longtail_selected = []
    used_longtail_directions = set()
    for record in longtail_records:
        if len(longtail_selected) >= reserve:
            break
        if record["cluster_id"] in selected_ids:
            continue
        if not can_take(record):
            continue
        if record["direction"] in used_longtail_directions and len(longtail_records) > reserve:
            continue
        longtail_selected.append(record)
        selected_ids.add(record["cluster_id"])
        used_longtail_directions.add(record["direction"])
        direction_counts[record["direction"]] += 1

    selected.extend(longtail_selected)

    remaining = sorted(cluster_records, key=lambda r: (-r["score"], -r["cluster_size"], -r["source_diversity"]))
    for record in remaining:
        if len(selected) >= max_clusters:
            break
        if record["cluster_id"] in selected_ids:
            continue
        if not can_take(record):
            continue
        take(record)

    return selected


def dedupe_rows(rows):
    raw_count = len(rows)

    # 1) URL exact dedupe
    by_url = {}
    for row in rows:
        by_url[row["article_url"]] = row
    rows1 = list(by_url.values())

    # 2) normalized title dedupe
    by_title = {}
    for row in rows1:
        key = normalize_title(row["title"])
        if not key:
            key = row["article_url"]
        if key not in by_title:
            by_title[key] = row
    rows2 = list(by_title.values())

    # 3) topic clustering dedupe (similar-title clustering)
    clusters = []
    for row in rows2:
        placed = False
        for cluster in clusters:
            if row_similarity(row, cluster[0]) >= 0.38:
                cluster.append(row)
                placed = True
                break
        if not placed:
            clusters.append([row])

    cluster_records = build_cluster_records(clusters)
    selected_cluster_records = select_balanced_cluster_records(cluster_records)
    rows3 = [record["representative"] for record in selected_cluster_records]
    cluster_map = {record["cluster_id"]: record["rows"] for record in cluster_records}

    return {
        "raw_count": raw_count,
        "after_url": len(rows1),
        "after_title": len(rows2),
        "after_topic": len(rows3),
        "rows": rows3,
        "clusters": cluster_map,
        "cluster_records": cluster_records,
        "selected_cluster_records": selected_cluster_records,
    }


def clean_inline_text(text: str, limit: int = 220, ellipsis: bool = True):
    raw = text or ''
    raw = raw.replace('\\n', ' ')
    raw = re.sub(r'!\[[^\]]*\]\([^)]*\)', ' ', raw)
    raw = re.sub(r'\[[^\]]*\]\([^)]*\)', ' ', raw)
    raw = re.sub(r'[`*_#>-]+', ' ', raw)
    raw = re.sub(r'[=]{3,}', ' ', raw)
    raw = re.sub(r'https?://\S+', ' ', raw)
    raw = re.sub(r'\s+', ' ', raw).strip()
    raw = re.sub(r'https?://\S+', '', raw).strip()
    raw = raw.replace('|', '｜')
    raw = raw.strip('：:|-—· ') 
    if len(raw) > limit:
        clipped = raw[:limit].rstrip()
        punct_positions = [clipped.rfind(ch) for ch in ['。', '！', '？', ';', '；']]
        punct = max(punct_positions) if punct_positions else -1
        if punct >= int(limit * 0.6):
            return clipped[:punct + 1].rstrip()
        if not ellipsis:
            return clipped
        return raw[: limit - 1].rstrip() + '…'
    return raw


def ensure_sentence_finished(text: str):
    raw = (text or '').strip()
    if not raw:
        return ''
    if raw.endswith(('。', '！', '？', '.', '!', '?', '”', '』', '】', '）', ')')):
        return raw
    return raw + '。'


def clean_signal_title(text: str, limit: int = 90):
    raw = text or ''
    raw = raw.replace('\\n', '\n')
    lines = [x.strip() for x in raw.splitlines() if x.strip()]
    if lines:
        raw = lines[0]
        if len(raw) < 12 and len(lines) >= 2:
            raw = f"{raw} {lines[1]}"
    raw = clean_inline_text(raw, limit=limit)
    for sep in [' ｜ ', '丨', '｜', ';', '；']:
        if sep in raw and len(raw.split(sep)[0].strip()) >= 10:
            raw = raw.split(sep)[0].strip()
            break
    return clean_inline_text(raw, limit=limit)


def representative_snippet(record):
    rep = record["representative"]
    content_md = rep["content_md"] if 'content_md' in rep.keys() else ''
    snippet = extract_source_snippet(content_md, limit=180)
    if snippet:
        cleaned = clean_inline_text(snippet, limit=180)
        title_clean = clean_signal_title(rep["title"] or '', limit=90)
        bad_prefixes = ["新智元报道", "编辑：", "如果把今天", "工具介绍", "外交部：", "将环球科学", "AI寒武纪", "机器之心"]
        if cleaned and '![图片]' not in cleaned and cleaned.count('###') == 0 and 18 <= len(cleaned) <= 100 and cleaned != title_clean and not cleaned.startswith(title_clean) and not any(cleaned.startswith(x) for x in bad_prefixes) and '公众号' not in cleaned and '导读' not in cleaned:
            return cleaned
    return ""


def split_sentences(text: str):
    raw = clean_inline_text(text or '', limit=1200, ellipsis=False)
    if not raw:
        return []
    parts = re.split(r'[。！？!?.]\s*', raw)
    out = []
    for part in parts:
        part = clean_inline_text(part, limit=220, ellipsis=False)
        if len(part) >= 14:
            out.append(part)
    return out


def good_summary_sentence(sentence: str, title: str = ''):
    s = clean_inline_text(sentence or '', limit=100)
    t = clean_inline_text(title or '', limit=100)
    if not s:
        return False
    if '====' in sentence or s.count('=') >= 2:
        return False
    bad_prefixes = ["编辑", "导读", "原标题", "作者", "来源", "工具介绍", "如果把今天", "公众号", "点击", "阅读原文"]
    if any(s.startswith(x) for x in bad_prefixes):
        return False
    if t and (s == t or s.startswith(t)):
        return False
    if t:
        title_terms = title_tokens(t)
        sent_terms = title_tokens(s)
        if title_terms and sent_terms and len(title_terms & sent_terms) == 0:
            return False
    if s[:1].isdigit() or s.startswith(('↑', '→', '\\')):
        return False
    if '\\' in s:
        return False
    if len(s) < 16:
        return False
    return True


def best_summary_for_row(row):
    title = clean_signal_title(row["title"] if "title" in row.keys() else '', limit=90)
    content_md = row["content_md"] if 'content_md' in row.keys() else ''
    for sent in split_sentences(content_md):
        cleaned = clean_inline_text(sent, limit=180, ellipsis=False)
        if title and cleaned.startswith(title):
            cleaned = cleaned[len(title):].lstrip('：:，,。.-— ')
        bad_tokens = ["编辑", "导读", "公众号", "发自", "阅读之前", "星标", "关注+", "关注 ", "原标题", "本文", "工具介绍"]
        if any(tok in cleaned for tok in bad_tokens):
            continue
        if good_summary_sentence(cleaned, title):
            return cleaned
    return ""


def fallback_summary_from_title(title: str, direction: str):
    t = clean_signal_title(title or '', limit=90)
    if not t:
        return ""
    chunks = [clean_inline_text(x, limit=40) for x in re.split(r'[；;｜|/]', t) if clean_inline_text(x, limit=40)]
    if len(chunks) >= 2:
        if direction == "教育 / 科学":
            return f"这条内容汇总了 {chunks[0]} 与 {chunks[1]} 两个科学观察，适合快速把握当天值得一看的新发现。"
        if direction == "具身智能 / 机器人":
            return f"这是一条快讯式汇总，串起了 {chunks[0]}、{chunks[1]} 等几条动态，建议按需点回原文看细节。"
        return f"这条内容把 {chunks[0]}、{chunks[1]} 等变化合并在一起，适合做快速浏览。"
    if direction == "生活健康 / 医疗":
        if "寿命" in t or "衰老" in t:
            return "文章聚焦延缓衰老与寿命延长方向的新进展，重点在于这项研究可能意味着什么。"
        if "维生素" in t or "VD" in t or "维生素 D" in t:
            return "围绕补充频率、适用年龄和重点人群给出实用建议。"
        if "刷牙" in t or "口腔" in t:
            return "围绕儿童独立清洁能力和家长辅助时长给出建议。"
        if any(x in t for x in ["结节", "桥本", "高尿酸", "睡眠", "饮食"]):
            return "围绕常见健康问题给出风险判断和生活建议。"
    if direction == "国际局势 / 地缘政治":
        return "围绕事件最新进展与外部影响给出关键信息。"
    if direction == "中国军事 / 外交":
        return "围绕军事动向或外交表态给出核心信息。"
    if direction == "科技产业 / 商业":
        return "围绕公司动态、融资或产业变化给出关键信息。"
    if direction == "网络安全":
        return "围绕漏洞、攻击或安全风险给出关键信息。"
    if direction == "AI / 大模型":
        if "白皮书" in t or "指南" in t or "报告" in t:
            return "文章更偏方法论或框架梳理，适合拿来快速理解这个方向的关键概念、落地路径和风险边界。"
        if "Kimi" in t or "模型" in t:
            return "围绕模型能力、定位或性能变化给出关键信息。"
        if "Agent" in t or "AiScientist" in t:
            return "围绕 Agent 或长程记忆能力的进展给出关键信息。"
        if "AniGen" in t or "3D资产" in t:
            return "围绕 3D 生成与可动画资产能力的进展给出关键信息。"
        return "围绕模型、研究或开源项目进展给出关键信息。"
    if direction == "具身智能 / 机器人":
        if "JiuwenClaw" in t or "Coordination Engineering" in t:
            return "文章重点在多 Agent 协同与 Team Skills 新范式，核心看点是把协作工程化能力进一步产品化。"
        return "围绕机器人或具身系统的最新进展给出关键信息。"
    if direction == "教育 / 科学":
        return "围绕最新科学发现或研究进展给出高信号信息。"
    return "围绕该事件的核心变化给出简要信息。"


def record_is_bad_fit(record):
    direction = record.get("direction") or ""
    rep = record["representative"]
    title = clean_inline_text(rep["title"] or '', limit=160)
    source = rep["account_name"] or "未知公众号"
    if direction == "生活健康 / 医疗" and source == "环球科学" and ("要闻" in title or "；" in title or "｜" in title or "丨" in title):
        return True
    if direction == "科技产业 / 商业" and any(k in title for k in ["B站热门精选", "为什么玩6小时游戏不累"]):
        return True
    if direction == "具身智能 / 机器人" and title.count('/') >= 2 and not any(k in title.lower() for k in ["机器人", "具身", "claw", "agent", "jiuwen"]):
        return True
    if direction == "其他观察" and any(k in title for k in ["鼠标手势", "版本发布"]) and len(title) < 20:
        return True
    return False


def signal_kind_for_record(record):
    return "多源共识" if record.get("source_diversity", 1) >= 2 and record.get("cluster_size", 1) >= 2 else "单点信号"


def signal_sources_for_record(record):
    if signal_kind_for_record(record) == "多源共识":
        return "/".join(record.get("sources", [])[:2]) or (record["representative"]["account_name"] or "未知公众号")
    return (record["representative"]["account_name"] or "未知公众号")


def unique_direction_records(selected_cluster_records):
    buckets = defaultdict(list)
    for record in selected_cluster_records or []:
        buckets[record["direction"]].append(record)

    picked = []
    for direction in STABLE_DIRECTIONS:
        records = sorted(buckets.get(direction, []), key=lambda r: (-r["score"], -r["cluster_size"], -r["source_diversity"]))
        if not records:
            continue
        source_counts = defaultdict(int)
        limit = direction_signal_limit(direction)
        for record in records:
            if record_is_bad_fit(record):
                continue
            source = record["representative"]["account_name"] or "未知公众号"
            same_source_cap = report_same_source_cap(direction, source)
            if source_counts[source] >= same_source_cap:
                continue
            picked.append(record)
            source_counts[source] += 1
            if sum(1 for x in picked if x["direction"] == direction) >= limit:
                break
        # backfill if direction got starved
        if sum(1 for x in picked if x["direction"] == direction) < min(limit, len(records)):
            for record in records:
                if record_is_bad_fit(record):
                    continue
                if record in picked:
                    continue
                picked.append(record)
                if sum(1 for x in picked if x["direction"] == direction) >= min(limit, len(records)):
                    break
    return picked


def records_semantically_same(a, b):
    a_rep = a['representative']
    b_rep = b['representative']
    a_focus = {focus_term_key(x) for x in extract_focus_terms('\n'.join(filter(None, [a_rep['title'] or '', best_summary_for_row(a_rep)])))}
    b_focus = {focus_term_key(x) for x in extract_focus_terms('\n'.join(filter(None, [b_rep['title'] or '', best_summary_for_row(b_rep)])))}
    shared_focus = set()
    for af in a_focus:
        if not af or af in TOPIC_FOCUS_GENERIC_STOPWORDS:
            continue
        for bf in b_focus:
            if not bf or bf in TOPIC_FOCUS_GENERIC_STOPWORDS:
                continue
            if af == bf or af in bf or bf in af:
                key = af if len(af) >= len(bf) else bf
                if len(key) >= 8 or any(ord(ch) > 127 for ch in key):
                    shared_focus.add(key)
    if shared_focus:
        return True

    ta = title_tokens(a["representative"]["title"] or '')
    tb = title_tokens(b["representative"]["title"] or '')
    overlap = len(ta & tb)
    union = len(ta | tb) or 1
    sim = overlap / union
    a_title = (a["representative"]["title"] or '').lower()
    b_title = (b["representative"]["title"] or '').lower()
    a_anchors = set(extract_entity_anchors(a_title))
    b_anchors = set(extract_entity_anchors(b_title))
    if sim >= 0.32:
        return True
    if a_anchors and b_anchors and a_anchors & b_anchors and sim >= 0.16:
        return True
    return False


def group_signal_records(records):
    groups = []
    for record in records:
        placed = False
        for group in groups:
            if records_semantically_same(record, group[0]):
                group.append(record)
                placed = True
                break
        if not placed:
            groups.append([record])
    return groups


def lead_record_for_group(group):
    return sorted(group, key=lambda r: (-r["score"], -r["cluster_size"], -r["source_diversity"]))[0]


def grouped_direction_signal_candidates(selected_cluster_records, source_quality_stats=None):
    source_quality_stats = source_quality_stats or {}
    buckets = defaultdict(list)
    for record in selected_cluster_records or []:
        if record_is_bad_fit(record):
            continue
        buckets[record["direction"]].append(record)

    picked_groups_by_direction = {}
    for direction in STABLE_DIRECTIONS:
        records = sorted(buckets.get(direction, []), key=lambda r: (-r["score"], -r["cluster_size"], -r["source_diversity"]))
        if not records:
            picked_groups_by_direction[direction] = []
            continue

        groups = group_signal_records(records)
        groups = sorted(groups, key=lambda g: -signal_group_priority_score(g, direction, source_quality_stats))

        source_counts = defaultdict(int)
        limit = direction_signal_limit(direction)
        picked_groups = []
        picked_group_keys = set()
        for group in groups:
            group = sort_group_records_by_source_quality(group, source_quality_stats)
            group_key = signal_group_key(group)
            if group_key in picked_group_keys:
                continue
            lead = lead_record_for_group(group)
            source = lead["representative"]["account_name"] or "未知公众号"
            same_source_cap = report_same_source_cap(direction, source)
            if source_counts[source] >= same_source_cap:
                continue
            picked_groups.append(group)
            picked_group_keys.add(group_key)
            source_counts[source] += 1
            if len(picked_groups) >= limit:
                break

        if len(picked_groups) < min(limit, len(groups)):
            for group in groups:
                group = sort_group_records_by_source_quality(group, source_quality_stats)
                group_key = signal_group_key(group)
                if group_key in picked_group_keys:
                    continue
                picked_groups.append(group)
                picked_group_keys.add(group_key)
                if len(picked_groups) >= min(limit, len(groups)):
                    break

        picked_groups_by_direction[direction] = picked_groups
    return picked_groups_by_direction


def group_focus_label(group):
    counter = Counter()
    display = {}
    for item in group:
        rep = item["representative"]
        raw = "\n".join(filter(None, [
            rep["title"] or '',
            best_summary_for_row(rep),
            extract_source_snippet(rep["content_md"] if 'content_md' in rep.keys() else '', limit=120),
        ]))
        for term in extract_focus_terms(raw):
            key = focus_term_key(term)
            if not key:
                continue
            counter[key] += 1
            display.setdefault(key, term)
    if not counter:
        return ''
    best_key, _ = sorted(counter.items(), key=lambda kv: (-kv[1], -len(kv[0]), kv[0]))[0]
    return display.get(best_key, '')


def synthesized_title_suffix(group, direction: str, focus: str):
    joined_titles = ' '.join(clean_signal_title(item['representative']['title'] or '', limit=100) for item in group)
    low = joined_titles.lower()
    focus_low = (focus or '').lower()
    if 'deepseek' in focus_low:
        if any(k in joined_titles for k in ['发布', '预览版']) and any(k in joined_titles for k in ['开源', '芯片', '华为', '寒武纪', '适配', 'API']):
            return '发布与生态进展'
        if any(k in joined_titles for k in ['发布', '预览版']):
            return '发布进展'
        return '相关进展'
    if direction == 'AI / 大模型':
        if any(k in joined_titles for k in ['白皮书', '报告', '技术报告', '指南']):
            return '研究与方法进展'
        if any(k in joined_titles for k in ['开源', 'GitHub', '社区', '框架', '工具', '适配']):
            return '开源与生态进展'
        return '相关进展'
    if direction == '国际局势 / 地缘政治':
        if any(k in joined_titles for k in ['制裁', '关税', '出口管制', '名单']):
            return '政策动向'
        return '相关动向'
    if direction == '中国军事 / 外交':
        return '最新动向'
    if direction == '科技产业 / 商业':
        return '产业进展'
    if direction == '具身智能 / 机器人':
        if any(k in low for k in ['agent', 'team skills', 'coordination']):
            return '协同进展'
        return '进展观察'
    if direction == '生活健康 / 医疗':
        return '观察'
    if direction == '教育 / 科学':
        return '研究观察'
    return '相关进展'


def signal_display_title(group, lead, direction: str, kind: str):
    title = clean_signal_title(lead["representative"]["title"] or '', limit=90)
    if kind != "多源共识":
        return title
    focus = group_focus_label(group)
    if not focus:
        return title
    suffix = synthesized_title_suffix(group, direction, focus)
    return clean_signal_title(f"{focus}{suffix}", limit=90)


def source_summary_for_record(record, direction: str):
    rep = record["representative"]
    summary = best_summary_for_row(rep)
    if not summary:
        summary = fallback_summary_from_title(rep["title"] or '', direction)
    return ensure_sentence_finished(clean_inline_text(summary, limit=220, ellipsis=False))


def summary_is_generic(text: str):
    raw = clean_inline_text(text or '', limit=160)
    if not raw:
        return True
    generic_markers = [
        "围绕模型能力、定位或性能变化给出关键信息",
        "围绕模型、研究或开源项目进展给出关键信息",
        "围绕该事件的核心变化给出简要信息",
        "围绕公司动态、融资或产业变化给出关键信息",
        "围绕事件最新进展与外部影响给出关键信息",
        "围绕军事动向或外交表态给出核心信息",
        "围绕漏洞、攻击或安全风险给出关键信息",
    ]
    return any(marker in raw for marker in generic_markers)


def group_common_summary(group, direction: str):
    ranked = sorted(group, key=lambda r: (-r["score"], -r["cluster_size"], -r["source_diversity"]))
    best = ""
    for item in ranked:
        sent = source_summary_for_record(item, direction)
        if not sent:
            continue
        if not summary_is_generic(sent):
            return sent
        if not best:
            best = sent
    return best


def focus_common_summary(focus: str, direction: str, source_count: int):
    f = clean_inline_text(focus or '', limit=60)
    low = (f or '').lower()
    if not f:
        return ''
    if 'deepseek' in low:
        return f"多个来源共同指向 {f} 已进入新一轮发布与适配阶段，讨论重点集中在模型能力升级、开源/API 供给、国产算力适配和落地节奏。"
    if direction == 'AI / 大模型':
        return f"多个来源共同围绕 {f} 展开，核心都在讨论模型能力、产品节奏和生态落地。"
    if direction == '国际局势 / 地缘政治':
        return f"多个来源都在围绕 {f} 跟进，重点落在最新进展、外部影响和后续政策动向。"
    if direction == '中国军事 / 外交':
        return f"多个来源共同提到 {f}，核心信息集中在军事动作、官方表态和地区影响。"
    if direction == '科技产业 / 商业':
        return f"多个来源围绕 {f} 给出了相近判断，关注点主要在产业影响、公司动作和后续商业化空间。"
    if direction == '生活健康 / 医疗':
        return f"多篇内容都在围绕 {f} 提供可执行的信息，重点是风险判断、适用人群和具体建议。"
    if source_count >= 3:
        return f"多个来源共同聚焦 {f}，整体判断大体一致。"
    return ''


def build_group_summaries(group, lead, direction: str, kind: str):
    if kind != "多源共识":
        return {"summary": source_summary_for_record(lead, direction), "common": "", "diff": ""}

    focus = group_focus_label(group)
    source_count = len({(item["representative"]["account_name"] or "未知公众号") for item in group})
    common = focus_common_summary(focus, direction, source_count) or group_common_summary(group, direction) or "多篇文章都在描述同一件值得关注的变化。"
    if focus and focus_term_key(focus) not in focus_term_key(common):
        common = f"这组内容共同聚焦 {focus}，{common}"

    diff_bits = []
    seen = set()
    for item in sorted(group, key=lambda r: (-r["score"], -r["cluster_size"], -r["source_diversity"])):
        src = item["representative"]["account_name"] or "未知公众号"
        sent = source_summary_for_record(item, direction).rstrip('。！？；;')
        sent_key = focus_term_key(sent)
        if not sent or sent_key in seen:
            continue
        seen.add(sent_key)
        if summary_is_generic(sent) and diff_bits:
            continue
        diff_bits.append(f"{src}更强调{sent}")
        if len(diff_bits) >= 4:
            break

    diff = ''
    if len(diff_bits) >= 2:
        diff = '；'.join(diff_bits[1:])

    full = common
    if diff:
        full += f" 不同来源的侧重点略有差异，{diff}"
    return {
        "summary": ensure_sentence_finished(clean_inline_text(full, limit=520, ellipsis=False)),
        "common": ensure_sentence_finished(clean_inline_text(common, limit=220, ellipsis=False)),
        "diff": clean_inline_text(diff, limit=260, ellipsis=False),
    }


def build_signal_appendix_items(group, direction: str, kind: str, source_quality_stats=None):
    items = []
    seen_urls = set()
    ranked = sort_group_records_by_source_quality(group, source_quality_stats)
    if kind == "多源共识":
        if direction in preferred_directions():
            max_items = max(1, report_setting_int('multi_source_appendix_items_preferred', 5))
        else:
            max_items = max(1, report_setting_int('multi_source_appendix_items_default', 3))
    else:
        max_items = max(1, report_setting_int('single_source_appendix_items', 1))
    for idx, item in enumerate(ranked, start=1):
        rep = item["representative"]
        url = rep["article_url"]
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        reason = appendix_reason({"kind": kind, "direction": direction}) if idx == 1 else ("差异补充" if kind == "多源共识" else "补充入口")
        items.append({
            "title": clean_signal_title(rep["title"] or '', limit=90),
            "source": rep["account_name"] or "未知公众号",
            "url": url,
            "reason": reason,
        })
        if len(items) >= max_items:
            break
    return items


def signal_weight_for_group(group, direction: str):
    source_count = len({(item["representative"]["account_name"] or "未知公众号") for item in group})
    article_count = sum(max(1, int(item.get("cluster_size") or 1)) for item in group)
    preferred_bonus = 0.05 if direction in preferred_directions() else 0.0
    weight = 0.52
    weight += min(0.30, max(0, source_count - 1) * 0.10)
    weight += min(0.11, max(0, article_count - 1) * 0.025)
    weight += preferred_bonus
    return round(min(0.98, max(0.35, weight)), 2)


def build_signal_records(data):
    signal_records = []
    direction_counters = defaultdict(int)
    source_quality_stats = compute_source_quality_stats(data)
    grouped_candidates = grouped_direction_signal_candidates(data.get("selected_cluster_records") or [], source_quality_stats=source_quality_stats)

    for direction in STABLE_DIRECTIONS:
        groups = grouped_candidates.get(direction) or []
        for group in groups:
            direction_counters[direction] += 1
            lead = lead_record_for_group(group)
            rep = lead["representative"]
            sources = []
            seen_sources = set()
            for item in sort_group_records_by_source_quality(group, source_quality_stats):
                src = item["representative"]["account_name"] or "未知公众号"
                if src not in seen_sources:
                    seen_sources.add(src)
                    sources.append(src)
            summaries = []
            for item in group:
                sent = best_summary_for_row(item["representative"])
                if sent and sent not in summaries:
                    summaries.append(sent)
            kind = "多源共识" if len(sources) >= 2 or len(group) >= 2 else "单点信号"
            group_summary = build_group_summaries(group, lead, direction, kind)
            summary = group_summary.get("summary") or ''
            if not summary:
                summary = fallback_summary_from_title(rep["title"] or '', direction)
            source_count = len(sources)
            weight = signal_weight_for_group(group, direction)
            signal_records.append({
                "signal_id": f"{direction_counters[direction]:02d}@{direction}",
                "direction": direction,
                "title": signal_display_title(group, lead, direction, kind),
                "kind": kind,
                "weight": weight,
                "weight_label": f"权重{weight:.2f}·{source_count}源",
                "weight_badge_raw": f"权重{weight:.2f}·{source_count}源" + (f"·{'/'.join(sources)}" if sources else ""),
                "source_count": source_count,
                "source_names": sources,
                "sources": '/'.join(sources[:2]) if kind == '多源共识' else (sources[0] if sources else (rep["account_name"] or "未知公众号")),
                "source_primary": rep["account_name"] or "未知公众号",
                "url": rep["article_url"],
                "summary": ensure_sentence_finished(clean_inline_text(summary, limit=480, ellipsis=False)) if summary else '',
                "common_summary": group_summary.get("common") or '',
                "difference_summary": group_summary.get("diff") or '',
                "appendix_items": build_signal_appendix_items(group, direction, kind, source_quality_stats=source_quality_stats),
                "record": lead,
                "records": group,
            })
    return signal_records


def direction_overview(direction: str, items):
    titles = [x["title"] for x in items[:2]]
    if direction == "AI / 大模型":
        return f"{clean_inline_text('；'.join(titles), 60)}，反映 AI 模型、研究与开源工具持续演进。"
    if direction == "国际局势 / 地缘政治":
        return f"{clean_inline_text('；'.join(titles), 60)}，国际局势和地缘摩擦仍在升温。"
    if direction == "网络安全":
        return f"{clean_inline_text('；'.join(titles), 60)}，安全攻防继续向自动化与实战化演进。"
    if direction == "中国军事 / 外交":
        return f"{clean_inline_text('；'.join(titles), 60)}，体现地区军事动向与外交表态的直接变化。"
    if direction == "科技产业 / 商业":
        return f"{clean_inline_text('；'.join(titles), 60)}，产业竞争、融资与公司战略继续分化。"
    if direction == "具身智能 / 机器人":
        return f"{clean_inline_text('；'.join(titles), 60)}，具身方向继续朝工程化落地推进。"
    if direction == "生活健康 / 医疗":
        return f"{clean_inline_text('；'.join(titles), 60)}，以可直接用于生活决策的健康知识为主。"
    if direction == "消费 / 民生 / 社会风险":
        return f"{clean_inline_text('；'.join(titles), 60)}，聚焦真实影响日常生活与消费秩序的变化。"
    return clean_inline_text('；'.join(titles), 70)


def appendix_reason(signal):
    if float(signal.get("weight") or 0.0) >= 0.85:
        return "高权重信号"
    if signal["direction"] == "生活健康 / 医疗":
        return "实用指南"
    if signal["direction"] == "中国军事 / 外交":
        return "核心信号"
    if signal["direction"] == "国际局势 / 地缘政治":
        return "局势影响"
    if signal["direction"] == "科技产业 / 商业":
        return "代表性报道"
    return "主信源"


def render_empty_window_report(report_type, window_start, window_end, data):
    label = "早报" if report_type == "morning" else "晚报"
    raw_count = int((data or {}).get("raw_count") or 0)
    after_semantic_week = int((data or {}).get("after_semantic_week") or 0)
    return (
        "## 📌 方向归类\n\n"
        f"- **本时间窗暂无新增内容**：本期{label}统计窗口为 {window_start} ~ {window_end}，"
        f"原始入库 {raw_count} 篇，去重后 {after_semantic_week} 篇，因此本期不生成方向摘要。\n\n"
        "---\n\n"
        "## 📝 内容详览\n\n"
        f"- 本期{label}对应时间窗内没有可写入正文的新文章，当前页面保留为一次空窗记录。\n"
        "- 这不是渲染故障，而是数据窗口为空。若这种情况频繁出现，应调整晚报触发时间，或在无新增时只发送空窗提醒。\n\n"
        "---\n\n"
        "## 🔗 链接附录\n\n"
        "- 本时间窗暂无可附录链接。\n"
    )


def render_grounded_report(report_type, window_start, window_end, data):
    signals = build_signal_records(data)
    if not signals:
        return render_empty_window_report(report_type, window_start, window_end, data)
    grouped = defaultdict(list)
    for signal in signals:
        grouped[signal["direction"]].append(signal)

    lines = ["## 📌 方向归类", ""]
    for direction in STABLE_DIRECTIONS:
        items = grouped.get(direction) or []
        if not items:
            continue
        lines.append(f"- **{direction}**：{direction_overview(direction, items)}")

    lines.extend(["", "---", "", "## 📝 内容详览", ""])
    for direction in STABLE_DIRECTIONS:
        items = grouped.get(direction) or []
        if not items:
            continue
        lines.append(f"### {direction}")
        for signal in items:
            summary = signal.get('summary') or ''
            suffix = f" {summary}" if summary else ""
            lines.append(f"- **{signal['title']}**【{signal['weight_badge_raw']}】{suffix}")
        if direction == "AI / 大模型" and data.get("github_items"):
            lines.append("")
            lines.append("**开源项目 / GitHub**")
            seen_urls = set()
            for item in data["github_items"]:
                if item["url"] in seen_urls:
                    continue
                seen_urls.add(item["url"])
                source_article = clean_inline_text(item['title'], 80)
                source_label = f"来源文章：{source_article}（{item['account_name']}）"
                project_label = item.get('project_label') or clean_inline_text(item['title'], 80)
                intro = clean_inline_text(item.get('intro') or '相关开源项目。', 100)
                lines.append(f"- **{project_label}**：{intro} 项目地址：{item['url']} {source_label}")
        lines.append("")

    lines.extend(["---", "", "## 🔗 链接附录", ""])
    for direction in STABLE_DIRECTIONS:
        items = grouped.get(direction) or []
        if not items:
            continue
        lines.append(f"### {direction}")
        appendix_items = []
        seen_urls = set()
        for signal in items:
            for item in signal.get('appendix_items') or []:
                url = item.get('url')
                if not url or url in seen_urls:
                    continue
                seen_urls.add(url)
                appendix_items.append(item)
                if len(appendix_items) >= direction_appendix_limit(direction):
                    break
            if len(appendix_items) >= direction_appendix_limit(direction):
                break
        for idx, item in enumerate(appendix_items, start=1):
            lines.append(f"{idx}. {item['title']} | {item['source']} | {item['url']} | {item['reason']}")
        lines.append("")
    return "\n".join(lines).strip() + "\n"


# ── Build user message for MiniMax ─────────────────────────────────────────

def build_user_message(report_type, window_start, window_end, data):
    label = "早报" if report_type == "morning" else "晚报"
    lines = []
    lines.append(f"请基于以下数据生成今日{label}。")
    lines.append(f"时间范围：{window_start} ~ {window_end}")
    lines.append(f"原始 {data['raw_count']} 篇，URL去重后 {data['after_url']} 篇，标题去重后 {data['after_title']} 篇，主题去重后 {data['after_topic']} 篇。")
    if data.get('history_count') is not None:
        lines.append(f"已对过去一周 {data['history_count']} 篇历史标题做语义重复过滤，本轮额外过滤 {data.get('after_semantic_week_removed', 0)} 篇重复内容。")
    lines.append("")
    lines.append("稳定方向优先级提示：AI/大模型、网络安全、国际局势/地缘政治、中国军事/外交、科技产业/商业、具身智能/机器人、生活健康/医疗、消费/民生/社会风险、教育/科学。")
    lines.append("方向排序优先级：1.AI/大模型 2.国际局势/地缘政治 3.网络安全 4.中国军事/外交 5.科技产业/商业 6.具身智能/机器人 7.生活健康/医疗 8.消费/民生/社会风险 9.教育/科学。高优先级方向若内容充足，应排在前面。")
    lines.append("如果某方向内容很弱，可以不写；但不要为了凑数发明太多临时方向。")
    lines.append("写作密度要求：AI/大模型、国际局势/地缘政治、中国军事/外交可写得更充分；科技产业/商业、具身智能/机器人居中；生活健康/医疗、消费/民生/社会风险、教育/科学可以更短，但如果有高质量文章不要整类清空。")
    lines.append("选稿结构建议：优先输出 5 个主要聚类，再保留 1-3 个长尾观察，让最重要的事件在前面，同时保住高质量长尾内容。")
    lines.append("显式信号标记要求：多家来源共同指向同一事件/趋势时标记【多源共识·来源A/来源B】；只有单一来源时标记【单点信号·来源公众号】。绝对不要只写裸的【单点信号】或【多源共识】。")
    lines.append("粒度硬约束：每一条信号只写一个主事件 / 主判断；不要在同一条里混入两个以上不相干文章。若有第二个事件，必须另起一条。")
    lines.append("去重硬约束：同一方向下，不要连续产出多个内容高度相似的单点信号；也不要让同一来源在同一方向里反复刷屏，除非该方向确实没有别的有效来源。")
    lines.append("链接附录每个方向只保留 Top 3-6 条最值得看的链接。")
    lines.append("同一方向下如果多个号在讲同一件事，要做来源去重，默认只保留最强 1-2 条代表链接。")
    lines.append("每条附录链接后加一个很短的推荐理由标签，例如：主信源 / 代表性报道 / 争议点来源 / 背景补充 / 深读入口。")
    lines.append("如果 AI / 大模型方向中出现 GitHub、开源项目、开源框架、开源工具、代码仓库、ModelScope / GitHub / 开源社区相关内容，必须单独增加一个小节：开源项目 / GitHub，列出项目名称、项目简介、为什么值得看、项目地址。")
    lines.append("如果给出了候选 GitHub 地址，必须优先使用候选中的完整 URL；禁止输出 github.com/... 这类省略链接。")
    selected_cluster_records = data.get("selected_cluster_records") or []
    required_longtail = []
    seen_longtail_dirs = set()
    for record in selected_cluster_records:
        direction = record.get("direction") or "其他观察"
        if direction not in LONGTAIL_DIRECTIONS:
            continue
        if direction in seen_longtail_dirs:
            continue
        required_longtail.append(record)
        seen_longtail_dirs.add(direction)
    if required_longtail:
        lines.append("")
        lines.append("硬性要求：以下长尾方向已经由数据层明确选中，最终成文必须逐一覆盖，不能省略，也不能只保留其中 1 个。")
        lines.append(f"最终至少输出 {len(required_longtail)} 个长尾观察 / 补充方向。")
        for idx, record in enumerate(required_longtail, start=1):
            rep = record["representative"]
            lines.append(
                f"- 必须覆盖 {idx}：方向={record['direction']} | 代表标题={rep['title']} | 来源={rep['account_name'] or '未知公众号'}"
            )
        if any(record.get("direction") == "生活健康 / 医疗" for record in required_longtail):
            lines.append("特别注意：生活健康 / 医疗 已被选中，本期正文必须明确出现这个方向及其具体内容，禁止漏写。")
    lines.append("")
    lines.append("去重后的文章列表：")
    for i, row in enumerate(data["rows"][:80], start=1):
        lines.append(f"{i}. [{row['account_name'] or '未知'}] {row['title']} | {row['article_url']}")
    lines.append("")
    lines.append("主要聚类摘要（优先级排序后）：")
    for idx, record in enumerate(selected_cluster_records[:24], start=1):
        rep = record["representative"]
        direction = record.get("direction") or "其他观察"
        kind = "长尾保留" if direction in LONGTAIL_DIRECTIONS else "主要聚类"
        lines.append(
            f"{idx}. [{kind}] 方向：{direction} | 代表标题：{rep['title']} | 聚合数：{record['cluster_size']} | 来源数：{record['source_diversity']} | 来源：{', '.join(record['sources'])} | 综合分：{record['score']}"
        )
    github_items = data.get("github_items") or []
    if github_items:
        lines.append("")
        lines.append("从原文中抽取到的完整 GitHub / 开源项目地址：")
        for idx, item in enumerate(github_items[:80], start=1):
            lines.append(f"{idx}. [{item['account_name']}] {item['title']} | {item['url']}")
    entity_items = data.get("entity_items") or []
    if entity_items:
        lines.append("")
        lines.append("高风险实体锚点（标题模糊或涉及国家/军队/机构时，必须以这些源证据为准，禁止擅自替换主体）：")
        for idx, item in enumerate(entity_items[:60], start=1):
            ambiguous = "是" if item.get("ambiguous_title") else "否"
            lines.append(
                f"{idx}. [{item['account_name']}] 标题：{item['title']} | 标题是否歧义：{ambiguous} | 明确主体：{', '.join(item['anchors'])} | 原文首句：{item['snippet']} | 链接：{item['url']}"
            )
    return "\n".join(lines)


# ── Call MiniMax-M2.7 ──────────────────────────────────────────────────────

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

REPORT_REQUIRED_HEADINGS = [
    "## 📌 方向归类",
    "## 📝 内容详览",
    "## 🔗 链接附录",
]

SIGNAL_TAG_RE = re.compile(r"【(多源共识|单点信号)(?:·([^】]+))?】")


def is_retryable_error(text: str) -> bool:
    raw = (text or "").lower()
    return any(marker.lower() in raw for marker in RETRYABLE_ERROR_MARKERS)


def detect_signal_structure_issue(text: str):
    current_section = None
    section_source_counts = defaultdict(lambda: defaultdict(int))
    section_source_set = defaultdict(set)
    in_appendix = False
    for raw in (text or "").splitlines():
        line = raw.strip()
        if line.startswith("## ") and "链接附录" in line:
            in_appendix = True
        if in_appendix:
            continue
        if line.startswith("### "):
            current_section = re.sub(r'^\d+[\.、]\s*', '', line[4:]).strip()
            continue
        tags = SIGNAL_TAG_RE.findall(line)
        if len(tags) > 1:
            return "one signal line contains multiple signal tags / multiple events"
        if not current_section or not tags:
            continue
        kind, sources = tags[0]
        if kind != "单点信号":
            continue
        normalized_sources = [s.strip() for s in re.split(r"[\/、,，]", sources or "") if s.strip()]
        for src in normalized_sources:
            section_source_counts[current_section][src] += 1
            section_source_set[current_section].add(src)
            if section_source_counts[current_section][src] >= 3 and len(section_source_set[current_section]) >= 2:
                return f"section '{current_section}' repeats single-source signal too many times: {src}"
    return None


def detect_invalid_report_output(text: str):
    if not isinstance(text, str):
        return "report output is not text"
    raw = text.strip()
    if not raw:
        return "report output is empty"
    if raw.startswith("ERROR:"):
        return None

    parsed = extract_json_object(raw)
    if isinstance(parsed, dict):
        content = parsed.get("content")
        if parsed.get("type") == "message" and isinstance(content, list):
            text_blocks = [block for block in content if isinstance(block, dict) and block.get("type") == "text" and (block.get("text") or "").strip()]
            thinking_blocks = [block for block in content if isinstance(block, dict) and block.get("type") == "thinking"]
            if thinking_blocks and not text_blocks:
                return "provider returned thinking-only JSON instead of publishable report text"
        if "report_text" in parsed and isinstance(parsed.get("report_text"), str):
            inner = parsed.get("report_text", "")
            return detect_invalid_report_output(inner)
        if raw.startswith("{") and not any(h in raw for h in REPORT_REQUIRED_HEADINGS):
            return "provider returned JSON object instead of publishable markdown report"

    heading_hits = sum(1 for h in REPORT_REQUIRED_HEADINGS if h in raw)
    if heading_hits < len(REPORT_REQUIRED_HEADINGS):
        if '"type": "thinking"' in raw or '"thinking":' in raw:
            return "provider returned thinking payload instead of final markdown report"
        if raw.startswith("{") or raw.startswith("["):
            return "provider returned structured payload instead of final markdown report"
        return "report is missing required markdown sections"

    appendix_marker = "## 🔗 链接附录"
    appendix_idx = raw.find(appendix_marker)
    if appendix_idx < 0:
        return "report is missing appendix section"
    appendix_body = raw[appendix_idx + len(appendix_marker):].strip()
    if len(appendix_body) < 80:
        return "appendix section is unexpectedly short"
    if "http" not in appendix_body:
        return "appendix section is missing links"

    if len(raw) < 1200:
        return "report is unexpectedly short and may be truncated"
    signal_issue = detect_signal_structure_issue(raw)
    if signal_issue:
        return signal_issue
    return None


def load_openclaw_config():
    if not OPENCLAW_CONFIG_PATH.exists():
        return {}
    try:
        return json.loads(OPENCLAW_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def resolve_provider_config(provider_name: str):
    cfg = load_openclaw_config()
    return (((cfg.get("models") or {}).get("providers") or {}).get(provider_name) or {})

def resolve_minimax_api_key():
    if MINIMAX_API_KEY:
        return MINIMAX_API_KEY
    try:
        provider = resolve_provider_config("minimax")
        return provider.get("apiKey")
    except Exception:
        return None
    return None


def resolve_deepseek_config():
    provider = resolve_provider_config("deepseek")
    return {
        "api_key": provider.get("apiKey"),
        "base_url": (provider.get("baseUrl") or "https://api.deepseek.com/v1").rstrip("/"),
        "api": provider.get("api") or "openai-completions",
    }


class ApiCallTimeout(TimeoutError):
    pass


class time_limit:
    def __init__(self, seconds: int):
        self.seconds = max(1, int(seconds))
        self.prev_handler = None

    def _handle_timeout(self, signum, frame):
        raise ApiCallTimeout(f"API call exceeded {self.seconds}s")

    def __enter__(self):
        self.prev_handler = signal.getsignal(signal.SIGALRM)
        signal.signal(signal.SIGALRM, self._handle_timeout)
        signal.alarm(self.seconds)

    def __exit__(self, exc_type, exc, tb):
        signal.alarm(0)
        signal.signal(signal.SIGALRM, self.prev_handler)


def call_anthropic_compatible(base_url, api_key, model, system_prompt, user_message, max_retries=2):
    if not api_key:
        return f"ERROR: {model} api key is not set"

    if anthropic is not None:
        for attempt in range(max_retries + 1):
            try:
                client = anthropic.Anthropic(
                    base_url=base_url.rstrip('/'),
                    api_key=api_key,
                    timeout=150.0,
                    max_retries=0,
                )
                with time_limit(170):
                    message = client.messages.create(
                        model=model,
                        max_tokens=4096,
                        system=system_prompt,
                        messages=[
                            {
                                "role": "user",
                                "content": [
                                    {"type": "text", "text": user_message}
                                ],
                            }
                        ],
                    )
                parts = []
                for block in getattr(message, 'content', []) or []:
                    if getattr(block, 'type', None) == 'text':
                        parts.append(getattr(block, 'text', ''))
                if parts:
                    return ''.join(parts)
                return message.model_dump_json(indent=2)
            except Exception as e:
                print(f"[{model} sdk attempt {attempt+1}] {e}", file=sys.stderr)
                if attempt < max_retries:
                    time.sleep(2 ** attempt)
                else:
                    return f"ERROR: {e}"

    payload = json.dumps({
        "model": model,
        "max_tokens": 4096,
        "system": system_prompt,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_message}
                ],
            }
        ]
    }).encode("utf-8")

    headers = {
        "Content-Type": "application/json",
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
    }

    url = f"{base_url.rstrip('/')}/v1/messages"

    for attempt in range(max_retries + 1):
        try:
            req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
            with time_limit(150):
                with urllib.request.urlopen(req, timeout=120) as resp:
                    body = json.loads(resp.read().decode("utf-8"))
                if "content" in body:
                    for block in body["content"]:
                        if block.get("type") == "text":
                            return block["text"]
                    return json.dumps(body["content"], ensure_ascii=False)
                if "choices" in body:
                    return body["choices"][0]["message"]["content"]
                return json.dumps(body, ensure_ascii=False, indent=2)
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            print(f"[{model} attempt {attempt+1}] HTTP {e.code}: {err_body[:500]}", file=sys.stderr)
            if attempt < max_retries:
                time.sleep(2 ** attempt)
            else:
                return f"ERROR: HTTP {e.code} — {err_body[:500]}"
        except Exception as e:
            print(f"[{model} attempt {attempt+1}] {e}", file=sys.stderr)
            if attempt < max_retries:
                time.sleep(2 ** attempt)
            else:
                return f"ERROR: {e}"


def call_openai_compatible(base_url, api_key, model, system_prompt, user_message, max_retries=2):
    if not api_key:
        return f"ERROR: {model} api key is not set"

    payload = json.dumps({
        "model": model,
        "temperature": 0.3,
        "max_tokens": 4096,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ]
    }).encode("utf-8")

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    url = f"{base_url.rstrip('/')}/chat/completions"

    for attempt in range(max_retries + 1):
        try:
            req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
            with time_limit(150):
                with urllib.request.urlopen(req, timeout=120) as resp:
                    body = json.loads(resp.read().decode("utf-8"))
                if "choices" in body:
                    return body["choices"][0]["message"]["content"]
                return json.dumps(body, ensure_ascii=False, indent=2)
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            print(f"[{model} attempt {attempt+1}] HTTP {e.code}: {err_body[:500]}", file=sys.stderr)
            if attempt < max_retries:
                time.sleep(2 ** attempt)
            else:
                return f"ERROR: HTTP {e.code} — {err_body[:500]}"
        except Exception as e:
            print(f"[{model} attempt {attempt+1}] {e}", file=sys.stderr)
            if attempt < max_retries:
                time.sleep(2 ** attempt)
            else:
                return f"ERROR: {e}"


def _minimax_worker(queue, base_url, api_key, model, system_prompt, user_message, max_retries):
    try:
        text = call_anthropic_compatible(
            base_url,
            api_key,
            model,
            system_prompt,
            user_message,
            max_retries=max_retries,
        )
        queue.put({"text": text})
    except Exception as e:
        queue.put({"text": f"ERROR: {e}"})


def call_minimax_with_hard_timeout(system_prompt, user_message, max_retries=2):
    ctx = multiprocessing.get_context("fork")
    queue = ctx.Queue()
    proc = ctx.Process(
        target=_minimax_worker,
        args=(
            queue,
            MINIMAX_BASE_URL,
            resolve_minimax_api_key(),
            MINIMAX_MODEL,
            system_prompt,
            user_message,
            max_retries,
        ),
        daemon=True,
    )
    proc.start()
    proc.join(MINIMAX_CALL_TIMEOUT)
    if proc.is_alive():
        proc.terminate()
        proc.join(10)
        if proc.is_alive():
            proc.kill()
            proc.join(5)
        return f"ERROR: MiniMax call timeout after {MINIMAX_CALL_TIMEOUT}s"
    try:
        payload = queue.get_nowait()
        return payload.get("text") or "ERROR: MiniMax worker returned empty result"
    except Exception:
        if proc.exitcode and proc.exitcode != 0:
            return f"ERROR: MiniMax worker exited with code {proc.exitcode}"
        return "ERROR: MiniMax worker returned no result"


def call_model_with_fallback(system_prompt, user_message, max_retries=2, validate_fn=None, invalid_prefix="invalid output"):
    global LAST_PROVIDER_USED, LAST_FALLBACK_USED, LAST_FALLBACK_REASON
    LAST_PROVIDER_USED = MINIMAX_MODEL
    LAST_FALLBACK_USED = False
    LAST_FALLBACK_REASON = None

    minimax_text = None
    invalid_reason = None
    invalid_attempts = 0
    while invalid_attempts < 3:
        minimax_text = call_minimax_with_hard_timeout(
            system_prompt,
            user_message,
            max_retries=max_retries,
        )
        if isinstance(minimax_text, str) and minimax_text.startswith("ERROR:"):
            invalid_reason = None
            break
        invalid_reason = validate_fn(minimax_text) if validate_fn else None
        if not invalid_reason:
            return minimax_text
        invalid_attempts += 1
        print(
            f"{MINIMAX_MODEL} 返回非正式正文，第 {invalid_attempts}/3 次重试：{invalid_reason}",
            file=sys.stderr,
        )
        if invalid_attempts < 3:
            time.sleep(min(6, invalid_attempts * 2))

    if invalid_reason:
        minimax_text = f"ERROR: {invalid_prefix} after 3 attempts — {invalid_reason}"

    if not is_retryable_error(minimax_text) and not invalid_reason:
        return minimax_text

    deepseek_cfg = resolve_deepseek_config()
    LAST_PROVIDER_USED = DEEPSEEK_MODEL
    LAST_FALLBACK_USED = True
    LAST_FALLBACK_REASON = minimax_text
    print(f"MiniMax 失败且属于可重试错误，切换备用模型 {DEEPSEEK_MODEL}...", file=sys.stderr)
    fallback_text = call_openai_compatible(
        deepseek_cfg["base_url"],
        deepseek_cfg["api_key"],
        DEEPSEEK_MODEL,
        system_prompt,
        user_message,
        max_retries=1,
    )
    fallback_invalid_reason = validate_fn(fallback_text) if validate_fn else None
    if fallback_invalid_reason:
        return f"ERROR: fallback model returned {invalid_prefix} — {fallback_invalid_reason}"
    return fallback_text


def call_minimax(system_prompt, user_message, max_retries=2):
    return call_model_with_fallback(
        system_prompt,
        user_message,
        max_retries=max_retries,
        validate_fn=detect_invalid_report_output,
        invalid_prefix="invalid report output",
    )


# ── Main ────────────────────────────────────────────────────────────────────

def main():
    report_type = sys.argv[1] if len(sys.argv) > 1 else "morning"
    if report_type not in {"morning", "evening"}:
        raise SystemExit("usage: python report_generator.py [morning|evening] [--commit]")
    commit = "--commit" in sys.argv

    state = load_state()
    old_updated = None
    if ARTICLE_PREF_PROFILE_JSON_PATH.exists():
        try:
            old_updated = json.loads(ARTICLE_PREF_PROFILE_JSON_PATH.read_text(encoding='utf-8')).get('updated_at')
        except Exception:
            old_updated = None
    profile = ensure_article_preference_profile()
    if profile.get('updated_at') != old_updated:
        print(f"文章偏好画像已刷新：已标注 {profile.get('labeled_count', 0)} 篇（关注 {profile.get('follow_count', 0)} / 忽略 {profile.get('ignore_count', 0)} / 中性 {profile.get('neutral_count', 0)}）", file=sys.stderr)
    else:
        print(f"文章偏好画像跳过刷新：今日已生成或无新增标注（当前已标注 {profile.get('labeled_count', 0)} 篇）", file=sys.stderr)
    window_end = now_str()
    if report_type == "morning":
        window_start = state.get("last_evening_report_at") or "1970-01-01 00:00:00"
    else:
        window_start = state.get("last_morning_report_at") or "1970-01-01 00:00:00"

    rows = load_rows(window_start, window_end)
    data = dedupe_rows(rows)
    semantic = semantic_week_dedupe(data["rows"], window_start)
    data["rows"] = semantic["rows"]
    kept_urls = {r["article_url"] for r in data["rows"]}
    data["clusters"] = {
        key: [r for r in cluster if r["article_url"] in kept_urls]
        for key, cluster in data["clusters"].items()
    }
    data["clusters"] = {k: v for k, v in data["clusters"].items() if v}
    cluster_records = []
    for record in data.get("cluster_records") or []:
        rows_kept = [r for r in record["rows"] if r["article_url"] in kept_urls]
        if not rows_kept:
            continue
        rep = record["representative"]
        if rep["article_url"] not in kept_urls:
            rep = choose_cluster_representative(rows_kept)
        cluster_records.append({
            **record,
            "rows": rows_kept,
            "representative": rep,
            "cluster_size": len(rows_kept),
            "source_diversity": len({r['account_name'] or '未知公众号' for r in rows_kept}),
            "sources": sorted({r['account_name'] or '未知公众号' for r in rows_kept}),
        })
    data["cluster_records"] = cluster_records
    data["selected_cluster_records"] = select_balanced_cluster_records(cluster_records)
    data["history_count"] = semantic["history_count"]
    data["after_semantic_week_removed"] = semantic["semantic_removed"]
    data["after_semantic_week"] = len(data["rows"])
    data["semantic_dedupe_decisions"] = semantic["decisions"]
    data["github_items"] = collect_github_context(data["rows"])
    data["entity_items"] = collect_entity_context(data["rows"])

    print(f"去重统计：原始 {data['raw_count']} → URL去重 {data['after_url']} → 标题去重 {data['after_title']} → 主题去重 {data['after_topic']} → 周内语义去重 {data['after_semantic_week']}", file=sys.stderr)
    if data["github_items"]:
        print(f"检测到 {len(data['github_items'])} 个候选 GitHub / 开源项目地址，将结构化写入正文与附录。", file=sys.stderr)
    if data["entity_items"]:
        print(f"检测到 {len(data['entity_items'])} 条高风险实体锚点，将由结构化方向与来源绑定规避主体串线。", file=sys.stderr)
    print("使用 grounded deterministic pipeline 生成报告（方向 / 事件 / 来源 / 链接由程序锁定）...", file=sys.stderr)

    report_text = render_grounded_report(report_type, window_start, window_end, data)
    generation_provider_used = "grounded-deterministic"
    generation_fallback_used = False
    generation_fallback_reason = None

    result = {
        "report_type": report_type,
        "window_start": window_start,
        "window_end": window_end,
        "model_used": generation_provider_used,
        "fallback_used": generation_fallback_used,
        "fallback_reason": generation_fallback_reason,
        "stats": {
            "raw_count": data["raw_count"],
            "after_url": data["after_url"],
            "after_title": data["after_title"],
            "after_topic": data["after_topic"],
            "history_count": data.get("history_count", 0),
            "after_semantic_week": data.get("after_semantic_week", data["after_topic"]),
            "after_semantic_week_removed": data.get("after_semantic_week_removed", 0),
        },
        "report_text": report_text,
    }

    print(json.dumps(result, ensure_ascii=False))

    # Commit state if requested
    if commit:
        if report_type == "morning":
            state["last_morning_report_at"] = window_end
        else:
            state["last_evening_report_at"] = window_end
        save_state(state)
        print(f"\n[STATE] {report_type} report time committed: {window_end}", file=sys.stderr)


if __name__ == "__main__":
    main()
