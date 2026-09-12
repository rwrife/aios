.pragma library

function extent(preferred, screen, available) {
    // QML's desktopAvailable extent can span several monitors.
    return Math.min(preferred, Math.floor(Math.min(screen, available) * 0.7))
}

function topCenterY(screenHeight, windowHeight) {
    // Initial y offset that centers a window on the top 75% of the screen so
    // frameless windows (chat) stay clear of the orb docked at the bottom.
    return Math.max(0, Math.floor((screenHeight * 0.75 - windowHeight) / 2))
}
