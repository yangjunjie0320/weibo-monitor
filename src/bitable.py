from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re

import lark_oapi as lark
from lark_oapi.api.bitable.v1 import (
    AppTableCreateHeader,
    AppTableField,
    AppTableRecord,
    BatchCreateAppTableRecordRequest,
    BatchCreateAppTableRecordRequestBody,
    Condition,
    CreateAppTableFieldRequest,
    CreateAppTableRequest,
    CreateAppTableRequestBody,
    FilterInfo,
    ListAppTableFieldRequest,
    ListAppTableRequest,
    ReqTable,
    SearchAppTableRecordRequest,
    SearchAppTableRecordRequestBody,
)
from lark_oapi.api.wiki.v2 import GetNodeSpaceRequest

from .config import Settings
from .forward import ForwardStore

logger = logging.getLogger(__name__)

_BATCH_SIZE = 100

# 飞书多维表格字段类型码：1 多行文本、2 数字、5 日期、15 超链接
_FIELDS = [
    ("博主", 1),
    ("分类", 1),
    ("摘要", 1),
    ("原帖链接", 15),
    ("转发人", 1),
    ("发帖时间", 5),
    ("转发时间", 5),
    ("mid", 1),
]


class BitableError(Exception):
    pass


def _parse_url_token(url: str) -> tuple[str, str]:
    """从分享链接解析 (kind, token)。kind: base=直接是 app_token；wiki=需再解析。"""
    m = re.search(r"/(base|wiki)/([A-Za-z0-9]+)", url)
    if not m:
        raise BitableError(f"bitable_url 不是 /base/ 或 /wiki/ 形式的链接: {url}")
    return m.group(1), m.group(2)


def _to_ms(iso: str) -> int | None:
    if not iso:
        return None
    try:
        return int(dt.datetime.fromisoformat(iso).timestamp() * 1000)
    except ValueError:
        return None


