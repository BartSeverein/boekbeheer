<?php
// Neemt de winkelwagen (JSON) van de pagina, controleert prijzen en voorraad tegen de catalogus en geeft een Mollie-betaallink terug.
require __DIR__ . '/lib.php';
$lang = 'nl';

if ($_SERVER['REQUEST_METHOD'] !== 'POST') { json_out(['ok' => false, 'error' => tr('php_invalid', $lang)], 405); }
$c = cfg();
$origin = $_SERVER['HTTP_ORIGIN'] ?? '';
if ($origin !== '' && parse_url($origin, PHP_URL_HOST) !== parse_url($c['base_url'], PHP_URL_HOST)) { json_out(['ok' => false, 'error' => tr('php_invalid', $lang)], 403); }
$raw = file_get_contents('php://input', false, null, 0, 20001);
if ($raw === false || strlen($raw) > 20000) { json_out(['ok' => false, 'error' => tr('php_too_big', $lang)], 413); }
$in = json_decode($raw, true);
if (!is_array($in) || !isset($in['items']) || !is_array($in['items']) || !isset($in['customer']) || !is_array($in['customer'])) {
    json_out(['ok' => false, 'error' => tr('php_invalid', $lang)], 400);
}
$lang = pick_lang($in['lang'] ?? 'nl');
if (!empty($in['hp'])) { json_out(['ok' => false, 'error' => tr('php_retry', $lang)]); }

