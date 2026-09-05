from __future__ import annotations


SSH_BATCH_OPTIONS = (
    "-o",
    "BatchMode=yes",
    "-o",
    "ConnectTimeout=10",
    "-o",
    "StrictHostKeyChecking=accept-new",
    "-o",
    "UserKnownHostsFile=/tmp/cnc-ssh-known-hosts",
    "-o",
    "ControlMaster=auto",
    "-o",
    "ControlPersist=5m",
    "-o",
    "ControlPath=/tmp/cnc-ssh-%C",
)
