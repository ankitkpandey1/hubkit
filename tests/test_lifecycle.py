"""The lifecycle table (hubcore/lifecycle.py): well-formed, agreeing with the code that moves orders, routed work, requests
and replies, refusing illegal transitions with a NEXT hint, audited against the state files, and matching docs/architecture.md.

Everything runs on scratch state (the `env` fixture). No stand-ins."""

import json
import re
from pathlib import Path

import pytest
from conftest import SCRIPTS, SKILL  # noqa: F401  (puts the scripts on sys.path)

from hubcore import config, lifecycle, owed, replies, routed, status, store
from hubcore.orders import Orders

KINDS = ["order", "routed", "request", "reply"]
H = "a" * 40


@pytest.fixture
def cfg(env, monkeypatch):
    monkeypatch.setenv("HUBKIT_STATE", str(env.state))
    monkeypatch.setenv("HUBKIT_CONFIG", str(env.cfg))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(env.root / "xdg"))
    c = config.load()
    store.state(c).mkdir(parents=True, exist_ok=True)
    return c


# ---- the table is well formed ----

def test_every_transition_names_known_states_and_proofs():
    for k in KINDS:
        known = set(lifecycle.STATES[k]) | {lifecycle.NEW}
        for a, v, b, req in lifecycle.TRANSITIONS[k]:
            assert a in known and b in lifecycle.STATES[k], (k, a, v, b)
            assert set(req) <= {"evidence", "quote", "note", "sha", "github"}, (k, v, req)
            assert v and b != lifecycle.NEW


def test_terminal_states_have_no_outgoing_transitions_and_live_ones_have_a_clock_or_say_clockless():
    for k in KINDS:
        assert lifecycle.terminal_states(k) and lifecycle.live_states(k)
        for a, v, _b, _r in lifecycle.TRANSITIONS[k]:
            assert a not in lifecycle.terminal_states(k), (k, a, v)
        for s in lifecycle.live_states(k):
            assert lifecycle.clocks(k, s) or s in lifecycle.CLOCKLESS.get(k, ()), (k, s)
        for s in lifecycle.terminal_states(k):
            assert not lifecycle.clocks(k, s)


def test_every_live_state_is_reachable_and_can_end():
    for k in KINDS:
        reached = {lifecycle.NEW}
        grew = True
        while grew:
            grew = False
            for a, _v, b, _r in lifecycle.TRANSITIONS[k]:
                if a in reached and b not in reached:
                    reached.add(b)
                    grew = True
        assert reached - {lifecycle.NEW} == set(lifecycle.STATES[k]), k
        for s in lifecycle.live_states(k):
            assert any(a == s and b in lifecycle.terminal_states(k) for a, _v, b, _r in lifecycle.TRANSITIONS[k]), (k, s)


def test_the_code_uses_the_tables_live_and_dead_order_states():
    from hubcore import orders

    assert orders.LIVE_STATES == ("unconfirmed", "open", "held")
    assert set(orders.DEAD_STATES) == {"closed", "cancelled", "expired"}


# ---- illegal transitions are refused, with a NEXT ----

def test_check_refuses_an_unallowed_transition_and_a_missing_proof_with_a_next_hint():
    with pytest.raises(lifecycle.LifecycleError, match=r"cannot close from cancelled.*NEXT:"):
        lifecycle.check("order", "cancelled", "close", evidence={})
    with pytest.raises(lifecycle.LifecycleError, match=r"cannot done from done.*NEXT:"):
        lifecycle.check("request", "done", "done", evidence="shows it is done")
    with pytest.raises(lifecycle.LifecycleError, match=r"cannot replied from replied.*NEXT:"):
        lifecycle.check("reply", "replied", "replied", note="sent")
    with pytest.raises(lifecycle.LifecycleError, match=r"needs evidence.*NEXT:"):
        lifecycle.check("request", "open", "done", evidence="  ")
    with pytest.raises(lifecycle.LifecycleError, match=r"needs quote.*NEXT:"):
        lifecycle.check("order", "open", "cancel")
    with pytest.raises(lifecycle.LifecycleError, match="NEXT:"):
        lifecycle.check("nothing", "open", "close")
    lifecycle.check("routed", "reported", "close", github={"x": 1})  # any one of github, evidence, note
    assert lifecycle.allowed("order", "held", "resume") and not lifecycle.allowed("order", "open", "resume")


