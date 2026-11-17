# skills

A skills-only plugin marketplace for Claude and Cursor. It mirrors selected
upstream [Agent Skills](https://agentskills.io) without changing them, so one
repository serves the same skills to every client and follows upstream
updates.

This is a personal, unofficial mirror. It is not affiliated with or endorsed
by Cloudflare, OpenAI, GitHub or any other upstream author.

## What is included

The skills listed in [`sources.toml`](sources.toml), copied from the upstream
commits pinned in [`sources.lock.json`](sources.lock.json). See
[NOTICE.md](NOTICE.md) for each skill's source, commit and license.

The plugin adds instructions only. It contains no MCP servers, hooks, apps,
commands or credentials, and the check fails if a mirrored skill or a manifest
adds any of them.

## Licenses

Everything under `skills/` stays under its upstream license and belongs to its
authors. Each skill directory carries that license file: `LICENSE` copied from
the upstream repository root, or the skill's own `LICENSE.txt`. Skills are
copied byte for byte; the only difference from upstream is that the
subdirectories named in [NOTICE.md](NOTICE.md) are left out (`agents/` client
metadata, which declares MCP server dependencies).

A skill is mirrored only if its upstream grants a license for it. For example,
the OpenAI Developers plugin is marked "Proprietary" and its repository has no
root license, so only its two skills that ship their own Apache-2.0
`LICENSE.txt` are included.

The repository's own files (scripts, workflows, manifests and documentation,
everything outside `skills/`) are under the [MIT License](LICENSE).

## Install

- Claude: Customize → Plugins → Add → Add marketplace → `risu729/skills`, then
  install `skills`. Turn on automatic sync to follow new commits.
- Cursor: Customize → Plugins → Add → From GitHub Repository →
  `https://github.com/risu729/skills`, then install `skills`.

The plugin has no `version`, so clients follow the latest commit.

## Updates

[`sync.yml`](.github/workflows/sync.yml) runs daily. It resolves each
upstream branch, rebuilds `skills/` with `scripts/skills.py update`, and opens
or refreshes the `sync/upstream` pull request with upstream compare links and
the changed files. Upstream skills that are not in `sources.toml` are only
reported there, never copied. If upstream already matches `main`, an open sync
pull request is closed.

The pull request merges automatically once `CI Check` passes only when every
changed file is Markdown or a license file. Any other change, such as a script,
turns auto-merge off and waits for a person to review the compare links.

`CI Check` runs `scripts/skills.py check --upstream`. It fails when:

- `skills/` differs from a fresh copy of the locked upstream commits, in
  content or executable bit, or a locked commit is not on its upstream branch;
- a listed skill disappears upstream, or a skill directory is not listed;
- a `SKILL.md` lacks frontmatter, its `name` differs from its directory, or it
  uses a frontmatter key outside `name`, `description`, `license`, `metadata`
  and `compatibility` (client extensions such as `hooks` or `allowed-tools`
  change what a skill may do without asking);
- a Markdown file contains a shell command that runs when the skill loads
  (`` !`command` `` or a ```` ```! ```` block);
- a skill has no license file, contains a symlink, or contains a plugin
  component (`.mcp.json`, `hooks`, plugin manifests);
- a manifest declares anything besides skills;
- `NOTICE.md` or the file hashes in `sources.lock.json` are stale.

To add or remove a skill, edit `sources.toml`, run
`python3 scripts/skills.py update`, and open a pull request. Python 3.11 or
later and Git are the only requirements.

## Repository settings the workflows rely on

- Actions: "Allow GitHub Actions to create and approve pull requests" on, so
  `sync.yml` can open its pull request with `GITHUB_TOKEN`.
- Pull requests: squash merging only, auto-merge allowed.
- Ruleset on `main`: pull request with no required approvals, signed commits
  (GitHub signs squash merges), linear history, and the `CI Check` and CodeQL
  checks.
