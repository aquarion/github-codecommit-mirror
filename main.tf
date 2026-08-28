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

  # CreateRepository and ListRepositories have no resource-level permissions;
  # everything else is scoped to the names this stack owns.
  codecommit_repository_arn_pattern = "arn:${data.aws_partition.current.partition}:codecommit:${var.aws_region}:${data.aws_caller_identity.current.account_id}:${var.codecommit_name_prefix}*"
}

resource "terraform_data" "validate_secret_configuration" {
  lifecycle {
    precondition {
      condition     = var.create_github_token_secret || var.github_token_secret_arn != null
      error_message = "Set github_token_secret_arn when create_github_token_secret is false."
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
