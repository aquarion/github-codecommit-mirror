import importlib
import json

import pytest

handler = importlib.import_module("handler")


class TestCodeCommitName:
    def test_maps_owner_and_repo_with_the_prefix(self):
        assert handler.codecommit_name("octocat/hello-world") == "gh-octocat-hello-world"

    def test_replaces_characters_codecommit_rejects(self):
        assert handler.codecommit_name("octo cat/hello+world") == "gh-octo-cat-hello-world"

    def test_truncates_long_names_but_keeps_them_unique(self):
        long_one = "octocat/" + "a" * 200
        long_two = "octocat/" + "a" * 201

        first = handler.codecommit_name(long_one)
        second = handler.codecommit_name(long_two)

        assert len(first) <= 100
        assert first != second

    def test_names_stay_within_the_character_set_codecommit_allows(self):
        import re

        name = handler.codecommit_name("Some.Org/weird~name!")
        assert re.fullmatch(r"[A-Za-z0-9._-]{1,100}", name)


class TestRepositoryFilters:
    def repo(self, **overrides):
        base = {
            "full_name": "octocat/hello-world",
            "owner": {"login": "octocat"},
            "fork": False,
            "archived": False,
            "private": False,
        }
        base.update(overrides)
        return base

    def wanted(self, expected_owner="octocat", **overrides):
        return handler._wanted(self.repo(**overrides), expected_owner)

    def test_keeps_a_plain_repository(self):
        assert self.wanted()

    def test_drops_repositories_owned_by_someone_else(self):
        assert not self.wanted(full_name="other/thing", owner={"login": "other"})

    def test_owner_comparison_ignores_case(self):
        assert self.wanted(owner={"login": "OctoCat"})

    def test_matches_against_the_owner_being_listed(self):
        org_repo = self.repo(full_name="acme/thing", owner={"login": "acme"})

        assert handler._wanted(org_repo, "acme")
        assert not handler._wanted(org_repo, "octocat")

    def test_drops_forks_and_archived_repositories_by_default(self):
        assert not self.wanted(fork=True)
        assert not self.wanted(archived=True)

    def test_keeps_forks_when_configured(self, monkeypatch):
        monkeypatch.setattr(handler, "INCLUDE_FORKS", True)
        assert self.wanted(fork=True)

    @pytest.mark.parametrize(
        "visibility,private,expected",
        [
            ("all", True, True),
            ("all", False, True),
            ("private", True, True),
            ("private", False, False),
            ("public", True, False),
            ("public", False, True),
        ],
    )
    def test_visibility_filter(self, monkeypatch, visibility, private, expected):
        monkeypatch.setattr(handler, "VISIBILITY", visibility)
        assert self.wanted(private=private) is expected

    def test_include_and_exclude_patterns(self, monkeypatch):
        monkeypatch.setattr(handler, "INCLUDE_PATTERN", r"hello")
        assert self.wanted()
        assert not self.wanted(full_name="octocat/goodbye")

        monkeypatch.setattr(handler, "INCLUDE_PATTERN", None)
        monkeypatch.setattr(handler, "EXCLUDE_PATTERN", r"^octocat/hello")
        assert not self.wanted()


class TestPagination:
    def test_finds_the_next_page(self):
        link = (
            '<https://api.github.com/user/repos?page=2>; rel="next", '
            '<https://api.github.com/user/repos?page=9>; rel="last"'
        )
        assert handler._next_link(link) == "https://api.github.com/user/repos?page=2"

    def test_returns_none_on_the_last_page(self):
        link = '<https://api.github.com/user/repos?page=8>; rel="prev"'
        assert handler._next_link(link) is None

    def test_returns_none_without_a_link_header(self):
        assert handler._next_link("") is None


class TestSecretScrubbing:
    def test_removes_the_token_from_git_output(self, monkeypatch):
        monkeypatch.setattr(handler, "_token_cache", "ghp_supersecret")
        message = "fatal: could not read from https://ghp_supersecret@github.com/o/r"

        scrubbed = handler._scrub(message)

        assert "ghp_supersecret" not in scrubbed
        assert "***" in scrubbed

    def test_redacts_credentials_embedded_in_a_url(self):
        assert handler._redact_url("https://user:pass@github.com/o/r") == (
            "https://***@github.com/o/r"
        )


class TestTokenParsing:
    def test_accepts_a_plain_string_secret(self, monkeypatch):
        monkeypatch.setattr(handler, "_token_cache", None)
        monkeypatch.setattr(
            handler.secretsmanager,
            "get_secret_value",
            lambda **_: {"SecretString": "  ghp_plain  "},
        )
        assert handler.github_token() == "ghp_plain"

    def test_accepts_a_json_secret(self, monkeypatch):
        monkeypatch.setattr(handler, "_token_cache", None)
        monkeypatch.setattr(
            handler.secretsmanager,
            "get_secret_value",
            lambda **_: {"SecretString": '{"token": "ghp_json"}'},
        )
        assert handler.github_token() == "ghp_json"

    def test_rejects_a_json_secret_without_a_token_key(self, monkeypatch):
        monkeypatch.setattr(handler, "_token_cache", None)
        monkeypatch.setattr(
            handler.secretsmanager,
            "get_secret_value",
            lambda **_: {"SecretString": '{"username": "octocat"}'},
        )
        with pytest.raises(ValueError, match="no 'token' key"):
            handler.github_token()


