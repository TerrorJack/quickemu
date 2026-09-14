#!/usr/bin/env python3
"""Host-side safety checks; run with nix develop -c python3 -m unittest discover -s tests."""

import gzip
import hashlib
import json
import os
from pathlib import Path
import runpy
import stat
import struct
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
import xml.etree.ElementTree as ET
import zlib


ROOT = Path(__file__).resolve().parents[1]
HELPER = runpy.run_path(str(ROOT / "quickemu-macos"))
MIB = 1024 * 1024


def command(*args, **kwargs):
    return subprocess.check_output(args, **kwargs)


def xar_files(data):
    """Read the archive using its declared offsets and verify its checksums."""
    magic, size, version, compressed_size, xml_size, algorithm = struct.unpack(
        ">IHHQQI", data[:28]
    )
    if (magic, size, version, algorithm) != (0x78617221, 28, 1, 1):
        raise ValueError("invalid XAR header")
    compressed = data[size:size + compressed_size]
    xml = zlib.decompress(compressed)
    if len(xml) != xml_size:
        raise ValueError("invalid XAR TOC length")
    toc = ET.fromstring(xml).find("toc")
    heap = data[size + compressed_size:]
    checksum = toc.find("checksum")
    offset, length = int(checksum.findtext("offset")), int(checksum.findtext("size"))
    if heap[offset:offset + length] != hashlib.sha1(compressed).digest():
        raise ValueError("invalid XAR TOC checksum")
    files = {}

    def visit(parent, prefix=""):
        for entry in parent.findall("file"):
            name = prefix + entry.findtext("name")
            if entry.findtext("type") == "directory":
                visit(entry, name + "/")
                continue
            record = entry.find("data")
            offset, length = int(record.findtext("offset")), int(record.findtext("length"))
            content = heap[offset:offset + length]
            if hashlib.sha1(content).hexdigest() != record.findtext("extracted-checksum"):
                raise ValueError("invalid XAR entry checksum")
            files[name] = content

    visit(toc)
    return files


def cpio_files(data):
    archive, offset, files = gzip.decompress(data), 0, {}
    while archive[offset:offset + 6] == b"070707":
        header = archive[offset:offset + 76]
        mode = int(header[18:24], 8)
        name_size, file_size = int(header[59:65], 8), int(header[65:76], 8)
        offset += 76
        name = archive[offset:offset + name_size - 1].decode()
        offset += name_size
        if name == "TRAILER!!!":
            return files
        files[name] = (mode, archive[offset:offset + file_size])
        offset += file_size
    raise ValueError("missing CPIO trailer")


class HostTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="quickemu-macos-test-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.disk = self.directory / "disk.raw"
        with self.disk.open("wb") as stream:
            stream.truncate(32 * MIB)
        self.state = self.directory / "unattended"
        self.args = SimpleNamespace(disk=str(self.disk), dir=str(self.directory))


class BlankDiskTests(HostTest):
    def test_sparse_and_allocated_zero_disks_are_blank(self):
        self.assertTrue(HELPER["blank_disk"](self.disk))
        with self.disk.open("r+b") as stream:
            stream.seek(17 * MIB)
            stream.write(bytes(MIB))
        self.assertTrue(HELPER["blank_disk"](self.disk))

    def test_nonzero_data_beyond_first_megabyte_is_not_blank(self):
        for offset in (9 * MIB + 7, 32 * MIB - 1):
            with self.subTest(offset=offset):
                with self.disk.open("r+b") as stream:
                    stream.seek(offset)
                    stream.write(b"\x01")
                self.assertFalse(HELPER["blank_disk"](self.disk))
                with self.disk.open("r+b") as stream:
                    stream.seek(offset)
                    stream.write(b"\0")

    def test_prepare_refuses_used_disk_without_creating_installation_media(self):
        with self.disk.open("r+b") as stream:
            stream.seek(24 * MIB)
            stream.write(b"existing filesystem data")
        with self.assertRaisesRegex(RuntimeError, "blank raw system disk"):
            HELPER["prepare"](self.args, self.state)
        self.assertFalse(self.state.exists())
        with self.disk.open("rb") as stream:
            stream.seek(24 * MIB)
            self.assertEqual(stream.read(24), b"existing filesystem data")


