#!/bin/bash
# Run from the QUICKEMU seed volume in macOS Recovery.
set -Eeuo pipefail
export PATH=/usr/bin:/bin:/usr/sbin:/sbin

function finish_recovery() {
    local result="$1"
    if [[ -n "${INSTALL_LOG_PID:-}" ]]; then
        kill "${INSTALL_LOG_PID}" 2>/dev/null || true
        wait "${INSTALL_LOG_PID}" 2>/dev/null || true
    fi
    if [[ -n "${CAFFEINATE_PID:-}" ]]; then
        kill "${CAFFEINATE_PID}" 2>/dev/null || true
        wait "${CAFFEINATE_PID}" 2>/dev/null || true
    fi
    if (( result != 0 )); then
        printf '%s\n' "Recovery provisioning failed (exit ${result})" > "${STATE}/error"
        printf '%s\n' recovery-failed > "${STATE}/status"
        sync
    fi
}

function plist_value() {
    /usr/libexec/PlistBuddy -c "Print '$2'" "$1" 2>/dev/null
}

function find_whole_media() {
    local path="$1" matched="$2" child=0 bsd whole name
    bsd="$(plist_value /tmp/quickemu-disks.plist "${path}:BSD Name" || true)"
    whole="$(plist_value /tmp/quickemu-disks.plist "${path}:Whole" || true)"
    name="$(plist_value /tmp/quickemu-disks.plist "${path}:IORegistryEntryName" || true)"
    # Apple's VirtIO driver presents the QEMU serial as its media product name.
    if [[ "${name}" == 'Apple Inc. QUICKEMU_SYSTEM Media' ]]; then
        matched=true
    fi
    if [[ "${matched}" == true && "${whole}" == true && "${bsd}" =~ ^disk[0-9]+$ ]]; then
        printf '%s\n' "${bsd}"
        # APFS exposes another Whole=true synthesized disk below its physical
        # store. Stop at the physical medium instead of selecting both disks.
        return 0
    fi
    while plist_value /tmp/quickemu-disks.plist "${path}:IORegistryEntryChildren:${child}" >/dev/null; do
        find_whole_media "${path}:IORegistryEntryChildren:${child}" "${matched}"
        child=$((child + 1))
    done
}

function find_system_disk() {
    local index=0 serial matched
    ioreg -r -c IOBlockStorageDevice -al > /tmp/quickemu-disks.plist
    cp /tmp/quickemu-disks.plist "${STATE}/disks.plist"
    while plist_value /tmp/quickemu-disks.plist ":${index}" >/dev/null; do
        serial="$(plist_value /tmp/quickemu-disks.plist ":${index}:Device Characteristics:Serial Number" || true)"
        serial="${serial// /}"
        matched=false
        if [[ "${serial}" == QUICKEMU_SYSTEM ]]; then
            matched=true
        fi
        find_whole_media ":${index}" "${matched}"
        index=$((index + 1))
    done
}

function ensure_blank_system_disk() {
    local system_disk="$1" content
    # Never erase a disk on a repeated invocation, or a disk with existing data.
    if [[ -e "${STATE}/install-started" ]]; then
        echo 'ERROR: installation has already been started; refusing to erase the disk.'
        return 1
    fi
    if ! diskutil list -plist "/dev/${system_disk}" > /tmp/quickemu-target.plist; then
        echo "ERROR: could not inspect /dev/${system_disk}."
        return 1
    fi
    content="$(plist_value /tmp/quickemu-target.plist ':AllDisksAndPartitions:0:Content' || true)"
    if [[ -n "${content}" ]] || plist_value /tmp/quickemu-target.plist ':AllDisksAndPartitions:0:Partitions:0' >/dev/null; then
        echo "ERROR: /dev/${system_disk} is not an unpartitioned blank disk."
        return 1
    fi
    if dd if="/dev/${system_disk}" bs=1048576 count=1 2>/dev/null | LC_ALL=C tr -d '\000' | wc -c | awk '$1 != 0 {exit 1}'; then
        :
    else
        echo "ERROR: /dev/${system_disk} contains data; refusing to erase it."
        return 1
    fi
}

