# 信息流素材分析 Agent — 项目上下文

> 这是交给 Codex 继续迭代的完整上下文文档。每次进入这个目录时请先读它。

---

## 一、项目概述

一个 **Flask Web 应用**，用于对抖音/快手等信息流广告视频进行 AI 自动分析。

**核心价值**：上传一支广告视频 → 自动抽帧/分离音频/语音转文字/画面分析/DeepSeek 综合评分 → 导出 Word/PDF 报告。

**用户**：吴晓俊（EDY），广告公司 AI 编导，为字节系命理测算客户制作 AI 广告素材，月交付约 60 条视频。

---

## 二、技术栈

| 层 | 技术 |
|---|---|
| Web 框架 | Flask (host=0.0.0.0, port=5001, threaded) |
| 视频处理 | FFmpeg (通过 imageio-ffmpeg 内置二进制) |
| 视觉分析 | 通义千问 VL (qwen-vl-plus, DashScope API) |
| 语音识别 | Qwen3-ASR-Flash (DashScope API, base64 音频上传) |
| 文本分析 | DeepSeek V4 Flash (通过 Anthropic 兼容接口) |
| 报告导出 | python-docx (Word) + fpdf2 (PDF) + matplotlib (图表) |
| Python 环境 | `C:\Users\EDY\.workbuddy\binaries\python\envs\workbuddy\` |

---

## 三、文件结构

```
C:\Users\EDY\WorkBuddy\2026-05-21-task-13\
├── app.py                 # Flask 主应用 (路由/upload/progress/export/tags)
├── processor.py           # 视频处理核心 (ffmpeg/ASR/VL/DeepSeek分析/拆解)
├── report_generator.py    # Word + PDF 报告生成 (含matplotlib图表)
├── generate_covers.py     # 批量补生成历史视频封面图
├── start_server.py        # 启动脚本 (设置 sys.path 后启动 Flask)
├── start_tunnel.bat       # Cloudflare Tunnel 内网穿透脚本
├── config.json            # API Key 存储 (deepseek_api_key + qwen_api_key)
├── agent-design-spec.html # 产品设计需求文档 v0.2
├── CODEX.md               # 本文件
├── templates/
│   └── index.html         # 前端单页 UI
├── uploads/               # 上传视频临时存储
└── output/                # 每个视频的处理结果 (按 video_id 分目录)
    └── {video_id}/
        ├── {视频名}.mp4
        ├── result.json    # 处理结果主文件
        ├── frames/        # 抽帧 JPEG
        ├── audio.wav      # 分离的音频
        ├── cover.jpg      # 封面图
        ├── transcript.json
        ├── frame_analysis.json
        ├── content_analysis.json
        └── breakdown.json
```

---

## 四、7 步处理流水线

每步通过 SSE (`/api/progress/<video_id>`) 实时推送进度给前端：

```
Step 1: 读取视频信息 (ffprobe 解析 duration/分辨率/fps/有无音频)
Step 2: 提取关键帧 (ffmpeg fps=2, 高质量 JPEG)
         → 自动选第3帧做封面图
Step 3: 分离音频 (ffmpeg → 16kHz mono PCM WAV)
Step 4: 语音转文字 (Qwen3-ASR-Flash, base64 data URI, ≤8MB)
Step 5: 画面分析 (Qwen VL, 均匀采样最多10帧, base64 发送)
Step 6: 综合分析 (DeepSeek V4 Flash, 整合前面所有数据)
         → 6维评分 + 跑量归因 + 标签生成