class TestRetryDelay:
    def make_error(self, code, headers):
        import urllib.error

        return urllib.error.HTTPError("https://api.github.com", code, "", headers, None)

    def test_honours_retry_after(self):
        error = self.make_error(429, {"Retry-After": "12"})
        assert handler._retry_delay(error, 1) == 12

    def test_caps_the_wait_so_the_lambda_does_not_time_out(self):
        error = self.make_error(429, {"Retry-After": "3600"})
        assert handler._retry_delay(error, 1) == 60

    def test_waits_for_the_rate_limit_reset(self, monkeypatch):
        monkeypatch.setattr(handler.time, "time", lambda: 1_000)
        error = self.make_error(
            403, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1030"}
        )
        assert handler._retry_delay(error, 1) == 30

    def test_falls_back_to_exponential_backoff(self):
        error = self.make_error(500, {})
        assert handler._retry_delay(error, 3) == 8


class TestPruneRefs:
    """Uses a real repository on disk, because the point is the git behaviour."""

    def make_mirror(self, tmp_path):
        import subprocess

        source = tmp_path / "source"
        source.mkdir()
        run = lambda *args: subprocess.run(
            ["git", *args], cwd=source, check=True, capture_output=True
        )
        run("init", "--quiet", "--initial-branch=main")
        run("config", "user.email", "test@example.com")
        run("config", "user.name", "test")
        (source / "file.txt").write_text("hello\n")
        run("add", "file.txt")
        run("commit", "--quiet", "-m", "initial")
        run("tag", "v1.0.0")

        mirror = tmp_path / "mirror.git"
        subprocess.run(
            ["git", "clone", "--mirror", "--quiet", str(source), str(mirror)],
            check=True,
            capture_output=True,
        )
        # GitHub advertises refs a mirror clone happily copies but CodeCommit
        # rejects, so fake one here.
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=source, check=True,
            capture_output=True, text=True,
        ).stdout.strip()
        subprocess.run(
            ["git", "update-ref", "refs/pull/42/head", head],
            cwd=mirror, check=True, capture_output=True,
        )
        return mirror

    def refs(self, mirror):
        return set(
            handler.git(["for-each-ref", "--format=%(refname)"], cwd=str(mirror)).split()
        )

    def test_drops_pull_refs_and_keeps_branches_and_tags(self, tmp_path):
        mirror = self.make_mirror(tmp_path)
        assert "refs/pull/42/head" in self.refs(mirror)

        handler.prune_refs(str(mirror))

        assert self.refs(mirror) == {"refs/heads/main", "refs/tags/v1.0.0"}

    def test_is_a_no_op_when_there_is_nothing_to_drop(self, tmp_path):
        mirror = self.make_mirror(tmp_path)
        handler.prune_refs(str(mirror))
        before = self.refs(mirror)

        handler.prune_refs(str(mirror))

        assert self.refs(mirror) == before


class TestGitFailures:
    def test_raises_a_mirror_error_without_leaking_the_token(self, tmp_path, monkeypatch):
        monkeypatch.setattr(handler, "_token_cache", "ghp_supersecret")

        with pytest.raises(handler.MirrorError) as failure:
            handler.git(["clone", "https://ghp_supersecret@example.invalid/o/r.git",
                         str(tmp_path / "out")])

        assert "ghp_supersecret" not in str(failure.value)


class TestConfigureGit:
    def test_stores_a_credential_git_will_actually_match(self, tmp_path, monkeypatch):
        credentials = tmp_path / ".git-credentials"
        monkeypatch.setattr(handler, "GIT_CREDENTIALS_FILE", str(credentials))
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / ".gitconfig"))

        handler.configure_git("ghp_token")

        # git only reuses a stored credential when the username matches the one
        # it asks for, so the entry has to be user:password, not password alone.
        assert credentials.read_text() == "https://x-access-token:ghp_token@github.com\n"
        assert oct(credentials.stat().st_mode)[-3:] == "600"

    def test_percent_encodes_awkward_characters(self, tmp_path, monkeypatch):
        credentials = tmp_path / ".git-credentials"
        monkeypatch.setattr(handler, "GIT_CREDENTIALS_FILE", str(credentials))
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / ".gitconfig"))

        handler.configure_git("tok/en@with:chars")

        assert "tok%2Fen%40with%3Achars" in credentials.read_text()

    def test_derives_the_clone_host_from_the_api_url(self, tmp_path, monkeypatch):
        credentials = tmp_path / ".git-credentials"
        monkeypatch.setattr(handler, "GIT_CREDENTIALS_FILE", str(credentials))
        monkeypatch.setattr(handler, "GITHUB_API_URL", "https://ghe.example.com/api/v3")
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / ".gitconfig"))

        handler.configure_git("ghp_token")

        assert credentials.read_text().endswith("@ghe.example.com\n")


