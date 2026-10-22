"""
评测服务：运行可复现性/可用性检查并把报告写入 MinIO。
"""
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List

import requests

from app.services.retrieval_service import RetrievalService
from app.services.storage import StorageService
from app.services.vector_store import VectorStoreService


class EvalService:
    """
    关键设计决策：评测不依赖外部人工标注，而是检查系统自身健康与核心可复现性
    （同一查询两次 RRF 结果是否一致），并把每次报告持久化到对象存储，形成历史。
    """

    TEST_QUERY = "machine learning"

    def __init__(self):
        self.storage = StorageService()
        self.retrieval = RetrievalService()
        self.vector = VectorStoreService()

    def run(self) -> Dict[str, Any]:
        """运行一次完整评测并写入 provenance bucket。"""
        report = {
            "id": uuid.uuid4().hex,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "checks": {},
        }
        report["checks"]["api_health"] = self._check_api_health()
        report["checks"]["qdrant_health"] = self._check_qdrant_health()
        report["checks"]["minio_health"] = self._check_minio_health()
        report["checks"]["retrieval_reproducibility"] = self._check_retrieval_reproducibility()

        key = f"eval/{report['timestamp'][:10]}/{report['id']}.json"
        self.storage.upload_provenance(
            key,
            json.dumps(report, ensure_ascii=False, sort_keys=True).encode("utf-8"),
        )
        report["key"] = key
        return report

    def list_reports(self, limit: int = 50) -> List[Dict[str, Any]]:
        """列出历史评测报告（按 key 降序）。"""
        objects = self.storage.list_objects(
            self.storage.provenance_bucket, prefix="eval/", limit=limit
        )
        return sorted(objects, key=lambda x: x.get("key", ""), reverse=True)

    def get_report(self, key: str) -> Dict[str, Any]:
        """读取指定报告内容。"""
        data = self.storage.get_object_bytes(self.storage.provenance_bucket, key)
        return json.loads(data.decode("utf-8"))

    def _check_api_health(self) -> Dict[str, Any]:
        try:
            resp = requests.get("http://localhost:8000/api/v1/health/", timeout=5)
            return {"ok": resp.status_code == 200, "status_code": resp.status_code}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def _check_qdrant_health(self) -> Dict[str, Any]:
        try:
            self.vector.client.get_collections()
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def _check_minio_health(self) -> Dict[str, Any]:
        try:
            self.storage.client.list_buckets()
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def _check_retrieval_reproducibility(self) -> Dict[str, Any]:
        try:
            run1 = self.retrieval.search(self.TEST_QUERY, top_k=5, rrf_k=60)
            run2 = self.retrieval.search(self.TEST_QUERY, top_k=5, rrf_k=60)
            ids1 = [c.chunk_id for c in run1["fusion"]]
            ids2 = [c.chunk_id for c in run2["fusion"]]
            return {"ok": ids1 == ids2, "run1_ids": ids1, "run2_ids": ids2}
        except Exception as e:
            return {"ok": False, "error": str(e)}
