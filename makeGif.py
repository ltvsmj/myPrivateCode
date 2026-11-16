#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
功能：
    扫描 VIDEO_DIR 下的视频，为每个视频生成 1~3 个「片段拼接」GIF，
    输出到 OUTPUT_DIR，文件名与视频同名（重名时追加两位序号）。

GIF 规则：
    - 视频 >= 32s：随机抽取 4 个互不重复的 8s 片段，按时间顺序拼成 32s；
    - 8s < 视频 < 32s：整片输出 1 个 GIF；
    - 视频 <= 8s：跳过；
    - 单个 GIF 不超过 35MB（MAX_SIZE），帧率不低于 8fps（MIN_FPS）；
    - 按「短边」缩放，输出宽度不低于 320px（MIN_WIDTH）；短边默认不超过
      原片短边，只有极端窄幅视频为满足宽度下限才会被放大。

适用系统：Lubuntu（Debian / Ubuntu 系）
依赖：ffmpeg、ffprobe —— 若未安装，请执行：  sudo apt install ffmpeg
"""

import os
import random
import shutil
import subprocess
import sys
import tempfile
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed

# ==================== 目录 ====================
VIDEO_DIR   = "/home/ltvsmj/Videos"          # 视频所在目录（只扫描该目录本身）
OUTPUT_DIR  = "/home/ltvsmj/Videos/output"   # GIF 输出目录

# ==================== 片段规则 ====================
SEG_LEN       = 8.0                          # 每个片段时长（秒）
SEG_COUNT     = 4                            # 拼接片段数量（总时长 32s）
MIN_VIDEO_LEN = SEG_LEN                      # 视频时长下限（<= 此值跳过）

# ==================== 输出限制 ====================
MAX_SIZE   = 35 * 1024 * 1024                # 单个 GIF 大小上限 35MB
MIN_FPS    = 8                               # 帧率下限 8 fps
MIN_WIDTH  = 320                             # 输出宽度下限（px）

# ==================== 每个视频生成几个 GIF ====================
# (视频时长上限秒, GIF 数量)；超过最后一档则用 MAX_GIFS_PER_VIDEO
GIF_COUNT_TIERS = ((100.0, 1), (300.0, 2))
MAX_GIFS_PER_VIDEO = 3

# ==================== 体积预估 ====================
# 从第一个片段开头截取这么长做快速编码，用于估算整段体积
ESTIMATE_SAMPLE_SEC = 1.5
# 外推安全系数：样本短、整体长，长片段帧间差分通常更省，乘 < 1 的系数避免误判
ESTIMATE_SAFETY = 0.85

# ==================== 并行 ====================
# 视频之间并行；每个视频内部只启动 1 个 ffmpeg 进程
MAX_WORKERS = max(1, min(4, os.cpu_count() or 1))

# ==================== 候选参数池 ====================
# 降级优先级：缩放（短边） > 帧率 > 颜色数，由 build_attempts() 展开成有序列表。
# 约束：帧率 >= MIN_FPS；短边不低于由 MIN_WIDTH 换算出的下限。
FPS_STEPS   = (15, 12, 10, 8)
SHORT_SIDES = (800, 720, 640, 560, 480, 420, 360, 320)
COLOR_STEPS = (256, 192, 160, 128, 96, 64, 48)

# 支持的视频扩展名（用于自动扫描 VIDEO_DIR）
VIDEO_EXTS = {
    ".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv", ".m4v",
    ".wmv", ".ts", ".mpg", ".mpeg", ".3gp", ".rmvb", ".vob",
}


def _mb(size):
    """字节数转 MB，仅用于打印。"""
    return size / 1024 / 1024


def _remove_file(path):
    """静默删除文件（不存在时忽略），用于清理临时文件与半成品。"""
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        print(f"  ⚠️ 无法删除 {path}：{exc}")


def check_tools():
    """检查 ffmpeg / ffprobe 是否可用"""
    missing = [name for name in ("ffmpeg", "ffprobe") if shutil.which(name) is None]
    if missing:
        sys.exit(
            f"错误：未检测到 {' / '.join(missing)}。\n"
            "请在终端执行以下命令安装后重试：\n"
            "    sudo apt update && sudo apt install -y ffmpeg"
        )


def list_videos():
    """扫描 VIDEO_DIR，返回其中所有视频文件的绝对路径（按文件名排序）"""
    if not os.path.isdir(VIDEO_DIR):
        sys.exit(f"错误：视频目录不存在 -> {VIDEO_DIR}")

    videos = [
        os.path.join(VIDEO_DIR, f)
        for f in os.listdir(VIDEO_DIR)
        if os.path.isfile(os.path.join(VIDEO_DIR, f))
        and os.path.splitext(f)[1].lower() in VIDEO_EXTS
    ]
    videos.sort(key=lambda p: os.path.basename(p).lower())

    if not videos:
        sys.exit(f"错误：目录 {VIDEO_DIR} 下没有找到任何视频文件")

    return videos


def probe_video(video_path):
    """
    用 ffprobe 一次性获取视频的时长与原始宽高（避免多次调用 ffprobe）。
    返回 (duration, width, height)。
    """
    cmd = [
        "ffprobe", "-hide_banner", "-loglevel", "error",
        "-select_streams", "v:0",
        "-show_entries", "format=duration:stream=width,height",
        "-of", "default=noprint_wrappers=1",
        video_path,
    ]
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode != 0:
        print("  ffprobe 报错：")
        print(result.stderr.decode("utf-8", errors="ignore"))
        sys.exit(f"错误：无法读取视频信息 -> {video_path}")

    info = {}
    for line in result.stdout.decode("utf-8", errors="ignore").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            info[k.strip()] = v.strip()

    try:
        duration = float(info.get("duration", "0"))
        width = int(info.get("width", "0"))
        height = int(info.get("height", "0"))
    except ValueError:
        sys.exit(f"错误：无法解析视频信息 -> {video_path}")

    if duration <= 0:
        sys.exit(f"错误：视频时长无效（{duration}）-> {video_path}")
    if width <= 0 or height <= 0:
        sys.exit(f"错误：视频分辨率无效（{width}x{height}）-> {video_path}")

    return duration, width, height


def unique_out_path(out_dir, stem):
    """
    生成不与已有文件冲突的输出路径：
      - 若 {stem}.gif 不存在，直接使用；
      - 否则依次尝试 {stem}01.gif、{stem}02.gif ... 直到不冲突。
    """
    candidate = os.path.join(out_dir, f"{stem}.gif")
    if not os.path.exists(candidate):
        return candidate

    i = 1
    while True:
        candidate = os.path.join(out_dir, f"{stem}{i:02d}.gif")
        if not os.path.exists(candidate):
            return candidate
        i += 1


def gif_count_for_duration(duration):
    """根据视频时长决定生成多少个集锦 GIF（1~MAX_GIFS_PER_VIDEO 个）。"""
    for max_len, count in GIF_COUNT_TIERS:
        if duration <= max_len:
            return min(count, MAX_GIFS_PER_VIDEO)
    return MAX_GIFS_PER_VIDEO


def build_attempts(src_w, src_h):
    """
    根据视频原始分辨率，生成「由强到弱」的参数列表 [(fps, 短边px, 颜色数), ...]。

    缩放规则：
      - 以短边为基准（横屏短边=高，竖屏短边=宽），横竖屏视觉清晰度一致；
      - 短边不超过原始短边（不放大），除非为满足 MIN_WIDTH 必须放大；
      - 短边不低于 MIN_WIDTH 换算出的下限。

    降级顺序（严格）：
      1) 最高帧率 + 最高颜色，短边从大到小；
      2) 短边到下限后，颜色不变，帧率从高到低；
      3) 帧率到下限后，最后降颜色数。

    结果列表同时是 find_best_index() 二分查找的搜索空间：越靠前参数越强、
    预估体积越大（体积随三者单调递增），因此列表顺序即「体积由大到小」。
    """
    src_short = min(src_w, src_h)

    # 宽度下限换算成短边下限：
    #   横屏（宽 >= 高）：宽度 = 短边 * 宽/高，要求宽度 >= MIN_WIDTH；
    #   竖屏（宽 < 高）：短边就是宽度，直接要求短边 >= MIN_WIDTH。
    if src_w >= src_h:
        min_short = int(round(MIN_WIDTH * src_h / src_w))
    else:
        min_short = MIN_WIDTH
    min_short = max(2, min_short)

    # 不放大 + 不低于下限；SHORT_SIDES 递减，钳制后仍递减，故可顺序去重
    shorts = []
    for side in SHORT_SIDES:
        s = max(min(side, src_short), min_short)
        if not shorts or shorts[-1] != s:
            shorts.append(s)

    fps_steps = [f for f in FPS_STEPS if f >= MIN_FPS]
    if not fps_steps:
        sys.exit(f"错误：FPS_STEPS 中没有 >= MIN_FPS({MIN_FPS}) 的帧率")

    attempts = []
    seen = set()

    def _add(fps, short_side, colors):
        key = (fps, short_side, colors)
        if key in seen:
            return
        seen.add(key)
        attempts.append(key)

    min_short_val = shorts[-1]
    top_colors = COLOR_STEPS[0]

    for s in shorts:                                   # 阶段 1：降缩放
        _add(fps_steps[0], s, top_colors)
    for f in fps_steps[1:]:                            # 阶段 2：降帧率
        _add(f, min_short_val, top_colors)
    for c in COLOR_STEPS[1:]:                          # 阶段 3：降颜色数
        _add(fps_steps[-1], min_short_val, c)

    return attempts


def pick_segments(duration, seg_len=SEG_LEN, count=SEG_COUNT,
                  min_len=MIN_VIDEO_LEN, exclude=None):
    """
    根据视频总时长挑选片段，返回 [(start, length), ...] 列表。

    规则：
      - duration <= min_len：无法生成，返回空列表。
      - min_len < duration < seg_len * count：整片剪辑（全片生成 1 个 GIF）。
      - duration >= seg_len * count：随机抽取 count 个不重复的 seg_len 片段。

    exclude：已使用过的槽位索引集合，用于多个 GIF 之间尽量不重复。
    """
    if duration <= min_len:
        print(
            f"⚠️ 视频总时长 {duration:.2f}s <= {min_len:.0f}s，"
            "无法生成满足要求的 GIF，跳过。"
        )
        return []

    total = seg_len * count

    # 情况 1：视频较短（8s ~ 32s），整片剪辑
    if duration < total:
        print(
            f"ℹ️ 视频总时长 {duration:.2f}s < {total:.0f}s，"
            f"将整片剪辑生成 1 个 GIF（时长 {duration:.2f}s）。"
        )
        return [(0.0, duration)]

    # 情况 2：把时间轴按 seg_len 切槽，随机抽取不重复的槽位
    slots = int(duration // seg_len)
    all_slots = list(range(slots))

    if exclude:
        available = [s for s in all_slots if s not in exclude]
    else:
        available = all_slots

    if len(available) < count:      # 剩余槽位不足，退化为从全部槽位中抽取
        available = all_slots

    chosen = random.sample(available, count)
    chosen.sort()
    return [(i * seg_len, seg_len) for i in chosen]


def _estimate_cache_key(video_path, segments, fps, short_side, colors):
    """
    预估结果缓存的键。

    必须包含片段信息：同一个视频的两轮 GIF 片段不同，体积差异可能很大，
    只用参数做键会把第一轮的预估值错误复用到第二轮。
    """
    seg_sig = tuple((round(start, 3), round(length, 3)) for start, length in segments)
    return (video_path, seg_sig, fps, short_side, colors)


def estimate_gif_size(video_path, segments, fps, short_side, colors,
                      src_w, src_h, cache=None):
    """
    预估使用给定参数生成 GIF 的体积（字节）。

    方法（采样探测法）：
      1. 取第一个片段开头 ESTIMATE_SAMPLE_SEC 秒，用与正式生成完全相同的
         filtergraph（fps/scale/palettegen/paletteuse）快速编码成临时 GIF；
      2. 按「总时长 / 样本时长」线性外推，再乘 ESTIMATE_SAFETY 修正。

    样本包含该视频真实的运动复杂度、色彩与噪声特征，预估误差远小于经验公式。
    返回预估字节数；探测失败时返回 None（调用方视为「无法预估」）。
    """
    if not segments:
        return None

    key = _estimate_cache_key(video_path, segments, fps, short_side, colors)
    if cache is not None and key in cache:
        return cache[key]

    total_len = sum(length for _, length in segments)
    if total_len <= 0:
        return None

    first_start, first_len = segments[0]
    sample_len = min(ESTIMATE_SAMPLE_SEC, first_len)
    if sample_len <= 0:
        return None

    scale_expr = _scale_expr(src_w, src_h, short_side)

    fd, tmp_path = tempfile.mkstemp(suffix=".gif", prefix="gif_est_")
    os.close(fd)
    try:
        vf = (
            f"[0:v]fps={fps},{scale_expr},setpts=PTS-STARTPTS[cat];"
            f"[cat]split[a][b];"
            f"[a]palettegen=max_colors={colors}:stats_mode=diff[p];"
            f"[b][p]paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle"
        )
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-ss", f"{first_start:.3f}", "-t", f"{sample_len:.3f}",
            "-i", video_path,
            "-an", "-sn",
            "-filter_threads", "0",
            "-filter_complex", vf,
            "-gifflags", "+transdiff",
            "-loop", "0",
            tmp_path,
        ]
        result = subprocess.run(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
        )
        if result.returncode != 0 or not os.path.exists(tmp_path):
            if cache is not None:
                cache[key] = None
            return None

        sample_bytes = os.path.getsize(tmp_path)
        if sample_bytes <= 0:
            if cache is not None:
                cache[key] = None
            return None

        estimated = int(sample_bytes * (total_len / sample_len) * ESTIMATE_SAFETY)
        if cache is not None:
            cache[key] = estimated
        return estimated
    finally:
        _remove_file(tmp_path)


def _scale_expr(src_w, src_h, short_side):
    """按横/竖屏构造 ffmpeg 的 scale 表达式（短边固定，长边按比例推导）。"""
    if src_w >= src_h:
        return f"scale=-2:{short_side}:flags=lanczos"
    return f"scale={short_side}:-2:flags=lanczos"


def find_best_index(video_path, segments, attempts, src_w, src_h, cache=None):
    """
    在 attempts（由强到弱、预估体积单调递减）中二分查找
    「预估体积 <= MAX_SIZE」的最强参数下标。

    复杂度：O(log n) 次预估（n=17 时最多 5 次），而按维度分段二分最多约 9 次。
    依赖「体积随参数强度单调递增」，该假设与分段二分本质相同，但一次二分
    即可覆盖三个维度，且天然保证返回的是全局最强可行参数。

    预估失准（偏小/无法预估）时不会出错：真实编码后仍超限会由
    make_one_gif() 继续向下兜底。

    返回下标；若全部参数预估都超限，返回 None。
    """
    lo, hi = 0, len(attempts) - 1
    best = None

    while lo <= hi:
        mid = (lo + hi) // 2
        fps, short_side, colors = attempts[mid]
        est = estimate_gif_size(
            video_path, segments, fps, short_side, colors,
            src_w, src_h, cache=cache
        )
        # 预估失败时按「可用」处理：宁可多试一次强参数，也不白白降画质
        if est is None or est <= MAX_SIZE:
            best = mid
            hi = mid - 1        # 达标，尝试更强的参数（下标更小）
        else:
            lo = mid + 1        # 超限，尝试更弱的参数（下标更大）

    return best


def build_gif(video_path, out_path, segments, fps, short_side, colors,
              src_w, src_h):
    """
    使用 ffmpeg 的「单遍调色板法」生成高质量 GIF：
      在同一个 filtergraph 中先 split，一路生成调色板，一路等待调色板后输出。
      相比两遍法只需解码一次视频，速度提升明显，画质与两遍法基本一致。

    segments 为 [(start, length), ...]，多段通过 concat 拼接；
    每段用一个 -ss/-t 输入单独定位，避免解码无关部分。

    先写到同目录的 <out_path>.tmp.gif，编码成功后再原子改名，
    因此 out_path 只会出现完整文件，失败时不会留下半成品。
    """
    tmp_path = out_path + ".tmp.gif"
    _remove_file(tmp_path)

    scale_expr = _scale_expr(src_w, src_h, short_side)

    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y"]
    for start, length in segments:
        cmd += ["-ss", f"{start:.3f}", "-t", f"{length:.3f}", "-i", video_path]

    n = len(segments)

    parts = [
        f"[{i}:v]fps={fps},{scale_expr},setpts=PTS-STARTPTS[v{i}]"
        for i in range(n)
    ]
    concat_inputs = "".join(f"[v{i}]" for i in range(n))
    parts.append(f"{concat_inputs}concat=n={n}:v=1:a=0[cat]")
    parts.append("[cat]split[a][b]")
    parts.append(f"[a]palettegen=max_colors={colors}:stats_mode=diff[p]")
    parts.append("[b][p]paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle")
    vf = ";".join(parts)

    cmd += [
        "-an", "-sn",                       # 丢弃音频/字幕，减少解码开销
        "-filter_threads", "0",             # 0 = 自动使用所有可用线程
        "-filter_complex", vf,
        "-gifflags", "+transdiff",          # 帧间差分，减小体积
        "-loop", "0",                       # 无限循环播放
        tmp_path,
    ]

    result = subprocess.run(
        cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
    )
    if result.returncode != 0 or not os.path.exists(tmp_path):
        print("  ffmpeg 报错：")
        print(result.stderr.decode("utf-8", errors="ignore"))
        _remove_file(tmp_path)
        return False

    os.replace(tmp_path, out_path)
    return True


def make_one_gif(video_path, out_path, segments, attempts,
                 src_w, src_h, estimate_cache=None):
    """
    为单个视频生成 GIF。

    先用 find_best_index() 定位最接近 MAX_SIZE 的参数，再实际编码；
    若实际体积仍超限，则按 attempts 顺序继续降级（兜底）。
    任何失败路径都会删除 out_path，避免残留半成品。

    返回 (是否成功, 最终字节数, 参数描述)。
    """
    last_size = None
    succeeded = False

    best = find_best_index(
        video_path, segments, attempts, src_w, src_h, cache=estimate_cache
    )
    if best is None:
        print("  预估：所有参数组合均超过大小上限，先用最弱参数实际编码确认")
        start = len(attempts) - 1
    else:
        start = best

    try:
        for idx in range(start, len(attempts)):
            fps, short_side, colors = attempts[idx]
            params = f"{fps} fps / 短边 {short_side}px / {colors} 色"

            estimated = estimate_gif_size(
                video_path, segments, fps, short_side, colors,
                src_w, src_h, cache=estimate_cache
            )
            if estimated is not None:
                print(f"  尝试参数 -> {params} ...（预估 {_mb(estimated):.2f} MB）")
            else:
                print(f"  尝试参数 -> {params} ...")

            if not build_gif(video_path, out_path, segments,
                             fps, short_side, colors, src_w, src_h):
                print("    本次生成失败，尝试下一组参数")
                continue

            size = os.path.getsize(out_path)
            last_size = size
            print(f"    生成成功，当前大小：{_mb(size):.2f} MB")

            if size <= MAX_SIZE:
                succeeded = True
                return True, size, params

            print(f"    超过 {_mb(MAX_SIZE):.0f}MB，降低参数后重试")
            _remove_file(out_path)

        return False, last_size, None
    finally:
        # 失败或中途异常（含 Ctrl+C）时不留下任何半成品
        if not succeeded:
            _remove_file(out_path)


def process_video(video_path):
    """
    处理单个视频：按时长生成 1~3 个集锦 GIF，每个 GIF 使用不同的随机片段。
    返回 (成功数, 失败文件名列表)。
    """
    print(f"\n{'#' * 60}")
    print(f"开始处理视频：{video_path}")
    print(f"{'#' * 60}")

    duration, src_w, src_h = probe_video(video_path)
    print(f"视频总时长：{duration:.2f}s，分辨率：{src_w}x{src_h}")

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    stem = os.path.splitext(os.path.basename(video_path))[0]

    attempts = build_attempts(src_w, src_h)

    target_count = gif_count_for_duration(duration)
    print(f"大小上限：{_mb(MAX_SIZE):.0f} MB，帧率下限：{MIN_FPS} fps，"
          f"宽度下限：{MIN_WIDTH}px")
    print(f"根据视频时长，将生成 {target_count} 个集锦 GIF")
    print()

    ok_count = 0
    failed = []
    used_slots = set()   # 已用片段槽位，避免多个 GIF 片段重复
    estimate_cache = {}  # 体积预估缓存（同视频同片段同参数只探测一次）

    for round_no in range(1, target_count + 1):
        print(f"--- 第 {round_no}/{target_count} 个 GIF ---")

        segments = pick_segments(duration, exclude=used_slots)
        if not segments:
            print("  ⚠️ 该视频无法生成满足要求的 GIF，跳过。\n")
            break

        # 记录本次使用的槽位（整片剪辑只有一个片段，没有槽位概念）
        if len(segments) > 1:
            for start, _ in segments:
                used_slots.add(int(round(start / SEG_LEN)))

        out_path = unique_out_path(OUTPUT_DIR, stem)
        print(f"输出文件：{out_path}")
        print(f"片段数：{len(segments)}，总时长："
              f"{sum(l for _, l in segments):.2f}s")
        print()

        ok, size, params = make_one_gif(
            video_path, out_path, segments, attempts, src_w, src_h,
            estimate_cache=estimate_cache
        )

        name = os.path.basename(out_path)
        if ok:
            print(f"  ✅ {name}：{_mb(size):.2f} MB（{params}）")
            ok_count += 1
        else:
            size_text = f"{_mb(size):.2f} MB" if size else "未知"
            print(f"  ❌ {name}：可用参数下仍不满足限制（当前 {size_text}）")
            print("     建议：在 SHORT_SIDES / COLOR_STEPS 末尾追加更低参数。")
            failed.append(name)
            break

    print()
    return ok_count, failed


def main():
    check_tools()

    videos = list_videos()
    print(f"在 {VIDEO_DIR} 下找到 {len(videos)} 个视频文件：")
    for v in videos:
        print(f"  - {os.path.basename(v)}")

    total_ok = 0
    all_failed = []

    def _task(i, video_path):
        print(f"\n[{i}/{len(videos)}] 处理：{os.path.basename(video_path)}")
        try:
            return process_video(video_path)
        except SystemExit as exc:
            # 单个视频信息异常（如时长不足）时跳过，继续处理下一个
            print(f"  ⚠️ 跳过该视频：{exc}")
            return 0, []
        except Exception:  # 单个视频崩溃不应中断整批任务
            print("  ⚠️ 处理失败：")
            traceback.print_exc()
            return 0, []

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = [
            pool.submit(_task, i, v)
            for i, v in enumerate(videos, 1)
        ]
        for fut in as_completed(futures):
            ok_count, failed = fut.result()
            total_ok += ok_count
            all_failed.extend(failed)

    print("\n" + "#" * 60)
    print(f"全部完成：共处理 {len(videos)} 个视频，成功生成 {total_ok} 个 GIF")
    if all_failed:
        print("失败列表：" + "、".join(all_failed))
    print("#" * 60)


if __name__ == "__main__":
    main()
