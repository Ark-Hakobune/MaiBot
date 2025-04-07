#Programmable Friendly Conversationalist
#Prefrontal cortex
import datetime
import asyncio
from typing import List, Optional, Dict, Any, Tuple, Literal, Set
from enum import Enum
from src.common.logger import get_module_logger
from ..chat.chat_stream import ChatStream
from ..message.message_base import UserInfo, Seg
from ..chat.message import Message
from ..models.utils_model import LLM_request
from ..config.config import global_config
from src.plugins.chat.message import MessageSending
from src.plugins.chat.chat_stream import chat_manager
from ..message.api import global_api
from ..storage.storage import MessageStorage
from .chat_observer import ChatObserver
from .pfc_KnowledgeFetcher import KnowledgeFetcher
from .reply_checker import ReplyChecker
from .pfc_utils import get_items_from_json
from src.individuality.individuality import Individuality
from .chat_states import NotificationHandler, Notification, NotificationType
import time
from dataclasses import dataclass, field

logger = get_module_logger("pfc")


class ConversationState(Enum):
    """对话状态"""
    INIT = "初始化"
    RETHINKING = "重新思考"
    ANALYZING = "分析历史"
    PLANNING = "规划目标"
    GENERATING = "生成回复"
    CHECKING = "检查回复"
    SENDING = "发送消息"
    WAITING = "等待"
    LISTENING = "倾听"
    ENDED = "结束"
    JUDGING = "判断"


ActionType = Literal["direct_reply", "fetch_knowledge", "wait"]


@dataclass
class DecisionInfo:
    """决策信息类，用于收集和管理来自chat_observer的通知信息"""
    
    # 消息相关
    last_message_time: Optional[float] = None
    last_message_content: Optional[str] = None
    last_message_sender: Optional[str] = None
    new_messages_count: int = 0
    unprocessed_messages: List[Dict[str, Any]] = field(default_factory=list)
    
    # 对话状态
    is_cold_chat: bool = False
    cold_chat_duration: float = 0.0
    last_bot_speak_time: Optional[float] = None
    last_user_speak_time: Optional[float] = None
    
    # 对话参与者
    active_users: Set[str] = field(default_factory=set)
    bot_id: str = field(default="")
    
    def update_from_message(self, message: Dict[str, Any]):
        """从消息更新信息
        
        Args:
            message: 消息数据
        """
        self.last_message_time = message["time"]
        self.last_message_content = message.get("processed_plain_text", "")
        
        user_info = UserInfo.from_dict(message.get("user_info", {}))
        self.last_message_sender = user_info.user_id
        
        if user_info.user_id == self.bot_id:
            self.last_bot_speak_time = message["time"]
        else:
            self.last_user_speak_time = message["time"]
            self.active_users.add(user_info.user_id)
        
        self.new_messages_count += 1
        self.unprocessed_messages.append(message)
    
    def update_cold_chat_status(self, is_cold: bool, current_time: float):
        """更新冷场状态
        
        Args:
            is_cold: 是否冷场
            current_time: 当前时间
        """
        self.is_cold_chat = is_cold
        if is_cold and self.last_message_time:
            self.cold_chat_duration = current_time - self.last_message_time
    
    def get_active_duration(self) -> float:
        """获取当前活跃时长
        
        Returns:
            float: 最后一条消息到现在的时长（秒）
        """
        if not self.last_message_time:
            return 0.0
        return time.time() - self.last_message_time
    
    def get_user_response_time(self) -> Optional[float]:
        """获取用户响应时间
        
        Returns:
            Optional[float]: 用户最后发言到现在的时长（秒），如果没有用户发言则返回None
        """
        if not self.last_user_speak_time:
            return None
        return time.time() - self.last_user_speak_time
    
    def get_bot_response_time(self) -> Optional[float]:
        """获取机器人响应时间
        
        Returns:
            Optional[float]: 机器人最后发言到现在的时长（秒），如果没有机器人发言则返回None
        """
        if not self.last_bot_speak_time:
            return None
        return time.time() - self.last_bot_speak_time
    
    def clear_unprocessed_messages(self):
        """清空未处理消息列表"""
        self.unprocessed_messages.clear()
        self.new_messages_count = 0


# Forward reference for type hints
DecisionInfoType = DecisionInfo


