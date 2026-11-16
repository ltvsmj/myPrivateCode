#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按大小把文件夹（含子文件夹）内的文件分批压缩成多个 zip 包。

用法:
    python zip_split.py                       # 使用默认目录
    python zip_split.py -f /path/to/folder    # 指定目录
    python zip_split.py -f /path/to/folder -o /path/to/out -m 210
"""

import argparse
import os
import sys
import zipfile

DEFAULT_FOLDER = "/home/ltvsmj/Videos/output"
DEFAULT_MAX_MB = 210
DEFAULT_PREFIX = "fuli"
MB = 1024 * 1024


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


def group_files(files, max_bytes):
    """按文件大小贪心分组，每组总大小不超过 max_bytes。

    单个文件本身超过 max_bytes 时，单独成组（无法再拆分）。
    """
    groups = []
    current = []
    current_size = 0
    for path in files:
        size = os.path.getsize(path)
        if size > max_bytes:
            # 超大文件单独成组
            if current:
                groups.append(current)
                current, current_size = [], 0
            groups.append([path])
            continue
        if current and current_size + size > max_bytes:
            groups.append(current)
            current, current_size = [], 0
        current.append(path)
        current_size += size
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
                        help=f"每个 zip 的最大大小，单位 MB（默认: {DEFAULT_MAX_MB}）")
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
    groups = group_files(files, max_bytes)

    print(f"共 {len(files)} 个文件，将生成 {len(groups)} 个压缩包。")
    idx = next_index(out_dir, args.prefix)
    for group in groups:
        zip_path = os.path.join(out_dir, f"{args.prefix}{idx:02d}.zip")
        write_zip(zip_path, group, root)
        size_mb = os.path.getsize(zip_path) / MB
        print(f"已生成: {zip_path}  ({len(group)} 个文件, {size_mb:.1f} MB)")
        idx += 1


if __name__ == "__main__":
    main()
