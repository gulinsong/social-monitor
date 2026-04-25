# Social Media Monitor

Multi-platform social media keyword monitoring system. Supports scheduled crawling, sentiment analysis, and Feishu push notifications for Weibo, WeChat, Maimai, and Xiaohongshu.

## Features

- **Multi-platform** — Weibo, WeChat Official Accounts (Sogou + WeRead), Maimai, Xiaohongshu
- **Unified database** — All platform data stored in a single SQLite database
- **Sentiment analysis** — SnowNLP + jieba real-time analysis, optional LLM deep analysis
- **Feishu integration** — Auto-push rich text cards to Feishu groups
- **Web dashboard** — Dashboard, login management, schedule config, data browser, sentiment charts
- **QR login** — Auto-capture cookies via Playwright QR code scanning, no manual paste needed
- **Anti-ban protection** — Request randomization, circuit breaker, exponential backoff, hourly rate limits
- **Colleague circle** — Maimai colleague circle feed crawling with pagination support

## Quick Start

### Install Dependencies

```bash
pip install -r requirements.txt
playwright install chromium
```

### Configuration

Copy and edit the config file:

```bash
cp config.example.yaml config.yaml
```

Key settings:

```yaml
app:
  password: "your-password"       # Web login password

default_keywords: ["your-keyword"] # Global default keywords

feishu:
  enabled: true
  webhook_url: "https://open.feishu.cn/open-apis/bot/v2/hook/xxx"

platforms:
  maimai:
    enabled: true
    interval_hours: 8
    source: colleague_circle      # colleague_circle / search / both
    keywords: []
  weibo:
    enabled: true
    interval_hours: 6
    keywords: ["your-keyword"]
  wechat:
    enabled: true
    interval_hours: 6
    keywords: ["your-keyword"]
  xiaohongshu:
    enabled: true
    interval_hours: 6
    keywords: ["your-keyword"]
```

### Data Migration (from older versions)

```bash
python3 main.py --migrate
```

### Running

```bash
# Start Web UI + Scheduler (recommended)
python3 main.py

# Web UI only
python3 main.py --web

# Scheduler only
python3 main.py --scheduler

# Test mode: run once
python3 main.py --test
```

After starting, visit http://localhost:5000 and login with the password from `config.yaml`.

### Maimai Data Source

Maimai supports two data sources, selectable from the login management page:

| Source | API | Description |
|--------|-----|-------------|
| `colleague_circle` | `/groundhog/gossip/v3/feed` | Company colleague circle feed (large volume, paginated) |
| `search` | `/sdk/search/web_get` | Keyword search (limited to ~2 results per query) |
| `both` | Both APIs | Crawl from both sources |

For `colleague_circle` mode, keywords are optional — leave empty to crawl the full feed.

## Project Structure

```
monitor/
├── main.py                  # Entry point
├── config.yaml              # Centralized config
├── requirements.txt
├── db/
│   ├── schema.py            # DB schema, connection management
│   └── migrate.py           # Data migration
├── core/
│   ├── base_monitor.py      # Abstract base monitor class
│   ├── scheduler.py         # Unified scheduler (heap-based)
│   ├── config_loader.py     # Config loading
│   └── rate_limiter.py      # Rate limiting + circuit breaker
├── platforms/
│   ├── weibo/
│   │   ├── monitor.py       # Weibo crawler (Playwright)
│   │   └── login.py         # Weibo QR login (Playwright)
│   ├── wechat/
│   │   ├── monitor.py       # WeChat monitor (Sogou + WeRead)
│   │   └── weread_client.py # WeRead API client
│   ├── maimai/
│   │   ├── monitor.py       # Maimai crawler (Playwright)
│   │   └── login.py         # Maimai QR login (Playwright)
│   └── xiaohongshu/
│       ├── monitor.py       # XHS crawler (Playwright)
│       └── login.py         # XHS QR login (Playwright)
├── analysis/
│   ├── sentiment.py         # SnowNLP + jieba real-time analysis
│   ├── llm_analyzer.py      # LLM deep analysis (optional)
│   └── custom_dict.txt      # Domain-specific dictionary
├── notifiers/
│   └── feishu.py            # Feishu Webhook push
├── web/
│   ├── app.py               # Flask application
│   ├── api/                 # REST API endpoints
│   └── templates/           # Frontend templates (Bootstrap 5)
└── legacy/                  # Legacy modules (read-only reference)
```

## Database Schema

| Table | Description |
|-------|-------------|
| `posts` | Content from all platforms |
| `comments` | Comment data |
| `scheduler_runs` | Scheduler execution logs |
| `platform_auth` | Platform credentials (encrypted storage) |

## Security

| Measure | Description |
|---------|-------------|
| Cookie encryption | XOR + machine-key based encryption, no plaintext in DB |
| Auto circuit breaker | Stops immediately on 403/CAPTCHA/login redirect |
| Exponential backoff | Wait time grows exponentially on consecutive failures |
| Hourly rate limit | Sliding window request frequency control |
| Header randomization | User-Agent, Accept-Language rotation per request |
| Web auth | Password-protected login with secure session cookies |
| Random scheduler jitter | ±10% interval variation + pre/post task random delays |
| Input validation | Platform allowlist, cookie length limits, SSRF protection |
| XSS prevention | All user data HTML-escaped in templates |

## Adding a New Platform

1. Create `platforms/<name>/monitor.py` extending `BaseMonitor`
2. Implement `crawl()`, `verify_auth()`, `get_comments()` methods
3. Add config entry under `platforms` in `config.yaml`
4. Register platform in `core/scheduler.py` `_load_monitor_class()` map
5. Set `enabled: true`

## Dependencies

- Python 3.10+
- requests, beautifulsoup4, lxml
- Flask, PyYAML
- jieba, snownlp
- qrcode
- playwright (for Weibo, Maimai, XHS crawling and QR login)
