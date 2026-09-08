# Preserve Alpine's standard /etc/profile and scope startup to the local session.
if [ "$(id -un)" = aios ] && [ -z "${DISPLAY:-}" ] && [ "$(tty 2>/dev/null)" = /dev/tty1 ]; then
    if grep -qw aios.install /proc/cmdline; then
        doas -n /usr/local/sbin/aios-install
    fi
    if ! dbus-run-session -- startx -- -keeptty; then
        printf '\nAIOS desktop could not start. Recovery shell is available; run startx to retry.\n'
    fi
fi
