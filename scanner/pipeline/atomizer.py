"""
Stage 2 — Atomizer: deep entity extraction from raw content.

Two extraction strategies are applied in sequence:

1. Code atoms from PR diffs
   - Primary:  tree-sitter AST parsing (if `tree-sitter` + grammar packages installed)
   - Fallback: regex-based heuristics (always available, works for 90%+ of real diffs)
   Extracts:  FUNCTION, CLASS, IMPORT entities per changed file

2. Acceptance criteria from issue bodies
   - Splits structured requirement statements from issue/ticket body text
   - Uses simple rules: bullet/checkbox lines that contain requirement keywords
   Extracts:  ACCEPTANCE_CRITERION entities linked to their parent ISSUE

Both strategies are pure-Python and produce Entity objects that feed into
Stage 3 (Embedder).  All extraction is deterministic — same input = same output.
"""

from __future__ import annotations

import logging
import re
from typing import Sequence

from scanner.pipeline.entities import (
    Entity,
    EntityType,
    SourceIntegration,
    make_entity,
    make_entity_id,
)

logger = logging.getLogger(__name__)

# ── Requirement-keyword pattern (for AC extraction) ──────────────────────────

_REQ_PATTERN = re.compile(
    r"\b(should|must|shall|needs?\s+to|required|expects?|will|cannot|can't|won't)\b",
    re.IGNORECASE,
)

# ── Languages supported by tree-sitter grammars ──────────────────────────────

_EXT_TO_LANG: dict[str, str] = {
    ".py":   "python",
    ".js":   "javascript",
    ".jsx":  "javascript",
    ".ts":   "typescript",
    ".tsx":  "tsx",
    ".go":   "go",
    ".rs":   "rust",
    ".java": "java",
    ".c":    "c",
    ".cpp":  "cpp",
    ".cc":   "cpp",
    ".cs":   "c_sharp",
    ".rb":   "ruby",
    ".php":  "php",
    ".kt":   "kotlin",
    ".swift":"swift",
}

# Regex patterns for function/class/import detection (fallback)
_FUNC_PATTERNS: list[re.Pattern] = [
    # Python / JS / TS
    re.compile(r"^\+\s*(?:async\s+)?def\s+(\w+)\s*\(", re.M),
    re.compile(r"^\+\s*(?:export\s+)?(?:async\s+)?function\s+(\w+)\s*[\(\<]", re.M),
    re.compile(r"^\+\s*(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s+)?\(", re.M),
    re.compile(r"^\+\s*(?:export\s+default\s+)?(?:async\s+)?(?:function\s*\*?\s*)?(\w+)\s*[:=]\s*(?:async\s+)?\(", re.M),
    # Go
    re.compile(r"^\+\s*func\s+(?:\(\w+\s+\*?\w+\)\s+)?(\w+)\s*\(", re.M),
    # Rust
    re.compile(r"^\+\s*(?:pub\s+)?(?:async\s+)?fn\s+(\w+)\s*[\(<]", re.M),
    # Java / Kotlin / C#
    re.compile(r"^\+\s*(?:public|private|protected|static|override|internal|override)(?:\s+\w+)*\s+(\w+)\s*\(", re.M),
    # Ruby
    re.compile(r"^\+\s*def\s+(\w+)(?:\s*\(|\s|$)", re.M),
]

_CLASS_PATTERNS: list[re.Pattern] = [
    re.compile(r"^\+\s*(?:export\s+)?(?:abstract\s+)?class\s+(\w+)", re.M),
    re.compile(r"^\+\s*(?:pub\s+)?(?:struct|enum|trait|impl)\s+(\w+)", re.M),
    re.compile(r"^\+\s*type\s+(\w+)\s*(?:=|\{)", re.M),
    re.compile(r"^\+\s*interface\s+(\w+)", re.M),
]

_IMPORT_PATTERNS: list[re.Pattern] = [
    re.compile(r"^\+\s*(import\s+.+)$", re.M),
    re.compile(r"^\+\s*(from\s+\S+\s+import\s+.+)$", re.M),
    re.compile(r"^\+\s*(require\s*\(['\"].+['\"]\))", re.M),
    re.compile(r"^\+\s*(use\s+\S+(?:::\S+)*\s*;)", re.M),
]


# ── Public entry-point ────────────────────────────────────────────────────────

