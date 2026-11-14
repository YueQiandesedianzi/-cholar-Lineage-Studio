# -*- coding: utf-8 -*-
"""导出：把已核验证据整理成交付物（Markdown 证据稿 / CSV 证据表 / JSON）。

约定：
- 只有 `verified` 卡片进入正文；其他状态在末尾以"未采用卡片"清单列出。
- 引用信息只用库里真实存在的字段（作者、年份、标题、DOI），不编造页码或期刊。
"""
import csv
import io

STATUS_LABELS = {
    "pending": "待核验", "verified": "已核验",
    "rejected": "已驳回", "needs_revision": "需修订",
}
EXCERPT_LABELS = {
    "verbatim_verified": "原文逐字", "structured_numeric": "结构化数值",
    "unverified": "未通过原文校验",
}


def _paper_label(paper):
    if not paper:
        return "（来源缺失）"
    authors = (paper.get("authors") or "").strip()
    year = (paper.get("year") or "").strip()
    title = (paper.get("title") or "").strip()
    head = authors.split(";")[0].strip() if authors else ""
    label = title
    if head:
        label = f"{head} ({year or 'n.d.'})《{title}》" if title else f"{head} ({year or 'n.d.'})"
    if paper.get("doi"):
        label += f" DOI: {paper['doi']}"
    return label


def _citation_line(paper, index):
    authors = (paper.get("authors") or "").strip() or "（未署名）"
    year = (paper.get("year") or "").strip() or "n.d."
    title = (paper.get("title") or "").strip()
    line = f"[{index}] {authors}. {title}. {year}."
    if paper.get("doi"):
        line += f" DOI: {paper['doi']}"
    return line


def build_markdown(papers, cards, sections, mounted, generated_at):
    """papers/cards 为 dict 列表，sections 有序，mounted 为 {section_id: [card_dict]}。"""
    paper_by_id = {paper["id"]: paper for paper in papers}
    verified = [card for card in cards if card["status"] == "verified"]
    others = [card for card in cards if card["status"] != "verified"]
    cited_ids = []
    for card in verified:
        if card["paper_id"] not in cited_ids:
            cited_ids.append(card["paper_id"])

    out = ["# 综述证据稿（自动生成）", ""]
    out.append(f"> 生成时间：{generated_at}　·　已核验卡片 {len(verified)} 张　·　"
               f"章节 {len(sections)} 个　·　涉及文献 {len(cited_ids)} 篇")
    out.append("")
    out.append("> 本文只收录状态为「已核验」的卡片；未采用卡片列在文末，便于复核。")
    out.append("")

    for index, section in enumerate(sections, start=1):
        out.append(f"## {index}. {section['title']}")
        out.append("")
        cards_here = mounted.get(section["id"], [])
        if not cards_here:
            out.append("_（本节暂无已核验卡片）_")
            out.append("")
            continue
        for card in cards_here:
            paper = paper_by_id.get(card["paper_id"])
            out.append(f"### 卡片 #{card['id']} · {card['field']}")
            out.append("")
            out.append(f"- **论断**：{card['claim']}")
            out.append(f"- **原文摘录**：{card['excerpt']}")
            out.append(f"- **摘录状态**：{EXCERPT_LABELS.get(card['excerpt_status'], card['excerpt_status'])}")
            out.append(f"- **来源**：{_paper_label(paper)}")
            out.append("")

    out.append("## 参考文献")
    out.append("")
    if cited_ids:
        for order, paper_id in enumerate(cited_ids, start=1):
            out.append(_citation_line(paper_by_id.get(paper_id, {}), order))
    else:
        out.append("_（暂无被引用的已核验证据）_")
    out.append("")

    if others:
        out.append("## 未采用卡片（供复核）")
        out.append("")
        out.append("| 卡片 | 状态 | 字段 | 论断 | 来源 |")
        out.append("|---|---|---|---|---|")
        for card in others:
            paper = paper_by_id.get(card["paper_id"], {})
            out.append("| #%d | %s | %s | %s | %s |" % (
                card["id"], STATUS_LABELS.get(card["status"], card["status"]),
                card["field"], _escape_cell(card["claim"]), _escape_cell(paper.get("title", "")),
            ))
        out.append("")
    return "\n".join(out)


def _escape_cell(text):
    return " ".join(str(text or "").replace("|", "\\|").split())


def build_csv(papers, cards, section_titles):
    """证据表：每张卡片一行，便于 Excel / pandas 复核。"""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow([
        "card_id", "status", "field", "claim", "excerpt", "excerpt_status",
        "evidence_type", "confidence", "paper_id", "paper_title", "authors",
        "year", "doi", "sections",
    ])
    paper_by_id = {paper["id"]: paper for paper in papers}
    for card in cards:
        paper = paper_by_id.get(card["paper_id"], {})
        writer.writerow([
            card["id"], STATUS_LABELS.get(card["status"], card["status"]), card["field"],
            card["claim"], card["excerpt"],
            EXCERPT_LABELS.get(card["excerpt_status"], card["excerpt_status"]),
            card["evidence_type"], card["confidence"], card["paper_id"],
            paper.get("title", ""), paper.get("authors", ""), paper.get("year", ""),
            paper.get("doi", ""),
            " / ".join(section_titles.get(section_id, "#%d" % section_id)
                       for section_id in card.get("section_ids", [])),
        ])
    return buffer.getvalue()


def find_substring_violations(cards, paper_text):
    """导出前自检：已核验卡片的摘录必须是来源文本的逐字子串。"""
    problems = []
    for card in cards:
        if card["status"] != "verified":
            continue
        text = paper_text.get(card["paper_id"], "")
        if not card["excerpt"] or card["excerpt"] not in text:
            problems.append({
                "card_id": card["id"], "field": card["field"],
                "excerpt": card["excerpt"], "paper_id": card["paper_id"],
            })
    return problems
