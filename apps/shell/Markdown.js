.pragma library

// Minimal, dependency-free Markdown -> HTML converter for chat replies.
//
// AIOS renders assistant replies with Qt's rich-text engine, so replies
// arrive as raw Markdown text. This module converts a practical subset of
// Markdown into the HTML dialect QTextDocument understands:
//   - fenced code blocks (``` ... ```) and indented code
//   - inline code, bold, italic, strikethrough
//   - ATX headings (# .. ######)
//   - unordered (-, *, +) and ordered (1.) lists
//   - blockquotes
//   - links [text](url) restricted to http/https/mailto
//   - horizontal rules and paragraphs
//
// All input is HTML-escaped *before* any markup is generated, so reply text
// can never inject tags into the shell. Colors are supplied by the caller so
// the output always follows apps/shell/Theme.qml instead of a fixed palette.

function escapeHtml(text) {
    return String(text)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
}

function safeUrl(url) {
    var trimmed = String(url).trim()
    // Strip a leading < and trailing > that Markdown allows around URLs.
    if (trimmed.charAt(0) === "<" && trimmed.charAt(trimmed.length - 1) === ">")
        trimmed = trimmed.slice(1, -1)
    // Reject anything that is not http(s) or mailto; JS in href would be a
    // way for model output to drive actions it is not authorized to take.
    if (/^https?:\/\/[^\s]+$/i.test(trimmed) || /^mailto:[^\s@]+@[^\s@]+$/i.test(trimmed))
        return trimmed
    return ""
}

function inline(text, colors) {
    var out = ""
    var i = 0
    // Inline code spans are terminal: they suppress every other inline rule
    // inside their delimiters, matching CommonMark.
    while (i < text.length) {
        var tick = text.indexOf("`", i)
        if (tick < 0) { out += emphasis(text.slice(i), colors); break }
        out += emphasis(text.slice(i, tick), colors)
        var close = text.indexOf("`", tick + 1)
        if (close < 0) { out += escapeHtml(text.slice(tick)); break }
        var code = text.slice(tick + 1, close)
        out += "<span style=\"font-family:monospace;background-color:" + colors.code + ";color:" + colors.ink + ";\">" + escapeHtml(code) + "</span>"
        i = close + 1
    }
    return out
}

function emphasis(text, colors) {
    var theme = colors || { ink: "#f1f5f6", muted: "#b2c3cd", code: "#203340", accent: "#bde4e6" }
    var out = escapeHtml(text)
    out = out.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, function (match, label, url) {
        var href = safeUrl(url)
        // Unsafe links stay verbatim as plain text: the label never
        // disappears and the target never becomes clickable.
        return href === "" ? match : "<a href=\"" + escapeHtml(href) + "\" style=\"text-decoration:none;color:" + theme.accent + ";\">" + label + "</a>"
    })
    out = out.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    out = out.replace(/(^|[^*])\*([^*\s][^*]*)\*/g, "$1<em>$2</em>")
    out = out.replace(/~~([^~]+)~~/g, "<del>$1</del>")
    return out
}

function isFence(line) {
    return /^\s{0,3}(```|~~~)/.test(line)
}

function fenceMarker(line) {
    var match = /^\s{0,3}(```|~~~)/.exec(line)
    return match ? match[1] : ""
}

function isListLine(line) {
    return /^\s*([-*+]|\d+[.)])\s+/.test(line)
}

function isBlockquoteLine(line) {
    return /^\s{0,3}>/.test(line)
}

function isRuleLine(line) {
    return /^\s{0,3}(?:([-*_])\s*){3,}$/.test(line) && /[-*_]/.test(line)
}

function isHeadingLine(line) {
    return /^\s{0,3}#{1,6}\s+/.test(line)
}

