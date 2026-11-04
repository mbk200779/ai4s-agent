"""
对象存储服务：原始 PDF、页面图、生成图表、provenance 日志统一存入 MinIO。
"""
from io import BytesIO
from minio import Minio
from minio.error import S3Error
from app.core.config import settings


class StorageService:
    """
    MinIO 客户端封装，提供上传、下载、 presigned URL 能力。
    关键设计决策：所有 bucket 在 docker-compose 初始化时创建，代码只做读写，
    避免运行时因权限竞争导致 bucket 创建失败。
    """

    def __init__(self):
        self.client = Minio(
            settings.minio_endpoint,
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key,
            secure=settings.minio_secure,
        )
        self.papers_bucket = settings.minio_bucket_papers
        self.figures_bucket = settings.minio_bucket_figures
        self.provenance_bucket = settings.minio_bucket_provenance

    def upload_pdf(self, key: str, data: bytes) -> str:
        """上传原始 PDF，返回对象 key。"""
        self.client.put_object(
            self.papers_bucket,
            key,
            BytesIO(data),
            length=len(data),
            content_type="application/pdf",
        )
        return key

    def upload_figure(self, key: str, data: bytes, content_type: str = "image/png") -> str:
        """上传图片/图表对象。"""
        self.client.put_object(
            self.figures_bucket,
            key,
            BytesIO(data),
            length=len(data),
            content_type=content_type,
        )
        return key

    def put_object(
        self, bucket: str, key: str, data: bytes, content_type: str = "application/octet-stream"
    ) -> str:
        """通用对象上传。"""
        self.client.put_object(
            bucket,
            key,
            BytesIO(data),
            length=len(data),
            content_type=content_type,
        )
        return key

    def upload_provenance(self, key: str, data: bytes, content_type: str = "application/json") -> str:
        """上传 provenance 日志。"""
        return self.put_object(self.provenance_bucket, key, data, content_type)

    def get_presigned_url(self, bucket: str, key: str, expires: int = 3600) -> str:
        """获取临时访问链接。新版 minio SDK 要求 timedelta 类型；
        用外部 endpoint（localhost:9000）签名，保证浏览器可直接访问。"""
        try:
            from datetime import timedelta
            import os
            # 容器内 endpoint（minio:9000）签名出的 URL 浏览器无法访问，
            # 改用宿主机可达的 localhost:9000 生成签名（端口已映射）。
            external_endpoint = os.environ.get("MINIO_EXTERNAL_ENDPOINT", "localhost:9000")
            external_client = Minio(
                external_endpoint,
                access_key=settings.minio_access_key,
                secret_key=settings.minio_secret_key,
                secure=settings.minio_secure,
            )
            return external_client.presigned_get_object(
                bucket, key, expires=timedelta(seconds=expires)
            )
        except Exception:
            # S3Error 之外还可能出现连接错误（容器内 localhost 不可达等），
            # 统一降级为空串，避免 500；调用方走代理端点兜底。
            return ""

    def get_pdf_bytes(self, key: str) -> bytes:
        """下载 PDF 字节。"""
        resp = self.client.get_object(self.papers_bucket, key)
        return resp.read()

    def get_figure_bytes(self, key: str) -> tuple:
        """下载 figures bucket 对象，返回 (bytes, content_type)。"""
        resp = self.client.get_object(self.figures_bucket, key)
        data = resp.read()
        ctype = resp.headers.get("Content-Type") if resp.headers else None
        return data, ctype

    def list_objects(self, bucket: str, prefix: str = "", recursive: bool = True, limit: int = 100):
        """列出对象，返回 {key, size, last_modified} 列表。"""
        objects = self.client.list_objects(bucket, prefix=prefix, recursive=recursive)
        results = []
        for obj in objects:
            results.append({
                "key": obj.object_name,
                "size": obj.size,
                "last_modified": obj.last_modified.isoformat() if obj.last_modified else None,
            })
            if len(results) >= limit:
                break
        return results

    def get_object_bytes(self, bucket: str, key: str) -> bytes:
        """下载任意对象字节。"""
        resp = self.client.get_object(bucket, key)
        return resp.read()
