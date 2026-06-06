# ESE Auto Updater

这个工具用于 Ubuntu 服务器自动维护 ESE 曲库：

- 每天对 ESE 仓库执行 `git pull --ff-only`
- `git pull` 默认走 Ubuntu 本地 `socks5h://127.0.0.1:40000`，且不设置超时
- systemd service 会设置 `TimeoutStartSec=infinity`，慢仓库不会被定时任务超时杀掉
- 根据 `uploaded2.json` 只上传还没成功上传过的新歌
- 歌曲上传强制直连，不使用 Git 代理，也不读取系统代理环境变量
- 上传成功后立刻写回 `uploaded2.json`
- 每次运行结束后用 Resend API 发送邮件汇总新歌列表、成功数量、失败/跳过数量、当前歌曲数量和 git pull 信息
- 支持 CLI 手动触发，部署用户可以在 `setup.sh` 里交互式调整配置

## 部署

```bash
cd /path/to/auto-updater
chmod +x setup.sh
./setup.sh
```

`setup.sh` 会安装虚拟环境、安装依赖、生成或更新 `.env`，并逐项询问：

- ESE 仓库路径、站点 URL、上传记录 JSON
- `git pull` 参数和 Git 代理
- 音频扩展名、上传重试次数
- Resend API key、发件人、收件人、邮件标题前缀
- 每日 systemd timer 执行时间和随机延迟
- 是否安装/更新 timer，是否部署后立刻执行一次

ESE 仓库默认自动匹配上级目录的 `ESE`：

```text
/path/to/ESE
/path/to/auto-updater
```

也可以在 setup 提示里手动输入绝对路径，或之后修改 `.env`：

```env
ESE_DIR=/absolute/path/to/ESE
```

## 非交互部署

已经准备好 `.env` 时：

```bash
./setup.sh --non-interactive
```

只安装依赖、不安装 timer：

```bash
./setup.sh --no-timer
```

使用自定义配置文件：

```bash
./setup.sh --env-file production.env
```

## 常用命令

```bash
# 完整流程：pull + 上传新增 + 发邮件
.venv/bin/python upload.py run --env-file .env

# 只查看候选新歌，不上传
.venv/bin/python upload.py upload --env-file .env --dry-run

# 只上传新增，不 pull、不发邮件
.venv/bin/python upload.py upload --env-file .env

# 只执行 git pull
.venv/bin/python upload.py pull --env-file .env

# 查看当前统计
.venv/bin/python upload.py status --env-file .env

# 测试 Resend 邮件配置
.venv/bin/python upload.py email-test --env-file .env
```

## 代理规则

`.env` 里的 Git 代理默认是：

```env
GIT_PROXY_URL=socks5h://127.0.0.1:40000
```

只影响 `git pull`。如果要让 Git 直连，把它留空即可：

```env
GIT_PROXY_URL=
```

上传歌曲接口始终直连，不走这个代理，也不走系统里的 `HTTP_PROXY` / `HTTPS_PROXY`。

## 查看定时任务

```bash
systemctl --user list-timers ese-auto-updater.timer
journalctl --user -u ese-auto-updater.service -n 100 --no-pager
```

如果服务器需要用户退出登录后 timer 仍然运行，可以让管理员执行：

```bash
sudo loginctl enable-linger "$USER"
```
