#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按大小把文件夹（含子文件夹）内的文件分批压缩成多个 zip 包。

用法:
    python zipSplit.py                       # 使用默认目录
    python zipSplit.py -f /path/to/folder    # 指定目录
    python zipSplit.py -f /path/to/folder -o /path/to/out -m 50
"""

import argparse
import os
import sys
import zipfile

DEFAULT_FOLDER = "/home/ltvsmj/Videos/output"
DEFAULT_MAX_MB = 100
DEFAULT_PREFIX = "fuli"
MB = 1024 * 1024

# 每个条目在 zip 里的固定开销：本地文件头 30 字节 + 中央目录项 46 字节。
ZIP_ENTRY_OVERHEAD = 30 + 46
# 文件名在本地文件头和中央目录里各存一份，所以还要加 2 倍文件名长度。
# 结尾目录（EOCD）固定 22 字节。
ZIP_EOCD_BYTES = 22
# 条目数超过 65535 会触发 zip64 额外字段，上面的固定开销就不再成立；
# 留出余量，达到这个数量就开新包。
MAX_ENTRIES_PER_ZIP = 65000


def collect_files(root):
    """递归收集 root 下所有普通文件，跳过已生成的 fuli*.zip。"""
    files = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            if name.lower().startswith(DEFAULT_PREFIX) and name.lower().endswith(".zip"):
                continue
            path = os.path.join(dirpath, name)
            if os.path.isfile(path):
                files.append(path)
    return files


def next_index(out_dir, prefix):
    """返回下一个可用的编号（从 1 开始），跳过已存在的 fuliNN.zip。"""
    idx = 1
    while True:
        candidate = os.path.join(out_dir, f"{prefix}{idx:02d}.zip")
        if not os.path.exists(candidate):
            return idx
        idx += 1


def entry_bytes(path, root):
    """单个文件在 zip 里占用的字节数（数据 + 头部开销）。"""
    arcname = os.path.relpath(path, root)
    # zip 里存的是 UTF-8 编码后的文件名
    name_len = len(arcname.encode("utf-8"))
    return os.path.getsize(path) + ZIP_ENTRY_OVERHEAD + 2 * name_len


def estimate_zip_bytes(files, root):
    """估算写出来的 zip 文件大小：EOCD + 各条目字节数之和。

    ZIP_STORED 不压缩，所以这个估算是精确值（文件名带中文时也成立），
    仅在条目超过 65535 或单文件超过 4 GiB 触发 zip64 时会有偏差。
    """
    return ZIP_EOCD_BYTES + sum(entry_bytes(path, root) for path in files)


def group_files(files, max_bytes, root):
    """按文件大小贪心分组，保证成包大小不超过 max_bytes。

    分组算的是"文件大小 + zip 头开销"，不是裸文件大小：
    ZIP_STORED 不压缩，但每个条目仍要加 76 字节头部和两倍文件名长度，
    只按裸大小分组会让成品比限制略大。

    单个文件自身的成本就超过上限时，单独成组（无法再拆分）。
    """
    budget = max_bytes - ZIP_EOCD_BYTES
    groups = []
    current = []
    current_bytes = 0
    for path in files:
        cost = entry_bytes(path, root)
        if cost > budget:
            # 超大文件单独成组
            if current:
                groups.append(current)
                current, current_bytes = [], 0
            groups.append([path])
            continue
        if current and (current_bytes + cost > budget
                        or len(current) >= MAX_ENTRIES_PER_ZIP):
            groups.append(current)
            current, current_bytes = [], 0
        current.append(path)
        current_bytes += cost
    if current:
        groups.append(current)
    return groups


def write_zip(zip_path, files, root):
    """把 files 写入 zip_path，保留相对 root 的目录结构。"""
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_STORED) as zf:
        for path in files:
            arcname = os.path.relpath(path, root)
            zf.write(path, arcname)


def main():
    parser = argparse.ArgumentParser(description="按大小分批压缩文件夹为多个 zip 包")
    parser.add_argument("-f", "--folder", default=DEFAULT_FOLDER,
                        help=f"要压缩的文件夹（默认: {DEFAULT_FOLDER}）")
    parser.add_argument("-o", "--output", default=None,
                        help="zip 输出目录（默认: 与输入文件夹相同）")
    parser.add_argument("-m", "--max-mb", type=int, default=DEFAULT_MAX_MB,
                        help=f"每个 zip 的上限，单位 MB（默认: {DEFAULT_MAX_MB}）")
    parser.add_argument("-p", "--prefix", default=DEFAULT_PREFIX,
                        help=f"zip 文件名前缀（默认: {DEFAULT_PREFIX}）")
    args = parser.parse_args()

    root = os.path.abspath(args.folder)
    if not os.path.isdir(root):
        print(f"错误: 文件夹不存在: {root}", file=sys.stderr)
        sys.exit(1)

    out_dir = os.path.abspath(args.output) if args.output else root
    os.makedirs(out_dir, exist_ok=True)

    max_bytes = args.max_mb * MB
    files = collect_files(root)
    if not files:
        print("没有找到需要压缩的文件。")
        return

    # 按大小排序，便于更紧凑地分组
    files.sort(key=os.path.getsize, reverse=True)
    groups = group_files(files, max_bytes, root)

    print(f"共 {len(files)} 个文件，将生成 {len(groups)} 个压缩包。")
    idx = next_index(out_dir, args.prefix)
    for group in groups:
        zip_path = os.path.join(out_dir, f"{args.prefix}{idx:02d}.zip")
        write_zip(zip_path, group, root)
        size = os.path.getsize(zip_path)
        expected = estimate_zip_bytes(group, root)
        print(f"已生成: {zip_path}  ({len(group)} 个文件, {size / MB:.1f} MB)")
        if size != expected:
            print(f"警告: {zip_path} 估算 {expected} 字节、实际 {size} 字节，"
                  f"可能触发了 zip64，上限不再保证", file=sys.stderr)
        if size > max_bytes:
            if len(group) > 1:
                # 分组按精确开销计算，走到这里说明有 bug，不能默默产出超限包
                raise RuntimeError(
                    f"分组失效: {zip_path} 实际 {size} 字节 > 上限 {max_bytes} 字节")
            # 单个文件本身就超过上限，无法再拆
            print(f"警告: 单个文件已超过 {args.max_mb} MB 上限，无法再拆分: {group[0]}",
                  file=sys.stderr)
        idx += 1


if __name__ == "__main__":
    main()
