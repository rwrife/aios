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
//   - LaTeX math ($...$, $$...$$, \( \), \[ \]) mapped to Unicode glyphs,
//     <sup>/<sub> scripts, and linearized fractions and roots
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
        if (tick < 0) { out += mathAware(text.slice(i), colors); break }
        out += mathAware(text.slice(i, tick), colors)
        var close = text.indexOf("`", tick + 1)
        if (close < 0) { out += escapeHtml(text.slice(tick)); break }
        var code = text.slice(tick + 1, close)
        out += "<span style=\"font-family:monospace;background-color:" + colors.code + ";color:" + colors.ink + ";\">" + escapeHtml(code) + "</span>"
        i = close + 1
    }
    return out
}

// mathAware renders one code-free segment: LaTeX math spans are terminal
// (their contents are never treated as markdown emphasis), and the prose
// around them goes through the normal emphasis/link rules. Delimiters:
// $...$ (inline, gated by looksLikeMath), $$...$$, \( \) and \[ \].
function mathAware(segment, colors) {
    var text = segment
    var out = ""
    var i = 0
    function prose(upto) {
        if (upto > i) out += emphasis(text.slice(i, upto), colors)
    }
    while (i < text.length) {
        var posD = text.indexOf("$", i)
        var posP = text.indexOf("\\(", i)
        var posB = text.indexOf("\\[", i)
        var start = -1
        var opener = ""
        var closer = ""
        function take(pos, op, cl) {
            if (pos < 0) return
            if (start < 0 || pos < start || (pos === start && op.length > opener.length)) {
                start = pos; opener = op; closer = cl
            }
        }
        if (posD >= 0) {
            // A lone $ only opens inline math when it is not part of a $$ pair.
            if (text.charAt(posD + 1) !== "$") take(posD, "$", "$")
            if (text.charAt(posD + 1) === "$" && (posD === i || text.charAt(posD - 1) !== "$"))
                take(posD, "$$", "$$")
        }
        take(posP, "\\(", "\\)")
        take(posB, "\\[", "\\]")
        if (start < 0) break
        var contentStart = start + opener.length
        var end = text.indexOf(closer, contentStart)
        if (end < 0) {
            // Unclosed delimiter: everything from here on stays prose verbatim.
            prose(start); out += emphasis(text.slice(start), colors)
            i = text.length
            break
        }
        var inner = text.slice(contentStart, end)
        var rendered = opener === "$" && !looksLikeMath(inner) ? null : renderMath(inner)
        if (rendered === null) {
            // Fail closed: the raw span survives as ordinary prose.
            prose(start)
            out += emphasis(text.slice(start, end + closer.length), colors)
            i = end + closer.length
            continue
        }
        prose(start)
        out += "<span style=\"font-style:italic;\">" + rendered + "</span>"
        i = end + closer.length
    }
    prose(text.length)
    return out
}

// looksLikeMath is deliberately conservative: a $...$ span becomes math only
// when it carries an explicit LaTeX signal (a command, ^ or _) or is a single
// math-like token starting with a letter, so "$5 and $10 total" and any
// sentence-with-spaces pair stay prose.
function looksLikeMath(inner) {
    if (inner.length === 0 || inner.length > 300) return false
    if (/^\s|\s$/.test(inner)) return false
    if (/^[0-9]/.test(inner)) return false
    if (inner.indexOf("$") >= 0) return false
    if (/\\[a-zA-Z]/.test(inner) || inner.indexOf("^") >= 0 || inner.indexOf("_") >= 0)
        return true
    return /^[A-Za-z][A-Za-z0-9+\-*/{}().|=<>!]*$/.test(inner)
}


