# Preserve Alpine's standard /etc/profile and scope startup to the local session.
if [ "$(id -un)" = aios ] && [ -z "${DISPLAY:-}" ] && [ "$(tty 2>/dev/null)" = /dev/tty1 ]; then
    if grep -qw aios.install /proc/cmdline; then
        doas -n /usr/local/sbin/aios-install
    fi
    if grep -qw aios.recovery /proc/cmdline; then
        printf '\nAIOS recovery mode: starting a software-rendered (XRender) desktop.\n'
        printf 'If it does not appear, this console stays available on tty1-tty3 and the serial port.\n'
        printf 'Run aios-hardware-report --human here for a sanitized network/graphics report.\n\n'
    fi
    if ! dbus-run-session -- startx -- -keeptty; then
        printf '\nAIOS desktop could not start. This recovery shell stays available; run startx to retry.\n'
        printf 'Diagnose with: aios-hardware-report --human\n'
        printf 'Logs: ~/.local/state/aios/session.log and ~/.local/state/aios/graphics.log\n'
        printf 'Reboot and choose the recovery boot entry for a conservative graphics path.\n'
    fi
fi
