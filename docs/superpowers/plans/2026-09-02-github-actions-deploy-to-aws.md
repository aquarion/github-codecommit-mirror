# GitHub Actions Deploy to AWS Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A push that lands on `main` deploys this stack to AWS automatically, authenticated via OIDC, once the existing CI checks pass.

**Architecture:** A one-time `bootstrap/` addition creates a GitHub Actions OIDC provider and a deploy IAM role trusted only for `repo:aquarion/github-codecommit-mirror:ref:refs/heads/main`. `backend.hcl` and `terraform.tfvars` — both non-secret — move from gitignored-local into the repo so CI and local `terraform apply` read identical config. A new `deploy` job in `.github/workflows/ci.yml`, gated on the existing `terraform`/`tests`/`image` jobs passing, assumes that role and runs `terraform apply`.

**Tech Stack:** Terraform (AWS provider), GitHub Actions, `aws-actions/configure-aws-credentials`.

**Full spec:** `docs/superpowers/specs/2026-09-02-github-actions-deploy-design.md`

---

### Task 1: Commit `backend.hcl` and `terraform.tfvars`

Both files already exist locally with real, non-secret values. Un-ignore and commit them so CI reads the same config a human would apply locally.

**Files:**
- Modify: `.gitignore`
- Commit: `backend.hcl` (already exists on disk, currently gitignored)
- Commit: `terraform.tfvars` (already exists on disk, currently gitignored)

- [ ] **Step 1: Remove `backend.hcl` and `terraform.tfvars` from `.gitignore`**

In `.gitignore`, find:

```
# Local files holding account-specific values
*.auto.tfvars
terraform.tfvars
backend.hcl
```

Replace with:

```
# Local files holding account-specific values
*.auto.tfvars
```

- [ ] **Step 2: Verify neither file contains anything secret**

Run: `cat backend.hcl terraform.tfvars`

Expected output — confirm every line matches this shape (bucket name, region, owners, email addresses, cron schedule; no tokens, keys, or ARNs with account-specific secrets):

```
bucket       = "aqcom-terraform-state"
key          = "github-codecommit-mirror/terraform.tfstate"
region       = "eu-west-1"
encrypt      = true
use_lockfile = true

# Copy to terraform.tfvars and edit. terraform.tfvars is gitignored.

aws_region = "eu-west-1"

# Whose repositories to mirror. A personal account and any number of
# organisations can share one deployment, as long as one token reaches them all.
github_owners = [
  { name = "aquarion", type = "user" },
  { name = "istic", type = "org" },
]

# What to mirror. The defaults skip forks and archived repositories.
visibility       = "all"
include_forks    = true
include_archived = false
# exclude_pattern = "^aquarion/(scratch|sandbox)-"

# CodeCommit repositories are named <prefix><owner>-<repo>.
codecommit_name_prefix = "gh-"

# Daily at 03:00 UTC.
schedule_expression = "cron(0 4 * * ? *)"

# Get told when a run fails. alert_email_from must be a verified SES identity;
# the SNS subscription AWS creates for the alarms needs confirming by email once.
alert_email_to   = ["nicholas+codecommitmirror@aquarionics.com"]
alert_email_from = "codecommitmirror@aquarionics.com"
# ses_region = "eu-west-1"

# Or route the alarms into a topic you already manage:
# alarm_sns_topic_arn = "arn:aws:sns:eu-west-1:123456789012:alerts"
```

The one comment line in `terraform.tfvars` ("Copy to terraform.tfvars and edit...") is now stale since the file is the real, committed config rather than something copied from the example — leave it for this task; Task 6 removes it while updating the README's deploy instructions, since that's where the "copy the example" instruction lives too.

- [ ] **Step 3: Stage and commit**

```bash
git add .gitignore backend.hcl terraform.tfvars
git commit -m "🔄️ Commit backend.hcl and terraform.tfvars for CI

Neither file holds secrets - backend.hcl is account-specific names
only, terraform.tfvars covers owners/visibility/alert addresses/
schedule. Committing them means CI's terraform apply reads the exact
config a local apply would, instead of drifting from an
uncommitted copy."
```

---

### Task 2: Add bootstrap variables for the GitHub Actions deploy role

**Files:**
- Modify: `bootstrap/variables.tf`

- [ ] **Step 1: Add the new variables**

Append to `bootstrap/variables.tf`:

```hcl

variable "github_repository" {
  description = "GitHub repository allowed to deploy via OIDC, as 'owner/repo'."
  type        = string
  default     = "aquarion/github-codecommit-mirror"
}

variable "mirror_stack_name" {
  description = <<-EOT
    Must match `name` in the parent module (default
    "github-codecommit-mirror"). Scopes the deploy role's permissions to the
    resources that stack creates.
  EOT
  type        = string
  default     = "github-codecommit-mirror"
}

variable "create_oidc_provider" {
  description = <<-EOT
    Whether to create the GitHub Actions OIDC provider. AWS allows only one
    provider per URL per account - set this to false and provide
    oidc_provider_arn if the account already has one (e.g. from another
    repository's deploy setup).
  EOT
  type        = bool
  default     = true
}

variable "oidc_provider_arn" {
  description = "ARN of an existing GitHub Actions OIDC provider. Required when create_oidc_provider is false."
  type        = string
  default     = null
}
```

- [ ] **Step 2: Validate syntax**

Run: `terraform -chdir=bootstrap init -backend=false -input=false && terraform -chdir=bootstrap validate`
Expected: `Success! The configuration is valid.`

- [ ] **Step 3: Commit**

```bash
git add bootstrap/variables.tf
git commit -m "🎇 Add bootstrap variables for the GitHub Actions deploy role"
```

---

### Task 3: Add the OIDC provider, deploy role, and scoped policy

**Files:**
- Create: `bootstrap/deploy_role.tf`

This role is what the `deploy` CI job will assume. Its trust policy accepts only a workflow run whose OIDC token carries `sub = repo:aquarion/github-codecommit-mirror:ref:refs/heads/main` — a pull request run (different `sub` claim) or a run in a fork cannot assume it. Its permissions are scoped to exactly the resource types and names the parent module's `.tf` files create (checked against every `resource` block in `ecr.tf`, `eventbridge.tf`, `iam.tf`, `lambda.tf`, `main.tf`, `monitoring.tf` — the mirror stack never manages CodeCommit repositories via Terraform, so no CodeCommit permissions are needed here; the Lambda's own execution role already covers that at runtime).

- [ ] **Step 1: Create `bootstrap/deploy_role.tf`**

