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
  mirror_lambda_arn      = "arn:${data.aws_partition.current.partition}:lambda:*:${data.aws_caller_identity.current.account_id}:function:${var.mirror_stack_name}"
  mirror_ecr_arn         = "arn:${data.aws_partition.current.partition}:ecr:*:${data.aws_caller_identity.current.account_id}:repository/${var.mirror_stack_name}"
  mirror_log_group_arn   = "arn:${data.aws_partition.current.partition}:logs:*:${data.aws_caller_identity.current.account_id}:log-group:/aws/lambda/${var.mirror_stack_name}"
  mirror_events_rule_arn = "arn:${data.aws_partition.current.partition}:events:*:${data.aws_caller_identity.current.account_id}:rule/${var.mirror_stack_name}*"
  mirror_sns_topic_arn   = "arn:${data.aws_partition.current.partition}:sns:*:${data.aws_caller_identity.current.account_id}:${var.mirror_stack_name}*"
  mirror_alarm_arn       = "arn:${data.aws_partition.current.partition}:cloudwatch:*:${data.aws_caller_identity.current.account_id}:alarm:${var.mirror_stack_name}*"
  mirror_secret_arn      = "arn:${data.aws_partition.current.partition}:secretsmanager:*:${data.aws_caller_identity.current.account_id}:secret:${var.mirror_stack_name}/github-token*"
  state_object_arn       = "arn:${data.aws_partition.current.partition}:s3:::${var.bucket_name}/${var.mirror_stack_name}/*"
  state_bucket_arn       = "arn:${data.aws_partition.current.partition}:s3:::${var.bucket_name}"
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

    # AWS requires a scoped sub or job_workflow_ref condition on any web
    # identity trust policy - it rejects a policy conditioned only on
    # repository/ref below. See github_actions_subject's description for why
    # this is an ID-based value rather than the plain repo name.
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = [var.github_actions_subject]
    }

    # Belt-and-braces on top of sub above - name-based and unaffected by
    # immutable subject claims, so this stays correct even if the sub value
    # above ever needs updating for a reason other than a rename (e.g. a
    # provider-side format change).
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:repository"
      values   = [var.github_repository]
    }

    # Only a workflow run triggered by a push already on main can assume
    # this role - a pull_request-triggered run carries a different ref value
    # (e.g. refs/pull/N/merge).
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:ref"
      values   = ["refs/heads/main"]
    }
  }
}

resource "aws_iam_role" "github_actions_deploy" {
  name               = "${var.mirror_stack_name}-github-actions-deploy"
  assume_role_policy = data.aws_iam_policy_document.github_actions_assume_role.json
}

# No CodeCommit permissions here: this stack never manages CodeCommit
# repositories via Terraform. The Lambda's own execution role creates them
# at runtime, so the deploy role needs none.
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
      "iam:ListRolePolicies",
      "iam:ListAttachedRolePolicies",
      "iam:DeleteRolePolicy",
    ]
    resources = [local.mirror_lambda_role_arn]
  }

  statement {
    sid       = "PassLambdaExecutionRole"
    actions   = ["iam:PassRole"]
    resources = [local.mirror_lambda_role_arn]

    # Scoped so this role can only be passed to Lambda, not any other AWS
    # service.
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
      "lambda:ListVersionsByFunction",
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
      "logs:ListTagsForResource",
    ]
    resources = [local.mirror_log_group_arn]
  }

  # No resource-level support: DescribeLogGroups is a list action and does
  # not accept a specific log-group ARN in all cases.
  statement {
    sid       = "LogsDescribe"
    actions   = ["logs:DescribeLogGroups"]
    resources = ["*"]
  }

  statement {
    sid = "EcrRepository"
    actions = [
      "ecr:CreateRepository",
      "ecr:DescribeRepositories",
      "ecr:DescribeImages",
      "ecr:DeleteRepository",
      "ecr:PutLifecyclePolicy",
      "ecr:GetLifecyclePolicy",
      "ecr:TagResource",
      "ecr:ListTagsForResource",
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
      "events:ListTagsForResource",
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
      "sns:GetSubscriptionAttributes",
      "sns:SetSubscriptionAttributes",
      "sns:ListSubscriptionsByTopic",
      "sns:TagResource",
      "sns:ListTagsForResource",
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
      "cloudwatch:ListTagsForResource",
    ]
    resources = [local.mirror_alarm_arn]
  }

  statement {
    sid = "GitHubTokenSecret"
    actions = [
      "secretsmanager:CreateSecret",
      "secretsmanager:DescribeSecret",
      "secretsmanager:DeleteSecret",
      "secretsmanager:TagResource",
      "secretsmanager:GetResourcePolicy",
    ]
    resources = [local.mirror_secret_arn]
  }
}

resource "aws_iam_role_policy" "github_actions_deploy" {
  name   = "${var.mirror_stack_name}-github-actions-deploy"
  role   = aws_iam_role.github_actions_deploy.id
  policy = data.aws_iam_policy_document.github_actions_deploy.json
}
