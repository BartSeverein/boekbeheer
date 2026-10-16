<?php
// Gedeelde functies voor checkout.php, search.php, webhook.php en status.php. PHP 7.4+, pdo_sqlite en curl nodig.
// Er is bewust geen framework: alles wat hier staat is nodig.

function cfg() {
    static $c = null;
    if ($c === null) { $c = require __DIR__ . '/config.php'; }
    return $c;
}

// Teksten per taal (gegenereerd uit webshop/i18n.py). Onbekende taal of ontbrekende tekst -> Nederlands.
function i18n() {
    static $i = null;
    if ($i === null) { $i = require __DIR__ . '/i18n.php'; }
    return $i;
}

function pick_lang($l) {
    $l = strtolower(substr((string)$l, 0, 2));
    return in_array($l, i18n()['langs'], true) ? $l : 'nl';
}

function tr($key, $lang = 'nl', $vars = []) {
    $m = i18n()['msgs'];
    $s = $m[$lang][$key] ?? ($m['nl'][$key] ?? $key);
    foreach ($vars as $k => $v) { $s = str_replace('{' . $k . '}', (string)$v, $s); }
    return $s;
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
        try { $db->exec('ALTER TABLE order_lines ADD COLUMN location TEXT'); } catch (Throwable $e) { /* bestaat al */ }
        try { $db->exec('ALTER TABLE orders ADD COLUMN intime_id TEXT'); } catch (Throwable $e) { /* bestaat al */ }
        $db->exec('CREATE INDEX IF NOT EXISTS ol_book ON order_lines(book_id)');
        $db->exec('CREATE INDEX IF NOT EXISTS ol_order ON order_lines(order_id)');
        $db->exec('CREATE TABLE IF NOT EXISTS ratelimit (ip TEXT NOT NULL, ts INTEGER NOT NULL)');
        $db->exec('CREATE INDEX IF NOT EXISTS rl_ip ON ratelimit(ip, ts)');
        // Bestelnummers beginnen bij 2048 (ziet er minder nieuw uit voor klanten); alleen omhoog, nooit omlaag.
        try {
            $seq = $db->query("SELECT seq FROM sqlite_sequence WHERE name = 'orders'")->fetchColumn();
            if ($seq === false) { $db->exec("INSERT INTO sqlite_sequence(name, seq) VALUES ('orders', 2047)"); }
            elseif ((int)$seq < 2047) { $db->exec("UPDATE sqlite_sequence SET seq = 2047 WHERE name = 'orders'"); }
        } catch (Throwable $e) {}
        // Oudere shop.sqlite zonder taalkolom: bijwerken (twee gelijktijdige verzoeken: de tweede krijgt een fout die we negeren).
        $cols = [];
        foreach ($db->query('PRAGMA table_info(orders)')->fetchAll() as $r) { $cols[] = $r['name']; }
        if (!in_array('lang', $cols, true)) {
            try { $db->exec("ALTER TABLE orders ADD COLUMN lang TEXT NOT NULL DEFAULT 'nl'"); } catch (Throwable $e) {}
        }
    }
    return $db;
}

function clean_text($s, $max) {
    $s = preg_replace('/[\x00-\x1F\x7F]+/u', ' ', (string)$s);
    if ($s === null) { return ''; }
    $s = trim(preg_replace('/\s+/u', ' ', $s));
    return function_exists('mb_substr') ? mb_substr($s, 0, $max, 'UTF-8') : substr($s, 0, $max);
}