class TestOwnerParsing:
    def test_reads_a_list_of_owners(self):
        owners = handler._parse_owners(
            '[{"name": "octocat"}, {"name": "acme", "type": "org"}]'
        )
        assert owners == [
            {"name": "octocat", "type": "user"},
            {"name": "acme", "type": "org"},
        ]

    def test_type_defaults_to_user(self):
        assert handler._parse_owners('[{"name": "octocat"}]')[0]["type"] == "user"

    @pytest.mark.parametrize(
        "raw,message",
        [
            ("not json", "not valid JSON"),
            ("[]", "non-empty JSON array"),
            ('{"name": "octocat"}', "non-empty JSON array"),
            ('[{"type": "org"}]', "needs a 'name'"),
            ('[{"name": "acme", "type": "team"}]', "expected 'user' or 'org'"),
        ],
    )
    def test_rejects_bad_configuration(self, raw, message):
        with pytest.raises(ValueError, match=message):
            handler._parse_owners(raw)


class TestListingAcrossOwners:
    """A personal account and an organisation in one run."""

    def owners(self, monkeypatch):
        monkeypatch.setattr(
            handler,
            "GITHUB_OWNERS",
            [{"name": "octocat", "type": "user"}, {"name": "acme", "type": "org"}],
        )
        monkeypatch.setattr(handler, "_viewer_cache", "octocat")

    def repo(self, full_name, **overrides):
        owner, name = full_name.split("/")
        base = {
            "full_name": full_name,
            "name": name,
            "owner": {"login": owner},
            "clone_url": f"https://github.com/{full_name}.git",
            "description": "",
            "size": 10,
            "fork": False,
            "archived": False,
            "private": False,
        }
        base.update(overrides)
        return base

    def fake_github(self, pages):
        """pages: {url_fragment: (payload, link_header)}"""
        def github_get(url, token, attempts=5):
            for fragment, (payload, link) in pages.items():
                if fragment in url:
                    return payload, {"Link": link}
            raise AssertionError(f"unexpected URL {url}")
        return github_get

    def test_mirrors_repositories_from_both_owners(self, monkeypatch):
        self.owners(monkeypatch)
        monkeypatch.setattr(
            handler,
            "github_get",
            self.fake_github({
                "/user/repos": ([self.repo("octocat/personal")], ""),
                "/orgs/acme/repos": ([self.repo("acme/service")], ""),
            }),
        )

        names = [repo["full_name"] for repo in handler.list_github_repositories("t")]

        assert names == ["acme/service", "octocat/personal"]

    def test_carries_the_default_branch_through(self, monkeypatch):
        self.owners(monkeypatch)
        monkeypatch.setattr(
            handler,
            "github_get",
            self.fake_github({
                "/user/repos": (
                    [self.repo("octocat/personal", default_branch="trunk")], "",
                ),
                "/orgs/acme/repos": ([], ""),
            }),
        )

        [repo] = handler.list_github_repositories("t")

        assert repo["default_branch"] == "trunk"

    def test_uses_the_org_endpoint_for_organisations(self, monkeypatch):
        self.owners(monkeypatch)
        requested = []

        def github_get(url, token, attempts=5):
            requested.append(url)
            return [], {"Link": ""}

        monkeypatch.setattr(handler, "github_get", github_get)
        handler.list_github_repositories("t")

        assert any("/user/repos" in url for url in requested)
        assert any("/orgs/acme/repos" in url for url in requested)

    def test_follows_pagination_per_owner(self, monkeypatch):
        self.owners(monkeypatch)
        monkeypatch.setattr(
            handler,
            "github_get",
            self.fake_github({
                "/user/repos?page=2": ([self.repo("octocat/second")], ""),
                "/user/repos": (
                    [self.repo("octocat/first")],
                    '<https://api.github.com/user/repos?page=2>; rel="next"',
                ),
                "/orgs/acme/repos": ([], ""),
            }),
        )

        names = [repo["full_name"] for repo in handler.list_github_repositories("t")]

        assert names == ["octocat/first", "octocat/second"]

    def test_a_repository_visible_twice_is_mirrored_once(self, monkeypatch):
        self.owners(monkeypatch)
        shared = self.repo("acme/service")
        monkeypatch.setattr(
            handler,
            "github_get",
            self.fake_github({
                # An org repo the user also has owner affiliation on.
                "/user/repos": ([shared], ""),
                "/orgs/acme/repos": ([shared], ""),
            }),
        )

        names = [repo["full_name"] for repo in handler.list_github_repositories("t")]

        assert names == ["acme/service"]

    def test_each_owner_only_keeps_its_own_repositories(self, monkeypatch):
        self.owners(monkeypatch)
        monkeypatch.setattr(
            handler,
            "github_get",
            self.fake_github({
                # A repo from an org the user merely belongs to.
                "/user/repos": (
                    [self.repo("octocat/mine"), self.repo("someone-else/theirs")],
                    "",
                ),
                "/orgs/acme/repos": ([], ""),
            }),
        )

        names = [repo["full_name"] for repo in handler.list_github_repositories("t")]

        assert names == ["octocat/mine"]

    def test_owners_get_distinct_codecommit_names(self):
        assert handler.codecommit_name("octocat/api") != handler.codecommit_name(
            "acme/api"
        )


class FakeContext:
    aws_request_id = "req-123"
    log_group_name = "/aws/lambda/mirror"
    log_stream_name = "2026/08/28/[$LATEST]abc"

    def __init__(self, remaining_ms=900_000):
        self._remaining = remaining_ms

    def get_remaining_time_in_millis(self):
        return self._remaining


