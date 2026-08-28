resource "aws_ecr_repository" "lambda" {
  name                 = var.name
  image_tag_mutability = "MUTABLE"
  force_delete         = true

  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_lifecycle_policy" "lambda" {
  repository = aws_ecr_repository.lambda.name

  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep the 10 most recent images"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 10
      }
      action = { type = "expire" }
    }]
  })
}

# Builds lambda/ with the local Docker daemon and pushes it to ECR. Disable with
# build_and_push_image = false if the image is built in CI instead.
resource "null_resource" "image" {
  count = var.build_and_push_image ? 1 : 0

  triggers = {
    image_uri   = local.image_uri
    source_hash = local.source_hash
  }

  provisioner "local-exec" {
    command     = "${path.module}/scripts/build_and_push.sh"
    interpreter = ["/bin/bash", "-c"]

    environment = {
      AWS_REGION     = var.aws_region
      ECR_REPOSITORY = aws_ecr_repository.lambda.repository_url
      IMAGE_TAG      = local.image_tag
      SOURCE_DIR     = "${path.module}/lambda"
      AWS_ACCOUNT_ID = data.aws_caller_identity.current.account_id
    }
  }
}