function locate_install_volume() {
    local system_disk="$1" expected_uuid="$2" index=0 store=0 volume=0
    local physical name role uuid container_count=0 volume_count=0 matches
    APFS_CONTAINER_UUID=""
    APFS_VOLUME=""
    if ! diskutil apfs list -plist > /tmp/quickemu-apfs.plist; then
        echo 'ERROR: could not inspect the APFS containers.'
        return 1
    fi
    while plist_value /tmp/quickemu-apfs.plist ":Containers:${index}" >/dev/null; do
        store=0
        matches=false
        while physical="$(plist_value /tmp/quickemu-apfs.plist ":Containers:${index}:PhysicalStores:${store}:DeviceIdentifier")"; do
            if [[ "${physical}" =~ ^${system_disk}s[0-9]+$ ]]; then
                matches=true
            fi
            store=$((store + 1))
        done
        if [[ "${matches}" == true ]]; then
            if (( store != 1 )); then
                echo 'ERROR: refusing an APFS container spanning multiple physical stores.'
                return 1
            fi
            container_count=$((container_count + 1))
            uuid="$(plist_value /tmp/quickemu-apfs.plist ":Containers:${index}:APFSContainerUUID" || true)"
            if [[ ! "${uuid}" =~ ^[[:xdigit:]]{8}-[[:xdigit:]]{4}-[[:xdigit:]]{4}-[[:xdigit:]]{4}-[[:xdigit:]]{12}$ ]]; then
                echo 'ERROR: the target APFS container has no valid UUID.'
                return 1
            fi
            if [[ -n "${expected_uuid}" && "${uuid}" != "${expected_uuid}" ]]; then
                echo 'ERROR: the APFS container does not match the interrupted installation.'
                return 1
            fi
            APFS_CONTAINER_UUID="${uuid}"
            volume=0
            while plist_value /tmp/quickemu-apfs.plist ":Containers:${index}:Volumes:${volume}" >/dev/null; do
                name="$(plist_value /tmp/quickemu-apfs.plist ":Containers:${index}:Volumes:${volume}:Name" || true)"
                role="$(plist_value /tmp/quickemu-apfs.plist ":Containers:${index}:Volumes:${volume}:Roles:0" || true)"
                if [[ "${name}" == 'Macintosh HD' && ( -z "${role}" || "${role}" == System ) ]]; then
                    volume_count=$((volume_count + 1))
                    APFS_VOLUME="$(plist_value /tmp/quickemu-apfs.plist ":Containers:${index}:Volumes:${volume}:DeviceIdentifier" || true)"
                fi
                volume=$((volume + 1))
            done
        fi
        index=$((index + 1))
    done
    if (( container_count != 1 || volume_count != 1 )) || [[ ! "${APFS_VOLUME}" =~ ^disk[0-9]+s[0-9]+$ ]]; then
        echo 'ERROR: expected exactly one Macintosh HD volume on the identified installation disk.'
        return 1
    fi
}

function check_install_stage() {
    local previous_status="$1"
    case "${previous_status}" in
        ''|recovery-ready|installing) ;;
        *)
            echo "ERROR: refusing to restart installation from state '${previous_status}'."
            return 1;;
    esac
    if [[ -e "${STATE}/install-started" ]]; then
        if [[ "${previous_status}" != installing && "${previous_status}" != recovery-ready ]]; then
            echo 'ERROR: an installation marker exists without a resumable installation state.'
            return 1
        fi
        if [[ ! -s "${STATE}/container-uuid" ]]; then
            echo 'ERROR: the interrupted installation has no recorded APFS container UUID; refusing to erase or adopt a disk.'
            return 1
        fi
    elif [[ "${previous_status}" == installing ]]; then
        echo 'ERROR: installation state exists without its trusted installation marker.'
        return 1
    fi
}

function main() {
    local system_disk previous_status="" expected_uuid="" install_volume
    SEED="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    STATE="${SEED}/state"
    mkdir -p "${STATE}"
    exec > >(tee -a "${STATE}/recovery.log") 2>&1
    if [[ -f "${STATE}/status" ]]; then
        IFS= read -r previous_status < "${STATE}/status" || true
    fi
    check_install_stage "${previous_status}"
    trap 'finish_recovery "$?"' EXIT
    tail -n +1 -F /var/log/install.log > "${STATE}/install.log" 2>&1 &
    INSTALL_LOG_PID=$!
    # The installer's own assertion prevents system sleep, but not display
    # sleep. Keep screenshots available throughout preparation and downloads.
    /usr/bin/caffeinate -di -w "$$" &
    CAFFEINATE_PID=$!
    if [[ ! -e "${STATE}/install-started" ]]; then
        printf '%s\n' recovery-ready > "${STATE}/status"
    fi
    diskutil list
    system_disk="$(find_system_disk)"
    if [[ ! "${system_disk}" =~ ^disk[0-9]+$ ]]; then
        echo 'ERROR: expected exactly one whole disk with serial QUICKEMU_SYSTEM.'
        return 1
    fi
    if [[ -e "${STATE}/install-started" ]]; then
        IFS= read -r expected_uuid < "${STATE}/container-uuid"
        locate_install_volume "${system_disk}" "${expected_uuid}"
        echo "Resuming installation on ${APFS_VOLUME} without erasing the disk."
    else
        ensure_blank_system_disk "${system_disk}"
        touch "${STATE}/install-started"
        printf '%s\n' "${system_disk}" > "${STATE}/system-disk"
        sync
        diskutil eraseDisk APFS 'Macintosh HD' GPT "/dev/${system_disk}"
        locate_install_volume "${system_disk}" ''
        printf '%s\n' "${APFS_CONTAINER_UUID}" > "${STATE}/container-uuid"
        sync
    fi
    diskutil mount "${APFS_VOLUME}"
    diskutil info -plist "${APFS_VOLUME}" > /tmp/quickemu-volume.plist
    install_volume="$(plist_value /tmp/quickemu-volume.plist ':MountPoint')"
    if [[ ! -d "${install_volume}" || "${install_volume}" == / ]]; then
        echo 'ERROR: the installation volume is not mounted at a safe destination.'
        return 1
    fi
    printf '%s\n' installing > "${STATE}/status"
    sync
    '/Install macOS Tahoe.app/Contents/Resources/startosinstall' \
        --volume "${install_volume}" \
        --agreetolicense --nointeraction \
        --installpackage "${SEED}/setup.pkg"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main
fi
