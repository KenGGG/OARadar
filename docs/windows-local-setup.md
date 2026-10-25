# Windows 本机调试记录

以下说明原生 Windows 上的本地运行与只读 OA 采集。示例项目目录为 `D:\Projects\OARadar`，可替换为自己的路径。

## 已安装和配置

- 项目 `.venv` 使用 Python 3.12.14；安装了应用及开发依赖。
- `.bootstrap\Scripts\uv.exe` 为项目内 uv，无需修改系统 PATH。
- 本机 Node.js 24.21.0、npm 11.19.0 和 Google Chrome 已可用。
- `config.yaml` 已设置 Windows Chrome 路径，状态和缓存位于 `runtime/`，原件和 Markdown 位于 `data/`。
- OA 地址由项目根目录 `.env` 的 `OA_BASE_URL` 读取；`.env.example` 提供模板。请在本机填写真实地址、登录和列表路径，再进行浏览器登录。不要将密码写入 YAML。
- MinerU、Ollama、飞书通知保持示例配置中的关闭状态。

## 本次代码适配

- CLI 使用跨平台非阻塞进程锁；Windows 使用 `msvcrt`，Linux 保留 `flock`。
- Windows 显式依赖 tzdata；SecretStorage 仅在 Linux 安装。
- 相对路径校验拒绝 Windows 根路径。
- 测试隔离兼容 Windows 的 USERPROFILE，避免不同测试共用默认数据库。

## 启动

在项目目录的 PowerShell 中分别运行以下命令（每个进程一个终端）：

```powershell
.\scripts\run-windows.ps1 web
.\scripts\run-windows.ps1 worker
.\scripts\run-windows.ps1 markdown-worker
```

访问 http://127.0.0.1:2567 。诊断和状态：

```powershell
.\scripts\run-windows.ps1 doctor
.\scripts\run-windows.ps1 status
```

本次调试已启动三个后台进程。日志和 PID 文件在 `runtime/`。启动新的进程前先检查这些 PID 对应的进程仍属于本项目，避免重复启动。

## 验证结果

- 数据库初始化及迁移到 0041 成功，SQLite 完整性检查通过。
- 两个 Worker 的 `--once` 运行成功。
- WebUI 类型检查和生产构建通过。
- 浏览器实际加载控制台，HTTP 200，无 JavaScript 页面错误。
- 进程锁、配置、Web 安全、Worker、Markdown Worker、V2 合同相关测试：79 passed。
- doctor 必需项通过；MinerU 和本地模型因未启用而显示 FAIL（均为可选项）。

## 仍需处理的 Ubuntu 差异

1. `scripts/*.sh` 和 systemd 服务/定时器不能在原生 Windows 使用。本次未配置开机启动及每小时/夜间扫描；需要 Windows 任务计划程序适配，或在 WSL2 Ubuntu 内部署。
2. 历史批次发现和部分 MinerU 语义/候选解析仍使用 SIGALRM/setitimer。原生 Windows 不支持这些路径，本次没有改写其硬超时机制。完整使用这些功能需继续适配，或使用 Ubuntu/WSL。
3. 账号密码由专用 Chrome 浏览器配置保存。应用已停用 Windows 凭据管理器保存入口；验证码或额外认证仍需人工完成。
4. OCR 使用独立 `.mineru` 环境中的 MinerU 4.0.11，本机部署无需 Docker；见下文。

## MinerU 4.0.11 本地部署

本机可在 `mineru` 配置中设置 `on_demand: true`，并设置
`startup_timeout_seconds: 180`。此模式仅在实际解析期间启动 `.mineru`
中的服务，解析完成、失败或超时后结束服务进程树，释放内存和显存。
每份文件会有模型冷启动开销；状态探测和版本查询不会加载模型。
启用前须退出手工运行的 `run-mineru.ps1` 服务，并在更新后重启应用工作进程。
Docker 或外部管理的服务继续使用 `on_demand: false`。

