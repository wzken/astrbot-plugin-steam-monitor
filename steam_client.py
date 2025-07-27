import aiohttp
import asyncio
from bs4 import BeautifulSoup
from urllib.parse import urlparse
from astrbot.api import logger

STEAM_PROFILE_BASE_URL = "https://steamcommunity.com/profiles/"
STEAM_CUSTOM_ID_BASE_URL = "https://steamcommunity.com/id/"

class SteamClient:
    def __init__(self, steam_login_secure_cookie: str = None, session_id_cookie: str = None, proxy_url: str = None):
        self.steam_login_secure_cookie = steam_login_secure_cookie
        self.session_id_cookie = session_id_cookie
        self.proxy_url = proxy_url
        self._session = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            cookies = {}
            if self.steam_login_secure_cookie and self.session_id_cookie:
                cookies = {
                    "steamLoginSecure": self.steam_login_secure_cookie,
                    "sessionid": self.session_id_cookie
                }
            self._session = aiohttp.ClientSession(cookies=cookies)
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
            logger.info("SteamClient aiohttp session closed.")

    async def fetch_html(self, url: str, cache_info: dict = None) -> tuple[str | int | None, dict]:
        """
        通用HTML抓取器，支持HTTP缓存头，带Cookie、代理和错误处理。
        :param url: 要抓取的URL
        :param cache_info: 包含 'etag' 和 'last_modified' 的字典
        :return: 一个元组 (响应内容, 新的缓存信息)。响应内容为str表示有新内容，为304表示无变化，为None表示出错。
        """
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7"
        }
        if cache_info:
            if 'etag' in cache_info and cache_info['etag']:
                headers['If-None-Match'] = cache_info['etag']
            if 'last_modified' in cache_info and cache_info['last_modified']:
                headers['If-Modified-Since'] = cache_info['last_modified']

        new_cache_info = {}
        try:
            session = await self._get_session()
            async with session.get(url, headers=headers, timeout=15, proxy=self.proxy_url if self.proxy_url else None) as response:
                if response.status == 304:
                    logger.debug(f"页面 {url} 未修改 (304 Not Modified)。")
                    return 304, cache_info

                new_cache_info['etag'] = response.headers.get('ETag')
                new_cache_info['last_modified'] = response.headers.get('Last-Modified')

                if response.status == 403:
                    logger.warning(f"访问 {url} 被拒绝 (403 Forbidden)。Cookie可能失效或目标隐私设置问题。")
                    return 403, new_cache_info
                
                response.raise_for_status()
                content = await response.text()
                return content, new_cache_info
        except aiohttp.ClientProxyConnectionError as e:
            logger.error(f"连接代理 {self.proxy_url} 失败: {e}")
            return None, new_cache_info
        except aiohttp.ClientError as e:
            logger.error(f"获取 {url} 失败: {e}")
            return None, new_cache_info
        except asyncio.TimeoutError:
            logger.error(f"连接 {url} 超时。")
            return None, new_cache_info
        except Exception as e:
            logger.error(f"获取 {url} 时发生未知错误: {e}")
            return None, new_cache_info

    def extract_player_name_from_html(self, html_content: str) -> str | None:
        """从个人资料HTML中提取玩家昵称。"""
        soup = BeautifulSoup(html_content, 'html.parser')
        name_span = soup.find('span', class_='actual_steamname')
        if name_span:
            return name_span.get_text(strip=True)
        name_div = soup.find('div', class_='friends_header_name')
        if name_div:
            return name_div.get_text(strip=True)
        return None

    def extract_avatar_from_profile_html(self, html_content: str) -> str | None:
        """从个人资料HTML中提取玩家头像URL。"""
        soup = BeautifulSoup(html_content, 'html.parser')
        avatar_div = soup.find('div', class_='playerAvatarAutoSizeInner')
        if avatar_div:
            img_tag = avatar_div.find('img')
            if img_tag and img_tag.has_attr('src'):
                return img_tag['src'].replace('_medium.jpg', '_full.jpg')
        avatar_div_alt = soup.find('div', class_='friends_header_avatar')
        if avatar_div_alt:
            img_tag = avatar_div_alt.find('img')
            if img_tag and img_tag.has_attr('src'):
                return img_tag['src'].replace('_medium.jpg', '_full.jpg')
        return None

    def extract_game_from_profile_html(self, html_content: str) -> str | None:
        """从单个个人资料HTML中提取当前正在玩的游戏名称。"""
        soup = BeautifulSoup(html_content, 'html.parser')
        game_name_div = soup.find('div', class_='profile_in_game_name')
        if game_name_div:
            return game_name_div.get_text(strip=True)
        return None

    async def get_single_player_status(self, steam_id64: str) -> dict | None:
        """获取单个玩家的个人资料并解析其状态"""
        profile_url = f"{STEAM_PROFILE_BASE_URL}{steam_id64}/"
        html_content, _ = await self.fetch_html(profile_url)
        if not isinstance(html_content, str):
            logger.warning(f"无法获取玩家 {steam_id64} 的个人资料页面。")
            return None
        
        game = self.extract_game_from_profile_html(html_content)
        name = self.extract_player_name_from_html(html_content)
        avatar_url = self.extract_avatar_from_profile_html(html_content)

        return {"name": name, "game": game, "avatar_url": avatar_url}

    def extract_friends_game_status_from_html(self, html_content: str) -> dict[str, dict]:
        """
        从好友页面HTML中提取所有好友的游戏状态、昵称和头像。
        返回: {id64: {"name": "...", "game": "..." or None, "avatar_url": "..." or None}}
        """
        soup = BeautifulSoup(html_content, 'html.parser')
        friend_status_map = {}
        friend_elements = soup.select('.friend_block_v2')

        for friend_elem in friend_elements:
            friend_id64 = friend_elem.get('data-steamid')
            if not friend_id64:
                continue

            friend_name_elem = friend_elem.find('div', class_='friend_block_content')
            friend_name = friend_name_elem.contents[0].strip() if friend_name_elem and friend_name_elem.contents else f"未知好友({friend_id64})"

            game_name_elem = friend_elem.find('span', class_='friend_game_link')
            current_game = game_name_elem.get_text(strip=True) if game_name_elem else None
            
            avatar_img_elem = friend_elem.find('div', class_='player_avatar').find('img')
            avatar_url = avatar_img_elem['src'].replace('_medium.jpg', '_full.jpg') if avatar_img_elem and avatar_img_elem.has_attr('src') else None

            friend_status_map[friend_id64] = {"name": friend_name, "game": current_game, "avatar_url": avatar_url}
        
        logger.debug(f"从好友页面解析到 {len(friend_status_map)} 个好友状态。")
        return friend_status_map

    async def resolve_steam_url_to_id64(self, steam_url_or_id: str, steam_id_cache: dict, force_re_resolve: bool = False) -> tuple[str | None, str | None, str | None]:
        """
        解析Steam URL或ID，返回(ID64, 玩家昵称, 头像URL)。
        使用缓存，如果需要则强制重解析。
        """
        parsed_url = urlparse(steam_url_or_id)
        if parsed_url.netloc == "steamcommunity.com" and (parsed_url.path.startswith("/id/") or parsed_url.path.startswith("/profiles/")):
            normalized_input = steam_url_or_id.rstrip('/')
        elif steam_url_or_id.isdigit() and len(steam_url_or_id) == 17 and steam_url_or_id.startswith('7656'):
             normalized_input = steam_url_or_id
        else:
            normalized_input = f"{STEAM_CUSTOM_ID_BASE_URL}{steam_url_or_id}"

        if not force_re_resolve and normalized_input in steam_id_cache:
            cached_data = steam_id_cache[normalized_input]
            logger.debug(f"从缓存获取 '{normalized_input}' -> ID64: {cached_data['id64']}, Name: {cached_data['name']}, Avatar: {cached_data.get('avatar_url')}")
            return cached_data['id64'], cached_data['name'], cached_data.get('avatar_url')

        if normalized_input.isdigit() and len(normalized_input) == 17 and normalized_input.startswith('7656'):
            profile_url = f"{STEAM_PROFILE_BASE_URL}{normalized_input}/"
        else:
            profile_url = normalized_input

        logger.info(f"正在解析 Steam URL: {profile_url}")
        html_content, _ = await self.fetch_html(profile_url)
        if not isinstance(html_content, str):
            return None, None, None

        soup = BeautifulSoup(html_content, 'html.parser')

        resolved_id64 = None
        meta_id64 = soup.find('meta', property='steamID64')
        if meta_id64 and 'content' in meta_id64.attrs:
            resolved_id64 = meta_id64['content']

        player_name = self.extract_player_name_from_html(html_content)
        avatar_url = self.extract_avatar_from_profile_html(html_content)
        
        if resolved_id64:
            steam_id_cache[normalized_input] = {
                "id64": resolved_id64, 
                "name": player_name if player_name else "未知玩家", 
                "avatar_url": avatar_url,
                "timestamp": int(time.time())
            }
            logger.info(f"成功解析 '{normalized_input}' -> ID64: {resolved_id64}, Name: {player_name}, Avatar: {avatar_url}")
            return resolved_id64, player_name, avatar_url
        
        logger.warning(f"无法从 {profile_url} 解析出Steam ID64。")
        return None, None, None