class ActionPlanner:
    """行动规划器"""
    
    def __init__(self, stream_id: str):
        self.llm = LLM_request(
            model=global_config.llm_normal,
            temperature=0.7,
            max_tokens=1000,
            request_type="action_planning"
        )
        self.personality_info = Individuality.get_instance().get_prompt(type = "personality", x_person = 2, level = 2)
        self.name = global_config.BOT_NICKNAME
        self.chat_observer = ChatObserver.get_instance(stream_id)
        
    async def plan(
        self, 
        goal: str, 
        method: str, 
        reasoning: str,
        action_history: List[Dict[str, str]] = None,
        decision_info: DecisionInfoType = None  # Use DecisionInfoType here
    ) -> Tuple[str, str]:
        """规划下一步行动
        
        Args:
            goal: 对话目标
            method: 实现方法
            reasoning: 目标原因
            action_history: 行动历史记录
            decision_info: 决策信息
            
        Returns:
            Tuple[str, str]: (行动类型, 行动原因)
        """
        # 构建提示词
        logger.debug(f"开始规划行动：当前目标: {goal}")
        
        # 获取最近20条消息
        messages = self.chat_observer.get_message_history(limit=20)
        chat_history_text = ""
        for msg in messages:
            time_str = datetime.datetime.fromtimestamp(msg["time"]).strftime("%H:%M:%S")
            user_info = UserInfo.from_dict(msg.get("user_info", {}))
            sender = user_info.user_nickname or f"用户{user_info.user_id}"
            if sender == self.name:
                sender = "你说"
            chat_history_text += f"{time_str},{sender}:{msg.get('processed_plain_text', '')}\n"
            
        personality_text = f"你的名字是{self.name}，{self.personality_info}"
        
        # 构建action历史文本
        action_history_text = ""
        if action_history and action_history[-1]['action'] == "direct_reply":
            action_history_text = "你刚刚发言回复了对方"
            
        # 构建决策信息文本
        decision_info_text = ""
        if decision_info:
            decision_info_text = "当前对话状态：\n"
            if decision_info.is_cold_chat:
                decision_info_text += f"对话处于冷场状态，已持续{int(decision_info.cold_chat_duration)}秒\n"
            
            if decision_info.new_messages_count > 0:
                decision_info_text += f"有{decision_info.new_messages_count}条新消息未处理\n"
                
            user_response_time = decision_info.get_user_response_time()
            if user_response_time:
                decision_info_text += f"距离用户上次发言已过去{int(user_response_time)}秒\n"
                
            bot_response_time = decision_info.get_bot_response_time()
            if bot_response_time:
                decision_info_text += f"距离你上次发言已过去{int(bot_response_time)}秒\n"
                
            if decision_info.active_users:
                decision_info_text += f"当前活跃用户数: {len(decision_info.active_users)}\n"

        prompt = f"""{personality_text}。现在你在参与一场QQ聊天，请分析以下内容，根据信息决定下一步行动：

当前对话目标：{goal}
实现该对话目标的方式：{method}
产生该对话目标的原因：{reasoning}

{decision_info_text}
{action_history_text}

最近的对话记录：
{chat_history_text}

请你接下去想想要你要做什么，可以发言，可以等待，可以倾听，可以调取知识。注意不同行动类型的要求，不要重复发言：
行动类型：
fetch_knowledge: 需要调取知识，当需要专业知识或特定信息时选择
wait: 当你做出了发言,对方尚未回复时等待对方的回复
listening: 倾听对方发言，当你认为对方发言尚未结束时采用
direct_reply: 不符合上述情况，回复对方，注意不要过多或者重复发言
rethink_goal: 重新思考对话目标，当发现对话目标不合适时选择，会重新思考对话目标
judge_conversation: 判断对话是否结束，当发现对话目标已经达到或者希望停止对话时选择，会判断对话是否结束

请以JSON格式输出，包含以下字段：
1. action: 行动类型，注意你之前的行为
2. reason: 选择该行动的原因，注意你之前的行为（简要解释）

注意：请严格按照JSON格式输出，不要包含任何其他内容。"""

        logger.debug(f"发送到LLM的提示词: {prompt}")
        try:
            content, _ = await self.llm.generate_response_async(prompt)
            logger.debug(f"LLM原始返回内容: {content}")
            
            # 使用简化函数提取JSON内容
            success, result = get_items_from_json(
                content,
                "action", "reason",
                default_values={"action": "direct_reply", "reason": "默认原因"}
            )
            
            if not success:
                return "direct_reply", "JSON解析失败，选择直接回复"
            
            action = result["action"]
            reason = result["reason"]
            
            # 验证action类型
            if action not in ["direct_reply", "fetch_knowledge", "wait", "listening", "rethink_goal", "judge_conversation"]:
                logger.warning(f"未知的行动类型: {action}，默认使用listening")
                action = "listening"
                
            logger.info(f"规划的行动: {action}")
            logger.info(f"行动原因: {reason}")
            return action, reason
            
        except Exception as e:
            logger.error(f"规划行动时出错: {str(e)}")
            return "direct_reply", "发生错误，选择直接回复"