def atomize(base_entities: list[Entity]) -> list[Entity]:
    """
    Given the entities produced by the Collector, extract deeper code atoms
    and acceptance criteria.

    Returns only the *new* entities (additions to the graph).
    """
    new_entities: list[Entity] = []

    for entity in base_entities:
        if entity.entity_type == EntityType.FILE_HUNK.value:
            new_entities.extend(_extract_code_atoms(entity))
        elif entity.entity_type in (
            EntityType.ISSUE.value,
            EntityType.JIRA_TICKET.value,
            EntityType.LINEAR_ISSUE.value,
        ):
            new_entities.extend(_extract_acceptance_criteria(entity))

    logger.info(f"Atomizer extracted {len(new_entities)} additional entities")
    return new_entities


# ── Code atom extraction ──────────────────────────────────────────────────────

def _extract_code_atoms(file_hunk: Entity) -> list[Entity]:
    """
    Extract FUNCTION / CLASS / IMPORT atoms from a FILE_HUNK diff patch.
    Tries tree-sitter first; falls back to regex.
    """
    filepath = file_hunk.metadata.get("meta_filepath") or file_hunk.title
    patch    = file_hunk.body
    if not patch or not filepath:
        return []

    ext = "." + filepath.rsplit(".", 1)[-1].lower() if "." in filepath else ""

    # Only process recognised code file types
    if ext not in _EXT_TO_LANG and ext not in (
        ".py", ".js", ".ts", ".go", ".rs", ".java", ".c", ".cpp", ".rb", ".kt"
    ):
        return []

    # Try tree-sitter first
    atoms = _ts_extract(filepath, patch, file_hunk)
    if atoms is not None:
        return atoms

    # Fallback: regex
    return _regex_extract(filepath, patch, file_hunk)


def _ts_extract(filepath: str, patch: str, parent: Entity) -> list[Entity] | None:
    """
    tree-sitter extraction.  Returns None if tree-sitter or grammar isn't installed.
    """
    ext  = "." + filepath.rsplit(".", 1)[-1].lower() if "." in filepath else ""
    lang = _EXT_TO_LANG.get(ext)
    if not lang:
        return None

    try:
        import tree_sitter_languages
        parser = tree_sitter_languages.get_parser(lang)
    except ImportError:
        try:
            # Try individual language packages (tree-sitter >= 0.22 style)
            _mod = __import__(f"tree_sitter_{lang}", fromlist=["language"])
            from tree_sitter import Language, Parser
            ts_lang = Language(_mod.language())
            parser = Parser(ts_lang)
        except (ImportError, Exception):
            return None  # tree-sitter not available

    # Extract the added lines only (lines starting with "+")
    added_lines = "\n".join(
        line[1:] for line in patch.split("\n") if line.startswith("+")
    )
    if not added_lines.strip():
        return []

    try:
        tree  = parser.parse(added_lines.encode())
        atoms = _walk_ts_tree(tree.root_node, added_lines, filepath, parent, lang)
        return atoms
    except Exception as exc:
        logger.debug(f"tree-sitter parse failed for {filepath}: {exc}")
        return None


def _walk_ts_tree(
    node,
    source:   str,
    filepath: str,
    parent:   Entity,
    lang:     str,
) -> list[Entity]:
    """Recursively walk a tree-sitter CST and extract named functions/classes."""
    entities: list[Entity] = []

    FUNCTION_NODES = {
        "function_definition",       # Python
        "function_declaration",      # JS / Go
        "method_definition",         # JS / TS
        "arrow_function",            # JS / TS
        "function_item",             # Rust
        "method_declaration",        # Java / Kotlin
        "local_function_statement",  # Lua
    }
    CLASS_NODES = {
        "class_definition",   # Python
        "class_declaration",  # JS / Java
        "struct_item",        # Rust
        "type_declaration",   # Go
    }

    def _get_name(n) -> str:
        for child in n.children:
            if child.type in ("identifier", "name", "type_identifier"):
                return source[child.start_byte:child.end_byte]
        return ""

    def _recurse(n):
        if n.type in FUNCTION_NODES:
            name = _get_name(n)
            if name:
                snippet = source[n.start_byte:n.end_byte][:500]
                entities.append(
                    make_entity(
                        source=parent.source,
                        entity_type=EntityType.FUNCTION.value,
                        natural_key=f"{parent.natural_key}::fn::{name}",
                        title=f"fn {name} in {filepath}",
                        body=snippet,
                        metadata={"filepath": filepath, "language": lang},
                        related_ids=[parent.id],
                    )
                )
        elif n.type in CLASS_NODES:
            name = _get_name(n)
            if name:
                snippet = source[n.start_byte:n.end_byte][:500]
                entities.append(
                    make_entity(
                        source=parent.source,
                        entity_type=EntityType.CLASS.value,
                        natural_key=f"{parent.natural_key}::cls::{name}",
                        title=f"class {name} in {filepath}",
                        body=snippet,
                        metadata={"filepath": filepath, "language": lang},
                        related_ids=[parent.id],
                    )
                )
        for child in n.children:
            _recurse(child)

    _recurse(node)
    return entities


