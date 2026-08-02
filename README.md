# 百度网盘自动转存

这是一个纯后端、配置文件驱动的百度网盘订阅转存工具。2.0 版移除了 Flask、HTTP API、SSE、Vue 前端和登录页面；用户、订阅、定时、通知等声明全部来自 JSON 配置，运行和运维操作通过 `cli.py` 完成。

## 核心能力

- 自动扫描分享内容并转存新增文件，保留原有目录处理和去重逻辑
- 多百度账号配置，通过 `baidu.current_user` 选择本次运行账号
- 每个订阅独立 cron，或使用一个/多个全局默认 cron
- 正则文件过滤与重命名；无效正则会终止该订阅，不会静默改为全量转存
- 多种通知渠道和自定义 Webhook，支持短时间结果合并
- 网盘容量检查与阈值告警
- 为网盘目录生成分享链接
- `tqdm` 展示准确的订阅完成进度和当前阶段
- 每次订阅运行保存独立、完整的 DEBUG 日志
- 声明式配置、运行状态和日志相互隔离；配置与状态使用原子替换写入

转存与去重的核心实现仍位于 `storage.py`，没有替换为近似算法。无法确认目标目录内容时任务会失败，而不是把扫描失败当成空目录后重复转存。

所有运行路径均为非交互式。百度接口若要求图形验证码，程序不会打开 GUI 或等待输入，而会让该订阅明确失败并保留完整日志；本项目没有实现验证码绕过或近似替代。

## 环境要求与安装

- Python 3.10
- Windows、Linux 或 macOS

```bash
source .venv/bin/activate
uv pip install -r requirements.txt
```

开发与测试依赖：

```bash
source .venv/bin/activate
uv pip install -r requirements-dev.txt
```

## 快速开始

创建配置：

```bash
source .venv/bin/activate
python cli.py init
```

编辑 `config/config.json`，至少填写当前用户和订阅。Cookies 必须包含 `BDUSS` 与 `STOKEN`：

```json
{
  "runtime": {
    "timezone": "Asia/Shanghai",
    "progress": true,
    "log_dir": "log",
    "state_file": "state/task_status.json",
    "log_level": "INFO",
    "general_log_retention_days": 14
  },
  "baidu": {
    "users": {
      "main": {
        "cookies": "BDUSS=替换为真实值; STOKEN=替换为真实值"
      }
    },
    "current_user": "main",
    "tasks": [
      {
        "order": 1,
        "name": "示例订阅",
        "url": "https://pan.baidu.com/s/1AbCdEfGhIj",
        "pwd": "a1b2",
        "save_dir": "/自动转存/示例订阅",
        "regex_pattern": ".*\\.mp4$",
        "regex_replace": ""
      }
    ]
  },
  "cron": {
    "default_schedule": ["0 10 * * *"]
  },
  "notify": {
    "enabled": false,
    "notification_delay": 30,
    "direct_fields": {},
    "custom_fields": {}
  },
  "scheduler": {
    "max_workers": 1,
    "misfire_grace_time": 3600,
    "coalesce": true,
    "max_instances": 1
  },
  "quota_alert": {
    "enabled": true,
    "threshold_percent": 90,
    "check_schedule": "0 0 * * *"
  },
  "share": {
    "default_password": "1234",
    "default_period_days": 7
  },
  "file_operations": {
    "rename_delay_seconds": 0.5
  }
}
```

先做离线校验，再访问百度网盘：

```bash
python cli.py validate
python cli.py run
```

常驻运行定时任务：

```bash
python cli.py daemon
```

同一进程内的订阅转存严格串行执行；多个定时点同时触发时会排队，不会因抢锁失败而丢弃其中一条订阅。`scheduler.max_workers` 控制调度器工作线程数量，但不会绕过这项转存串行约束。

## 配置语义

### 订阅字段

每个 `baidu.tasks` 元素使用以下字段：

