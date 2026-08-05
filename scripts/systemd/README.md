# OARadar systemd 用户服务

这些 unit 让用户级 systemd (`systemctl --user`) 在登录会话中运行 OARadar 的
常驻 worker 与定时扫描。路径中的 `%h` 会被展开为对应用户的家目录根。

## 安装

将 unit 文件软链（或复制）到用户 systemd 目录：

```bash
mkdir -p ~/.config/systemd/user
ln -s "$PWD"/oaradar-*.service ~/.config/systemd/user/
ln -s "$PWD"/oaradar-*.timer   ~/.config/systemd/user/
```

> 注意：unit 文件中 `WorkingDirectory=%h/OARadar` 与 `ExecStart` 里的
> `%h/.local/bin/uv` 是占位路径，请按你的实际仓库位置与 `uv` 安装路径修改。

## 环境变量

飞书 webhook/secret 等敏感值通过 env 文件注入，不要写进 YAML：

```bash
mkdir -p ~/.config/oaradar && chmod 700 ~/.config/oaradar
cat > ~/.config/oaradar/env <<'EOF'
FEISHU_OA_WEBHOOK=https://open.feishu.cn/open-apis/bot/v2/hook/替换为真实值
FEISHU_OA_SECRET=替换为真实签名密钥
EOF
chmod 600 ~/.config/oaradar/env
```

## 启用

```bash
systemctl --user daemon-reload
systemctl --user enable --now \
  oaradar-worker.service \
  oaradar-markdown-worker.service \
  oaradar-hourly.timer \
  oaradar-nightly.timer
```

## 巡检

```bash
systemctl --user status oaradar-worker.service oaradar-markdown-worker.service
systemctl --user list-timers 'oaradar-*'
```

## 登出后继续运行

若希望服务器登出后 worker 仍运行：

```bash
sudo loginctl enable-linger "$USER"
```

## 调度说明

- `oaradar-hourly`：工作日上午 09:05 至 17:05 每小时触发
  `scripts/hourly-sync.sh`（Pending discover + Done refresh-head），用 `flock`
  防止上一次未结束时重复启动。
- `oaradar-nightly`：工作日晚 23:30 跑一次 `oa manifest sync` 全量核对，
  弥补每小时只扫最新三页可能遗漏的异常变化。
