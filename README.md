# AstrBot Plugin: Steam Monitor

这是一个为 [AstrBot](https://github.com/Soulter/AstrBot) 设计的插件，用于监控指定 Steam 用户的好友列表或单个用户的游戏状态，并在其开始或停止游戏时发送通知。

## ✨ 功能特性

- **两种监控模式**:
  - **好友列表监控**: 监控一个公开的 Steam 个人资料的好友列表，自动追踪其中所有好友的游戏状态。
  - **单独用户监控**: 直接添加并监控任意 Steam 用户，无需其在特定好友列表中。
- **智能模式切换**: 添加监控时，插件会自动检测目标用户是否在配置的好友列表中，并选择相应的监控模式。
- **动态刷新频率**: 为单独监控的用户实现了动态刷新间隔。当用户在游戏中时，刷新频率更高；当用户在线或离线时，频率会降低，以节省资源。
- **丰富的通知方式**:
  - 默认使用美观的 HTML 渲染图文卡片进行通知。
  - 支持通过 LLM（需在 AstrBot 中配置）生成风趣幽默的通知文案。
  - 支持调用外部 [Meme API](https://github.com/MeetWq/meme-generator) 生成生动的表情包通知。
- **数据持久化**: 所有监控规则和缓存都保存在 AstrBot 的 `data` 目录中，插件更新或重载后数据不丢失。
- **周期性缓存刷新**: 自动周期性地刷新 Steam ID 缓存，以应对用户更改其个人主页自定义 URL 的情况。
- **完善的指令系统**: 提供了一套完整的指令来管理监控规则。
- **代理支持**: 支持通过 HTTP/HTTPS/SOCKS5 代理访问 Steam。

## ⚙️ 安装与配置

1.  将插件文件夹放置于 AstrBot 的 `custom_plugins` 目录下。
2.  在 AstrBot 的 `requirements.txt` 中添加以下依赖，然后安装它们：
    ```
    httpx
    aiohttp
    beautifulsoup4
    ```
3.  启动 AstrBot，插件会自动加载。首次加载后，请在 AstrBot WebUI 的插件管理页面找到本插件，点击“管理”进入配置页面。

### 配置项说明

请在插件的管理页面填写以下配置：

- **核心配置**:
  - `target_profile_url`: **(好友列表模式必需)** 要监控其好友列表的 Steam 个人资料 URL。此用户的个人资料和好友列表必须设置为“公开”。
  - `steam_login_secure_cookie` / `session_id_cookie`: **(可选但强烈建议)** 您自己的 Steam Cookie。填写后能极大提升抓取稳定性，避免被 Steam 限制。
  - `proxy_url`: **(可选)** 代理服务器地址，如 `http://user:pass@host:port`。

- **监控频率 (秒)**:
  - `friend_list_check_interval_seconds`: 好友列表模式的固定刷新间隔。
  - `individual_ingame_interval_seconds`: 单独监控用户在游戏中的刷新间隔。
  - `individual_online_interval_seconds`: 单独监控用户在线但未游戏的刷新间隔。
  - `individual_offline_interval_seconds`: 单独监控用户离线时的刷新间隔。
  - `id_re_resolve_interval_hours`: Steam ID 缓存的周期性刷新间隔（小时）。

- **通知设置**:
  - `enable_llm_summaries`: 是否启用 LLM 生成通知文案。
  - `use_meme_notification`: 是否使用 Meme API 生成图片通知。
  - `meme_api_base_url`: Meme API 的服务地址。
  - `admin_notification_uids`: 用于接收插件关键错误（如 Cookie 失效）的管理员会话 ID 列表。

## 🚀 使用指令

指令前缀: `/steam` (或别名 `/steam监控`)

- **添加监控**:
  - `/steam add <Steam个人资料URL或ID>`
  - *示例*: `/steam add https://steamcommunity.com/id/gabelogannewell`
  - *说明*: 插件会自动判断监控模式。默认通知到当前会话。

- **指定游戏和会话添加**:
  - `/steam add <URL或ID> <游戏名> <会话ID>`
  - *示例*: `/steam add gabelogannewell "Counter-Strike 2" group_123456`
  - *说明*: `游戏名` 用于仅在该游戏状态变化时通知。`会话ID` 用于指定接收通知的群聊或私聊。

- **列出规则**:
  - `/steam list` (或 `/steam 列表`)
  - *说明*: 列出当前会话的所有监控规则，并显示详细的在线状态（正在玩/在线/离线）。

- **删除规则**:
  - `/steam remove <规则ID前缀>` (或 `/steam 删除`)
  - *示例*: `/steam remove a4c2b8ef`
  - *说明*: `规则ID` 可通过 `list` 指令查看。输入ID的前几位即可。

- **更新Cookie**:
  - `/steam update_cookies <steamLoginSecure值> <sessionid值>` (或 `/steam 更新cookie`)
  - *说明*: 用于在线更新配置中的 Steam Cookie。

- **强制刷新**:
  - `/steam force_refresh` (或 `/steam 强制刷新`)
  - *说明*: 立即强制刷新所有监控规则的状态，用于调试。

## ⚠️ 注意事项

- **隐私设置**: “好友列表监控”模式要求目标用户的个人资料和好友列表隐私设置为“公开”。
- **Cookie**: 强烈建议配置您自己的 Steam Cookie，否则长时间、高频率的抓取很可能被 Steam 临时屏蔽。
- **Meme API**: 如果您启用 Meme 通知，请确保您的 Meme API 服务正在运行，并且 `steam_message` 表情模板可用。
