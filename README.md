# steam_free_game_plugin（AstrBot 插件）

自动抓取 Steam 商店促销信息，发现 **限时免费（-100%）** 的游戏时推送到白名单群聊/私聊。

## 功能

- 数据源：Steam 商店搜索 `search/results` 接口（避免 `steamdb.info` 的 Cloudflare 403）
- 过滤：只推送 `-100%` 且现价为 `Free/0` 的条目
- 推送内容：游戏名称、游戏图片、原价、购买链接
- 去重：使用 `workflow.csv` 记录已推送条目（不会重复发）
- 白名单：
  - 静态白名单：只填 QQ 群号/QQ 号（无需手写 `unified_msg_origin`）
  - 动态白名单：在目标群/私聊发送 `/steamfree_sub` 一键订阅

## 配置

主要配置项在 `_conf_schema.json` 中声明，AstrBot 会在控制台生成配置界面。

- `static_group_ids`: 静态推送白名单（QQ群号列表，只填群号）
- `static_user_ids`: 静态推送白名单（QQ号列表，只填 QQ 号/用户号）
- `push_platform_name`: 推送平台 ID（`unified_msg_origin` 的第一段），一般为 `aiocqhttp`；Satori 可能是 `satori_1`
- `check_interval_seconds`: 自动检查间隔（秒）
- `workflow_path_mode`: `workflow.csv` 存放位置（默认 `plugin_data`，推荐）
- `http_proxy`: HTTP 代理（留空则使用 AstrBot 全局 `http_proxy`）

## 指令

- `/steamfree_check`：手动检查一次（会先回复“开始检查”，完成后再发送统计信息）
- `/steamfree_status`：查看当前状态
- `/steamfree_sub`：将当前会话加入推送白名单（写入 `subscriptions.json`）
- `/steamfree_unsub`：将当前会话移出推送白名单
- `/steamfree_clear_history`：清空推送历史（仅 AstrBot 管理员可用）

## 文件说明

- `workflow.csv`：工作流/去重状态表（动态更新；默认写入 `data/plugin_data/<plugin>/workflow.csv`）
- `subscriptions.json`：动态白名单（由订阅指令维护，位于 `data/plugin_data/<plugin>/`）

## 去重与过期清理

插件会把“已推送过”的游戏记录在 `workflow.csv` 里，用于避免在限免期间重复刷屏。

当游戏 **不再免费** 后，会在配置的 `cleanup_not_free_after_hours`（默认 6 小时）后自动清理该条记录（默认 `cleanup_mode=delete`），以便同一游戏未来再次限免时仍然可以再次推送。