def _regex_extract(filepath: str, patch: str, parent: Entity) -> list[Entity]:
    """
    Regex-based fallback for code atom extraction.
    Works for Python, JS/TS, Go, Rust, Java, and most C-style languages.
    """
    entities: list[Entity] = []
    seen_names: set[str] = set()

    # Functions
    for pat in _FUNC_PATTERNS:
        for match in pat.finditer(patch):
            name = match.group(1)
            if not name or name in seen_names or _is_noise_name(name):
                continue
            seen_names.add(name)
            # Grab a small context snippet around the match
            start   = max(0, match.start() - 20)
            snippet = patch[start:start + 300]
            entities.append(
                make_entity(
                    source=parent.source,
                    entity_type=EntityType.FUNCTION.value,
                    natural_key=f"{parent.natural_key}::fn::{name}",
                    title=f"fn {name} in {filepath}",
                    body=snippet,
                    metadata={"filepath": filepath},
                    related_ids=[parent.id],
                )
            )

    # Classes / types
    for pat in _CLASS_PATTERNS:
        for match in pat.finditer(patch):
            name = match.group(1)
            if not name or name in seen_names:
                continue
            seen_names.add(name)
            snippet = patch[match.start():match.start() + 200]
            entities.append(
                make_entity(
                    source=parent.source,
                    entity_type=EntityType.CLASS.value,
                    natural_key=f"{parent.natural_key}::cls::{name}",
                    title=f"class {name} in {filepath}",
                    body=snippet,
                    metadata={"filepath": filepath},
                    related_ids=[parent.id],
                )
            )

    # Imports (aggregated, not individually named to avoid noise)
    import_lines: list[str] = []
    for pat in _IMPORT_PATTERNS:
        for match in pat.finditer(patch):
            import_lines.append(match.group(1)[:120])
    if import_lines:
        entities.append(
            make_entity(
                source=parent.source,
                entity_type=EntityType.IMPORT.value,
                natural_key=f"{parent.natural_key}::imports",
                title=f"New imports in {filepath}",
                body="\n".join(dict.fromkeys(import_lines))[:800],  # dedup
                metadata={"filepath": filepath},
                related_ids=[parent.id],
            )
        )

    return entities


def _is_noise_name(name: str) -> bool:
    """Skip single-char names, common test/setup boilerplate, etc."""
    return (
        len(name) <= 1
        or name.lower() in {"test", "setup", "teardown", "init", "main", "run"}
    )


# ── Acceptance criteria extraction ───────────────────────────────────────────

def _extract_acceptance_criteria(issue: Entity) -> list[Entity]:
    """
    Split bullet/checkbox lines from the issue body that look like
    requirements, and create individual ACCEPTANCE_CRITERION entities.
    """
    body = issue.body
    if not body or len(body) < 40:
        return []

    criteria: list[str] = []
    for line in body.splitlines():
        stripped = line.strip()
        # Checkbox  "- [ ] ..." or "- [x] ..."
        if re.match(r"^-\s*\[[ xX]\]", stripped):
            text = re.sub(r"^-\s*\[[ xX]\]\s*", "", stripped).strip()
            if text and _REQ_PATTERN.search(text):
                criteria.append(text)
        # Bullet  "- ..." or "* ..."
        elif re.match(r"^[-*•]\s+", stripped):
            text = re.sub(r"^[-*•]\s+", "", stripped).strip()
            if len(text) > 15 and _REQ_PATTERN.search(text):
                criteria.append(text)
        # Numbered  "1. ..."
        elif re.match(r"^\d+\.\s+", stripped):
            text = re.sub(r"^\d+\.\s+", "", stripped).strip()
            if len(text) > 15 and _REQ_PATTERN.search(text):
                criteria.append(text)

    entities: list[Entity] = []
    for i, criterion in enumerate(criteria[:20]):  # cap at 20 per issue
        entities.append(
            make_entity(
                source=issue.source,
                entity_type=EntityType.ACCEPTANCE_CRITERION.value,
                natural_key=f"{issue.natural_key}:ac:{i}",
                title=f"AC: {criterion[:100]}",
                body=criterion,
                metadata={"parent_key": issue.natural_key},
                related_ids=[issue.id],
            )
        )

    return entities
