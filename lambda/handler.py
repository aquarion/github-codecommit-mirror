"""Mirror GitHub repositories into private AWS CodeCommit repositories.

Invoked on an EventBridge schedule. Each run:

1. lists the repositories owned by the configured GitHub account,
2. makes sure a matching (private) CodeCommit repository exists,
3. ``git clone --mirror`` from GitHub and ``git push --mirror`` to CodeCommit.

Lambda caps a single invocation at 15 minutes, so when the remaining time
drops below ``TIME_BUDGET_SECONDS`` the function asynchronously re-invokes
itself with the repositories it has not reached yet.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

import boto3
from botocore.exceptions import ClientError

LOG = logging.getLogger()
LOG.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

GITHUB_API_URL = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
GITHUB_OWNER = os.environ["GITHUB_OWNER"]
GITHUB_OWNER_TYPE = os.environ.get("GITHUB_OWNER_TYPE", "user").lower()
GITHUB_TOKEN_SECRET_ARN = os.environ["GITHUB_TOKEN_SECRET_ARN"]

CODECOMMIT_REGION = os.environ.get("CODECOMMIT_REGION") or os.environ["AWS_REGION"]
NAME_PREFIX = os.environ.get("CODECOMMIT_NAME_PREFIX", "")

INCLUDE_FORKS = os.environ.get("INCLUDE_FORKS", "false").lower() == "true"
INCLUDE_ARCHIVED = os.environ.get("INCLUDE_ARCHIVED", "false").lower() == "true"
VISIBILITY = os.environ.get("VISIBILITY", "all").lower()
INCLUDE_PATTERN = os.environ.get("INCLUDE_PATTERN") or None
EXCLUDE_PATTERN = os.environ.get("EXCLUDE_PATTERN") or None

MAX_REPO_SIZE_MB = int(os.environ.get("MAX_REPO_SIZE_MB", "4096"))
TIME_BUDGET_SECONDS = int(os.environ.get("TIME_BUDGET_SECONDS", "180"))
MAX_CONTINUATIONS = int(os.environ.get("MAX_CONTINUATIONS", "10"))
METRIC_NAMESPACE = os.environ.get("METRIC_NAMESPACE", "GitHubCodeCommitMirror")

WORK_DIR = os.environ.get("WORK_DIR", "/tmp/mirror")
GIT_CREDENTIALS_FILE = "/tmp/.git-credentials"

# CodeCommit accepts [\w\.-]{1,100}.
_UNSAFE_NAME_CHARS = re.compile(r"[^A-Za-z0-9._-]")
_MAX_CC_NAME = 100

codecommit = boto3.client("codecommit", region_name=CODECOMMIT_REGION)
secretsmanager = boto3.client("secretsmanager")
cloudwatch = boto3.client("cloudwatch")
lambda_client = boto3.client("lambda")

_token_cache: str | None = None


# --------------------------------------------------------------------------
# GitHub
# --------------------------------------------------------------------------
def github_token() -> str:
    """Read the GitHub token from Secrets Manager, cached for the container."""
    global _token_cache
    if _token_cache is None:
        secret = secretsmanager.get_secret_value(SecretId=GITHUB_TOKEN_SECRET_ARN)
        raw = secret["SecretString"].strip()
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            _token_cache = raw
        else:
            if not isinstance(parsed, dict):
                raise ValueError("GitHub token secret JSON must be an object")
            for key in ("token", "github_token", "GITHUB_TOKEN", "value"):
                if parsed.get(key):
                    _token_cache = str(parsed[key]).strip()
                    break
            else:
                raise ValueError(
                    "GitHub token secret JSON has no 'token' key; store the "
                    "token as a plain string or as {\"token\": \"...\"}"
                )
    return _token_cache


def github_get(url: str, token: str, attempts: int = 5) -> tuple[dict | list, dict]:
    """GET a GitHub API URL, retrying on rate limits and transient errors."""
    request = urllib.request.Request(url)
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("X-GitHub-Api-Version", "2022-11-28")
    request.add_header("User-Agent", "github-codecommit-mirror")

    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                headers = dict(response.headers)
                return json.loads(response.read().decode("utf-8")), headers
        except urllib.error.HTTPError as error:
            retryable = error.code in (403, 429) or error.code >= 500
            if not retryable or attempt == attempts:
                raise
            delay = _retry_delay(error, attempt)
            LOG.warning(
                "GitHub returned %s for %s, retrying in %ss (attempt %s/%s)",
                error.code, _redact_url(url), delay, attempt, attempts,
            )
            time.sleep(delay)
        except urllib.error.URLError as error:
            if attempt == attempts:
                raise
            LOG.warning("GitHub request failed (%s), retrying", error)
            time.sleep(2 ** attempt)

    raise RuntimeError("unreachable")


def _retry_delay(error: urllib.error.HTTPError, attempt: int) -> int:
    """Honour Retry-After / x-ratelimit-reset, capped so we stay inside Lambda."""
    retry_after = error.headers.get("Retry-After")
    if retry_after and retry_after.isdigit():
        return min(int(retry_after), 60)
    if error.headers.get("x-ratelimit-remaining") == "0":
        reset = error.headers.get("x-ratelimit-reset")
        if reset and reset.isdigit():
            return max(1, min(int(reset) - int(time.time()), 60))
    return min(2 ** attempt, 60)


def _redact_url(url: str) -> str:
    return re.sub(r"//[^@/]+@", "//***@", url)


def list_github_repositories(token: str) -> list[dict]:
    """List every repository owned by GITHUB_OWNER that passes the filters."""
    if GITHUB_OWNER_TYPE == "org":
        url = f"{GITHUB_API_URL}/orgs/{GITHUB_OWNER}/repos?per_page=100&type=all"
    else:
        viewer, _ = github_get(f"{GITHUB_API_URL}/user", token)
        if str(viewer.get("login", "")).lower() == GITHUB_OWNER.lower():
            # /user/repos is the only listing that includes private repos.
            url = (
                f"{GITHUB_API_URL}/user/repos"
                f"?per_page=100&affiliation=owner&visibility={VISIBILITY}"
            )
        else:
            LOG.warning(
                "Token belongs to %s, not %s; only public repositories are visible",
                viewer.get("login"), GITHUB_OWNER,
            )
            url = f"{GITHUB_API_URL}/users/{GITHUB_OWNER}/repos?per_page=100&type=owner"

    repositories: list[dict] = []
    while url:
        page, headers = github_get(url, token)
        repositories.extend(page)
        url = _next_link(headers.get("Link", ""))

    selected = [repo for repo in repositories if _wanted(repo)]
    selected.sort(key=lambda repo: repo["full_name"].lower())
    LOG.info(
        "GitHub returned %s repositories, %s selected for mirroring",
        len(repositories), len(selected),
    )
    return [
        {
            "full_name": repo["full_name"],
            "clone_url": repo["clone_url"],
            "description": repo.get("description") or "",
            "size_kb": repo.get("size") or 0,
        }
        for repo in selected
    ]


def _next_link(link_header: str) -> str | None:
    for part in link_header.split(","):
        section = part.split(";")
        if len(section) < 2:
            continue
        if 'rel="next"' in section[1].replace(" ", "").replace("'", '"'):
            return section[0].strip().strip("<>")
    return None


def _wanted(repo: dict) -> bool:
    name = repo["full_name"]
    owner = (repo.get("owner") or {}).get("login", "")
    if owner.lower() != GITHUB_OWNER.lower():
        return False
    if repo.get("fork") and not INCLUDE_FORKS:
        LOG.debug("Skipping fork %s", name)
        return False
    if repo.get("archived") and not INCLUDE_ARCHIVED:
        LOG.debug("Skipping archived repo %s", name)
        return False
    if VISIBILITY == "private" and not repo.get("private"):
        return False
    if VISIBILITY == "public" and repo.get("private"):
        return False
    if INCLUDE_PATTERN and not re.search(INCLUDE_PATTERN, name):
        LOG.debug("Skipping %s, does not match INCLUDE_PATTERN", name)
        return False
    if EXCLUDE_PATTERN and re.search(EXCLUDE_PATTERN, name):
        LOG.debug("Skipping %s, matches EXCLUDE_PATTERN", name)
        return False
    return True


# --------------------------------------------------------------------------
# CodeCommit
# --------------------------------------------------------------------------
def codecommit_name(full_name: str) -> str:
    """Map ``owner/repo`` onto a valid, collision-free CodeCommit name."""
    candidate = NAME_PREFIX + _UNSAFE_NAME_CHARS.sub("-", full_name.replace("/", "-"))
    candidate = candidate.strip("-") or "repository"
    if len(candidate) > _MAX_CC_NAME:
        digest = hashlib.sha256(full_name.encode("utf-8")).hexdigest()[:8]
        candidate = candidate[: _MAX_CC_NAME - len(digest) - 1].rstrip("-") + "-" + digest
    return candidate


def ensure_codecommit_repository(name: str, description: str, source: str) -> None:
    """Create the CodeCommit repository if this is the first time we see it."""
    try:
        codecommit.get_repository(repositoryName=name)
        return
    except ClientError as error:
        if error.response["Error"]["Code"] != "RepositoryDoesNotExistException":
            raise

    LOG.info("Creating CodeCommit repository %s", name)
    summary = " ".join(f"Mirror of {source}. {description}".split())[:1000]
    try:
        codecommit.create_repository(
            repositoryName=name,
            repositoryDescription=summary,
            tags={"ManagedBy": "github-codecommit-mirror", "SourceRepository": source},
        )
    except ClientError as error:
        # A concurrent run may have won the race; that is fine.
        if error.response["Error"]["Code"] != "RepositoryNameExistsException":
            raise


# --------------------------------------------------------------------------
# git
# --------------------------------------------------------------------------
def configure_git(token: str) -> None:
    """Set up a credential store so the token never lands in argv or a remote."""
    host = urllib.parse.urlsplit(GITHUB_API_URL).netloc
    host = host[4:] if host.startswith("api.") else host
    credential = f"https://x-access-token:{urllib.parse.quote(token, safe='')}@{host}\n"
    with open(GIT_CREDENTIALS_FILE, "w", encoding="utf-8") as handle:
        handle.write(credential)
    os.chmod(GIT_CREDENTIALS_FILE, 0o600)

    git(["config", "--global", "credential.helper", f"store --file={GIT_CREDENTIALS_FILE}"])
    git(["config", "--global", "credential.https://" + host + ".username", "x-access-token"])
    git(["config", "--global", "user.name", "github-codecommit-mirror"])
    git(["config", "--global", "user.email", "mirror@localhost"])


def git(
    args: list[str],
    cwd: str | None = None,
    timeout: int = 900,
    stdin: str | None = None,
) -> str:
    """Run git, raising a MirrorError whose message never contains the token."""
    environment = dict(os.environ)
    environment.setdefault("HOME", "/tmp")
    environment.setdefault("GIT_CONFIG_GLOBAL", "/tmp/.gitconfig")
    environment["GIT_TERMINAL_PROMPT"] = "0"
    environment["GIT_ASKPASS"] = "/bin/true"

    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=environment,
        input=stdin,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        detail = f"{result.stdout}\n{result.stderr}".strip()
        # Scrub the whole message: the arguments can carry credentials too.
        raise MirrorError(
            _scrub(f"git {' '.join(args[:2])} failed ({result.returncode}): {detail}")
        )
    return result.stdout


def _scrub(text: str) -> str:
    token = _token_cache
    if token:
        text = text.replace(token, "***").replace(
            base64.b64encode(f"x-access-token:{token}".encode()).decode(), "***"
        )
    return _redact_url(text)


class MirrorError(RuntimeError):
    """A single repository failed to mirror."""


def mirror_repository(repo: dict) -> str:
    """Mirror one GitHub repository into CodeCommit. Returns a status string."""
    source = repo["full_name"]
    target = codecommit_name(source)
    size_mb = repo.get("size_kb", 0) / 1024

    if size_mb > MAX_REPO_SIZE_MB:
        LOG.warning(
            "Skipping %s: %.0f MB exceeds MAX_REPO_SIZE_MB (%s)",
            source, size_mb, MAX_REPO_SIZE_MB,
        )
        return "skipped"

    ensure_codecommit_repository(target, repo.get("description", ""), source)

    workdir = os.path.join(WORK_DIR, f"{target}.git")
    shutil.rmtree(workdir, ignore_errors=True)
    os.makedirs(WORK_DIR, exist_ok=True)

    try:
        LOG.info("Cloning %s (%.0f MB)", source, size_mb)
        git(["clone", "--mirror", "--quiet", repo["clone_url"], workdir])

        prune_refs(workdir)
        if not git(["for-each-ref", "--format=%(refname)"], cwd=workdir).strip():
            LOG.info("%s has no branches or tags; created %s empty", source, target)
            return "empty"

        LOG.info("Pushing %s to CodeCommit repository %s", source, target)
        git(
            ["push", "--mirror", "--quiet", f"codecommit::{CODECOMMIT_REGION}://{target}"],
            cwd=workdir,
        )
        return "mirrored"
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def prune_refs(workdir: str) -> None:
    """Drop refs CodeCommit will not accept, e.g. GitHub's refs/pull/*."""
    refs = git(["for-each-ref", "--format=%(refname)"], cwd=workdir).splitlines()
    unwanted = [
        ref for ref in refs
        if not (ref.startswith("refs/heads/") or ref.startswith("refs/tags/"))
    ]
    if not unwanted:
        return

    LOG.info("Dropping %s non-branch/tag refs before push", len(unwanted))
    git(
        ["update-ref", "--stdin"],
        cwd=workdir,
        stdin="".join(f"delete {ref}\n" for ref in unwanted),
    )


