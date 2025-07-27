import asyncio
import httpx
import os
import json
import uuid
import re
import time
from urllib.parse import urlparse

from .steam_client import SteamClient
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.api import AstrBotConfig
from astrbot.api.message_components import Plain, Image

DATA_DIR_NAME = "steam_monitor"
MONITORING_RULES_FILE = "monitoring_rules.json"
STEAM_ID_CACHE_FILE = "steam_id_cache.json"

@register("steam_monitor_public_v2", "wzken", "通过公开好友列表或直接添加用户监控Steam游戏状态", "2.5.0", "https://github.com/your-repo/steam-monitor-plugin-v2")
class SteamMonitorPublicPluginV2(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        # --- 配置加载 ---
        self.target_profile_url = self.config.get("target_profile_url", "")
        self.proxy_url = self.config.get("proxy_url", "")
        self.id_re_resolve_interval = self.config.get("id_re_resolve_interval_hours", 24) * 3600
        self.use_meme_notification = self.config.get("use_meme_notification", False)
        self.meme_api_base_url = self.config.get("meme_api_base_url", "http://127.0.0.1:2233")
        # 好友列表监控配置
        self.friend_list_interval = self.config.get("friend_list_check_interval_seconds", 120)
        # 单独监控配置
        self.ind_ingame_interval = self.config.get("individual_ingame_interval_seconds", 60)
        self.ind_online_interval = self.config.get("individual_online_interval_seconds", 300)
        self.ind_offline_interval = self.config.get("individual_offline_interval_seconds", 900)

        # --- 客户端与数据初始化 ---
        self.steam_client = SteamClient(
            steam_login_secure_cookie=self.config.get("steam_login_secure_cookie"),
            session_id_cookie=self.config.get("session_id_cookie"),
            proxy_url=self.proxy_url
        )
        self.monitoring_rules = self._load_monitoring_rules()
        self.steam_id_cache = self._load_steam_id_cache()
        
        # --- 共享状态缓存 ---
        self.friend_list_states = {} # 用于存储好友列表快照
        self.friend_list_cache = set() # 仅用于添加规则时快速判断

        # --- 启动分离的监控任务 ---
        self.friend_monitor_task = asyncio.create_task(self._friend_list_monitor_task())
        self.individual_monitor_task = asyncio.create_task(self._individual_monitor_task())
        self.cache_resolver_task = asyncio.create_task(self._periodic_cache_resolver_task())
        
        logger.info(f"Steam Monitor Plugin V2.5.0: 插件已启动。当前监控规则数量: {len(self.monitoring_rules)}")

    # --- 数据持久化 ---
    def _get_data_filepath(self, filename: str) -> str:
        plugin_data_dir = os.path.join(self.context.data_dir, DATA_DIR_NAME)
        os.makedirs(plugin_data_dir, exist_ok=True)
        return os.path.join(plugin_data_dir, filename)

    def _load_json(self, filename: str, default_value):
        filepath = self._get_data_filepath(filename)
        if os.path.exists(filepath):
            try:
                with open(filepath, "r", encoding="utf-8") as f: return json.load(f)
            except Exception as e:
                logger.error(f"加载 {filename} 失败: {e}")
        return default_value

    def _save_json(self, filename: str, data):
        filepath = self._get_data_filepath(filename)
        try:
            with open(filepath, "w", encoding="utf-8") as f: json.dump(data, f, ensure_ascii=False, indent=4)
        except Exception as e:
            logger.error(f"保存 {filename} 失败: {e}")

    def _load_monitoring_rules(self) -> list:
        rules = self._load_json(MONITORING_RULES_FILE, [])
        for rule in rules:
            rule.setdefault("monitoring_type", "friend_list")
            rule.setdefault("event_history", [])
            rule.setdefault("total_playtime_today", 0)
            rule.setdefault("last_reset_day", time.strftime("%Y-%m-%d"))
            rule.setdefault("current_game_start_timestamp", 0)
        return rules

    def _save_monitoring_rules(self):
        self._save_json(MONITORING_RULES_FILE, self.monitoring_rules)

    def _load_steam_id_cache(self) -> dict:
        return self._load_json(STEAM_ID_CACHE_FILE, {})

    def _save_steam_id_cache(self):
        self._save_json(STEAM_ID_CACHE_FILE, self.steam_id_cache)

    # --- 核心监控逻辑 ---
    async def _friend_list_monitor_task(self):
        """独立的后台任务，用于定期刷新好友列表快照并检查相关规则。"""
        await asyncio.sleep(5) # 错开启动时间
        while True:
            try:
                friend_rules = [r for r in self.monitoring_rules if r.get("monitoring_type") == "friend_list"]
                if not friend_rules or not self.target_profile_url:
                    await asyncio.sleep(self.friend_list_interval)
                    continue

                logger.info("开始好友列表监控检查...")
                html_content, _ = await self.steam_client.fetch_html(self.target_profile_url.strip('/') + '/friends/')
                
                if html_content == 403:
                    await self._notify_admins("⚠️ Steam 监控插件错误：访问好友列表被拒绝(403)，可能是 Cookie 已失效或目标隐私设置问题。请使用 `/steam 更新cookie` 命令更新。")
                    # 等待更长时间再重试
                    await asyncio.sleep(self.friend_list_interval * 5)
                    continue

                if isinstance(html_content, str):
                    self.friend_list_states = self.steam_client.extract_friends_game_status_from_html(html_content)
                    self.friend_list_cache = set(self.friend_list_states.keys())
                
                tasks = [self._process_rule_update(rule, self.friend_list_states.get(rule["target_steam_id64"])) for rule in friend_rules]
                await asyncio.gather(*tasks)
                self._save_monitoring_rules()
                logger.info("好友列表监控检查完成。")

            except Exception as e:
                logger.error(f"好友列表监控任务发生错误: {e}", exc_info=True)
            
            await asyncio.sleep(self.friend_list_interval)

    async def _individual_monitor_task(self):
        """独立的后台任务，用于动态刷新单独监控的规则。"""
        await asyncio.sleep(10) # 错开启动时间
        while True:
            try:
                individual_rules = [r for r in self.monitoring_rules if r.get("monitoring_type") == "individual"]
                if not individual_rules:
                    await asyncio.sleep(self.ind_online_interval) # 如果没有规则，就按中等频率检查
                    continue

                next_sleep_time = self._calculate_individual_next_sleep_time(individual_rules)
                logger.info(f"下一次单独监控检查将在 {next_sleep_time} 秒后进行。")
                await asyncio.sleep(next_sleep_time)

                logger.info("开始单独监控检查...")
                tasks = []
                for rule in individual_rules:
                    current_info = await self.steam_client.get_single_player_status(rule["target_steam_id64"])
                    tasks.append(self._process_rule_update(rule, current_info))
                
                await asyncio.gather(*tasks)
                self._save_monitoring_rules()
                logger.info("单独监控检查完成。")

            except Exception as e:
                logger.error(f"单独监控任务发生错误: {e}", exc_info=True)
                await asyncio.sleep(self.ind_online_interval) # 发生错误时，使用中等间隔

    async def _periodic_cache_resolver_task(self):
        """独立的后台任务，用于定期刷新Steam ID缓存。"""
        if not self.id_re_resolve_interval or self.id_re_resolve_interval <= 0:
            logger.info("ID缓存周期性刷新已禁用。")
            return
        
        await asyncio.sleep(60) # 错开启动时间
        while True:
            try:
                logger.info("开始周期性检查并刷新Steam ID缓存...")
                now = int(time.time())
                # 创建副本以安全地迭代和修改
                cache_copy = dict(self.steam_id_cache)
                updated_count = 0
                for key, value in cache_copy.items():
                    # 如果时间戳不存在或已过期
                    if "timestamp" not in value or (now - value["timestamp"]) > self.id_re_resolve_interval:
                        logger.info(f"缓存条目 '{key}' 已过期，正在强制刷新...")
                        # 使用 force_re_resolve=True 调用解析函数
                        await self.steam_client.resolve_steam_url_to_id64(key, self.steam_id_cache, force_re_resolve=True)
                        updated_count += 1
                        await asyncio.sleep(2) # 避免请求过于频繁
                
                if updated_count > 0:
                    self._save_steam_id_cache()
                    logger.info(f"Steam ID缓存刷新完成，共更新 {updated_count} 个条目。")
                else:
                    logger.info("所有Steam ID缓存均在有效期内，无需刷新。")

            except Exception as e:
                logger.error(f"周期性缓存刷新任务发生错误: {e}", exc_info=True)
            
            await asyncio.sleep(self.id_re_resolve_interval / 2) # 按设定间隔的一半进行下一次检查

    def _calculate_individual_next_sleep_time(self, individual_rules: list) -> int:
        """根据单独监控规则的当前状态计算下一次的休眠时间。"""
        is_anyone_ingame = any(rule.get("last_known_game_name") for rule in individual_rules)
        if is_anyone_ingame:
            return self.ind_ingame_interval

        is_anyone_online = any(rule.get("target_steam_avatar_url") for rule in individual_rules)
        if is_anyone_online:
            return self.ind_online_interval
        
        return self.ind_offline_interval

    async def _process_rule_update(self, rule: dict, current_info: dict | None):
        """通用函数，处理单个规则的状态比较、更新和通知。"""
        # 每日重置今日游戏总时长
        today_str = time.strftime("%Y-%m-%d")
        if rule.get("last_reset_day") != today_str:
            rule["total_playtime_today"] = 0
            rule["last_reset_day"] = today_str

        if not current_info:
            return

        player_name = current_info.get("name") or rule["target_steam_display_name"]
        current_game_name = current_info.get("game")
        old_game_name = rule.get("last_known_game_name")

        if current_game_name != old_game_name:
            event_type = None
            duration_seconds = 0
            
            # 停止游戏
            if not current_game_name and old_game_name:
                event_type = 'stop'
                start_time = rule.get('current_game_start_timestamp', 0)
                if start_time and start_time > 0:
                    duration_seconds = int(time.time() - start_time)
                    rule['total_playtime_today'] = rule.get('total_playtime_today', 0) + duration_seconds
                rule['current_game_start_timestamp'] = 0 # 重置开始时间
            
            # 开始游戏
            elif current_game_name and not old_game_name:
                event_type = 'start'
                rule['current_game_start_timestamp'] = int(time.time())

            # 换游戏 (视为先停后开)
            elif current_game_name and old_game_name:
                # 停止旧游戏
                event_type_stop = 'stop'
                start_time_old = rule.get('current_game_start_timestamp', 0)
                if start_time_old and start_time_old > 0:
                    duration_seconds_old = int(time.time() - start_time_old)
                    rule['total_playtime_today'] = rule.get('total_playtime_today', 0) + duration_seconds_old
                    self._add_event_to_history(rule, event_type_stop, old_game_name, duration_seconds_old)
                
                # 开始新游戏
                event_type = 'start'
                rule['current_game_start_timestamp'] = int(time.time())

            if event_type:
                self._add_event_to_history(rule, event_type, old_game_name or current_game_name, duration_seconds)
                logger.info(f"状态变更 ({rule['monitoring_type']}): {player_name} 从 '{old_game_name or '离线'}' 变为 '{current_game_name or '在线'}'")
                await self._notify_user(rule, event_type, current_game_name or old_game_name, duration_seconds, current_info.get("avatar_url"))

        rule.update({
            'target_steam_display_name': player_name,
            'target_steam_avatar_url': current_info.get("avatar_url"),
            'last_known_game_name': current_game_name,
            'last_state_change_timestamp': int(time.time()) if current_game_name != old_game_name else rule.get('last_state_change_timestamp', int(time.time()))
        })

    async def _notify_user(self, rule: dict, event_type: str, game_name: str, duration_seconds: int, avatar_url: str):
        player_name = rule['target_steam_display_name']
        game_to_monitor = rule.get('game_name_to_monitor')
        
        # 如果设置了特定游戏监控，但当前事件的游戏不匹配，则不通知
        if game_to_monitor and game_to_monitor.lower() not in game_name.lower():
            return

        # 构建基础消息
        action_text = "开始玩" if event_type == 'start' else "停止了玩"
        base_message = f"{( '🟢' if event_type == 'start' else '🔴')} {player_name} {action_text}《{game_name}》。"
        if event_type == 'stop' and duration_seconds > 0:
            base_message += f" 本次游玩时长: {self._format_duration(duration_seconds)}。"

        notification_message = base_message

        # 如果启用了LLM，则生成增强版消息
        if self.config.get("enable_llm_summaries", True):
            provider = self.context.get_using_provider()
            if provider:
                try:
                    prompt = self._build_llm_prompt(rule, player_name, event_type, game_name, duration_seconds)
                    llm_response = await provider.text_chat(prompt=prompt, contexts=[])
                    if llm_response.completion_text:
                        notification_message = f"{base_message}\n> {llm_response.completion_text.strip()}"
                except Exception as e:
                    logger.error(f"调用LLM生成通知时出错: {e}")
        
        chain = None
        # 尝试使用Meme API生成图片
        if self.use_meme_notification and avatar_url:
            meme_text = f"{player_name}正在玩{game_name}" if event_type == 'start' else f"{player_name}停止了玩{game_name}"
            meme_path = await self._create_meme_notification(avatar_url, meme_text)
            if meme_path:
                chain = [Image.fromFileSystem(meme_path)]

        # 如果Meme API失败或未启用，则使用html_render
        if not chain:
            try:
                if avatar_url:
                    html_template = f"""
                    <div style="display: flex; align-items: center; font-family: sans-serif; padding: 10px; border-radius: 5px; background-color: #f0f2f5;">
                        <img src="{avatar_url}" style="width: 50px; height: 50px; border-radius: 50%; margin-right: 15px;">
                        <p style="font-size: 16px; margin: 0;">{notification_message.replace('\\n', '<br>')}</p>
                    </div>
                    """
                    image_path = await self.html_render(html_template, {}, return_url=False)
                    chain = [Image.fromFileSystem(image_path)]
                else:
                    chain = [Plain(notification_message)]
            except Exception as e:
                logger.error(f"使用html_render生成通知图片时出错: {e}")
                chain = [Plain(notification_message)] # 最终回退到纯文本

        try:
            await self.context.send_message(rule["notification_uid"], chain)
            logger.info(f"已向 {rule['notification_uid']} 发送通知。")
        except Exception as e:
            logger.error(f"发送通知失败: {e}")

    async def _create_meme_notification(self, avatar_url: str, text: str) -> str | None:
        """使用Meme API创建通知图片，成功返回图片路径，失败返回None。"""
        try:
            # 1. 上传头像图片
            async with httpx.AsyncClient(proxies=self.proxy_url or None) as client:
                upload_payload = {"type": "url", "url": avatar_url}
                resp_upload = await client.post(f"{self.meme_api_base_url}/image/upload", json=upload_payload, timeout=20)
                resp_upload.raise_for_status()
                avatar_image_id = resp_upload.json().get("image_id")
                if not avatar_image_id:
                    logger.error("Meme API: 上传头像失败，未返回image_id。")
                    return None

                # 2. 生成Meme
                meme_payload = {
                    "images": [{"name": "avatar", "id": avatar_image_id}],
                    "texts": [text],
                }
                resp_meme = await client.post(f"{self.meme_api_base_url}/memes/steam_message", json=meme_payload, timeout=20)
                resp_meme.raise_for_status()
                meme_image_id = resp_meme.json().get("image_id")
                if not meme_image_id:
                    logger.error("Meme API: 生成Meme失败，未返回image_id。")
                    return None

                # 3. 下载Meme图片
                resp_download = await client.get(f"{self.meme_api_base_url}/image/{meme_image_id}", timeout=20)
                resp_download.raise_for_status()

                # 4. 保存到文件
                meme_filename = f"meme_{uuid.uuid4()}.gif"
                save_path = self._get_data_filepath(meme_filename)
                with open(save_path, "wb") as f:
                    f.write(resp_download.content)
                
                logger.info(f"Meme通知图片已生成并保存到: {save_path}")
                return save_path

        except httpx.RequestError as e:
            logger.error(f"请求Meme API时出错: {e}")
            return None
        except Exception as e:
            logger.error(f"处理Meme通知时发生未知错误: {e}", exc_info=True)
            return None

    # --- LLM 与格式化辅助函数 ---
    def _add_event_to_history(self, rule: dict, event_type: str, game_name: str, duration: int):
        """向规则的事件历史中添加一个新事件，并保持列表长度。"""
        history = rule.get("event_history", [])
        history.insert(0, {
            "timestamp": int(time.time()),
            "type": event_type,
            "game": game_name,
            "duration": duration
        })
        # 保持历史记录不超过10条
        rule["event_history"] = history[:10]

    def _format_duration(self, seconds: int) -> str:
        """将秒数格式化为易读的字符串。"""
        if not isinstance(seconds, (int, float)) or seconds < 0:
            return "未知时长"
        if seconds < 60:
            return f"{int(seconds)}秒"
        elif seconds < 3600:
            return f"{int(seconds // 60)}分钟"
        else:
            hours = int(seconds // 3600)
            minutes = int((seconds % 3600) // 60)
            return f"{hours}小时{minutes}分钟"

    def _build_llm_prompt(self, rule: dict, player_name: str, event_type: str, game_name: str, duration_seconds: int) -> str:
        """构建用于生成风趣评论的LLM Prompt。"""
        history_str = ""
        now = int(time.time())
        for event in rule.get("event_history", []):
            time_ago = self._format_duration(now - event['timestamp'])
            action = "开始了" if event['type'] == 'start' else f"停止了 (玩了{self._format_duration(event['duration'])})"
            history_str += f"- {time_ago}前: {action}《{event['game']}》\n"

        latest_event_desc = f"刚刚{'开始了' if event_type == 'start' else '结束了'}游戏"
        if event_type == 'stop':
            latest_event_desc += f"，本次游玩时长: {self._format_duration(duration_seconds)}"

        total_today_str = self._format_duration(rule.get('total_playtime_today', 0))
        
        prompt = f"""你是一个风趣幽默的AI助手，负责评论朋友的游戏动态。请根据以下信息，为你的朋友 {player_name} 生成一句简短、适合在聊天软件中发送的评论。请自由发挥，可以吐槽、可以鼓励、可以提醒，也可以结合时事或游戏本身玩梗。只返回评论本身，不要超过40个字。

**最新事件:**
- **类型:** {latest_event_desc}
- **游戏名称:** 《{game_name}》
- **当前时间:** {time.strftime('%Y-%m-%d %H:%M:%S')}

**最近活动历史 (从新到旧):**
```
{history_str.strip()}
```

**今日统计:**
- **累计游戏时长:** {total_today_str}
"""
        return prompt

    async def _notify_admins(self, message: str):
        """向配置文件中指定的所有管理员发送通知。"""
        admin_uids = self.config.get("admin_notification_uids", [])
        if not admin_uids:
            logger.warning("发生了一个需要管理员注意的事件，但未配置 'admin_notification_uids'。")
            return
        
        for uid in admin_uids:
            try:
                await self.context.send_message(uid, [Plain(message)])
                logger.info(f"已向管理员 ({uid}) 发送错误通知。")
            except Exception as e:
                logger.error(f"向管理员 ({uid}) 发送通知失败: {e}")

    # --- 插件终止 ---
    async def terminate(self):
        if self.friend_monitor_task and not self.friend_monitor_task.done():
            self.friend_monitor_task.cancel()
        if self.individual_monitor_task and not self.individual_monitor_task.done():
            self.individual_monitor_task.cancel()
        if self.cache_resolver_task and not self.cache_resolver_task.done():
            self.cache_resolver_task.cancel()
        # 安全关闭 aiohttp session
        if self.steam_client:
            await self.steam_client.close()

    # --- 用户指令组 ---
    @filter.command_group("steam", alias={"steam监控"})
    def steam_cmd_group(self):
        """管理Steam游戏状态监控"""
        pass

    @steam_cmd_group.command("add", alias={"添加"})
    async def add_rule(self, event: AstrMessageEvent, steam_profile_input: str, game_name_to_monitor: str = None, notification_uid: str = None):
        """
        添加一条新的监控规则。
        - 插件会自动判断用户是否在好友列表，并设置相应监控模式。
        - 默认监控当前会话，也可通过 notification_uid 指定其他会话。
        """
        if notification_uid is None:
            notification_uid = event.unified_msg_origin

        yield event.plain_result(f"正在解析Steam资料并添加规则...")
        steam_id64, display_name, avatar_url = await self.steam_client.resolve_steam_url_to_id64(steam_profile_input, self.steam_id_cache)
        self._save_steam_id_cache()

        if not steam_id64:
            yield event.plain_result(f"❌ 错误：无法解析Steam个人资料 '{steam_profile_input}'。")
            return

        display_name = display_name or f"用户({steam_id64})"
        if any(r["target_steam_id64"] == steam_id64 and r["notification_uid"] == notification_uid for r in self.monitoring_rules):
            yield event.plain_result(f"⚠️ 警告：该玩家 ({display_name}) 已在该会话被监控。")
            return

        # 根据好友列表缓存自动判断监控类型
        monitoring_type = "friend_list" if steam_id64 in self.friend_list_cache else "individual"
        
        if monitoring_type == "friend_list" and not self.target_profile_url:
             yield event.plain_result("❌ 错误：未在插件配置中设置“目标好友列表URL”，无法添加好友列表监控。")
             return

        new_rule = {
            "rule_id": str(uuid.uuid4()), "notification_uid": notification_uid,
            "original_input": steam_profile_input, "target_steam_id64": steam_id64,
            "target_steam_display_name": display_name, "target_steam_avatar_url": avatar_url,
            "game_name_to_monitor": game_name_to_monitor, "monitoring_type": monitoring_type,
            "last_known_game_name": None, "last_state_change_timestamp": int(time.time()),
            "event_history": [], "total_playtime_today": 0,
            "last_reset_day": time.strftime("%Y-%m-%d"),
            "current_game_start_timestamp": 0
        }
        self.monitoring_rules.append(new_rule)
        self._save_monitoring_rules()
        yield event.plain_result(f"✅ 成功添加监控规则！\n"
                                f"会话ID: {notification_uid}\n"
                                f"类型：{'单独监控' if monitoring_type == 'individual' else '好友列表'}\n"
                                f"玩家：{display_name} ({steam_id64})\n"
                                f"游戏：{game_name_to_monitor or '任意游戏'}")

    @steam_cmd_group.command("list", alias={"列表"})
    async def list_rules(self, event: AstrMessageEvent, notification_uid: str = None):
        """列出当前会话或所有的监控规则。"""
        rules_to_list = [r for r in self.monitoring_rules if notification_uid is None or r["notification_uid"] == notification_uid]
        if not rules_to_list:
            yield event.plain_result("没有找到任何监控规则。")
            return
        
        response_parts = ["📚 当前监控规则列表："]
        for i, rule in enumerate(rules_to_list):
            game_name = rule.get("last_known_game_name")
            is_online = bool(rule.get("target_steam_avatar_url"))
            
            if game_name:
                status_text = f"🎮 正在玩:《{game_name}》"
            elif is_online:
                status_text = "🟢 在线"
            else:
                status_text = "⚫️ 离线/未知"

            rule_type = "单独" if rule.get("monitoring_type") == "individual" else "好友列表"
            response_parts.append(f"\n--- 规则 {i+1} ({rule_type}) ---\n"
                                  f"ID: {rule['rule_id'][:8]}\n"
                                  f"玩家: {rule.get('target_steam_display_name', '未知')} ({rule['target_steam_id64']})\n"
                                  f"游戏: {rule.get('game_name_to_monitor') or '任意游戏'}\n"
                                  f"状态: {status_text}")
        yield event.plain_result("".join(response_parts))

    @steam_cmd_group.command("remove", alias={"删除"})
    async def remove_rule(self, event: AstrMessageEvent, rule_id_prefix: str):
        """根据ID前缀删除一个监控规则。"""
        rule_to_remove = next((r for r in self.monitoring_rules if r['rule_id'].startswith(rule_id_prefix)), None)
        if rule_to_remove:
            self.monitoring_rules.remove(rule_to_remove)
            self._save_monitoring_rules()
            yield event.plain_result(f"✅ 已删除规则 ID 为 {rule_to_remove['rule_id']} 的监控。")
        else:
            yield event.plain_result(f"❌ 未找到 ID 前缀为 '{rule_id_prefix}' 的规则。")

    @steam_cmd_group.command("update_cookies", alias={"更新cookie"})
    async def update_cookies(self, event: AstrMessageEvent, secure_cookie: str, session_id: str):
        """更新并保存用于登录的Steam Cookie。"""
        self.config["steam_login_secure_cookie"] = secure_cookie
        self.config["session_id_cookie"] = session_id
        self.config.save_config()
        self.steam_client = SteamClient(
            steam_login_secure_cookie=secure_cookie,
            session_id_cookie=session_id,
            proxy_url=self.proxy_url
        )
        yield event.plain_result("✅ Cookie 已成功更新并保存。")

    @steam_cmd_group.command("force_refresh", alias={"强制刷新"})
    async def force_refresh(self, event: AstrMessageEvent):
        """立即强制刷新所有监控规则并检查状态。"""
        yield event.plain_result("正在强制刷新所有监控规则...")
        try:
            # 强制刷新好友列表
            friend_rules = [r for r in self.monitoring_rules if r.get("monitoring_type") == "friend_list"]
            if friend_rules and self.target_profile_url:
                logger.info("强制刷新好友列表...")
                html_content, _ = await self.steam_client.fetch_html(self.target_profile_url.strip('/') + '/friends/')
                if isinstance(html_content, str):
                    self.friend_list_states = self.steam_client.extract_friends_game_status_from_html(html_content)
                    self.friend_list_cache = set(self.friend_list_states.keys())
                
                friend_tasks = [self._process_rule_update(rule, self.friend_list_states.get(rule["target_steam_id64"])) for rule in friend_rules]
                await asyncio.gather(*friend_tasks)

            # 强制刷新单独监控
            individual_rules = [r for r in self.monitoring_rules if r.get("monitoring_type") == "individual"]
            if individual_rules:
                logger.info("强制刷新单独监控规则...")
                individual_tasks = []
                for rule in individual_rules:
                    current_info = await self.steam_client.get_single_player_status(rule["target_steam_id64"])
                    individual_tasks.append(self._process_rule_update(rule, current_info))
                await asyncio.gather(*individual_tasks)

            self._save_monitoring_rules()
            yield event.plain_result("✅ 强制刷新完成！请留意状态变更通知。")
        except Exception as e:
            logger.error(f"强制刷新失败: {e}", exc_info=True)
            yield event.plain_result(f"❌ 强制刷新时发生错误: {e}")
