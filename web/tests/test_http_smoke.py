# -*- coding: utf-8 -*-
"""真实进程/HTTP 冒烟测试；数据库放在临时目录，不读取正式演示库。"""

import json
import os
import hashlib
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request


ROOT = Path(__file__).resolve().parents[2]


class CollaborationHttpSmokeTest(unittest.TestCase):
    def request(self, path, method="GET", payload=None):
        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + path, data=body, method=method,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            content = response.read()
            self.assertEqual(response.status, 200 if method == "GET" else response.status)
        return json.loads(content.decode("utf-8"))

    def test_real_launch_page_and_branch_review_merge_tag(self):
        with tempfile.TemporaryDirectory(prefix="reviewgit-http-smoke-") as directory:
            formal_db = ROOT / "web" / "reviewgit.db"
            formal_hash_before = hashlib.sha256(formal_db.read_bytes()).hexdigest() if formal_db.exists() else None
            environment = os.environ.copy()
            environment["REVIEWGIT_DB"] = os.path.join(directory, "http.sqlite3")
            environment["PYTHONDONTWRITEBYTECODE"] = "1"
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            self.base_url = "http://127.0.0.1:%d" % port
            log_path = os.path.join(directory, "server.log")
            with open(log_path, "wb") as log:
                process = subprocess.Popen(
                    [sys.executable, "scripts/launch.py", "--port", str(port)],
                    cwd=str(ROOT), env=environment, stdout=log, stderr=subprocess.STDOUT,
                )
                try:
                    ready = False
                    for _ in range(80):
                        if process.poll() is not None:
                            break
                        try:
                            with urllib.request.urlopen(self.base_url + "/", timeout=1) as response:
                                html = response.read().decode("utf-8")
                            self.assertIn('data-tab="collaboration"', html)
                            self.assertIn("async function resolveConflict(", html)
                            ready = True
                            break
                        except OSError:
                            time.sleep(0.1)
                    self.assertTrue(ready, "启动失败：" + Path(log_path).read_text(encoding="utf-8", errors="replace"))
                    self.assertNotEqual(environment["REVIEWGIT_DB"], str(formal_db))

                    branches = self.request("/api/branches")
                    main = next(branch for branch in branches if branch["name"] == "main")
                    cards = self.request("/api/cards")
                    self.assertTrue(cards)
                    card = cards[0]
                    self.assertGreater(card["id"], 0)
                    branch = self.request("/api/branches", "POST", {
                        "name": "http-collaboration", "kind": "person",
                        "owner": "HTTP 作者", "purpose": "验证真实进程中的分支与合并",
                    })
                    self.assertEqual(self.request("/api/merge-requests"), [])
                    claim_key = "card_attr:%d:claim" % card["id"]
                    changed = card["claim"] + "（HTTP 端到端修订）"
                    commit = self.request("/api/branches/%d/commits" % branch["id"], "POST", {
                        "author": "HTTP 作者", "message": "HTTP 修订论断",
                        "changes": [{"unit_type": "card_attr", "unit_key": claim_key, "new_content": changed}],
                    })
                    self.assertEqual(commit["parent_commit_id"], main["head_commit_id"])
                    self.assertNotEqual(self.request("/api/cards")[0]["claim"], changed)
                    mr = self.request("/api/merge-requests", "POST", {
                        "source_branch_id": branch["id"], "target_branch_id": main["id"],
                        "author": "HTTP 作者", "title": "HTTP 审核合并",
                    })
                    preview = self.request("/api/merge-requests/%d/preview" % mr["id"], "POST", {})
                    self.assertFalse(preview["conflicts"])
                    self.assertEqual(preview["preview"]["changed_unit_keys"], [claim_key])
                    self.request("/api/merge-requests/%d/reviews" % mr["id"], "POST", {
                        "reviewer": "HTTP 审核人", "verdict": "approve",
                        "comments": [{"unit_key": claim_key, "body": "已检查这条修订"}],
                    })
                    merge = self.request("/api/merge-requests/%d/merge" % mr["id"], "POST", {"actor": "HTTP 负责人"})
                    detail = self.request("/api/commits/%d" % merge["merge_commit_id"])
                    self.assertEqual(len(detail["parent_commit_ids"]), 2)
                    self.assertEqual(self.request("/api/cards")[0]["claim"], changed)
                    tag = self.request("/api/tags", "POST", {
                        "name": "http-v1.0", "commit_id": merge["merge_commit_id"], "author": "HTTP 负责人",
                    })
                    snapshot = self.request("/api/tags/%d" % tag["id"])["snapshot"]
                    self.assertEqual(snapshot[claim_key]["content"], changed)
                    self.assertTrue(any("merge" in item["action"] for item in self.request("/api/audit-logs")))
                    formal_hash_after = hashlib.sha256(formal_db.read_bytes()).hexdigest() if formal_db.exists() else None
                    self.assertEqual(formal_hash_after, formal_hash_before, "隔离测试不得改写正式演示数据库")
                finally:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
