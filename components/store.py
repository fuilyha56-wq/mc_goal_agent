"""MC Goal Agent 持久化层。

基于 ``storage_api`` 的 JSON 存储实现目标记录的持久化。每个目标记录以
``goal_id`` 为键保存到 ``data/json_storage/mc_goal_agent/`` 目录，重启
后可恢复未完成的目标。

设计要点：

- 使用 ``storage_api.save_json / load_json / list_json / delete_json``，
  避免直接操作文件系统，遵循 MoFox 统一存储约定。
- 所有方法均为 ``async``，与 storage_api 异步接口对齐。
- 不维护内存缓存，每次读写都落盘，保证崩溃一致性。
- ``list_active`` 用于启动时恢复未完成目标（pending/running/paused）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from src.app.plugin_system.api import storage_api
from src.kernel.logger import get_logger

from .models import GoalRecord, GoalState

if TYPE_CHECKING:
    pass

logger = get_logger("mc_goal_agent.store")

#: 存储命名空间，对应 ``data/json_storage/mc_goal_agent/``。
_STORE_NAME = "mc_goal_agent"


class GoalStore:
    """目标记录持久化存储。

    基于 ``storage_api`` JSON 存储，按 ``goal_id`` 作为键。每个目标记录
    独立保存为一个 JSON 文件，便于人工检视与恢复。
    """

    async def save(self, record: GoalRecord) -> None:
        """保存或更新目标记录。

        Args:
            record: 目标记录实例。
        """
        await storage_api.save_json(_STORE_NAME, record.goal_id, record.to_dict())
        logger.debug(
            f"目标 {record.goal_id} 已保存（state={record.state.value}, "
            f"steps={len(record.steps)}）"
        )

    async def load(self, goal_id: str) -> GoalRecord | None:
        """加载单个目标记录。

        Args:
            goal_id: 目标 ID。

        Returns:
            目标记录；不存在时返回 ``None``。
        """
        data = await storage_api.load_json(_STORE_NAME, goal_id)
        if data is None:
            return None
        return GoalRecord.from_dict(data)

    async def delete(self, goal_id: str) -> bool:
        """删除目标记录。

        Args:
            goal_id: 目标 ID。

        Returns:
            是否成功删除（不存在时返回 ``False``）。
        """
        return await storage_api.delete_json(_STORE_NAME, goal_id)

    async def list_all(self) -> list[GoalRecord]:
        """列出所有目标记录。

        Returns:
            目标记录列表（按 ``created_at`` 升序）。
        """
        keys = await storage_api.list_json(_STORE_NAME)
        records: list[GoalRecord] = []
        for key in keys:
            data = await storage_api.load_json(_STORE_NAME, key)
            if data is not None:
                records.append(GoalRecord.from_dict(data))
        records.sort(key=lambda r: r.created_at)
        return records

    async def list_active(self) -> list[GoalRecord]:
        """列出所有未完成（pending/running/paused）的目标记录。

        用于启动时恢复被中断的目标。

        Returns:
            未完成的目标记录列表（按 ``created_at`` 升序）。
        """
        all_records = await self.list_all()
        return [r for r in all_records if r.is_active()]

    async def list_by_stream(self, stream_id: str) -> list[GoalRecord]:
        """列出指定聊天流下的所有目标记录。

        Args:
            stream_id: 聊天流 ID。

        Returns:
            该流下的目标记录列表（按 ``created_at`` 升序）。
        """
        all_records = await self.list_all()
        return [r for r in all_records if r.stream_id == stream_id]

    async def update_state(
        self,
        goal_id: str,
        state: GoalState,
        conclusion: str = "",
    ) -> GoalRecord | None:
        """更新目标状态与结论。

        Args:
            goal_id: 目标 ID。
            state: 新状态。
            conclusion: 可选的结论描述（成功摘要 / 失败原因）。

        Returns:
            更新后的目标记录；目标不存在时返回 ``None``。
        """
        import time

        record = await self.load(goal_id)
        if record is None:
            return None
        record.state = state
        if conclusion:
            record.conclusion = conclusion
        record.updated_at = time.time()
        await self.save(record)
        return record
