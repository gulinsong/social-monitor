# 社媒监控系统 — 设计文档

## 1. 项目概述

本系统是一个**多平台社交媒体关键词监控系统**，支持微博、微信公众号、脉脉、小红书四个平台的自动化数据采集，提供实时情感分析、飞书推送通知和 Web 管理后台（含二维码登录）。

**核心能力**：
- 4 个平台统一调度采集
- QR 码扫码登录 + Cookie 手动录入
- 实时情感分析（SnowNLP + jieba）
- 可选 LLM 深度分析
- 飞书 Webhook 富文本卡片推送
- Bootstrap 5 + HTMX 响应式 Web 仪表盘
- 配置热更新，无需重启

**技术栈**：Python 3 / Flask / Playwright / SQLite / BeautifulSoup / SnowNLP / jieba

---

## 2. 项目结构

```
monitor/
├── main.py                    # 入口：CLI 参数解析
├── config.yaml                # 生产配置（含凭证）
├── config.example.yaml        # 配置模板
├── requirements.txt           # 依赖
│
├── core/                      # 核心框架
│   ├── base_monitor.py        # 爬虫抽象基类（反检测 + Cookie 加解密）
│   ├── scheduler.py           # 统一调度器（heapq 优先队列 + 热更新）
│   ├── rate_limiter.py        # 限速器 + 熔断器
│   └── config_loader.py       # YAML 配置管理
│
├── db/                        # 数据层
│   ├── schema.py              # SQLite 建表 + 连接管理
│   ├── migrate.py             # 旧模块数据迁移
│   └── monitor.db             # SQLite 数据库
│
├── platforms/                 # 各平台爬虫
│   ├── weibo/                 #   微博（HTTP API）
│   ├── wechat/                #   微信（搜狗 + 微信读书）
│   ├── maimai/                #   脉脉（Playwright）
│   └── xiaohongshu/           #   小红书（Playwright）
│
├── analysis/                  # 情感分析
│   ├── sentiment.py           # SnowNLP + jieba 实时分析
│   ├── llm_analyzer.py        # 可选 LLM 深度分析
│   └── custom_dict.txt        # 领域词典
│
├── notifiers/                 # 通知服务
│   └── feishu.py              # 飞书 Webhook 推送
│
├── web/                       # Flask Web 应用
│   ├── app.py                 #   应用工厂
│   ├── api/                   #   REST API（5 个蓝图）
│   └── templates/             #   Jinja2 模板（6 页面）
│
└── legacy/                    # 旧模块（只读参考）
```

---

## 3. 功能模块详解

### 3.1 平台爬虫

| 平台 | 采集方式 | 数据源 | 关键特性 |
|------|---------|--------|---------|
| **微博** | HTTP API (`m.weibo.cn`) | 关键词搜索 + 评论区 | 移动端接口，验证 Cookie 有效性后采集 |
| **微信** | HTTP + 代理 | 搜狗搜索 + 微信读书 (wewe-rss) | 双源合并去重，支持公众号订阅管理 |
| **脉脉** | Playwright 浏览器自动化 | 同事圈 Feed / 关键词搜索 | API 拦截 + 滚动翻页，支持全量同事圈爬取 |
| **小红书** | Playwright 浏览器自动化 | 关键词搜索 + 笔记评论 | 绕过 `x-s`/`x-t` 签名，API 响应拦截 |

#### 微博

- **搜索接口**：`m.weibo.cn/api/container/getIndex`，按 containerid 搜索
- **评论接口**：`m.weibo.cn/api/comments/show`
- **认证**：Cookie（`SUB` 字段），每次采集前验证 `/api/config`
- **采集范围**：每个关键词最多 3 页，前 5 条帖子各取最多 10 条评论

#### 微信公众号

- **搜狗源**：`weixin.sogou.com/weixin`，BeautifulSoup 解析，按 URL MD5 去重
- **微信读书源**：通过 wewe-rss 代理获取已订阅公众号文章，Bearer Token 认证
- **合并逻辑**：双源结果合并，标记 `source: "sogou"` / `"weread"`，按 URL 哈希去重

#### 脉脉

- **同事圈模式** (`source: "colleague_circle"`)：
  - 入口 API 获取 `webcid` → 打开同事圈页面 → 滚动翻页 → 拦截 `/groundhog/gossip/v3/feed` 响应
  - 数据量大，支持全量爬取或按关键词过滤
- **搜索模式** (`source: "search"`)：
  - 填入搜索框 → 拦截 `/sdk/search/web_get` 响应
  - 每次查询约 2 条结果
