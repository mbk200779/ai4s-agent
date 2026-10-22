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
        """获取临时访问链接。"""
        try:
            return self.client.presigned_get_object(bucket, key, expires=expires)
        except S3Error:
            return ""

    def get_pdf_bytes(self, key: str) -> bytes:
        """下载 PDF 字节。"""
        resp = self.client.get_object(self.papers_bucket, key)
        return resp.read()

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