class TestFailureEmail:
    def test_names_the_repositories_that_failed(self):
        report = {"counts": {"mirrored": 3, "failed": 2}, "failures": ["o/a", "o/b"]}

        subject, body = handler._failure_email(
            RuntimeError("2 repositories failed to mirror"), report, FakeContext()
        )

        assert subject == "GitHub mirror: 2 repositories failed"
        assert "o/a" in body and "o/b" in body
        assert "mirrored: 3" in body

    def test_reports_a_run_that_died_before_any_repository(self):
        subject, body = handler._failure_email(
            RuntimeError("boom"), {"counts": {}, "failures": []}, FakeContext()
        )

        assert subject == "GitHub mirror: run failed"
        assert "boom" in body

    def test_includes_where_to_find_the_logs(self):
        _, body = handler._failure_email(
            RuntimeError("boom"), {"counts": {}, "failures": []}, FakeContext()
        )

        assert "/aws/lambda/mirror" in body
        assert "req-123" in body

    def test_truncates_a_very_long_failure_list(self):
        failures = [f"o/repo{n}" for n in range(70)]
        _, body = handler._failure_email(
            RuntimeError("boom"), {"counts": {}, "failures": failures}, FakeContext()
        )

        assert "...and 20 more" in body
        assert "o/repo69" not in body

    def test_never_carries_the_token(self, monkeypatch):
        monkeypatch.setattr(handler, "_token_cache", "ghp_supersecret")

        _, body = handler._failure_email(
            RuntimeError("failed: https://ghp_supersecret@github.com/o/r"),
            {"counts": {}, "failures": []},
            FakeContext(),
        )

        assert "ghp_supersecret" not in body


class TestFailureAlerting:
    def test_sends_to_the_configured_recipients(self, monkeypatch):
        sent = {}
        monkeypatch.setattr(handler, "ALERT_EMAIL_TO", ["ops@example.com"])
        monkeypatch.setattr(handler, "ALERT_EMAIL_FROM", "mirror@example.com")
        monkeypatch.setattr(
            handler, "_ses", lambda: type("S", (), {"send_email": lambda _self=None, **kw: sent.update(kw)})()
        )

        handler._email_failure(RuntimeError("boom"), {"counts": {}, "failures": []}, None)

        assert sent["FromEmailAddress"] == "mirror@example.com"
        assert sent["Destination"]["ToAddresses"] == ["ops@example.com"]

    def test_stays_quiet_when_no_recipients_are_configured(self, monkeypatch):
        monkeypatch.setattr(handler, "ALERT_EMAIL_TO", [])
        monkeypatch.setattr(
            handler, "_ses", lambda: pytest.fail("should not build an SES client")
        )

        handler._email_failure(RuntimeError("boom"), {}, None)

    def test_a_broken_mailbox_does_not_hide_the_run_failure(self, monkeypatch):
        monkeypatch.setattr(handler, "ALERT_EMAIL_TO", ["ops@example.com"])
        monkeypatch.setattr(handler, "ALERT_EMAIL_FROM", "mirror@example.com")

        def exploding():
            raise RuntimeError("SES is down")

        monkeypatch.setattr(handler, "_ses", exploding)

        # Must return rather than propagate: the caller re-raises the real error.
        handler._email_failure(RuntimeError("boom"), {}, None)

    def test_the_handler_emails_then_re_raises(self, monkeypatch):
        alerts = []
        monkeypatch.setattr(
            handler, "_email_failure", lambda error, report, ctx: alerts.append(error)
        )
        monkeypatch.setattr(handler, "github_token", lambda: "ghp_token")
        monkeypatch.setattr(handler, "configure_git", lambda token: None)
        monkeypatch.setattr(
            handler, "list_github_repositories", lambda token: (_ for _ in ()).throw(
                RuntimeError("GitHub is unreachable")
            )
        )

        with pytest.raises(RuntimeError, match="GitHub is unreachable"):
            handler.lambda_handler({}, FakeContext())

        assert len(alerts) == 1

    def test_a_failed_repository_reaches_the_email_with_its_name(self, monkeypatch):
        alerts = []
        monkeypatch.setattr(
            handler, "_email_failure", lambda error, report, ctx: alerts.append(report)
        )
        monkeypatch.setattr(handler, "github_token", lambda: "ghp_token")
        monkeypatch.setattr(handler, "configure_git", lambda token: None)
        monkeypatch.setattr(handler, "_publish_metrics", lambda counts: None)
        monkeypatch.setattr(
            handler,
            "mirror_repository",
            lambda repo: (_ for _ in ()).throw(handler.MirrorError("clone failed")),
        )

        with pytest.raises(RuntimeError):
            handler.lambda_handler(
                {"pending": [{"full_name": "octocat/broken", "clone_url": "x"}]},
                FakeContext(),
            )

        assert alerts[0]["failures"] == ["octocat/broken"]
        assert alerts[0]["counts"]["failed"] == 1

    def test_a_clean_run_sends_nothing(self, monkeypatch):
        monkeypatch.setattr(
            handler, "_email_failure", lambda *a: pytest.fail("should not alert")
        )
        monkeypatch.setattr(handler, "github_token", lambda: "ghp_token")
        monkeypatch.setattr(handler, "configure_git", lambda token: None)
        monkeypatch.setattr(handler, "_publish_metrics", lambda counts: None)
        monkeypatch.setattr(handler, "mirror_repository", lambda repo: "mirrored")

        summary = handler.lambda_handler(
            {"pending": [{"full_name": "octocat/fine", "clone_url": "x"}]},
            FakeContext(),
        )

        assert summary["mirrored"] == 1


