# -*- coding: utf-8 -*-
"""
信息流素材分析 Agent - Flask 后端
启动: python app.py
访问: http://localhost:5000
"""

import os
import sys
import json
import uuid
import queue
import shutil
import threading
from datetime import datetime
from flask import Flask, render_template, request, jsonify, Response, send_from_directory

from processor import get_video_info, extract_frames, extract_audio, transcribe_audio, analyze_frames, analyze_content, extract_cover, generate_breakdown, generate_script_variants
from report_generator import generate_word_report, generate_pdf_report

app = Flask(__name__)

# ========== 配置 ==========
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", BASE_DIR)
OUTPUT_DIR = os.path.join(DATA_DIR, "output")
UPLOAD_FOLDER = os.path.join(DATA_DIR, "uploads")
REVIEW_RULES_DIR = os.path.join(DATA_DIR, "review_rules")

os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(REVIEW_RULES_DIR, exist_ok=True)

# API 配置
DEEPSEEK_BASE_URL = "https://api.deepseek.com/anthropic"
DEEPSEEK_API_KEY = ""

QWEN_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
QWEN_API_KEY = ""

ANALYSIS_MODELS = {
    "vision": "qwen-vl-plus",
    "text": "deepseek-v4-flash",
}

# 处理任务状态 (video_id -> Queue)
task_queues = {}
# 处理结果 (video_id -> dict)
task_results = {}


# ========== 工具函数 ==========
def get_env_float(name, default):
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return float(default)


def get_env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return int(default)


def get_frame_sampling(video_info):
    """根据视频时长控制抽帧量，避免免费服务器长时间卡在 FFmpeg。"""
    duration = max(float(video_info.get("duration") or 0), 1.0)
    max_fps = max(get_env_float("FRAME_FPS", 1), 0.1)
    max_frames = max(get_env_int("MAX_EXTRACTED_FRAMES", 24), 6)
    return min(max_fps, max_frames / duration)


def load_config():
    """加载 API 配置"""
    env_config = {
        "deepseek_api_key": os.environ.get("DEEPSEEK_API_KEY", "").strip(),
        "qwen_api_key": os.environ.get("QWEN_API_KEY", "").strip(),
    }
    config_paths = [
        os.path.join(DATA_DIR, "config.json"),
        os.path.join(BASE_DIR, "config.json"),
    ]
    for config_path in config_paths:
        if os.path.exists(config_path):
            with open(config_path, "r", encoding="utf-8") as f:
                file_config = json.load(f)
            return {
                "deepseek_api_key": env_config["deepseek_api_key"] or file_config.get("deepseek_api_key", ""),
                "qwen_api_key": env_config["qwen_api_key"] or file_config.get("qwen_api_key", ""),
            }
    return env_config


def save_config(config):
    """保存 API 配置"""
    os.makedirs(DATA_DIR, exist_ok=True)
    config_path = os.path.join(DATA_DIR, "config.json")
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


def init_api_keys():
    """初始化 API Key"""
    global DEEPSEEK_API_KEY, QWEN_API_KEY

    config = load_config()
    if config.get("deepseek_api_key"):
        DEEPSEEK_API_KEY = config["deepseek_api_key"]
    if config.get("qwen_api_key"):
        QWEN_API_KEY = config["qwen_api_key"]


def _safe_rule_filename(name):
    """生成审核规范文件名，避免路径穿越和特殊字符问题"""
    base = os.path.splitext(name or "")[0].strip()
    base = "".join(ch if ch.isalnum() or ch in ("-", "_", " ") else "_" for ch in base)
    base = base.strip(" ._") or "review_rule"
    return base[:60] + ".txt"