// Verzendkosten: tarief van het land voor een klein pakje, of voor een groot pakje als een boek in de wagen groot is,
// plus toeslag per extra boek; eventueel gratis vanaf een bedrag. Landen zonder eigen tarief gebruiken het tarief OTHER.
// Dezelfde regel staat in c.js (totals) — wijzig je er één, wijzig dan ook de ander.
// Gratis verzenden vanaf een bedrag geldt alleen voor Nederland.
function shipping_for($lines, $subtotal, $country = 'NL') {
    $c = cfg();
    $big = 0; $n = 0;
    foreach ($lines as $l) { $n += $l['qty']; if (!empty($l['big'])) { $big = 1; } }
    if ($n < 1) { return 0; }
    $rates = $c['rates'] ?? [];
    $rate = $rates[$country] ?? ($rates['OTHER'] ?? ($rates['NL'] ?? [$c['default_shipping_cents'], $c['default_shipping_cents']]));
    $ship = (int)$rate[$big] + $c['extra_cents'] * ($n - 1);
    if ($c['free_from_cents'] > 0 && $subtotal >= $c['free_from_cents'] && $country === 'NL') { $ship = 0; }
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

function mail_log($msg) {
    $line = date('Y-m-d H:i:s') . ' ' . $msg . "\n";
    @file_put_contents(data_path('mail.log'), $line, FILE_APPEND | LOCK_EX);
    error_log('webshop mail: ' . $msg);
}

// Verstuurt via SMTP (als WEBSHOP_SMTP_* is ingesteld), anders via PHP mail(). Geeft true/false terug en schrijft altijd een regel in mail.log.
function smtp_send($to, $subject, $text, $from, $fromname, $reply) {
    $c = cfg()['smtp'] ?? [];
    $host = $c['host'] ?? ''; $port = (int)($c['port'] ?? 587);
    $user = $c['user'] ?? ''; $pass = $c['pass'] ?? '';
    $fp = @stream_socket_client(($port === 465 ? 'ssl://' : 'tcp://') . $host . ':' . $port, $en, $es, 12);
    if (!$fp) { return 'geen verbinding met ' . $host . ':' . $port . ' (' . $es . ')'; }
    stream_set_timeout($fp, 12);
    $read = function () use ($fp) {
        $out = '';
        while (($line = fgets($fp, 1024)) !== false) { $out .= $line; if (strlen($line) < 4 || $line[3] === ' ') { break; } }
        return $out;
    };
    $cmd = function ($line, $ok) use ($fp, $read) {
        if ($line !== null) { fwrite($fp, $line . "\r\n"); }
        $r = $read();
        $okl = array_map('strval', (array)$ok);
        return [in_array(substr($r, 0, 3), $okl, true), trim($r)];
    };
    $fail = function ($msg) use ($fp) { @fwrite($fp, "QUIT\r\n"); @fclose($fp); return $msg; };
    $r = $cmd(null, 220); if (!$r[0]) { return $fail('begroeting: ' . $r[1]); }
    $ehlo = preg_replace('/[^a-z0-9.-]/i', '', (string)parse_url(cfg()['base_url'], PHP_URL_HOST)) ?: 'localhost';
    $r = $cmd('EHLO ' . $ehlo, 250); if (!$r[0]) { return $fail('EHLO: ' . $r[1]); }
    if ($port !== 465 && stripos($r[1], 'STARTTLS') !== false) {
        $r = $cmd('STARTTLS', 220); if (!$r[0]) { return $fail('STARTTLS: ' . $r[1]); }
        if (!@stream_socket_enable_crypto($fp, true, STREAM_CRYPTO_METHOD_TLS_CLIENT)) { return $fail('TLS mislukt'); }
        $r = $cmd('EHLO ' . $ehlo, 250); if (!$r[0]) { return $fail('EHLO na TLS: ' . $r[1]); }
    }
    if ($user !== '') {
        $r = $cmd('AUTH LOGIN', 334); if (!$r[0]) { return $fail('AUTH: ' . $r[1]); }
        $r = $cmd(base64_encode($user), 334); if (!$r[0]) { return $fail('gebruikersnaam: ' . $r[1]); }
        $r = $cmd(base64_encode($pass), 235); if (!$r[0]) { return $fail('inloggen mislukt: ' . $r[1]); }
    }
    $r = $cmd('MAIL FROM:<' . $from . '>', 250); if (!$r[0]) { return $fail('MAIL FROM: ' . $r[1]); }
    $r = $cmd('RCPT TO:<' . $to . '>', [250, 251]); if (!$r[0]) { return $fail('RCPT TO: ' . $r[1]); }
    $r = $cmd('DATA', 354); if (!$r[0]) { return $fail('DATA: ' . $r[1]); }
    $msgid = bin2hex(random_bytes(8)) . '@' . $ehlo;
    $head = "Date: " . date('r') . "\r\nMessage-ID: <" . $msgid . ">\r\nFrom: =?UTF-8?B?" . base64_encode($fromname) . "?= <" . $from . ">\r\n"
        . "To: <" . $to . ">\r\nReply-To: " . $reply . "\r\nSubject: =?UTF-8?B?" . base64_encode($subject) . "?=\r\nMIME-Version: 1.0\r\n"
        . "Content-Type: text/plain; charset=UTF-8\r\nContent-Transfer-Encoding: base64\r\n\r\n";
    $body = chunk_split(base64_encode($text), 76, "\r\n");
    fwrite($fp, $head . $body . ".\r\n");
    $r = $read();
    if (substr($r, 0, 3) !== '250') { return $fail('verzenden: ' . trim($r)); }
    @fwrite($fp, "QUIT\r\n"); @fclose($fp);
    return true;
}

function send_mail($to, $subject, $text) {
    $c = cfg();
    if (!$to || !filter_var($to, FILTER_VALIDATE_EMAIL)) { mail_log('ongeldig adres: ' . $to); return false; }
    $host = preg_replace('/^www\./', '', (string)parse_url($c['base_url'], PHP_URL_HOST));
    $reply = $c['email'] ?: ('webshop@' . $host);
    $smtp = $c['smtp'] ?? [];
    if (!empty($smtp['host'])) {
        $from = $smtp['from'] ?: (strpos($smtp['user'] ?? '', '@') !== false ? $smtp['user'] : 'webshop@' . $host);
        $res = smtp_send($to, $subject, $text, $from, $c['name'], $reply);
        mail_log(($res === true ? 'verzonden (SMTP)' : 'MISLUKT (SMTP: ' . $res . ')') . ' aan ' . $to . ': ' . $subject);
        return $res === true;
    }
    // Afzender op het eigen domein: veel hosters weigeren (of markeren als spam) mail met een @gmail.com-afzender.
    $from = 'webshop@' . $host;
    $name = '=?UTF-8?B?' . base64_encode($c['name']) . '?=';
    $headers = "From: " . $name . " <" . $from . ">\r\nReply-To: " . $reply . "\r\nMIME-Version: 1.0\r\n"
        . "Content-Type: text/plain; charset=UTF-8\r\nContent-Transfer-Encoding: 8bit\r\n";
    $ok = @mail($to, '=?UTF-8?B?' . base64_encode($subject) . '?=', $text, $headers, '-f' . $from);
    if (!$ok) { $ok = @mail($to, '=?UTF-8?B?' . base64_encode($subject) . '?=', $text, $headers); }
    mail_log(($ok ? 'verzonden' : 'MISLUKT') . ' (mail()) aan ' . $to . ': ' . $subject);
    return $ok;
}

function eur_text($cents, $lang = 'nl') {
    $en = $lang === 'en';
    return 'EUR ' . number_format($cents / 100, 2, $en ? '.' : ',', $en ? ',' : '.');
}

function order_mail_text($order, $lines, $lang = 'nl') {
    $t = tr('mail_thanks', $lang, ['id' => $order['id']]) . "\n\n";
    foreach ($lines as $l) { $t .= $l['qty'] . " x " . $l['title'] . " — " . eur_text($l['price_cents'], $lang) . "\n"; }
    $t .= "\n" . tr('mail_ship', $lang) . ": " . eur_text($order['ship_cents'], $lang) . "\n" . tr('mail_total', $lang) . ": " . eur_text($order['total_cents'], $lang) . "\n\n";
    $t .= tr('mail_addr', $lang) . ":\n" . $order['name'] . "\n" . $order['street'] . "\n" . $order['postcode'] . " " . $order['city'] . "\n" . $order['country'] . "\n";
    return $t;
}

// Mail aan de eigenaar (altijd Nederlands): met de locatie van elk boek, zodat het zo uit de kast gepakt kan worden.
function owner_mail_text($order, $lines) {
    $t = "Er is een nieuwe bestelling via de webshop, #" . $order['id'] . ".\n\n";
    foreach ($lines as $l) {
        $loc = trim((string)($l['location'] ?? ''));
        $t .= $l['qty'] . " x " . $l['title'] . ($loc !== '' ? " — locatie " . $loc : '') . "  —  " . eur_text($l['price_cents'], 'nl') . "\n";
    }
    $t .= "\nVerzendkosten: " . eur_text($order['ship_cents'], 'nl') . "\nTotaal: " . eur_text($order['total_cents'], 'nl') . "\n\n";
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
    if ($new === $order['status']) {
        if ($new === 'paid' && time() - (int)$order['paid_at'] < 3 * 86400
            && ((int)$order['mailed'] < 3 || (!empty(cfg()['intime_key']) && empty($order['intime_id'])))) { retry_order_mails($order); }
        return $new;
    }
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
            if ($fresh) { deliver_order_mails($fresh, $lines, $attention); deliver_intime($fresh, $lines); }
        }
        return 'paid';
    }
    $db->prepare("UPDATE orders SET status = ? WHERE id = ? AND status = 'open'")->execute([$new, $order['id']]);
    return $new;
}

