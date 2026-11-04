"""Recognise KiCad fabrication outputs dropped on the upload page.

Content first (KiCad writes Gerber X2 `%TF.FileFunction` attributes and an
Excellon header comment), file name second (older KiCad, other tools).
"""

from __future__ import annotations

import re
from collections import Counter

# roles
COPPER_TOP, COPPER_BOTTOM, EDGE = "copper_top", "copper_bottom", "edge"
DRILL_PTH, DRILL_NPTH, DRILL = "drill_pth", "drill_npth", "drill"
IGNORE = "ignore"
# a role the user did not override: classify() decides from the file contents
AUTO = "auto"

# roles offered in the upload page's per-file dropdown, in display order
ROLE_LABELS = [
    (COPPER_TOP, "top copper"),
    (COPPER_BOTTOM, "bottom copper"),
    (EDGE, "board outline"),
    (DRILL_PTH, "drills (plated / PTH)"),
    (DRILL_NPTH, "drills (non-plated / NPTH)"),
    (DRILL, "drills (unspecified)"),
    (IGNORE, "ignore"),
]
VALID_ROLES = frozenset(role for role, _ in ROLE_LABELS)
LABELS = dict(ROLE_LABELS)

_TF = re.compile(r"%TF\.FileFunction,([^*%]+)")
_XLN = re.compile(r";\s*#@!\s*TF\.FileFunction,([^\r\n]+)")

_NAME_RULES = [  # (regex on lower-cased file name, role)
    (r"(^|[-_.])(edge|outline|profile)([-_.]|$)|\.(gm1|gko)$", EDGE),
    (r"-f_cu\.(gbr)$|\.gtl$", COPPER_TOP),
    (r"-b_cu\.(gbr)$|\.gbl$", COPPER_BOTTOM),
    (r"-npth\.(drl|xln|txt)$", DRILL_NPTH),
    (r"-pth\.(drl|xln|txt)$", DRILL_PTH),
    (r"\.(drl|xln)$", DRILL),
]
_STEM = re.compile(r"[-_.](f_cu|b_cu|edge_cuts|npth|pth|job|f_mask|b_mask|f_silkscreen|b_silkscreen|"
                   r"f_paste|b_paste|in\d+_cu)\b.*$", re.I)


def classify(name: str, data: bytes) -> str:
    head = data[:4096].decode("latin-1", errors="replace")
    lname = name.lower()
    if m := _TF.search(head):
        fn = m.group(1).split(",")
        kind = fn[0].strip()
        if kind == "Copper":
            side = fn[-1].strip().lower()
            if side.startswith("top"):
                return COPPER_TOP
            if side.startswith("bot"):
                return COPPER_BOTTOM
            return IGNORE                      # inner layers
        if kind == "Profile":
            return EDGE
        return IGNORE                          # mask, silk, paste, ...
    if m := _XLN.search(head):
        fn = m.group(1)
        if "NonPlated" in fn or "NPTH" in fn:
            return DRILL_NPTH
        if "Plated" in fn or "PTH" in fn:
            return DRILL_PTH
    if lname.endswith(".gbrjob"):
        return IGNORE
    for rx, role in _NAME_RULES:
        if re.search(rx, lname):
            return role
    return IGNORE


def resolve_roles(files: list[tuple[str, bytes]], manual: list[str] | None = None) -> list[str]:
    """One role per file, in the order given.

    `manual[i]` is the role the user picked for `files[i]`; empty or `AUTO`
    leaves it to classify(). A manual pick always wins, even when the file's
    own metadata says otherwise.
    """
    if manual and len(manual) != len(files):
        raise DetectError("file type selections do not match the uploaded files")
    roles = []
    for i, (name, data) in enumerate(files):
        choice = (manual[i] if manual else "").strip()
        if not choice or choice == AUTO:
            roles.append(classify(name, data))
        elif choice in VALID_ROLES:
            roles.append(choice)
        else:
            raise DetectError(f"{name}: unknown file type {choice!r}")
    return roles


def board_name(names: list[str]) -> str:
    """'AQI esp32 pcb-F_Cu.gbr' -> 'AQI esp32 pcb' (most common stem)."""
    stems = Counter()
    for n in names:
        base = n.rsplit("/", 1)[-1]
        stem = _STEM.sub("", base)
        if stem == base:
            stem = base.rsplit(".", 1)[0]
        if stem.strip():
            stems[stem.strip()] += 1
    return stems.most_common(1)[0][0] if stems else "board"


def slug(text: str) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-._")
    return s[:48] or "board"


class DetectError(ValueError):
    pass


def assign(files: list[tuple[str, bytes]], manual: list[str] | None = None) -> dict:
    """Pick the inputs mill.py needs from a pile of files.

    `manual` overrides the auto-detected role per file (same order as `files`);
    empty entries fall back to content-then-name detection.

    -> {"copper": (name, data), "side": "top"|"bottom", "edge": ..., "drill": ...,
        "drill2": ... or None, "title": str, "roles": {name: role}}
    Raises DetectError with a message the user can act on.
    """
    roles = resolve_roles(files, manual)
    by_role: dict[str, list[tuple[str, bytes]]] = {}
    for item, role in zip(files, roles):
        by_role.setdefault(role, []).append(item)

    def one(role, what, required=True):
        got = by_role.get(role, [])
        if len(got) > 1:
            raise DetectError(f"more than one {what} file: " + ", ".join(n for n, _ in got)
                              + " - set the file type by hand if that is wrong")
        if not got and required:
            raise DetectError(f"no {what} file recognised")
        return got[0] if got else None

    top, bot = one(COPPER_TOP, "top copper", False), one(COPPER_BOTTOM, "bottom copper", False)
    if not top and not bot:
        raise DetectError("no copper layer recognised (expected e.g. *-F_Cu.gbr or *-B_Cu.gbr)")
    if top and bot:
        raise DetectError("both top and bottom copper selected - set the one you don't want to mill to ignore")
    side = "top" if top else "bottom"
    copper = top or bot

    pth, npth, generic = one(DRILL_PTH, "plated drill", False), one(DRILL_NPTH, "non-plated drill", False), by_role.get(DRILL, [])
    if not pth and generic:
        pth = generic[0]
    if not pth and not npth:
        raise DetectError("no drill file recognised (expected e.g. *-PTH.drl)")
    drill, drill2 = (pth, npth) if pth else (npth, None)
    return {
        "copper": copper, "side": side, "edge": one(EDGE, "board outline (Edge_Cuts)"),
        "drill": drill, "drill2": drill2, "roles": dict(zip((n for n, _ in files), roles)),
        "layers": {"top": bool(top), "bottom": bool(bot)},
        # a job is named after its copper Gerber (`board-F_Cu.gbr` -> `board`),
        # not after whichever file happens to be most common in the pile.
        "title": board_name([copper[0]]),
    }