# --------------------------------------------------------------------------
# Handler
# --------------------------------------------------------------------------
def lambda_handler(event, context):
    event = event if isinstance(event, dict) else {}
    continuation = int(event.get("continuation", 0))

    token = github_token()
    configure_git(token)

    pending = event.get("pending")
    if pending is None:
        pending = list_github_repositories(token)

    counts = {"mirrored": 0, "empty": 0, "skipped": 0, "failed": 0}
    failures: list[str] = []
    remaining: list[dict] = []

    for index, repo in enumerate(pending):
        if _out_of_time(context) and index > 0:
            remaining = pending[index:]
            break
        try:
            counts[mirror_repository(repo)] += 1
        except Exception as error:  # noqa: BLE001 - one repo must not stop the run
            counts["failed"] += 1
            failures.append(repo["full_name"])
            LOG.exception("Failed to mirror %s: %s", repo["full_name"], _scrub(str(error)))

    _publish_metrics(counts)
    summary = {**counts, "remaining": len(remaining), "continuation": continuation}
    LOG.info("Run summary: %s", json.dumps(summary))

    if remaining:
        _continue(context, remaining, continuation)

    if failures:
        raise RuntimeError(
            f"{len(failures)} repositories failed to mirror: {', '.join(failures[:20])}"
        )
    return summary


def _out_of_time(context) -> bool:
    if context is None:
        return False
    return context.get_remaining_time_in_millis() < TIME_BUDGET_SECONDS * 1000


def _continue(context, remaining: list[dict], continuation: int) -> None:
    """Hand the rest of the work to a fresh asynchronous invocation."""
    if continuation >= MAX_CONTINUATIONS:
        LOG.error(
            "Reached MAX_CONTINUATIONS (%s) with %s repositories left; they will be "
            "picked up on the next scheduled run",
            MAX_CONTINUATIONS, len(remaining),
        )
        return

    LOG.info("Out of time: continuing with %s repositories", len(remaining))
    lambda_client.invoke(
        FunctionName=context.invoked_function_arn,
        InvocationType="Event",
        Payload=json.dumps({"pending": remaining, "continuation": continuation + 1}).encode(),
    )


def _publish_metrics(counts: dict) -> None:
    try:
        cloudwatch.put_metric_data(
            Namespace=METRIC_NAMESPACE,
            MetricData=[
                {"MetricName": name.capitalize(), "Value": value, "Unit": "Count"}
                for name, value in counts.items()
            ],
        )
    except ClientError as error:
        LOG.warning("Could not publish metrics: %s", error)
