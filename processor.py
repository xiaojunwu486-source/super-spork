# -*- coding: utf-8 -*-
"""
视频处理模块 - 抽帧 + 音频分离 + 语音转文字
"""

import os
import re
import json
import subprocess
import threading
from datetime import datetime


# 全局变量


def get_subprocess_creationflags():
    """Windows 下隐藏 FFmpeg 控制台窗口，Linux/Render 下不传该参数。"""
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def get_api_key(key_name):
    """优先读取环境变量，兼容本地 config.json。"""
    env_value = os.environ.get(key_name.upper(), "").strip()
    if env_value:
        return env_value

    base_dir = os.path.dirname(os.path.abspath(__file__))
    data_dir = os.environ.get("DATA_DIR", base_dir)
    config_paths = [
        os.path.join(data_dir, "config.json"),
        os.path.join(base_dir, "config.json"),
    ]
    for config_path in config_paths:
        if os.path.exists(config_path):
            with open(config_path, "r", encoding="utf-8") as f:
                config = json.load(f)
            return config.get(key_name.lower(), "").strip()

    return ""


def get_ffmpeg_path():
    """获取 imageio-ffmpeg 内置的 ffmpeg 路径"""
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        pass
    except RuntimeError:
        # imageio_ffmpeg 装了但 get_ffmpeg_exe() 抛异常（已知 bug）
        pass

    # 手动回退：直接扫描 binaries 目录找 ffmpeg 可执行文件
    try:
        import imageio_ffmpeg
        bin_dir = os.path.join(os.path.dirname(imageio_ffmpeg.__file__), "binaries")
        if os.path.isdir(bin_dir):
            for f in os.listdir(bin_dir):
                if f.endswith(".exe") and "ffmpeg" in f.lower():
                    exe_path = os.path.join(bin_dir, f)
                    if os.path.isfile(exe_path):
                        return exe_path
    except Exception:
        pass

    return "ffmpeg"


def load_whisper_model(model_name="small", progress_callback=None):
    """保留接口兼容"""
    pass


def transcribe_audio(audio_path, output_dir, model_name="qwen3-asr-flash", progress_callback=None):
    """
    使用 DashScope Qwen3-ASR-Flash 进行语音转文字（云端 API）
    通过 OpenAI 兼容接口 + base64 编码上传本地音频
    不需要本地 PyTorch
    """
    import base64
    import requests as req_lib

    os.makedirs(output_dir, exist_ok=True)

    if not os.path.exists(audio_path):
        if progress_callback:
            progress_callback(0, 1, "音频文件不存在，跳过语音识别")
        return {"transcribed": False, "reason": "no_audio_file"}

    # 加载 API Key
    qwen_api_key = get_api_key("qwen_api_key")

    if not qwen_api_key:
        if progress_callback:
            progress_callback(0, 1, "DashScope API Key 未配置，跳过语音识别")
        return {"transcribed": False, "reason": "no_api_key"}

    if progress_callback:
        progress_callback(0, 3, "正在编码音频文件...")

    try:
        # 读取音频并 base64 编码
        audio_size = os.path.getsize(audio_path)
        if audio_size > 8 * 1024 * 1024:  # 8MB (base64 后约 10.6MB)
            if progress_callback:
                progress_callback(0, 1, f"音频文件过大 ({round(audio_size/1024/1024, 1)}MB)，跳过语音识别")
            return {"transcribed": False, "reason": "file_too_large"}

        with open(audio_path, "rb") as f:
            audio_base64 = base64.b64encode(f.read()).decode()

        # 构造 data URI
        data_uri = f"data:audio/wav;base64,{audio_base64}"

        if progress_callback:
            progress_callback(1, 3, "正在调用语音识别 API...")

        # 通过 OpenAI 兼容接口调用 Qwen3-ASR-Flash
        api_url = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {qwen_api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": model_name,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_audio",
                            "input_audio": {
                                "data": data_uri,
                            }
                        }
                    ]
                }
            ],
            "stream": False,
            "asr_options": {
                "language": "zh",
                "enable_itn": True,
            }
        }

        resp = req_lib.post(api_url, headers=headers, json=payload, timeout=60)
        resp_data = resp.json()

        if resp.status_code != 200:
            error_msg = resp_data.get("error", {}).get("message", str(resp_data))
            if progress_callback:
                progress_callback(0, 1, f"语音识别 API 错误: {error_msg[:80]}")
            return {"transcribed": False, "reason": "api_error", "error": error_msg}

        # 解析结果
        if progress_callback:
            progress_callback(2, 3, "正在解析识别结果...")

        choices = resp_data.get("choices", [])
        if not choices:
            if progress_callback:
                progress_callback(3, 3, "语音识别无结果")
            return {"transcribed": False, "reason": "empty_response"}

        full_text = choices[0].get("message", {}).get("content", "").strip()

        # Qwen3-ASR 返回的是纯文本，没有时间戳分段
        # 如果有分段格式，解析它
        segments = []
        if full_text:
            # 简单按句号/感叹号/问号分段
            import re
            sentences = re.split(r'([。！？\.\!\?])', full_text)
            current_time = 0.0
            for i in range(0, len(sentences) - 1, 2):
                text = sentences[i] + (sentences[i+1] if i+1 < len(sentences) else "")
                text = text.strip()
                if text:
                    # 粗略估算时间（按字符数比例分配）
                    segments.append({
                        "start": round(current_time, 2),
                        "end": round(current_time + 0.5, 2),  # 无法精确分时
                        "text": text,
                    })
                    current_time += 0.5

        if progress_callback:
            progress_callback(3, 3, f"语音识别完成: \"{full_text[:30]}{'...' if len(full_text) > 30 else ''}\"")

    except Exception as e:
        error_str = str(e)
        if progress_callback:
            progress_callback(0, 1, f"语音识别出错: {error_str[:80]}")
        return {"transcribed": False, "reason": "exception", "error": error_str}

    # 保存结果
    transcript = {
        "transcribed": len(full_text) > 0,
        "model": model_name,
        "full_text": full_text,
        "segments": segments,
        "segment_count": len(segments),
    }
    if not transcript["transcribed"]:
        transcript["reason"] = "empty_result"

    transcript_path = os.path.join(output_dir, "transcript.json")
    with open(transcript_path, "w", encoding="utf-8") as f:
        json.dump(transcript, f, ensure_ascii=False, indent=2)

    return transcript