def _read_review_rule(rule_id):
    if not rule_id:
        return ""
    rule_path = os.path.abspath(os.path.join(REVIEW_RULES_DIR, rule_id))
    if not rule_path.startswith(os.path.abspath(REVIEW_RULES_DIR)):
        return ""
    if not os.path.exists(rule_path):
        return ""
    with open(rule_path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()[:12000]


def _list_review_rules():
    rules = []
    for name in os.listdir(REVIEW_RULES_DIR):
        path = os.path.join(REVIEW_RULES_DIR, name)
        if not os.path.isfile(path) or not name.lower().endswith(".txt"):
            continue
        rules.append({
            "id": name,
            "name": os.path.splitext(name)[0],
            "size": os.path.getsize(path),
            "updated_at": datetime.fromtimestamp(os.path.getmtime(path)).strftime("%Y-%m-%d %H:%M:%S"),
        })
    rules.sort(key=lambda x: x["updated_at"], reverse=True)
    return rules


# ========== 后台处理 ==========
def process_video(video_id, video_path, video_name):
    """后台线程: 视频处理流程"""
    q = task_queues.get(video_id)
    if not q:
        return

    def progress(step, current, total, message):
        """发送进度到队列"""
        try:
            q.put(json.dumps({
                "step": step,
                "current": current,
                "total": total,
                "message": message,
            }, ensure_ascii=False))
        except Exception:
            pass

    video_dir = os.path.join(OUTPUT_DIR, video_id)
    frames_dir = os.path.join(video_dir, "frames")
    audio_path = os.path.join(video_dir, "audio.wav")

    result = {
        "video_id": video_id,
        "video_name": video_name,
        "video_path": video_path,
        "status": "processing",
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }

    try:
        # Step 1: 获取视频信息
        progress("info", 0, 1, "正在读取视频信息...")
        info = get_video_info(video_path)
        if not info:
            raise RuntimeError("无法读取视频信息，文件可能已损坏")
        result["video_info"] = info
        progress("info", 1, 1, f"视频信息: {info['width']}x{info['height']}, {info['duration']}s")

        # Step 2: 提取关键帧
        frame_fps = get_frame_sampling(info)
        progress("frames", 0, 1, f"正在提取关键帧，约 {frame_fps:.2f} 帧/秒...")
        frames_result = extract_frames(video_path, frames_dir, fps=frame_fps,
                                        progress_callback=lambda c, t, m: progress("frames", c, t, m))
        result["frames"] = frames_result

        # 提取封面图
        cover_path = extract_cover(frames_dir, video_dir)
        if cover_path:
            result["cover"] = True

        # 保存中间结果
        result["status"] = "frames_extracted"
        _save_result(video_dir, result)

        # Step 3: 分离音频
        if info.get("has_audio"):
            progress("audio", 0, 1, "正在分离音频...")
            audio_result = extract_audio(video_path, audio_path,
                                          progress_callback=lambda c, t, m: progress("audio", c, t, m))
            result["audio"] = audio_result
        else:
            progress("audio", 1, 1, "视频无音频轨道，跳过")
            result["audio"] = {"audio_extracted": False, "reason": "no_audio"}

        # 保存中间结果
        result["status"] = "audio_extracted"
        _save_result(video_dir, result)

        # Step 4: 语音转文字 (Qwen3-ASR-Flash)
        if result["audio"].get("audio_extracted"):
            progress("asr", 0, 1, "正在启动语音识别...")
            asr_result = transcribe_audio(
                audio_path, video_dir, model_name="qwen3-asr-flash",
                progress_callback=lambda c, t, m: progress("asr", c, t, m)
            )
            result["transcript"] = asr_result
        else:
            progress("asr", 1, 1, "无音频，跳过语音识别")
            result["transcript"] = {"transcribed": False, "reason": "no_audio"}

        # 保存中间结果
        result["status"] = "asr_done"
        _save_result(video_dir, result)

        # Step 5: 画面分析 (通义千问 VL)
        progress("vl", 0, 1, "正在分析关键帧画面...")
        vl_result = analyze_frames(
            frames_dir, video_dir, max_frames=get_env_int("MAX_VL_FRAMES", 8),
            progress_callback=lambda c, t, m: progress("vl", c, t, m)
        )
        result["frame_analysis"] = vl_result

        # 保存中间结果
        result["status"] = "vl_done"
        _save_result(video_dir, result)

        # Step 6: 综合分析 (DeepSeek)
        progress("analysis", 0, 1, "正在进行综合 AI 分析...")
        ad_data = result.get("ad_data")
        analysis_result = analyze_content(
            result["video_info"], vl_result, result["transcript"],
            video_dir, ad_data=ad_data,
            progress_callback=lambda c, t, m: progress("analysis", c, t, m)
        )
        result["content_analysis"] = analysis_result

        # 提取评分摘要
        if analysis_result.get("analyzed") and analysis_result.get("scoring"):
            scoring = analysis_result["scoring"]
            overall_score = scoring.get("overall", {}).get("score", 0)
            result["scoring"] = {
                "overall": overall_score,
                "details": {
                    k: v.get("score", 0) for k, v in scoring.items() if isinstance(v, dict) and "score" in v
                },
            }

        # 提取标签
        if analysis_result.get("analyzed") and analysis_result.get("tags"):
            result["tags"] = analysis_result["tags"]

        # 处理完成
        result["status"] = "analyzed"
        result["analyzed_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        _save_result(video_dir, result)

        task_results[video_id] = result
        progress("done", 1, 1, "分析完成！")

    except Exception as e:
        result["status"] = "error"
        result["error"] = str(e)
        _save_result(video_dir, result)
        progress("error", 0, 1, f"处理出错: {str(e)}")


def _save_result(video_dir, result):
    """保存处理结果到 result.json"""
    result_path = os.path.join(video_dir, "result.json")
    with open(result_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)


# ========== 路由 ==========
@app.route("/")
def index():
    """首页"""
    return render_template("index.html")


@app.route("/api/status")
def api_status():
    """API 状态检查"""
    config = load_config()
    return jsonify({
        "status": "ok",
        "deepseek_configured": bool(config.get("deepseek_api_key") or DEEPSEEK_API_KEY),
        "qwen_configured": bool(config.get("qwen_api_key") or QWEN_API_KEY),
        "models": ANALYSIS_MODELS,
    })


@app.route("/api/review-rules", methods=["GET"])
def list_review_rules():
    """获取审核规范列表"""
    return jsonify({"rules": _list_review_rules()})


@app.route("/api/review-rules", methods=["POST"])
def upload_review_rule():
    """上传审核规范。V1 支持 txt/md 文本文件。"""
    if "file" not in request.files:
        return jsonify({"error": "未选择文件"}), 400
    file = request.files["file"]
    if file.filename == "":
        return jsonify({"error": "文件名为空"}), 400

    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in {".txt", ".md"}:
        return jsonify({"error": "当前版本先支持 .txt / .md 审核规范文件"}), 400

    raw = file.read()
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError:
        content = raw.decode("gbk", errors="replace")
    content = content.strip()
    if not content:
        return jsonify({"error": "文件内容为空"}), 400

    rule_name = request.form.get("name") or file.filename
    safe_name = _safe_rule_filename(rule_name)
    output_path = os.path.join(REVIEW_RULES_DIR, safe_name)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(content)

    return jsonify({"status": "ok", "rule": {
        "id": safe_name,
        "name": os.path.splitext(safe_name)[0],
        "size": os.path.getsize(output_path),
        "updated_at": datetime.fromtimestamp(os.path.getmtime(output_path)).strftime("%Y-%m-%d %H:%M:%S"),
    }})


@app.route("/api/review-rules/<rule_id>", methods=["GET"])
def get_review_rule(rule_id):
    """查看审核规范内容"""
    content = _read_review_rule(rule_id)
    if not content:
        return jsonify({"error": "审核规范不存在"}), 404
    return jsonify({"id": rule_id, "content": content})


@app.route("/api/review-rules/<rule_id>", methods=["DELETE"])
def delete_review_rule(rule_id):
    """删除审核规范"""
    rule_path = os.path.abspath(os.path.join(REVIEW_RULES_DIR, rule_id))
    if not rule_path.startswith(os.path.abspath(REVIEW_RULES_DIR)):
        return jsonify({"error": "非法文件名"}), 400
    if not os.path.exists(rule_path):
        return jsonify({"error": "审核规范不存在"}), 404
    os.remove(rule_path)
    return jsonify({"status": "ok", "message": "审核规范已删除"})


@app.route("/api/config", methods=["GET"])
def get_config():
    """获取当前配置（隐藏完整 key）"""
    config = load_config()
    return jsonify({
        "deepseek_api_key": (config.get("deepseek_api_key") or DEEPSEEK_API_KEY)[:8] + "..." if (config.get("deepseek_api_key") or DEEPSEEK_API_KEY) else "",
        "qwen_api_key": (config.get("qwen_api_key") or QWEN_API_KEY)[:8] + "..." if (config.get("qwen_api_key") or QWEN_API_KEY) else "",
    })


@app.route("/api/config", methods=["POST"])
def update_config():
    """更新 API 配置"""
    data = request.json
    config = load_config()

    if "deepseek_api_key" in data:
        config["deepseek_api_key"] = data["deepseek_api_key"].strip()
    if "qwen_api_key" in data:
        config["qwen_api_key"] = data["qwen_api_key"].strip()

    save_config(config)
    init_api_keys()
    return jsonify({"status": "ok", "message": "配置已保存"})


@app.route("/api/upload", methods=["POST"])
def upload_video():
    """上传视频文件并启动处理"""
    if "file" not in request.files:
        return jsonify({"error": "未选择文件"}), 400

    file = request.files["file"]
    if file.filename == "":
        return jsonify({"error": "文件名为空"}), 400

    # 检查文件格式
    allowed_exts = {".mp4", ".mov", ".avi", ".mkv"}
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in allowed_exts:
        return jsonify({"error": f"不支持 {ext} 格式，请上传 MP4/MOV/AVI/MKV"}), 400

    # 保存文件
    video_id = str(uuid.uuid4())[:8]
    video_dir = os.path.join(OUTPUT_DIR, video_id)
    os.makedirs(video_dir, exist_ok=True)

    safe_name = os.path.splitext(file.filename)[0]
    video_path = os.path.join(video_dir, f"{safe_name}{ext}")
    file.save(video_path)

    # 如果上传时附带了投放数据，保存到 result.json
    ad_data_raw = request.form.get("ad_data")
    if ad_data_raw:
        try:
            ad_data = json.loads(ad_data_raw)
            if isinstance(ad_data, dict) and ad_data:
                _save_result(video_dir, {
                    "video_id": video_id,
                    "video_name": file.filename,
                    "video_path": video_path,
                    "ad_data": ad_data,
                    "status": "uploaded",
                    "created_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
                })
        except (json.JSONDecodeError, TypeError):
            pass

    # 创建进度队列并启动后台处理
    task_queues[video_id] = queue.Queue()
    thread = threading.Thread(
        target=process_video,
        args=(video_id, video_path, file.filename),
        daemon=True
    )
    thread.start()

    return jsonify({
        "video_id": video_id,
        "video_name": file.filename,
        "message": "上传成功，开始处理"
    })


@app.route("/api/progress/<video_id>")
def progress_stream(video_id):
    """SSE 进度推送"""
    q = task_queues.get(video_id)
    if not q:
        return jsonify({"error": "任务不存在"}), 404

    def generate():
        while True:
            try:
                data = q.get(timeout=30)
                yield f"data: {data}\n\n"
                # 如果处理完成或出错，结束流
                parsed = json.loads(data)
                if parsed.get("step") in ("done", "error"):
                    break
            except queue.Empty:
                yield f"data: {json.dumps({'step': 'heartbeat'})}\n\n"
                break

    return Response(generate(), mimetype="text/event-stream")


@app.route("/api/result/<video_id>")
def get_result(video_id):
    """获取视频处理结果"""
    result_path = os.path.join(OUTPUT_DIR, video_id, "result.json")
    if not os.path.exists(result_path):
        return jsonify({"error": "结果不存在"}), 404
    with open(result_path, "r", encoding="utf-8") as f:
        return jsonify(json.load(f))


@app.route("/api/cover/<video_id>")
def get_cover(video_id):
    """获取视频封面图"""
    video_dir = os.path.join(OUTPUT_DIR, video_id)
    cover_path = os.path.join(video_dir, "cover.jpg")

    if not os.path.exists(cover_path):
        # 如果没有封面，尝试从帧目录中生成一个
        frames_dir = os.path.join(video_dir, "frames")
        if os.path.isdir(frames_dir):
            cover_path = extract_cover(frames_dir, video_dir)
        if not cover_path or not os.path.exists(cover_path):
            return jsonify({"error": "封面不存在"}), 404

    return send_from_directory(
        video_dir, "cover.jpg",
        mimetype="image/jpeg"
    )


@app.route("/api/analyze/<video_id>", methods=["POST"])
def analyze_video(video_id):
    """AI 分析视频（从已有提取数据出发）"""
    result_path = os.path.join(OUTPUT_DIR, video_id, "result.json")
    if not os.path.exists(result_path):
        return jsonify({"error": "视频数据不存在，请先上传"}), 404

    with open(result_path, "r", encoding="utf-8") as f:
        result = json.load(f)

    if result.get("status") not in ("extracted", "asr_done", "vl_done", "analyzed"):
        return jsonify({"error": f"视频状态不适合分析: {result.get('status')}"}), 400

    # 创建进度队列
    task_queues[video_id] = queue.Queue()

    video_path = result.get("video_path", "")
    video_name = result.get("video_name", "")
    video_dir = os.path.join(OUTPUT_DIR, video_id)
    frames_dir = os.path.join(video_dir, "frames")

    # 启动分析线程
    def run_analysis():
        q = task_queues.get(video_id)
        if not q:
            return

        def progress(step, current, total, message):
            try:
                q.put(json.dumps({
                    "step": step, "current": current, "total": total, "message": message,
                }, ensure_ascii=False))
            except Exception:
                pass

        try:
            # 如果还没有 VL 结果，执行画面分析
            if not result.get("frame_analysis") or not result["frame_analysis"].get("analyzed"):
                progress("vl", 0, 1, "正在分析关键帧画面...")
                vl_result = analyze_frames(
                    frames_dir, video_dir, max_frames=10,
                    progress_callback=lambda c, t, m: progress("vl", c, t, m)
                )
                result["frame_analysis"] = vl_result
                result["status"] = "vl_done"
                _save_result(video_dir, result)
            else:
                progress("vl", 1, 1, "画面分析已有结果，跳过")

            # 如果还没有综合分析结果，执行分析
            if not result.get("content_analysis") or not result["content_analysis"].get("analyzed"):
                progress("analysis", 0, 1, "正在进行综合 AI 分析...")
                ad_data = result.get("ad_data")
                analysis_result = analyze_content(
                    result.get("video_info", {}),
                    result.get("frame_analysis", {}),
                    result.get("transcript", {}),
                    video_dir, ad_data=ad_data,
                    progress_callback=lambda c, t, m: progress("analysis", c, t, m)
                )
                result["content_analysis"] = analysis_result

                if analysis_result.get("analyzed") and analysis_result.get("scoring"):
                    scoring = analysis_result["scoring"]
                    overall_score = scoring.get("overall", {}).get("score", 0)
                    result["scoring"] = {
                        "overall": overall_score,
                        "details": {
                            k: v.get("score", 0) for k, v in scoring.items() if isinstance(v, dict) and "score" in v
                        },
                    }

                if analysis_result.get("analyzed") and analysis_result.get("tags"):
                    result["tags"] = analysis_result["tags"]

            result["status"] = "analyzed"
            result["analyzed_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            _save_result(video_dir, result)
            task_results[video_id] = result
            progress("done", 1, 1, "分析完成！")

        except Exception as e:
            result["status"] = "error"
            result["error"] = str(e)
            _save_result(video_dir, result)
            progress("error", 0, 1, f"分析出错: {str(e)}")

    thread = threading.Thread(target=run_analysis, daemon=True)
    thread.start()

    return jsonify({"status": "started", "message": "分析已启动"})


@app.route("/api/export/word/<video_id>")
def export_word(video_id):
    """导出 Word 分析报告"""
    result_path = os.path.join(OUTPUT_DIR, video_id, "result.json")
    if not os.path.exists(result_path):
        return jsonify({"error": "视频数据不存在"}), 404

    with open(result_path, "r", encoding="utf-8") as f:
        result = json.load(f)

    if result.get("status") != "analyzed":
        return jsonify({"error": "视频尚未完成分析，无法导出"}), 400

    try:
        video_dir = os.path.join(OUTPUT_DIR, video_id)
        video_name = os.path.splitext(result.get("video_name", "report"))[0]
        output_path = os.path.join(video_dir, f"{video_name}_分析报告.docx")
        generate_word_report(result, output_path)

        return send_from_directory(
            video_dir,
            f"{video_name}_分析报告.docx",
            as_attachment=True,
            download_name=f"{video_name}_分析报告.docx",
        )
    except Exception as e:
        return jsonify({"error": f"生成Word报告失败: {str(e)}"}), 500


@app.route("/api/export/pdf/<video_id>")
def export_pdf(video_id):
    """导出 PDF 分析报告（带数据可视化）"""
    result_path = os.path.join(OUTPUT_DIR, video_id, "result.json")
    if not os.path.exists(result_path):
        return jsonify({"error": "视频数据不存在"}), 404

    with open(result_path, "r", encoding="utf-8") as f:
        result = json.load(f)

    if result.get("status") != "analyzed":
        return jsonify({"error": "视频尚未完成分析，无法导出"}), 400

    try:
        video_dir = os.path.join(OUTPUT_DIR, video_id)
        video_name = os.path.splitext(result.get("video_name", "report"))[0]
        output_path = os.path.join(video_dir, f"{video_name}_分析报告.pdf")
        generate_pdf_report(result, output_path)

        return send_from_directory(
            video_dir,
            f"{video_name}_分析报告.pdf",
            as_attachment=True,
            download_name=f"{video_name}_分析报告.pdf",
        )
    except Exception as e:
        return jsonify({"error": f"生成PDF报告失败: {str(e)}"}), 500


@app.route("/api/video/<video_id>", methods=["DELETE"])
def delete_video(video_id):
    """删除视频及其所有数据"""
    video_dir = os.path.join(OUTPUT_DIR, video_id)

    if not os.path.exists(video_dir):
        return jsonify({"error": "视频不存在"}), 404

    try:
        # 清理内存缓存
        if video_id in task_queues:
            del task_queues[video_id]
        if video_id in task_results:
            del task_results[video_id]

        shutil.rmtree(video_dir)
        return jsonify({"status": "ok", "message": "视频已删除"})

    except Exception as e:
        return jsonify({"error": f"删除失败: {str(e)}"}), 500


@app.route("/api/video/<video_id>/ad-data", methods=["POST"])
def save_ad_data(video_id):
    """保存视频的投放数据"""
    result_path = os.path.join(OUTPUT_DIR, video_id, "result.json")
    if not os.path.exists(result_path):
        return jsonify({"error": "视频数据不存在"}), 404

    data = request.json
    if not data:
        return jsonify({"error": "无数据"}), 400

    ad_data = {}
    for field in ("cost", "impressions", "clicks", "ctr", "conversions", "cpa", "cvr", "completion_rate"):
        if field in data and str(data[field]).strip() != "":
            try:
                ad_data[field] = float(data[field])
            except (ValueError, TypeError):
                return jsonify({"error": f"{field} 格式错误，请输入数字"}), 400

    with open(result_path, "r", encoding="utf-8") as f:
        result = json.load(f)

    result["ad_data"] = ad_data
    _save_result(os.path.join(OUTPUT_DIR, video_id), result)

    return jsonify({"status": "ok", "message": "投放数据已保存"})


@app.route("/api/video/<video_id>/tags", methods=["POST"])
def update_tags(video_id):
    """手动更新视频标签"""
    result_path = os.path.join(OUTPUT_DIR, video_id, "result.json")
    if not os.path.exists(result_path):
        return jsonify({"error": "视频数据不存在"}), 404

    data = request.json
    if not data:
        return jsonify({"error": "无数据"}), 400

    tags = {
        "category": str(data.get("category", "")).strip(),
        "style": str(data.get("style", "")).strip(),
        "tags": data.get("tags", []) if isinstance(data.get("tags"), list) else [str(t).strip() for t in data.get("tags", [])],
    }

    with open(result_path, "r", encoding="utf-8") as f:
        result = json.load(f)

    result["tags"] = tags
    _save_result(os.path.join(OUTPUT_DIR, video_id), result)

    return jsonify({"status": "ok", "message": "标签已更新"})


@app.route("/api/video/<video_id>/breakdown", methods=["POST"])
def generate_video_breakdown(video_id):
    """生成内容拆解报告"""
    result_path = os.path.join(OUTPUT_DIR, video_id, "result.json")
    if not os.path.exists(result_path):
        return jsonify({"error": "视频数据不存在"}), 404

    with open(result_path, "r", encoding="utf-8") as f:
        result = json.load(f)

    # 检查是否有分析结果
    if result.get("status") != "analyzed":
        return jsonify({"error": "视频尚未完成分析，无法生成拆解报告"}), 400

    video_dir = os.path.join(OUTPUT_DIR, video_id)

    # 如果已有拆解报告，直接返回
    breakdown_path = os.path.join(video_dir, "breakdown.json")
    if os.path.exists(breakdown_path):
        with open(breakdown_path, "r", encoding="utf-8") as f:
            breakdown = json.load(f)
        return jsonify({"breakdown": breakdown.get("breakdown", {}), "cached": True})

    # 生成拆解报告
    try:
        breakdown = generate_breakdown(result, video_dir)
        if breakdown and breakdown.get("analyzed"):
            # 保存到 result.json
            result["breakdown"] = breakdown.get("breakdown", {})
            _save_result(video_dir, result)
            return jsonify({"breakdown": breakdown.get("breakdown", {}), "cached": False})
        else:
            return jsonify({"error": "生成拆解报告失败，DeepSeek 返回异常"}), 500
    except Exception as e:
        return jsonify({"error": f"生成拆解报告出错: {str(e)}"}), 500


@app.route("/api/video/<video_id>/breakdown", methods=["GET"])
def get_video_breakdown(video_id):
    """获取已生成的内容拆解报告"""
    result_path = os.path.join(OUTPUT_DIR, video_id, "result.json")
    if not os.path.exists(result_path):
        return jsonify({"error": "视频数据不存在"}), 404

    with open(result_path, "r", encoding="utf-8") as f:
        result = json.load(f)

    # 优先从 result.json 中读取
    if result.get("breakdown"):
        return jsonify({"breakdown": result["breakdown"], "cached": True})

    # 尝试从 breakdown.json 读取
    breakdown_path = os.path.join(OUTPUT_DIR, video_id, "breakdown.json")
    if os.path.exists(breakdown_path):
        with open(breakdown_path, "r", encoding="utf-8") as f:
            breakdown = json.load(f)
        return jsonify({"breakdown": breakdown.get("breakdown", {}), "cached": True})

    return jsonify({"error": "尚未生成拆解报告，请先点击生成"}), 404


@app.route("/api/video/<video_id>/script-variants", methods=["POST"])
def generate_video_script_variants(video_id):
    """生成脚本裂变方案"""
    result_path = os.path.join(OUTPUT_DIR, video_id, "result.json")
    if not os.path.exists(result_path):
        return jsonify({"error": "视频数据不存在"}), 404

    with open(result_path, "r", encoding="utf-8") as f:
        result = json.load(f)

    if result.get("status") != "analyzed":
        return jsonify({"error": "视频尚未完成分析，无法生成脚本裂变"}), 400

    data = request.get_json(silent=True) or {}
    user_variables = data.get("variables") if isinstance(data.get("variables"), dict) else None
    review_rule_id = str(data.get("review_rule_id", "")).strip()
    review_rules = _read_review_rule(review_rule_id)
    force = bool(data.get("force") or user_variables)

    video_dir = os.path.join(OUTPUT_DIR, video_id)
    variants_path = os.path.join(video_dir, "script_variants.json")
    if os.path.exists(variants_path) and not force:
        with open(variants_path, "r", encoding="utf-8") as f:
            variants = json.load(f)
        return jsonify({"script_variants": variants.get("script_variants", {}), "cached": True})

    try:
        variants = generate_script_variants(
            result,
            video_dir,
            user_variables=user_variables,
            review_rules=review_rules,
            review_rule_id=review_rule_id,
        )
        if variants and variants.get("analyzed"):
            result["script_variants"] = variants.get("script_variants", {})
            _save_result(video_dir, result)
            return jsonify({"script_variants": variants.get("script_variants", {}), "cached": False})
        return jsonify({"error": "生成脚本裂变失败，DeepSeek 返回异常"}), 500
    except Exception as e:
        return jsonify({"error": f"生成脚本裂变出错: {str(e)}"}), 500


@app.route("/api/video/<video_id>/script-variants", methods=["GET"])
def get_video_script_variants(video_id):
    """获取已生成的脚本裂变方案"""
    result_path = os.path.join(OUTPUT_DIR, video_id, "result.json")
    if not os.path.exists(result_path):
        return jsonify({"error": "视频数据不存在"}), 404

    with open(result_path, "r", encoding="utf-8") as f:
        result = json.load(f)

    if result.get("script_variants"):
        return jsonify({"script_variants": result["script_variants"], "cached": True})

    variants_path = os.path.join(OUTPUT_DIR, video_id, "script_variants.json")
    if os.path.exists(variants_path):
        with open(variants_path, "r", encoding="utf-8") as f:
            variants = json.load(f)
        return jsonify({"script_variants": variants.get("script_variants", {}), "cached": True})

    return jsonify({"error": "尚未生成脚本裂变，请先点击生成"}), 404


@app.route("/api/upload/batch", methods=["POST"])
def upload_videos_batch():
    """批量上传视频文件并启动处理"""
    if "files" not in request.files:
        return jsonify({"error": "未选择文件"}), 400

    files = request.files.getlist("files")
    if not files or files[0].filename == "":
        return jsonify({"error": "文件列表为空"}), 400

    allowed_exts = {".mp4", ".mov", ".avi", ".mkv"}
    video_ids = []
    errors = []

    for file in files:
        if not file.filename:
            continue
        ext = os.path.splitext(file.filename)[1].lower()
        if ext not in allowed_exts:
            errors.append(f"{file.filename}: 不支持 {ext} 格式")
            continue

        video_id = str(uuid.uuid4())[:8]
        video_dir = os.path.join(OUTPUT_DIR, video_id)
        os.makedirs(video_dir, exist_ok=True)

        safe_name = os.path.splitext(file.filename)[0]
        video_path = os.path.join(video_dir, f"{safe_name}{ext}")
        file.save(video_path)

        task_queues[video_id] = queue.Queue()
        thread = threading.Thread(
            target=process_video,
            args=(video_id, video_path, file.filename),
            daemon=True
        )
        thread.start()

        video_ids.append({"video_id": video_id, "video_name": file.filename})

    return jsonify({
        "video_ids": video_ids,
        "errors": errors,
        "message": f"成功上传 {len(video_ids)} 个视频"
    })


@app.route("/api/history")
def get_history():
    """获取分析历史"""
    history = []
    if os.path.exists(OUTPUT_DIR):
        for vid in os.listdir(OUTPUT_DIR):
            video_dir = os.path.join(OUTPUT_DIR, vid)
            if os.path.isdir(video_dir):
                result_file = os.path.join(video_dir, "result.json")
                if os.path.exists(result_file):
                    with open(result_file, "r", encoding="utf-8") as f:
                        result = json.load(f)
                    history.append(result)
    history.sort(key=lambda x: x.get("analyzed_at", ""), reverse=True)
    return jsonify({"history": history})


# ========== 启动 ==========
if __name__ == "__main__":
    print("=" * 50)
    PORT = int(os.environ.get("PORT", "5001"))
    print("  信息流素材分析 Agent")
    print(f"  http://localhost:{PORT}")
    print("=" * 50)

    init_api_keys()

    if DEEPSEEK_API_KEY:
        print(f"  [OK] DeepSeek API Key: {DEEPSEEK_API_KEY[:8]}...")
    else:
        print("  [!!] DeepSeek API Key 未配置")

    if QWEN_API_KEY:
        print(f"  [OK] 通义千问 API Key: {QWEN_API_KEY[:8]}...")
    else:
        print("  [!!] 通义千问 API Key 未配置")

    print("=" * 50)

    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)
