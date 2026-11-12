# -*- coding: utf-8 -*-
"""Database-backed collaboration primitives for the local ReviewGit MVP.

This module deliberately implements Git-like semantics with SQLite rows rather
than invoking git. It is a local, single-project, unauthenticated MVP: author,
reviewer and actor values are descriptive text, not authenticated identities or
security boundaries. History is append-only through the public API.

register_collaboration(app, get_db) performs no database or Flask-context work.
The first collaboration request lazily creates main and its root commit from
the then-current live cards, sections and blocks. The application must execute
COLLABORATION_SCHEMA during its normal database initialization.

Only main commits are projected to the legacy live tables. Sentence, citation
and metadata units are versioned but have no live-table projection; no sentence
level orphan-claim check is performed. Legacy writes outside this module are
not automatically versioned.
"""

import hashlib
import json
import sqlite3
from collections import deque
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import wraps

from flask import jsonify, request


UNIT_TYPES = frozenset((
    "card_attr", "card_field", "block", "sentence", "citation",
    "structure", "metadata",
))
BRANCH_KINDS = frozenset(("person", "topic", "section"))
CARD_ATTRS = (
    "paper_id", "template_id", "field", "claim", "excerpt",
    "excerpt_status", "evidence_type", "confidence", "status",
)
CARD_ATTR_SET = frozenset(CARD_ATTRS)
MR_OPEN_STATES = ("open", "needs_changes", "approved")
RESOLUTIONS = frozenset(("keep_base", "keep_branch", "manual"))
MISSING = object()


COLLABORATION_SCHEMA = """
CREATE TABLE IF NOT EXISTS blocks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  section_id INTEGER,
  ord INTEGER DEFAULT 0,
  text TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS branches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  kind TEXT NOT NULL,
  owner TEXT,
  purpose TEXT,
  base_commit_id INTEGER REFERENCES commits(id),
  head_commit_id INTEGER REFERENCES commits(id),
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS commits (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  branch_id INTEGER NOT NULL REFERENCES branches(id),
  author TEXT NOT NULL,
  parent_commit_id INTEGER REFERENCES commits(id),
  merge_parent_id INTEGER REFERENCES commits(id),
  reverts_commit_id INTEGER REFERENCES commits(id),
  message TEXT NOT NULL,
  snapshot_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS change_units (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  commit_id INTEGER NOT NULL REFERENCES commits(id),
  unit_type TEXT NOT NULL,
  unit_key TEXT NOT NULL,
  old_hash TEXT,
  new_hash TEXT,
  old_content_json TEXT,
  new_content_json TEXT,
  UNIQUE(commit_id, unit_key)
);

CREATE TABLE IF NOT EXISTS merge_requests (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_branch_id INTEGER NOT NULL REFERENCES branches(id),
  target_branch_id INTEGER NOT NULL REFERENCES branches(id),
  author TEXT NOT NULL,
  title TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open',
  required_approvals INTEGER NOT NULL DEFAULT 1,
  base_commit_id INTEGER REFERENCES commits(id),
  source_commit_id INTEGER REFERENCES commits(id),
  target_commit_id INTEGER REFERENCES commits(id),
  preview_signature TEXT,
  merged_commit_id INTEGER REFERENCES commits(id),
  created_at TEXT NOT NULL,
  CHECK(source_branch_id <> target_branch_id),
  CHECK(required_approvals >= 1)
);

CREATE TABLE IF NOT EXISTS reviews (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  mr_id INTEGER NOT NULL REFERENCES merge_requests(id),
  reviewer TEXT NOT NULL,
  verdict TEXT NOT NULL,
  source_commit_id INTEGER NOT NULL REFERENCES commits(id),
  created_at TEXT NOT NULL,
  CHECK(verdict IN ('approve', 'request_changes'))
);

CREATE TABLE IF NOT EXISTS review_comments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  review_id INTEGER NOT NULL REFERENCES reviews(id),
  unit_key TEXT NOT NULL,
  body TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conflicts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  mr_id INTEGER NOT NULL REFERENCES merge_requests(id),
  unit_type TEXT NOT NULL,
  unit_key TEXT NOT NULL,
  base_hash TEXT,
  ours_hash TEXT,
  theirs_hash TEXT,
  base_content_json TEXT,
  ours_content_json TEXT,
  theirs_content_json TEXT,
  preview_signature TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1,
  resolution TEXT,
  resolved_content_json TEXT,
  resolved_content_hash TEXT,
  resolved_by TEXT,
  resolved_at TEXT,
  created_at TEXT NOT NULL,
  CHECK(active IN (0, 1)),
  CHECK(resolution IS NULL OR resolution IN ('keep_base', 'keep_branch', 'manual'))
);

CREATE TABLE IF NOT EXISTS tags (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  commit_id INTEGER NOT NULL REFERENCES commits(id),
  author TEXT,
  snapshot_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  actor TEXT,
  action TEXT NOT NULL,
  entity_type TEXT NOT NULL,
  entity_id INTEGER,
  details_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_commits_branch_id ON commits(branch_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_change_units_commit ON change_units(commit_id, id);
CREATE INDEX IF NOT EXISTS idx_reviews_mr_source ON reviews(mr_id, source_commit_id, id);
CREATE INDEX IF NOT EXISTS idx_comments_review ON review_comments(review_id, id);
CREATE INDEX IF NOT EXISTS idx_conflicts_mr_active ON conflicts(mr_id, active, id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_conflicts_one_active_unit
  ON conflicts(mr_id, unit_key) WHERE active = 1;
CREATE INDEX IF NOT EXISTS idx_audit_logs_id ON audit_logs(id DESC);

CREATE TRIGGER IF NOT EXISTS audit_logs_append_only_update
BEFORE UPDATE ON audit_logs
BEGIN
  SELECT RAISE(ABORT, 'audit_logs are append-only');
END;
CREATE TRIGGER IF NOT EXISTS audit_logs_append_only_delete
BEFORE DELETE ON audit_logs
BEGIN
  SELECT RAISE(ABORT, 'audit_logs are append-only');
END;
"""


class ApiError(Exception):
    """An expected REST error which must not escape as an HTTP 500."""

    def __init__(self, status, message):
        super(ApiError, self).__init__(message)
        self.status = status
        self.message = message


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _canon(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    )


def _hash(content):
    try:
        encoded = _canon(content).encode("utf-8")
    except (TypeError, ValueError):
        raise ApiError(400, "单元内容必须是有限、可序列化的 JSON 值")
    return hashlib.sha256(encoded).hexdigest()


def _loads(value, default=None):
    if value is None:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        raise ApiError(409, "数据库中的 JSON 内容无效")


def _snapshot(commit_row):
    value = _loads(commit_row["snapshot_json"], {})
    if not isinstance(value, dict):
        raise ApiError(409, "提交快照格式无效")
    for key, unit in value.items():
        if not isinstance(key, str) or not isinstance(unit, dict):
            raise ApiError(409, "提交快照单元格式无效")
        if "content" not in unit or "unit_type" not in unit or "hash" not in unit:
            raise ApiError(409, "提交快照单元缺少必要字段")
    return value


def _unit(unit_type, content):
    return {"unit_type": unit_type, "hash": _hash(content), "content": content}


def _stored_unit(unit_type, content_hash, content):
    return {"unit_type": unit_type, "hash": content_hash, "content": content}


def _same(left, right):
    if left is MISSING or right is MISSING:
        return left is right
    return left["hash"] == right["hash"]


def _unit_json(unit):
    if unit is MISSING:
        return None
    return _canon(unit["content"])


