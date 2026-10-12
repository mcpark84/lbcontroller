#!/usr/bin/env python3
"""W2: render a new kube-apiserver static-pod manifest with the authn webhook
and audit flags inserted. Never touches the source file.

  w2-render-apiserver.py SRC DST

Inserts, preserving the original text layout so the diff is reviewable:
  * flags after the last `- --` entry of the kube-apiserver command
  * two volumeMounts after the `volumeMounts:` line
  * two volumes after the `volumes:` line
Then re-parses DST with PyYAML and checks the referenced host files exist.
"""
import os
import re
import sys

FLAGS = [
    "--authentication-token-webhook-config-file=/etc/kubernetes/webhook/webhook-kubeconfig.yaml",
    "--authentication-token-webhook-version=v1",
    "--authentication-token-webhook-cache-ttl=0s",
    "--audit-policy-file=/etc/kubernetes/webhook/audit-policy.yaml",
    "--audit-log-path=/var/log/kubernetes/audit/audit.log",
    "--audit-log-maxage=3",
    "--audit-log-maxbackup=3",
    "--audit-log-maxsize=50",
]
MOUNTS = [
    ("poc-webhook", "/etc/kubernetes/webhook", True),
    ("poc-audit", "/var/log/kubernetes/audit", False),
]
VOLUMES = [
    ("poc-webhook", "/etc/kubernetes/webhook", "Directory"),
    ("poc-audit", "/var/log/kubernetes/audit", "DirectoryOrCreate"),
]
MUST_EXIST = [
    "/etc/kubernetes/webhook/webhook-kubeconfig.yaml",
    "/etc/kubernetes/webhook/ca.crt",
    "/etc/kubernetes/webhook/audit-policy.yaml",
]


def die(msg):
    print("[w2] ERROR:", msg, file=sys.stderr)
    sys.exit(1)


def main():
    if len(sys.argv) != 3:
        die(__doc__)
    src, dst = sys.argv[1], sys.argv[2]
    lines = open(src).read().splitlines(keepends=True)
    text = "".join(lines)
    for f in FLAGS:
        if f.split("=")[0] in text:
            die(f"source already contains {f.split('=')[0]}")

    # 1) flags: after last line matching "<indent>- --..."
    flag_idx = [i for i, l in enumerate(lines) if re.match(r"^\s+- --", l)]
    if not flag_idx:
        die("no '- --' flag lines found")
    last = flag_idx[-1]
    indent = re.match(r"^(\s+)- --", lines[last]).group(1)
    ins_flags = [f"{indent}- {f}\n" for f in FLAGS]

    # 2) volumeMounts
    vm_idx = [i for i, l in enumerate(lines) if re.match(r"^\s+volumeMounts:\s*$", l)]
    if len(vm_idx) != 1:
        die(f"expected exactly one volumeMounts:, found {len(vm_idx)}")
    vm = vm_idx[0]
    m = re.match(r"^(\s+)- mountPath:", lines[vm + 1])
    if not m:
        die("volumeMounts: not followed by '- mountPath:'")
    vi = m.group(1)
    ins_mounts = []
    for name, path, ro in MOUNTS:
        ins_mounts.append(f"{vi}- mountPath: {path}\n{vi}  name: {name}\n")
        if ro:
            ins_mounts[-1] += f"{vi}  readOnly: true\n"

    # 3) volumes
    v_idx = [i for i, l in enumerate(lines) if re.match(r"^\s+volumes:\s*$", l)]
    if len(v_idx) != 1:
        die(f"expected exactly one volumes:, found {len(v_idx)}")
    vo = v_idx[0]
    m = re.match(r"^(\s+)- hostPath:", lines[vo + 1])
    if not m:
        die("volumes: not followed by '- hostPath:'")
    oi = m.group(1)
    ins_vols = [f"{oi}- hostPath:\n{oi}    path: {path}\n{oi}    type: {typ}\n{oi}  name: {name}\n"
                for name, path, typ in VOLUMES]

    # apply bottom-up so indices stay valid
    edits = sorted([(last + 1, ins_flags), (vm + 1, ins_mounts), (vo + 1, ins_vols)], reverse=True)
    out = list(lines)
    for pos, block in edits:
        out[pos:pos] = block
    open(dst, "w").write("".join(out))
    print(f"[w2] wrote {dst} (+{len(out) - len(lines)} lines)")

    # validation
    try:
        import yaml
    except ImportError:
        die("PyYAML missing; cannot validate. apt install python3-yaml")
    doc = yaml.safe_load(open(dst))
    c = doc["spec"]["containers"][0]
    cmd = c["command"]
    for f in FLAGS:
        if f not in cmd:
            die(f"flag missing after parse: {f}")
    names = {v["name"] for v in doc["spec"]["volumes"]}
    mnames = {m_["name"] for m_ in c["volumeMounts"]}
    for name, _, _ in VOLUMES:
        if name not in names or name not in mnames:
            die(f"volume/mount pair incomplete: {name}")
    if not doc["spec"].get("hostNetwork"):
        die("hostNetwork is not true; 127.0.0.1 webhook unreachable")
    print(f"[w2] yaml ok: {len(cmd)} command args, {len(names)} volumes, {len(mnames)} mounts, hostNetwork=True")
    missing = [p for p in MUST_EXIST if not os.path.exists(p)]
    if missing:
        print("[w2] WARN missing host files (ok on a workstation, fatal on the node):", missing)
    else:
        print("[w2] host files present:", MUST_EXIST)


if __name__ == "__main__":
    main()
