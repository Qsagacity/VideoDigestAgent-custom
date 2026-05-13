# Video Digest Agent Custom

这是一个基于 VideoDigestAgent--Feishu 二次定制的视频内容整理 Agent，用于自动追踪 YouTube 博主的新视频，提取视频字幕，并通过大模型生成结构化邮件内容。

原项目已经具备“视频监控、字幕提取、LLM 总结、邮件发送”的基础链路，但在实际使用中，我发现它更偏向“短摘要生成”。对于长视频、中文视频、播客类内容和深度科技商业内容，原项目存在几个明显缺口：

1. **总结深度不足**：原模板更偏向快速摘要，例如产品概述、优缺点、TL;DR，难以还原视频的完整论证过程。
2. **中文视频适配不足**：部分中文频道只有中文字幕，原逻辑优先查找英文字幕，容易失败或退回到 Whisper 转录，导致速度慢、资源消耗高。
3. **原文复用能力弱**：原始 transcript 没有被系统化保存，不方便后续排查、复用或沉淀到知识库。
4. **邮件内容可读性不足**：原项目会把原始字幕直接附在邮件后面，信息量大但结构混乱，用户仍然需要二次整理。
5. **云端运行稳定性不足**：在腾讯云等云服务器上访问 YouTube 时，容易因为云厂商 IP 被限制而导致字幕提取失败。

因此，我对项目进行了定制化改造，使它更适合 AI 产品经理、科技商业研究、长视频内容沉淀等使用场景。

## 我的定制优化

### 1. 从“短摘要”升级为“转录稿整理 + 重点总结”

原项目会根据视频类型套用不同摘要模板，例如科技评测、教育内容、新闻、播客等。但这些模板更适合快速浏览，不适合深度内容沉淀。

我将总结逻辑改为：

提取视频字幕
→ 整理转录文本
→ 添加小标题
→ 加粗重点观点、关键事件、关键判断和重要数据
→ 在文末生成重点内容总结

新的输出更接近一份可直接阅读的结构化文字稿，而不是简单摘要。

### 2. 提升中文视频处理效率

针对硅谷101、小Lin说、中文科技访谈等中文频道，我将字幕提取逻辑改为优先读取中文字幕：

- zh
- zh-Hans
- zh-CN
- zh-TW
- zh-Hant
- en

这样可以减少不必要的 Whisper 本地转录。对于长视频来说，直接读取 YouTube 字幕比下载音频再转录更快、更稳定，也更节省服务器资源。

### 3. 自动保存 transcript，提升可排查和复用能力

项目现在会在每次处理视频后自动保存原始 transcript：

transcripts/youtube_<video_id>.txt

这带来三个好处：

- 总结质量不理想时，可以回看原始 transcript 排查问题
- 可以后续换模型重新总结
- 可以作为知识库、RAG 或内容归档的数据来源

这让项目从“只发一次邮件的工具”升级为可以持续沉淀内容资产的工作流。

### 4. 邮件不再直接附加混乱原始字幕

原项目会在邮件末尾附上 Original Transcript，但原始字幕通常没有段落、没有标题、断句混乱，可读性较差。

我改造后，邮件正文直接发送大模型整理后的内容：

整理后的原文内容
+ 小标题
+ 重点加粗
+ 重点内容总结

这样用户收到邮件后可以直接阅读，不需要再手动复制到大模型中二次整理。

### 5. 增强云服务器部署稳定性

在腾讯云 Ubuntu 服务器上运行时，YouTube 可能因为云厂商 IP 被限制而无法稳定提取字幕。

我增加了代理环境支持，使项目可以通过代理访问 YouTube，提高云端长期运行的稳定性。

同时通过 .gitignore 避免上传敏感和运行文件，例如：

- .env
- API Key
- 邮箱授权码
- 代理账号密码
- transcript 文件
- 虚拟环境和缓存文件

## 改造后的工作流

定时监控 YouTube 频道
→ 发现新视频
→ 提取视频字幕 / transcript
→ 保存原始 transcript 到本地
→ 调用大模型整理转录稿
→ 添加小标题、重点加粗、关键事件总结
→ 通过 QQ 邮箱发送整理后的内容

## 改造价值

