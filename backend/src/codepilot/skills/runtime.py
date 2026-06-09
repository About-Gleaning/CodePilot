from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


FRONTMATTER_RE = re.compile(r"^\s*---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)
KEY_VALUE_RE = re.compile(r"^\s*([A-Za-z0-9_\-]+)\s*:\s*(.*?)\s*$")


@dataclass(slots=True)
class Skill:
    name: str
    description: str
    path: Path
    skill_md_path: Path
    metadata: dict[str, Any] = field(default_factory=dict)
    content: str | None = None

    def to_brief_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "description": self.description,
        }

    def load_full_content(self) -> str:
        if self.content is None:
            self.content = self.skill_md_path.read_text(encoding="utf-8")
        return self.content


class SkillRegistry:
    def __init__(self, skills_root: str | Path) -> None:
        self.skills_root = Path(skills_root).expanduser().resolve()
        self.skills: list[Skill] = []

    def discover(self) -> list[Skill]:
        if not self.skills_root.is_dir():
            self.skills = []
            return self.skills

        discovered: list[Skill] = []
        for entry in self.skills_root.iterdir():
            if not entry.is_dir():
                continue
            skill_md_path = entry / "SKILL.md"
            if not skill_md_path.is_file():
                continue
            discovered.append(self._parse_skill(entry, skill_md_path))

        self.skills = sorted(discovered, key=lambda skill: skill.name.lower())
        return self.skills

    def get_skill(self, name: str) -> Skill | None:
        target = name.strip().lower()
        for skill in self.skills:
            if skill.name.lower() == target:
                return skill
        return None

    def list_briefs(self) -> list[dict[str, str]]:
        return [skill.to_brief_dict() for skill in self.skills]

    def _parse_skill(self, skill_dir: Path, skill_md_path: Path) -> Skill:
        raw = skill_md_path.read_text(encoding="utf-8")
        metadata, body = parse_skill_markdown(raw)
        name = str(metadata.get("name") or skill_dir.name).strip()
        description = str(metadata.get("description") or "").strip()
        if not description:
            description = extract_first_meaningful_line(body) or f"Skill at {skill_dir.name}"

        return Skill(
            name=name,
            description=description,
            path=skill_dir,
            skill_md_path=skill_md_path,
            metadata=metadata,
        )


def parse_skill_markdown(raw: str) -> tuple[dict[str, Any], str]:
    match = FRONTMATTER_RE.match(raw)
    if not match:
        return {}, raw

    metadata: dict[str, Any] = {}
    for line in match.group(1).splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key_value = KEY_VALUE_RE.match(stripped)
        if key_value is None:
            continue
        metadata[key_value.group(1)] = strip_quotes(key_value.group(2))
    return metadata, match.group(2)


def strip_quotes(value: str) -> str:
    stripped = value.strip()
    if len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in {"'", '"'}:
        return stripped[1:-1]
    return stripped


def extract_first_meaningful_line(body: str) -> str | None:
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        return stripped
    return None
