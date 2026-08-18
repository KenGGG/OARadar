# OARadar systemd 用户服务

这些 unit 让 `systemctl --user` 在登录会话中运行 OARadar 的常驻 worker 与
定时扫描。模板文件位于 `scripts/systemd/templates/`，里面用占位符
（`{{PROJECT_ROOT}}`、`{{UV_BIN}}`、`{{CONFIG_PATH}}`、`{{ENV_FILE}}`、
`{{TIMEZONE}}`）表示机器相关路径；安装脚本会把它们替换为本机的绝对路径，
因此仓库里的模板不再写死某一台机器的路径（plan-0805-02 §5）。

## 安装

```bash
./scripts/install-systemd-user.sh \
  --project-root "$PWD" \
  --config "$PWD/config.yaml" \
  --timezone Asia/Shanghai
```

脚本会自动检测：**项目绝对路径、uv 绝对路径、Python 环境、config.yaml、
data_root、systemd 用户目录、环境变量文件**，并据此渲染 6 个 unit 文件到
`~/.config/systemd/user/`，然后 `daemon-reload` 并启用/启动：

- `oaradar-worker.service`        —— OA 操作 worker（常驻）
- `oaradar-markdown-worker.service` —— Markdown 转换 worker（常驻）
- `oaradar-hourly.service` / `.timer` —— 工作时段每小时扫描
- `oaradar-nightly.service` / `.timer` —— 工作日晚间全量核对

两个定时 one-shot 只向 SQLite 创建持久任务，不直接启动浏览器。真实 OA 只读扫描
统一由 `oaradar-worker.service` 串行执行，因此在线逐项核验、待办扫描和夜间补齐
不会争用同一个 Chromium 登录配置目录。

环境变量文件 `~/.config/oaradar/env`（chmod 600）用于注入飞书 webhook/secret
等敏感值，**不要写进 YAML**。若不存在，安装脚本会创建一个空文件，请手动填写。

## 调度时间

使用广州业务时区 `Asia/Shanghai`，通过 `OnCalendar=TZ=Asia/Shanghai ...` 固定，
不受宿主机时区影响：

- 每小时：周一至周五 `09:05 / 10:05 / … / 17:05`
- 夜间：周一至周五 `23:30`

安装脚本会用 `systemd-analyze calendar` 打印最终触发时间。

## 安全要求

service 单元包含：`Restart=always`、`RestartSec=5`、`NoNewPrivileges=true` 和
`UMask=0077`。Web 与 Markdown Worker 使用 `PrivateTmp=true`；OA Worker 因 Chrome
持久配置需要共享用户 `/tmp` 中的 singleton/IPC 状态，明确使用
`PrivateTmp=false`。未加入会阻止 Chrome / Playwright / GPU / Unix socket /
本地文件访问的过度沙箱。

## 防重复运行

定时入口和实际扫描依赖三层防护：

1. systemd 单实例 service；
2. `flock`（`%t/oaradar-*.lock`）；
3. 持久 `OperationJob` 队列与单一 OA Worker（浏览器/数据库租约）。

## 健康检查

```bash
./scripts/healthcheck.sh            # 使用当前目录 config.yaml
OA_CONFIG=/path/config.yaml ./scripts/healthcheck.sh
```

检查项：worker / markdown-worker 是否 active、timer 是否已安排下次运行、
最近一次 hourly / nightly Run 是否成功、数据库是否可读、OA 登录是否过期
（由最近一次成功 hourly 的时间推断）、Markdown 队列是否积压、飞书是否配置、
磁盘剩余空间。FAIL 时退出码非 0，WARN 仅为提示。

## 卸载

```bash
./scripts/uninstall-systemd-user.sh
```

仅禁用并删除生成的 unit；`~/.config/oaradar/env` 中的密钥不会被删除。

## 登出后继续运行

```bash
sudo loginctl enable-linger "$USER"
```
