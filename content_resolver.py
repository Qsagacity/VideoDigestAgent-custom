"""Multi-source content resolver.

Core rule:
First Success Wins.

For each selected video, try sources in priority order. As soon as a reliable
same-content text is found, stop and return it to the digest pipeline.

Same-topic material is clearly marked as supplement and never pretends to be
the original transcript.
"""

from __future__ import annotations

import html
import json
import logging
import os
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
import yaml
from typing import Optional

from dotenv import dotenv_values

from transcript_extractor import (
    get_bilibili_transcript,
    get_youtube_captions_only,
    read_transcript_cache,
    transcribe_url_with_whisper,
    transcribe_youtube_hard_fallback,
    write_transcript_cache,
)

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
_ENV = dotenv_values(BASE_DIR / ".env")

MIN_TEXT_CHARS = int(
    _ENV.get("RESOLVER_MIN_TEXT_CHARS")
    or os.getenv("RESOLVER_MIN_TEXT_CHARS")
    or "800"
)

MAX_SEARCH_RESULTS = int(
    _ENV.get("RESOLVER_MAX_SEARCH_RESULTS")
    or os.getenv("RESOLVER_MAX_SEARCH_RESULTS")
    or "5"
)

WEB_SEARCH_ENABLED = (
    _ENV.get("RESOLVER_WEB_SEARCH")
    or os.getenv("RESOLVER_WEB_SEARCH")
    or "true"
).lower() in ("1", "true", "yes", "on")

NEWS_SEARCH_ENABLED = (
    _ENV.get("RESOLVER_NEWS_SEARCH")
    or os.getenv("RESOLVER_NEWS_SEARCH")
    or "true"
).lower() in ("1", "true", "yes", "on")

ALLOW_SUPPLEMENT_ONLY = (
    _ENV.get("RESOLVER_ALLOW_SUPPLEMENT_ONLY")
    or os.getenv("RESOLVER_ALLOW_SUPPLEMENT_ONLY")
    or "true"
).lower() in ("1", "true", "yes", "on")

YOUTUBE_HARD_FALLBACK_ENABLED = (
    _ENV.get("YOUTUBE_HARD_FALLBACK_ENABLED")
    or os.getenv("YOUTUBE_HARD_FALLBACK_ENABLED")
    or "false"
).lower() in ("1", "true", "yes", "on")


VERIFIED_CREATOR_SOURCES_ENABLED = (
    _ENV.get("VERIFIED_CREATOR_SOURCES_ENABLED")
    or os.getenv("VERIFIED_CREATOR_SOURCES_ENABLED")
    or "false"
).lower() in ("1", "true", "yes", "on")

CREATOR_SOURCES_FILE = Path(
    _ENV.get("CREATOR_SOURCES_FILE")
    or os.getenv("CREATOR_SOURCES_FILE")
    or "creator_sources.enabled.yaml"
)
if not CREATOR_SOURCES_FILE.is_absolute():
    CREATOR_SOURCES_FILE = BASE_DIR / CREATOR_SOURCES_FILE

VERIFIED_MIN_TITLE_SIMILARITY = float(
    _ENV.get("VERIFIED_MIN_TITLE_SIMILARITY")
    or os.getenv("VERIFIED_MIN_TITLE_SIMILARITY")
    or "0.55"
)

VERIFIED_MAX_LINKS_PER_SOURCE = int(
    _ENV.get("VERIFIED_MAX_LINKS_PER_SOURCE")
    or os.getenv("VERIFIED_MAX_LINKS_PER_SOURCE")
    or "8"
)

PODCAST_FEEDS = [
    x.strip()
    for x in (
        _ENV.get("PODCAST_FEEDS")
        or os.getenv("PODCAST_FEEDS")
        or ""
    ).split(",")
    if x.strip()
]

URL_RE = re.compile(r"https?://[^\s<>()\"']+")


@dataclass
class ResolveResult:
    text: str
    source_type: str
    source_url: str
    match_type: str
    confidence: float
    risk_level: str
    note: str = ""

    def ok(self) -> bool:
        return bool(self.text and len(self.text.strip()) >= MIN_TEXT_CHARS)

    def to_dict(self) -> dict:
        return {
            "text": self.text.strip(),
            "source_type": self.source_type,
            "source_url": self.source_url,
            "match_type": self.match_type,
            "confidence": round(float(self.confidence), 3),
            "risk_level": self.risk_level,
            "note": self.note,
        }