- **配置项**：`source` 字段可选 `colleague_circle` / `search` / `both`

#### 小红书

- **搜索**：`xiaohongshu.com/search_result`，拦截搜索 API 响应中的 `data.items`
- **评论**：打开笔记页 → 滚动触发加载 → 拦截 `/api/sns/web/v2/comment/page`
- **使用 Playwright 的原因**：Web API 需要 `x-s` + `x-t` 加密签名，逆向成本高；浏览器自动化天然绕过

### 3.2 统一调度器 (`core/scheduler.py`)

**架构**：`heapq` 优先队列 + 后台守护线程

**单次任务执行流程**：
1. 从堆中弹出 `next_run <= now` 的任务
2. **随机前延迟**（30-180 秒），避免可预测模式
3. **验证认证状态** — Cookie 失效则跳过
4. 逐关键词执行采集 → `monitor.crawl(keyword, max_pages)`
5. 实时情感分析（如已启用）
6. 保存数据到 SQLite
7. 飞书推送（受 `max_push_per_run` 限制）
8. **随机后延迟**（30-300 秒）
9. 重调度：`next_run = now + interval ± 10% 抖动`

**特性**：
- 单线程串行执行（同一时刻只跑一个平台）
- 配置热更新：修改 `config.yaml` 后调度器自动重载
- 优雅关停：SIGINT/SIGTERM 信号处理

### 3.3 情感分析

**实时分析**（SnowNLP + jieba）：
- jieba 分词 + TF-IDF 提取 Top 5 关键词
- SnowNLP 预训练模型输出 0.0-1.0 情感分
- 分类：`≥0.6` 正面 / `≤0.4` 负面 / 其余中性
- 领域词典：比亚迪、BYD、加班、薪资、裁员、内卷、摸鱼等 14 个词条

**LLM 深度分析**（可选，默认关闭）：
- 兼容 OpenAI API 格式
- 输出：情感、分数、反讽检测、主题、摘要、风险等级
- 存入 `posts.llm_analysis` 字段

### 3.4 飞书推送 (`notifiers/feishu.py`)

- **消息格式**：交互式卡片（富文本）
- **内容**：平台标签（颜色区分）、作者、内容摘要（前 500 字）、关键词、情感、互动数据
- **防重**：`pushed_to_feishu` 标记
- **限速**：每条间隔 3 秒，遵守飞书 20 条/分钟限制

### 3.5 Web 管理后台

**6 个页面**：

| 页面 | 功能 |
|------|------|
| 仪表盘 | 系统概览、平台状态、最近运行记录 |
| 登录管理 | QR 码扫码登录、Cookie 手动录入、公众号订阅管理 |
| 调度配置 | 采集间隔、关键词、限速参数（热更新） |
| 数据浏览 | 帖子/评论查询、筛选、导出（JSON/CSV） |
| 情感分析 | 情感分布图、关键词趋势、平台对比 |
| 登录页 | 密码认证 |

**20+ REST API 端点**，覆盖仪表盘统计、认证管理、配置管理、数据查询、分析。

### 3.6 数据库设计

4 张表：

| 表 | 用途 |
|----|------|
| `posts` | 统一帖子存储（所有平台），含情感分析字段 |
| `comments` | 帖子评论 |
| `scheduler_runs` | 调度执行日志 |
| `platform_auth` | 平台认证信息（Cookie 加密存储） |

`posts.extra` 字段以 JSON 存储平台特有数据（微博图片/视频、微信公号信息、脉脉分类/地域、小红书封面等）。

---

## 4. 安全措施

### 4.1 反检测与反封禁

#### 4.1.1 请求层

| 措施 | 实现方式 | 位置 |
|------|---------|------|
| **UA 轮换** | 6 种 User-Agent（Win/Linux/Mac × Chrome/Firefox）随机选取 | `base_monitor.py` |
| **请求头随机化** | Accept-Language 从 4 种变体中随机选取 | `base_monitor.py` |
| **Session 定期重建** | 每 20 次请求关闭并重建 Session，避免长连接指纹 | `base_monitor.py` |
| **高斯抖动** | 基础延迟 ±20% 高斯随机变化 | `rate_limiter.py` |
| **指数退避** | 连续失败时延迟倍增 | `rate_limiter.py` |
| **熔断器** | 连续 5 次失败后停止请求 | `rate_limiter.py` |
| **滑动窗口限速** | 3600 秒窗口，强制 `max_requests_per_hour` | `rate_limiter.py` |
| **随机前后延迟** | 调度器在任务前后各加 30-180s / 30-300s 随机延迟 | `scheduler.py` |
| **间隔抖动** | 下次运行时间 ±10% 随机偏移 | `scheduler.py` |