class GoalAnalyzer:
    """对话目标分析器"""
    
    def __init__(self, stream_id: str):
        self.llm = LLM_request(
            model=global_config.llm_normal,
            temperature=0.7,
            max_tokens=1000,
            request_type="conversation_goal"
        )
        
        self.personality_info = Individuality.get_instance().get_prompt(type = "personality", x_person = 2, level = 2)
        self.name = global_config.BOT_NICKNAME
        self.nick_name = global_config.BOT_ALIAS_NAMES
        self.chat_observer = ChatObserver.get_instance(stream_id)
        
        # 多目标存储结构
        self.goals = []  # 存储多个目标
        self.max_goals = 3  # 同时保持的最大目标数量
        self.current_goal_and_reason = None

    async def analyze_goal(self) -> Tuple[str, str, str]:
        """分析对话历史并设定目标
        
        Args:
            chat_history: 聊天历史记录列表
            
        Returns:
            Tuple[str, str, str]: (目标, 方法, 原因)
        """
        max_retries = 3
        for retry in range(max_retries):
            try:
                # 构建提示词
                messages = self.chat_observer.get_message_history(limit=20)
                chat_history_text = ""
                for msg in messages:
                    time_str = datetime.datetime.fromtimestamp(msg["time"]).strftime("%H:%M:%S")
                    user_info = UserInfo.from_dict(msg.get("user_info", {}))
                    sender = user_info.user_nickname or f"用户{user_info.user_id}"
                    if sender == self.name:
                        sender = "你说"
                    chat_history_text += f"{time_str},{sender}:{msg.get('processed_plain_text', '')}\n"
                    
                personality_text = f"你的名字是{self.name}，{self.personality_info}"
                
                # 构建当前已有目标的文本
                existing_goals_text = ""
                if self.goals:
                    existing_goals_text = "当前已有的对话目标:\n"
                    for i, (goal, _, reason) in enumerate(self.goals):
                        existing_goals_text += f"{i+1}. 目标: {goal}, 原因: {reason}\n"
                    
                prompt = f"""{personality_text}。现在你在参与一场QQ聊天，请分析以下聊天记录，并根据你的性格特征确定多个明确的对话目标。
这些目标应该反映出对话的不同方面和意图。

{existing_goals_text}

聊天记录：
{chat_history_text}

请分析当前对话并确定最适合的对话目标。你可以：
1. 保持现有目标不变
2. 修改现有目标
3. 添加新目标
4. 删除不再相关的目标

请以JSON格式输出一个当前最主要的对话目标，包含以下字段：
1. goal: 对话目标（简短的一句话）
2. reasoning: 对话原因，为什么设定这个目标（简要解释）

输出格式示例：
{{
    "goal": "回答用户关于Python编程的具体问题",
    "reasoning": "用户提出了关于Python的技术问题，需要专业且准确的解答"
}}"""

                logger.debug(f"发送到LLM的提示词: {prompt}")
                content, _ = await self.llm.generate_response_async(prompt)
                logger.debug(f"LLM原始返回内容: {content}")
                
                # 使用简化函数提取JSON内容
                success, result = get_items_from_json(
                    content,
                    "goal", "reasoning",
                    required_types={"goal": str, "reasoning": str}
                )
                
                if not success:
                    logger.error(f"无法解析JSON，重试第{retry + 1}次")
                    continue
                    
                goal = result["goal"]
                reasoning = result["reasoning"]
                
                # 使用默认的方法
                method = "以友好的态度回应"
                
                # 更新目标列表
                await self._update_goals(goal, method, reasoning)
                
                # 返回当前最主要的目标
                if self.goals:
                    current_goal, current_method, current_reasoning = self.goals[0]
                    return current_goal, current_method, current_reasoning
                else:
                    return goal, method, reasoning
                
            except Exception as e:
                logger.error(f"分析对话目标时出错: {str(e)}，重试第{retry + 1}次")
                if retry == max_retries - 1:
                    return "保持友好的对话", "以友好的态度回应", "确保对话顺利进行"
                continue
        
        # 所有重试都失败后的默认返回
        return "保持友好的对话", "以友好的态度回应", "确保对话顺利进行"
    
    async def _update_goals(self, new_goal: str, method: str, reasoning: str):
        """更新目标列表
        
        Args:
            new_goal: 新的目标
            method: 实现目标的方法
            reasoning: 目标的原因
        """
        # 检查新目标是否与现有目标相似
        for i, (existing_goal, _, _) in enumerate(self.goals):
            if self._calculate_similarity(new_goal, existing_goal) > 0.7:  # 相似度阈值
                # 更新现有目标
                self.goals[i] = (new_goal, method, reasoning)
                # 将此目标移到列表前面（最主要的位置）
                self.goals.insert(0, self.goals.pop(i))
                return
        
        # 添加新目标到列表前面
        self.goals.insert(0, (new_goal, method, reasoning))
        
        # 限制目标数量
        if len(self.goals) > self.max_goals:
            self.goals.pop()  # 移除最老的目标
    
    def _calculate_similarity(self, goal1: str, goal2: str) -> float:
        """简单计算两个目标之间的相似度
        
        这里使用一个简单的实现，实际可以使用更复杂的文本相似度算法
        
        Args:
            goal1: 第一个目标
            goal2: 第二个目标
            
        Returns:
            float: 相似度得分 (0-1)
        """
        # 简单实现：检查重叠字数比例
        words1 = set(goal1)
        words2 = set(goal2)
        overlap = len(words1.intersection(words2))
        total = len(words1.union(words2))
        return overlap / total if total > 0 else 0
    
    async def get_all_goals(self) -> List[Tuple[str, str, str]]:
        """获取所有当前目标
        
        Returns:
            List[Tuple[str, str, str]]: 目标列表，每项为(目标, 方法, 原因)
        """
        return self.goals.copy()
    
    async def get_alternative_goals(self) -> List[Tuple[str, str, str]]:
        """获取除了当前主要目标外的其他备选目标
        
        Returns:
            List[Tuple[str, str, str]]: 备选目标列表
        """
        if len(self.goals) <= 1:
            return []
        return self.goals[1:].copy()

    async def analyze_conversation(self, goal, reasoning):
        messages = self.chat_observer.get_message_history()
        chat_history_text = ""
        for msg in messages:
            time_str = datetime.datetime.fromtimestamp(msg["time"]).strftime("%H:%M:%S")
            user_info = UserInfo.from_dict(msg.get("user_info", {}))
            sender = user_info.user_nickname or f"用户{user_info.user_id}"
            if sender == self.name:
                sender = "你说"
            chat_history_text += f"{time_str},{sender}:{msg.get('processed_plain_text', '')}\n"
            
        personality_text = f"你的名字是{self.name}，{self.personality_info}"
        
        prompt = f"""{personality_text}。现在你在参与一场QQ聊天，
        当前对话目标：{goal}
        产生该对话目标的原因：{reasoning}
        
        请分析以下聊天记录，并根据你的性格特征评估该目标是否已经达到，或者你是否希望停止该次对话。
        聊天记录：
        {chat_history_text}
        请以JSON格式输出，包含以下字段：
        1. goal_achieved: 对话目标是否已经达到（true/false）
        2. stop_conversation: 是否希望停止该次对话（true/false）
        3. reason: 为什么希望停止该次对话（简要解释）   

输出格式示例：
{{
    "goal_achieved": true,
    "stop_conversation": false,
    "reason": "用户已经得到了满意的回答，但我仍希望继续聊天"
}}"""
        logger.debug(f"发送到LLM的提示词: {prompt}")
        try:
            content, _ = await self.llm.generate_response_async(prompt)
            logger.debug(f"LLM原始返回内容: {content}")
            
            # 使用简化函数提取JSON内容
            success, result = get_items_from_json(
                content,
                "goal_achieved", "stop_conversation", "reason",
                required_types={
                    "goal_achieved": bool,
                    "stop_conversation": bool,
                    "reason": str
                }
            )
            
            if not success:
                return False, False, "确保对话顺利进行"
            
            # 如果当前目标达成，从目标列表中移除
            if result["goal_achieved"] and not result["stop_conversation"]:
                for i, (g, _, _) in enumerate(self.goals):
                    if g == goal:
                        self.goals.pop(i)
                        # 如果还有其他目标，不停止对话
                        if self.goals:
                            result["stop_conversation"] = False
                        break
            
            return result["goal_achieved"], result["stop_conversation"], result["reason"]
            
        except Exception as e:
            logger.error(f"分析对话目标时出错: {str(e)}")
            return False, False, "确保对话顺利进行"