def get_video_info(video_path):
    """获取视频元信息（时长、分辨率、帧率等）— 通过解析 FFmpeg stderr 输出"""
    ffmpeg = get_ffmpeg_path()

    # FFmpeg 不带输出文件时会把信息输出到 stderr
    cmd = [ffmpeg, "-i", video_path]
    result = subprocess.run(
        cmd,
        capture_output=True, timeout=30,
        creationflags=get_subprocess_creationflags()
    )
    output = result.stderr.decode('utf-8', errors='replace') if result.stderr else ""

    # 解析 duration
    duration = 0
    m = re.search(r"Duration: (\d{2}):(\d{2}):(\d{2})\.(\d{2})", output)
    if m:
        duration = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3)) + int(m.group(4)) / 100

    # 解析视频流: 720x1280
    width = 0
    height = 0
    m = re.search(r"Video:.*?(\d{3,5})x(\d{3,5})", output)
    if m:
        width = int(m.group(1))
        height = int(m.group(2))

    # 解析帧率: 30 fps
    fps = 0
    m = re.search(r"(\d+(?:\.\d+)?)\s*fps", output)
    if m:
        fps = float(m.group(1))

    # 检查是否有音频流
    has_audio = bool(re.search(r"Audio:", output))

    # 文件大小
    file_size = os.path.getsize(video_path) if os.path.exists(video_path) else 0

    return {
        "duration": round(duration, 2),
        "width": width,
        "height": height,
        "fps": round(fps, 2),
        "has_audio": has_audio,
        "file_size": file_size,
        "file_size_mb": round(file_size / 1024 / 1024, 2),
    }


def extract_frames(video_path, output_dir, fps=2, progress_callback=None):
    """
    从视频中提取关键帧
    fps: 每秒提取帧数
    progress_callback: 回调函数(current, total, step_description)
    """
    ffmpeg = get_ffmpeg_path()
    os.makedirs(output_dir, exist_ok=True)
    for old_frame in os.listdir(output_dir):
        if old_frame.startswith("frame_") and old_frame.endswith(".jpg"):
            try:
                os.remove(os.path.join(output_dir, old_frame))
            except OSError:
                pass

    # 先获取视频时长以估算帧数
    info = get_video_info(video_path)
    if not info:
        raise RuntimeError("无法读取视频信息")

    duration = info["duration"]
    estimated_frames = max(int(duration * fps) + 1, 1)

    if progress_callback:
        progress_callback(0, estimated_frames, "开始提取关键帧...")

    # FFmpeg 抽帧命令
    output_pattern = os.path.join(output_dir, "frame_%04d.jpg")
    cmd = [
        ffmpeg,
        "-err_detect", "ignore_err",
        "-i", video_path,
        "-vf", f"fps={fps}",
        "-vsync", "0",
        "-q:v", "2",           # 高质量 JPEG
        "-y",                  # 覆盖输出
        output_pattern
    ]

    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding='utf-8', errors='replace',
        creationflags=get_subprocess_creationflags()
    )

    # 读取 stderr 获取进度（FFmpeg 输出到 stderr），同时保留失败详情
    frame_count = 0
    stderr_lines = []
    while True:
        line = process.stderr.readline()
        if not line and process.poll() is not None:
            break
        if line:
            stderr_lines.append(line)
        # 简单估算进度
        if "frame=" in line and progress_callback:
            try:
                parts = line.strip().split()
                for i, p in enumerate(parts):
                    if p.startswith("frame="):
                        frame_count = int(p.split("=", 1)[1])
                        break
                    if p == "frame=" and i + 1 < len(parts):
                        frame_count = int(parts[i + 1])
                        break
                progress_callback(
                    min(frame_count, estimated_frames),
                    estimated_frames,
                    f"正在提取关键帧 ({frame_count}/{estimated_frames})"
                )
            except (ValueError, IndexError):
                pass

    process.wait()
    if process.returncode != 0:
        error_detail = "".join(stderr_lines).strip()
        if not error_detail:
            error_detail = f"FFmpeg 退出码 {process.returncode}，但没有返回详细错误"
        raise RuntimeError(f"FFmpeg 抽帧失败: {error_detail[-1200:]}")

    # 统计实际提取的帧数
    actual_frames = len([f for f in os.listdir(output_dir) if f.startswith("frame_") and f.endswith(".jpg")])
    if actual_frames == 0:
        raise RuntimeError("FFmpeg 抽帧失败: 没有生成任何关键帧，请检查视频编码或文件是否损坏")

    if progress_callback:
        progress_callback(actual_frames, actual_frames, f"关键帧提取完成，共 {actual_frames} 帧")

    return {
        "frames_count": actual_frames,
        "fps": fps,
        "frames_dir": output_dir,
    }


