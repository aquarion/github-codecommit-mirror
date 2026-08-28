data "aws_iam_policy_document" "lambda_assume_role" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "lambda" {
  name               = "${var.name}-lambda"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
}

data "aws_iam_policy_document" "lambda" {
  statement {
    sid       = "Logs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.lambda.arn}:*"]
  }

  statement {
    sid       = "ReadGitHubToken"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [local.github_token_secret_arn]
  }

  statement {
    sid = "CreateAndListMirrors"
    actions = [
      "codecommit:CreateRepository",
      "codecommit:ListRepositories",
    ]
    resources = ["*"]
  }

  statement {
    sid = "PushToMirrors"
    actions = [
      "codecommit:GetRepository",
      "codecommit:GitPull",
      "codecommit:GitPush",
      "codecommit:TagResource",
      "codecommit:UpdateRepositoryDescription",
    ]
    resources = [local.codecommit_repository_arn_pattern]
  }

  # CodeCommit encrypts repositories with KMS; git operations need these on the
  # key, which is the AWS managed aws/codecommit key unless you supply your own.
  statement {
    sid = "CodeCommitEncryption"
    actions = [
      "kms:Encrypt",
      "kms:Decrypt",
      "kms:ReEncrypt*",
      "kms:GenerateDataKey",
      "kms:GenerateDataKeyWithoutPlaintext",
      "kms:DescribeKey",
    ]
    resources = ["*"]

    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["codecommit.${var.aws_region}.amazonaws.com"]
    }
  }

  statement {
    sid       = "PublishMetrics"
    actions   = ["cloudwatch:PutMetricData"]
    resources = ["*"]

    condition {
      test     = "StringEquals"
      variable = "cloudwatch:namespace"
      values   = [var.name]
    }
  }

  # Used to hand the remaining repositories to a continuation invocation when a
  # run is about to hit the Lambda timeout.
  statement {
    sid       = "SelfInvokeForContinuation"
    actions   = ["lambda:InvokeFunction"]
    resources = [aws_lambda_function.mirror.arn]
  }

  # For the Lambda on-failure destination.
  dynamic "statement" {
    for_each = local.alerting_enabled ? [1] : []

    content {
      sid       = "PublishFailures"
      actions   = ["sns:Publish"]
      resources = [local.alarm_topic_arn]
    }
  }

  # Failure alert emails. Scoped to the verified identity and the one From
  # address, so this role cannot send as anything else.
  dynamic "statement" {
    # The from address is also required by a precondition; guarding here keeps
    # a misconfiguration reporting that message rather than an interpolation error.
    for_each = local.email_alerts_enabled && var.alert_email_from != null ? [1] : []

    content {
      sid     = "SendFailureAlerts"
      actions = ["ses:SendEmail"]

      resources = [
        "arn:${data.aws_partition.current.partition}:ses:${local.ses_region}:${data.aws_caller_identity.current.account_id}:identity/${local.ses_from_domain}",
        "arn:${data.aws_partition.current.partition}:ses:${local.ses_region}:${data.aws_caller_identity.current.account_id}:identity/${var.alert_email_from}",
      ]

      condition {
        test     = "StringEquals"
        variable = "ses:FromAddress"
        values   = [var.alert_email_from]
      }
    }
  }
}

resource "aws_iam_role_policy" "lambda" {
  name   = "${var.name}-lambda"
  role   = aws_iam_role.lambda.id
  policy = data.aws_iam_policy_document.lambda.json
}