// Symbol table for LaTeX commands Qt rich text cannot typeset directly.
// Values are plain Unicode so the shell stays dependency-free (no KaTeX/CDN).
var MATH_SYMBOLS = {
    alpha: "\u03b1", beta: "\u03b2", gamma: "\u03b3", delta: "\u03b4",
    epsilon: "\u03b5", varepsilon: "\u03b5", zeta: "\u03b6", eta: "\u03b7",
    theta: "\u03b8", vartheta: "\u03d1", iota: "\u03b9", kappa: "\u03ba",
    lambda: "\u03bb", mu: "\u03bc", nu: "\u03bd", xi: "\u03be",
    pi: "\u03c0", rho: "\u03c1", sigma: "\u03c3", tau: "\u03c4",
    upsilon: "\u03c5", phi: "\u03c6", varphi: "\u03c6", chi: "\u03c7",
    psi: "\u03c8", omega: "\u03c9",
    Gamma: "\u0393", Delta: "\u0394", Theta: "\u0398", Lambda: "\u039b",
    Xi: "\u039e", Pi: "\u03a0", Sigma: "\u03a3", Upsilon: "\u03a5",
    Phi: "\u03a6", Psi: "\u03a8", Omega: "\u03a9",
    Alpha: "\u0391", Beta: "\u0392", Epsilon: "\u0395",
    Zeta: "\u0396", Eta: "\u0397", Iota: "\u0399", Kappa: "\u039a",
    Mu: "\u039c", Nu: "\u039d", Omicron: "\u039f", Rho: "\u03a1",
    Tau: "\u03a4", Chi: "\u03a7",
    sum: "\u2211", prod: "\u220f", int: "\u222b", oint: "\u222e",
    infty: "\u221e", partial: "\u2202", nabla: "\u2207",
    in: "\u2208", notin: "\u2209", subset: "\u2282", supset: "\u2283",
    subseteq: "\u2286", supseteq: "\u2287", emptyset: "\u2205",
    cup: "\u222a", cap: "\u2229", setminus: "\u2216",
    forall: "\u2200", exists: "\u2203",
    to: "\u2192", rightarrow: "\u2192",
    leftarrow: "\u2190", Rightarrow: "\u21d2", Leftarrow: "\u21d0",
    leftrightarrow: "\u2194", Leftrightarrow: "\u21d4", mapsto: "\u21a6",
    leq: "\u2264", le: "\u2264", geq: "\u2265", ge: "\u2265",
    neq: "\u2260", ne: "\u2260", approx: "\u2248", equiv: "\u2261",
    sim: "\u223c", cong: "\u2245", propto: "\u221d",
    times: "\u00d7", div: "\u00f7", cdot: "\u22c5", ast: "\u2217",
    pm: "\u00b1", mp: "\u2213", circ: "\u2218", bullet: "\u2219",
    oplus: "\u2295", ominus: "\u2296", otimes: "\u2297",
    wedge: "\u2227", land: "\u2227", vee: "\u2228", lor: "\u2228",
    neg: "\u00ac", lnot: "\u00ac", perp: "\u22a5", parallel: "\u2225",
    angle: "\u2220", triangle: "\u25b3",
    langle: "\u27e8", rangle: "\u27e9",
    lceil: "\u2308", rceil: "\u2309", lfloor: "\u230a", rfloor: "\u230b",
    ldots: "\u2026", cdots: "\u22ef", vdots: "\u22ee", dots: "\u2026",
    ell: "\u2113", hbar: "\u210f", Re: "\u211c", Im: "\u2111",
    aleph: "\u2135", degree: "\u00b0"
}

var MATH_DOUBLE_STRUCK = { R: "\u211d", N: "\u2115", Z: "\u2124", Q: "\u211a", C: "\u2102", P: "\u2119", F: "\u210d" }
var MATH_WORDS = { deg: 1, gcd: 1, lim: 1, ln: 1, log: 1, max: 1, min: 1, sup: 1, inf: 1, sin: 1, cos: 1, tan: 1, cot: 1, sec: 1, csc: 1, exp: 1, arg: 1, det: 1, dim: 1, ker: 1 }