#### 4.1.2 平台专项

| 平台 | 措施 |
|------|------|
| **微博** | 使用移动端接口 `m.weibo.cn`（监控较宽松）；`X-Requested-With` 模拟 AJAX；采集前验证 Cookie |
| **微信（搜狗）** | 动态 SUID Cookie；设置 Referer；页间 3-6 秒延迟 |
| **脉脉** | Playwright 真实 Chrome 浏览器；随机滚动距离（500-1200px）+ 随机停顿（1.5-3.5s）；每页 4-8 次滚动 |
| **小红书** | Playwright 绕过签名验证；随机滚动（300-900px）+ 随机停顿（1-4s）；页间 5-12 秒间隔 |

#### 4.1.3 封禁检测与响应

**检测触发条件**：
- HTTP 403 响应
- HTML 含登录关键词（"登录" + "密码"）
- API 返回错误状态（如微博 `login: false`）

**响应动作**：
- 立即标记认证为 `expired`
- 记录失败次数（触发熔断器）
- 跳过本次执行，等待下次调度重试

### 4.2 数据安全

| 措施 | 实现 |
|------|------|
| **Cookie 加密** | XOR + 机器密钥（`/etc/machine-id` 或 `$USER:$HOME` 派生） |
| **数据库权限** | DB 文件 `chmod 600`，目录 `chmod 700` |
| **日志脱敏** | 日志中不记录 Cookie 值，UID 仅显示前 4 字符 |
| **敏感文件排除** | `.gitignore` 排除 `config.yaml`、`*.token`、`*.db`、`logs/` |
| **Session 安全** | Flask: `HttpOnly`、`SameSite=Lax`、24 小时有效期 |
| **密码保护** | Web UI 需配置密码才能访问 |
| **SSRF 防护** | 微信公众号 URL 限定 `mp.weixin.qq.com` / `weixin.qq.com` 域名 |
| **XSS 防护** | Jinja2 模板自动 HTML 转义 |
| **输入校验** | Cookie 手动输入限 10000 字符、平台名白名单 |

---

## 5. 关键技术决策

| 决策 | 原因 |
|------|------|
| Playwright 用于小红书和脉脉 | Web API 需加密签名（`x-s`/`x-t`），逆向成本高，浏览器自动化天然绕过 |
| 微信双数据源 | 搜狗覆盖广但不全，微信读书精确但需订阅，双源互补 |
| SQLite | 单用户场景零配置、嵌入式的优势大于并发限制 |
| 机器密钥 Cookie 加密 | 无需外部密钥管理，绑定物理机器 |
| heapq 调度器 | 相比 APScheduler 更细粒度的控制、更简单的热更新 |

---

## 6. 运行方式

```bash
python3 main.py                    # Web + 调度器（默认）
python3 main.py --web              # 仅 Web
python3 main.py --scheduler        # 仅调度器
python3 main.py --test             # 运行一次后退出
python3 main.py --migrate          # 从旧模块迁移数据
```

---

## 7. 默认配置

| 平台 | 采集间隔 | 请求延迟 | 每小时上限 | 默认关键词 |
|------|---------|---------|-----------|-----------|
| 微博 | 6 小时 | 3-8s | 60 | 比亚迪 |
| 微信 | 6 小时 | 搜狗 3-6s / 读书 2-4s | — | 比亚迪 |
| 脉脉 | 8 小时 | 15-30s | 30 | （可选） |
| 小红书 | 6 小时 | 5-15s | 40 | 迪子 |

---

## 8. 扩展指南

### 新增平台

1. 创建 `platforms/<name>/monitor.py`，继承 `BaseMonitor`
2. 实现必要方法：`crawl()`、`verify_auth()`、`get_comments()`
3. 可选实现 QR 登录：`get_login_qrcode()`、`check_login_status()`
4. 在 `config.yaml` 的 `platforms` 下添加配置
5. 在 `scheduler.py` 的 `_load_monitor_class()` 中注册
6. 设置 `enabled: true`

### 新增分析器

实现 `analyze(text) -> dict` 接口，在调度流程中接入。

### 新增通知渠道

在 `notifiers/` 目录下添加新的通知类，在调度器的 `_push_posts()` 中调用。