class BootEntryTests(unittest.TestCase):
    # Actual Recovery OCR: macOS and System are imperfectly recognized, and
    # the highlighted entry's marker is included in the extracted text.
    recovery_menu = (
        "OpenCore Boot Menu (REL-105-2023-07-07) = 1. mac05 Base Systen "
        "2. mac05 Installer |Shutdown| |Restart| Choose the Operating System:"
    )
    installed_menu = recovery_menu.replace(
        " |Shutdown|", " 3. Macintosh HD |Shutdown|"
    )

    def test_incomplete_preparation_selects_recovery_despite_installer_entry(self):
        for status in ("", "recovery-ready", "installing"):
            with self.subTest(status=status):
                self.assertEqual(HELPER["boot_entry"](self.recovery_menu, status), "1")

    def test_prepared_install_selects_installer(self):
        self.assertEqual(HELPER["boot_entry"](self.recovery_menu, "prepared"), "2")

    def test_provisioned_system_selects_macos_even_with_installer_present(self):
        for status in ("provisioned", "configuring", "complete"):
            with self.subTest(status=status):
                self.assertEqual(HELPER["boot_entry"](self.installed_menu, status), "3")

    def test_non_opencore_text_does_not_send_boot_selection(self):
        text = "1. mac05 Base Systen 2. mac05 Installer 3. Macintosh HD"
        for status in ("", "prepared", "provisioned"):
            with self.subTest(status=status):
                self.assertIsNone(HELPER["boot_entry"](text, status))

    def test_missing_target_does_not_select_unrelated_entry(self):
        cases = (
            ("OpenCore Boot Menu 1. mac05 Installer", ""),
            ("OpenCore Boot Menu 1. mac05 Base Systen", "prepared"),
            (self.recovery_menu, "provisioned"),
            (self.recovery_menu, "configuring"),
            ("OpenCore Boot Menu 1. UEFI Shell", "prepared"),
        )
        for menu, status in cases:
            with self.subTest(menu=menu, status=status):
                self.assertIsNone(HELPER["boot_entry"](menu, status))


class RecoveryRebootTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="quickemu-reboot-test-")
        self.addCleanup(temporary.cleanup)
        self.state = Path(temporary.name)
        self.watcher = HELPER["RecoveryReboot"](self.state)
        self.signature = ("recovery-frame", (("system-disk", (100, 200, 3, 0)),))

    def test_missing_marker_never_authorizes_reset(self):
        for now, marker in ((0, None), (180, ""), (3600, None)):
            with self.subTest(now=now, marker=marker):
                self.assertFalse(self.watcher.ready(marker, self.signature, 0, now))

    def test_unchanged_marker_and_activity_require_full_grace_period(self):
        self.assertFalse(self.watcher.ready("stage-one", self.signature, 0, 10))
        self.assertFalse(self.watcher.ready("stage-one", self.signature, 0, 189.999))
        self.assertTrue(self.watcher.ready("stage-one", self.signature, 0, 190))

    def test_new_marker_starts_a_new_grace_period(self):
        self.assertFalse(self.watcher.ready("stage-one", self.signature, 0, 0))
        self.assertFalse(self.watcher.ready("stage-two", self.signature, 0, 179))
        self.assertFalse(self.watcher.ready("stage-two", self.signature, 0, 180))
        self.assertTrue(self.watcher.ready("stage-two", self.signature, 0, 359))

    def test_missing_marker_interrupts_the_grace_period(self):
        self.assertFalse(self.watcher.ready("stage-one", self.signature, 0, 0))
        self.assertFalse(self.watcher.ready(None, None, 0, 179))
        self.assertFalse(self.watcher.ready("stage-one", self.signature, 0, 180))
        self.assertFalse(self.watcher.ready("stage-one", self.signature, 0, 359))
        self.assertTrue(self.watcher.ready("stage-one", self.signature, 0, 360))

    def test_frame_and_disk_activity_each_restart_the_grace_period(self):
        changed_frame = ("updated-recovery-frame", self.signature[1])
        changed_io = (changed_frame[0], (("system-disk", (100, 201, 3, 0)),))
        self.assertFalse(self.watcher.ready("stage-one", self.signature, 0, 0))
        self.assertFalse(self.watcher.ready("stage-one", changed_frame, 0, 179))
        self.assertFalse(self.watcher.ready("stage-one", changed_frame, 0, 180))
        self.assertFalse(self.watcher.ready("stage-one", changed_io, 0, 358))
        self.assertFalse(self.watcher.ready("stage-one", changed_io, 0, 359))
        self.assertTrue(self.watcher.ready("stage-one", changed_io, 0, 538))

    def test_native_reset_suppresses_fallback_for_same_marker(self):
        self.assertFalse(self.watcher.ready("stage-one", self.signature, 4, 0))
        self.assertFalse(self.watcher.ready("stage-one", self.signature, 5, 179))
        self.assertFalse(self.watcher.ready("stage-one", self.signature, 5, 3600))
        # A briefly absent OCR marker must not re-arm a completed native reboot.
        self.assertFalse(self.watcher.ready(None, None, 5, 3601))
        self.assertFalse(self.watcher.ready("stage-one", self.signature, 5, 3602))
        self.assertFalse(self.watcher.ready("stage-one", self.signature, 5, 7200))

    def test_native_reset_while_marker_is_absent_does_not_rearm_old_stage(self):
        self.assertFalse(self.watcher.ready("stage-one", self.signature, 4, 0))
        self.assertFalse(self.watcher.ready(None, None, 5, 179))
        self.assertFalse(self.watcher.ready("stage-one", self.signature, 5, 180))
        self.assertFalse(self.watcher.ready("stage-one", self.signature, 5, 360))

    def test_record_prevents_repeat_reset_after_watcher_restart(self):
        self.assertFalse(self.watcher.ready("stage-one", self.signature, 0, 0))
        self.assertTrue(self.watcher.ready("stage-one", self.signature, 0, 180))
        self.watcher.record()
        self.assertFalse(self.watcher.ready("stage-one", self.signature, 0, 360))
        restarted = HELPER["RecoveryReboot"](self.state)
        self.assertFalse(restarted.ready("stage-one", self.signature, 1, 7200))
        self.assertFalse(restarted.ready("stage-one", self.signature, 1, 7380))
        self.assertFalse(restarted.ready("stage-two", self.signature, 1, 7400))
        self.assertTrue(restarted.ready("stage-two", self.signature, 1, 7580))