// toHtml converts raw Markdown into the HTML subset Qt rich text renders.
// `colors` provides { ink, muted, code } theme strings from Theme.qml.
function toHtml(text, colors) {
    if (text === undefined || text === null || String(text).length === 0) return ""
    var theme = colors || { ink: "#f1f5f6", muted: "#b2c3cd", code: "#203340", accent: "#bde4e6" }
    var lines = String(text).replace(/\r\n?/g, "\n").split("\n")
    var html = []
    var paragraph = []
    var inCode = false
    var codeLines = []
    var codeMarker = ""
    var listItems = []
    var listOrdered = false
    var quoteLines = []

    function flushParagraph() {
        if (paragraph.length === 0) return
        // Qt rich text collapses raw newlines in paragraphs, so soft line
        // breaks become explicit <br/> to match the old plain-text display.
        html.push("<p style=\"margin:0 0 8px 0;color:" + theme.ink + ";\">" + paragraph.map(function (l) { return inline(l, theme) }).join("<br/>") + "</p>")
        paragraph = []
    }

    function flushList() {
        if (listItems.length === 0) return
        var tag = listOrdered ? "ol" : "ul"
        html.push("<" + tag + " style=\"margin:0 0 8px 0; color:" + theme.ink + ";\">" + listItems.join("") + "</" + tag + ">")
        listItems = []
    }

    function flushQuote() {
        if (quoteLines.length === 0) return
        html.push("<blockquote style=\"margin:0 0 8px 0;color:" + theme.muted + ";\">" + quoteLines.map(function (l) { return inline(l.replace(/^\s{0,3}>\s?/, ""), theme) }).join("<br/>") + "</blockquote>")
        quoteLines = []
    }

    function flushAll() {
        flushParagraph()
        flushList()
        flushQuote()
    }

    for (var i = 0; i < lines.length; ++i) {
        var line = lines[i]
        if (inCode) {
            if (codeMarker.length > 0 && line.indexOf(codeMarker) === 0 && line.trim() === codeMarker) {
                html.push("<pre style=\"font-family:monospace;background-color:" + theme.code + ";color:" + theme.ink + ";padding:8px;\">" + escapeHtml(codeLines.join("\n")) + "</pre>")
                inCode = false
                codeLines = []
                codeMarker = ""
                continue
            }
            codeLines.push(line)
            continue
        }
        if (isFence(line)) {
            flushAll()
            inCode = true
            codeMarker = fenceMarker(line)
            codeLines = []
            continue
        }
        if (line.trim().length === 0) { flushAll(); continue }
        if (isRuleLine(line)) {
            flushAll()
            html.push("<p style=\"color:" + theme.muted + ";\">────────</p>")
            continue
        }
        if (isBlockquoteLine(line)) {
            flushParagraph()
            flushList()
            quoteLines.push(line)
            continue
        }
        flushQuote()
        if (isListLine(line)) {
            flushParagraph()
            var ordered = /^\s*\d+[.)]\s+/.test(line)
            if (listItems.length > 0 && ordered !== listOrdered) flushList()
            listOrdered = ordered
            var content = line.replace(/^\s*([-*+]|\d+[.)])\s+/, "")
            listItems.push("<li style=\"color:" + theme.ink + ";margin:0 0 4px 0;\">" + inline(content, theme) + "</li>")
            continue
        }
        flushList()
        if (isHeadingLine(line)) {
            flushParagraph()
            var marker = /^\s{0,3}(#{1,6})\s+/.exec(line)[1]
            var body = line.trim().slice(marker.length).trim()
            var level = Math.min(marker.length + 1, 6)
            html.push("<h" + level + " style=\"color:" + theme.ink + ";margin:8px 0 6px 0;\">" + inline(body, theme) + "</h" + level + ">")
            continue
        }
        paragraph.push(line)
        // A paragraph flushes at the next blank line or block construct;
        // soft line breaks are preserved via white-space:pre-wrap.
        var next = lines[i + 1]
        if (next === undefined || next.trim().length === 0 || isFence(next) || isListLine(next)
            || isBlockquoteLine(next) || isHeadingLine(next) || isRuleLine(next))
            flushParagraph()
    }
    if (inCode) {
        // Unterminated fence: still render what arrived as a code block.
        html.push("<pre style=\"font-family:monospace;background-color:" + theme.code + ";color:" + theme.ink + ";padding:8px;\">" + escapeHtml(codeLines.join("\n")) + "</pre>")
    }
    flushAll()
    return html.join("")
}
