"""MC Goal Agent 触发事件处理器。

监听 :data:`EventType.ON_MESSAGE_RECEIVED`，解析用户消息文本：当消息以
配置的触发关键词前缀开头时（如「代理完成: 收集 64 个圆石」），提取冒号
后的目标描述，调用 :class:`GoalAgentManager.start_goal` 启动后台代理。

本处理器仅在 ``minecraft`` 平台聊天流生效，避免误触发其他平台会话。
处理器不拦截消息（``intercept_message=False``），仅作为旁路触发器，
让正常聊天链继续运行。

触发格式（两种均可）：

- ``代理完成: <目标描述>``（全角冒号，兼容中文输入法）
- ``代理完成: <目标描述>``（半角冒号）

关键词本身由配置 ``agent.trigger_keyword`` 决定，默认 ``代理完成``。
"""

from __future__ import annotations

from typing import Any

from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.api.send_api import send_text
from src.core.components.base.event_handler import BaseEventHandler
from src.core.components.types import EventType
from src.core.models import Message
from src.kernel.event import EventDecision

from .manager import GoalAgentManager

logger = get_logger("mc_goal_agent.trigger")

#: MC 平台标识。
_MC_PLATFORM = "minecraft"


class MCGoalTriggerHandler(BaseEventHandler):
    """目标代理触发器。

    订阅 :data:`EventType.ON_MESSAGE_RECEIVED`，解析消息文本匹配触发关键词，
    命中后调用 :class:`GoalAgentManager.start_goal` 启动后台代理。

    Attributes:
        name: 处理器名称。
        weight: 权重（低优先级，避免影响预处理与主聊天链）。
        intercept_message: 不拦截消息，仅旁路触发。
        init_subscribe: 订阅消息接收事件。
        timeout: 禁用超时保护（``start_goal`` 仅创建任务即返回，但仍保守禁用）。
    """

    name: str = "mc_goal_trigger"
    description: str = "MC 目标代理触发器：解析消息关键词前缀启动目标代理"
    weight: int = 5
    intercept_message: bool = False
    init_subscribe: list[EventType | str] = [EventType.ON_MESSAGE_RECEIVED]
    timeout: float | None = None

    async def execute(
        self, event_name: str, params: dict[str, Any]
    ) -> tuple[EventDecision, dict[str, Any]]:
        """处理消息接收事件，匹配触发关键词。

        Args:
            event_name: 事件名称。
            params: 事件参数，含 ``message`` (:class:`Message`)。

        Returns:
            始终返回 ``EventDecision.SUCCESS`` 与原参数，不拦截消息。
        """
        # 仅在插件启用时工作
        manager = self._get_manager()
        if manager is None:
            return EventDecision.SUCCESS, params

        message = params.get("message")
        if not isinstance(message, Message):
            return EventDecision.SUCCESS, params

        # 仅 minecraft 平台生效
        if str(message.platform).lower() != _MC_PLATFORM:
            return EventDecision.SUCCESS, params

        # 提取纯文本
        text = (message.processed_plain_text or "").strip()
        if not text:
            return EventDecision.SUCCESS, params

        # 读取触发关键词（从插件配置）
        keyword = self._get_trigger_keyword()
        if not keyword:
            return EventDecision.SUCCESS, params

        # 匹配前缀（兼容全角/半角冒号）
        objective = self._extract_objective(text, keyword)
        if objective is None:
            return EventDecision.SUCCESS, params

        stream_id = message.stream_id
        sender = message.sender_name or message.sender_id or "unknown"

        # 读取最大步数配置
        max_steps = self._get_max_steps()

        logger.info(
            f"触发目标代理：keyword={keyword!r}, objective={objective!r}, "
            f"stream={stream_id[:8]}, sender={sender}, max_steps={max_steps}"
        )

        # 启动后台代理（立即返回 goal_id，不阻塞事件链）
        try:
            goal_id = await manager.start_goal(
                stream_id=stream_id,
                objective=objective,
                max_steps=max_steps,
                created_by=sender,
            )
        except Exception:  # noqa: BLE001 - 触发失败不阻断消息链
            logger.exception("启动目标代理失败")
            try:
                await send_text(
                    "目标代理启动失败，请稍后重试或联系管理员。",
                    stream_id,
                )
            except Exception:  # noqa: BLE001
                pass
            return EventDecision.SUCCESS, params

        # 回执 goal_id，让用户知道目标已受理
        try:
            await send_text(
                f"已受理目标 #{goal_id}：{objective}\n"
                f"最多执行 {max_steps} 步，完成后会通知结果。",
                stream_id,
            )
        except Exception:  # noqa: BLE001 - 回执失败不影响代理执行
            logger.debug(f"发送目标受理回执失败 goal={goal_id}")

        return EventDecision.SUCCESS, params

    def _get_manager(self) -> GoalAgentManager | None:
        """从所属插件获取 :class:`GoalAgentManager` 单例。

        Returns:
            管理器实例；插件未加载或未初始化返回 ``None``。
        """
        return getattr(self.plugin, "_goal_agent_manager", None)

    def _get_trigger_keyword(self) -> str:
        """从插件配置读取触发关键词。

        Returns:
            关键词字符串；配置不可用时返回空串。
        """
        config = getattr(self.plugin, "config", None)
        if config is None:
            return ""
        return getattr(getattr(config, "agent", None), "trigger_keyword", "") or ""

    def _get_max_steps(self) -> int:
        """从插件配置读取最大步数。

        Returns:
            最大步数；配置不可用时返回默认值 20。
        """
        config = getattr(self.plugin, "config", None)
        if config is None:
            return 20
        return getattr(getattr(config, "agent", None), "max_steps", 20) or 20

    @staticmethod
    def _extract_objective(text: str, keyword: str) -> str | None:
        """从消息文本中提取目标描述。

        支持全角（``：``）与半角（``:``）冒号分隔，关键词与前缀不区分大小写。
        冒号后的目标描述需非空。

        Args:
            text: 消息纯文本。
            keyword: 触发关键词。

        Returns:
            目标描述字符串；不匹配或目标为空返回 ``None``。
        """
        # 不区分大小写匹配前缀
        if not text.lower().startswith(keyword.lower()):
            return None

        rest = text[len(keyword) :]
        # 去除冒号分隔符（全角/半角均可，允许冒号前有空格）
        rest = rest.lstrip()
        if rest.startswith(":"):
            rest = rest[1:]
        elif rest.startswith("："):
            rest = rest[1:]
        else:
            # 没有冒号分隔符，不视为触发
            return None

        objective = rest.strip()
        if not objective:
            return None
        return objective


__all__ = ["MCGoalTriggerHandler"]