class BitableSyncer:
    """把本地转发归档记录周期性批量写入多维表格。

    表格由用户手建（机器人需为可编辑协作者），数据表和字段不存在则自动创建。
    全部 lark 调用是同步 SDK，整个同步体放线程池里跑。
    """

    def __init__(self, settings: Settings, client: lark.Client, store: ForwardStore) -> None:
        self._settings = settings
        self._client = client
        self._store = store
        self._app_token: str = ""
        self._table_id: str = ""

    async def run_forever(self) -> None:
        interval = self._settings.bitable_sync_interval_seconds
        loop = asyncio.get_running_loop()
        while True:
            try:
                synced = await loop.run_in_executor(None, self._sync_once)
                if synced:
                    logger.info("bitable sync: %d record(s) written", synced)
            except Exception:
                logger.exception("bitable sync failed, will retry next round")
            await asyncio.sleep(interval)

    # ---- 以下均为阻塞调用，跑在线程池 ----

    def _sync_once(self) -> int:
        pending = self._store.pending_archive()
        if not pending:
            return 0
        try:
            self._ensure_table()
        except Exception as exc:
            self._store.mark_archive_failed(
                [mid for mid, _ in pending],
                exc,
                retry_after_seconds=self._settings.bitable_sync_interval_seconds,
            )
            raise

        to_create: list[tuple[str, dict]] = []
        for index, (mid, rec) in enumerate(pending):
            try:
                exists = self._remote_has_mid(mid)
            except Exception as exc:
                # Defer this and all records not yet checked. Previously checked items
                # remain safe: existing ones were marked synced and missing ones have
                # not been written yet.
                deferred = [pending_mid for pending_mid, _ in pending[index:]]
                self._store.mark_archive_failed(
                    deferred,
                    exc,
                    retry_after_seconds=self._settings.bitable_sync_interval_seconds,
                )
                raise
            if exists:
                self._store.mark_archive_synced([mid])
                logger.info("bitable record already exists: mid=%s", mid)
            else:
                to_create.append((mid, rec))

        created = 0
        for start in range(0, len(to_create), _BATCH_SIZE):
            batch = to_create[start : start + _BATCH_SIZE]
            try:
                self._batch_create(batch)
            except Exception as exc:
                # Only this and later, not-yet-attempted batches need another round.
                deferred = [mid for mid, _ in to_create[start:]]
                self._store.mark_archive_failed(
                    deferred,
                    exc,
                    retry_after_seconds=self._settings.bitable_sync_interval_seconds,
                )
                raise
            mids = [mid for mid, _ in batch]
            self._store.mark_archive_synced(mids)
            created += len(batch)
        return created

    def _batch_create(self, batch: list[tuple[str, dict]]) -> None:
        records = [
            AppTableRecord.builder().fields(self._to_fields(mid, rec)).build()
            for mid, rec in batch
        ]
        request = (
            BatchCreateAppTableRecordRequest.builder()
            .app_token(self._app_token)
            .table_id(self._table_id)
            .request_body(
                BatchCreateAppTableRecordRequestBody.builder().records(records).build()
            )
            .build()
        )
        response = self._client.bitable.v1.app_table_record.batch_create(request)
        if not response.success():
            raise BitableError(
                f"batch create failed: code={response.code} msg={response.msg}"
            )
        created_records = response.data.records if response.data else None
        if created_records is None or len(created_records) != len(batch):
            # Treat an ambiguous success as retryable. The next run's mid search will
            # discover any records the server actually committed and avoid duplicates.
            actual = 0 if created_records is None else len(created_records)
            raise BitableError(
                f"batch create returned {actual} record(s), expected {len(batch)}"
            )

    def _remote_has_mid(self, mid: str) -> bool:
        condition = (
            Condition.builder().field_name("mid").operator("is").value([mid]).build()
        )
        filter_info = (
            FilterInfo.builder().conjunction("and").conditions([condition]).build()
        )
        body = (
            SearchAppTableRecordRequestBody.builder()
            .field_names(["mid"])
            .filter(filter_info)
            .build()
        )
        request = (
            SearchAppTableRecordRequest.builder()
            .app_token(self._app_token)
            .table_id(self._table_id)
            .page_size(1)
            .request_body(body)
            .build()
        )
        response = self._client.bitable.v1.app_table_record.search(request)
        if not response.success():
            raise BitableError(
                f"record search failed: mid={mid} code={response.code} msg={response.msg}"
            )
        return bool(response.data and response.data.items)

    def _to_fields(self, mid: str, rec: dict) -> dict:
        fields: dict[str, object] = {
            "博主": rec.get("screen_name", ""),
            "分类": rec.get("label", ""),
            "摘要": rec.get("summary", ""),
            "转发人": rec.get("forwarder_name") or rec.get("forwarder_open_id", ""),
            "mid": mid,
        }
        if rec.get("url"):
            fields["原帖链接"] = {"text": "原帖", "link": rec["url"]}
        posted = _to_ms(rec.get("post_created_at", ""))
        if posted:
            fields["发帖时间"] = posted
        forwarded = _to_ms(rec.get("forwarded_at", ""))
        if forwarded:
            fields["转发时间"] = forwarded
        return fields

    def _ensure_table(self) -> None:
        if self._app_token and self._table_id:
            return
        self._app_token = self._resolve_app_token()
        name = self._settings.bitable_table_name
        existing = self._list_tables()
        for table_id, table_name in existing:
            if table_name == name:
                self._table_id = table_id
                logger.info("bitable table found: %s (%s)", name, table_id)
                break
        else:
            # Never fall back to the first unrelated table: that can silently put
            # archive data into a user-owned dataset with a different meaning.
            self._table_id = self._create_table(name)
            logger.info("bitable table created: %s (%s)", name, self._table_id)
        self._ensure_fields()

    def _ensure_fields(self) -> None:
        existing = self._list_fields()
        for field_name, field_type in _FIELDS:
            if field_name in existing:
                continue
            request = (
                CreateAppTableFieldRequest.builder()
                .app_token(self._app_token)
                .table_id(self._table_id)
                .request_body(
                    AppTableField.builder().field_name(field_name).type(field_type).build()
                )
                .build()
            )
            response = self._client.bitable.v1.app_table_field.create(request)
            if not response.success():
                raise BitableError(
                    f"field create failed: {field_name}: "
                    f"code={response.code} msg={response.msg}"
                )
            logger.info("bitable field created: %s", field_name)

    def _list_fields(self) -> set[str]:
        fields: set[str] = set()
        page_token = ""
        while True:
            builder = (
                ListAppTableFieldRequest.builder()
                .app_token(self._app_token)
                .table_id(self._table_id)
                .page_size(100)
            )
            if page_token:
                builder = builder.page_token(page_token)
            response = self._client.bitable.v1.app_table_field.list(builder.build())
            if not response.success():
                raise BitableError(
                    f"field list failed: code={response.code} msg={response.msg}"
                )
            for item in response.data.items or []:
                if item.field_name:
                    fields.add(item.field_name)
            if not response.data.has_more:
                break
            page_token = response.data.page_token or ""
        return fields

    def _resolve_app_token(self) -> str:
        kind, token = _parse_url_token(self._settings.bitable_url)
        if kind == "base":
            return token
        # wiki 节点：解析出真实的 bitable app_token（需 wiki:wiki:readonly 权限）
        request = GetNodeSpaceRequest.builder().token(token).obj_type("wiki").build()
        response = self._client.wiki.v2.space.get_node(request)
        if not response.success():
            raise BitableError(
                f"wiki node resolve failed: code={response.code} msg={response.msg}"
                "（检查是否已开通 wiki:wiki:readonly 权限、机器人是否有该文档权限）"
            )
        node = response.data.node
        if not node or node.obj_type != "bitable" or not node.obj_token:
            raise BitableError(
                f"wiki 节点不是多维表格: obj_type={getattr(node, 'obj_type', None)}"
            )
        return node.obj_token

    def _list_tables(self) -> list[tuple[str, str]]:
        tables: list[tuple[str, str]] = []
        page_token = ""
        while True:
            builder = ListAppTableRequest.builder().app_token(self._app_token).page_size(100)
            if page_token:
                builder = builder.page_token(page_token)
            response = self._client.bitable.v1.app_table.list(builder.build())
            if not response.success():
                raise BitableError(
                    f"table list failed: code={response.code} msg={response.msg}"
                )
            for item in response.data.items or []:
                tables.append((item.table_id or "", item.name or ""))
            if not response.data.has_more:
                break
            page_token = response.data.page_token or ""
        return tables

    def _create_table(self, name: str) -> str:
        fields = [
            AppTableCreateHeader.builder().field_name(fname).type(ftype).build()
            for fname, ftype in _FIELDS
        ]
        request = (
            CreateAppTableRequest.builder()
            .app_token(self._app_token)
            .request_body(
                CreateAppTableRequestBody.builder()
                .table(ReqTable.builder().name(name).fields(fields).build())
                .build()
            )
            .build()
        )
        response = self._client.bitable.v1.app_table.create(request)
        if not response.success():
            raise BitableError(
                f"table create failed: code={response.code} msg={response.msg}"
            )
        table_id = response.data.table_id if response.data else ""
        if not table_id:
            raise BitableError("table create succeeded without a table_id")
        return table_id