def test_the_code_refuses_what_the_table_refuses(cfg):
    o = Orders(cfg)
    x = o.open("code_red", "prod is down")
    o.cancel(x["id"], "never mind")
    with pytest.raises(ValueError, match="was cancelled"):
        o.close(x["id"], {"resolved": "all fine here"})  # the older, specific message is kept
    with pytest.raises(lifecycle.LifecycleError, match="NEXT:"):
        o.hold(x["id"], "wait")  # a cancelled order cannot be held
    with pytest.raises(lifecycle.LifecycleError, match="NEXT:"):
        o.resume(x["id"])
    y = o.open("code_red", "second one")
    with pytest.raises(lifecycle.LifecycleError, match="cannot confirm from open"):
        o.confirm(y["id"])
    r = owed.add(cfg, "delete the stale branches", 1000.0)
    owed.done(cfg, r["id"], "git branch -r shows 0 stale branches")
    with pytest.raises(KeyError, match="no open request"):
        owed.done(cfg, r["id"], "git branch -r shows 0 stale branches")  # the old message; the item is gone


def test_replied_on_a_closed_ask_is_refused(cfg):
    sd = store.state(cfg)
    status.work_save(sd, [{"id": "W1", "kind": "reply", "owner": "peer-a", "title": "t", "note": "n", "added": 1.0, "msg_id": "5", "prs": []}])
    replies.mark_replied(cfg, "W1", "sent: done")
    with pytest.raises(KeyError, match="no open reply item"):
        replies.mark_replied(cfg, "W1", "sent: again")


# ---- the code and the table agree: every state seen and every transition taken is in the table ----

class Seen:
    def __init__(self, monkeypatch):
        self.calls: list[tuple[str, str, str]] = []
        real = lifecycle.check

        def spy(kind, state, verb, **given):
            self.calls.append((kind, state, verb))
            return real(kind, state, verb, **given)

        monkeypatch.setattr(lifecycle, "check", spy)

    def taken(self, kind):
        return {(s, v) for k, s, v in self.calls if k == kind}


def _order_state(o, oid):
    return o.all()[oid]["state"]


def _expect(kind, before, verb, after):
    assert after in lifecycle.next_states(kind, before, verb), (kind, before, verb, after)


def test_orders_move_only_along_the_table(cfg, monkeypatch):
    seen = Seen(monkeypatch)
    o = Orders(cfg)
    a = o.open("code_red", "outage one", state="unconfirmed")["id"]
    _expect("order", lifecycle.NEW, "open", _order_state(o, a))
    for verb, call in [("confirm", lambda: o.confirm(a)), ("hold", lambda: o.hold(a, "hold it")), ("resume", lambda: o.resume(a)),
                       ("hold", lambda: o.hold(a, "hold again"))]:
        before = _order_state(o, a)
        call()
        _expect("order", before, verb, _order_state(o, a))
    before = _order_state(o, a)
    o.close(a, {"resolved": "all fine here"})
    _expect("order", before, "close", _order_state(o, a))
    b = o.open("code_red", "outage two")["id"]
    before = _order_state(o, b)
    o.cancel(b, "never mind")
    _expect("order", before, "cancel", _order_state(o, b))
    c = o.open("code_red", "outage three", state="unconfirmed")["id"]
    before = _order_state(o, c)
    o.expire(c, "no answer")
    _expect("order", before, "expire", _order_state(o, c))
    assert {s for s, _ in seen.taken("order")} <= set(lifecycle.STATES["order"])
    assert all(lifecycle.allowed("order", s, v) for s, v in seen.taken("order"))
    assert {"confirm", "hold", "resume", "close", "cancel", "expire"} <= {v for _, v in seen.taken("order")}
    assert {o.all()[i]["state"] for i in (a, b, c)} <= set(lifecycle.STATES["order"])


def _item(cfg, wid):
    return next((i for i in status.work_load(store.state(cfg)) if i["id"] == wid), None)


def test_requests_move_only_along_the_table(cfg, monkeypatch):
    seen = Seen(monkeypatch)
    ids = [owed.add(cfg, f"ask number {n} for the team", 1000.0)["id"] for n in range(3)]
    s = lifecycle.state_of("request", _item(cfg, ids[0]))
    assert s == "open"
    owed.note(cfg, ids[0], "listed them", 1100.0)
    _expect("request", s, "note", lifecycle.state_of("request", _item(cfg, ids[0])))
    s = lifecycle.state_of("request", _item(cfg, ids[0]))
    owed.chase(cfg, ids[0], 1200.0)
    _expect("request", s, "chase", lifecycle.state_of("request", _item(cfg, ids[0])))
    s = lifecycle.state_of("request", _item(cfg, ids[0]))
    owed.note(cfg, ids[0], "more progress", 1300.0)
    _expect("request", s, "note", lifecycle.state_of("request", _item(cfg, ids[0])))
    owed.chase(cfg, ids[1], 1200.0)
    owed.done(cfg, ids[0], "git branch -r shows 0 stale branches")
    owed.drop(cfg, ids[1], "forget it")
    owed.done(cfg, ids[2], "git branch -r shows 0 stale branches")
    assert seen.taken("request") >= {("open", "note"), ("noted", "chase"), ("chased", "note"), ("chased", "done"), ("chased", "drop"), ("open", "done")}
    assert all(lifecycle.allowed("request", s, v) for s, v in seen.taken("request"))


