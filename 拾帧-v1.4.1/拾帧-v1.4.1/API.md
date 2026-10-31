# 本地接口与扩展约定

服务入口为 `http://127.0.0.1:8765`，仅监听本机回环地址。前端为根目录 `web/` 下的静态文件。接口使用 UTF-8 JSON；错误体为 `{"error": "可读的错误原因"}`。

## 接口

| 方法 / 路径 | 输入 | 返回 |
| --- | --- | --- |
| GET `/api/health` | 无 | 可用性、版本、FFmpeg / FFprobe / H.264 / AAC / yt-dlp、browser、interpolation、gpu_encoding 状态、max_fps=120、会话 token |
| GET `/api/compute` | 无 | gpu_interpolation、gpu_encoding、reason、device、model、default_mode=auto、pipeline=streaming；GPU 能力独立检测 |
| GET `/api/demo` | 无 | 程序生成的示例媒体 URL |
| GET `/api/upscale` | 无 | protocol_version=1、RTX / ComfyUI 可用性及原因、ComfyUI 地址和已安装模型 |
| POST `/api/upscale/configure` | `{"comfyui_url":"http://127.0.0.1:8188","comfyui_model":"4x-UltraSharp.pth"}` | 保存本机配置并检测，返回同上状态；允许保存离线地址 |
| POST `/api/analyze` | `{"url":"https://..."}` | 媒体 id、标题、来源、时长、尺寸、帧率、编码、轨道和可用格式 |
| POST `/api/jobs` | 下述输出设置 | HTTP 202 和新任务记录 |
| GET `/api/jobs` | 无 | `{"jobs":[...]}`，最近 100 个任务 |
| GET `/api/jobs/{id}` | 无 | 单个任务记录 |
| POST `/api/jobs/{id}/cancel` | `{}` | 取消后的任务记录 |
| GET `/api/jobs/{id}/download` | 已完成任务 id | 附件下载，支持单段 HTTP Range 和 HEAD |
| GET `/api/session/douyin` | 无 | 会话是否已导入、有效条目数；不包含 Cookie 内容 |
| POST `/api/session/douyin` | `{"cookies":"Netscape 文本"}` | 域限定、仅内存的会话状态 |
| POST `/api/session/douyin/clear` | `{}` | 清除会话与关联解析缓存后的状态 |
| POST `/api/browser/analyze` | `{"url":"抖音作品地址或分享文本"}` | HTTP 202 和独立浏览器任务 |
| GET `/api/browser/analyze/{id}` | 无 | starting / waiting / completed / failed / cancelled；成功时 media 可创建下载任务 |
| POST `/api/browser/analyze/{id}/cancel` | `{}` | 取消状态 |

所有 POST 必须提供 `Content-Type: application/json` 与 `X-App-Token` 请求头。token 从同源健康接口读取，服务重启后刷新。应用检查 Host 和 Origin，不提供跨域授权。当前接口面向同机个人应用，不是公开网络 API。

## 创建任务

```json
{
  "media_id": "解析返回的媒体 id",
  "kind": "video",
  "container": "mp4",
  "height": null,
  "format_id": "best",
  "video_quality": "high",
  "fps": 120,
  "frame_rate_mode": "interpolate",
  "upscale": "none",
  "processing_mode": "auto"
}
```

`kind` 为 `video`、`audio` 或 `original`。video 容器为 mp4 / avi；audio 容器为 m4a / aac；original 容器跟随来源，传入的 container 被忽略。视频 height 为 null（保持源尺寸）或 360、480、720、1080、1440、2160；超过源尺寸的上限不会放大素材。`format_id` 必须为 best 或该次解析返回的格式 id；前端在原文件保存中提供来源版本选择。

1.2.0 的三个新增字段均可省略：`video_quality` 为 balanced / high / maximum，默认 high；`fps` 为 null（默认、跟随来源）或 1–120 的整数，不接受布尔、字符串或小数；`frame_rate_mode` 为 interpolate（默认）或 fast。audio / original 不应用这些设置，记录为 null。原有请求和已保存任务仍兼容，不要求 SQLite 表迁移。

1.4.1 增加可选 `processing_mode`：auto（默认，GPU 优先）、gpu（仅 GPU）、cpu（CPU 兼容）。仅 video 应用。auto 按实际能力选择 GPU；因能力不足使用 CPU 时，`result.compute_note` 说明原因。gpu 要求 NVENC，以及实际需要插帧时的 RIFE / CUDA；缺少能力明确失败，不静默改成 CPU 或快速转换。cpu 使用 libx264 与需要时的 minterpolate；AI 超分由 `upscale` 独立决定。

视频结果增加 `video_encoder`（h264_nvenc / libx264）、`interpolation_backend`（gpu / cpu / none）、`compute_note`、`interpolation_model`、`interpolation_generated_frames`、`pipeline`、`processing_seconds`、`total_seconds`。`pipeline=streaming` 表示 GPU 插帧画面通过有界队列送入编码器，不产生新增插值 PNG。`processing_seconds` 包含超分及插帧 / 编码处理，不含下载与等待槽位；`total_seconds` 从任务执行器开始运行计时，包含下载、探测与执行期间等待，不含启动执行器之前的排队时间。没有执行模型插值时，不得把复制帧计入 `interpolation_generated_frames`。旧任务没有记录的字段不由界面推造。

