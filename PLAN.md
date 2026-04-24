# 社媒监控系统重构方案

## 一、背景与目标

现有 `monitor_weibo` 和 `monitor_wechat` 两个独立模块，分别用 SQLite 和 JSON 存数据，各自有独立的配置和调度。需要：

1. 新增**脉脉**、**小红书**监控
2. **统一数据库**，所有平台数据入同一张表
3. **Flask Web 前端**（后端 Linux，浏览器访问，支持 Linux/Windows 客户端）
4. **飞书群消息推送**
5. **舆情分析**（SnowNLP 实时 + LLM 深度）
6. Cookie **扫码登录后自动获取**，无需手动粘贴
7. 前端可配置**爬取频率**和**关键词**
8. 先搭框架，脉脉/小红书 Cookie 到位后再接入

---

## 二、项目结构

```
monitor/
├── config.yaml                    # 集中配置
├── requirements.txt
├── main.py                        # 启动入口：Web UI + 调度线程
├── db/
│   ├── __init__.py
│   ├── schema.py                  # 建表、连接管理
│   └── migrate.py                 # 从旧数据迁移
├── core/
│   ├── __init__.py
│   ├── base_monitor.py            # 抽象基类
│   ├── scheduler.py               # 统一调度器（heapq + thread）
│   ├── config_loader.py           # YAML 配置加载
│   └── rate_limiter.py            # 共享限速逻辑
├── platforms/
│   ├── __init__.py
│   ├── weibo/
│   │   ├── monitor.py             # 改造自 monitor_weibo/weibo_monitor.py
│   │   └── login.py
│   ├── wechat/
│   │   ├── monitor.py             # 改造自 monitor_wechat/wechat_monitor.py
│   │   ├── weread_client.py       # 改造自 monitor_wechat/weread_client.py
│   │   └── merge.py
│   ├── maimai/
│   │   ├── monitor.py             # 新建，Cookie 到位后实现
│   │   └── login.py
│   └── xiaohongshu/
│       ├── monitor.py             # 新建，Cookie 到位后实现
│       ├── login.py
│       └── x_s_sign.py            # x-s 签名生成
├── analysis/
│   ├── __init__.py
│   ├── sentiment.py               # SnowNLP + jieba 实时分析
│   ├── llm_analyzer.py            # LLM 深度分析（可选）
│   └── custom_dict.txt            # 领域词典（迪子、比亚迪等）
├── notifiers/
│   ├── __init__.py
│   └── feishu.py                  # 飞书 Webhook 推送
├── web/
│   ├── __init__.py
│   ├── app.py                     # Flask 应用
│   ├── api/
│   │   ├── dashboard.py           # 首页状态
│   │   ├── auth.py                # Cookie/登录管理
│   │   ├── data.py                # 数据查询
│   │   ├── analysis.py            # 舆情分析结果
│   │   └── config_api.py          # 调度配置
│   ├── templates/                 # Jinja2 + Bootstrap 5
│   └── static/
└── legacy/                        # 保留原模块，只读参考
    ├── monitor_weibo/
    └── monitor_wechat/
```

---

## 三、统一数据库设计（SQLite）

### 3.1 posts 表 — 所有平台的内容统一存储