class Waiter:
    """快 速 等 待"""
    def __init__(self, stream_id: str):
        self.chat_observer = ChatObserver.get_instance(stream_id)
        self.personality_info = Individuality.get_instance().get_prompt(type = "personality", x_person = 2, level = 2)
        self.name = global_config.BOT_NICKNAME
        
    async def wait(self) -> bool:
        """等待
        
        Returns:
            bool: 是否超时（True表示超时）
        """
        # 使用当前时间作为等待开始时间
        wait_start_time = time.time()
        self.chat_observer.waiting_start_time = wait_start_time  # 设置等待开始时间
        
        while True:
            # 检查是否有新消息
            if self.chat_observer.new_message_after(wait_start_time):
                logger.info("等待结束，收到新消息")
                return False
                
            # 检查是否超时
            if time.time() - wait_start_time > 300:
                logger.info("等待超过300秒，结束对话")
                return True
                
            await asyncio.sleep(1)
            logger.info("等待中...")

        
class ReplyGenerator:
    """回复生成器"""
    
    def __init__(self, stream_id: str):
        self.llm = LLM_request(
            model=global_config.llm_normal,
            temperature=0.7,
            max_tokens=300,
            request_type="reply_generation"
        )
        self.personality_info = Individuality.get_instance().get_prompt(type = "personality", x_person = 2, level = 2)
        self.name = global_config.BOT_NICKNAME
        self.chat_observer = ChatObserver.get_instance(stream_id)
        self.reply_checker = ReplyChecker(stream_id)
        
    async def generate(
        self,
        goal: str,
        chat_history: List[Message],
        knowledge_cache: Dict[str, str],
        previous_reply: Optional[str] = None,
        retry_count: int = 0
    ) -> str:
        """生成回复
        
        Args:
            goal: 对话目标
            chat_history: 聊天历史
            knowledge_cache: 知识缓存
            previous_reply: 上一次生成的回复（如果有）
            retry_count: 当前重试次数
            
        Returns:
            str: 生成的回复
        """
        # 构建提示词
        logger.debug(f"开始生成回复：当前目标: {goal}")
        self.chat_observer.trigger_update()  # 触发立即更新
        if not await self.chat_observer.wait_for_update():
            logger.warning("等待消息更新超时")
                
        messages = self.chat_observer.get_message_history(limit=20)
        chat_history_text = ""
        for msg in messages:
            time_str = datetime.datetime.fromtimestamp(msg["time"]).strftime("%H:%M:%S")
            user_info = UserInfo.from_dict(msg.get("user_info", {}))
            sender = user_info.user_nickname or f"用户{user_info.user_id}"
            if sender == self.name:
                sender = "你说"
            chat_history_text += f"{time_str},{sender}:{msg.get('processed_plain_text', '')}\n"
        
        # 整理知识缓存
        knowledge_text = ""
        if knowledge_cache:
            knowledge_text = "\n相关知识："
            if isinstance(knowledge_cache, dict):
                for _source, content in knowledge_cache.items():
                    knowledge_text += f"\n{content}"
            elif isinstance(knowledge_cache, list):
                for item in knowledge_cache:
                    knowledge_text += f"\n{item}"
                
        # 添加上一次生成的回复信息
        previous_reply_text = ""
        if previous_reply:
            previous_reply_text = f"\n上一次生成的回复（需要改进）：\n{previous_reply}"
        
        personality_text = f"你的名字是{self.name}，{self.personality_info}"
        
        prompt = f"""{personality_text}。现在你在参与一场QQ聊天，请根据以下信息生成回复：

当前对话目标：{goal}
{knowledge_text}
{previous_reply_text}
最近的聊天记录：
{chat_history_text}

请根据上述信息，以你的性格特征生成一个自然、得体的回复。回复应该：
1. 符合对话目标，以"你"的角度发言
2. 体现你的性格特征
3. 自然流畅，像正常聊天一样，简短
4. 适当利用相关知识，但不要生硬引用
{'5. 改进上一次回复中的问题' if previous_reply else ''}

请注意把握聊天内容，不要回复的太有条理，可以有个性。请分清"你"和对方说的话，不要把"你"说的话当做对方说的话，这是你自己说的话。
请你回复的平淡一些，简短一些，说中文，不要刻意突出自身学科背景，尽量不要说你说过的话 
请你注意不要输出多余内容(包括前后缀，冒号和引号，括号，表情等)，只输出回复内容。
不要输出多余内容(包括前后缀，冒号和引号，括号，表情包，at或 @等 )。

请直接输出回复内容，不需要任何额外格式。"""

        try:
            content, _ = await self.llm.generate_response_async(prompt)
            logger.info(f"生成的回复: {content}")
            # is_new = self.chat_observer.check()
            # logger.debug(f"再看一眼聊天记录，{'有' if is_new else '没有'}新消息")
            
            # 如果有新消息,重新生成回复
            # if is_new:
            #     logger.info("检测到新消息,重新生成回复")
            #     return await self.generate(
            #         goal, chat_history, knowledge_cache,
            #         None, retry_count
            #     )
                
            return content
            
        except Exception as e:
            logger.error(f"生成回复时出错: {e}")
            return "抱歉，我现在有点混乱，让我重新思考一下..."

    async def check_reply(
        self,
        reply: str,
        goal: str,
        retry_count: int = 0
    ) -> Tuple[bool, str, bool]:
        """检查回复是否合适
        
        Args:
            reply: 生成的回复
            goal: 对话目标
            retry_count: 当前重试次数
            
        Returns:
            Tuple[bool, str, bool]: (是否合适, 原因, 是否需要重新规划)
        """
        return await self.reply_checker.check(reply, goal, retry_count)


