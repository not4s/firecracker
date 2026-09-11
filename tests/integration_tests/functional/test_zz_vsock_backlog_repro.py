# Temporary experiment, not for merging.
"""Guest-initiated vsock connect to a host listener whose accept backlog is full.

While the guest's connect is pending we time a Firecracker API call, which is
served by the VMM thread, and record what the guest's connect() returned.
Stalled VMM: API latency ~HOLD_S, guest times out. Fixed: API latency ~ms,
guest gets ECONNRESET."""

import os
import socket
import threading
import time

from framework.utils_vsock import VSOCK_UDS_PATH, boot_vsock_vm

PORT = 5300
HOLD_S = 3.0
TRIALS = 5


def _full_backlog_listener(path):
    """Listen with backlog 0 and pre-fill it so the next connect cannot be queued."""
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(path)
    srv.listen(0)
    pending = []
    while True:
        cli = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        cli.setblocking(False)
        try:
            cli.connect(path)
            pending.append(cli)
        except BlockingIOError:
            cli.close()
            break
    return srv, pending


def test_repro(uvm):
    vm = boot_vsock_vm(uvm, vcpu_count=1, mem_size_mib=512)
    host_path = os.path.join(vm.path, f"{VSOCK_UDS_PATH}_{PORT}")

    t0 = time.monotonic()
    vm.api.describe.get()
    baseline_ms = round((time.monotonic() - t0) * 1000)

    api_ms, guest = [], []
    for _ in range(TRIALS):
        srv, pending = _full_backlog_listener(host_path)
        jailed = vm.create_jailed_resource(host_path)

        def release():
            for cli in pending:
                cli.close()
            srv.close()

        result = {}

        def guest_connect():
            rc, _, err = vm.ssh.run(
                f"timeout 5 socat -u /dev/null vsock-connect:2:{PORT}"
            )
            result["rc"], result["err"] = rc, (
                err.strip().splitlines()[-1] if err.strip() else ""
            )

        closer = threading.Timer(HOLD_S, release)
        worker = threading.Thread(target=guest_connect)
        worker.start()
        time.sleep(0.3)
        closer.start()

        t0 = time.monotonic()
        vm.api.describe.get()
        api_ms.append(round((time.monotonic() - t0) * 1000))

        closer.join()
        worker.join()
        guest.append(f"rc={result['rc']} {result['err']}")
        # `create_jailed_resource` returns a chroot-relative path; the hard link
        # lives under the jail root.
        os.unlink(os.path.join(vm.chroot(), jailed.lstrip("/")))
        os.unlink(host_path)
        time.sleep(0.5)

    print(
        f"\n[REPRO] api_baseline_ms={baseline_ms} api_ms_during_guest_connect={api_ms} "
        f"guest_connect={guest}"
    )