def _text(data, field, required=True, allow_none=False):
    value = data.get(field)
    if value is None and allow_none and not required:
        return None
    if not isinstance(value, str):
        if required or value is not None:
            raise ApiError(400, "%s 必须是字符串" % field)
        return None
    value = value.strip()
    if required and not value:
        raise ApiError(400, "%s 不能为空" % field)
    return value


def _json_body():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ApiError(400, "请求体必须是 JSON 对象")
    return data


def _integer(value, field, minimum=None, optional=False):
    if value is None and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ApiError(400, "%s 必须是整数" % field)
    if minimum is not None and value < minimum:
        raise ApiError(400, "%s 必须大于等于 %s" % (field, minimum))
    return value


def _parse_id(value, label):
    if isinstance(value, bool):
        raise ApiError(400, "%s 标识无效" % label)
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise ApiError(400, "%s 标识无效" % label)
    if parsed <= 0 or str(value) != str(parsed):
        raise ApiError(400, "%s 标识无效" % label)
    return parsed


def _validate_key(unit_type, unit_key):
    if not isinstance(unit_type, str) or unit_type not in UNIT_TYPES:
        raise ApiError(400, "不支持的 unit_type")
    if not isinstance(unit_key, str) or not unit_key.startswith(unit_type + ":"):
        raise ApiError(400, "unit_key 必须与 unit_type 前缀一致")
    parts = unit_key.split(":")
    if len(parts) < 2 or not parts[1]:
        raise ApiError(400, "unit_key 缺少标识")
    if unit_type in ("card_attr", "card_field", "block", "structure"):
        _parse_id(parts[1], unit_type)
    if unit_type == "card_attr":
        if len(parts) != 3 or parts[2] not in CARD_ATTR_SET:
            raise ApiError(400, "card_attr 键必须为 card_attr:<id>:<合法属性>")
    elif unit_type == "card_field":
        if len(parts) < 3 or not ":".join(parts[2:]):
            raise ApiError(400, "card_field 键必须包含字段名")
    elif unit_type in ("block", "structure") and len(parts) != 2:
        raise ApiError(400, "%s 键格式无效" % unit_type)


def _valid_int_or_none(value):
    return value is None or (isinstance(value, int) and not isinstance(value, bool))


def _validate_new_content(unit_type, unit_key, content):
    if content is None:
        return
    if unit_type == "card_attr":
        attr = unit_key.split(":", 2)[2]
        if attr == "paper_id":
            if isinstance(content, bool) or not isinstance(content, int) or content <= 0:
                raise ApiError(400, "paper_id 必须是正整数")
        elif attr == "template_id":
            if not _valid_int_or_none(content):
                raise ApiError(400, "template_id 必须是整数或 null")
        elif attr == "confidence":
            if content is not None and (
                    isinstance(content, bool) or not isinstance(content, (int, float))):
                raise ApiError(400, "confidence 必须是数值或 null")
        elif content is not None and not isinstance(content, str):
            raise ApiError(400, "%s 必须是字符串或 null" % attr)
    elif unit_type == "structure":
        if not isinstance(content, dict):
            raise ApiError(400, "structure 内容必须是对象")
        if "title" in content and not isinstance(content["title"], str):
            raise ApiError(400, "structure.title 必须是字符串")
        if "parent_id" in content and not _valid_int_or_none(content["parent_id"]):
            raise ApiError(400, "structure.parent_id 必须是整数或 null")
        if "ord" in content and (
                isinstance(content["ord"], bool) or not isinstance(content["ord"], int)):
            raise ApiError(400, "structure.ord 必须是整数")
    elif unit_type == "block":
        if not isinstance(content, dict):
            raise ApiError(400, "block 内容必须是对象")
        if "section_id" in content and not _valid_int_or_none(content["section_id"]):
            raise ApiError(400, "block.section_id 必须是整数或 null")
        if "ord" in content and (
                isinstance(content["ord"], bool) or not isinstance(content["ord"], int)):
            raise ApiError(400, "block.ord 必须是整数")
        if "text" in content and not isinstance(content["text"], str):
            raise ApiError(400, "block.text 必须是字符串")


def _branch(db, branch_id, label="分支"):
    row = db.execute("SELECT * FROM branches WHERE id = ?", (branch_id,)).fetchone()
    if row is None:
        raise ApiError(404, "%s不存在" % label)
    return row


def _commit(db, commit_id, label="提交"):
    row = db.execute("SELECT * FROM commits WHERE id = ?", (commit_id,)).fetchone()
    if row is None:
        raise ApiError(404, "%s不存在" % label)
    return row


def _mr(db, mr_id):
    row = db.execute("SELECT * FROM merge_requests WHERE id = ?", (mr_id,)).fetchone()
    if row is None:
        raise ApiError(404, "合并请求不存在")
    return row


def _audit(db, actor, action, entity_type, entity_id, details=None):
    db.execute(
        "INSERT INTO audit_logs(actor, action, entity_type, entity_id, details_json, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (actor, action, entity_type, entity_id, _canon(details or {}), _now()),
    )


@contextmanager
def _write(db, immediate=False):
    """Use a savepoint when the host request already opened a transaction."""
    if db.in_transaction:
        db.execute("SAVEPOINT collaboration_write")
        try:
            yield
        except Exception:
            db.execute("ROLLBACK TO SAVEPOINT collaboration_write")
            db.execute("RELEASE SAVEPOINT collaboration_write")
            raise
        else:
            db.execute("RELEASE SAVEPOINT collaboration_write")
    else:
        db.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield
        except Exception:
            db.rollback()
            raise
        else:
            db.commit()


def _live_root_snapshot(db):
    snapshot = {}
    cards = db.execute("SELECT * FROM cards ORDER BY id").fetchall()
    for card in cards:
        for attr in CARD_ATTRS:
            key = "card_attr:%s:%s" % (card["id"], attr)
            snapshot[key] = _unit("card_attr", card[attr])
        values = _loads(card["field_values_json"], {})
        if not isinstance(values, dict):
            values = {}
        for name, value in values.items():
            key = "card_field:%s:%s" % (card["id"], name)
            snapshot[key] = _unit("card_field", value)
    for section in db.execute("SELECT * FROM sections ORDER BY id").fetchall():
        content = {
            "title": section["title"], "parent_id": section["parent_id"],
            "ord": section["ord"],
        }
        snapshot["structure:%s" % section["id"]] = _unit("structure", content)
    for block in db.execute("SELECT * FROM blocks ORDER BY id").fetchall():
        content = {
            "section_id": block["section_id"], "ord": block["ord"],
            "text": block["text"],
        }
        snapshot["block:%s" % block["id"]] = _unit("block", content)
    return snapshot


def _insert_commit(db, branch_id, author, message, parent_id, snapshot, changes,
                   merge_parent_id=None, reverts_commit_id=None):
    created_at = _now()
    cur = db.execute(
        "INSERT INTO commits(branch_id, author, parent_commit_id, merge_parent_id, "
        "reverts_commit_id, message, snapshot_json, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (branch_id, author, parent_id, merge_parent_id, reverts_commit_id,
         message, _canon(snapshot), created_at),
    )
    commit_id = cur.lastrowid
    for change in changes:
        old = change["old_unit"]
        new = change["new_unit"]
        db.execute(
            "INSERT INTO change_units(commit_id, unit_type, unit_key, old_hash, new_hash, "
            "old_content_json, new_content_json) VALUES (?,?,?,?,?,?,?)",
            (commit_id, change["unit_type"], change["unit_key"],
             None if old is MISSING else old["hash"],
             None if new is MISSING else new["hash"],
             _unit_json(old), _unit_json(new)),
        )
    return commit_id


