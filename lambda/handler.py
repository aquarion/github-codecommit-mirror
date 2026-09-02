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
from botocore.exceptions import BotoCoreError, ClientError

LOG = logging.getLogger()
# An unknown level would raise at import, before any error handling exists.
try:
    LOG.setLevel(os.environ.get("LOG_LEVEL", "INFO"))
except ValueError:
    LOG.setLevel("INFO")
    LOG.warning("Ignoring unknown LOG_LEVEL %r", os.environ.get("LOG_LEVEL"))

GITHUB_API_URL = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
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

ALERT_EMAIL_TO = [
    address.strip()
    for address in os.environ.get("ALERT_EMAIL_TO", "").split(",")
    if address.strip()
]
ALERT_EMAIL_FROM = os.environ.get("ALERT_EMAIL_FROM") or None
SES_REGION = os.environ.get("SES_REGION") or os.environ["AWS_REGION"]

WORK_DIR = os.environ.get("WORK_DIR", "/tmp/mirror")
GIT_CREDENTIALS_FILE = "/tmp/.git-credentials"

def _parse_owners(raw: str) -> list[dict]:
    """GITHUB_OWNERS is JSON: [{"name": "octocat", "type": "user"}, ...]."""
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ValueError(f"GITHUB_OWNERS is not valid JSON: {error}") from error

    if not isinstance(parsed, list) or not parsed:
        raise ValueError("GITHUB_OWNERS must be a non-empty JSON array")

    owners = []
    for entry in parsed:
        if not isinstance(entry, dict) or not entry.get("name"):
            raise ValueError(f"GITHUB_OWNERS entry needs a 'name': {entry!r}")
        owner_type = str(entry.get("type", "user")).lower()
        if owner_type not in ("user", "org"):
            raise ValueError(
                f"GITHUB_OWNERS entry {entry['name']!r} has type {owner_type!r}; "
                "expected 'user' or 'org'"
            )
        owners.append({"name": str(entry["name"]), "type": owner_type})
    return owners


GITHUB_OWNERS = _parse_owners(os.environ["GITHUB_OWNERS"])

# CodeCommit accepts [\w\.-]{1,100}.
_UNSAFE_NAME_CHARS = re.compile(r"[^A-Za-z0-9._-]")
_MAX_CC_NAME = 100

codecommit = boto3.client("codecommit", region_name=CODECOMMIT_REGION)
secretsmanager = boto3.client("secretsmanager")
cloudwatch = boto3.client("cloudwatch")
lambda_client = boto3.client("lambda")

_token_cache: str | None = None
_viewer_cache: str | None = None
# Only built when a run actually fails, so a healthy run pays nothing for it.
_ses_client = None


# --------------------------------------------------------------------------
# GitHub
# --------------------------------------------------------------------------
def github_token() -> str:
    """Read the GitHub token from Secrets Manager, cached for the container."""
    global _token_cache
    if _token_cache is None:
        try:
            secret = secretsmanager.get_secret_value(SecretId=GITHUB_TOKEN_SECRET_ARN)
        except ClientError as error:
            if error.response["Error"]["Code"] != "ResourceNotFoundException":
                raise
            # Terraform creates the secret empty, so this is what a deployment
            # that has not had its token set yet looks like.
            raise RuntimeError(
                f"No value stored in {GITHUB_TOKEN_SECRET_ARN}. Set the GitHub "
                "token with: aws secretsmanager put-secret-value --secret-id "
                f"{GITHUB_TOKEN_SECRET_ARN} --secret-string <token>"
            ) from error

        raw = secret.get("SecretString", "").strip()
        if not raw:
            raise RuntimeError(
                f"The value stored in {GITHUB_TOKEN_SECRET_ARN} is empty. Set the "
                "GitHub token with: aws secretsmanager put-secret-value "
                f"--secret-id {GITHUB_TOKEN_SECRET_ARN} --secret-string <token>"
            )
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


def _viewer_login(token: str) -> str:
    """Login of the account the token belongs to, fetched once per container."""
    global _viewer_cache
    if _viewer_cache is None:
        viewer, _ = github_get(f"{GITHUB_API_URL}/user", token)
        _viewer_cache = str(viewer.get("login", ""))
    return _viewer_cache