```sql
CREATE TABLE posts (
    id TEXT PRIMARY KEY,                -- 平台原生 ID（weibo mid / xhs note_id / maimai post_id / wechat url_key）
    platform TEXT NOT NULL,             -- weibo | wechat | maimai | xiaohongshu
    keyword TEXT NOT NULL DEFAULT '',   -- 搜索关键词（如"迪子"）
    user_name TEXT NOT NULL DEFAULT '',
    user_id TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',     -- 微博为空，微信/小红书/脉脉有标题
    content TEXT NOT NULL DEFAULT '',   -- 清洗后的正文
    url TEXT NOT NULL DEFAULT '',       -- 原文链接
    created_at TEXT NOT NULL DEFAULT '', -- 平台发布时间
    fetched_at TEXT NOT NULL DEFAULT '', -- 爬取时间（ISO 8601）
    -- 互动数据
    reposts_count INTEGER DEFAULT 0,
    comments_count INTEGER DEFAULT 0,
    likes_count INTEGER DEFAULT 0,
    shares_count INTEGER DEFAULT 0,
    -- 舆情分析结果
    sentiment TEXT,                      -- positive | negative | neutral
    sentiment_score REAL,               -- 0.0~1.0（0=负面，0.5=中性，1=正面）
    keywords TEXT,                       -- JSON 数组 ["迪子","加班","薪资"]
    llm_analysis TEXT,                   -- LLM 深度分析结果 JSON
    -- 平台扩展字段
    extra TEXT,                          -- JSON（平台特有数据，见下方说明）
    -- 推送追踪
    pushed_to_feishu INTEGER DEFAULT 0
);

CREATE INDEX idx_posts_platform ON posts(platform);
CREATE INDEX idx_posts_keyword ON posts(keyword);
CREATE INDEX idx_posts_sentiment ON posts(sentiment);
CREATE INDEX idx_posts_fetched ON posts(fetched_at);
```

**extra 字段各平台内容：**

| 平台 | extra 示例 |
|------|-----------|
| Weibo | `{"is_original": true, "pic_urls": [], "video_url": ""}` |
| WeChat | `{"digest": "...", "account": "迪窝", "source": "weread", "mp_id": "..."}` |
| Maimai | `{"is_anonymous": true, "company": "比亚迪", "upvotes": 42, "reply_count": 15}` |
| Xiaohongshu | `{"note_type": "normal", "tags": ["标签1"], "image_urls": []}` |

### 3.2 comments 表

```sql
CREATE TABLE comments (
    id TEXT PRIMARY KEY,
    post_id TEXT NOT NULL,
    platform TEXT NOT NULL,
    user_name TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT '',
    fetched_at TEXT NOT NULL DEFAULT '',
    sentiment TEXT,
    sentiment_score REAL,
    keywords TEXT,
    extra TEXT,
    FOREIGN KEY (post_id) REFERENCES posts(id)
);

CREATE INDEX idx_comments_post ON comments(post_id);
```

### 3.3 scheduler_runs 表 — 调度执行记录

```sql
CREATE TABLE scheduler_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL,
    keyword TEXT DEFAULT '',
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT DEFAULT 'running',     -- running | success | error
    posts_found INTEGER DEFAULT 0,
    error_message TEXT
);
```

### 3.4 platform_auth 表 — 平台登录凭据

```sql
CREATE TABLE platform_auth (
    platform TEXT PRIMARY KEY,
    cookies TEXT,                       -- 加密存储
    auth_status TEXT DEFAULT 'inactive', -- active | expired | inactive
    last_validated TEXT,
    extra TEXT
);
```

---

## 四、核心模块设计

### 4.1 抽象基类 — BaseMonitor

所有平台监控器继承此基类，统一接口：

```python
class BaseMonitor(ABC):
    PLATFORM_NAME: str  # 'weibo' | 'wechat' | 'maimai' | 'xiaohongshu'

    @abstractmethod
    def crawl(self, keyword: str, max_pages: int = 3) -> CrawlResult:
        """执行一次爬取，返回标准化结果"""

    @abstractmethod
    def verify_auth(self) -> bool:
        """检查当前 Cookie/凭据是否有效"""

    @abstractmethod
    def get_comments(self, post_id: str, max_count: int = 20) -> list[dict]:
        """获取指定帖子的评论"""

    @abstractmethod
    def get_login_qrcode(self) -> dict:
        """获取登录二维码（返回 {qr_url, uuid}）"""

    @abstractmethod
    def check_login_status(self, uuid: str) -> dict:
        """轮询登录状态"""

    # 共享方法（基类实现）
    # _safe_request()  — 带重试、熔断的安全请求
    # _delay()          — 调用 rate_limiter.wait()
    # save_posts()      — 写入统一 DB
    # save_comments()   — 写入统一 DB
```

### 4.2 扫码登录流程 — Cookie 自动获取

**统一流程：前端展示二维码 → 用户手机扫码 → 后端轮询状态 → 自动保存 Cookie**

