# -*- coding: utf-8 -*-
"""题录导入：解析 BibTeX / RIS 文本，规范化去重键，可选联网补全 DOI 题录。

设计约束：
- 解析全程离线可用；只有 `fetch_crossref` 需要网络，且失败由调用方降级处理。
- 去重键：有 DOI 用小写 DOI，否则用标题规范化结果（去空格标点、转小写）。
"""
import json
import re
import urllib.error
import urllib.parse
import urllib.request

CROSSREF_UA = "ReviewGit/0.2 (https://github.com/YueQiandesedianzi/-cholar-Lineage-Studio)"


# ---------- 通用 ----------

def normalize_title(title):
    """标题规范化：去掉空白与常见标点，保留中日韩字符与字母数字。"""
    if not title:
        return ""
    text = "".join(ch for ch in str(title).lower()
                   if ch.isalnum() or "\u4e00" <= ch <= "\u9fff")
    return text


def dedupe_key(entry):
    doi = (entry.get("doi") or "").strip().lower()
    if doi:
        return "doi:" + doi
    return "title:" + normalize_title(entry.get("title"))


def clean_entry(entry):
    """补齐字段并去掉空值，供写库使用。"""
    authors = entry.get("authors") or ""
    if isinstance(authors, (list, tuple)):
        authors = "; ".join(str(a).strip() for a in authors if str(a).strip())
    return {
        "title": " ".join(str(entry.get("title") or "").split()),
        "authors": " ".join(str(authors).split()),
        "year": str(entry.get("year") or "")[:12],
        "doi": " ".join(str(entry.get("doi") or "").split()),
        "abstract": " ".join(str(entry.get("abstract") or "").split()),
    }


# ---------- BibTeX ----------

def _iter_bibtex_entries(text):
    """按配对花括号切出每个 @entry 的正文，跳过 @string/@comment 等。"""
    index = 0
    length = len(text)
    while index < length:
        at = text.find("@", index)
        if at < 0:
            return
        brace = text.find("{", at)
        if brace < 0:
            return
        depth = 0
        cursor = brace
        while cursor < length:
            if text[cursor] == "{":
                depth += 1
            elif text[cursor] == "}":
                depth -= 1
                if depth == 0:
                    break
            cursor += 1
        yield text[brace + 1:cursor]
        index = cursor + 1


def _split_top_level(body):
    parts, buffer = [], []
    depth, in_quote = 0, False
    for char in body:
        if char == '"' and depth == 0:
            in_quote = not in_quote
        if char == "{" and not in_quote:
            depth += 1
        elif char == "}" and not in_quote:
            depth -= 1
        if char == "," and depth == 0 and not in_quote:
            parts.append("".join(buffer))
            buffer = []
        else:
            buffer.append(char)
    if buffer:
        parts.append("".join(buffer))
    return parts


def _bibtex_field(raw):
    if "=" not in raw:
        return None, None
    name, value = raw.split("=", 1)
    name = name.strip().lower()
    value = value.strip().rstrip(",").strip()
    if len(value) >= 2 and value[0] == "{" and value[-1] == "}":
        value = value[1:-1]
    elif len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        value = value[1:-1]
    value = value.replace("{", "").replace("}", "")
    return name, " ".join(value.split())


def parse_bibtex(text):
    entries = []
    for body in _iter_bibtex_entries(text or ""):
        fields = {}
        parts = _split_top_level(body)
        for raw in parts[1:]:  # 第一段是 "类型, 引用键"
            name, value = _bibtex_field(raw)
            if name and value:
                fields[name] = value
        if not fields:
            continue
        title = fields.get("title") or fields.get("booktitle") or ""
        entry = {
            "title": title,
            "authors": fields.get("author", "").replace(" and ", "; "),
            "year": fields.get("year") or fields.get("date", ""),
            "doi": fields.get("doi", ""),
            "abstract": fields.get("abstract", ""),
        }
        if entry["title"] or entry["doi"]:
            entries.append(clean_entry(entry))
    return entries


# ---------- RIS ----------

RIS_TAGS = {
    "TI": "title", "T1": "title", "BT": "title",
    "AU": "authors", "A1": "authors",
    "PY": "year", "DA": "year", "Y1": "year",
    "DO": "doi",
    "AB": "abstract", "N2": "abstract",
}


def parse_ris(text):
    records, current = [], {}
    for line in (text or "").splitlines():
        match = re.match(r"^([A-Z][A-Z0-9])\s{2}-\s?(.*)$", line.rstrip())
        if not match:
            continue
        tag, value = match.group(1), match.group(2).strip()
        if tag == "TY":
            current = {}
            continue
        if tag == "ER":
            if current.get("title") or current.get("doi"):
                records.append(clean_entry(current))
            current = {}
            continue
        field = RIS_TAGS.get(tag)
        if not field or not value:
            continue
        if field == "authors":
            current.setdefault("authors", []).append(value)
        elif field == "year":
            current.setdefault("year", value[:4])
        else:
            current.setdefault(field, value)
    if current.get("title") or current.get("doi"):
        records.append(clean_entry(current))
    return records


# ---------- DOI 列表 / Crossref ----------

def parse_doi_lines(text):
    """把多行文本里的 DOI 抠出来，支持逗号/分号分隔与带 URL 的写法。"""
    found = []
    for chunk in re.split(r"[\s,;，；]+", text or ""):
        candidate = chunk.strip().strip(".,;")
        if not candidate:
            continue
        match = re.search(r"10\.\d{4,9}/[^\s]+", candidate)
        if match:
            found.append(match.group(0).rstrip("."))
    seen, result = set(), []
    for doi in found:
        key = doi.lower()
        if key not in seen:
            seen.add(key)
            result.append(doi)
    return result


def fetch_crossref(doi, timeout=8):
    """按 DOI 取题录；网络或格式异常时抛异常，由调用方降级。"""
    url = "https://api.crossref.org/works/" + urllib.parse.quote(doi)
    request = urllib.request.Request(url, headers={"User-Agent": CROSSREF_UA})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    message = payload.get("message") or {}
    titles = message.get("title") or []
    authors = "; ".join(
        " ".join(part for part in (item.get("given"), item.get("family")) if part).strip()
        for item in message.get("author", [])
    )
    year = ""
    for key in ("published-print", "published-online", "issued", "created"):
        parts = (message.get(key) or {}).get("date-parts") or []
        if parts and parts[0]:
            year = str(parts[0][0])
            break
    abstract = re.sub(r"<[^>]+>", " ", message.get("abstract") or "")
    return clean_entry({
        "title": titles[0] if titles else "",
        "authors": authors,
        "year": year,
        "doi": doi,
        "abstract": abstract,
    })
