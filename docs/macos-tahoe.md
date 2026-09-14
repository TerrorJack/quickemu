# Unattended macOS Tahoe installation

Quickemu can install macOS Tahoe on an x86_64 host into an x86_64 guest, create
an administrator account and enable SSH without interacting with the installer.

```shell
quickget --unattended macos tahoe
quickemu --vm macos-tahoe.conf --display none
```

Use an AVX2-capable CPU with hardware virtualization enabled. The generated
configuration allocates 8 GiB RAM and a 128 GiB virtual disk, with 1 CPU core on
AMD hosts and 4 on other hosts. Internet
access is needed both to download the recovery image and for the guest installer
to download macOS. Installation can take several hours, with several automatic
reboots. Keep the VM running while the installer is working.
Use Quickemu's default network with SSH forwarding. Installation requires
persistent disk writes, so `--status-quo` is unavailable during installation.

The additional host tools are Python 3, OpenSSH, 7-Zip (`7zz` or `7z`), mtools,
util-linux and Tesseract with English language data. They are included in the
Nix package and development shell. From a checkout, run `nix develop` and use
the generated `.direnv/bin/quickemu` so firmware paths resolve correctly.

The unattended option is explicit: `quickget macos tahoe` prepares a VM for
manual installation. Existing configuration files are preserved. To opt in for
an existing, uninstalled Tahoe VM, add `macos_unattended="on"` to its configuration.
On AMD, also set `cpu_cores=1` when opting in this way.

The conservative AMD core count follows Dockur's compatibility guidance. On the
tested Ryzen 9 7950X3D with Tahoe 26.6.2 and QEMU 11.1.1, four-core guests could
hang during native shutdown, while a one-core guest shut down cleanly. Increasing
`cpu_cores` can improve parallel workloads, but may reintroduce shutdown or reboot
hangs. Verify those operations after changing it. Manual Tahoe configurations
retain the four-core default.

## Disk and boot files

The main disk is `macos-tahoe/disk.img` with `disk_format="raw"`. The file is sparse,
so the 128 GiB virtual capacity is not allocated immediately. On Btrfs, the disk
must have the `NOCOW` attribute before any data is written. Quickemu prepares and
checks this automatically. To inspect it:

```shell
lsattr macos-tahoe/disk.img
qemu-img info macos-tahoe/disk.img
```

On Btrfs, `lsattr` must show `C`. Other filesystems do not implement this Btrfs
attribute. Do not replace an existing populated disk to change its attributes.

Quickget verifies Apple's recovery image and downloads pinned OpenCore,
VMHide and firmware assets with SHA256 verification. It builds `OpenCore.img`
without requiring privileged mounts. The VM uses a `MacPro7,1` identity, saved
in `macos-identity.json`. The identity, OpenCore image and writable OVMF variables
are reused on later invocations. Keep these files with the VM disk.

The macOS firmware runs without System Management Mode (SMM). Quickemu keeps
its variable flash writable and applies QEMU's secure flash restriction only
when SMM is enabled. Applying that restriction with SMM disabled blocks NVRAM
writes and loses the installer's saved boot choices. Firmware code remains
read-only.

OpenCore routes the guest's boot variables with `RequestBootVarRouting=true`,
as in the LongQT configuration. This preserves
the installer's next boot choice through OVMF, so automatic reboots can select
the staged macOS installer and then the installed system while the recovery
image remains attached. `LauncherOption` remains `Disabled` because QEMU's
boot priority already selects OpenCore. The helper shows a text boot picker
while installation is pending and hides it after the installation is verified.

The installer uses a small writable seed disk at
`macos-tahoe/unattended/seed.img` to carry its scripts and record guest progress.
Keep the entire `unattended` directory with the VM while installation is running.

## Account configuration

The default guest account is `quickemu`, with a generated password and SSH key.
The account is an administrator and can use passwordless `sudo`. Automatic login
is enabled for the test VM. The username and password are stored in
`macos-tahoe/unattended/credentials.json`; the SSH private key is
`macos-tahoe/unattended/id_ed25519`. Protect the `unattended` directory like an
SSH private key.

After installation, connect using the SSH port printed by Quickemu (normally
22220):

```shell
ssh -i macos-tahoe/unattended/id_ed25519 -p 22220 quickemu@127.0.0.1
```

## Progress and diagnosis

Quickemu waits in the foreground while the installation runs. The helper reads
guest progress from the seed disk and checks the actual guest over SSH before
reporting completion. When needed, it performs one final reboot to activate
automatic login, then verifies that the new boot has logged in the generated
account. `macos-tahoe/unattended/verification.txt` records the
installed macOS version and guest architecture. The `complete` marker is written
only after this verification succeeds.

The latest guest screenshot and its recognized text are saved as
`macos-tahoe/unattended/screen.png` and `screen.txt`. Open those files while the
observer is running. After the observer stops, you can capture another screenshot
through QEMU's QMP socket:

```shell
quickemu-macos screenshot --dir macos-tahoe \
  --qmp macos-tahoe/macos-tahoe-qmp.socket
```

The guest's current status and recovery log can be read without mounting its disk:

```shell
MTOOLS_SKIP_CHECK=1 mtype -i macos-tahoe/unattended/seed.img@@1048576 ::/state/status
MTOOLS_SKIP_CHECK=1 mtype -i macos-tahoe/unattended/seed.img@@1048576 ::/state/recovery.log
MTOOLS_SKIP_CHECK=1 mtype -i macos-tahoe/unattended/seed.img@@1048576 ::/state/install.log
```

`MTOOLS_SKIP_CHECK` permits reading the FAT volume while macOS has it mounted.
It does not change the guest disk. First-boot diagnostics are saved in
`state/firstboot.log`; observer SSH failures are recorded in `ssh-error.txt`.

The default observation timeout is four hours. A timeout leaves QEMU running and
preserves the installation state. Set `macos_install_timeout` in the VM
configuration to change this timeout in seconds. To continue observing that VM
for another four hours, use its QMP socket and the SSH port printed when it started:

```shell
quickemu-macos run --dir macos-tahoe \
  --qmp macos-tahoe/macos-tahoe-qmp.socket --ssh-port 22220 --timeout 14400
```

Some x86 Recovery kernels stall after announcing their installer restart. After
that explicit request, three minutes of unchanged Recovery display and disk I/O
allow the helper to complete the restart once. An observed native reboot prevents
this fallback. Handled restart markers are saved in `unattended/reboot-resets.json`.

If QEMU has stopped, start it again with the same configuration. Preserve the
system disk, OVMF variables and `unattended` directory across restarts. A new
installation requires a blank raw disk; an existing installed disk is rejected
when creating a new unattended seed. Do not delete installation state to bypass
that check.

The seed is bound to the original disk file through `unattended/target.json`.
Keep the disk at its original path while unattended control is enabled; the
helper refuses replacement files. An interrupted
Recovery install resumes only when its saved APFS container matches that disk;
the resume path does not format it again.

## Sources

The QEMU and OpenCore configuration follows
[dockur/macos](https://github.com/dockur/macos) and
[LongQT OpenCore](https://github.com/LongQT-sea/OpenCore-ISO).
The installation approach is informed by Cirrus Labs'
[vanilla Tahoe template](https://github.com/cirruslabs/macos-image-templates/blob/main/templates/vanilla-tahoe.pkr.hcl).
Boot variable routing follows OpenCore's documented boot algorithm and
[`RequestBootVarRouting` and `LauncherOption` settings](https://github.com/acidanthera/OpenCorePkg/blob/1.0.7/Docs/Configuration.pdf).
