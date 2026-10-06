"""MC Goal Agent 配置定义。

配置分两个节：

- ``plugin``：插件启用开关与配置版本。
- ``agent``：GoalAgent 行为参数（最大步数、进度通知、重启恢复等）。
"""

from __future__ import annotations

from typing import ClassVar

from src.core.components.base.config import (
    BaseConfig,
    Field,
    SectionBase,
    config_section,
)


class MCGoalAgentConfig(BaseConfig):
    """Minecraft 目标代理插件配置。"""

    name: ClassVar[str] = "config"
    description: ClassVar[str] = "Minecraft 目标代理配置"

    @config_section("plugin", title="插件设置", tag="plugin")
    class PluginSection(SectionBase):
        """插件基本配置。"""

        enabled: bool = Field(
            default=True,
            description="是否启用 MC 目标代理插件",
            label="启用插件",
            tag="plugin",
        )
        config_version: str = Field(
            default="1.0.0",
            description="配置文件版本",
            label="配置版本",
            disabled=True,
            tag="general",
        )

    @config_section("agent", title="代理行为", tag="agent")
    class AgentSection(SectionBase):
        """GoalAgent 行为参数。"""

        max_steps: int = Field(
            default=20,
            description="单个目标允许的最大 ReAct 步数，超出视为失败",
            label="最大步数",
            tag="agent",
        )
        progress_notify: bool = Field(
            default=True,
            description="是否在目标执行过程中向聊天流发送进度通知",
            label="进度通知",
            tag="agent",
        )
        progress_interval: int = Field(
            default=3,
            description="进度通知间隔（每 N 步发一次）",
            label="通知间隔",
            tag="agent",
        )
        recover_on_start: bool = Field(
            default=True,
            description="插件加载时是否自动恢复未完成的目标",
            label="启动恢复",
            tag="agent",
        )
        trigger_keyword: str = Field(
            default="代理完成",
            description=(
                "触发目标代理的关键词前缀。用户消息以「{关键词}: <目标>」"
                "开头时启动代理（如「代理完成: 收集 64 个圆石」）"
            ),
            label="触发关键词",
            tag="agent",
        )

    plugin: PluginSection = Field(default_factory=PluginSection)
    agent: AgentSection = Field(default_factory=AgentSection)
