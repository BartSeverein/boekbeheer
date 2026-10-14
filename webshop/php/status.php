<?php
// De bedankpagina vraagt hier de status op: ?o=<32 hex-tekens> -> {status: paid|open|failed}
require __DIR__ . '/lib.php';

$tok = (string)($_GET['o'] ?? '');
if (!preg_match('/^[a-f0-9]{32}$/', $tok)) { json_out(['status' => 'unknown'], 404); }
try {
    $st = db_shop()->prepare('SELECT * FROM orders WHERE token = ?');
    $st->execute([$tok]);
    $order = $st->fetch();
    if (!$order) { json_out(['status' => 'unknown'], 404); }
    $status = $order['status'];
    if ($status === 'open') {
        $s = sync_payment($order);   // werkt ook als de webhook niet aankomt
        if ($s !== null) { $status = $s; }
    }
    json_out(['status' => $status === 'paid' ? 'paid' : ($status === 'open' ? 'open' : 'failed')]);
} catch (Throwable $e) {
    error_log('status.php: ' . $e->getMessage());
    json_out(['status' => 'open'], 500);
}
