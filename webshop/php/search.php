<?php
// Zoekt in de catalogus. GET ?q=...&page=1 -> JSON {ok, items[], total, per_page}
require __DIR__ . '/lib.php';
require __DIR__ . '/norm.php';
$lang = pick_lang($_GET['lang'] ?? 'nl');

$per = 24;
$q = norm(substr((string)($_GET['q'] ?? ''), 0, 100));
$page = max(1, min(200, (int)($_GET['page'] ?? 1)));
if (strlen($q) < 2) { json_out(['ok' => false, 'error' => tr('php_search_min', $lang)], 400); }
$tokens = array_slice(array_unique(explode(' ', $q)), 0, 6);
if (!extension_loaded('pdo_sqlite')) { json_out(['ok' => false, 'code' => 'no_pdo_sqlite', 'error' => tr('php_search_fail', $lang)], 500); }
if (!is_file(data_path('catalog.sqlite'))) { json_out(['ok' => false, 'code' => 'no_catalog', 'error' => tr('php_search_fail', $lang)], 500); }
// Zoekt de tokens (allemaal moeten voorkomen); geeft [aantal, rijen] terug.
function search_books($db, $tokens, $q, $per, $page) {
    $where = []; $args = [];
    foreach ($tokens as $t) { $where[] = 'instr(hay, ?) > 0'; $args[] = $t; }
    $cond = implode(' AND ', $where);
    $st = $db->prepare("SELECT COUNT(*) AS n FROM books WHERE $cond");
    $st->execute($args);
    $total = (int)$st->fetch()['n'];
    if ($total === 0) { return [0, []]; }
    // Titel die met de zoekterm begint of hem helemaal bevat gaat voor; daarna nieuwste eerst.
    $st = $db->prepare("SELECT id, title, author, price_cents, ship_cents, big, amount, path, img, iw, ih FROM books WHERE $cond
        ORDER BY CASE WHEN instr(tnorm, ?) = 1 THEN 0 WHEN instr(tnorm, ?) > 0 THEN 1 ELSE 2 END, seq LIMIT $per OFFSET " . (($page - 1) * $per));
    $st->execute(array_merge($args, [$q, $q]));
    return [$total, $st->fetchAll()];
}

// Tikfouten opvangen: levert de zoekterm niets op, dan wordt elk woord dat nergens voorkomt vervangen door het woord uit de
// catalogus dat er het meest op lijkt (hooguit 1 afwijkende letter bij korte woorden, 2 bij middellange, 3 bij lange).
// Zo vindt 'mysopotamisch' toch 'mesopotamische'. Geeft de verbeterde woorden terug, of null als er niets te verbeteren valt.
function fuzzy_tokens($db, $tokens, $table = 'books', $col = 'hay') {
    $missing = [];
    $st = $db->prepare("SELECT 1 FROM $table WHERE instr($col, ?) > 0 LIMIT 1");
    foreach ($tokens as $i => $t) {
        $st->execute([$t]);
        if (!$st->fetch()) { $missing[] = $i; }
        $st->closeCursor();
    }
    if (!$missing) { return null; }   // elk woord bestaat wel, alleen niet samen in één boek: daar valt niets aan te verbeteren
    $words = [];
    foreach ($db->query("SELECT $col AS hay FROM $table") as $r) {
        foreach (explode(' ', $r['hay']) as $w) {
            if (strlen($w) >= 3 && strlen($w) <= 40) { $words[$w] = ($words[$w] ?? 0) + 1; }
        }
    }
    foreach ($missing as $i) {
        $t = $tokens[$i]; $len = strlen($t);
        if ($len < 4 || $len > 40 || ctype_digit($t)) { return null; }   // te kort om te raden, of een (ISBN-)getal
        $max = $len <= 5 ? 1 : ($len <= 8 ? 2 : 3);
        $best = null; $bestScore = null;
        foreach ($words as $w => $n) {
            $wl = strlen($w);
            if ($wl < $len - $max) { continue; }
            // Het hele woord, of (bij een langer woord) het begin ervan: 'mysopotamisch' lijkt op het begin van 'mesopotamische'.
            $cand = $wl > $len + $max ? substr($w, 0, $len) : $w;
            $d = levenshtein($t, $cand);
            if ($wl > $len && $cand === $w) {
                $d2 = levenshtein($t, substr($w, 0, $len));
                if ($d2 < $d) { $d = $d2; $cand = substr($w, 0, $len); }
            }
            if ($d > $max) { continue; }
            // Kleinste afwijking wint; daarna dezelfde beginletter, daarna het woord dat het vaakst voorkomt.
            $score = [$d, $w[0] === $t[0] ? 0 : 1, -$n];
            if ($bestScore === null || $score < $bestScore) { $bestScore = $score; $best = $cand; }
        }
        if ($best === null) { return null; }
        $tokens[$i] = $best;
    }
    return array_values(array_unique($tokens));
}

// Niets gevonden: hoort de zoekterm bij een categorie? De termen komen uit Categoriebeheer (plus de categorienamen zelf).
// Alle woorden moeten in één term voorkomen en samen het grootste deel van die term zijn; een term die precies de zoekterm is gaat voor, daarna de categorie met de
// meeste passende termen. Lukt dat niet, dan nog een poging met verbeterde spelling. Geeft het categoriepad terug of null.
function find_category($db, $tokens, $q) {
    foreach ($tokens as $t) { if (strlen($t) < 3) { return null; } }
    $pick = function ($tokens, $q) use ($db) {
        $where = []; $args = []; $len = 0;
        foreach ($tokens as $t) { $where[] = 'instr(term, ?) > 0'; $args[] = $t; $len += strlen($t); }
        // De zoekterm moet het grootste deel van de term zijn (minstens 60% van de letters). Zo past 'detective' bij
        // 'detectives', maar 'japan' niet bij 'detectives japan': zulke bijzaken in lange termen tellen niet mee.
        $where[] = "? * 10 >= length(replace(term, ' ', '')) * 6";
        $args[] = $len;
        $st = $db->prepare('SELECT cat FROM cat_terms WHERE ' . implode(' AND ', $where) .
            ' GROUP BY cat ORDER BY MAX(term = ?) DESC, COUNT(*) DESC, length(cat) DESC LIMIT 1');
        $st->execute(array_merge($args, [$q]));
        $r = $st->fetch();
        return $r ? $r['cat'] : null;
    };
    $cat = $pick($tokens, $q);
    if ($cat === null) {
        $fixed = fuzzy_tokens($db, $tokens, 'cat_terms', 'term');
        if ($fixed) { $cat = $pick($fixed, implode(' ', $fixed)); }
    }
    return $cat;
}

// Naam en adres van een categorie in de taal van de bezoeker. De vertaalde naam staat niet in de catalogus; die lezen we uit
// de kop van de categoriepagina zelf (lukt dat niet, dan de Nederlandse naam).
function category_info($db, $cat, $lang) {
    $st = $db->prepare('SELECT label FROM cats WHERE cat = ?');
    $st->execute([$cat]);
    $r = $st->fetch();
    $label = $r ? (string)$r['label'] : '';
    $url = $cat;
    if ($lang !== 'nl' && strpos($cat, '/categorie/') === 0) {
        $url = (i18n()['paths']['category'][$lang] ?? '/categorie/') . substr($cat, 11);
        $file = rtrim((string)($_SERVER['DOCUMENT_ROOT'] ?? ''), '/') . $url . 'index.html';
        if (preg_match('~^[a-z0-9/\-]+$~', $url) && is_file($file)) {
            $html = (string)file_get_contents($file, false, null, 0, 60000);
            if (preg_match('~<h1[^>]*>(.*?)</h1>~s', $html, $m)) {
                $found = trim(html_entity_decode(strip_tags($m[1]), ENT_QUOTES | ENT_HTML5, 'UTF-8'));
                if ($found !== '') { $label = $found; }
            }
        }
    }
    return ['label' => $label, 'url' => $url];
}

try {
    $db = db_catalog();
    $meant = null;
    [$total, $rows] = search_books($db, $tokens, $q, $per, $page);
    if ($total === 0) {
        $fixed = fuzzy_tokens($db, $tokens);
        if ($fixed) {
            $fixedQ = implode(' ', $fixed);
            [$total, $rows] = search_books($db, $fixed, $fixedQ, $per, $page);
            if ($total > 0) { $meant = $fixedQ; }
        }
    }
    $catInfo = null;
    if ($total === 0) {
        try {   // een oudere catalogus heeft deze tabellen nog niet: dan gewoon 'niets gevonden'
            $cat = find_category($db, $tokens, $q);
            if ($cat !== null) {
                $st = $db->prepare("SELECT COUNT(*) AS n FROM books WHERE cat = ? OR cat LIKE ? || '%'");
                $st->execute([$cat, $cat]);
                $n = (int)$st->fetch()['n'];
                if ($n > 0) {
                    $st = $db->prepare("SELECT id, title, author, price_cents, ship_cents, big, amount, path, img, iw, ih FROM books WHERE cat = ? OR cat LIKE ? || '%'
                        ORDER BY seq LIMIT $per OFFSET " . (($page - 1) * $per));
                    $st->execute([$cat, $cat]);
                    $rows = $st->fetchAll();
                    $total = $n;
                    $catInfo = category_info($db, $cat, $lang);
                }
            }
        } catch (Throwable $e) { $catInfo = null; }
    }
    $items = [];
    foreach ($rows as $r) {
        // In de catalogus staat het Nederlandse pad (/boek/...); de link gaat naar de pagina in de taal van de bezoeker.
        $url = $r['path'];
        if (strpos($url, '/boek/') === 0) { $url = (i18n()['paths']['book'][$lang] ?? '/boek/') . substr($url, 6); }
        // id, ship, big, max en path (het Nederlandse pad) zijn voor de knop 'in winkelwagen' op het kaartje.
        $items[] = ['title' => $r['title'], 'author' => $r['author'], 'price' => (int)$r['price_cents'], 'url' => $url,
                    'img' => $r['img'], 'w' => (int)$r['iw'], 'h' => (int)$r['ih'], 'id' => (int)$r['id'],
                    'ship' => (int)$r['ship_cents'], 'big' => (int)$r['big'], 'max' => (int)$r['amount'], 'path' => $r['path']];
    }
    $out = ['ok' => true, 'items' => $items, 'total' => $total, 'per_page' => $per];
    if ($meant !== null) { $out['meant'] = $meant; }
    if ($catInfo !== null) { $out['cat'] = $catInfo; }   // niets gevonden: dit zijn de boeken uit een passende categorie   // de zoekterm is verbeterd: de pagina meldt waarop is gezocht
    json_out($out, 200, 'public, max-age=60');
} catch (Throwable $e) {
    error_log('search.php: ' . $e->getMessage());
    json_out(['ok' => false, 'code' => 'exception', 'error' => tr('php_search_fail', $lang)], 500);
}
