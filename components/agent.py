"""MC Goal Agent 核心 ReAct 引擎。

:class:`MCGoalAgent` 继承 :class:`BaseAgent`，实现「让 bot 自主完成目标」
的长线调度能力。它通过 ReAct（Reasoning + Acting）循环驱动 bot 分步
完成目标：

1. **观察**：读取当前目标与历史步骤。
2. **思考**：调用 LLM 决策下一步该做什么。
3. **行动**：LLM 选择工具 -> ``execute_local_usable`` 执行 MC 工具。
4. **反思**：观察工具结果，更新历史，进入下一轮。

直到 LLM 声明目标完成、达到最大步数、或发生不可恢复错误。

Agent 的私有 usables 动态收集 ``mc_tools`` 插件注册的、
``associated_platforms=["minecraft"]`` 的全部 ACTIVE 工具，主模型调用
``agent-mc_goal_agent`` 时进入本 Agent，内部 LLM 再看到这组 MC 工具。

本 Agent 不直接注册到全局 chatter 的工具面板，而是由
:class:`GoalAgentManager` 在收到目标请求时通过 ``agent_api.execute_agent``
显式驱动，避免与主聊天链耦合。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Annotated, Any, Callable, TYPE_CHECKING

from src.app.plugin_system.api import llm_api, send_api
from src.app.plugin_system.base import BaseAgent
from src.app.plugin_system.types import TaskType
from src.core.components.types import ChatType
from src.kernel.llm import LLMPayload, LLMUsable, ROLE, Text, ToolResult
from src.kernel.logger import get_logger

from .completion import CompleteGoalTool
from .models import GoalRecord, GoalState, GoalStep
from .store import GoalStore

if TYPE_CHECKING:
    from src.core.models.message import Message

logger = get_logger("mc_goal_agent")

#: 存储命名空间（与 store.py 保持一致）。
_STORE_NAME = "mc_goal_agent"

#: MC 平台标识。
_MC_PLATFORM = "minecraft"

#: ReAct 循环默认最大步数。
_DEFAULT_MAX_STEPS = 20

#: 默认进度通知间隔（每 N 步发一次进度）。
_DEFAULT_PROGRESS_INTERVAL = 3


class MCGoalAgent(BaseAgent):
    """Minecraft 目标代理。

    通过 ReAct 循环驱动 bot 自主完成多步目标。继承 :class:`BaseAgent`，
    动态收集 MC 平台 ACTIVE 工具作为私有 usables，由
    :class:`GoalAgentManager` 显式调用 ``execute`` 驱动。

    Class Attributes:
        name: Agent 名称（schema 前缀 ``agent-mc_goal_agent``）。
        description: Agent 描述，主模型据此判断何时调用。
        chatter_allow: 允许的 chatter 列表（空表示全部）。
        chat_type: 支持的聊天类型。
        associated_platforms: 关联平台（仅 minecraft）。
    """

    name: str = "mc_goal_agent"
    description: str = (
        "Minecraft 目标代理：让 bot 自主完成一个长线目标。"
        "适合需要多步规划、观察环境、反复调整的任务，例如「收集 64 个圆石」"
        "「建造一个小屋」「探索到村庄」「击败末影龙」。"
        "调用时传入 objective（目标描述），代理会自主分解、执行、反思，"
        "完成后返回结论摘要。"
    )
    chatter_allow: list[str] = []
    chat_type: ChatType = ChatType.ALL
    associated_platforms: list[str] = [_MC_PLATFORM]
    associated_types: list[str] = ["text"]
    dependencies: list[str] = []
    usables: list[Any] = [CompleteGoalTool]

    async def execute_local_usable(
        self,
        usable_name: str,
        message: "Message | None" = None,
        task_observer: Callable[[asyncio.Task[None]], None] | None = None,
        **kwargs: Any,
    ) -> tuple[bool, Any]:
        """使用 usable 所属插件实例执行私有工具。

        Args:
            usable_name: usable schema 名称或去掉类型前缀后的短名。
            message: 当前消息。
            task_observer: 后台任务观察回调。
            **kwargs: 传递给 usable 的参数。

        Returns:
            ``(是否成功, 返回结果)``。

        Raises:
            ValueError: usable 不存在或所属插件未加载。
        """
        usable_cls: type[LLMUsable] | None = None
        for candidate in self._get_all_usables():
            function_schema = candidate.to_schema().get("function", {})
            schema_name = function_schema.get("name")
            if not isinstance(schema_name, str):
                continue
            short_name = schema_name
            for prefix in ("tool-", "action-", "agent-"):
                if short_name.startswith(prefix):
                    short_name = short_name[len(prefix) :]
                    break
            if usable_name in {schema_name, short_name}:
                usable_cls = candidate
                break

        if usable_cls is None:
            raise ValueError(f"Agent 私有 usable 不存在: {usable_name}")

        owner_plugin = self.plugin
        owner_name = getattr(usable_cls, "_plugin_", "")
        if owner_name and owner_name != getattr(type(self), "_plugin_", ""):
            from src.app.plugin_system.api import plugin_api

            owner_plugin = plugin_api.get_plugin(owner_name)
            if owner_plugin is None:
                raise ValueError(f"usable 所属插件未加载: {owner_name}")

        return await llm_api.exec_llm_usable(
            usable_cls,
            plugin=owner_plugin,
            stream_id=self.stream_id,
            message=message,
            kwargs=kwargs,
            task_observer=task_observer,
        )

    async def execute(
        self,
        objective: Annotated[str, "需要 bot 完成的目标描述（自然语言）"],
        max_steps: Annotated[int, "允许的最大 ReAct 步数"] = _DEFAULT_MAX_STEPS,
        goal_id: Annotated[str, "目标 ID（由 Manager 分配；留空则新建）"] = "",
        created_by: Annotated[str, "发起者标识（用户名/system/auto）"] = "user",
    ) -> tuple[Annotated[bool, "是否成功"], Annotated[str | dict, "返回结果"]]:
        """执行 ReAct 循环完成目标。

        Args:
            objective: 自然语言目标描述。
            max_steps: 最大步数，超出视为失败。
            goal_id: 目标 ID；留空时由本方法生成。
            created_by: 发起者标识。

        Returns:
            ``(是否成功, 结论)``。成功时结论为目标成果摘要；失败时为
            失败原因。同时返回的 dict 形如
            ``{"goal_id": ..., "state": ..., "steps": N, "conclusion": ...}``。
        """
        store = GoalStore()
        gid = goal_id or f"goal_{uuid.uuid4().hex[:12]}"
        now = time.time()

        record = await store.load(gid) if goal_id else None
        if record is None:
            record = GoalRecord(
                goal_id=gid,
                stream_id=self.stream_id,
                platform=_MC_PLATFORM,
                objective=objective,
                state=GoalState.RUNNING,
                created_at=now,
                updated_at=now,
                max_steps=max_steps,
                created_by=created_by,
            )
        else:
            record.state = GoalState.RUNNING
            record.conclusion = ""
            record.updated_at = now
        await store.save(record)

        logger.info(f"GoalAgent 启动目标 {gid}：{objective}（max_steps={max_steps}）")

        try:
            success, conclusion = await self._run_react_loop(record, store)
        except asyncio.CancelledError:
            record.state = GoalState.CANCELLED
            record.conclusion = "目标被取消"
            record.updated_at = time.time()
            await store.save(record)
            logger.info(f"目标 {gid} 被取消")
            return False, _result_dict(record, "目标被取消")
        except Exception as exc:  # noqa: BLE001 - 兜底记录失败
            record.state = GoalState.FAILED
            record.conclusion = f"执行异常: {exc}"
            record.updated_at = time.time()
            await store.save(record)
            logger.error(f"目标 {gid} 执行异常", exc_info=True)
            return False, _result_dict(record, record.conclusion)

        record.state = GoalState.COMPLETED if success else GoalState.FAILED
        record.conclusion = conclusion
        record.updated_at = time.time()
        await store.save(record)

        logger.info(
            f"目标 {gid} 结束：state={record.state.value}, steps={len(record.steps)}"
        )
        return success, _result_dict(record, conclusion)

    async def _run_react_loop(
        self, record: GoalRecord, store: GoalStore
    ) -> tuple[bool, str]:
        """驱动 ReAct 循环。

        Args:
            record: 目标记录（state=RUNNING）。
            store: 持久化存储。

        Returns:
            ``(是否成功, 结论)``。
        """
        model_set = llm_api.get_model_set_by_task(TaskType.TOOL_USE.value)
        request = self.create_llm_request(
            model_set=model_set,
            request_name="mc_goal_agent_react",
            with_usables=True,
        )
        request.add_payload(
            LLMPayload(
                ROLE.SYSTEM,
                Text(_build_system_prompt(record)),
            )
        )
        request.add_payload(
            LLMPayload(
                ROLE.USER,
                Text(_build_initial_user_prompt(record)),
            )
        )

        response = await request.send(stream=False)
        await response

        config = self.plugin.config
        agent_config = getattr(config, "agent", None) if config is not None else None
        progress_notify = (
            agent_config.progress_notify if agent_config is not None else True
        )
        progress_interval = max(
            1,
            agent_config.progress_interval
            if agent_config is not None
            else _DEFAULT_PROGRESS_INTERVAL,
        )
        first_step_index = len(record.steps)

        for step_index in range(first_step_index, record.max_steps + 1):
            calls = response.call_list or []
            thought = (response.message or "").strip()

            # LLM 未发起工具调用 -> 违反显式完成协议
            if not calls:
                step = GoalStep(
                    step_index=step_index,
                    thought=thought,
                    timestamp=time.time(),
                )
                record.steps.append(step)
                record.updated_at = time.time()
                await store.save(record)
                return False, "Agent 未调用 complete_goal 便停止执行"

            completion_calls = [
                call
                for call in calls
                if str(call.name) in {"complete_goal", "tool-complete_goal"}
            ]
            if completion_calls:
                tool_calls_meta = [
                    {
                        "name": str(call.name),
                        "args": call.args if isinstance(call.args, dict) else {},
                        "call_id": call.id,
                    }
                    for call in calls
                ]
                if len(calls) != 1 or len(completion_calls) != 1:
                    conclusion = "complete_goal 必须单独调用，不能与其他工具混用"
                    step = GoalStep(
                        step_index=step_index,
                        thought=thought,
                        tool_calls=tool_calls_meta,
                        tool_results=[
                            {
                                "name": "complete_goal",
                                "success": False,
                                "result": conclusion,
                            }
                        ],
                        timestamp=time.time(),
                    )
                    record.steps.append(step)
                    record.updated_at = time.time()
                    await store.save(record)
                    return False, conclusion

                completion_call = completion_calls[0]
                call_args = (
                    completion_call.args
                    if isinstance(completion_call.args, dict)
                    else {}
                )
                try:
                    valid, result = await self.execute_local_usable(
                        usable_name=str(completion_call.name),
                        **call_args,
                    )
                except (TypeError, ValueError) as exc:
                    valid = False
                    result = {"error": str(exc)}

                step = GoalStep(
                    step_index=step_index,
                    thought=thought,
                    tool_calls=tool_calls_meta,
                    tool_results=[
                        {
                            "name": str(completion_call.name),
                            "success": bool(valid),
                            "result": _truncate_result(result),
                        }
                    ],
                    timestamp=time.time(),
                )
                record.steps.append(step)
                record.updated_at = time.time()
                await store.save(record)

                if not valid or not isinstance(result, dict):
                    return False, f"complete_goal 声明无效: {result}"
                conclusion = str(result.get("conclusion", "")).strip()
                declared_success = result.get("success")
                if not conclusion or not isinstance(declared_success, bool):
                    return False, f"complete_goal 声明无效: {result}"
                return declared_success, conclusion

            if step_index >= record.max_steps:
                return False, f"目标在 {record.max_steps} 步内未完成"

            # 执行本轮全部工具调用
            tool_calls_meta: list[dict[str, Any]] = []
            tool_results_meta: list[dict[str, Any]] = []

            for call in calls:
                call_args = call.args if isinstance(call.args, dict) else {}
                call_name = str(call.name)
                tool_calls_meta.append(
                    {
                        "name": call_name,
                        "args": call_args,
                        "call_id": call.id,
                    }
                )
                try:
                    ok, result = await self.execute_local_usable(
                        usable_name=call_name,
                        **call_args,
                    )
                except ValueError as exc:
                    ok = False
                    result = f"工具不可用: {exc}"
                except Exception as exc:  # noqa: BLE001 - 工具异常不应中断循环
                    ok = False
                    result = f"工具执行异常: {exc}"

                tool_results_meta.append(
                    {
                        "name": call_name,
                        "success": bool(ok),
                        "result": _truncate_result(result),
                    }
                )
                response.add_payload(
                    LLMPayload(
                        ROLE.TOOL_RESULT,
                        ToolResult(
                            value=result,
                            call_id=call.id,
                            name=call_name,
                        ),
                    )
                )

            step = GoalStep(
                step_index=step_index,
                thought=thought,
                tool_calls=tool_calls_meta,
                tool_results=tool_results_meta,
                timestamp=time.time(),
            )
            record.steps.append(step)
            record.updated_at = time.time()
            await store.save(record)

            # 周期性进度通知
            if progress_notify and (step_index + 1) % progress_interval == 0:
                await self._notify_progress(record, step_index)

            # 下一轮：把工具结果回传给 LLM 继续推理
            response = await response.send(stream=False)
            await response

        # 超出最大步数
        return False, f"目标在 {record.max_steps} 步内未完成"

    def _get_extra_usables(self) -> list[type[LLMUsable]]:
        """动态收集 MC 平台 ACTIVE 工具作为私有 usables。

        从全局注册表按 ``ComponentType.TOOL`` 收集，筛选
        ``associated_platforms`` 包含 ``minecraft`` 且 state 为 ACTIVE
        的工具。这样 Agent 启动时自动获得当前可用的全部 MC 工具，
        无需在 ``usables`` 类属性里硬编码。

        Returns:
            MC 平台 ACTIVE 工具类列表。
        """
        from src.core.components.registry import get_global_registry
        from src.core.components.state_manager import get_global_state_manager
        from src.core.components.types import ComponentState, ComponentType

        registry = get_global_registry()
        state_manager = get_global_state_manager()

        all_tools = registry.get_by_type(ComponentType.TOOL)
        extra: list[type[LLMUsable]] = []
        for sig, tool_cls in all_tools.items():
            platforms = getattr(tool_cls, "associated_platforms", None) or []
            if _MC_PLATFORM not in platforms:
                continue
            if state_manager.get_state(sig) != ComponentState.ACTIVE:
                continue
            extra.append(tool_cls)  # type: ignore[arg-type]
        return extra

    async def _notify_progress(self, record: GoalRecord, step_index: int) -> None:
        """向聊天流发送进度通知。

        Args:
            record: 目标记录。
            step_index: 当前步骤序号。
        """
        try:
            last_step = record.steps[-1] if record.steps else None
            summary = ""
            if last_step and last_step.tool_calls:
                names = ", ".join(c["name"] for c in last_step.tool_calls)
                summary = f"（最近动作: {names}）"
            text = (
                f"[目标进度] {record.objective}\n"
                f"已执行 {step_index + 1}/{record.max_steps} 步{summary}"
            )
            await send_api.send_text(text, record.stream_id)
        except Exception:  # noqa: BLE001 - 进度通知失败不影响主流程
            logger.debug(f"目标 {record.goal_id} 进度通知发送失败", exc_info=True)


def _build_system_prompt(record: GoalRecord) -> str:
    """构造 ReAct 系统提示词。

    Args:
        record: 目标记录。

    Returns:
        系统提示词文本。
    """
    return (
        "你是 Minecraft 目标代理。你需要自主驱动一个 bot 完成给定目标。\n\n"
        "工作方式（ReAct 循环）：\n"
        "1. 观察当前状态（可用 get_status / get_surroundings / view_inventory）。\n"
        "2. 思考下一步该做什么，选择合适的工具调用。\n"
        "3. 根据工具返回结果反思，调整下一步策略。\n"
        "4. 重复直到目标完成。\n\n"
        "规则：\n"
        "- 目标尚未结束时，每轮必须调用一个或多个观察或动作工具。\n"
        "- 目标完成时，必须单独调用 complete_goal(success=true, conclusion=成果摘要)。\n"
        "- 确认目标无法完成时，必须单独调用 complete_goal(success=false, conclusion=失败原因)。\n"
        "- complete_goal 不得与任何其他工具在同一轮调用。\n"
        "- 只输出自然语言而不调用工具会被视为协议失败。\n"
        "- 优先用最小动作达成目标，避免无效探索。\n"
        "- 注意 bot 的生存状态（生命/饥饿），必要时先保证安全。\n"
        f"- 当前目标最多允许 {record.max_steps} 步，请合理规划。\n\n"
        "当前目标平台：Minecraft（java 版）。"
    )


def _build_initial_user_prompt(record: GoalRecord) -> str:
    """构造 ReAct 初始用户提示词。

    Args:
        record: 目标记录。

    Returns:
        用户提示词文本。
    """
    history_summary = ""
    if record.steps:
        lines = []
        for s in record.steps:
            actions = ", ".join(c["name"] for c in s.tool_calls) or "（无工具调用）"
            lines.append(f"  步骤{s.step_index}: {actions}")
        history_summary = (
            "\n\n已有历史步骤：\n" + "\n".join(lines) + "\n请基于历史继续推进。"
        )
    return (
        f"目标：{record.objective}\n\n"
        "请开始执行。建议先观察当前状态，再规划步骤。"
        f"{history_summary}"
    )


def _truncate_result(result: Any, max_len: int = 800) -> Any:
    """截断过长的工具结果，避免历史步骤膨胀。

    Args:
        result: 原始结果。
        max_len: 字符串最大长度。

    Returns:
        截断后的结果（保持原类型）。
    """
    if isinstance(result, str) and len(result) > max_len:
        return result[:max_len] + f"...(截断，共 {len(result)} 字符)"
    if isinstance(result, dict):
        s = str(result)
        if len(s) > max_len:
            return {"_truncated": True, "preview": s[:max_len]}
    return result


def _result_dict(record: GoalRecord, conclusion: str) -> dict[str, Any]:
    """构造 execute 返回值的 dict 部分。"""
    return {
        "goal_id": record.goal_id,
        "state": record.state.value,
        "steps": len(record.steps),
        "conclusion": conclusion,
        "objective": record.objective,
    }
