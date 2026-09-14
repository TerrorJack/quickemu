#!/bin/bash
# Runs as root after the installed system, including Directory Services, starts.
set -euo pipefail
export PATH=/usr/bin:/bin:/usr/sbin:/sbin
ROOT='/Library/Application Support/Quickemu'
DONE='/var/db/quickemu-unattended-complete'
[[ ! -e "${DONE}" ]] || exit 0
# shellcheck source=/dev/null
source "${ROOT}/settings.sh"

function report_phase() {
    if [[ -z "${FIRSTBOOT_LOG_PID:-}" && -d /Volumes/QUICKEMU/state ]]; then
        tail -n +1 -F /var/log/quickemu-firstboot.log > /Volumes/QUICKEMU/state/firstboot.log 2>/dev/null &
        FIRSTBOOT_LOG_PID=$!
    fi
    printf '%s\n' "$1"
}

function stop_log_mirror() {
    if [[ -n "${FIRSTBOOT_LOG_PID:-}" ]]; then
        kill "${FIRSTBOOT_LOG_PID}" 2>/dev/null || true
        wait "${FIRSTBOOT_LOG_PID}" 2>/dev/null || true
        FIRSTBOOT_LOG_PID=""
    fi
}

function report_state() {
    local attempt
    # Disk Arbitration can mount removable media after this daemon starts.
    if [[ "$1" == complete ]]; then
        for ((attempt = 0; attempt < 15; attempt++)); do
            [[ ! -d /Volumes/QUICKEMU/state ]] || break
            sleep 2
        done
    fi
    if [[ -d /Volumes/QUICKEMU/state ]]; then
        if [[ "$1" == complete ]]; then
            stop_log_mirror
            cp /var/log/quickemu-firstboot.log /Volumes/QUICKEMU/state/firstboot.log || true
        fi
        printf '%s\n' "$1" > /Volumes/QUICKEMU/state/status
        sync
    fi
}

function finish_firstboot() {
    local result="$1"
    stop_log_mirror
    if (( result != 0 )); then
        if [[ -d /Volumes/QUICKEMU/state ]]; then
            printf '%s\n' "First boot provisioning failed (exit ${result}); see firstboot.log." > /Volumes/QUICKEMU/state/error
            cp /var/log/quickemu-firstboot.log /Volumes/QUICKEMU/state/firstboot.log || true
        fi
        report_state firstboot-failed
    fi
}
trap 'finish_firstboot "$?"' EXIT
report_state configuring

# launchd may start us before opendirectoryd has finished initializing.
report_phase 'Waiting for Directory Services.'
for ((attempt = 0; attempt < 60; attempt++)); do
    if dscl . -read /Groups/admin >/dev/null 2>&1; then
        break
    fi
    sleep 2
done
dscl . -read /Groups/admin >/dev/null
report_phase 'Configuring the administrator account.'
EXISTING_UID="$(dscl . -read "/Users/${QUICKEMU_USERNAME}" UniqueID 2>/dev/null | awk '{print $2}' || true)"
if [[ -n "${EXISTING_UID}" && "${EXISTING_UID}" != 501 ]]; then
    echo 'ERROR: refusing to modify an existing account with a different user ID.' >&2
    exit 1
fi
if ! dscl . -read "/Users/${QUICKEMU_USERNAME}" >/dev/null 2>&1; then
    if dscl . -list /Users UniqueID | awk '$2 == 501 {found = 1} END {exit !found}'; then
        echo 'ERROR: user ID 501 is already occupied by another account.' >&2
        exit 1
    fi
    dscl . -create "/Users/${QUICKEMU_USERNAME}"
fi
dscl . -create "/Users/${QUICKEMU_USERNAME}" UserShell /bin/bash
dscl . -create "/Users/${QUICKEMU_USERNAME}" RealName 'Quickemu'
dscl . -create "/Users/${QUICKEMU_USERNAME}" UniqueID 501
dscl . -create "/Users/${QUICKEMU_USERNAME}" PrimaryGroupID 20
dscl . -create "/Users/${QUICKEMU_USERNAME}" NFSHomeDirectory "/Users/${QUICKEMU_USERNAME}"
dscl . -passwd "/Users/${QUICKEMU_USERNAME}" "${QUICKEMU_PASSWORD}"
dscl . -append /Groups/admin GroupMembership "${QUICKEMU_USERNAME}"
USER_HOME="/Users/${QUICKEMU_USERNAME}"
mkdir -p "${USER_HOME}/.ssh" "${USER_HOME}/Library/Preferences"
cp "${ROOT}/authorized_keys" "${USER_HOME}/.ssh/authorized_keys"
chmod 700 "${USER_HOME}/.ssh"
chmod 600 "${USER_HOME}/.ssh/authorized_keys"

report_phase 'Configuring user and system preferences.'
defaults write "${USER_HOME}/Library/Preferences/com.apple.SetupAssistant" DidSeeCloudSetup -bool true
defaults write "${USER_HOME}/Library/Preferences/com.apple.SetupAssistant" DidSeePrivacy -bool true
defaults write "${USER_HOME}/Library/Preferences/com.apple.SetupAssistant" DidSeeSiriSetup -bool true
defaults write "${USER_HOME}/Library/Preferences/com.apple.SetupAssistant" DidSeeAppearanceSetup -bool true
defaults write "${USER_HOME}/Library/Preferences/com.apple.SetupAssistant" DidSeeScreenTime -bool true
defaults write "${USER_HOME}/Library/Preferences/com.apple.SetupAssistant" RunNonInteractive -bool true
defaults write "${USER_HOME}/Library/Preferences/com.apple.SetupAssistant" LastSeenCloudProductVersion "$(sw_vers -productVersion)"
defaults write "${USER_HOME}/Library/Preferences/com.apple.SetupAssistant" LastSeenBuddyBuildVersion "$(sw_vers -buildVersion)"
defaults write "${USER_HOME}/Library/Preferences/com.apple.screensaver" idleTime -int 0
chown -R 501:20 "${USER_HOME}"
touch /var/db/.AppleSetupDone

cp "${ROOT}/kcpassword" /etc/kcpassword
chmod 600 /etc/kcpassword
defaults write /Library/Preferences/com.apple.loginwindow autoLoginUser "${QUICKEMU_USERNAME}"
defaults write /Library/Preferences/com.apple.screensaver loginWindowIdleTime -int 0
pmset -a sleep 0 displaysleep 0 disksleep 0

# Enabling launchd's built-in SSH service avoids the GUI-only Full Disk Access
# requirement imposed on systemsetup -setremotelogin in recent macOS versions.
report_phase 'Enabling SSH.'
launchctl enable system/com.openssh.sshd
if ! launchctl print system/com.openssh.sshd >/dev/null 2>&1; then
    launchctl bootstrap system /System/Library/LaunchDaemons/ssh.plist
fi
launchctl print system/com.openssh.sshd >/dev/null
mkdir -p /etc/sudoers.d
printf '%s ALL=(ALL) NOPASSWD: ALL\n' "${QUICKEMU_USERNAME}" > /etc/sudoers.d/quickemu
chmod 440 /etc/sudoers.d/quickemu
visudo -c -f /etc/sudoers.d/quickemu

sw_vers > "${DONE}"
report_phase 'First-boot provisioning complete.'
report_state complete
# The persistent marker makes repeated starts harmless. Remove credentials and
# disable this service after successful provisioning.
rm -f "${ROOT}/settings.sh" "${ROOT}/kcpassword"
launchctl disable system/org.quickemu.firstboot
# Refresh loginwindow so it recognizes the newly created account.
killall loginwindow || true
