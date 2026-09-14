#!/usr/bin/env python3
"""Format compatibility and destructive-operation guards for the guest seed."""

import gzip
import copy
import hashlib
import os
from pathlib import Path
import plistlib
import runpy
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zlib


SOURCE = Path(__file__).resolve().parent
BUILDER = runpy.run_path(str(SOURCE / "build.py"))
PLIST_READER = '''import plistlib, sys
value = plistlib.load(open(sys.argv[1], "rb"))
try:
    for part in sys.argv[2].strip(":").split(":"):
        value = value[int(part)] if isinstance(value, list) else value[part]
except (KeyError, IndexError, ValueError):
    sys.exit(1)
print(str(value).lower() if isinstance(value, bool) else value)
'''


def unpack_xar(data):
    magic, header_size, version, compressed_size, xml_size, algorithm = struct.unpack(
        ">IHHQQI", data[:28]
    )
    assert (magic, header_size, version, algorithm) == (0x78617221, 28, 1, 1)
    compressed = data[header_size : header_size + compressed_size]
    xml = zlib.decompress(compressed)
    assert len(xml) == xml_size
    heap = data[header_size + compressed_size :]
    assert hashlib.sha1(compressed).digest() == heap[:20]
    toc = ET.fromstring(xml).find("toc")
    result = {}

    def walk(parent, path=""):
        for node in parent.findall("file"):
            filename = path + node.findtext("name")
            if node.findtext("type") == "directory":
                walk(node, filename + "/")
                continue
            entry = node.find("data")
            offset, length = int(entry.findtext("offset")), int(entry.findtext("length"))
            content = heap[offset : offset + length]
            assert len(content) == int(entry.findtext("size"))
            assert entry.find("encoding").attrib["style"] == "application/octet-stream"
            for kind in ("extracted-checksum", "archived-checksum"):
                assert hashlib.sha1(content).hexdigest() == entry.findtext(kind)
            result[filename] = content

    walk(toc)
    return result


def unpack_cpio(data):
    data = gzip.decompress(data)
    files = {}
    cursor = 0
    while True:
        header = data[cursor : cursor + 76]
        assert header[:6] == b"070707"
        mode = int(header[18:24], 8)
        name_size, size = int(header[59:65], 8), int(header[65:76], 8)
        cursor += 76
        name = data[cursor : cursor + name_size - 1].decode()
        assert data[cursor + name_size - 1] == 0
        cursor += name_size
        content = data[cursor : cursor + size]
        assert len(content) == size
        cursor += size
        if name == "TRAILER!!!":
            break
        files[name] = (content, mode)
    return files


class PackageTests(unittest.TestCase):
    def test_product_archive_contains_executable_scripts_and_private_settings(self):
        password = "spaces ' quotes $() `backticks` \\ and unicode é"
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "seed"
            BUILDER["build"](output, "vm_admin", password, "")
            entries = unpack_xar((output / "setup.pkg").read_bytes())
            distribution = ET.fromstring(entries["Distribution"])
            self.assertEqual(
                distribution.find("pkg-ref").text, "#quickemu-component.pkg"
            )
            self.assertEqual(distribution.find("pkg-ref").attrib["onConclusion"], "RequireRestart")
            info = ET.fromstring(entries["quickemu-component.pkg/PackageInfo"])
            self.assertEqual(info.attrib["postinstall-action"], "restart")
            self.assertEqual(info.find("scripts/postinstall").attrib["file"], "./postinstall")
            scripts = unpack_cpio(entries["quickemu-component.pkg/Scripts"])
            self.assertEqual(scripts["./postinstall"][1], 0o100755)
            self.assertEqual(scripts["./firstboot.sh"][1], 0o100755)
            self.assertEqual(scripts["./settings.sh"][1], 0o100600)
            self.assertEqual(
                plistlib.loads(scripts["./com.apple.keyboardtype.plist"][0]),
                {"keyboardtype": {"65535-1452-0": 40, "1-1575-0": 40}},
            )
            self.assertEqual(
                scripts["./firstboot.sh"][0], (SOURCE / "firstboot.sh").read_bytes()
            )
            settings = Path(temporary) / "settings"
            settings.write_bytes(scripts["./settings.sh"][0])
            result = subprocess.run(
                [shutil.which("bash"), "-c", 'source "$1"; printf "%s" "$QUICKEMU_PASSWORD"', "test", str(settings)],
                check=True, stdout=subprocess.PIPE,
            )
            self.assertEqual(result.stdout.decode(), password)
            key = bytes.fromhex("7d895223d2bcddeaa3b91f")
            decoded = bytes(
                byte ^ key[index % len(key)]
                for index, byte in enumerate(scripts["./kcpassword"][0])
            )
            self.assertEqual(decoded.rstrip(b"\0").decode(), password)
            self.assertEqual(output.stat().st_mode & 0o777, 0o700)
            self.assertEqual((output / "setup.pkg").stat().st_mode & 0o777, 0o600)

    def test_invalid_account_inputs_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            for username, password in (("root;id", "valid"), ("root", "valid"), ("daemon", "valid"), ("nobody", "valid"), ("quickemu", ""), ("quickemu", "line\nbreak")):
                with self.subTest(username=username, password=password):
                    with self.assertRaises(ValueError):
                        BUILDER["build"](Path(temporary), username, password, "")


