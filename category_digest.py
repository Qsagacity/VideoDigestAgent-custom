#!/usr/bin/env python3
"""
Category digest runner.

Final workflow:
1. Fetch all new/unprocessed videos from configured sources.
2. Enrich YouTube candidates with view/like/comment stats.
3. Save candidate_videos.json and selected_videos.json.
4. Select top N videos by recent popularity score.
5. Classify selected videos into three equal-priority categories.
6. Extract transcript, chunk long transcripts, extract key information, and compress each video into one article.
7. Send one grouped directory email.
8. Send category-separated content emails.
9. One video appears in only one content email.
"""

import argparse
import html
import json
import logging
import math
import os
import re
import smtplib
import time
from collections import defaultdict
from datetime import datetime, timezone
from email.message import EmailMessage
from email.policy import SMTP as SMTP_POLICY
from pathlib import Path

from googleapiclient.discovery import build

import config
from youtube_monitor import get_new_videos
from transcript_extractor import get_transcript, get_bilibili_transcript
from summarizer import _llm_call
from history import mark_sent, mark_failed, save_summary_to_file


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


CATEGORY_LABELS = {
    "ai_tech_product": "AI / 科技 / 产品",
    "business_startup_interview": "商业 / 创业 / 产品访谈",
    "finance_macro_business_news": "金融 / 宏观 / 商业新闻",
}

CATEGORY_SHORT = {
    "ai_tech_product": "AI科技产品",
    "business_startup_interview": "商业创业访谈",
    "finance_macro_business_news": "金融宏观新闻",
}

CATEGORY_ORDER = [
    "ai_tech_product",
    "business_startup_interview",
    "finance_macro_business_news",
]

IMPORTANCE_RANK = {
    "S": 0,
    "A": 1,
    "B": 2,
    "C": 3,
}


MAX_VIDEOS_PER_CYCLE = int(os.getenv("DIGEST_MAX_VIDEOS_PER_CYCLE", "10"))

CANDIDATE_MAX_AGE_HOURS = int(os.getenv("DIGEST_CANDIDATE_MAX_AGE_HOURS", "72"))
LIKE_WEIGHT = float(os.getenv("DIGEST_LIKE_WEIGHT", "30"))
COMMENT_WEIGHT = float(os.getenv("DIGEST_COMMENT_WEIGHT", "10"))
RECENCY_DECAY_POWER = float(os.getenv("DIGEST_RECENCY_DECAY_POWER", "0.6"))

CHUNK_MAX_CHARS = int(os.getenv("DIGEST_CHUNK_MAX_CHARS", "12000"))
MAX_CHUNKS_PER_VIDEO = int(os.getenv("DIGEST_MAX_CHUNKS_PER_VIDEO", "0"))

CONTENT_BATCH_SIZE = int(os.getenv("DIGEST_CONTENT_BATCH_SIZE", "3"))
MAX_CONTENT_EMAILS_PER_CATEGORY = int(os.getenv("DIGEST_MAX_CONTENT_EMAILS_PER_CATEGORY", "0"))

BRIEF_MAX_CHARS = int(os.getenv("DIGEST_BRIEF_MAX_CHARS", "2000"))
IMPORTANT_BRIEF_MAX_CHARS = int(os.getenv("DIGEST_IMPORTANT_BRIEF_MAX_CHARS", "2500"))

ARCHIVE_ROOT = Path(os.getenv("DIGEST_ARCHIVE_DIR", "digest_archive"))