```
用户点击"登录" → 前端请求 /api/auth/qrcode/<platform>
                        ↓
              后端调用 platform.get_login_qrcode()
              返回二维码图片 URL + UUID
                        ↓
              前端展示二维码 + 倒计时
                        ↓
              前端每 3 秒轮询 /api/auth/status/<platform>/<uuid>
                        ↓
              后端调用 platform.check_login_status(uuid)
                        ↓
              扫码成功 → 提取 Cookie → 加密存入 platform_auth 表
              → 前端显示"登录成功" → 自动跳转
```

| 平台 | 登录入口 | Cookie 字段 |
|------|----------|-------------|
| Weibo | passport.weibo.com 扫码 | SUB |
| WeChat/WeRead | 已有 weread_client.py 扫码流程 | token + vid |
| Maimai | maimai.cn 扫码（需抓包确认） | session cookie |
| Xiaohongshu | passport.xiaohongshu.com 扫码 | a1 + webId + web_session |

### 4.3 统一调度器 — Scheduler

- 单进程，`heapq` 优先队列，每个平台一个 `ScheduledJob`
- 后台 `threading.Thread` 运行，与 Flask 共存
- 每次执行流程：**verify_auth → crawl → sentiment analyze → push feishu → record run**
- Web UI 可动态修改间隔、关键词、启用/禁用

### 4.4 限速器 — RateLimiter

```python
class RateLimiter:
    def wait(self):
        """请求前等待"""
        # 1. 基础随机延迟（3~8秒，各平台可配）
        # 2. 高斯抖动 ±20%，避免等间距特征
        # 3. 连续失败指数退避
        # 4. 每小时请求上限检查（滑动窗口）

    def record_success(self):
        """记录成功，重置失败计数"""

    def record_failure(self):
        """记录失败，连续 5 次触发熔断"""
```

### 4.5 舆情分析 — 双层架构

**实时层（每次爬取后立即执行）：**
- `jieba` 分词 + TF-IDF 关键词提取
- `SnowNLP` 情感打分（0.0~1.0）
- 结果写入 `posts.sentiment` / `sentiment_score` / `keywords`

**深度层（定时批量执行，可配置开关）：**
- 调用 OpenAI 兼容 API
- 反讽检测、话题分类、趋势判断
- 结果写入 `posts.llm_analysis` JSON

**领域词典** (`analysis/custom_dict.txt`)：
```
迪子 5 n
比亚迪 5 n
加班 3 v
薪资 3 n
离职 3 v
续签 3 v
```

### 4.6 飞书推送 — FeishuNotifier

- Webhook POST，富文本卡片消息
- 消息包含：平台标签（颜色区分）、标题/内容摘要、情感标签、互动数据、原文链接
- 每条间隔 3 秒，遵守 20 条/分钟限制
- `pushed_to_feishu` 字段防重复推送

### 4.7 配置文件 — config.yaml

```yaml
app:
  name: "社媒监控系统"
  host: "0.0.0.0"
  port: 5000
  secret_key: "change-me-in-production"
  db_path: "db/monitor.db"
  log_dir: "logs"

# 全局默认关键词
default_keywords: ["迪子"]

# 飞书推送
feishu:
  enabled: false
  webhook_url: ""
  sign_secret: ""
  push_new_posts: true
  push_new_comments: false
  max_push_per_run: 50

# 舆情分析
sentiment:
  snowNLP: true
  custom_dict: "analysis/custom_dict.txt"
  llm:
    enabled: false
    api_url: ""
    api_key: ""
    model: ""

# 各平台配置
platforms:
  weibo:
    enabled: true
    interval_hours: 6
    keywords: ["迪子"]
    cookies: ""
    max_pages_per_keyword: 3
    max_comments_per_post: 20
    request_delay:
      min: 3.0
      max: 8.0
    max_requests_per_hour: 60

  wechat:
    enabled: true
    interval_hours: 6
    keywords: ["迪子"]
    sogou:
      enabled: true
      max_pages: 5
      request_delay: { min: 3.0, max: 6.0 }
    weread:
      enabled: true
      proxy_url: "https://weread.111965.xyz"
      request_delay: { min: 2.0, max: 4.0 }

  maimai:
    enabled: false
    interval_hours: 8
    keywords: ["迪子"]
    cookies: ""
    request_delay: { min: 15.0, max: 30.0 }
    max_requests_per_hour: 30

  xiaohongshu:
    enabled: false
    interval_hours: 6
    keywords: ["迪子"]
    cookies:
      a1: ""
      webId: ""
      web_session: ""
    request_delay: { min: 5.0, max: 15.0 }
    max_requests_per_hour: 40
```