def analyze_frames(frames_dir, output_dir, max_frames=10, progress_callback=None):
    """
    使用通义千问 VL 分析视频关键帧
    1. 从所有帧中选择 max_frames 张代表性帧
    2. base64 编码后发送给 qwen-vl-plus 进行画面描述
    """
    import base64
    import requests as req_lib

    os.makedirs(output_dir, exist_ok=True)

    # 加载 API Key
    qwen_api_key = get_api_key("qwen_api_key")

    if not qwen_api_key:
        if progress_callback:
            progress_callback(0, 1, "通义千问 API Key 未配置，跳过画面分析")
        return {"analyzed": False, "reason": "no_api_key"}

    # 获取所有帧文件并按序排列
    frame_files = sorted([
        f for f in os.listdir(frames_dir)
        if f.startswith("frame_") and f.endswith(".jpg")
    ])

    if not frame_files:
        if progress_callback:
            progress_callback(0, 1, "未找到关键帧文件，跳过画面分析")
        return {"analyzed": False, "reason": "no_frames"}

    # 选择代表性帧：均匀采样 max_frames 张
    if len(frame_files) <= max_frames:
        selected = frame_files
    else:
        # 始终包含第一帧和最后一帧，其余均匀采样
        step = (len(frame_files) - 1) / (max_frames - 1)
        selected = []
        for i in range(max_frames):
            idx = min(int(round(i * step)), len(frame_files) - 1)
            if idx not in [selected[-1]] if selected else True:
                frame_name = frame_files[idx]
                if frame_name not in selected:
                    selected.append(frame_name)
        # 去重保序
        selected = list(dict.fromkeys(selected))
        # 确保首尾都在
        if frame_files[0] not in selected:
            selected.insert(0, frame_files[0])
        if frame_files[-1] not in selected:
            selected.append(frame_files[-1])

    if progress_callback:
        progress_callback(0, len(selected), f"正在读取 {len(selected)} 张关键帧...")

    # 读取并 base64 编码所有选中帧
    frames_data = []
    for i, fname in enumerate(selected):
        fpath = os.path.join(frames_dir, fname)
        with open(fpath, "rb") as f:
            img_b64 = base64.b64encode(f.read()).decode()
        frames_data.append({
            "filename": fname,
            "frame_index": i + 1,
            "data_uri": f"data:image/jpeg;base64,{img_b64}",
        })
        if progress_callback:
            progress_callback(i + 1, len(selected), f"已读取 {i + 1}/{len(selected)} 帧")

    if progress_callback:
        progress_callback(0, len(selected), "正在调用通义千问 VL 分析画面...")

    # 构造 VL 请求 — 一次性发送所有帧
    content_parts = []
    for fd in frames_data:
        content_parts.append({
            "type": "image_url",
            "image_url": {"url": fd["data_uri"]},
        })

    prompt = """你是一位信息流广告视频分析专家。请分析以下来自同一支竖版信息流视频的关键帧画面（按时间顺序排列）。

请对每一帧画面进行简要描述，包括：
1. 画面内容：主体、场景、人物、道具
2. 文字信息：画面上出现的字幕/贴纸/标题文字（原样引用）
3. 视觉风格：色调、构图、特效
4. 与前一帧的变化（如有）

注意：这是竖版信息流广告视频（通常20秒以内），重点关注广告相关的视觉元素。

请严格按以下 JSON 格式输出（不要输出其他内容）：
```json
{
  "frames": [
    {
      "frame": 1,
      "content": "画面内容描述",
      "text": "画面上的文字（无则填空）",
      "style": "视觉风格描述",
      "change": "与前一帧的变化（第1帧填'起始帧'）"
    }
  ],
  "overall_visual": "整体视觉风格和连贯性总结"
}
```"""

    content_parts.append({"type": "text", "text": prompt})

    api_url = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {qwen_api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": "qwen-vl-plus",
        "messages": [
            {"role": "user", "content": content_parts}
        ],
        "max_tokens": 4000,
        "temperature": 0.3,
    }

    try:
        resp = req_lib.post(api_url, headers=headers, json=payload, timeout=120)
        resp_data = resp.json()

        if resp.status_code != 200:
            error_msg = resp_data.get("error", {}).get("message", str(resp_data))
            if progress_callback:
                progress_callback(0, 1, f"VL 分析 API 错误: {error_msg[:80]}")
            return {"analyzed": False, "reason": "api_error", "error": error_msg}

        raw_text = resp_data.get("choices", [{}])[0].get("message", {}).get("content", "")

        # 提取 JSON（可能被 ```json 包裹）
        json_match = re.search(r'\{[\s\S]*\}', raw_text)
        if json_match:
            analysis = json.loads(json_match.group())
        else:
            analysis = {"frames": [], "overall_visual": raw_text}

        analysis["raw_text"] = raw_text
        analysis["model"] = "qwen-vl-plus"
        analysis["analyzed"] = True
        analysis["frames_analyzed"] = len(frames_data)
        analysis["total_frames"] = len(frame_files)
        analysis["selected_frames"] = [fd["filename"] for fd in frames_data]

        if progress_callback:
            progress_callback(len(selected), len(selected), "画面分析完成")

    except json.JSONDecodeError as e:
        if progress_callback:
            progress_callback(0, 1, f"VL 结果解析失败: {str(e)[:60]}")
        return {"analyzed": False, "reason": "json_parse_error", "error": str(e)}
    except Exception as e:
        error_str = str(e)
        if progress_callback:
            progress_callback(0, 1, f"VL 分析出错: {error_str[:80]}")
        return {"analyzed": False, "reason": "exception", "error": error_str}

    # 保存结果
    analysis_path = os.path.join(output_dir, "frame_analysis.json")
    with open(analysis_path, "w", encoding="utf-8") as f:
        json.dump(analysis, f, ensure_ascii=False, indent=2)

    return analysis


