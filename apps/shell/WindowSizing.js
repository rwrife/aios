.pragma library

function extent(preferred, screen, available) {
    // QML's desktopAvailable extent can span several monitors.
    return Math.min(preferred, Math.floor(Math.min(screen, available) * 0.7))
}
