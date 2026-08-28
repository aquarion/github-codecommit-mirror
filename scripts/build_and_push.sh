#!/usr/bin/env bash
#
# Build the mirror Lambda's container image and push it to ECR.
# Called by Terraform, but safe to run by hand:
#
#   AWS_REGION=eu-west-1 \
#   AWS_ACCOUNT_ID=123456789012 \
#   ECR_REPOSITORY=123456789012.dkr.ecr.eu-west-1.amazonaws.com/github-codecommit-mirror \
#   IMAGE_TAG=$(git rev-parse --short HEAD) \
#   SOURCE_DIR=./lambda \
#   ./scripts/build_and_push.sh
set -euo pipefail

: "${AWS_REGION:?}" "${AWS_ACCOUNT_ID:?}" "${ECR_REPOSITORY:?}" "${IMAGE_TAG:?}" "${SOURCE_DIR:?}"

registry="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"
image="${ECR_REPOSITORY}:${IMAGE_TAG}"

if aws ecr describe-images \
    --region "${AWS_REGION}" \
    --repository-name "${ECR_REPOSITORY##*/}" \
    --image-ids "imageTag=${IMAGE_TAG}" >/dev/null 2>&1; then
  echo "Image ${image} already exists, nothing to build."
  exit 0
fi

echo "Logging in to ${registry}"
aws ecr get-login-password --region "${AWS_REGION}" \
  | docker login --username AWS --password-stdin "${registry}"

echo "Building ${image}"
# The Lambda runtime is linux/amd64; build for it explicitly so this also works
# from an Apple silicon workstation.
docker build --platform linux/amd64 -t "${image}" "${SOURCE_DIR}"

echo "Pushing ${image}"
docker push "${image}"
