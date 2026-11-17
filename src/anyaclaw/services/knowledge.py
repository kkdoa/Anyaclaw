"""Slash-command helpers for the long-term memory / knowledge base."""

from __future__ import annotations

from anyaclaw.core.memory import MemoryStore

HELP = """记忆库命令：
/memory            列出最近记忆
/memory <关键词>   搜索记忆
/remember <内容>   写入一条记忆
/forget <内容|id>  删除记忆
"""


class KnowledgeBase:
    def __init__(self, memory: MemoryStore) -> None:
        self.memory = memory

    def help_text(self) -> str:
        return HELP

    async def list_text(self, query: str = "") -> str:
        if query:
            memories = await self.memory.search(query, limit=20)
            if not memories:
                return f"没有找到与 {query!r} 相关的记忆。"
            return "相关记忆：\n" + "\n".join(f"#{m.id} {m.content}" for m in memories)

        memories = await self.memory.list(limit=50)
        if not memories:
            return "记忆库为空。用 /remember <内容> 添加。"
        return "记忆库：\n" + "\n".join(f"#{m.id} {m.content}" for m in memories)

    async def remember_text(self, content: str) -> str:
        memory = await self.memory.remember(content)
        return f"已记住 #{memory.id}：{memory.content}"

    async def forget_text(self, target: str) -> str:
        removed = await self.memory.forget(target)
        return f"已删除 {removed} 条记忆。" if removed else f"没有匹配 {target!r} 的记忆。"

    async def export_markdown(self) -> str:
        return await self.memory.export_markdown()
