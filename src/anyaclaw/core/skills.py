"""Skill discovery (``skills/<name>/SKILL.md``).

Skills are cheap: only a one-line summary goes into the system prompt, and the
model reads the full document with ``read_file`` when it matters. That keeps the
prompt small even with dozens of installed skills.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from anyaclaw.logging import get_logger

log = get_logger("skills")


@dataclass(slots=True)
class Skill:
    name: str
    description: str
    when_to_use: str
    path: Path
    body: str = ""

    def summary_line(self) -> str:
        detail = f" — {self.description}" if self.description else ""
        trigger = f" (use when: {self.when_to_use})" if self.when_to_use else ""
        return f"- **{self.name}**: `{self.path}`{detail}{trigger}"


class SkillLoader:
    """Loads ``SKILL.md`` files with an optional YAML-ish front matter."""

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)

    def load(self) -> list[Skill]:
        if not self.directory.is_dir():
            return []
        skills: list[Skill] = []
        for path in sorted(self.directory.glob("*/SKILL.md")):
            try:
                text = path.read_text(encoding="utf-8")
            except OSError as exc:  # pragma: no cover - filesystem dependent
                log.warning("cannot read skill %s: %s", path, exc)
                continue
            meta, body = _split_front_matter(text)
            name = meta.get("name") or path.parent.name
            skills.append(
                Skill(
                    name=name,
                    description=meta.get("description", ""),
                    when_to_use=meta.get("when_to_use", meta.get("when-to-use", "")),
                    path=path,
                    body=body,
                )
            )
        return skills

    def get(self, name: str) -> Skill | None:
        for skill in self.load():
            if skill.name == name:
                return skill
        return None

    def summary(self, *, max_chars: int = 2400) -> str:
        skills = self.load()
        if not skills:
            return ""
        lines: list[str] = []
        for skill in skills:
            lines.append(skill.summary_line())
        text = "\n".join(lines)
        if len(text) > max_chars:
            text = text[:max_chars] + "\n- … (more skills omitted)"
        return text


def _split_front_matter(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    meta: dict[str, str] = {}
    for line in parts[1].splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        cleaned = value.strip().strip('"').strip("'")
        meta[key.strip().lower()] = cleaned
    return meta, parts[2].lstrip("\n")
