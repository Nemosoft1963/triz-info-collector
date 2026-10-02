"""PEP / RFC / KEP 本文から指定節だけ抜き出す（SPEC 0.5 / 0.6）。"""

from __future__ import annotations

import re

from app.textutil import nfc

PEP_HEADINGS = (
    "Motivation",
    "Rationale",
    "Rejected Ideas",
    "Rejected Alternative",
    "Alternatives",
    "Backwards Compatibility",
)
RFC_HEADINGS = (
    "Motivation",
    "Drawbacks",
    "Rationale and alternatives",
    "Rationale",
    "Alternatives",
)
KEP_HEADINGS = (
    "Motivation",
    "Alternatives",
    "Drawbacks",
    "Risks and Mitigations",
)
PYTORCH_HEADINGS = (
    "Motivation",
    "Motivation and Scope",
    "Drawbacks",
    "Alternatives",
    "Unresolved questions",
    "Proposed Implementation",
)
TF_HEADINGS = (
    "Objective",
    "Motivation",
    "Alternatives",
    "Drawbacks",
)
ARCHITECTURE_HEADINGS = (
    "Design considerations",
    "Design decisions",
    "Considerations",
    "Trade-offs",
    "Tradeoffs",
    "Alternatives",
    "Risks",
    "Limitations",
    "Challenges",
    "Motivation",
    "Architecture",
    "Components",
    "Requirements",
)

MD_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
RST_UNDERLINE_RE = re.compile(r"^[=~\-`:'^\"*+#+]{3,}\s*$")


def normalize_heading(text: str) -> str:
    t = nfc(text or "").strip().lower()
    t = re.sub(r"^#+\s*", "", t)
    t = re.sub(r"[*_`]+", "", t)  # 太字・強調記号(例: "## **Motivation**")を除く
    t = re.sub(r"^[0-9]+[.)]\s*", "", t)
    t = t.strip(" \t:-")
    t = re.sub(r"\s+", " ", t)
    return t


def _is_heading(line: str, next_line: str | None) -> tuple[bool, str]:
    md = MD_HEADING_RE.match(line)
    if md:
        return True, md.group(2).strip()
    if next_line is not None and RST_UNDERLINE_RE.match(next_line):
        if line.strip() and not RST_UNDERLINE_RE.match(line):
            return True, line.strip()
    return False, ""


def split_heading_blocks(text: str) -> list[tuple[str, str]]:
    lines = nfc(text or "").replace("\r\n", "\n").split("\n")
    blocks: list[tuple[str, str]] = []
    current_h = ""
    buf: list[str] = []
    i = 0
    while i < len(lines):
        nxt = lines[i + 1] if i + 1 < len(lines) else None
        is_h, heading = _is_heading(lines[i], nxt)
        if is_h:
            if current_h or buf:
                blocks.append((current_h, "\n".join(buf).strip()))
            current_h = heading
            buf = []
            if nxt is not None and RST_UNDERLINE_RE.match(nxt) and not MD_HEADING_RE.match(lines[i]):
                i += 2
                continue
            i += 1
            continue
        buf.append(lines[i])
        i += 1
    if current_h or buf:
        blocks.append((current_h, "\n".join(buf).strip()))
    return blocks


def extract_named_sections(text: str, wanted: tuple[str, ...]) -> str:
    wanted_norm = {normalize_heading(w) for w in wanted}
    parts: list[str] = []
    for heading, body in split_heading_blocks(text):
        if heading and normalize_heading(heading) in wanted_norm and body.strip():
            parts.append(f"{heading.strip()}\n{body.strip()}")
    return "\n\n".join(parts).strip()