// mailed is een bitmasker: 1 = klantmail verzonden, 2 = eigenaarmail verzonden. Alleen een gelukte verzending telt.
function deliver_order_mails($order, $lines, $attention) {
    $db = db_shop();
    $mailed = (int)$order['mailed'];
    $olang = pick_lang($order['lang'] ?? 'nl');
    if (!($mailed & 1)) {
        $text = tr('mail_greeting', $olang) . "\n\n" . order_mail_text($order, $lines, $olang) . "\n" . tr('mail_reply', $olang) . "\n\n" . tr('mail_regards', $olang) . "\n\n" . (cfg()['owner'] ?? '' ?: cfg()['name']) . "\n";
        if (send_mail($order['email'], tr('mail_subject', $olang, ['id' => $order['id']]), $text)) { $mailed |= 1; }
    }
    if (!($mailed & 2)) {
        // De eigenaar krijgt de bestelling altijd in het Nederlands, met de taal van de klant erbij.
        $att = $attention ?: ($order['attention'] ? [$order['attention']] : []);
        $ok = send_mail(cfg()['email'], 'Nieuwe bestelling #' . $order['id'] . ($att ? ' — LET OP' : ''),
            owner_mail_text($order, $lines) . "\nE-mail klant: " . $order['email'] . "\nTaal klant: " . strtoupper($olang) . "\n"
            . ($att ? "\nLET OP: " . implode('; ', $att) . "\n" : ''));
        if ($ok) { $mailed |= 2; }
    }
    $db->prepare('UPDATE orders SET mailed = ? WHERE id = ?')->execute([$mailed, $order['id']]);
}

