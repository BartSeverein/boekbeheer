<?php
// Wist persoonsgegevens uit shop.sqlite. Wordt na elke run aangeroepen door het publicatiescript, nadat de bestellingen veilig in
// de eigen administratie staan. POST t=<sleutel> ids=<bestelnummers, komma-gescheiden>.
// - Betaalde bestelling in 'ids': alleen gewist als de klantmail en eigenaarmail zijn verstuurd (mailed = 3) en, als Intime aan staat,
//   de zending daar is aangemaakt. Anders blijft hij staan tot de volgende keer.
// - Vangnet: ook zonder die voorwaarden 14 dagen na betaling.
// - Niet-betaalde bestellingen (afgebroken of mislukte betalingen) worden na 7 dagen gewist, ongeacht 'ids'.
// Bestelnummer, bedragen, regels, status en Intime-id blijven staan (nodig voor reservering en terugkoppeling).
require __DIR__ . '/lib.php';

header('Content-Type: application/json; charset=utf-8');
$key = substr((string)(cfg()['data_dir'] ?? ''), 2);
$want = hash('sha256', 'purge|' . $key);
$t = (string)($_POST['t'] ?? '');
if ($key === '' || !hash_equals($want, $t)) { http_response_code(403); echo '{"ok":false}'; exit; }

try {
    $db = db_shop();
    $blank = "name = '', email = '', street = '', postcode = '', city = '', note = NULL";
    $n = 0;
    $needIntime = !empty(cfg()['intime_key']);
    $ids = array_filter(array_map('intval', explode(',', (string)($_POST['ids'] ?? ''))));
    $st = $db->prepare("UPDATE orders SET $blank WHERE id = ? AND status = 'paid' AND mailed >= 3"
        . ($needIntime ? " AND COALESCE(intime_id, '') <> ''" : '') . " AND COALESCE(email, '') <> ''");
    foreach ($ids as $id) { $st->execute([$id]); $n += $st->rowCount(); }
    // Vangnet: een betaalde bestelling die in de eigen administratie staat, verdwijnt 14 dagen na betaling hoe dan ook
    // (ook als een mail of de Intime-zending nooit is gelukt, of de bestelling niet wordt verzonden).
    $st = $db->prepare("UPDATE orders SET $blank WHERE id = ? AND status = 'paid' AND COALESCE(paid_at, created_at) < CAST(? AS INTEGER) AND COALESCE(email, '') <> ''");
    $cut = time() - 14 * 86400;
    foreach ($ids as $id) { $st->execute([$id, $cut]); $n += $st->rowCount(); }
    $st = $db->prepare("UPDATE orders SET $blank WHERE status <> 'paid' AND created_at < CAST(? AS INTEGER) AND COALESCE(email, '') <> ''");
    $st->execute([time() - 7 * 86400]);
    $m = $st->rowCount();
    // Waarom bleef een betaalde bestelling met gegevens staan? (alleen aantallen, voor in het log)
    $wait_mail = (int)$db->query("SELECT COUNT(*) FROM orders WHERE status = 'paid' AND COALESCE(email, '') <> '' AND mailed < 3")->fetchColumn();
    $wait_intime = $needIntime ? (int)$db->query("SELECT COUNT(*) FROM orders WHERE status = 'paid' AND COALESCE(email, '') <> '' AND mailed >= 3 AND COALESCE(intime_id, '') = ''")->fetchColumn() : 0;
    $left_unpaid = (int)$db->query("SELECT COUNT(*) FROM orders WHERE status <> 'paid' AND COALESCE(email, '') <> ''")->fetchColumn();
    echo json_encode(['ok' => true, 'paid' => $n, 'unpaid' => $m, 'wait_mail' => $wait_mail, 'wait_intime' => $wait_intime, 'left_unpaid' => $left_unpaid]);
} catch (Throwable $e) {
    error_log('purge.php: ' . $e->getMessage());
    http_response_code(500);
    echo '{"ok":false}';
}
