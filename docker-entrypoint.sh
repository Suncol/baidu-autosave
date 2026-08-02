#!/bin/sh
set -eu

config_path="${BAIDU_AUTOSAVE_CONFIG:-/app/config/config.json}"

if [ ! -e "$config_path" ]; then
    mkdir -p "$(dirname "$config_path")"
    cp /app/template/config.template.json "$config_path"
    echo "已创建配置模板: $config_path"
    echo "请填写用户和订阅配置；可使用 validate 命令校验。"
fi

exec python /app/cli.py --config "$config_path" "$@"
