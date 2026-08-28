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
