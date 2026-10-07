"""The closed forms the model fills, and the code that decides from them.

The model never decides keep/drop or which claims survive. It fills these forms; the functions
at the bottom of this file make every decision.

What is collected is technique, independent of any application: the idea, how it works, the
decisions and knobs that matter, and the signals it consumes and produces, so techniques can be
composed into new effects and rebuilt anywhere. Where a technique was seen (Blender, Houdini, a
pixel-art editor) is provenance, not a category.
"""

import re
from typing import Any, Literal

from pydantic import BaseModel, Field

# Saved model answers are keyed with this, so a form change never replays an old-shape answer.
FORMS_VERSION = "v5"

# What techniques pass between each other. A technique consumes some and produces some; two
# compose when one's output type is another's input type.
Signal = Literal[
    "time",  # a 0..1 progress, curve or clock that drives change
    "mask",  # a per-pixel or per-vertex 0..1 field that selects where
    "coords",  # positions or UVs, and distortions of them
    "shape",  # geometry or a silhouette
    "deform",  # motion or displacement of geometry
    "color",  # color, emission, gradients mapped from values
    "particles",  # many small emitted elements
    "screen",  # post-processing of the whole frame
    "motion",  # how things move over time: timing, spacing, smears, stepped frames
    "normal",  # surface orientation: normals, tangents, bent or baked normals
    "light",  # light arriving at a surface: diffuse, specular, shadow, occlusion, bounce
    "distance",  # a signed distance or other unbounded scalar field
    "buffer",  # GPU data one pass writes and another reads: render targets, simulation state
]
SIGNALS: tuple[str, ...] = Signal.__args__  # type: ignore[attr-defined]

# Where a technique can be realized. `geometry` is procedural; `modeling` is shaping a mesh by
# hand (topology, bevels, retopology, UV layout); `compute` is GPU work outside the draw.
Medium = Literal[
    "shader",
    "geometry",
    "modeling",
    "compute",
    "lighting",
    "particles",
    "2d-frames",
    "post",
    "animation",
]
MEDIUMS: tuple[str, ...] = Medium.__args__  # type: ignore[attr-defined]


NAME_RULE = (
    "Name the idea in 2 to 6 words by what it does, never in the application's vocabulary: "
    "'stagger timing by remapping a shared clock', not 'insert a color ramp between map range "
    "and float curve'."
)


class PreScreen(BaseModel):
    """Filled from text alone (title, description, chapters, captions), before any download."""

    on_topic: bool = Field(
        description="About visual effects, real-time graphics (shaders, WebGL, WebGPU, GPU "
        "compute), 3D modeling or animation, not something else entirely."
    )
    explains_why: bool = Field(description="The text gives reasons, not just what was clicked.")
    has_mechanism_or_math_terms: bool = Field(
        description="Mentions maths, formulas, algorithms or a mechanism by name, not just UI."
    )
    mostly_ui_clicks: bool = Field(
        description="Reads like a menu-by-menu click-along with little or no reasoning."
    )
    is_showcase_or_reel: bool = Field(
        description="A result reel or short showcase with no explanation of how it was made."
    )
    depth: int = Field(
        ge=1,
        le=5,
        description="1 nothing transferable from what the text describes .. 5 dense, "
        "well-reasoned technique. A coarse guess from text alone, not the real screening.",
    )
    reasons: str = Field(description="One or two sentences behind the fields above.")


class Screen(BaseModel):
    teaches: bool = Field(
        description="Explains or clearly demonstrates how something is made: a tutorial, a "
        "breakdown or a walkthrough of a setup. A finished result alone does not teach."
    )
    teaches_reasoning: bool = Field(
        description="Gives the why: principles, mechanisms, decisions, trade-offs."
    )
    ui_walkthrough_only: bool = Field(
        description="Mostly which buttons to click and menus to open, with little reasoning."
    )
    is_reference: bool = Field(
        description="A finished effect, animation or look worth replicating is clearly on screen, "
        "whether or not it explains itself and even if the post is about something else."
    )
    made_with: str | None = Field(
        description="The application or medium it was made in, if visible or said: 'blender', "
        "'houdini', 'unreal', 'figma weave', 'pixel art', 'digital painting'. Else null."
    )
    signals: list[Signal] = Field(description="The kinds of signal the video is about.")
    quality: int = Field(
        ge=1,
        le=5,
        description="Insight density: 1 nothing transferable .. 5 dense ideas, explained.",
    )
    reasons: str = Field(description="One or two sentences behind the fields above.")