def _ensure_main(db):
    row = db.execute("SELECT * FROM branches WHERE name = 'main'").fetchone()
    if row is not None:
        return row
    with _write(db, immediate=True):
        row = db.execute("SELECT * FROM branches WHERE name = 'main'").fetchone()
        if row is not None:
            return row
        snapshot = _live_root_snapshot(db)
        now = _now()
        cur = db.execute(
            "INSERT INTO branches(name, kind, owner, purpose, base_commit_id, "
            "head_commit_id, created_at) VALUES ('main', 'main', 'system', ?, NULL, NULL, ?)",
            ("默认主分支", now),
        )
        branch_id = cur.lastrowid
        changes = []
        for key in sorted(snapshot):
            unit = snapshot[key]
            changes.append({
                "unit_type": unit["unit_type"], "unit_key": key,
                "old_unit": MISSING, "new_unit": unit,
            })
        commit_id = _insert_commit(
            db, branch_id, "system", "Root snapshot of live project",
            None, snapshot, changes,
        )
        db.execute(
            "UPDATE branches SET base_commit_id = ?, head_commit_id = ? WHERE id = ?",
            (commit_id, commit_id, branch_id),
        )
        _audit(db, "system", "branch.create", "branch", branch_id,
               {"name": "main", "root_commit_id": commit_id})
        _audit(db, "system", "commit.root", "commit", commit_id,
               {"branch_id": branch_id, "units": len(snapshot)})
        return db.execute("SELECT * FROM branches WHERE id = ?", (branch_id,)).fetchone()


def _branch_dict(row):
    result = dict(row)
    result["branch_id"] = result["id"]
    return result


def _commit_dict(row, snapshot=None):
    result = {
        "id": row["id"], "commit_id": row["id"], "branch_id": row["branch_id"],
        "author": row["author"], "parent_commit_id": row["parent_commit_id"],
        "merge_parent_id": row["merge_parent_id"],
        "reverts_commit_id": row["reverts_commit_id"], "message": row["message"],
        "created_at": row["created_at"],
        "parent_commit_ids": [
            parent for parent in (row["parent_commit_id"], row["merge_parent_id"])
            if parent is not None
        ],
    }
    if snapshot is not None:
        result["snapshot"] = snapshot
    return result


def _change_dict(row):
    return {
        "id": row["id"], "change_unit_id": row["id"], "commit_id": row["commit_id"],
        "unit_type": row["unit_type"], "unit_key": row["unit_key"],
        "old_hash": row["old_hash"], "new_hash": row["new_hash"],
        "old_content": _loads(row["old_content_json"]),
        "new_content": _loads(row["new_content_json"]),
    }


def _commit_detail(db, row):
    result = _commit_dict(row, _snapshot(row))
    result["changes"] = [
        _change_dict(r) for r in db.execute(
            "SELECT * FROM change_units WHERE commit_id = ? ORDER BY id", (row["id"],)
        ).fetchall()
    ]
    return result


def _diff(old_snapshot, new_snapshot):
    changes = []
    for key in sorted(set(old_snapshot) | set(new_snapshot)):
        old = old_snapshot.get(key, MISSING)
        new = new_snapshot.get(key, MISSING)
        if _same(old, new):
            continue
        unit = new if new is not MISSING else old
        changes.append({
            "unit_type": unit["unit_type"], "unit_key": key,
            "old_unit": old, "new_unit": new,
        })
    return changes


def _ancestor_distances(db, start_id):
    distances = {start_id: 0}
    queue = deque((start_id,))
    while queue:
        commit_id = queue.popleft()
        row = db.execute(
            "SELECT parent_commit_id, merge_parent_id FROM commits WHERE id = ?",
            (commit_id,),
        ).fetchone()
        if row is None:
            continue
        for parent in (row["parent_commit_id"], row["merge_parent_id"]):
            if parent is not None and parent not in distances:
                distances[parent] = distances[commit_id] + 1
                queue.append(parent)
    return distances


def _common_ancestor(db, source_id, target_id):
    source = _ancestor_distances(db, source_id)
    target = _ancestor_distances(db, target_id)
    common = set(source) & set(target)
    if not common:
        raise ApiError(409, "源分支与目标分支没有共同祖先")
    return min(common, key=lambda cid: (max(source[cid], target[cid]),
                                       source[cid] + target[cid], -cid))


def _signature(base_id, source_id, target_id):
    return hashlib.sha256(_canon({
        "base_commit_id": base_id, "source_commit_id": source_id,
        "target_commit_id": target_id,
    }).encode("utf-8")).hexdigest()


def _three_way(base, ours, theirs):
    result = {}
    conflicts = []
    for key in sorted(set(base) | set(ours) | set(theirs)):
        b = base.get(key, MISSING)
        o = ours.get(key, MISSING)
        t = theirs.get(key, MISSING)
        if _same(o, t):
            selected = o
        elif _same(o, b):
            selected = t
        elif _same(t, b):
            selected = o
        else:
            selected = o
            representative = o if o is not MISSING else (t if t is not MISSING else b)
            conflicts.append({
                "unit_type": representative["unit_type"], "unit_key": key,
                "base_unit": b, "ours_unit": o, "theirs_unit": t,
            })
        if selected is not MISSING:
            result[key] = selected
    return result, conflicts


def _compute_preview(db, mr):
    source = _branch(db, mr["source_branch_id"], "源分支")
    target = _branch(db, mr["target_branch_id"], "目标分支")
    if source["head_commit_id"] is None or target["head_commit_id"] is None:
        raise ApiError(409, "源分支或目标分支没有提交头")
    source_id = source["head_commit_id"]
    target_id = target["head_commit_id"]
    base_id = _common_ancestor(db, source_id, target_id)
    base_snapshot = _snapshot(_commit(db, base_id))
    source_snapshot = _snapshot(_commit(db, source_id))
    target_snapshot = _snapshot(_commit(db, target_id))
    merged, found = _three_way(base_snapshot, target_snapshot, source_snapshot)
    return {
        "base_id": base_id, "source_id": source_id, "target_id": target_id,
        "signature": _signature(base_id, source_id, target_id),
        "base_snapshot": base_snapshot, "source_snapshot": source_snapshot,
        "target_snapshot": target_snapshot, "merged": merged, "found": found,
        "changed_unit_keys": [
            change["unit_key"] for change in _diff(base_snapshot, source_snapshot)
        ],
        "source_branch": source, "target_branch": target,
    }


def _conflict_dict(row):
    return {
        "id": row["id"], "conflict_id": row["id"], "mr_id": row["mr_id"],
        "unit_type": row["unit_type"], "unit_key": row["unit_key"],
        "base_hash": row["base_hash"], "ours_hash": row["ours_hash"],
        "theirs_hash": row["theirs_hash"],
        "base_content": _loads(row["base_content_json"]),
        "ours_content": _loads(row["ours_content_json"]),
        "theirs_content": _loads(row["theirs_content_json"]),
        "preview_signature": row["preview_signature"], "active": bool(row["active"]),
        "resolution": row["resolution"],
        "resolved_content": _loads(row["resolved_content_json"]),
        "resolved_content_hash": row["resolved_content_hash"],
        "resolved_hash": row["resolved_content_hash"],
        "resolved_by": row["resolved_by"], "resolved_at": row["resolved_at"],
        "created_at": row["created_at"],
    }