| 字段 | 必需 | 含义 |
|---|---:|---|
| `order` | 是 | 正整数且全局唯一；也可用于 CLI 选择任务 |
| `url` | 是 | 不含查询参数的 `http://pan.baidu.com/s/...` 或 `https://pan.baidu.com/s/...` 分享链接；提取码必须单独写入 `pwd` |
| `save_dir` | 是 | 百度网盘目标目录；没有 `/` 前缀时运行时会补齐 |
| `name` | 否 | 可读名称；同名任务应用 `task_uid` 或 `order` 选择 |
| `pwd` | 否 | 分享提取码；日志只记录是否配置，不记录原值 |
| `task_uid` | 否 | 稳定标识；省略时首次初始化会生成并原子写回配置 |
| `cron` | 否 | 该订阅专用定时；省略时使用 `cron.default_schedule` |
| `category` | 否 | 分类元数据，转存逻辑不依赖它 |
| `regex_pattern` | 否 | 使用 `re.search` 筛选完整分享路径 |
| `regex_replace` | 否 | 匹配成功后使用 `re.sub` 生成目标文件名 |

`regex_pattern` 不匹配的文件不会转存。匹配且 `regex_replace` 非空时，程序会在转存后重命名；重命名仍沿用原项目的限频延迟和重试逻辑。

### cron

配置接受五字段 cron：`分钟 小时 日期 月份 星期`。星期数字按传统 cron 编号转换：`0`/`7` 为周日，`1` 为周一；三字母英文星期与月份缩写也会先展开为明确集合。所有字段都会严格检查边界、范围和步长，然后才交给 APScheduler。

传统 cron 与 APScheduler 对“日期和星期同时受限”分别常用 OR 与 AND 语义。为了不静默改变执行日期，本项目明确拒绝这种有歧义的单条表达式；请只限制其中一个字段。

示例：

- `*/5 * * * *`：每 5 分钟
- `0 */2 * * *`：每 2 小时
- `0 8,12,18 * * *`：每天 08:00、12:00、18:00
- `0 10 * * 1-5`：周一至周五 10:00

时区来自 `runtime.timezone`，必须是有效 IANA 时区名称。

### 通知

只有 `notify.enabled=true` 时才会入队和发送通知。`notification_delay=0` 表示立即发送；正数表示在最后一个结果到达后延迟指定秒数并合并。`direct_fields` 与 `custom_fields` 的字段名对应 `notify.py` 中的通知变量，例如：

```json
{
  "enabled": true,
  "notification_delay": 30,
  "direct_fields": {
    "PUSH_PLUS_TOKEN": "token",
    "PUSH_PLUS_USER": "",
    "WEBHOOK_URL": "https://example.invalid/hook",
    "WEBHOOK_METHOD": "POST",
    "WEBHOOK_CONTENT_TYPE": "application/json",
    "WEBHOOK_HEADERS": "Content-Type: application/json",
    "WEBHOOK_BODY": "title: $title\ncontent: $content"
  }
}
```

通知渠道自身的 HTTP 返回与错误处理仍由 `notify.py` 负责。`notify-test` 表示通知函数调用完成，不等同于第三方服务最终送达保证，应同时检查接收端。

## CLI

全局参数必须写在子命令之前，例如：

```bash
python cli.py --config /path/config.json --log-dir /path/log run --task 1
```

常用命令：

```bash
# 不联网校验配置
python cli.py validate

# 查看用户、订阅、定时和最近运行状态（不打印 Cookies）
python cli.py list

# 执行全部订阅
python cli.py run

# 按 order、task_uid、唯一名称或 URL 执行；--task 可重复
python cli.py run --task 1 --task another-task-uid

# 常驻调度
python cli.py daemon

# 容量检查；达到阈值且通知启用时发送告警
python cli.py quota

# 测试通知
python cli.py notify-test

# 获取分享内容顶层名称，辅助填写配置
python cli.py inspect-share 'https://pan.baidu.com/s/1AbCdEfGhIj' --password a1b2

# 分享任意网盘路径或某个订阅的保存目录
python cli.py share --path /自动转存/示例订阅
python cli.py share --task 1 --password 1234 --period-days 7
```