class Claim(BaseModel):
    text: str
    t: float = Field(description="Seconds into this clip where it is shown or said.")


class Card(BaseModel):
    technique: str = Field(description=NAME_RULE)
    made_with: str | None = Field(description="Where it was seen being made, as provenance only.")
    idea: str = Field(
        description="The core insight in one or two application-independent sentences: what "
        "the technique does and the principle that makes it work."
    )
    idea_t: float
    result: str = Field(description="One line on what the viewer sees when it is done.")
    consumes: list[Signal] = Field(description="Signals it takes as input; empty for a source.")
    produces: list[Signal] = Field(min_length=1, description="Signals it outputs.")
    mediums: list[Medium] = Field(min_length=1, description="Where it can be realized.")
    app_chore: bool = Field(
        description="The idea is really operating the tool: exposing a parameter, wiring up a "
        "UI, setting a file path or similar chore, not a technique someone would reuse elsewhere."
    )
    textbook_basic: bool = Field(
        description="A standard operation almost any introductory tutorial on this kind of work "
        "covers, e.g. a plain fade or a default light setup."
    )
    non_obvious: bool = Field(
        description="Gives an insight a competent practitioner in this field might not already "
        "know: an unusual combination, a subtle reason, a trick that isn't standard practice."
    )
    has_numbers: bool = Field(
        description="Concrete values, ratios or a formula are given, not just qualitative "
        "description."
    )
    mechanism: list[Claim] = Field(
        description="How and why it works: the maths, the signal flow, the perceptual reason."
    )
    decisions: list[Claim] = Field(
        description="Choices the creator makes and the reason given for each."
    )
    knobs: list[Claim] = Field(
        description="Parameters that matter and what changing each does to the result."
    )
    recipe: list[Claim] = Field(
        description="The build as application-independent maths or pseudo-code, e.g. "
        "'mask = step(threshold, fbm(p * 5))', with the creator's values as examples. No "
        "application names, menus, node names or shortcuts.",
    )
    pitfalls: list[Claim]


class Span(BaseModel):
    technique: str = Field(description=NAME_RULE)
    start: float = Field(description="Seconds where teaching it starts.")
    end: float = Field(description="Seconds where it ends.")


class Outline(BaseModel):
    techniques: list[Span]


class Link(BaseModel):
    source: str = Field(description="Technique name whose output is used, exactly as listed.")
    signal: Signal = Field(description="What flows along the link.")
    target: str = Field(description="Technique name that consumes it, exactly as listed.")
    role: str = Field(description="What it drives in the target, e.g. 'the dissolve threshold'.")
    why: str = Field(description="Why the creator connects them this way, as said or shown.")
    t: float


class Effect(BaseModel):
    name: str
    result: str = Field(description="What the finished effect looks like.")
    links: list[Link]


class Composition(BaseModel):
    effects: list[Effect]


class RefPart(BaseModel):
    what: str = Field(description="One visible element of the look, e.g. 'hot core gradient'.")
    signal: Signal
    how: str = Field(
        description="The most likely application-independent way to build it, as a hypothesis."
    )
    matches: list[str] = Field(
        description="Names from the catalog list in <data> that would build this part."
    )
    search: str = Field(description="A short search query for a tutorial teaching this part.")


class Reference(BaseModel):
    name: str
    result: str = Field(description="The look in one or two sentences.")
    parts: list[RefPart] = Field(min_length=1)
    composition: str = Field(
        description="How the parts likely combine: which drives which, as a hypothesis."
    )


Verdict = Literal["supported", "unsupported", "unclear"]


class Check(BaseModel):
    claim: str = Field(description="The claim id exactly as given, e.g. 'c0.r2'.")
    verdict: Verdict
    t: float | None = Field(description="Seconds where the evidence is, or null.")


class Verification(BaseModel):
    checks: list[Check]


AuditVerdict = Literal["correct", "wrong", "overstated", "unsure"]
# Verdicts the catalog shows as a note. Nothing is dropped: the auditor has not seen the video.
FLAGGED: tuple[AuditVerdict, ...] = ("wrong", "overstated")


