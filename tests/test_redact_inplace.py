"""Redaction replaces each secret in place and keeps the words around it; ordinary prose is not a secret."""

from hubcore.redact import count_hits, redact, redact_obj
from hubcore.store import safe

SHA = "58810605d48e989e5c1d6d18287e08f8daace2f0"


def test_prose_with_author_or_tokenizer_or_a_sha_is_kept_whole():
    for t in ("author: bob fixed the parser and then pushed", "the tokenizer: handles unicode",
              f"commit {SHA} broke prod, revert it and tell me", "the authentication flow: needs a retry"):
        assert redact(t) == (t, 0), t


def test_a_secret_in_the_middle_keeps_the_text_on_both_sides():
    out, hits = redact("before ghp_" + "a1" * 15 + " after, and api_token=zz99 then done")
    assert hits == 2 and out == "before [REDACTED] after, and [REDACTED] then done"


def test_whole_key_names_and_segments_are_caught():
    for t in ("AUTH_TOKEN=abc123", "x-auth-token: abc123", "api_token=abc123", "password: hunter2value", "Authorization: Bearer abcdefgh12345"):
        out, hits = redact(f"see {t} ok")
        assert hits >= 1 and "abc123" not in out and "hunter2value" not in out and "abcdefgh12345" not in out, t
        assert out.startswith("see ") and out.endswith(" ok"), out


def test_long_runs_hex_ids_are_kept_and_mixed_case_runs_are_masked():
    assert redact(f"id {SHA} and {'ab12' * 8}")[1] == 0
    assert redact("key " + "Ab1" * 12)[0] == "key [REDACTED]"
    assert redact("sha256 " + "ab12" * 16)[1] == 1  # 64 hex is not a commit id


def test_count_hits_matches_redact():
    text = "x ghp_" + "a1" * 15 + " y"
    assert count_hits(text) == redact(text)[1] == 1


def test_safe_still_never_leaks_a_cut_secret_prefix():
    tok = "ghp_" + "a1" * 15
    out = safe("x" * 295 + tok, 300)
    assert "ghp_" not in out and "a1a1" not in out
    assert redact_obj({"q": "so " + tok + " it"})["q"] == "so [REDACTED] it"
