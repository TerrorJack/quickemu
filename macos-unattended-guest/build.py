#!/usr/bin/env python3
"""Build macOS Recovery provisioning media without Apple's packaging tools.

The output directory can be copied into a writable FAT volume named QUICKEMU.
setup.pkg is an unsigned product archive, containing a scripts-only component
package, in the same format produced by productbuild/pkgbuild on macOS.
"""

import argparse
import gzip
import hashlib
import os
from pathlib import Path
import re
import shlex
import struct
import xml.etree.ElementTree as ET
import zlib


def cpio(files):
    """Create the portable ASCII (odc) cpio format used by installer scripts."""
    result = bytearray()
    entries = [(".", b"", 0o40755), *files, ("TRAILER!!!", b"", 0)]
    for inode, (name, content, mode) in enumerate(entries, 1):
        encoded = name.encode() + b"\0"
        header = (
            f"070707{0:06o}{inode:06o}{mode:06o}{0:06o}{0:06o}"
            f"{1:06o}{0:06o}{0:011o}{len(encoded):06o}{len(content):011o}"
        )
        result.extend(header.encode() + encoded + content)
    result.extend(b"\0" * (-len(result) % 512))
    return gzip.compress(bytes(result), mtime=0)


def xar(files):
    """Create a XAR v1 archive with authenticated TOC and uncompressed entries."""
    root = ET.Element("xar")
    toc = ET.SubElement(root, "toc")
    checksum = ET.SubElement(toc, "checksum", style="sha1")
    ET.SubElement(checksum, "offset").text = "0"
    ET.SubElement(checksum, "size").text = "20"
    parents = {"": toc}
    heap = bytearray()
    next_id = 1
    for filename, content in files:
        parts = filename.split("/")
        for index, part in enumerate(parts):
            path = "/".join(parts[: index + 1])
            if path in parents:
                continue
            node = ET.SubElement(
                parents["/".join(parts[:index])], "file", id=str(next_id)
            )
            next_id += 1
            ET.SubElement(node, "name").text = part
            is_file = index == len(parts) - 1
            ET.SubElement(node, "type").text = "file" if is_file else "directory"
            ET.SubElement(node, "mode").text = "0644" if is_file else "0755"
            ET.SubElement(node, "uid").text = "0"
            ET.SubElement(node, "gid").text = "0"
            if not is_file:
                parents[path] = node
                continue
            data = ET.SubElement(node, "data")
            ET.SubElement(data, "length").text = str(len(content))
            ET.SubElement(data, "offset").text = str(20 + len(heap))
            ET.SubElement(data, "size").text = str(len(content))
            ET.SubElement(data, "encoding", style="application/octet-stream")
            digest = hashlib.sha1(content).hexdigest()
            ET.SubElement(data, "extracted-checksum", style="sha1").text = digest
            ET.SubElement(data, "archived-checksum", style="sha1").text = digest
            heap.extend(content)
    toc_xml = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    compressed = zlib.compress(toc_xml)
    header = struct.pack(
        ">IHHQQI", 0x78617221, 28, 1, len(compressed), len(toc_xml), 1
    )
    return header + compressed + hashlib.sha1(compressed).digest() + heap


def kcpassword(password):
    key = bytes.fromhex("7d895223d2bcddeaa3b91f")
    data = password.encode() + b"\0"
    data += b"\0" * (-len(data) % len(key))
    return bytes(value ^ key[index % len(key)] for index, value in enumerate(data))


def build(output, username, password, public_key):
    source = Path(__file__).resolve().parent
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,30}", username):
        raise ValueError("username must contain 1-31 lowercase letters, digits or underscores")
    if username in {"root", "daemon", "nobody"}:
        raise ValueError("username must not name a macOS system account")
    if not password or "\0" in password or "\n" in password or "\r" in password:
        raise ValueError("password must be nonempty and contain no NUL or newline")
    if public_key and not re.fullmatch(
        r"(?:ssh-ed25519|ssh-rsa|ecdsa-sha2-\S+) [A-Za-z0-9+/=]+(?: [^\r\n]*)?\n?",
        public_key,
    ):
        raise ValueError("public key must be one OpenSSH public key")
    settings = (
        f"QUICKEMU_USERNAME={shlex.quote(username)}\n"
        f"QUICKEMU_PASSWORD={shlex.quote(password)}\n"
    ).encode()
    scripts = [
        ("./postinstall", (source / "postinstall.sh").read_bytes(), 0o100755),
        ("./firstboot.sh", (source / "firstboot.sh").read_bytes(), 0o100755),
        ("./settings.sh", settings, 0o100600),
        ("./authorized_keys", (public_key.rstrip() + "\n").encode(), 0o100600),
        ("./kcpassword", kcpassword(password), 0o100600),
        (
            "./com.apple.keyboardtype.plist",
            (source / "com.apple.keyboardtype.plist").read_bytes(),
            0o100644,
        ),
        (
            "./org.quickemu.firstboot.plist",
            (source / "org.quickemu.firstboot.plist").read_bytes(),
            0o100644,
        ),
    ]
    distribution = b'''<?xml version="1.0" encoding="UTF-8"?>
<installer-gui-script minSpecVersion="1">
  <title>Quickemu unattended setup</title>
  <options customize="never" require-scripts="false"/>
  <choices-outline><line choice="default"><line choice="org.quickemu.setup"/></line></choices-outline>
  <choice id="default"/>
  <choice id="org.quickemu.setup" visible="false"><pkg-ref id="org.quickemu.setup"/></choice>
  <pkg-ref id="org.quickemu.setup" version="1.0.0" onConclusion="RequireRestart" installKBytes="0">#quickemu-component.pkg</pkg-ref>
</installer-gui-script>
'''
    package_info = b'''<?xml version="1.0" encoding="UTF-8"?>
<pkg-info overwrite-permissions="true" relocatable="false" identifier="org.quickemu.setup" postinstall-action="restart" version="1.0.0" format-version="2" auth="root">
  <bundle-version/><upgrade-bundle/><update-bundle/><atomic-update-bundle/><strict-identifier/><relocate/>
  <scripts><postinstall file="./postinstall"/></scripts>
</pkg-info>
'''
    package = xar(
        [
            ("Distribution", distribution),
            ("quickemu-component.pkg/PackageInfo", package_info),
            ("quickemu-component.pkg/Scripts", cpio(scripts)),
        ]
    )
    output.mkdir(parents=True, exist_ok=True)
    os.chmod(output, 0o700)
    (output / "state").mkdir(exist_ok=True)
    (output / "recovery.sh").write_bytes((source / "recovery.sh").read_bytes())
    (output / "setup.pkg").write_bytes(package)
    os.chmod(output / "setup.pkg", 0o600)
    os.chmod(output / "recovery.sh", 0o700)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--username", default="quickemu")
    parser.add_argument("--password-file", type=Path, required=True)
    parser.add_argument("--public-key-file", type=Path)
    args = parser.parse_args()
    password = args.password_file.read_text().removesuffix("\n")
    public_key = args.public_key_file.read_text() if args.public_key_file else ""
    try:
        build(args.output, args.username, password, public_key)
    except ValueError as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
