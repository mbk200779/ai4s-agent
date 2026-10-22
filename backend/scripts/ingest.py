"""
批量入库脚本：支持本地 PDF 目录或 arXiv ID 列表。
用法示例：
    python scripts/ingest.py --pdf-dir /path/to/pdfs
    python scripts/ingest.py --arxiv 2401.12345 2402.56789
"""
import argparse
import os
import sys
from pathlib import Path
from typing import List

# 把 backend 加入 Python 路径
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.storage import StorageService
from app.services.arxiv_client import ArxivClient
from app.tasks.tasks import parse_and_index_pdf


def ingest_pdfs(pdf_dir: str):
    """批量上传本地 PDF 并派发解析任务。"""
    storage = StorageService()
    for path in Path(pdf_dir).glob("*.pdf"):
        content = path.read_bytes()
        if not content.startswith(b"%PDF"):
            print(f"跳过非 PDF: {path}")
            continue
        # 默认以文件名（不含扩展名）作为 arxiv_id
        arxiv_id = path.stem
        version = "v1"
        s3_key = f"{arxiv_id}/{version}/paper.pdf"
        storage.upload_pdf(s3_key, content)
        task = parse_and_index_pdf.delay(
            arxiv_id=arxiv_id,
            version=version,
            title=path.name,
            s3_key=s3_key,
        )
        print(f"已入队: {arxiv_id} task={task.id}")


def ingest_arxiv(ids: List[str]):
    """批量拉取 arXiv 论文并派发解析任务。"""
    client = ArxivClient()
    storage = StorageService()
    for raw_id in ids:
        meta = client.fetch_by_id(raw_id)
        if not meta:
            print(f"未找到: {raw_id}")
            continue
        pdf_bytes = client.download_pdf(meta["pdf_url"])
        s3_key = f"{meta['arxiv_id']}/{meta['version']}/paper.pdf"
        storage.upload_pdf(s3_key, pdf_bytes)
        task = parse_and_index_pdf.delay(
            arxiv_id=meta["arxiv_id"],
            version=meta["version"],
            title=meta["title"],
            s3_key=s3_key,
            authors=meta.get("authors", []),
            abstract=meta.get("abstract", ""),
            doi=meta.get("doi"),
        )
        print(f"已入队: {meta['arxiv_id']} task={task.id}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="AI4S 论文批量入库")
    parser.add_argument("--pdf-dir", help="本地 PDF 目录")
    parser.add_argument("--arxiv", nargs="+", help="arXiv ID 列表")
    args = parser.parse_args()

    if args.pdf_dir:
        ingest_pdfs(args.pdf_dir)
    elif args.arxiv:
        ingest_arxiv(args.arxiv)
    else:
        parser.print_help()
