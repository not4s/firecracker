# Copyright 2025 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Tests the performance of MMDS token generation and verification."""

import re

import pytest

from framework.utils import configure_mmds, populate_data_store

# Default IPv4 address for MMDS
DEFAULT_IPV4 = "169.254.169.254"

# Number of steady-state samples to keep per metric.
ITERATIONS = 500
# Warm-up requests discarded before measuring. The first requests of a batch pay
# for cold caches; discarding them keeps the sample representative.
WARMUP = 10

TOKEN_URL = f"http://{DEFAULT_IPV4}/latest/api/token"
DATA_URL = f"http://{DEFAULT_IPV4}/latest/meta-data/instance-id"
EXPECTED_RESPONSE = '"i-1234567890abcdef0"'


def parse_curl_timing(prefix: str, timing_line: str):
    """Parse curl timing output and extract timing information in milliseconds."""
    # curl -w format outputs timing in seconds, convert to milliseconds
    # Expected format: "<prefix>:0.123456"
    match = re.search(prefix + r":([\d.]+)", timing_line)
    if match:
        return float(match.group(1)) * 1000  # Convert to milliseconds

    raise ValueError(f"Could not parse timing from curl output: {timing_line}")


@pytest.fixture
def mmds_microvm(uvm):
    """Creates a microvm with MMDS configured for performance testing."""
    uvm.spawn(log_level="Info")
    uvm.basic_config()
    uvm.add_net_iface()

    # Configure MMDS V2 (requires tokens)
    configure_mmds(uvm, iface_ids=["eth0"], version="V2", ipv4_address=DEFAULT_IPV4)

    # Populate with minimal test data
    test_data = {"latest": {"meta-data": {"instance-id": "i-1234567890abcdef0"}}}
    populate_data_store(uvm, test_data)

    uvm.start()

    uvm.ssh.check_output(f"ip route add {DEFAULT_IPV4} dev eth0")

    return uvm


@pytest.mark.nonci
def test_mmds_token(mmds_microvm, metrics):
    """
    Test MMDS token generation and data-request performance.

    Previously this test spawned a fresh ``curl`` process (with a fresh TCP
    connection) for every request. At MMDS's microsecond-scale response times the
    per-request process fork and connection setup dominate and jitter, producing
    run-to-run volatility that tripped the A/B statistical gate as a false positive.

    Instead, each metric is measured by a *single* ``curl`` process that performs
    all requests over one reused connection (``curl -K`` reads a config file whose
    transfers are separated by ``next``; curl keeps the connection alive across
    same-host transfers). The first WARMUP requests are discarded. This measures
    MMDS handling time rather than curl/connection overhead.
    """

    total = ITERATIONS + WARMUP

    metrics.set_dimensions(
        {
            "performance_test": "test_mmds_performance",
            **mmds_microvm.dimensions,
        }
    )

    # --- token_generation_time: `total` PUTs over one reused connection ---
    # A single-transfer curl config block is written once (quoted heredoc, so
    # %{time_total} and \n pass through to curl literally), repeated `total` times,
    # and fed to one curl process. Bodies are discarded; we only need per-request
    # timing on stdout as `token_generation_time:<seconds>` lines.
    gen_cmd = (
        "cat > /tmp/mmds_tok_block <<'EOF'\n"
        f'url = "{TOKEN_URL}"\n'
        'request = "PUT"\n'
        'header = "X-metadata-token-ttl-seconds: 60"\n'
        'output = "/dev/null"\n'
        'write-out = "token_generation_time:%{time_total}\\n"\n'
        "next\n"
        "EOF\n"
        f"for _ in $(seq 1 {total}); do cat /tmp/mmds_tok_block; done "
        "> /tmp/mmds_tok_cfg\n"
        "curl -sS -K /tmp/mmds_tok_cfg"
    )
    _, gen_out, gen_err = mmds_microvm.ssh.check_output(gen_cmd)
    assert gen_err == "", f"Error generating MMDS tokens: {gen_err}"

    gen_times = re.findall(r"token_generation_time:([\d.]+)", gen_out)
    assert (
        len(gen_times) == total
    ), f"Expected {total} token timings, got {len(gen_times)}"
    for value in gen_times[WARMUP:]:
        metrics.put_metric("token_generation_time", float(value) * 1000, "Milliseconds")

    # --- request_time: `total` GETs over one reused connection ---
    # Fetch one token (valid 60s, comfortably longer than the batch) and reuse it
    # for every data request. The GET config block substitutes the token (unquoted
    # heredoc expands $TOKEN but leaves \n and %{...} for curl). Each transfer emits
    # the response body followed by `request_time:<seconds>` and a `---` delimiter,
    # so we can both validate the response and read the timing.
    req_cmd = (
        "TOKEN=$(curl -sS -X PUT -H 'X-metadata-token-ttl-seconds: 60' "
        f"{TOKEN_URL})\n"
        "cat > /tmp/mmds_get_block <<EOF\n"
        f'url = "{DATA_URL}"\n'
        'header = "X-metadata-token: $TOKEN"\n'
        'header = "Accept: application/json"\n'
        'write-out = "\\nrequest_time:%{time_total}\\n---\\n"\n'
        "EOF\n"
        f"for i in $(seq 1 {total}); do "
        '[ "$i" -gt 1 ] && echo next; cat /tmp/mmds_get_block; '
        "done > /tmp/mmds_get_cfg\n"
        "curl -sS -K /tmp/mmds_get_cfg"
    )
    _, req_out, req_err = mmds_microvm.ssh.check_output(req_cmd)
    assert req_err == "", f"Error calling MMDS: {req_err}"

    # Each block is "<body>\nrequest_time:<seconds>", separated by "---".
    blocks = [b for b in req_out.split("---\n") if b.strip()]
    assert len(blocks) == total, f"Expected {total} request blocks, got {len(blocks)}"

    for i, block in enumerate(blocks):
        lines = block.strip().split("\n")
        assert len(lines) == 2, f"Unexpected output block: {block!r}"

        response = lines[0].strip()
        assert (
            response == EXPECTED_RESPONSE
        ), f"MMDS request failed. Response: {response}"

        if i < WARMUP:
            continue
        request_time_ms = parse_curl_timing("request_time", lines[1])
        metrics.put_metric("request_time", request_time_ms, "Milliseconds")
