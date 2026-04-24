# 社媒监控系统

多平台社交媒体关键词监控系统，支持微博、微信公众号、脉脉、小红书的定时爬取、舆情分析和飞书推送。

## 功能

- **多平台监控** — 微博、微信公众号（搜狗 + 微信读书双源）、脉脉、小红书
- **统一数据库** — 所有平台数据存储在同一 SQLite 数据库中
- **舆情分析** — SnowNLP + jieba 实时情感分析，可选 LLM 深度分析
- **飞书推送** — 新内容自动推送富文本卡片到飞书群
- **Web 管理界面** — 仪表盘、登录管理、调度配置、数据浏览、舆情分析图表
- **扫码登录** — Cookie 扫码后自动获取，无需手动粘贴
- **反封号保护** — 请求随机化、熔断机制、指数退避、每小时请求上限

## 快速开始

### 安装依赖

```bash
pip install -r requirements.txt
```

### 配置

复制并编辑配置文件：

```bash
cp config.example.yaml config.yaml
```

主要配置项：

```yaml
app:
  password: "your-password"     # Web 登录密码

default_keywords: ["迪子"]      # 全局默认关键词

feishu:
  enabled: true
  webhook_url: "https://open.feishu.cn/open-apis/bot/v2/hook/xxx"

platforms:
  weibo:
    enabled: true
    interval_hours: 6
    keywords: ["迪子"]
  # ...
```

### 数据迁移（从旧版本）

```bash
python3 main.py --migrate
```

### 启动

```bash
# 启动 Web UI + 调度器（推荐）
python3 main.py

# 仅启动 Web UI
python3 main.py --web

# 仅启动调度器
python3 main.py --scheduler

# 测试模式：运行一次爬取
python3 main.py --test
```

启动后访问 http://localhost:5000，使用 `config.yaml` 中配置的密码登录。

## 项目结构

```
monitor/
├── main.py                  # 启动入口
├── config.yaml              # 集中配置
├── requirements.txt
├── db/
│   ├── schema.py            # 数据库建表、连接管理
│   └── migrate.py           # 数据迁移
├── core/
│   ├── base_monitor.py      # 平台监控器抽象基类
│   ├── scheduler.py         # 统一调度器
│   ├── config_loader.py     # 配置加载
│   └── rate_limiter.py      # 限速 + 熔断
├── platforms/
│   ├── weibo/               # 微博监控
│   ├── wechat/              # 微信公众号监控（搜狗 + WeRead）
│   ├── maimai/              # 脉脉监控（Cookie 到位后启用）
│   └── xiaohongshu/         # 小红书监控（Cookie 到位后启用）
├── analysis/
│   ├── sentiment.py         # SnowNLP + jieba 实时分析
│   ├── llm_analyzer.py      # LLM 深度分析（可选）
│   └── custom_dict.txt      # 领域词典
├── notifiers/
│   └── feishu.py            # 飞书 Webhook 推送
├── web/
│   ├── app.py               # Flask 应用
│   ├── api/                 # REST API
│   └── templates/           # 前端模板（Bootstrap 5）
└── legacy/                  # 旧版模块（只读参考）
```

## 数据库表

| 表 | 说明 |
|----|------|
| `posts` | 所有平台的内容（微博/微信/脉脉/小红书） |
| `comments` | 评论数据 |
| `scheduler_runs` | 调度执行记录 |
| `platform_auth` | 平台登录凭据（加密存储） |

## 安全措施

| 措施 | 说明 |
|------|------|
| Cookie 加密存储 | XOR + 机器特征密钥，数据库中不存明文 |
| 自动熔断 | 403/验证码/登录重定向时立即停止 |
| 指数退避 | 连续失败等待时间指数增长 |
| 每小时请求上限 | 滑动窗口控制请求频率 |
| 请求头随机化 | User-Agent、Accept-Language 每次请求轮换 |
| Web 登录认证 | 密码保护，Session 安全设置 |

## 添加新平台

1. 在 `platforms/<name>/` 下创建 `monitor.py`，继承 `BaseMonitor`
2. 实现 `crawl()`、`verify_auth()`、`get_comments()` 方法
3. 在 `config.yaml` 的 `platforms` 中添加配置
4. 设置 `enabled: true`

## 依赖

- Python 3.10+
- requests, beautifulsoup4, lxml
- Flask, PyYAML
- jieba, snownlp
- qrcode
