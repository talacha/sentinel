"""Users and passwords for the visitor and admin logins.

Passwords are stored only as salted scrypt hashes in `users.json` (owner-only permissions), never
in plaintext, and are never returned by any API. The environment still defines the first two users
(`ADMIN_USER`/`ADMIN_PASSWORD` and `ACCESS_USER`/`ACCESS_PASSWORD`) so an existing deployment keeps
working: those entries follow the environment until an admin resets the password in the console,
after which the console's password wins and the environment value is ignored for that user.
Admins can also add users and edit a user's role and password in the console.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

log = logging.getLogger(__name__)

Role = Literal["admin", "visitor"]
Source = Literal["env", "console"]

MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 128
WEAK_PASSWORDS = {"password", "demo", "sentinel", "changeme", "admin", "letmein", "123456"}

MAX_USERS = 100
# No colon (HTTP Basic splits at the first one), no spaces, and a letter or digit first.
_USERNAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@-]{0,63}")

_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2**14, 8, 1
_CACHE_TTL_SECONDS = 60.0
_CACHE_MAX = 1024


class PasswordPolicyError(ValueError):
    """A new password was rejected (the message never contains the password)."""


class UserError(ValueError):
    """A user change was refused. `field` is the form field to show the message against;
    `conflict` marks "already exists" (HTTP 409) as opposed to invalid input (422)."""

    def __init__(self, message: str, field: str = "user", conflict: bool = False):
        super().__init__(message)
        self.field = field
        self.conflict = conflict


@dataclass(frozen=True)
class UserChange:
    """What an update actually did (for the audit log; never contains a password)."""

    role_from: Role | None = None
    role_to: Role | None = None
    password_changed: bool = False


# --------------------------------------------------------------------------- hashing


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32
    )
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time check of `password` against a stored hash; malformed hashes never verify."""
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(digest)
        candidate = hashlib.scrypt(
            password.encode("utf-8"),
            salt=base64.b64decode(salt),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(expected),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(candidate, expected)


def validate_password(username: str, password: str) -> None:
    if not isinstance(password, str) or len(password) < MIN_PASSWORD_LENGTH:
        raise PasswordPolicyError(f"must be at least {MIN_PASSWORD_LENGTH} characters")
    if len(password) > MAX_PASSWORD_LENGTH:
        raise PasswordPolicyError(f"must be at most {MAX_PASSWORD_LENGTH} characters")
    if password.lower() in WEAK_PASSWORDS or password.lower() == username.lower():
        raise PasswordPolicyError("is too easy to guess")
    if password != password.strip():
        raise PasswordPolicyError("must not start or end with whitespace")


def validate_username(username: str) -> None:
    if not isinstance(username, str) or not _USERNAME_RE.fullmatch(username):
        raise UserError(
            "use 1 to 64 letters, digits, or . _ @ - (starting with a letter or digit; no spaces)",
            field="username",
        )


def generate_password() -> str:
    """A strong random password (24 URL-safe characters)."""
    return secrets.token_urlsafe(18)


# --------------------------------------------------------------------------- the store


@dataclass
class User:
    username: str
    role: Role
    hash: str
    source: Source
    bootstrap: Role | None  # which environment slot defines this user, if any
    created_at: str
    password_changed_at: str


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class UserStore:
    def __init__(self, path: Path | None = None):
        self.path = path
        self.load_error: str | None = None
        self._lock = threading.RLock()
        self._users: dict[str, User] = {}
        self._cache: dict[bytes, tuple[str, float]] = {}
        self._cache_key = os.urandom(32)  # per-process: cached entries are useless elsewhere
        self._dummy_hash: str | None = None
        self._load()

    # -- persistence -------------------------------------------------------------------

    def _load(self) -> None:
        if self.path is None or not self.path.is_file():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            for raw in data["users"]:
                user = User(**raw)
                self._users[user.username] = user
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self._users = {}
            self.load_error = f"users file unreadable, ignored: {type(exc).__name__}"
            log.error("%s (%s)", self.load_error, self.path)

    def _save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        payload = json.dumps(
            {"version": 1, "users": [u.__dict__ for u in self._users.values()]}, indent=2
        )
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        os.replace(tmp, self.path)

    # -- environment bootstrap ---------------------------------------------------------

    def sync_env(
        self,
        *,
        admin_user: str,
        admin_password: str | None,
        visitor_user: str,
        visitor_password: str | None,
    ) -> None:
        """Make the environment-defined users exist (and follow the environment until reset)."""
        with self._lock:
            changed = self._sync_slot("admin", "admin", admin_user, admin_password)
            if visitor_user == admin_user and visitor_password:
                log.warning("ACCESS_USER equals ADMIN_USER; the visitor login was not created")
            else:
                changed |= self._sync_slot("visitor", "visitor", visitor_user, visitor_password)
            if changed:
                self._cache.clear()
                self._save()

    def _sync_slot(self, slot: Role, role: Role, name: str, password: str | None) -> bool:
        existing = next((u for u in self._users.values() if u.bootstrap == slot), None)
        if not password or not name:
            if existing is not None and existing.source == "env":
                del self._users[existing.username]  # no longer defined by the environment
                return True
            return False
        now = _now()
        if existing is None:
            if name in self._users:  # a console user already has this name: never overwrite it
                log.warning("environment user %r not created: a console user has that name", name)
                return False
            self._users[name] = User(name, role, hash_password(password), "env", slot, now, now)
            return True
        changed = False
        if existing.username != name and name not in self._users:
            del self._users[existing.username]
            existing.username = name
            self._users[name] = existing
            changed = True
        if existing.source == "env" and not verify_password(password, existing.hash):
            existing.hash = hash_password(password)  # the environment password was rotated
            existing.password_changed_at = now
            changed = True
        return changed

    # -- authentication ----------------------------------------------------------------

    def login_required(self) -> bool:
        """The visitor login is on exactly when at least one visitor user exists."""
        with self._lock:
            return any(u.role == "visitor" for u in self._users.values())

    def _cache_id(self, username: str, password: str) -> bytes:
        return hmac.new(
            self._cache_key, f"{username}\0{password}".encode(), hashlib.sha256
        ).digest()

    def authenticate(self, username: str, password: str) -> User | None:
        """Return the user if the credentials are right (unknown users cost the same work)."""
        with self._lock:
            user = self._users.get(username)
            stored = user.hash if user else None
            cache_id = self._cache_id(username, password)
            hit = self._cache.get(cache_id)
            if hit and user is not None and hit[0] == user.hash and hit[1] > time.monotonic():
                return user
        if stored is None:
            if self._dummy_hash is None:
                self._dummy_hash = hash_password("not-a-real-password")
            verify_password(password, self._dummy_hash)  # equalize timing; result is ignored
            return None
        if not verify_password(password, stored):
            return None
        with self._lock:
            current = self._users.get(username)
            if current is None or current.hash != stored:
                # The password was changed (or the user removed) while this one was being checked:
                # a reset must end the old password at once, not one hash computation later.
                return None
            if len(self._cache) >= _CACHE_MAX:
                self._cache.clear()
            self._cache[cache_id] = (stored, time.monotonic() + _CACHE_TTL_SECONDS)
            return current

    # -- management --------------------------------------------------------------------

    def _role_lock(self, user: User) -> str | None:
        """Why this user's role cannot be changed, or None. Call with the lock held."""
        if user.bootstrap is not None:
            return "Set by the server's environment (ADMIN_USER / ACCESS_USER)."
        same = sum(1 for u in self._users.values() if u.role == user.role)
        if same <= 1 and user.role == "admin":
            return "The only admin: the console would have no one to sign in."
        if same <= 1 and user.role == "visitor":
            return "The only visitor: changing it would turn the visitor login off."
        return None

    def _describe(self, u: User) -> dict[str, Any]:
        return {
            "username": u.username,
            "role": u.role,
            "source": "environment" if u.source == "env" else "console",
            "bootstrap": u.bootstrap is not None,
            "role_locked": self._role_lock(u),
            "created_at": u.created_at,
            "password_changed_at": u.password_changed_at,
        }

    def list(self) -> list[dict[str, Any]]:
        """Users for display: the admin and visitor defined by the environment come first."""
        with self._lock:
            order = {"admin": 0, "visitor": 1}
            users = sorted(
                self._users.values(),
                key=lambda u: (order.get(u.bootstrap or "", 2), u.created_at, u.username),
            )
            return [self._describe(u) for u in users]

    def describe(self, username: str) -> dict[str, Any]:
        with self._lock:
            return self._describe(self._users[username])

    def get(self, username: str) -> User | None:
        with self._lock:
            return self._users.get(username)

    def create(self, username: str, role: str, password: str) -> None:
        """Add a console user. Raises UserError (bad name or role, duplicate, too many) or
        PasswordPolicyError; nothing is stored unless everything is valid."""
        with self._lock:
            validate_username(username)
            if role not in ("admin", "visitor"):
                raise UserError("must be admin or visitor", field="role")
            if any(u.username.lower() == username.lower() for u in self._users.values()):
                raise UserError("a user with that name already exists", "username", conflict=True)
            if len(self._users) >= MAX_USERS:
                raise UserError(f"at most {MAX_USERS} users", field="username")
            validate_password(username, password)
            now = _now()
            self._users[username] = User(
                username, role, hash_password(password), "console", None, now, now
            )
            self._cache.clear()
            self._save()

    def update(
        self,
        username: str,
        *,
        role: str | None = None,
        password: str | None = None,
        actor: str | None = None,
    ) -> UserChange:
        """Change a user's role and/or password, all or nothing. `actor` is who is asking: nobody
        can change their own role. Raises KeyError (unknown user), UserError, PasswordPolicyError.
        """
        with self._lock:
            user = self._users[username]
            if role is not None and role not in ("admin", "visitor"):
                raise UserError("must be admin or visitor", field="role")
            new_role = role if role is not None and role != user.role else None
            if new_role is not None:
                if username == actor:
                    raise UserError("You cannot change your own role.", field="role")
                reason = self._role_lock(user)
                if reason:
                    raise UserError(reason, field="role")
            if password is not None:
                validate_password(username, password)
            if new_role is None and password is None:
                raise UserError("nothing to change")
            change = UserChange(
                role_from=user.role if new_role else None,
                role_to=new_role,
                password_changed=password is not None,
            )
            if new_role is not None:
                user.role = new_role
            if password is not None:
                user.hash = hash_password(password)
                user.source = "console"
                user.password_changed_at = _now()
            self._cache.clear()  # an old password must stop working immediately
            self._save()
            return change

    def reset_password(self, username: str, password: str) -> None:
        """Set a new password (validated). Raises KeyError for an unknown user."""
        self.update(username, password=password)
