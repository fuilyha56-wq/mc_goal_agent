"""MC Goal Agent 显式完成工具。"""

from __future__ import annotations

from typing import Annotated

from src.app.plugin_system.base import BaseTool


class CompleteGoalTool(BaseTool):
    """结束当前 Minecraft 目标代理任务。"""

    name: str = "complete_goal"
    description: str = (
        "结束当前 Minecraft 目标代理任务。仅在确认目标已经达成或确实无法完成时"
        "调用；必须单独调用，不得与观察或动作工具混用。success=true 表示目标"
        "成功达成，success=false 表示确认失败；conclusion 必须提供可核验的成果"
        "摘要或具体失败原因。"
    )
    associated_platforms: list[str] = ["minecraft"]

    async def execute(
        self,
        success: Annotated[bool, "目标是否已成功达成"],
        conclusion: Annotated[str, "成果摘要或具体失败原因"],
    ) -> tuple[
        Annotated[bool, "声明是否有效"],
        Annotated[dict[str, object], "完成声明"],
    ]:
        """校验并规范化目标完成声明。

        Args:
            success: 目标是否已成功达成。
            conclusion: 成果摘要或具体失败原因。

        Returns:
            声明是否有效，以及规范化后的完成声明或错误信息。
        """
        if not isinstance(success, bool):
            return False, {"error": "success 必须是布尔值"}
        if not isinstance(conclusion, str):
            return False, {"error": "conclusion 必须是字符串"}
        normalized_conclusion = conclusion.strip()
        if not normalized_conclusion:
            return False, {"error": "conclusion 不能为空"}
        return True, {
            "success": success,
            "conclusion": normalized_conclusion,
        }
