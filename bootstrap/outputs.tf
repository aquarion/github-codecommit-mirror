output "bucket_name" {
  description = "Name of the state bucket."
  value       = aws_s3_bucket.state.id
}

output "backend_hcl" {
  description = "Ready-made contents for the parent directory's backend.hcl."
  value       = <<-EOT
    bucket       = "${aws_s3_bucket.state.id}"
    key          = "github-codecommit-mirror/terraform.tfstate"
    region       = "${var.aws_region}"
    encrypt      = true
    use_lockfile = true
  EOT
}

output "github_actions_role_arn" {
  description = "Role for GitHub Actions to assume via OIDC. Set as the AWS_DEPLOY_ROLE_ARN repository variable."
  value       = aws_iam_role.github_actions_deploy.arn
}