function retry_order_mails($order) {
    $db = db_shop();
    $lines = $db->prepare('SELECT * FROM order_lines WHERE order_id = ?');
    $lines->execute([$order['id']]);
    $lines = $lines->fetchAll();
    if ((int)$order['mailed'] < 3) { deliver_order_mails($order, $lines, []); }
    deliver_intime($order, $lines);
}

// Zet de betaalde bestelling klaar in Intime (onvolledige zending, validate=false): in Intime kies je daarna vervoerder, gewicht en maat
// en print je het label. Lukt het niet, dan probeert de volgende statuscontrole het opnieuw (tot 3 dagen na betaling).
function intime_request($method, $path, $body = null) {
    $key = cfg()['intime_key'] ?? '';
    $ch = curl_init('https://api.intime.delivery' . $path);
    $h = ['X-API-KEY: ' . $key, 'Accept: application/json'];
    $opt = [CURLOPT_RETURNTRANSFER => true, CURLOPT_TIMEOUT => 12, CURLOPT_CONNECTTIMEOUT => 6, CURLOPT_CUSTOMREQUEST => $method];
    if ($body !== null) { $h[] = 'Content-Type: application/json'; $opt[CURLOPT_POSTFIELDS] = json_encode($body, JSON_UNESCAPED_UNICODE); }
    $opt[CURLOPT_HTTPHEADER] = $h;
    curl_setopt_array($ch, $opt);
    $resp = curl_exec($ch);
    $code = (int)curl_getinfo($ch, CURLINFO_HTTP_CODE);
    $err = curl_error($ch);
    curl_close($ch);
    return [$code, $resp === false ? null : json_decode($resp, true), $resp === false ? $err : (string)$resp];
}

function intime_body($order, $lines) {
    $b = [
        'validate' => false,
        'customReference' => 'Webshop #' . $order['id'],
        'recipientName' => (string)$order['name'],
        'recipientFirstAddressLine' => (string)$order['street'],
        'recipientZipCode' => (string)$order['postcode'],
        'recipientPlace' => (string)$order['city'],
        'recipientCountryCode' => (string)$order['country'],
        'recipientEmailAddress' => (string)$order['email'],
        'contentType' => 5,
    ];
    $n = 0;
    foreach ($lines as $l) {
        if (++$n > 5) { break; }
        $b["contentLine{$n}Count"] = (int)$l['qty'];
        $b["contentLine{$n}Description"] = (string)$l['title'];
        $b["contentLine{$n}PieceValue"] = round((int)$l['price_cents'] / 100, 2);
        $b["contentLine{$n}HsCode"] = '4901';
    }
    return $b;
}

function deliver_intime($order, $lines) {
    if (empty(cfg()['intime_key']) || !empty($order['intime_id'])) { return; }
    [$code, $data, $raw] = intime_request('POST', '/v1/shipping/shipments/create-flat?validate=false', intime_body($order, $lines));
    $id = is_array($data) ? ($data['shipmentId'] ?? '') : '';
    if ($code === 200 && $id !== '') {
        db_shop()->prepare('UPDATE orders SET intime_id = ? WHERE id = ?')->execute([$id, $order['id']]);
        mail_log('intime: zending ' . $id . ' aangemaakt voor bestelling #' . $order['id']);
    } else {
        mail_log('intime: MISLUKT voor bestelling #' . $order['id'] . ' (HTTP ' . $code . '): ' . substr($raw, 0, 300));
    }
}
