<?php
/**
 * extract.php — exact symbol map for a PHP file.
 *
 * Uses token_get_all(), PHP's official tokenizer. This is not an
 * approximation: it is the same lexer the interpreter itself uses.
 *
 * Usage: php extract.php <file.php>
 * Emits: JSON listing every symbol with its exact byte offsets.
 *
 * NOTE: all offsets are BYTE offsets. Applying them to a character-indexed
 * string (a Python `str`, for instance) shifts the cut by one position per
 * multi-byte character preceding the symbol, and does so silently. Consumers
 * must slice bytes. See docs/calibration.md, Finding 1.
 *
 * Each symbol carries two possible starts:
 *   - start_byte:     the first modifier (public/private/static/final...)
 *   - doc_start_byte: the preceding docblock, if any
 * Choosing between them is a design decision, not a detail.
 */

if ($argc < 2) {
    fwrite(STDERR, "usage: php extract.php <file.php>\n");
    exit(2);
}

$path = $argv[1];
$src = @file_get_contents($path);
if ($src === false) {
    fwrite(STDERR, "cannot read: $path\n");
    exit(2);
}

$tokens = @token_get_all($src);
if ($tokens === false) {
    fwrite(STDERR, "token_get_all failed\n");
    exit(3);
}

// Normalise: every token becomes [id, text, line, offset].
// Concatenating the texts reconstructs the file byte for byte, which lets us
// derive exact offsets without guessing at anything.
$norm = [];
$offset = 0;
foreach ($tokens as $t) {
    if (is_array($t)) {
        $norm[] = ['id' => $t[0], 'text' => $t[1], 'line' => $t[2], 'off' => $offset];
        $offset += strlen($t[1]);
    } else {
        $norm[] = ['id' => -1, 'text' => $t, 'line' => null, 'off' => $offset];
        $offset += strlen($t);
    }
}

if ($offset !== strlen($src)) {
    // Sanity check: if this fails the offsets cannot be trusted, and it is
    // far better to say so now than to corrupt a file later.
    fwrite(STDERR, "WARNING: token reconstruction mismatch ($offset vs " . strlen($src) . ")\n");
    exit(4);
}

$n = count($norm);
$skippable = [T_WHITESPACE, T_COMMENT, T_DOC_COMMENT, T_ATTRIBUTE];
$modifiers = [T_PUBLIC, T_PRIVATE, T_PROTECTED, T_STATIC, T_ABSTRACT, T_FINAL, T_READONLY];
$containers = [T_CLASS, T_INTERFACE, T_TRAIT];
if (defined('T_ENUM')) { $containers[] = T_ENUM; }

// Braces that PHP tokenises as NAMED tokens rather than as a bare '{'.
// String interpolation is the trap: "{$a['b']}" yields T_CURLY_OPEN for the
// opening brace but a plain '}' character for the closing one. Counting only
// bare characters therefore sees one close too many, the depth goes negative
// early, and the symbol is truncated — silently, and only in files that use
// interpolation. This cost a real corrupted patch before it was found.
$openCurly = [T_CURLY_OPEN, T_DOLLAR_OPEN_CURLY_BRACES];
if (defined('T_STRING_VARNAME')) { /* companion token, no brace of its own */ }

/** Does this token open a brace? */
$opensBrace = function (array $t) use ($openCurly): bool {
    return ($t['id'] === -1 && $t['text'] === '{')
        || in_array($t['id'], $openCurly, true);
};

$symbols = [];
$classStack = []; // [name, brace depth at open]
$depth = 0;

