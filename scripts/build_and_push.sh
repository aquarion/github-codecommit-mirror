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

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
registry="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"
repository="${ECR_REPOSITORY##*/}"
image="${ECR_REPOSITORY}:${IMAGE_TAG}"

# Lambda accepts nothing else. Checked after the push so a bad image fails here,
# with an explanation, rather than inside CreateFunction.
required_media_type="application/vnd.docker.distribution.manifest.v2+json"

media_type_of() {
  aws ecr describe-images \
    --region "${AWS_REGION}" \
    --repository-name "${repository}" \
    --image-ids "imageTag=${IMAGE_TAG}" \
    --query 'imageDetails[0].imageManifestMediaType' \
    --output text 2>/dev/null
}

existing="$(media_type_of || true)"
if [ -n "${existing}" ] && [ "${existing}" != "None" ]; then
  if [ "${existing}" = "${required_media_type}" ]; then
    echo "Image ${image} already exists, nothing to build."
    exit 0
  fi
  echo "Image ${image} exists but is ${existing}; rebuilding." >&2
fi

echo "Logging in to ${registry}"
aws ecr get-login-password --region "${AWS_REGION}" \
  | docker login --username AWS --password-stdin "${registry}"

"${here}/build_image.sh" "${image}" "${SOURCE_DIR}"

pushed="$(media_type_of || true)"
if [ "${pushed}" != "${required_media_type}" ]; then
  cat >&2 <<ERROR
error: ${image} was pushed as
         ${pushed}
       but Lambda only supports
         ${required_media_type}

       Lambda would reject this with "The image manifest, config or layer media
       type for the source image ... is not supported". This usually means the
       build produced an OCI index, from BuildKit's provenance and SBOM
       attestations or from the containerd image store. scripts/build_image.sh
       disables both; check that your docker buildx supports --provenance and
       that the docker-container builder started.
ERROR
  exit 1
fi

echo "Pushed ${image} (${pushed})"