class AuditCheck(BaseModel):
    claim: str = Field(description="The claim id exactly as given, e.g. 'm0'.")
    verdict: AuditVerdict
    reason: str = Field(description="One sentence on why.")
    correction: str | None = Field(
        description="For wrong or overstated: the claim as it should read. Otherwise null."
    )


class Audit(BaseModel):
    checks: list[AuditCheck]


# The checkable lists of a card and the id letter each claim carries.
CLAIM_LISTS: tuple[tuple[str, str], ...] = (
    ("m", "mechanism"),
    ("d", "decisions"),
    ("k", "knobs"),
    ("r", "recipe"),
    ("p", "pitfalls"),
)

Mode = Literal["card", "reference", "drop"]


def route(screen: Screen, threshold: int) -> tuple[Mode, str]:
    """Code's rule over the model's checklist: learn from it, study it as a target, or drop it."""
    if screen.teaches and screen.teaches_reasoning and screen.quality >= threshold:
        return "card", "teaches"
    if screen.is_reference:
        return "reference", "a look to replicate"
    if not screen.teaches:
        return "drop", "neither teaches nor shows a look worth replicating"
    if not screen.teaches_reasoning:
        return "drop", "no reasoning, only UI steps"
    return "drop", f"quality {screen.quality} < {threshold}"


def technique_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def claim_ids(cards: list[Card]) -> dict[str, str]:
    """Every checkable claim, keyed by a stable id the verifier cites."""
    out: dict[str, str] = {}
    for i, card in enumerate(cards):
        out[f"c{i}.idea"] = card.idea
        for letter, field in CLAIM_LISTS:
            for j, item in enumerate(getattr(card, field)):
                out[f"c{i}.{letter}{j}"] = item.text
    return out


def link_ids(comp: Composition) -> dict[str, str]:
    out: dict[str, str] = {}
    for i, effect in enumerate(comp.effects):
        for j, link in enumerate(effect.links):
            out[f"e{i}.l{j}"] = (
                f"{link.source} --{link.signal}--> {link.target} ({link.role}): {link.why}"
            )
    return out


def _in_clip(t: float | None, duration: float) -> bool:
    return t is not None and 0 <= t <= duration + 1


def _supported(checks: Verification, duration: float) -> set[str]:
    return {c.claim for c in checks.checks if c.verdict == "supported" and _in_clip(c.t, duration)}


def apply_verification(cards: list[Card], checks: Verification, duration: float) -> list[Card]:
    """Keeps only claims the verifier supported with a timestamp inside the clip.

    A card survives only if it is not an application chore, its idea survives, and at least one
    mechanism or recipe claim does. A claim the verifier did not mention counts as unsupported.
    """
    supported = _supported(checks, duration)
    kept: list[Card] = []
    for i, card in enumerate(cards):
        if card.app_chore or f"c{i}.idea" not in supported:
            continue
        lists = {
            field: [
                c for j, c in enumerate(getattr(card, field)) if f"c{i}.{letter}{j}" in supported
            ]
            for letter, field in CLAIM_LISTS
        }
        if not lists["mechanism"] and not lists["recipe"]:
            continue
        kept.append(card.model_copy(update=lists))
    return kept


def card_depth(card: Card) -> int:
    """0..5: how much this card is worth surfacing over an app chore or a textbook basic.

    The model only flags recognizable patterns (`textbook_basic`, `non_obvious`, `has_numbers`)
    and what survived verification; code turns that into one number. `app_chore` cards never
    reach this (`apply_verification` drops them), but a stray one still rates 0.
    """
    if card.app_chore:
        return 0
    base = 3 if card.non_obvious else (1 if card.textbook_basic else 2)
    substance = len(card.mechanism) + len(card.decisions) + len(card.pitfalls)
    base += min(substance, 2)
    if card.recipe and card.has_numbers:
        base += 1
    return max(0, min(base, 5))


def shift_card(card: Card, offset: float) -> Card:
    """A card with every timestamp moved by `offset`: span-clip time back to clip time."""
    update: dict[str, Any] = {"idea_t": round(card.idea_t + offset, 2)}
    for _, field in CLAIM_LISTS:
        update[field] = [
            c.model_copy(update={"t": round(c.t + offset, 2)}) for c in getattr(card, field)
        ]
    return card.model_copy(update=update)


