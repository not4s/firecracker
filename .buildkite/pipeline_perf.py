#!/usr/bin/env python3
# Copyright 2023 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""TEMPORARY: vsock backlog-full stall repro (replaces the perf pipeline)."""

from common import BKPipeline

pipeline = BKPipeline(priority=2, timeout_in_minutes=45)
pipeline.per_instance["instances"] = ["m6i.metal"]
pipeline.per_instance["platforms"] = [("al2023", "linux_6.1")]
pipeline.build_group(
    "vsock-backlog-repro",
    pipeline.devtool_test(
        pytest_opts="-s integration_tests/functional/test_zz_vsock_backlog_repro.py",
    ),
)
print(pipeline.to_json())
