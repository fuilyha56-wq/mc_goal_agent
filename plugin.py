"""MC Goal Agent 插件入口。

注册 :class:`MCGoalAgent` 组件（``component_type="agent"``），并在
``on_plugin_loaded`` 时创建 :class:`GoalAgentManager` 单例，按配置决定是否
自动恢复未完成的目标。``on_plugin_unloaded`` 时关闭管理器，取消所有正在
执行的后台任务。

触发链路：用户在 Minecraft 平台聊天流发送 ``代理完成: <目标>`` ->
:class:`MCGoalTriggerHandler` 命中关键词 -> ``GoalAgentManager.start_goal``
-> 后台协程调用 ``agent_api.execute_agent`` -> :class:`MCGoalAgent.execute`
ReAct 循环分步完成目标。

本插件强依赖 ``mc_adapter`` 插件已加载（提供 Minecraft 平台适配与 bridge
服务），以及 ``mc_tools`` 插件注册的 MC 工具集（作为 Agent 的私有 usables）。
但不在 ``dependent_components`` 中声明硬依赖，而是通过
``associated_platforms=["minecraft"]`` 在运行时动态过滤可用工具，未就绪时
Agent 会以「无可用工具」状态启动并依赖 LLM 自主决策。
"""

from __future__ import annotations

from typing import cast

from src.app.plugin_system.base import BasePlugin, register_plugin
from src.kernel.logger import get_logger

from .components.agent import MCGoalAgent
from .components.event_handler import MCGoalTriggerHandler
from .components.manager import GoalAgentManager
from .config import MCGoalAgentConfig

logger = get_logger("mc_goal_agent")


@register_plugin
class MCGoalAgentPlugin(BasePlugin):
    """Minecraft 目标代理插件。

    注册 :class:`MCGoalAgent`（长线调度 Agent）与 :class:`MCGoalTriggerHandler`
    （消息触发器），让 bot 能在 Minecraft 平台自主完成用户给定的目标。

    组件注册：

    - ``mc_goal_agent:agent:mc_goal_agent``：ReAct Agent，通过
      ``agent_api.execute_agent`` 显式驱动，不进入主聊天工具面板。
    - ``mc_goal_agent:event_handler:mc_goal_trigger``：监听
      :data:`EventType.ON_MESSAGE_RECEIVED`，解析关键词前缀触发代理。
    """

    plugin_name: str = "mc_goal_agent"
    plugin_description: str = (
        "Minecraft 目标代理：基于 ReAct 循环让 bot 自主完成长线目标。"
        "用户在 MC 平台发送「代理完成: <目标>」触发，Agent 分步调用 MC 工具"
        "完成目标，支持多目标并发、持久化与重启恢复。强依赖 mc_adapter 与"
        "mc_tools 插件已加载。"
    )
    plugin_version: str = "1.0.1"
    plugin_author: str = "MoFox Team"

    configs: list[type] = [MCGoalAgentConfig]
    dependent_components: list[str] = []

    def get_components(self) -> list[type]:
        """返回插件组件类。

        - 总开关 ``plugin.enabled`` 关闭后所有组件都不注册。
        - Agent 与触发器成对注册，缺一不可。
        """
        components: list[type] = []
        config = cast(MCGoalAgentConfig, self.config) if self.config else None
        if config is None or not config.plugin.enabled:
            return components

        components.append(MCGoalAgent)
        components.append(MCGoalTriggerHandler)
        return components

    async def on_plugin_loaded(self) -> None:
        """插件加载完成：创建管理器单例并按配置恢复未完成目标。"""
        config = cast(MCGoalAgentConfig, self.config) if self.config else None
        if config is None or not config.plugin.enabled:
            logger.info("mc_goal_agent 插件已加载（未启用）")
            return

        # 创建管理器单例，供 event_handler 访问
        manager = GoalAgentManager(self)
        self._goal_agent_manager = manager

        recovered = 0
        if config.agent.recover_on_start:
            try:
                recovered = await manager.recover_pending_goals()
            except Exception:  # noqa: BLE001 - 恢复失败不阻断插件加载
                logger.error("恢复未完成目标失败", exc_info=True)

        logger.info(
            f"mc_goal_agent 插件已加载（agent=mc_goal_agent, "
            f"trigger=mc_goal_trigger, recovered={recovered}）"
        )

    async def on_plugin_unloaded(self) -> None:
        """插件卸载：关闭管理器，取消所有正在执行的后台任务。"""
        manager: GoalAgentManager | None = getattr(self, "_goal_agent_manager", None)
        if manager is not None:
            try:
                await manager.shutdown()
            except Exception:  # noqa: BLE001 - 卸载兜底
                logger.error("关闭 GoalAgentManager 失败", exc_info=True)
            self._goal_agent_manager = None
        logger.info("mc_goal_agent 插件已卸载")