class ActivationTests(unittest.TestCase):
    def test_only_the_running_installed_system_can_activate_firstboot(self):
        for target, installed, recovery, launchd, allowed in (
            ("/", "true", "false", "true", True),
            ("/Volumes/Macintosh HD", "true", "false", "true", False),
            ("/", "false", "true", "true", False),
            ("/", "true", "true", "true", False),
            ("/", "true", "false", "false", False),
        ):
            with self.subTest(target=target, installed=installed, recovery=recovery, launchd=launchd):
                result = subprocess.run(
                    [shutil.which("bash"), "-c", 'source "$1"; can_activate_firstboot "$2" "$3" "$4" "$5"',
                     "test", str(SOURCE / "postinstall.sh"), target, installed, recovery, launchd],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                )
                self.assertEqual(result.returncode == 0, allowed, result.stderr)


class DiskGuardTests(unittest.TestCase):
    def test_real_apfs_registry_does_not_select_the_synthesized_disk(self):
        # Captured after an actual Tahoe preparation reboot. disk1 is physical;
        # its descendant AppleAPFSMedia disk3 also reports Whole=true.
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)
            (path / "read.py").write_text(PLIST_READER)
            fixture = SOURCE / "fixtures/tahoe-apfs-ioreg.plist"
            program = r'''
source "$1"
export PATH="$2"
PYTHON="$3" READER="$4" FIXTURE="$5" STATE="$6"
function plist_value() { "$PYTHON" "$READER" "$FIXTURE" "$2"; }
function ioreg() { cat "$FIXTURE"; }
find_system_disk
'''
            result = subprocess.run(
                [shutil.which("bash"), "-c", program, "test", str(SOURCE / "recovery.sh"),
                 os.environ["PATH"], sys.executable, str(path / "read.py"), str(fixture), str(path)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "disk1\n")

    def test_apple_virtio_media_name_identifies_only_the_whole_target_disk(self):
        # Shape captured from Tahoe's actual IORegistry: VirtIO stores the QEMU
        # serial in the media name, while its Device Characteristics lacks it.
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)
            (path / "read.py").write_text(PLIST_READER)
            for name, expected in (
                ("Apple Inc. QUICKEMU_SYSTEM Media", "disk0\n"),
                ("Apple Inc. QUICKEMU_SYSTEM_OTHER Media", ""),
                ("QEMU HARDDISK Media", ""),
            ):
                with self.subTest(name=name):
                    fixture = [{
                        "IOObjectClass": "AppleVirtIOBlockStorageDevice",
                        "Device Characteristics": {"Medium Type": "Solid State"},
                        "IORegistryEntryChildren": [{
                            "IOObjectClass": "IOBlockStorageDriver",
                            "IORegistryEntryChildren": [{
                                "IORegistryEntryName": name,
                                "Whole": True, "BSD Name": "disk0",
                                "IORegistryEntryChildren": [{
                                    "IORegistryEntryName": name,
                                    "Whole": False, "BSD Name": "disk0s1",
                                }],
                            }],
                        }],
                    }]
                    (path / "fixture.plist").write_bytes(plistlib.dumps(fixture))
                    program = r'''
source "$1"
export PATH="$2"
PYTHON="$3" READER="$4" FIXTURE="$5"
function plist_value() { "$PYTHON" "$READER" "$FIXTURE" "$2"; }
find_whole_media :0 false
'''
                    result = subprocess.run(
                        [shutil.which("bash"), "-c", program, "test",
                         str(SOURCE / "recovery.sh"), os.environ["PATH"], sys.executable,
                         str(path / "read.py"), str(path / "fixture.plist")],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout, expected)

    def check_guard(self, content="", partition=False, data="", started=False, read_error=False):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            if started:
                (state / "install-started").touch()
            program = r'''
source "$1"
export PATH="$2"
STATE="$3"
function diskutil() { [[ "$1" == list ]]; }
function plist_value() {
    case "$2" in
        *:Content) printf '%s' "$CONTENT" ;;
        *:Partitions:0) [[ "$PARTITION" == yes ]] ;;
        *) return 1 ;;
    esac
}
function dd() {
    [[ "$READ_ERROR" != yes ]] || return 1
    printf '%s' "$DISK_DATA"
}
ensure_blank_system_disk disk999
printf '%s\n' SAFE_TO_ERASE
'''
            environment = dict(os.environ, CONTENT=content,
                               PARTITION="yes" if partition else "no",
                               DISK_DATA=data, READ_ERROR="yes" if read_error else "no")
            return subprocess.run(
                [shutil.which("bash"), "-c", program, "test", str(SOURCE / "recovery.sh"), os.environ["PATH"], str(state)],
                env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )

    def test_blank_disk_is_accepted(self):
        result = self.check_guard()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("SAFE_TO_ERASE", result.stdout)

    def test_existing_data_and_repeated_install_are_refused(self):
        cases = (
            {"content": "GUID_partition_scheme"},
            {"partition": True},
            {"data": "existing unpartitioned content"},
            {"started": True},
            {"read_error": True},
        )
        for case in cases:
            with self.subTest(case=case):
                result = self.check_guard(**case)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("SAFE_TO_ERASE", result.stdout)


