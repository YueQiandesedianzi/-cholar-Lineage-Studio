# -*- coding: utf-8 -*-
"""Σcholar Lineage Studio —— 导入 → 提取 → 核验 → 框架 → 协作与版本。
单进程 Flask + SQLite。AI 提取为 Mock 规则抽取（抽取真实原文子串），
接口预留真实 LLM 替换点（见 mock_extract 注释）。
collaboration.py 提供本地单项目的 ChangeUnit 历史与三方合并，接口见 README.md。
"""
import os
import re
import json
import sqlite3

from flask import Flask, g, jsonify, request, render_template

from collaboration import COLLABORATION_SCHEMA, register_collaboration

DB_PATH = os.environ.get(
    "REVIEWGIT_DB",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "reviewgit.db"),
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS papers (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL,
  authors TEXT,
  year TEXT,
  doi TEXT,
  abstract TEXT,
  full_text TEXT,
  created_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS templates (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  schema_json TEXT NOT NULL,
  is_active INTEGER DEFAULT 1,
  created_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS cards (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  paper_id INTEGER NOT NULL,
  template_id INTEGER,
  field TEXT,
  claim TEXT,
  excerpt TEXT,
  excerpt_status TEXT DEFAULT 'verbatim_verified',
  evidence_type TEXT DEFAULT 'claim',
  confidence REAL DEFAULT 0.8,
  status TEXT DEFAULT 'pending',
  field_values_json TEXT DEFAULT '{}',
  created_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS sections (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL,
  parent_id INTEGER,
  ord INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS card_sections (
  card_id INTEGER NOT NULL,
  section_id INTEGER NOT NULL,
  PRIMARY KEY (card_id, section_id)
);
""" + COLLABORATION_SCHEMA

DEFAULT_TEMPLATE = {
    "name": "通用提取模板",
    "fields": ["研究问题", "方法", "主要发现", "结论"],
}

DEFAULT_SECTIONS = ["引言", "方法", "结果", "讨论"]


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


def close_db(e=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    if conn.execute("SELECT COUNT(*) c FROM templates").fetchone()["c"] == 0:
        conn.execute(
            "INSERT INTO templates (name, schema_json, is_active) VALUES (?, ?, 1)",
            (DEFAULT_TEMPLATE["name"],
             json.dumps({"fields": [{"name": f, "type": "text"} for f in DEFAULT_TEMPLATE["fields"]]},
                        ensure_ascii=False)),
        )
    if conn.execute("SELECT COUNT(*) c FROM sections").fetchone()["c"] == 0:
        for i, s in enumerate(DEFAULT_SECTIONS):
            conn.execute("INSERT INTO sections (title, ord) VALUES (?, ?)", (s, i))
    conn.commit()
    conn.close()


def split_sentences(text):
    if not text:
        return []
    parts = re.split(r"(?<=[。！？.!?])\s*", text)
    return [p.strip() for p in parts if p.strip()]


def mock_extract(paper_text, fields):
    """Mock 抽取：为每个模板字段取一条真实原文句子作为逐字摘录。
    替换点：在此处接入真实 LLM Provider，返回同样结构的 CardDraft，
    服务层仍执行同样的子串校验（excerpt 必须来自 paper_text）。
    """
    sentences = split_sentences(paper_text)
    if not sentences:
        sentences = ["（未提供可抽取的正文文本）"]
    drafts = []
    for i, f in enumerate(fields):
        sent = sentences[i % len(sentences)]
        claim = sent[:60] + ("…" if len(sent) > 60 else "")
        drafts.append({
            "field": f,
            "claim": claim,
            "excerpt": sent,
            "excerpt_status": "verbatim_verified",
            "evidence_type": "claim",
            "confidence": round(0.8 + (i % 3) * 0.05, 2),
            "field_values": {f: claim},
        })
    return drafts


app = Flask(__name__)
app.teardown_appcontext(close_db)
init_db()


# ---------- 页面 ----------
@app.route("/")
def index():
    return render_template("index.html")


# ---------- 文献 ----------
@app.route("/api/papers", methods=["GET"])
def list_papers():
    rows = get_db().execute("SELECT * FROM papers ORDER BY id DESC").fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/papers", methods=["POST"])
def create_paper():
    d = request.get_json(force=True, silent=True) or {}
    title = (d.get("title") or "").strip()
    abstract = (d.get("abstract") or "").strip()
    if not title or not abstract:
        return jsonify({"error": "标题与摘要均为必填"}), 400
    db = get_db()
    cur = db.execute(
        "INSERT INTO papers (title, authors, year, doi, abstract, full_text) VALUES (?,?,?,?,?,?)",
        (title, d.get("authors", "").strip(), d.get("year", "").strip(),
         d.get("doi", "").strip(), abstract, abstract),
    )
    db.commit()
    return jsonify({"id": cur.lastrowid}), 201


# ---------- 模板 ----------
@app.route("/api/templates", methods=["GET"])
def list_templates():
    rows = get_db().execute("SELECT * FROM templates ORDER BY id").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["schema"] = json.loads(d.pop("schema_json"))
        out.append(d)
    return jsonify(out)


@app.route("/api/templates", methods=["POST"])
def create_template():
    d = request.get_json(force=True, silent=True) or {}
    name = (d.get("name") or "").strip()
    fields = [f.strip() for f in (d.get("fields") or []) if f.strip()]
    if not name or not fields:
        return jsonify({"error": "模板名与至少一个字段必填"}), 400
    schema = {"fields": [{"name": f, "type": "text"} for f in fields]}
    db = get_db()
    cur = db.execute("INSERT INTO templates (name, schema_json, is_active) VALUES (?, ?, 1)",
                     (name, json.dumps(schema, ensure_ascii=False)))
    db.commit()
    return jsonify({"id": cur.lastrowid}), 201


@app.route("/api/templates/<int:tid>/activate", methods=["POST"])
def activate_template(tid):
    db = get_db()
    db.execute("UPDATE templates SET is_active = 0")
    db.execute("UPDATE templates SET is_active = 1 WHERE id = ?", (tid,))
    db.commit()
    return jsonify({"ok": True})


# ---------- 提取 ----------
@app.route("/api/extract", methods=["POST"])
def extract():
    d = request.get_json(force=True, silent=True) or {}
    paper_id = d.get("paper_id")
    template_id = d.get("template_id")
    db = get_db()
    paper = db.execute("SELECT * FROM papers WHERE id = ?", (paper_id,)).fetchone()
    tpl = db.execute("SELECT * FROM templates WHERE id = ?", (template_id,)).fetchone()
    if not paper or not tpl:
        return jsonify({"error": "文献或模板不存在"}), 404
    fields = [f["name"] for f in json.loads(tpl["schema_json"])["fields"]]
    drafts = mock_extract(paper["full_text"] or paper["abstract"], fields)
    for dr in drafts:
        db.execute(
            "INSERT INTO cards (paper_id, template_id, field, claim, excerpt, excerpt_status, "
            "evidence_type, confidence, status, field_values_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (paper_id, template_id, dr["field"], dr["claim"], dr["excerpt"],
             dr["excerpt_status"], dr["evidence_type"], dr["confidence"],
             "pending", json.dumps(dr["field_values"], ensure_ascii=False)),
        )
    db.commit()
    return jsonify({"created": len(drafts)})


@app.route("/api/cards", methods=["GET"])
def list_cards():
    paper_id = request.args.get("paper_id")
    status = request.args.get("status")
    q = "SELECT c.*, p.title AS paper_title FROM cards c JOIN papers p ON p.id = c.paper_id"
    conds, args = [], []
    if paper_id:
        conds.append("c.paper_id = ?"); args.append(paper_id)
    if status:
        conds.append("c.status = ?"); args.append(status)
    if conds:
        q += " WHERE " + " AND ".join(conds)
    q += " ORDER BY c.id"
    rows = get_db().execute(q, args).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["field_values"] = json.loads(d.pop("field_values_json"))
        out.append(d)
    return jsonify(out)


@app.route("/api/cards/<int:cid>/verify", methods=["POST"])
def verify_card(cid):
    d = request.get_json(force=True, silent=True) or {}
    action = d.get("action")
    if action not in ("approve", "reject", "revise"):
        return jsonify({"error": "action 必须是 approve/reject/revise"}), 400
    db = get_db()
    card = db.execute(
        "SELECT c.*, COALESCE(p.full_text, p.abstract, '') AS source_text "
        "FROM cards c JOIN papers p ON p.id = c.paper_id WHERE c.id = ?", (cid,)
    ).fetchone()
    if not card:
        return jsonify({"error": "卡片不存在"}), 404
    if action in ("approve", "revise"):
        if card["excerpt_status"] not in ("verbatim_verified", "structured_numeric"):
            return jsonify({"error": "摘录未通过原文校验，不能核验通过"}), 400
        if not card["excerpt"] or card["excerpt"] not in card["source_text"]:
            return jsonify({"error": "摘录不是来源文本的逐字子串，不能核验通过"}), 400
    if action == "approve":
        db.execute("UPDATE cards SET status = 'verified' WHERE id = ?", (cid,))
    elif action == "reject":
        db.execute("UPDATE cards SET status = 'rejected' WHERE id = ?", (cid,))
    elif action == "revise":
        claim = (d.get("claim") or "").strip()
        if not claim:
            return jsonify({"error": "revise 需要新的 claim"}), 400
        db.execute("UPDATE cards SET claim = ?, status = 'verified' WHERE id = ?", (claim, cid))
    db.commit()
    return jsonify({"ok": True})


# ---------- 综述框架 ----------
@app.route("/api/sections", methods=["GET"])
def list_sections():
    rows = get_db().execute("SELECT * FROM sections ORDER BY ord, id").fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/sections", methods=["POST"])
def create_section():
    d = request.get_json(force=True, silent=True) or {}
    title = (d.get("title") or "").strip()
    if not title:
        return jsonify({"error": "章节标题必填"}), 400
    db = get_db()
    n = db.execute("SELECT COUNT(*) c FROM sections").fetchone()["c"]
    cur = db.execute("INSERT INTO sections (title, parent_id, ord) VALUES (?, ?, ?)",
                     (title, d.get("parent_id"), n))
    db.commit()
    return jsonify({"id": cur.lastrowid}), 201


@app.route("/api/sections/<int:sid>", methods=["PUT"])
def rename_section(sid):
    d = request.get_json(force=True, silent=True) or {}
    title = (d.get("title") or "").strip()
    if not title:
        return jsonify({"error": "章节标题必填"}), 400
    db = get_db()
    db.execute("UPDATE sections SET title = ? WHERE id = ?", (title, sid))
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/sections/<int:sid>", methods=["DELETE"])
def delete_section(sid):
    db = get_db()
    db.execute("DELETE FROM card_sections WHERE section_id = ?", (sid,))
    db.execute("DELETE FROM sections WHERE id = ?", (sid,))
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/sections/suggest", methods=["POST"])
def suggest_sections():
    """Mock：基于文献标题/摘要的"AI 建议框架"。MVP 返回可配置的默认骨架。"""
    db = get_db()
    if db.execute("SELECT COUNT(*) c FROM sections").fetchone()["c"] == 0:
        for i, s in enumerate(DEFAULT_SECTIONS):
            db.execute("INSERT INTO sections (title, ord) VALUES (?, ?)", (s, i))
        db.commit()
        return jsonify({"suggested": DEFAULT_SECTIONS})
    return jsonify({"suggested": [], "note": "已有章节，未覆盖"})


@app.route("/api/cards/<int:cid>/assign", methods=["POST"])
def assign_card(cid):
    d = request.get_json(force=True, silent=True) or {}
    section_id = d.get("section_id")
    db = get_db()
    db.execute("INSERT OR REPLACE INTO card_sections (card_id, section_id) VALUES (?, ?)",
               (cid, section_id))
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/sections/<int:sid>/cards", methods=["GET"])
def section_cards(sid):
    rows = get_db().execute(
        "SELECT c.*, p.title AS paper_title FROM cards c "
        "JOIN card_sections cs ON cs.card_id = c.id "
        "JOIN papers p ON p.id = c.paper_id WHERE cs.section_id = ? ORDER BY c.id", (sid,)
    ).fetchall()
    return jsonify([dict(r) for r in rows])


# 协作引擎独立维护 ChangeUnit 历史；常规列表是 main 的活动投影。
register_collaboration(app, get_db)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
