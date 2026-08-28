data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

locals {
  # Every file that ends up in the image, so the tag changes when the code does.
  source_files = fileset("${path.module}/lambda", "**")
  source_hash = sha1(join("", [
    for file in sort(local.source_files) : filesha256("${path.module}/lambda/${file}")
  ]))

  image_tag = coalesce(var.image_tag, substr(local.source_hash, 0, 12))
  image_uri = "${aws_ecr_repository.lambda.repository_url}:${local.image_tag}"

  github_token_secret_arn = var.create_github_token_secret ? aws_secretsmanager_secret.github_token[0].arn : var.github_token_secret_arn

  email_alerts_enabled = length(var.alert_email_to) > 0
  ses_region           = coalesce(var.ses_region, var.aws_region)
  # Null-safe so a missing from address reaches the precondition below with a
  # readable message rather than failing inside split().
  ses_from_domain = var.alert_email_from == null ? "" : split("@", var.alert_email_from)[1]

  # Whether anything is wired up to receive alarms. Kept separate from the topic
  # ARN itself, which is not known until apply and so cannot drive a count.
  alerting_enabled = var.alarm_sns_topic_arn != null || local.email_alerts_enabled
  alarm_topic_arn = (
    var.alarm_sns_topic_arn != null
    ? var.alarm_sns_topic_arn
    : (local.email_alerts_enabled ? aws_sns_topic.alerts[0].arn : null)
  )

  # CreateRepository and ListRepositories have no resource-level permissions;
  # everything else is scoped to the names this stack owns.
  codecommit_repository_arn_pattern = "arn:${data.aws_partition.current.partition}:codecommit:${var.aws_region}:${data.aws_caller_identity.current.account_id}:${var.codecommit_name_prefix}*"
}

resource "terraform_data" "validate_configuration" {
  lifecycle {
    precondition {
      condition     = var.create_github_token_secret || var.github_token_secret_arn != null
      error_message = "Set github_token_secret_arn when create_github_token_secret is false."
    }

    precondition {
      condition     = length(var.alert_email_to) == 0 || var.alert_email_from != null
      error_message = "Set alert_email_from when alert_email_to is not empty."
    }
  }
}

resource "aws_secretsmanager_secret" "github_token" {
  count = var.create_github_token_secret ? 1 : 0

  name        = "${var.name}/github-token"
  description = "GitHub token used to clone repositories for the CodeCommit mirror."
  kms_key_id  = var.github_token_secret_kms_key_id
}

# Placeholder only. The real token is written out of band with the AWS CLI, and
# Terraform ignores the value from then on so it never enters the state file.
resource "aws_secretsmanager_secret_version" "github_token_placeholder" {
  count = var.create_github_token_secret ? 1 : 0

  secret_id     = aws_secretsmanager_secret.github_token[0].id
  secret_string = "REPLACE_ME"

  lifecycle {
    ignore_changes = [secret_string]
  }
}
