# -*- coding: utf-8 -*-
"""ReviewGit 前端启动入口。

用法：
  python scripts/launch.py            首次自动灌入演示语料并启动
  python scripts/launch.py --seed     重建演示语料后启动
  python scripts/launch.py --port 8000
"""
import argparse
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
WEB = os.path.join(ROOT, "web")


def ensure_seed(force: bool):
    db = os.environ.get("REVIEWGIT_DB", os.path.join(WEB, "reviewgit.db"))
    if force or not os.path.exists(db):
        print("正在灌入演示语料（聚酰亚胺建模）…")
        subprocess.run([sys.executable, os.path.join(WEB, "seed.py")], check=True)


def main():
    p = argparse.ArgumentParser(description="ReviewGit 前端")
    p.add_argument("--seed", action="store_true", help="重建演示语料")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=5000)
    args = p.parse_args()

    ensure_seed(args.seed)

    sys.path.insert(0, WEB)
    import app  # noqa: E402
    url = f"http://{args.host}:{args.port}"
    print(f"ReviewGit 前端已启动：{url}")
    app.app.run(host=args.host, port=args.port, debug=False)


if __name__ == "__main__":
    main()
