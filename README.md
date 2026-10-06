# MC Goal Agent

Minecraft 目标代理插件：基于 ReAct（Reasoning + Acting）循环让 bot 自主完成长线目标。

## 功能概述

本插件为 MoFox 框架提供「让 bot 像 Agent 一样完成目标」的长线调度能力。用户在 Minecraft 平台聊天流发送指令后，Agent 会：

1. **观察**：读取目标与历史步骤
2. **思考**：调用 LLM 决策下一步
3. **行动**：LLM 选择 MC 工具 → 执行（移动/采集/合成/攻击等）
4. **反思**：观察工具结果，更新历史，进入下一轮

一次消息触发后，后台 Agent 会在同一个执行中连续调用 LLM 与工具，无需用户
发送新消息推动下一步。直到 Agent 显式调用 `complete_goal`、达到最大步数或
发生不可恢复错误。

## 部署

Neo-MoFox 插件加载器只扫描 `plugins/` 的一级子目录。本目录是适配器套件中的
独立插件子项目，不能只把整个 `mofox_mc_adapter` 套件目录放进 `plugins/` 后
期待递归发现。请把本目录单独链接或复制为一级插件：

```bash
# 在 Neo-MoFox 根目录；Windows 可使用目录联接或直接复制
ln -s /path/to/mofox_mc_adapter/goal_agent_plugin plugins/mc_goal_agent
```

同时部署 `client/` 为 `plugins/mc_adapter`、`tools_plugin/` 为
`plugins/mc_tools`。加载后组件签名为
`mc_goal_agent:agent:mc_goal_agent`。

## 核心特性

- **ReAct 循环引擎**：继承 `BaseAgent`，复用 MoFox 官方 Agent 机制
- **多目标并发**：基于 `TaskManager` 起后台协程，支持同时跑多个目标
- **持久化恢复**：目标步骤写入 `data/json_storage/mc_goal_agent/`，重启后沿用原 `goal_id` 和历史步骤继续执行
- **动态工具收集**：收集已激活的 Minecraft 工具，并使用各工具所属插件实例执行
- **显式完成协议**：私有 `complete_goal(success, conclusion)` 工具可靠区分成功与失败
- **结果通知**：按配置发送周期进度，并在成功、失败、取消或异常后回送最终结果
- **关键词触发**：用户消息以「代理完成: <目标>」开头即触发

## 触发方式

在 Minecraft 平台聊天流发送消息，格式：

```
代理完成: 收集 64 个圆石
代理完成: 去寻找一个村庄
代理完成: 合成一把铁镐
```

- 触发关键词可在配置 `agent.trigger_keyword` 修改（默认 `代理完成`）
- 支持全角 `：` 与半角 `:` 冒号分隔
- 关键词不区分大小写

## 配置说明

配置文件位于 `config/plugins/mc_goal_agent/config.toml`（首次加载自动生成）。

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `plugin.enabled` | `true` | 插件总开关 |
| `plugin.config_version` | `1.0.0` | 配置版本 |
| `agent.max_steps` | `20` | 单个目标最大 ReAct 步数 |
| `agent.progress_notify` | `true` | 是否发送进度通知 |
| `agent.progress_interval` | `3` | 进度通知间隔（每 N 步） |
| `agent.recover_on_start` | `true` | 启动时恢复未完成目标 |
| `agent.trigger_keyword` | `代理完成` | 触发关键词前缀 |

## 架构设计

### 组件

| 组件 | 签名 | 类型 | 说明 |
|------|------|------|------|
| `MCGoalAgent` | `mc_goal_agent:agent:mc_goal_agent` | agent | ReAct 引擎核心 |
| `MCGoalTriggerHandler` | `mc_goal_agent:event_handler:mc_goal_trigger` | event_handler | 消息触发器 |

### 文件结构

```
goal_agent_plugin/
├── __init__.py              # 插件包入口
├── plugin.py                # 插件注册与生命周期
├── manifest.json            # 插件清单
├── config.py                # 配置定义
└── components/
    ├── __init__.py          # 组件导出
    ├── models.py            # 数据模型（GoalRecord/GoalStep/GoalState）
    ├── store.py             # 持久化层（storage_api）
    ├── completion.py        # Agent 私有 complete_goal 工具
    ├── agent.py             # MCGoalAgent ReAct 引擎
    ├── manager.py           # GoalAgentManager 并发管理
    └── event_handler.py     # MCGoalTriggerHandler 触发器
```

