resource "aws_cloudwatch_log_group" "lambda" {
  name              = "/aws/lambda/${var.name}"
  retention_in_days = var.log_retention_days
}

resource "aws_lambda_function" "mirror" {
  function_name = var.name
  description   = "Mirrors GitHub repositories into private CodeCommit repositories"
  role          = aws_iam_role.lambda.arn

  package_type  = "Image"
  image_uri     = local.image_uri
  architectures = ["x86_64"]

  timeout                        = var.lambda_timeout_seconds
  memory_size                    = var.lambda_memory_mb
  reserved_concurrent_executions = var.lambda_reserved_concurrency

  ephemeral_storage {
    size = var.lambda_ephemeral_storage_mb
  }

  environment {
    variables = {
      GITHUB_OWNERS           = jsonencode(var.github_owners)
      GITHUB_API_URL          = var.github_api_url
      GITHUB_TOKEN_SECRET_ARN = local.github_token_secret_arn
      CODECOMMIT_REGION       = var.aws_region
      CODECOMMIT_NAME_PREFIX  = var.codecommit_name_prefix
      INCLUDE_FORKS           = tostring(var.include_forks)
      INCLUDE_ARCHIVED        = tostring(var.include_archived)
      VISIBILITY              = var.visibility
      INCLUDE_PATTERN         = var.include_pattern == null ? "" : var.include_pattern
      EXCLUDE_PATTERN         = var.exclude_pattern == null ? "" : var.exclude_pattern
      MAX_REPO_SIZE_MB        = tostring(var.max_repo_size_mb)
      TIME_BUDGET_SECONDS     = tostring(var.time_budget_seconds)
      MAX_CONTINUATIONS       = tostring(var.max_continuations)
      METRIC_NAMESPACE        = var.name
      LOG_LEVEL               = var.log_level
    }
  }

  depends_on = [
    aws_cloudwatch_log_group.lambda,
    null_resource.image,
  ]
}

resource "aws_lambda_function_event_invoke_config" "mirror" {
  function_name = aws_lambda_function.mirror.function_name

  # A failed run must not be replayed: the next scheduled run picks the work up
  # again, and a retry would re-clone everything that already succeeded.
  maximum_retry_attempts = 0

  dynamic "destination_config" {
    for_each = var.alarm_sns_topic_arn == null ? [] : [var.alarm_sns_topic_arn]

    content {
      on_failure {
        destination = destination_config.value
      }
    }
  }
}