class PFCNotificationHandler(NotificationHandler):
    """PFC的通知处理器"""
    
    def __init__(self, conversation: 'Conversation'):
        self.conversation = conversation
        self.logger = get_module_logger("pfc_notification")
        self.decision_info = conversation.decision_info
        
    async def handle_notification(self, notification: Notification):
        """处理通知"""
        try:
            if not notification or not hasattr(notification, 'data') or notification.data is None:
                self.logger.error("收到无效的通知：notification 或 data 为空")
                return
                
            if notification.type == NotificationType.NEW_MESSAGE:
                # 处理新消息通知
                message = notification.data
                if not isinstance(message, dict):
                    self.logger.error(f"无效的消息格式: {type(message)}")
                    return
                    
                content = message.get('content', '')
                self.logger.info(f"收到新消息通知: {content[:30] if content else ''}...")
                
                # 更新决策信息
                try:
                    self.decision_info.update_from_message(message)
                except Exception as e:
                    self.logger.error(f"更新决策信息失败: {e}")
                    return
                    
                # 触发对话系统更新
                self.conversation.chat_observer.trigger_update()
                
            elif notification.type == NotificationType.COLD_CHAT:
                # 处理冷场通知
                try:
                    is_cold = bool(notification.data.get("is_cold", False))
                    # 更新决策信息
                    self.decision_info.update_cold_chat_status(is_cold, time.time())
                    
                    if is_cold:
                        self.logger.info("检测到对话冷场")
                    else:
                        self.logger.info("对话恢复活跃")
                except Exception as e:
                    self.logger.error(f"处理冷场状态失败: {e}")
                    return
                    
        except Exception as e:
            self.logger.error(f"处理通知时出错: {str(e)}")
            # 添加更详细的错误信息
            self.logger.error(f"通知类型: {getattr(notification, 'type', None)}")
            self.logger.error(f"通知数据: {getattr(notification, 'data', None)}")