def test_replies_move_only_along_the_table(cfg, monkeypatch):
    seen = Seen(monkeypatch)
    sd = store.state(cfg)
    status.work_save(sd, [{"id": f"W{n}", "kind": "reply", "owner": "peer-a", "title": "t", "note": "n", "added": 1.0, "msg_id": str(n), "prs": []}
                          for n in (1, 2)])
    assert lifecycle.state_of("reply", _item(cfg, "W1")) == "open"
    replies.note_states(cfg, {}, 5.0)  # no PR named: never due
    assert lifecycle.state_of("reply", _item(cfg, "W1")) == "open"
    items = status.work_load(sd)
    items[1]["due"] = True
    status.work_save(sd, items)
    assert lifecycle.state_of("reply", _item(cfg, "W2")) == "due"
    replies.mark_replied(cfg, "W1", "sent: ok")
    replies.mark_replied(cfg, "W2", "sent: ok")
    assert seen.taken("reply") == {("open", "replied"), ("due", "replied")}


def test_routed_work_moves_only_along_the_table(cfg, monkeypatch):
    seen = Seen(monkeypatch)
    i = routed.record(cfg, "peer-a", "fix the flaky test", [], {}, now=1000.0)
    assert lifecycle.state_of("routed", _item(cfg, i["id"])) == "routed"
    routed.record(cfg, "peer-a", "status?", [], {}, now=1100.0, chase=True)
    assert lifecycle.state_of("routed", _item(cfg, i["id"])) == "chased"
    routed.record(cfg, "peer-a", "status once more?", [], {}, now=1150.0, chase=True)
    sd = store.state(cfg)
    items = status.work_load(sd)
    items[0]["reported"] = 1200.0
    status.work_save(sd, items)
    assert lifecycle.state_of("routed", _item(cfg, i["id"])) == "reported"
    routed.record(cfg, "peer-a", "status again?", [], {}, now=1300.0, chase=True)
    assert lifecycle.state_of("routed", _item(cfg, i["id"])) == "reported"
    items = status.work_load(sd)
    items[0]["answered"] = True
    status.work_save(sd, items)
    assert lifecycle.state_of("routed", _item(cfg, i["id"])) == "answered"
    routed._archive(cfg, _item(cfg, i["id"]), "answered", 1400.0)
    assert {(s, v) for s, v in seen.taken("routed")} >= {("routed", "chase"), ("chased", "chase"), ("reported", "chase"), ("answered", "close"), ("new", "route")}
    assert all(lifecycle.allowed("routed", s, v) for s, v in seen.taken("routed"))
    assert lifecycle.next_states("routed", "reported", "chase") == ["reported"]


# ---- the audit ----

def test_audit_is_clean_on_a_healthy_state_and_flags_a_hand_corrupted_one(cfg):
    o = Orders(cfg)
    o.open("code_red", "outage")
    owed.add(cfg, "delete the stale branches", 1000.0)
    assert lifecycle.audit(cfg) == []
    assert lifecycle.audit_line(cfg) == "lifecycle: ok"
    path = store.state(cfg) / "orders.jsonl"
    rows = [json.loads(ln) for ln in path.read_text().splitlines()]
    rows[0]["state"] = "zombie"
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    found = lifecycle.audit(cfg)
    assert any("zombie" in p and "not in the lifecycle table" in p for p in found)
    assert lifecycle.audit_line(cfg).startswith("lifecycle: order ")


def test_audit_flags_a_live_state_whose_clock_has_no_source(cfg, monkeypatch):
    owed.add(cfg, "delete the stale branches", 1000.0)
    monkeypatch.setitem(lifecycle.STATES["request"], "open", (True, ("cfg:requests.no_such_key",)))
    assert any("no_such_key has no source" in p for p in lifecycle.audit(cfg))
    monkeypatch.setitem(lifecycle.STATES["request"], "open", (True, ()))
    assert any("request open is live with no clock" in p for p in lifecycle.audit(cfg))


def test_doctor_prints_the_lifecycle_line(env):
    out = env.run("doctor").stdout
    assert "lifecycle: ok" in out


# ---- the docs cannot drift from the table ----

def test_docs_architecture_lifecycle_section_matches_describe():
    text = (SKILL / "docs" / "architecture.md").read_text(encoding="utf-8")
    m = re.search(r"<!-- lifecycle:begin -->\n```text\n(.*?)\n```\n<!-- lifecycle:end -->", text, re.S)
    assert m, "docs/architecture.md needs the Lifecycle section between the lifecycle markers"
    assert m[1].splitlines() == lifecycle.describe()
    assert Path(SKILL / "scripts" / "hubcore" / "lifecycle.py").exists()