def _save_preview(db, mr, preview):
    """Refresh detection while preserving all conflict and resolution history."""
    db.execute("UPDATE conflicts SET active = 0 WHERE mr_id = ? AND active = 1", (mr["id"],))
    for found in preview["found"]:
        key = found["unit_key"]
        b, o, t = found["base_unit"], found["ours_unit"], found["theirs_unit"]
        candidates = db.execute(
            "SELECT * FROM conflicts WHERE mr_id = ? AND unit_key = ? AND "
            "preview_signature = ? ORDER BY id DESC",
            (mr["id"], key, preview["signature"]),
        ).fetchall()
        reused = None
        for candidate in candidates:
            if (candidate["base_hash"] == (None if b is MISSING else b["hash"]) and
                    candidate["ours_hash"] == (None if o is MISSING else o["hash"]) and
                    candidate["theirs_hash"] == (None if t is MISSING else t["hash"])):
                reused = candidate
                break
        if reused is not None:
            db.execute("UPDATE conflicts SET active = 1 WHERE id = ?", (reused["id"],))
            continue
        db.execute(
            "INSERT INTO conflicts(mr_id, unit_type, unit_key, base_hash, ours_hash, "
            "theirs_hash, base_content_json, ours_content_json, theirs_content_json, "
            "preview_signature, active, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,1,?)",
            (mr["id"], found["unit_type"], key,
             None if b is MISSING else b["hash"], None if o is MISSING else o["hash"],
             None if t is MISSING else t["hash"], _unit_json(b), _unit_json(o),
             _unit_json(t), preview["signature"], _now()),
        )
    db.execute(
        "UPDATE merge_requests SET base_commit_id = ?, source_commit_id = ?, "
        "target_commit_id = ?, preview_signature = ? WHERE id = ?",
        (preview["base_id"], preview["source_id"], preview["target_id"],
         preview["signature"], mr["id"]),
    )
    return _mr(db, mr["id"])


def _review_state(db, mr, source_commit_id=None):
    if source_commit_id is None:
        source_commit_id = _branch(db, mr["source_branch_id"])["head_commit_id"]
    rows = db.execute(
        "SELECT * FROM reviews WHERE mr_id = ? AND source_commit_id = ? ORDER BY id",
        (mr["id"], source_commit_id),
    ).fetchall()
    latest = {}
    for row in rows:
        latest[row["reviewer"]] = row["verdict"]
    approvers = sorted(
        reviewer for reviewer, verdict in latest.items()
        if verdict == "approve" and reviewer != mr["author"]
    )
    requesters = sorted(
        reviewer for reviewer, verdict in latest.items() if verdict == "request_changes"
    )
    return {
        "approvals": len(approvers), "approvers": approvers,
        "request_changes": requesters, "required_approvals": mr["required_approvals"],
        "source_commit_id": source_commit_id,
    }


def _mr_dict(row):
    result = dict(row)
    result["mr_id"] = result["id"]
    return result


def _mr_payload(db, mr):
    preview = _compute_preview(db, mr)
    reviews = []
    review_rows = db.execute(
        "SELECT * FROM reviews WHERE mr_id = ? ORDER BY id", (mr["id"],)
    ).fetchall()
    for review in review_rows:
        item = dict(review)
        item["review_id"] = item["id"]
        item["comments"] = [dict(c) for c in db.execute(
            "SELECT * FROM review_comments WHERE review_id = ? ORDER BY id", (review["id"],)
        ).fetchall()]
        reviews.append(item)
    stale = mr["preview_signature"] != preview["signature"]
    active = [_conflict_dict(r) for r in db.execute(
        "SELECT * FROM conflicts WHERE mr_id = ? AND active = 1 ORDER BY id", (mr["id"],)
    ).fetchall()]
    base_commit = _commit_dict(_commit(db, preview["base_id"]))
    source_commit = _commit_dict(_commit(db, preview["source_id"]))
    target_commit = _commit_dict(_commit(db, preview["target_id"]))
    response = _mr_dict(mr)
    response.update({
        "mr": _mr_dict(mr),
        "base": base_commit, "source": _branch_dict(preview["source_branch"]),
        "target": _branch_dict(preview["target_branch"]),
        "base_commit": base_commit, "source_commit": source_commit,
        "target_commit": target_commit, "reviews": reviews, "conflicts": active,
        "active_conflicts": active,
        "review_status": _review_state(db, mr, preview["source_id"]),
        "preview": {
            "base_commit_id": preview["base_id"], "source_commit_id": preview["source_id"],
            "target_commit_id": preview["target_id"],
            "ours_commit_id": preview["target_id"], "theirs_commit_id": preview["source_id"],
            "source_head_commit_id": preview["source_id"],
            "target_head_commit_id": preview["target_id"],
            "signature": preview["signature"], "stored_signature": mr["preview_signature"],
            "stored_base_commit_id": mr["base_commit_id"],
            "stored_source_commit_id": mr["source_commit_id"],
            "stored_target_commit_id": mr["target_commit_id"], "stale": stale,
            "conflict_count": len(preview["found"]),
            "conflict_unit_keys": [c["unit_key"] for c in preview["found"]],
            "changed_unit_keys": preview["changed_unit_keys"],
            "changes": [{
                "unit_key": key,
                "unit_type": (preview["source_snapshot"].get(key) or
                              preview["base_snapshot"].get(key))["unit_type"],
                "base_content": (preview["base_snapshot"].get(key) or {}).get("content"),
                "ours_content": (preview["target_snapshot"].get(key) or {}).get("content"),
                "theirs_content": (preview["source_snapshot"].get(key) or {}).get("content"),
            } for key in preview["changed_unit_keys"]],
        },
    })
    return response


def _mark_head_changed(db, branch_id, reopen_source=True):
    db.execute(
        "UPDATE conflicts SET active = 0 WHERE active = 1 AND mr_id IN "
        "(SELECT id FROM merge_requests WHERE source_branch_id = ? OR target_branch_id = ?)",
        (branch_id, branch_id),
    )
    if reopen_source:
        db.execute(
            "UPDATE merge_requests SET status = 'open' WHERE source_branch_id = ? "
            "AND status IN ('open', 'needs_changes', 'approved')",
            (branch_id,),
        )


def _desired_cards(snapshot):
    ids = set()
    for key in snapshot:
        if key.startswith("card_attr:") or key.startswith("card_field:"):
            parts = key.split(":")
            if len(parts) >= 3:
                ids.add(int(parts[1]))
    cards = {}
    defaults = {
        "paper_id": None, "template_id": None, "field": None, "claim": None,
        "excerpt": None, "excerpt_status": "verbatim_verified",
        "evidence_type": "claim", "confidence": 0.8, "status": "pending",
    }
    for card_id in sorted(ids):
        values = dict(defaults)
        for attr in CARD_ATTRS:
            key = "card_attr:%s:%s" % (card_id, attr)
            if key in snapshot:
                values[attr] = snapshot[key]["content"]
        if (isinstance(values["paper_id"], bool) or
                not isinstance(values["paper_id"], int) or values["paper_id"] <= 0):
            raise ApiError(409, "最终快照中的卡片 %s 缺少有效 paper_id" % card_id)
        fields = {}
        prefix = "card_field:%s:" % card_id
        for key, unit in snapshot.items():
            if key.startswith(prefix):
                fields[key[len(prefix):]] = unit["content"]
        cards[card_id] = (values, fields)
    return cards


def _desired_structures(snapshot):
    result = {}
    for key, unit in snapshot.items():
        if not key.startswith("structure:"):
            continue
        section_id = int(key.split(":", 1)[1])
        content = unit["content"]
        if not isinstance(content, dict):
            raise ApiError(409, "structure:%s 的最终内容不是对象" % section_id)
        title = content.get("title", "")
        parent_id = content.get("parent_id")
        order = content.get("ord", 0)
        if not isinstance(title, str) or not _valid_int_or_none(parent_id):
            raise ApiError(409, "章节 %s 的标题或父节点无效" % section_id)
        if isinstance(order, bool) or not isinstance(order, int):
            raise ApiError(409, "章节 %s 的 ord 无效" % section_id)
        result[section_id] = (title, parent_id, order)
    return result