for ($i = 0; $i < $n; $i++) {
    $tok = $norm[$i];

    if (in_array($tok['id'], $openCurly, true)) { $depth++; continue; }

    if ($tok['id'] === -1) {
        if ($tok['text'] === '{') { $depth++; }
        elseif ($tok['text'] === '}') {
            $depth--;
            // We leave a class when we close the brace it opened at.
            while (!empty($classStack) && end($classStack)['depth'] > $depth) {
                array_pop($classStack);
            }
        }
        continue;
    }

    // --- Containers (class / interface / trait / enum) ---
    if (in_array($tok['id'], $containers, true)) {
        // `Foo::class` and anonymous classes carry no usable name.
        $j = $i + 1;
        while ($j < $n && in_array($norm[$j]['id'], $skippable, true)) { $j++; }
        if ($j < $n && $norm[$j]['id'] === T_STRING) {
            $classStack[] = ['name' => $norm[$j]['text'], 'depth' => $depth];
        }
        continue;
    }

    // --- Functions and methods ---
    if ($tok['id'] !== T_FUNCTION) { continue; }

    // Name. If the next significant token is '(' this is a closure: skip it,
    // it is not addressable by name.
    $j = $i + 1;
    while ($j < $n && in_array($norm[$j]['id'], $skippable, true)) { $j++; }
    if ($j >= $n) { break; }
    if ($norm[$j]['id'] === -1 && $norm[$j]['text'] === '(') { continue; }
    if ($norm[$j]['id'] === -1 && $norm[$j]['text'] === '&') {
        $j++;
        while ($j < $n && in_array($norm[$j]['id'], $skippable, true)) { $j++; }
    }
    if ($j >= $n || $norm[$j]['id'] !== T_STRING) { continue; }
    $name = $norm[$j]['text'];

    // Real start: walk back over the modifiers.
    $start = $i;
    $k = $i - 1;
    while ($k >= 0) {
        if (in_array($norm[$k]['id'], [T_WHITESPACE], true)) { $k--; continue; }
        if (in_array($norm[$k]['id'], $modifiers, true)) { $start = $k; $k--; continue; }
        break;
    }

    // Immediately preceding docblock (only if no code sits in between).
    $docStart = null;
    $k = $start - 1;
    while ($k >= 0 && $norm[$k]['id'] === T_WHITESPACE) { $k--; }
    if ($k >= 0 && $norm[$k]['id'] === T_DOC_COMMENT) { $docStart = $norm[$k]['off']; }

    // Body: find the opening '{' at the same level, or ';' if abstract.
    $bodyDepth = 0;
    $endOff = null;
    $isAbstract = false;
    for ($m = $j + 1; $m < $n; $m++) {
        $t2 = $norm[$m];
        if ($t2['id'] !== -1) { continue; }  // named tokens open no plain brace
        if ($t2['text'] === '(') { $bodyDepth++; }
        elseif ($t2['text'] === ')') { $bodyDepth--; }
        elseif ($t2['text'] === ';' && $bodyDepth === 0) {
            // Declaration with no body (interface / abstract).
            $isAbstract = true;
            $endOff = $t2['off'] + 1;
            break;
        } elseif ($t2['text'] === '{' && $bodyDepth === 0) {
            // Match braces until the block closes. Interpolation braces count
            // too — see $opensBrace above.
            $d = 0;
            for ($q = $m; $q < $n; $q++) {
                if ($opensBrace($norm[$q])) { $d++; continue; }
                if ($norm[$q]['id'] !== -1) { continue; }
                if ($norm[$q]['text'] === '}') {
                    $d--;
                    if ($d === 0) { $endOff = $norm[$q]['off'] + 1; break; }
                }
            }
            break;
        }
    }

    if ($endOff === null) { continue; }

    $startOff = $norm[$start]['off'];
    $class = empty($classStack) ? null : end($classStack)['name'];

    $symbols[] = [
        'name'           => $name,
        'class'          => $class,
        'fqn'            => $class ? "$class::$name" : $name,
        'abstract'       => $isAbstract,
        'start_byte'     => $startOff,
        'end_byte'       => $endOff,
        'doc_start_byte' => $docStart,
        'start_line'     => substr_count(substr($src, 0, $startOff), "\n") + 1,
        'end_line'       => substr_count(substr($src, 0, $endOff), "\n") + 1,
        'n_lines'        => substr_count(substr($src, $startOff, $endOff - $startOff), "\n") + 1,
        'indent'         => (function () use ($src, $startOff) {
            // Text between start of line and the symbol. If it is nothing but
            // whitespace, that whitespace is the symbol's indentation.
            $ls = strrpos(substr($src, 0, $startOff), "\n");
            $ls = ($ls === false) ? 0 : $ls + 1;
            $prefix = substr($src, $ls, $startOff - $ls);
            return (trim($prefix) === '') ? $prefix : null;
        })(),
    ];
}


// ---------------------------------------------------------------- switch cases
//
// A `case` label is not a PHP symbol — no parser will hand you one — yet in a
// router-style file it is the unit everything is added to. Located here by the
// same tokenizer, so it inherits the same guarantees about offsets.
//
// A case ENDS after its last real statement (typically `break;`), not at the
// start of the next `case`. The gap between the two usually holds a blank line
// and a section comment that belongs to what follows; ending early leaves them
// where their author put them.

$cases = [];
$switchStack = [];
$cdepth = 0;

/** Close the case currently open on the top of the stack, ending it at the
 *  last real token before $upTo. */