class Conversation:
    # 类级别的实例管理
    _instances: Dict[str, 'Conversation'] = {}
    _instance_lock = asyncio.Lock()
    _init_events: Dict[str, asyncio.Event] = {}
    _initializing: Dict[str, bool] = {}
    
    @classmethod
    async def get_instance(cls, stream_id: str) -> Optional['Conversation']:
        """获取或创建对话实例
        
        Args:
            stream_id: 聊天流ID
            
        Returns:
            Optional[Conversation]: 对话实例，如果创建或等待失败则返回None
        """
        try:
            # 检查是否已经有实例
            if stream_id in cls._instances:
                return cls._instances[stream_id]
                
            async with cls._instance_lock:
                # 再次检查，防止在获取锁的过程中其他线程创建了实例
                if stream_id in cls._instances:
                    return cls._instances[stream_id]
                    
                # 如果正在初始化，等待初始化完成
                if stream_id in cls._initializing and cls._initializing[stream_id]:
                    event = cls._init_events.get(stream_id)
                    if event:
                        try:
                            # 在等待之前释放锁
                            cls._instance_lock.release()
                            await asyncio.wait_for(event.wait(), timeout=10.0)  # 增加超时时间到10秒
                            # 重新获取锁
                            await cls._instance_lock.acquire()
                            if stream_id in cls._instances:
                                return cls._instances[stream_id]
                        except asyncio.TimeoutError:
                            logger.error(f"等待实例 {stream_id} 初始化超时")
                            # 清理超时的初始化状态
                            cls._initializing[stream_id] = False
                            if stream_id in cls._init_events:
                                del cls._init_events[stream_id]
                            return None
                
                # 创建新实例
                logger.info(f"创建新的对话实例: {stream_id}")
                cls._initializing[stream_id] = True
                cls._init_events[stream_id] = asyncio.Event()
                
                # 在锁保护下创建实例
                instance = cls(stream_id)
                cls._instances[stream_id] = instance
                
                # 启动实例初始化（在后台运行）
                asyncio.create_task(instance._initialize())
                
                return instance
                
        except Exception as e:
            logger.error(f"获取对话实例失败: {e}")
            return None
            
    async def _initialize(self):
        """初始化实例（在后台运行）"""
        try:
            logger.info(f"开始初始化对话实例: {self.stream_id}")
            
            start_time = time.time()
            logger.info("启动观察器...")
            self.chat_observer.start()  # 启动观察器
            logger.info(f"观察器启动完成，耗时: {time.time() - start_time:.2f}秒")
            
            await asyncio.sleep(1)  # 给观察器一些启动时间
            
            # 获取初始目标
            logger.info("开始分析初始对话目标...")
            goal_start_time = time.time()
            self.current_goal, self.current_method, self.goal_reasoning = await self.goal_analyzer.analyze_goal()
            logger.info(f"目标分析完成，耗时: {time.time() - goal_start_time:.2f}秒")
            
            # 标记初始化完成
            logger.info("标记初始化完成...")
            self.__class__._initializing[self.stream_id] = False
            if self.stream_id in self.__class__._init_events:
                self.__class__._init_events[self.stream_id].set()
                
            # 启动对话循环
            logger.info("启动对话循环...")
            asyncio.create_task(self._conversation_loop())
            
            total_time = time.time() - start_time
            logger.info(f"实例初始化完成，总耗时: {total_time:.2f}秒")
            
        except Exception as e:
            logger.error(f"初始化对话实例失败: {e}")
            # 清理失败的初始化
            self.__class__._initializing[self.stream_id] = False
            if self.stream_id in self.__class__._init_events:
                self.__class__._init_events[self.stream_id].set()
            if self.stream_id in self.__class__._instances:
                del self.__class__._instances[self.stream_id]
    
    async def start(self):
        """开始对话流程"""
        try:
            logger.info("对话系统启动")
            self.should_continue = True
            await self._conversation_loop()
        except Exception as e:
            logger.error(f"启动对话系统失败: {e}")
            raise

    async def _conversation_loop(self):
        """对话循环"""
        # 获取最近的消息历史
        self.current_goal, self.current_method, self.goal_reasoning = await self.goal_analyzer.analyze_goal()
        
        while self.should_continue:
            # 执行行动
            self.chat_observer.trigger_update()  # 触发立即更新
            if not await self.chat_observer.wait_for_update():
                logger.warning("等待消息更新超时")
            
            # 使用决策信息来辅助行动规划
            action, reason = await self.action_planner.plan(
                self.current_goal,
                self.current_method,
                self.goal_reasoning,
                self.action_history,
                self.decision_info  # 传入决策信息
            )
            
            # 执行行动
            await self._handle_action(action, reason)
            
            # 清理已处理的消息
            self.decision_info.clear_unprocessed_messages()
            
    def _convert_to_message(self, msg_dict: Dict[str, Any]) -> Message:
        """将消息字典转换为Message对象"""
        try:
            chat_info = msg_dict.get("chat_info", {})
            chat_stream = ChatStream.from_dict(chat_info)
            user_info = UserInfo.from_dict(msg_dict.get("user_info", {}))
            
            return Message(
                message_id=msg_dict["message_id"],
                chat_stream=chat_stream,
                time=msg_dict["time"],
                user_info=user_info,
                processed_plain_text=msg_dict.get("processed_plain_text", ""),
                detailed_plain_text=msg_dict.get("detailed_plain_text", "")
            )
        except Exception as e:
            logger.warning(f"转换消息时出错: {e}")
            raise

    async def _handle_action(self, action: str, reason: str):
        """处理规划的行动"""
        logger.info(f"执行行动: {action}, 原因: {reason}")
        
        # 记录action历史
        self.action_history.append({
            "action": action,
            "reason": reason,
            "time": datetime.datetime.now().strftime("%H:%M:%S")
        })
        
        # 只保留最近的10条记录
        if len(self.action_history) > 10:
            self.action_history = self.action_history[-10:]
        
        if action == "direct_reply":
            self.state = ConversationState.GENERATING
            messages = self.chat_observer.get_message_history(limit=30)
            self.generated_reply = await self.reply_generator.generate(
                self.current_goal,
                self.current_method,
                [self._convert_to_message(msg) for msg in messages],
                self.knowledge_cache
            )
            
            # 检查回复是否合适
            is_suitable, reason, need_replan = await self.reply_generator.check_reply(
                self.generated_reply,
                self.current_goal
            )
            
            await self._send_reply()
            
        elif action == "fetch_knowledge":
            self.state = ConversationState.GENERATING
            messages = self.chat_observer.get_message_history(limit=30)
            knowledge, sources = await self.knowledge_fetcher.fetch(
                self.current_goal,
                [self._convert_to_message(msg) for msg in messages]
            )
            logger.info(f"获取到知识，来源: {sources}")
            
            if knowledge != "未找到相关知识":
                self.knowledge_cache[sources] = knowledge
        
        elif action == "rethink_goal":
            self.state = ConversationState.RETHINKING
            self.current_goal, self.current_method, self.goal_reasoning = await self.goal_analyzer.analyze_goal()
        
        elif action == "judge_conversation":
            self.state = ConversationState.JUDGING
            self.goal_achieved, self.stop_conversation, self.reason = await self.goal_analyzer.analyze_conversation(self.current_goal, self.goal_reasoning)
            
            # 如果当前目标达成但还有其他目标
            if self.goal_achieved and not self.stop_conversation:
                alternative_goals = await self.goal_analyzer.get_alternative_goals()
                if alternative_goals:
                    # 切换到下一个目标
                    self.current_goal, self.current_method, self.goal_reasoning = alternative_goals[0]
                    logger.info(f"当前目标已达成，切换到新目标: {self.current_goal}")
                    return
            
            if self.stop_conversation:
                await self._stop_conversation()
            
        elif action == "listening":
            self.state = ConversationState.LISTENING
            logger.info("倾听对方发言...")
            if await self.waiter.wait():  # 如果返回True表示超时
                await self._send_timeout_message()
                await self._stop_conversation()
            
        else:  # wait
            self.state = ConversationState.WAITING
            logger.info("等待更多信息...")
            if await self.waiter.wait():  # 如果返回True表示超时
                await self._send_timeout_message()
                await self._stop_conversation()

    async def _stop_conversation(self):
        """完全停止对话"""
        logger.info("停止对话")
        self.should_continue = False
        self.state = ConversationState.ENDED
        # 删除实例（这会同时停止chat_observer）
        await self.remove_instance(self.stream_id)

    async def _send_timeout_message(self):
        """发送超时结束消息"""
        try:
            messages = self.chat_observer.get_message_history(limit=1)
            if not messages:
                return
                
            latest_message = self._convert_to_message(messages[0])
            await self.direct_sender.send_message(
                chat_stream=self.chat_stream,
                content="抱歉，由于等待时间过长，我需要先去忙别的了。下次再聊吧~",
                reply_to_message=latest_message
            )
        except Exception as e:
            logger.error(f"发送超时消息失败: {str(e)}")

    async def _send_reply(self):
        """发送回复"""
        if not self.generated_reply:
            logger.warning("没有生成回复")
            return
            
        messages = self.chat_observer.get_message_history(limit=1)
        if not messages:
            logger.warning("没有最近的消息可以回复")
            return
            
        latest_message = self._convert_to_message(messages[0])
        try:
            await self.direct_sender.send_message(
                chat_stream=self.chat_stream,
                content=self.generated_reply,
                reply_to_message=latest_message
            )
            self.chat_observer.trigger_update()  # 触发立即更新
            if not await self.chat_observer.wait_for_update():
                logger.warning("等待消息更新超时")
            
            self.state = ConversationState.ANALYZING
        except Exception as e:
            logger.error(f"发送消息失败: {str(e)}")
            self.state = ConversationState.ANALYZING


