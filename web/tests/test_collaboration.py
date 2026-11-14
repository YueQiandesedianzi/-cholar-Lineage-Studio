# -*- coding: utf-8 -*-
"""ReviewGit 协作与版本 API 的真实 SQLite/Flask 集成测试。

测试只通过 Flask test_client 和既有 legacy API 建立文献、真实摘录卡片，
不启动服务器，也不以 Mock 替代协作服务。app 只在类初始化时动态导入一次；
导入前 REVIEWGIT_DB 已指向类级临时目录，每个 test 再使用独立临时子目录，
重设 app.DB_PATH、关闭 Flask 旧连接并执行 app.init_db()，因此不会访问正式库。
类结束时恢复原环境变量和 app 模块；每个临时数据库随测试清理。

首次协作 API 查询负责从 main live cards 根引导。断言使用紧凑、键排序、
保留 Unicode 的 JSON SHA-256，覆盖完整快照、双家长合并、审核时效、
活跃冲突、冲突解决、tag 不可变性、追加式 revert 和 verified 摘录校验。
新卡同步、非 main 编辑及删除单元的额外三边细则尚无更明确契约，不在此
臆测实现。运行前需由应用集成方提供 collaboration.py 并接入 app.py。
"""

import hashlib
import importlib
import json
import os
import sys
import tempfile
import unittest


WEB_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_PATH = os.path.join(WEB_DIR, "app.py")
_MISSING = object()