class TestMissingSecretValue:
    def test_says_how_to_set_a_token_that_was_never_written(self, monkeypatch):
        monkeypatch.setattr(handler, "_token_cache", None)

        def missing(**_):
            raise handler.ClientError(
                {"Error": {"Code": "ResourceNotFoundException", "Message": "nope"}},
                "GetSecretValue",
            )

        monkeypatch.setattr(handler.secretsmanager, "get_secret_value", missing)

        with pytest.raises(RuntimeError, match="put-secret-value"):
            handler.github_token()

    def test_other_secrets_manager_errors_are_not_swallowed(self, monkeypatch):
        monkeypatch.setattr(handler, "_token_cache", None)

        def denied(**_):
            raise handler.ClientError(
                {"Error": {"Code": "AccessDeniedException", "Message": "no"}},
                "GetSecretValue",
            )

        monkeypatch.setattr(handler.secretsmanager, "get_secret_value", denied)

        with pytest.raises(handler.ClientError):
            handler.github_token()


class TestContinuation:
    """The self-invoking loop: the riskiest path, and the one that bills."""

    def repos(self, count):
        return [
            {"full_name": f"octocat/r{n}", "clone_url": f"https://x/{n}.git", "size_kb": 1}
            for n in range(count)
        ]

    def run(self, monkeypatch, pending, remaining_ms, continuation=0, invoke=None):
        calls = []
        monkeypatch.setattr(handler, "github_token", lambda: "ghp_token")
        monkeypatch.setattr(handler, "configure_git", lambda token: None)
        monkeypatch.setattr(handler, "_publish_metrics", lambda counts: None)
        monkeypatch.setattr(handler, "mirror_repository", lambda repo: "mirrored")
        monkeypatch.setattr(
            handler.lambda_client,
            "invoke",
            invoke or (lambda **kw: calls.append(kw)),
        )
        context = FakeContext(remaining_ms)
        context.invoked_function_arn = "arn:aws:lambda:eu-west-1:1:function:mirror"
        event = {"pending": pending, "continuation": continuation}
        return context, calls, event

    def test_always_completes_one_repository_before_deferring(self, monkeypatch):
        """The property README claims: a run cannot loop without progressing."""
        mirrored = []
        context, calls, event = self.run(monkeypatch, self.repos(3), remaining_ms=0)
        monkeypatch.setattr(
            handler,
            "mirror_repository",
            lambda repo: (mirrored.append(repo["full_name"]), "mirrored")[1],
        )

        summary = handler.lambda_handler(event, context)

        # Out of time from the very first check, yet one repository is done.
        assert mirrored == ["octocat/r0"]
        assert summary["mirrored"] == 1
        assert summary["remaining"] == 2

    def test_forwards_exactly_the_repositories_not_reached(self, monkeypatch):
        context, calls, event = self.run(monkeypatch, self.repos(3), remaining_ms=0)

        handler.lambda_handler(event, context)

        payload = json.loads(calls[0]["Payload"])
        assert [repo["full_name"] for repo in payload["pending"]] == [
            "octocat/r1",
            "octocat/r2",
        ]

    def test_increments_the_continuation_counter(self, monkeypatch):
        context, calls, event = self.run(
            monkeypatch, self.repos(2), remaining_ms=0, continuation=3
        )

        handler.lambda_handler(event, context)

        assert json.loads(calls[0]["Payload"])["continuation"] == 4
        assert calls[0]["InvocationType"] == "Event"

    def test_stops_invoking_at_the_continuation_cap(self, monkeypatch):
        monkeypatch.setattr(handler, "MAX_CONTINUATIONS", 3)
        context, calls, event = self.run(
            monkeypatch, self.repos(2), remaining_ms=0, continuation=3
        )

        with pytest.raises(RuntimeError, match="could not be handed to a continuation"):
            handler.lambda_handler(event, context)

        assert calls == []

    def test_repositories_dropped_at_the_cap_are_reported_not_silent(self, monkeypatch):
        monkeypatch.setattr(handler, "MAX_CONTINUATIONS", 1)
        alerts = []
        monkeypatch.setattr(
            handler, "_email_failure", lambda error, report, ctx: alerts.append(report)
        )
        context, calls, event = self.run(
            monkeypatch, self.repos(4), remaining_ms=0, continuation=1
        )

        with pytest.raises(RuntimeError):
            handler.lambda_handler(event, context)

        assert alerts[0]["counts"]["deferred"] == 3

    def test_an_oversized_payload_is_reported_rather_than_raised_raw(self, monkeypatch):
        monkeypatch.setattr(handler, "MAX_ASYNC_PAYLOAD_BYTES", 200)
        context, calls, event = self.run(monkeypatch, self.repos(50), remaining_ms=0)

        with pytest.raises(RuntimeError, match="could not be handed to a continuation"):
            handler.lambda_handler(event, context)

        assert calls == []

    def test_a_failed_invoke_is_reported_rather_than_lost(self, monkeypatch):
        def exploding(**_):
            raise handler.ClientError(
                {"Error": {"Code": "TooManyRequestsException", "Message": "no"}}, "Invoke"
            )

        context, _, event = self.run(
            monkeypatch, self.repos(3), remaining_ms=0, invoke=exploding
        )

        with pytest.raises(RuntimeError, match="could not be handed to a continuation"):
            handler.lambda_handler(event, context)

    def test_a_full_run_does_not_invoke_a_continuation(self, monkeypatch):
        context, calls, event = self.run(monkeypatch, self.repos(3), remaining_ms=900_000)

        summary = handler.lambda_handler(event, context)

        assert calls == []
        assert summary["mirrored"] == 3 and summary["deferred"] == 0


