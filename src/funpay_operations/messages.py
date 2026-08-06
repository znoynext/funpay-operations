"""Message-template persistence. No message delivery is implemented here."""

from __future__ import annotations

from .database import Database


class MessageTemplateRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def save(self, name: str, body: str) -> None:
        if not name.strip() or not body.strip():
            raise ValueError("Template name and body must not be empty")
        with self.database.session() as connection:
            connection.execute(
                """
                INSERT INTO message_templates (name, body)
                VALUES (?, ?)
                ON CONFLICT(name) DO UPDATE SET body = excluded.body, updated_at = CURRENT_TIMESTAMP
                """,
                (name, body),
            )