视频完成结果另有 `source_fps`、`target_fps`、`effective_frame_rate_mode`（source / fast / interpolate）、`frame_rate_note` 和 `video_quality`；`result.fps` 始终为输出文件实测值。低于或等于源帧率、素材过短或元数据不完整时不执行运动补偿，实际模式和原因由结果说明。缺少插帧运行能力时，实际需要运动补偿的任务明确失败，不悄悄替换为快速模式。已有任务记录缺少新字段时，界面不虚构旧任务使用的设置。

抖音解析结果或错误可带 `normalized_url`、`source_id`、`diagnostics`（strategy / status / message / http_status）。Cookie 文件在解码后最多 256 KiB、3000 条记录，仅保留抖音域有效记录；浏览器解析必须由用户显式创建，登录和验证由用户在独立窗口中完成。浏览器端点不接受外部传入个人资料目录或 headless 参数。

任务状态为 queued、downloading、converting、completed、failed、cancelled。progress 为 0–100 的分阶段进度；音画分离的下载可能分多条流报告。完整结果通过 result 返回实际探测的尺寸、时长、编码、采样率和文件大小；转码结果额外记录 target_audio_bit_rate=256000。路径和适配器内部数据不会由 JSON 接口公开。

## 扩展位置

### AI 提供者协议 v1

`upscale` 为可选字段，默认 none；其他值为 auto / rtx / comfyui，仅在 video 模式生效。启用时 height 必须明确指定。目标不高于来源时执行普通缩小，结果注明未使用 AI。auto 在创建任务时优先选择实际自检成功的 RTX，再选择在线且模型可用的 ComfyUI；任务持有独立配置快照，不受随后修改接口设置影响。提供者执行失败会明确报错，不自动将普通缩放替代 AI。

新增结果字段：upscale_provider、upscale_model、upscale_note、source_width / source_height、upscale_width / upscale_height、upscale_frames。result.width / height / fps 仍来自成品探测。旧任务缺失字段时不填造结果。

主程序只导入 Python 标准库；RTXProcess 通过固定 rtx_worker.py 的行 JSON 协议调用独立 Python 环境，--probe 必须通过 GPU 实际推理才报告 available。--serve 每次请求只接受 input / output / width / height，并返回 ok / provider / model / passes / 实际尺寸；不接受 shell、任意脚本或任意滤镜。worker 源码由本项目编写，NVIDIA 包及模型独立安装。

ComfyClient 仅接受 HTTP 回环地址，无账号、路径、查询参数；禁用代理与重定向。固定上传图像、提交基础超分工作流、轮询本任务历史并取回 PNG，不安装或执行用户上传的任意工作流。调用方向为拾帧主动发起请求；读取响应不构成反向控制。没有回调服务或 ComfyUI→拾帧创建任务入口。

后续新增第三方提供者可实现同样的能力检测、逐帧处理、取消与结构化结果，再在 UpscaleService 的选择层注册。双向接口需单独新增认证、幂等任务标识、事件版本和资源范围；当前不预先开放公网地址、跨域授权、任意工作流或任意本地文件读取。风险清单见 AI_INTEGRATION.md。

### 其他扩展

1. 网站解析：在 `extractors.py` 实现适配器的 `analyze(url)` 和 `download(media, options, folder, progress, cancel)`，注册到 `ExtractorService.providers`，并在服务解析分派中接入。不要向前端暴露内部下载路径或未经筛选的第三方对象。
2. 编码格式：同时修改 `jobs.validate_options` 的允许值、`media.build_encode_command` 的明确参数和界面选项，并检查实际输出轨道。执行参数始终以列表传入子进程，不经过 shell。
3. 任务管理：`JobManager` 统一负责队列、取消事件、SQLite 状态、临时目录及完成文件。适配器应定期检查取消事件并报告进度。
4. 界面：直接编辑 `web/index.html`、`web/app.js` 和 `web/styles.css`，刷新浏览器即可；无需前端依赖安装和构建。


## GPU 长视频插帧（2026-10-07）

`GET /api/health` 新增 `max_gpu_interpolation_seconds=10800`、`max_gpu_source_frames=1296000`、`max_gpu_output_frames=1296000`；版本保持 1.4.1，`max_fps=120`。

GPU worker 使用包内 `times_file` / `source_count` / `duration` 固定协议，通过当前任务目录的 float64 时间戳文件读取来源 PTS。这些字段属于内部任务文件，不是 HTTP 用户可提交参数。旧的短时间戳列表协议保留供兼容测试使用，仍校验时长和帧数。

明确请求升帧、GPU 优先/仅 GPU、且已知来源超过三小时时，创建任务即返回 `gpu_interpolation_duration_limit`，避免下载后才失败；下载后仍按实际探测重新校验。快速转换、保持/降低帧率、原文件、音频导出保持原有行为。
