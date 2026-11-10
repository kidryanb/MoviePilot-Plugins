#!/usr/bin/env python3
"""找出并修复和中转目录断开硬链接的 STRM。默认只列出，加 --apply 才修改。"""
import argparse, os, re, shutil, sys, time
from pathlib import Path

FID = re.compile(r"(?<![0-9a-f])[0-9a-f]{32}(?![0-9a-f])")

def read(path):
    try:
        return path.read_text(encoding="utf-8").strip()
    except Exception:
        return ""

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, help="插件生成 STRM 的中转目录")
    ap.add_argument("--lib", required=True, help="MP 整理后的媒体库目录")
    ap.add_argument("--backup", default="/config/strm_backup", help="备份目录")
    ap.add_argument("--apply", action="store_true", help="真正修复（默认只列出）")
    a = ap.parse_args()
    src, lib = Path(a.src), Path(a.lib)
    by_inode, by_fid = {}, {}
    for p in src.rglob("*.strm"):
        st = p.stat()
        by_inode[(st.st_dev, st.st_ino)] = p
        for fid in set(FID.findall(read(p))):
            by_fid.setdefault(fid, []).append(p)
    ok = broken = unmatched = fixed = 0
    stamp = time.strftime("%Y%m%d-%H%M%S")
    for p in sorted(lib.rglob("*.strm")):
        st = p.stat()
        if (st.st_dev, st.st_ino) in by_inode:
            ok += 1
            continue
        fids = set(FID.findall(read(p)))
        cands = {c for f in fids for c in by_fid.get(f, [])}
        if len(cands) != 1:
            unmatched += 1
            print(f"[找不到对应] {p}  （候选 {len(cands)} 个）")
            continue
        target = cands.pop()
        broken += 1
        same = read(p) == read(target)
        print(f"[断开{'·内容相同' if same else '·内容过期'}] {p}\n    -> {target}")
        if not a.apply:
            continue
        if target.stat().st_dev != st.st_dev:
            print("    跳过：两个目录不在同一个文件系统，无法硬链接")
            continue
        bak = Path(a.backup) / stamp / p.relative_to(lib)
        bak.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, bak)
        tmp = p.with_name(p.name + ".relink")
        os.link(target, tmp)
        os.replace(tmp, p)
        fixed += 1
    print(f"\n库里 STRM：正常 {ok}，断开 {broken}，找不到对应 {unmatched}"
          + (f"，已修复 {fixed}，备份在 {a.backup}/{stamp}" if a.apply else "（只列出，未修改）"))

if __name__ == "__main__":
    sys.exit(main())
