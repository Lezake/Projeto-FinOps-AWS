terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.4"
    }
  }
}

provider "aws" {
  region = "us-east-2"
}

# ==============================================================================
# Infraestrutura Base: AMI & Launch Template
# ==============================================================================

data "aws_ami" "amazon_linux" {
  most_recent = true
  owners      = ["amazon"]

  filter {
    name   = "name"
    values = ["al2023-ami-2023.*-x86_64"]
  }
}

resource "aws_launch_template" "padrao_micro" {
  name_prefix   = "modelo-base-"
  image_id      = data.aws_ami.amazon_linux.id
  instance_type = "t2.micro"
}

# ==============================================================================
# Auto Scaling Groups (Dev Frontend, Dev Backend e Prod)
# ==============================================================================

resource "aws_autoscaling_group" "asg_dev_frontend" {
  name               = "asg-dev-frontend"
  availability_zones = ["us-east-2a", "us-east-2b"]
  desired_capacity   = 1
  max_size           = 1
  min_size           = 0

  launch_template {
    id      = aws_launch_template.padrao_micro.id
    version = "$Latest"
  }

  tag {
    key                 = "ambiente"
    value               = "dev"
    propagate_at_launch = true
  }
  tag {
    key                 = "Name"
    value               = "dev-frontend-asg"
    propagate_at_launch = true
  }
}

resource "aws_autoscaling_group" "asg_dev_backend" {
  name               = "asg-dev-backend"
  availability_zones = ["us-east-2a", "us-east-2b"]
  desired_capacity   = 1
  max_size           = 1
  min_size           = 0

  launch_template {
    id      = aws_launch_template.padrao_micro.id
    version = "$Latest"
  }

  tag {
    key                 = "ambiente"
    value               = "dev"
    propagate_at_launch = true
  }
  tag {
    key                 = "Name"
    value               = "dev-backend-asg"
    propagate_at_launch = true
  }
}

resource "aws_autoscaling_group" "asg_prod" {
  name               = "asg-prod"
  availability_zones = ["us-east-2a", "us-east-2b"]
  desired_capacity   = 1
  max_size           = 1
  min_size           = 0

  launch_template {
    id      = aws_launch_template.padrao_micro.id
    version = "$Latest"
  }

  tag {
    key                 = "ambiente"
    value               = "prod"
    propagate_at_launch = true
  }
  tag {
    key                 = "Name"
    value               = "prod-asg"
    propagate_at_launch = true
  }
}

# ==============================================================================
# Bancos de Dados RDS (Dev e Prod com Criptografia KMS em Repouso)
# ==============================================================================

resource "aws_db_instance" "banco_dev" {
  identifier          = "banco-dev"
  allocated_storage   = 20
  storage_type        = "gp2"
  engine              = "mysql"
  engine_version      = "8.0"
  instance_class      = "db.t3.micro"
  username            = var.db_username
  password            = var.db_password
  storage_encrypted   = true
  skip_final_snapshot = true

  tags = {
    Name     = "banco-dev"
    ambiente = "dev"
  }
}

resource "aws_db_instance" "banco_prod" {
  identifier          = "banco-prod"
  allocated_storage   = 20
  storage_type        = "gp2"
  engine              = "mysql"
  engine_version      = "8.0"
  instance_class      = "db.t3.micro"
  username            = var.db_username
  password            = var.db_password
  storage_encrypted   = true
  skip_final_snapshot = true

  tags = {
    Name     = "banco-prod"
    ambiente = "prod"
  }
}

# ==============================================================================
# Automação FinOps: IAM (Least Privilege)
# ==============================================================================

resource "aws_iam_role" "lambda_finops_role" {
  name = "role-lambda-finops-dev"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "lambda.amazonaws.com"
        }
      }
    ]
  })
}

