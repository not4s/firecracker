#!/usr/bin/env python3
# Copyright 2025 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Buildkite pipeline for release QA
"""

import os

from common import BKPipeline

# Release tag builds don't get a VERSION, so use the tag. Step env overrides the build's.
version = os.environ.get("BUILDKITE_TAG") or os.environ["VERSION"]
pipeline = BKPipeline(with_build_step=False, env={"VERSION": version})

# NOTE: we need to escape $ using $$ otherwise buildkite tries to replace it instead of the shell

# The release build starts from the same tag push, so retry until it has uploaded every file
# listed in SHA256SUMS.
pipeline.add_step(
    {
        "label": "download-release",
        "if": 'build.env("VERSION") != "dev"',
        "command": [
            (
                "for arch in x86_64 aarch64; do"
                "  RELEASE_DIR=release-$$VERSION-$$arch;"
                "  OUT_DIR=release-$$VERSION/$$arch;"
                "  until aws s3 sync s3://firecracker-release-prod-us-east-1/$$VERSION/$$arch $$RELEASE_DIR"
                "    && (cd $$RELEASE_DIR && sha256sum --quiet -c SHA256SUMS); do"
                "    sleep 60;"
                "  done;"
                "  mkdir -p $$OUT_DIR;"
                "  for f in $$RELEASE_DIR/*-$$arch; do"
                "    mv $$f $$OUT_DIR/$$(basename $$f -$$VERSION-$$arch);"
                "    mv $$f.debug $$OUT_DIR/$$(basename $$f -$$VERSION-$$arch).debug;"
                "  done;"
                "done"
            ),
            'buildkite-agent artifact upload "release-$$VERSION/**/*"',
        ],
        "env": {"VERSION": version},
        "timeout_in_minutes": 60,
    },
    depends_on_build=False,
)

pipeline.build_group_per_arch(
    "make-release",
    # if is a keyword for python, so we need this workaround to expand it as a kwarg
    **{"if": 'build.env("VERSION") == "dev"'},
    command=[
        "./tools/devtool -y make_release",
        "RELEASE_DIR=$$(echo release-*dev-$$(uname -m))",
        "RELEASE_SUFFIX=$${{RELEASE_DIR#release}}",
        "OUT_DIR=release-$$VERSION/$$(uname -m)",
        "mkdir -p $$OUT_DIR",
        (
            "for f in $$RELEASE_DIR/*-$$(uname -m); do"
            "  mv $$f $$OUT_DIR/$$(basename $$f $$RELEASE_SUFFIX);"
            "  mv $$f.debug $$OUT_DIR/$$(basename $$f $$RELEASE_SUFFIX).debug;"
            "done"
        ),
        'buildkite-agent artifact upload "release-$$VERSION/**/*"',
    ],
    depends_on_build=False,
)

# The devtool expects the examples to be in the same folder as the binaries to run some tests
# (for example, uffd handler tests). Build them and upload them in the same folder.
pipeline.build_group_per_arch(
    "build-examples",
    command=[
        "CARGO_TARGET=$$(uname -m)-unknown-linux-musl",
        "./tools/devtool -y sh cargo build --target $$CARGO_TARGET --release --examples",
        "mkdir -p release-$$VERSION/$$(uname -m)/",
        "cp -R build/cargo_target/$$CARGO_TARGET/release/examples release-$$VERSION/$$(uname -m)/",
        'buildkite-agent artifact upload "release-$$VERSION/**/*"',
    ],
    depends_on_build=False,
)

pipeline.add_step("wait", depends_on_build=False)

pipeline.add_step(
    {
        "label": "run-pr-pipeline",
        "command": (
            ".buildkite/pipeline_pr.py --binary-dir release-$$VERSION "
            "| jq '(..|select(.priority? != null).priority) += 100' "
            "| buildkite-agent pipeline upload"
        ),
        "env": {"VERSION": version},
    },
    depends_on_build=False,
)

print(pipeline.to_json())