// renderMath converts one LaTeX source string into the Qt rich-text subset
// (Unicode symbols plus <sup>/<sub>); fractions and radicals linearize as
// (a)/(b) and \u221a(a). Returns null to reject the span entirely: matrices,
// alignments, unbalanced braces and oversized input must not render as
// misleading half-translations, so the caller shows the raw source instead.
// All text is escaped here; only sup/sub/span tags are ever emitted.
function renderMath(source) {
    var src = String(source)
    if (src.length === 0 || src.length > 4000) return null
    var pos = 0
    function skipSpace() { while (pos < src.length && /\s/.test(src.charAt(pos))) ++pos }

    // parse(stopAtBrace) returns escaped HTML for the consumed run, or null.
    function parse(stopAtBrace) {
        var out = ""
        while (pos < src.length) {
            var ch = src.charAt(pos)
            if (ch === "}") {
                if (stopAtBrace) return out
                return null // stray closer
            }
            if (ch === "{") {
                ++pos
                var inner = parse(true)
                if (inner === null || src.charAt(pos) !== "}") return null
                ++pos
                out += inner
                continue
            }
            if (ch === "^" || ch === "_") {
                ++pos
                var script = parseArg()
                if (script === null) return null
                out += "<" + (ch === "^" ? "sup" : "sub") + ">" + script + "</" + (ch === "^" ? "sup" : "sub") + ">"
                continue
            }
            if (ch === "\\") {
                var cmd = parseCommand()
                if (cmd === null) return null
                out += cmd
                continue
            }
            // & # ~ are multi-row/alignment constructs we cannot typeset;
            // a bare $ cannot occur (spans were cut at closers).
            if (ch === "&" || ch === "#" || ch === "$" || ch === "~") return null
            out += escapeHtml(ch)
            ++pos
        }
        return stopAtBrace ? null : out
    }

    // parseArg reads one script/frac/sqrt argument: {group}, a single safe
    // character, or a single math command (so \frac12, x^\pi and \sqrt2 work).
    function parseArg() {
        skipSpace()
        var ch = src.charAt(pos)
        if (ch === "{") {
            ++pos
            var inner = parse(true)
            if (inner === null || src.charAt(pos) !== "}") return null
            ++pos
            return inner
        }
        if (ch === "\\") return parseCommand()
        if (/[A-Za-z0-9+\-=()<>|.,!*/'"]/.test(ch)) { ++pos; return escapeHtml(ch) }
        return null
    }

    function parseCommand() {
        ++pos // consume backslash
        var start = pos
        while (pos < src.length && /[a-zA-Z]/.test(src.charAt(pos))) ++pos
        if (start === pos) {
            // Escaped symbol: \{ \} \% \$ \_ \\ \, \; \! \: \<space>
            var esc = src.charAt(pos)
            if (esc === undefined) return null
            pos++
            if (esc === "," || esc === ";" || esc === ":") return "<span style=\"letter-spacing:6px;\">" + escapeHtml(" ") + "</span>"
            if (esc === "!") return ""
            if (esc === " " || esc === "\\") return " "
            return escapeHtml(esc)
        }
        var name = src.slice(start, pos)
        // Matrix/aligned/cases environments cannot be typeset in Qt rich
        // text; reject the whole span instead of a misleading half-render.
        if (name === "begin" || name === "end") return null
        if (name === "frac" || name === "dfrac" || name === "tfrac" || name === "cfrac") {
            var num = parseArg(); if (num === null) return null
            var den = parseArg(); if (den === null) return null
            return "(" + num + ")/(" + den + ")"
        }
        if (name === "sqrt") {
            var root = ""
            skipSpace()
            if (src.charAt(pos) === "[") {
                var rStart = ++pos
                while (pos < src.length && src.charAt(pos) !== "]") ++pos
                if (pos >= src.length) return null
                root = "[" + escapeHtml(src.slice(rStart, pos)) + "]"
                ++pos
            }
            var under = parseArg(); if (under === null) return null
            return "\u221a" + root + "(" + under + ")"
        }
        if (name === "text" || name === "mathrm" || name === "mbox" || name === "operatorname") {
            var body = parseArg(); if (body === null) return null
            return body
        }
        if (name === "mathbf" || name === "boldsymbol") {
            var bold = parseArg(); if (bold === null) return null
            return "<span style=\"font-weight:600;\">" + bold + "</span>"
        }
        if (name === "mathit") {
            var ital = parseArg(); if (ital === null) return null
            return ital
        }
        if (name === "mathbb") {
            skipSpace()
            var letter = src.charAt(pos)
            if (letter === "{") {
                var g = parseArg(); if (g === null) return null
                if (g.length === 1 && MATH_DOUBLE_STRUCK[g] !== undefined) return MATH_DOUBLE_STRUCK[g]
                return g
            }
            if (MATH_DOUBLE_STRUCK[letter] !== undefined) { ++pos; return MATH_DOUBLE_STRUCK[letter] }
            return null
        }
        if (name === "left" || name === "right" || name === "big" || name === "Big" ||
            name === "bigg" || name === "Bigg" || name === "bigl" || name === "bigr" ||
            name === "bigm" || name === "bigs") {
            skipSpace()
            var delim = src.charAt(pos)
            if (delim === undefined) return null
            if (delim === "\\") return parseCommand() // \left\{ ... 
            if (delim === ".") { ++pos; return "" }
            ++pos
            return escapeHtml(delim === "{" ? "{" : delim)
        }
        if (MATH_WORDS[name]) return name + "\u2061"
        if (MATH_SYMBOLS[name] !== undefined) return MATH_SYMBOLS[name]
        // Unknown command: keep it verbatim so nothing silently changes meaning.
        return escapeHtml("\\" + name)
    }

    var html = parse(false)
    return html === null || html.length === 0 ? null : html
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

// splitChoices extracts an option-picker fence from a reply so the chat can
// render the options as clickable buttons instead of a code block. The wire
// contract taught by the agent POLICY is:
//
//     ```Choose
//     Native app
//     Web app
//     ```
//
// The fence may also be plain ``` whose first inner line is "Choose" (or
// "Choose:"). Parsing is fail-closed: anything incomplete, oversized, or
// malformed stays in `text` and renders as the ordinary code block the fence
// already is, so a half-streamed or mis-formatted block can never lose the
// user's content. Returns { text: string, choices: [string] }.
function splitChoices(text) {
    var raw = text === undefined || text === null ? "" : String(text)
    var noBlock = { text: raw, choices: [] }
    if (raw.length === 0) return noBlock
    var lines = raw.replace(/\r\n?/g, "\n").split("\n")
    var openIndex = -1
    var marker = ""
    var optionStart = -1
    for (var i = 0; i < lines.length && openIndex < 0; ++i) {
        if (!isFence(lines[i])) continue
        marker = fenceMarker(lines[i])
        var tag = lines[i].trim().slice(marker.length).trim().replace(/:$/, "").toLowerCase()
        if (tag === "choose") { openIndex = i; optionStart = i + 1; continue }
        // Plain fence: the keyword may sit on the first inner line instead.
        var next = i + 1 < lines.length ? lines[i + 1].trim().replace(/:$/, "").toLowerCase() : ""
        if (next === "choose") { openIndex = i; optionStart = i + 2 }
    }
    if (openIndex < 0) return noBlock
    var choices = []
    var closeIndex = -1
    for (var j = optionStart; j < lines.length; ++j) {
        var line = lines[j]
        if (line.indexOf(marker) === 0 && line.trim() === marker) { closeIndex = j; break }
        var label = line.trim().replace(/^([-*+]|\d+[.)])\s+/, "").trim()
        if (label.length === 0) continue
        if (label.length > 120) { openIndex = -1; break }
        choices.push(label)
    }
    // Incomplete or degenerate blocks fall back to plain rendering: during
    // streaming the block only appears as buttons once it is closed.
    if (openIndex < 0 || closeIndex < 0 || choices.length < 2 || choices.length > 6)
        return noBlock
    var rest = lines.slice(0, openIndex).concat(lines.slice(closeIndex + 1))
    return { text: rest.join("\n"), choices: choices }
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
    var inMath = false
    var mathLines = []
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
        if (inMath) {
            var trimmedMath = line.trim()
            if (trimmedMath === "$$" || trimmedMath === "\\]") {
                flushAll()
                var mathHtml = renderMath(mathLines.join(" ").trim())
                if (mathHtml === null)
                    html.push("<pre style=\"font-family:monospace;background-color:" + theme.code + ";color:" + theme.ink + ";padding:8px;\">" + escapeHtml(mathLines.join("\n")) + "</pre>")
                else
                    html.push("<p style=\"margin:0 0 8px 0;text-align:center;font-style:italic;color:" + theme.ink + ";\">" + mathHtml + "</p>")
                inMath = false
                mathLines = []
                continue
            }
            mathLines.push(line)
            continue
        }
        var edge = line.trim()
        if (edge === "$$" || edge === "\\[") {
            flushAll()
            inMath = true
            mathLines = []
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
    if (inMath) {
        // Unterminated display block: render the collected source verbatim.
        flushAll()
        html.push("<pre style=\"font-family:monospace;background-color:" + theme.code + ";color:" + theme.ink + ";padding:8px;\">" + escapeHtml(mathLines.join("\n")) + "</pre>")
    }
    flushAll()
    return html.join("")
}