try {
    $shop = db_shop();
    // Eenvoudige rem op misbruik: max 30 afrekenpogingen per 10 minuten per IP.
    $ip = hash('sha256', ($_SERVER['REMOTE_ADDR'] ?? '') . '|' . $c['data_dir']);
    $shop->prepare('DELETE FROM ratelimit WHERE ts < ?')->execute([time() - 600]);
    $st = $shop->prepare('SELECT COUNT(*) AS n FROM ratelimit WHERE ip = ?');
    $st->execute([$ip]);
    if ((int)$st->fetch()['n'] >= 30) { json_out(['ok' => false, 'error' => tr('php_too_many', $lang)], 429); }
    $shop->prepare('INSERT INTO ratelimit (ip, ts) VALUES (?, ?)')->execute([$ip, time()]);

    // Klantgegevens
    $cu = $in['customer'];
    $cust = [
        'name' => clean_text($cu['name'] ?? '', 100), 'email' => clean_text($cu['email'] ?? '', 150),
        'street' => clean_text($cu['street'] ?? '', 120), 'postcode' => clean_text($cu['postcode'] ?? '', 10),
        'city' => clean_text($cu['city'] ?? '', 80), 'country' => strtoupper(clean_text($cu['country'] ?? '', 2)),
        'note' => clean_text($cu['note'] ?? '', 300),
    ];
    foreach (['name', 'email', 'street', 'postcode', 'city'] as $f) {
        if ($cust[$f] === '') { json_out(['ok' => false, 'error' => tr('php_required', $lang)]); }
    }
    if (!filter_var($cust['email'], FILTER_VALIDATE_EMAIL)) { json_out(['ok' => false, 'error' => tr('php_email', $lang)]); }
    if (!in_array($cust['country'], $c['countries'], true)) { json_out(['ok' => false, 'error' => tr('php_country', $lang)]); }

    // Regels samenvoegen en begrenzen
    $want = [];
    foreach ($in['items'] as $it) {
        if (!is_array($it)) { continue; }
        $id = (int)($it['id'] ?? 0); $q = (int)($it['q'] ?? 0);
        if ($id < 1 || $q < 1) { continue; }
        $want[$id] = min(20, ($want[$id] ?? 0) + $q);
    }
    if (!$want || count($want) > 20) { json_out(['ok' => false, 'error' => tr('php_cart_bad', $lang)]); }

    // Voorraad controleren en bestelling vastleggen in één transactie, zodat twee klanten niet hetzelfde boek krijgen.
    $shop->exec('BEGIN IMMEDIATE');
    $cat = db_catalog();
    $ids = array_keys($want);
    $ph = implode(',', array_fill(0, count($ids), '?'));
    $st = $cat->prepare("SELECT id, title, author, isbn, price_cents, ship_cents FROM books WHERE id IN ($ph)");
    $st->execute($ids);
    $books = [];
    foreach ($st->fetchAll() as $r) { $books[(int)$r['id']] = $r; }
    $av = available_map($ids);
    $unavailable = [];
    foreach ($want as $id => $q) { if (!isset($books[$id]) || ($av[$id] ?? 0) < $q) { $unavailable[] = $id; } }
    if ($unavailable) {
        $shop->exec('ROLLBACK');
        json_out(['ok' => false, 'unavailable' => $unavailable, 'error' => tr('php_sold', $lang)]);
    }
    $lines = []; $sub = 0; $prices = [];
    foreach ($want as $id => $q) {
        $b = $books[$id];
        $lines[] = ['book_id' => $id, 'qty' => $q, 'title' => $b['title'], 'author' => $b['author'], 'isbn' => $b['isbn'],
                    'price_cents' => (int)$b['price_cents'], 'ship_cents' => (int)$b['ship_cents']];
        $sub += (int)$b['price_cents'] * $q;
        $prices[$id] = ['p' => (int)$b['price_cents'], 's' => (int)$b['ship_cents']];
    }
    $ship = shipping_for($lines, $sub, $cust['country']);
    $total = $sub + $ship;
    if ((int)($in['expected'] ?? -1) !== $total) {
        $shop->exec('ROLLBACK');
        json_out(['ok' => false, 'prices' => $prices, 'error' => tr('php_prices', $lang)]);
    }
    $token = bin2hex(random_bytes(16));
    $now = time();
    $shop->prepare("INSERT INTO orders (token, status, created_at, reserved_until, subtotal_cents, ship_cents, total_cents, name, email, street, postcode, city, country, note, lang)
        VALUES (?, 'open', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)")
        ->execute([$token, $now, $now + (int)$c['reserve_seconds'], $sub, $ship, $total, $cust['name'], $cust['email'], $cust['street'], $cust['postcode'], $cust['city'], $cust['country'], $cust['note'], $lang]);
    $oid = (int)$shop->lastInsertId();
    $ins = $shop->prepare('INSERT INTO order_lines (order_id, book_id, qty, title, author, isbn, price_cents, ship_cents) VALUES (?,?,?,?,?,?,?,?)');
    foreach ($lines as $l) { $ins->execute([$oid, $l['book_id'], $l['qty'], $l['title'], $l['author'], $l['isbn'], $l['price_cents'], $l['ship_cents']]); }
    $shop->exec('COMMIT');

    // Betaling aanmaken bij Mollie
    $body = [
        'amount' => ['currency' => 'EUR', 'value' => number_format($total / 100, 2, '.', '')],
        'description' => tr('php_desc', $lang, ['id' => $oid, 'shop' => $c['name']]),
        'redirectUrl' => $c['base_url'] . (i18n()['paths']['thanks'][$lang] ?? '/bedankt/') . '?o=' . $token,
        'webhookUrl' => $c['webhook_base'] . '/webhook.php',
        'locale' => i18n()['locale'][$lang] ?? 'nl_NL',
        'metadata' => ['order_id' => $oid],
    ];
    [$code, $pay, $err] = mollie_request('POST', '/payments', $body);
    $url = $pay['_links']['checkout']['href'] ?? null;
    if ($code !== 201 || !$url || empty($pay['id'])) {
        $shop->prepare("UPDATE orders SET status = 'failed' WHERE id = ? AND status = 'open'")->execute([$oid]);
        error_log('Mollie-betaling aanmaken mislukt: HTTP ' . $code . ' ' . $err);
        json_out(['ok' => false, 'error' => tr('php_pay_fail', $lang)], 502);
    }
    $shop->prepare('UPDATE orders SET mollie_id = ? WHERE id = ?')->execute([$pay['id'], $oid]);
    json_out(['ok' => true, 'url' => $url]);
} catch (Throwable $e) {
    try { if (isset($shop) && $shop->inTransaction()) { $shop->exec('ROLLBACK'); } } catch (Throwable $e2) {}
    error_log('checkout.php: ' . $e->getMessage());
    json_out(['ok' => false, 'error' => tr('php_error', $lang)], 500);
}