CLASSIFY_PROMPT = """
你是一个视频/新闻内容分类与重要性排序器。

请把输入内容分到以下三类之一：
1. ai_tech_product：AI / 科技 / 产品
2. business_startup_interview：商业 / 创业 / 产品访谈
3. finance_macro_business_news：金融 / 宏观 / 商业新闻

这三个分类平权重要，分类之间不要互相比较谁更重要。
你只做三件事：
1. 分类；
2. 在本分类内部判断重要性；
3. 提取“事件名称”和“主要内容”。

不要写新闻分析，不要写影响判断，不要写行业评论。

重要性等级必须按下面规则判断：

【S 级】
本分类内必须优先整理的内容。
通常需要满足至少 2 个条件：
- 出现明确关键人物、公司、产品、国家、机构或市场；
- 有具体数字、金额、估值、用户数、增长率、时间、技术参数或市场表现；
- 围绕一个清晰事件、产品更新、政策变化、融资估值、重要访谈观点展开；
- 信息密度高，可以整理出较完整的正文；
- 不是泛泛聊天，而是有明确事实、例子、观点或数据支撑。

S 级不等于“标题看起来很大”。如果只有宏大话题但缺少具体内容，不要评为 S。

【A 级】
有清晰主题和具体信息，值得整理。
通常特征：
- 有明确主体和动作，例如某公司发布产品、某人物谈判断、某市场发生变化；
- 有一定事实、观点或例子；
- 但具体数据、细节密度或独特性不如 S 级。

【B 级】
有一定价值，但适合简略整理。
通常特征：
- 主题相关，但信息密度一般；
- 具体数据或细节较少；
- 内容偏泛谈、评论、重复或背景介绍；
- 可以进入正文邮件，但不应该写很长。

【C 级】
低优先级，只保留在目录。
通常特征：
- 信息稀疏；
- 标题党或重复内容；
- 没有明确主体、动作、数据或观点；
- 转录内容价值较低；
- 不值得进入重点整理正文。

重要性评分 importance_score：
- S：85-100
- A：70-84
- B：45-69
- C：1-44

评分时不要虚高。如果不确定，宁可评低一级，不要为了显得重要而评高。

必须输出严格 JSON，不要 markdown，不要解释：
{
  "category_key": "ai_tech_product / business_startup_interview / finance_macro_business_news",
  "importance": "S / A / B / C",
  "importance_score": 1-100,
  "main_topic": "事件名称，必须具体",
  "subject_terms": ["关键人物/公司/产品/国家/机构/市场/数字"],
  "one_line_value": "主要内容：这条视频/新闻主要讲了什么，必须具体",
  "reason": "简短说明分类和重要性依据",
  "should_expand": true
}

事件名称要求：
- 必须具体，不能写“某公司”“某产品”“某创始人”“某国家”。
- 优先包含关键人物、公司、产品、国家、机构、市场、金额、估值、时间、用户数、增长率等。
- 如果原文没有明确名称或数据，才可以写“原文未说明具体名称/数据”。

主要内容要求：
- 只说明视频/新闻主要讲了什么；
- 必须保留关键人物、产品、公司、数字、事件；
- 不要写“可能影响”“值得关注”“行业机会”等泛泛表达。

should_expand 判断：
- S / A：通常为 true；
- B：如果内容虽然一般但仍有具体信息，可以为 true；
- C：通常为 false。
"""


CHUNK_EXTRACT_PROMPT = """
你是一个转录稿信息提取助手。

你的任务不是写文章，也不是做行业评论，而是从当前这一段转录文字中提取重要内容。

请按新闻六要素作为提取标准，但不要机械输出“何人、何事、何时、何地、为何、如何”六个标题。
你需要重点提取：
- 关键人物、公司、产品、国家、机构、市场；
- 发生了什么、说了什么、发布了什么、改变了什么；
- 时间、阶段、周期、地点、行业或使用场景；
- 背景、原因、问题；
- 方法、路径、技术、策略；
- 具体金额、估值、比例、用户数、增长率、技术参数、市场表现；
- 原文中反复强调的观点或判断。

要求：
1. 只基于输入转录文字，不要编造。
2. 保留具体名称和数字，不能模糊成“某公司”“某产品”“某数据”。
3. 删除寒暄、口癖、重复句、无信息闲聊。
4. 不要写“可能影响行业”“值得关注”“带来机会”等空泛话。
5. 输出要具体，可以用项目符号。
6. 如果这一段没有具体数据，请写“本段未提到明确具体数据”。

输出格式：

【本段重要内容】
- ...

【本段关键人物/公司/产品/数据】
- ...
"""


FINAL_BRIEF_PROMPT = """
请根据输入的转录稿信息，整理成一篇适合邮件阅读的内容稿。

重要原则：
你的主要任务是整理和压缩转录内容，不是写新闻评论，也不是做行业分析。
你需要提取重要内容，而不是完整复述全部文字。
请删除寒暄、重复口癖、无信息闲聊和跑题内容，但不能删除重要事实、人物、公司、产品、数字、例子和观点。

请按新闻六要素作为提取标准：
- 谁：关键人物、公司、机构、国家；
- 做了什么：事件、产品、动作、观点；
- 什么时候：时间、阶段、周期；
- 在哪里：行业、市场、平台、国家、场景；
- 为什么：背景、原因、问题；
- 怎么做：方法、路径、技术、策略。

但是输出时不要机械列“何人、何事、何时、何地、为何、如何”。
请把这些信息自然整理到正文里。

每个视频最终输出必须遵守输入中的“建议长度”和“硬性上限”。
硬性上限不是目标长度，不要默认写满 2000 或 2500 字。
只有信息密度很高、关键人物/产品/数据很多的 S 级内容，才可以接近 2500 字；普通内容应明显短于上限。

如果内容很多，优先保留：
1. 关键人物 / 公司 / 产品 / 国家 / 机构；
2. 具体事件和动作；
3. 具体数字、金额、估值、时间、技术参数；
4. 原文中反复强调的观点；
5. 能帮助理解内容结构的关键背景。

请按照以下结构输出：

【标题】
使用原视频/文章标题。

【来源】
保留来源、作者/博主。不要输出链接，链接由目录邮件统一管理。

【内容整理】
在保持原内容大致顺序的基础上整理：
- 添加小标题，帮助理解内容结构；
- 重要句子、关键词、公司名、产品名、数字、核心观点请加粗；
- 尽量保持原文意思，不要改写成评论；
- 不要加入原文没有的信息；
- 不要输出无意义乱码。

【重点内容与具体信息】
用 5-10 条整理这篇内容最重要的信息。
要求：
- 每条都要具体，不能写空泛判断；
- 把主要内容和具体事实/数据融合在一起写；
- 如果原文出现人物、公司、产品、国家/机构、金额、估值、时间、数字、技术参数、市场表现、关键判断，必须保留；
- 不要单独分成“重点总结”和“具体数据/事实”两个部分；
- 如果转录文字中没有具体数据，请写：“转录文字中未提到明确具体数据。”

写作要求：
- 以转录内容为主；
- 少评论，少拔高；
- 不要写“可能影响行业”“值得关注”“带来机会”等空泛话；
- 不要编造事实；
- 不要机械列六要素；
- 重点是把长视频的重要内容提取出来，整理得更清楚、更好读。
"""


