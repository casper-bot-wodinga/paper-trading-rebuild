"""Prompt versioning — git-based prompt evolution per SPEC-v3 §13.

Branch naming conventions:
  main                              → production prompts
  sweep/YYYY-MM-DD/{trader}/variant-NNN  → nightly sweep (auto-generated, disposable)
  experiment/{trader}/{name}        → manual or agent-proposed experiments
  {trader}/v{major}.{minor}.{patch}  → git tags for stable releases

Winner promotion: sweep branch → PR → main (squash-merge) → tag
Pruning: sweep branches deleted after 7 days, experiment after 14 days idle.
"""

from __future__ import annotations

import re
import subprocess
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional, Tuple

log = logging.getLogger("prompt_versioning")

# ── Branch name parsing ──────────────────────────────────────────────────────

SWEEP_RE = re.compile(
    r"^sweep/(?P<date>\d{4}-\d{2}-\d{2})/(?P<trader>\w+)/variant-(?P<variant>\d+)$"
)
EXPERIMENT_RE = re.compile(
    r"^experiment/(?P<trader>\w+)/(?P<name>[\w-]+)$"
)
TAG_RE = re.compile(
    r"^(?P<trader>\w+)/v(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)$"
)


@dataclass
class SweepBranch:
    """Parsed sweep branch name."""
    date: str          # "YYYY-MM-DD"
    trader: str         # "kairos"
    variant: int        # 1-999
    branch_name: str    # full branch name

    @property
    def remote_name(self) -> str:
        return f"origin/{self.branch_name}"

    @classmethod
    def create(cls, date_str: str, trader: str, variant: int) -> "SweepBranch":
        name = f"sweep/{date_str}/{trader}/variant-{variant:03d}"
        return cls(date=date_str, trader=trader, variant=variant, branch_name=name)

    @classmethod
    def parse(cls, branch_name: str) -> Optional["SweepBranch"]:
        m = SWEEP_RE.match(branch_name.strip().removeprefix("origin/"))
        if not m:
            return None
        return cls(
            date=m.group("date"),
            trader=m.group("trader"),
            variant=int(m.group("variant")),
            branch_name=branch_name.strip().removeprefix("origin/"),
        )


@dataclass
class ExperimentBranch:
    """Parsed experiment branch name."""
    trader: str
    name: str
    branch_name: str

    @classmethod
    def parse(cls, branch_name: str) -> Optional["ExperimentBranch"]:
        m = EXPERIMENT_RE.match(branch_name.strip().removeprefix("origin/"))
        if not m:
            return None
        return cls(
            trader=m.group("trader"),
            name=m.group("name"),
            branch_name=branch_name.strip().removeprefix("origin/"),
        )


@dataclass
class PromptTag:
    """Parsed git tag for a stable prompt release."""
    trader: str
    major: int
    minor: int
    patch: int
    tag_name: str

    @classmethod
    def parse(cls, tag_name: str) -> Optional["PromptTag"]:
        m = TAG_RE.match(tag_name.strip())
        if not m:
            return None
        return cls(
            trader=m.group("trader"),
            major=int(m.group("major")),
            minor=int(m.group("minor")),
            patch=int(m.group("patch")),
            tag_name=tag_name.strip(),
        )

    def bump_patch(self) -> "PromptTag":
        return PromptTag(
            trader=self.trader,
            major=self.major,
            minor=self.minor,
            patch=self.patch + 1,
            tag_name=f"{self.trader}/v{self.major}.{self.minor}.{self.patch + 1}",
        )

    def bump_minor(self) -> "PromptTag":
        return PromptTag(
            trader=self.trader,
            major=self.major,
            minor=self.minor + 1,
            patch=0,
            tag_name=f"{self.trader}/v{self.major}.{self.minor + 1}.0",
        )

    def __str__(self) -> str:
        return self.tag_name


# ── Git operations ────────────────────────────────────────────────────────────


class GitError(Exception):
    """Git operation failed."""
    pass


