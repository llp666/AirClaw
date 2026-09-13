"""AirClaw API 路由层。所有路由以 /api 为前缀挂载。

模块划分：

    chat.py        POST /api/chat      SSE 流式对话
    sessions.py    会话 CRUD、标题生成、对话压缩（归档 + 摘要）
    files.py       文件读写（路径白名单校验，供前端编辑器）
    skills.py      技能包上传 / 卸载 / 列表
    knowledge.py   语料上传待审 / 确认入库 / 退回 / 删除 / 重建索引
    tokens.py      Token 统计（tiktoken cl100k_base）
    config_api.py  运行配置（RAG 模式、操作者身份）
    rag.py         RAG 检索诊断（供入库管理员查看召回明细）

每个模块暴露一个名为 `router` 的 APIRouter 实例，由 app.py 统一 include_router。
"""