def analyze_content(video_info, frame_analysis, transcript, output_dir, ad_data=None, progress_callback=None):
    """
    使用 DeepSeek 进行综合分析
    整合视频信息 + 画面分析 + 语音转文字 → 元素拆解 + 6维评分 + 跑量归因
    通过 Anthropic 兼容接口调用
    """
    import requests as req_lib

    os.makedirs(output_dir, exist_ok=True)

    # 加载 API Key
    deepseek_api_key = get_api_key("deepseek_api_key")

    if not deepseek_api_key:
        if progress_callback:
            progress_callback(0, 1, "DeepSeek API Key 未配置，跳过综合分析")
        return {"analyzed": False, "reason": "no_api_key"}

    if progress_callback:
        progress_callback(0, 3, "正在组织分析数据...")

    # 构造输入摘要
    video_info_str = json.dumps(video_info, ensure_ascii=False, indent=2)

    if frame_analysis and frame_analysis.get("analyzed"):
        frame_summary = json.dumps(frame_analysis, ensure_ascii=False, indent=2)
    else:
        frame_summary = "无画面分析结果"

    if transcript and transcript.get("transcribed"):
        transcript_str = f"原文: {transcript.get('full_text', '')}"
        if transcript.get("segments"):
            transcript_str += "\n分段: " + json.dumps(transcript["segments"], ensure_ascii=False, indent=2)
    else:
        transcript_str = "无语音内容（静音视频或无语音）"

    # 投放数据（如有）
    ad_data_str = "无投放数据"
    if ad_data and isinstance(ad_data, dict):
        parts = []
        if ad_data.get('cost') is not None: parts.append(f"消耗: {ad_data['cost']} 元")
        if ad_data.get('impressions') is not None: parts.append(f"展示次数: {int(ad_data['impressions'])}")
        if ad_data.get('clicks') is not None: parts.append(f"点击次数: {int(ad_data['clicks'])}")
        if ad_data.get('ctr') is not None: parts.append(f"CTR (点击率): {ad_data['ctr']}%")
        if ad_data.get('conversions') is not None: parts.append(f"转化数: {int(ad_data['conversions'])}")
        if ad_data.get('cpa') is not None: parts.append(f"CPA (平均转化成本): {ad_data['cpa']} 元")
        if ad_data.get('cvr') is not None: parts.append(f"CVR (转化率): {ad_data['cvr']}%")
        if ad_data.get('completion_rate') is not None: parts.append(f"完播率: {ad_data['completion_rate']}%")
        if parts:
            ad_data_str = "\n".join(parts)
            ad_hint = "注意：已有真实投放数据，请结合实际表现校准评分，分析内容质量与投放数据的匹配度。例如：高CTR说明素材吸引力强，高CVR说明转化引导有效，高完播率说明内容留存好。如果CPA远高于行业水平，需指出素材的不足之处。"
        else:
            ad_hint = "（无投放数据，仅基于内容分析评分）"
    else:
        ad_hint = "（无投放数据，仅基于内容分析评分）"

    # 综合分析 prompt
    analysis_prompt = f"""你是一位资深信息流广告编导和跑量分析师，擅长抖音/快手/腾讯广告等信息流平台。

请根据以下信息对这支信息流视频进行专业分析。

## 视频基本信息
{video_info_str}

## 关键帧画面分析
{frame_summary}

## 语音转文字结果
{transcript_str}

## 投放数据
{ad_data_str}

---
{ad_hint}

请严格按照以下 JSON 格式输出分析结果（不要输出任何其他内容）：

```json
{{
  "element_breakdown": {{
    "visual": {{
      "subject": "画面主体描述",
      "scene": "场景分析",
      "color_tone": "色调特征",
      "composition": "构图特点",
      "effects": "使用的视觉特效"
    }},
    "text": {{
      "spoken": "口播/旁白文字（如有）",
      "subtitles": "字幕/贴纸文字（如有）",
      "cta": "转化引导文字（如'点击下方''立即购买'等）",
      "headline": "标题/大字（如有）"
    }},
    "audio": {{
      "voice_style": "语音风格（播音腔/口语化/AI配音等）",
      "bgm_style": "BGM风格（如有）",
      "sound_effects": "音效使用"
    }},
    "structure": {{
      "hook": "前3秒内容（钩子）",
      "body": "中间内容",
      "ending": "结尾/CTA",
      "rhythm": "节奏分析（快/慢/有变化）"
    }}
  }},
  "scoring": {{
    "hook_strength": {{
      "score": 0,
      "max": 10,
      "comment": "前3秒钩子评价"
    }},
    "visual_quality": {{
      "score": 0,
      "max": 10,
      "comment": "画面质量评价"
    }},
    "copy_quality": {{
      "score": 0,
      "max": 10,
      "comment": "文案质量评价"
    }},
    "audio_quality": {{
      "score": 0,
      "max": 10,
      "comment": "音频质量评价"
    }},
    "rhythm": {{
      "score": 0,
      "max": 10,
      "comment": "节奏感评价"
    }},
    "conversion_guidance": {{
      "score": 0,
      "max": 10,
      "comment": "转化引导评价"
    }},
    "overall": {{
      "score": 0,
      "max": 10,
      "comment": "综合评价"
    }}
  }},
  "traffic_attribution": {{
    "ctr_drivers": ["点击率驱动因素1", "驱动因素2"],
    "cvr_drivers": ["转化率驱动因素1", "驱动因素2"],
    "completion_drivers": ["完播率驱动因素1", "驱动因素2"],
    "potential_issues": ["可能影响跑量的问题1", "问题2"],
    "overall_assessment": "综合跑量潜力评估",
    "improvement_suggestions": ["改进建议1", "建议2", "建议3"]
  }},
  "tags": {{
    "category": "品类分类（如：日用品、美妆、食品、服装、数码、教育、线索广告、游戏等）",
    "style": "视频风格（如：口播、剧情、测评、种草、搞笑、真人出镜、混剪等）",
    "duration_segment": "时长段（如：15s以内、15-30s、30-60s、60s以上）",
    "target_audience": "目标人群（如：18-25岁女性、宝妈、上班族、中老年等）",
    "tags": ["关键词标签1", "关键词标签2", "关键词标签3"]
  }}
}}
```"""

    if progress_callback:
        progress_callback(1, 3, "正在调用 DeepSeek 综合分析...")

    # 通过 Anthropic 兼容接口调用 DeepSeek
    api_url = "https://api.deepseek.com/anthropic/v1/messages"
    headers = {
        "x-api-key": deepseek_api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    payload = {
        "model": "deepseek-v4-flash",
        "max_tokens": 4096,
        "messages": [
            {"role": "user", "content": analysis_prompt}
        ],
    }

    try:
        resp = req_lib.post(api_url, headers=headers, json=payload, timeout=120)
        resp_data = resp.json()

        if resp.status_code != 200:
            error_msg = ""
            if "error" in resp_data:
                error_msg = resp_data["error"].get("message", str(resp_data["error"]))
            else:
                error_msg = str(resp_data)
            if progress_callback:
                progress_callback(0, 1, f"DeepSeek 分析 API 错误: {error_msg[:80]}")
            return {"analyzed": False, "reason": "api_error", "error": error_msg}

        # 解析 Anthropic 格式响应
        raw_text = ""
        content_list = resp_data.get("content", [])
        for block in content_list:
            if block.get("type") == "text":
                raw_text += block.get("text", "")

        if not raw_text:
            if progress_callback:
                progress_callback(0, 1, "DeepSeek 返回空结果")
            return {"analyzed": False, "reason": "empty_response"}

        if progress_callback:
            progress_callback(2, 3, "正在解析分析结果...")

        # 提取 JSON
        json_match = re.search(r'\{[\s\S]*\}', raw_text)
        if json_match:
            analysis = json.loads(json_match.group())
        else:
            analysis = {"raw_text": raw_text}

        analysis["model"] = "deepseek-v4-flash"
        analysis["analyzed"] = True
        analysis["raw_text"] = raw_text

        if progress_callback:
            progress_callback(3, 3, "综合分析完成")

    except json.JSONDecodeError as e:
        if progress_callback:
            progress_callback(0, 1, f"分析结果解析失败: {str(e)[:60]}")
        return {"analyzed": False, "reason": "json_parse_error", "error": str(e), "raw_text": raw_text if 'raw_text' in dir() else ""}
    except Exception as e:
        error_str = str(e)
        if progress_callback:
            progress_callback(0, 1, f"综合分析出错: {error_str[:80]}")
        return {"analyzed": False, "reason": "exception", "error": error_str}

    # 保存结果
    analysis_path = os.path.join(output_dir, "content_analysis.json")
    with open(analysis_path, "w", encoding="utf-8") as f:
        json.dump(analysis, f, ensure_ascii=False, indent=2)

    return analysis


def generate_breakdown(result, output_dir, progress_callback=None):
    """
    基于已有分析结果，生成内容拆解报告（数据/脚本/风险/建议）。
    复用现有分析数据，只调一次 DeepSeek 做格式化输出。
    返回结构化 JSON，失败返回 None。
    """
    import requests as req_lib

    # 加载 API Key
    deepseek_api_key = get_api_key("deepseek_api_key")

    if not deepseek_api_key:
        if progress_callback:
            progress_callback(0, 1, "DeepSeek API Key 未配置")
        return None

    # 组装已有分析数据
    video_info = result.get("video_info", {})
    frame_analysis = result.get("frame_analysis", {})
    transcript = result.get("transcript", {})
    content_analysis = result.get("content_analysis", {})
    ad_data = result.get("ad_data", {})
    tags = result.get("tags", {})
    scoring = result.get("scoring", {})

    # 构建输入摘要
    video_info_str = json.dumps(video_info, ensure_ascii=False, indent=2)

    frame_summary = "无画面分析结果"
    if frame_analysis and frame_analysis.get("analyzed"):
        frame_summary = json.dumps(frame_analysis, ensure_ascii=False, indent=2)

    transcript_str = "无语音内容"
    if transcript and transcript.get("transcribed"):
        transcript_str = transcript.get("full_text", "")

    ad_data_str = "无投放数据"
    if ad_data and isinstance(ad_data, dict) and ad_data:
        parts = []
        for k, v in ad_data.items():
            if v is not None:
                parts.append(f"{k}: {v}")
        if parts:
            ad_data_str = "\n".join(parts)

    scoring_str = "无评分数据"
    if scoring and scoring.get("overall"):
        scoring_str = json.dumps(scoring, ensure_ascii=False, indent=2)

    element_breakdown = content_analysis.get("element_breakdown", {})
    element_str = json.dumps(element_breakdown, ensure_ascii=False, indent=2) if element_breakdown else "无元素拆解数据"

    traffic = content_analysis.get("traffic_attribution", {})
    traffic_str = json.dumps(traffic, ensure_ascii=False, indent=2) if traffic else "无跑量归因数据"

    if progress_callback:
        progress_callback(0, 2, "正在生成内容拆解报告...")

    # 构建 prompt（字符串拼接避免 f-string 花括号冲突）
    prompt = (
        "你是一位资深信息流广告编导，擅长对视频素材进行结构化拆解。\n\n"
        "请根据以下已有分析数据，输出一份**简洁内容报告**，用于指导脚本修改。\n\n"
        "## 视频信息\n" + video_info_str + "\n\n"
        "## 画面分析\n" + frame_summary + "\n\n"
        "## 语音转写\n" + transcript_str + "\n\n"
        "## 投放数据\n" + ad_data_str + "\n\n"
        "## 评分数据\n" + scoring_str + "\n\n"
        "## 元素拆解\n" + element_str + "\n\n"
        "## 跑量归因\n" + traffic_str + "\n\n"
        "---\n\n"
        "请严格按照以下 JSON 格式输出（不要输出任何其他内容，只输出 JSON）：\n\n"
        "```json\n"
        "{\n"
        '  "breakdown": {\n'
        '    "data": {\n'
        '      "overall_score": "综合评分",\n'
        '      "hook_score": "钩子强度评分",\n'
        '      "visual_score": "画面质量评分",\n'
        '      "copy_score": "文案质量评分",\n'
        '      "audio_score": "音频质量评分",\n'
        '      "rhythm_score": "节奏感评分",\n'
        '      "conversion_score": "转化引导评分",\n'
        '      "ad_metrics": "投放数据摘要（如有）",\n'
        '      "traffic_potential": "跑量潜力评估"\n'
        "    },\n"
        '    "script_content": {\n'
        '      "hook": "前3秒钩子内容（画面+口播/字幕）",\n'
        '      "body": "中间主体内容（画面+口播/字幕）",\n'
        '      "ending": "结尾CTA内容（画面+口播/字幕）",\n'
        '      "full_script": "完整脚本文字稿（按时间线）",\n'
        '      "key_visuals": ["关键画面描述1", "关键画面描述2"],\n'
        '      "key_audio": ["关键音频/口播1", "关键音频/口播2"]\n'
        "    },\n"
        '    "risks": [\n'
        "      {\n"
        '        "item": "风险点描述",\n'
        '        "severity": "高/中/低",\n'
        '        "reason": "为什么这是个风险"\n'
        "      }\n"
        "    ],\n"
        '    "improvements": [\n'
        "      {\n"
        '        "item": "改进建议描述",\n'
        '        "priority": "高/中/低",\n'
        '        "expected_impact": "预期效果"\n'
        "      }\n"
        "    ]\n"
        "  }\n"
        "}\n"
        "```"
    )

    api_url = "https://api.deepseek.com/anthropic/v1/messages"
    headers = {
        "x-api-key": deepseek_api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    payload = {
        "model": "deepseek-v4-flash",
        "max_tokens": 4096,
        "messages": [{"role": "user", "content": prompt}],
    }

    try:
        resp = req_lib.post(api_url, headers=headers, json=payload, timeout=120)
        resp_data = resp.json()

        if resp.status_code != 200:
            error_msg = resp_data.get("error", {}).get("message", str(resp_data))
            if progress_callback:
                progress_callback(0, 1, f"DeepSeek API 错误: {error_msg[:80]}")
            return None

        raw_text = ""
        if "content" in resp_data:
            for block in resp_data["content"]:
                raw_text += block.get("text", "")

        if not raw_text:
            if progress_callback:
                progress_callback(0, 1, "DeepSeek 返回空结果")
            return None

        # 尝试从 ```json 代码块中提取 JSON
        json_match = re.search(r'```json\s*([\s\S]*?)\s*```', raw_text)
        if json_match:
            try:
                breakdown = json.loads(json_match.group(1))
            except json.JSONDecodeError:
                breakdown = {"raw_text": raw_text}
        else:
            #  fallback：尝试直接找最外层的花括号
            brace_match = re.search(r'\{[\s\S]*\}', raw_text)
            if brace_match:
                try:
                    breakdown = json.loads(brace_match.group())
                except json.JSONDecodeError:
                    breakdown = {"raw_text": raw_text}
            else:
                breakdown = {"raw_text": raw_text}

        breakdown["analyzed"] = True
        breakdown["model"] = "deepseek-v4-flash"

        if progress_callback:
            progress_callback(2, 2, "内容拆解报告生成完成")

        # 保存结果
        breakdown_path = os.path.join(output_dir, "breakdown.json")
        with open(breakdown_path, "w", encoding="utf-8") as f:
            json.dump(breakdown, f, ensure_ascii=False, indent=2)

        return breakdown

    except Exception as e:
        if progress_callback:
            progress_callback(0, 1, f"生成拆解报告出错: {str(e)[:80]}")
        return None


def generate_script_variants(result, output_dir, user_variables=None, review_rules="", review_rule_id="", progress_callback=None):
    """
    基于已有分析结果生成爆款归因和脚本裂变方案。
    user_variables 可传入用户编辑后的人群/场景/痛点/开头/反转变量。
    """
    import requests as req_lib

    deepseek_api_key = get_api_key("deepseek_api_key")

    if not deepseek_api_key:
        if progress_callback:
            progress_callback(0, 1, "DeepSeek API Key 未配置")
        return None

    video_info = result.get("video_info", {})
    frame_analysis = result.get("frame_analysis", {})
    transcript = result.get("transcript", {})
    content_analysis = result.get("content_analysis", {})
    tags = result.get("tags", {})
    scoring = result.get("scoring", {})
    ad_data = result.get("ad_data", {})

    transcript_str = "无语音转写"
    if transcript and transcript.get("transcribed"):
        transcript_str = transcript.get("full_text", "")

    payload_context = {
        "video_name": result.get("video_name", ""),
        "video_info": video_info,
        "frame_analysis": frame_analysis,
        "transcript": transcript_str,
        "content_analysis": content_analysis,
        "tags": tags,
        "scoring": scoring,
        "ad_data": ad_data,
        "user_variables": user_variables or {},
        "review_rule_id": review_rule_id or "",
        "review_rules": review_rules or "未选择审核规范。仍需按信息流广告常识避免绝对化、迷信保证、夸大承诺和敏感收益表达。",
    }
    context_str = json.dumps(payload_context, ensure_ascii=False, indent=2)

    if progress_callback:
        progress_callback(0, 2, "正在生成脚本裂变方案...")

    prompt = (
        "你是一位资深信息流广告编导和短视频策划，擅长命理测算、情感关系、剧情广告素材的爆款归因与脚本裂变。\n\n"
        "请基于已有视频分析数据，完成三件事：\n"
        "1. 解释这条素材为什么可能跑量，拆出可复制结构。\n"
        "2. 生成可供编导前期策划使用的脚本裂变变量。\n"
        "3. 输出可直接进入编导修改环节的完整视频文案，而不是只给方向或大纲。\n\n"
        "如果 user_variables 中提供了用户自定义变量，请优先使用；如果没有，请先由你推荐。\n\n"
        "如果 review_rules 中提供了审核规范，请严格遵守：避开禁用词、敏感承诺、绝对化表达，并在审核自检中指出替代表达。\n\n"
        "## 输入数据\n"
        + context_str +
        "\n\n请严格输出 JSON，不要输出解释性前后缀。格式如下：\n"
        "```json\n"
        "{\n"
        '  "script_variants": {\n'
        '    "viral_attribution": {\n'
        '      "core_hook": "这条素材最核心的开头钩子",\n'
        '      "emotional_trigger": "打中的情绪/焦虑/欲望",\n'
        '      "audience_insight": "目标人群洞察",\n'
        '      "structure_formula": "可复制脚本公式，例如：人群痛点 + 异常事件 + 点醒反转 + 结果承诺 + CTA",\n'
        '      "why_it_may_work": ["跑量原因1", "跑量原因2", "跑量原因3"],\n'
        '      "risks": ["可能影响转化或审核的风险"]\n'
        "    },\n"
        '    "original_structure": {\n'
        '      "audience": "原片人群",\n'
        '      "scene": "原片场景",\n'
        '      "pain_point": "原片痛点",\n'
        '      "opening": "原片开头方式",\n'
        '      "reversal": "原片反转/点醒",\n'
        '      "cta": "原片结尾引导"\n'
        "    },\n"
        '    "variable_bank": {\n'
        '      "audiences": ["AI推荐人群1", "AI推荐人群2", "AI推荐人群3", "AI推荐人群4", "AI推荐人群5"],\n'
        '      "scenes": ["AI推荐场景1", "AI推荐场景2", "AI推荐场景3", "AI推荐场景4", "AI推荐场景5"],\n'
        '      "pain_points": ["AI推荐痛点1", "AI推荐痛点2", "AI推荐痛点3", "AI推荐痛点4", "AI推荐痛点5"],\n'
        '      "openings": ["AI推荐开头1", "AI推荐开头2", "AI推荐开头3", "AI推荐开头4", "AI推荐开头5"],\n'
        '      "reversals": ["AI推荐反转1", "AI推荐反转2", "AI推荐反转3", "AI推荐反转4", "AI推荐反转5"]\n'
        "    },\n"
        '    "directions": [\n'
        "      {\n"
        '        "title": "裂变方向标题",\n'
        '        "variant_type": "换人群/换场景/换痛点/换开头/换反转/综合裂变",\n'
        '        "selected_variables": {\n'
        '          "audience": "使用的人群",\n'
        '          "scene": "使用的场景",\n'
        '          "pain_point": "使用的痛点",\n'
        '          "opening": "使用的开头",\n'
        '          "reversal": "使用的反转"\n'
        "        },\n"
        '        "script_outline": "15-30秒脚本概要，按开头/发展/反转/CTA描述",\n'
        '        "complete_script": {\n'
        '          "duration": "建议时长，例如 20-30秒",\n'
        '          "title": "视频文案标题",\n'
        '          "opening_hook": "前3秒完整开头台词/字幕",\n'
        '          "voiceover": "完整口播文案，按成片顺序写，避免只写要点",\n'
        '          "on_screen_text": ["字幕/大字1", "字幕/大字2", "字幕/大字3"],\n'
        '          "shot_list": [\n'
        '            {"time": "0-3s", "visual": "画面/表演", "audio": "口播/音效", "subtitle": "屏幕文字"}\n'
        '          ],\n'
        '          "cta": "完整结尾引导文案",\n'
        '          "production_notes": "拍摄/剪辑/BGM/节奏建议"\n'
        '        },\n'
        '        "audit_check": {\n'
        '          "risk_level": "低/中/高",\n'
        '          "matched_sensitive_words": ["命中的敏感词，没有则为空数组"],\n'
        '          "replacement_suggestions": [{"original": "原词", "replace_with": "替代表达", "reason": "替换原因"}],\n'
        '          "compliance_notes": ["审核注意事项1", "审核注意事项2"]\n'
        '        },\n'
        '        "risk_note": "给编导看的整体风险提醒"\n'
        "      }\n"
        "    ]\n"
        "  }\n"
        "}\n"
        "```"
    )

    api_url = "https://api.deepseek.com/anthropic/v1/messages"
    headers = {
        "x-api-key": deepseek_api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    payload = {
        "model": "deepseek-v4-flash",
        "max_tokens": 6000,
        "messages": [{"role": "user", "content": prompt}],
    }

    try:
        resp = req_lib.post(api_url, headers=headers, json=payload, timeout=160)
        resp_data = resp.json()

        if resp.status_code != 200:
            error_msg = resp_data.get("error", {}).get("message", str(resp_data))
            if progress_callback:
                progress_callback(0, 1, f"DeepSeek API 错误: {error_msg[:80]}")
            return None

        raw_text = ""
        for block in resp_data.get("content", []):
            raw_text += block.get("text", "")

        if not raw_text:
            if progress_callback:
                progress_callback(0, 1, "DeepSeek 返回空结果")
            return None

        json_match = re.search(r'```json\s*([\s\S]*?)\s*```', raw_text)
        if json_match:
            variants = json.loads(json_match.group(1))
        else:
            brace_match = re.search(r'\{[\s\S]*\}', raw_text)
            variants = json.loads(brace_match.group()) if brace_match else {"raw_text": raw_text}

        variants["analyzed"] = True
        variants["model"] = "deepseek-v4-flash"
        variants["raw_text"] = raw_text

        variants_path = os.path.join(output_dir, "script_variants.json")
        with open(variants_path, "w", encoding="utf-8") as f:
            json.dump(variants, f, ensure_ascii=False, indent=2)

        if progress_callback:
            progress_callback(2, 2, "脚本裂变方案生成完成")

        return variants

    except Exception as e:
        if progress_callback:
            progress_callback(0, 1, f"生成脚本裂变出错: {str(e)[:80]}")
        return None


def extract_audio(video_path, output_path, progress_callback=None):
    """
    从视频中分离音频轨道
    output_path: 输出 wav 文件路径
    """
    ffmpeg = get_ffmpeg_path()
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    if progress_callback:
        progress_callback(0, 1, "正在分离音频...")

    cmd = [
        ffmpeg,
        "-i", video_path,
        "-vn",                 # 不要视频
        "-acodec", "pcm_s16le", # 16bit PCM WAV
        "-ar", "16000",        # 16kHz 采样率 (Whisper 推荐格式)
        "-ac", "1",            # 单声道
        "-y",
        output_path
    ]

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        creationflags=get_subprocess_creationflags()
    )

    # 检查输出文件是否存在且大小 > 0
    audio_size = 0
    if os.path.exists(output_path):
        audio_size = os.path.getsize(output_path)

    if result.returncode != 0 or audio_size == 0:
        # 可能没有音频轨道，或提取失败
        if audio_size == 0 and os.path.exists(output_path):
            os.remove(output_path)
        if progress_callback:
            progress_callback(0, 1, "该视频没有音频轨道")
        return {"audio_extracted": False, "reason": "no_audio"}

    if progress_callback:
        progress_callback(1, 1, f"音频分离完成 ({round(audio_size/1024, 1)} KB)")

    return {
        "audio_extracted": True,
        "audio_path": output_path,
        "audio_size_kb": round(audio_size / 1024, 1),
    }


def extract_cover(frames_dir, video_dir, cover_name="cover.jpg"):
    """
    从已提取的帧中选取封面图，复制到视频输出目录。
    策略：优先选取第3帧（约1秒处，跳过黑帧/片头过渡），
          如果不足3帧则取最后一帧。
    返回封面图路径，失败返回 None。
    """
    import shutil

    if not os.path.isdir(frames_dir):
        return None

    # 收集所有帧文件并按编号排序
    frame_files = sorted([
        f for f in os.listdir(frames_dir)
        if f.startswith("frame_") and f.endswith(".jpg")
    ])

    if not frame_files:
        return None

    # 优先取第3帧（跳过可能的黑帧/转场）
    pick = frame_files[2] if len(frame_files) > 2 else frame_files[-1]
    src = os.path.join(frames_dir, pick)
    dst = os.path.join(video_dir, cover_name)

    try:
        shutil.copy2(src, dst)
        return dst
    except Exception:
        return None
