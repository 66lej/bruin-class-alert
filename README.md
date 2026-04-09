# Bruin Class Alert

一个基于 Python 3 的 UCLA 选课提醒脚本。它会定期查询 UCLA 当前的 Schedule of Classes 页面，在你关注的 section 有 seat open 时发出通知。

## 为什么不用旧仓库

你找到的参考仓库思路是对的，但它依赖的是 2013 年左右的页面结构和 Python 2 生态：

- 抓的是旧版 Registrar HTML。
- 依赖 Python 2、`execfile` 和 BeautifulSoup 3。
- 用的是 Gmail 明文密码式 SMTP 配置。

这个新版直接查询现在的 UCLA Schedule of Classes 搜索结果页，并按当前页面结构解析 section 状态。

## 功能

- 支持 Python 3
- 支持多门课同时监控
- 支持按 section 过滤，比如 `Lec 1`、`Dis 1A`
- 支持自动尝试常见 UCLA 课号格式，比如把 `31` 自动尝试成 `0031`
- 默认支持 macOS 桌面通知
- 支持 Discord webhook 手机提醒
- 支持 SMTP 邮件提醒
- 支持本机 Chrome 上的 MyUCLA 自动 enroll 尝试
- 会记住已经提醒过的 open 状态，避免每轮轮询都重复轰炸

## 安装

```bash
python3 -m pip install -r requirements.txt
cp config.example.json config.json
cp .env.example .env
```

## 配置

编辑 `config.json`。

### 基本字段

- `poll_interval_seconds`: 轮询间隔，建议 60 秒或更慢
- `request_timeout_seconds`: 每次请求的超时时间
- `request_retries`: 遇到 UCLA 网络抖动时的自动重试次数
- `retry_backoff_seconds`: 每次重试前的退避秒数
- `watchlist`: 你要监控的课

### watchlist 里每一项

- `term`: UCLA term code，例如 `26S`，也可以写成 `Spring 2026`
- `subject`: UCLA subject code，例如 `COM SCI`，也可以写成 `Computer Science`
- `catalog`: 课程号，例如 `31`、`100A`、`M146`
- `section`: 可选。如果不写，就表示这门课任何一个 section open 都提醒
- `session_group`: 可选。夏季学期需要时可写，比如 `A%`、`C6`
- `notify_on_waitlist`: 可选。默认 `false`。如果设成 `true`，waitlist 还有空间时也提醒

### 本机自动 Enroll

`auto_enroll` 是可选的。本机自动 enroll 当前只支持：

- `macOS`
- `Google Chrome`
- 你已经在本机 Chrome 里手动登录过 `MyUCLA`

推荐先保守使用：

```json
"auto_enroll": {
  "enabled": true,
  "allow_waitlist_auto_enroll": false
}
```

默认行为是：

- 只有真正有 seat open 时才尝试自动 enroll
- 不会因为 waitlist 有位置就自动把你送进 waitlist
- 会复用你本机 Chrome 当前登录态，不保存 UCLA 密码

第一次使用前，先手动打开 MyUCLA 登录页：

```bash
python3 myucla_auto_enroll.py --setup-login
```

在 Chrome 里完成 UCLA Logon 和 Duo 之后，以后监控脚本就会在检测到 open seat 时自动尝试。

另外需要在 Chrome 打开这个开关：

- `View > Developer > Allow JavaScript from Apple Events`

如果这个开关没开，脚本可以打开 Chrome，但不能真正驱动页面点击。

如果你想先确认本机浏览器控制没问题，可以跑：

```bash
python3 myucla_auto_enroll.py --self-test
```

注意：

- `GEOG 7` 这种需要 `Lecture + Laboratory` 的课，我现在的策略是自动选择当前 enroll flow 里第一个可选 secondary section
- 如果 MyUCLA 弹出 `PTE`、特殊 warning、限制条件或登录过期，脚本会停止自动动作并把结果写进通知正文
- 这部分是 best-effort，本机浏览器 DOM 以后如果 UCLA 改版，可能需要再调

### 通知方式

#### macOS

```json
"notifiers": {
  "macos": true
}
```

#### Discord webhook

```json
"notifiers": {
  "macos": true,
  "discord_webhook_env": "BRUIN_ALERT_DISCORD_WEBHOOK_URL"
}
```

#### 邮件

建议用 Gmail App Password，不要用主密码。

```json
"email": {
  "enabled": true,
  "smtp_host": "smtp.gmail.com",
  "smtp_port": 587,
  "use_tls": true,
  "username_env": "BRUIN_ALERT_EMAIL_USERNAME",
  "password_env": "BRUIN_ALERT_SMTP_PASSWORD",
  "from_email_env": "BRUIN_ALERT_EMAIL_FROM",
  "to_email_env": "BRUIN_ALERT_EMAIL_TO"
}
```

然后在 `.env` 里填：

```bash
BRUIN_ALERT_DISCORD_WEBHOOK_URL='你的 Discord webhook'
BRUIN_ALERT_EMAIL_USERNAME='你的 Gmail'
BRUIN_ALERT_EMAIL_FROM='发件邮箱'
BRUIN_ALERT_EMAIL_TO='收件邮箱'
BRUIN_ALERT_SMTP_PASSWORD='你的 Gmail app password'
```

## 运行

先跑一次单次检查：

```bash
python3 bruin_alert.py --config config.json --once --debug
```

如果输出正常，再持续运行：

```bash
python3 bruin_alert.py --config config.json
```

脚本启动时会自动读取当前目录下的 `.env`。如果 Discord 或 Email 的变量还没填好，它会先跳过这些通道，但桌面通知和终端输出仍然能继续工作。

## 辅助命令

列出当前 UCLA term：

```bash
python3 bruin_alert.py --list-terms
```

列出当前 UCLA subject：

```bash
python3 bruin_alert.py --list-subjects
```

## 使用建议

- 不要把轮询间隔设得太快，`60` 秒通常已经够用了。
- 先用 `--once --debug` 看脚本到底识别出了哪些 section 名称，再决定 `section` 字段怎么填。
- 如果你合上笔记本或者电脑睡眠，本地桌面通知当然也不会弹。想要 24/7 跑的话，可以放到一直在线的机器上，再配 Discord 或邮件提醒。