def shift_verification(checks: Verification, offset: float) -> Verification:
    """A verification's timestamps moved by `offset`, matching a card shifted the same way."""
    return Verification(
        checks=[
            c.model_copy(update={"t": round(c.t + offset, 2) if c.t is not None else None})
            for c in checks.checks
        ]
    )


def prescreen_route(p: PreScreen, min_depth: int) -> tuple[bool, str]:
    """Whether to download at all, from captions alone. Uncertain proceeds: only an off-topic
    video, a clear click-along, or a depth clearly below the floor is dropped this early."""
    if not p.on_topic:
        return False, "off-topic"
    if p.mostly_ui_clicks and not p.explains_why:
        return False, "button clicks, no reasoning"
    if p.depth < min_depth:
        return False, f"depth {p.depth} < {min_depth}"
    return True, "proceed"


def apply_link_verification(
    comp: Composition, checks: Verification, duration: float, known: set[str]
) -> Composition:
    """Keeps links the verifier supported and whose two ends are techniques carded from this
    video; an effect with no surviving link is dropped."""
    supported = _supported(checks, duration)
    effects = []
    for i, effect in enumerate(comp.effects):
        links = [
            link
            for j, link in enumerate(effect.links)
            if f"e{i}.l{j}" in supported
            and technique_key(link.source) in known
            and technique_key(link.target) in known
        ]
        if links:
            effects.append(effect.model_copy(update={"links": links}))
    return Composition(effects=effects)


def clean_reference(ref: Reference, catalog: set[str]) -> Reference:
    """Keeps only catalog matches that exist; a part with none is a gap to learn."""
    parts = [
        p.model_copy(update={"matches": [m for m in p.matches if technique_key(m) in catalog]})
        for p in ref.parts
    ]
    return ref.model_copy(update={"parts": parts})


def gaps(ref: Reference) -> list[str]:
    """Search queries for the parts nothing in the catalog builds yet."""
    return [p.search.strip() for p in ref.parts if not p.matches and p.search.strip()]


# --- Workflow mode: modding pipelines where using the tools is the content -------------------

GameVersion = Literal["1.12", "2.4.3", "3.3.5a", "4.3.4", "other", "unstated"]
GAME_VERSIONS: tuple[str, ...] = GameVersion.__args__  # type: ignore[attr-defined]

ServerCore = Literal["trinitycore", "azerothcore", "vmangos", "cmangos", "other", "none"]
SERVER_CORES: tuple[str, ...] = ServerCore.__args__  # type: ignore[attr-defined]

FileFormat = Literal[
    "M2", "WMO", "ADT", "WDT", "BLP", "DBC", "WDBX", "MPQ", "SQL", "SKIN", "ANIM", "TGA", "PNG",
    "OBJ", "FBX", "BLEND", "LUA", "XML", "other",
]  # fmt: skip
FILE_FORMATS: tuple[str, ...] = FileFormat.__args__  # type: ignore[attr-defined]


class WorkflowScreen(BaseModel):
    on_topic: bool = Field(
        description="About making game content or running a game server: models, zones, items, "
        "spells, game data files, archives, databases, server setup. Not unrelated."
    )
    shows_workflow: bool = Field(
        description="A task is carried out with tools, on screen or narrated, far enough that a "
        "viewer could repeat it. A showcase, gameplay, news or vlog of a result alone is not."
    )
    made_with: list[str] = Field(description="The tools seen or named, e.g. 'Noggit', 'Keira3'.")
    game_versions: list[GameVersion] = Field(description="The client version(s) targeted.")
    quality: int = Field(
        ge=1,
        le=5,
        description="How followable the workflow is: 1 nothing usable .. 5 every step and "
        "file shown, ends in a working result.",
    )
    reasons: str = Field(description="One or two sentences behind the fields above.")


class WorkflowSpan(BaseModel):
    goal: str = Field(
        description="What the modder sets out to do, in 4 to 12 words: 'add a custom item to "
        "AzerothCore with SQL'."
    )
    start: float = Field(description="Seconds where the task starts.")
    end: float = Field(description="Seconds where it ends.")


class WorkflowOutline(BaseModel):
    workflows: list[WorkflowSpan]