这次定制不是简单改 prompt，而是围绕实际使用中的内容消费效率做优化。

原项目更像一个“视频摘要工具”，而定制后更接近一个“视频内容整理 Agent”。它弥补了原项目在中文视频、长视频、深度内容沉淀、邮件可读性和云端稳定性上的不足。

主要提升了三类效率：

1. **信息获取效率**  
   不需要每天手动打开 YouTube 检查博主更新，系统会自动追踪并处理新视频。

2. **内容理解效率**  
   邮件不再只是短摘要，而是整理后的结构化文字稿，可以快速理解视频主线、关键事件和核心判断。

3. **知识沉淀效率**  
   transcript 被自动保存，整理后的内容也通过邮件沉淀，后续可以继续用于复盘、面试准备、行业研究或知识库建设。

## 适用场景

- AI 产品经理日常追踪 AI 工具、模型和 Agent 趋势
- 科技商业类 YouTube 视频自动整理
- 中文长视频 / 播客内容沉淀
- 海外 AI 博主内容追踪
- 自动化邮件简报工作流

## Setup

### 1. Get API Keys

#### YouTube Data API v3 Key (required for YouTube sources)
1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a new project (or select an existing one)
3. Go to **APIs & Services > Library**
4. Search for **"YouTube Data API v3"** and click **Enable**
5. Go to **APIs & Services > Credentials**
6. Click **Create Credentials > API Key**
7. Copy the key — this is your `YOUTUBE_API_KEY`
8. (Recommended) Click **Restrict Key** and limit it to YouTube Data API v3 only

#### LLM API Key (choose one)