class SshVerificationTests(unittest.TestCase):
    old_boot = "{ sec = 1789092000, usec = 0 } Fri Sep 11 04:00:00 2026"
    new_boot = "{ sec = 1789092300, usec = 0 } Fri Sep 11 04:05:00 2026"
    restart_command = "sudo -n /sbin/shutdown -r now"

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="quickemu-ssh-test-")
        self.addCleanup(temporary.cleanup)
        self.state = Path(temporary.name) / "unattended"
        self.state.mkdir(mode=0o700)
        credentials = self.state / "credentials.json"
        credentials.write_text(json.dumps({"username": "quickemu", "password": "test-only"}))
        credentials.chmod(0o600)
        self.args = SimpleNamespace(ssh_port=22220)
        self.request = self.state / "login-reboot.json"

    def response(self, console="root", boot=old_boot, returncode=0):
        output = "ProductName:\tmacOS\nProductVersion:\t26.1\nBuildVersion:\t25B78\nx86_64\n"
        if console is not None:
            output += f"QUICKEMU_CONSOLE={console}\n"
        if boot is not None:
            output += f"QUICKEMU_BOOT={boot}\n"
        return subprocess.CompletedProcess(["ssh"], returncode, output, "")

    def verify(self, response):
        with mock.patch.object(subprocess, "run", return_value=response) as ssh:
            completed = HELPER["ssh_verify"](self.args, self.state)
        return completed, ssh

    def test_root_console_requests_one_reboot_after_persisting_boot_identity(self):
        def remote(command, **kwargs):
            if command[-1] == self.restart_command:
                self.assertEqual(json.loads(self.request.read_text()), {"boot": self.old_boot})
            return self.response()

        with mock.patch.object(subprocess, "run", side_effect=remote) as ssh:
            self.assertFalse(HELPER["ssh_verify"](self.args, self.state))
        self.assertEqual(ssh.call_count, 2)
        self.assertEqual(ssh.call_args_list[1].args[0][-1], self.restart_command)
        self.assertEqual(json.loads(self.request.read_text()), {"boot": self.old_boot})
        self.assertFalse((self.state / "complete").exists())

    def test_repeated_same_boot_never_reboots_again_or_completes(self):
        self.assertFalse(self.verify(self.response())[0])
        original = self.request.read_bytes()
        for attempt in range(3):
            with self.subTest(attempt=attempt):
                completed, ssh = self.verify(self.response())
                self.assertFalse(completed)
                self.assertEqual(ssh.call_count, 1)
                self.assertEqual(self.request.read_bytes(), original)
                self.assertFalse((self.state / "complete").exists())

    def test_new_boot_with_user_console_completes_without_another_reboot(self):
        self.assertFalse(self.verify(self.response())[0])
        response = self.response(console="quickemu", boot=self.new_boot)
        completed, ssh = self.verify(response)
        self.assertTrue(completed)
        self.assertEqual(ssh.call_count, 1)
        self.assertTrue((self.state / "complete").is_file())
        self.assertEqual((self.state / "verification.txt").read_text(), response.stdout)

    def test_same_boot_with_user_console_still_waits_for_requested_reboot(self):
        self.assertFalse(self.verify(self.response())[0])
        completed, ssh = self.verify(self.response(console="quickemu"))
        self.assertFalse(completed)
        self.assertEqual(ssh.call_count, 1)
        self.assertFalse((self.state / "complete").exists())

    def test_missing_or_empty_login_markers_never_request_a_reboot(self):
        for console, boot in ((None, self.old_boot), ("root", None),
                              ("", self.old_boot), ("root", ""), (None, None)):
            with self.subTest(console=console, boot=boot):
                completed, ssh = self.verify(self.response(console=console, boot=boot))
                self.assertFalse(completed)
                self.assertEqual(ssh.call_count, 1)
                self.assertFalse(self.request.exists())
                self.assertFalse((self.state / "complete").exists())

    def test_already_logged_in_completes_without_requesting_reboot(self):
        completed, ssh = self.verify(self.response(console="quickemu"))
        self.assertTrue(completed)
        self.assertEqual(ssh.call_count, 1)
        self.assertFalse(self.request.exists())
        self.assertTrue((self.state / "complete").is_file())

    def test_failed_guest_completion_check_never_requests_a_reboot(self):
        completed, ssh = self.verify(self.response(returncode=1))
        self.assertFalse(completed)
        self.assertEqual(ssh.call_count, 1)
        self.assertFalse(self.request.exists())
        self.assertFalse((self.state / "complete").exists())