Step 7: 可选——生成内容拆解报告 (再次调用 DeepSeek 做结构化输出)
```

每步完成后自动保存 `result.json`（断点续传，避免重复分析）。

---

## 五、API 路由表

| 路由 | 方法 | 功能 |
|---|---|---|
| `/` | GET | 首页 (templates/index.html) |
| `/api/status` | GET | 检查 API Key 配置状态 |
| `/api/config` | GET/POST | 读取/更新 API Key |
| `/api/upload` | POST | 上传单个视频，立即启动处理 |
| `/api/upload/batch` | POST | 批量上传多个视频 |
| `/api/progress/<video_id>` | GET | SSE 进度流 |
| `/api/result/<video_id>` | GET | 获取完整 result.json |
| `/api/cover/<video_id>` | GET | 获取封面图 JPEG |
| `/api/analyze/<video_id>` | POST | 对已有数据重新跑 AI 分析 |
| `/api/export/word/<video_id>` | GET | 下载 Word 报告 |
| `/api/export/pdf/<video_id>` | GET | 下载 PDF 报告 |
| `/api/video/<video_id>` | DELETE | 删除视频及所有数据 |
| `/api/video/<video_id>/ad-data` | POST | 保存投放数据 (cost/impressions/clicks/ctr等8字段) |
| `/api/video/<video_id>/tags` | POST | 手动更新标签 |
| `/api/video/<video_id>/breakdown` | GET/POST | 获取/生成内容拆解报告 |
| `/api/history` | GET | 获取所有分析历史 |

---

## 六、result.json 数据结构

```json
{
  "video_id": "abc12345",
  "video_name": "广告素材.mp4",
  "video_path": "output/abc12345/广告素材.mp4",
  "status": "analyzed",          // uploaded → frames_extracted → audio_extracted → asr_done → vl_done → analyzed
  "created_at": "2026-06-01 10:00",
  "analyzed_at": "2026-06-01 10:05",
  "video_info": { "duration": 15.5, "width": 720, "height": 1280, "fps": 30, "has_audio": true, "file_size_mb": 8.2 },
  "frames": { "frames_count": 31, "fps": 2 },
  "cover": true,
  "audio": { "audio_extracted": true, "audio_size_kb": 484.5 },
  "transcript": { "transcribed": true, "model": "qwen3-asr-flash", "full_text": "...", "segments": [...] },
  "frame_analysis": { "analyzed": true, "model": "qwen-vl-plus", "overall_visual": "...", "frames": [...] },
  "content_analysis": {
    "analyzed": true,
    "element_breakdown": { "visual": {}, "text": {}, "audio": {}, "structure": {} },
    "scoring": { "hook_strength": {"score":7,"max":10,"comment":"..."}, ... },
    "traffic_attribution": { "ctr_drivers":[], "cvr_drivers":[], "completion_drivers":[], "potential_issues":[], "overall_assessment":"", "improvement_suggestions":[] },
    "tags": { "category":"", "style":"", "duration_segment":"", "target_audience":"", "tags":[] }
  },
  "scoring": { "overall": 7, "details": {...} },
  "tags": { "category": "日用品", "style": "口播", "tags": [...] },
  "ad_data": { "cost": 500, "impressions": 10000, "clicks": 300, "ctr": 3.0, "conversions": 15, "cpa": 33.3, "cvr": 5.0, "completion_rate": 65.0 },
  "breakdown": { "data":{}, "script_content":{}, "risks":[], "improvements":[] }
}
```

---

## 七、已知 Bug & 修复记录

### 1. ✅ ffmpeg 路径获取失败 (已修复 — 2026-06-01)
- **症状**：处理视频时报「读取音频信息失败 / No ffmpeg exe could be found」
- **根因**：`processor.py` 的 `get_ffmpeg_path()` 只捕获 `ImportError`，但 `imageio_ffmpeg.get_ffmpeg_exe()` 可能抛 `RuntimeError`
- **修复**：增加 `RuntimeError` 捕获 + 手动扫描 `imageio_ffmpeg/binaries/` 目录作为回退
- **位置**：`processor.py` 第 17-41 行

### 2. ✅ isinstance(data.get('tags'), []) 错误 (已修复)
- **症状**：标签保存时 500 错误 + HTML 响应
- **根因**：`app.py` 第 592 行用了 `isinstance(data.get('tags'), [])` — Python 中 `[]` 字面量不能用于 isinstance
- **修复**：改为 `isinstance(data.get('tags'), list)`
- **位置**：`app.py` 第 592 行（`/api/video/<video_id>/tags` 路由）

### 3. ✅ matplotlib 中文乱码 (已修复)
- **修复**：PDF 报告使用 `C:\Windows\Fonts\msyh.ttc` (微软雅黑) 作为字体

### 4. ✅ fpdf2 布局偏移 (已修复)
- 已通过调整 PDF 类的 margin 和坐标解决

### 5. Flask 后台进程被杀问题
- **症状**：用 `run_in_background` 方式启动的 Flask 进程可能被意外杀死
- **建议**：用 `Start-Process -WindowStyle Hidden` 或 VBS 脚本做进程持久化
- **当前方案**：手动双击 `start_server.py` 或 `start_tunnel.bat`

---

## 八、配置说明

### config.json
```json
{
  "deepseek_api_key": "sk-5b354b9c3ddc41d9801dfd7556b00131",
  "qwen_api_key": "sk-6a3e42b713764e4e8b48d4f30bee1213"
}
```

- DeepSeek 通过 Anthropic 兼容接口调用 (`https://api.deepseek.com/anthropic/v1/messages`)，模型 `deepseek-v4-flash`
- Qwen 通过 DashScope OpenAI 兼容接口 (`https://dashscope.aliyuncs.com/compatible-mode/v1`)
  - VL 模型: `qwen-vl-plus`
  - ASR 模型: `qwen3-asr-flash`