COMPRESS_PROMPT = """
请根据输入中的重要性和信息密度压缩内容。
2000/2500 字只是硬性上限，不是默认目标。
内容信息密度不高时，应主动压缩到 600-1600 字，不要为了接近上限而扩写。

要求：
1. 保留原文中的关键人物、公司、产品、机构、国家、金额、估值、时间、数字、技术参数和关键观点。
2. 删除重复表达、空泛评论、寒暄、无信息内容。
3. 不要新增原文没有的信息。
4. 保留以下结构：
【标题】
【来源】
【内容整理】
【重点内容与具体信息】

输出必须是压缩后的正文，不要解释。
"""


def clean_text(text):
    return re.sub(r"[\xa0\u2000-\u200b\u2028\u2029\u202f\u205f\u3000\ufeff]", " ", text or "")


def remove_urls(text):
    return re.sub(r"https?://\S+", "", text or "").strip()


def safe_int(value, default=0):
    try:
        return int(value)
    except Exception:
        return default


def markdown_to_html(md):
    text = html.escape(md or "")
    text = re.sub(r"^# (.+)$", r"<h1>\1</h1>", text, flags=re.MULTILINE)
    text = re.sub(r"^## (.+)$", r"<h2>\1</h2>", text, flags=re.MULTILINE)
    text = re.sub(r"^### (.+)$", r"<h3>\1</h3>", text, flags=re.MULTILINE)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = text.replace("\n", "<br>\n")
    return text


def safe_filename(text, max_len=80):
    text = re.sub(r"[^\w\u4e00-\u9fff.-]+", "_", text or "")
    return text.strip("_")[:max_len] or "item"


def video_url(item):
    platform = item.get("platform", "youtube")
    vid = item["video_id"]
    if platform == "bilibili":
        bvid = vid.replace("bilibili:", "")
        return f"https://www.bilibili.com/video/{bvid}"
    return f"https://www.youtube.com/watch?v={vid}"


def parse_datetime(value):
    if not value:
        return None
    try:
        if value.endswith("Z"):
            value = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def age_hours(published_at):
    dt = parse_datetime(published_at)
    if not dt:
        return 9999.0
    now = datetime.now(timezone.utc)
    return max(0.0, (now - dt).total_seconds() / 3600.0)


def split_text(text, max_chars):
    text = text or ""
    if len(text) <= max_chars:
        return [text]

    chunks = []
    current = []

    for line in text.splitlines(True):
        current_len = sum(len(x) for x in current)
        if current_len + len(line) <= max_chars:
            current.append(line)
        else:
            if current:
                chunks.append("".join(current))
                current = []
            while len(line) > max_chars:
                chunks.append(line[:max_chars])
                line = line[max_chars:]
            current.append(line)

    if current:
        chunks.append("".join(current))

    return chunks


def select_representative_chunks(chunks, max_chunks):
    # max_chunks <= 0 means process all chunks.
    if max_chunks <= 0 or len(chunks) <= max_chunks:
        return list(enumerate(chunks, start=1)), False

    if max_chunks == 1:
        return [(1, chunks[0])], True

    selected = []
    last_index = len(chunks) - 1

    for i in range(max_chunks):
        idx = round(i * last_index / (max_chunks - 1))
        selected.append(idx)

    deduped = []
    seen = set()
    for idx in selected:
        if idx not in seen:
            seen.add(idx)
            deduped.append((idx + 1, chunks[idx]))

    return deduped, True


def extract_json(raw):
    raw = (raw or "").strip()

    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    match = re.search(r"\{.*\}", raw, flags=re.S)
    if match:
        return json.loads(match.group(0))

    raise ValueError(f"LLM did not return JSON: {raw[:300]}")


def safe_llm_call(system_prompt, user_message, task_name="llm_call", retries=3, sleep_seconds=3):
    last_error = None

    for attempt in range(1, retries + 1):
        try:
            result = _llm_call(system_prompt, user_message)

            if result is None:
                raise RuntimeError("LLM returned None")

            result = str(result).strip()

            if not result:
                raise RuntimeError("LLM returned empty content")

            return result

        except Exception as exc:
            last_error = exc
            logger.warning(
                "%s failed, attempt %d/%d: %s",
                task_name,
                attempt,
                retries,
                exc,
            )
            if attempt < retries:
                time.sleep(sleep_seconds * attempt)

    raise RuntimeError(f"{task_name} failed after {retries} retries: {last_error}")


