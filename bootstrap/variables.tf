variable "bucket_name" {
  description = "Globally unique name for the state bucket, e.g. 'acme-terraform-state'."
  type        = string
}

variable "aws_region" {
  description = "Region for the state bucket. Use the same region as the mirror stack."
  type        = string
  default     = "eu-west-1"
}

variable "kms_key_arn" {
  description = "Optional customer managed KMS key for the bucket. Defaults to SSE-S3."
  type        = string
  default     = null
}

variable "state_version_retention_days" {
  description = "How long superseded state versions are kept."
  type        = number
  default     = 90
}

variable "tags" {
  description = "Tags applied to the bucket."
  type        = map(string)
  default = {
    ManagedBy = "terraform"
    Project   = "github-codecommit-mirror"
  }
}

variable "github_repository" {
  description = "GitHub repository allowed to deploy via OIDC, as 'owner/repo'."
  type        = string
  default     = "aquarion/github-codecommit-mirror"

  validation {
    condition     = can(regex("^[^/]+/[^/]+$", var.github_repository))
    error_message = "github_repository must be in 'owner/repo' form."
  }
}

variable "github_actions_subject" {
  description = <<-EOT
    The exact OIDC 'sub' claim GitHub Actions presents for a push to main on
    this repository, used as the trust policy's scoped sub condition (AWS
    requires either a scoped sub or job_workflow_ref condition on a web
    identity trust policy - it rejects one scoped only on the separate
    repository/ref claims).

    This repository has GitHub's immutable-subject-claims feature enabled
    (automatic once an org/repo has been renamed - this one has, from
    "istic" to "aquarion"), so the claim carries permanent numeric IDs
    rather than plain names:
    "repo:<owner>@<owner_id>/<repo>@<repo_id>:ref:refs/heads/<branch>".
    Captured from a CloudTrail AssumeRoleWithWebIdentity event's
    userIdentity.principalId after a deploy attempt. Being ID-based, it does
    not need updating if the repository or owner is renamed again.
  EOT
  type        = string
  default     = "repo:aquarion@201155/github-codecommit-mirror@1349881595:ref:refs/heads/main"
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