### Python 环境
```
Python:  C:\Users\EDY\.workbuddy\binaries\python\envs\workbuddy\Scripts\python.exe
依赖:   Flask, imageio_ffmpeg, python-docx, fpdf2, matplotlib, requests
```

---

## 九、启动方式

### 本地开发
```powershell
cd C:\Users\EDY\WorkBuddy\2026-05-21-task-13
C:\Users\EDY\.workbuddy\binaries\python\envs\workbuddy\Scripts\python.exe start_server.py
# 访问 http://localhost:5001
```

### 分享给同事 (Cloudflare Tunnel)
```batch
双击 start_tunnel.bat
# 会自动启动 Flask (如未运行) 并创建 Cloudflare 隧道
# 把生成的 https://xxx.trycloudflare.com 链接发给同事
```

---

## 十、未来迭代方向（用户期望）

按优先级排列：

1. **广告数据字段扩展** — result.json 的 ad_data 目前支持 8 个字段 (cost/impressions/clicks/ctr/conversions/cpa/cvr/completion_rate)，考虑增加更多可选字段
2. **批量分析性能优化** — 目前批量上传后逐个串行处理，可考虑并行
3. **飞书集成** — 用户想接入公司飞书账号（目前仅员工级权限，受限）
4. **部署到 Render** — 研究云部署方案，让服务不依赖本地电脑
5. **前端 UI 改进** — 参考 `agent-design-spec.html` 中的设计稿

---

## 十一、开发约定

- 用户偏好中文回复，自然语气（非 AI 味）
- 直接操作本地文件，不要只给指导
- 修改代码时提供 before/after 对比
- 删除文件前先列清单确认
- 用户是 Python 初学者，学习风格是「模仿→拆解→迭代」
- 偏好实用方案，不追求完美

---

## 十二、Codex 使用方式

进入项目目录后，Codex 会自动读取本文件。常用命令：

```bash
# 启动开发
codex "启动 Flask 服务器"

# 修 Bug
codex "修复 app.py 里 XXX 的问题"

# 加功能
codex "在 processor.py 里加一个新步骤：视频去水印"

# 部署
codex "帮我把这个项目部署到 Render"
```

Codex 已经通过 DeepSeek API 配置好了 (`codex` 命令在两个 Node 版本下都可用)。