---

## 五、反封号与安全措施

### 5.1 爬取侧反检测

| 措施 | 说明 | 实现位置 |
|------|------|----------|
| 请求间隔随机化 + 高斯抖动 | 基础随机延迟上加 ±20% 抖动，避免等间距特征 | `core/rate_limiter.py` |
| User-Agent 每次请求轮换 | 现有 weibo 固定 UA，需改为每次请求随机选 | `core/base_monitor.py._safe_request()` |
| Session 定期重建 | 每 N 次请求重建 session，避免长期固定会话特征 | `core/base_monitor.py` |
| Cookie 有效期预检 | 每次爬取前 `verify_auth()`，失效则跳过不强行请求 | `core/scheduler.py` |
| 自动熔断 | 403/验证码/登录重定向 → 立即停止，标记 expired | `core/base_monitor.py._safe_request()` |
| 指数退避 | 连续失败等待 10s → 30s → 90s → 暂停 | `core/base_monitor.py` |
| 每小时请求上限 | `max_requests_per_hour` 配置，滑动窗口控制 | `core/rate_limiter.py` |
| 请求头随机化 | Accept-Language 等微调，模拟真实浏览器 | `core/base_monitor.py._safe_request()` |

### 5.2 数据安全

| 措施 | 说明 |
|------|------|
| Cookie 加密存储 | XOR + 机器特征密钥加密（复用 `weread_client.py` 已有方案），DB 中不存明文 |
| 数据库文件权限 | `db/monitor.db` 设为 600，目录设为 700 |
| 日志脱敏 | 不输出 Cookie 值、用户 UID，仅显示前 4 位 + 掩码 |
| 敏感文件不入 Git | `.gitignore` 排除 `*.db`、`config.yaml`、`*.token`、`logs/` |

### 5.3 Web UI 安全

| 措施 | 说明 |
|------|------|
| 登录认证 | Web UI 自带密码认证（config.yaml 配置） |
| Session 安全 | Flask secret_key + HttpOnly + SameSite |
| CORS 限制 | 可配置仅内网 IP 访问 |
| Cookie 脱敏 | API 返回时仅显示前 8 位 + 掩码 |

---

## 六、Web 前端页面

前端采用 **Flask + Jinja2 + Bootstrap 5 + HTMX**，服务端渲染，浏览器访问。

| 页面 | URL | 功能 |
|------|-----|------|
| 仪表盘 | `/` | 系统状态、各平台认证状态、最近运行记录、快速统计 |
| 登录管理 | `/login` | 每平台扫码登录、Cookie 自动获取、认证状态显示 |
| 调度配置 | `/schedule` | 每平台独立配置：爬取间隔、关键词增删、启用/禁用、限速参数 |
| 数据浏览 | `/data` | 按平台/关键词/情感/时间筛选、搜索、分页浏览 |
| 舆情分析 | `/analysis` | 情感分布图表、关键词词云、趋势线、LLM 分析结果 |

---

## 七、实施计划

### Phase 1: 基础框架（优先）

| 步骤 | 内容 | 产出文件 |
|------|------|----------|
| 1.1 | 项目结构调整，移动旧模块到 `legacy/` | 目录结构 |
| 1.2 | config.yaml + config_loader.py | `config.yaml`, `core/config_loader.py` |
| 1.3 | 统一数据库 schema | `db/schema.py` |
| 1.4 | base_monitor + rate_limiter | `core/base_monitor.py`, `core/rate_limiter.py` |
| 1.5 | 数据迁移脚本 | `db/migrate.py` |
| 1.6 | requirements.txt + .gitignore | `requirements.txt`, `.gitignore` |
| 1.7 | 验证：迁移后数据完整 | - |

