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
    CreateAppTableFieldRequest,
    CreateAppTableRequest,
    CreateAppTableRequestBody,
    ListAppTableFieldRequest,
    ListAppTableRequest,
    ReqTable,
)
from lark_oapi.api.wiki.v2 import GetNodeSpaceRequest

from .config import Settings
from .forward import ForwardStore

logger = logging.getLogger(__name__)

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
        pending = self._store.pending()
        if not pending:
            return 0
        self._ensure_table()
        records = [
            AppTableRecord.builder().fields(self._to_fields(mid, rec)).build()
            for mid, rec in pending
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
        self._store.mark_synced([mid for mid, _ in pending])
        return len(pending)

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
            try:
                self._table_id = self._create_table(name)
                logger.info("bitable table created: %s (%s)", name, self._table_id)
            except BitableError as e:
                # wiki 承载的表格「可编辑」权限不允许建数据表（需可管理），
                # 回落到第一张现有表，缺的字段补上
                if not existing:
                    raise
                self._table_id = existing[0][0]
                logger.warning(
                    "cannot create table (%s), falling back to existing table %s (%s)",
                    e,
                    existing[0][1],
                    self._table_id,
                )
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
        return response.data.table_id or ""