class DirectMessageSender:
    """直接发送消息到平台的发送器"""
    
    def __init__(self):
        self.logger = get_module_logger("direct_sender")
        self.storage = MessageStorage()

    async def send_message(
        self,
        chat_stream: ChatStream,
        content: str,
        reply_to_message: Optional[Message] = None,
    ) -> None:
        """直接发送消息到平台
        
        Args:
            chat_stream: 聊天流
            content: 消息内容
            reply_to_message: 要回复的消息
        """
        # 构建消息对象
        message_segment = Seg(type="text", data=content)
        bot_user_info = UserInfo(
            user_id=global_config.BOT_QQ,
            user_nickname=global_config.BOT_NICKNAME,
            platform=chat_stream.platform,
        )
        
        message = MessageSending(
            message_id=f"dm{round(time.time(), 2)}",
            chat_stream=chat_stream,
            bot_user_info=bot_user_info,
            sender_info=reply_to_message.message_info.user_info if reply_to_message else None,
            message_segment=message_segment,
            reply=reply_to_message,
            is_head=True,
            is_emoji=False,
            thinking_start_time=time.time(),
        )

        # 处理消息
        await message.process()

        # 发送消息
        try:
            message_json = message.to_dict()
            end_point = global_config.api_urls.get(chat_stream.platform, None)
            
            if not end_point:
                raise ValueError(f"未找到平台：{chat_stream.platform} 的url配置")
                
            await global_api.send_message_REST(end_point, message_json)
            
            # 存储消息
            await self.storage.store_message(message, message.chat_stream)
            
            self.logger.info(f"直接发送消息成功: {content[:30]}...")
            
        except Exception as e:
            self.logger.error(f"直接发送消息失败: {str(e)}")
            raise

