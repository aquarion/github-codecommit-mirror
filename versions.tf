terraform {
  # 1.10 introduced use_lockfile, the S3 backend's native locking.
  required_version = ">= 1.10.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.40"
    }
    null = {
      source  = "hashicorp/null"
      version = ">= 3.2"
    }
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = var.tags
  }
}