def _desired_blocks(snapshot):
    result = {}
    for key, unit in snapshot.items():
        if not key.startswith("block:"):
            continue
        block_id = int(key.split(":", 1)[1])
        content = unit["content"]
        if not isinstance(content, dict):
            raise ApiError(409, "block:%s 的最终内容不是对象" % block_id)
        section_id = content.get("section_id")
        order = content.get("ord", 0)
        text = content.get("text", "")
        if not _valid_int_or_none(section_id) or not isinstance(text, str):
            raise ApiError(409, "块 %s 的章节或正文无效" % block_id)
        if isinstance(order, bool) or not isinstance(order, int):
            raise ApiError(409, "块 %s 的 ord 无效" % block_id)
        result[block_id] = (section_id, order, text)
    return result


def _verify_final_excerpts(db, cards):
    for card_id, (values, unused_fields) in cards.items():
        if values["status"] != "verified":
            continue
        paper = db.execute(
            "SELECT full_text, abstract FROM papers WHERE id = ?", (values["paper_id"],)
        ).fetchone()
        if paper is None:
            raise ApiError(409, "已核验卡片 %s 的来源文献不存在" % card_id)
        if values["excerpt_status"] not in ("verbatim_verified", "structured_numeric"):
            raise ApiError(409, "已核验卡片 %s 的摘录状态不允许核验通过" % card_id)
        excerpt = values["excerpt"]
        if not isinstance(excerpt, str) or not excerpt.strip():
            raise ApiError(409, "已核验卡片 %s 的摘录为空" % card_id)
        if excerpt not in (paper["full_text"] or "") and excerpt not in (paper["abstract"] or ""):
            raise ApiError(409, "已核验卡片 %s 的摘录不是当前文献原文子串" % card_id)


def _apply_main_projection(db, snapshot):
    """Reconcile only whitelisted live columns inside the caller's transaction."""
    cards = _desired_cards(snapshot)
    structures = _desired_structures(snapshot)
    blocks = _desired_blocks(snapshot)
    for card_id, (values, fields) in cards.items():
        paper = db.execute("SELECT id FROM papers WHERE id = ?", (values["paper_id"],)).fetchone()
        if paper is None:
            raise ApiError(409, "卡片 %s 的来源文献不存在" % card_id)
        if values["template_id"] is not None and (
                isinstance(values["template_id"], bool) or
                not isinstance(values["template_id"], int)):
            raise ApiError(409, "卡片 %s 的 template_id 无效" % card_id)
        if values["confidence"] is not None and (
                isinstance(values["confidence"], bool) or
                not isinstance(values["confidence"], (int, float))):
            raise ApiError(409, "卡片 %s 的 confidence 无效" % card_id)
        for attr in ("field", "claim", "excerpt", "excerpt_status", "evidence_type", "status"):
            if values[attr] is not None and not isinstance(values[attr], str):
                raise ApiError(409, "卡片 %s 的 %s 无效" % (card_id, attr))
    _verify_final_excerpts(db, cards)

    existing_sections = {
        r["id"] for r in db.execute("SELECT id FROM sections").fetchall()
    }
    for section_id in sorted(existing_sections - set(structures)):
        db.execute("DELETE FROM card_sections WHERE section_id = ?", (section_id,))
        db.execute("DELETE FROM sections WHERE id = ?", (section_id,))
    for section_id, (title, parent_id, order) in structures.items():
        if section_id in existing_sections:
            db.execute(
                "UPDATE sections SET title = ?, parent_id = ?, ord = ? WHERE id = ?",
                (title, parent_id, order, section_id),
            )
        else:
            db.execute(
                "INSERT INTO sections(id, title, parent_id, ord) VALUES (?,?,?,?)",
                (section_id, title, parent_id, order),
            )

    existing_cards = {r["id"] for r in db.execute("SELECT id FROM cards").fetchall()}
    for card_id in sorted(existing_cards - set(cards)):
        db.execute("DELETE FROM card_sections WHERE card_id = ?", (card_id,))
        db.execute("DELETE FROM cards WHERE id = ?", (card_id,))
    update_sql = (
        "UPDATE cards SET paper_id = ?, template_id = ?, field = ?, claim = ?, excerpt = ?, "
        "excerpt_status = ?, evidence_type = ?, confidence = ?, status = ?, "
        "field_values_json = ? WHERE id = ?"
    )
    insert_sql = (
        "INSERT INTO cards(id, paper_id, template_id, field, claim, excerpt, excerpt_status, "
        "evidence_type, confidence, status, field_values_json) VALUES (?,?,?,?,?,?,?,?,?,?,?)"
    )
    for card_id, (values, fields) in cards.items():
        params = (
            values["paper_id"], values["template_id"], values["field"], values["claim"],
            values["excerpt"], values["excerpt_status"], values["evidence_type"],
            values["confidence"], values["status"], _canon(fields),
        )
        if card_id in existing_cards:
            db.execute(update_sql, params + (card_id,))
        else:
            db.execute(insert_sql, (card_id,) + params)

    existing_blocks = {r["id"] for r in db.execute("SELECT id FROM blocks").fetchall()}
    for block_id in sorted(existing_blocks - set(blocks)):
        db.execute("DELETE FROM blocks WHERE id = ?", (block_id,))
    for block_id, (section_id, order, text) in blocks.items():
        if block_id in existing_blocks:
            db.execute(
                "UPDATE blocks SET section_id = ?, ord = ?, text = ? WHERE id = ?",
                (section_id, order, text, block_id),
            )
        else:
            db.execute(
                "INSERT INTO blocks(id, section_id, ord, text) VALUES (?,?,?,?)",
                (block_id, section_id, order, text),
            )


def _tag_dict(row, include_snapshot=False):
    result = {
        "id": row["id"], "tag_id": row["id"], "name": row["name"],
        "commit_id": row["commit_id"], "author": row["author"],
        "created_at": row["created_at"],
    }
    if include_snapshot:
        result["snapshot"] = _loads(row["snapshot_json"], {})
    return result


