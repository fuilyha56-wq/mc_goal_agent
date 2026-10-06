"""MC Goal Agent 数据模型。

定义目标代理（GoalAgent）的核心数据结构：

- :class:`GoalState`：目标状态机枚举。
- :class:`GoalStep`：单步 ReAct 执行记录（思考/动作/观察/反思）。
- :class:`GoalRecord`：完整目标记录，含元数据、步骤历史、最终结论。

这些结构用于 :class:`GoalStore` 持久化与 :class:`MCGoalAgent` 循环内部
状态追踪。所有模型均为纯数据容器，不携带行为，便于 JSON 序列化。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class GoalState(str, Enum):
    """目标状态机枚举。

    状态流转：

    - ``pending``：已创建但未启动（等待调度）。
    - ``running``：ReAct 循环执行中。
    - ``paused``：被显式暂停（可恢复）。
    - ``completed``：目标成功完成。
    - ``failed``：执行失败（超步数/工具错误/LLM 判定失败）。
    - ``cancelled``：被显式取消。
    """

    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(slots=True)
class GoalStep:
    """单步 ReAct 执行记录。

    对应 ReAct 循环的一次「思考-行动-观察」迭代。

    Attributes:
        step_index: 步骤序号（从 0 开始）。
        thought: LLM 本轮的思考内容（assistant message）。
        tool_calls: 本轮发起的工具调用列表，每项形如
            ``{"name": ..., "args": ..., "call_id": ...}``。
        tool_results: 本轮工具执行结果列表，每项形如
            ``{"name": ..., "success": bool, "result": ...}``。
        reflection: 可选的反思记录（当前轮结束时的总结）。
        timestamp: 该步完成时的 Unix 时间戳（秒）。
    """

    step_index: int
    thought: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    tool_results: list[dict[str, Any]] = field(default_factory=list)
    reflection: str = ""
    timestamp: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """序列化为可 JSON 持久化的字典。"""
        return {
            "step_index": self.step_index,
            "thought": self.thought,
            "tool_calls": list(self.tool_calls),
            "tool_results": list(self.tool_results),
            "reflection": self.reflection,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GoalStep:
        """从字典反序列化。"""
        return cls(
            step_index=int(data.get("step_index", 0)),
            thought=str(data.get("thought", "")),
            tool_calls=list(data.get("tool_calls", []) or []),
            tool_results=list(data.get("tool_results", []) or []),
            reflection=str(data.get("reflection", "")),
            timestamp=float(data.get("timestamp", 0.0)),
        )


@dataclass(slots=True)
class GoalRecord:
    """完整目标记录。

    一个 GoalRecord 描述「bot 要达成什么目标」以及「目前进行到哪一步」。

    Attributes:
        goal_id: 目标唯一标识（UUID）。
        stream_id: 发起目标的聊天流 ID（用于回送进度通知）。
        platform: 平台名称（固定为 ``minecraft``）。
        objective: 自然语言目标描述。
        state: 当前状态。
        steps: 已完成的 ReAct 步骤历史。
        conclusion: 目标最终结论（成功时为成果摘要，失败时为失败原因）。
        created_at: 创建时间戳。
        updated_at: 最近更新时间戳。
        max_steps: 允许的最大步数。
        created_by: 发起者（用户名 / "system" / "auto"）。
    """

    goal_id: str
    stream_id: str
    platform: str
    objective: str
    state: GoalState = GoalState.PENDING
    steps: list[GoalStep] = field(default_factory=list)
    conclusion: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0
    max_steps: int = 20
    created_by: str = "user"

    def to_dict(self) -> dict[str, Any]:
        """序列化为可 JSON 持久化的字典。"""
        return {
            "goal_id": self.goal_id,
            "stream_id": self.stream_id,
            "platform": self.platform,
            "objective": self.objective,
            "state": self.state.value,
            "steps": [s.to_dict() for s in self.steps],
            "conclusion": self.conclusion,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "max_steps": self.max_steps,
            "created_by": self.created_by,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GoalRecord:
        """从字典反序列化。"""
        state_raw = data.get("state", GoalState.PENDING.value)
        try:
            state = GoalState(str(state_raw))
        except ValueError:
            state = GoalState.PENDING
        return cls(
            goal_id=str(data.get("goal_id", "")),
            stream_id=str(data.get("stream_id", "")),
            platform=str(data.get("platform", "minecraft")),
            objective=str(data.get("objective", "")),
            state=state,
            steps=[GoalStep.from_dict(s) for s in (data.get("steps") or [])],
            conclusion=str(data.get("conclusion", "")),
            created_at=float(data.get("created_at", 0.0)),
            updated_at=float(data.get("updated_at", 0.0)),
            max_steps=int(data.get("max_steps", 20)),
            created_by=str(data.get("created_by", "user")),
        )

    def is_terminal(self) -> bool:
        """是否处于终态（completed/failed/cancelled）。"""
        return self.state in (
            GoalState.COMPLETED,
            GoalState.FAILED,
            GoalState.CANCELLED,
        )

    def is_active(self) -> bool:
        """是否处于活动态（pending/running/paused），可被恢复执行。"""
        return self.state in (GoalState.PENDING, GoalState.RUNNING, GoalState.PAUSED)
