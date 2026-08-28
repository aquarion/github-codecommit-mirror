#!/usr/bin/env bash
#
# Build the mirror Lambda's image and push it to <image-ref>.
#
#   ./scripts/build_image.sh 1234.dkr.ecr.eu-west-1.amazonaws.com/mirror:tag ./lambda
#
# Lambda only accepts Docker Image Manifest V2 Schema 2
# (application/vnd.docker.distribution.manifest.v2+json). BuildKit defaults to
# OCI media types and attaches provenance and SBOM attestations, which turn the
# tag into an OCI index of several manifests. Lambda cannot resolve that and
# rejects the image with:
#
#   InvalidParameterValueException: The image manifest, config or layer media
#   type for the source image ... is not supported.
#
# So the attestations are disabled and the media types pinned.
set -euo pipefail

image="${1:?usage: build_image.sh <image-ref> <source-dir>}"
source_dir="${2:?usage: build_image.sh <image-ref> <source-dir>}"
builder="${BUILDX_BUILDER_NAME:-github-codecommit-mirror}"

# The Lambda runtime is linux/amd64; naming it explicitly means this also works
# from an Apple silicon workstation.
platform="linux/amd64"

build_with_buildx() {
  # A docker-container builder is what makes --output honour oci-mediatypes.
  docker buildx inspect "${builder}" >/dev/null 2>&1 \
    || docker buildx create --name "${builder}" --driver docker-container >/dev/null

  docker buildx build \
    --builder "${builder}" \
    --platform "${platform}" \
    --provenance=false \
    --sbom=false \
    --output "type=image,name=${image},oci-mediatypes=false,push=true" \
    "${source_dir}"
}

build_with_legacy_builder() {
  # The pre-BuildKit builder only ever produced schema 2, so it is a safe
  # fallback where buildx is unavailable or cannot start a container builder.
  echo "Falling back to the legacy builder."
  DOCKER_BUILDKIT=0 docker build --platform "${platform}" -t "${image}" "${source_dir}"
  docker push "${image}"
}

echo "Building and pushing ${image}"
if docker buildx version >/dev/null 2>&1; then
  build_with_buildx || build_with_legacy_builder
else
  build_with_legacy_builder
fi
