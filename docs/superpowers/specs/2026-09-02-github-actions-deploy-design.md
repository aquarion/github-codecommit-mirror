# GitHub Actions deploy to AWS

## Problem

Deploying this stack today means a human runs `terraform apply` from their own
machine, with `backend.hcl` and `terraform.tfvars` copied and hand-edited
locally (both gitignored). There is no record of who deployed what, when, or
whether CI passed first. We want `main` to deploy itself once its CI checks
are green.

## Goals

- A push that lands on `main` deploys automatically, only after the existing
  `terraform`, `tests`, and `image` CI jobs pass.
- No long-lived AWS credentials stored in GitHub. Authentication is OIDC role
  assumption, scoped to this repository and to `main` only.
- Local `terraform apply` and CI `terraform apply` read the same config —
  no more drift between a developer's local copy and what CI would produce.

## Non-goals

- A `terraform plan` on pull requests. Explicitly declined — kept to one job,
  minimal moving parts.
- Multi-environment (staging/prod) deploys. This stack has one account, one
  environment, matching its current design.
- Automating the one-time account bootstrap (state bucket, OIDC provider).
  That stays a manual, run-once step, consistent with how the state bucket
  itself is bootstrapped today.

## Design

### 1. OIDC trust, created once via `bootstrap/`

`bootstrap/` already exists to set up one-time, account-level prerequisites
(the state bucket) outside the mirror stack's own state, because the mirror
stack cannot create the thing it depends on to run. The OIDC provider and
deploy role have the same chicken-and-egg problem — the identity GitHub
Actions will assume has to exist before GitHub Actions can apply anything —
so they belong in the same module.

Add to `bootstrap/`:

- `aws_iam_openid_connect_provider` for `token.actions.githubusercontent.com`
  (skipped via a `count`/data-source check if one already exists in the
  account — only one OIDC provider per URL is allowed per account, and a
  repo is not always the first thing to set this up).
- `aws_iam_role` "github-actions-deploy", trust policy restricted to:
  - `aud` = `sts.amazonaws.com`
  - `sub` = `repo:aquarion/github-codecommit-mirror:ref:refs/heads/main`

  This means only a workflow run triggered by a push that has already landed
  on `main` can assume the role. A pull request (even from a branch in the
  same repo) cannot — PR-triggered workflows carry a `pull_request` sub
  claim, not a `ref:refs/heads/main` one.
- An inline or attached policy granting the deploy role what `terraform
  apply` needs across the mirror stack's resource types: IAM (role + policy
  CRUD for the Lambda's own role), Lambda, ECR, CodeCommit, EventBridge,
  Secrets Manager (create/describe the empty secret shell — never read/write
  its value), SNS, CloudWatch Logs/Alarms, SES identity lookup, plus
  read/write on the state bucket (S3 native locking needs no DynamoDB table,
  so none is granted). Actions are scoped to resource ARNs carrying the stack's
  `var.name` prefix where the service supports resource-level ARNs (mirrors
  the existing scoping pattern in `iam.tf`); actions on services with no
  resource-level ARN support (e.g. `ecr:GetAuthorizationToken`,
  `ses:GetAccount`) are `"*"` of necessity, same as `ListRepositories`
  already is in `iam.tf`.

New `bootstrap/outputs.tf` output: `github_actions_role_arn`.

This module is applied once, by hand, exactly like today:
```
cd bootstrap && terraform apply
```
Then the printed role ARN is set as a repository variable:
```
gh variable set AWS_DEPLOY_ROLE_ARN --body "<arn>"
```
That variable — not a secret, since a role ARN grants nothing without the
matching trust policy — is what the workflow reads.

### 2. Commit `backend.hcl` and `terraform.tfvars`

Both are removed from `.gitignore` and committed with real values (the
account's actual state bucket name, the actual owners/emails/schedule this
deployment already uses). Per the existing README, neither file holds
secrets — `backend.hcl` is "account-specific names" only, and `tfvars`
covers owners, visibility filters, alert addresses, and the cron schedule.
The GitHub token remains the one genuinely sensitive value, and it already
never passes through Terraform or git.

`terraform.tfvars.example` and `backend.hcl.example` stay as-is as templates
for anyone standing up a second, independent deployment of this stack.

### 3. New `deploy` job in `.github/workflows/ci.yml`

```yaml
deploy:
  name: Deploy
  runs-on: ubuntu-latest
  needs: [terraform, tests, image]
  if: github.ref == 'refs/heads/main' && github.event_name == 'push'
  permissions:
    id-token: write
    contents: read
  steps:
    - uses: actions/checkout@v4
    - uses: aws-actions/configure-aws-credentials@v4
      with:
        role-to-assume: ${{ vars.AWS_DEPLOY_ROLE_ARN }}
        aws-region: eu-west-1
    - uses: hashicorp/setup-terraform@v3
      with:
        terraform_version: 1.13.3
    - run: terraform init -backend-config=backend.hcl
    - run: terraform apply -auto-approve
```

`needs: [terraform, tests, image]` means a failing lint, test, or Docker
build blocks the deploy outright — the same CI gate that already runs on
every PR now also gates what reaches AWS.

The existing `null_resource.image` / `scripts/build_and_push.sh` path is
unchanged: it runs the GitHub-hosted runner's own Docker daemon instead of a
developer's, using the OIDC-derived credentials already in the environment
via `aws ecr get-login-password`. No script changes needed.

### Data flow

```
push to main
     │
     ▼
CI: terraform validate / pytest / docker build   (existing jobs, unchanged)
     │  all pass
     ▼
deploy job: assume AWS_DEPLOY_ROLE_ARN via OIDC
     │
     ▼
terraform init (S3 backend) → terraform apply
     │
     ├─ null_resource.image → docker build & push → ECR
     └─ aws_lambda_function.mirror → picks up new image_uri, other resources
```

### Error handling

- If `terraform apply` fails, the job fails and shows in the commit's check
  status on GitHub — same visibility as any other CI failure, no separate
  alerting needed for this path.
- The role's trust condition means a compromised or misconfigured workflow
  in a fork, or a workflow run from a non-`main` ref, gets an AWS-side
  `AccessDenied` on the OIDC assume-role call, before any AWS API is
  reachable.
- `terraform apply -auto-approve` on `main` mirrors what a human runs
  locally today; there is no plan/approve gate by design (declined above).

### Testing

- `bootstrap/` has no test suite today (it's a one-time, manually-applied
  module); the new resources get the same `terraform validate` coverage the
  CI `terraform` job already runs, plus a manual `terraform plan` review
  before the one-time `terraform apply` in `bootstrap/`.
- The `deploy` job itself is validated by observation: first push to `main`
  after this lands should show a green `Deploy` check and an updated Lambda
  image/config in AWS.
- No changes to `lambda/` code, so the existing 94 `pytest` tests are
  untouched.
