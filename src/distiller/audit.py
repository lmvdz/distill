"""Audit: a second opinion on each card's claims from the text model, without the video.

Verification asks whether the creator said it. The audit asks whether it is true: a creator
can be wrong, and verification passes that. The audit only flags (SPEC section 10); the
catalog shows flagged claims as notes and removes nothing, since the auditor never saw the
clip and can be wrong too."""

import asyncio
import hashlib
import json
import logging
from typing import Any

from .db import Queue
from .forms import CLAIM_LISTS, FLAGGED, Audit
from .model import Model, ModelError

log = logging.getLogger("distill")

AUDIT = """You are a senior graphics, shader and VFX engineer reviewing the claims a tutorial made
about one technique. <data> holds the technique and its claims by id. Judge each claim on
whether it is true, from what you know, not on whether the tutorial said it: that was already
checked against the video. `correct`: true as stated. `wrong`: false as stated, e.g. bad
maths, a reversed effect, a misnamed principle. `overstated`: true only in narrower cases than
stated, or given as the only way when it is one of several. `unsure`: you cannot tell without
seeing the video, or it depends on context not given. Recipe values are the creator's example
values: judge the method, not the taste. For wrong or overstated, give the claim as it should
read. Check every id."""


def card_claims(body: dict[str, Any]) -> dict[str, str]:
    """Every claim on one card by id: the idea, then each list, e.g. `m0` for the first
    mechanism claim. The ids match `forms.claim_ids` without the `c0.` card prefix."""
    out = {"idea": body["idea"]}
    for letter, field in CLAIM_LISTS:
        for j, item in enumerate(body.get(field) or []):
            out[f"{letter}{j}"] = item["text"]
    return out


def audit_hash(body: dict[str, Any]) -> str:
    """Changes when the card's claims or the audit prompt change."""
    return hashlib.sha256((json.dumps(card_claims(body)) + AUDIT).encode()).hexdigest()


def _input(card: dict[str, Any]) -> str:
    body = card["body"]
    return json.dumps(
        {"technique": card["technique"], "result": body.get("result"), "claims": card_claims(body)},
        ensure_ascii=False,
    )


def is_current(card: dict[str, Any], done: dict[str, Any] | None, model: str) -> bool:
    return bool(done and done["card_hash"] == audit_hash(card["body"]) and done["model"] == model)


def notes(card: dict[str, Any], done: dict[str, Any] | None) -> list[dict[str, Any]]:
    """The flagged claims of one card, with their text. An audit made for older claims says
    nothing about these ones, so it is ignored."""
    if not done or done["card_hash"] != audit_hash(card["body"]):
        return []
    claims = card_claims(card["body"])
    return [
        {**c, "text": claims[c["claim"]], "model": done["model"]}
        for c in done["checks"]
        if c["verdict"] in FLAGGED and c["claim"] in claims
    ]


async def _one(q: Queue, model: Model, name: str, card: dict[str, Any]) -> bool:
    try:
        audit = await model.fill(Audit, AUDIT, data=_input(card))
    except ModelError as err:
        # Not saved, so the next run tries this card again.
        log.warning("audit of card %s failed: %s", card["id"], str(err)[:160])
        return False
    known = card_claims(card["body"])
    checks = [c for c in audit.checks if c.claim in known]
    body = Audit(checks=checks).model_dump_json()
    q.save_audit(card["id"], audit_hash(card["body"]), name, body)
    return True


async def audit(
    q: Queue, model: Model, name: str, *, force: bool = False, limit: int | None = None
) -> tuple[int, int, int, int]:
    """Audits every card not yet audited by `name` for its current claims, at most `limit`.
    Returns (audited, failed, already current, left for a later run)."""
    cards = [c for c in q.cards() if "idea" in c["body"]]
    done = q.audits()
    pending = [c for c in cards if force or not is_current(c, done.get(c["id"]), name)]
    todo = pending[:limit] if limit is not None else pending
    log.info(
        "audit: %d cards to check with %s, %d current", len(todo), name, len(cards) - len(pending)
    )
    ok = await asyncio.gather(*(_one(q, model, name, c) for c in todo))
    return sum(ok), len(ok) - sum(ok), len(cards) - len(pending), len(pending) - len(todo)


def report(q: Queue) -> list[dict[str, Any]]:
    """Every flagged claim, by card, for `distill audit --report`."""
    done = q.audits()
    out = []
    for card in q.cards():
        if "idea" not in card["body"]:
            continue
        for n in notes(card, done.get(card["id"])):
            out.append(
                {"card": card["id"], "technique": card["technique"], "url": card["url"], **n}
            )
    return out