def _listing_url(owner: dict, token: str) -> str:
    """Pick the listing endpoint that shows the most of this owner's repos."""
    name = owner["name"]
    if owner["type"] == "org":
        return f"{GITHUB_API_URL}/orgs/{name}/repos?per_page=100&type=all"

    if _viewer_login(token).lower() == name.lower():
        # /user/repos is the only listing that includes a user's private repos.
        return (
            f"{GITHUB_API_URL}/user/repos"
            f"?per_page=100&affiliation=owner&visibility={VISIBILITY}"
        )

    LOG.warning(
        "Token belongs to %s, not %s; only public repositories of %s are visible",
        _viewer_login(token), name, name,
    )
    return f"{GITHUB_API_URL}/users/{name}/repos?per_page=100&type=owner"


def list_github_repositories(token: str) -> list[dict]:
    """List every repository of every configured owner that passes the filters."""
    selected: dict[str, dict] = {}
    seen = 0

    for owner in GITHUB_OWNERS:
        url = _listing_url(owner, token)
        while url:
            page, headers = github_get(url, token)
            seen += len(page)
            for repo in page:
                # An owner listed twice, or a repo visible through two listings,
                # must still only be mirrored once.
                if _wanted(repo, owner["name"]):
                    selected[repo["full_name"]] = repo
            url = _next_link(headers.get("Link", ""))

        LOG.info("Listed %s (%s)", owner["name"], owner["type"])

    ordered = sorted(selected.values(), key=lambda repo: repo["full_name"].lower())
    LOG.info(
        "GitHub returned %s repositories across %s owners, %s selected for mirroring",
        seen, len(GITHUB_OWNERS), len(ordered),
    )
    return [
        {
            "full_name": repo["full_name"],
            "clone_url": repo["clone_url"],
            "description": repo.get("description") or "",
            "size_kb": repo.get("size") or 0,
            "default_branch": repo.get("default_branch") or "",
        }
        for repo in ordered
    ]


def _next_link(link_header: str) -> str | None:
    for part in link_header.split(","):
        section = part.split(";")
        if len(section) < 2:
            continue
        if 'rel="next"' in section[1].replace(" ", "").replace("'", '"'):
            return section[0].strip().strip("<>")
    return None


def _wanted(repo: dict, expected_owner: str) -> bool:
    name = repo["full_name"]
    owner = (repo.get("owner") or {}).get("login", "")
    if owner.lower() != expected_owner.lower():
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


def realign_default_branch(name: str, default_branch: str) -> None:
    """Point CodeCommit's default branch at GitHub's current one.

    A ``push --mirror`` refuses to delete whatever branch CodeCommit
    considers "current" (e.g. a merged dependabot branch left over from a
    prior run). Moving the default onto the branch GitHub still has avoids
    that rejection. Skipped when that branch has not reached CodeCommit yet
    (e.g. the very first push, or a just-renamed default branch).
    """
    if not default_branch:
        return
    try:
        codecommit.update_default_branch(
            repositoryName=name, defaultBranchName=default_branch
        )
    except ClientError as error:
        if error.response["Error"]["Code"] != "BranchDoesNotExistException":
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

        realign_default_branch(target, repo.get("default_branch", ""))

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
    """Entry point. Every way this can fail ends in an alert email."""
    report = {"counts": {}, "failures": []}
    try:
        return _run(event, context, report)
    except Exception as error:
        # The email must never replace the real failure: log it, send what we
        # can, then re-raise so Lambda still records the invocation as failed.
        _email_failure(error, report, context)
        raise


def _run(event, context, report: dict):
    event = event if isinstance(event, dict) else {}
    continuation = int(event.get("continuation", 0))

    token = github_token()
    configure_git(token)

    pending = event.get("pending")
    if pending is None:
        pending = list_github_repositories(token)

    counts = {"mirrored": 0, "empty": 0, "skipped": 0, "failed": 0, "deferred": 0}
    failures: list[str] = []
    remaining: list[dict] = []
    report["counts"] = counts
    report["failures"] = failures

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

    # Before the metrics: telemetry must never be able to cost us the handover
    # of work that has not been done yet.
    if remaining:
        counts["deferred"] = _continue(context, remaining, continuation)

    _publish_metrics(counts)
    summary = {**counts, "remaining": len(remaining), "continuation": continuation}
    LOG.info("Run summary: %s", json.dumps(summary))

    problems = []
    if failures:
        problems.append(
            f"{len(failures)} repositories failed to mirror: {', '.join(failures[:20])}"
        )
    if counts["deferred"]:
        # Not handed on and not mirrored: without raising, the run reports
        # success while those repositories were quietly skipped.
        problems.append(
            f"{counts['deferred']} repositories were not mirrored and could not be "
            "handed to a continuation"
        )
    if problems:
        raise RuntimeError("; ".join(problems))
    return summary


