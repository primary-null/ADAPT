"""Registry validation for failure guidance (design section 2.2).

Offline: reads source files and the registry only.
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from adapt_adb import failure_guide as fg
from adapt_adb import failure_report as fr
from adapt_adb.doctor import CHECK_IDS
from adapt_adb.help_system import HOW_TO
from adapt_adb.mcp.access import MUTATING_TOOLS, OBSERVATION_TOOLS

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "adapt_adb"
ID_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*){1,3}$")
HELP_ALIASES = {"wireless", "device_manager", "udev", "recheck"}
MCP_TOOLS = OBSERVATION_TOOLS | MUTATING_TOOLS

FORBIDDEN = [re.compile(p, re.IGNORECASE) for p in (
    r"kill-server", r"\bsudo\s+(\S*/)?adb\b", r"\badb\s+root\b",
    r"(rm|del|erase|remove-item|unlink|delete|remove|wipe|regenerate)\b[^.\n]*adbkey",
    # The design's pattern stops at a dot, so it misses "rm ~/.android/adbkey"; this closes that gap.
    r"\b(rm|del|erase|remove-item|unlink)\s+(-\S+\s+)*\S*adbkey",
    r"zadig", r"bcdedit", r"testsigning",
    r"(oem|flashing)\s+unlock", r"unlock(ing)?\s+(the\s+)?bootloader", r"\bmagisk\b", r"\bsu\s+-c\b",
    r"\broot(ing)?\s+(the|your)\s+(phone|device)",
    r"(taskkill|pkill|killall)\b[^\n]*adb", r"ANDROID_HOME\s*=", r"chmod\s+777",
    r"\brun\b[^.\n]*\bas administrator\b", r"disable\b[^.\n]*(antivirus|security software|defender)",
)]


def _all_text(guide: fg.FailureGuide) -> list[str]:
    texts = [guide.summary, *(c.text for c in guide.causes), *(c.if_unknown or "" for c in guide.causes)]
    for step in guide.steps:
        texts += [step.text, step.command or "", step.if_unknown or ""]
    texts.append(guide.doctor_remediation or "")
    texts += [text for _variant, text in guide.doctor_variants]
    if guide.mcp:
        texts += [guide.mcp.ask_user or "", guide.mcp.argument_hint or ""]
    return texts


def test_ids_follow_grammar_namespace_and_renames_resolve():
    assert len(fg.GUIDES) >= 32
    for guide_id, guide in fg.GUIDES.items():
        assert guide_id == guide.id
        assert ID_RE.fullmatch(guide_id), guide_id
        assert guide_id.split(".", 1)[0] in fg.NAMESPACES, guide_id
    assert "dexpc" in fg.NAMESPACES
    for old, new in fg.RENAMED.items():
        assert old not in fg.GUIDES and new in fg.GUIDES


def test_duplicate_ids_are_rejected_at_build_time():
    guide = fg.GUIDES["adb.not_found"]
    with pytest.raises(ValueError):
        fg._build((guide, guide))


def test_help_topics_and_doctor_checks_exist():
    for guide in fg.GUIDES.values():
        if guide.help_topic is not None:
            assert guide.help_topic in HOW_TO or guide.help_topic in HELP_ALIASES, guide.id
        if guide.doctor_check is not None:
            assert guide.doctor_check in CHECK_IDS, guide.id


def test_doctor_remediation_has_a_single_source_in_the_registry():
    # D9: Phase 1 copied the text; Phase 3 made the registry the single source, so doctor.py holds
    # no second copy. tests/test_doctor_failure_ids.py pins the rendered text per doctor branch.
    source = (SRC / "doctor.py").read_text(encoding="utf-8")
    flattened = re.sub(r'"\s*\n\s*"', "", source)
    for guide in fg.GUIDES.values():
        if guide.doctor_variants:
            assert guide.doctor_remediation, guide.id
        if guide.doctor_remediation:
            assert guide.doctor_check, guide.id
            for text in (guide.doctor_remediation, *dict(guide.doctor_variants).values()):
                assert text not in flattened, guide.id


def test_every_guide_has_steps_unless_it_is_an_agent_only_refusal():
    for guide in fg.GUIDES.values():
        assert guide.summary.endswith("."), guide.id
        assert guide.steps, guide.id
        assert guide.certainty in {"observed", "inferred"}


def test_summaries_do_not_assert_causes_unless_observed():
    for guide in fg.GUIDES.values():
        if guide.certainty != "observed":
            assert not re.search(r"\bbecause\b|the cause is|this means", guide.summary, re.IGNORECASE), guide.id


def test_text_interpolates_only_allow_listed_template_fields():
    for guide in fg.GUIDES.values():
        for text in _all_text(guide):
            for name in re.findall(r"\{([^}]*)\}", text):
                assert name in fg.TEMPLATE_FIELDS, (guide.id, name)


def test_commands_use_placeholders_not_real_values():
    for guide in fg.GUIDES.values():
        for step in guide.steps:
            command = step.command or ""
            assert not re.search(r"\b\d{1,3}(\.\d{1,3}){3}\b", command), (guide.id, command)
            assert not re.search(r"[A-Za-z]:\\|/home/|/Users/|~/", command), (guide.id, command)
            assert "\n" not in command


def test_forbidden_advice_never_appears_outside_avoid_notes():
    for guide in fg.GUIDES.values():
        for text in _all_text(guide):
            for pattern in FORBIDDEN:
                assert not pattern.search(text), (guide.id, pattern.pattern, text)


def test_forbidden_scan_catches_known_bad_advice():
    for bad in ("adb kill-server", "sudo adb start-server", "rm ~/.android/adbkey", "Use Zadig",
                "fastboot flashing unlock", "taskkill /IM adb.exe", "adb root"):
        assert any(p.search(bad) for p in FORBIDDEN), bad


def test_admin_steps_are_marked():
    for guide in fg.GUIDES.values():
        for step in guide.steps:
            blob = f"{step.text} {step.command or ''}"
            if step.effect == "changes_host" and re.search(r"\bsudo\b|administrator|usermod", blob, re.IGNORECASE):
                assert step.needs_admin, (guide.id, step.text)


def test_mcp_tool_names_are_real_tools():
    for guide in fg.GUIDES.values():
        for step in guide.steps:
            assert step.mcp_tool is None or step.mcp_tool in MCP_TOOLS, (guide.id, step.mcp_tool)
        if guide.mcp:
            for name in ((guide.mcp.next_tool,) if guide.mcp.next_tool else ()) + guide.mcp.fallback_tools:
                assert name in MCP_TOOLS, (guide.id, name)


def test_docs_paths_are_repo_relative_and_exist():
    for guide in fg.GUIDES.values():
        if guide.docs:
            assert not guide.docs.startswith(("http:", "https:", "/")), guide.id
            assert (ROOT / guide.docs.split("#", 1)[0]).is_file(), guide.id


def test_device_touching_help_topics_link_to_at_least_one_guide():
    for topic in ("devices", "adb", "pair", "connect", "setup", "doctor"):
        linked = [g.id for g in fg.GUIDES.values() if g.help_topic == topic or topic in g.commands]
        assert linked, topic


def test_inventory_id_map_points_at_existing_guides():
    # The inventory map is a working paper kept outside the repository, so a clean
    # checkout does not have it; the check runs wherever the file is present.
    path = ROOT / "docs" / "failure-guidance" / "id-map.json"
    if not path.is_file():
        pytest.skip("docs/failure-guidance/id-map.json is not part of the repository")
    id_map = json.loads(path.read_text(encoding="utf-8"))
    assert id_map
    for inventory_id, value in id_map.items():
        for failure_id in (value if isinstance(value, list) else [value]):
            assert failure_id in fg.GUIDES, (inventory_id, failure_id)


def test_failure_guide_imports_no_other_adapt_module():
    code = ("import sys, adapt_adb.failure_guide; "
            "print(sorted(m for m in sys.modules if m.startswith('adapt_adb') "
            "and m not in ('adapt_adb', 'adapt_adb.failure_guide')))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True, timeout=60)
    assert out.stdout.strip() == "[]"


# ---------------------------------------------------------------------------
# AST cross-reference and ratchet
# ---------------------------------------------------------------------------

def _source_trees() -> list[tuple[Path, ast.Module]]:
    return [(path, ast.parse(path.read_text(encoding="utf-8"))) for path in sorted(SRC.rglob("*.py"))]


def _call_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _str(node: ast.AST | None) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _referenced_ids() -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for path, tree in _source_trees():
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = _call_name(node.func)
                if name == "tagged" and len(node.args) >= 2 and _str(node.args[1]):
                    found.append((path.name, _str(node.args[1])))
                if name == "emit_failure" and node.args and _str(node.args[0]):
                    found.append((path.name, _str(node.args[0])))
                for keyword in node.keywords:
                    if keyword.arg == "failure_id" and _str(keyword.value):
                        found.append((path.name, _str(keyword.value)))
                    # failure_id="a" if condition else "b" (or None): both literal branches count.
                    if keyword.arg == "failure_id" and isinstance(keyword.value, ast.IfExp):
                        found += [(path.name, _str(v)) for v in (keyword.value.body, keyword.value.orelse) if _str(v)]
            if isinstance(node, ast.ClassDef):
                for item in node.body:
                    target = item.target if isinstance(item, ast.AnnAssign) else (item.targets[0] if isinstance(item, ast.Assign) else None)
                    if isinstance(target, ast.Name) and target.id == "failure_id" and _str(item.value):
                        found.append((path.name, _str(item.value)))
    found += [("KIND_MAP", v) for v in fr.KIND_MAP.values()]
    found += [("MCP_ERROR_MAP", v) for v in fr.MCP_ERROR_MAP.values()]
    return found


def test_every_referenced_failure_id_exists():
    references = _referenced_ids()
    assert len(references) > 30
    for where, failure_id in references:
        assert failure_id in fg.GUIDES or failure_id in fg.RENAMED, (where, failure_id)


KIND_CLASSES = {"DevOptionsError", "DexError", "AndroidSupportError", "AdbCommandError"}


def _kind_raises() -> list[tuple[str, str]]:
    raises: list[tuple[str, str]] = []
    for path, tree in _source_trees():
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _call_name(node.func) in KIND_CLASSES and node.args and _str(node.args[0]):
                raises.append((path.stem, _str(node.args[0])))
    return raises


def test_kind_map_covers_every_kind_in_covered_modules():
    # Phase 1 started with an empty KIND_MAP; Phase 2 covers dev_options, dex and android_support.
    for module, kind in _kind_raises():
        if module in fr.KIND_MAP_COVERED_MODULES:
            assert (module, kind) in fr.KIND_MAP, (module, kind)
    assert all(module.isidentifier() for module, _ in fr.KIND_MAP)


def test_every_kind_raised_by_the_three_kind_classes_is_mapped():
    # Design §2.2 item 4: an AST scan of the constructor calls' first argument. Literal kinds must be
    # mapped; the one computed kind (dev_options `setting_<status>`) is mapped for every status
    # `_read_setting` can return.
    raises = _kind_raises()
    modules = {module for module, _ in raises}
    assert modules == {"dev_options", "dex", "android_support"}
    assert set(fr.KIND_MAP_COVERED_MODULES) == modules
    assert len(raises) > 60
    for module, kind in raises:
        assert (module, kind) in fr.KIND_MAP, (module, kind)
    for status in ("timeout", "missing_adb", "adb_error", "denied", "malformed"):
        assert ("dev_options", f"setting_{status}") in fr.KIND_MAP, status
    dynamic = []
    for path, tree in _source_trees():
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _call_name(node.func) in KIND_CLASSES and node.args and not _str(node.args[0]):
                dynamic.append((path.stem, ast.unparse(node.args[0])))
    assert dynamic == [("dev_options", "f'setting_{status}'")], dynamic


_BUILTIN_ROOTS = {"BaseException", "Exception", "ValueError", "RuntimeError", "OSError", "TypeError",
                  "KeyError", "LookupError", "ImportError", "ArithmeticError"}


def untagged_raise_sites() -> list[str]:
    trees = _source_trees()
    bases: dict[str, list[str]] = {}
    defaults: set[str] = set()
    for _, tree in trees:
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                bases[node.name] = [n for n in (_call_name(b) for b in node.bases) if n]
                for item in node.body:
                    target = item.target if isinstance(item, ast.AnnAssign) else (item.targets[0] if isinstance(item, ast.Assign) else None)
                    if isinstance(target, ast.Name) and target.id == "failure_id" and _str(getattr(item, "value", None)):
                        defaults.add(node.name)
    project: set[str] = set()
    changed = True
    while changed:
        changed = False
        for name, parents in bases.items():
            if name not in project and any(p in _BUILTIN_ROOTS or p in project for p in parents):
                project.add(name)
                changed = True

    def has_default(name: str, depth: int = 0) -> bool:
        return name in defaults or (depth < 10 and any(has_default(p, depth + 1) for p in bases.get(name, []) if p in project))

    counted = project | {"ValueError", "RuntimeError"}
    sites: list[str] = []
    for path, tree in trees:
        for node in ast.walk(tree):
            if not isinstance(node, ast.Raise) or not isinstance(node.exc, ast.Call):
                continue
            name = _call_name(node.exc.func)
            if name == "tagged" or name not in counted or has_default(name):
                continue
            if name in KIND_CLASSES and node.exc.args and (path.stem, _str(node.exc.args[0])) in fr.KIND_MAP:
                continue
            sites.append(f"{path.relative_to(SRC).as_posix()}:{node.lineno}:{name}")
    return sites


def test_untagged_raise_ratchet_never_grows():
    baseline = json.loads((ROOT / "tests" / "failure_guidance_baseline.json").read_text(encoding="utf-8"))
    count = len(untagged_raise_sites())
    assert count <= baseline["untagged_raise_sites"], (
        f"{count} untagged raise sites exceed the baseline {baseline['untagged_raise_sites']}; "
        "tag new raises with failure_report.tagged() or a class default")
    assert count == baseline["untagged_raise_sites"], (
        f"Coverage grew: lower tests/failure_guidance_baseline.json to {count}")