class TestMetricsCannotCostWork:
    """Telemetry must never be able to lose the handover of unfinished work."""

    def run(self, monkeypatch, put_metric_data):
        calls = []
        monkeypatch.setattr(handler, "github_token", lambda: "ghp_token")
        monkeypatch.setattr(handler, "configure_git", lambda token: None)
        monkeypatch.setattr(handler, "mirror_repository", lambda repo: "mirrored")
        monkeypatch.setattr(handler.lambda_client, "invoke", lambda **kw: calls.append(kw))
        monkeypatch.setattr(handler.cloudwatch, "put_metric_data", put_metric_data)

        context = FakeContext(0)
        context.invoked_function_arn = "arn:aws:lambda:eu-west-1:1:function:mirror"
        pending = [
            {"full_name": f"octocat/r{n}", "clone_url": "x", "size_kb": 1} for n in range(2)
        ]
        return context, calls, {"pending": pending}

    def test_a_botocore_failure_publishing_metrics_is_swallowed(self, monkeypatch):
        def broken(**_):
            raise handler.BotoCoreError()

        context, calls, event = self.run(monkeypatch, broken)

        summary = handler.lambda_handler(event, context)

        assert len(calls) == 1
        assert summary["deferred"] == 0

    def test_an_unexpected_metrics_error_cannot_cost_the_handover(self, monkeypatch):
        """Proves the ordering, not the except clause.

        An error _publish_metrics does not catch still propagates, so the only
        thing keeping the unfinished repositories alive is that the handover
        already happened by then.
        """

        def broken(**_):
            raise RuntimeError("something no one anticipated")

        context, calls, event = self.run(monkeypatch, broken)

        with pytest.raises(RuntimeError, match="no one anticipated"):
            handler.lambda_handler(event, context)

        assert len(calls) == 1, "the continuation must be invoked before metrics"


class TestEnsureCodeCommitRepository:
    class FakeCodeCommit:
        def __init__(self, exists=False, create_error=None):
            self.exists = exists
            self.create_error = create_error
            self.created = []

        def get_repository(self, repositoryName):
            if self.exists:
                return {"repositoryMetadata": {"repositoryName": repositoryName}}
            raise handler.ClientError(
                {"Error": {"Code": "RepositoryDoesNotExistException", "Message": "no"}},
                "GetRepository",
            )

        def create_repository(self, **kwargs):
            if self.create_error:
                raise self.create_error
            self.created.append(kwargs)

    def test_creates_a_repository_that_does_not_exist(self, monkeypatch):
        fake = self.FakeCodeCommit(exists=False)
        monkeypatch.setattr(handler, "codecommit", fake)

        handler.ensure_codecommit_repository("gh-o-r", "A thing", "o/r")

        assert fake.created[0]["repositoryName"] == "gh-o-r"
        assert fake.created[0]["tags"]["SourceRepository"] == "o/r"
        assert "Mirror of o/r" in fake.created[0]["repositoryDescription"]

    def test_does_not_recreate_an_existing_repository(self, monkeypatch):
        fake = self.FakeCodeCommit(exists=True)
        monkeypatch.setattr(handler, "codecommit", fake)

        handler.ensure_codecommit_repository("gh-o-r", "", "o/r")

        assert fake.created == []

    def test_a_concurrent_run_winning_the_race_is_not_an_error(self, monkeypatch):
        race = handler.ClientError(
            {"Error": {"Code": "RepositoryNameExistsException", "Message": "exists"}},
            "CreateRepository",
        )
        monkeypatch.setattr(
            handler, "codecommit", self.FakeCodeCommit(exists=False, create_error=race)
        )

        handler.ensure_codecommit_repository("gh-o-r", "", "o/r")

    def test_a_real_create_failure_propagates(self, monkeypatch):
        denied = handler.ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "no"}},
            "CreateRepository",
        )
        monkeypatch.setattr(
            handler, "codecommit", self.FakeCodeCommit(exists=False, create_error=denied)
        )

        with pytest.raises(handler.ClientError):
            handler.ensure_codecommit_repository("gh-o-r", "", "o/r")

    def test_a_description_longer_than_codecommit_allows_is_truncated(self, monkeypatch):
        fake = self.FakeCodeCommit(exists=False)
        monkeypatch.setattr(handler, "codecommit", fake)

        handler.ensure_codecommit_repository("gh-o-r", "x" * 2000, "o/r")

        assert len(fake.created[0]["repositoryDescription"]) <= 1000