resource "aws_iam_policy" "lambda_finops_policy" {
  name        = "policy-lambda-finops-dev"
  description = "Permissoes granulares de Least Privilege para automacao FinOps em recursos Dev"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "CloudWatchLogsAccess"
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents"
        ]
        Resource = "arn:aws:logs:*:*:*"
      },
      {
        Sid    = "FinOpsReadOnlyDescribeOperations"
        Effect = "Allow"
        Action = [
          "rds:DescribeDBInstances",
          "autoscaling:DescribeAutoScalingGroups",
          "ec2:DescribeVolumes",
          "ec2:DescribeSnapshots"
        ]
        Resource = "*"
      },
      {
        Sid    = "RDSFinOpsDevOperations"
        Effect = "Allow"
        Action = [
          "rds:StartDBInstance",
          "rds:StopDBInstance",
          "rds:AddTagsToResource",
          "rds:RemoveTagsFromResource"
        ]
        Resource = "*"
        Condition = {
          StringEquals = {
            "aws:ResourceTag/ambiente" = "dev"
          }
        }
      },
      {
        Sid    = "AutoScalingFinOpsDevOperations"
        Effect = "Allow"
        Action = [
          "autoscaling:UpdateAutoScalingGroup",
          "autoscaling:CreateOrUpdateTags",
          "autoscaling:DeleteTags"
        ]
        Resource = "*"
        Condition = {
          StringEquals = {
            "aws:ResourceTag/ambiente" = "dev"
          }
        }
      },
      {
        Sid    = "EC2DeleteDevVolumesOnly"
        Effect = "Allow"
        Action = [
          "ec2:DeleteVolume"
        ]
        Resource = "*"
        Condition = {
          StringEquals = {
            "aws:ResourceTag/ambiente" = "dev"
          }
        }
      },
      {
        Sid    = "EC2SnapshotDevVolumesOnly"
        Effect = "Allow"
        Action = [
          "ec2:CreateSnapshot",
          "ec2:CreateTags"
        ]
        Resource = "*"
      },
      {
        Sid    = "EC2DeleteDevSnapshotsOnly"
        Effect = "Allow"
        Action = [
          "ec2:DeleteSnapshot"
        ]
        Resource = "*"
        Condition = {
          StringEquals = {
            "aws:ResourceTag/ambiente"      = "dev",
            "aws:ResourceTag/finops:origin" = "auto-cleanup-backup"
          }
        }
      },
      {
        Sid    = "SQSSendMessageToDLQ"
        Effect = "Allow"
        Action = [
          "sqs:SendMessage"
        ]
        Resource = [
          aws_sqs_queue.dlq_stop_dev.arn,
          aws_sqs_queue.dlq_start_dev.arn
        ]
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "lambda_finops_attachment" {
  role       = aws_iam_role.lambda_finops_role.name
  policy_arn = aws_iam_policy.lambda_finops_policy.arn
}

# ==============================================================================
# Automação FinOps: Dead-Letter Queues (DLQ) para Resiliência do EventBridge
# ==============================================================================

resource "aws_sqs_queue" "dlq_stop_dev" {
  name                      = "finops-stop-dev-dlq"
  message_retention_seconds = 1209600 # 14 dias de retenção

  tags = {
    Name     = "finops-stop-dev-dlq"
    ambiente = "finops"
  }
}

resource "aws_sqs_queue_policy" "dlq_stop_dev_policy" {
  queue_url = aws_sqs_queue.dlq_stop_dev.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "AllowEventBridgeToSendMessages"
        Effect = "Allow"
        Principal = {
          Service = "events.amazonaws.com"
        }
        Action   = "sqs:SendMessage"
        Resource = aws_sqs_queue.dlq_stop_dev.arn
        Condition = {
          ArnEquals = {
            "aws:SourceArn" = aws_cloudwatch_event_rule.rule_stop_dev.arn
          }
        }
      },
      {
        Sid    = "AllowLambdaRoleToSendMessages"
        Effect = "Allow"
        Principal = {
          AWS = aws_iam_role.lambda_finops_role.arn
        }
        Action   = "sqs:SendMessage"
        Resource = aws_sqs_queue.dlq_stop_dev.arn
      }
    ]
  })
}

resource "aws_sqs_queue" "dlq_start_dev" {
  name                      = "finops-start-dev-dlq"
  message_retention_seconds = 1209600 # 14 dias de retenção

  tags = {
    Name     = "finops-start-dev-dlq"
    ambiente = "finops"
  }
}

