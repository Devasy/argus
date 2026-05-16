from pydantic import BaseModel


class PromptBlocks(BaseModel):
    static: str
    per_mr: str
    per_agent: str


def assemble_system_prompt(blocks: PromptBlocks) -> str:
    """Cache-aligned assembly. Byte order is a contract: static, per_mr, per_agent."""
    return "\n\n".join([blocks.static, blocks.per_mr, blocks.per_agent])
