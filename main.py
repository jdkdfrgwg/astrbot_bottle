import json
import time
import aiohttp
import asyncio
from pathlib import Path
from typing import List, Optional, Dict

# 严格按官方规范导入模块（参考摘要1、3、5的导入示例）
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.event.filter import (
    PermissionType, 
    PlatformAdapterType, 
    EventMessageType
)
from astrbot.api.star import Context, Star, register
from astrbot.api import logger as astr_logger  # 官方日志接口（禁止logging模块）
from astrbot.api.message_components import Comp  # 消息链组件（参考摘要1的ComponentTypes）
from astrbot.api.config import AstrBotConfig  # 配置对象（v3.4.15+支持）


# 插件注册（官方强制：@register装饰器，参考摘要1的最小实例）
@register(
    name="小云鲨漂流瓶",
    author="开发者名称",
    description="基于小云鲨API的漂流瓶插件，支持捡/投瓶、次数限制、管理员管控",
    version="1.3.0",
    repo_url="https://github.com/你的仓库地址/astrbot_plugin_drift_bottle"
)
class DriftBottlePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.logger = astr_logger
        self.config = config  # 插件配置（来自_conf_schema.json，参考摘要3的配置处理）
        self.session: Optional[aiohttp.ClientSession] = None  # 异步会话（参考摘要4的异步任务）
        
        # 1. 初始化异步会话（无Context.loop依赖，兼容v4.x，参考摘要2的Context结构）
        self._init_aiohttp_session()
        
        # 2. 初始化数据存储路径（官方要求：存data目录，避免插件更新丢失，参考摘要1原则）
        self.data_dir = Path(self.context.get_config().data_dir) / "drift_bottle"
        self.data_dir.mkdir(exist_ok=True, parents=True)
        self.user_data_path = self.data_dir / "user_data.json"
        
        # 3. 异步初始化配置与用户数据（避免阻塞初始化流程，参考摘要4的异步初始化）
        self.loop = asyncio.get_event_loop()
        self.loop.run_until_complete(self._init_async_resources())

    def _init_aiohttp_session(self):
        """初始化aiohttp异步会话（符合官方异步规范，参考摘要1原则）"""
        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self.config.get("api_timeout", 8)),
            headers={"User-Agent": "AstrBot-DriftBottle/1.3.0"}
        )

    async def _init_async_resources(self):
        """异步初始化配置与用户数据（参考摘要6的异步数据库操作）"""
        await self._init_config()
        self.user_data = await self._load_user_data()
        self.logger.info("小云鲨漂流瓶插件：异步资源初始化完成")

    async def _init_config(self):
        """加载并补全插件配置（使用AstrBotConfig原生方法，参考摘要3的配置处理）"""
        # 读取配置（缺省值兜底，避免KeyError）
        self.api_key = self.config.get("api_key", "").strip()
        self.daily_pick_limit = max(1, self.config.get("daily_pick_limit", 10))
        self.daily_throw_limit = max(1, self.config.get("daily_throw_limit", 5))
        self.api_timeout = max(3, self.config.get("api_timeout", 8))
        
        # 补全缺失配置并保存（官方save_config方法，参考摘要1的配置管理）
        if self.config.get("daily_pick_limit") != self.daily_pick_limit:
            self.config["daily_pick_limit"] = self.daily_pick_limit
            await asyncio.to_thread(self.config.save_config)

    def _get_default_user_data(self) -> Dict:
        """默认用户数据结构（每日重置，参考摘要4的数据存储）"""
        return {
            "last_reset_date": time.strftime("%Y-%m-%d"),
            "users": {}  # 格式：{QQ号: {"pick": 已捡次数, "throw": 已投次数}}
        }

    # ===================== 异步文件操作（避免阻塞事件循环）=====================
    async def _load_user_data(self) -> Dict:
        """异步加载用户数据（用asyncio.to_thread包装同步操作，参考摘要6的异步处理）"""
        try:
            if not self.user_data_path.exists():
                default_data = self._get_default_user_data()
                await asyncio.to_thread(self._save_user_data_sync, default_data)
                return default_data
            
            # 同步读取包装为异步（避免阻塞事件循环，参考摘要1原则）
            data = await asyncio.to_thread(self._load_user_data_sync)
            # 兼容旧数据格式（防止插件升级后数据异常）
            if "last_reset_date" not in data:
                data = self._get_default_user_data()
                await asyncio.to_thread(self._save_user_data_sync, data)
            return data
        except Exception as e:
            self.logger.error(f"加载用户数据失败：{str(e)}")
            default_data = self._get_default_user_data()
            await asyncio.to_thread(self._save_user_data_sync, default_data)
            return default_data

    def _load_user_data_sync(self) -> Dict:
        """同步读取用户数据（仅内部调用，参考摘要5的文件操作）"""
        with open(self.user_data_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _save_user_data_sync(self, data: Dict):
        """同步保存用户数据（仅内部调用，参考摘要5的文件操作）"""
        with open(self.user_data_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    async def _save_user_data(self, data: Dict):
        """异步保存用户数据（参考摘要6的异步数据库操作）"""
        await asyncio.to_thread(self._save_user_data_sync, data)

    # ===================== API调用与数据处理 =====================
    async def _check_reset_data(self):
        """异步检查跨日数据重置（参考摘要4的定时任务逻辑）"""
        today = time.strftime("%Y-%m-%d")
        if self.user_data["last_reset_date"] != today:
            self.user_data["last_reset_date"] = today
            self.user_data["users"] = {}
            await self._save_user_data(self.user_data)
            self.logger.info("漂流瓶用户数据：跨日自动重置完成")

    async def _get_user_count(self, qq_id: str) -> Dict:
        """异步获取用户今日次数（无则初始化，参考摘要6的用户数据管理）"""
        await self._check_reset_data()
        if qq_id not in self.user_data["users"]:
            self.user_data["users"][qq_id] = {"pick": 0, "throw": 0}
            await self._save_user_data(self.user_data)
        return self.user_data["users"][qq_id]

    async def _call_bottle_api(self, action: str, qq_id: str, content: str = "", image_url: str = "") -> str:
        """异步调用小云鲨API（符合官方异步网络规范，参考摘要1原则）"""
        api_url = "http://rnrqsj.top/api/a/index.php"
        params = {
            "id": qq_id,
            "key": self.api_key,
            "type": "text"  # 优先用text格式，解析简单
        }

        # 投瓶时添加文字/图片参数（参考摘要1的消息链组件）
        if action == "throw":
            if content:
                params["character"] = content
            if image_url:
                params["url"] = image_url

        try:
            async with self.session.get(api_url, params=params) as resp:
                resp.raise_for_status()  # 触发4xx/5xx错误（参考摘要6的错误处理）
                result = await resp.text()
                result = result.strip()
                # 处理API错误（text格式错误以“错误：”开头）
                return result if not result.startswith("错误：") else f"API错误：{result[4:]}"
        except aiohttp.ClientTimeout:
            return f"请求超时（{self.api_timeout}秒）：API响应过慢"
        except aiohttp.ClientConnectionError:
            return "连接失败：API服务器可能离线"
        except aiohttp.ClientResponseError as e:
            self.logger.error(f"API HTTP错误（QQ：{qq_id}）：{e.status} {e.message}")
            return f"请求错误：HTTP {e.status}"
        except Exception as e:
            self.logger.error(f"API调用异常（QQ：{qq_id}）：{str(e)[:50]}")
            return f"调用异常：{str(e)[:30]}..."

    def _extract_image_url(self, event: AstrMessageEvent) -> Optional[str]:
        """从消息链提取第一张图片URL（官方消息链解析方式，参考摘要1的ComponentTypes）"""
        for comp in event.message_obj.message:
            if isinstance(comp, Comp.Image):
                # 过滤非HTTP链接（适配钉钉等平台限制，参考摘要1的平台适配矩阵）
                if comp.url.startswith(("http://", "https://")):
                    return comp.url
        return None

    # ===================== 普通用户指令（带别名+平台过滤）=====================
    @filter.command("捡漂流瓶", alias={"捡瓶"})  # 官方v3.4.28+支持别名（参考摘要1的指令别名）
    @filter.platform_adapter_type(PlatformAdapterType.AIOCQHTTP)  # 仅QQ个人号（参考摘要5的平台过滤）
    @filter.event_message_type(EventMessageType.ALL)  # 支持群聊/私聊（参考摘要1的事件类型过滤）
    async def handle_pick(self, event: AstrMessageEvent):
        """捡漂流瓶指令：支持“捡漂流瓶”“捡瓶”（参考摘要1的指令示例）"""
        try:
            qq_id = event.get_sender_id()
            user_count = await self._get_user_count(qq_id)

            # 1. 次数限制校验
            if user_count["pick"] >= self.daily_pick_limit:
                yield event.plain_result(
                    f"今日捡瓶次数已用尽！\n每日上限：{self.daily_pick_limit}次\n明日0点自动重置"
                )
                return

            # 2. 调用API捡瓶
            api_result = await self._call_bottle_api("pick", qq_id)
            if api_result.startswith(("API错误", "请求", "调用")):
                yield event.plain_result(f"❌ 捡瓶失败：{api_result}")
                return

            # 3. 更新次数统计
            user_count["pick"] += 1
            await self._save_user_data(self.user_data)
            remaining = self.daily_pick_limit - user_count["pick"]

            # 4. 构建回复（含图片组件，参考摘要1的消息链示例）
            reply_chain = [
                Comp.Plain(text=f"✅ 捡到漂流瓶啦！\n{api_result}\n\n📊 今日剩余：{remaining}次")
            ]
            # 提取API返回的图片URL（若有）
            for line in api_result.split("\n"):
                if line.startswith("图片URL:"):
                    img_url = line.split(":", 1)[1].strip()
                    if img_url != "无" and img_url.startswith(("http://", "https://")):
                        reply_chain.append(Comp.Image.fromURL(img_url))
                    break

            yield event.chain_result(reply_chain)
        except Exception as e:
            self.logger.error(f"捡瓶指令异常（QQ：{qq_id}）：{str(e)}")
            yield event.plain_result(f"❌ 操作失败：{str(e)[:30]}...")

    @filter.command("投漂流瓶", alias={"投瓶"})
    @filter.platform_adapter_type(PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(EventMessageType.ALL)
    async def handle_throw(self, event: AstrMessageEvent, content: str = ""):
        """投漂流瓶指令：支持“投漂流瓶 文字”（可附带图片）（参考摘要1的带参指令）"""
        try:
            qq_id = event.get_sender_id()
            user_count = await self._get_user_count(qq_id)

            # 1. 次数限制校验
            if user_count["throw"] >= self.daily_throw_limit:
                yield event.plain_result(
                    f"今日投瓶次数已用尽！\n每日上限：{self.daily_throw_limit}次\n明日0点自动重置"
                )
                return

            # 2. 内容校验（文字/图片至少其一，参考摘要1的消息链组件）
            image_url = self._extract_image_url(event)
            if not content and not image_url:
                yield event.plain_result(
                    "❌ 投放失败：需携带文字内容或图片！\n示例：\n投漂流瓶 今天天气真好～\n（发送时可附带图片）"
                )
                return

            # 3. 调用API投瓶
            api_result = await self._call_bottle_api("throw", qq_id, content, image_url)
            if api_result.startswith(("API错误", "请求", "调用")):
                yield event.plain_result(f"❌ 投瓶失败：{api_result}")
                return

            # 4. 更新次数统计
            user_count["throw"] += 1
            await self._save_user_data(self.user_data)
            remaining = self.daily_throw_limit - user_count["throw"]

            # 5. 构建回复（含用户投放的图片，参考摘要1的消息链示例）
            reply_chain = [
                Comp.Plain(text=f"✅ 漂流瓶投放成功！\n{api_result}\n\n📊 今日剩余：{remaining}次")
            ]
            if image_url:
                reply_chain.extend([
                    Comp.Plain(text="\n你投放的图片："),
                    Comp.Image.fromURL(image_url)
                ])

            yield event.chain_result(reply_chain)
        except Exception as e:
            self.logger.error(f"投瓶指令异常（QQ：{qq_id}）：{str(e)}")
            yield event.plain_result(f"❌ 操作失败：{str(e)[:30]}...")

    @filter.command("我的漂流瓶", alias={"漂流瓶统计"})
    @filter.platform_adapter_type(PlatformAdapterType.AIOCQHTTP)
    async def handle_my_stats(self, event: AstrMessageEvent):
        """查询个人今日漂流瓶统计（参考摘要4的用户数据展示）"""
        try:
            qq_id = event.get_sender_id()
            user_count = await self._get_user_count(qq_id)

            reply = (
                f"📊 你的今日漂流瓶统计\n"
                f"✅ 已捡瓶：{user_count['pick']}/{self.daily_pick_limit}次（剩余{self.daily_pick_limit - user_count['pick']}）\n"
                f"✅ 已投瓶：{user_count['throw']}/{self.daily_throw_limit}次（剩余{self.daily_throw_limit - user_count['throw']}）\n"
                f"📌 次数每日0点自动重置～"
            )
            yield event.plain_result(reply)
        except Exception as e:
            self.logger.error(f"统计指令异常（QQ：{qq_id}）：{str(e)}")
            yield event.plain_result(f"❌ 查询失败：{str(e)[:30]}...")

    # ===================== 管理员指令组（权限控制）=====================
    @filter.command_group("漂流瓶管理")  # 指令组（参考摘要1的指令组示例）
    @filter.permission_type(PermissionType.ADMIN)  # 仅管理员可访问（参考摘要1的管理员指令）
    @filter.platform_adapter_type(PlatformAdapterType.AIOCQHTTP)
    def drift_admin(self):
        """管理员指令组：查询/重置用户次数、全局统计（指令组函数无需实现逻辑）"""
        pass

    @drift_admin.command("查询")
    async def admin_query(self, event: AstrMessageEvent, target_qq: str):
        """管理员查询用户次数：漂流瓶管理 查询 123456789（参考摘要6的管理员功能）"""
        try:
            target_qq = target_qq.strip()
            user_count = self.user_data["users"].get(target_qq, {"pick": 0, "throw": 0})

            yield event.plain_result(
                f"🔍 【管理员查询】QQ{target_qq}今日统计\n"
                f"✅ 已捡瓶：{user_count['pick']}/{self.daily_pick_limit}次（剩余{self.daily_pick_limit - user_count['pick']}）\n"
                f"✅ 已投瓶：{user_count['throw']}/{self.daily_throw_limit}次（剩余{self.daily_throw_limit - user_count['throw']}）"
            )
        except Exception as e:
            self.logger.error(f"管理员查询异常：{str(e)}")
            yield event.plain_result(f"❌ 查询失败：{str(e)[:30]}...")

    @drift_admin.command("重置")
    async def admin_reset(self, event: AstrMessageEvent, target_qq: str):
        """管理员重置用户次数：漂流瓶管理 重置 123456789（参考摘要6的管理员功能）"""
        try:
            target_qq = target_qq.strip()
            if target_qq in self.user_data["users"]:
                self.user_data["users"][target_qq] = {"pick": 0, "throw": 0}
                await self._save_user_data(self.user_data)
                yield event.plain_result(f"✅ 已重置QQ{target_qq}的今日捡/投瓶次数！")
            else:
                yield event.plain_result(f"ℹ️ QQ{target_qq}今日未操作，无需重置～")
        except Exception as e:
            self.logger.error(f"管理员重置异常：{str(e)}")
            yield event.plain_result(f"❌ 重置失败：{str(e)[:30]}...")

    @drift_admin.command("全局统计")
    async def admin_global(self, event: AstrMessageEvent):
        """管理员全局统计：漂流瓶管理 全局统计（参考摘要4的全局数据统计）"""
        try:
            total_users = len(self.user_data["users"])
            total_pick = sum(u["pick"] for u in self.user_data["users"].values())
            total_throw = sum(u["throw"] for u in self.user_data["users"].values())

            yield event.plain_result(
                f"📊 【管理员全局统计】今日数据\n"
                f"👥 参与用户：{total_users}人\n"
                f"✅ 总捡瓶：{total_pick}次\n"
                f"✅ 总投瓶：{total_throw}次\n"
                f"📌 上限：捡{self.daily_pick_limit}次/人，投{self.daily_throw_limit}次/人"
            )
        except Exception as e:
            self.logger.error(f"管理员全局统计异常：{str(e)}")
            yield event.plain_result(f"❌ 统计失败：{str(e)[:30]}...")

    # ===================== 插件生命周期（官方强制实现）=====================
    async def terminate(self):
        """插件卸载/停用时释放资源（官方要求，参考摘要1的terminate示例）"""
        # 1. 关闭aiohttp异步会话（参考摘要4的资源释放）
        if self.session and not self.session.closed:
            await self.session.close()
            self.logger.info("漂流瓶插件：aiohttp会话已关闭")
        
        # 2. 保存最后一次用户数据（参考摘要6的异步数据保存）
        await self._save_user_data(self.user_data)
        self.logger.info("漂流瓶插件：资源释放完成，已停止运行")