"""Authentication helpers."""

import hashlib


class UserService:
    """Look up users and check their credentials."""

    def __init__(self, db):
        self.db = db

    def authenticate(self, username: str, password: str) -> bool:
        """Return True when the password matches the stored hash."""
        user = self.db.find_user(username)
        if user is None:
            return False
        return user.password_hash == hash_password(password)


def hash_password(password: str) -> str:
    """Hash a password with SHA-256."""
    return hashlib.sha256(password.encode()).hexdigest()
