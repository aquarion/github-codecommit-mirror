# Partial backend configuration: the bucket, key and region come from a
# backend.hcl file so this repository carries no account-specific values.
#
#   terraform init -backend-config=backend.hcl
#
# Create the bucket first with the bootstrap/ module. Locking uses the S3
# object lock file (use_lockfile), so there is no DynamoDB table to run.
terraform {
  backend "s3" {}
}