def _norm(text: str) -> str:
    text = html.unescape(text or "").lower()
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _similarity(a: str, b: str) -> float:
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return 0.0
    if a in b or b in a:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def _strip_html(raw: str) -> str:
    raw = re.sub(r"(?is)<script.*?>.*?</script>", " ", raw)
    raw = re.sub(r"(?is)<style.*?>.*?</style>", " ", raw)
    raw = re.sub(r"(?is)<br\s*/?>", "\n", raw)
    raw = re.sub(r"(?is)</p>", "\n", raw)
    raw = re.sub(r"(?is)</div>", "\n", raw)
    text = re.sub(r"(?is)<.*?>", " ", raw)
    text = html.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


def _fetch(url: str, timeout: int = 25, max_bytes: int = 2_000_000) -> str:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; VideoDigestAgent/1.0)",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read(max_bytes)
        charset = resp.headers.get_content_charset() or "utf-8"
        return data.decode(charset, errors="ignore")


def _extract_urls(description: str) -> list[str]:
    urls = []
    for m in URL_RE.finditer(description or ""):
        url = m.group(0).rstrip(".,;，。)")
        if url not in urls:
            urls.append(url)
    return urls


def _text_quality(text: str) -> bool:
    text = (text or "").strip()
    if len(text) < MIN_TEXT_CHARS:
        return False
    # Avoid pages that are mostly navigation.
    words = len(_norm(text).split())
    return words >= 80 or len(text) >= MIN_TEXT_CHARS * 1.5


def _is_same_content_page(url: str, text: str, video: dict) -> tuple[bool, float, str]:
    title = video.get("title", "")
    channel = video.get("channel", "")
    url_l = url.lower()
    sample = text[:4000]

    title_score = max(
        _similarity(title, sample[:1500]),
        _similarity(title, url),
    )
    channel_score = _similarity(channel, sample[:2000]) if channel else 0.0

    same_content_hints = [
        "transcript",
        "show notes",
        "show-notes",
        "episode",
        "podcast",
        "文字稿",
        "访谈",
        "播客",
        "transcription",
    ]

    has_hint = any(h in url_l or h in sample.lower() for h in same_content_hints)

    confidence = max(title_score, title_score * 0.8 + channel_score * 0.2)
    if has_hint and confidence >= 0.22:
        return True, max(confidence, 0.72), "same-content hint + title/channel match"

    if confidence >= 0.55:
        return True, confidence, "high title/channel similarity"

    return False, confidence, "not enough same-content evidence"


def resolver_cache(video: dict) -> Optional[ResolveResult]:
    platform = video.get("platform", "youtube")
    vid = video.get("video_id", "")
    if not vid:
        return None

    cached = read_transcript_cache(platform, vid, min_chars=MIN_TEXT_CHARS)
    if not cached and platform == "youtube":
        cached = read_transcript_cache("youtube", vid, min_chars=MIN_TEXT_CHARS)

    if cached:
        return ResolveResult(
            text=cached,
            source_type="local_cache",
            source_url=video.get("url", ""),
            match_type="same_content",
            confidence=1.0,
            risk_level="very_low",
            note="Previously resolved transcript cache.",
        )
    return None


def resolver_youtube_captions(video: dict) -> Optional[ResolveResult]:
    if video.get("platform", "youtube") != "youtube":
        return None

    vid = video.get("video_id")
    if not vid:
        return None

    text = get_youtube_captions_only(vid)
    if text and _text_quality(text):
        return ResolveResult(
            text=text,
            source_type="youtube_public_captions",
            source_url=video.get("url") or f"https://www.youtube.com/watch?v={vid}",
            match_type="same_content",
            confidence=0.98,
            risk_level="low",
            note="YouTube public captions.",
        )
    return None


