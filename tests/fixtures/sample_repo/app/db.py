"""Database access."""

import sqlite3
from contextlib import contextmanager


@contextmanager
def open_connection(path: str):
    """Open a database connection and close it when done."""
    conn = sqlite3.connect(path)
    try:
        yield conn
    finally:
        conn.close()


class UserRepository:
    def __init__(self, conn):
        self.conn = conn

    async def find_user(self, username):
        row = self.conn.execute(
            "SELECT * FROM users WHERE name = ?", (username,)
        ).fetchone()
        return row

    @staticmethod
    def save_user(
        conn,
        username: str,
        password_hash: str,
    ) -> None:
        conn.execute("INSERT INTO users VALUES (?, ?)", (username, password_hash))
