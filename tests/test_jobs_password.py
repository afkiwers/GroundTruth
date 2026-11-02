"""The password that guards Preferences › Clear past jobs (ui/jobs_password.py): only a salted PBKDF2 hash is kept,
the right password matches, a wrong one does not, two records of one password differ (their salts)."""
import json

from groundtruth.ui import jobs_password as jp


def test_only_a_salted_hash_is_kept_and_it_checks_the_password():
    rec = jp.make_record("correct horse")
    assert rec["algo"] == jp.ALGO and rec["iterations"] == jp.ITERATIONS >= 600_000
    assert "correct horse" not in json.dumps(rec) and len(bytes.fromhex(rec["salt"])) == 16
    assert jp.matches(rec, "correct horse") and not jp.matches(rec, "correct horsE") and not jp.matches(rec, "")
    assert jp.make_record("correct horse")["hash"] != rec["hash"]             # a fresh salt each time
    assert jp.record({jp.PREF_KEY: rec}) == rec and jp.record({}) is None
    assert jp.record({jp.PREF_KEY: {"algo": "plain", "hash": "x"}}) is None    # an unknown record is no password
    assert not jp.matches({"salt": "zz", "hash": "00", "iterations": 1}, "x")  # unreadable: never a match


def test_a_new_password_needs_a_length_and_the_same_twice():
    assert jp.problem("abc", "abc").startswith("the password needs at least")
    assert jp.problem("abcdef", "abcdeg") == "the two entries differ"
    assert jp.problem("abcdef", "abcdef") == ""