def resolver_bilibili(video: dict) -> Optional[ResolveResult]:
    if video.get("platform") != "bilibili":
        return None

    vid = video.get("video_id", "")
    bvid = video.get("bvid") or vid.replace("bilibili:", "")
    if not bvid:
        return None

    try:
        text = get_bilibili_transcript(bvid)
        if text and _text_quality(text):
            return ResolveResult(
                text=text,
                source_type="bilibili_subtitle_or_audio",
                source_url=f"https://www.bilibili.com/video/{bvid}",
                match_type="same_content",
                confidence=0.95,
                risk_level="low_or_medium_if_cookie_used",
                note="Bilibili subtitle or audio transcription.",
            )
    except Exception as exc:
        logger.warning("Bilibili resolver failed for %s: %s", bvid, exc)
    return None


def resolver_description_links(video: dict) -> Optional[ResolveResult]:
    urls = _extract_urls(video.get("description", ""))
    if not urls:
        return None

    for url in urls[:10]:
        try:
            raw = _fetch(url)
            text = _strip_html(raw)
            if not _text_quality(text):
                continue

            is_same, confidence, reason = _is_same_content_page(url, text, video)
            if is_same:
                logger.info("Description link resolved same content: %s", url)
                write_transcript_cache(video.get("platform", "web"), video.get("video_id", url), text)
                return ResolveResult(
                    text=text,
                    source_type="description_official_link",
                    source_url=url,
                    match_type="same_content",
                    confidence=confidence,
                    risk_level="low",
                    note=reason,
                )
        except Exception as exc:
            logger.debug("Description link failed: %s — %s", url, exc)

    return None


def _xml_text(elem: ET.Element, name: str) -> str:
    child = elem.find(name)
    if child is not None and child.text:
        return child.text.strip()

    for c in elem:
        if c.tag.split("}")[-1] == name and c.text:
            return c.text.strip()
    return ""


def _item_text(item: ET.Element) -> str:
    parts = []
    for name in ("title", "description", "summary"):
        v = _xml_text(item, name)
        if v:
            parts.append(v)

    for c in item.iter():
        local = c.tag.split("}")[-1]
        if local in ("encoded", "content") and c.text:
            parts.append(c.text)

    return _strip_html("\n\n".join(parts))


def _item_audio_url(item: ET.Element) -> str:
    for c in item.iter():
        local = c.tag.split("}")[-1].lower()
        if local in ("enclosure", "content"):
            url = c.attrib.get("url", "")
            typ = c.attrib.get("type", "")
            if url and ("audio" in typ or url.lower().endswith((".mp3", ".m4a", ".aac", ".wav"))):
                return url
    return ""


def resolver_podcast_rss(video: dict) -> Optional[ResolveResult]:
    if not PODCAST_FEEDS:
        return None

    title = video.get("title", "")
    best_audio = None
    best_audio_score = 0.0
    best_audio_title = ""

    for feed_url in PODCAST_FEEDS:
        try:
            raw = _fetch(feed_url, max_bytes=4_000_000)
            root = ET.fromstring(raw.encode("utf-8"))
            items = root.findall(".//item")

            for item in items[:100]:
                item_title = _xml_text(item, "title")
                score = _similarity(title, item_title)
                if score < 0.55:
                    continue

                text = _item_text(item)
                if _text_quality(text):
                    logger.info("Podcast RSS text matched: %.2f %s", score, item_title)
                    return ResolveResult(
                        text=text,
                        source_type="podcast_rss_text",
                        source_url=feed_url,
                        match_type="same_content",
                        confidence=max(score, 0.78),
                        risk_level="low",
                        note=f"RSS episode title matched: {item_title}",
                    )

                audio_url = _item_audio_url(item)
                if audio_url and score > best_audio_score:
                    best_audio = audio_url
                    best_audio_score = score
                    best_audio_title = item_title

        except Exception as exc:
            logger.warning("Podcast RSS resolver failed for %s: %s", feed_url, exc)

    if best_audio:
        try:
            logger.info("Podcast RSS audio matched; transcribing: %.2f %s", best_audio_score, best_audio_title)
            text = transcribe_url_with_whisper(
                source_id=f"podcast_{_norm(best_audio_title)[:80]}",
                url=best_audio,
                platform="podcast",
                use_youtube_cookies=False,
            )
            if _text_quality(text):
                return ResolveResult(
                    text=text,
                    source_type="podcast_rss_audio_whisper",
                    source_url=best_audio,
                    match_type="same_content",
                    confidence=max(best_audio_score, 0.75),
                    risk_level="low",
                    note=f"Public podcast audio transcribed: {best_audio_title}",
                )
        except Exception as exc:
            logger.warning("Podcast audio transcription failed: %s", exc)

    return None