| Provider | Cost | How to get the key |
|----------|------|--------------------|
| **Gemini** (recommended) | Free tier | Go to [Google AI Studio](https://aistudio.google.com/apikey) → Create API Key |
| **OpenAI** | ~$0.02-0.10/video | Go to [OpenAI Platform](https://platform.openai.com/api-keys) → Create new secret key |
| **Anthropic (Claude)** | ~$0.02-0.06/video | Go to [Anthropic Console](https://console.anthropic.com/) → API Keys → Create Key |

#### Gmail App Password (only needed if `OUTPUT_MODE` is `email` or `both`)
1. Go to [Google Account Security](https://myaccount.google.com/security)
2. Enable **2-Step Verification** if not already on
3. Go to [App Passwords](https://myaccount.google.com/apppasswords)
4. Select **Mail** and your device, then click **Generate**
5. Copy the 16-character password — this is your `SENDER_PASSWORD`

> **Note:** If you don't use Gmail, update `SMTP_SERVER` and `SMTP_PORT` for your provider (e.g., Outlook: `smtp.office365.com:587`).
>
> **Tip:** If you just want to save summaries locally without email, set `OUTPUT_MODE=local` and skip this step entirely.

### 2. Install Dependencies

```bash
# Required for Whisper audio fallback (Mac)
brew install ffmpeg

pip3 install -r requirements.txt
```

> **Note:** `ffmpeg` is only needed if a video has no captions and Whisper kicks in. The app tries YouTube captions first (instant), and only downloads + transcribes audio as a fallback.

### 3. Configure Environment

```bash
cp .env.example .env
```

Edit `.env` with your actual values:

```env
# YouTube channels to monitor (comma-separated, without @)
YOUTUBE_CHANNELS=RhinoFinance,MeetKevin

YOUTUBE_API_KEY=AIza...

# Pick your LLM: gemini, openai, or anthropic
LLM_PROVIDER=gemini
GEMINI_API_KEY=AIza...

# Summary languages (up to 2)
SUMMARY_LANGUAGES=English,Chinese

# Verify accuracy with a second LLM pass (optional)
VERIFY_SUMMARY=false

# Output: email, local, or both
OUTPUT_MODE=email

# Email (only needed when OUTPUT_MODE is email or both)
SENDER_EMAIL=you@gmail.com
SENDER_PASSWORD=abcd efgh ijkl mnop
RECIPIENT_EMAILS=you@gmail.com
```

## Usage

### Web UI

```bash
python3 app.py
```

> **macOS — port 5000 already in use?** macOS Monterey and later runs AirPlay Receiver on port 5000 by default. Either disable it (**System Settings → General → AirDrop & Handoff → AirPlay Receiver → off**) or use a different port:
> ```bash
> python3 app.py --port 8080
> ```

Opens a dashboard at `http://127.0.0.1:5000` with:
- **Dashboard** — live stats, recent history, config overview
- **Run** — trigger once, poll, test a specific video, retry failed, or validate config
- **Config** — edit all settings through a form (no manual `.env` editing required)
- **Archive** — browse and read saved summary files

```bash
python3 app.py --port 8080 --host 0.0.0.0   # custom port / expose to network
```

### CLI

#### Check once for new videos
```bash
python3 main.py
```

#### Run continuously (checks every hour)
```bash
python3 main.py --poll
```

#### Test with a specific video
```bash
# YouTube video
python3 main.py --video dQw4w9WgXcQ

# Bilibili video (prefix BV IDs are detected automatically)
python3 main.py --video BV1xx411c7XZ
```

#### Dry run (no email sent — prints summary to stdout)
```bash
python3 main.py --video dQw4w9WgXcQ --dry-run
```

#### Validate your configuration
```bash
python3 main.py --check
```

#### Show processing history
```bash
python3 main.py --history
```

#### Retry previously failed videos
```bash
python3 main.py --retry
```

## Video Sources

### YouTube Channels

Set `YOUTUBE_CHANNELS` to a comma-separated list of channel handles (without `@`). The agent resolves handles to channel IDs and caches them locally to minimise API usage.

### YouTube Keyword Search

Set `YOUTUBE_SEARCH_QUERIES` to discover new videos beyond your subscribed channels:

```env
YOUTUBE_SEARCH_QUERIES=AI news,machine learning,LLM
YOUTUBE_SEARCH_MAX_RESULTS=5          # results per query
YOUTUBE_SEARCH_INTERVAL=14400         # search every 4 hours (seconds)
YOUTUBE_SEARCH_QUOTA_BUDGET=5000      # max API units/day for search
YOUTUBE_SEARCH_RELEVANCE_KEYWORDS=AI,LLM,GPT   # title pre-filter
YOUTUBE_SEARCH_MIN_DURATION=10        # skip clips shorter than N minutes
YOUTUBE_SEARCH_MAX_TOTAL=15           # cap total videos per search cycle
YOUTUBE_SEARCH_MIN_VIEWS=1000         # skip low-traffic videos (0 = off)
```

Keyword search uses the YouTube Search API (100 units/call). The daily free tier is 10,000 units total across all API calls.

### Bilibili

Monitor Bilibili user spaces for new uploads. Requires browser cookies for subtitle/transcript access:

```env
BILIBILI_ENABLED=true
BILIBILI_USERS=12345,67890            # numeric UIDs from profile URLs
BILIBILI_SESSDATA=...                 # get from browser DevTools
BILIBILI_BILI_JCT=...
BILIBILI_BUVID3=...
```

To get the cookies: open [bilibili.com](https://www.bilibili.com), log in, open DevTools (F12) → Application → Cookies, and copy the values for `SESSDATA`, `bili_jct`, and `buvid3`.

Bilibili support requires the optional `bilibili-api-python` and `httpx` packages (included in `requirements.txt`).

## Configuration Reference

### Core

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `YOUTUBE_CHANNELS` | One source required | — | Comma-separated channel handles (without @) |
| `YOUTUBE_API_KEY` | If YouTube enabled | — | YouTube Data API v3 key |
| `LLM_PROVIDER` | No | `gemini` | LLM to use: `gemini`, `openai`, or `anthropic` |
| `GEMINI_API_KEY` | If gemini | — | Google Gemini API key |
| `GEMINI_MODEL` | No | `gemini-3.1-pro-preview` | Gemini model to use |
| `GEMINI_FALLBACK_MODELS` | No | see `.env.example` | Fallback model chain when primary hits quota |
| `OPENAI_API_KEY` | If openai | — | OpenAI API key |
| `OPENAI_MODEL` | No | `gpt-4o-mini` | OpenAI model to use |
| `ANTHROPIC_API_KEY` | If anthropic | — | Anthropic API key |
| `ANTHROPIC_MODEL` | No | `claude-sonnet-4-5-20250929` | Claude model to use |
| `SUMMARY_LANGUAGES` | No | `English` | Up to 2 languages, comma-separated |
| `VERIFY_SUMMARY` | No | `false` | Enable accuracy verification pass |
| `OUTPUT_MODE` | No | `email` | `email`, `local` (save file only), or `both` |
| `SMTP_SERVER` | No | `smtp.gmail.com` | SMTP server |
| `SMTP_PORT` | No | `587` | SMTP port |
| `SENDER_EMAIL` | If email/both | — | Email to send from |
| `SENDER_PASSWORD` | If email/both | — | SMTP password / app password |
| `RECIPIENT_EMAILS` | If email/both | — | Email(s) to send summaries to (comma-separated) |
| `POLL_INTERVAL` | No | `3600` | Seconds between checks |

### YouTube Search

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `YOUTUBE_SEARCH_QUERIES` | No | — | Comma-separated search terms; leave empty to disable |
| `YOUTUBE_SEARCH_MAX_RESULTS` | No | `5` | Results per search query (1–50) |
| `YOUTUBE_SEARCH_INTERVAL` | No | `14400` | Seconds between search runs |
| `YOUTUBE_SEARCH_QUOTA_BUDGET` | No | `5000` | Max YouTube API units for search per day |
| `YOUTUBE_SEARCH_RELEVANCE_KEYWORDS` | No | see `.env.example` | Title pre-filter keywords |
| `YOUTUBE_SEARCH_MIN_DURATION` | No | `10` | Skip videos shorter than N minutes |
| `YOUTUBE_SEARCH_MAX_TOTAL` | No | `15` | Max total search results processed per cycle |
| `YOUTUBE_SEARCH_MIN_VIEWS` | No | `1000` | Skip videos with fewer views (0 = off) |

### Bilibili

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `BILIBILI_ENABLED` | No | `false` | Enable Bilibili monitoring |
| `BILIBILI_USERS` | If enabled | — | Comma-separated numeric UIDs |
| `BILIBILI_SESSDATA` | If enabled | — | Browser cookie for auth |
| `BILIBILI_BILI_JCT` | If enabled | — | CSRF token cookie |
| `BILIBILI_BUVID3` | If enabled | — | Device identifier cookie |

## Run as a Background Service (Optional)

### Using cron (simplest)

```bash
crontab -e
```

Add this line to check every hour:
```
0 * * * * cd /path/to/VideoDigestAgent && /usr/bin/python3 main.py >> /tmp/video-digest.log 2>&1
```

### Using systemd (Linux)

Create `/etc/systemd/system/video-digest.service`:

```ini
[Unit]
Description=Video Digest Agent
After=network.target

[Service]
Type=simple
WorkingDirectory=/path/to/VideoDigestAgent
ExecStart=/usr/bin/python3 main.py --poll
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
```

Then:
```bash
sudo systemctl enable video-digest
sudo systemctl start video-digest
```

## Architecture

```
app.py                   — Flask web UI (dashboard, config editor, archive viewer)
main.py                  — CLI orchestrator: argument parsing + polling loop
youtube_monitor.py       — Detects new uploads via YouTube Data API; keyword search
bilibili_monitor.py      — Detects new uploads from Bilibili user spaces
transcript_extractor.py  — Extracts captions (YouTube API → Whisper fallback; Bilibili subtitles)
summarizer.py            — Agent pipeline: classify → prompt → summarize → verify
emailer.py               — Formats and sends summary email via SMTP
history.py               — Tracks processed videos + saves summaries locally
config.py                — Loads and validates all settings from .env
```

## Cost Estimate

Each video goes through 2–3 LLM calls (classify + summarize, optionally + verify):

| | Gemini | OpenAI | Anthropic |
|---|---|---|---|
| Without verification | Free | ~$0.02/video | ~$0.02/video |
| With verification | Free | ~$0.04/video | ~$0.04/video |
| Per extra language | Free | ~$0.02/video | ~$0.02/video |

- **YouTube Data API**: Free tier (10,000 units/day; channel polling ~4 units/channel/check; search ~100 units/call)
- **Email**: Free via Gmail SMTP (or skip email entirely with `OUTPUT_MODE=local`)
