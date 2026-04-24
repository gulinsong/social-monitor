import heapq
import logging
import threading
import time
from datetime import datetime

from core.config_loader import load_config, get_platform_config
from db.schema import get_connection

log = logging.getLogger(__name__)


class ScheduledJob:
    def __init__(self, platform_name: str, interval_seconds: int, keywords: list[str], enabled: bool = True):
        self.platform_name = platform_name
        self.interval_seconds = interval_seconds
        self.keywords = keywords
        self.enabled = enabled
        self.next_run = time.time()
        self.monitor = None

    def __lt__(self, other):
        return self.next_run < other.next_run


def _load_monitor_class(platform_name: str):
    module_map = {
        "weibo": "platforms.weibo.monitor",
        "wechat": "platforms.wechat.monitor",
        "maimai": "platforms.maimai.monitor",
        "xiaohongshu": "platforms.xiaohongshu.monitor",
    }
    module_path = module_map.get(platform_name)
    if not module_path:
        log.error("未知平台: %s", platform_name)
        return None

    import importlib
    try:
        module = importlib.import_module(module_path)
        return module.Monitor
    except (ImportError, AttributeError) as e:
        log.error("加载平台模块 %s 失败: %s", platform_name, e)
        return None


class UnifiedScheduler:
    def __init__(self, config: dict = None, db_path: str = None):
        self.config = config or load_config()
        self.db_path = db_path or self.config.get("app", {}).get("db_path", "db/monitor.db")
        self.jobs: list[ScheduledJob] = []
        self._running = False
        self._lock = threading.Lock()
        self._sentiment_analyzer = None
        self._feishu_notifier = None
        self._init_jobs()

    def _init_jobs(self):
        platforms = self.config.get("platforms", {})
        default_kw = self.config.get("default_keywords", [])

        for name, pcfg in platforms.items():
            if not pcfg.get("enabled", False):
                continue
            keywords = pcfg.get("keywords", default_kw)
            interval = pcfg.get("interval_hours", 6) * 3600
            job = ScheduledJob(name, interval, keywords)
            self.jobs.append(job)

        heapq.heapify(self.jobs)
        log.info("调度器初始化: %d 个任务", len(self.jobs))

    def _init_analyzer(self):
        if self._sentiment_analyzer:
            return
        cfg = self.config.get("sentiment", {})
        if cfg.get("snowNLP", True):
            try:
                from analysis.sentiment import SentimentAnalyzer
                self._sentiment_analyzer = SentimentAnalyzer(cfg.get("custom_dict"))
                log.info("舆情分析器已加载")
            except ImportError as e:
                log.warning("舆情分析模块导入失败: %s", e)

    def _init_notifier(self):
        if self._feishu_notifier:
            return
        fcfg = self.config.get("feishu", {})
        if fcfg.get("enabled") and fcfg.get("webhook_url"):
            try:
                from notifiers.feishu import FeishuNotifier
                self._feishu_notifier = FeishuNotifier(
                    fcfg["webhook_url"], fcfg.get("sign_secret", "")
                )
                log.info("飞书推送已加载")
            except ImportError as e:
                log.warning("飞书推送模块导入失败: %s", e)

    def _create_monitor(self, platform_name: str):
        MonitorClass = _load_monitor_class(platform_name)
        if not MonitorClass:
            return None
        pcfg = get_platform_config(platform_name, self.config)
        return MonitorClass(pcfg, self.db_path)

    def _execute_job(self, job: ScheduledJob):
        log.info("[调度] 开始执行 %s", job.platform_name)
        run_id = self._record_start(job.platform_name)

        try:
            monitor = self._create_monitor(job.platform_name)
            if not monitor:
                raise RuntimeError(f"无法创建 {job.platform_name} 监控器")

            if not monitor.verify_auth():
                log.warning("[%s] 认证失效，跳过本次执行", job.platform_name)
                self._record_finish(run_id, "error", error="认证失效")
                return

            all_posts = []
            all_comments = []
            total_scanned = 0

            for keyword in job.keywords:
                max_pages = monitor.config.get("max_pages_per_keyword",
                                               monitor.config.get("sogou", {}).get("max_pages", 3))
                result = monitor.crawl(keyword, max_pages=max_pages)
                all_posts.extend(result.new_posts)
                all_comments.extend(result.new_comments)
                total_scanned += result.posts_scanned
                if result.errors:
                    for err in result.errors:
                        log.warning("[%s] %s", job.platform_name, err)

            # 舆情分析
            if self._sentiment_analyzer and all_posts:
                self._analyze_posts(all_posts)

            # 存储
            monitor.save_posts(all_posts)
            monitor.save_comments(all_comments)

            # 飞书推送
            if self._feishu_notifier and all_posts:
                self._push_posts(all_posts)

            self._record_finish(run_id, "success", posts_found=len(all_posts))
            log.info("[调度] %s 完成: %d 条帖子, %d 条评论",
                     job.platform_name, len(all_posts), len(all_comments))

        except Exception as e:
            log.error("[调度] %s 执行失败: %s", job.platform_name, e, exc_info=True)
            self._record_finish(run_id, "error", error=str(e))

    def _analyze_posts(self, posts: list[dict]):
        for post in posts:
            text = f"{post.get('title', '')} {post.get('content', '')}"
            if not text.strip():
                continue
            result = self._sentiment_analyzer.analyze(text)
            post["sentiment"] = result["sentiment"]
            post["sentiment_score"] = result["score"]
            post["keywords"] = result["keywords"]

        conn = get_connection(self.db_path)
        try:
            for post in posts:
                if "sentiment" in post:
                    import json
                    conn.execute(
                        """UPDATE posts SET sentiment=?, sentiment_score=?, keywords=?
                           WHERE id=?""",
                        (post["sentiment"], post["sentiment_score"],
                         json.dumps(post["keywords"], ensure_ascii=False), post["id"]),
                    )
            conn.commit()
        finally:
            conn.close()

    def _push_posts(self, posts: list[dict]):
        max_push = self.config.get("feishu", {}).get("max_push_per_run", 50)
        for post in posts[:max_push]:
            try:
                self._feishu_notifier.push_post(post)
            except Exception as e:
                log.warning("飞书推送失败: %s", e)

        conn = get_connection(self.db_path)
        try:
            import json
            from datetime import datetime as dt
            now = dt.now().isoformat()
            for post in posts[:max_push]:
                conn.execute(
                    "UPDATE posts SET pushed_to_feishu=1 WHERE id=?",
                    (post["id"],),
                )
            conn.commit()
        finally:
            conn.close()

    def _record_start(self, platform: str) -> int:
        conn = get_connection(self.db_path)
        try:
            cursor = conn.execute(
                """INSERT INTO scheduler_runs (platform, started_at, status)
                   VALUES (?, ?, 'running')""",
                (platform, datetime.now().isoformat()),
            )
            conn.commit()
            return cursor.lastrowid
        finally:
            conn.close()

    def _record_finish(self, run_id: int, status: str, posts_found: int = 0, error: str = ""):
        conn = get_connection(self.db_path)
        try:
            conn.execute(
                """UPDATE scheduler_runs SET finished_at=?, status=?, posts_found=?, error_message=?
                   WHERE id=?""",
                (datetime.now().isoformat(), status, posts_found, error, run_id),
            )
            conn.commit()
        finally:
            conn.close()

    def run(self):
        self._running = True
        self._init_analyzer()
        self._init_notifier()
        log.info("调度器启动，%d 个任务", len(self.jobs))

        while self._running:
            with self._lock:
                if not self.jobs:
                    break
                job = heapq.heappop(self.jobs)

            now = time.time()
            if job.next_run > now:
                wait = min(job.next_run - now, 5.0)
                time.sleep(wait)
                with self._lock:
                    heapq.heappush(self.jobs, job)
                continue

            if not job.enabled:
                job.next_run = time.time() + job.interval_seconds
                with self._lock:
                    heapq.heappush(self.jobs, job)
                continue

            self._execute_job(job)

            job.next_run = time.time() + job.interval_seconds
            with self._lock:
                heapq.heappush(self.jobs, job)

            next_str = datetime.fromtimestamp(job.next_run).strftime("%Y-%m-%d %H:%M:%S")
            log.info("[%s] 下次执行: %s", job.platform_name, next_str)

    def stop(self):
        self._running = False
        log.info("调度器停止")

    def reload_config(self):
        self.config = load_config(reload=True)
        with self._lock:
            self.jobs.clear()
        self._init_jobs()
        self._sentiment_analyzer = None
        self._feishu_notifier = None
        self._init_analyzer()
        self._init_notifier()

    def get_status(self) -> list[dict]:
        with self._lock:
            result = []
            for job in self.jobs:
                result.append({
                    "platform": job.platform_name,
                    "enabled": job.enabled,
                    "interval_hours": round(job.interval_seconds / 3600, 1),
                    "keywords": job.keywords,
                    "next_run": datetime.fromtimestamp(job.next_run).isoformat() if job.next_run else None,
                })
            return result
