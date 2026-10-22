from pydantic import BaseModel, Field


class OutlineRequest(BaseModel):
    topic: str = Field(..., description="论文主题")
    top_k: int = Field(default=8, description="参考片段数")


class OutlineResponse(BaseModel):
    topic: str
    outline: str
