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