`run` 的退出码：全部成功或跳过为 `0`，存在转存失败为 `1`，配置/选择器错误为 `2`，用户中断为 `130`。

修改常驻进程的配置后，在 Linux/macOS 向进程发送 `SIGHUP` 触发严格校验和安全重载；无效配置会被拒绝，旧调度继续运行。Windows 或不便发送信号的容器环境请重启进程。

## 进度、状态与日志

`tqdm` 的 `0/1` 表示一个订阅是否完成，后缀显示当前转存阶段。百度接口按目录批量转存，底层没有可靠的逐文件完成事件，因此程序不会伪造文件百分比。可用 `--no-progress` 或 `runtime.progress=false` 关闭。

文件布局：

```text
log/
├── application_2026-08-01.log
└── subscriptions/
    └── <task_uid>/
        └── 20260801T120000+0800_<run_id>.log
state/
└── task_status.json
```

- 应用日志始终记录 DEBUG 到文件，控制台级别由 `runtime.log_level` 控制。
- 通用应用日志默认保留 14 天；设 `general_log_retention_days=0` 表示不自动清理。
- 订阅日志每次运行一个文件，不自动删除，包含从任务开始、底层扫描/API 操作、回调进度到最终结果的完整记录。
- `state/task_status.json` 只保存最近状态、时间、本次运行文件列表和最近日志路径；新一轮开始时会清除上一轮的文件列表。
- `config/config.json` 不再承载运行状态，任务执行不会覆盖用户正在编辑的声明式配置。
- 日志过滤常见 Cookie 键，提取码不写入日志；配置文件本身仍包含敏感凭据，请限制文件权限并避免提交到 Git。

## Docker Compose

```bash
mkdir -p config log state
cp config/config.template.json config/config.json
# 编辑 config/config.json
docker compose up -d
docker compose logs -f
```

容器没有 HTTP 端口。卷分别保存配置、日志和运行状态。如果挂载目录中没有 `config.json`，入口脚本会创建模板；填写后重启容器。

## 从 Web 版本迁移

原有 `config/config.json` 中的用户、任务、定时表达式、通知、调度器、容量告警、分享和重命名延迟配置可继续使用。迁移步骤：

1. 备份现有 `config/config.json` 与 `log/`。
2. 将模板新增的 `runtime` 段加入配置。删除旧 `auth`、顶层 `retry`、顶层 `regex`、`cron.auto_install`、`file_operations.batch_size` 和 `file_operations.concurrent_limit`。`auth` 与 `cron.auto_install` 只服务已删除的 Web 登录/生命周期；其余列出的字段未被转存核心读取。新版本会明确拒绝这些遗留项。正则规则应放到每个任务的 `regex_pattern`/`regex_replace`。
3. 运行 `python cli.py validate`。无 `task_uid` 的订阅会在真正初始化存储时生成稳定标识。
4. 先运行一次 `python cli.py run --task <order>` 核对目标目录、正则与日志。
5. 确认后改用 `python cli.py daemon` 或 Docker Compose。

Web 路由、端口 5000、会话认证、前端轮询/SSE 和在线任务编辑已删除，不提供兼容 HTTP 接口。

## 开发验证

```bash
source .venv/bin/activate
PYTHONPYCACHEPREFIX=/tmp/baidu-autosave-pycache python -m pytest -q
PYTHONPYCACHEPREFIX=/tmp/baidu-autosave-pycache python -m compileall -q \
  cli.py config_loader.py cron_utils.py progress_display.py runtime_logging.py \
  runtime_state.py scheduler.py storage.py notify.py utils.py
UV_CACHE_DIR=/tmp/baidu-autosave-uv-cache uv pip check
```

真实百度 API 的端到端测试需要有效 Cookies 和分享链接，不应在公共 CI 中使用真实凭据。

## 许可证与致谢

项目使用 AGPL-3.0 许可证（见 `LICENSE`）。核心依赖包括 APScheduler、baidupcs-py、Loguru 和 tqdm。