resource "aws_sqs_queue_policy" "dlq_start_dev_policy" {
  queue_url = aws_sqs_queue.dlq_start_dev.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "AllowEventBridgeToSendMessages"
        Effect = "Allow"
        Principal = {
          Service = "events.amazonaws.com"
        }
        Action   = "sqs:SendMessage"
        Resource = aws_sqs_queue.dlq_start_dev.arn
        Condition = {
          ArnEquals = {
            "aws:SourceArn" = aws_cloudwatch_event_rule.rule_start_dev.arn
          }
        }
      },
      {
        Sid    = "AllowLambdaRoleToSendMessages"
        Effect = "Allow"
        Principal = {
          AWS = aws_iam_role.lambda_finops_role.arn
        }
        Action   = "sqs:SendMessage"
        Resource = aws_sqs_queue.dlq_start_dev.arn
      }
    ]
  })
}

# ==============================================================================
# Automação FinOps: Pacotes Lambda (ZIP)
# ==============================================================================

data "archive_file" "zip_start_dev" {
  type        = "zip"
  source_file = "${path.module}/../src/lambda_start_dev/index.py"
  output_path = "${path.module}/lambda_start_dev.zip"
}

data "archive_file" "zip_stop_dev" {
  type        = "zip"
  source_file = "${path.module}/../src/lambda_stop_dev/index.py"
  output_path = "${path.module}/lambda_stop_dev.zip"
}

# ==============================================================================
# Automação FinOps: Funções AWS Lambda
# ==============================================================================

resource "aws_lambda_function" "lambda_start_dev" {
  function_name    = "lambda-start-dev"
  role             = aws_iam_role.lambda_finops_role.arn
  handler          = "index.lambda_handler"
  runtime          = "python3.11"
  filename         = data.archive_file.zip_start_dev.output_path
  source_code_hash = data.archive_file.zip_start_dev.output_base64sha256
  timeout          = 300

  tags = {
    Name     = "lambda-start-dev"
    ambiente = "finops"
  }
}

resource "aws_lambda_function" "lambda_stop_dev" {
  function_name    = "lambda-stop-dev"
  role             = aws_iam_role.lambda_finops_role.arn
  handler          = "index.lambda_handler"
  runtime          = "python3.11"
  filename         = data.archive_file.zip_stop_dev.output_path
  source_code_hash = data.archive_file.zip_stop_dev.output_base64sha256
  timeout          = 300

  tags = {
    Name     = "lambda-stop-dev"
    ambiente = "finops"
  }
}

# ==============================================================================
# Automação FinOps: CloudWatch Log Groups com Retenção Gerenciada
# ==============================================================================

resource "aws_cloudwatch_log_group" "log_group_start_dev" {
  name              = "/aws/lambda/${aws_lambda_function.lambda_start_dev.function_name}"
  retention_in_days = 14

  tags = {
    Name     = "log-group-lambda-start-dev"
    ambiente = "finops"
  }
}

resource "aws_cloudwatch_log_group" "log_group_stop_dev" {
  name              = "/aws/lambda/${aws_lambda_function.lambda_stop_dev.function_name}"
  retention_in_days = 14

  tags = {
    Name     = "log-group-lambda-stop-dev"
    ambiente = "finops"
  }
}

# ==============================================================================
# Automação FinOps: Configuração de Destino Assíncrono (DLQ On-Failure)
# ==============================================================================

resource "aws_lambda_function_event_invoke_config" "invoke_config_stop_dev" {
  function_name                = aws_lambda_function.lambda_stop_dev.function_name
  maximum_event_age_in_seconds = 21600
  maximum_retry_attempts       = 2

  destination_config {
    on_failure {
      destination = aws_sqs_queue.dlq_stop_dev.arn
    }
  }
}

resource "aws_lambda_function_event_invoke_config" "invoke_config_start_dev" {
  function_name                = aws_lambda_function.lambda_start_dev.function_name
  maximum_event_age_in_seconds = 21600
  maximum_retry_attempts       = 2

  destination_config {
    on_failure {
      destination = aws_sqs_queue.dlq_start_dev.arn
    }
  }
}

