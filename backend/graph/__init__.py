"""AirClaw Agent 核心逻辑层。

模块划分：

    agent.py            AgentManager —— 构建 & 流式调用，astream() 逐事件产出
    session_manager.py  会话持久化（JSON 文件，v1 数组格式自动迁移到 v2）
    prompt_builder.py   System Prompt 组装器（运行时上下文 + 6 组件按序拼接）
    memory_indexer.py   MEMORY.md 混合检索索引（按条目切块，独立于知识库索引）
    daily_log.py        每日工作日志（每轮对话落盘后由后端追加，只增不改）
    audit_hooks.py      pre_hook / post_hook 审计钩子（审查拦截 + 结果记录）
    tool_dedup.py       单轮对话内的工具调用去重
    llm.py              模型接入（内网 OpenAI 兼容服务）+ 思考内容提取
    embedding.py        Embedding 接入 + 向量摘要工具

Agent 编排使用 LangChain 1.x 的 create_agent：

    from langchain.agents import create_agent

严禁使用旧版 AgentExecutor 或 create_react_agent。
"""
