"""
Feishu Bitable (多维表格) Writer — push crawled posts to a Feishu spreadsheet.

API Reference:
- Token: POST /open-apis/auth/v3/tenant_access_token/internal
- List fields: GET /open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/fields
- Create field: POST /open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/fields
- Create records: POST /open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records/batch_create
"""

import logging
import time

import requests

log = logging.getLogger(__name__)

BASE_URL = "https://open.feishu.cn"

# Mapping: post field → (Bitable column name, field type, extra props)
FIELD_DEFS = [
    ("platform", "平台", 1, {}),          # 1 = MultiSelect
    ("keyword", "关键词", 1, {}),
    ("user_name", "作者", 1, {}),
    ("title", "标题", 1, {}),
    ("content", "内容", 1, {}),
    ("url", "链接", 15, {}),              # 15 = Url
    ("created_at", "发布时间", 1, {}),
    ("fetched_at", "采集时间", 1, {}),
    ("sentiment", "情感", 3, {            # 3 = Select
        "property": {"options": [
            {"name": "positive"}, {"name": "negative"}, {"name": "neutral"},
        ]}
    }),
    ("sentiment_score", "情感分值", 2, {}),  # 2 = Number
    ("likes_count", "点赞数", 2, {}),
    ("comments_count", "评论数", 2, {}),
    ("reposts_count", "转发数", 2, {}),
]

PLATFORM_OPTIONS = ["weibo", "wechat", "maimai", "xiaohongshu"]


class FeishuBitableWriter:
    def __init__(self, app_id: str, app_secret: str, app_token: str, table_id: str):
        self.app_id = app_id
        self.app_secret = app_secret
        self.app_token = app_token
        self.table_id = table_id
        self._token = ""
        self._token_expires = 0
        self._fields_ready = False

    def _get_token(self) -> str:
        if self._token and time.time() < self._token_expires:
            return self._token
        resp = requests.post(
            f"{BASE_URL}/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": self.app_id, "app_secret": self.app_secret},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"Failed to get tenant token: {data.get('msg')}")
        self._token = data["tenant_access_token"]
        self._token_expires = time.time() + data.get("expire", 7200) - 300
        log.info("[Bitable] Token refreshed, expires in %d seconds", data.get("expire", 7200))
        return self._token

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._get_token()}",
            "Content-Type": "application/json",
        }

    def _api_url(self, path: str) -> str:
        return f"{BASE_URL}/open-apis/bitable/v1/apps/{self.app_token}/tables/{self.table_id}/{path}"

    def _ensure_fields(self):
        """Auto-create missing columns. Fail gracefully."""
        if self._fields_ready:
            return
        try:
            token = self._get_token()
        except Exception as e:
            log.error("[Bitable] Cannot get token, skipping field creation: %s", e)
            return

        # List existing fields
        existing = set()
        try:
            resp = requests.get(self._api_url("fields"), headers=self._headers(), timeout=10)
            data = resp.json()
            if data.get("code") == 0:
                for item in data.get("data", {}).get("items", []):
                    existing.add(item.get("field_name", ""))
        except Exception as e:
            log.warning("[Bitable] Failed to list fields: %s", e)

        # Create missing fields
        for post_key, col_name, field_type, extra_props in FIELD_DEFS:
            if col_name in existing:
                continue
            body = {"field_name": col_name, "type": field_type}
            if col_name == "平台":
                body["property"] = {"options": [{"name": p} for p in PLATFORM_OPTIONS]}
            body.update(extra_props)
            try:
                resp = requests.post(
                    self._api_url("fields"), headers=self._headers(), json=body, timeout=10,
                )
                result = resp.json()
                if result.get("code") != 0:
                    log.warning("[Bitable] Failed to create field '%s': %s", col_name, result.get("msg"))
                else:
                    log.info("[Bitable] Created field '%s'", col_name)
            except Exception as e:
                log.warning("[Bitable] Failed to create field '%s': %s", col_name, e)

        self._fields_ready = True

    def push_posts(self, posts: list[dict]) -> int:
        """Batch write posts to Bitable. Returns number of records written."""
        if not posts:
            return 0

        self._ensure_fields()

        # Build column name map
        col_map = {post_key: col_name for post_key, col_name, _, _ in FIELD_DEFS}

        # Convert posts to Bitable records
        records = []
        for post in posts:
            fields = {}
            for post_key, col_name in col_map.items():
                value = post.get(post_key)
                if value is None:
                    continue
                if isinstance(value, (int, float)):
                    fields[col_name] = value
                else:
                    fields[col_name] = str(value)
            if fields:
                records.append({"fields": fields})

        if not records:
            return 0

        # Batch create (max 500 per request)
        total_written = 0
        for i in range(0, len(records), 500):
            batch = records[i:i + 500]
            try:
                resp = requests.post(
                    self._api_url("records/batch_create"),
                    headers=self._headers(),
                    json={"records": batch},
                    timeout=30,
                )
                result = resp.json()
                if result.get("code") != 0:
                    log.error("[Bitable] Batch write failed: %s", result.get("msg"))
                else:
                    written = len(result.get("data", {}).get("records", []))
                    total_written += written
                    log.info("[Bitable] Wrote %d records (batch %d)", written, i // 500 + 1)
            except Exception as e:
                log.error("[Bitable] Batch write error: %s", e)

        return total_written
