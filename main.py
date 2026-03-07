import asyncio
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from urllib import error, request

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.utils.session_waiter import SessionController, session_waiter


DATABASE_NAME = "yuan_redeem.sqlite3"
REDEEM_ENDPOINT = "https://p11132-game-adapter.qookkagames.com/cms/active_code/change"
HTTP_TIMEOUT_SECONDS = 15
CODE_SPLIT_PATTERN = re.compile(r"[\s,，;；]+")
PLAYER_ID_PATTERN = re.compile(r"\d{1,32}")
ADMIN_COMMAND_PREFIX = "/"


@dataclass(slots=True)
class BindingRecord:
    scope_key: str
    sender_id: str
    sender_name: str
    player_id: str
    player_name: str
    created_at: str
    updated_at: str


@dataclass(slots=True)
class RedeemAttempt:
    code: str
    status: str
    message: str
    raw_response: str


class RedeemStore:
    def __init__(self, db_path: Path):
        self.db_path = db_path

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS user_bindings (
                    scope_key TEXT PRIMARY KEY,
                    sender_id TEXT NOT NULL,
                    sender_name TEXT NOT NULL,
                    player_id TEXT NOT NULL,
                    player_name TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS global_codes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    is_active INTEGER NOT NULL DEFAULT 1
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS redeem_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    scope_key TEXT NOT NULL,
                    sender_id TEXT NOT NULL,
                    code TEXT NOT NULL,
                    status TEXT NOT NULL,
                    response_message TEXT NOT NULL,
                    raw_response TEXT NOT NULL,
                    redeemed_at TEXT NOT NULL,
                    UNIQUE(scope_key, code)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_redeem_scope_key ON redeem_records(scope_key)"
            )

    def get_binding(self, scope_key: str) -> BindingRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT scope_key, sender_id, sender_name, player_id, player_name, created_at, updated_at
                FROM user_bindings
                WHERE scope_key = ?
                """,
                (scope_key,),
            ).fetchone()
        if row is None:
            return None
        return BindingRecord(*row)

    def upsert_binding(
        self,
        scope_key: str,
        sender_id: str,
        sender_name: str,
        player_id: str,
        player_name: str,
    ) -> None:
        now = self._now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO user_bindings(scope_key, sender_id, sender_name, player_id, player_name, created_at, updated_at)
                VALUES(?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(scope_key) DO UPDATE SET
                    sender_id = excluded.sender_id,
                    sender_name = excluded.sender_name,
                    player_id = excluded.player_id,
                    player_name = excluded.player_name,
                    updated_at = excluded.updated_at
                """,
                (scope_key, sender_id, sender_name, player_id, player_name, now, now),
            )

    def delete_binding(self, scope_key: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM user_bindings WHERE scope_key = ?", (scope_key,))
        return cursor.rowcount > 0

    def add_codes(self, codes: Iterable[str], created_by: str) -> tuple[list[str], list[str]]:
        inserted: list[str] = []
        duplicated: list[str] = []
        now = self._now()
        with self._connect() as conn:
            for code in codes:
                try:
                    conn.execute(
                        """
                        INSERT INTO global_codes(code, created_at, created_by, is_active)
                        VALUES(?, ?, ?, 1)
                        """,
                        (code, now, created_by),
                    )
                    inserted.append(code)
                except sqlite3.IntegrityError:
                    duplicated.append(code)
        return inserted, duplicated

    def list_active_codes(self) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT code
                FROM global_codes
                WHERE is_active = 1
                ORDER BY id ASC
                """
            ).fetchall()
        return [row[0] for row in rows]

    def delete_code(self, code: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM global_codes WHERE code = ?", (code,))
        return cursor.rowcount > 0

    def clear_codes(self) -> int:
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM global_codes")
        return cursor.rowcount

    def list_processed_codes(self, scope_key: str) -> set[str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT code FROM redeem_records WHERE scope_key = ?",
                (scope_key,),
            ).fetchall()
        return {row[0] for row in rows}

    def save_redeem_record(
        self,
        scope_key: str,
        sender_id: str,
        attempt: RedeemAttempt,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO redeem_records(
                    scope_key, sender_id, code, status, response_message, raw_response, redeemed_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    scope_key,
                    sender_id,
                    attempt.code,
                    attempt.status,
                    attempt.message,
                    attempt.raw_response,
                    self._now(),
                ),
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _now() -> str:
        return datetime.now().isoformat(timespec="seconds")


@register("astrbot_plugin_yuan_redeem", "mrsnake", "代号鸢兑换插件", "1.0.0")
class YuanRedeemPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.store = RedeemStore(Path(__file__).with_name(DATABASE_NAME))

    async def initialize(self):
        self.store.initialize()
        logger.info("代号鸢兑换插件已初始化")

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def handle_private_commands(self, event: AstrMessageEvent):
        text = self._normalized_text(event)
        if not text:
            return

        if text == "绑定代号鸢":
            result = await self._start_bind_flow(event)
            if result is not None:
                yield result
            event.stop_event()
            return

        if text == "解绑代号鸢":
            yield self._handle_unbind(event)
            event.stop_event()
            return

        if text in {"代号鸢绑定状态", "查询代号鸢绑定", "查看代号鸢绑定"}:
            yield self._handle_binding_status(event)
            event.stop_event()
            return

        if text == "代号鸢兑换":
            yield await self._handle_redeem(event)
            event.stop_event()
            return

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def handle_admin_commands(self, event: AstrMessageEvent):
        text = self._normalized_text(event)
        if not text:
            return

        text = self._strip_admin_command_prefix(text)
        if not text:
            return

        sender_label = self._get_sender_name(event)

        if text.startswith("添加代号鸢兑换码") or text.startswith("新增代号鸢兑换码"):
            payload = self._split_command_payload(text)
            codes = self._parse_codes(payload)
            if not codes:
                yield event.plain_result(
                    self._format_message(
                        "兑换码管理",
                        [
                            "还没收到兑换码内容。",
                            f"可以这样发送：{ADMIN_COMMAND_PREFIX}添加代号鸢兑换码 CODE123",
                        ],
                    )
                )
            else:
                added, duplicated = self.store.add_codes(codes, sender_label)
                lines = []
                if added:
                    lines.append(f"新加入 {len(added)} 个兑换码：{', '.join(added)}")
                if duplicated:
                    lines.append(f"这些兑换码之前已经有了，已自动跳过：{', '.join(duplicated)}")
                yield event.plain_result(self._format_message("兑换码管理", lines))
            event.stop_event()
            return

        if text.startswith("删除代号鸢兑换码"):
            payload = self._split_command_payload(text)
            code = payload.strip()
            if not code:
                yield event.plain_result(
                    self._format_message(
                        "兑换码管理",
                        [
                            "还没指定要删除的兑换码。",
                            f"可以这样发送：{ADMIN_COMMAND_PREFIX}删除代号鸢兑换码 CODE123",
                        ],
                    )
                )
            elif self.store.delete_code(code):
                yield event.plain_result(self._format_message("兑换码管理", [f"已删除兑换码：{code}"]))
            else:
                yield event.plain_result(self._format_message("兑换码管理", [f"没有找到兑换码：{code}"]))
            event.stop_event()
            return

        if text in {"查看代号鸢兑换码", "代号鸢兑换码列表"}:
            codes = self.store.list_active_codes()
            if not codes:
                yield event.plain_result(self._format_message("兑换码列表", ["现在还没有可用的全局兑换码。"]))
            else:
                lines = [f"当前共有 {len(codes)} 个全局兑换码："]
                lines.extend(f"{index}. {code}" for index, code in enumerate(codes, start=1))
                yield event.plain_result(self._format_message("兑换码列表", lines, bullet=False))
            event.stop_event()
            return

        if text == "清空代号鸢兑换码":
            count = self.store.clear_codes()
            yield event.plain_result(
                self._format_message("兑换码管理", [f"已清空全局兑换码，本次共删除 {count} 条记录。"])
            )
            event.stop_event()
            return

    async def terminate(self):
        logger.info("代号鸢兑换插件已卸载")

    async def _start_bind_flow(self, event: AstrMessageEvent):
        scope_key = self._get_scope_key(event)
        existing = self.store.get_binding(scope_key)
        if existing:
            return event.plain_result(
                self._format_message(
                    "账号已绑定",
                    [
                        f"角色 ID：{existing.player_id}",
                        f"角色名：{existing.player_name}",
                        "如果想换绑，先发送“解绑代号鸢”就好。",
                    ],
                )
            )

        holder: dict[str, str] = {}

        try:
            await event.send(
                event.plain_result(
                    self._format_message(
                        "开始绑定",
                        ["先把角色 ID 发给我吧。", "中途想停下时，发送“取消”或“退出”即可。"],
                    )
                )
            )

            @session_waiter(timeout=120, record_history_chains=False)
            async def bind_waiter(controller: SessionController, session_event: AstrMessageEvent):
                message = self._normalized_text(session_event)
                if message in {"取消", "退出"}:
                    await session_event.send(
                        session_event.plain_result(
                            self._format_message(
                                "绑定已取消",
                                ["这次绑定流程已经结束。", "想继续时，再发送“绑定代号鸢”就行。"],
                            )
                        )
                    )
                    controller.stop()
                    return

                if "player_id" not in holder:
                    if not PLAYER_ID_PATTERN.fullmatch(message):
                        await session_event.send(
                            session_event.plain_result(
                                self._format_message(
                                    "角色 ID 不太对",
                                    ["角色 ID 需要是 1 到 32 位数字。", "请重新输入一次。"],
                                )
                            )
                        )
                        controller.keep(timeout=120, reset_timeout=True)
                        return
                    holder["player_id"] = message
                    await session_event.send(
                        session_event.plain_result(
                            self._format_message(
                                "收到角色 ID",
                                [
                                    f"角色 ID：{message}",
                                    "接下来把角色名发给我吧。",
                                    "中途仍可发送“取消”结束流程。",
                                ],
                            )
                        )
                    )
                    controller.keep(timeout=120, reset_timeout=True)
                    return

                if not message or len(message) > 32:
                    await session_event.send(
                        session_event.plain_result(
                            self._format_message(
                                "角色名不符合要求",
                                ["角色名不能为空，且长度不能超过 32 个字符。", "请重新输入一次。"],
                            )
                        )
                    )
                    controller.keep(timeout=120, reset_timeout=True)
                    return

                holder["player_name"] = message
                self.store.upsert_binding(
                    scope_key=scope_key,
                    sender_id=self._get_sender_id(session_event),
                    sender_name=self._get_sender_name(session_event),
                    player_id=holder["player_id"],
                    player_name=holder["player_name"],
                )
                await session_event.send(
                    session_event.plain_result(
                        self._format_message(
                            "绑定完成",
                            [
                                f"角色 ID：{holder['player_id']}",
                                f"角色名：{holder['player_name']}",
                                "现在可以直接发送“代号鸢兑换”开始兑换啦。",
                            ],
                        )
                    )
                )
                controller.stop()

            await bind_waiter(event)
            return None
        except TimeoutError:
            return event.plain_result(
                self._format_message(
                    "绑定超时",
                    ["120 秒内没有收到新消息，这次绑定已自动取消。", "需要时重新发送“绑定代号鸢”即可。"],
                )
            )
        except Exception as exc:
            logger.exception("绑定代号鸢失败")
            return event.plain_result(self._format_message("绑定失败", [f"处理绑定时出了点问题：{exc}"]))

    def _handle_unbind(self, event: AstrMessageEvent):
        scope_key = self._get_scope_key(event)
        if self.store.delete_binding(scope_key):
            return event.plain_result(self._format_message("解绑完成", ["当前代号鸢账号已经解绑。"]))
        return event.plain_result(self._format_message("还没有绑定", ["你当前还没有绑定代号鸢账号。"]))

    def _handle_binding_status(self, event: AstrMessageEvent):
        binding = self.store.get_binding(self._get_scope_key(event))
        if binding is None:
            return event.plain_result(self._format_message("还没有绑定", ["你当前还没有绑定代号鸢账号。"]))
        return event.plain_result(
            self._format_message(
                "当前绑定信息",
                [f"角色 ID：{binding.player_id}", f"角色名：{binding.player_name}"],
            )
        )

    async def _handle_redeem(self, event: AstrMessageEvent):
        scope_key = self._get_scope_key(event)
        binding = self.store.get_binding(scope_key)
        if binding is None:
            return event.plain_result(
                self._format_message("还不能兑换", ["请先私聊发送“绑定代号鸢”，先把账号绑定好。"])
            )

        all_codes = self.store.list_active_codes()
        if not all_codes:
            return event.plain_result(self._format_message("暂时没有兑换码", ["管理员还没有设置可用的兑换码。"]))

        processed = self.store.list_processed_codes(scope_key)
        pending_codes = [code for code in all_codes if code not in processed]
        if not pending_codes:
            return event.plain_result(
                self._format_message("没有新的兑换码", ["当前全局兑换码你都处理过了，暂时没有新的可兑换内容。"])
            )

        lines = [
            f"本次共有 {len(pending_codes)} 个新兑换码待处理。",
            f"绑定账号：{binding.player_name}（ID：{binding.player_id}）",
        ]
        for code in pending_codes:
            attempt = await self._redeem_code(binding.player_id, binding.player_name, code)
            self.store.save_redeem_record(scope_key, self._get_sender_id(event), attempt)
            status_text = "兑换成功" if attempt.status == "success" else "兑换失败"
            status_icon = "✅" if attempt.status == "success" else "❌"
            lines.append(f"{status_icon} {code}：{status_text}，{attempt.message}")

        return event.plain_result(self._format_message("兑换结果", lines, bullet=False))

    async def _redeem_code(self, player_id: str, player_name: str, code: str) -> RedeemAttempt:
        payload = {
            "player_name": player_name,
            "player_id": player_id,
            "code": code,
        }
        headers = {
            "accept": "application/json, text/plain, */*",
            "accept-language": "zh-CN,zh;q=0.9",
            "content-type": "application/json",
            "origin": "https://game-notice.sialiagames.com.tw",
            "referer": "https://game-notice.sialiagames.com.tw/",
            "user-agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
            ),
        }

        def do_request() -> tuple[int | None, str]:
            body = json.dumps(payload).encode("utf-8")
            req = request.Request(REDEEM_ENDPOINT, data=body, headers=headers, method="POST")
            try:
                with request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as response:
                    charset = response.headers.get_content_charset("utf-8")
                    return response.getcode(), response.read().decode(charset, errors="replace")
            except error.HTTPError as http_error:
                charset = http_error.headers.get_content_charset("utf-8")
                body_text = http_error.read().decode(charset, errors="replace")
                return http_error.code, body_text

        try:
            status_code, raw_text = await asyncio.to_thread(do_request)
            status, message = self._parse_redeem_response(status_code, raw_text)
            logger.info("兑换请求完成 code=%s status=%s", code, status)
            return RedeemAttempt(code=code, status=status, message=message, raw_response=raw_text[:1000])
        except Exception as exc:
            logger.exception("兑换请求失败 code=%s", code)
            return RedeemAttempt(
                code=code,
                status="error",
                message=f"请求异常：{exc}",
                raw_response=str(exc),
            )

    def _parse_redeem_response(self, status_code: int | None, raw_text: str) -> tuple[str, str]:
        message = f"HTTP {status_code}" if status_code is not None else "接口未返回状态码"
        try:
            payload = json.loads(raw_text)
        except json.JSONDecodeError:
            if raw_text.strip():
                message = raw_text.strip()[:120]
            return ("success" if status_code and 200 <= status_code < 300 else "error", message)

        normalized_message = self._extract_message(payload) or message
        status_value = self._extract_status_value(payload)

        if status_value in {0, 200, "0", "200", True, "success", "ok", "SUCCESS", "OK"}:
            return "success", normalized_message

        if isinstance(normalized_message, str) and any(keyword in normalized_message for keyword in ("成功", "success", "Success")):
            return "success", normalized_message

        if status_code and 200 <= status_code < 300 and status_value is None:
            return "success", normalized_message

        return "error", normalized_message

    @staticmethod
    def _extract_status_value(payload: Any) -> Any:
        if not isinstance(payload, dict):
            return None
        for key in ("code", "status", "errno", "errcode", "ret", "success"):
            if key in payload:
                return payload[key]
        return None

    @staticmethod
    def _extract_message(payload: Any) -> str:
        if isinstance(payload, dict):
            for key in ("message", "msg", "errmsg", "error", "detail"):
                value = payload.get(key)
                if value not in (None, ""):
                    return str(value)
            data = payload.get("data")
            if isinstance(data, dict):
                for key in ("message", "msg", "errmsg", "error", "detail"):
                    value = data.get(key)
                    if value not in (None, ""):
                        return str(value)
        return ""

    @staticmethod
    def _normalized_text(event: AstrMessageEvent) -> str:
        return (getattr(event, "message_str", "") or "").strip()

    @staticmethod
    def _strip_admin_command_prefix(text: str) -> str:
        if not text.startswith(ADMIN_COMMAND_PREFIX):
            return ""
        return text[len(ADMIN_COMMAND_PREFIX) :].strip()

    @staticmethod
    def _split_command_payload(text: str) -> str:
        parts = text.split(maxsplit=1)
        return parts[1] if len(parts) > 1 else ""

    @staticmethod
    def _parse_codes(payload: str) -> list[str]:
        seen: set[str] = set()
        codes: list[str] = []
        for code in CODE_SPLIT_PATTERN.split(payload.strip()):
            if not code or code in seen:
                continue
            seen.add(code)
            codes.append(code)
        return codes

    @staticmethod
    def _format_message(title: str, lines: Iterable[str], *, bullet: bool = True) -> str:
        rendered_lines = [line for line in lines if line]
        if not rendered_lines:
            return f"【{title}】"

        prefix = "• " if bullet else ""
        body = [f"{prefix}{line}" for line in rendered_lines]
        return "\n".join([f"【{title}】", *body])

    @staticmethod
    def _get_scope_key(event: AstrMessageEvent) -> str:
        for attr in ("unified_msg_origin",):
            value = getattr(event, attr, None)
            if value:
                return str(value)

        message_obj = getattr(event, "message_obj", None)
        if message_obj is not None:
            for attr in ("session_id",):
                value = getattr(message_obj, attr, None)
                if value:
                    return str(value)

        sender_id = YuanRedeemPlugin._get_sender_id(event)
        return f"fallback::{sender_id}"

    @staticmethod
    def _get_sender_id(event: AstrMessageEvent) -> str:
        getter = getattr(event, "get_sender_id", None)
        if callable(getter):
            value = getter()
            if value:
                return str(value)

        message_obj = getattr(event, "message_obj", None)
        sender = getattr(message_obj, "sender", None)
        for attr in ("user_id", "id", "qq"):
            value = getattr(sender, attr, None)
            if value:
                return str(value)

        return str(getattr(message_obj, "session_id", "unknown_sender"))

    @staticmethod
    def _get_sender_name(event: AstrMessageEvent) -> str:
        getter = getattr(event, "get_sender_name", None)
        if callable(getter):
            value = getter()
            if value:
                return str(value)

        message_obj = getattr(event, "message_obj", None)
        sender = getattr(message_obj, "sender", None)
        for attr in ("nickname", "card", "name"):
            value = getattr(sender, attr, None)
            if value:
                return str(value)

        return "unknown_user"
