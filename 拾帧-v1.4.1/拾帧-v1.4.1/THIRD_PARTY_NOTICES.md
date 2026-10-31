# 第三方组件与许可说明

本项目自编写的 Python 服务、界面、任务调度、下载流程和启动脚本采用 **GPL-3.0-or-later**。完整许可正文见 [LICENSE](LICENSE)。源码包不附带 `.venv`、FFmpeg / ffprobe / Node.js 二进制文件，也不将下列项目的代码复制到自编写模块中。第三方组件由本机安装或通过包管理器单独安装，仍适用各自许可。

## 使用方式与上游来源

| 组件 | 本项目中的用途 | 上游许可与来源 |
| --- | --- | --- |
| Python | 运行本地服务、下载逻辑和界面接口 | [Python 软件许可](https://docs.python.org/3/license.html)；由用户自行安装 |
| FFmpeg / ffprobe | 独立进程探测媒体及编码；使用 libx264 或 h264_nvenc，以及 FFmpeg 原生 AAC | 实际许可取决于构建选项。FFmpeg 本体采用 LGPL，启用 GPL 组件后构建适用 GPL；参阅 [FFmpeg 许可说明](https://ffmpeg.org/legal.html)、[官方源码与下载入口](https://ffmpeg.org/download.html) |
| x264 / libx264 | H.264 视频编码，由 FFmpeg 调用 | [x264 官方项目与 GPL 说明](https://www.videolan.org/developers/x264.html) |
| yt-dlp | 可选的网站解析适配器，Python 包独立安装 | [项目及使用说明](https://github.com/yt-dlp/yt-dlp)、[Unlicense 正文](https://github.com/yt-dlp/yt-dlp/blob/master/LICENSE) |
| Node.js | 可选的 JavaScript 运行时，帮助 yt-dlp 执行 EJS 网站解析组件 | [Node.js 许可及第三方通知](https://github.com/nodejs/node/blob/main/LICENSE)；本项目不附带其二进制文件 |
| Playwright | 可选的独立浏览器控制，使用本机 Chrome / Edge | [Playwright Python](https://github.com/microsoft/playwright-python)、[Apache-2.0 许可](https://github.com/microsoft/playwright-python/blob/main/LICENSE)；包单独安装，不复制其实现 |

## 本机网站增强依赖快照

以下版本在交付验证时通过 PyPI 安装到本项目独立 `.venv`，版本及许可取自安装包元数据。基础 `requirements.txt` 仅安装 yt-dlp；`python tools/bootstrap.py --with-ejs` 安装增强依赖。该列表是本次验证快照，之后更新依赖时应重新核对。

| 包 | 验证版本 | 包声明的许可 | 上游 |
| --- | --- | --- | --- |
| yt-dlp | 2026.8.19 | Unlicense | [yt-dlp](https://github.com/yt-dlp/yt-dlp) |
| yt-dlp-ejs | 0.8.0 | Unlicense AND MIT AND ISC | [EJS](https://github.com/yt-dlp/ejs) |
| brotli | 1.2.0 | MIT | [Brotli](https://github.com/google/brotli) |
| certifi | 2026.7.22 | MPL-2.0 | [certifi](https://github.com/certifi/python-certifi) |
| mutagen | 1.48.1 | GPL-2.0-or-later | [Mutagen](https://github.com/quodlibet/mutagen) |
| pycryptodomex | 3.23.0 | BSD 与 Public Domain；以随包许可为准 | [PyCryptodome](https://github.com/Legrandin/pycryptodome) |
| requests | 2.34.2 | Apache-2.0 | [Requests](https://github.com/psf/requests) |
| urllib3 | 2.7.0 | MIT | [urllib3](https://github.com/urllib3/urllib3) |
| websockets | 17.1 | BSD-3-Clause | [websockets](https://github.com/python-websockets/websockets) |
| charset_normalizer | 3.5.1 | MIT | [charset-normalizer](https://github.com/jawah/charset_normalizer) |
| idna | 3.19 | BSD-3-Clause | [idna](https://github.com/kjd/idna) |
| playwright | 1.62.0 | Apache-2.0 | [Playwright Python](https://github.com/microsoft/playwright-python) |
| greenlet | 3.5.5 | MIT AND PSF-2.0 | [greenlet](https://github.com/python-greenlet/greenlet) |
| pyee | 13.0.1 | MIT | [pyee](https://github.com/jfhbrook/pyee) |
| typing_extensions | 4.16.0 | PSF-2.0 | [typing_extensions](https://github.com/python/typing_extensions) |

浏览器支持通过 `setup.bat --with-browser` 单独安装，可与 `--with-ejs` 合用。1.2.0 的画质档位及插帧复用现有 FFmpeg / libx264 / minterpolate，不新增模型、专有 SDK 或第三方源码副本。

yt-dlp 的 wheel 与预编译可执行版本可能具有不同的附带组件。本项目通过 PyPI 安装 Python wheel，EJS 另行安装；[yt-dlp 官方 EJS 说明](https://github.com/yt-dlp/yt-dlp/wiki/EJS)解释了依赖关系与运行时要求。软件未启用自动下载远程 EJS 代码的选项。

## FFmpeg 本机验证

交付环境复用了计算机已经安装的 FFmpeg / ffprobe，版本为 `n5.1.6-16-g6e63e49496-20250131`。实际运行输出包括 `--enable-gpl`、`--enable-version3`、`--enable-libx264`，不含 `--enable-nonfree`，并且原生 `aac` 编码器可用。该路径只写入本机的 `runtime.local.json`；源码压缩包不包含该配置或该安装目录的任何文件。

更换机器时，从 [FFmpeg 官方下载入口](https://ffmpeg.org/download.html)自行选择包含 `libx264`、原生 `aac` 和 `ffprobe` 的 GPL 构建。本软件通过独立进程使用编码器，不需要专有编码 SDK，不使用 `libfdk_aac`。

## 再分发说明

### 1.4.1 GPU 插帧与编码运行时

本项目新编写的 GPU worker、流式队列、管道、任务调度和网页接口采用 GPL-3.0-or-later。GPU 模型推理调用独立安装的 Practical-RIFE，实现和权重位于本机 `data/rife` 等运行目录，不放入本项目源码包，不将其描述为原创模型。Practical-RIFE 官方仓库标明 MIT，README 同时说明其训练模型链接内容适用相同 MIT 许可。独立安装副本应保留上游版权与许可正文；若后续分发模型，需要核对实际权重来源并随附通知。

来源：[Practical-RIFE 官方项目](https://github.com/hzwer/Practical-RIFE)、[MIT 许可正文](https://github.com/hzwer/Practical-RIFE/blob/main/LICENSE)。本版沿用本机外部 Python / PyTorch / CUDA 环境，源码包不包含它们、NVIDIA 驱动或模型权重。

GPU H.264 使用 FFmpeg 的 `h264_nvenc` 接口调用 NVIDIA 硬件编码器。硬件编码与 CUDA RIFE 插帧是独立能力，不将 NVIDIA 驱动或 SDK 重许可为 GPL；参考 [NVIDIA FFmpeg 集成说明](https://docs.nvidia.com/video-technologies/video-codec-sdk/13.0/ffmpeg-with-nvidia-gpu/index.html)。项目继续分发原创适配代码和文档；包含运行库的安装包需要针对实际分发组件另行核对许可义务。

### 1.3.0 AI 可选运行时

RTX worker 和 ComfyUI HTTP 适配器为本项目原创 GPL-3.0-or-later 源码。独立运行环境及其包、SDK、模型均不放入本源码 ZIP，也不安装到拾帧的 Python 环境。本次使用本机已经存在的安装，不复制第三方节点实现。

| 外部组件 | 本次使用方式 | 来源与许可边界 |
| --- | --- | --- |
| NVIDIA VFX Python 包 nvidia-vfx / nvvfx | 独立 Python 进程调用公开 VideoSuperRes API；本机 0.1.0.1 | [NVIDIA VFX 包说明](https://pypi.org/project/nvidia-vfx/)、[RTX Video SDK 官方入口](https://developer.nvidia.com/rtx-video-sdk)。适用 NVIDIA 自身许可，不将其重许可为 GPL |
| PyTorch、CUDA、Pillow、NumPy | 复用外部 RTX Python 环境；主服务不导入这些包 | 各自软件及随包第三方许可；本源码包不包含它们 |
| ComfyUI | 回环 HTTP 调用基础图像超分节点 | [ComfyUI 官方仓库](https://github.com/Comfy-Org/ComfyUI)；软件与节点、模型许可分别核对 |
| 4x-UltraSharp 模型 | 用户本机已有模型，仅通过其 ComfyUI 使用 | 未向源码包加入权重；本次没有重新核实这份本地权重的完整来源和再分发授权，后续制作含模型安装包前需另行核查 |

独立进程和 HTTP 边界用于隔离依赖，不代表可忽略 NVIDIA SDK 或模型本身的许可。后续打包二进制、模型或提供服务器服务时，需重新核对对应版本条款；当前交付只分发原创适配代码和说明。

分发本项目源码时保留 GPL 许可、原始声明与本文件。若另行制作含第三方二进制或依赖的安装包，应针对**实际装入的版本**一并履行许可证、版权通知及对应源码等义务；本文件和上游首页链接不能替代所需的对应源码。

GPL 是代码许可。网站内容授权、服务条款与特定地区 H.264 / AAC 等技术可能涉及的专利要求是另外的问题，不能由项目采用 GPL 推导为一律免除。FFmpeg 官方提供了相关[许可与专利说明](https://ffmpeg.org/legal.html)。
