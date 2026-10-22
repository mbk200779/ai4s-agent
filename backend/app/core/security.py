"""
安全相关工具：本地复现不强制鉴权，云端 Demo 启用评审账号。
"""
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from app.core.config import settings

security = HTTPBasic(auto_error=False)


def require_judge(credentials: HTTPBasicCredentials | None = Depends(security)) -> bool:
    """
    若云端启用评审账号，校验 credentials；本地开发可跳过。
    """
    if not credentials:
        return True
    if credentials.username == settings.judge_username and credentials.password == settings.judge_password:
        return True
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid judge credentials",
        headers={"WWW-Authenticate": "Basic"},
    )
