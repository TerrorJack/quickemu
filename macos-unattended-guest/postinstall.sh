#!/bin/bash
# Installer executes this script with the destination volume as argument 3.
set -euo pipefail
export PATH=/usr/bin:/bin:/usr/sbin:/sbin

function can_activate_firstboot() {
    local target="$1" installed_system="$2" recovery_system="$3" launchd_available="$4"
    [[ "${target}" == / && "${installed_system}" == true && "${recovery_system}" == false && "${launchd_available}" == true ]]
}

function main() {
    local source target dest installed_system=false recovery_system=false launchd_available=false
    source="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    target="${3:?Installer did not supply a target volume}"
    dest="${target%/}/Library/Application Support/Quickemu"
    mkdir -p "${dest}" "${target%/}/Library/LaunchDaemons" "${target%/}/private/var/db"
    cp "${source}/firstboot.sh" "${source}/settings.sh" "${source}/authorized_keys" "${source}/kcpassword" "${dest}/"
    chown -R 0:0 "${dest}"
    chmod 700 "${dest}" "${dest}/firstboot.sh"
    chmod 600 "${dest}/settings.sh" "${dest}/authorized_keys" "${dest}/kcpassword"
    cp "${source}/org.quickemu.firstboot.plist" "${target%/}/Library/LaunchDaemons/"
    chown 0:0 "${target%/}/Library/LaunchDaemons/org.quickemu.firstboot.plist"
    chmod 644 "${target%/}/Library/LaunchDaemons/org.quickemu.firstboot.plist"
    # Tahoe identifies the Voodoo PS/2 and QEMU USB keyboards as ANSI layouts.
    # Seed the preference before first login to avoid Keyboard Setup Assistant.
    mkdir -p "${target%/}/Library/Preferences"
    if [[ ! -e "${target%/}/Library/Preferences/com.apple.keyboardtype.plist" ]]; then
        cp "${source}/com.apple.keyboardtype.plist" "${target%/}/Library/Preferences/"
        chown 0:0 "${target%/}/Library/Preferences/com.apple.keyboardtype.plist"
        chmod 644 "${target%/}/Library/Preferences/com.apple.keyboardtype.plist"
    fi
    # Suppress Setup Assistant before loginwindow starts on the first normal boot.
    touch "${target%/}/private/var/db/.AppleSetupDone"

    [[ ! -d /System/Library/CoreServices/loginwindow.app ]] || installed_system=true
    [[ ! -d '/System/Installation/CDIS/Recovery Springboard.app' ]] || recovery_system=true
    if launchctl print system >/dev/null 2>&1; then
        launchd_available=true
    fi
    if [[ -d /Volumes/QUICKEMU/state ]]; then
        {
            printf 'target=%s\n' "${target}"
            printf 'installed-loginwindow=%s\n' "${installed_system}"
            printf 'recovery-springboard=%s\n' "${recovery_system}"
            printf 'launchd-system=%s\n' "${launchd_available}"
        } > /Volumes/QUICKEMU/state/postinstall.log
        printf '%s\n' provisioned > /Volumes/QUICKEMU/state/status
        sync
    fi

    # Additional packages can run after launchd has already scanned its plists.
    # Activate immediately only in the installed OS; never configure Recovery.
    # The package also declares RequireRestart, covering offline installation
    # and activation failure without relying on a GUI action.
    if can_activate_firstboot "${target}" "${installed_system}" "${recovery_system}" "${launchd_available}"; then
        if launchctl print system/org.quickemu.firstboot >/dev/null 2>&1 ||
            launchctl bootstrap system /Library/LaunchDaemons/org.quickemu.firstboot.plist; then
            if [[ -d /Volumes/QUICKEMU/state ]]; then
                printf '%s\n' activation=bootstrapped >> /Volumes/QUICKEMU/state/postinstall.log
            fi
        elif [[ -d /Volumes/QUICKEMU/state ]]; then
            printf '%s\n' activation=deferred-to-required-restart >> /Volumes/QUICKEMU/state/postinstall.log
        fi
    elif [[ -d /Volumes/QUICKEMU/state ]]; then
        printf '%s\n' activation=offline-required-restart >> /Volumes/QUICKEMU/state/postinstall.log
    fi
    sync
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