class TestRealignDefaultBranch:
    class FakeCodeCommit:
        def __init__(self, missing_branch=False):
            self.missing_branch = missing_branch
            self.calls = []

        def update_default_branch(self, **kwargs):
            self.calls.append(kwargs)
            if self.missing_branch:
                raise handler.ClientError(
                    {"Error": {"Code": "BranchDoesNotExistException", "Message": "no"}},
                    "UpdateDefaultBranch",
                )

    def test_points_codecommit_at_githubs_current_default_branch(self, monkeypatch):
        fake = self.FakeCodeCommit()
        monkeypatch.setattr(handler, "codecommit", fake)

        handler.realign_default_branch("gh-o-r", "main")

        assert fake.calls == [{"repositoryName": "gh-o-r", "defaultBranchName": "main"}]

    def test_does_nothing_when_github_reported_no_default_branch(self, monkeypatch):
        fake = self.FakeCodeCommit()
        monkeypatch.setattr(handler, "codecommit", fake)

        handler.realign_default_branch("gh-o-r", "")

        assert fake.calls == []

    def test_a_branch_not_yet_mirrored_is_not_an_error(self, monkeypatch):
        monkeypatch.setattr(
            handler, "codecommit", self.FakeCodeCommit(missing_branch=True)
        )

        handler.realign_default_branch("gh-o-r", "main")

    def test_other_codecommit_errors_propagate(self, monkeypatch):
        class Denying(self.FakeCodeCommit):
            def update_default_branch(self, **kwargs):
                raise handler.ClientError(
                    {"Error": {"Code": "AccessDeniedException", "Message": "no"}},
                    "UpdateDefaultBranch",
                )

        monkeypatch.setattr(handler, "codecommit", Denying())

        with pytest.raises(handler.ClientError):
            handler.realign_default_branch("gh-o-r", "main")


class TestMirrorRepository:
    """Runs real git against real repositories on disk."""

    def source_repo(self, tmp_path, empty=False):
        import subprocess

        source = tmp_path / "source"
        source.mkdir()
        run = lambda *args: subprocess.run(
            ["git", *args], cwd=source, check=True, capture_output=True
        )
        run("init", "--quiet", "--initial-branch=main")
        run("config", "user.email", "test@example.com")
        run("config", "user.name", "test")
        if not empty:
            (source / "file.txt").write_text("hello\n")
            run("add", "file.txt")
            run("commit", "--quiet", "-m", "initial")
            run("tag", "v1.0.0")
        return source

    def setup(self, monkeypatch, tmp_path, source, size_kb=1):
        monkeypatch.setattr(handler, "WORK_DIR", str(tmp_path / "work"))
        monkeypatch.setattr(
            handler, "ensure_codecommit_repository", lambda *a, **k: None
        )
        return {
            "full_name": "octocat/thing",
            "clone_url": str(source),
            "description": "",
            "size_kb": size_kb,
        }

    def intercept_push(self, monkeypatch):
        """Let every git command run for real except the push to CodeCommit."""
        pushes = []
        real_git = handler.git

        def fake_git(args, cwd=None, timeout=900, stdin=None):
            if args and args[0] == "push":
                pushes.append((args, cwd))
                return ""
            return real_git(args, cwd=cwd, timeout=timeout, stdin=stdin)

        monkeypatch.setattr(handler, "git", fake_git)
        return pushes

    def test_clones_and_pushes_to_the_codecommit_remote(self, monkeypatch, tmp_path):
        repo = self.setup(monkeypatch, tmp_path, self.source_repo(tmp_path))
        pushes = self.intercept_push(monkeypatch)

        assert handler.mirror_repository(repo) == "mirrored"

        args, _ = pushes[0]
        assert "--mirror" in args
        assert args[-1] == f"codecommit::{handler.CODECOMMIT_REGION}://gh-octocat-thing"

    def test_removes_the_working_copy_afterwards(self, monkeypatch, tmp_path):
        repo = self.setup(monkeypatch, tmp_path, self.source_repo(tmp_path))
        self.intercept_push(monkeypatch)

        handler.mirror_repository(repo)

        assert not (tmp_path / "work" / "gh-octocat-thing.git").exists()

    def test_removes_the_working_copy_even_when_the_push_fails(
        self, monkeypatch, tmp_path
    ):
        repo = self.setup(monkeypatch, tmp_path, self.source_repo(tmp_path))
        real_git = handler.git

        def fake_git(args, cwd=None, timeout=900, stdin=None):
            if args and args[0] == "push":
                raise handler.MirrorError("push rejected")
            return real_git(args, cwd=cwd, timeout=timeout, stdin=stdin)

        monkeypatch.setattr(handler, "git", fake_git)

        with pytest.raises(handler.MirrorError):
            handler.mirror_repository(repo)

        assert not (tmp_path / "work" / "gh-octocat-thing.git").exists()

    def test_an_empty_repository_is_created_but_never_pushed(self, monkeypatch, tmp_path):
        repo = self.setup(monkeypatch, tmp_path, self.source_repo(tmp_path, empty=True))
        pushes = self.intercept_push(monkeypatch)

        assert handler.mirror_repository(repo) == "empty"
        assert pushes == []

    def test_skips_a_repository_over_the_size_limit_without_cloning(
        self, monkeypatch, tmp_path
    ):
        repo = self.setup(
            monkeypatch, tmp_path, self.source_repo(tmp_path), size_kb=5 * 1024 * 1024
        )
        monkeypatch.setattr(handler, "MAX_REPO_SIZE_MB", 10)
        monkeypatch.setattr(
            handler, "git", lambda *a, **k: pytest.fail("should not run git")
        )
        monkeypatch.setattr(
            handler,
            "ensure_codecommit_repository",
            lambda *a, **k: pytest.fail("should not touch CodeCommit"),
        )

        assert handler.mirror_repository(repo) == "skipped"

    def test_a_stale_working_copy_does_not_break_the_next_run(
        self, monkeypatch, tmp_path
    ):
        repo = self.setup(monkeypatch, tmp_path, self.source_repo(tmp_path))
        stale = tmp_path / "work" / "gh-octocat-thing.git"
        stale.mkdir(parents=True)
        (stale / "junk").write_text("left over from a killed run")
        self.intercept_push(monkeypatch)

        assert handler.mirror_repository(repo) == "mirrored"

    def test_realigns_the_default_branch_before_pushing(self, monkeypatch, tmp_path):
        repo = self.setup(monkeypatch, tmp_path, self.source_repo(tmp_path))
        repo["default_branch"] = "main"
        pushes = self.intercept_push(monkeypatch)
        calls = []
        monkeypatch.setattr(
            handler, "realign_default_branch", lambda *a: calls.append(a)
        )

        assert handler.mirror_repository(repo) == "mirrored"

        assert calls == [("gh-octocat-thing", "main")]
        assert calls[0][0] and pushes, "realignment must happen before the push"

    def test_an_empty_repository_is_never_realigned(self, monkeypatch, tmp_path):
        repo = self.setup(monkeypatch, tmp_path, self.source_repo(tmp_path, empty=True))
        repo["default_branch"] = "main"
        self.intercept_push(monkeypatch)
        calls = []
        monkeypatch.setattr(
            handler, "realign_default_branch", lambda *a: calls.append(a)
        )

        assert handler.mirror_repository(repo) == "empty"
        assert calls == []


