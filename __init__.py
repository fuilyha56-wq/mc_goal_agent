"""MC Goal Agent 插件包入口。

注意：这里不能 `from .plugin import MCGoalAgentPlugin` —— 插件加载器会先把
``mc_goal_agent.plugin`` 放进 ``sys.modules`` 再执行模块体，而 ``plugin.py``
里的相对导入（``from .components.agent import ...``）会先触发本包
``__init__.py`` 运行，此时 ``MCGoalAgentPlugin`` 类尚未定义，直接 re-import
会形成循环导入并导致插件加载失败。因此本文件保持 docstring-only，
与其它插件顶层 ``__init__.py`` 的既有约定一致。
"""
