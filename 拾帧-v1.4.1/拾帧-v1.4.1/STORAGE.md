# 拾帧 1.4.1 本地存储（8773）

2026-10-01 起，项目根目录为 `E:\拾帧\拾帧1.3`。本程序管理的数据、成品、临时文件和可重定向的工具缓存都在这个根目录内。

| 用途 | 根目录下的位置 |
|---|---|
| 原任务成品 | `data\downloads\<任务编号>\` |
| 网页「保存文件」副本 | `data\downloads\browser\` |
| 下载、编码、逐帧超分的工作文件 | `data\tmp\<任务编号>\` |
| Python、浏览器解析的系统临时文件 | `data\tmp\runtime\` |
| GPU、模型和安装工具可控缓存 | `data\cache\` |
| 拾帧专用网页浏览器配置 | `data\browser\ui\` |
| 任务历史 | `data\jobs.sqlite3` |
| 示例素材 | `data\demo\` |
| 已保留的中断工作文件 | `data\recovery\` |

双击 `start.bat` 启动服务及专用 Edge 窗口。服务已启动时，双击 `打开网页-E盘缓存.bat` 只打开专用窗口。`打开网页-Edge.bat` 也已更新为同一入口。专用窗口中的「保存文件」默认保存到上述 browser 目录；无需再把成品下载到 Windows 的 C 盘 Downloads。

如果继续用自己日常的 Chrome/Edge 标签页访问地址，那个浏览器仍按其自身配置选择缓存和下载位置。请使用上述专用入口。程序不改动日常浏览器的配置、历史、下载目录和系统用户文件夹。

启动入口在环境探测之前覆盖 TEMP、TMP、TMPDIR。子进程继承项目专用的 CUDA_CACHE_PATH、TORCH_HOME、TORCH_EXTENSIONS_DIR、TORCHINDUCTOR_CACHE_DIR、TRITON_CACHE_DIR、Hugging Face、pip、Playwright 等缓存变量。安装入口也应用相同设置，不修改其他 AI 软件的环境。生产启动忽略旧的 MEDIA_DATA_DIR 外部覆盖；库测试通过显式 data_dir 参数隔离数据。

接口 `GET /api/health` 的 storage 字段返回当前实际路径；网页「本地运行环境」也显示成品、缓存与临时位置。数据目录指向 C 盘或目录符号链接逃出受控数据根时，会拒绝运行而不会静默回落到 C 盘。

边界：C 盘上已安装的 Python、FFmpeg、Edge 仍可被只读调用。Windows、浏览器更新组件以及 NVIDIA 驱动的共享 DXCache/NGX 数据不由本程序控制；当前 NVVFX 没有公开的 NGX 缓存重定向参数。本次没有修改系统级驱动路径或全局 APPDATA/LOCALAPPDATA，也不承诺整个操作系统零 C 盘写入。

本次迁移与测试记录在 `deployment\storage-migration-20261001\`。重启前发现原 8773 服务和编码进程已停止，保留了任务 `dca9d0a886464494af552b0e8fb265f0` 的 source.mkv（4,223,736,675 字节）和 encoded.mp4（20,878,196,784 字节）到 `data\recovery\dca9d0a886464494af552b0e8fb265f0\`。encoded.mp4 是未完成的工作文件，不视为合格成品；来源文件可供后续重新处理。保留目录不会在普通任务清理中删除。
