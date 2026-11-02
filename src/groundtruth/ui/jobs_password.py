"""The password that guards Preferences › Compute server › Clear past jobs (the user, 2026-11-02: "offer a clear past
jobs button, maybe under preferences under compute server? ideally, have some sort of password that is required to
do so").

Only a salted hash is kept, in this computer's ui.json under PREF_KEY: PBKDF2-HMAC-SHA256 with a random 16-byte salt
and ITERATIONS rounds (OWASP's 2023 figure for that hash), never the password itself; a check compares in constant
time (hmac.compare_digest). It guards against clearing the list by accident or in passing: someone with access to
this Windows account can still delete the job files themselves. Hashing takes ~0.4-0.9 s, so the window runs it off
its GUI thread (prefs_dialog). No wx here.
"""
from __future__ import annotations

import hashlib
import hmac
import os
from typing import Optional

PREF_KEY = "past_jobs_password"
ALGO = "pbkdf2_sha256"
ITERATIONS = 600_000
MIN_LENGTH = 6


def record(prefs: dict) -> Optional[dict]:
    """The stored password record from the preferences, or None when none is set (or it is unreadable)."""
    rec = prefs.get(PREF_KEY)
    if not isinstance(rec, dict) or rec.get("algo") != ALGO:
        return None
    if not all(isinstance(rec.get(k), str) for k in ("salt", "hash")) or not isinstance(rec.get("iterations"), int):
        return None
    return rec


def _derive(password: str, salt: bytes, iterations: int) -> bytes:
    """The PBKDF2-HMAC-SHA256 key of a password (slow on purpose)."""
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)


def make_record(password: str) -> dict:
    """A new record for `password` (a fresh salt), to store under PREF_KEY. Slow: run it off the GUI thread."""
    salt = os.urandom(16)
    return {"algo": ALGO, "iterations": ITERATIONS, "salt": salt.hex(),
            "hash": _derive(password, salt, ITERATIONS).hex()}


def matches(rec: dict, password: str) -> bool:
    """Whether `password` is the one the record was made from (constant-time comparison). Slow: off the GUI
    thread."""
    try:
        salt, want = bytes.fromhex(rec["salt"]), bytes.fromhex(rec["hash"])
    except (KeyError, ValueError, TypeError):
        return False
    return hmac.compare_digest(_derive(password, salt, int(rec["iterations"])), want)


def problem(password: str, again: str) -> str:
    """Why a new password is refused ("" when it is fine): too short, or the two entries differ."""
    if len(password) < MIN_LENGTH:
        return f"the password needs at least {MIN_LENGTH} characters"
    if password != again:
        return "the two entries differ"
    return ""
