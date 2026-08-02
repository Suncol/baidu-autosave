# 配置文件

运行时只读取 JSON 配置，不再提供 Web 管理界面。先执行：

```bash
python cli.py init
```

然后编辑 `config/config.json`。最小可运行的 `baidu` 配置如下：

```json
{
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
        "cron": "0 */2 * * *",
        "regex_pattern": ".*\\.mp4$",
        "regex_replace": ""
      }
    ]
  }
}
```

实际文件还需保留模板中的 `runtime`、`cron`、`notify`、`scheduler`、
`quota_alert`、`share` 和 `file_operations` 配置。`cron` 使用标准五字段格式，
星期数字遵循标准 cron：`0`/`7` 是周日、`1` 是周一。每个任务可省略
`cron` 以使用 `cron.default_schedule`。`task_uid` 可以省略，首次启动时会生成
稳定标识并以原子写入方式保存回配置文件。

`url` 必须写成不含查询参数的 `http://pan.baidu.com/s/...` 或
`https://pan.baidu.com/s/...`；如果复制的链接
带有 `?pwd=a1b2`，请从 URL 删除这一段，并把 `a1b2` 写入任务的 `pwd` 字段。

为避免传统 cron 与 APScheduler 在“日期 + 星期”组合上的 OR/AND 差异，单条
表达式不能同时限制这两个字段。运行状态写入 `runtime.state_file`，不会写回
声明式配置；每次运行的完整日志位于 `runtime.log_dir/subscriptions/<task_uid>/`。

编辑后必须先校验：

```bash
python cli.py validate
```

无效 JSON、重复字段、无效 cron、重复 order/task_uid、错误正则表达式、无效
当前用户等都会使校验失败，不会用弱化逻辑继续运行。
