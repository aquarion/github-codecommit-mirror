variable "aws_region" {
  description = "Region that hosts the Lambda function, the ECR repository and the CodeCommit mirrors."
  type        = string
  default     = "eu-west-1"
}

variable "name" {
  description = "Base name for the resources this stack creates."
  type        = string
  default     = "github-codecommit-mirror"
}

variable "tags" {
  description = "Tags applied to every resource in this stack."
  type        = map(string)
  default = {
    ManagedBy = "terraform"
    Project   = "github-codecommit-mirror"
  }
}

# --------------------------------------------------------------------------
# GitHub
# --------------------------------------------------------------------------
variable "github_owners" {
  description = <<-EOT
    The accounts whose repositories are mirrored, each with its type. One
    deployment can mirror a personal account and any number of organisations:

      github_owners = [
        { name = "aquarion", type = "user" },
        { name = "bb-cli",   type = "org" },
      ]

    The token needs access to all of them. A classic PAT with 'repo' (and
    'read:org' for organisations) covers several owners at once; fine-grained
    tokens are scoped to a single account, so mirroring owners that need
    separate tokens means a separate deployment per token.
  EOT

  type = list(object({
    name = string
    type = optional(string, "user")
  }))

  validation {
    condition     = length(var.github_owners) > 0
    error_message = "github_owners must list at least one account."
  }

  validation {
    condition     = alltrue([for owner in var.github_owners : contains(["user", "org"], owner.type)])
    error_message = "Each github_owners entry must have type 'user' or 'org'."
  }

  validation {
    condition     = length(distinct([for owner in var.github_owners : lower(owner.name)])) == length(var.github_owners)
    error_message = "github_owners must not list the same account twice."
  }
}

variable "github_api_url" {
  description = "GitHub API base URL. Change this for GitHub Enterprise Server."
  type        = string
  default     = "https://api.github.com"
}

variable "create_github_token_secret" {
  description = <<-EOT
    Create an empty Secrets Manager secret for the GitHub token. Terraform never
    stores the token itself: it creates the secret and ignores later changes to
    the value, so you set the token once with the AWS CLI (see the README).
    Set to false to reuse an existing secret via github_token_secret_arn.
  EOT
  type        = bool
  default     = true
}

variable "github_token_secret_arn" {
  description = "ARN of an existing secret holding the GitHub token. Required when create_github_token_secret is false."
  type        = string
  default     = null
}

variable "github_token_secret_kms_key_id" {
  description = "Optional KMS key id/ARN for the created secret. Defaults to the AWS managed key."
  type        = string
  default     = null
}

# --------------------------------------------------------------------------
# What to mirror
# --------------------------------------------------------------------------
variable "include_forks" {
  description = "Mirror repositories that are forks."
  type        = bool
  default     = false
}

variable "include_archived" {
  description = "Mirror archived repositories."
  type        = bool
  default     = false
}

variable "visibility" {
  description = "Which repositories to mirror: 'all', 'public' or 'private'."
  type        = string
  default     = "all"

  validation {
    condition     = contains(["all", "public", "private"], var.visibility)
    error_message = "visibility must be one of 'all', 'public' or 'private'."
  }
}

variable "include_pattern" {
  description = "Optional regex; only repositories whose 'owner/name' matches are mirrored."
  type        = string
  default     = null
}

variable "exclude_pattern" {
  description = "Optional regex; repositories whose 'owner/name' matches are skipped."
  type        = string
  default     = null
}

variable "codecommit_name_prefix" {
  description = "Prefix for the CodeCommit repository names, e.g. 'gh-'. Also scopes the IAM policy."
  type        = string
  default     = "gh-"

  validation {
    condition     = can(regex("^[A-Za-z0-9._-]*$", var.codecommit_name_prefix))
    error_message = "codecommit_name_prefix may only contain letters, digits, '.', '_' and '-'."
  }
}

variable "max_repo_size_mb" {
  description = "Repositories larger than this (per the GitHub API) are skipped, so one huge repo cannot fill the Lambda's disk."
  type        = number
  default     = 4096
}

# --------------------------------------------------------------------------
# Lambda
# --------------------------------------------------------------------------
variable "lambda_memory_mb" {
  description = "Lambda memory. More memory also means more CPU and network throughput, which is what makes git faster."
  type        = number
  default     = 3008
}

variable "lambda_ephemeral_storage_mb" {
  description = "Size of /tmp, where repositories are cloned. Max 10240."
  type        = number
  default     = 10240
}

variable "lambda_timeout_seconds" {
  description = "Lambda timeout. 900 is the maximum."
  type        = number
  default     = 900
}

variable "lambda_reserved_concurrency" {
  description = <<-EOT
    Reserved concurrency for the function. Keep at least 2 so a run that is
    running out of time can immediately hand the remaining repositories to a
    continuation invocation. Set to -1 to use unreserved concurrency.
  EOT
  type        = number
  default     = 2
}

variable "time_budget_seconds" {
  description = "Stop starting new repositories when less than this much of the Lambda timeout remains, and continue in a new invocation."
  type        = number
  default     = 180
}

variable "max_continuations" {
  description = "Maximum number of chained continuation invocations per scheduled run."
  type        = number
  default     = 10
}

variable "log_retention_days" {
  description = "CloudWatch Logs retention for the function."
  type        = number
  default     = 30
}

variable "log_level" {
  description = "Python log level for the function."
  type        = string
  default     = "INFO"
}

# --------------------------------------------------------------------------
# Schedule and alerting
# --------------------------------------------------------------------------
variable "schedule_expression" {
  description = "EventBridge schedule, e.g. 'rate(1 day)' or 'cron(0 3 * * ? *)'."
  type        = string
  default     = "rate(1 day)"
}

variable "schedule_enabled" {
  description = "Whether the EventBridge rule is enabled."
  type        = bool
  default     = true
}

variable "alarm_sns_topic_arn" {
  description = "Optional SNS topic notified when a run fails. Also used as the Lambda on-failure destination."
  type        = string
  default     = null
}

# --------------------------------------------------------------------------
# Container image
# --------------------------------------------------------------------------
variable "build_and_push_image" {
  description = <<-EOT
    Build the Lambda container image with the local Docker daemon and push it to
    ECR during 'terraform apply'. Set to false when your CI pipeline builds the
    image instead, and pass the tag through image_tag.
  EOT
  type        = bool
  default     = true
}

variable "image_tag" {
  description = "Image tag to deploy. Defaults to a hash of the lambda/ directory, so the function redeploys whenever the source changes."
  type        = string
  default     = null
}