$closeCase = function (int $top, int $upTo) use (&$switchStack, &$cases, $norm, $src) {
    if ($switchStack[$top]['start'] === null) { return; }
    $skip = [T_WHITESPACE, T_COMMENT, T_DOC_COMMENT];

    // Fall-through: no statement of its own between this label's colon and
    // whatever comes next. The branch is a bare label that drops into the one
    // below it, which matters because inserting after it would cut the chain
    // and change what the code does while still parsing cleanly.
    $fell = true;
    for ($q = $switchStack[$top]['labelTok'] + 1; $q < $upTo; $q++) {
        if (in_array($norm[$q]['id'], $skip, true)) { continue; }
        $fell = false;
        break;
    }

    if ($fell) {
        $endOff = $switchStack[$top]['labelEnd'];
    } else {
        $endOff = null;
        for ($q = $upTo - 1; $q > $switchStack[$top]['startTok']; $q--) {
            if (in_array($norm[$q]['id'], $skip, true)) { continue; }
            $endOff = $norm[$q]['off'] + strlen($norm[$q]['text']);
            break;
        }
        if ($endOff === null) { $endOff = $switchStack[$top]['labelEnd']; }
    }
    $cases[] = [
        'label'       => $switchStack[$top]['label'],
        'start_byte'  => $switchStack[$top]['start'],
        'end_byte'    => $endOff,
        'indent'      => $switchStack[$top]['indent'],
        'fallthrough' => $fell,
        'start_line'  => substr_count(substr($src, 0, $switchStack[$top]['start']), "\n") + 1,
        'end_line'    => substr_count(substr($src, 0, $endOff), "\n") + 1,
    ];
    $switchStack[$top]['start'] = null;
};

for ($i = 0; $i < $n; $i++) {
    $tok = $norm[$i];

    // Brace depth for this pass. A switch body must be popped when its own
    // brace closes — not left on the stack until the end of the file, which
    // made every case after a nested switch get attributed to the inner one
    // and silently dropped the outer branches that followed.
    if ($opensBrace($tok)) { $cdepth++; continue; }
    if ($tok['id'] === -1 && $tok['text'] === '}') {
        $cdepth--;
        while (!empty($switchStack) && end($switchStack)['depth'] > $cdepth) {
            $top = count($switchStack) - 1;
            $closeCase($top, $i);
            array_pop($switchStack);
        }
        continue;
    }

    if ($tok['id'] === T_SWITCH) {
        $d = 0;
        for ($m = $i + 1; $m < $n; $m++) {
            if ($norm[$m]['id'] !== -1) { continue; }
            if ($norm[$m]['text'] === '(') { $d++; }
            elseif ($norm[$m]['text'] === ')') { $d--; }
            elseif ($norm[$m]['text'] === '{' && $d === 0) {
                // +1: the body's contents sit one level inside this brace.
                $switchStack[] = ['depth' => $cdepth + 1, 'label' => null,
                                  'start' => null, 'startTok' => null,
                                  'indent' => null, 'labelEnd' => null,
                                  'labelTok' => null];
                break;
            }
        }
        continue;
    }

    if (empty($switchStack)) { continue; }
    if ($tok['id'] !== T_CASE && $tok['id'] !== T_DEFAULT) { continue; }

    $top = count($switchStack) - 1;
    // A case belongs to the innermost OPEN switch only if it sits at that
    // switch's own depth.
    if ($cdepth !== $switchStack[$top]['depth']) { continue; }

    $label = ($tok['id'] === T_DEFAULT) ? 'default' : '';
    $labelEnd = null;
    for ($m = $i + 1; $m < $n; $m++) {
        if ($norm[$m]['id'] === -1 && ($norm[$m]['text'] === ':' || $norm[$m]['text'] === ';')) {
            $labelEnd = $m;
            break;
        }
        if ($tok['id'] === T_CASE) { $label .= $norm[$m]['text']; }
    }
    if ($labelEnd === null) { continue; }
    $label = trim(trim($label), "'\"");

    $closeCase($top, $i);

    $startOff = $tok['off'];
    $ls = strrpos(substr($src, 0, $startOff), "\n");
    $ls = ($ls === false) ? 0 : $ls + 1;
    $prefix = substr($src, $ls, $startOff - $ls);

    $switchStack[$top]['label']    = $label;
    $switchStack[$top]['start']    = $startOff;
    $switchStack[$top]['startTok'] = $i;
    $switchStack[$top]['labelEnd'] = $norm[$labelEnd]['off'] + 1;
    $switchStack[$top]['labelTok'] = $labelEnd;
    $switchStack[$top]['indent']   = (trim($prefix) === '') ? $prefix : '';
}

// Anything still open ran to the end of the file.
while (!empty($switchStack)) {
    $top = count($switchStack) - 1;
    $closeCase($top, $n);
    array_pop($switchStack);
}

usort($cases, fn($a, $b) => $a['start_byte'] <=> $b['start_byte']);

echo json_encode([
    'file'      => $path,
    'bytes'     => strlen($src),
    'lines'     => substr_count($src, "\n") + 1,
    'n_symbols' => count($symbols),
    'symbols'   => $symbols,
    'n_cases'   => count($cases),
    'cases'     => $cases,
], JSON_PRETTY_PRINT | JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES), "\n";