class ResumeTests(unittest.TestCase):
    UUID = "E1234567-1234-5678-ABCD-1234567890AB"

    def test_resume_requires_trusted_marker_uuid_and_incomplete_stage(self):
        cases = (
            ("", False, False, True),
            ("recovery-ready", False, False, True),
            ("installing", True, True, True),
            ("recovery-ready", True, True, True),
            ("installing", False, True, False),
            ("installing", True, False, False),
            ("", True, True, False),
            ("provisioned", True, True, False),
            ("configuring", True, True, False),
            ("complete", True, True, False),
            ("recovery-failed", True, True, False),
            ("firstboot-failed", True, True, False),
        )
        for status, marker, uuid, allowed in cases:
            with self.subTest(status=status, marker=marker, uuid=uuid):
                with tempfile.TemporaryDirectory() as temporary:
                    state = Path(temporary)
                    if marker:
                        (state / "install-started").touch()
                    if uuid:
                        (state / "container-uuid").write_text(self.UUID + "\n")
                    program = 'source "$1"; STATE="$2"; check_install_stage "$3"'
                    result = subprocess.run(
                        [shutil.which("bash"), "-c", program, "test",
                         str(SOURCE / "recovery.sh"), str(state), status],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    )
                    self.assertEqual(result.returncode == 0, allowed, result.stdout)

    def inspect_apfs(self, fixture, expected_uuid=None):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)
            (path / "read.py").write_text(PLIST_READER)
            (path / "fixture.plist").write_bytes(plistlib.dumps(fixture))
            program = r'''
source "$1"
export PATH="$2"
PYTHON="$3" READER="$4" FIXTURE="$5"
function plist_value() { "$PYTHON" "$READER" "$FIXTURE" "$2"; }
function diskutil() { [[ "$1 $2" == 'apfs list' ]]; }
locate_install_volume disk0 "$6"
printf '%s:%s\n' "$APFS_CONTAINER_UUID" "$APFS_VOLUME"
'''
            return subprocess.run(
                [shutil.which("bash"), "-c", program, "test", str(SOURCE / "recovery.sh"),
                 os.environ["PATH"], sys.executable, str(path / "read.py"),
                 str(path / "fixture.plist"), expected_uuid or self.UUID],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )

    def test_resume_selects_volume_by_physical_store_and_container_uuid(self):
        fixture = {"Containers": [{
            "APFSContainerUUID": self.UUID,
            "PhysicalStores": [{"DeviceIdentifier": "disk0s2"}],
            "Volumes": [
                {"Name": "Macintosh HD", "Roles": ["System"], "DeviceIdentifier": "disk7s1"},
                {"Name": "Macintosh HD - Data", "Roles": ["Data"], "DeviceIdentifier": "disk7s2"},
            ],
        }]}
        result = self.inspect_apfs(fixture)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, self.UUID + ":disk7s1\n")
        failures = []
        changed = copy.deepcopy(fixture)
        changed["Containers"][0]["APFSContainerUUID"] = "F1234567-1234-5678-ABCD-1234567890AB"
        failures.append(changed)
        changed = copy.deepcopy(fixture)
        changed["Containers"][0]["PhysicalStores"][0]["DeviceIdentifier"] = "disk9s2"
        failures.append(changed)
        changed = copy.deepcopy(fixture)
        changed["Containers"][0]["PhysicalStores"].append({"DeviceIdentifier": "disk9s2"})
        failures.append(changed)
        changed = copy.deepcopy(fixture)
        changed["Containers"][0]["Volumes"].append(copy.deepcopy(changed["Containers"][0]["Volumes"][0]))
        failures.append(changed)
        changed = copy.deepcopy(fixture)
        changed["Containers"][0]["Volumes"][0]["Roles"] = ["Data"]
        failures.append(changed)
        failures.append({"Containers": []})
        for changed in failures:
            with self.subTest(fixture=changed):
                result = self.inspect_apfs(changed)
                self.assertNotEqual(result.returncode, 0, result.stdout)


if __name__ == "__main__":
    unittest.main()
