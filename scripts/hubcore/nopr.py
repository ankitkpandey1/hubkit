"""A routed non-review item that names no PR, whose owner reported done: nothing to check the claim against.

Such an item never closes on its own (the close needs an artifact on a PR). After the owner's report the hub is nudged to
check the result itself and close the item with a note saying what it checked: in the status block, in the prompt lines
(once per stall interval, `routed.stall_min`, not on every prompt) and as a tick alert (the tick's own back-off)."""

from __future__ import annotations

from . import routed, status, store


def waiting(cfg: dict, work: list[dict]) -> list[dict]:
    """Routed, non-review items with a report from the owner and no PR (or queued PR) to check it against."""
    return [i for i in work if i.get("kind") == "routed" and i.get("reported") and not i.get("review") and not i.get("prs") and not i.get("queue")]


def line(i: dict) -> str:
    if i.get("done_when"):  # the brief declared what shows it is done
        return (f"{i['id']}: {i['owner']} reported done. Check: {i['done_when']}. "
                f"NEXT: run the check, then hubkit work done {i['id']} --note \"<what the check showed>\"")
    return (f"{i['id']}: {i['owner']} reported done, nothing to check it against. "
            f"NEXT: check the result yourself, then hubkit work done {i['id']} --note \"<what you checked>\"")


def block_lines(cfg: dict, work: list[dict]) -> list[str]:
    return [line(i) for i in waiting(cfg, work)]


def alerts(cfg: dict, now: float) -> list[dict]:
    return [{"key": f"{i['id']}:no_pr_reported", "msg": line(i)} for i in waiting(cfg, status.work_load(store.state(cfg)))]


def prompt_lines(cfg: dict, now: float) -> list[str]:
    """The nudge lines due now: an item is nudged again only after `routed.stall_min` minutes."""
    every = float(routed.opts(cfg)["stall_min"]) * 60
    sd = store.state(cfg)
    if not waiting(cfg, status.work_load(sd)):
        return []
    out = []
    with store.locked(sd / "work.lock"):
        all_ = status.work_load(sd)
        for i in waiting(cfg, all_):
            if now - float(i.get("nopr_nudged") or 0) >= every:
                i["nopr_nudged"] = now
                out.append(line(i))
        if out:
            status.work_save(sd, all_)
    return out
