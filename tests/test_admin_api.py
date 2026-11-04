"""管理操作层的纯逻辑测试。

不连接 QQ、不启动 NapCat：直接构造仓储、存储与引擎，验证

- 只读查询的返回形状与分页；
- 写操作确实改库、确实留下审计行；
- 越界值、未知用户、未授权通道都被拒绝且不写库；
- 备份生成真实文件、删除后查不到记录。
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _bootstrap import PACKAGE_NAME  # noqa: E402

admin_api = __import__(f"{PACKAGE_NAME}.admin_api", fromlist=["admin_api"])
config_module = __import__(f"{PACKAGE_NAME}.config", fromlist=["config"])
engine_module = __import__(f"{PACKAGE_NAME}.engine", fromlist=["engine"])
host_adapter = __import__(f"{PACKAGE_NAME}.host_adapter", fromlist=["host_adapter"])
models_module = __import__(f"{PACKAGE_NAME}.models", fromlist=["models"])
repository_module = __import__(f"{PACKAGE_NAME}.repository", fromlist=["repository"])

# 2026-01-01 12:00:00 +08:00
BASE_NOW = int(datetime(2026, 1, 1, 4, 0, 0, tzinfo=timezone.utc).timestamp())

BOT_ACCOUNT = "100000002"
USER_ID = "20000"
OTHER_USER_ID = "30000"
ADMIN_ID = "100000001"

USER_KEY = f"qq\x1f{BOT_ACCOUNT}\x1f\x1f{USER_ID}"
OTHER_USER_KEY = f"qq\x1f{BOT_ACCOUNT}\x1f\x1f{OTHER_USER_ID}"


def build_admin_config() -> "config_module.WhaleAffectConfig":
    """构造一份启用插件、维护者为 ADMIN_ID 的配置。"""

    config = config_module.WhaleAffectConfig()
    config.plugin.enabled = True
    config.plugin.shadow_mode = False
    config.admin.user_ids = [ADMIN_ID]
    config.admin.bot_account_id = BOT_ACCOUNT
    return config


class AdminTestCase(unittest.TestCase):
    """使用临时数据库装配管理上下文。"""

    def setUp(self) -> None:
        """准备临时数据库、存储、引擎与管理上下文。"""

        self.tmpdir = Path(tempfile.mkdtemp(prefix="whale-affect-admin-"))
        self.plugin_config = build_admin_config()
        self.engine_config = engine_module.build_engine_config(
            mood=self.plugin_config.mood,
            affinity=self.plugin_config.affinity,
            reactions=self.plugin_config.reactions,
            scoring=self.plugin_config.scoring,
            classifier=self.plugin_config.classifier,
        )
        self.repository = repository_module.AffectRepository(
            self.tmpdir / "affect.sqlite3",
            single_threaded=True,
        )
        self.repository.start()
        self.storage = repository_module.create_storage(self.repository, lambda: self.engine_config)
        self.clock = engine_module.LocalClock("Asia/Shanghai")
        self.engine = engine_module.AffectEngine(self.storage, self.engine_config, self.clock)
        self.context = admin_api.AdminContext(
            repository=self.repository,
            storage=self.storage,
            engine=self.engine,
            config=self.plugin_config,
        )
        self.local_date = self.clock.local_date(BASE_NOW)

    def tearDown(self) -> None:
        """关闭数据库并清理临时目录。"""

        self.repository.close()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def seed_user(
        self,
        user_key: str = USER_KEY,
        *,
        affinity: float = 30.0,
        active_days: int = 3,
        valid_interactions: int = 12,
        title_opt_in: bool = False,
        preferred_title: str = "",
        title_enabled: bool = False,
    ) -> None:
        """直接写入一行用户关系状态。

        Args:
            user_key: 用户身份键。
            affinity: 好感分数点。
            active_days: 活跃日数。
            valid_interactions: 有效交流次数。
            title_opt_in: 是否愿意被称呼。
            preferred_title: 偏好称呼词。
            title_enabled: 称呼许可是否已生效。
        """

        baseline = self.storage.load_user(user_key, self.engine_config)
        row = admin_api._default_user_row(baseline, self.local_date, BASE_NOW)
        row.update(
            {
                "affinity_centi": engine_module.to_centi(affinity),
                "offset_centi": engine_module.to_centi(-2.0),
                "offset_local_date": self.local_date,
                "offset_update_utc": BASE_NOW,
                "active_days": active_days,
                "valid_interactions": valid_interactions,
                "preferred_title": preferred_title,
                "title_opt_in": 1 if title_opt_in else 0,
                "title_enabled": 1 if title_enabled else 0,
            }
        )
        self.storage.mutate_user(user_key, lambda before: row)

    def insert_event(
        self,
        user_key: str = USER_KEY,
        *,
        status: str = "applied",
        category: str = "praise",
        delta: dict | None = None,
        event_key: str = "",
    ) -> None:
        """写入一条事件行，供事件查询与聚合使用。

        Args:
            user_key: 用户身份键。
            status: 事件状态。
            category: 事件分类。
            delta: 计分增量字典。
            event_key: 事件键；空串时自动生成。
        """

        seq = time.time_ns()
        now = int(time.time())
        local_date = self.clock.local_date(now)
        event = models_module.AffectEvent(
            event_key=event_key or f"evt-{user_key}-{seq}",
            user_key=user_key,
            bot_key=host_adapter.build_bot_key("qq", BOT_ACCOUNT),
            session_id="session-private",
            message_id=f"m-{seq}",
            event_time_utc=now,
            received_time_utc=now,
            local_date=local_date,
            classification=models_module.Classification(
                target=models_module.Target.BOT.value,
                event=category,
                confidence=0.95,
                reason_code="test",
            ),
            text_hash="hash",
            config_version="0.1.0",
        )
        self.storage.write_event(
            event,
            status=status,
            category=category,
            confidence=0.95,
            classifier="rules",
            reason_code="test",
            score_before={"mood": 60.0},
            delta={"mood": 2.0, "offset": 3.0, "affinity": 0.3, "suppressed": []}
            if delta is None
            else delta,
            score_after={"mood": 62.0},
        )


class IdentityParsingTest(AdminTestCase):
    """身份键解析与 QQ 号匹配。"""

    def test_describe_user_key_splits_four_parts(self) -> None:
        """合法身份键被拆成四段。"""

        parts = admin_api.describe_user_key(USER_KEY)
        self.assertEqual(parts["platform"], "qq")
        self.assertEqual(parts["bot_account_id"], BOT_ACCOUNT)
        self.assertEqual(parts["scope"], "")
        self.assertEqual(parts["user_id"], USER_ID)

    def test_describe_user_key_keeps_malformed_input(self) -> None:
        """格式非法时原样放进 user_id，不抛异常。"""

        parts = admin_api.describe_user_key("not-a-key")
        self.assertEqual(parts["user_id"], "not-a-key")
        self.assertEqual(parts["platform"], "")

    def test_resolve_full_key_passthrough(self) -> None:
        """完整身份键直接返回，不查库。"""

        self.assertEqual(admin_api.resolve_user_keys(self.context, USER_KEY), [USER_KEY])

    def test_resolve_by_qq_suffix(self) -> None:
        """纯 QQ 号通过身份键最后一段匹配。"""

        self.seed_user()
        self.seed_user(OTHER_USER_KEY)
        self.assertEqual(admin_api.resolve_user_keys(self.context, USER_ID), [USER_KEY])

    def test_resolve_unknown_returns_empty(self) -> None:
        """未登记的 QQ 号返回空列表。"""

        self.assertEqual(admin_api.resolve_user_keys(self.context, "99999"), [])


class AuthorizationTest(AdminTestCase):
    """管理通道鉴权（技术路线 §12）。"""

    def test_webui_channel_is_allowed(self) -> None:
        """本机 WebUI 通道已由 maibot_session 认证，直接放行。"""

        self.assertIsNone(admin_api.authorize_actor(self.context, via_channel="webui"))

    def test_qq_channel_requires_admin(self) -> None:
        """QQ 通道必须命中维护者列表。"""

        self.assertIsNone(
            admin_api.authorize_actor(self.context, via_channel="qq", actor_user_id=ADMIN_ID)
        )
        denied = admin_api.authorize_actor(self.context, via_channel="qq", actor_user_id=USER_ID)
        self.assertIsNotNone(denied)
        self.assertFalse(denied["ok"])

    def test_unknown_channel_is_denied(self) -> None:
        """未知通道一律拒绝，不能靠 API 名字拿到权限。"""

        denied = admin_api.authorize_actor(self.context, via_channel="", actor_user_id=ADMIN_ID)
        self.assertIsNotNone(denied)
        self.assertFalse(denied["ok"])

    def test_audit_actor_key_is_labelled(self) -> None:
        """审计标识带上通道前缀，便于区分来源。"""

        self.assertEqual(
            admin_api.audit_actor_key(via_channel="webui"), "webui:local"
        )
        self.assertEqual(
            admin_api.audit_actor_key(via_channel="qq", actor_user_id=ADMIN_ID),
            f"qq:{ADMIN_ID}",
        )


class GlobalStateTest(AdminTestCase):
    """全局状态查询。"""

    def test_read_global_state_shape(self) -> None:
        """返回心情、当日聚合、事件计数与审计总量。"""

        state = admin_api.read_global_state(self.context)
        self.assertTrue(state["ok"])
        self.assertEqual(state["local_date"], self.clock.local_date(self.clock.now_utc()))
        self.assertAlmostEqual(float(state["mood"]), 60.0)
        self.assertIsNone(state["stored_mood"])
        self.assertIn("total_events", state["aggregate"])
        self.assertIn("audit_total", state["aggregate"])
        self.assertEqual(state["suppressed_today"], 0)
        self.assertTrue(state["enabled"])
        self.assertFalse(state["shadow_mode"])

    def test_read_global_state_counts_suppressed(self) -> None:
        """delta 里 suppressed 非空的事件计入被抑制数。"""

        self.insert_event(delta={"suppressed": ["positive_f_daily_cap"]})
        self.assertEqual(admin_api.read_global_state(self.context)["suppressed_today"], 1)


class UserStateTest(AdminTestCase):
    """单用户状态查询。"""

    def test_unknown_user_reports_error(self) -> None:
        """找不到用户时返回 ok=False，不抛异常。"""

        state = admin_api.read_user_state(self.context, "88888")
        self.assertFalse(state["ok"])
        self.assertIn("找不到用户", state["error"])

    def test_user_state_fields(self) -> None:
        """返回 F/D/E、分段、当日预算与最近事件。"""

        self.seed_user(affinity=42.0, active_days=5, valid_interactions=20)
        self.insert_event()
        state = admin_api.read_user_state(self.context, USER_ID)
        self.assertTrue(state["ok"])
        self.assertEqual(state["user_key"], USER_KEY)
        self.assertAlmostEqual(state["affinity"], 42.0)
        self.assertEqual(state["affinity_level"], "familiar")
        self.assertIn("mood_level", state)
        self.assertIn("effective", state)
        self.assertIn("reaction", state)
        self.assertEqual(state["daily"]["valid_count"], 0)
        self.assertEqual(len(state["recent_events"]), 1)
        self.assertEqual(state["identity"]["user_id"], USER_ID)

    def test_ambiguous_identifier_lists_candidates(self) -> None:
        """同一 QQ 号对应多个身份键时要求补全。"""

        self.seed_user()
        self.seed_user(f"qq\x1f{BOT_ACCOUNT}\x1fgroup:1\x1f{USER_ID}")
        state = admin_api.read_user_state(self.context, USER_ID)
        self.assertFalse(state["ok"])
        self.assertEqual(len(state["candidates"]), 2)


class ListUsersTest(AdminTestCase):
    """用户分页列表。"""

    def test_paging_and_ordering(self) -> None:
        """按好感降序分页，并给出总页数。"""

        self.seed_user(USER_KEY, affinity=10.0)
        self.seed_user(OTHER_USER_KEY, affinity=70.0)
        page = admin_api.list_users(self.context, page=1, page_size=1)
        self.assertTrue(page["ok"])
        self.assertEqual(page["total"], 2)
        self.assertEqual(page["pages"], 2)
        self.assertEqual(page["items"][0]["user_key"], OTHER_USER_KEY)
        self.assertAlmostEqual(page["items"][0]["affinity"], 70.0)

        second = admin_api.list_users(self.context, page=2, page_size=1)
        self.assertEqual(second["items"][0]["user_key"], USER_KEY)

    def test_page_size_is_clamped(self) -> None:
        """每页条数被限制在 1..50，避免一次拉爆结果上限。"""

        self.assertEqual(admin_api.list_users(self.context, page=1, page_size=500)["page_size"], 50)
        self.assertEqual(admin_api.list_users(self.context, page=1, page_size=0)["page_size"], 1)
        self.assertEqual(admin_api.list_users(self.context, page=0)["page"], 1)

    def test_out_of_range_page_falls_back_to_last_page(self) -> None:
        """总数变少时页码回落到最后一页，避免前端分页组件拒绝渲染。"""

        self.seed_user(USER_KEY, affinity=10.0)
        page = admin_api.list_users(self.context, page=5, page_size=1)
        self.assertTrue(page["ok"])
        self.assertEqual(page["pages"], 1)
        self.assertEqual(page["page"], 1)
        self.assertEqual(len(page["items"]), 1)
        self.assertEqual(page["items"][0]["user_key"], USER_KEY)

    def test_empty_result_keeps_first_page(self) -> None:
        """没有任何用户时仍是合法分页（pages=1、page=1、total=0）。"""

        page = admin_api.list_users(self.context)
        self.assertEqual(page["pages"], 1)
        self.assertEqual(page["page"], 1)
        self.assertEqual(page["total"], 0)
        self.assertEqual(page["items"], [])


class RecentEventsTest(AdminTestCase):
    """事件查询。"""

    def test_cross_user_query(self) -> None:
        """空标识时不筛用户。"""

        self.seed_user()
        self.insert_event()
        result = admin_api.recent_events(self.context)
        self.assertTrue(result["ok"])
        self.assertEqual(result["user_key"], "")
        self.assertEqual(len(result["items"]), 1)
        self.assertIn("user_id", result["items"][0])

    def test_unknown_user_reports_error(self) -> None:
        """标识查不到用户时返回错误。"""

        result = admin_api.recent_events(self.context, identifier="77777")
        self.assertFalse(result["ok"])


class AdjustAffinityTest(AdminTestCase):
    """好感调整与审计。"""

    def test_set_mode_replaces_value(self) -> None:
        """set 模式把 F 直接设为目标值。"""

        self.seed_user(affinity=30.0)
        result = admin_api.adjust_affinity(self.context, identifier=USER_ID, value=88.0)
        self.assertTrue(result["ok"])
        self.assertAlmostEqual(result["before_affinity"], 30.0)
        self.assertAlmostEqual(result["after_affinity"], 88.0)
        self.assertEqual(
            self.storage.load_user(USER_KEY, self.engine_config).affinity_centi,
            engine_module.to_centi(88.0),
        )

    def test_delta_mode_accumulates(self) -> None:
        """delta 模式在现有值上累加。"""

        self.seed_user(affinity=30.0)
        result = admin_api.adjust_affinity(
            self.context, identifier=USER_ID, value=5.5, mode="delta"
        )
        self.assertTrue(result["ok"])
        self.assertAlmostEqual(result["after_affinity"], 35.5)

    def test_delta_is_clamped(self) -> None:
        """累加结果被夹到 0..100。"""

        self.seed_user(affinity=99.0)
        result = admin_api.adjust_affinity(
            self.context, identifier=USER_ID, value=50.0, mode="delta"
        )
        self.assertAlmostEqual(result["after_affinity"], 100.0)

    def test_rejects_out_of_range_value(self) -> None:
        """超出 -100..100 的值被拒绝且不写库。"""

        self.seed_user(affinity=30.0)
        result = admin_api.adjust_affinity(self.context, identifier=USER_ID, value=150.0)
        self.assertFalse(result["ok"])
        self.assertEqual(
            self.storage.load_user(USER_KEY, self.engine_config).affinity_centi,
            engine_module.to_centi(30.0),
        )

    def test_rejects_non_numeric_value(self) -> None:
        """非数字被拒绝。"""

        self.seed_user()
        result = admin_api.adjust_affinity(self.context, identifier=USER_ID, value="ab" )  # type: ignore[arg-type]
        self.assertFalse(result["ok"])

    def test_rejects_unknown_mode(self) -> None:
        """未知模式被拒绝。"""

        self.seed_user()
        result = admin_api.adjust_affinity(
            self.context, identifier=USER_ID, value=10.0, mode="multiply"
        )
        self.assertFalse(result["ok"])

    def test_writes_audit_row(self) -> None:
        """每次调整都留下一行审计。"""

        self.seed_user(affinity=30.0)
        admin_api.adjust_affinity(self.context, identifier=USER_ID, value=50.0, reason="验收")
        audit = self.storage.list_audit(limit=5)
        self.assertEqual(len(audit), 1)
        self.assertEqual(audit[0]["operation"], "adjust_affinity:set")
        self.assertEqual(audit[0]["actor_key"], "webui:local")
        self.assertEqual(audit[0]["target_key"], USER_KEY)
        self.assertAlmostEqual(audit[0]["after"]["affinity_centi"], 50.0)
        self.assertEqual(audit[0]["after"]["reason"], "验收")

    def test_qq_channel_denied_for_non_admin(self) -> None:
        """非维护者通过 QQ 通道调用被拒绝且不写审计。"""

        self.seed_user()
        result = admin_api.adjust_affinity(
            self.context,
            identifier=USER_ID,
            value=99.0,
            via_channel="qq",
            actor_user_id=USER_ID,
        )
        self.assertFalse(result["ok"])
        self.assertEqual(self.storage.list_audit(limit=5), [])

    def test_creates_missing_row(self) -> None:
        """用户此前不存在时，调整会创建一行并写审计。"""

        result = admin_api.adjust_affinity(self.context, identifier=OTHER_USER_KEY, value=40.0)
        self.assertTrue(result["ok"])
        self.assertEqual(len(self.storage.list_audit(limit=5)), 1)
        self.assertAlmostEqual(
            admin_api.read_user_state(self.context, OTHER_USER_KEY)["affinity"], 40.0
        )


class ResetUserTest(AdminTestCase):
    """用户重置。"""

    def test_reset_clears_offset_and_counters(self) -> None:
        """重置把 D、活跃日、交流次数清零，并把 F 打回初始值。"""

        self.seed_user(affinity=70.0, active_days=9, valid_interactions=50)
        result = admin_api.reset_user(self.context, identifier=USER_ID)
        self.assertTrue(result["ok"])
        state = self.storage.load_user(USER_KEY, self.engine_config)
        self.assertEqual(state.affinity_centi, self.engine_config.initial_affinity_centi)
        self.assertEqual(state.offset_centi, 0)
        self.assertEqual(state.active_days, 0)
        self.assertEqual(state.valid_interactions, 0)
        self.assertEqual(state.reaction, models_module.Reaction.NONE.value)

    def test_reset_with_explicit_affinity(self) -> None:
        """可以指定重置后的具体好感。"""

        self.seed_user(affinity=70.0)
        result = admin_api.reset_user(self.context, identifier=USER_ID, affinity=25.0)
        self.assertTrue(result["ok"])
        self.assertAlmostEqual(result["affinity"], 25.0)

    def test_reset_rejects_out_of_range(self) -> None:
        """大于 100 的重置值被拒绝。"""

        self.seed_user()
        self.assertFalse(admin_api.reset_user(self.context, identifier=USER_ID, affinity=120.0)["ok"])

    def test_reset_writes_audit(self) -> None:
        """重置留下审计行。"""

        self.seed_user()
        admin_api.reset_user(self.context, identifier=USER_ID)
        audit = self.storage.list_audit(limit=5)
        self.assertEqual(audit[0]["operation"], "reset_user")


class TitleAdminTest(AdminTestCase):
    """称呼相关管理动作。"""

    def test_revoke_title_only_clears_flags(self) -> None:
        """撤销称呼不扣分，只清标志与偏好词。"""

        self.seed_user(affinity=90.0, title_opt_in=True, preferred_title="哥哥", title_enabled=True)
        result = admin_api.revoke_title(self.context, identifier=USER_ID)
        self.assertTrue(result["ok"])
        state = self.storage.load_user(USER_KEY, self.engine_config)
        self.assertEqual(state.affinity_centi, engine_module.to_centi(90.0))
        self.assertFalse(state.title_enabled)
        self.assertFalse(state.title_opt_in)
        self.assertEqual(state.preferred_title, "")

    def test_set_title_opt_in(self) -> None:
        """设置称呼意愿与偏好词。"""

        self.seed_user()
        result = admin_api.set_title_opt_in(
            self.context, identifier=USER_ID, enabled=True, title="小主人"
        )
        self.assertTrue(result["ok"])
        state = self.storage.load_user(USER_KEY, self.engine_config)
        self.assertTrue(state.title_opt_in)
        self.assertEqual(state.preferred_title, "小主人")
        self.assertFalse(state.title_enabled)

    def test_set_title_rejects_long_title(self) -> None:
        """称呼词过长被拒绝。"""

        self.seed_user()
        result = admin_api.set_title_opt_in(
            self.context, identifier=USER_ID, enabled=True, title="称" * 20
        )
        self.assertFalse(result["ok"])


class DeleteUserTest(AdminTestCase):
    """删除用户状态。"""

    def test_delete_removes_rows_and_keeps_audit(self) -> None:
        """删除后查不到用户，事件与当日统计一并清空，审计保留。"""

        self.seed_user()
        self.insert_event()
        result = admin_api.delete_user(self.context, identifier=USER_ID, reason="验收后清理")
        self.assertTrue(result["ok"])
        self.assertEqual(result["removed"]["user_relation"], 1)
        self.assertEqual(admin_api.recent_events(self.context)["items"], [])
        self.assertEqual(admin_api.list_users(self.context)["total"], 0)
        self.assertEqual(len(self.storage.list_audit(limit=5)), 1)

    def test_delete_unknown_user(self) -> None:
        """删除不存在的用户返回错误。"""

        self.assertFalse(admin_api.delete_user(self.context, identifier="66666")["ok"])

    def test_delete_does_not_touch_global_mood(self) -> None:
        """全局心情是共享状态，不随单用户删除而消失。"""

        bot_key = host_adapter.build_bot_key("qq", BOT_ACCOUNT)
        self.storage.save_mood(
            models_module.MoodState(
                bot_key=bot_key,
                local_date=self.local_date,
                mood_centi=7000,
                last_update_utc=BASE_NOW,
            )
        )
        self.seed_user()
        admin_api.delete_user(self.context, identifier=USER_ID)
        self.assertIsNotNone(self.storage.load_mood(bot_key))


class BackupTest(AdminTestCase):
    """数据库备份。"""

    def test_backup_creates_file(self) -> None:
        """备份生成可读的 SQLite 文件，并写审计。"""

        self.seed_user()
        target = self.tmpdir / "backups"
        result = admin_api.backup_database(self.context, target_dir=str(target))
        self.assertTrue(result["ok"])
        path = Path(result["path"])
        self.assertTrue(path.is_file())
        self.assertGreater(result["size_bytes"], 0)
        self.assertEqual(path.parent, target)
        self.assertEqual(self.storage.list_audit(limit=5)[0]["operation"], "backup_database")

    def test_backup_default_directory(self) -> None:
        """未指定目录时写到数据库同级的 backups 子目录。"""

        result = admin_api.backup_database(self.context)
        self.assertTrue(result["ok"])
        self.assertEqual(Path(result["path"]).parent, self.tmpdir / "backups")


class ExportAndAuditTest(AdminTestCase):
    """导出与审计读取。"""

    def test_export_contains_state_and_events(self) -> None:
        """导出包含状态、当日统计与事件，且不含消息原文。"""

        self.seed_user()
        self.insert_event()
        result = admin_api.export_user(self.context, USER_ID)
        self.assertTrue(result["ok"])
        self.assertIn("state", result)
        self.assertIn("daily", result)
        self.assertEqual(len(result["events"]), 1)
        dumped = str(result)
        self.assertNotIn("processed_plain_text", dumped)

    def test_export_unknown_user(self) -> None:
        """导出不存在的用户返回错误。"""

        self.assertFalse(admin_api.export_user(self.context, "55555")["ok"])

    def test_list_audit_empty(self) -> None:
        """没有管理动作时审计为空。"""

        result = admin_api.list_audit(self.context)
        self.assertTrue(result["ok"])
        self.assertEqual(result["items"], [])

    def test_list_audit_limit_clamped(self) -> None:
        """审计条数被限制在 1..200。"""

        self.seed_user()
        for index in range(3):
            admin_api.adjust_affinity(
                self.context, identifier=USER_ID, value=float(index + 10), reason=f"r{index}"
            )
        self.assertEqual(len(admin_api.list_audit(self.context, limit=0)["items"]), 1)
        self.assertEqual(len(admin_api.list_audit(self.context, limit=2)["items"]), 2)


if __name__ == "__main__":
    unittest.main()
