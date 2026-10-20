<?php
// Controleert een kortingscode voor de afrekenpagina. Geeft alleen het soort korting terug; de echte controle staat in checkout.php.
require __DIR__ . '/lib.php';
$lang = pick_lang($_GET['lang'] ?? 'nl');
try {
    $shop = db_shop();
    if (!code_attempt_allowed($shop)) { json_out(['ok' => false, 'error' => tr('php_too_many', $lang)], 429); }
    $row = find_code($_GET['c'] ?? '');
    if (!$row) { json_out(['ok' => false, 'error' => tr('php_code_bad', $lang)]); }
    json_out(['ok' => true, 'code' => $row['code'], 'kind' => $row['kind'], 'pct' => $row['pct']]);
} catch (Throwable $e) {
    error_log('code.php: ' . $e->getMessage());
    json_out(['ok' => false, 'error' => tr('php_error', $lang)], 500);
}
