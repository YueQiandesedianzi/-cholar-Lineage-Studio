# -*- coding: utf-8 -*-
"""从规范版插件目录重建分发快照（仅使用标准库）。

默认写入 ./dist；可通过 --output-dir 指定已确认的分发目录。
只覆盖同名源文件和 ZIP，不递归删除输出目录，不触碰历史 MVP。
"""

import argparse
import shutil
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXCLUDED_DIRS = {"__pycache__", ".git", "dist"}
EXCLUDED_SUFFIXES = {".db", ".sqlite", ".sqlite3", ".pyc", ".pyo"}


def source_files():
    files = []
    for path in ROOT.rglob("*"):
        relative = path.relative_to(ROOT)
        if not path.is_file() or any(part in EXCLUDED_DIRS for part in relative.parts):
            continue
        name = path.name.lower()
        if path.suffix.lower() in EXCLUDED_SUFFIXES:
            continue
        if name.endswith((".db-wal", ".db-journal", ".sqlite-wal", ".sqlite-journal")):
            continue
        files.append((path, relative))
    return sorted(files, key=lambda item: item[1].as_posix())


def package(output_dir):
    # Materialize the input allowlist before creating any output files.
    files = source_files()
    if not (ROOT / "web" / "collaboration.py").is_file():
        raise RuntimeError("缺少 web/collaboration.py，拒绝生成不完整快照")
    output_dir.mkdir(parents=True, exist_ok=True)
    unpacked = output_dir / "reviewgit-plugin"
    archive = output_dir / "reviewgit-plugin.zip"
    entries = []
    for source, relative in files:
        destination = unpacked / "reviewgit" / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(source), str(destination))
        entry = "reviewgit-plugin/reviewgit/" + relative.as_posix()
        entries.append((destination, entry))
    outer_readme = unpacked / "README.md"
    shutil.copy2(str(ROOT / "README.md"), str(outer_readme))
    entries.append((outer_readme, "reviewgit-plugin/README.md"))
    with zipfile.ZipFile(str(archive), "w", zipfile.ZIP_DEFLATED) as bundle:
        for path, entry in entries:
            bundle.write(str(path), entry)
    with zipfile.ZipFile(str(archive)) as bundle:
        assert not bundle.testzip(), "ZIP CRC 校验失败"
        names = bundle.namelist()
        assert names and all(name.startswith("reviewgit-plugin/") for name in names)
        assert not any("/__pycache__/" in name or name.endswith((".db", ".pyc")) for name in names)
    print("已打包 %d 个文件：" % len(entries))
    print("ZIP:", archive.resolve())
    print("解压副本:", unpacked.resolve())


def main():
    parser = argparse.ArgumentParser(description="重建 ReviewGit 插件分发快照")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dist")
    args = parser.parse_args()
    package(args.output_dir.resolve())


if __name__ == "__main__":
    main()