class SeedTests(HostTest):
    def setUp(self):
        super().setUp()
        HELPER["prepare"](self.args, self.state)

    def read_seed(self, path):
        return command("mtype", "-i", HELPER["seed_spec"](self.state), "::/" + path,
                       env={**os.environ, "MTOOLS_SKIP_CHECK": "1"})

    def test_partition_and_fat_geometry_fit_media(self):
        seed = self.state / "seed.img"
        layout = json.loads(command("sfdisk", "--json", str(seed)))["partitiontable"]
        self.assertEqual(layout["label"], "gpt")
        self.assertEqual(len(layout["partitions"]), 1)
        partition = layout["partitions"][0]
        sector_size = layout["sectorsize"]
        self.assertEqual(partition["start"] * sector_size, MIB)
        with seed.open("rb") as stream:
            stream.seek(MIB)
            boot = stream.read(512)
        self.assertEqual(boot[510:512], b"\x55\xaa")
        bytes_per_sector = struct.unpack_from("<H", boot, 11)[0]
        fat_sectors = struct.unpack_from("<H", boot, 19)[0] or struct.unpack_from("<I", boot, 32)[0]
        self.assertEqual(bytes_per_sector, sector_size)
        self.assertEqual(fat_sectors, partition["size"])
        self.assertLessEqual((partition["start"] + fat_sectors) * sector_size, seed.stat().st_size)

    def test_seed_package_has_bootstrap_scripts_and_generated_credentials(self):
        package = xar_files(self.read_seed("setup.pkg"))
        distribution = ET.fromstring(package["Distribution"])
        reference = next(node for node in distribution.findall("pkg-ref") if node.text)
        component = reference.text.removeprefix("#")
        package_info = ET.fromstring(package[component + "/PackageInfo"])
        scripts = cpio_files(package[component + "/Scripts"])
        postinstall = package_info.find("scripts/postinstall").get("file")
        self.assertTrue(scripts[postinstall][0] & stat.S_IXUSR)
        self.assertIn(b"org.quickemu.firstboot", scripts[postinstall][1])
        self.assertTrue(scripts["./firstboot.sh"][0] & stat.S_IXUSR)
        settings = json.loads((self.state / "credentials.json").read_text())
        self.assertIn(settings["username"].encode(), scripts["./settings.sh"][1])
        self.assertIn(settings["password"].encode(), scripts["./settings.sh"][1])
        self.assertEqual(scripts["./authorized_keys"][1], (self.state / "id_ed25519.pub").read_bytes())
        self.assertIn(b"startosinstall", self.read_seed("recovery.sh"))

    def test_private_state_and_credentials_permissions(self):
        for path, expected in ((self.state, 0o700),
                               (self.state / "credentials.json", 0o600),
                               (self.state / "id_ed25519", 0o600)):
            with self.subTest(path=path.name):
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), expected)

    def test_rerun_preserves_credentials_keys_and_guest_state(self):
        marker = self.directory / "status"
        marker.write_text("installing\n")
        command("mcopy", "-i", HELPER["seed_spec"](self.state), str(marker), "::/state/status")
        paths = [self.state / name for name in ("credentials.json", "id_ed25519", "id_ed25519.pub", "seed.img", "target.json")]
        before = {path: (hashlib.sha256(path.read_bytes()).digest(), path.stat().st_mtime_ns) for path in paths}
        HELPER["prepare"](self.args, self.state)
        for path in paths:
            self.assertEqual((hashlib.sha256(path.read_bytes()).digest(), path.stat().st_mtime_ns), before[path])
        self.assertEqual(HELPER["seed_read"](self.state, "status"), "installing")

    def assert_target_rejected(self):
        # Neither an existing seed nor a stale completion marker authorizes a different disk.
        (self.state / "complete").write_text("previous installation\n")
        for action in ("prepare", "observe"):
            with self.subTest(action=action):
                with self.assertRaisesRegex(RuntimeError, "system disk differs"):
                    HELPER[action](self.args, self.state)

    def test_existing_media_rejects_different_disk(self):
        other = self.directory / "other.raw"
        with other.open("wb") as stream:
            stream.truncate(self.disk.stat().st_size)
        self.args.disk = str(other)
        self.assert_target_rejected()

    def test_existing_media_rejects_disk_replaced_at_same_path(self):
        self.disk.rename(self.directory / "original.raw")
        with self.disk.open("wb") as stream:
            stream.truncate(32 * MIB)
        self.assert_target_rejected()

    def test_status_read_works_while_fat_clean_shutdown_bit_is_clear(self):
        status = self.directory / "status"
        status.write_text("installing\n")
        command("mcopy", "-i", HELPER["seed_spec"](self.state), str(status), "::/state/status")
        with (self.state / "seed.img").open("r+b") as stream:
            stream.seek(MIB)
            boot = stream.read(512)
            sector_size = struct.unpack_from("<H", boot, 11)[0]
            reserved = struct.unpack_from("<H", boot, 14)[0]
            fat_size = struct.unpack_from("<H", boot, 22)[0]
            if fat_size:
                entry_size, format_, clean_bit = 2, "<H", 0x8000
            else:
                fat_size = struct.unpack_from("<I", boot, 36)[0]
                entry_size, format_, clean_bit = 4, "<I", 0x08000000
            for fat_index in range(boot[16]):
                offset = MIB + (reserved + fat_index * fat_size) * sector_size + entry_size
                stream.seek(offset)
                entry = struct.unpack(format_, stream.read(entry_size))[0]
                stream.seek(offset)
                stream.write(struct.pack(format_, entry & ~clean_bit))
        environment = {name: value for name, value in os.environ.items() if name != "MTOOLS_SKIP_CHECK"}
        ordinary_read = subprocess.run(
            ["mtype", "-i", HELPER["seed_spec"](self.state), "::/state/status"],
            capture_output=True, env=environment,
        )
        self.assertNotEqual(ordinary_read.returncode, 0)
        self.assertEqual(HELPER["seed_read"](self.state, "status"), "installing")
        self.assertEqual(HELPER["seed_read"](self.state, "missing"), "")


if __name__ == "__main__":
    unittest.main()