def _decode_duckduckgo_url(url: str) -> str:
    if "duckduckgo.com/l/" in url or url.startswith("//duckduckgo.com/l/"):
        parsed = urllib.parse.urlparse(url if url.startswith("http") else "https:" + url)
        qs = urllib.parse.parse_qs(parsed.query)
        if qs.get("uddg"):
            return qs["uddg"][0]
    return html.unescape(url)


def _duckduckgo_search(query: str, max_results: int = 5) -> list[str]:
    url = "https://duckduckgo.com/html/?" + urllib.parse.urlencode({"q": query})
    raw = _fetch(url, timeout=25, max_bytes=1_500_000)

    links = re.findall(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"', raw)
    results = []
    for link in links:
        link = _decode_duckduckgo_url(link)
        if link.startswith("http") and link not in results:
            results.append(link)
        if len(results) >= max_results:
            break
    return results


def resolver_web_exact_search(video: dict) -> Optional[ResolveResult]:
    if not WEB_SEARCH_ENABLED:
        return None

    title = video.get("title", "")
    channel = video.get("channel", "")
    if not title:
        return None

    queries = [
        f'"{title}" transcript',
        f'"{title}" "show notes"',
        f'"{title}" podcast',
        f'"{title}" official',
        f'"{channel}" "{title}"',
    ]

    for query in queries:
        try:
            links = _duckduckgo_search(query, MAX_SEARCH_RESULTS)
        except Exception as exc:
            logger.warning("Web search failed for query %r: %s", query, exc)
            continue

        for url in links:
            try:
                raw = _fetch(url)
                text = _strip_html(raw)
                if not _text_quality(text):
                    continue

                is_same, confidence, reason = _is_same_content_page(url, text, video)
                if is_same:
                    logger.info("Web exact search resolved same content: %s", url)
                    return ResolveResult(
                        text=text,
                        source_type="web_exact_search",
                        source_url=url,
                        match_type="same_content",
                        confidence=confidence,
                        risk_level="low",
                        note=reason,
                    )
            except Exception as exc:
                logger.debug("Web result failed: %s — %s", url, exc)

    return None


def resolver_gdelt_news_supplement(video: dict) -> Optional[ResolveResult]:
    """Find same-topic public news/articles.

    This is not a transcript. It is only used if supplement-only mode is enabled.
    """
    if not NEWS_SEARCH_ENABLED:
        return None

    title = video.get("title", "")
    if not title:
        return None

    query = re.sub(r"[^\w\s\u4e00-\u9fff-]+", " ", title)
    query = " ".join(query.split()[:12])
    if not query:
        return None

    api = (
        "https://api.gdeltproject.org/api/v2/doc/doc?"
        + urllib.parse.urlencode({
            "query": query,
            "mode": "ArtList",
            "format": "json",
            "maxrecords": "5",
            "sort": "HybridRel",
        })
    )

    try:
        raw = _fetch(api, timeout=25, max_bytes=1_500_000)
        data = json.loads(raw)
        articles = data.get("articles", [])
    except Exception as exc:
        logger.warning("GDELT resolver failed: %s", exc)
        return None

    for article in articles:
        url = article.get("url")
        article_title = article.get("title", "")
        if not url:
            continue

        # Require some topic match.
        confidence = _similarity(title, article_title)
        if confidence < 0.25:
            continue

        try:
            raw_page = _fetch(url)
            text = _strip_html(raw_page)
            if _text_quality(text):
                logger.info("GDELT same-topic supplement found: %s", url)
                return ResolveResult(
                    text=text,
                    source_type="gdelt_news_article",
                    source_url=url,
                    match_type="same_topic",
                    confidence=max(confidence, 0.55),
                    risk_level="low",
                    note="Same-topic public news/article, not original video transcript.",
                )
        except Exception:
            continue

    return None



def _load_verified_creators() -> list[dict]:
    if not VERIFIED_CREATOR_SOURCES_ENABLED:
        return []

    if not CREATOR_SOURCES_FILE.exists():
        logger.warning("Creator source file not found: %s", CREATOR_SOURCES_FILE)
        return []

    try:
        data = yaml.safe_load(CREATOR_SOURCES_FILE.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        logger.warning("Failed to load creator source file %s: %s", CREATOR_SOURCES_FILE, exc)
        return []

    creators = []
    for creator in data.get("creators", []):
        if creator.get("enabled") is not True:
            continue

        # Only use manually reviewed entries.
        status = creator.get("review_status", "verified")
        if status not in ("verified", "needs_user_check"):
            continue

        creators.append(creator)

    return creators


def _creator_matches_video(creator: dict, video: dict) -> bool:
    channel = (
        video.get("channel")
        or video.get("channel_title")
        or video.get("channelTitle")
        or ""
    )
    if not channel:
        return False

    ch = _norm(channel)
    names = [creator.get("canonical_name", "")]
    names.extend(creator.get("youtube_match_names", []) or [])

    for name in names:
        n = _norm(name)
        if not n:
            continue

        if ch == n or ch in n or n in ch:
            return True

    return False


def _extract_anchor_links(raw_html: str, base_url: str) -> list[tuple[str, str]]:
    links: list[tuple[str, str]] = []

    for match in re.finditer(
        r'(?is)<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
        raw_html or "",
    ):
        href = html.unescape(match.group(1).strip())
        anchor_text = _strip_html(match.group(2)).strip()

        if not href:
            continue

        url = urllib.parse.urljoin(base_url, href)

        if not url.startswith(("http://", "https://")):
            continue

        if url not in [u for u, _ in links]:
            links.append((url, anchor_text))

    return links


def _source_is_safe_same_content(source: dict) -> bool:
    if source.get("relation") != "same_content":
        return False

    if source.get("risk_level", "medium") != "low":
        return False

    if not source.get("url"):
        return False

    # Bilibili cross-platform search is not implemented in this safe first version.
    # Native Bilibili videos are still handled by resolver_bilibili().
    if source.get("type") == "bilibili_space":
        return False

    return True


def _candidate_score_for_verified_source(video: dict, url: str, anchor_text: str) -> float:
    title = video.get("title", "")
    return max(
        _similarity(title, anchor_text),
        _similarity(title, url),
    )


def resolver_verified_creator_sources(video: dict) -> Optional[ResolveResult]:
    """Use only manually verified official creator sources.

    This resolver is intentionally conservative:
    - only runs after YouTube captions fail
    - only uses enabled creators from creator_sources.enabled.yaml
    - only accepts relation=same_content and risk_level=low
    - skips same-topic/news content entirely
    """

    if video.get("platform", "youtube") != "youtube":
        return None

    title = video.get("title", "")
    if not title:
        return None

    matched_creators = [
        creator
        for creator in _load_verified_creators()
        if _creator_matches_video(creator, video)
    ]

    if not matched_creators:
        return None

    for creator in matched_creators:
        creator_name = creator.get("canonical_name", "unknown_creator")

        for source in creator.get("sources", []) or []:
            if not _source_is_safe_same_content(source):
                continue

            source_url = source.get("url", "")
            try:
                raw = _fetch(source_url, max_bytes=4_000_000)
                index_text = _strip_html(raw)
            except Exception as exc:
                logger.debug("Verified source index fetch failed: %s — %s", source_url, exc)
                continue

            # Some source URLs are already an episode/transcript page.
            if _text_quality(index_text):
                is_same, confidence, reason = _is_same_content_page(source_url, index_text, video)
                if is_same:
                    logger.info("Verified source direct match: %s", source_url)
                    write_transcript_cache(video.get("platform", "youtube"), video.get("video_id", source_url), index_text)
                    return ResolveResult(
                        text=index_text,
                        source_type="verified_creator_source",
                        source_url=source_url,
                        match_type="same_content",
                        confidence=max(confidence, 0.78),
                        risk_level="low",
                        note=f"Verified creator source direct match: {creator_name}; {reason}",
                    )

            # Otherwise, treat it as an index/archive page and fetch likely episode links.
            links = _extract_anchor_links(raw, source_url)
            scored_links = []
            for url, anchor_text in links:
                score = _candidate_score_for_verified_source(video, url, anchor_text)
                url_l = url.lower()
                hint = any(
                    h in url_l or h in _norm(anchor_text)
                    for h in ("transcript", "podcast", "episode", "show-notes", "show_notes")
                )

                if score >= VERIFIED_MIN_TITLE_SIMILARITY or (hint and score >= 0.35):
                    scored_links.append((score, url, anchor_text))

            scored_links.sort(reverse=True, key=lambda x: x[0])

            for score, candidate_url, anchor_text in scored_links[:VERIFIED_MAX_LINKS_PER_SOURCE]:
                try:
                    raw_page = _fetch(candidate_url, max_bytes=4_000_000)
                    text = _strip_html(raw_page)
                    if not _text_quality(text):
                        continue

                    is_same, confidence, reason = _is_same_content_page(candidate_url, text, video)
                    if not is_same:
                        continue

                    logger.info(
                        "Verified creator source matched: creator=%s score=%.2f url=%s",
                        creator_name,
                        score,
                        candidate_url,
                    )
                    write_transcript_cache(video.get("platform", "youtube"), video.get("video_id", candidate_url), text)
                    return ResolveResult(
                        text=text,
                        source_type="verified_creator_source",
                        source_url=candidate_url,
                        match_type="same_content",
                        confidence=max(confidence, score, 0.78),
                        risk_level="low",
                        note=f"Verified creator source matched: {creator_name}; {reason}",
                    )

                except Exception as exc:
                    logger.debug("Verified candidate failed: %s — %s", candidate_url, exc)

    return None

def resolver_youtube_hard_fallback(video: dict) -> Optional[ResolveResult]:
    if not YOUTUBE_HARD_FALLBACK_ENABLED:
        logger.info("YouTube hard fallback disabled by YOUTUBE_HARD_FALLBACK_ENABLED=false")
        return None

    if video.get("platform", "youtube") != "youtube":
        return None

    vid = video.get("video_id")
    if not vid:
        return None

    try:
        text = transcribe_youtube_hard_fallback(vid)
        if _text_quality(text):
            return ResolveResult(
                text=text,
                source_type="youtube_cookie_ytdlp_whisper",
                source_url=video.get("url") or f"https://www.youtube.com/watch?v={vid}",
                match_type="same_content",
                confidence=0.9,
                risk_level="medium_high",
                note="Last-resort YouTube cookie/proxy + yt-dlp + Whisper.",
            )
    except Exception as exc:
        logger.warning("YouTube hard fallback failed for %s: %s", vid, exc)

    return None


def resolve_video_text(video: dict) -> dict:
    """Resolve text for one video.

    First Success Wins:
    - same_content success: return immediately
    - same_topic supplement: store it, continue looking for same_content
    - if no same_content found, supplement may be returned only when enabled
    """
    title = video.get("title", "")
    platform = video.get("platform", "youtube")
    logger.info("Resolving text: platform=%s title=%s", platform, title[:100])

    supplements: list[ResolveResult] = []

    resolvers = [
        resolver_cache,
        resolver_youtube_captions,
        resolver_bilibili,
        resolver_verified_creator_sources,
        resolver_description_links,
        resolver_podcast_rss,
        resolver_youtube_hard_fallback,
    ]

    for resolver in resolvers:
        try:
            result = resolver(video)
        except Exception as exc:
            logger.warning("Resolver %s failed: %s", resolver.__name__, exc)
            continue

        if not result or not result.ok():
            continue

        if result.match_type == "same_content":
            logger.info(
                "Resolved same-content text via %s (%d chars, confidence=%.2f)",
                result.source_type,
                len(result.text),
                result.confidence,
            )
            return result.to_dict()

        if result.match_type == "same_topic":
            logger.info(
                "Collected same-topic supplement via %s (%d chars, confidence=%.2f)",
                result.source_type,
                len(result.text),
                result.confidence,
            )
            supplements.append(result)
            continue

    if ALLOW_SUPPLEMENT_ONLY and supplements:
        best = sorted(supplements, key=lambda x: x.confidence, reverse=True)[0]
        logger.info("Returning supplement-only content via %s", best.source_type)
        return best.to_dict()

    raise RuntimeError("No usable text resolved from any source.")
