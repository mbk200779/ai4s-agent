"""
全局配置模块
关键设计决策：所有外部连接参数均通过环境变量注入，本地与云端共用同一份代码，
避免在代码里写死任何环境相关常量。
"""
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # 复现性控制
    temperature: float = 0.0
    prompt_version: str = "v1.0.0"

    # Qdrant 向量库
    qdrant_url: str = "http://localhost:6333"
    qdrant_collection: str = "ai4s_papers"
    qdrant_paper_meta_collection: str = "ai4s_paper_meta"

    # Redis / Celery
    redis_url: str = "redis://localhost:6379/0"

    # MinIO 对象存储
    minio_endpoint: str = "localhost:9000"
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = "minioadmin"
    minio_bucket_papers: str = "papers"
    minio_bucket_figures: str = "figures"
    minio_bucket_provenance: str = "provenance"
    minio_secure: bool = False

    # LLM
    litellm_model: str = "openai/gpt-4o-mini"
    litellm_cheap_model: str = "openai/gpt-4o-mini"  # 量大/结构化降本模型，如 qwen-max
    litellm_vision_model: str = "openai/gpt-4o-mini"  # 多模态模型

    # 外部 API key（可选）
    openai_api_key: str = ""
    anthropic_api_key: str = ""
    dashscope_api_key: str = ""
    s2_api_key: str = ""

    # 评审账号（云端）
    judge_username: str = "judge"
    judge_password: str = "ai4s2024"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"


settings = Settings()