OARadar 使用 V1 上传、异步任务与 ZIP 结果接口。旧版 `/file_parse` 服务不再适用。
安装环境、模型缓存、上传文件和服务日志均留在本地，且不进入 Git。

```powershell
$env:UV_CACHE_DIR = Join-Path $PWD '.uv-cache'
$env:UV_PYTHON_INSTALL_DIR = Join-Path $PWD '.bootstrap/python'
.bootstrap/Scripts/uv.exe venv .mineru --python .venv/Scripts/python.exe
.bootstrap/Scripts/uv.exe pip install --python .mineru/Scripts/python.exe 'mineru==4.0.11'
$env:MINERU_HOME = Join-Path $PWD 'data/mineru'
$env:MODELSCOPE_CACHE = Join-Path $PWD 'data/mineru/modelscope'
.mineru/Scripts/python.exe -m mineru.kit.main models download --tier standard --source modelscope
.\scripts\run-mineru.ps1
```

服务仅监听 `127.0.0.1:58000`，单任务并发，使用已下载的本地模型。
默认安装的小模型使用 ONNX CPU，VLM 使用 llama.cpp Vulkan；运行时无需上传文档至云端。
`standard` 用于正式 OCR；`flash` 用于原生文本快速解析，不能代替扫描件 OCR 验收。

在 `config.yaml` 的 `mineru` 节中设置 `enabled: true`、`tier: standard` 和
`api_url: http://127.0.0.1:58000`。配置修改后重新启动 OARadar 的 Web 与解析 worker。
健康接口为 `http://127.0.0.1:58000/v1/health`。启动脚本以前台方式运行，关闭窗口即停止服务。
如果已运行后台实例，需先停止该实例，再启动脚本以免端口冲突。

V1 ZIP 包保留 `markdown.md`、`middle_json.json`、`structured_content.json` 及图片素材；
PDF 通常另有 `model_output.json`。4.0 的结构化内容替代旧版 content list 输出。
Docker 配置提供相同版本的独立部署方式；须先在映射的 `data/mineru` 下准备模型，
跨 Windows/Linux 使用时建议在对应平台下载模型，不复制含绝对路径的旧配置。
5. 可选待办模型摘要需要本机 Ollama 与 qwen3.5:9b；本次未安装或验证。
6. 旧 `.doc` 的 antiword 外部工具尚未验证；基础 Office/PDF Python 解析依赖已安装。

本次未运行全量测试；部分现有浏览器测试硬编码 `/usr/bin/google-chrome`，Shell/systemd 测试亦需要 Linux。

首次克隆遇到 GitHub TLS 吊销查询不可达，使用仅对该次命令生效的 `http.schannelCheckRevoke=false` 成功下载。未修改全局 Git 配置，HTTPS 证书验证仍开启。

## 已办全量下载恢复

“已办资料”页现在提供独立的全量清单同步与原件下载控制：

1. 在 `.env` 填写 OA_BASE_URL（只包含协议、主机及端口），按需修改应用、登录、列表路径。点击“打开浏览器登录”，在 OA 页面输入账号密码并接受 Chrome 的保存密码提示。配置文件和数据库均不保存明文密码。
2. 点击“同步全量清单”。每次自动从 OA 读取当前总数，没有固定数量或人工预计值；采集标题、发起时间、处理时间、发起人和文本事项 ID。逐页保存，未完整核对前禁止全量原件下载。
3. 查看标题排除建议的实际命中数和示例。规则只有点击保存后才生效；被排除事项仍保留在清单中。系统设置可修改、停用规则。
4. 点击“下载全部未完成原件”。暂停后在当前页面或事项保存完毕时停止；继续沿用原任务，复用已验证文件。失败项可继续重试，单个附件内部不按字节续传。
5. CSV 导出包含发起和处理时间；页面表格亦显示这些字段。源字段缺失时保留为空并显示数量。

清单按页保存断点，续传前检查源总数、首页面和断点前一页；源发生变化时重扫清单，不删除已保存原件。扫描和下载中的会话失效会尝试重新登录，失败后保存断点并显示需要登录。

