#!/usr/bin/env python3
"""Mirror allowlisted upstream skills into skills/ and verify the mirror.

Commands:
  update  Resolve each source branch to its current commit, rebuild skills/,
          and rewrite sources.lock.json and NOTICE.md.
  check   Validate skills/, the lock file, NOTICE.md and the plugin manifests.
          With --upstream, also rebuild from the locked commits and require a
          byte-for-byte match.

Standard library only. Upstream repositories are fetched anonymously over
HTTPS; no credential is used.
"""

from __future__ import annotations

import argparse
import filecmp
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILLS_DIR = ROOT / "skills"
SOURCES_FILE = ROOT / "sources.toml"
LOCK_FILE = ROOT / "sources.lock.json"
NOTICE_FILE = ROOT / "NOTICE.md"
MANIFESTS = [
    ROOT / ".claude-plugin" / "marketplace.json",
    ROOT / ".claude-plugin" / "plugin.json",
    ROOT / ".cursor-plugin" / "marketplace.json",
    ROOT / ".cursor-plugin" / "plugin.json",
]
# Plugin components other than skills. None may appear in a manifest or in a
# mirrored skill: the plugin must only add instructions.
FORBIDDEN_MANIFEST_KEYS = {
    "mcpServers",
    "hooks",
    "apps",
    "lspServers",
    "commands",
    "agents",
    "userConfig",
    "experimental",
    "channels",
    "monitors",
}
FORBIDDEN_NAMES = {
    ".mcp.json",
    "mcp.json",
    ".app.json",
    "hooks",
    "hooks.json",
    ".claude-plugin",
    ".cursor-plugin",
    ".codex-plugin",
}
SKILL_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
# Frontmatter keys of the Agent Skills format. Client extensions such as
# `hooks`, `allowed-tools` or `context` change what a skill may do without
# asking, so any other key fails the check.
ALLOWED_FRONTMATTER = {"name", "description", "license", "metadata", "compatibility"}
# Claude Code runs `!`command`` and ```! blocks in a skill when it loads.
SHELL_INJECTION = re.compile(r"(^|\s)!`|^[ \t]*```!", re.MULTILINE)


class SyncError(Exception):
    pass