### 执行链路

```
用户消息 "代理完成: 收集圆石"
        │
        ▼
ON_MESSAGE_RECEIVED 事件
        │
        ▼
MCGoalTriggerHandler.execute()
  ├─ 校验 platform == minecraft
  ├─ 匹配关键词前缀
  ├─ 提取目标描述
  └─ GoalAgentManager.start_goal()
        │
        ▼
  TaskManager.create_task()  ← 立即返回 goal_id
        │
        ▼ (后台协程)
  agent_api.execute_agent(signature="mc_goal_agent:agent:mc_goal_agent")
        │
        ▼
  MCGoalAgent.execute(objective, max_steps)
        ├─ 创建 GoalRecord → 持久化
        ├─ _run_react_loop:
        │    ├─ create_llm_request(with_usables=True)
        │    ├─ 注入 SYSTEM/USER payload
        │    └─ 循环 max_steps:
        │         ├─ LLM 返回 call_list?
        │         ├─ complete_goal? → 按 success 显式结束
        │         ├─ execute_local_usable() 执行工具
        │         ├─ add_payload(TOOL_RESULT)
        │         ├─ 持久化 GoalStep
        │         ├─ 周期进度通知
        │         └─ response.send() 续发下一轮
        ├─ 更新 state (COMPLETED/FAILED)
        ├─ 持久化 conclusion
        └─ 回送最终状态、结论与步骤数
```

## 依赖

- **mc_adapter**：提供 Minecraft 平台适配与 bridge 服务
- **mc_tools**：提供 MC 工具集（作为 Agent 的私有 usables）

本插件不在 `dependent_components` 中声明硬依赖，而是通过
`associated_platforms=["minecraft"]` 在运行时动态过滤可用工具。

## 数据存储

目标执行记录持久化到 `data/json_storage/mc_goal_agent/`：

- 文件名：`<goal_id>.json`
- 内容：`GoalRecord.to_dict()`（含所有步骤、状态、结论）
- 可通过 `GoalStore.list_all()` / `list_active()` / `list_by_stream()` 查询

## 开发说明

### 框架 Agent 边界

Neo-MoFox 已提供 `BaseAgent`、Agent 私有 usables、统一 usable 执行器、
`agent_api.execute_agent()` 与 `TaskManager`，但 `BaseAgent` 不会自动执行
ReAct 循环。具体 Agent 仍需负责“LLM → 工具 → 结果 → 下一轮 LLM”的循环。
本插件保留目标持久化和有界循环，同时复用上述框架能力。

### 显式完成协议

内部 LLM 只能通过 Agent 私有工具结束目标：

```text
complete_goal(success=true, conclusion="已收集 64 个圆石")
complete_goal(success=false, conclusion="目标区域不可达")
```

`complete_goal` 必须单独调用。普通自然语言即使包含“任务完成”也不会被视为
成功；没有工具调用会作为协议失败结束，避免“尚未完成”被关键词误判。

### ReAct 循环参考

Agent 的 ReAct 循环实现参考 MoFox 官方文档
`docs/guides/plugin-authoring/13-agent-orchestration.md`：

- `create_llm_request(model_set, with_usables=True)` 注入私有工具
- `execute_local_usable(name, **kwargs)` 执行工具调用
- `response.add_payload(ROLE.TOOL_RESULT, ToolResult(...))` 回填结果
- `response.send(stream=False)` 续发下一次请求

### 动态工具收集

`MCGoalAgent._get_extra_usables()` 重写基类方法，从组件注册表筛选：

```python
registry.get_by_type(ComponentType.TOOL)
  → 过滤 associated_platforms 含 "minecraft"
  → 过滤 status == ACTIVE
```

动态工具仍属于原注册插件。`BaseAgent.execute_local_usable()` 会读取组件的
`_plugin_` 元数据并取得对应插件实例，避免用 `mc_goal_agent` 的配置错误构造
`mc_tools` 或其他 Minecraft 插件提供的工具。

### 重启恢复

插件加载时 `GoalAgentManager.recover_pending_goals()` 会：

- 读取所有未完成目标（pending/running/paused）
- running 重置为 pending 后使用原 `goal_id` 重新投递
- `MCGoalAgent` 读取原记录和历史步骤，从剩余步数继续执行
- 不创建重复目标记录

## 许可证

见项目根目录 LICENSE。