def canonical_hash(content):
    """Return the specified content hash without ASCII-escaping Unicode."""
    encoded = json.dumps(
        content, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class CollaborationApiTest(unittest.TestCase):
    """Exercise the complete collaboration API against isolated real databases."""

    @classmethod
    def setUpClass(cls):
        sys.dont_write_bytecode = True
        cls._temporary_root = tempfile.TemporaryDirectory(
            prefix="reviewgit-collaboration-tests-"
        )
        cls._previous_db_env = os.environ.get("REVIEWGIT_DB", _MISSING)
        cls._previous_app_module = sys.modules.pop("app", _MISSING)
        cls._added_web_dir = str(WEB_DIR) not in sys.path
        if cls._added_web_dir:
            sys.path.insert(0, str(WEB_DIR))
        os.environ["REVIEWGIT_DB"] = os.path.join(
            cls._temporary_root.name, "import.sqlite3"
        )
        try:
            # Canonical import name also lets collaboration.py import this same app.
            cls.app_module = importlib.import_module("app")
            cls.app_module.app.config.update(TESTING=True)
        except Exception:
            cls._restore_environment()
            raise

    @classmethod
    def _restore_environment(cls):
        app_module = getattr(cls, "app_module", None)
        if app_module is not None:
            with app_module.app.app_context():
                app_module.close_db()
        sys.modules.pop("app", None)
        if cls._previous_app_module is not _MISSING:
            sys.modules["app"] = cls._previous_app_module
        if cls._previous_db_env is _MISSING:
            os.environ.pop("REVIEWGIT_DB", None)
        else:
            os.environ["REVIEWGIT_DB"] = cls._previous_db_env
        if cls._added_web_dir:
            try:
                sys.path.remove(str(WEB_DIR))
            except ValueError:
                pass
        cls._temporary_root.cleanup()

    @classmethod
    def tearDownClass(cls):
        cls._restore_environment()

    def setUp(self):
        self._close_old_connection()
        self._test_directory = tempfile.TemporaryDirectory(
            prefix="case-", dir=self._temporary_root.name
        )
        self.db_path = os.path.join(self._test_directory.name, "test.sqlite3")
        os.environ["REVIEWGIT_DB"] = self.db_path
        # DB_PATH is read at import time by the legacy app, so reset it explicitly.
        self.app_module.DB_PATH = self.db_path
        self.app_module.init_db()
        self.client = self.app_module.app.test_client()
        self._sequence = 0
        formal_db = os.path.join(WEB_DIR, "reviewgit.db")
        self.assertNotEqual(os.path.abspath(self.db_path), os.path.abspath(formal_db))
        self.assertEqual(
            os.path.commonpath(
                [os.path.abspath(self.db_path), os.path.abspath(self._temporary_root.name)]
            ),
            os.path.abspath(self._temporary_root.name),
        )

    def tearDown(self):
        self._close_old_connection()
        self._test_directory.cleanup()

    @classmethod
    def _close_old_connection(cls):
        app_module = getattr(cls, "app_module", None)
        if app_module is not None:
            with app_module.app.app_context():
                app_module.close_db()

    def next_sequence(self):
        self._sequence += 1
        return self._sequence

    @staticmethod
    def statuses(expected):
        return expected if isinstance(expected, tuple) else (expected,)

    def api_json(self, method, path, expected=200, **kwargs):
        response = self.client.open(path, method=method, **kwargs)
        body_text = response.get_data(as_text=True)
        self.assertIn(
            response.status_code,
            self.statuses(expected),
            "%s %s returned %s: %s"
            % (method, path, response.status_code, body_text),
        )
        body = response.get_json(silent=True)
        self.assertIsNotNone(
            body, "%s %s did not return JSON: %s" % (method, path, body_text)
        )
        return body

    def assert_error(self, response, expected=400):
        body_text = response.get_data(as_text=True)
        self.assertIn(
            response.status_code,
            self.statuses(expected),
            "expected an API error, got %s: %s"
            % (response.status_code, body_text),
        )
        body = response.get_json(silent=True)
        self.assertIsInstance(body, dict, body_text)
        self.assertIn("error", body)
        self.assertIsInstance(body["error"], str)
        self.assertTrue(body["error"].strip())
        return body

    def entity_id(self, payload, kind):
        self.assertIsInstance(payload, dict)
        key = kind + "_id"
        if key in payload:
            return payload[key]
        self.assertIn("id", payload, payload)
        return payload["id"]

    def raw_id(self, record):
        if isinstance(record, int):
            return record
        if "id" in record:
            return record["id"]
        for key in (
            "branch_id",
            "commit_id",
            "mr_id",
            "conflict_id",
            "tag_id",
        ):
            if key in record:
                return record[key]
        self.fail("record has no id: %r" % (record,))

    # ---------- Real legacy fixtures and reusable collaboration operations ----------

    def make_card(self, excerpt, claim=None, verified=True):
        """Import one paper, extract exactly one card, then verify/revise via API."""
        sequence = self.next_sequence()
        template = self.api_json(
            "POST",
            "/api/templates",
            expected=201,
            json={"name": "测试模板-%d" % sequence, "fields": ["研究问题"]},
        )
        paper_id = self.entity_id(
            self.api_json(
                "POST",
                "/api/papers",
                expected=201,
                json={
                    "title": "协作测试文献-%d" % sequence,
                    "authors": "测试作者",
                    "year": "2026",
                    "abstract": excerpt,
                },
            ),
            "paper",
        )
        extraction = self.api_json(
            "POST",
            "/api/extract",
            expected=200,
            json={"paper_id": paper_id, "template_id": self.entity_id(template, "template")},
        )
        self.assertEqual(extraction["created"], 1)
        cards = self.api_json("GET", "/api/cards?paper_id=%d" % paper_id)
        self.assertEqual(len(cards), 1)
        card = cards[0]
        self.assertIn(card["excerpt"], excerpt)
        paper = next(
            p for p in self.api_json("GET", "/api/papers") if p["id"] == paper_id
        )
        self.assertIn(card["excerpt"], paper["full_text"])

        if claim is not None:
            self.assertTrue(verified, "a revised claim is verified by the legacy API")
            self.api_json(
                "POST",
                "/api/cards/%d/verify" % card["id"],
                json={"action": "revise", "claim": claim},
            )
        elif verified:
            self.api_json(
                "POST",
                "/api/cards/%d/verify" % card["id"],
                json={"action": "approve"},
            )
        return self.live_card(card["id"])

    def live_card(self, card_or_id):
        card_id = self.raw_id(card_or_id)
        cards = self.api_json("GET", "/api/cards")
        matches = [card for card in cards if card["id"] == card_id]
        self.assertEqual(len(matches), 1)
        return matches[0]

    def main(self):
        branches = self.api_json("GET", "/api/branches")
        matches = [branch for branch in branches if branch.get("name") == "main"]
        self.assertEqual(len(matches), 1)
        return matches[0]

    def get_branch(self, branch):
        return self.api_json(
            "GET", "/api/branches/%d" % self.raw_id(branch)
        )

    def branch(self, name, kind="topic", owner="alice", purpose="test", base=None):
        payload = {"name": name, "kind": kind, "owner": owner, "purpose": purpose}
        if base is not None:
            payload["base_commit_id"] = self.raw_id(base)
        created = self.api_json(
            "POST", "/api/branches", expected=201, json=payload
        )
        return self.get_branch(self.entity_id(created, "branch"))

    def branch_head(self, branch):
        # A returned branch object may be stale after later commits; always reload.
        detail = self.get_branch(branch)
        self.assertIn("head_commit_id", detail)
        return detail["head_commit_id"]

    def claim_key(self, card):
        return "card_attr:%d:claim" % self.raw_id(card)

    def attr_key(self, card, attr):
        return "card_attr:%d:%s" % (self.raw_id(card), attr)

    def unit_change(self, card, attr, content, **extra):
        change = {
            "unit_type": "card_attr",
            "unit_key": self.attr_key(card, attr),
            "new_content": content,
        }
        change.update(extra)
        return change

    def claim_change(self, card, content, **extra):
        return self.unit_change(card, "claim", content, **extra)

    def get_commit(self, commit):
        body = self.api_json("GET", "/api/commits/%d" % self.raw_id(commit))
        self.assertIn("changes", body)
        self.assertIn("snapshot", body)
        self.assertIsInstance(body["changes"], list)
        self.assertIsInstance(body["snapshot"], dict)
        return body

    def commit(self, branch, message, changes, author="alice"):
        created = self.api_json(
            "POST",
            "/api/branches/%d/commits" % self.raw_id(branch),
            expected=201,
            json={"author": author, "message": message, "changes": changes},
        )
        return self.get_commit(self.entity_id(created, "commit"))

    def get_mr(self, mr):
        return self.api_json(
            "GET", "/api/merge-requests/%d" % self.raw_id(mr)
        )

    def open_mr(
        self,
        source,
        target=None,
        author="alice",
        title="merge topic",
        required_approvals=None,
    ):
        payload = {
            "source_branch_id": self.raw_id(source),
            "target_branch_id": self.raw_id(target if target is not None else self.main()),
            "author": author,
            "title": title,
        }
        if required_approvals is not None:
            payload["required_approvals"] = required_approvals
        created = self.api_json(
            "POST", "/api/merge-requests", expected=201, json=payload
        )
        mr = self.get_mr(self.entity_id(created, "mr"))
        self.assertEqual(mr["source_branch_id"], payload["source_branch_id"])
        self.assertEqual(mr["target_branch_id"], payload["target_branch_id"])
        return mr

    def review(self, mr, reviewer, verdict="approve", comments=None):
        return self.api_json(
            "POST",
            "/api/merge-requests/%d/reviews" % self.raw_id(mr),
            expected=201,
            json={
                "reviewer": reviewer,
                "verdict": verdict,
                "comments": comments if comments is not None else [],
            },
        )

    def preview(self, mr, method="GET"):
        kwargs = {"json": {}} if method == "POST" else {}
        body = self.api_json(
            method,
            "/api/merge-requests/%d/preview" % self.raw_id(mr),
            **kwargs
        )
        self.assertIn("reviews", body)
        self.assertIn("conflicts", body)
        self.assertIn("preview", body)
        self.assertIsInstance(body["reviews"], list)
        self.assertIsInstance(body["conflicts"], list)
        return body

    def merge(self, mr, actor="merger"):
        body = self.api_json(
            "POST",
            "/api/merge-requests/%d/merge" % self.raw_id(mr),
            json={"actor": actor},
        )
        self.assertIn("merge_commit_id", body)
        return self.get_commit(body["merge_commit_id"])

    def assert_merge_error(self, mr, actor="merger", expected=409):
        response = self.client.post(
            "/api/merge-requests/%d/merge" % self.raw_id(mr),
            json={"actor": actor},
        )
        return self.assert_error(response, expected)

    def resolve(self, conflict, actor="resolver", resolution="keep_branch", manual=None):
        payload = {"actor": actor, "resolution": resolution}
        if resolution == "manual":
            payload["manual_content"] = manual
        return self.api_json(
            "POST",
            "/api/merge-requests/conflicts/%d/resolve" % self.raw_id(conflict),
            json=payload,
        )

    def assert_snapshot_content(self, snapshot, card, attr, content):
        self.assertIsInstance(snapshot, dict)
        key = self.attr_key(card, attr)
        self.assertIn(key, snapshot)
        unit = snapshot[key]
        self.assertIsInstance(unit, dict)
        self.assertEqual(unit["unit_type"], "card_attr")
        self.assertEqual(unit["content"], content)
        self.assertEqual(unit["hash"], canonical_hash(content))

    def assert_live(self, card, attr, expected):
        actual = self.live_card(card)
        self.assertEqual(actual[attr], expected)
        return actual

    def assert_merge_parents(self, merge_commit, target_parent, source_parent):
        parents = merge_commit.get("parent_commit_ids")
        self.assertIsInstance(parents, list)
        self.assertEqual(len(parents), 2)
        self.assertEqual(
            set(parents), {self.raw_id(target_parent), self.raw_id(source_parent)}
        )

    def assert_merge_audit(self):
        logs = self.api_json("GET", "/api/audit-logs")
        self.assertIsInstance(logs, list)
        self.assertTrue(logs)
        self.assertTrue(
            any("merge" in str(log.get("action", "")).lower() for log in logs),
            logs,
        )
        return logs

    def prepare_conflict(self, include_unrelated_target_change=True):
        """Create sibling A/B at root, merge A, then refresh B's active conflict."""
        card = self.make_card(
            "The original claim is supported by this exact sentence.",
            claim="original claim",
        )
        other = self.make_card(
            "A second independent sentence supports the other field.",
            claim="other original",
        )
        root = self.main()
        root_id = self.branch_head(root)
        target_branch = self.branch(
            "target-side", owner="target-owner", purpose="advance target"
        )
        source_branch = self.branch(
            "source-side", owner="source-owner", purpose="advance source"
        )
        target_changes = [self.claim_change(card, "target claim")]
        if include_unrelated_target_change:
            target_changes.append(self.claim_change(other, "target unrelated"))
        target_commit = self.commit(
            target_branch, "target changes", target_changes, author="target-author"
        )
        source_commit = self.commit(
            source_branch,
            "source changes",
            [self.claim_change(card, "source claim")],
            author="source-author",
        )
        # Both siblings really start at root, not at the other sibling's commit.
        self.assertEqual(target_commit["parent_commit_id"], root_id)
        self.assertEqual(source_commit["parent_commit_id"], root_id)
        self.assertEqual(self.get_branch(source_branch)["base_commit_id"], root_id)

        target_mr = self.open_mr(
            target_branch, author="target-author", title="target MR"
        )
        source_mr = self.open_mr(
            source_branch, author="source-author", title="source MR"
        )
        self.review(target_mr, "target-reviewer")
        self.review(source_mr, "source-reviewer")
        target_merge = self.merge(target_mr)
        self.assert_live(card, "claim", "target claim")
        if include_unrelated_target_change:
            self.assert_live(other, "claim", "target unrelated")

        # A GET detail and an explicit preview refresh must preserve, not erase, B's conflict.
        self.assertEqual(self.raw_id(self.get_mr(source_mr)), self.raw_id(source_mr))
        refreshed = self.preview(source_mr)
        active = [
            conflict
            for conflict in refreshed["conflicts"]
            if conflict.get("resolution") is None
        ]
        self.assertEqual(len(active), 1, refreshed)
        conflict = active[0]
        self.assertEqual(conflict["unit_key"], self.claim_key(card))
        self.assertEqual(conflict["base_hash"], canonical_hash("original claim"))
        self.assertEqual(conflict["ours_hash"], canonical_hash("target claim"))
        self.assertEqual(conflict["theirs_hash"], canonical_hash("source claim"))
        self.assertIsInstance(refreshed["preview"], (dict, list))
        self.assertTrue(
            any(review.get("reviewer") == "source-reviewer" for review in refreshed["reviews"])
        )
        return {
            "card": card,
            "other": other,
            "root": root,
            "target_branch": target_branch,
            "source_branch": source_branch,
            "target_commit": target_commit,
            "source_commit": source_commit,
            "target_mr": target_mr,
            "source_mr": source_mr,
            "target_merge": target_merge,
            "conflict": conflict,
        }

    # ---------- Branch bootstrap and commit validation ----------

    def test_bootstrap_is_idempotent_and_branch_metadata_uses_main_head(self):
        card = self.make_card(
            "Exact evidence for the immutable root snapshot.", claim="root claim"
        )
        first = self.api_json("GET", "/api/branches")
        second = self.api_json("GET", "/api/branches")
        self.assertEqual(first, second)
        self.assertEqual(len([b for b in first if b["name"] == "main"]), 1)
        main = next(b for b in first if b["name"] == "main")
        root_id = self.branch_head(main)
        self.assertIsNotNone(root_id)
        root = self.get_commit(root_id)
        self.assert_snapshot_content(root["snapshot"], card, "claim", "root claim")

        main_detail = self.get_branch(main)
        self.assertIn("units", main_detail)
        self.assert_snapshot_content(main_detail["units"], card, "claim", "root claim")
        topic = self.branch(
            "literature-topic",
            kind="topic",
            owner="owner-7",
            purpose="check one paper",
        )
        self.assertEqual(topic["name"], "literature-topic")
        self.assertEqual(topic["kind"], "topic")
        self.assertEqual(topic["owner"], "owner-7")
        self.assertEqual(topic["purpose"], "check one paper")
        self.assertEqual(topic["base_commit_id"], root_id)
        self.assertEqual(topic["head_commit_id"], root_id)
        after_create = self.api_json("GET", "/api/branches")
        self.assertEqual(after_create, self.api_json("GET", "/api/branches"))

        duplicate = self.client.post(
            "/api/branches",
            json={
                "name": "literature-topic",
                "kind": "person",
                "owner": "other-owner",
                "purpose": "duplicate",
            },
        )
        self.assert_error(duplicate, expected=(400, 409))
        self.assertEqual(after_create, self.api_json("GET", "/api/branches"))

    def test_invalid_commits_never_advance_head_or_history(self):
        card = self.make_card(
            "A real sentence anchors invalid commit tests.", claim="baseline"
        )
        topic = self.branch("invalid-commits")
        original_head = self.branch_head(topic)
        history_path = "/api/branches/%d/commits" % self.raw_id(topic)
        original_history = self.api_json("GET", history_path)
        valid_change = self.claim_change(card, "valid-looking change")
        cases = [
            (
                "missing message",
                {"author": "alice", "changes": [valid_change]},
                400,
            ),
            (
                "empty changes",
                {"author": "alice", "message": "empty", "changes": []},
                400,
            ),
            (
                "no-op",
                {
                    "author": "alice",
                    "message": "same value",
                    "changes": [self.claim_change(card, "baseline")],
                },
                400,
            ),
            (
                "unit key and type mismatch",
                {
                    "author": "alice",
                    "message": "wrong type",
                    "changes": [
                        {
                            "unit_type": "card_field",
                            "unit_key": self.claim_key(card),
                            "new_content": "wrong type",
                        }
                    ],
                },
                400,
            ),
            (
                "stale old hash",
                {
                    "author": "alice",
                    "message": "stale base",
                    "changes": [
                        self.claim_change(
                            card, "stale write", old_hash="0" * 64
                        )
                    ],
                },
                (400, 409),
            ),
        ]
        for label, payload, expected_status in cases:
            with self.subTest(label=label):
                response = self.client.post(
                    "/api/branches/%d/commits" % self.raw_id(topic), json=payload
                )
                self.assert_error(response, expected_status)
                self.assertEqual(self.branch_head(topic), original_head)
                self.assertEqual(
                    self.api_json("GET", history_path), original_history
                )
                self.assert_live(card, "claim", "baseline")

    def test_real_excerpt_baseline_and_branch_commit_are_isolated(self):
        excerpt = "The measured value remained stable throughout this real experiment."
        card = self.make_card(excerpt, claim="main claim")
        main = self.main()
        old_main_head = self.branch_head(main)
        topic = self.branch("isolated-edit", owner="bob", purpose="draft only")
        root_snapshot = self.get_commit(old_main_head)["snapshot"]
        old_hash = root_snapshot[self.claim_key(card)]["hash"]
        created = self.commit(
            topic,
            "draft a Unicode claim",
            [
                self.claim_change(
                    card,
                    "分支论断-Δ",
                    old_hash=old_hash,
                    old_content="main claim",
                )
            ],
            author="bob",
        )
        self.assertEqual(self.branch_head(topic), self.raw_id(created))
        self.assertEqual(self.branch_head(main), old_main_head)
        self.assert_live(card, "claim", "main claim")
        self.assert_snapshot_content(created["snapshot"], card, "claim", "分支论断-Δ")
        self.assertEqual(created["parent_commit_id"], old_main_head)
        changed = [
            change
            for change in created["changes"]
            if change["unit_key"] == self.claim_key(card)
        ]
        self.assertEqual(len(changed), 1)
        self.assertEqual(changed[0]["new_hash"], canonical_hash("分支论断-Δ"))
        history = self.api_json(
            "GET", "/api/branches/%d/commits" % self.raw_id(topic)
        )
        self.assertIn(self.raw_id(created), [self.raw_id(c) for c in history])

    # ---------- Three-way preview, non-conflicts, and resolutions ----------

    def test_unrelated_units_merge_cleanly_after_sibling_target_merge(self):
        first = self.make_card(
            "First exact source sentence for independent edits.", claim="first base"
        )
        second = self.make_card(
            "Second exact source sentence for independent edits.", claim="second base"
        )
        root_id = self.branch_head(self.main())
        left = self.branch("independent-left")
        right = self.branch("independent-right")
        left_commit = self.commit(
            left, "left", [self.claim_change(first, "left result")], "left-author"
        )
        right_commit = self.commit(
            right, "right", [self.claim_change(second, "right result")], "right-author"
        )
        self.assertEqual(left_commit["parent_commit_id"], root_id)
        self.assertEqual(right_commit["parent_commit_id"], root_id)
        left_mr = self.open_mr(left, author="left-author")
        right_mr = self.open_mr(right, author="right-author")
        self.review(left_mr, "reviewer-one")
        self.review(right_mr, "reviewer-two")
        left_merge = self.merge(left_mr)
        preview = self.preview(right_mr, method="POST")
        self.assertEqual(preview["conflicts"], [])
        right_merge = self.merge(right_mr)
        self.assert_merge_parents(right_merge, left_merge, right_commit)
        self.assert_live(first, "claim", "left result")
        self.assert_live(second, "claim", "right result")
        self.assert_snapshot_content(
            right_merge["snapshot"], first, "claim", "left result"
        )
        self.assert_snapshot_content(
            right_merge["snapshot"], second, "claim", "right result"
        )
        self.assertEqual(self.branch_head(right), self.raw_id(right_commit))

    def test_identical_both_sided_value_is_not_a_conflict(self):
        card = self.make_card(
            "Both reviewers independently derive exactly the same claim.",
            claim="before consensus",
        )
        left = self.branch("same-left")
        right = self.branch("same-right")
        self.commit(left, "left consensus", [self.claim_change(card, "consensus")], "a")
        right_commit = self.commit(
            right, "right consensus", [self.claim_change(card, "consensus")], "b"
        )
        left_mr = self.open_mr(left, author="a")
        right_mr = self.open_mr(right, author="b")
        self.review(left_mr, "reviewer-a")
        self.review(right_mr, "reviewer-b")
        left_merge = self.merge(left_mr)
        self.assertEqual(self.preview(right_mr)["conflicts"], [])
        right_merge = self.merge(right_mr)
        self.assert_merge_parents(right_merge, left_merge, right_commit)
        self.assert_live(card, "claim", "consensus")
        self.assert_snapshot_content(right_merge["snapshot"], card, "claim", "consensus")

    def test_keep_branch_resolution_preserves_unrelated_target_side(self):
        data = self.prepare_conflict(include_unrelated_target_change=True)
        card, other = data["card"], data["other"]
        source_mr = data["source_mr"]
        main_head_before = self.branch_head(self.main())
        self.assert_merge_error(source_mr)
        self.assertEqual(self.branch_head(self.main()), main_head_before)
        self.assert_live(card, "claim", "target claim")

        self.resolve(data["conflict"], resolution="keep_branch")
        resolved = self.preview(source_mr)["conflicts"]
        self.assertTrue(resolved)
        self.assertTrue(all(c["resolution"] is not None for c in resolved))
        self.assertEqual(self.branch_head(self.main()), main_head_before)
        self.assert_live(card, "claim", "target claim")

        merged = self.merge(source_mr)
        self.assert_merge_parents(merged, data["target_merge"], data["source_commit"])
        self.assertEqual(self.branch_head(data["source_branch"]), self.raw_id(data["source_commit"]))
        self.assert_live(card, "claim", "source claim")
        self.assert_live(other, "claim", "target unrelated")
        self.assert_snapshot_content(merged["snapshot"], card, "claim", "source claim")
        self.assert_snapshot_content(merged["snapshot"], other, "claim", "target unrelated")
        self.assertEqual(self.get_mr(source_mr)["status"], "merged")
        self.assert_merge_audit()

    def test_manual_resolution_uses_exact_unicode_content(self):
        data = self.prepare_conflict(include_unrelated_target_change=True)
        card, other = data["card"], data["other"]
        source_mr = data["source_mr"]
        manual_content = "人工裁决：保留双方可证实的结论 Ω"
        self.assert_merge_error(source_mr)
        self.resolve(
            data["conflict"],
            resolution="manual",
            manual=manual_content,
        )
        resolved = self.preview(source_mr)["conflicts"]
        self.assertTrue(resolved)
        self.assertTrue(all(c["resolution"] is not None for c in resolved))
        merged = self.merge(source_mr)
        self.assert_merge_parents(merged, data["target_merge"], data["source_commit"])
        self.assert_live(card, "claim", manual_content)
        self.assert_live(other, "claim", "target unrelated")
        self.assert_snapshot_content(merged["snapshot"], card, "claim", manual_content)
        self.assert_snapshot_content(merged["snapshot"], other, "claim", "target unrelated")
        self.assertEqual(
            merged["snapshot"][self.claim_key(card)]["hash"],
            canonical_hash(manual_content),
        )
        self.assert_merge_audit()

    def test_keep_base_resolution_retains_current_main_ours(self):
        data = self.prepare_conflict(include_unrelated_target_change=False)
        card = data["card"]
        source_mr = data["source_mr"]
        self.assert_merge_error(source_mr)
        self.resolve(data["conflict"], resolution="keep_base")
        resolved = self.preview(source_mr)["conflicts"]
        self.assertTrue(resolved)
        self.assertTrue(all(c["resolution"] is not None for c in resolved))
        merged = self.merge(source_mr)
        self.assert_merge_parents(merged, data["target_merge"], data["source_commit"])
        self.assert_live(card, "claim", "target claim")
        self.assert_snapshot_content(merged["snapshot"], card, "claim", "target claim")
        # Resolution affects the new merge snapshot, not source-branch history.
        self.assert_snapshot_content(
            data["source_commit"]["snapshot"], card, "claim", "source claim"
        )
        source_after = self.get_branch(data["source_branch"])
        self.assertEqual(
            source_after["head_commit_id"], self.raw_id(data["source_commit"])
        )
        self.assert_merge_audit()

    # ---------- Review validity and inline comments ----------

    def test_review_threshold_self_approval_request_changes_and_comment_anchor(self):
        card = self.make_card(
            "The exact excerpt supports review-policy testing.", claim="initial"
        )
        topic = self.branch("review-policy")
        source_commit = self.commit(
            topic, "review me", [self.claim_change(card, "reviewed claim")], "alice"
        )
        mr = self.open_mr(
            topic,
            author="alice",
            title="needs two reviewers",
            required_approvals=2,
        )
        listed = self.api_json("GET", "/api/merge-requests")
        self.assertIn(self.raw_id(mr), [self.raw_id(item) for item in listed])
        root_head = self.branch_head(self.main())
        self.assert_merge_error(mr)

        self_approval = self.client.post(
            "/api/merge-requests/%d/reviews" % self.raw_id(mr),
            json={"reviewer": "alice", "verdict": "approve", "comments": []},
        )
        self.assert_error(self_approval, expected=(400, 403))
        self.assertEqual(self.preview(mr)["reviews"], [])

        valid_comment = {"unit_key": self.claim_key(card), "body": "claim changed correctly"}
        self.review(mr, "bob", comments=[valid_comment])
        current = self.preview(mr)
        bob_review = next(r for r in current["reviews"] if r.get("reviewer") == "bob")
        self.assertIsInstance(bob_review.get("comments"), list)
        self.assertTrue(
            any(
                comment.get("unit_key") == self.claim_key(card)
                and comment.get("body") == "claim changed correctly"
                for comment in bob_review["comments"]
            )
        )
        self.assert_merge_error(mr)  # one of two required approvals

        invalid_anchor = self.client.post(
            "/api/merge-requests/%d/reviews" % self.raw_id(mr),
            json={
                "reviewer": "carol",
                "verdict": "approve",
                "comments": [
                    {
                        "unit_key": self.attr_key(card, "excerpt"),
                        "body": "this unit is not in the current diff",
                    }
                ],
            },
        )
        self.assert_error(invalid_anchor, expected=400)
        self.assertEqual(len(self.preview(mr)["reviews"]), 1)

        self.review(mr, "dave")
        self.review(mr, "carol", verdict="request_changes")
        self.assert_merge_error(mr)  # two approvals cannot override request_changes
        self.assertEqual(self.branch_head(self.main()), root_head)
        self.assertEqual(self.branch_head(topic), self.raw_id(source_commit))
        self.assert_live(card, "claim", "initial")

    def test_new_source_head_invalidates_old_approval_until_rereviewed(self):
        card = self.make_card(
            "A genuine baseline sentence supports stale review detection.",
            claim="initial",
        )
        root_head = self.branch_head(self.main())
        topic = self.branch("moving-source")
        first_commit = self.commit(
            topic, "version one", [self.claim_change(card, "version one")], "alice"
        )
        mr = self.open_mr(topic, author="alice", title="moving head")
        self.review(
            mr,
            "bob",
            comments=[{"unit_key": self.claim_key(card), "body": "first version"}],
        )
        second_commit = self.commit(
            topic, "version two", [self.claim_change(card, "version two")], "alice"
        )
        self.assertNotEqual(self.raw_id(first_commit), self.raw_id(second_commit))
        self.assertEqual(self.preview(mr)["conflicts"], [])
        self.assert_merge_error(mr)  # the old approval does not approve the second head
        self.assertEqual(self.branch_head(self.main()), root_head)
        self.assert_live(card, "claim", "initial")

        self.review(mr, "bob")
        merged = self.merge(mr)
        self.assert_merge_parents(merged, root_head, second_commit)
        self.assert_live(card, "claim", "version two")
        self.assert_snapshot_content(merged["snapshot"], card, "claim", "version two")

    # ---------- Tags, reverts, and verified-card merge guard ----------

    def test_tag_is_unique_immutable_and_restricted_to_main_commit(self):
        card = self.make_card(
            "The exact excerpt supports immutable tagged history.", claim="v0"
        )
        main = self.main()
        first = self.commit(main, "main v1", [self.claim_change(card, "v1")], "alice")
        self.assert_live(card, "claim", "v1")
        created = self.api_json(
            "POST",
            "/api/tags",
            expected=201,
            json={"name": "v1.0", "commit_id": self.raw_id(first), "author": "release"},
        )
        tag_id = self.entity_id(created, "tag")
        tag = self.api_json("GET", "/api/tags/%d" % tag_id)
        self.assertEqual(tag["name"], "v1.0")
        self.assertEqual(tag["commit_id"], self.raw_id(first))
        self.assert_snapshot_content(tag["snapshot"], card, "claim", "v1")
        self.assertIn(
            tag_id,
            [self.raw_id(item) for item in self.api_json("GET", "/api/tags")],
        )

        duplicate = self.client.post(
            "/api/tags",
            json={"name": "v1.0", "commit_id": self.raw_id(first), "author": "release"},
        )
        self.assert_error(duplicate, expected=(400, 409))

        second = self.commit(main, "main v2", [self.claim_change(card, "v2")], "alice")
        self.assertEqual(self.branch_head(main), self.raw_id(second))
        self.assert_live(card, "claim", "v2")
        reread = self.api_json("GET", "/api/tags/%d" % tag_id)
        self.assertEqual(reread["snapshot"], tag["snapshot"])
        self.assert_snapshot_content(reread["snapshot"], card, "claim", "v1")

        topic = self.branch("not-main-tag-source")
        non_main = self.commit(
            topic, "branch only", [self.claim_change(card, "branch v3")], "bob"
        )
        rejected = self.client.post(
            "/api/tags",
            json={
                "name": "not-main",
                "commit_id": self.raw_id(non_main),
                "author": "release",
            },
        )
        self.assert_error(rejected, expected=400)
        self.assertNotIn(
            "not-main",
            [item.get("name") for item in self.api_json("GET", "/api/tags")],
        )
        self.assert_live(card, "claim", "v2")

    def test_revert_appends_inverse_main_commit_without_rewriting_target(self):
        card = self.make_card(
            "Exact evidence remains valid across a revert.", claim="original"
        )
        main = self.main()
        target = self.commit(
            main, "change to revert", [self.claim_change(card, "changed")], "alice"
        )
        old_target = self.get_commit(target)
        self.assert_live(card, "claim", "changed")
        created = self.api_json(
            "POST",
            "/api/commits/%d/revert" % self.raw_id(target),
            expected=201,
            json={
                "author": "bob",
                "message": "restore original",
                "reason": "incorrect change",
            },
        )
        revert_id = self.entity_id(created, "commit")
        reverted = self.get_commit(revert_id)
        self.assertEqual(reverted["reverts_commit_id"], self.raw_id(target))
        self.assertEqual(reverted["parent_commit_id"], self.raw_id(target))
        self.assertEqual(self.branch_head(main), revert_id)
        self.assert_snapshot_content(reverted["snapshot"], card, "claim", "original")
        self.assert_live(card, "claim", "original")
        self.assertEqual(self.get_commit(target), old_target)
        history = self.api_json(
            "GET", "/api/branches/%d/commits" % self.raw_id(main)
        )
        ids = [self.raw_id(item) for item in history]
        self.assertIn(self.raw_id(target), ids)
        self.assertIn(revert_id, ids)

    def test_revert_is_rejected_when_later_commit_touches_same_unit(self):
        card = self.make_card(
            "Exact evidence for checking revert conflicts.", claim="original"
        )
        main = self.main()
        first = self.commit(main, "first change", [self.claim_change(card, "v1")], "alice")
        first_immutable = self.get_commit(first)
        second = self.commit(main, "later change", [self.claim_change(card, "v2")], "alice")
        history_path = "/api/branches/%d/commits" % self.raw_id(main)
        history_before = self.api_json("GET", history_path)
        response = self.client.post(
            "/api/commits/%d/revert" % self.raw_id(first),
            json={"author": "bob", "message": "must conflict", "reason": "later edit"},
        )
        self.assert_error(response, expected=409)
        self.assertEqual(self.branch_head(main), self.raw_id(second))
        self.assertEqual(self.api_json("GET", history_path), history_before)
        self.assertEqual(self.get_commit(first), first_immutable)
        self.assert_live(card, "claim", "v2")

    def test_verified_card_with_non_substring_excerpt_cannot_partially_merge(self):
        original_excerpt = "This exact original sentence is a genuine paper substring."
        card = self.make_card(original_excerpt, claim="claim remains unchanged")
        main = self.main()
        old_main_head = self.branch_head(main)
        topic = self.branch("invalid-verified-excerpt")
        bad_commit = self.commit(
            topic,
            "break verbatim evidence",
            [self.unit_change(card, "excerpt", "not present in the paper")],
            "alice",
        )
        mr = self.open_mr(topic, author="alice")
        self.review(mr, "bob")
        self.assertEqual(self.preview(mr)["conflicts"], [])
        history_path = "/api/branches/%d/commits" % self.raw_id(main)
        main_history_before = self.api_json("GET", history_path)
        self.assert_merge_error(mr, expected=409)
        self.assertEqual(self.branch_head(main), old_main_head)
        self.assertEqual(self.branch_head(topic), self.raw_id(bad_commit))
        self.assertEqual(self.api_json("GET", history_path), main_history_before)
        live = self.assert_live(card, "claim", "claim remains unchanged")
        self.assertEqual(live["excerpt"], card["excerpt"])
        self.assertIn(live["excerpt"], original_excerpt)
        self.assertEqual(live["status"], "verified")
        self.assert_snapshot_content(
            bad_commit["snapshot"], card, "excerpt", "not present in the paper"
        )

    # ---------- Section/block projection and deletion corner cases ----------

    def test_structure_edit_merges_from_branch_to_live_section(self):
        self.make_card("The exact source supports this section edit.", claim="先准备卡片")
        main_detail = self.get_branch(self.main())
        unit_key = sorted(key for key in main_detail["units"] if key.startswith("structure:"))[0]
        old_unit = main_detail["units"][unit_key]
        content = dict(old_unit["content"])
        content["title"] = "分支修订后的章节"
        topic = self.branch("topic-section-editor")
        self.commit(topic, "revise section", [{
            "unit_type": "structure", "unit_key": unit_key,
            "old_hash": old_unit["hash"], "old_content": old_unit["content"],
            "new_content": content,
        }])
        section_id = int(unit_key.split(":", 1)[1])
        self.assertNotEqual(
            next(s for s in self.api_json("GET", "/api/sections") if s["id"] == section_id)["title"],
            content["title"],
        )
        mr = self.open_mr(topic)
        self.assertEqual(self.preview(mr)["preview"]["changed_unit_keys"], [unit_key])
        self.review(mr, "bob")
        merge = self.merge(mr)
        self.assertEqual(len(merge["parent_commit_ids"]), 2)
        self.assertEqual(
            next(s for s in self.api_json("GET", "/api/sections") if s["id"] == section_id)["title"],
            content["title"],
        )
        self.assertEqual(self.get_branch(topic)["units"][unit_key]["content"], content)

    def test_card_field_and_block_create_update_delete_projection(self):
        import sqlite3

        card = self.make_card("This source is exact for projection checks.", claim="projection")
        section = self.api_json("GET", "/api/sections")[0]
        main = self.main()
        block_key = "block:901"
        field_key = "card_field:%d:复核记录" % card["id"]
        field_value = {"结论": "人工已复核", "version": 1}
        block_value = {"section_id": section["id"], "ord": 0, "text": "第一版段落。"}
        first = self.commit(main, "create block and card field", [
            {"unit_type": "card_field", "unit_key": field_key, "new_content": field_value},
            {"unit_type": "block", "unit_key": block_key, "new_content": block_value},
        ])
        self.assertEqual(first["snapshot"][block_key]["hash"], canonical_hash(block_value))
        self.assertEqual(self.live_card(card)["field_values"]["复核记录"], field_value)

        def read_block():
            connection = sqlite3.connect(self.db_path)
            try:
                return connection.execute(
                    "SELECT section_id, ord, text FROM blocks WHERE id = 901"
                ).fetchone()
            finally:
                connection.close()

        self.assertEqual(read_block(), (section["id"], 0, "第一版段落。"))
        block_value["text"] = "第二版段落，保留原来的章节位置。"
        second = self.commit(main, "update block", [{
            "unit_type": "block", "unit_key": block_key,
            "old_hash": first["snapshot"][block_key]["hash"], "new_content": block_value,
        }])
        self.assertEqual(read_block()[2], block_value["text"])
        deleted = self.commit(main, "delete block through history", [{
            "unit_type": "block", "unit_key": block_key,
            "old_hash": second["snapshot"][block_key]["hash"], "new_content": None,
        }])
        self.assertNotIn(block_key, deleted["snapshot"])
        self.assertIsNone(read_block())
        change = next(c for c in deleted["changes"] if c["unit_key"] == block_key)
        self.assertIsNone(change["new_hash"])
        self.assertEqual(change["old_hash"], canonical_hash(block_value))

    def test_delete_side_conflict_can_resolve_to_missing_unit(self):
        data = self.prepare_conflict(include_unrelated_target_change=False)
        card = data["card"]
        deleted_commit = self.commit(
            data["source_branch"], "delete claim instead of revising it",
            [self.claim_change(card, None)], author="source-author",
        )
        mr = data["source_mr"]
        self.review(mr, "source-reviewer-second-round")
        refreshed = self.preview(mr, method="POST")
        conflict = refreshed["conflicts"][0]
        self.assertIsNone(conflict["theirs_hash"])
        self.assertIsNone(conflict["theirs_content"])
        self.assert_merge_error(mr, expected=409)
        resolution = self.resolve(conflict, resolution="keep_branch")
        self.assertIsNone(resolution["resolved_content_hash"])
        merge = self.merge(mr)
        self.assertNotIn(self.claim_key(card), merge["snapshot"])
        self.assertIsNone(self.live_card(card)["claim"])
        self.assertIsNotNone(self.get_commit(deleted_commit)["snapshot"])

    def test_resolved_conflict_is_reused_on_repeated_preview(self):
        data = self.prepare_conflict(include_unrelated_target_change=False)
        mr = data["source_mr"]
        resolved = self.resolve(data["conflict"], resolution="manual", manual="有记录的合并结果")
        conflict_id = self.raw_id(data["conflict"])
        first = self.preview(mr, method="POST")["conflicts"]
        second = self.preview(mr)["conflicts"]
        self.assertEqual(first, second)
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0]["id"], conflict_id)
        self.assertEqual(first[0]["resolution"], resolved["resolution"])
        self.assertEqual(first[0]["resolved_content"], "有记录的合并结果")
        self.assertEqual(self.merge(mr)["snapshot"][self.claim_key(data["card"])]["content"], "有记录的合并结果")

    # ---------- Existing application minimum regression ----------

    def test_legacy_list_import_extract_verify_mount_and_section_list_still_work(self):
        self.assertEqual(self.api_json("GET", "/api/papers"), [])
        invalid_paper = self.client.post(
            "/api/papers", json={"title": "", "abstract": ""}
        )
        self.assert_error(invalid_paper, expected=400)
        default_sections = self.api_json("GET", "/api/sections")
        self.assertGreaterEqual(len(default_sections), 1)

        template_id = self.entity_id(
            self.api_json(
                "POST",
                "/api/templates",
                expected=201,
                json={"name": "最小回归模板", "fields": ["方法"]},
            ),
            "template",
        )
        templates = self.api_json("GET", "/api/templates")
        self.assertIn(template_id, [item["id"] for item in templates])
        self.api_json("POST", "/api/templates/%d/activate" % template_id)
        excerpt = "The legacy regression sentence is extracted verbatim."
        paper_id = self.entity_id(
            self.api_json(
                "POST",
                "/api/papers",
                expected=201,
                json={"title": "legacy paper", "abstract": excerpt},
            ),
            "paper",
        )
        self.assertEqual(len(self.api_json("GET", "/api/papers")), 1)
        extracted = self.api_json(
            "POST",
            "/api/extract",
            json={"paper_id": paper_id, "template_id": template_id},
        )
        self.assertEqual(extracted["created"], 1)
        cards = self.api_json("GET", "/api/cards?paper_id=%d" % paper_id)
        self.assertEqual(len(cards), 1)
        card_id = cards[0]["id"]
        original_claim = cards[0]["claim"]
        self.assertEqual(cards[0]["excerpt"], excerpt)
        self.api_json(
            "POST", "/api/cards/%d/verify" % card_id, json={"action": "approve"}
        )
        verified = self.api_json("GET", "/api/cards?status=verified")
        self.assertEqual([item["id"] for item in verified], [card_id])

        section_id = self.entity_id(
            self.api_json(
                "POST", "/api/sections", expected=201, json={"title": "回归章节"}
            ),
            "section",
        )
        self.api_json(
            "POST",
            "/api/cards/%d/assign" % card_id,
            json={"section_id": section_id},
        )
        mounted = self.api_json("GET", "/api/sections/%d/cards" % section_id)
        self.assertEqual([item["id"] for item in mounted], [card_id])
        self.assertIn(section_id, [s["id"] for s in self.api_json("GET", "/api/sections")])
        # Collaboration bootstrap can still read the same live-card database afterwards.
        root = self.get_commit(self.branch_head(self.main()))
        self.assert_snapshot_content(root["snapshot"], card_id, "claim", original_claim)

    def test_verify_decision_can_be_reopened_and_changed(self):
        """核验结论可撤回、可改判：避免误点后只能删库重来。"""
        excerpt = "Reopening a verification decision must stay possible."
        template_id = self.entity_id(
            self.api_json(
                "POST",
                "/api/templates",
                expected=201,
                json={"name": "撤回核验模板", "fields": ["结论"]},
            ),
            "template",
        )
        self.api_json("POST", "/api/templates/%d/activate" % template_id)
        paper_id = self.entity_id(
            self.api_json(
                "POST",
                "/api/papers",
                expected=201,
                json={"title": "reopen paper", "abstract": excerpt},
            ),
            "paper",
        )
        self.api_json(
            "POST", "/api/extract", json={"paper_id": paper_id, "template_id": template_id}
        )
        card_id = self.api_json("GET", "/api/cards?paper_id=%d" % paper_id)[0]["id"]

        def status_of():
            return self.api_json("GET", "/api/cards?paper_id=%d" % paper_id)[0]["status"]

        self.api_json("POST", "/api/cards/%d/verify" % card_id, json={"action": "reject"})
        self.assertEqual(status_of(), "rejected")
        # 误判后可以直接改判通过（摘录确为原文子串）
        self.api_json("POST", "/api/cards/%d/verify" % card_id, json={"action": "approve"})
        self.assertEqual(status_of(), "verified")
        # 也可以撤回核验，回到待核验继续处理
        self.api_json("POST", "/api/cards/%d/verify" % card_id, json={"action": "reset"})
        self.assertEqual(status_of(), "pending")
        self.assert_error(
            self.client.post("/api/cards/%d/verify" % card_id, json={"action": "unknown"}),
            expected=400,
        )
        self.assertEqual(status_of(), "pending")

    def test_verify_audit_trail_and_branch_diff_endpoint(self):
        """核验改动写入审计；分支差异端点按 added/changed/removed 分类。"""
        excerpt = "Branch diff must report exactly one changed unit."
        template_id = self.entity_id(
            self.api_json(
                "POST",
                "/api/templates",
                expected=201,
                json={"name": "差异模板", "fields": ["结论"]},
            ),
            "template",
        )
        self.api_json("POST", "/api/templates/%d/activate" % template_id)
        paper_id = self.entity_id(
            self.api_json(
                "POST",
                "/api/papers",
                expected=201,
                json={"title": "diff paper", "abstract": excerpt},
            ),
            "paper",
        )
        self.api_json(
            "POST", "/api/extract", json={"paper_id": paper_id, "template_id": template_id}
        )
        card_id = self.api_json("GET", "/api/cards?paper_id=%d" % paper_id)[0]["id"]

        verified = self.api_json(
            "POST", "/api/cards/%d/verify" % card_id,
            json={"action": "approve", "actor": "张三"},
        )
        self.assertEqual(verified["status"], "verified")
        logs = self.api_json(
            "GET", "/api/audit-logs?entity_type=card&entity_id=%d" % card_id
        )
        self.assertTrue(logs)
        self.assertEqual(logs[0]["actor"], "张三")
        self.assertEqual(logs[0]["action"], "card.verify.approve")
        self.assertEqual(logs[0]["details"]["from_status"], "pending")
        self.assertEqual(logs[0]["details"]["to_status"], "verified")

        # 引导 main 根提交（含刚才这张卡片），再在分支上改一次论断
        self.main()
        card = self.live_card(card_id)
        branch = self.api_json(
            "POST", "/api/branches", expected=201,
            json={"name": "diff-branch", "kind": "person", "owner": "张三",
                  "purpose": "验证分支差异视图"},
        )
        branch_id = branch["id"]
        self.commit(branch_id, "修订论断", [self.claim_change(card, "分支上的新论断")])

        diff = self.api_json("GET", "/api/branches/%d/diff?base=main" % branch_id)
        self.assertEqual(diff["base_branch_name"], "main")
        self.assertEqual(diff["counts"]["changed"], 1)
        self.assertEqual(diff["counts"]["added"], 0)
        self.assertEqual(diff["changes"][0]["unit_key"], self.claim_key(card))
        self.assertEqual(diff["changes"][0]["status"], "changed")
        self.assertEqual(diff["changes"][0]["base_content"], card["claim"])
        self.assertEqual(diff["changes"][0]["head_content"], "分支上的新论断")

        # 与自身比较时没有差异
        same = self.api_json("GET", "/api/branches/%d/diff?base=%d" % (branch_id, branch_id))
        self.assertEqual(same["counts"]["total"], 0)
        self.assertEqual(same["base_commit_id"], same["head_commit_id"])

    def test_card_assignment_is_audited(self):
        excerpt = "Assigning a card to a section must leave an audit entry."
        template_id = self.entity_id(
            self.api_json("POST", "/api/templates", expected=201,
                          json={"name": "挂载模板", "fields": ["结论"]}),
            "template",
        )
        self.api_json("POST", "/api/templates/%d/activate" % template_id)
        paper_id = self.entity_id(
            self.api_json("POST", "/api/papers", expected=201,
                          json={"title": "assign paper", "abstract": excerpt}),
            "paper",
        )
        self.api_json("POST", "/api/extract",
                      json={"paper_id": paper_id, "template_id": template_id})
        card_id = self.api_json("GET", "/api/cards?paper_id=%d" % paper_id)[0]["id"]
        self.api_json("POST", "/api/cards/%d/verify" % card_id, json={"action": "approve"})
        section_id = self.entity_id(
            self.api_json("POST", "/api/sections", expected=201,
                          json={"title": "审计章节"}),
            "section",
        )
        self.api_json("POST", "/api/cards/%d/assign" % card_id,
                      json={"section_id": section_id, "actor": "李四"})
        logs = self.api_json("GET", "/api/audit-logs?entity_type=card&entity_id=%d" % card_id)
        assign_entries = [item for item in logs if item["action"] == "card.assign"]
        self.assertEqual(len(assign_entries), 1)
        self.assertEqual(assign_entries[0]["actor"], "李四")
        self.assertEqual(assign_entries[0]["details"]["section_id"], section_id)

    # ---------- 题录导入 / 导出 ----------

    BIBTEX_SAMPLE = """@article{chen2024pi,
      title = {Machine learning for polyimide design},
      author = {Chen, Wei and Liu, Yan},
      journal = {Polymer},
      year = {2024},
      doi = {10.1000/pi.2024.001},
      abstract = {We collect 1261 polyimides and train a gradient boosting model.}
    }

    @inproceedings{liu2023md,
      title = "Molecular dynamics of polyurethane",
      author = {Liu, Yan},
      year = {2023},
      abstract = {MD simulations reproduce the experimental modulus.}
    }
    """

    RIS_SAMPLE = """TY  - JOUR
TI  - Data-efficient learning of polymer properties
AU  - Zhang, San
AU  - Wang, Si
PY  - 2022
DO  - 10.1000/poly.2022.777
AB  - A data-efficient workflow is proposed.
ER  -
"""

    def test_bibtex_import_parses_fields_and_dedupes(self):
        first = self.api_json(
            "POST", "/api/papers/import", expected=201,
            json={"format": "bibtex", "text": self.BIBTEX_SAMPLE, "actor": "导入测试"},
        )
        self.assertEqual(first["summary"]["created"], 2)
        titles = [item["title"] for item in first["created"]]
        self.assertIn("Machine learning for polyimide design", titles)
        self.assertIn("Molecular dynamics of polyurethane", titles)

        papers = self.api_json("GET", "/api/papers")
        by_title = {paper["title"]: paper for paper in papers}
        pi = by_title["Machine learning for polyimide design"]
        self.assertEqual(pi["authors"], "Chen, Wei; Liu, Yan")
        self.assertEqual(pi["year"], "2024")
        self.assertEqual(pi["doi"], "10.1000/pi.2024.001")
        self.assertIn("1261 polyimides", pi["abstract"])

        # 同一个 BibTeX 再导入一次：全部按 DOI / 标题去重
        again = self.api_json(
            "POST", "/api/papers/import", expected=201,
            json={"format": "bibtex", "text": self.BIBTEX_SAMPLE},
        )
        self.assertEqual(again["summary"]["created"], 0)
        self.assertEqual(again["summary"]["skipped"], 2)

    def test_ris_import_and_doi_import_falls_back_offline(self):
        ris = self.api_json(
            "POST", "/api/papers/import", expected=201,
            json={"format": "ris", "text": self.RIS_SAMPLE},
        )
        self.assertEqual(ris["summary"]["created"], 1)
        paper = self.api_json("GET", "/api/papers")[0]
        self.assertEqual(paper["authors"], "Zhang, San; Wang, Si")
        self.assertEqual(paper["year"], "2022")
        self.assertEqual(paper["doi"], "10.1000/poly.2022.777")

        # DOI 导入：已存在的 DOI 去重，取不到题录的新 DOI 降级为 failed 供手工补全
        self.api_json("POST", "/api/papers/import", expected=201,
                      json={"format": "bibtex", "text": self.BIBTEX_SAMPLE})
        original = self.app_module.fetch_crossref

        def boom(doi, timeout=8):
            raise OSError("network unreachable")

        self.app_module.fetch_crossref = boom
        try:
            doi_result = self.api_json(
                "POST", "/api/papers/import", expected=201,
                json={"format": "doi",
                      "text": "10.1000/pi.2024.001\nhttps://doi.org/10.1000/new.2026.9"},
            )
        finally:
            self.app_module.fetch_crossref = original
        self.assertEqual(doi_result["summary"]["created"], 0)
        self.assertEqual(doi_result["summary"]["skipped"], 1)   # 已存在的 DOI
        self.assertEqual(doi_result["summary"]["failed"], 1)    # 网络不可达的那条
        self.assertEqual(doi_result["failed"][0]["doi"], "10.1000/new.2026.9")
        self.assertIn("Crossref", doi_result["failed"][0]["reason"])

    def test_export_markdown_blocks_when_excerpt_is_not_verbatim(self):
        excerpt = "Verbatim export sentence for the review draft."
        template_id = self.entity_id(
            self.api_json("POST", "/api/templates", expected=201,
                          json={"name": "导出模板", "fields": ["方法"]}),
            "template",
        )
        self.api_json("POST", "/api/templates/%d/activate" % template_id)
        paper_id = self.entity_id(
            self.api_json("POST", "/api/papers", expected=201,
                          json={"title": "export paper", "authors": "Chen, Wei",
                                "year": "2024", "doi": "10.1000/export",
                                "abstract": excerpt}),
            "paper",
        )
        self.api_json("POST", "/api/extract",
                      json={"paper_id": paper_id, "template_id": template_id})
        card_id = self.api_json("GET", "/api/cards?paper_id=%d" % paper_id)[0]["id"]
        self.api_json("POST", "/api/cards/%d/verify" % card_id, json={"action": "approve"})
        section_id = self.entity_id(
            self.api_json("POST", "/api/sections", expected=201, json={"title": "导出章节"}),
            "section",
        )
        self.api_json("POST", "/api/cards/%d/assign" % card_id, json={"section_id": section_id})

        response = self.client.get("/api/export/markdown")
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("综述证据稿", body)
        self.assertIn("导出章节", body)
        self.assertIn("### 卡片 #%d" % card_id, body)
        self.assertIn(excerpt, body)
        self.assertIn("DOI: 10.1000/export", body)
        self.assertIn("text/markdown", response.headers["Content-Type"])
        self.assertIn("attachment", response.headers["Content-Disposition"])

        # 制造一张"已核验但摘录非原文"的卡片，导出应被拦截，除非 force=1
        with self.app_module.app.app_context():
            db = self.app_module.get_db()
            db.execute("UPDATE cards SET excerpt = ? WHERE id = ?", ("这不是原文子串", card_id))
            db.commit()
        blocked = self.client.get("/api/export/markdown")
        self.assertEqual(blocked.status_code, 409)
        payload = json.loads(blocked.get_data(as_text=True))
        self.assertEqual(payload["problems"][0]["card_id"], card_id)
        forced = self.client.get("/api/export/markdown?force=1")
        self.assertEqual(forced.status_code, 200)
        self.assertIn("强制导出", forced.get_data(as_text=True))

    def test_export_csv_and_json_contain_evidence_rows(self):
        excerpt = "CSV export row sentence."
        template_id = self.entity_id(
            self.api_json("POST", "/api/templates", expected=201,
                          json={"name": "CSV 模板", "fields": ["结论"]}),
            "template",
        )
        self.api_json("POST", "/api/templates/%d/activate" % template_id)
        paper_id = self.entity_id(
            self.api_json("POST", "/api/papers", expected=201,
                          json={"title": "csv paper", "abstract": excerpt}),
            "paper",
        )
        self.api_json("POST", "/api/extract",
                      json={"paper_id": paper_id, "template_id": template_id})
        card_id = self.api_json("GET", "/api/cards?paper_id=%d" % paper_id)[0]["id"]

        csv_response = self.client.get("/api/export/cards.csv")
        self.assertEqual(csv_response.status_code, 200)
        csv_text = csv_response.get_data(as_text=True)
        self.assertIn("card_id,status,field,claim", csv_text)
        self.assertIn("csv paper", csv_text)
        self.assertTrue(csv_text.startswith("\ufeff"))

        json_response = self.client.get("/api/export/cards.json")
        self.assertEqual(json_response.status_code, 200)
        payload = json.loads(json_response.get_data(as_text=True))
        self.assertEqual(len(payload["cards"]), 1)
        self.assertEqual(payload["cards"][0]["id"], card_id)
        self.assertEqual(payload["papers"][0]["title"], "csv paper")


if __name__ == "__main__":
    unittest.main(verbosity=2)