# ==============================================================================
# Automação FinOps: EventBridge (Agendamento de Execução com DLQ)
# ==============================================================================

# Sexta-feira às 20h BRT (23:00 UTC) -> Desligar recursos Dev
resource "aws_cloudwatch_event_rule" "rule_stop_dev" {
  name                = "finops-stop-dev-friday"
  description         = "Executa a rotina de stop no ambiente de Dev toda sexta-feira as 20h BRT (23h UTC)"
  schedule_expression = "cron(0 23 ? * FRI *)"
}

resource "aws_cloudwatch_event_target" "target_stop_dev" {
  rule      = aws_cloudwatch_event_rule.rule_stop_dev.name
  target_id = "TargetLambdaStopDev"
  arn       = aws_lambda_function.lambda_stop_dev.arn

  dead_letter_config {
    arn = aws_sqs_queue.dlq_stop_dev.arn
  }
}

resource "aws_lambda_permission" "permission_stop_dev" {
  statement_id  = "AllowExecutionFromEventBridgeStop"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.lambda_stop_dev.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.rule_stop_dev.arn
}

# Segunda-feira às 08h BRT (11:00 UTC) -> Restaurar recursos Dev
resource "aws_cloudwatch_event_rule" "rule_start_dev" {
  name                = "finops-start-dev-monday"
  description         = "Executa a rotina de start no ambiente de Dev toda segunda-feira as 08h BRT (11h UTC)"
  schedule_expression = "cron(0 11 ? * MON *)"
}

resource "aws_cloudwatch_event_target" "target_start_dev" {
  rule      = aws_cloudwatch_event_rule.rule_start_dev.name
  target_id = "TargetLambdaStartDev"
  arn       = aws_lambda_function.lambda_start_dev.arn

  dead_letter_config {
    arn = aws_sqs_queue.dlq_start_dev.arn
  }
}

resource "aws_lambda_permission" "permission_start_dev" {
  statement_id  = "AllowExecutionFromEventBridgeStart"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.lambda_start_dev.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.rule_start_dev.arn
}

# ==============================================================================
# Automação FinOps: Monitoramento Proativo de DLQ (SNS & CloudWatch Alarms)
# ==============================================================================

resource "aws_sns_topic" "finops_alerts" {
  name = "finops-operacional-alerts"

  tags = {
    Name     = "finops-operacional-alerts"
    ambiente = "finops"
  }
}

resource "aws_sns_topic_subscription" "email_alert" {
  count     = var.alert_email != "" ? 1 : 0
  topic_arn = aws_sns_topic.finops_alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

resource "aws_cloudwatch_metric_alarm" "alarm_dlq_stop" {
  alarm_name          = "finops-dlq-stop-mensagens-retidas"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 1
  metric_name         = "ApproximateNumberOfMessagesVisible"
  namespace           = "AWS/SQS"
  period              = 60
  statistic           = "Maximum"
  threshold           = 1
  alarm_description   = "Alerta operacional FinOps: A rotina de STOP falhou apos todos os retries e reteve mensagem na DLQ."
  alarm_actions       = [aws_sns_topic.finops_alerts.arn]

  dimensions = {
    QueueName = aws_sqs_queue.dlq_stop_dev.name
  }

  tags = {
    Name     = "alarm-finops-dlq-stop"
    ambiente = "finops"
  }
}

resource "aws_cloudwatch_metric_alarm" "alarm_dlq_start" {
  alarm_name          = "finops-dlq-start-mensagens-retidas"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 1
  metric_name         = "ApproximateNumberOfMessagesVisible"
  namespace           = "AWS/SQS"
  period              = 60
  statistic           = "Maximum"
  threshold           = 1
  alarm_description   = "Alerta operacional FinOps: A rotina de START falhou apos todos os retries e reteve mensagem na DLQ."
  alarm_actions       = [aws_sns_topic.finops_alerts.arn]

  dimensions = {
    QueueName = aws_sqs_queue.dlq_start_dev.name
  }

  tags = {
    Name     = "alarm-finops-dlq-start"
    ambiente = "finops"
  }
}
