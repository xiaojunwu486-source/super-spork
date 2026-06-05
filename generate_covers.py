# -*- coding: utf-8 -*-
"""批量补生成历史视频的封面图"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from processor import extract_cover

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(BASE_DIR, "output")

generated = 0
skipped = 0
failed = 0

for vid in os.listdir(OUTPUT_DIR):
    video_dir = os.path.join(OUTPUT_DIR, vid)
    if not os.path.isdir(video_dir):
        continue

    cover_path = os.path.join(video_dir, "cover.jpg")
    if os.path.exists(cover_path):
        skipped += 1
        continue

    frames_dir = os.path.join(video_dir, "frames")
    result = extract_cover(frames_dir, video_dir)
    if result:
        generated += 1
        print(f"  [OK] {vid} -> cover.jpg")
    else:
        failed += 1
        print(f"  [--] {vid} -> 无帧数据，跳过")

print(f"\n完成: 新生成 {generated}, 已存在 {skipped}, 失败 {failed}")