def _out_of_time(context) -> bool:
    if context is None:
        return False
    return context.get_remaining_time_in_millis() < TIME_BUDGET_SECONDS * 1000


# Lambda's asynchronous invocation payload limit, with room for the envelope.
MAX_ASYNC_PAYLOAD_BYTES = 250_000


def _continue(context, remaining: list[dict], continuation: int) -> int:
    """Hand the rest of the work to a fresh invocation.

    Returns the number of repositories that could NOT be handed on, so the
    caller can report them rather than let them disappear.
    """
    if continuation >= MAX_CONTINUATIONS:
        LOG.error(
            "Reached MAX_CONTINUATIONS (%s) with %s repositories left; they are not "
            "mirrored this run",
            MAX_CONTINUATIONS, len(remaining),
        )
        return len(remaining)

    payload = json.dumps({"pending": remaining, "continuation": continuation + 1}).encode()
    if len(payload) > MAX_ASYNC_PAYLOAD_BYTES:
        # Raising here would discard the batch already mirrored and fail
        # identically on every future run; reporting lets the operator act.
        LOG.error(
            "Continuation payload for %s repositories is %s bytes, over Lambda's "
            "asynchronous limit; narrow the run with include_pattern or "
            "exclude_pattern, or split it across deployments",
            len(remaining), len(payload),
        )
        return len(remaining)

    LOG.info("Out of time: continuing with %s repositories", len(remaining))
    try:
        lambda_client.invoke(
            FunctionName=context.invoked_function_arn,
            InvocationType="Event",
            Payload=payload,
        )
    except (ClientError, BotoCoreError):
        LOG.exception(
            "Could not invoke the continuation for %s repositories (%s bytes)",
            len(remaining), len(payload),
        )
        return len(remaining)

    return 0


def _ses():
    global _ses_client
    if _ses_client is None:
        _ses_client = boto3.client("sesv2", region_name=SES_REGION)
    return _ses_client


def _failure_email(error: Exception, report: dict, context) -> tuple[str, str]:
    """Subject and body for the alert. Never includes the GitHub token."""
    counts = report.get("counts") or {}
    failures = report.get("failures") or []

    if failures:
        subject = f"GitHub mirror: {len(failures)} repositories failed"
    else:
        # Listing GitHub failed, credentials are wrong, the run timed out --
        # something that stopped the run before individual repositories.
        subject = "GitHub mirror: run failed"

    lines = [subject, "", f"Error: {_scrub(str(error)) or error.__class__.__name__}", ""]

    if counts:
        lines += [
            "Counts:",
            *(f"  {name}: {value}" for name, value in counts.items()),
            "",
        ]

    if failures:
        lines += ["Repositories that failed:"]
        lines += [f"  {name}" for name in failures[:50]]
        if len(failures) > 50:
            lines.append(f"  ...and {len(failures) - 50} more")
        lines.append("")

    if context is not None:
        lines += [
            "Logs:",
            f"  group:   {getattr(context, 'log_group_name', '?')}",
            f"  stream:  {getattr(context, 'log_stream_name', '?')}",
            f"  request: {getattr(context, 'aws_request_id', '?')}",
        ]

    return subject, "\n".join(lines)


def _email_failure(error: Exception, report: dict, context) -> None:
    if not ALERT_EMAIL_TO or not ALERT_EMAIL_FROM:
        return

    subject, body = _failure_email(error, report, context)
    try:
        _ses().send_email(
            FromEmailAddress=ALERT_EMAIL_FROM,
            Destination={"ToAddresses": ALERT_EMAIL_TO},
            Content={
                "Simple": {
                    "Subject": {"Data": subject, "Charset": "UTF-8"},
                    "Body": {"Text": {"Data": body, "Charset": "UTF-8"}},
                }
            },
        )
        LOG.info("Sent failure alert to %s recipients", len(ALERT_EMAIL_TO))
    except Exception:  # noqa: BLE001 - a broken mailbox must not hide the run failure
        LOG.exception("Could not send the failure alert email")


def _publish_metrics(counts: dict) -> None:
    try:
        cloudwatch.put_metric_data(
            Namespace=METRIC_NAMESPACE,
            MetricData=[
                {"MetricName": name.capitalize(), "Value": value, "Unit": "Count"}
                for name, value in counts.items()
            ],
        )
    except (ClientError, BotoCoreError):
        LOG.exception("Could not publish metrics")