def git(*args: str, cwd: Path | None = None) -> str:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    result = subprocess.run(
        ["git", "-c", "credential.helper=", *args],
        cwd=cwd,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def load_sources() -> list[dict]:
    with SOURCES_FILE.open("rb") as f:
        sources = tomllib.load(f)["source"]
    seen: set[str] = set()
    for source in sources:
        for key in ("name", "repo", "branch", "path", "spdx", "skills"):
            if key not in source:
                raise SyncError(f"source {source.get('name')!r} lacks {key!r}")
        if ("license" in source) == ("skill_license" in source):
            raise SyncError(
                f"source {source['name']!r} needs exactly one of license and skill_license"
            )
        for skill in source["skills"]:
            if not SKILL_NAME.match(skill):
                raise SyncError(f"skill name {skill!r} is not kebab-case")
            if skill in seen:
                raise SyncError(f"skill {skill!r} is listed twice")
            seen.add(skill)
    return sources


def repo_url(source: dict) -> str:
    return f"https://github.com/{source['repo']}"


def resolve_branch(source: dict) -> str:
    out = git("ls-remote", repo_url(source), f"refs/heads/{source['branch']}")
    if not out.strip():
        raise SyncError(f"{source['repo']} has no branch {source['branch']!r}")
    return out.split()[0]


def fetch(source: dict, commit: str, dest: Path) -> Path:
    """Check out only the skill path and the license file at one commit."""
    git("init", "-q", str(dest))
    git("remote", "add", "origin", repo_url(source), cwd=dest)
    git("fetch", "-q", "--depth", "1", "--filter=blob:none", "origin", commit, cwd=dest)
    patterns = [f"/{source['path']}/"]
    if "license" in source:
        patterns.append(f"/{source['license']}")
    git("sparse-checkout", "set", "--no-cone", *patterns, cwd=dest)
    git("checkout", "-q", "FETCH_HEAD", cwd=dest)
    fetched = git("rev-parse", "HEAD", cwd=dest).strip()
    if fetched != commit:
        raise SyncError(f"{source['repo']}: fetched {fetched}, expected {commit}")
    return dest


def frontmatter(text: str) -> dict[str, str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise SyncError("SKILL.md does not start with YAML frontmatter")
    fields: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            return fields
        if not line.strip() or line.startswith((" ", "\t", "#")):
            continue
        match = re.match(r"^([A-Za-z_][\w-]*):(?:\s+(.*))?$", line)
        if not match:
            # Quoted, merge (<<) or complex (?) keys could smuggle in keys the
            # allowlist would reject, so anything unrecognised fails closed.
            raise SyncError(f"SKILL.md frontmatter line not understood: {line!r}")
        fields[match.group(1)] = (match.group(2) or "").strip().strip("'\"")
    raise SyncError("SKILL.md frontmatter is not closed")


def validate_skill(skill_dir: Path) -> None:
    name = skill_dir.name
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.is_file():
        raise SyncError(f"{name}: SKILL.md is missing")
    fields = frontmatter(skill_md.read_text(encoding="utf-8"))
    if fields.get("name") != name:
        raise SyncError(f"{name}: frontmatter name is {fields.get('name')!r}")
    if "description" not in fields:
        raise SyncError(f"{name}: frontmatter has no description")
    extra = set(fields) - ALLOWED_FRONTMATTER
    if extra:
        raise SyncError(f"{name}: frontmatter keys outside the allowlist: {sorted(extra)}")
    if not (skill_dir / "LICENSE").is_file() and not (skill_dir / "LICENSE.txt").is_file():
        raise SyncError(f"{name}: no LICENSE or LICENSE.txt")
    for path in skill_dir.rglob("*"):
        rel = path.relative_to(skill_dir)
        if path.is_symlink():
            raise SyncError(f"{name}: symlink {rel}")
        if path.name in FORBIDDEN_NAMES:
            raise SyncError(f"{name}: forbidden plugin component {rel}")
        if path.suffix == ".md" and SHELL_INJECTION.search(path.read_text(encoding="utf-8")):
            raise SyncError(f"{name}: {rel} contains a shell command that runs on load")


def copy_skill(source: dict, upstream: Path, skill: str, out: Path) -> None:
    src = upstream / source["path"] / skill
    if not src.is_dir():
        raise SyncError(f"{source['repo']}: allowlisted skill {skill!r} no longer exists")
    dst = out / skill
    excluded = set(source.get("exclude", []))
    for path in sorted(src.rglob("*")):
        rel = path.relative_to(src)
        if rel.parts[0] in excluded:
            continue
        if path.is_symlink():
            raise SyncError(f"{skill}: upstream symlink {rel}")
        target = dst / rel
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
            # Keep the executable bit: skills run their scripts directly.
            shutil.copymode(path, target)
    if "skill_license" in source:
        if not (dst / source["skill_license"]).is_file():
            raise SyncError(f"{skill}: upstream {source['skill_license']} is missing")
    else:
        if (dst / "LICENSE").exists():
            raise SyncError(f"{skill}: upstream skill already has a LICENSE file")
        shutil.copyfile(upstream / source["license"], dst / "LICENSE")
    validate_skill(dst)


def unlisted_skills(source: dict, upstream: Path) -> list[str]:
    base = upstream / source["path"]
    found = {p.parent.name for p in base.glob("*/SKILL.md")}
    return sorted(found - set(source["skills"]))


def require_on_branch(source: dict, commit: str, repo: Path) -> None:
    """Fail unless commit is reachable from the upstream branch.

    A fetch by SHA also serves unmerged pull request commits, so a hand-edited
    lock could otherwise point at code that never landed upstream.
    """
    git("fetch", "-q", "--filter=tree:0", "origin", f"refs/heads/{source['branch']}", cwd=repo)
    try:
        git("merge-base", "--is-ancestor", commit, "FETCH_HEAD", cwd=repo)
    except subprocess.CalledProcessError:
        raise SyncError(
            f"{source['repo']}: {commit} is not on branch {source['branch']!r}"
        ) from None


def build(
    sources: list[dict],
    commits: dict[str, str],
    out: Path,
    work: Path,
    verify_branch: bool = False,
) -> dict[str, list[str]]:
    """Build skills into out from the given commits. Returns unlisted skills per source."""
    out.mkdir(parents=True)
    unlisted: dict[str, list[str]] = {}
    for source in sources:
        upstream = fetch(source, commits[source["name"]], work / source["name"])
        if verify_branch:
            require_on_branch(source, commits[source["name"]], upstream)
        for skill in source["skills"]:
            copy_skill(source, upstream, skill, out)
        unlisted[source["name"]] = unlisted_skills(source, upstream)
    return unlisted


def file_mode(path: Path) -> str:
    return "100755" if os.access(path, os.X_OK) else "100644"


def file_hashes(skills_dir: Path) -> dict[str, str]:
    """Map each file to "<git mode> <sha256>"."""
    hashes = {}
    for path in sorted(skills_dir.rglob("*")):
        if path.is_file():
            rel = path.relative_to(skills_dir.parent).as_posix()
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            hashes[rel] = f"{file_mode(path)} {digest}"
    return hashes


def lock_data(sources: list[dict], commits: dict[str, str], skills_dir: Path) -> dict:
    return {
        "sources": {
            s["name"]: {"repo": s["repo"], "branch": s["branch"], "commit": commits[s["name"]]}
            for s in sources
        },
        "files": file_hashes(skills_dir),
    }


def notice_text(sources: list[dict], commits: dict[str, str]) -> str:
    lines = [
        "# Third-party notices",
        "",
        "<!-- Generated by scripts/skills.py from sources.toml and sources.lock.json. -->",
        "",
        "Every directory under `skills/` is an unmodified copy of an upstream skill,",
        "except that the subdirectories named in the Excluded column are left out.",
        "Each skill keeps its upstream license file (`LICENSE` copied from the",
        "upstream repository root, or the skill's own `LICENSE.txt`). The skills",
        "remain under those licenses and belong to their authors. This repository",
        "is not affiliated with or endorsed by any of them, and no trademark rights",
        "are granted.",
        "",
        "| Skill | Upstream | Path | Commit | License | Excluded |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for source in sources:
        commit = commits[source["name"]]
        excluded = ", ".join(f"`{e}/`" for e in source.get("exclude", [])) or "none"
        for skill in source["skills"]:
            url = f"{repo_url(source)}/tree/{commit}/{source['path']}/{skill}"
            lines.append(
                f"| `{skill}` | [{source['repo']}]({repo_url(source)}) "
                f"| [`{source['path']}/{skill}`]({url}) | `{commit[:7]}` "
                f"| {source['spdx']} | {excluded} |"
            )
    return "\n".join(lines) + "\n"


def write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def compare_trees(expected: Path, actual: Path) -> list[str]:
    diffs = []
    exp = {p.relative_to(expected).as_posix() for p in expected.rglob("*") if p.is_file()}
    act = {p.relative_to(actual).as_posix() for p in actual.rglob("*") if p.is_file()}
    diffs += [f"missing {p}" for p in sorted(exp - act)]
    diffs += [f"unexpected {p}" for p in sorted(act - exp)]
    for p in sorted(exp & act):
        if not filecmp.cmp(expected / p, actual / p, shallow=False):
            diffs.append(f"differs {p}")
        elif file_mode(expected / p) != file_mode(actual / p):
            diffs.append(f"mode differs {p}")
    return diffs


def check_manifests() -> None:
    for path in MANIFESTS:
        data = json.loads(path.read_text(encoding="utf-8"))
        entries = [data, *data.get("plugins", [])]
        for entry in entries:
            bad = FORBIDDEN_MANIFEST_KEYS & set(entry)
            if bad:
                raise SyncError(f"{path.relative_to(ROOT)}: forbidden keys {sorted(bad)}")


def cmd_check(upstream: bool) -> None:
    sources = load_sources()
    lock = json.loads(LOCK_FILE.read_text(encoding="utf-8"))
    commits = {name: entry["commit"] for name, entry in lock["sources"].items()}
    if set(commits) != {s["name"] for s in sources}:
        raise SyncError("sources.lock.json sources do not match sources.toml")
    for source in sources:
        locked = lock["sources"][source["name"]]
        if (locked["repo"], locked["branch"]) != (source["repo"], source["branch"]):
            raise SyncError(f"{source['name']}: lock repo/branch differ from sources.toml")
    listed = sorted(s for source in sources for s in source["skills"])
    present = sorted(p.name for p in SKILLS_DIR.iterdir())
    if listed != present:
        raise SyncError(f"skills/ holds {present}, sources.toml lists {listed}")
    for name in present:
        validate_skill(SKILLS_DIR / name)
    if file_hashes(SKILLS_DIR) != lock["files"]:
        raise SyncError("skills/ does not match the hashes in sources.lock.json")
    if NOTICE_FILE.read_text(encoding="utf-8") != notice_text(sources, commits):
        raise SyncError("NOTICE.md is stale; run scripts/skills.py update")
    check_manifests()
    if upstream:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "skills"
            build(sources, commits, out, Path(tmp) / "work", verify_branch=True)
            diffs = compare_trees(out, SKILLS_DIR)
            if diffs:
                raise SyncError("skills/ differs from the locked upstreams:\n" + "\n".join(diffs))
    print(f"ok: {len(present)} skills")


REVIEW_FREE = re.compile(r"(\.md|/LICENSE(\.txt)?)$")


def cmd_update(summary: Path | None, auto_merge_flag: Path | None) -> None:
    sources = load_sources()
    old = json.loads(LOCK_FILE.read_text(encoding="utf-8")) if LOCK_FILE.exists() else None
    commits = {s["name"]: resolve_branch(s) for s in sources}
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "skills"
        unlisted = build(sources, commits, out, Path(tmp) / "work")
        if SKILLS_DIR.exists():
            shutil.rmtree(SKILLS_DIR)
        shutil.copytree(out, SKILLS_DIR)
    lock = lock_data(sources, commits, SKILLS_DIR)
    write_json(LOCK_FILE, lock)
    NOTICE_FILE.write_text(notice_text(sources, commits), encoding="utf-8")
    check_manifests()
    changed = changed_files(old, lock)
    review_free = bool(changed) and all(REVIEW_FREE.search(f) for f in changed)
    if summary:
        summary.write_text(
            summary_text(sources, old, lock, unlisted, changed, review_free), encoding="utf-8"
        )
    if auto_merge_flag:
        auto_merge_flag.write_text("true\n" if review_free else "false\n", encoding="utf-8")


def changed_files(old: dict | None, new: dict) -> list[str]:
    old_files = old["files"] if old else {}
    return sorted(
        f for f in set(old_files) | set(new["files"]) if old_files.get(f) != new["files"].get(f)
    )


def summary_text(
    sources: list[dict],
    old: dict | None,
    new: dict,
    unlisted: dict[str, list[str]],
    changed: list[str],
    review_free: bool,
) -> str:
    lines = ["Upstream commits:", ""]
    for source in sources:
        name = source["name"]
        before = old["sources"].get(name, {}).get("commit") if old else None
        after = new["sources"][name]["commit"]
        if before and before != after:
            compare = f"{repo_url(source)}/compare/{before}...{after}"
            lines.append(f"- {source['repo']}: [`{before[:7]}...{after[:7]}`]({compare})")
        else:
            lines.append(f"- {source['repo']}: `{after[:7]}`" + (" (unchanged)" if before else ""))
    lines += ["", f"Changed files: {len(changed)}", ""]
    lines += [f"- `{f}`" for f in changed[:100]]
    if len(changed) > 100:
        lines.append(f"- … and {len(changed) - 100} more")
    if not review_free:
        lines += [
            "",
            "**Needs review:** files other than Markdown and license files changed, so",
            "auto-merge is off. Check the compare links, especially scripts.",
        ]
    new_skills = {k: v for k, v in unlisted.items() if v}
    if new_skills:
        lines += ["", "Upstream skills not mirrored (add them to sources.toml to include):", ""]
        for name, skills in new_skills.items():
            lines.append(f"- {name}: " + ", ".join(f"`{s}`" for s in skills))
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    update = sub.add_parser("update")
    update.add_argument("--summary", type=Path, help="write a Markdown change summary here")
    update.add_argument(
        "--auto-merge-flag",
        type=Path,
        help="write true here when only Markdown and license files changed",
    )
    check = sub.add_parser("check")
    check.add_argument("--upstream", action="store_true", help="also rebuild from locked commits")
    args = parser.parse_args()
    try:
        if args.command == "update":
            cmd_update(args.summary, args.auto_merge_flag)
        else:
            cmd_check(args.upstream)
    except (SyncError, subprocess.CalledProcessError) as error:
        detail = getattr(error, "stderr", None)
        print(f"error: {error}" + (f"\n{detail}" if detail else ""), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