def _api_view(get_db, fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        db = get_db()
        try:
            _ensure_main(db)
            return fn(db, *args, **kwargs)
        except ApiError as exc:
            db.rollback()
            return jsonify({"error": exc.message}), exc.status
        except sqlite3.IntegrityError:
            db.rollback()
            return jsonify({"error": "唯一性、状态或引用约束冲突"}), 409
    return wrapped


def register_collaboration(app, get_db):
    """Register all collaboration routes; no application context is acquired."""
    def add(rule, methods, endpoint, fn):
        app.add_url_rule(rule, endpoint, _api_view(get_db, fn), methods=methods)

    def list_branches(db):
        rows = db.execute("SELECT * FROM branches ORDER BY id").fetchall()
        return jsonify([_branch_dict(r) for r in rows])

    def create_branch(db):
        data = _json_body()
        name = _text(data, "name")
        kind = _text(data, "kind")
        if kind not in BRANCH_KINDS:
            raise ApiError(400, "kind 必须是 person/topic/section")
        owner = _text(data, "owner")
        purpose = _text(data, "purpose")
        if data.get("base_commit_id") is None:
            base_id = _ensure_main(db)["head_commit_id"]
        else:
            base_id = _integer(data.get("base_commit_id"), "base_commit_id", 1)
            _commit(db, base_id)
        if db.execute("SELECT id FROM branches WHERE name = ?", (name,)).fetchone():
            raise ApiError(409, "分支名称已存在")
        with _write(db, immediate=True):
            cur = db.execute(
                "INSERT INTO branches(name, kind, owner, purpose, base_commit_id, "
                "head_commit_id, created_at) VALUES (?,?,?,?,?,?,?)",
                (name, kind, owner, purpose, base_id, base_id, _now()),
            )
            branch_id = cur.lastrowid
            _audit(db, owner, "branch.create", "branch", branch_id,
                   {"name": name, "kind": kind, "purpose": purpose, "base_commit_id": base_id})
        return jsonify(_branch_dict(_branch(db, branch_id))), 201

    def branch_detail(db, branch_id):
        branch = _branch(db, branch_id)
        result = _branch_dict(branch)
        result["units"] = _snapshot(_commit(db, branch["head_commit_id"]))
        return jsonify(result)

    def branch_commits(db, branch_id):
        _branch(db, branch_id)
        rows = db.execute(
            "SELECT * FROM commits WHERE branch_id = ? ORDER BY id DESC", (branch_id,)
        ).fetchall()
        return jsonify([_commit_dict(r) for r in rows])

    def create_commit(db, branch_id):
        branch = _branch(db, branch_id)
        data = _json_body()
        author = _text(data, "author")
        message = _text(data, "message")
        raw_changes = data.get("changes")
        if not isinstance(raw_changes, list) or not raw_changes:
            raise ApiError(400, "changes 必须是非空数组")
        old_head = branch["head_commit_id"]
        current = _snapshot(_commit(db, old_head))
        new_snapshot = dict(current)
        changes = []
        seen = set()
        for raw in raw_changes:
            if not isinstance(raw, dict):
                raise ApiError(400, "每个 change 必须是对象")
            unit_type = raw.get("unit_type")
            unit_key = raw.get("unit_key")
            _validate_key(unit_type, unit_key)
            if unit_key in seen:
                raise ApiError(400, "changes 中 unit_key 不可重复")
            seen.add(unit_key)
            if "new_content" not in raw:
                raise ApiError(400, "每个 change 必须显式提供 new_content")
            new_content = raw["new_content"]
            _validate_new_content(unit_type, unit_key, new_content)
            old = current.get(unit_key, MISSING)
            if "old_hash" in raw:
                expected_hash = raw["old_hash"]
                if expected_hash is not None and not isinstance(expected_hash, str):
                    raise ApiError(400, "old_hash 必须是字符串或 null")
                actual_hash = None if old is MISSING else old["hash"]
                if expected_hash != actual_hash:
                    raise ApiError(409, "old_hash 与分支当前单元不匹配: %s" % unit_key)
            if "old_content" in raw:
                expected_content = raw["old_content"]
                _hash(expected_content)
                matches = ((old is MISSING and expected_content is None) or
                           (old is not MISSING and old["hash"] == _hash(expected_content)))
                if not matches:
                    raise ApiError(409, "old_content 与分支当前单元不匹配: %s" % unit_key)
            new = MISSING if new_content is None else _unit(unit_type, new_content)
            if _same(old, new):
                raise ApiError(400, "提交包含 no-op 变更: %s" % unit_key)
            if new is MISSING:
                new_snapshot.pop(unit_key, None)
            else:
                new_snapshot[unit_key] = new
            changes.append({
                "unit_type": unit_type, "unit_key": unit_key,
                "old_unit": old, "new_unit": new,
            })
        with _write(db, immediate=True):
            branch_now = _branch(db, branch_id)
            if branch_now["head_commit_id"] != old_head:
                raise ApiError(409, "分支 head 已改变，请重新基于最新提交")
            if branch["name"] == "main":
                _apply_main_projection(db, new_snapshot)
            commit_id = _insert_commit(
                db, branch_id, author, message, old_head, new_snapshot, changes,
            )
            updated = db.execute(
                "UPDATE branches SET head_commit_id = ? WHERE id = ? AND head_commit_id = ?",
                (commit_id, branch_id, old_head),
            )
            if updated.rowcount != 1:
                raise ApiError(409, "分支 head 更新冲突")
            _mark_head_changed(db, branch_id, reopen_source=True)
            _audit(db, author, "commit.create", "commit", commit_id,
                   {"branch_id": branch_id, "parent_commit_id": old_head,
                    "changes": [c["unit_key"] for c in changes]})
        return jsonify(_commit_detail(db, _commit(db, commit_id))), 201

    def commit_detail(db, commit_id):
        return jsonify(_commit_detail(db, _commit(db, commit_id)))

    def revert_commit(db, commit_id):
        original = _commit(db, commit_id)
        data = _json_body()
        author = _text(data, "author")
        supplied_message = data.get("message")
        reason = data.get("reason")
        if supplied_message is not None and not isinstance(supplied_message, str):
            raise ApiError(400, "message 必须是字符串")
        if reason is not None and not isinstance(reason, str):
            raise ApiError(400, "reason 必须是字符串")
        message = (supplied_message or "").strip() or (reason or "").strip()
        if not message:
            message = "Revert commit %s" % commit_id
        main = _ensure_main(db)
        if original["branch_id"] != main["id"] or main["head_commit_id"] != original["id"]:
            raise ApiError(409, "MVP 只允许回滚 main 当前 head 提交")
        with _write(db, immediate=True):
            main_now = _ensure_main(db)
            if main_now["head_commit_id"] != original["id"]:
                raise ApiError(409, "main head 已改变，无法回滚")
            current = _snapshot(original)
            new_snapshot = dict(current)
            changes = []
            rows = db.execute(
                "SELECT * FROM change_units WHERE commit_id = ? ORDER BY id", (commit_id,)
            ).fetchall()
            for row in rows:
                key = row["unit_key"]
                live = current.get(key, MISSING)
                live_hash = None if live is MISSING else live["hash"]
                if live_hash != row["new_hash"]:
                    raise ApiError(409, "后续工作已改变单元，拒绝静默回滚: %s" % key)
                if row["old_hash"] is None:
                    restored = MISSING
                    new_snapshot.pop(key, None)
                else:
                    restored = _stored_unit(
                        row["unit_type"], row["old_hash"], _loads(row["old_content_json"]),
                    )
                    new_snapshot[key] = restored
                if not _same(live, restored):
                    changes.append({
                        "unit_type": row["unit_type"], "unit_key": key,
                        "old_unit": live, "new_unit": restored,
                    })
            if not changes:
                raise ApiError(400, "回滚不会产生任何差异")
            _apply_main_projection(db, new_snapshot)
            new_id = _insert_commit(
                db, main["id"], author, message, original["id"], new_snapshot, changes,
                reverts_commit_id=original["id"],
            )
            db.execute("UPDATE branches SET head_commit_id = ? WHERE id = ?",
                       (new_id, main["id"]))
            _mark_head_changed(db, main["id"], reopen_source=True)
            _audit(db, author, "commit.revert", "commit", new_id,
                   {"reverts_commit_id": original["id"], "reason": reason,
                    "changes": [c["unit_key"] for c in changes]})
            _audit(db, author, "revert", "commit", original["id"],
                   {"new_commit_id": new_id})
        return jsonify(_commit_detail(db, _commit(db, new_id))), 201

    def list_mrs(db):
        status = request.args.get("status")
        if status is not None and status not in MR_OPEN_STATES + ("merged", "closed"):
            raise ApiError(400, "无效的合并请求状态")
        if status:
            rows = db.execute(
                "SELECT * FROM merge_requests WHERE status = ? ORDER BY id DESC", (status,)
            ).fetchall()
        else:
            rows = db.execute("SELECT * FROM merge_requests ORDER BY id DESC").fetchall()
        return jsonify([_mr_dict(r) for r in rows])

    def create_mr(db):
        data = _json_body()
        source_id = _integer(data.get("source_branch_id"), "source_branch_id", 1)
        target_id = _integer(data.get("target_branch_id"), "target_branch_id", 1)
        if source_id == target_id:
            raise ApiError(400, "源分支与目标分支必须不同")
        _branch(db, source_id, "源分支")
        _branch(db, target_id, "目标分支")
        author = _text(data, "author")
        title = _text(data, "title")
        required = _integer(data.get("required_approvals", 1), "required_approvals", 1)
        with _write(db, immediate=True):
            cur = db.execute(
                "INSERT INTO merge_requests(source_branch_id, target_branch_id, author, title, "
                "status, required_approvals, created_at) VALUES (?,?,?,?, 'open', ?, ?)",
                (source_id, target_id, author, title, required, _now()),
            )
            mr_id = cur.lastrowid
            mr = _mr(db, mr_id)
            preview = _compute_preview(db, mr)
            mr = _save_preview(db, mr, preview)
            _audit(db, author, "merge_request.create", "merge_request", mr_id,
                   {"source_branch_id": source_id, "target_branch_id": target_id,
                    "title": title, "conflicts": len(preview["found"])})
        return jsonify(_mr_payload(db, mr)), 201

    def mr_detail(db, mr_id):
        return jsonify(_mr_payload(db, _mr(db, mr_id)))

    def refresh_mr_preview(db, mr_id):
        with _write(db, immediate=True):
            mr = _mr(db, mr_id)
            if mr["status"] not in MR_OPEN_STATES:
                raise ApiError(409, "已合并或关闭的请求不能刷新预览")
            preview = _compute_preview(db, mr)
            mr = _save_preview(db, mr, preview)
        return jsonify(_mr_payload(db, mr))

    def create_review(db, mr_id):
        data = _json_body()
        reviewer = _text(data, "reviewer")
        verdict = data.get("verdict")
        if verdict not in ("approve", "request_changes"):
            raise ApiError(400, "verdict 必须是 approve/request_changes")
        comments = data.get("comments", [])
        if not isinstance(comments, list):
            raise ApiError(400, "comments 必须是数组")
        with _write(db, immediate=True):
            mr = _mr(db, mr_id)
            if mr["status"] not in MR_OPEN_STATES:
                raise ApiError(409, "已合并或关闭的请求不能审核")
            if verdict == "approve" and reviewer == mr["author"]:
                raise ApiError(400, "合并请求作者不可自我批准")
            preview = _compute_preview(db, mr)
            # Inline review must identify a unit changed by this source branch,
            # rather than an arbitrary unit merely present in some snapshot.
            valid_keys = set(preview["changed_unit_keys"])
            for comment in comments:
                if not isinstance(comment, dict):
                    raise ApiError(400, "评论必须是对象")
                unit_key = comment.get("unit_key")
                body = comment.get("body")
                if not isinstance(unit_key, str) or unit_key not in valid_keys:
                    raise ApiError(400, "评论的 unit_key 锚点无效")
                if not isinstance(body, str) or not body.strip():
                    raise ApiError(400, "评论正文不能为空")
            cur = db.execute(
                "INSERT INTO reviews(mr_id, reviewer, verdict, source_commit_id, created_at) "
                "VALUES (?,?,?,?,?)",
                (mr_id, reviewer, verdict, preview["source_id"], _now()),
            )
            review_id = cur.lastrowid
            for comment in comments:
                comment_cur = db.execute(
                    "INSERT INTO review_comments(review_id, unit_key, body, created_at) "
                    "VALUES (?,?,?,?)",
                    (review_id, comment["unit_key"], comment["body"].strip(), _now()),
                )
                _audit(db, reviewer, "review_comment.create", "review_comment",
                       comment_cur.lastrowid,
                       {"mr_id": mr_id, "review_id": review_id,
                        "unit_key": comment["unit_key"]})
            mr = _mr(db, mr_id)
            state = _review_state(db, mr, preview["source_id"])
            if state["request_changes"]:
                status = "needs_changes"
            elif state["approvals"] >= mr["required_approvals"]:
                status = "approved"
            else:
                status = "open"
            db.execute("UPDATE merge_requests SET status = ? WHERE id = ?", (status, mr_id))
            _audit(db, reviewer, "review.create", "review", review_id,
                   {"mr_id": mr_id, "verdict": verdict,
                    "source_commit_id": preview["source_id"],
                    "comments": len(comments), "mr_status": status})
        review = db.execute("SELECT * FROM reviews WHERE id = ?", (review_id,)).fetchone()
        result = dict(review)
        result["review_id"] = review_id
        result["comments"] = [dict(c) for c in db.execute(
            "SELECT * FROM review_comments WHERE review_id = ? ORDER BY id", (review_id,)
        ).fetchall()]
        result["mr_status"] = status
        return jsonify(result), 201

    def resolve_conflict(db, conflict_id):
        data = _json_body()
        resolution = data.get("resolution")
        if resolution == "keep_ours":
            resolution = "keep_base"
        if not isinstance(resolution, str) or resolution not in RESOLUTIONS:
            raise ApiError(400, "resolution 必须是 keep_base/keep_branch/manual")
        actor = _text(data, "actor")
        if resolution == "manual" and "manual_content" not in data:
            raise ApiError(400, "manual 必须显式提供 manual_content（可为 null）")
        with _write(db, immediate=True):
            row = db.execute("SELECT * FROM conflicts WHERE id = ?", (conflict_id,)).fetchone()
            if row is None:
                raise ApiError(404, "冲突不存在")
            mr = _mr(db, row["mr_id"])
            if mr["status"] not in MR_OPEN_STATES:
                raise ApiError(409, "已合并或关闭请求的冲突不可解决")
            preview = _compute_preview(db, mr)
            if (not row["active"] or mr["preview_signature"] != preview["signature"] or
                    row["preview_signature"] != preview["signature"]):
                raise ApiError(409, "冲突已过期或预览 stale，请刷新预览")
            matching = None
            for found in preview["found"]:
                if found["unit_key"] == row["unit_key"]:
                    matching = found
                    break
            if matching is None:
                raise ApiError(409, "冲突已不属于当前三方预览")
            b, o, t = matching["base_unit"], matching["ours_unit"], matching["theirs_unit"]
            if (row["base_hash"] != (None if b is MISSING else b["hash"]) or
                    row["ours_hash"] != (None if o is MISSING else o["hash"]) or
                    row["theirs_hash"] != (None if t is MISSING else t["hash"])):
                raise ApiError(409, "冲突三侧内容已改变，请刷新预览")
            if resolution == "keep_base":
                selected = o
                content = None if o is MISSING else o["content"]
                content_hash = None if o is MISSING else o["hash"]
            elif resolution == "keep_branch":
                selected = t
                content = None if t is MISSING else t["content"]
                content_hash = None if t is MISSING else t["hash"]
            else:
                selected = MISSING if data["manual_content"] is None else data["manual_content"]
                content = data["manual_content"]
                _validate_new_content(row["unit_type"], row["unit_key"], content)
                content_hash = None if content is None else _hash(content)
            if selected is MISSING:
                resolved_json = None
            elif resolution == "manual":
                resolved_json = _canon(content)
            else:
                resolved_json = _unit_json(selected)
            db.execute(
                "UPDATE conflicts SET resolution = ?, resolved_content_json = ?, "
                "resolved_content_hash = ?, resolved_by = ?, resolved_at = ? WHERE id = ?",
                (resolution, resolved_json, content_hash, actor, _now(), conflict_id),
            )
            _audit(db, actor, "conflict.resolve", "conflict", conflict_id,
                   {"mr_id": mr["id"], "unit_key": row["unit_key"],
                    "resolution": resolution, "resolved_content_hash": content_hash})
        row = db.execute("SELECT * FROM conflicts WHERE id = ?", (conflict_id,)).fetchone()
        return jsonify(_conflict_dict(row))

    def merge_mr(db, mr_id):
        data = _json_body()
        actor = _text(data, "actor")
        with _write(db, immediate=True):
            mr = _mr(db, mr_id)
            if mr["status"] not in MR_OPEN_STATES:
                raise ApiError(409, "只有 open/needs_changes/approved 请求可合并")
            preview = _compute_preview(db, mr)
            if mr["preview_signature"] != preview["signature"]:
                raise ApiError(409, "合并预览 stale，请重新执行 preview")
            review_state = _review_state(db, mr, preview["source_id"])
            if review_state["request_changes"]:
                raise ApiError(409, "当前 source head 存在 request_changes，禁止合并")
            if review_state["approvals"] < mr["required_approvals"]:
                raise ApiError(409, "当前 source head 的有效批准数不足")
            active = db.execute(
                "SELECT * FROM conflicts WHERE mr_id = ? AND active = 1 ORDER BY id", (mr_id,)
            ).fetchall()
            found_by_key = {c["unit_key"]: c for c in preview["found"]}
            if {r["unit_key"] for r in active} != set(found_by_key):
                raise ApiError(409, "活动冲突集合与当前预览不一致，请刷新预览")
            final = dict(preview["merged"])
            for conflict in active:
                found = found_by_key[conflict["unit_key"]]
                b, o, t = found["base_unit"], found["ours_unit"], found["theirs_unit"]
                if (conflict["preview_signature"] != preview["signature"] or
                        conflict["base_hash"] != (None if b is MISSING else b["hash"]) or
                        conflict["ours_hash"] != (None if o is MISSING else o["hash"]) or
                        conflict["theirs_hash"] != (None if t is MISSING else t["hash"])):
                    raise ApiError(409, "冲突三侧签名不匹配，请刷新预览")
                if conflict["resolution"] is None:
                    raise ApiError(409, "仍有未解决冲突: %s" % conflict["unit_key"])
                key = conflict["unit_key"]
                if conflict["resolved_content_hash"] is None:
                    final.pop(key, None)
                else:
                    content = _loads(conflict["resolved_content_json"])
                    final[key] = _stored_unit(
                        conflict["unit_type"], conflict["resolved_content_hash"], content,
                    )
            target = preview["target_branch"]
            if target["name"] == "main":
                _apply_main_projection(db, final)
            changes = _diff(preview["target_snapshot"], final)
            merge_commit_id = _insert_commit(
                db, target["id"], actor, "Merge MR #%s: %s" % (mr_id, mr["title"]),
                preview["target_id"], final, changes, merge_parent_id=preview["source_id"],
            )
            updated = db.execute(
                "UPDATE branches SET head_commit_id = ? WHERE id = ? AND head_commit_id = ?",
                (merge_commit_id, target["id"], preview["target_id"]),
            )
            if updated.rowcount != 1:
                raise ApiError(409, "目标分支 head 更新冲突")
            db.execute(
                "UPDATE merge_requests SET status = 'merged', merged_commit_id = ? WHERE id = ?",
                (merge_commit_id, mr_id),
            )
            _mark_head_changed(db, target["id"], reopen_source=True)
            _audit(db, actor, "commit.merge", "commit", merge_commit_id,
                   {"mr_id": mr_id, "parent_commit_id": preview["target_id"],
                    "merge_parent_id": preview["source_id"],
                    "changes": [c["unit_key"] for c in changes]})
            _audit(db, actor, "merge_request.merge", "merge_request", mr_id,
                   {"merge_commit_id": merge_commit_id, "target_branch_id": target["id"],
                    "resolved_conflicts": len(active)})
        return jsonify({
            "merge_commit_id": merge_commit_id, "commit_id": merge_commit_id,
            "id": merge_commit_id, "mr_id": mr_id, "status": "merged",
        })

    def list_tags(db):
        rows = db.execute("SELECT * FROM tags ORDER BY id").fetchall()
        return jsonify([_tag_dict(r) for r in rows])

    def create_tag(db):
        data = _json_body()
        name = _text(data, "name")
        commit_id = _integer(data.get("commit_id"), "commit_id", 1)
        author = _text(data, "author", required=False, allow_none=True)
        commit = _commit(db, commit_id)
        main = _ensure_main(db)
        if commit["branch_id"] != main["id"]:
            raise ApiError(400, "标签只能指向 main 分支直接拥有的提交")
        if db.execute("SELECT id FROM tags WHERE name = ?", (name,)).fetchone():
            raise ApiError(409, "标签名称已存在")
        with _write(db, immediate=True):
            cur = db.execute(
                "INSERT INTO tags(name, commit_id, author, snapshot_json, created_at) "
                "VALUES (?,?,?,?,?)",
                (name, commit_id, author, commit["snapshot_json"], _now()),
            )
            tag_id = cur.lastrowid
            _audit(db, author, "tag.create", "tag", tag_id,
                   {"name": name, "commit_id": commit_id})
        return jsonify(_tag_dict(db.execute(
            "SELECT * FROM tags WHERE id = ?", (tag_id,)
        ).fetchone())), 201

    def tag_detail(db, tag_id):
        row = db.execute("SELECT * FROM tags WHERE id = ?", (tag_id,)).fetchone()
        if row is None:
            raise ApiError(404, "标签不存在")
        return jsonify(_tag_dict(row, include_snapshot=True))

    def audit_logs(db):
        rows = db.execute("SELECT * FROM audit_logs ORDER BY id DESC").fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = _loads(item["details_json"], {})
            result.append(item)
        return jsonify(result)

    add("/api/branches", ["GET"], "collaboration_list_branches", list_branches)
    add("/api/branches", ["POST"], "collaboration_create_branch", create_branch)
    add("/api/branches/<int:branch_id>", ["GET"], "collaboration_branch_detail", branch_detail)
    add("/api/branches/<int:branch_id>/commits", ["GET"],
        "collaboration_branch_commits", branch_commits)
    add("/api/branches/<int:branch_id>/commits", ["POST"],
        "collaboration_create_commit", create_commit)
    add("/api/commits/<int:commit_id>", ["GET"], "collaboration_commit_detail", commit_detail)
    add("/api/commits/<int:commit_id>/revert", ["POST"],
        "collaboration_revert_commit", revert_commit)
    add("/api/merge-requests", ["GET"], "collaboration_list_mrs", list_mrs)
    add("/api/merge-requests", ["POST"], "collaboration_create_mr", create_mr)
    add("/api/merge-requests/<int:mr_id>", ["GET"], "collaboration_mr_detail", mr_detail)
    add("/api/merge-requests/<int:mr_id>/preview", ["GET", "POST"],
        "collaboration_refresh_mr_preview", refresh_mr_preview)
    add("/api/merge-requests/<int:mr_id>/reviews", ["POST"],
        "collaboration_create_review", create_review)
    add("/api/merge-requests/conflicts/<int:conflict_id>/resolve", ["POST"],
        "collaboration_resolve_conflict", resolve_conflict)
    add("/api/merge-requests/<int:mr_id>/merge", ["POST"],
        "collaboration_merge_mr", merge_mr)
    add("/api/tags", ["GET"], "collaboration_list_tags", list_tags)
    add("/api/tags", ["POST"], "collaboration_create_tag", create_tag)
    add("/api/tags/<int:tag_id>", ["GET"], "collaboration_tag_detail", tag_detail)
    add("/api/audit-logs", ["GET"], "collaboration_audit_logs", audit_logs)