def save_text(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def normalize_video(video):
    item = dict(video)
    item.setdefault("platform", "youtube")
    item.setdefault("description", "")
    item.setdefault("source", "channel")
    item.setdefault("channel", item.get("channelTitle", "unknown"))
    item["url"] = video_url(item)
    return item


def enrich_youtube_statistics(videos):
    youtube_videos = [
        v for v in videos
        if v.get("platform", "youtube") == "youtube" and v.get("video_id")
    ]

    if not youtube_videos:
        return videos

    by_id = {v["video_id"]: v for v in youtube_videos}
    ids = list(by_id.keys())

    youtube = build("youtube", "v3", developerKey=config.YOUTUBE_API_KEY)

    for i in range(0, len(ids), 50):
        batch = ids[i:i + 50]
        try:
            resp = youtube.videos().list(
                part="statistics,snippet,contentDetails",
                id=",".join(batch),
            ).execute()

            for entry in resp.get("items", []):
                vid = entry["id"]
                target = by_id.get(vid)
                if not target:
                    continue

                stats = entry.get("statistics", {})
                snippet = entry.get("snippet", {})

                target["view_count"] = safe_int(stats.get("viewCount", 0))
                target["like_count"] = safe_int(stats.get("likeCount", 0))
                target["comment_count"] = safe_int(stats.get("commentCount", 0))

                if snippet.get("publishedAt"):
                    target["published_at"] = snippet.get("publishedAt")
                if snippet.get("title"):
                    target["title"] = snippet.get("title")
                if snippet.get("channelTitle"):
                    target.setdefault("channel_title", snippet.get("channelTitle"))

        except Exception as exc:
            logger.warning("Failed to enrich YouTube statistics: %s", exc)

    for v in videos:
        v.setdefault("view_count", 0)
        v.setdefault("like_count", 0)
        v.setdefault("comment_count", 0)

    return videos


def compute_candidate_score(video):
    views = safe_int(video.get("view_count", 0))
    likes = safe_int(video.get("like_count", 0))
    comments = safe_int(video.get("comment_count", 0))
    hours = age_hours(video.get("published_at", ""))

    popularity = views + likes * LIKE_WEIGHT + comments * COMMENT_WEIGHT
    recency_factor = math.pow(hours + 2.0, RECENCY_DECAY_POWER)

    score = popularity / recency_factor

    video["age_hours"] = round(hours, 2)
    video["selection_score"] = round(score, 2)

    return score


def select_videos(candidates, cycle_dir):
    candidates = [normalize_video(v) for v in candidates]
    candidates = enrich_youtube_statistics(candidates)

    for v in candidates:
        compute_candidate_score(v)

    save_text(
        cycle_dir / "candidate_videos.json",
        json.dumps(candidates, ensure_ascii=False, indent=2),
    )

    if not candidates:
        return []

    recent_candidates = [
        v for v in candidates
        if age_hours(v.get("published_at", "")) <= CANDIDATE_MAX_AGE_HOURS
    ]

    if recent_candidates:
        pool = recent_candidates
        logger.info(
            "Candidate pool filtered to %d recent videos within %d hours",
            len(pool),
            CANDIDATE_MAX_AGE_HOURS,
        )
    else:
        pool = candidates
        logger.info("No candidates within age window; using all %d candidates", len(pool))

    pool.sort(
        key=lambda v: (
            float(v.get("selection_score", 0)),
            v.get("published_at", ""),
        ),
        reverse=True,
    )

    selected = pool[:MAX_VIDEOS_PER_CYCLE] if MAX_VIDEOS_PER_CYCLE > 0 else pool

    selected_dump = []
    for rank, v in enumerate(selected, 1):
        selected_dump.append({
            "rank": rank,
            "video_id": v.get("video_id"),
            "title": v.get("title"),
            "channel": v.get("channel"),
            "published_at": v.get("published_at"),
            "view_count": v.get("view_count", 0),
            "like_count": v.get("like_count", 0),
            "comment_count": v.get("comment_count", 0),
            "age_hours": v.get("age_hours"),
            "selection_score": v.get("selection_score"),
            "url": v.get("url"),
            "source": v.get("source"),
            "selection_reason": "在未处理候选视频中，按播放量、点赞量、评论数和新鲜度综合排序入选。",
        })

    save_text(
        cycle_dir / "selected_videos.json",
        json.dumps(selected_dump, ensure_ascii=False, indent=2),
    )

    logger.info("Selected %d videos for this cycle", len(selected))

    for row in selected_dump:
        logger.info(
            "Selected #%s: %s | views=%s likes=%s comments=%s score=%s",
            row["rank"],
            row["title"][:80],
            row["view_count"],
            row["like_count"],
            row["comment_count"],
            row["selection_score"],
        )

    return selected


def fallback_category(title, channel):
    text = f"{title} {channel}".lower()

    finance_words = [
        "fed", "rate", "inflation", "macro", "economy", "stock", "market",
        "nasdaq", "s&p", "gold", "oil", "bond", "bitcoin", "crypto",
        "美联储", "降息", "加息", "通胀", "宏观", "金融", "股票", "美股",
        "日元", "黄金", "债券", "汇率", "比特币", "估值", "融资",
    ]

    business_words = [
        "founder", "startup", "yc", "ceo", "interview", "podcast",
        "business", "entrepreneur", "growth", "商业", "创业", "创始人",
        "访谈", "增长", "商业化", "管理", "组织",
    ]

    ai_words = [
        "ai", "openai", "anthropic", "claude", "gemini", "agent", "llm",
        "cursor", "code", "robot", "tech", "product", "nvidia", "gpu",
        "人工智能", "大模型", "智能体", "模型", "科技", "产品", "英伟达",
    ]

    if any(w in text for w in finance_words):
        cat = "finance_macro_business_news"
    elif any(w in text for w in business_words):
        cat = "business_startup_interview"
    elif any(w in text for w in ai_words):
        cat = "ai_tech_product"
    else:
        cat = "ai_tech_product"

    return {
        "category_key": cat,
        "importance": "B",
        "importance_score": 50,
        "main_topic": title,
        "subject_terms": [title[:30]],
        "one_line_value": f"主要内容：{title}",
        "reason": "LLM 分类失败后使用关键词兜底。",
        "should_expand": True,
    }


def normalize_classification(result, video):
    if result.get("category_key") not in CATEGORY_LABELS:
        result = fallback_category(video.get("title", ""), video.get("channel", ""))

    importance = str(result.get("importance", "B")).upper()
    if importance not in IMPORTANCE_RANK:
        importance = "B"

    try:
        score = int(result.get("importance_score", 50))
    except Exception:
        score = 50

    if importance == "S":
        score = max(score, 85)
    elif importance == "A":
        score = min(max(score, 70), 84)
    elif importance == "B":
        score = min(max(score, 45), 69)
    elif importance == "C":
        score = min(score, 44)

    result["importance"] = importance
    result["importance_score"] = max(1, min(100, score))
    result["subject_terms"] = result.get("subject_terms") or []
    result["main_topic"] = result.get("main_topic") or video.get("title", "")
    result["one_line_value"] = result.get("one_line_value") or f"主要内容：{video.get('title', '')}"
    result["should_expand"] = bool(result.get("should_expand", importance in ("S", "A", "B")))

    return result


def classify_item(video, transcript):
    preview = transcript[:4500] if transcript else ""

    user_msg = f"""
标题：{video.get("title", "")}
来源/博主：{video.get("channel", "")}
发布时间：{video.get("published_at", "")}
平台：{video.get("platform", "youtube")}
播放量：{video.get("view_count", 0)}
点赞量：{video.get("like_count", 0)}
评论数：{video.get("comment_count", 0)}
链接：{video_url(video)}

Transcript preview:
{preview}
"""

    try:
        raw = safe_llm_call(CLASSIFY_PROMPT, user_msg, task_name="classify_item")
        result = extract_json(raw)
    except Exception as exc:
        logger.warning("Classification failed, using fallback: %s", exc)
        result = fallback_category(video.get("title", ""), video.get("channel", ""))

    return normalize_classification(result, video)


def target_brief_chars(classification):
    importance = str(classification.get("importance", "B")).upper()
    if importance == "S":
        return IMPORTANT_BRIEF_MAX_CHARS
    return BRIEF_MAX_CHARS


def target_length_instruction(classification):
    importance = str(classification.get("importance", "B")).upper()
    score = safe_int(classification.get("importance_score", 50), 50)

    if importance == "S":
        if score >= 90:
            return "建议长度 1800-2500 字；只有信息密度很高、关键人物/产品/数据很多时才写到 2500 字。"
        return "建议长度 1200-1800 字；不要为了接近 2500 字而扩写。"

    if importance == "A":
        return "建议长度 1000-1600 字；保留主要内容和具体信息即可。"

    if importance == "B":
        return "建议长度 600-1000 字；只保留主要内容、关键事实和具体数据。"

    return "建议长度 300-600 字；只做非常简短整理，低价值内容不要扩写。"


def extract_chunk_notes(video, classification, chunk, chunk_index, total_chunks):
    user_msg = f"""
视频/新闻元信息：
- 标题：{video.get("title", "")}
- 来源/博主：{video.get("channel", "")}
- 发布时间：{video.get("published_at", "")}
- 已分类：{CATEGORY_LABELS[classification["category_key"]]}
- 重要性：{classification["importance"]}（{classification["importance_score"]}）
- 事件名称：{classification.get("main_topic", "")}
- 当前分块：第 {chunk_index}/{total_chunks} 块

Transcript chunk:
{chunk}
"""
    return safe_llm_call(
        CHUNK_EXTRACT_PROMPT,
        user_msg,
        task_name="extract_chunk_notes",
    ).strip()


def final_brief_from_notes(video, classification, notes, coverage_note=""):
    target_chars = target_brief_chars(classification)
    length_instruction = target_length_instruction(classification)

    user_msg = f"""
视频/新闻元信息：
- 标题：{video.get("title", "")}
- 来源/博主：{video.get("channel", "")}
- 发布时间：{video.get("published_at", "")}
- 已分类：{CATEGORY_LABELS[classification["category_key"]]}
- 重要性：{classification["importance"]}（{classification["importance_score"]}）
- 事件名称：{classification.get("main_topic", "")}
- 硬性上限：不超过 {target_chars} 中文字
- 建议长度：{length_instruction}

覆盖说明：
{coverage_note}

转录稿重要信息：
{notes}
"""
    brief = safe_llm_call(
        FINAL_BRIEF_PROMPT,
        user_msg,
        task_name="final_brief",
    ).strip()

    if len(brief) > int(target_chars * 1.25):
        logger.warning(
            "Brief too long for %s: %d chars, compressing",
            video.get("title", ""),
            len(brief),
        )
        brief = safe_llm_call(
            COMPRESS_PROMPT,
            f"""
目标：
- 硬性上限：不超过 {target_chars} 中文字
- 建议长度：{length_instruction}

标题：{video.get('title', '')}
来源：{video.get('channel', '')}

内容：
{brief}
""",
            task_name="compress_brief",
        ).strip()

    return brief


def generate_single_brief(video, transcript, classification, notes_path=None):
    chunks = split_text(transcript, CHUNK_MAX_CHARS)
    selected_chunks, truncated = select_representative_chunks(chunks, MAX_CHUNKS_PER_VIDEO)

    if truncated:
        logger.warning(
            "Transcript too long for %s: %d chunks, processing %d representative chunks",
            video.get("title", ""),
            len(chunks),
            len(selected_chunks),
        )
        coverage_note = (
            f"原转录文字共 {len(chunks)} 个分块，本次抽取 {len(selected_chunks)} 个代表分块进行整理。"
            "因此结果用于提取重点内容，不代表逐字覆盖完整视频。"
        )
    else:
        coverage_note = f"原转录文字共 {len(chunks)} 个分块，本次已处理全部可用分块。"

    if len(chunks) == 1:
        notes = f"完整转录文字：\n{chunks[0]}"
        if notes_path:
            save_text(notes_path, coverage_note + "\n\n" + notes)
        return final_brief_from_notes(video, classification, notes, coverage_note)

    notes_list = []

    for original_index, chunk in selected_chunks:
        logger.info(
            "Extracting chunk notes for %s: chunk %d/%d",
            video.get("title", ""),
            original_index,
            len(chunks),
        )
        notes = extract_chunk_notes(video, classification, chunk, original_index, len(chunks))
        notes_list.append(f"【分块 {original_index}/{len(chunks)}】\n{notes}")

    combined_notes = "\n\n".join(notes_list)

    if notes_path:
        save_text(notes_path, coverage_note + "\n\n" + combined_notes)

    return final_brief_from_notes(video, classification, combined_notes, coverage_note)


def send_email(subject, body_md):
    subject = clean_text(subject)
    body_md = clean_text(body_md)

    msg = EmailMessage(policy=SMTP_POLICY)
    msg["Subject"] = subject
    msg["From"] = config.SENDER_EMAIL
    msg["To"] = ", ".join(config.RECIPIENT_EMAILS)

    msg.set_content(body_md)

    html_body = f"""\
<html>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif;line-height:1.65;color:#222;max-width:880px;margin:0 auto;padding:20px;">
{markdown_to_html(body_md)}
</body>
</html>
"""
    msg.add_alternative(html_body, subtype="html")

    with smtplib.SMTP(config.SMTP_SERVER, config.SMTP_PORT) as server:
        server.starttls()
        server.login(config.SENDER_EMAIL, config.SENDER_PASSWORD)
        server.send_message(msg)

    logger.info("Email sent: %s", subject)


def sort_items(items):
    return sorted(
        items,
        key=lambda x: (
            IMPORTANCE_RANK.get(x["classification"].get("importance", "B"), 2),
            -safe_int(x["classification"].get("importance_score", 50), 50),
            x.get("published_at", ""),
        ),
    )


def grouped_by_category(items):
    grouped = defaultdict(list)
    for item in items:
        key = item["classification"]["category_key"]
        grouped[key].append(item)
    return grouped


def top_terms(items, max_terms=4):
    terms = []
    for item in sort_items(items):
        for term in item["classification"].get("subject_terms", []):
            term = str(term).strip()
            if term and term not in terms:
                terms.append(term)

        topic = item["classification"].get("main_topic", "")
        if topic and topic not in terms:
            terms.append(topic)

        if len(terms) >= max_terms:
            break

    return "、".join(terms[:max_terms]) or "本轮重点内容"


def normalize_main_content(text):
    text = str(text or "").strip()
    if not text:
        return "主要内容：原文未提供明确主要内容。"
    if text.startswith("主要内容"):
        return text
    return "主要内容：" + text


def build_directory_mail(items):
    items = sort_items(items)
    grouped = grouped_by_category(items)

    subject = f"【本轮重点目录】{top_terms(items)}"

    lines = []
    lines.append("【本轮重点目录】")
    lines.append("")
    lines.append(f"本轮成功整理 {len(items)} 条内容。以下按三类分别展示，每类内部按重要性排序。")
    lines.append("")

    section_no = 1

    for category_key in CATEGORY_ORDER:
        cat_items = sort_items(grouped.get(category_key, []))
        if not cat_items:
            continue

        lines.append(f"{section_no}、{CATEGORY_LABELS[category_key]}（{len(cat_items)} 条）")
        lines.append("")

        for idx, item in enumerate(cat_items, 1):
            c = item["classification"]
            topic = c.get("main_topic") or item["title"]
            main = normalize_main_content(c.get("one_line_value"))
            url = item["url"]
            stats = ""

            if item.get("platform", "youtube") == "youtube":
                stats = (
                    f"（播放 {item.get('view_count', 0)}，"
                    f"点赞 {item.get('like_count', 0)}，"
                    f"评论 {item.get('comment_count', 0)}）"
                )

            lines.append(f"{idx}. 事件名称：[{topic}]({url}) {stats}")
            lines.append(main)
            lines.append("")

        section_no += 1

    return {
        "subject": subject,
        "body": "\n".join(lines).strip(),
    }


def build_content_mail(category_key, items, mail_no):
    items = sort_items(items)
    label = CATEGORY_SHORT[category_key]
    subject = f"【{label}｜重点整理{mail_no}】{top_terms(items, max_terms=3)}"

    lines = []
    lines.append("【本封内容】")
    lines.append(
        f"本封整理 {CATEGORY_LABELS[category_key]} 分类下的 {len(items)} 篇内容，按本分类内部重要性排序。"
        "链接已放在总目录邮件中，这里不重复附链接。"
    )
    lines.append("")

    for idx, item in enumerate(items, 1):
        c = item["classification"]
        brief = remove_urls(item["brief"])

        lines.append(f"# {idx}. {item['title']}")
        lines.append(f"来源：{item['channel']}")
        lines.append(f"事件名称：{c.get('main_topic', item['title'])}")
        lines.append(f"重要性：{c['importance']}（{c['importance_score']}）")
        if item.get("platform", "youtube") == "youtube":
            lines.append(
                f"视频数据：播放 {item.get('view_count', 0)}；"
                f"点赞 {item.get('like_count', 0)}；"
                f"评论 {item.get('comment_count', 0)}"
            )
        lines.append("")
        lines.append(brief)
        lines.append("")
        lines.append("---")
        lines.append("")

    return {
        "subject": subject,
        "body": "\n".join(lines).strip(),
    }


def build_all_mails(items, cycle_dir):
    items = sort_items(items)

    mails = []
    mails.append({
        "kind": "01_directory",
        **build_directory_mail(items),
    })

    grouped = grouped_by_category(items)
    mail_counter = 2

    for category_key in CATEGORY_ORDER:
        cat_items = sort_items(grouped.get(category_key, []))

        expandable = [
            item for item in cat_items
            if item["classification"].get("importance") in ("S", "A", "B")
            and item["classification"].get("should_expand", True)
        ]

        if MAX_CONTENT_EMAILS_PER_CATEGORY > 0:
            max_items = CONTENT_BATCH_SIZE * MAX_CONTENT_EMAILS_PER_CATEGORY
            expandable = expandable[:max_items]
            mail_count = MAX_CONTENT_EMAILS_PER_CATEGORY
        else:
            mail_count = (len(expandable) + CONTENT_BATCH_SIZE - 1) // CONTENT_BATCH_SIZE

        for mail_idx in range(mail_count):
            start = mail_idx * CONTENT_BATCH_SIZE
            end = start + CONTENT_BATCH_SIZE
            batch = expandable[start:end]

            if not batch:
                continue

            mail = build_content_mail(category_key, batch, mail_idx + 1)
            mails.append({
                "kind": f"{mail_counter:02d}_{category_key}_content_{mail_idx + 1}",
                **mail,
            })
            mail_counter += 1

    email_dir = cycle_dir / "emails"
    email_dir.mkdir(parents=True, exist_ok=True)

    for mail in mails:
        path = email_dir / f"{mail['kind']}.md"
        save_text(path, f"# {mail['subject']}\n\n{mail['body']}")
        logger.info("Saved generated mail: %s", path)

    return mails


def send_mails(mails, dry_run=False):
    sent_count = 0

    for mail in mails:
        if config.OUTPUT_MODE in ("email", "both") and not dry_run:
            send_email(mail["subject"], mail["body"])
            sent_count += 1
        else:
            logger.info("[dry/local] Not sending email: %s", mail["subject"])

    return sent_count


def process_video_for_digest(video, cycle_dir):
    vid_id = video["video_id"]
    title = video["title"]
    channel = video.get("channel", "unknown")
    source = video.get("source", "channel")
    platform = video.get("platform", "youtube")

    logger.info("Processing for category digest: %s", title)

    try:
        if platform == "bilibili":
            bvid = video.get("bvid") or vid_id.replace("bilibili:", "")
            transcript = get_bilibili_transcript(bvid)
        else:
            transcript = get_transcript(vid_id)

        safe_vid = safe_filename(f"{platform}_{vid_id}")
        transcript_path = cycle_dir / "transcripts" / f"{safe_vid}.txt"
        save_text(transcript_path, transcript)
        logger.info("Saved transcript: %s", transcript_path)

    except RuntimeError as exc:
        logger.warning("Skipping %s — %s", vid_id, exc)
        mark_failed(vid_id, title, channel, str(exc), source=source, platform=platform)
        return None

    try:
        classification = classify_item(video, transcript)

        safe_vid = safe_filename(f"{platform}_{vid_id}")
        notes_path = cycle_dir / "notes" / f"{safe_vid}.md"

        brief = generate_single_brief(
            video,
            transcript,
            classification,
            notes_path=notes_path,
        )

    except Exception as exc:
        logger.exception("Digest generation failed for %s", vid_id)
        mark_failed(vid_id, title, channel, str(exc), source=source, platform=platform)
        return None

    item = {
        "video_id": vid_id,
        "title": title,
        "channel": channel,
        "published_at": video.get("published_at", ""),
        "source": source,
        "platform": platform,
        "url": video_url(video),
        "view_count": video.get("view_count", 0),
        "like_count": video.get("like_count", 0),
        "comment_count": video.get("comment_count", 0),
        "selection_score": video.get("selection_score", 0),
        "classification": classification,
        "brief": brief,
    }

    safe_vid = safe_filename(f"{platform}_{vid_id}")
    item_path = cycle_dir / "items" / classification["category_key"] / f"{safe_vid}.json"
    item_path.parent.mkdir(parents=True, exist_ok=True)
    item_path.write_text(json.dumps(item, ensure_ascii=False, indent=2), encoding="utf-8")

    save_summary_to_file(
        vid_id,
        title,
        channel,
        {"Chinese": brief},
        platform=platform,
    )

    return item


def mark_items_sent(items):
    for item in items:
        mark_sent(
            item["video_id"],
            item["title"],
            item["channel"],
            source=item.get("source", "channel"),
            platform=item.get("platform", "youtube"),
        )


def fetch_candidate_videos():
    all_videos = []

    if config.YOUTUBE_CHANNELS or config.YOUTUBE_SEARCH_ENABLED:
        all_videos.extend(get_new_videos())

    if getattr(config, "BILIBILI_ENABLED", False):
        from bilibili_monitor import get_new_videos as get_bilibili_videos
        all_videos.extend(get_bilibili_videos())

    return [normalize_video(v) for v in all_videos]


def run_once(dry_run=False):
    cycle_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    cycle_dir = ARCHIVE_ROOT / cycle_id
    cycle_dir.mkdir(parents=True, exist_ok=True)

    candidates = fetch_candidate_videos()

    if not candidates:
        logger.info("No new videos found.")
        return 0

    logger.info("Fetched %d candidate videos", len(candidates))

    selected_videos = select_videos(candidates, cycle_dir)

    if not selected_videos:
        logger.info("No videos selected for processing.")
        return 0

    items = []

    for video in selected_videos:
        item = process_video_for_digest(video, cycle_dir)
        if item:
            items.append(item)

    if not items:
        logger.info("No valid digest items generated.")
        return 0

    try:
        mails = build_all_mails(items, cycle_dir)
        sent_count = send_mails(mails, dry_run=dry_run)

        if not dry_run:
            mark_items_sent(items)

    except Exception as exc:
        logger.exception("Mail build/send failed")

        for item in items:
            mark_failed(
                item["video_id"],
                item["title"],
                item["channel"],
                f"mail build/send failed: {exc}",
                source=item.get("source", "channel"),
                platform=item.get("platform", "youtube"),
            )

        return 0

    logger.info(
        "Cycle done. Candidates: %d, selected: %d, items processed: %d, emails sent: %d, archive: %s",
        len(candidates),
        len(selected_videos),
        len(items),
        sent_count,
        cycle_dir,
    )

    return len(items)


def run_poll(dry_run=False):
    logger.info("Starting category digest polling loop.")
    logger.info(
        "Poll interval: %d seconds / %d minutes",
        config.POLL_INTERVAL,
        config.POLL_INTERVAL // 60,
    )

    while True:
        try:
            run_once(dry_run=dry_run)
        except Exception:
            logger.exception("Error during category digest polling cycle")

        logger.info("Sleeping %d seconds until next check...", config.POLL_INTERVAL)
        time.sleep(config.POLL_INTERVAL)


def main():
    parser = argparse.ArgumentParser(description="Category Digest Runner")
    parser.add_argument("--poll", action="store_true", help="Run continuously")
    parser.add_argument("--dry-run", action="store_true", help="Generate locally without sending email or marking sent")
    args = parser.parse_args()

    if args.poll:
        run_poll(dry_run=args.dry_run)
    else:
        run_once(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
