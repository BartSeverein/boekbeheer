<?php
// De boekpagina vraagt hier de actuele voorraad op: ?id=<boek-id> -> {id, amount}
// amount houdt ook rekening met betaalde en gereserveerde webshop-bestellingen die nog niet in het dashboard zijn verwerkt.
require __DIR__ . '/lib.php';

$id = (int)($_GET['id'] ?? 0);
if ($id <= 0) { json_out(['error' => 'id'], 400); }
if (!extension_loaded('pdo_sqlite') || !is_file(data_path('catalog.sqlite'))) { json_out(['error' => 'unavailable'], 503); }
try {
    $map = available_map([$id]);
    json_out(['id' => $id, 'amount' => (int)($map[$id] ?? 0)]);
} catch (Throwable $e) {
    error_log('stock.php: ' . $e->getMessage());
    json_out(['error' => 'failed'], 500);
}
