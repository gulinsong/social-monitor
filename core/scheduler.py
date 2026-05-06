import heapq
import logging
import random
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
        log.error("Unknown platform: %s", platform_name)
        return None

    import importlib
    try:
        module = importlib.import_module(module_path)
        return module.Monitor
    except (ImportError, AttributeError) as e:
        log.error("Failed to load platform module %s: %s", platform_name, e)
        return None


class UnifiedScheduler:
    def __init__(self, config: dict = None, db_path: str = None):
        self.config = config or load_config()
        self.db_path = db_path or self.config.get("app", {}).get("db_path", "db/monitor.db")
        self.jobs: list[ScheduledJob] = []
        self._running = False
        self._lock = threading.Lock()
        self._sentiment_analyzer = None
        self._llm_analyzer = None
        self._feishu_notifier = None
        self._bitable_writer = None
        self._last_cleanup_date = None
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
        log.info("Scheduler initialized: %d jobs", len(self.jobs))

    def _cleanup_old_data(self):
        """Layered data cleanup — runs once per day"""
        today = datetime.now().strftime("%Y-%m-%d")
        if self._last_cleanup_date == today:
            return
        self._last_cleanup_date = today

        cfg = self.config.get("app", {})
        retention = cfg.get("retention", {})

        runs_days = retention.get("runs_days", 30)
        posts_days = retention.get("posts_days", 0)
        pushed_days = retention.get("pushed_days", 0)

        # Nothing to clean if all are 0
        if runs_days <= 0 and posts_days <= 0 and pushed_days <= 0:
            return

        conn = get_connection(self.db_path)
        try:
            deleted_runs = 0
            deleted_posts = 0
            deleted_comments = 0

            # Layer 1: scheduler_runs — operational logs, short retention
            if runs_days > 0:
                deleted_runs = conn.execute(
                    f"DELETE FROM scheduler_runs WHERE started_at < datetime('now', '-{runs_days} days')"
                ).rowcount

            # Layer 2: posts/comments — only if explicit retention set
            if posts_days > 0:
                # Find posts to delete, then delete their comments first (FK constraint)
                old_post_ids = conn.execute(
                    f"SELECT id FROM posts WHERE fetched_at < datetime('now', '-{posts_days} days')"
                ).fetchall()
                if old_post_ids:
                    placeholders = ",".join(["?"] * len(old_post_ids))
                    ids = [r["id"] for r in old_post_ids]
                    deleted_comments += conn.execute(
                        f"DELETE FROM comments WHERE post_id IN ({placeholders})", ids
                    ).rowcount
                    deleted_posts = conn.execute(
                        f"DELETE FROM posts WHERE id IN ({placeholders})", ids
                    ).rowcount

            # Layer 3: pushed posts older than N days — already notified, lower value
            if pushed_days > 0 and pushed_days != posts_days:
                old_pushed = conn.execute(
                    f"""SELECT id FROM posts
                        WHERE pushed_to_feishu = 1
                          AND fetched_at < datetime('now', '-{pushed_days} days')""",
                ).fetchall()
                if old_pushed:
                    placeholders = ",".join(["?"] * len(old_pushed))
                    ids = [r["id"] for r in old_pushed]
                    deleted_comments += conn.execute(
                        f"DELETE FROM comments WHERE post_id IN ({placeholders})", ids
                    ).rowcount
                    extra_posts = conn.execute(
                        f"DELETE FROM posts WHERE id IN ({placeholders})", ids
                    ).rowcount
                    deleted_posts += extra_posts

            conn.commit()

            if deleted_posts or deleted_comments or deleted_runs:
                log.info(
                    "Data cleanup: deleted %d posts, %d comments, %d runs "
                    "(runs>%dd, posts>%dd, pushed>%dd)",
                    deleted_posts, deleted_comments, deleted_runs,
                    runs_days, posts_days, pushed_days,
                )
        finally:
            conn.close()

    def _init_analyzer(self):
        cfg = self.config.get("sentiment", {})
        if not self._sentiment_analyzer and cfg.get("snowNLP", True):
            try:
                from analysis.sentiment import SentimentAnalyzer
                self._sentiment_analyzer = SentimentAnalyzer(cfg.get("custom_dict"))
                log.info("Sentiment analyzer loaded")
            except ImportError as e:
                log.warning("Failed to import sentiment analysis module: %s", e)

        if not self._llm_analyzer:
            llm_cfg = cfg.get("llm", {})
            if llm_cfg.get("enabled") and llm_cfg.get("api_url") and llm_cfg.get("api_key"):
                try:
                    from analysis.llm_analyzer import LLMAnalyzer
                    self._llm_analyzer = LLMAnalyzer(llm_cfg)
                    log.info("LLM analyzer loaded")
                except ImportError as e:
                    log.warning("Failed to import LLM analyzer: %s", e)

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
                log.info("Feishu webhook notifier loaded")
            except ImportError as e:
                log.warning("Failed to import Feishu notifier module: %s", e)

        if not self._bitable_writer:
            bcfg = fcfg.get("bitable", {})
            if bcfg.get("enabled") and bcfg.get("app_id") and bcfg.get("app_secret"):
                try:
                    from notifiers.feishu_bitable import FeishuBitableWriter
                    self._bitable_writer = FeishuBitableWriter(
                        bcfg["app_id"], bcfg["app_secret"],
                        bcfg.get("app_token", ""), bcfg.get("table_id", ""),
                    )
                    log.info("Feishu Bitable writer loaded")
                except ImportError as e:
                    log.warning("Failed to import Feishu Bitable module: %s", e)

    def _create_monitor(self, platform_name: str):
        MonitorClass = _load_monitor_class(platform_name)
        if not MonitorClass:
            return None
        pcfg = get_platform_config(platform_name, self.config)
        return MonitorClass(pcfg, self.db_path)

    def _execute_job(self, job: ScheduledJob):
        log.info("[Scheduler] Starting execution: %s", job.platform_name)
        run_id = self._record_start(job.platform_name)

        try:
            monitor = self._create_monitor(job.platform_name)
            if not monitor:
                raise RuntimeError(f"Failed to create monitor for {job.platform_name}")

            if not monitor.verify_auth():
                log.warning("[%s] Authentication expired, skipping execution", job.platform_name)
                self._record_finish(run_id, "error", error="Authentication expired")
                return

            all_posts = []
            all_comments = []
            total_scanned = 0

            for keyword in (job.keywords or [""]):
                max_pages = monitor.config.get("max_pages_per_keyword",
                                               monitor.config.get("sogou", {}).get("max_pages", 3))
                result = monitor.crawl(keyword, max_pages=max_pages)
                all_posts.extend(result.new_posts)
                all_comments.extend(result.new_comments)
                total_scanned += result.posts_scanned
                if result.errors:
                    for err in result.errors:
                        log.warning("[%s] %s", job.platform_name, err)

            # Sentiment analysis
            if self._sentiment_analyzer and all_posts:
                self._analyze_posts(all_posts)

            # Save to database
            monitor.save_posts(all_posts)
            monitor.save_comments(all_comments)

            # Feishu push notifications
            if self._feishu_notifier and all_posts:
                self._push_posts(all_posts)

            self._record_finish(run_id, "success", posts_found=len(all_posts))
            log.info("[Scheduler] %s completed: %d posts, %d comments",
                     job.platform_name, len(all_posts), len(all_comments))

        except Exception as e:
            log.error("[Scheduler] %s execution failed: %s", job.platform_name, e, exc_info=True)
            self._record_finish(run_id, "error", error=str(e))

    def _analyze_posts(self, posts: list[dict]):
        # Step 1: SnowNLP full analysis + tags/summary/risk
        for post in posts:
            text = f"{post.get('title', '')} {post.get('content', '')}"
            if not text.strip():
                continue
            result = self._sentiment_analyzer.analyze(text)
            post["sentiment"] = result["sentiment"]
            post["sentiment_score"] = result["score"]
            post["keywords"] = result["keywords"]
            post["tags"] = self._sentiment_analyzer.extract_tags(text)
            post["summary"] = self._sentiment_analyzer.generate_summary(text)
            post["risk_level"] = self._sentiment_analyzer.assess_risk(text, result["score"])

        # Step 2: LLM deep analysis for high-risk content
        if self._llm_analyzer:
            high_risk = [p for p in posts if p.get("risk_level") in ("medium", "high")]
            if high_risk:
                log.info("[Analysis] %d high-risk posts, triggering LLM analysis", len(high_risk))
            for post in high_risk:
                text = f"{post.get('title', '')} {post.get('content', '')}"
                if not text.strip():
                    continue
                llm_result = self._llm_analyzer.analyze(text)
                if llm_result:
                    post["llm_analysis"] = llm_result
                    if llm_result.get("sentiment"):
                        post["sentiment"] = llm_result["sentiment"]
                    if llm_result.get("score") is not None:
                        post["sentiment_score"] = llm_result["score"]
                    if llm_result.get("risk_level"):
                        post["risk_level"] = llm_result["risk_level"]
                    if llm_result.get("tags"):
                        post["tags"] = list(set(post.get("tags", []) + llm_result["tags"]))

        # Save to DB
        conn = get_connection(self.db_path)
        try:
            import json as _json
            for post in posts:
                if "sentiment" not in post:
                    continue
                # Build extra JSON with tags/summary/risk
                extra = {}
                if post.get("extra"):
                    try:
                        extra = _json.loads(post["extra"]) if isinstance(post["extra"], str) else post["extra"]
                    except (ValueError, TypeError):
                        extra = {}
                if post.get("tags"):
                    extra["tags"] = post["tags"]
                if post.get("summary"):
                    extra["summary"] = post["summary"]
                if post.get("risk_level"):
                    extra["risk_level"] = post["risk_level"]

                llm_json = _json.dumps(post["llm_analysis"], ensure_ascii=False) if post.get("llm_analysis") else None

                conn.execute(
                    """UPDATE posts SET sentiment=?, sentiment_score=?, keywords=?,
                       extra=?, llm_analysis=? WHERE id=?""",
                    (post["sentiment"], post["sentiment_score"],
                     _json.dumps(post["keywords"], ensure_ascii=False),
                     _json.dumps(extra, ensure_ascii=False),
                     llm_json, post["id"]),
                )
            conn.commit()
        finally:
            conn.close()

    def _push_posts(self, posts: list[dict]):
        max_push = self.config.get("feishu", {}).get("max_push_per_run", 50)

        # Webhook push
        if self._feishu_notifier:
            for post in posts[:max_push]:
                try:
                    self._feishu_notifier.push_post(post)
                except Exception as e:
                    log.warning("Feishu webhook push failed: %s", e)
            conn = get_connection(self.db_path)
            try:
                for post in posts[:max_push]:
                    conn.execute(
                        "UPDATE posts SET pushed_to_feishu=1 WHERE id=?",
                        (post["id"],),
                    )
                conn.commit()
            finally:
                conn.close()

        # Bitable push
        if self._bitable_writer:
            try:
                written = self._bitable_writer.push_posts(posts)
                if written:
                    conn = get_connection(self.db_path)
                    try:
                        for post in posts:
                            conn.execute(
                                "UPDATE posts SET pushed_to_bitable=1 WHERE id=?",
                                (post["id"],),
                            )
                        conn.commit()
                    finally:
                        conn.close()
                    log.info("[Bitable] Marked %d posts as pushed", written)
            except Exception as e:
                log.error("Feishu Bitable push failed: %s", e)

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
        self._cleanup_old_data()
        log.info("Scheduler started, %d jobs", len(self.jobs))

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
                job.next_run = time.time() + job.interval_seconds + random.randint(60, 600)
                with self._lock:
                    heapq.heappush(self.jobs, job)
                continue

            # Random pre-task delay 30~180 seconds
            pre_delay = random.randint(30, 180)
            log.info("[Scheduler] %s will start in %d seconds", job.platform_name, pre_delay)
            time.sleep(pre_delay)

            self._execute_job(job)

            # Random post-task delay 30~300 seconds
            post_delay = random.randint(30, 300)
            time.sleep(post_delay)

            # Next run time with ±10% random jitter
            jitter = random.uniform(-0.1, 0.1) * job.interval_seconds
            job.next_run = time.time() + job.interval_seconds + jitter
            with self._lock:
                heapq.heappush(self.jobs, job)

            next_str = datetime.fromtimestamp(job.next_run).strftime("%Y-%m-%d %H:%M:%S")
            log.info("[%s] Next run: %s", job.platform_name, next_str)

    def stop(self):
        self._running = False
        log.info("Scheduler stopped")

    def reload_config(self):
        self.config = load_config(reload=True)
        with self._lock:
            self.jobs.clear()
        self._init_jobs()
        self._sentiment_analyzer = None
        self._llm_analyzer = None
        self._feishu_notifier = None
        self._bitable_writer = None
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