class WorkflowTool(BaseModel):
    name: str
    version: str | None = Field(description="The version, only if stated or shown; else null.")
    role: str = Field(description="What it is used for in this workflow.")
    t: float


class WorkflowCard(BaseModel):
    goal: str = Field(description="What the modder sets out to do, in 4 to 12 words.")
    goal_t: float
    result: str = Field(description="One line on what they end up with.")
    game_versions: list[GameVersion] = Field(min_length=1, description="'unstated' if unclear.")
    server_cores: list[ServerCore] = Field(
        min_length=1, description="The server software involved; 'none' for client-only work."
    )
    tools: list[WorkflowTool]
    inputs: list[FileFormat] = Field(description="File formats the workflow starts from.")
    outputs: list[FileFormat] = Field(description="File formats it produces or changes.")
    steps: list[Claim] = Field(
        min_length=1,
        description="The ordered steps, each one action with the tool and file it touches. "
        "Concrete: names, paths, field values and settings as said or shown.",
    )
    pain_points: list[Claim] = Field(
        description="Problems, bugs, crashes or limits hit, with the workaround if one is shown."
    )
    time_spent: Claim | None = Field(
        description="How long the task takes, only if a timer, a cut or the creator says so."
    )


class WorkflowCarded(BaseModel):
    cards: list[WorkflowCard]


def route_workflow(screen: WorkflowScreen, threshold: int) -> tuple[Mode, str]:
    """Code's rule for a workflow video: card it or drop it. Click-alongs are the content here."""
    if not screen.on_topic:
        return "drop", "off-topic for workflows"
    if not screen.shows_workflow:
        return "drop", "no workflow shown: a showcase, gameplay or talk"
    if screen.quality < threshold:
        return "drop", f"quality {screen.quality} < {threshold}"
    return "card", "shows a workflow"


def tool_text(tool: WorkflowTool) -> str:
    return f"uses {tool.name}{f' {tool.version}' if tool.version else ''} to {tool.role}"


def workflow_claim_ids(cards: list[WorkflowCard]) -> dict[str, str]:
    """Every checkable claim of a workflow card, keyed by a stable id the verifier cites."""
    out: dict[str, str] = {}
    for i, card in enumerate(cards):
        out[f"w{i}.goal"] = card.goal
        for j, tool in enumerate(card.tools):
            out[f"w{i}.t{j}"] = tool_text(tool)
        for letter, field in (("s", "steps"), ("p", "pain_points")):
            for j, item in enumerate(getattr(card, field)):
                out[f"w{i}.{letter}{j}"] = item.text
        if card.time_spent:
            out[f"w{i}.time"] = card.time_spent.text
    return out


def apply_workflow_verification(
    cards: list[WorkflowCard], checks: Verification, duration: float
) -> list[WorkflowCard]:
    """Keeps only claims the verifier supported with a timestamp inside the clip. A card
    survives if its goal and at least one step do; unsupported time and tools are dropped."""
    supported = _supported(checks, duration)
    kept: list[WorkflowCard] = []
    for i, card in enumerate(cards):
        steps = [c for j, c in enumerate(card.steps) if f"w{i}.s{j}" in supported]
        if f"w{i}.goal" not in supported or not steps:
            continue
        kept.append(
            card.model_copy(
                update={
                    "steps": steps,
                    "tools": [t for j, t in enumerate(card.tools) if f"w{i}.t{j}" in supported],
                    "pain_points": [
                        c for j, c in enumerate(card.pain_points) if f"w{i}.p{j}" in supported
                    ],
                    "time_spent": card.time_spent if f"w{i}.time" in supported else None,
                }
            )
        )
    return kept


def shift_workflow_card(card: WorkflowCard, offset: float) -> WorkflowCard:
    """A card with every timestamp moved by `offset`: span-clip time back to clip time."""

    def moved(t: float) -> float:
        return round(t + offset, 2)

    return card.model_copy(
        update={
            "goal_t": moved(card.goal_t),
            "tools": [t.model_copy(update={"t": moved(t.t)}) for t in card.tools],
            "steps": [c.model_copy(update={"t": moved(c.t)}) for c in card.steps],
            "pain_points": [c.model_copy(update={"t": moved(c.t)}) for c in card.pain_points],
            "time_spent": card.time_spent.model_copy(update={"t": moved(card.time_spent.t)})
            if card.time_spent
            else None,
        }
    )
