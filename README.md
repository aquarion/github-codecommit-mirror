# github-codecommit-mirror

Terraform for an EventBridge-scheduled Lambda that mirrors every GitHub
repository you own into a private AWS CodeCommit repository.

```
EventBridge (rate(1 day))
        │
        ▼
   Lambda (container image: python 3.12 + git + git-remote-codecommit)
        │  1. read GitHub token from Secrets Manager
        │  2. list repositories via the GitHub API
        │  3. for each repository:
        │       git clone --mirror  https://github.com/<owner>/<repo>.git
        │       git push  --mirror  codecommit::<region>://gh-<owner>-<repo>
        ▼
   CodeCommit (one private repository per GitHub repository)
```

## What you need

* An AWS account with credentials that can create IAM roles, Lambda functions,
  ECR repositories, CodeCommit repositories and EventBridge rules.
* Terraform >= 1.10, for the S3 backend's native locking.
* Docker, to build the Lambda container image. The function needs the `git`
  binary, which is not in the zip runtimes, so it ships as an image. If you would
  rather build in CI, set `build_and_push_image = false` and pass `image_tag`.
* A GitHub token that can read every account you are mirroring: a classic PAT
  with `repo` (plus `read:org` for organisations), or a fine-grained token with
  read-only **Contents** and **Metadata**. Read-only is enough — nothing is ever
  written back to GitHub. See
  [Mirroring several accounts](#mirroring-several-accounts) if one token cannot
  reach them all.

## Deploy

State lives in S3, so the bucket has to exist first. Once per account:

```shell
cd bootstrap
terraform init
terraform apply -var bucket_name=acme-terraform-state -var aws_region=eu-west-1
terraform output -raw backend_hcl > ../backend.hcl
cd ..
```

That creates a versioned, encrypted, private bucket and prints the backend
configuration for it. Then the stack itself:

```shell
cp terraform.tfvars.example terraform.tfvars
$EDITOR terraform.tfvars

terraform init -backend-config=backend.hcl
terraform apply
```

`backend.hcl` and `terraform.tfvars` are both gitignored. If you already have a
state bucket, skip the bootstrap step and write `backend.hcl` by hand from
[`backend.hcl.example`](backend.hcl.example).

### Set the token

Terraform creates the secret **empty** — it never holds the token, so the token
never passes through Terraform and never lands in state. Write it once, out of
band:

```shell
aws secretsmanager put-secret-value \
  --secret-id "$(terraform output -raw github_token_secret_arn)" \
  --secret-string 'ghp_yourtokenhere'
```

The value can be a bare token or JSON like `{"token": "ghp_..."}`. Until it is
set, runs fail with a message telling you to run exactly this.

Rotating later is the same command; the function picks the new value up on its
next cold start.

To point at a secret you already manage instead, set
`create_github_token_secret = false` and
`github_token_secret_arn = "arn:aws:secretsmanager:..."`.

### Run it without waiting for the schedule

```shell
aws lambda invoke \
  --function-name "$(terraform output -raw lambda_function_name)" \
  --payload '{}' --cli-binary-format raw-in-base64-out /dev/stdout

aws logs tail "$(terraform output -raw log_group_name)" --follow
```

## State

The backend is S3 with `use_lockfile = true`, which is Terraform's native S3
locking — there is no DynamoDB table to create or pay for. That needs Terraform
1.10 or newer, which is why `required_version` asks for it.

The `bootstrap/` module keeps its own state locally rather than in the bucket it
creates, because that would be circular. It creates one bucket; if you lose that
state, `terraform import` recovers it, or you can simply leave the bucket alone.

Nothing sensitive is in the mirror stack's state: the GitHub token is written
directly to Secrets Manager and Terraform manages no secret version, so the
value is never read into state. What is in there is ARNs, the ECR URL and the
Lambda environment — owners, alert addresses, region.

## How the mirroring works

* **Full mirror, not a snapshot.** `git clone --mirror` followed by
  `git push --mirror` copies every branch and tag, and deletes refs on the
  CodeCommit side that no longer exist on GitHub. The CodeCommit copy tracks
  GitHub exactly; treat it as read-only, because anything committed only there
  is removed on the next run.
* **Refs GitHub invents are dropped.** GitHub exposes `refs/pull/*`, which
  CodeCommit rejects. Anything outside `refs/heads/*` and `refs/tags/*` is
  deleted locally before the push.
* **Naming.** `owner/repo` becomes `<prefix>owner-repo`, with characters
  CodeCommit disallows replaced by `-`. Names longer than CodeCommit's 100
  character limit are truncated with a hash suffix so they stay unique.
* **New repositories are created on demand,** private (CodeCommit repositories
  always are), tagged `ManagedBy=github-codecommit-mirror` and
  `SourceRepository=<owner>/<repo>`.
* **Deleting is manual.** A repository that disappears from GitHub keeps its
  CodeCommit mirror. That is deliberate — an accidental GitHub deletion should
  not take the backup with it. Find orphans by comparing the `SourceRepository`
  tags against the GitHub listing.
* **Runs are resumable.** Lambda stops at 15 minutes. When less than
  `time_budget_seconds` remains, the function invokes itself asynchronously with
  the repositories it has not reached, up to `max_continuations` times. Each
  invocation always completes at least one repository, so a run cannot loop
  without making progress.
* **Each repository is cloned fresh** into `/tmp` and deleted afterwards, so
  disk use is bounded by the largest single repository, not the total. Anything
  the GitHub API reports as larger than `max_repo_size_mb` is skipped rather
  than risking a full disk mid-run.

## Mirroring several accounts

One deployment handles a personal account and any number of organisations:

```hcl
github_owners = [
  { name = "aquarion", type = "user" },
  { name = "bb-cli",   type = "org" },
]
```

Each account is listed through the endpoint that reveals the most of it —
`/user/repos` for the token's own account, which is the only listing that
includes a user's private repositories, and `/orgs/<name>/repos` for an
organisation. Repositories are filtered back to the account being listed, so an
organisation you merely belong to does not get pulled in by your personal
listing, and a repository visible through two listings is mirrored once.

Mirror names already carry the owner (`gh-aquarion-api`, `gh-bb-cli-api`), so
two accounts with a same-named repository do not collide.

The catch is the token. A classic PAT with `repo` and `read:org` reaches
several accounts at once, but **fine-grained tokens are scoped to a single
account** — if your personal repositories and your organisation need separate
fine-grained tokens, run a deployment per token, giving each its own `name` and
`codecommit_name_prefix`:

```hcl
name                   = "github-mirror-myorg"
codecommit_name_prefix = "gh-myorg-"
```

Separate deployments are also the right answer when you want different
schedules or filters per account, at the cost of an ECR repository and image
per deployment.

## Configuration

Every variable is documented in [`variables.tf`](variables.tf). The ones worth
knowing about:

| Variable | Default | Notes |
| --- | --- | --- |
| `github_owners` | *required* | Accounts to mirror: `[{ name = "you", type = "user" }, { name = "your-org", type = "org" }]`. |
| `visibility` | `all` | `all`, `public` or `private`. |
| `include_forks` / `include_archived` | `false` | Off by default. |
| `include_pattern` / `exclude_pattern` | none | Regexes matched against `owner/repo`. |
| `codecommit_name_prefix` | `gh-` | Also scopes the IAM policy to those names. |
| `schedule_expression` | `rate(1 day)` | Any EventBridge schedule. |
| `lambda_memory_mb` | `3008` | More memory means more CPU and network, which is what makes git faster. |
| `lambda_ephemeral_storage_mb` | `10240` | Size of `/tmp`; must exceed your largest repository. |
| `max_repo_size_mb` | `4096` | Larger repositories are skipped and logged. |
| `alert_email_to` | `[]` | Addresses emailed when a run fails. Empty disables alerting entirely. |
| `alert_email_from` | none | Verified SES sender. Required when `alert_email_to` is set. |
| `ses_region` | `aws_region` | Region holding the verified SES identity. |
| `alarm_sns_topic_arn` | none | Route alarms to a topic you already manage instead of one created here. |

For GitHub Enterprise Server, set `github_api_url` to
`https://ghe.example.com/api/v3`; the clone host is derived from it.

## Failure alerts

Set two variables and every failure arrives as an email:

```hcl
alert_email_to   = ["ops@example.com"]
alert_email_from = "mirror@example.com" # a verified SES identity
```

Two things report, because they catch different failures:

* **The function emails you directly, via SES.** Any error that reaches the
  handler — a repository that would not clone, an expired token, GitHub
  unreachable — sends a mail naming the repositories that failed, the counts for
  the run, and the log group, stream and request id to look at. The message is
  scrubbed of the token first, and a mail that cannot be sent is logged and
  swallowed so it never masks the failure it was reporting.
* **CloudWatch alarms cover what the function cannot report itself.** A timeout,
  an out-of-memory kill or a crash before the error path runs leaves nothing to
  send the mail. The alarms on Lambda `Errors` and on this stack's own `Failed`
  metric catch those. With `alert_email_to` set and no `alarm_sns_topic_arn`,
  the stack creates an SNS topic and subscribes those addresses; **AWS emails
  each one a confirmation link that has to be clicked** before anything is
  delivered. Supply `alarm_sns_topic_arn` instead to route alarms into a topic
  you already manage, and this stack leaves its subscriptions alone.

The from address needs its domain (or the address itself) verified as an SES
identity in `ses_region`, which defaults to `aws_region`. If that SES account is
still in the sandbox, the recipients have to be verified too. The Lambda's IAM
policy is scoped to that identity with a `ses:FromAddress` condition, so the
role cannot send as anything else.

Leave `alert_email_to` empty and no alarms, topic or SES permissions are created
at all.

## Monitoring

Each run publishes `Mirrored`, `Empty`, `Skipped` and `Failed` counts to the
CloudWatch namespace named after the stack (`github-codecommit-mirror` by
default). A repository that fails does not stop the run — the other repositories
are still mirrored, and the invocation fails at the end with the list of names,
which surfaces as a Lambda `Errors` data point.

Failed runs are never retried automatically. A retry would re-clone everything
that already succeeded, and the next scheduled run picks the work up anyway.

## Limits worth knowing

* **Git LFS is not mirrored.** LFS objects live outside the git object store, so
  `clone --mirror` copies the pointer files and not the blobs. Adding it would
  mean `git lfs fetch --all` and `git lfs push --all` around the existing
  commands, plus the `git-lfs` binary in the image. AWS announced LFS support
  for CodeCommit during 2026; check whether it has landed in your region before
  relying on it.
* **Issues, pull requests, wikis, releases and Actions history are not
  mirrored.** This copies git, not GitHub.
* **Very large repositories** may not finish a clone and push inside 15 minutes,
  however much memory you give the function. Exclude them with
  `exclude_pattern` and mirror them from somewhere without a timeout, such as
  CodeBuild.
* **Around 1,700 repositories** is the practical ceiling for one scheduled run,
  because the continuation payload has to fit Lambda's 256 KB asynchronous
  invocation limit. Past that, split the work with `include_pattern` across
  several deployments.
* **A fresh clone every run** means bandwidth scales with total repository size,
  not with what changed. For very large estates, run it less often.

## Troubleshooting

**`InvalidParameterValueException: The image manifest, config or layer media
type for the source image ... is not supported`** when Terraform creates or
updates the function.

Lambda only accepts Docker Image Manifest V2 Schema 2. BuildKit defaults to OCI
media types and attaches provenance and SBOM attestations, which makes the tag
an OCI *index* pointing at several manifests; Lambda cannot resolve that.
[`scripts/build_image.sh`](scripts/build_image.sh) disables both attestations
and pins `oci-mediatypes=false`, and
[`scripts/build_and_push.sh`](scripts/build_and_push.sh) checks the media type
in ECR after pushing, so a bad image fails the build with an explanation rather
than at `CreateFunction`.

This is not a size problem — Lambda allows images up to 10 GB and reports that
separately — and Lambda layers are not an option for container-image functions;
layers only attach to zip-packaged ones.

If you hit it with an image built elsewhere, rebuild with:

```shell
docker buildx build --provenance=false --sbom=false \
  --output "type=image,name=<image>,oci-mediatypes=false,push=true" ./lambda
```

The image tag is a hash of `lambda/` *and* `scripts/`, so changing how the image
is built produces a new tag rather than reusing the broken one.

## Tests

The Lambda's logic — repository naming, filters, pagination, ref pruning and
token scrubbing — is covered by unit tests:

```shell
pip install boto3 pytest
python -m pytest tests -q
```

The Terraform is checked with:

```shell
terraform fmt -check -recursive
terraform init -backend=false && terraform validate
```

Both run in CI on every push.

## Mirroring somewhere other than CodeCommit

The same handler works with very little change against any git host that takes a
`git push --mirror`, if CodeCommit turns out not to suit you:

* **CodeBuild instead of Lambda** — no 15 minute ceiling and much more disk, at
  the cost of slower starts. The clone and push commands are identical.
* **S3** — replace the push with `git bundle create` and an upload. Cheaper and
  simpler for pure disaster recovery, but the result is not a live git remote.
* **A self-hosted remote** (Gitea, GitLab, a bare repo on EC2/EFS) — change the
  push URL and the credential setup; everything else stays.