相关测试覆盖清单中断恢复、重复项核对、源边界变化、自动重新登录、下载暂停、排除后保留清单、CSRF 和凭据不回显。旧的系统凭据保存接口已停用。

测试和浏览器验收使用合成数据；真实 OA 地址、登录状态、下载内容和运行记录仅保留在本地，不纳入仓库。

OA 连接表单已移除，网页默认只显示清单同步和原件下载两行控制；登录及标题排除规则折叠显示。`.env` 已被 Git 忽略，只有不含真实地址的 `.env.example` 可纳入仓库。编辑 `.env` 后刷新网页即可用于下一次采集操作，无需重启服务。OA 连接配置优先读取 `.env` 当前内容，未在文件中定义的变量才从系统环境变量读取，避免依赖导入时缓存旧值。

## 桌面登录与自动登录凭据

当前本机控制台地址为 `http://127.0.0.1:2568`。点击“打开浏览器登录”，在 OA 页面输入账号密码并由 Chrome 保存。人工登录和自动同步使用同一个固定 user_data_dir，并须由同一个 Windows 用户启动。应用不再使用 Windows 凭据管理器保存或读取账号密码。

出现“发生个人资料错误”时，先检查偏好文件权限及启动账户，不要删除包含密码的旧配置。无法修复时保留旧目录并切换到新的固定配置目录，新目录需重新在浏览器保存密码。本机现选择 `browser-profile-desktop`，原目录保留。

在平时使用应用的 Windows 用户下执行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\restart-browser-login.ps1
```

脚本在无活动任务时重启 Web 与每日调度，并移除该 OA 来源的旧 OARadar 系统凭据条目；不删除 Chrome 配置或已归档文件。不要用其他 Windows 用户或自动化沙箱账户完成首次密码保存。

`desktop_collection` 在交互用户桌面处理人工登录，扫描与下载复用会话。普通定时采集默认无界面；人工登录开启的窗口在本批下载结束后关闭。数据库繁忙会重试，不因此关闭登录浏览器。登录 Cookie 缓存通过 Windows DPAPI 加密并限定 OA 来源。

新增交付使用 `local_delivery_worker`，与旧后台消费者隔离，执行附件清点、解析、发布、来源分类和完成核验。解析任务锁防止并发处理同一任务。`scripts/repair-markdown.py` 仅补转缺失附件；不支持格式和实际失败写入本地复核队列，保留原件。

## Windows 每日已办自动更新

默认本地时间为 `02:00`。使用实际登录并保存 OA 凭据的 Windows 用户运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/install-daily-done.ps1
```

这会注册 `OARadar-DailyDone` 任务，具有每日和用户登录两个触发器，使用普通交互用户权限、隐藏窗口运行；不会将密码写入任务参数。电脑关闭或用户注销期间不执行，登录后补跑。任务设为禁止重复实例、失败后重启；协调器与操作 worker 各有独立进程锁。

安装脚本用 `oa daily-done --enable` 写入本机 `daily-done-settings.json` 开关，兼容仍在运行、尚未重启的旧版 worker，不必修改其严格校验的 YAML 结构。`oa daily-done --disable --config config.yaml` 停用每日扫描；协调器会读取更新后的开关。新部署也可在配置文件的 `daily_done` 中设置时间与轮询间隔，本机开关优先决定是否启用。

协调器先同步并核对完整已办清单，再执行现有下载筛选和未完成附件下载，最后只将本次新增事项／新增附件移交给 `markdown_delivery` 队列。成功下载的事项即使同批其他附件失败也会进入转换；旧存量缺失 Markdown 不会因此整体重复入队。分类使用当前启用的来源方案，全部处理均在本机完成。

登录失效时保留清单页码或下载断点，显示“等待 OA 登录”，不会循环尝试密码。完成本机登录检查后自动继续原任务；人工暂停的任务保持暂停。`runtime` 下的 `daily-done.json` 记录同步阶段、任务引用和下一次时间，总览页直接读取该状态。转换入队表示已移交处理，不能当作附件全部转换成功。
