# QQ小鲸鱼每日推歌（astrbot_plugin_playlist_daily）

导入歌单，每天定时推一首，附一句推荐语，30 天内不重复。

## 指令

| 指令 | 说明 |
|---|---|
| `/歌单导入 <Apple Music 链接>` | 抓取 Apple Music 歌单页面并解析曲目 |
| `/歌单导入` + 粘贴多行文本 | 每行「歌名 - 歌手」 |
| `/歌单` | 查看歌单数量与推送时间 |
| `/推歌` | 立刻推一首 |
| `/歌单清空` | 清空歌单与推送记录 |

## 配置（WebUI 插件配置页）

| 键 | 默认 | 说明 |
|---|---|---|
| enabled | true | 启用每日推送 |
| push_time | 09:00 | 每天推送时间（24 小时制） |
| push_target | 空 | 推送目标 UMO，例如 `鱼鱼:FriendMessage:2890150433` |
| history_days | 30 | 多少天内不重复 |
| with_blurb | true | 是否用模型写一句推荐语 |
| provider_id | 空 | 推荐语使用的 provider，留空用默认 |

## 数据

- 歌单与推送记录：`plugin_data/astrbot_plugin_playlist_daily/songs.json`

## 说明

- Apple Music 曲库本地文件是私有格式（`hfma`），无法直接读取；本插件走**分享链接抓取**。
- 抓取依赖页面结构，若 Apple 改版可能失效，届时更新解析规则即可。
- 只能读取**公开或已分享**的歌单页面。
