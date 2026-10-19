<?php
// Diagnose voor de Intime-koppeling. Open: /intimetest.php?k=<WEBSHOP_DATA_KEY>
//   (zonder verdere parameters)  controleert de sleutel door de lijst met vervoerders op te halen
//   &maak=1                      maakt een testzending "TEST webshop" aan (onvolledig, kost niets) en toont het nummer
//   &wis=<zendingsnummer>        verwijdert zo'n zending weer
require __DIR__ . '/lib.php';
header('Content-Type: text/plain; charset=utf-8');
header('Cache-Control: no-store');
$key = substr(cfg()['data_dir'], 2);
if (!hash_equals($key, (string)($_GET['k'] ?? ''))) { http_response_code(404); echo "niet gevonden\n"; exit; }
echo "WEBSHOP_INTIME_KEY ingesteld: " . (!empty(cfg()['intime_key']) ? 'ja' : 'NEE') . "\n";
if (empty(cfg()['intime_key'])) { exit; }
[$code, $data, $raw] = intime_request('GET', '/v1/shipping/carriers/shipment-options');
echo "\nvervoerders (HTTP $code):\n" . ($code === 200 ? json_encode($data, JSON_PRETTY_PRINT | JSON_UNESCAPED_UNICODE) : $raw) . "\n";
if (!empty($_GET['maak'])) {
    $order = ['id' => 'TEST', 'name' => 'Test Webshop', 'street' => 'Teststraat 1', 'postcode' => '1234 AB', 'city' => 'Teststad', 'country' => 'NL', 'email' => (cfg()['email'] ?: 'test@example.com')];
    $lines = [['qty' => 1, 'title' => 'Testboek', 'price_cents' => 395]];
    [$c2, $d2, $r2] = intime_request('POST', '/v1/shipping/shipments/create-flat?validate=false', intime_body($order, $lines));
    echo "\ntestzending (HTTP $c2): " . $r2 . "\n";
    echo "Kijk nu in Intime onder '1. Verwerk je orders' of hij daar staat. Verwijderen: &wis=<shipmentId>\n";
}
if (!empty($_GET['wis'])) {
    [$c3, $d3, $r3] = intime_request('DELETE', '/v1/shipping/shipments/' . rawurlencode((string)$_GET['wis']));
    echo "\nverwijderen (HTTP $c3): $r3\n";
}
echo "\n--- mail.log, regels met intime ---\n";
$f = data_path('mail.log');
echo is_file($f) ? implode('', array_slice(array_filter(file($f), function ($l) { return strpos($l, 'intime') !== false; }), -10)) : "(nog geen log)\n";