```hcl
# The IAM identity GitHub Actions assumes to run `terraform apply` against
# this account. Created here rather than in the parent module for the same
# reason the state bucket is: the identity that will apply the parent module
# has to exist before that module can be applied.

data "aws_partition" "current" {}
data "aws_caller_identity" "current" {}

# AWS validates GitHub's certificate chain directly and no longer checks this
# thumbprint after creation, but the argument is still required by the API.
resource "aws_iam_openid_connect_provider" "github_actions" {
  count = var.create_oidc_provider ? 1 : 0

  url             = "https://token.actions.githubusercontent.com"
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = ["6938fd4d98bab03faadb97b34396831e3780aea1"]
}

locals {
  oidc_provider_arn = var.create_oidc_provider ? aws_iam_openid_connect_provider.github_actions[0].arn : var.oidc_provider_arn

  mirror_lambda_role_arn = "arn:${data.aws_partition.current.partition}:iam::${data.aws_caller_identity.current.account_id}:role/${var.mirror_stack_name}-lambda"
  mirror_lambda_arn       = "arn:${data.aws_partition.current.partition}:lambda:*:${data.aws_caller_identity.current.account_id}:function:${var.mirror_stack_name}"
  mirror_ecr_arn          = "arn:${data.aws_partition.current.partition}:ecr:*:${data.aws_caller_identity.current.account_id}:repository/${var.mirror_stack_name}"
  mirror_log_group_arn    = "arn:${data.aws_partition.current.partition}:logs:*:${data.aws_caller_identity.current.account_id}:log-group:/aws/lambda/${var.mirror_stack_name}"
  mirror_events_rule_arn  = "arn:${data.aws_partition.current.partition}:events:*:${data.aws_caller_identity.current.account_id}:rule/${var.mirror_stack_name}*"
  mirror_sns_topic_arn    = "arn:${data.aws_partition.current.partition}:sns:*:${data.aws_caller_identity.current.account_id}:${var.mirror_stack_name}*"
  mirror_alarm_arn        = "arn:${data.aws_partition.current.partition}:cloudwatch:*:${data.aws_caller_identity.current.account_id}:alarm:${var.mirror_stack_name}*"
  mirror_secret_arn       = "arn:${data.aws_partition.current.partition}:secretsmanager:*:${data.aws_caller_identity.current.account_id}:secret:${var.mirror_stack_name}/github-token*"
  state_object_arn        = "arn:${data.aws_partition.current.partition}:s3:::${var.bucket_name}/${var.mirror_stack_name}/*"
  state_bucket_arn        = "arn:${data.aws_partition.current.partition}:s3:::${var.bucket_name}"
}

data "aws_iam_policy_document" "github_actions_assume_role" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [local.oidc_provider_arn]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    # Only a workflow run triggered by a push already on main can assume
    # this role - a pull_request-triggered run carries a different sub claim.
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = ["repo:${var.github_repository}:ref:refs/heads/main"]
    }
  }
}

resource "aws_iam_role" "github_actions_deploy" {
  name               = "${var.mirror_stack_name}-github-actions-deploy"
  assume_role_policy = data.aws_iam_policy_document.github_actions_assume_role.json
}

data "aws_iam_policy_document" "github_actions_deploy" {
  statement {
    sid       = "TerraformState"
    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
    resources = [local.state_object_arn]
  }

  statement {
    sid       = "TerraformStateBucket"
    actions   = ["s3:ListBucket", "s3:GetBucketLocation"]
    resources = [local.state_bucket_arn]
  }

  statement {
    sid = "LambdaExecutionRole"
    actions = [
      "iam:CreateRole",
      "iam:GetRole",
      "iam:DeleteRole",
      "iam:TagRole",
      "iam:PutRolePolicy",
      "iam:GetRolePolicy",
      "iam:DeleteRolePolicy",
    ]
    resources = [local.mirror_lambda_role_arn]
  }

  statement {
    sid       = "PassLambdaExecutionRole"
    actions   = ["iam:PassRole"]
    resources = [local.mirror_lambda_role_arn]

    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["lambda.amazonaws.com"]
    }
  }

  statement {
    sid = "LambdaFunction"
    actions = [
      "lambda:CreateFunction",
      "lambda:GetFunction",
      "lambda:UpdateFunctionCode",
      "lambda:UpdateFunctionConfiguration",
      "lambda:DeleteFunction",
      "lambda:TagResource",
      "lambda:ListTags",
      "lambda:PutFunctionEventInvokeConfig",
      "lambda:GetFunctionEventInvokeConfig",
      "lambda:DeleteFunctionEventInvokeConfig",
      "lambda:GetPolicy",
      "lambda:AddPermission",
      "lambda:RemovePermission",
    ]
    resources = [local.mirror_lambda_arn]
  }

  statement {
    sid = "LambdaLogGroup"
    actions = [
      "logs:CreateLogGroup",
      "logs:DeleteLogGroup",
      "logs:PutRetentionPolicy",
      "logs:TagResource",
      "logs:DescribeLogGroups",
      "logs:ListTagsForResource",
    ]
    resources = [local.mirror_log_group_arn]
  }

  statement {
    sid = "EcrRepository"
    actions = [
      "ecr:CreateRepository",
      "ecr:DescribeRepositories",
      "ecr:DeleteRepository",
      "ecr:PutLifecyclePolicy",
      "ecr:GetLifecyclePolicy",
      "ecr:TagResource",
      "ecr:PutImageScanningConfiguration",
    ]
    resources = [local.mirror_ecr_arn]
  }

  statement {
    sid = "EcrPushImage"
    actions = [
      "ecr:GetDownloadUrlForLayer",
      "ecr:BatchGetImage",
      "ecr:BatchCheckLayerAvailability",
      "ecr:PutImage",
      "ecr:InitiateLayerUpload",
      "ecr:UploadLayerPart",
      "ecr:CompleteLayerUpload",
    ]
    resources = [local.mirror_ecr_arn]
  }

  # No resource-level support: ECR login tokens are account-wide, not scoped
  # to one repository.
  statement {
    sid       = "EcrAuth"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  statement {
    sid = "EventBridgeSchedule"
    actions = [
      "events:PutRule",
      "events:DescribeRule",
      "events:DeleteRule",
      "events:PutTargets",
      "events:RemoveTargets",
      "events:ListTargetsByRule",
      "events:TagResource",
    ]
    resources = [local.mirror_events_rule_arn]
  }

  statement {
    sid = "AlertTopic"
    actions = [
      "sns:CreateTopic",
      "sns:GetTopicAttributes",
      "sns:SetTopicAttributes",
      "sns:DeleteTopic",
      "sns:Subscribe",
      "sns:Unsubscribe",
      "sns:ListSubscriptionsByTopic",
      "sns:TagResource",
    ]
    resources = [local.mirror_sns_topic_arn]
  }

  statement {
    sid = "AlertAlarms"
    actions = [
      "cloudwatch:PutMetricAlarm",
      "cloudwatch:DescribeAlarms",
      "cloudwatch:DeleteAlarms",
      "cloudwatch:TagResource",
    ]
    resources = [local.mirror_alarm_arn]
  }

  statement {
    sid = "GithubTokenSecretShell"
    actions = [
      "secretsmanager:CreateSecret",
      "secretsmanager:DescribeSecret",
      "secretsmanager:DeleteSecret",
      "secretsmanager:TagResource",
    ]
    resources = [local.mirror_secret_arn]
  }
}

resource "aws_iam_role_policy" "github_actions_deploy" {
  name   = "${var.mirror_stack_name}-github-actions-deploy"
  role   = aws_iam_role.github_actions_deploy.id
  policy = data.aws_iam_policy_document.github_actions_deploy.json
}
```

