FROM public.ecr.aws/lambda/python:3.12

# git is not in the Lambda base image, and git-remote-codecommit needs it to
# talk to CodeCommit over SigV4-signed HTTPS.
# The base image is Amazon Linux 2023; which package manager it ships with has
# varied between releases, so try each in turn.
RUN (dnf install -y git tar gzip && dnf clean all) \
    || (microdnf install -y git tar gzip && microdnf clean all) \
    || (yum install -y git tar gzip && yum clean all) \
    && rm -rf /var/cache/dnf /var/cache/yum

COPY requirements.txt ${LAMBDA_TASK_ROOT}/
RUN pip install --no-cache-dir -r ${LAMBDA_TASK_ROOT}/requirements.txt

# Only /tmp is writable at runtime, so git's config and credential store live there.
ENV HOME=/tmp \
    GIT_CONFIG_GLOBAL=/tmp/.gitconfig \
    GIT_TERMINAL_PROMPT=0

COPY handler.py ${LAMBDA_TASK_ROOT}/

CMD ["handler.lambda_handler"]
