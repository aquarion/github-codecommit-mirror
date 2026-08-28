output "lambda_function_name" {
  description = "Name of the mirror function, handy for 'aws lambda invoke'."
  value       = aws_lambda_function.mirror.function_name
}

output "lambda_function_arn" {
  description = "ARN of the mirror function."
  value       = aws_lambda_function.mirror.arn
}

output "ecr_repository_url" {
  description = "ECR repository holding the Lambda container image."
  value       = aws_ecr_repository.lambda.repository_url
}

output "image_uri" {
  description = "Container image currently deployed to the function."
  value       = local.image_uri
}

output "github_token_secret_arn" {
  description = "Secret the function reads the GitHub token from."
  value       = local.github_token_secret_arn
}

output "schedule_rule_name" {
  description = "EventBridge rule that triggers the mirror."
  value       = aws_cloudwatch_event_rule.schedule.name
}

output "log_group_name" {
  description = "CloudWatch log group for the function."
  value       = aws_cloudwatch_log_group.lambda.name
}
