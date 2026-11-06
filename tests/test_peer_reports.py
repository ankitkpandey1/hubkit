"""A peer's result message to the hub counts as heard and closes a non-review item. Real transcript shapes."""

import json
import time

from test_hubkit import PR1, age_routed, iso, queue_line, routed_items, send

PR2 = "https://github.com/acme/repo/pull/102"


def peer_entry(name, body, ago=30):
    """The user entry the harness writes when the hub takes a peer message up."""
    text = f'Another Claude session sent a message:\n<cross-session-message from="uds:x" from-name="{name}" from-mode="bypass">\n{body}\n</cross-session-message>'
    return json.dumps({"type": "user", "origin": {"kind": "peer", "from": "uds:x"}, "timestamp": iso(time.time() - ago), "message": {"content": text}})


def queued(name, body, ago=30):
    return queue_line(f'<cross-session-message from="uds:x" from-name="{name}" from-mode="bypass">\n{body}', iso(time.time() - ago))


def deliver(env, *lines):
    tr = env.transcripts / "sess-hub.jsonl"
    with open(tr, "a", encoding="utf-8") as fh:  # the transcript only grows
        fh.write("\n".join(lines) + "\n")
    return env.hook("prompt", {"session_id": "sess-hub", "prompt": "carry on", "transcript_path": str(tr)}).stdout


def archived(env):
    return [r for f in (env.state / "archive").glob("archive_*.jsonl") for r in map(json.loads, f.read_text().splitlines()) if r.get("type") == "work"]


def test_a_result_message_from_the_owner_closes_a_non_review_item_and_nothing_stalls(env):
    env.bind()
    send(env, "peer-a", f"Build the fix for {PR1}. Done-when: tests pass")
    age_routed(env, 40)
    out = deliver(env, peer_entry("peer-a", "Fix is pushed and the tests passed. PR 101 is ready."))
    assert "STALLED" not in out and routed_items(env) == []
    assert "owner reported its result" in archived(env)[0]["done_note"]


def test_a_queued_result_message_closes_it_too(env):
    env.bind()
    send(env, "peer-a", f"Build the fix for {PR1}. Done-when: tests pass")
    age_routed(env, 40)
    deliver(env, queued("peer-a", "step 4 done, all green"))
    assert routed_items(env) == []


def test_a_question_or_a_blocker_from_the_owner_is_heard_but_does_not_close(env):
    env.bind()
    send(env, "peer-a", f"Build the fix for {PR1}. Done-when: tests pass")
    age_routed(env, 40)
    out = deliver(env, peer_entry("peer-a", "Which branch should this be done on?"), peer_entry("peer-a", "Fix is pushed but the tests failed, I am blocked on a host.", 10))
    (item,) = routed_items(env)
    assert item["last_heard"] and "STALLED" not in out


def test_a_report_on_a_review_item_leaves_it_to_the_pr_review(env):
    env.bind()
    send(env, "peer-a", f"Full gated review of {PR1}", "review 101")
    age_routed(env, 40)
    deliver(env, peer_entry("peer-a", "Review done."))
    (item,) = routed_items(env)
    assert item["review"] and item["last_heard"]


def test_a_report_from_someone_else_closes_nothing(env):
    env.bind()
    send(env, "peer-a", f"Build the fix for {PR1}. Done-when: tests pass")
    age_routed(env, 40)
    deliver(env, peer_entry("peer-b", "All done and merged."))
    assert len(routed_items(env)) == 1


def test_an_owner_with_two_items_closes_only_the_one_the_report_names(env):
    env.bind()
    send(env, "peer-a", f"Build the fix for {PR1}. Done-when: tests pass")
    send(env, "peer-a", f"Build the other fix for {PR2}. Done-when: tests pass")
    age_routed(env, 40)
    deliver(env, peer_entry("peer-a", "Finished, all done."))
    assert len(routed_items(env)) == 2  # which job? the report does not say
    deliver(env, peer_entry("peer-a", "PR 102 is finished and merged.", 5))
    assert [i["prs"] for i in routed_items(env)] == [[PR1]]
