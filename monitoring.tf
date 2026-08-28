# Created only when no existing topic was supplied. The alarms below cover the
# failures the function cannot report itself: a timeout, an out-of-memory kill,
# or a crash before the handler's own error path runs.
resource "aws_sns_topic" "alerts" {
  count = var.alarm_sns_topic_arn == null && local.email_alerts_enabled ? 1 : 0

  name = "${var.name}-alerts"
}

resource "aws_sns_topic_subscription" "alerts_email" {
  for_each = var.alarm_sns_topic_arn == null ? toset(var.alert_email_to) : toset([])

  topic_arn = aws_sns_topic.alerts[0].arn
  protocol  = "email"
  endpoint  = each.value
}

resource "aws_cloudwatch_metric_alarm" "failures" {
  count = local.alerting_enabled ? 1 : 0

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

  alarm_actions = [local.alarm_topic_arn]
  ok_actions    = [local.alarm_topic_arn]
}

resource "aws_cloudwatch_metric_alarm" "repositories_failed" {
  count = local.alerting_enabled ? 1 : 0

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

  alarm_actions = [local.alarm_topic_arn]
  ok_actions    = [local.alarm_topic_arn]
}
