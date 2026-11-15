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
import tempfile
from datetime import datetime
from urllib.parse import quote

from flask import Flask, Response, g, jsonify, request, render_template

from collaboration import COLLABORATION_SCHEMA, record_audit, register_collaboration
from exporter import build_csv, build_markdown, find_substring_violations
from importer import clean_entry, dedupe_key, fetch_crossref, parse_bibtex, parse_doi_lines, parse_ris

DB_PATH = os.environ.get(
    "REVIEWGIT_DB",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "reviewgit.db"),
)
SCHEMA_VERSION = 1
BACKUP_MAX_BYTES = 64 * 1024 * 1024

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


def close_db(_exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def schema_version(conn):
    return conn.execute("PRAGMA user_version").fetchone()[0]


def apply_migrations(conn):
    """把数据库结构升到 SCHEMA_VERSION；结构变更时在这里追加迁移步骤。"""
    current = schema_version(conn)
    if current == SCHEMA_VERSION:
        return current
    if current > SCHEMA_VERSION:
        raise RuntimeError(
            "数据库结构版本 %d 高于本程序支持的 %d，请升级程序或改用其他数据库"
            % (current, SCHEMA_VERSION)
        )
    # 版本 0 → 1：建表语句本身已用 IF NOT EXISTS，只需登记版本号
    conn.execute("PRAGMA user_version = %d" % SCHEMA_VERSION)
    conn.commit()
    return SCHEMA_VERSION


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    apply_migrations(conn)
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
app.config["MAX_CONTENT_LENGTH"] = BACKUP_MAX_BYTES + 1024 * 1024  # 备份恢复上传上限
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
    q = (
        "SELECT c.*, p.title AS paper_title, "
        "(SELECT group_concat(cs.section_id) FROM card_sections cs WHERE cs.card_id = c.id) "
        "AS section_ids FROM cards c JOIN papers p ON p.id = c.paper_id"
    )
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
        d["section_ids"] = sorted(
            int(part) for part in (d.pop("section_ids") or "").split(",") if part
        )
        out.append(d)
    return jsonify(out)


@app.route("/api/cards/<int:cid>/verify", methods=["POST"])
def verify_card(cid):
    d = request.get_json(force=True, silent=True) or {}
    action = d.get("action")
    if action not in ("approve", "reject", "revise", "reset"):
        return jsonify({"error": "action 必须是 approve/reject/revise/reset"}), 400
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
    previous_status = card["status"]
    previous_claim = card["claim"]
    actor = (d.get("actor") or "").strip() or "未署名"
    if action == "approve":
        db.execute("UPDATE cards SET status = 'verified' WHERE id = ?", (cid,))
    elif action == "reject":
        db.execute("UPDATE cards SET status = 'rejected' WHERE id = ?", (cid,))
    elif action == "revise":
        claim = (d.get("claim") or "").strip()
        if not claim:
            return jsonify({"error": "revise 需要新的 claim"}), 400
        db.execute("UPDATE cards SET claim = ?, status = 'verified' WHERE id = ?", (claim, cid))
    elif action == "reset":
        # 撤回核验结论，回到待核验；摘录校验只约束"通过"，不约束撤回。
        db.execute("UPDATE cards SET status = 'pending' WHERE id = ?", (cid,))
    new_status = db.execute("SELECT status, claim FROM cards WHERE id = ?", (cid,)).fetchone()
    record_audit(db, actor, "card.verify." + action, "card", cid, {
        "from_status": previous_status, "to_status": new_status["status"],
        "claim_changed": new_status["claim"] != previous_claim,
        "previous_claim": previous_claim, "claim": new_status["claim"],
    })
    db.commit()
    return jsonify({"ok": True, "status": new_status["status"]})


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
    actor = (d.get("actor") or "").strip() or "未署名"
    record_audit(db, actor, "card.assign", "card", cid, {"section_id": section_id})
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


# ---------- 健康检查与备份 / 恢复 ----------

OPTIONAL_TABLES = ("branches", "commits", "merge_requests", "tags", "audit_logs")
REQUIRED_TABLES = ("papers", "templates", "cards", "sections", "card_sections")


def _table_count(db, table):
    try:
        return db.execute("SELECT COUNT(*) c FROM %s" % table).fetchone()["c"]
    except sqlite3.OperationalError:
        return None


@app.route("/api/health", methods=["GET"])
def health():
    db = get_db()
    counts = {table: _table_count(db, table) for table in REQUIRED_TABLES + OPTIONAL_TABLES}
    checks = []
    writable = True
    try:
        db.execute("SAVEPOINT health_probe")
        db.execute("ROLLBACK TO health_probe")
        db.execute("RELEASE health_probe")
    except sqlite3.Error:
        writable = False
    checks.append({"name": "数据库可写", "ok": writable, "severity": "error"})

    bad_excerpts = 0
    for row in db.execute(
        "SELECT c.excerpt, COALESCE(p.full_text, p.abstract, '') AS source FROM cards c "
        "JOIN papers p ON p.id = c.paper_id WHERE c.status = 'verified'"
    ):
        if not row["excerpt"] or row["excerpt"] not in row["source"]:
            bad_excerpts += 1
    checks.append({
        "name": "已核验卡片摘录均为原文子串",
        "ok": bad_excerpts == 0,
        "severity": "error",
        "detail": "" if bad_excerpts == 0 else "有 %d 张需复核" % bad_excerpts,
    })
    branches = counts.get("branches") or 0
    checks.append({
        "name": "协作历史已引导",
        "ok": branches > 0,
        "severity": "info",
        "detail": ("已有 %d 个分支" % branches) if branches
                  else "首次访问协作接口时会自动引导 main 根提交",
    })
    return jsonify({
        "status": "ok" if all(item["ok"] for item in checks if item["severity"] == "error") else "warn",
        "schema_version": schema_version(db),
        "expected_schema_version": SCHEMA_VERSION,
        "db_path": DB_PATH,
        "counts": counts,
        "checks": checks,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    })


@app.route("/api/backup/db", methods=["GET"])
def backup_db():
    db = get_db()
    handle = tempfile.NamedTemporaryFile(delete=False, suffix=".sqlite3")
    handle.close()
    target = sqlite3.connect(handle.name)
    try:
        db.backup(target)  # 一致性快照，包含 WAL 中未落盘的数据
    finally:
        target.close()
    with open(handle.name, "rb") as source:
        payload = source.read()
    os.unlink(handle.name)
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    return _attachment(payload, "reviewgit-backup-%s.sqlite3" % stamp,
                       "application/octet-stream")


@app.route("/api/backup/restore", methods=["POST"])
def restore_db():
    upload = request.files.get("file")
    if upload is None:
        return jsonify({"error": "请以 form-data 上传字段名为 file 的 SQLite 文件"}), 400
    raw = upload.read(BACKUP_MAX_BYTES + 1)
    if len(raw) > BACKUP_MAX_BYTES:
        return jsonify({"error": "文件超过 64 MB 上限"}), 413
    if not raw.startswith(b"SQLite format 3\x00"):
        return jsonify({"error": "这不是 SQLite 数据库文件（文件头不匹配）"}), 400

    handle = tempfile.NamedTemporaryFile(delete=False, suffix=".sqlite3")
    handle.write(raw)
    handle.close()
    source = sqlite3.connect(handle.name)
    try:
        tables = {row[0] for row in source.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        missing = [name for name in REQUIRED_TABLES if name not in tables]
        if missing:
            return jsonify({"error": "备份缺少必要数据表：%s" % "、".join(missing)}), 400
        db = get_db()
        source.backup(db)  # 直接覆盖当前库，避免替换文件时句柄失效
    except sqlite3.DatabaseError as exc:
        return jsonify({"error": "备份文件无法读取：%s" % exc}), 400
    finally:
        source.close()
        os.unlink(handle.name)

    actor = (request.form.get("actor") or "").strip() or "未署名"
    record_audit(db, actor, "db.restore", "database", None, {"bytes": len(raw),
                                                             "source": upload.filename})
    db.commit()
    return jsonify({"ok": True, "bytes": len(raw),
                    "counts": {table: _table_count(db, table) for table in REQUIRED_TABLES}})


# ---------- 题录导入 ----------

def _existing_keys(db):
    keys = set()
    for row in db.execute("SELECT title, doi FROM papers"):
        keys.add(dedupe_key({"doi": row["doi"], "title": row["title"]}))
    return keys


def _insert_paper(db, entry):
    cursor = db.execute(
        "INSERT INTO papers (title, authors, year, doi, abstract) VALUES (?,?,?,?,?)",
        (entry["title"], entry["authors"], entry["year"], entry["doi"], entry["abstract"]),
    )
    return cursor.lastrowid


@app.route("/api/papers/import", methods=["POST"])
def import_papers():
    d = request.get_json(force=True, silent=True) or {}
    fmt = (d.get("format") or "bibtex").lower()
    text = d.get("text") or ""
    use_network = d.get("resolve_doi", True) is not False

    if fmt == "bibtex":
        entries = parse_bibtex(text)
    elif fmt == "ris":
        entries = parse_ris(text)
    elif fmt == "doi":
        entries = [clean_entry({"doi": doi}) for doi in parse_doi_lines(text)]
    else:
        return jsonify({"error": "format 必须是 bibtex/ris/doi"}), 400
    if not entries:
        return jsonify({"error": "没有解析出任何题录，请检查文本格式"}), 400

    db = get_db()
    known = _existing_keys(db)
    created, skipped, failed = [], [], []
    for entry in entries:
        key = dedupe_key(entry)
        if key in known:
            skipped.append({"title": entry["title"], "doi": entry["doi"],
                            "reason": "已存在（DOI 或标题重复）"})
            continue
        if fmt == "doi":
            if not use_network:
                failed.append({"doi": entry["doi"],
                               "reason": "已关闭联网补全，请手工补全题录",
                               "manual": entry})
                continue
            try:
                entry = fetch_crossref(entry["doi"])
            except (OSError, ValueError) as exc:  # 网络不可达 / 响应不是合法 JSON
                failed.append({"doi": entry["doi"],
                               "reason": "Crossref 取题录失败：%s" % exc,
                               "manual": entry})
                continue
        if not entry["title"]:
            failed.append({"doi": entry["doi"], "reason": "缺少标题，无法建库", "manual": entry})
            continue
        paper_id = _insert_paper(db, entry)
        known.add(key)
        created.append({"id": paper_id, "title": entry["title"], "doi": entry["doi"]})
    record_audit(db, (d.get("actor") or "").strip() or "未署名", "paper.import", "paper", None, {
        "format": fmt, "created": len(created), "skipped": len(skipped), "failed": len(failed),
    })
    db.commit()
    return jsonify({
        "created": created, "skipped": skipped, "failed": failed,
        "summary": {"created": len(created), "skipped": len(skipped), "failed": len(failed)},
    }), 201


# ---------- 导出 ----------

def _export_context(db):
    papers = [dict(row) for row in db.execute("SELECT * FROM papers ORDER BY id")]
    cards = []
    for row in db.execute(
        "SELECT c.*, p.title AS paper_title FROM cards c "
        "JOIN papers p ON p.id = c.paper_id ORDER BY c.id"
    ):
        card = dict(row)
        card["section_ids"] = []
        cards.append(card)
    by_id = {card["id"]: card for card in cards}
    for row in db.execute(
        "SELECT cs.card_id, cs.section_id FROM card_sections cs ORDER BY cs.section_id"
    ):
        if row["card_id"] in by_id:
            by_id[row["card_id"]]["section_ids"].append(row["section_id"])
    sections = [dict(row) for row in db.execute("SELECT * FROM sections ORDER BY ord, id")]
    mounted = {}
    for card in cards:
        if card["status"] != "verified":
            continue
        for section_id in card["section_ids"]:
            mounted.setdefault(section_id, []).append(card)
    text_by_paper = {paper["id"]: (paper["full_text"] or paper["abstract"] or "")
                     for paper in papers}
    return papers, cards, sections, mounted, text_by_paper


def _attachment(payload, filename, mimetype):
    encoded = quote(filename.encode("utf-8"))
    disposition = "attachment; filename=%s; filename*=UTF-8''%s" % (encoded, encoded)
    return Response(payload, mimetype=mimetype, headers={"Content-Disposition": disposition})


@app.route("/api/export/markdown", methods=["GET"])
def export_markdown():
    db = get_db()
    papers, cards, sections, mounted, text_by_paper = _export_context(db)
    problems = find_substring_violations(cards, text_by_paper)
    if problems and request.args.get("force") != "1":
        return jsonify({
            "error": "有 %d 张已核验卡片的摘录不是来源原文子串，已阻止导出" % len(problems),
            "problems": problems,
            "hint": "修正这些卡片，或带 force=1 强制导出（导出稿会标注该风险）。",
        }), 409
    markdown = build_markdown(papers, cards, sections, mounted,
                              datetime.now().strftime("%Y-%m-%d %H:%M"))
    if problems:
        markdown = ("> ⚠️ 本次为强制导出：有 %d 张已核验卡片的摘录未通过原文子串校验。\n\n"
                    % len(problems)) + markdown
    return _attachment(markdown, "reviewgit-evidence.md", "text/markdown; charset=utf-8")


@app.route("/api/export/cards.csv", methods=["GET"])
def export_cards_csv():
    db = get_db()
    papers, cards, sections, _mounted, _text = _export_context(db)
    titles = {section["id"]: section["title"] for section in sections}
    return _attachment("\ufeff" + build_csv(papers, cards, titles),
                       "reviewgit-cards.csv", "text/csv; charset=utf-8")


@app.route("/api/export/cards.json", methods=["GET"])
def export_cards_json():
    db = get_db()
    papers, cards, sections, _mounted, _text = _export_context(db)
    payload = json.dumps({
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "papers": papers, "cards": cards, "sections": sections,
    }, ensure_ascii=False, indent=2)
    return _attachment(payload, "reviewgit-cards.json", "application/json; charset=utf-8")


# 协作引擎独立维护 ChangeUnit 历史；常规列表是 main 的活动投影。
register_collaboration(app, get_db)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
