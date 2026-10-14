<?php
// Gedeelde functies voor checkout.php, search.php, webhook.php en status.php. PHP 7.4+, pdo_sqlite en curl nodig.
// Er is bewust geen framework: alles wat hier staat is nodig.

function cfg() {
    static $c = null;
    if ($c === null) { $c = require __DIR__ . '/config.php'; }
    return $c;
}

function json_out($data, $code = 200, $cache = 'no-store') {
    http_response_code($code);
    header('Content-Type: application/json; charset=utf-8');
    header('Cache-Control: ' . $cache);
    header('X-Content-Type-Options: nosniff');
    echo json_encode($data, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);
    exit;
}

function data_path($name) { return __DIR__ . '/' . cfg()['data_dir'] . '/' . $name; }

function db_catalog() {
    static $db = null;
    if ($db === null) {
        $db = new PDO('sqlite:' . data_path('catalog.sqlite'));
        $db->setAttribute(PDO::ATTR_ERRMODE, PDO::ERRMODE_EXCEPTION);
        $db->setAttribute(PDO::ATTR_DEFAULT_FETCH_MODE, PDO::FETCH_ASSOC);
    }
    return $db;
}

function db_shop() {
    static $db = null;
    if ($db === null) {
        $db = new PDO('sqlite:' . data_path('shop.sqlite'));
        $db->setAttribute(PDO::ATTR_ERRMODE, PDO::ERRMODE_EXCEPTION);
        $db->setAttribute(PDO::ATTR_DEFAULT_FETCH_MODE, PDO::FETCH_ASSOC);
        $db->exec('PRAGMA busy_timeout = 8000');
        $db->exec("CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT, token TEXT UNIQUE NOT NULL, mollie_id TEXT, status TEXT NOT NULL,
            created_at INTEGER NOT NULL, reserved_until INTEGER NOT NULL, paid_at INTEGER,
            subtotal_cents INTEGER NOT NULL, ship_cents INTEGER NOT NULL, total_cents INTEGER NOT NULL,
            name TEXT, email TEXT, street TEXT, postcode TEXT, city TEXT, country TEXT, note TEXT,
            attention TEXT, mailed INTEGER NOT NULL DEFAULT 0)");
        $db->exec("CREATE TABLE IF NOT EXISTS order_lines (
            order_id INTEGER NOT NULL, book_id INTEGER NOT NULL, qty INTEGER NOT NULL, title TEXT, author TEXT, isbn TEXT,
            price_cents INTEGER NOT NULL, ship_cents INTEGER NOT NULL)");
        $db->exec('CREATE INDEX IF NOT EXISTS ol_book ON order_lines(book_id)');
        $db->exec('CREATE INDEX IF NOT EXISTS ol_order ON order_lines(order_id)');
        $db->exec('CREATE TABLE IF NOT EXISTS ratelimit (ip TEXT NOT NULL, ts INTEGER NOT NULL)');
        $db->exec('CREATE INDEX IF NOT EXISTS rl_ip ON ratelimit(ip, ts)');
    }
    return $db;
}

function clean_text($s, $max) {
    $s = preg_replace('/[\x00-\x1F\x7F]+/u', ' ', (string)$s);
    if ($s === null) { return ''; }
    $s = trim(preg_replace('/\s+/u', ' ', $s));
    return function_exists('mb_substr') ? mb_substr($s, 0, $max, 'UTF-8') : substr($s, 0, $max);
}

// Verzendkosten: hoogste verzendprijs in de wagen + toeslag per extra boek; eventueel gratis vanaf een bedrag.
// Dezelfde regel staat in c.js (totals) — wijzig je er één, wijzig dan ook de ander.
function shipping_for($lines, $subtotal) {
    $c = cfg();
    $max = 0; $n = 0;
    foreach ($lines as $l) { $n += $l['qty']; if ($l['ship_cents'] > $max) { $max = $l['ship_cents']; } }
    if ($n < 1) { return 0; }
    $ship = $max + $c['extra_cents'] * ($n - 1);
    if ($c['free_from_cents'] > 0 && $subtotal >= $c['free_from_cents']) { $ship = 0; }
    return $ship;
}