class TestGitHubRetries:
    """The retry loop itself, not just the delay arithmetic."""

    def response(self, payload):
        import io
        import json as _json

        class FakeResponse(io.BytesIO):
            headers = {"Link": ""}

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        return FakeResponse(_json.dumps(payload).encode())

    def error(self, code, headers=None):
        import urllib.error

        return urllib.error.HTTPError(
            "https://api.github.com/user", code, "", headers or {}, None
        )

    def test_retries_a_rate_limited_request_and_succeeds(self, monkeypatch):
        slept = []
        monkeypatch.setattr(handler.time, "sleep", slept.append)
        attempts = []

        def urlopen(request, timeout=30):
            attempts.append(request)
            if len(attempts) == 1:
                raise self.error(403, {"Retry-After": "1"})
            return self.response({"login": "octocat"})

        monkeypatch.setattr(handler.urllib.request, "urlopen", urlopen)

        payload, _ = handler.github_get("https://api.github.com/user", "ghp_token")

        assert payload == {"login": "octocat"}
        assert len(attempts) == 2 and slept == [1]

    def test_retries_a_server_error(self, monkeypatch):
        monkeypatch.setattr(handler.time, "sleep", lambda seconds: None)
        attempts = []

        def urlopen(request, timeout=30):
            attempts.append(request)
            if len(attempts) < 3:
                raise self.error(502)
            return self.response({"ok": True})

        monkeypatch.setattr(handler.urllib.request, "urlopen", urlopen)

        handler.github_get("https://api.github.com/user", "ghp_token")

        assert len(attempts) == 3

    def test_gives_up_after_the_last_attempt(self, monkeypatch):
        import urllib.error

        monkeypatch.setattr(handler.time, "sleep", lambda seconds: None)
        monkeypatch.setattr(
            handler.urllib.request,
            "urlopen",
            lambda request, timeout=30: (_ for _ in ()).throw(self.error(500)),
        )

        with pytest.raises(urllib.error.HTTPError):
            handler.github_get("https://api.github.com/user", "ghp_token", attempts=3)

    def test_does_not_retry_a_bad_token(self, monkeypatch):
        import urllib.error

        attempts = []

        def urlopen(request, timeout=30):
            attempts.append(request)
            raise self.error(401)

        monkeypatch.setattr(handler.urllib.request, "urlopen", urlopen)

        with pytest.raises(urllib.error.HTTPError):
            handler.github_get("https://api.github.com/user", "ghp_token")

        assert len(attempts) == 1

    def test_sends_the_token_as_a_bearer_header(self, monkeypatch):
        captured = []

        def urlopen(request, timeout=30):
            captured.append(request)
            return self.response({})

        monkeypatch.setattr(handler.urllib.request, "urlopen", urlopen)

        handler.github_get("https://api.github.com/user", "ghp_token")

        assert captured[0].get_header("Authorization") == "Bearer ghp_token"
