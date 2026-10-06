"""MC Goal Agent 并发管理器。

:class:`GoalAgentManager` 负责：

1. **多目标并发**：每个目标用 ``TaskManager.create_task`` 起一个后台
   协程驱动 :class:`MCGoalAgent.execute`，支持同时跑多个目标。
2. **生命周期管理**：启动 / 取消 / 查询目标状态。
3. **重启恢复**：插件加载时从持久化层读取未完成目标（pending/running/
   paused），重新投递执行（running 的会被重置为 pending 后重新跑，
   因为上次中断的内存态已丢失）。
4. **统一入口**：对外暴露 ``start_goal / cancel_goal / get_status /
   list_goals``，供插件层（event_handler / command / chatter）调用。

设计要点：

- 管理器是单例（由插件 ``on_plugin_loaded`` 创建），全进程共享。
- 不维护目标的内存态副本，状态查询直接读 :class:`GoalStore`。
- ``_running_tasks`` 只记 ``goal_id -> task_id`` 映射，用于取消。
- 后台协程异常不向上抛（已被 :class:`MCGoalAgent.execute` 内部捕获并
  写入持久化），此处仅记录任务完成回调日志。
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

from src.app.plugin_system.api import agent_api, send_api
from src.app.plugin_system.api.log_api import get_logger
from src.kernel.concurrency import get_task_manager

from .models import GoalState
from .store import GoalStore

if TYPE_CHECKING:
    from src.core.components.base.plugin import BasePlugin

logger = get_logger("mc_goal_agent.manager")

#: Agent 组件签名（与 MCGoalAgent.to_schema 生成的前缀对应）。
_AGENT_SIGNATURE = "mc_goal_agent:agent:mc_goal_agent"

#: 任务组名，便于统一取消 / 统计。
_TASK_GROUP = "mc_goal_agent"

#: 单个目标的最大步数默认值。
_DEFAULT_MAX_STEPS = 20

#: 单个目标后台协程超时（秒），None 表示不超时。
_DEFAULT_TIMEOUT: float | None = None


class GoalAgentManager:
    """目标代理并发管理器。

    管理多个 :class:`MCGoalAgent` 后台执行，支持并发、取消、重启恢复。
    由插件 ``on_plugin_loaded`` 创建为单例，全进程共享。

    Attributes:
        _plugin: 所属插件实例（用于 ``agent_api.execute_agent``）。
        _store: 持久化存储。
        _running_tasks: ``goal_id -> task_id`` 映射，用于取消。
    """

    def __init__(self, plugin: BasePlugin) -> None:
        """初始化管理器。

        Args:
            plugin: 所属插件实例。
        """
        self._plugin = plugin
        self._store = GoalStore()
        self._running_tasks: dict[str, str] = {}
        self._lock = asyncio.Lock()

    async def start_goal(
        self,
        stream_id: str,
        objective: str,
        *,
        max_steps: int = _DEFAULT_MAX_STEPS,
        created_by: str = "user",
        goal_id: str | None = None,
    ) -> str:
        """启动一个新目标。

        创建后台任务驱动 :class:`MCGoalAgent.execute`，立即返回 ``goal_id``。
        目标执行过程异步进行，进度通过持久化层与聊天流通知查询。

        Args:
            stream_id: 发起目标的聊天流 ID。
            objective: 自然语言目标描述。
            max_steps: 最大 ReAct 步数。
            created_by: 发起者标识。
            goal_id: 可选的已有目标 ID；重启恢复时用于原地续跑。

        Returns:
            新建的 ``goal_id``。
        """
        import uuid

        goal_id = goal_id or f"goal_{uuid.uuid4().hex[:12]}"
        logger.info(
            f"启动目标 {goal_id}：{objective}（stream={stream_id}, max_steps={max_steps}）"
        )

        task_manager = get_task_manager()
        coro = self._run_goal(
            goal_id=goal_id,
            stream_id=stream_id,
            objective=objective,
            max_steps=max_steps,
            created_by=created_by,
        )
        task_info = task_manager.create_task(
            coro=coro,
            name=f"mc_goal_agent:{goal_id}",
            group_name=_TASK_GROUP,
            timeout=_DEFAULT_TIMEOUT,
            metadata={
                "goal_id": goal_id,
                "stream_id": stream_id,
                "objective": objective,
            },
        )

        async with self._lock:
            self._running_tasks[goal_id] = task_info.task_id

        return goal_id

    async def cancel_goal(self, goal_id: str) -> bool:
        """取消一个正在执行的目标。

        Args:
            goal_id: 目标 ID。

        Returns:
            是否成功发起取消（目标不存在或已结束返回 ``False``）。
        """
        async with self._lock:
            task_id = self._running_tasks.pop(goal_id, None)

        if task_id is None:
            return False

        task_manager = get_task_manager()
        cancelled = task_manager.cancel_task(task_id)
        if cancelled:
            await self._store.update_state(
                goal_id, GoalState.CANCELLED, "目标被用户取消"
            )
            logger.info(f"目标 {goal_id} 已取消")
        return cancelled

    async def get_status(self, goal_id: str) -> dict[str, Any] | None:
        """查询单个目标状态。

        Args:
            goal_id: 目标 ID。

        Returns:
            状态字典（含 state/steps/conclusion/objective）；不存在返回 ``None``。
        """
        record = await self._store.load(goal_id)
        if record is None:
            return None
        return {
            "goal_id": record.goal_id,
            "state": record.state.value,
            "steps": len(record.steps),
            "max_steps": record.max_steps,
            "conclusion": record.conclusion,
            "objective": record.objective,
            "created_at": record.created_at,
            "updated_at": record.updated_at,
        }

    async def list_goals(
        self, stream_id: str | None = None, active_only: bool = False
    ) -> list[dict[str, Any]]:
        """列出目标。

        Args:
            stream_id: 可选，按聊天流过滤。
            active_only: 是否只返回未完成目标。

        Returns:
            目标状态字典列表（按创建时间升序）。
        """
        if stream_id is not None:
            records = await self._store.list_by_stream(stream_id)
        else:
            records = await self._store.list_all()
        if active_only:
            records = [r for r in records if r.is_active()]
        return [
            {
                "goal_id": r.goal_id,
                "state": r.state.value,
                "steps": len(r.steps),
                "max_steps": r.max_steps,
                "conclusion": r.conclusion,
                "objective": r.objective,
                "created_at": r.created_at,
                "updated_at": r.updated_at,
            }
            for r in records
        ]

    async def recover_pending_goals(self) -> int:
        """重启恢复：把持久化中未完成的目标重新投递执行。

        - ``pending``：直接重新投递。
        - ``running``：上次中断，内存态丢失，重置为 ``pending`` 后重新投递。
        - ``paused``：暂不自动恢复（需显式 resume，当前版本等同 pending 处理）。

        Returns:
            恢复的目标数量。
        """
        active = await self._store.list_active()
        if not active:
            return 0

        recovered = 0
        for record in active:
            # running 的重置为 pending（内存态已丢失，从头跑）
            if record.state == GoalState.RUNNING:
                record.state = GoalState.PENDING
                record.conclusion = ""
                record.updated_at = time.time()
                await self._store.save(record)
                logger.info(
                    f"恢复中断目标 {record.goal_id}（重置 running->pending）："
                    f"{record.objective}"
                )

            # 重新投递
            try:
                await self.start_goal(
                    stream_id=record.stream_id,
                    objective=record.objective,
                    max_steps=record.max_steps,
                    created_by=record.created_by,
                    goal_id=record.goal_id,
                )
                recovered += 1
            except Exception:  # noqa: BLE001 - 恢复失败不阻断其他目标
                logger.error(f"恢复目标 {record.goal_id} 失败", exc_info=True)
        logger.info(f"共恢复 {recovered} 个未完成目标")
        return recovered

    async def shutdown(self) -> None:
        """关闭管理器：取消所有正在执行的目标任务。

        由插件 ``on_plugin_unloaded`` 调用。已持久化的目标状态保持
        ``running``，下次启动时由 :meth:`recover_pending_goals` 恢复。
        """
        async with self._lock:
            goal_ids = list(self._running_tasks.keys())
            task_ids = list(self._running_tasks.values())
            self._running_tasks.clear()

        if not task_ids:
            return

        task_manager = get_task_manager()
        for goal_id, task_id in zip(goal_ids, task_ids):
            task_manager.cancel_task(task_id)
            logger.info(f"关闭时取消目标 {goal_id}")

    async def _run_goal(
        self,
        goal_id: str,
        stream_id: str,
        objective: str,
        max_steps: int,
        created_by: str,
    ) -> None:
        """后台执行单个目标（由 TaskManager 调度）。

        Args:
            goal_id: 目标 ID。
            stream_id: 聊天流 ID。
            objective: 目标描述。
            max_steps: 最大步数。
            created_by: 发起者。
        """
        try:
            success, result = await agent_api.execute_agent(
                signature=_AGENT_SIGNATURE,
                plugin=self._plugin,
                stream_id=stream_id,
                objective=objective,
                max_steps=max_steps,
                goal_id=goal_id,
                created_by=created_by,
            )
            logger.info(f"目标 {goal_id} 执行结束：success={success}, result={result}")
            conclusion = (
                str(result.get("conclusion", "")).strip()
                if isinstance(result, dict)
                else str(result).strip()
            )
            steps = result.get("steps") if isinstance(result, dict) else None
            state = result.get("state") if isinstance(result, dict) else None
            state_text = (
                "目标取消"
                if state == GoalState.CANCELLED.value
                else "目标完成"
                if success
                else "目标失败"
            )
            detail_lines = [f"[{state_text}] #{goal_id}", f"目标：{objective}"]
            if conclusion:
                detail_lines.append(f"结果：{conclusion}")
            if isinstance(steps, int):
                detail_lines.append(f"执行步骤：{steps}")
            try:
                await send_api.send_text("\n".join(detail_lines), stream_id)
            except Exception:  # noqa: BLE001 - 通知失败不改变目标结果
                logger.warning(f"发送目标 {goal_id} 最终结果失败", exc_info=True)
        except Exception as exc:  # noqa: BLE001 - 后台任务异常兜底
            logger.error(f"目标 {goal_id} 后台执行异常", exc_info=True)
            await self._store.update_state(
                goal_id,
                GoalState.FAILED,
                f"执行异常: {exc}",
            )
            try:
                await send_api.send_text(
                    f"[目标失败] #{goal_id}\n目标：{objective}\n执行异常：{exc}",
                    stream_id,
                )
            except Exception:  # noqa: BLE001 - 通知失败不遮蔽原异常
                logger.warning(f"发送目标 {goal_id} 异常通知失败", exc_info=True)
        finally:
            async with self._lock:
                self._running_tasks.pop(goal_id, None)
