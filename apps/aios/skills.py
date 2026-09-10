"""AIOS agent skills discovery and activation."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from . import core

__all__ = [
    "Skill",
    "BUILTIN_SKILLS_ROOT",
    "USER_SKILLS_ROOT",
    "catalog_prompt",
    "initial_skills",
    "load_skills",
]


MAX_SKILLS = 64
MAX_SKILL_BYTES = 48 * 1024
DEFAULT_MODEL = "current"
ALLOWED_MODELS = {"current", "remote-preferred"}
SKILL_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
LEADING_SKILL_RE = re.compile(r"^\s*/([a-z0-9]+(?:-[a-z0-9]+)*)\b")
TOKEN_RE = re.compile(r"[a-z0-9]+")
NEGATION_WINDOW = 4
NEGATION_TOKENS = {"no", "not", "never", "without"}
NEGATION_BIGRAMS = {
    ("aren", "t"),
    ("can", "t"),
    ("couldn", "t"),
    ("didn", "t"),
    ("doesn", "t"),
    ("don", "t"),
    ("isn", "t"),
    ("shouldn", "t"),
    ("wasn", "t"),
    ("weren", "t"),
    ("won", "t"),
    ("wouldn", "t"),
}

BUILTIN_SKILLS_ROOT = Path("/usr/local/share/aios/skills")
USER_SKILLS_ROOT = core.config_dir() / "skills"


@dataclass(frozen=True, slots=True)
class Skill:
    name: str
    description: str
    instructions: str
    allowed_tools: tuple[str, ...]
    triggers: tuple[str, ...]
    model: str = DEFAULT_MODEL


def load_skills(*, include_warnings: bool = False):
    catalog: dict[str, Skill] = {}
    warnings: list[str] = []
    for root in (BUILTIN_SKILLS_ROOT, USER_SKILLS_ROOT):
        _load_root(root, catalog, warnings)
    ordered_catalog = list(catalog.values())
    if include_warnings:
        return ordered_catalog, warnings
    return ordered_catalog


def catalog_prompt(catalog: Iterable[Skill]) -> str:
    lines = ["Available skills:"]
    for skill in catalog:
        lines.append(f"- {skill.name}: {skill.description}")
    return "\n".join(lines)


def initial_skills(catalog: Iterable[Skill], prompt: str) -> list[Skill]:
    catalog = list(catalog)
    if not prompt:
        return []

    selected: list[Skill] = []
    selected_names: set[str] = set()
    name_to_skill = {skill.name: skill for skill in catalog}

    explicit = _leading_skill_name(prompt)
    if explicit:
        skill = name_to_skill.get(explicit)
        if skill is not None:
            selected.append(skill)
            selected_names.add(skill.name)

    prompt_tokens = _tokens(prompt)
    for skill in catalog:
        if skill.name in selected_names:
            continue
        if _skill_matches_prompt(skill, prompt_tokens):
            selected.append(skill)
            selected_names.add(skill.name)
            if len(selected) == 3:
                break

    return selected[:3]


def _load_root(root: Path, catalog: dict[str, Skill], warnings: list[str]) -> None:
    if not root.exists():
        return
    skill_dirs = sorted((child for child in root.iterdir() if child.is_dir()), key=lambda path: path.name)
    for skill_dir in skill_dirs:
        if not SKILL_NAME_RE.fullmatch(skill_dir.name):
            continue
        skill = _load_skill_dir(skill_dir, warnings)
        if skill is None:
            continue
        if skill.name in catalog:
            catalog[skill.name] = skill
            continue
        if len(catalog) >= MAX_SKILLS:
            warnings.append(f"{skill_dir.name}: skill limit reached")
            continue
        catalog[skill.name] = skill


def _load_skill_dir(skill_dir: Path, warnings: list[str]) -> Skill | None:
    skill_file = skill_dir / "SKILL.md"
    if not skill_file.is_file():
        warnings.append(f"{skill_dir.name}: missing SKILL.md")
        return None
    try:
        if skill_file.stat().st_size > MAX_SKILL_BYTES:
            warnings.append(f"{skill_dir.name}: SKILL.md exceeds 48 KiB")
            return None
        text = skill_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        warnings.append(f"{skill_dir.name}: unreadable SKILL.md")
        return None

    try:
        fields, body = _parse_skill_text(text)
    except ValueError:
        warnings.append(f"{skill_dir.name}: invalid SKILL.md")
        return None

    name = fields.get("name")
    description = fields.get("description")
    if not isinstance(name, str) or name != skill_dir.name:
        warnings.append(f"{skill_dir.name}: invalid name")
        return None
    if not isinstance(description, str) or not (1 <= len(description) <= 1024):
        warnings.append(f"{skill_dir.name}: invalid description")
        return None
    if not body.strip():
        warnings.append(f"{skill_dir.name}: empty instructions")
        return None

    allowed_tools_text = fields.get("allowed-tools", "")
    if not isinstance(allowed_tools_text, str):
        warnings.append(f"{skill_dir.name}: invalid allowed-tools")
        return None
    allowed_tools = _split_unique_words(allowed_tools_text)

    model = fields.get("metadata.aios-model", DEFAULT_MODEL)
    if not isinstance(model, str) or model not in ALLOWED_MODELS:
        warnings.append(f"{skill_dir.name}: invalid metadata.aios-model")
        return None

    triggers_text = fields.get("metadata.aios-triggers", "")
    if not isinstance(triggers_text, str):
        warnings.append(f"{skill_dir.name}: invalid metadata.aios-triggers")
        return None
    triggers = _split_triggers(triggers_text)

    return Skill(
        name=name,
        description=description,
        instructions=body.strip("\n"),
        allowed_tools=allowed_tools,
        triggers=triggers,
        model=model,
    )


def _parse_skill_text(text: str) -> tuple[dict[str, str], str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError("missing frontmatter start")
    try:
        end = next(index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---")
    except StopIteration as exc:
        raise ValueError("missing frontmatter end") from exc

    fields: dict[str, str] = {}
    in_metadata = False
    for raw_line in lines[1:end]:
        if not raw_line.strip():
            continue
        if raw_line.startswith("\t"):
            raise ValueError("invalid frontmatter indentation")
        if raw_line.startswith(" "):
            if not in_metadata:
                raise ValueError("invalid frontmatter indentation")
            if not raw_line.startswith("  ") or raw_line.startswith("   "):
                raise ValueError("invalid metadata indentation")
            nested = raw_line[2:]
            if not nested or nested[0].isspace() or ":" not in nested:
                raise ValueError("invalid metadata line")
            key, raw_value = nested.split(":", 1)
            key = key.strip()
            if not key:
                raise ValueError("invalid metadata line")
            value = _parse_scalar(raw_value)
            field_name = f"metadata.{key}"
            if field_name in fields:
                raise ValueError("duplicate frontmatter key")
            fields[field_name] = value
            continue
        in_metadata = False
        if ":" not in raw_line:
            raise ValueError("invalid frontmatter line")
        key, raw_value = raw_line.split(":", 1)
        key = key.strip()
        if not key:
            raise ValueError("invalid frontmatter line")
        if key == "metadata":
            if raw_value.strip():
                raise ValueError("invalid metadata frontmatter")
            in_metadata = True
            continue
        value = _parse_scalar(raw_value)
        if key in fields:
            raise ValueError("duplicate frontmatter key")
        fields[key] = value

    body = "\n".join(lines[end + 1 :])
    return fields, body


def _parse_scalar(raw_value: str) -> str:
    value = raw_value.strip()
    if not value:
        return ""
    if value[0] in {'"', "'"}:
        if len(value) < 2 or value[-1] != value[0]:
            raise ValueError("unterminated quoted frontmatter value")
    if value.startswith('"') and value.endswith('"'):
        parsed = json.loads(value)
        if not isinstance(parsed, str):
            raise ValueError("frontmatter scalar must be a string")
        return parsed
    if value.startswith("'") and value.endswith("'"):
        return value[1:-1]
    return value


def _split_unique_words(value: str) -> tuple[str, ...]:
    items: list[str] = []
    seen: set[str] = set()
    for token in value.split():
        if token and token not in seen:
            seen.add(token)
            items.append(token)
    return tuple(items)


def _split_triggers(value: str) -> tuple[str, ...]:
    items: list[str] = []
    seen: set[str] = set()
    for part in value.split(","):
        trigger = part.strip()
        if trigger and trigger not in seen:
            seen.add(trigger)
            items.append(trigger)
    return tuple(items)


def _leading_skill_name(prompt: str) -> str | None:
    match = LEADING_SKILL_RE.match(prompt)
    if not match:
        return None
    return match.group(1)


def _tokens(text: str) -> tuple[str, ...]:
    return tuple(TOKEN_RE.findall(text.lower()))


def _phrase_positions(haystack: tuple[str, ...], needle: tuple[str, ...]) -> Iterable[int]:
    if not needle or len(needle) > len(haystack):
        return ()
    window = len(needle)
    return (
        index
        for index in range(len(haystack) - window + 1)
        if haystack[index : index + window] == needle
    )


def _is_negated_phrase(prompt_tokens: tuple[str, ...], start_index: int) -> bool:
    preceding = prompt_tokens[max(0, start_index - NEGATION_WINDOW):start_index]
    if any(token in NEGATION_TOKENS for token in preceding):
        return True
    return any(pair in NEGATION_BIGRAMS for pair in zip(preceding, preceding[1:]))


def _skill_matches_prompt(skill: Skill, prompt_tokens: tuple[str, ...]) -> bool:
    for trigger in skill.triggers:
        trigger_tokens = _tokens(trigger)
        if any(not _is_negated_phrase(prompt_tokens, start) for start in _phrase_positions(prompt_tokens, trigger_tokens)):
            return True
    return False
