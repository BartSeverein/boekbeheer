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
try {
    $where = []; $args = [];
    foreach ($tokens as $t) { $where[] = 'instr(hay, ?) > 0'; $args[] = $t; }
    $cond = implode(' AND ', $where);
    $db = db_catalog();
    $st = $db->prepare("SELECT COUNT(*) AS n FROM books WHERE $cond");
    $st->execute($args);
    $total = (int)$st->fetch()['n'];
    // Titel die met de zoekterm begint of hem helemaal bevat gaat voor; daarna nieuwste eerst.
    $st = $db->prepare("SELECT title, author, price_cents, path, img, iw, ih FROM books WHERE $cond
        ORDER BY CASE WHEN instr(tnorm, ?) = 1 THEN 0 WHEN instr(tnorm, ?) > 0 THEN 1 ELSE 2 END, seq LIMIT $per OFFSET " . (($page - 1) * $per));
    $st->execute(array_merge($args, [$q, $q]));
    $items = [];
    foreach ($st->fetchAll() as $r) {
        // In de catalogus staat het Nederlandse pad (/boek/...); de link gaat naar de pagina in de taal van de bezoeker.
        $url = $r['path'];
        if (strpos($url, '/boek/') === 0) { $url = (i18n()['paths']['book'][$lang] ?? '/boek/') . substr($url, 6); }
        $items[] = ['title' => $r['title'], 'author' => $r['author'], 'price' => (int)$r['price_cents'], 'url' => $url,
                    'img' => $r['img'], 'w' => (int)$r['iw'], 'h' => (int)$r['ih']];
    }
    json_out(['ok' => true, 'items' => $items, 'total' => $total, 'per_page' => $per], 200, 'public, max-age=60');
} catch (Throwable $e) {
    error_log('search.php: ' . $e->getMessage());
    json_out(['ok' => false, 'code' => 'exception', 'error' => tr('php_search_fail', $lang)], 500);
}
