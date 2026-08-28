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

  dynamic "statement" {
    for_each = var.alarm_sns_topic_arn == null ? [] : [var.alarm_sns_topic_arn]

    content {
      sid       = "PublishFailures"
      actions   = ["sns:Publish"]
      resources = [statement.value]
    }
  }
}

resource "aws_iam_role_policy" "lambda" {
  name   = "${var.name}-lambda"
  role   = aws_iam_role.lambda.id
  policy = data.aws_iam_policy_document.lambda.json
}
