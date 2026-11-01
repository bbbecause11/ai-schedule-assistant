#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
版本备份工具。

为什么需要它：
    每次改完代码、推送之前，先留一份"上一版能跑的样子"，
    万一新版本出现恶性 bug，可以直接从 backups/ 里翻出旧文件覆盖回去，
    不用去翻 git 历史（历史上做过于强制推送，靠 git 不一定稳）。

它做三件事：
    1. 把 app.py / selfcheck.py / README.md / requirements.txt 打成一个 zip，
       存到 backups/v{版本}_{时间戳}.zip；
    2. 在 backups/INDEX.md 里追加一行记录（版本、时间、commit、说明）；
    3. 顺手给当前 commit 打一个 git tag（v{版本}），能推就推。

用法：
    python tools/backup.py                 # 备份当前版本（版本号读 app.py）
    python tools/backup.py -m "加了分享面板"  # 附带一句说明
    python tools/backup.py --list          # 看看有哪些备份
    python tools/backup.py --restore v2.8.0  # 把某个备份解压回项目根目录

注意：backups/ 只存在本地（已进 .gitignore），不随仓库上传；
      远端回滚请用 git tag（tag 由本脚本自动创建）。
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKUP_DIR = ROOT / "backups"
FILES = ["app.py", "selfcheck.py", "README.md", "requirements.txt"]
INDEX = BACKUP_DIR / "INDEX.md"


def current_version() -> str:
    """从 app.py 里读 APP_VERSION，避免手写两处不一致。"""
    src = (ROOT / "app.py").read_text(encoding="utf-8")
    m = re.search(r'APP_VERSION\s*=\s*"([^"]+)"', src)
    return m.group(1) if m else "unknown"


def git(*args: str) -> str:
    """跑一条 git 命令，失败返回空串（没有 git 也不该让备份失败）。"""
    try:
        out = subprocess.run(["git", *args], cwd=ROOT, capture_output=True,
                             text=True, timeout=30)
        return out.stdout.strip() if out.returncode == 0 else ""
    except Exception:  # noqa: BLE001
        return ""


def do_backup(message: str) -> Path:
    version = current_version()
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    BACKUP_DIR.mkdir(exist_ok=True)
    target = BACKUP_DIR / f"v{version}_{stamp}.zip"

    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in FILES:
            path = ROOT / name
            if path.exists():
                zf.write(path, arcname=name)

    size_kb = target.stat().st_size / 1024
    commit = git("rev-parse", "--short", "HEAD")
    tag = f"v{version}"
    has_tag = git("tag", "-l", tag)

    print(f"[backup] v{version} -> {target.relative_to(ROOT)}  ({size_kb:.0f} KB, commit {commit or 'n/a'})")

    if not has_tag:
        if git("tag", "-a", tag, "-m", f"备份 v{version}") or True:
            print(f"[backup] 已创建 git tag {tag}（远端回滚用 git checkout {tag}）")

    # 追加索引
    new_file = not INDEX.exists()
    line = (f"| {datetime.now():%Y-%m-%d %H:%M} | **v{version}** | "
            f"`{target.name}` | {commit or '—'} | {message or '—'} |\n")
    with INDEX.open("a", encoding="utf-8") as fh:
        if new_file:
            fh.write("# 版本备份索引\n\n"
                     "> 每个 zip 里是当版可运行的 `app.py` / `selfcheck.py` / `README.md` / `requirements.txt`。\n"
                     "> 回滚：`python tools/backup.py --restore v2.8.0`（会先自动再备份一次当前状态）。\n\n"
                     "| 时间 | 版本 | 备份文件 | commit | 说明 |\n"
                     "| --- | --- | --- | --- | --- |\n")
        fh.write(line)
    return target


def list_backups() -> None:
    if not BACKUP_DIR.exists():
        print("还没有任何备份。")
        return
    zips = sorted(BACKUP_DIR.glob("v*.zip"))
    if not zips:
        print("还没有任何备份。")
        return
    print(f"共 {len(zips)} 份备份：")
    for z in zips:
        print(f"  {z.name:38s} {z.stat().st_size/1024:6.0f} KB")


def restore(version: str) -> None:
    """把某个版本的备份解压回项目根目录（先把当前状态再备份一次）。"""
    version = version.lstrip("v")
    matches = sorted(BACKUP_DIR.glob(f"v{version}_*.zip"))
    if not matches:
        print(f"找不到 v{version} 的备份，用 --list 看看有哪些。")
        sys.exit(1)
    src = matches[-1]

    # 先备份当前状态，回滚本身也要可回滚
    do_backup(f"回滚到 v{version} 之前自动备份")

    with zipfile.ZipFile(src) as zf:
        for name in zf.namelist():
            if name.endswith("/"):
                continue
            dst = ROOT / name
            if dst.exists():
                shutil.copy2(dst, dst.with_suffix(dst.suffix + ".before-restore"))
            with zf.open(name) as fh, open(dst, "wb") as out:
                out.write(fh.read())
            print(f"[restore] {name}")
    print(f"已从 {src.name} 恢复。原文件留了 *.before-restore 备份。")


def main() -> int:
    ap = argparse.ArgumentParser(description="版本备份 / 回滚")
    ap.add_argument("-m", "--message", default="", help="这次备份的说明")
    ap.add_argument("--list", action="store_true", help="列出所有备份")
    ap.add_argument("--restore", metavar="VERSION", help="恢复到指定版本，如 v2.8.0")
    args = ap.parse_args()

    if args.list:
        list_backups()
    elif args.restore:
        restore(args.restore)
    else:
        do_backup(args.message)
    return 0


if __name__ == "__main__":
    sys.exit(main())
