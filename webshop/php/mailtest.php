<?php
// Diagnose voor bevestigingsmails. Open: /mailtest.php?k=<WEBSHOP_DATA_KEY>&to=<jouw e-mailadres>
// Stuurt een testmail en toont het resultaat, de PHP-mailinstellingen en de laatste regels van mail.log.
require __DIR__ . '/lib.php';
header('Content-Type: text/plain; charset=utf-8');
header('Cache-Control: no-store');
$key = substr(cfg()['data_dir'], 2);
if (!hash_equals($key, (string)($_GET['k'] ?? ''))) { http_response_code(404); echo "niet gevonden\n"; exit; }
echo "mail() beschikbaar: " . (function_exists('mail') ? 'ja' : 'NEE (uitgeschakeld door de hoster)') . "\n";
echo "disable_functions: " . (ini_get('disable_functions') ?: '(leeg)') . "\n";
echo "sendmail_path: " . (ini_get('sendmail_path') ?: '(leeg)') . "\n";
echo "contactadres (WEBSHOP_CONTACT_EMAIL): " . (cfg()['email'] ?: '(LEEG)') . "\n";
$to = (string)($_GET['to'] ?? '');
if ($to !== '') {
    $ok = send_mail($to, 'Testmail webshop', "Dit is een testmail van de webshop.\nAls je dit leest, werkt het verzenden.\n");
    echo "testmail aan $to: " . ($ok ? 'door PHP aangenomen (check ook je spam)' : 'MISLUKT') . "\n";
}
echo "\n--- mail.log (laatste 20 regels) ---\n";
$f = data_path('mail.log');
echo is_file($f) ? implode('', array_slice(file($f), -20)) : "(nog geen log)\n";
echo "\n--- laatste 5 bestellingen ---\n";
foreach (db_shop()->query('SELECT id,status,mailed,email,created_at FROM orders ORDER BY id DESC LIMIT 5')->fetchAll() as $r) {
    echo "#{$r['id']} {$r['status']} mailed={$r['mailed']} {$r['email']} " . date('Y-m-d H:i', (int)$r['created_at']) . "\n";
}