### Phase 2: 改造已有平台

| 步骤 | 内容 | 产出文件 |
|------|------|----------|
| 2.1 | Weibo 监控器改造 | `platforms/weibo/monitor.py` |
| 2.2 | WeChat 监控器改造（含 weread + merge） | `platforms/wechat/*` |
| 2.3 | 统一调度器 | `core/scheduler.py` |
| 2.4 | 启动入口 | `main.py` |
| 2.5 | 端到端测试 | - |

### Phase 3: 舆情分析

| 步骤 | 内容 | 产出文件 |
|------|------|----------|
| 3.1 | SnowNLP + jieba 实时分析 | `analysis/sentiment.py` |
| 3.2 | 领域词典 | `analysis/custom_dict.txt` |
| 3.3 | LLM 深度分析 | `analysis/llm_analyzer.py` |
| 3.4 | 集成到调度 + 回填 | - |

### Phase 4: 飞书推送

| 步骤 | 内容 | 产出文件 |
|------|------|----------|
| 4.1 | Webhook 推送 | `notifiers/feishu.py` |
| 4.2 | 集成到调度流程 | - |

### Phase 5: Web 前端

| 步骤 | 内容 | 产出文件 |
|------|------|----------|
| 5.1 | Flask 骨架 + base 模板 | `web/app.py`, `templates/base.html` |
| 5.2 | 仪表盘页 | `web/api/dashboard.py` |
| 5.3 | 登录管理页（扫码 + 自动获取 Cookie） | `web/api/auth.py` |
| 5.4 | 调度配置页（频率、关键词、启禁用） | `web/api/config_api.py` |
| 5.5 | 数据浏览页 | `web/api/data.py` |
| 5.6 | 舆情分析页 | `web/api/analysis.py` |
| 5.7 | main.py 整合 | `main.py` 最终版 |

### Phase 6: 新平台（Cookie 到位后）

| 步骤 | 内容 | 产出文件 |
|------|------|----------|
| 6.1 | 脉脉监控（需先抓包确认 API） | `platforms/maimai/monitor.py`, `login.py` |
| 6.2 | 小红书监控 + x-s 签名 | `platforms/xiaohongshu/monitor.py`, `x_s_sign.py`, `login.py` |

---

## 八、验证方式

1. **Phase 1**: 运行 `db/migrate.py`，检查统一 DB 数据量 = 旧 weibo 35帖253评论 + 旧 wechat 26篇文章
2. **Phase 2**: `main.py --test` 运行一次爬取，验证 Weibo + WeChat 数据入统一 DB
3. **Phase 3**: 查询 posts 表确认 `sentiment` / `sentiment_score` / `keywords` 已填充
4. **Phase 4**: 配置飞书 webhook 后运行爬取，确认群内收到卡片消息
5. **Phase 5**: 浏览器访问 `http://<server>:5000`，测试所有页面功能
6. **Phase 6**: 配置 Cookie 后运行脉脉/小红书爬取，验证数据入库 + 推送

---

## 九、关键复用

| 已有代码 | 复用目标 |
|----------|----------|
| `monitor_weibo/weibo_monitor.py` — session、API、HTML 清洗 | `platforms/weibo/monitor.py` |
| `monitor_weibo/weibo_monitor.py` — `_safe_get()` 重试逻辑 | `core/base_monitor.py` |
| `monitor_weibo/scheduler.py` — 信号处理 + 可中断等待 | `core/scheduler.py` |
| `monitor_wechat/weread_client.py` — 加密存储 + 扫码登录 | `platforms/wechat/weread_client.py` |
| `monitor_wechat/merge.py` — URL 去重 + 标题模糊去重 | `platforms/wechat/merge.py` |
| `monitor_wechat/wechat_monitor.py` — 搜狗爬取逻辑 | `platforms/wechat/monitor.py` |