- [ ] **Step 2: Fix formatting**

Run: `terraform fmt bootstrap/deploy_role.tf`

(The `locals` block above is hand-aligned; `terraform fmt` will realign the `=` signs — that's expected and correct.)

- [ ] **Step 3: Validate**

Run: `terraform -chdir=bootstrap init -backend=false -input=false && terraform -chdir=bootstrap validate`
Expected: `Success! The configuration is valid.`

- [ ] **Step 4: Commit**

```bash
git add bootstrap/deploy_role.tf
git commit -m "🎇 Add GitHub Actions OIDC provider and scoped deploy role

Trust is restricted to repo:aquarion/github-codecommit-mirror:ref:refs/heads/main,
so only a workflow run triggered by a push already on main can assume
this role. Permissions are scoped to exactly the resource types and
names the parent module's .tf files create - no CodeCommit
permissions, since the mirror stack creates those at runtime via the
Lambda's own execution role, never through Terraform."
```

---

### Task 4: Output the deploy role ARN

**Files:**
- Modify: `bootstrap/outputs.tf`

- [ ] **Step 1: Add the output**

Append to `bootstrap/outputs.tf`:

```hcl

output "github_actions_role_arn" {
  description = "Role for GitHub Actions to assume via OIDC. Set as the AWS_DEPLOY_ROLE_ARN repository variable."
  value       = aws_iam_role.github_actions_deploy.arn
}
```

- [ ] **Step 2: Validate**

Run: `terraform -chdir=bootstrap init -backend=false -input=false && terraform -chdir=bootstrap validate`
Expected: `Success! The configuration is valid.`

- [ ] **Step 3: Commit**

```bash
git add bootstrap/outputs.tf
git commit -m "🎇 Output the GitHub Actions deploy role ARN from bootstrap"
```

---

### Task 5: Add the `deploy` job to CI

**Files:**
- Modify: `.github/workflows/ci.yml`

- [ ] **Step 1: Add the job**

In `.github/workflows/ci.yml`, append a new job after the existing `image` job (matching its indentation — jobs are top-level keys under `jobs:`):

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

      - name: Init
        run: terraform init -backend-config=backend.hcl

      - name: Apply
        run: terraform apply -auto-approve
```

- [ ] **Step 2: Validate the workflow YAML**

Run: `python3 -c "import yaml, sys; yaml.safe_load(open('.github/workflows/ci.yml'))" && echo "valid YAML"`
Expected: `valid YAML`

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/ci.yml
git commit -m "⚙️ Deploy to AWS on push to main

Gated on the existing terraform/tests/image jobs passing first, so a
failing lint, test, or Docker build blocks the deploy outright.
Authenticates via OIDC role assumption - no long-lived AWS
credentials stored in GitHub."
```

---

### Task 6: Update the README

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Replace the manual-apply instructions with the CI-deploy flow**

Find this block in `README.md` (under `## Deploy`):

```markdown
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
```

Replace with:

```markdown
That creates a versioned, encrypted, private bucket and prints the backend
configuration for it.

The same `bootstrap` apply also creates the OIDC provider and IAM role that
GitHub Actions uses to deploy: `terraform apply` on `main` runs automatically
in CI once its checks pass. Print the role ARN it created and set it as a
repository variable:

```shell
gh variable set AWS_DEPLOY_ROLE_ARN \
  --body "$(terraform -chdir=bootstrap output -raw github_actions_role_arn)"
```

`backend.hcl` and `terraform.tfvars` are committed, real, non-secret config -
edit them directly rather than starting from the `.example` files, which are
templates for standing up a second, independent deployment of this stack. If
you already have a state bucket, skip the bootstrap step and write
`backend.hcl` by hand from [`backend.hcl.example`](backend.hcl.example).

To apply locally instead of waiting for CI (e.g. while iterating on a change
before it reaches `main`):

```shell
terraform init -backend-config=backend.hcl
terraform apply
```
```

- [ ] **Step 2: Update the "Set the token" section's context**

No code change needed here — the token-writing instructions are unaffected by CI deploy (the secret is still created empty by Terraform and written out-of-band). Confirm by rereading that section after Step 1's edit that nothing there references the now-removed "copy the example" flow. Read `README.md` and check the "### Set the token" heading still reads correctly in context — no edit expected unless something looks broken.

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "📖 Document the CI auto-deploy flow"
```

---

### Task 7: Apply bootstrap and wire up the repository variable

This task touches the real AWS account and GitHub repository settings — creating an IAM OIDC provider and role, and setting a repository variable. **Confirm with the user before running `terraform apply` here even though the plan spells out every step**, per this project's standing rule to check before actions that modify shared/real infrastructure.

**Files:** none (infrastructure + GitHub repository settings only)

- [ ] **Step 1: Review the bootstrap plan**

Run: `terraform -chdir=bootstrap plan`
Expected: a plan showing two new resources to add (`aws_iam_openid_connect_provider.github_actions[0]`, `aws_iam_role.github_actions_deploy`) and one new policy (`aws_iam_role_policy.github_actions_deploy`) — zero resources changed or destroyed. The existing state bucket resources should show no changes.

- [ ] **Step 2: Apply**

Run: `terraform -chdir=bootstrap apply`
Confirm the plan matches Step 1's expectation, then type `yes`.

- [ ] **Step 3: Set the repository variable**

```bash
gh variable set AWS_DEPLOY_ROLE_ARN \
  --body "$(terraform -chdir=bootstrap output -raw github_actions_role_arn)" \
  -R aquarion/github-codecommit-mirror
```

- [ ] **Step 4: Verify**

Run: `gh variable list -R aquarion/github-codecommit-mirror`
Expected: `AWS_DEPLOY_ROLE_ARN` listed with a value starting `arn:aws:iam::`.

---

### Task 8: Open the PR and watch the first real deploy

**Files:** none

- [ ] **Step 1: Push the branch and open a draft PR**

```bash
git push -u origin feature/ci-deploy-to-aws
gh pr create --draft --title "Deploy to AWS from GitHub Actions" --body "$(cat <<'EOF'
## Summary
- One-time bootstrap addition: GitHub Actions OIDC provider + a deploy role
  trusted only for pushes already on main, scoped to exactly the resources
  this stack's Terraform manages.
- backend.hcl and terraform.tfvars committed (both non-secret) so CI and
  local apply read identical config.
- New `deploy` job in CI: gated on terraform/tests/image passing, runs
  `terraform apply` via the OIDC role.

## Test plan
- [x] `terraform validate` on both the root module and `bootstrap/`
- [x] `bootstrap` applied by hand, `AWS_DEPLOY_ROLE_ARN` repo variable set
- [ ] Merge to main and confirm the `Deploy` check goes green
- [ ] Confirm the Lambda's image/config reflects this branch's changes after merge
EOF
)"
```

- [ ] **Step 2: After merge, watch the deploy job**

Run: `gh run watch --exit-status $(gh run list --workflow=ci.yml --branch=main --limit=1 --json databaseId -q '.[0].databaseId')`
Expected: all jobs including `Deploy` show `success`.

- [ ] **Step 3: Confirm AWS reflects the deploy**

Run: `terraform output -raw image_uri`

Then: `aws lambda get-function --function-name github-codecommit-mirror --query 'Configuration.{Image:PackageType,LastModified:LastModified}'`
Expected: `LastModified` timestamp matches the time of the CI run just watched.

---

## Self-review notes

- **Spec coverage:** OIDC provider + trust condition (Task 3), scoped permissions (Task 3), `backend.hcl`/`terraform.tfvars` committed (Task 1), `deploy` job gated on existing CI jobs and `main`-push only (Task 5), role ARN as a repo variable not a secret (Task 4 + 7), README updated (Task 6), no PR-time plan step (intentionally absent, matches the spec's declined non-goal), manual one-time bootstrap apply (Task 7) — every spec section has a task.
- **No placeholders:** all file contents are complete; the one deliberately-deferred item (README's "Set the token" section) is explicitly a no-op check, not an unfinished edit.
- **Naming consistency:** `mirror_stack_name` (bootstrap var) defaults to `"github-codecommit-mirror"`, matching `name` in the parent module's `variables.tf` and the `key` prefix already in the real `backend.hcl` — Task 3's `state_object_arn` local uses the variable rather than hardcoding the string, so the two stay in sync if either is ever renamed. `AWS_DEPLOY_ROLE_ARN` is used identically in Task 5 (workflow) and Task 7 (repo variable name) and matches the output name from Task 4.
