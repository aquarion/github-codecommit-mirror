resource "aws_cloudwatch_metric_alarm" "failures" {
  count = var.alarm_sns_topic_arn == null ? 0 : 1

  alarm_name          = "${var.name}-failures"
  alarm_description   = "The GitHub to CodeCommit mirror failed to complete a run."
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  statistic           = "Sum"
  period              = 3600
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

  dimensions = {
    FunctionName = aws_lambda_function.mirror.function_name
  }

  alarm_actions = [var.alarm_sns_topic_arn]
  ok_actions    = [var.alarm_sns_topic_arn]
}

resource "aws_cloudwatch_metric_alarm" "repositories_failed" {
  count = var.alarm_sns_topic_arn == null ? 0 : 1

  alarm_name          = "${var.name}-repositories-failed"
  alarm_description   = "One or more repositories could not be mirrored."
  namespace           = var.name
  metric_name         = "Failed"
  statistic           = "Sum"
  period              = 3600
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

  alarm_actions = [var.alarm_sns_topic_arn]
  ok_actions    = [var.alarm_sns_topic_arn]
}
