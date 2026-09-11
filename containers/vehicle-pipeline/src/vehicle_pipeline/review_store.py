from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool


@dataclass(frozen=True)
class ReviewItem:
    id: int
    paperless_doc_id: int
    paperless_doc_title: str
    paperless_doc_url: str
    vin: str | None
    mygarage_entity: str
    extracted_category: str
    payload: dict[str, Any]
    confidence: str | None
    status: str
    mygarage_record_id: str | None


class ReviewQueueStore:
    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool

    def create_draft(
        self,
        *,
        paperless_doc_id: int,
        paperless_doc_title: str,
        paperless_doc_url: str,
        vin: str | None,
        mygarage_entity: str,
        extracted_category: str,
        payload: dict[str, Any],
        confidence: str | None,
    ) -> int:
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO vehicle_pipeline.review_items
                    (paperless_doc_id, paperless_doc_title, paperless_doc_url, vin,
                     mygarage_entity, extracted_category, payload, confidence)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    paperless_doc_id,
                    paperless_doc_title,
                    paperless_doc_url,
                    vin,
                    mygarage_entity,
                    extracted_category,
                    Jsonb(payload),
                    confidence,
                ),
            )
            row = cur.fetchone()
            assert row is not None
            return int(row[0])

    def list_pending(self) -> list[ReviewItem]:
        return self._select("WHERE status = 'pending' ORDER BY created_at ASC")

    def get(self, item_id: int) -> ReviewItem | None:
        items = self._select("WHERE id = %s", (item_id,))
        return items[0] if items else None

    def update_payload(self, item_id: int, payload: dict[str, Any]) -> None:
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE vehicle_pipeline.review_items SET payload = %s WHERE id = %s",
                (Jsonb(payload), item_id),
            )

    def mark_approved(self, item_id: int, mygarage_record_id: str) -> None:
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE vehicle_pipeline.review_items
                SET status = 'approved', mygarage_record_id = %s, reviewed_at = now()
                WHERE id = %s
                """,
                (mygarage_record_id, item_id),
            )

    def mark_rejected(self, item_id: int) -> None:
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE vehicle_pipeline.review_items
                SET status = 'rejected', reviewed_at = now()
                WHERE id = %s
                """,
                (item_id,),
            )

    def _select(self, where_clause: str, params: tuple[Any, ...] = ()) -> list[ReviewItem]:
        with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"""
                SELECT id, paperless_doc_id, paperless_doc_title, paperless_doc_url, vin,
                       mygarage_entity, extracted_category, payload, confidence, status,
                       mygarage_record_id
                FROM vehicle_pipeline.review_items
                {where_clause}
                """,
                params,
            )
            return [ReviewItem(**row) for row in cur.fetchall()]
