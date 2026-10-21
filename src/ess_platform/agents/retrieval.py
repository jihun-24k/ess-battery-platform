"""학습 문서를 버전·적용 자산으로 제한해 등록하고 근거 ID를 포함한 chunk를 검색한다."""

import hashlib
import re

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Engine

from ess_platform.db import document_chunks, documents
from ess_platform.operations.incidents import MANUAL_PATH


def seed_learning_manual(engine: Engine) -> dict:
    """입력: DB engine. 반환: 학습 문서 ID. 같은 버전·내용은 재사용하고 변조는 거부한다."""
    raw = MANUAL_PATH.read_text(encoding="utf-8")
    digest = hashlib.sha256(raw.encode()).hexdigest()
    with engine.begin() as conn:
        doc_id = conn.execute(
            insert(documents)
            .values(
                document_key="learning-measurement-anomaly",
                version="learning-1",
                title="가상 ESS 측정 이상·온도·냉각 대응",
                asset_type="simulation_cell",
                approval_scope="simulation",
                tags=["온도", "냉각", "이상", "temperature", "cooling"],
                content=raw,
                sha256=digest,
            )
            .on_conflict_do_nothing(index_elements=["document_key", "version"])
            .returning(documents.c.id)
        ).scalar_one_or_none()
        if doc_id is None:
            row = (
                conn.execute(
                    select(documents).where(
                        documents.c.document_key == "learning-measurement-anomaly",
                        documents.c.version == "learning-1",
                    )
                )
                .mappings()
                .one()
            )
            if row["sha256"] != digest:
                raise ValueError("same manual version has different content")
            return {"document_id": row["id"], "status": "existing"}
        parts = re.split(r"(?m)(?=^## )", raw)
        chunks = [
            part[i : i + 1800]
            for part in parts
            for i in range(0, len(part), 1800)
            if part[i : i + 1800].strip()
        ]
        for position, content in enumerate(chunks):
            conn.execute(
                document_chunks.insert().values(
                    document_id=doc_id,
                    position=position,
                    content=content,
                    sha256=hashlib.sha256(content.encode()).hexdigest(),
                )
            )
        return {"document_id": doc_id, "status": "inserted"}


def search_manuals(engine: Engine, asset_type: str, query: str, limit: int) -> list[dict]:
    """입력: DB·확정 자산 종류·검색어·개수. 반환: 적용 가능한 문서 chunk와 검증된 출처
    목록.
    """
    terms = set(re.findall(r"[\w]+", query.lower()))
    aliases = {"temperature": "온도", "cooling": "냉각", "anomaly": "이상"}
    terms |= {aliases[t] for t in terms if t in aliases}
    with engine.connect() as conn:
        rows = (
            conn.execute(
                select(
                    document_chunks.c.id.label("chunk_id"),
                    document_chunks.c.content,
                    document_chunks.c.sha256.label("chunk_hash"),
                    documents.c.id.label("document_id"),
                    documents.c.version,
                    documents.c.title,
                    documents.c.sha256,
                    documents.c.approval_scope,
                    documents.c.tags,
                    documents.c.content.label("full_content"),
                )
                .join(documents, documents.c.id == document_chunks.c.document_id)
                .where(
                    documents.c.active.is_(True),
                    documents.c.asset_type == asset_type,
                    documents.c.approval_scope == "simulation",
                )
                .order_by(document_chunks.c.id)
                .limit(200)
            )
            .mappings()
            .all()
        )
    ranked = []
    for row in rows:
        if hashlib.sha256(row["full_content"].encode()).hexdigest() != row["sha256"]:
            continue
        if hashlib.sha256(row["content"].encode()).hexdigest() != row["chunk_hash"]:
            continue
        text = (row["title"] + " " + " ".join(row["tags"]) + " " + row["content"]).lower()
        score = sum(term in text for term in terms)
        if score:
            ranked.append(
                (score, {k: v for k, v in row.items() if k not in {"full_content", "tags"}})
            )
    ranked.sort(key=lambda pair: (-pair[0], pair[1]["chunk_id"]))
    return [{**item, "ref": f"document:{item['chunk_id']}"} for _, item in ranked[:limit]]
