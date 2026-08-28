"""Import handler.py with the environment it expects at import time."""

import os
import sys
from pathlib import Path

os.environ.setdefault("AWS_REGION", "eu-west-1")
os.environ.setdefault("AWS_DEFAULT_REGION", "eu-west-1")
os.environ.setdefault("GITHUB_OWNER", "octocat")
os.environ.setdefault("GITHUB_OWNER_TYPE", "user")
os.environ.setdefault(
    "GITHUB_TOKEN_SECRET_ARN",
    "arn:aws:secretsmanager:eu-west-1:123456789012:secret:github-token-AbCdEf",
)
os.environ.setdefault("CODECOMMIT_NAME_PREFIX", "gh-")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lambda"))
