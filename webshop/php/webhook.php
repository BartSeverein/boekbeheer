<?php
// Mollie roept dit aan met POST id=tr_xxx. Er is geen handtekening: we vertrouwen alleen wat we zelf bij Mollie opvragen.
require __DIR__ . '/lib.php';

$id = (string)($_POST['id'] ?? '');
if (!preg_match('/^tr_[A-Za-z0-9]{3,40}$/', $id)) { http_response_code(200); exit; }
try {
    $st = db_shop()->prepare('SELECT * FROM orders WHERE mollie_id = ?');
    $st->execute([$id]);
    $order = $st->fetch();
    if (!$order) { http_response_code(200); exit; }   // onbekend: 200, anders blijft Mollie het proberen
    $res = sync_payment($order);
    http_response_code($res === null ? 500 : 200);    // 500 = tijdelijk probleem, Mollie probeert het later opnieuw
} catch (Throwable $e) {
    error_log('webhook.php: ' . $e->getMessage());
    http_response_code(500);
}