def _run_git(
    repo_path: Path,
    args: List[str],
    capture: bool = True,
    timeout: int = 30,
) -> Tuple[int, str, str]:
    """Run a git command and return (exit_code, stdout, stderr)."""
    cmd = ["git", "-C", str(repo_path)] + args
    try:
        r = subprocess.run(
            cmd,
            capture_output=capture,
            text=True,
            timeout=timeout,
        )
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except subprocess.TimeoutExpired:
        raise GitError(f"Git command timed out after {timeout}s: {' '.join(cmd)}")
    except FileNotFoundError:
        raise GitError("git not found on PATH")


def _must_git(repo_path: Path, args: List[str], timeout: int = 30) -> str:
    """Run git, raise GitError on failure, return stdout."""
    rc, out, err = _run_git(repo_path, args, capture=True, timeout=timeout)
    if rc != 0:
        raise GitError(f"git {' '.join(args)} failed (exit {rc}): {err}")
    return out


class PromptRepo:
    """Manages prompt versioning in a git repository.

    Args:
        repo_path: Path to the git repository containing prompt files.
        remote: Remote name (default 'origin').
    """

    def __init__(self, repo_path: Path, remote: str = "origin"):
        self.repo_path = Path(repo_path).resolve()
        self.remote = remote
        if not (self.repo_path / ".git").exists():
            raise GitError(f"Not a git repository: {self.repo_path}")

    # ── Discovery ─────────────────────────────────────────────────────────

    def list_branches(self) -> List[str]:
        """List all local branches."""
        out = _must_git(self.repo_path, ["branch", "--format=%(refname:short)"])
        return out.split("\n") if out else []

    def list_remote_branches(self) -> List[str]:
        """List all remote branches."""
        out = _must_git(self.repo_path, ["branch", "-r", "--format=%(refname:short)"])
        return out.split("\n") if out else []

    def list_tags(self) -> List[str]:
        """List all tags."""
        out = _must_git(self.repo_path, ["tag", "--format=%(refname:strip=2)"])
        return out.split("\n") if out else []

    def list_sweep_branches(self) -> List[SweepBranch]:
        """Find all sweep branches (local + remote)."""
        branches = set()
        branches.update(self.list_remote_branches())
        branches.update(self.list_branches())
        return sorted(
            [b for b in (SweepBranch.parse(br) for br in branches) if b is not None],
            key=lambda b: (b.date, b.trader, b.variant),
        )

    def list_experiment_branches(self) -> List[ExperimentBranch]:
        """Find all experiment branches (local + remote)."""
        branches = set()
        branches.update(self.list_remote_branches())
        branches.update(self.list_branches())
        return sorted(
            [b for b in (ExperimentBranch.parse(br) for br in branches) if b is not None],
            key=lambda b: (b.trader, b.name),
        )

    def get_latest_tag(self, trader: str) -> Optional[PromptTag]:
        """Get the latest version tag for a trader."""
        tags = self.list_tags()
        trader_tags = sorted(
            [t for t in (PromptTag.parse(tag) for tag in tags) if t is not None and t.trader == trader],
            key=lambda t: (t.major, t.minor, t.patch),
            reverse=True,
        )
        return trader_tags[0] if trader_tags else None

    # ── Sweep lifecycle ───────────────────────────────────────────────────

    def create_sweep_branches(
        self,
        trader: str,
        prompt_content: str,
        variants: List[Tuple[int, str]],  # [(variant_id, new_prompt_content), ...]
        date_str: Optional[str] = None,
        base_branch: str = "main",
    ) -> List[SweepBranch]:
        """Create sweep branches from a base branch.

        Each variant gets its own branch with the modified prompt committed.

        Args:
            trader: Trader name (e.g., 'kairos').
            prompt_content: Base prompt content (what's currently on main).
            variants: List of (variant_id, prompt_content) pairs.
            date_str: Date string YYYY-MM-DD. Defaults to today.
            base_branch: Branch to branch from (default 'main').

        Returns:
            List of created SweepBranch objects.

        Raises:
            GitError: If git operations fail.
        """
        date = date_str or datetime.now().strftime("%Y-%m-%d")
        created = []

        # Ensure we're on the base branch and it's clean
        _must_git(self.repo_path, ["checkout", base_branch])
        _must_git(self.repo_path, ["pull", self.remote, base_branch])

        for vid, new_content in variants:
            sb = SweepBranch.create(date, trader, vid)
            prompt_path = self.repo_path / "traders" / trader / "prompt.md"

            try:
                _must_git(self.repo_path, ["checkout", "-b", sb.branch_name, base_branch])
                prompt_path.parent.mkdir(parents=True, exist_ok=True)
                prompt_path.write_text(new_content)
                _must_git(self.repo_path, ["add", str(prompt_path.relative_to(self.repo_path))])
                _must_git(self.repo_path, [
                    "commit", "-m",
                    f"sweep({trader}): variant-{vid:03d} Calmar via replay",
                ])
                _must_git(self.repo_path, ["push", "-u", self.remote, sb.branch_name])
                created.append(sb)
                log.info("Created sweep branch: %s", sb.branch_name)
            except GitError as e:
                log.error("Failed to create sweep branch %s: %s", sb.branch_name, e)
                # Return to base and continue
                _must_git(self.repo_path, ["checkout", base_branch])
                continue

        # Return to base
        _must_git(self.repo_path, ["checkout", base_branch])
        return created

    def promote_winner(
        self,
        sweep_branch: SweepBranch,
        bump: str = "patch",
        message: Optional[str] = None,
    ) -> PromptTag:
        """Promote a winning sweep branch: squash-merge to main, tag, push.

        Args:
            sweep_branch: The winning sweep branch.
            bump: 'patch' or 'minor' — which version component to increment.
            message: Merge commit message. Defaults to auto-generated.

        Returns:
            The new PromptTag created.

        Raises:
            GitError: If git operations fail.
        """
        trader = sweep_branch.trader
        prev_tag = self.get_latest_tag(trader)

        if prev_tag:
            new_tag = prev_tag.bump_patch() if bump == "patch" else prev_tag.bump_minor()
        else:
            new_tag = PromptTag(
                trader=trader, major=1, minor=0, patch=0,
                tag_name=f"{trader}/v1.0.0",
            )

        msg = message or (
            f"promote({trader}): {sweep_branch.branch_name} -> main\n\n"
            f"Sweep date: {sweep_branch.date}, variant: {sweep_branch.variant:03d}\n"
            f"New version: {new_tag}"
        )

        try:
            # Squash merge
            _must_git(self.repo_path, ["checkout", "main"])
            _must_git(self.repo_path, ["pull", self.remote, "main"])
            _must_git(self.repo_path, [
                "merge", "--squash", sweep_branch.branch_name,
            ])
            _must_git(self.repo_path, ["commit", "-m", msg])

            # Tag
            _must_git(self.repo_path, ["tag", "-a", new_tag.tag_name, "-m", msg])

            # Push
            _must_git(self.repo_path, ["push", self.remote, "main"])
            _must_git(self.repo_path, ["push", self.remote, new_tag.tag_name])

            log.info("Promoted %s -> %s (tagged %s)", sweep_branch.branch_name, "main", new_tag)
            return new_tag

        except GitError:
            # Rollback: reset main to remote
            try:
                _must_git(self.repo_path, ["reset", "--hard", f"{self.remote}/main"])
            except GitError:
                pass
            raise

    def prune_stale_branches(
        self,
        sweep_days: int = 7,
        experiment_days: int = 14,
        dry_run: bool = False,
    ) -> List[str]:
        """Delete stale branches according to pruning rules.

        - sweep/* branches: deleted after `sweep_days` days (always).
        - experiment/* branches: deleted after `experiment_days` days from
          last commit IF no open PR exists.

        Args:
            sweep_days: Days before sweep branches are pruned (default 7).
            experiment_days: Days before idle experiment branches are pruned (default 14).
            dry_run: If True, return list of branches that WOULD be deleted.

        Returns:
            List of deleted (or would-be-deleted) branch names.
        """
        deleted = []
        cutoff_sweep = datetime.now() - timedelta(days=sweep_days)
        cutoff_experiment = datetime.now() - timedelta(days=experiment_days)

        # Fetch to get remote state
        _must_git(self.repo_path, ["fetch", "--prune"])

        # Prune sweep branches
        for sb in self.list_sweep_branches():
            try:
                branch_date = datetime.strptime(sb.date, "%Y-%m-%d")
            except ValueError:
                continue
            if branch_date < cutoff_sweep:
                deleted.append(sb.branch_name)
                if not dry_run:
                    self._delete_remote_branch(sb.branch_name)

        # Prune experiment branches
        for eb in self.list_experiment_branches():
            try:
                # Check last commit date
                out = _must_git(self.repo_path, [
                    "log", "-1", "--format=%aI", eb.branch_name,
                ])
                last_commit = datetime.fromisoformat(out)
            except (GitError, ValueError):
                continue

            if last_commit < cutoff_experiment:
                deleted.append(eb.branch_name)
                if not dry_run:
                    self._delete_remote_branch(eb.branch_name)

        return deleted

    def _delete_remote_branch(self, branch_name: str) -> None:
        """Delete a remote branch (and local tracking branch)."""
        try:
            _must_git(self.repo_path, ["push", self.remote, "--delete", branch_name])
            log.info("Deleted remote branch: %s", branch_name)
        except GitError as e:
            log.warning("Failed to delete remote branch %s: %s", branch_name, e)
        # Also delete local tracking if it exists
        local_name = branch_name.removeprefix(f"{self.remote}/") if branch_name.startswith(f"{self.remote}/") else branch_name
        if local_name in self.list_branches():
            try:
                _must_git(self.repo_path, ["branch", "-D", local_name])
            except GitError:
                pass

    # ── Prompt file access ─────────────────────────────────────────────────

    def read_prompt(self, trader: str, ref: str = "main") -> str:
        """Read a trader's prompt file at a given ref.

        Args:
            trader: Trader name (e.g., 'kairos').
            ref: Git ref (branch, tag, or commit). Default 'main'.

        Returns:
            Prompt content as string.
        """
        try:
            out = _must_git(self.repo_path, [
                "show", f"{ref}:traders/{trader}/prompt.md",
            ])
            return out
        except GitError:
            raise GitError(f"Prompt not found for trader '{trader}' at ref '{ref}'")

    def write_prompt(self, trader: str, content: str, branch: str = "main", commit: bool = True) -> None:
        """Write a prompt file and optionally commit it.

        Args:
            trader: Trader name.
            content: New prompt content.
            branch: Branch to write to (default 'main').
            commit: If True, stage and commit the change.
        """
        prompt_path = self.repo_path / "traders" / trader / "prompt.md"
        prompt_path.parent.mkdir(parents=True, exist_ok=True)

        _must_git(self.repo_path, ["checkout", branch])
        prompt_path.write_text(content)

        if commit:
            _must_git(self.repo_path, [
                "add", str(prompt_path.relative_to(self.repo_path)),
            ])
            _must_git(self.repo_path, [
                "commit", "-m", f"prompt({trader}): update prompt on {branch}",
            ])

    # ── Utility ────────────────────────────────────────────────────────────

    def is_clean(self) -> bool:
        """Check if working tree is clean."""
        rc, out, _ = _run_git(self.repo_path, ["status", "--porcelain"])
        return rc == 0 and out == ""

    def current_branch(self) -> str:
        """Get the current branch name."""
        return _must_git(self.repo_path, ["branch", "--show-current"])


# ── Convenience functions ────────────────────────────────────────────────────


def setup_prompt_repo(repo_path: Path, remote_url: str) -> PromptRepo:
    """Clone or open the prompt repository.

    Args:
        repo_path: Local path for the repo.
        remote_url: Git remote URL.

    Returns:
        PromptRepo instance ready to use.
    """
    if not (repo_path / ".git").exists():
        subprocess.run(["git", "clone", remote_url, str(repo_path)], check=True)
    return PromptRepo(repo_path)


def version_string(tag: Optional[PromptTag]) -> str:
    """Format a tag as a version string."""
    if tag is None:
        return "v0.0.0 (unreleased)"
    return f"{tag.trader}/v{tag.major}.{tag.minor}.{tag.patch}"