// Aantal exemplaren dat nog te koop is: voorraad uit de catalogus min webshop-bestellingen die daar nog niet in zijn verwerkt
// (betaald, of nog open en minder dan reserve_seconds oud). $skip_order: tel die bestelling niet mee (voor controle achteraf).
function available_map($ids, $skip_order = 0) {
    $out = [];
    if (!$ids) { return $out; }
    $cat = db_catalog();
    $ph = implode(',', array_fill(0, count($ids), '?'));
    $st = $cat->prepare("SELECT id, amount FROM books WHERE id IN ($ph)");
    $st->execute(array_values($ids));
    foreach ($st->fetchAll() as $r) { $out[(int)$r['id']] = (int)$r['amount']; }
    $imported = [];
    foreach ($cat->query('SELECT id FROM imported_orders')->fetchAll() as $r) { $imported[(int)$r['id']] = true; }
    $shop = db_shop();
    $st = $shop->prepare("SELECT l.book_id, l.qty, o.id AS oid FROM order_lines l JOIN orders o ON o.id = l.order_id
        WHERE l.book_id IN ($ph) AND o.id <> ? AND (o.status = 'paid' OR (o.status = 'open' AND o.reserved_until > ?))");
    $st->execute(array_merge(array_values($ids), [$skip_order, time()]));
    foreach ($st->fetchAll() as $r) {
        if (isset($imported[(int)$r['oid']])) { continue; }
        $b = (int)$r['book_id'];
        if (isset($out[$b])) { $out[$b] -= (int)$r['qty']; }
    }
    foreach ($out as $k => $v) { if ($v < 0) { $out[$k] = 0; } }
    return $out;
}

function mollie_request($method, $path, $body = null) {
    $c = cfg();
    if ($c['mollie_key'] === '') { return [0, null, 'Mollie is nog niet ingesteld']; }
    $ch = curl_init(rtrim($c['mollie_api_base'], '/') . $path);
    $headers = ['Authorization: Bearer ' . $c['mollie_key'], 'Accept: application/json'];
    $opts = [CURLOPT_RETURNTRANSFER => true, CURLOPT_TIMEOUT => 12, CURLOPT_CONNECTTIMEOUT => 6, CURLOPT_CUSTOMREQUEST => $method];
    if ($body !== null) {
        $headers[] = 'Content-Type: application/json';
        $opts[CURLOPT_POSTFIELDS] = json_encode($body, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);
    }
    $opts[CURLOPT_HTTPHEADER] = $headers;
    curl_setopt_array($ch, $opts);
    $resp = curl_exec($ch);
    $code = (int)curl_getinfo($ch, CURLINFO_RESPONSE_CODE);
    $err = curl_error($ch);
    curl_close($ch);
    if ($resp === false) { return [0, null, $err ?: 'geen verbinding met Mollie']; }
    return [$code, json_decode($resp, true), $err];
}

function send_mail($to, $subject, $text) {
    $c = cfg();
    if (!$to || !filter_var($to, FILTER_VALIDATE_EMAIL)) { return false; }
    $from = $c['email'] ?: ('webshop@' . parse_url($c['base_url'], PHP_URL_HOST));
    $headers = "From: " . $c['name'] . " <" . $from . ">\r\nMIME-Version: 1.0\r\nContent-Type: text/plain; charset=UTF-8\r\n";
    return @mail($to, '=?UTF-8?B?' . base64_encode($subject) . '?=', $text, $headers);
}

function eur_text($cents) { return 'EUR ' . number_format($cents / 100, 2, ',', '.'); }

function order_mail_text($order, $lines) {
    $t = "Bedankt voor je bestelling #" . $order['id'] . ".\n\n";
    foreach ($lines as $l) { $t .= $l['qty'] . " x " . $l['title'] . " — " . eur_text($l['price_cents']) . "\n"; }
    $t .= "\nVerzendkosten: " . eur_text($order['ship_cents']) . "\nTotaal: " . eur_text($order['total_cents']) . "\n\n";
    $t .= "Verzendadres:\n" . $order['name'] . "\n" . $order['street'] . "\n" . $order['postcode'] . " " . $order['city'] . "\n" . $order['country'] . "\n";
    return $t;
}

// Haalt de betaalstatus bij Mollie en werkt de bestelling bij. Idempotent. Geeft de nieuwe status terug,
// of null als Mollie niet bereikbaar was (dan moet de webhook een 500 geven zodat Mollie het later opnieuw probeert).
function sync_payment($order) {
    if (!$order['mollie_id']) { return $order['status']; }
    [$code, $pay, $err] = mollie_request('GET', '/payments/' . rawurlencode($order['mollie_id']));
    if ($code !== 200 || !is_array($pay)) { return $code >= 400 && $code < 500 ? $order['status'] : null; }
    $meta = $pay['metadata'] ?? [];
    if ((string)($meta['order_id'] ?? '') !== (string)$order['id']) { return $order['status']; }
    $db = db_shop();
    $mstatus = $pay['status'] ?? 'open';
    $new = $order['status'];
    if ($mstatus === 'paid') { $new = 'paid'; }
    elseif (in_array($mstatus, ['failed', 'expired', 'canceled'], true) && $order['status'] === 'open') { $new = $mstatus; }
    if ($new === $order['status']) { return $new; }
    if ($new === 'paid') {
        $paid_ok = (($pay['amount']['currency'] ?? '') === 'EUR') && (($pay['amount']['value'] ?? '') === number_format($order['total_cents'] / 100, 2, '.', ''));
        $db->beginTransaction();
        $st = $db->prepare("UPDATE orders SET status = 'paid', paid_at = ? WHERE id = ? AND status <> 'paid'");
        $st->execute([time(), $order['id']]);
        $changed = $st->rowCount() > 0;
        $db->commit();
        if ($changed) {
            $attention = [];
            if (!$paid_ok) { $attention[] = 'bedrag bij Mollie wijkt af'; }
            $lines = $db->prepare('SELECT * FROM order_lines WHERE order_id = ?');
            $lines->execute([$order['id']]);
            $lines = $lines->fetchAll();
            $ids = array_map(function ($l) { return (int)$l['book_id']; }, $lines);
            $av = available_map($ids, $order['id']);
            foreach ($lines as $l) {
                if (($av[(int)$l['book_id']] ?? 0) < (int)$l['qty']) { $attention[] = 'boek ' . $l['book_id'] . ' was niet meer beschikbaar'; }
            }
            if ($attention) { $db->prepare('UPDATE orders SET attention = ? WHERE id = ?')->execute([implode('; ', $attention), $order['id']]); }
            $fresh = $db->prepare('SELECT * FROM orders WHERE id = ?');
            $fresh->execute([$order['id']]);
            $fresh = $fresh->fetch();
            if ($fresh && !$fresh['mailed']) {
                $db->prepare('UPDATE orders SET mailed = 1 WHERE id = ?')->execute([$order['id']]);
                $text = order_mail_text($fresh, $lines);
                send_mail($fresh['email'], 'Bevestiging bestelling #' . $order['id'], $text);
                send_mail(cfg()['email'], 'Nieuwe bestelling #' . $order['id'] . ($attention ? ' — LET OP' : ''),
                    $text . "\nE-mail klant: " . $fresh['email'] . "\n" . ($attention ? "\nLET OP: " . implode('; ', $attention) . "\n" : ''));
            }
        }
        return 'paid';
    }
    $db->prepare("UPDATE orders SET status = ? WHERE id = ? AND status = 'open'")->execute([$new, $order['id']]);
    return $new;
}
